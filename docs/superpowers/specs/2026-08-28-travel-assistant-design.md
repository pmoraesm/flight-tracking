# Travel assistant design

## Context

The flight tracker currently follows one trip: a fixed destination, a fixed
origins list, and one date range, all defined in `config.yaml`. This design
turns it into a travel assistant that tracks several concurrent trip
requests, including fuzzy ones ("a warm beach getaway in December"), and
alerts on good deals inferred from each trip's own price history plus a
one-time research step done when the trip is created.

The relay (`claude-relay-server`, running on the same VPS as a Docker
container attached to the default bridge network) is the reasoning engine
behind every natural-language step: resolving a fuzzy destination, parsing
a date description, and interpreting `/set-config` edits.

## Data model

A new **`trips`** table in the existing SQLite database (`prices.db`):

```sql
CREATE TABLE trips (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    description             TEXT NOT NULL,
    destinations            TEXT NOT NULL,  -- JSON array of IATA codes
    origins                 TEXT,           -- JSON array of IATA codes, NULL = use config.yaml default
    ideal_date              TEXT NOT NULL,
    ideal_return_date       TEXT NOT NULL,
    departure_range_before  INTEGER NOT NULL,
    departure_range_after   INTEGER NOT NULL,
    return_range_before     INTEGER NOT NULL,
    return_range_after      INTEGER NOT NULL,
    seat                    TEXT,           -- NULL = use config.yaml default
    passengers              TEXT,           -- JSON, NULL = use config.yaml default
    max_duration_hours      INTEGER,        -- NULL = use config.yaml default
    results_per_query       INTEGER,        -- NULL = use config.yaml default
    baseline_price_estimate REAL,           -- from the research step, may be NULL
    status                  TEXT NOT NULL DEFAULT 'active',  -- active | expired | cancelled
    created_at              TEXT NOT NULL
);
```

The `prices` table gains a `trip_id INTEGER` column (nullable, for rows
written before this change), so historical prices join to the trip that
produced them.

`config.yaml` shrinks to shared defaults only: `origins`, `seat`,
`passengers`, `max_duration_hours`, `results_per_query`,
`interval_minutes`. The per-trip fields it holds today (`destination`,
`ideal_date`, `ideal_return_date`, the four range fields,
`price_alert_threshold`) are removed — a threshold is now computed per
trip, not configured, and destinations live in `trips`.

**Migration**: on first run after this change, if `config.yaml` still has
the old per-trip fields, they become trip #1 (`description: "Migrated
from config.yaml"`, `baseline_price_estimate: NULL`, `status: active`),
and those fields are then stripped from `config.yaml`.

## The assistant persona

A short, shared system prompt establishes the relay's role, reused across
every command that calls it:

```
You are the reasoning backend for a personal flight-price-tracking
assistant. Your job is narrow: turn a user's plain-language request into
a structured JSON answer, in the exact shape the current request
specifies. Do not run shell commands, browse files, or make any change
outside of what is asked — only answer with JSON. Use IATA airport codes
for airports, translating city, region, or "kind of destination"
descriptions yourself from your own knowledge. If a request is unclear or
names something that doesn't exist, ask a short clarifying question in a
clarification_needed field instead of guessing. Keep responses terse — no
chit-chat, no explanation beyond what's asked.
```

Each command appends its own task-specific instructions and JSON schema
below this shared block (e.g. `/set-config`'s key-value schema, or
`/new-trip`'s destination/date-resolution schema).

### Session reuse within one conversation

Sending this persona on every single relay call is wasteful once a
conversation runs multiple turns (a clarification loop). The relay
supports resuming a session via `session_id`. The fix:

- The persona (`system_prompt`) is sent only on a conversation's **first**
  relay call.
- The `session_id` returned in that call's `ResultMessage` is stored
  alongside the existing pending-conversation state (`_pending_config`
  becomes a dict of `chat_id -> session_id`, and the new trip-intake flow
  does the same).
- Every follow-up turn in that same conversation passes `session_id`
  instead of `system_prompt`.
- A new, unrelated command (a fresh `/set-config` later, a different
  `/new-trip`) starts a new session — this is a genuinely new task, not a
  repeated cost within one exchange.

This retrofits `/set-config`, which today re-sends its full context on
every clarification turn — that becomes session-scoped too, for
consistency.

## Trip intake — `/new-trip`

1. User sends `/new-trip`. Bot replies: "Where and when do you want to
   go? Describe it in plain language — a place, a kind of destination,
   specific dates, or a loose period."
2. User's reply goes to the relay (persona + a `/new-trip`-specific
   schema: `destinations`, `origins` (optional override), `ideal_date`,
   `ideal_return_date`, the four ranges, `baseline_price_estimate`,
   `clarification_needed`).
3. If `clarification_needed` is set, the bot asks it and stays pending
   (resuming the same `session_id` on the next reply).
4. Otherwise, the bot shows the resolved proposal and asks for
   confirmation: *"Destinations: BKK, DPS, HKT. Dates: Dec 5–19 ±3 days.
   Est. fare: ~€650. Reply 'yes' to start tracking, or describe what to
   change."*
5. A reply of "yes" (case-insensitive, trimmed) inserts the trip
   (`status: active`) and ends the conversation. Any other reply is sent
   back to the relay (same `session_id`) as a revision request, producing
   an updated proposal.
6. `/cancel` exits the flow at any point, same as it does today for
   `/set-config`.

## Search loop

`tracker.py`'s `search_flights` takes a merged trip record instead of the
global config, and gains one more nesting level:
origins × **destinations** × depart dates × return dates. The per-pair
search logic, URL building, and result parsing are unchanged.

`main.py`'s `run_check()`:

1. Loads `config.yaml` (shared defaults) once.
2. Calls a new `trips.get_active_trips()` (module `trips.py`), which also
   auto-expires any trip whose `ideal_return_date + return_range_after`
   has passed (`status: expired`).
3. For each active trip, merges trip fields over the shared defaults
   (trip value wins when present, else the default) and runs
   `search_flights(merged_trip)`.
4. Each result is stored via `storage.write_results(combo, trip_id)` and
   evaluated by `deals.is_good_deal`.
5. A search failure for one trip is logged and skipped; other trips still
   run that cycle.

## Deal detection — new `deals.py`

`is_good_deal(trip_id, price_value) -> bool`, called after the current
result is already written to `prices` — so its own price counts toward
its trip's history when computing the percentile:

- Fetch all historical `price_value`s for that `trip_id` from `prices`
  (this includes the price being evaluated).
- Fewer than 5 samples: compare against
  `trip.baseline_price_estimate * 0.85` (15% under the research
  estimate). No baseline either → return `False` (no basis to alert on).
- 5 or more samples: compare against the 20th percentile of that trip's
  own price history (`statistics.quantiles`, stdlib, no new dependency).
- `price_value <= threshold` → good deal.

Every result also carries `current_price_level`, already returned by
`fast-flights` but currently unused. Alerts include it as a supporting
note (e.g. "Google also rates this as low"), never as a gate — the
percentile/baseline check alone decides whether to alert.

Per the earlier decision, an alert fires every cycle a price still
qualifies as a good deal, not only on a new record low.

## Telegram interface

- **New**: `/new-trip` (above). `/trips` — lists active trips: id, short
  description, date range, cheapest price seen so far.
  `/cancel-trip <id>` — sets `status: cancelled`, where `<id>` is the
  `trips.id` value shown by `/trips`.
- **Changed**: `/set-config` now edits only the shared defaults in
  `config.yaml`; its relay context (current-config dump, valid-key list)
  shrinks to match. `/help` lists all current commands.
- **Unchanged**: `/get`, `/cancel`.

## Notifications

`notifier.py`'s alert path now takes results already filtered by
`deals.is_good_deal`, and includes the trip's `description` in the
message so concurrent trips stay distinguishable. The per-cycle summary
becomes one line per active trip (its single cheapest fare across all
destinations), not one line per destination, to keep message volume
sane as the number of tracked trips grows.

## Error handling

- A relay failure during `/new-trip` or `/set-config` reports the error
  to the user and stays pending, matching today's `/set-config` behavior.
- A trip with a malformed date or a failing search is logged and skipped;
  it does not stop other trips from being checked that cycle.
- A trip with no `baseline_price_estimate` and fewer than 5 historical
  prices produces no alerts until one of those conditions is met — this
  is expected behavior, not an error.
- If resuming a `session_id` fails (e.g. the relay's session expired
  between turns), fall back to a fresh session with the full persona
  resent, rather than failing the conversation.

## Testing

- Unit tests for `deals.is_good_deal`: cold-start fallback, percentile
  path, the boundary at exactly 5 samples, and the no-baseline/no-history
  case.
- Unit tests for the trip/default merge logic (trip value present vs.
  falling back to `config.yaml`).
- Unit test for the `config.yaml` → trip #1 migration, against a scratch
  copy, following the pattern already used to test `storage.py` and
  `telegram_commands.py` in this project.
- Mocked-relay tests for `/new-trip` (proposal, confirmation, revision,
  cancel) and the session-reuse behavior of both `/new-trip` and
  `/set-config`, following the pattern already used to test
  `/set-config`'s conversation flow.
- Full live verification (real search, real relay) can only happen on the
  VPS, same limitation noted for the current `/set-config` feature.
