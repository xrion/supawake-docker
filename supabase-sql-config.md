# Supabase keepalive table

Run this once per project in the Supabase dashboard, under **SQL Editor**.

```sql
create table if not exists public.keepalive (
  id         smallint primary key default 1,
  pinged_at  timestamptz not null default now()
);

insert into public.keepalive (id)
values (1)
on conflict do nothing;

alter table public.keepalive enable row level security;

-- Read access for the anonymous role only. No insert, update or delete.
drop policy if exists "anon can read keepalive" on public.keepalive;

create policy "anon can read keepalive"
on public.keepalive
for select
to anon
using (true);

-- Supabase grants these by default on the public schema, but restore them
-- explicitly in case they were revoked - without the grant the policy alone
-- is not enough and the ping comes back HTTP 403.
grant usage on schema public to anon;
grant select on public.keepalive to anon;

-- PostgREST answers from an in-memory schema cache. Without this reload a
-- table created seconds ago still returns 404 / PGRST205.
notify pgrst, 'reload schema';
```

## Checking it by hand

```bash
curl -i -H "apikey: <your-anon-or-publishable-key>" \
  "https://<ref>.supabase.co/rest/v1/keepalive?select=*&limit=1"
```

Expected: **200** or **206**, with a body of `[{...}]`. PostgREST answers a
range-limited read with `206 Partial Content` whenever the page it returned may
not be the whole collection, so 206 is a success, not an error.

| Response | Meaning |
|---|---|
| `200` / `206` | Working. The query reached Postgres. |
| `401` | Key rejected. Wrong project, revoked key, or stray quotes/newline in the value. |
| `403` | Table exists but RLS denies `anon` the select. Re-run the policy and grants above. |
| `404` + `PGRST205` | Table missing from the schema cache. Create it, then `notify pgrst, 'reload schema';`. |
