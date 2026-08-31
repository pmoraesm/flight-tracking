# Relay price-analysis design

## Context

The PostgreSQL migration (`docs/superpowers/specs/2026-08-31-postgres-migration-design.md`)
moved trip and price storage off a local SQLite file specifically to unblock
this feature, which it deliberately scoped out as "designed separately,
once this lands." This spec is that separate design.

The goal is to let the user ask the Telegram bot open-ended questions about
its price history — trends, comparisons across trips, whatever comes up —
instead of the fixed `/trips` summary. The relay (`claude-relay-server`)
already exists for turning plain language into structured answers, so the
question is how to give it real data to reason over.

Investigation during this design turned up two facts about the relay that
shape the whole architecture:

- `claude-relay-server`'s README states it "sends \[prompts\] to Claude Code
  through the Claude Agent SDK." Its `relay.py` builds a `ClaudeAgentOptions`
  session with no `allowed_tools` restriction, and `config.py` defaults
  `CLAUDE_RELAY_PERMISSION_MODE` to `bypassPermissions` — so the relay
  already has real Bash tool access on the VPS host today. It is restricted
  only by its working directory (`cwd` stays under a workspace root) and by
  what its system prompt tells it not to do. The current persona prompt
  explicitly says "Do not run shell commands... only answer with JSON" —
  a prompt-level restriction, not a tool-level one.
- The relay process runs directly on the VPS host, outside any container.
  `returnhub-postgres-1` publishes no port to the host and is only
  DNS-resolvable from inside its own `returnhub_default` Docker network —
  confirmed live: the bare host cannot resolve `returnhub-postgres-1` or
  reach port 5432 on it at all.

## Goals

- The user can ask about any aspect of the price history conversationally —
  trends, comparisons, whatever's on their mind — and get an answer backed
  by real queries against the data, not a fixed summary.
- SQL access is scoped to a distinct analysis conversation, not available to
  every router turn — trip proposals, config edits, and everything else the
  relay already handles keep today's "no shell commands" persona untouched.
- The relay never sees a writable credential. Even if the model tried
  something destructive, the database role it connects with rejects it.
- No credential appears in a prompt or a relay conversation transcript.

## Architecture

### Reachability: publish the port to loopback only

`returnhub-postgres-1`'s Postgres port publishes to `127.0.0.1:5432` on the
VPS host — not `0.0.0.0`. This keeps it unreachable from outside the VPS
(preserving its current "not exposed" property) while making it reachable
from local host processes, including the relay's Bash tool. This is an
infrastructure change to `returnhub-postgres-1`'s own `docker run`/compose
config (outside this repo), applied once by hand during rollout.

### A dedicated read-only role

A new PostgreSQL role, `flight_tracker_readonly`, gets `GRANT CONNECT` on
the `flight_tracker` database and `GRANT SELECT` on `trips` and `prices`
only — no `INSERT`/`UPDATE`/`DELETE`/DDL privileges at all, and no access
to the separate `returnhub` database in the same Postgres instance. This is
the actual security boundary: privilege, not prompt wording. Created once
by hand during rollout, alongside the existing `flight_tracker` role from
the Postgres migration.

### Credential delivery: `.pgpass`, never the prompt

A `~/.pgpass` file (mode `0600`) in the relay process's home directory
(`paulo`, since `claude-relay-server` runs as that user) holds the
read-only role's connection line:
```
127.0.0.1:5432:flight_tracker:flight_tracker_readonly:<password>
```
`psql` reads this automatically. The analysis persona's system prompt only
ever needs to say `psql -h 127.0.0.1 -U flight_tracker_readonly -d
flight_tracker` — the password never appears in a prompt, a system prompt,
or a stored conversation. Created once by hand during rollout.

### A second, separate relay session per chat

The general router (`_ROUTER_TASK_PROMPT`, unchanged persona) gains one new
action, `"analyze"`, in its JSON contract:

```
"action": "propose_trip | revise_trip | edit_trip | cancel_trip |
list_trips | list_trip_history | set_config | show_config | help |
answer | analyze | unclear"
```

Its handling instruction, added to `_ROUTER_TASK_PROMPT`:

> Use `"analyze"` when the user is asking a question about price history,
> trends, or any data-driven analysis of trips — anything that needs real
> historical numbers to answer well, not just the active trips list already
> given to you. Put a self-contained restatement of the question in
> `"query"`, resolving any references ("that trip", "the Sao Paulo one")
> against the active trips list above so it can be understood without this
> conversation's context.

`_execute_action`'s `analyze` branch does not answer directly — it starts
(or resumes) a **second relay session**, tracked as
`state["analysis_session_id"]` in the same per-chat `_pending` dict that
already holds `session_id`, `trip_draft`, and `trip_draft_id`. This session
uses a distinct system prompt (below) sent only on its own first turn, via
the same `_relay_turn`-style first-turn/resume logic already used for the
general router — `_relay_turn`'s existing signature (`followup_prompt,
full_prompt, task_prompt, session_id`) already generalizes to a second
session with no changes needed to that function itself.

Every incoming message still goes through the general router first,
unchanged. If it classifies as `analyze` again, the existing analysis
session resumes with the new `query`. If the user asks about something
else, the general router routes there instead, and the analysis session
simply stays dormant — no explicit "enter/exit analysis mode" bookkeeping,
the same way the general `session_id` already persists and gets reused
without a mode flag.

### The analysis persona

A new system prompt, sent only on an analysis session's first turn:

> You are the data-analysis backend for a personal flight-price-tracking
> assistant. You have read-only access to its PostgreSQL database via
> `psql -h 127.0.0.1 -U flight_tracker_readonly -d flight_tracker` (no
> password needed — already configured). The schema:
>
> `trips(id, description, destinations JSONB, origins JSONB, ideal_date
> DATE, ideal_return_date DATE, departure_range_before INT,
> departure_range_after INT, return_range_before INT, return_range_after
> INT, seat TEXT, passengers JSONB, max_duration_hours INT,
> results_per_query INT, baseline_price_estimate DOUBLE PRECISION, status
> TEXT, created_at TIMESTAMPTZ)`
>
> `prices(id, checked_at TIMESTAMPTZ, origin TEXT, destination TEXT,
> depart_date DATE, return_date DATE, airline TEXT, departure TEXT,
> arrival TEXT, duration TEXT, stops INT, price TEXT, price_value DOUBLE
> PRECISION, is_best BOOLEAN, trip_id INT REFERENCES trips)`
>
> Answer the user's question by querying this data. Use `SELECT` only —
> you have no write access, and none is needed. If the database is
> unreachable or a query fails, say so plainly in your reply rather than
> retrying indefinitely. Respond with nothing but a single JSON object:
> `{"reply": "text"}`.

## Code changes

- `telegram_commands.py`:
  - `_ROUTER_TASK_PROMPT`: add `analyze` to the action enum and its
    handling instruction, as above.
  - New module-level `_ANALYSIS_PERSONA_PROMPT` constant, as above.
  - `_pending[chat_id]` gains `analysis_session_id` (default `None`),
    alongside the existing `session_id`, `trip_draft`, `trip_draft_id`.
  - `_execute_action`: new `analyze` branch — calls `_relay_turn` against
    `state["analysis_session_id"]` with the classified `query`, using
    `_ANALYSIS_PERSONA_PROMPT` as its `task_prompt`, updates
    `state["analysis_session_id"]` from the response, and returns the
    resulting `reply`.
  - `_dispatch`: new `/analyze` command, mirroring `/new-trip`/`/set-config`
    — calls `_clear_pending(chat_id)` the same way those do (which already
    resets `analysis_session_id` to `None` along with everything else in
    `_pending`, since it's one shared dict), starting a fresh analysis
    session rather than resuming a stale one, and prompts "What would you
    like to know about your price history?" `/cancel` already ends an
    in-progress analysis conversation too, for the same reason — no new
    code needed there.
  - `HELP_TEXT`: add a line for `/analyze`.
- No changes to `relay_client.py` — `query()`, `extract_json()`, and
  `extract_result()` are already generic enough for a second session.
- No changes to `storage.py`, `trips.py`, or `deals.py` — this feature reads
  through the relay's own `psql`, not through this codebase's Python.

## Error handling

The SQL runs inside the relay's own agentic loop (its Bash tool), so a bad
query or a connection failure is something that session sees and reacts to
itself — retries, rephrases, or reports it — the same way it already
handles any tool error, bounded by the relay's existing `max_turns=30`
ceiling. No new error-handling logic is needed in this codebase beyond what
already exists: `relay_client.extract_json` already returns `{}` on
malformed output, and `_execute_action`'s existing fallback ("Sorry, I
didn't understand that...") covers a stuck or unproductive analysis turn.

## Testing

- `_execute_action`'s new `analyze` branch and the `/analyze` command get
  real pytest coverage, mocking `relay_client.query` exactly the way every
  existing router action is tested — asserting the second session starts
  with `_ANALYSIS_PERSONA_PROMPT` as `system_prompt` on its first turn, and
  resumes via `analysis_session_id` on later turns, mirroring the existing
  `test_revise_trip_resumes_session_without_resending_persona`-style tests.
- Whether the model actually writes correct SQL and gives good analysis is
  **not** something automated tests can verify — that requires a real
  conversation against the live relay, the same manual verification already
  called for after `/new-trip` in the travel-assistant plan's rollout
  checklist.
- Infrastructure correctness — the read-only role truly cannot write, the
  port really is loopback-only (not `0.0.0.0`), `.pgpass` permissions are
  `0600` — is verified once by hand during rollout, not by pytest.

## Out of scope

- Any write access for the relay, ever.
- Exposing `returnhub-postgres-1`'s port beyond `127.0.0.1`.
- Any change to the `returnhub` database or its other services.
- Multi-user access controls — this remains a single-user personal bot, so
  the read-only role's purpose is containing what an LLM can accidentally
  do, not restricting what the bot's one user can see.
- A dedicated UI for browsing price history outside the conversational
  interface.
