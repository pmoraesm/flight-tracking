# Conversational router design

## Context

The bot has two commands that start a short conversation: `/new-trip`
and `/set-config`. Each command sends every reply to the relay through
its own fixed schema. The schema only handles two cases: a valid
revision, or a request for clarification. A user question during the
conversation does not fit either case. The bot then shows a generic
failure message instead of an answer.

Example: a user starts `/new-trip`, gets a trip proposal, then asks
"What are the departure airports?" The relay cannot answer a question
under the current schema, so the bot replies "I couldn't work out a
full trip from that."

This design adds one router that understands free-language messages
and picks the right action itself. Slash commands keep working exactly
as they do today. The router only handles messages that are not a
literal `/command`.

## Goals

- A message inside an active conversation can ask a question, request
  a change, or switch to a different task, and the bot responds
  correctly in each case.
- A free-language message with no active conversation still starts the
  matching action, without the user having to type the slash command
  first.
- Slash commands stay unchanged: same names, same immediate behavior,
  no relay call.
- A trip proposal always shows the departure airports it will use, so
  the most common question never needs to be asked.

## Architecture and message flow

`_dispatch` keeps its exact `/command` matches unchanged. Every other
message goes through one new function, `_route(chat_id, text)`, which
replaces the two flow-specific handlers (`_handle_config_reply` and
`_handle_new_trip_reply`) and the old plain fallback.

`_route` runs two steps:

1. **Local fast path.** If a trip proposal is pending and the message
   is "yes" (case-insensitive, trimmed), the bot creates the trip
   directly, with no relay call. This is the one action that writes to
   the database, so it stays deterministic and does not depend on the
   relay's judgment.
2. **Router call.** Otherwise, the bot sends one relay call. The
   prompt carries the user's message, the pending trip draft (if any),
   the active trips (id and description), and the current config. The
   relay returns one action. `_route` runs that action through the
   same functions the slash commands already use.

`/new-trip` and `/set-config` still clear pending state and send their
opening question, exactly as today. Every reply after that opening
question goes through `_route`, so it can answer a question, revise
the draft, or switch to an unrelated action, all in one path.

## State model

`_pending_config` and `_pending_trip` merge into one dict,
`_pending: dict[chat_id, dict]`, holding:

- `session_id` — the chat's ongoing relay session. One session per
  chat, not one per flow, since the router now handles every topic.
  The bot sends the persona and the router prompt only on the first
  call for a chat, or after a failed resume — the same fallback
  `_relay_turn` already uses.
- `trip_draft` — the last proposed, unconfirmed trip, or `None`. This
  is the only state the app trusts over the relay's own memory, since
  it gates the local "yes" fast path.

There is no field that locks the conversation to one topic. The router
must stay free to switch topics between turns — a fixed "current flow"
field would recreate the exact problem this design fixes.

Every router call rebuilds a fresh context block (current config,
active trips, `trip_draft`) and sends it with the message. The bot
does not trust the relay's own memory for facts that can go stale
between turns. The relay's session carries conversational tone and
history only, not ground truth.

## Router contract

One task prompt lists the available actions and asks for a single JSON
object in reply:

```
{
  "action": "propose_trip | revise_trip | cancel_trip | list_trips |
             set_config | show_config | help | answer | unclear",
  "trip": { destinations, origins?, ideal_date, ideal_return_date,
            departure_range_before, departure_range_after,
            return_range_before, return_range_after,
            baseline_price_estimate },      // propose_trip, revise_trip
  "trip_id": int,                           // cancel_trip
  "config_edits": { "sets": [...], "add_origins": [...],
                     "remove_origins": [...] },   // set_config
  "reply": "text"                           // answer, unclear
}
```

`_route` reads `action` and calls the matching existing function:

- `propose_trip` / `revise_trip` — update `trip_draft` and show the
  proposal.
- `cancel_trip` — call `trips.cancel_trip(trip_id)`.
- `list_trips` — call `_handle_trips_list()`.
- `set_config` — call `_apply_changes()` then `_save_config()`.
- `show_config` — call `_format_config()`.
- `help` — return `HELP_TEXT`.
- `answer` / `unclear` — return `reply` as plain text, and keep
  `trip_draft` unchanged.

This retires `_CONFIG_TASK_PROMPT`, `_NEW_TRIP_TASK_PROMPT`,
`_parse_config_request`, `_parse_trip_request`,
`_handle_config_reply`, and `_handle_new_trip_reply`. Their logic
folds into the router prompt and `_route`.

## Proposal display always shows departure airports

`_format_proposal` currently shows an "Origins" line only when the
relay's proposal sets an override. This hides the airports for the
common case, where the trip uses the shared default from
`config.yaml`.

The new version resolves the airports the same way `merge_with_defaults`
does — the trip's own `origins` when set, otherwise `config.yaml`'s
`origins` list — and always shows them, labeled to make the source
clear (for example, "Departure airports: AMS, BRU, EIN (from shared
settings)" versus "Departure airports: CDG, ORY").

## Error handling

- **Relay failure** (network error, timeout) during routing — the bot
  reports the error and leaves `_pending` state unchanged. The relay
  runs as its own `systemd` service on the VPS host, not inside the
  bot's container, so a real network hop exists between them
  (`relay_client.py` reaches it over the Docker bridge gateway). A
  restart, an overloaded relay, or a reply past the 120-second timeout
  in `relay_client.query` can all raise `requests.RequestException`,
  the same failure path `/set-config` already handles today.
- **Malformed or unrecognized router output** (missing or unknown
  `action`) — the bot replies "Sorry, I didn't understand that — try
  rephrasing, or /help."
- **`cancel_trip` with an ambiguous or unmatched reference** — the
  router already sees the active trip list in its context, so it
  should return `unclear` and ask which trip, instead of guessing.
- **`set_config` with an unknown key** — unchanged. `_apply_changes`
  already reports "Unknown key: X" for each such edit.
- **Destructive actions** — `cancel_trip` through free language
  executes immediately, the same as the explicit `/cancel-trip <id>`
  command today. No extra confirmation step, to keep both paths
  consistent.
- **Trip creation stays local** — a trip is only ever created through
  the "yes" fast path, never as a direct router action. The relay
  cannot write to the database from a misread message.

## Testing

- Mocked-relay tests for `_route`: each `action` value dispatches to
  the correct function.
- The "yes" fast path creates a trip with no relay call.
- The context block sent to the relay carries the current config and
  the active trips.
- An ambiguous `cancel_trip` reference produces a clarifying reply,
  not a guess.
- A malformed or missing `action` falls back to the generic
  "didn't understand" reply.
- `_format_proposal` always shows departure airports, including the
  case where the trip uses the shared config default.
- Session reuse and the fallback to a fresh session on a failed resume
  — reusing the existing `_relay_turn` behavior and its current tests.

## Out of scope

- Changes to `tracker.py`'s search logic, `deals.py`, or
  `notifier.py`'s message formatting, beyond the departure-airports
  line described above.
- A confirmation step for `cancel_trip`, since the explicit command
  does not have one today either.
- Caching or reusing router results across chats — this bot serves one
  chat.
