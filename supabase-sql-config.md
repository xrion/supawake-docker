# Supabase keepalive setup

Run this once per project in the Supabase dashboard, under **SQL Editor**. It is
safe to re-run: every statement is idempotent.

Supabase pauses a free project when it sees too little *user database
activity* over a week — its docs say "a few user requests to the database each
day" keeps it running. A single read every few days is below that bar, so
supawake now sends, several times a day:

1. a **sign-in** as a real Auth user (optional, step 3 below),
2. a **read** of `public.keepalive`,
3. a **write** through the `supawake_heartbeat()` function,
4. a **sign-out**.

## 1. Keepalive table (read)

```sql
create table if not exists public.keepalive (
  id         smallint primary key default 1,
  pinged_at  timestamptz not null default now()
);

insert into public.keepalive (id)
values (1)
on conflict do nothing;

alter table public.keepalive enable row level security;

-- Read access only, for visitors (anon) and signed-in users (authenticated).
-- No insert, update or delete.
drop policy if exists "anon can read keepalive" on public.keepalive;
drop policy if exists "anyone can read keepalive" on public.keepalive;

create policy "anyone can read keepalive"
on public.keepalive
for select
to anon, authenticated
using (true);

-- Supabase grants these by default on the public schema, but restore them
-- explicitly in case they were revoked - without the grant the policy alone
-- is not enough and the ping comes back HTTP 403.
grant usage on schema public to anon, authenticated;
grant select on public.keepalive to anon, authenticated;
```

If you set up the table with an earlier version of this file, re-run the block:
the old policy only covered `anon`, so a ping that signs in as a user (and
therefore runs as `authenticated`) would get `403`.

## 2. Heartbeat function (write)

```sql
-- History of heartbeats, so you can see in the Table Editor that pings land.
create table if not exists public.keepalive_log (
  id         bigint generated always as identity primary key,
  pinged_at  timestamptz not null default now(),
  role       text
);

-- RLS on with no policy: nobody can read or write this table through the API.
-- Only the function below, which runs as its owner, touches it.
alter table public.keepalive_log enable row level security;

create or replace function public.supawake_heartbeat()
returns timestamptz
language plpgsql
security definer
set search_path = ''
as $$
begin
  -- The function is callable with the public key, so cap it at one write a
  -- minute: nobody can use it to flood the table.
  if exists (
    select 1 from public.keepalive_log
    where pinged_at > now() - interval '1 minute'
  ) then
    return now();
  end if;

  update public.keepalive set pinged_at = now() where id = 1;

  insert into public.keepalive_log (role)
  values (coalesce(auth.role(), 'unknown'));

  -- Keep a month of history, no more.
  delete from public.keepalive_log
  where pinged_at < now() - interval '30 days';

  return now();
end;
$$;

revoke execute on function public.supawake_heartbeat() from public;
grant execute on function public.supawake_heartbeat() to anon, authenticated;

-- PostgREST answers from an in-memory schema cache. Without this reload a
-- table or function created seconds ago still returns 404.
notify pgrst, 'reload schema';
```

Then set `SUPABASE_<n>_RPC=supawake_heartbeat` (or `SUPAWAKE_RPC` for every
project).

## 3. A dedicated user to sign in as (optional, recommended)

Signing in is the traffic a real visitor generates: Supabase Auth writes a
session, a refresh token and `last_sign_in_at` to the database. Use a user that
exists only for this, never a real account:

1. **Authentication → Users → Add user → Create new user**.
2. Email such as `supawake@your-domain.example`, a long random password, and
   tick **Auto Confirm User** (an unconfirmed user cannot sign in).
3. Set `SUPABASE_<n>_EMAIL` and `SUPABASE_<n>_PASSWORD`.

The user gets no more rights than any signed-up user of your app. If your
project has **email sign-in disabled**, this step fails with `400`; the read
and the write still run as `anon`.

## 4. Auto-restore (optional, recommended)

Pings cannot wake a project that is already paused. With a Management API
token, supawake checks each project's status before pinging and asks Supabase
to restore a paused one:

1. <https://supabase.com/dashboard/account/tokens> → **Generate new token**.
2. Set `SUPABASE_ACCESS_TOKEN` to it.

The token can manage every project in your account, so keep it in Coolify's
secrets, not in a file in the repo. The free plan allows two active projects
per organization; a restore beyond that is refused, and supawake prints why.

## Checking it by hand

```bash
# Read
curl -i -H "apikey: <your-anon-or-publishable-key>" \
  "https://<ref>.supabase.co/rest/v1/keepalive?select=*&limit=1"

# Write
curl -i -X POST -H "apikey: <your-anon-or-publishable-key>" \
  -H "Content-Type: application/json" -d '{}' \
  "https://<ref>.supabase.co/rest/v1/rpc/supawake_heartbeat"
```

Then, in the SQL Editor:

```sql
select * from public.keepalive_log order by pinged_at desc limit 20;
```

One row every few hours means the pings reach the database. Rows with role
`authenticated` come from pings that signed in; `anon` from pings that did not.

| Response | Meaning |
|---|---|
| `200` / `206` | Working. The query reached Postgres. |
| `401` | Key rejected. Wrong project, revoked key, or stray quotes/newline in the value. |
| `403` | RLS or grants deny the caller. Re-run step 1 (it now covers `authenticated` too). |
| `404` + `PGRST205` / `PGRST202` | Table / function missing from the schema cache. Create it, then `notify pgrst, 'reload schema';`. |
| `400` on sign-in | Wrong email/password, user not confirmed, or email sign-in disabled. |
