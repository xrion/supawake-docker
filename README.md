# supawake

Keep your free-tier Supabase projects from auto-pausing. `supawake` is a tiny CLI that pings the Supabase REST API on a schedule so your databases stay warm.

Supabase pauses free-tier projects after ~7 days of inactivity. `supawake` prevents that with a single scheduled HTTP request — run it locally, in a cron job, or as a GitHub Action.

## Install

```bash
npm install -g supawake
```

Or run without installing:

```bash
npx supawake ping
```

Requires Node.js 18+ (uses the built-in `fetch`).

## Quick start

```bash
# 1. Add a project (interactive)
supawake add

# 2. Verify it's reachable
supawake status

# 3. Ping once
supawake ping

# 4. Or run continuously on a schedule
supawake start --interval "0 0 */3 * *"
```

## Commands

| Command | Description |
|---|---|
| `supawake add` | Interactively add a Supabase project (name, URL, anon key, keepalive table) |
| `supawake remove <name>` | Remove a project by name |
| `supawake list` | List all configured projects |
| `supawake ping` | Ping all projects once and exit |
| `supawake start [--interval <cron>]` | Continuously ping projects on a cron schedule |
| `supawake status` | Check which projects are currently reachable |

### `supawake start`

Starts a long-running process that pings every project according to the given cron expression. Defaults to the value in `settings.defaultInterval` (every 3 days at midnight: `0 0 */3 * *`).

```bash
supawake start                         # use default schedule from config
supawake start -i "*/30 * * * *"       # every 30 minutes
supawake start -i "0 */12 * * *"       # every 12 hours
```

## How pings work

For each project with a `table` configured, `supawake` sends an HTTP `GET` to:

```
https://<your-ref>.supabase.co/rest/v1/<table>?select=*&limit=1
```

…with your key in the `apikey` header (legacy anon JWTs are additionally sent as `Authorization: Bearer …`; see [API keys](#api-keys)). Any **2xx** response means the project is alive. Anything else is reported as a failure. An empty result (`[]`) still counts as success — the query ran, which is the whole point.

Note that a healthy read here commonly answers **206 Partial Content**, not 200: PostgREST returns 206 for a range-limited query (`limit=1`) whenever the page it returned may not be the whole collection. Treating only 200 as success reports a perfectly healthy database as a failure.

**Why a table read specifically.** Supabase pauses free-tier projects based on *database* inactivity. This request goes through PostgREST to Postgres, which evaluates your row-level security policies inside the database — real activity that resets the timer. Endpoints like `/auth/v1/health` return a 200 without ever querying Postgres, so pinging them produces a green check while the database still pauses.

The table must actually exist. A nonexistent name returns `PGRST205` straight from PostgREST's in-memory schema cache without reaching the database.

If a project has **no** `table` set, supawake falls back to `GET /auth/v1/health`. That confirms the project responds but will **not** prevent auto-pause, and every ping prints a warning saying so.

The anon key is safe for this purpose — it's the same key you ship in client apps. Row-level security still protects your data.

### Setting up a keepalive table

If you'd rather not expose an existing table, create a dedicated one. Run this in the Supabase dashboard under **SQL Editor**, once per project:

```sql
create table if not exists public.keepalive (
  id         smallint primary key default 1,
  pinged_at  timestamptz not null default now()
);

insert into public.keepalive (id) values (1) on conflict do nothing;

alter table public.keepalive enable row level security;

create policy "anon can read keepalive"
  on public.keepalive
  for select
  to anon
  using (true);

-- Supabase grants these by default, but restore them explicitly in case they
-- were revoked: without the grant the policy alone still yields HTTP 403.
grant usage on schema public to anon;
grant select on public.keepalive to anon;

-- PostgREST answers from an in-memory schema cache; without this reload a
-- table created seconds ago still returns 404 / PGRST205.
notify pgrst, 'reload schema';
```

It holds one meaningless row and grants anon `select` only — no insert, update, or delete.

## Retries

A single DNS hiccup or cold edge node shouldn't page you. Each ping is retried up to twice (after 500 ms, then 2 s) before being reported as a failure. Retries apply to network errors, timeouts, and `5xx` responses. Authentication failures like `401` are **not** retried — those never fix themselves. When a ping needed more than one attempt, the count is shown in the output.

## Configuration

Configuration lives at `~/.config/supawake/config.json`:

```json
{
  "projects": [
    {
      "name": "anyigba",
      "url": "https://xyz.supabase.co",
      "anonKey": "eyJ...",
      "table": "keepalive"
    }
  ],
  "settings": {
    "defaultInterval": "0 0 */3 * *",
    "notifications": {
      "enabled": false,
      "webhookUrl": ""
    }
  }
}
```

### Environment variables (Docker / Coolify)

When `SUPABASE_1_URL` and `SUPABASE_1_KEY` are present, supawake reads its
projects from the environment instead of the config file — no volume needed.
Number the projects from 1 upwards:

```bash
SUPABASE_1_NAME=discoursparfait          # optional, defaults to supabase-1
SUPABASE_1_URL=https://xxxxx.supabase.co
SUPABASE_1_KEY=eyJ...                    # anon public key, or sb_publishable_...
SUPABASE_1_TABLE=keepalive               # anon-readable table to select from

SUPABASE_2_NAME=project2
SUPABASE_2_URL=https://yyyyy.supabase.co
SUPABASE_2_KEY=eyJ...
SUPABASE_2_TABLE=keepalive
```

| Variable | Description |
|---|---|
| `SUPABASE_<n>_NAME` | Label shown in the output. Defaults to `supabase-<n>` |
| `SUPABASE_<n>_URL` | Project URL. **Required** |
| `SUPABASE_<n>_KEY` | Anon public key or publishable key. **Required** |
| `SUPABASE_<n>_TABLE` | Table to read on each ping. Without it the ping falls back to the auth health check and **will not** prevent auto-pause |
| `SUPAWAKE_TABLE` | Table for every project that has no `SUPABASE_<n>_TABLE` of its own |
| `SUPAWAKE_INTERVAL` | Cron schedule used by `supawake start`. Defaults to `0 0 */3 * *` |
| `SUPAWAKE_WEBHOOK_URL` | Slack-compatible webhook posted to when a ping fails. Setting it enables notifications |

**`SUPABASE_<n>_TABLE` is what keeps the database awake.** Omit it and every
ping stops at the auth endpoint, which answers `200` without ever reaching
Postgres — the run looks green while the project still pauses. Create the
[keepalive table](#setting-up-a-keepalive-table) and point this at it.

Values are trimmed, and a pair of surrounding quotes is stripped, because
container platforms often store them that way. A key carrying a stray quote or
newline is rejected by Supabase with a `401` that looks exactly like a wrong
key.

### Running in Docker / Coolify

```bash
docker compose up -d --build
```

`docker-compose.yml` in this repository runs `supawake start`, which stays alive
and pings on `SUPAWAKE_INTERVAL`. Provide the `SUPABASE_*` variables through
Coolify's environment settings or a local `.env`.

Checklist for a deployment that actually keeps a database awake:

1. The [keepalive table](#setting-up-a-keepalive-table) exists in every project,
   with the RLS select policy and the grants — see `supabase-sql-config.md`.
2. `SUPABASE_<n>_URL` and `SUPABASE_<n>_KEY` are set, numbered from 1 with no
   gaps in the pairs. A missing half of a pair aborts startup with a named error.
3. `SUPABASE_<n>_TABLE` (or `SUPAWAKE_TABLE`) is set. **Without it the ping
   never touches Postgres and the project pauses anyway**, even though the run
   looks green.
4. The key is the *anon public* / *publishable* key — never `service_role`.
5. `restart: unless-stopped` is on, so the container survives host restarts.
6. Run `docker compose logs -f supawake` once after deploying: the first ping
   runs immediately at startup, so a misconfiguration shows up in seconds
   rather than three days later.

With no projects configured, `supawake start` now exits with status 1 and a
message. It used to exit 0, which read as success to Docker and Coolify and
produced a silent restart loop.

### API keys

Supabase issues two kinds of key, and they are sent differently:

- **Legacy anon keys** (`eyJ...`) are JWTs, sent in both the `apikey` and
  `Authorization: Bearer` headers.
- **New publishable keys** (`sb_publishable_...`) are opaque, not JWTs. They go
  in `apikey` only — sending one as a bearer token returns `401`.

supawake picks the right form from the key itself, so either kind just works.
Use the *anon public* / *publishable* key, never `service_role`.

### Notifications (optional)

Set `notifications.enabled` to `true` and provide a `webhookUrl` (Slack-compatible) to receive a simple `{ text: "…" }` POST whenever one or more pings fail.

In environment mode, set `SUPAWAKE_WEBHOOK_URL` — its presence enables notifications.

## Running on GitHub Actions

The most reliable way to keep your projects alive is to let GitHub run `supawake ping` on a schedule. Save the following as `.github/workflows/supawake.yml`:

```yaml
name: supawake

on:
  schedule:
    # Every 3 days at 06:00 UTC
    - cron: '0 6 */3 * *'
  workflow_dispatch:

jobs:
  ping:
    runs-on: ubuntu-latest
    steps:
      - name: Set up Node.js
        uses: actions/setup-node@v4
        with:
          node-version: '20'

      - name: Install supawake
        run: npm install -g supawake

      - name: Write config from secret
        run: |
          mkdir -p "$HOME/.config/supawake"
          echo "$SUPAWAKE_CONFIG" > "$HOME/.config/supawake/config.json"
        env:
          SUPAWAKE_CONFIG: ${{ secrets.SUPAWAKE_CONFIG }}

      - name: Ping all projects
        run: supawake ping
```

Then in your repo settings, add a secret called `SUPAWAKE_CONFIG` containing the full JSON contents of your `~/.config/supawake/config.json`.

## Development

```bash
git clone <this repo>
cd supawake
npm install
npm run build
node dist/index.js --help
```

Scripts:

- `npm run build` — compile TypeScript to `dist/`
- `npm run dev` — watch mode
- `npm run lint` — ESLint
- `npm run format` — Prettier

## Troubleshooting

**`✗ FAIL … HTTP 401`** — the key was rejected. Check that it is the project's
*anon public* (or publishable) key and not `service_role` or a key from another
project, and that no quotes or trailing newline crept into the value. supawake
prints the message Supabase returned alongside the status.

**`✓ OK … — auth only, DB not pinged`** — the project has no table configured,
so the ping never reached Postgres and the database will still pause. Set
`SUPABASE_<n>_TABLE` (or `"table"` in the config file) to an anon-readable
table.

**`HTTP 404 … PGRST205`** — the table name does not exist. PostgREST answers
from its schema cache without touching the database, so this never counts as
activity. Create the table and confirm the name.

**`HTTP 403`** — the table exists but row-level security denies `anon` the
`select`. Add the policy *and* the grants from
[Setting up a keepalive table](#setting-up-a-keepalive-table).

**`HTTP 206`** — this is a success, not a failure. PostgREST answers a
range-limited read with `206 Partial Content`. supawake accepts any 2xx.

**The container restarts in a loop with no output** — no projects were found in
the environment. Check that the variables are named `SUPABASE_1_URL` /
`SUPABASE_1_KEY` exactly, numbered from 1, and that they are attached to the
running service rather than only to the build step.

**Everything is green but the project still paused** — the pings are hitting
`/auth/v1/health`, which never reaches Postgres. Set `SUPABASE_<n>_TABLE`. The
output flags this with `— auth only, DB not pinged`.

## License

MIT
