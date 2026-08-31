# PostgreSQL migration design

## Context

The flight tracker stores trips and price history in a local SQLite file
(`prices.db`), written by the scheduler and read by the Telegram bot in
the same container. This has worked well, but it blocks a planned
feature: letting the Claude Code relay read historical data directly, to
answer questions about price trends and travel patterns.

The relay runs as its own process on the VPS host, outside any
container. It cannot reach the SQLite file today — the file lives inside
a Docker-managed volume under `/var/lib/docker`, a directory locked down
to `root` for good reason, and that boundary should stay intact rather
than be punched through for one feature.

This migration moves storage to PostgreSQL, reusing the `returnhub-postgres-1`
container already running on the VPS (a new, separate database inside
it — not shared tables) rather than a second dedicated Postgres
instance, since the VPS has under 2GB of RAM. A real database server,
reachable over the network with role-based access control, sidesteps the
filesystem permission problem entirely and gives the relay a clean,
narrow way to connect later.

This spec covers the migration only. The relay-facing analysis feature
that motivated it is a separate, later piece of work.

## Goals

- Trips and price history live in PostgreSQL, with no functional change
  to any existing bot command.
- Trip IDs are preserved exactly across the migration — they are already
  visible to the user (`/cancel-trip 4`) and referenced by `prices.trip_id`.
- Local development and tests run against a local Postgres, never the
  VPS instance.
- The data model gets real types where SQLite only had text: dates,
  timestamps, booleans, and structured JSON — directly useful for the
  analysis feature this unblocks, not speculative polish.

## Architecture

- **Connection library:** `psycopg` (psycopg3), used with raw SQL — the
  same style `storage.py`/`trips.py`/`deals.py` already use with
  `sqlite3`. No ORM.
- **VPS database:** a new role and database inside `returnhub-postgres-1`,
  created once with that container's existing superuser (never stored in
  this repo). Only the new role's own, freshly generated password goes
  into flight-tracker's `.env`, as `DATABASE_URL` — gitignored, the same
  as `TELEGRAM_BOT_TOKEN` already is.
- **Local database:** a new `docker-compose` service, `postgres:16-alpine`,
  for development and tests only, entirely separate from the VPS.
- **Connection style unchanged:** each function still opens, uses, and
  closes its own connection, matching the current SQLite code. A
  connection pool is not needed at this bot's traffic level, and adding
  one now would be complexity the migration doesn't require.

## Schema

Both tables move to PostgreSQL with a few deliberate type upgrades,
since they directly serve the analysis feature this migration exists
for:

- `destinations`, `origins`, `passengers` become `JSONB` instead of
  JSON-encoded `TEXT`.
- `checked_at`/`created_at` become `TIMESTAMPTZ`; `ideal_date`,
  `ideal_return_date`, `depart_date`, `return_date` become `DATE`.
- `is_best` becomes `BOOLEAN` instead of `0`/`1`.
- `status` stays `TEXT`, with an added
  `CHECK (status IN ('active', 'cancelled', 'expired'))` constraint.
- `prices.trip_id` gets a real `REFERENCES trips(id)` foreign key — the
  SQLite version only had a plain column and an index.
- IDs use `GENERATED ALWAYS AS IDENTITY`, the modern equivalent of
  SQLite's `AUTOINCREMENT`.

The full schema lives in one new file, `schema.sql`, checked into the
repo. It replaces the "create tables on first connection if missing"
pattern both `storage.py` and `trips.py` currently use
(`_get_connection`'s `if not _initialized: conn.executescript(...)`) —
that pattern fit a local SQLite file; it is not the right shape for a
shared server. `schema.sql` is applied once by the migration script on
the VPS, and automatically by Postgres's own Docker image on first
startup for the local dev/test instance, via its standard
`/docker-entrypoint-initdb.d/` convention.

## Code changes

- `storage.py`, `trips.py`: `sqlite3.connect` becomes `psycopg.connect`;
  `?` placeholders become `%s`; `sqlite3.Row` becomes
  `psycopg.rows.dict_row`, passed at connection time — the same
  dict-like row access as today.
- `trips.py`'s `_row_to_trip` drops its `json.loads` calls for
  `destinations`/`origins`/`passengers` — `psycopg` already hands those
  back as Python lists/dicts from `JSONB` columns.
- `create_trip`/`update_trip` wrap those same fields with
  `psycopg.types.json.Json(...)` on the way in, replacing `json.dumps`.
- `deals.py` keeps connecting independently, the way it already does
  today, rather than going through `storage.py`'s helper — not a change
  this migration makes.
- Configuration moves from `FLIGHT_DB_PATH` to one `DATABASE_URL`
  environment variable, in the same `.env` file as every other secret.

## Networking

`returnhub-postgres-1` sits on its own Docker network
(`returnhub_default`), with no port published to the host — that stays
true after this migration; nothing here should expose it more broadly.

Flight-tracker's container needs to join that network to reach it,
alongside the network it already needs for `claude-relay-server`. That
second part needs care during implementation: flight-tracker currently
relies on being on the real default `bridge` network specifically,
because `relay_client.py` finds the relay through the container's
default gateway address (`172.17.0.1`). Attaching flight-tracker to
`returnhub_default` as an *additional* network is fine; replacing the
real default bridge network with a custom one is not — it would change
the container's default gateway and break relay connectivity. This
needs verifying against the live containers during implementation, not
assumed from reading configuration alone.

## Data migration

A one-time script reads every row from the live `prices.db` — `trips`
first, then `prices` — and inserts each into PostgreSQL with its
existing `id` preserved, since the bot exposes trip IDs directly to the
user and `prices.trip_id` must keep pointing at the right row. After the
insert, the script resets PostgreSQL's identity sequences for both
tables so new rows continue numbering correctly from the highest
existing ID.

This script is run once, by hand, during the cutover — not part of the
app's normal runtime path. The bot is stopped for the short time the
migration takes, to avoid losing a write mid-cutover.

## Error handling

Unlike an embedded SQLite file, a networked Postgres connection can fail
independently of the bot itself — the container restarting, a brief
network blip. `run_check()`'s scheduled price checks already skip a
single trip's failure without stopping the whole cycle
(`docs/superpowers/plans/2026-08-28-travel-assistant.md`'s existing
error-handling contract); a database error during a check should follow
that same pattern — log it and continue to the next scheduled run, not
crash the process. Telegram commands that hit a database error report it
to the user the same way a relay failure already does today, rather than
raising an unhandled exception into `_poll_loop`.

## Testing

- `schema.sql` applies cleanly against a fresh Postgres, both locally
  and via the VPS cutover script.
- Existing `storage.py`/`trips.py`/`deals.py` test suites are ported to
  run against the local `docker-compose` Postgres service, with a
  fixture that truncates both tables before each test rather than
  pointing `DB_PATH` at a fresh temp file — this fits the connection
  style this migration keeps (one connection per function call, no
  pooling), where a shared transaction per test would not.
- A dedicated test for the migration script: given a small SQLite
  fixture database with known IDs and a `prices.trip_id` reference,
  running the script produces matching rows in Postgres with the same
  IDs, and a new trip created afterward gets an ID higher than any
  migrated one.
- Manual verification during rollout: flight-tracker's container can
  resolve and reach `returnhub-postgres-1` on the joined network, and
  still reach `claude-relay-server` through the default bridge gateway
  afterward — both checked directly against the running containers, not
  inferred from config.

## Out of scope

- The relay-facing analysis feature that motivated this migration —
  designed separately, once this lands.
- Connection pooling or an ORM.
- Any change to `deals.py`'s good-deal logic, beyond the type changes
  needed to keep it working against the new schema.
- Multi-user or multi-tenant considerations — this remains a single-user
  personal bot.
