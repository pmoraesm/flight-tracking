# Conversational Router Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `/new-trip` and `/set-config` flows' rigid, schema-only follow-up handling with one relay-backed router that also answers questions, switches tasks mid-conversation, and understands free-language messages with no slash command first.

**Architecture:** `_dispatch` keeps its exact `/command` matches. Every other message goes through one new function, `_route`, which sends one relay call per message with a fresh context block (config, active trips, any pending trip draft) and executes whichever action the relay picks, through the same functions the slash commands already use. A local "yes" fast path confirms a pending trip without a relay call, so trip creation never depends on the model's judgment.

**Tech Stack:** Python 3.11, the existing `relay_client.py` HTTP client, `pytest` with `unittest.mock.patch` for relay mocking (all patterns already in use in this codebase — no new dependencies).

**Spec:** `docs/superpowers/specs/2026-08-29-conversational-router-design.md`

## Global Constraints

- No new pip dependencies. The router reuses `relay_client.py`, `trips.py`, and the existing `_relay_turn` session machinery unchanged.
- The router fully replaces `_CONFIG_TASK_PROMPT`, `_NEW_TRIP_TASK_PROMPT`, `_parse_config_request`, `_parse_trip_request`, `_handle_config_reply`, and `_handle_new_trip_reply` — no dual code paths left behind.
- Trip creation happens only through the local "yes" fast path, never as a direct router action — the relay never writes to the database on a misread.
- `cancel_trip` through free language executes immediately, with no extra confirmation step, matching the explicit `/cancel-trip <id>` command's existing behavior.
- Slash commands (`/new-trip`, `/set-config`, `/trips`, `/cancel-trip`, `/get`, `/cancel`, `/help`) keep their exact current behavior and stay local — no relay call.

---

## Task 1: `_format_proposal` always shows departure airports

**Files:**
- Modify: `telegram_commands.py:180-193` (`_format_proposal`), `telegram_commands.py:289` (its call site in `_handle_new_trip_reply`)
- Test: `tests/test_telegram_commands.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `telegram_commands._format_proposal(proposal: dict, config: dict) -> str` (signature change — now takes `config` as a second argument; used again by Task 2's router).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_telegram_commands.py`:

```python
def test_format_proposal_shows_configured_origins_when_not_overridden():
    proposal = {
        "destinations": ["GRU"],
        "ideal_date": "2026-12-14", "ideal_return_date": "2026-12-24",
        "departure_range_before": 13, "departure_range_after": 17,
        "return_range_before": 13, "return_range_after": 17,
        "baseline_price_estimate": 750,
    }
    config = {"origins": ["AMS", "BRU", "EIN"]}

    reply = tc._format_proposal(proposal, config)

    assert "Departure airports: AMS, BRU, EIN (from shared settings)" in reply


def test_format_proposal_shows_trip_specific_origins_when_set():
    proposal = {
        "destinations": ["GRU"], "origins": ["CDG", "ORY"],
        "ideal_date": "2026-12-14", "ideal_return_date": "2026-12-24",
        "departure_range_before": 13, "departure_range_after": 17,
        "return_range_before": 13, "return_range_after": 17,
    }
    config = {"origins": ["AMS", "BRU", "EIN"]}

    reply = tc._format_proposal(proposal, config)

    assert "Departure airports: CDG, ORY" in reply
    assert "from shared settings" not in reply
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_telegram_commands.py -k format_proposal -v`
Expected: FAIL with `TypeError: _format_proposal() takes 1 positional argument but 2 were given`

- [ ] **Step 3: Modify `telegram_commands.py`**

Replace `_format_proposal`:

```python
def _format_proposal(proposal: dict, config: dict) -> str:
    destinations = ", ".join(proposal["destinations"])
    origins = proposal.get("origins")
    if origins:
        airports_line = f"Departure airports: {', '.join(origins)}"
    else:
        airports_line = (
            f"Departure airports: {', '.join(config.get('origins', []))} "
            "(from shared settings)"
        )
    lines = [
        f"Destinations: {destinations}",
        f"Dates: {proposal['ideal_date']} to {proposal['ideal_return_date']} "
        f"(-{proposal['departure_range_before']}/+{proposal['departure_range_after']}d "
        f"departure, -{proposal['return_range_before']}/+{proposal['return_range_after']}d return)",
        airports_line,
    ]
    if proposal.get("baseline_price_estimate"):
        lines.append(f"Est. fare: ~€{proposal['baseline_price_estimate']:.0f}")
    lines.append("Reply 'yes' to start tracking, or describe what to change.")
    return "\n".join(lines)
```

In `_handle_new_trip_reply`, change the final two lines from:

```python
    state["proposal"] = parsed
    return _format_proposal(parsed)
```

to:

```python
    state["proposal"] = parsed
    return _format_proposal(parsed, _load_config())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_telegram_commands.py -v`
Expected: PASS (all tests, including the 2 new ones)

- [ ] **Step 5: Commit**

```bash
git add telegram_commands.py tests/test_telegram_commands.py
git commit -m "Always show departure airports on a trip proposal"
```

---

## Task 2: Conversational router replacing the `/new-trip` and `/set-config` follow-up flows

**Files:**
- Modify: `telegram_commands.py` (module docstring, imports section unchanged; replace `_CONFIG_TASK_PROMPT`, `_NEW_TRIP_TASK_PROMPT`, `_pending_config`, `_pending_trip`, `_parse_config_request`, `_parse_trip_request`, `_handle_config_reply`, `_handle_new_trip_reply`, `_clear_pending`, `_dispatch`; add `_ROUTER_TASK_PROMPT`, `_pending`, `_build_router_prompt`, `_confirm_trip`, `_execute_action`, `_route`)
- Modify: `tests/test_telegram_commands.py` (full rewrite of the router-facing tests; `_handle_get`/`_handle_trips_list`/`_handle_cancel_trip` tests are untouched)

**Interfaces:**
- Consumes: `relay_client.query()`, `relay_client.extract_json()`, `relay_client.PERSONA_PROMPT`, `telegram_commands._relay_turn()` (all unchanged), `telegram_commands._format_proposal(proposal, config)` (Task 1), `trips.create_trip()`, `trips.cancel_trip()`, `trips.get_active_trips()`.
- Produces: `telegram_commands._pending: dict` (`chat_id -> {"session_id": str | None, "trip_draft": dict | None}`), `telegram_commands._route(chat_id, text: str) -> str` (used directly by `_dispatch`, and by any future command that needs router access).

- [ ] **Step 1: Replace the test file's imports, `setup_function`, and router-facing tests**

Replace the entire contents of `tests/test_telegram_commands.py` with:

```python
import requests
from unittest.mock import patch

import deals
import storage
import trips
import telegram_commands as tc


def setup_function():
    tc._pending.clear()


def _router_env(tmp_path, monkeypatch, origins=("AMS",)):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(trips, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(trips, "_initialized", False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("origins:\n" + "".join(f"  - {o}\n" for o in origins))
    monkeypatch.setattr(tc, "CONFIG_PATH", config_path)


def test_format_proposal_shows_configured_origins_when_not_overridden():
    proposal = {
        "destinations": ["GRU"],
        "ideal_date": "2026-12-14", "ideal_return_date": "2026-12-24",
        "departure_range_before": 13, "departure_range_after": 17,
        "return_range_before": 13, "return_range_after": 17,
        "baseline_price_estimate": 750,
    }
    config = {"origins": ["AMS", "BRU", "EIN"]}

    reply = tc._format_proposal(proposal, config)

    assert "Departure airports: AMS, BRU, EIN (from shared settings)" in reply


def test_format_proposal_shows_trip_specific_origins_when_set():
    proposal = {
        "destinations": ["GRU"], "origins": ["CDG", "ORY"],
        "ideal_date": "2026-12-14", "ideal_return_date": "2026-12-24",
        "departure_range_before": 13, "departure_range_after": 17,
        "return_range_before": 13, "return_range_after": 17,
    }
    config = {"origins": ["AMS", "BRU", "EIN"]}

    reply = tc._format_proposal(proposal, config)

    assert "Departure airports: CDG, ORY" in reply
    assert "from shared settings" not in reply


def test_propose_trip_from_free_text_with_no_pending_state(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch, origins=("AMS", "BRU"))

    chat_id = 2
    with patch("telegram_commands.relay_client.query", return_value={
        "result": (
            '{"action": "propose_trip", "trip": {'
            '"description": "Beach getaway", "destinations": ["BKK", "HKT"], '
            '"ideal_date": "2026-12-05", "ideal_return_date": "2026-12-19", '
            '"departure_range_before": 3, "departure_range_after": 3, '
            '"return_range_before": 3, "return_range_after": 3, '
            '"baseline_price_estimate": 650}}'
        ),
        "session_id": "sess-trip-1",
    }) as mock_query:
        reply = tc._dispatch(chat_id, "somewhere warm in SE Asia in December")

    assert "BKK" in reply and "HKT" in reply
    assert "Departure airports: AMS, BRU (from shared settings)" in reply
    assert "yes" in reply.lower()
    assert tc._pending[chat_id]["trip_draft"]["description"] == "Beach getaway"
    args, kwargs = mock_query.call_args
    assert kwargs["system_prompt"] is not None


def test_propose_trip_missing_range_field_defaults_instead_of_crashing(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    chat_id = 2
    with patch("telegram_commands.relay_client.query", return_value={
        "result": (
            '{"action": "propose_trip", "trip": {'
            '"description": "Beach getaway", "destinations": ["BKK", "HKT"], '
            '"ideal_date": "2026-12-05", "ideal_return_date": "2026-12-19", '
            '"departure_range_after": 3, '
            '"return_range_before": 3, "return_range_after": 3, '
            '"baseline_price_estimate": 650}}'
        ),
        "session_id": "sess-trip-1",
    }):
        reply = tc._dispatch(chat_id, "somewhere warm in SE Asia in December")

    assert not reply.startswith("Error:")
    assert "BKK" in reply and "HKT" in reply
    assert "-3/+3d departure" in reply
    assert tc._pending[chat_id]["trip_draft"]["departure_range_before"] == 3


def test_propose_trip_missing_required_field_shows_generic_message(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    with patch("telegram_commands.relay_client.query", return_value={
        "result": '{"action": "propose_trip", "trip": {"destinations": ["BKK"]}}',
        "session_id": "sess-1",
    }):
        reply = tc._dispatch(2, "somewhere")

    assert "couldn't work out a full trip" in reply


def test_revise_trip_resumes_session_without_resending_persona(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    chat_id = 2
    tc._pending[chat_id] = {
        "session_id": "sess-trip-1",
        "trip_draft": {
            "description": "Beach getaway", "destinations": ["BKK", "HKT"],
            "ideal_date": "2026-12-05", "ideal_return_date": "2026-12-19",
            "departure_range_before": 3, "departure_range_after": 3,
            "return_range_before": 3, "return_range_after": 3,
            "baseline_price_estimate": 650,
        },
    }

    with patch("telegram_commands.relay_client.query", return_value={
        "result": (
            '{"action": "revise_trip", "trip": {'
            '"description": "Beach getaway", "destinations": ["BKK"], '
            '"ideal_date": "2026-12-05", "ideal_return_date": "2026-12-19", '
            '"departure_range_before": 3, "departure_range_after": 3, '
            '"return_range_before": 3, "return_range_after": 3, '
            '"baseline_price_estimate": 650}}'
        ),
        "session_id": "sess-trip-1",
    }) as mock_query:
        reply = tc._dispatch(chat_id, "drop HKT, just BKK")

    assert "BKK" in reply and "HKT" not in reply
    args, kwargs = mock_query.call_args
    assert kwargs["session_id"] == "sess-trip-1"
    assert kwargs.get("system_prompt") is None


def test_yes_confirms_pending_trip_without_calling_relay(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    chat_id = 2
    tc._pending[chat_id] = {
        "session_id": "sess-trip-1",
        "trip_draft": {
            "description": "Beach getaway", "destinations": ["BKK", "HKT"],
            "ideal_date": "2026-12-05", "ideal_return_date": "2026-12-19",
            "departure_range_before": 3, "departure_range_after": 3,
            "return_range_before": 3, "return_range_after": 3,
            "baseline_price_estimate": 650,
        },
    }

    with patch("telegram_commands.relay_client.query") as mock_query:
        reply = tc._dispatch(chat_id, "yes")

    mock_query.assert_not_called()
    assert "Beach getaway" in reply
    assert chat_id not in tc._pending
    active = trips.get_active_trips()
    assert len(active) == 1
    assert active[0]["description"] == "Beach getaway"


def test_cancel_trip_action_cancels_matched_trip(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    trip_id = trips.create_trip(
        description="Rio getaway", destinations=["GIG"],
        ideal_date="2026-12-05", ideal_return_date="2026-12-19",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
    )

    with patch("telegram_commands.relay_client.query", return_value={
        "result": f'{{"action": "cancel_trip", "trip_id": {trip_id}}}',
        "session_id": "sess-1",
    }):
        reply = tc._dispatch(3, "cancel my Rio trip")

    assert f"Trip #{trip_id} cancelled" in reply
    assert trips.get_active_trips() == []


def test_list_trips_action_returns_active_trips(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    trips.create_trip(
        description="Beach getaway", destinations=["BKK"],
        ideal_date="2026-12-05", ideal_return_date="2026-12-19",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
    )

    with patch("telegram_commands.relay_client.query", return_value={
        "result": '{"action": "list_trips"}',
        "session_id": "sess-1",
    }):
        reply = tc._dispatch(3, "what trips am I tracking?")

    assert "Beach getaway" in reply


def test_set_config_action_applies_edit(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)
    tc.CONFIG_PATH.write_text("origins:\n  - AMS\ninterval_minutes: 60\n")

    chat_id = 1
    with patch("telegram_commands.relay_client.query", return_value={
        "result": (
            '{"action": "set_config", "config_edits": '
            '{"sets": [{"key": "interval_minutes", "value": "90"}]}}'
        ),
        "session_id": "sess-1",
    }) as mock_query:
        reply = tc._dispatch(chat_id, "set interval to 90 minutes")

    assert "Set interval\\_minutes to 90" in reply
    args, kwargs = mock_query.call_args
    assert "Shared config" in args[0]
    assert kwargs["system_prompt"] is not None


def test_show_config_action(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    with patch("telegram_commands.relay_client.query", return_value={
        "result": '{"action": "show_config"}',
        "session_id": "sess-1",
    }):
        reply = tc._dispatch(1, "what are my current settings?")

    assert "origins" in reply


def test_help_action_returns_help_text(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    with patch("telegram_commands.relay_client.query", return_value={
        "result": '{"action": "help"}',
        "session_id": "sess-1",
    }):
        reply = tc._dispatch(1, "what can you do?")

    assert reply == tc.HELP_TEXT


def test_answer_action_responds_to_a_question_without_losing_the_draft(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch, origins=("AMS", "BRU"))

    chat_id = 2
    draft = {
        "description": "Beach getaway", "destinations": ["GRU"],
        "ideal_date": "2026-12-14", "ideal_return_date": "2026-12-24",
        "departure_range_before": 13, "departure_range_after": 17,
        "return_range_before": 13, "return_range_after": 17,
        "baseline_price_estimate": 750,
    }
    tc._pending[chat_id] = {"session_id": "sess-trip-1", "trip_draft": draft}

    with patch("telegram_commands.relay_client.query", return_value={
        "result": '{"action": "answer", "reply": "AMS and BRU, from your shared settings."}',
        "session_id": "sess-trip-1",
    }):
        reply = tc._dispatch(chat_id, "What are the departure airports?")

    assert reply == "AMS and BRU, from your shared settings."
    assert tc._pending[chat_id]["trip_draft"] == draft


def test_malformed_router_output_falls_back_to_generic_message(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    with patch("telegram_commands.relay_client.query", return_value={
        "result": '{"action": "not_a_real_action"}',
        "session_id": "sess-1",
    }):
        reply = tc._dispatch(1, "asdkjhasd")

    assert "didn't understand" in reply


def test_router_relay_failure_reports_error(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    with patch("telegram_commands.relay_client.query", side_effect=requests.RequestException("boom")):
        reply = tc._dispatch(1, "anything")

    assert "couldn't process that" in reply


def test_router_falls_back_to_fresh_session_when_resume_fails(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    chat_id = 1
    tc._pending[chat_id] = {"session_id": "stale-session", "trip_draft": None}

    responses = [
        requests.RequestException("session expired"),
        {"result": '{"action": "show_config"}', "session_id": "sess-new"},
    ]

    def fake_query(*args, **kwargs):
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    with patch("telegram_commands.relay_client.query", side_effect=fake_query) as mock_query:
        reply = tc._dispatch(chat_id, "what are my settings?")

    assert "origins" in reply
    assert mock_query.call_count == 2

    first_args, first_kwargs = mock_query.call_args_list[0]
    assert first_kwargs.get("session_id") == "stale-session"
    assert "system_prompt" not in first_kwargs

    second_args, second_kwargs = mock_query.call_args_list[1]
    assert second_kwargs["system_prompt"] is not None


def test_cancel_clears_pending_state():
    chat_id = 1
    tc._pending[chat_id] = {"session_id": "sess-1", "trip_draft": None}

    reply = tc._dispatch(chat_id, "/cancel")

    assert reply == "Cancelled."
    assert chat_id not in tc._pending


def test_new_trip_command_resets_prior_pending_state():
    chat_id = 2
    tc._pending[chat_id] = {"session_id": "some-session", "trip_draft": {"description": "old"}}

    tc._dispatch(chat_id, "/new-trip")

    assert tc._pending[chat_id] == {"session_id": None, "trip_draft": None}


def test_trips_list_shows_no_active_trips_message(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(trips, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(trips, "_initialized", False)

    reply = tc._dispatch(3, "/trips")

    assert reply == "No active trips."


def test_trips_list_shows_cheapest_price(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(trips, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(trips, "_initialized", False)

    trip_id = trips.create_trip(
        description="Beach getaway", destinations=["BKK"],
        ideal_date="2026-12-05", ideal_return_date="2026-12-19",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
    )
    storage.write_results([{
        "origin": "AMS", "destination": "BKK", "depart_date": "2026-12-05",
        "return_date": "2026-12-19", "price_value": 650.0, "price": "€650",
    }], trip_id=trip_id)

    reply = tc._dispatch(3, "/trips")

    assert "Beach getaway" in reply
    assert "€650" in reply


def test_cancel_trip_marks_trip_cancelled(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(trips, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(trips, "_initialized", False)

    trip_id = trips.create_trip(
        description="Beach getaway", destinations=["BKK"],
        ideal_date="2026-12-05", ideal_return_date="2026-12-19",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
    )

    reply = tc._dispatch(3, f"/cancel-trip {trip_id}")

    assert f"Trip #{trip_id} cancelled" in reply
    assert trips.get_active_trips() == []


def test_cancel_trip_unknown_id(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(trips, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(trips, "_initialized", False)

    reply = tc._dispatch(3, "/cancel-trip 999")
    assert "No active trip" in reply


def test_cancel_trip_missing_id():
    reply = tc._dispatch(3, "/cancel-trip")
    assert "Usage" in reply
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `pytest tests/test_telegram_commands.py -v`
Expected: the pre-existing tests (`_format_proposal`, `/trips`, `/cancel-trip`) PASS; every new router-facing test FAILS with `AttributeError: module 'telegram_commands' has no attribute '_pending'` or similar (`_route` and `_pending` don't exist yet).

- [ ] **Step 3: Replace the router-facing sections of `telegram_commands.py`**

Replace the module docstring:

```python
"""Telegram bot commands: slash commands for direct actions, and one
relay-backed router for everything else.

/new-trip and /set-config start a short conversation by asking a
question; /cancel-trip, /trips, /get, /help act immediately. Every
other message — including replies inside an open conversation — goes
through _route(), which asks the Claude Code relay running on the same
VPS to pick one action (propose or revise a trip, cancel a trip, list
trips, edit or show the config, answer a question, or say it's
unclear) and runs it. The relay's shared persona is sent once per
chat (relay_client.PERSONA_PROMPT); every following turn resumes the
same session instead of resending it.
"""
```

Replace `HELP_TEXT` (adds one paragraph about free-language use):

```python
HELP_TEXT = (
    "*Flight Tracker commands*\n\n"
    "/new-trip — start tracking a new trip by describing it in plain language\n"
    "/trips — list active trips and their cheapest price so far\n"
    "/cancel-trip <id> — stop tracking a trip\n"
    "/set-config — change a shared setting by describing it in plain language\n"
    "/get — show current shared settings\n"
    "/cancel — cancel a pending /set-config or /new-trip\n"
    "/help — show this message\n\n"
    "You can also just describe what you want in plain language, any "
    "time — start a trip, ask about one, cancel one, or change a "
    "setting.\n\n"
    "Changes apply on the next scheduled check, except interval\\_minutes, "
    "which needs a restart."
)
```

Replace `_CONFIG_TASK_PROMPT` and `_NEW_TRIP_TASK_PROMPT` (everything from `_CONFIG_TASK_PROMPT`'s opening line through the end of `_NEW_TRIP_TASK_PROMPT`) with one `_ROUTER_TASK_PROMPT`:

```python
_ROUTER_TASK_PROMPT = (
    "You are the router for a flight-price-tracking Telegram bot. Every "
    "message you see is one turn in an ongoing conversation with one "
    "user. Decide what the user wants and respond with nothing but a "
    "single JSON object, shaped exactly like this:\n"
    '{"action": "propose_trip | revise_trip | cancel_trip | list_trips '
    '| set_config | show_config | help | answer | unclear", '
    '"trip": {"description": "a short 3-6 word label for this trip", '
    '"destinations": ["IATA", ...], "origins": ["IATA", ...] (omit if '
    'not mentioned), "ideal_date": "YYYY-MM-DD", "ideal_return_date": '
    '"YYYY-MM-DD", "departure_range_before": int, '
    '"departure_range_after": int, "return_range_before": int, '
    '"return_range_after": int, "baseline_price_estimate": a plausible '
    'round-trip economy fare in EUR for this route as a number}, '
    '"trip_id": int, '
    '"config_edits": {"sets": [{"key": "dotted.path", "value": "string"}], '
    '"add_origins": ["IATA"], "remove_origins": ["IATA"]}, '
    '"reply": "text"}\n'
    'Use "propose_trip" to start a new trip, or when there is no '
    'pending proposal to revise. Use "revise_trip" only to change a '
    "proposal already shown to the user, and carry over every field "
    "from it that the new message doesn't change. Use \"cancel_trip\" "
    "only when the request matches exactly one id in the active trips "
    "list given below; if it's ambiguous or matches none, use "
    '"unclear" instead and ask which trip in "reply". Use "set_config" '
    "for changes to the shared settings given below — dotted paths for "
    "nested keys, e.g. passengers.adults; IATA airport codes for "
    "origins, translating city or airport names yourself. Use "
    '"answer" to respond to a question about the pending proposal, the '
    'active trips, or the shared settings, using the data given below '
    '— put the answer in "reply". Use "unclear" when the request is '
    "ambiguous or names something that doesn't exist, and ask a short "
    'clarifying question in "reply". Omit fields that don\'t apply to '
    "the chosen action. For a loose date description (a month, \"a "
    "couple of weeks in December\"), pick a sensible ideal_date roughly "
    "in the middle of it and a return date matching the trip length "
    "implied, with ranges wide enough to cover the described period. "
    "Default range fields to 3 when not implied by the request."
)
```

Replace `_pending_config: dict = {}` and `_pending_trip: dict = {}` with:

```python
_pending: dict = {}
```

Keep `_relay_turn`, `_load_config`, `_save_config`, `_format_config`, `_coerce`, `_apply_changes`, `_format_proposal` (Task 1), `_handle_get`, `_handle_trips_list`, `_handle_cancel_trip` unchanged.

Remove `_parse_config_request`, `_parse_trip_request`, `_handle_config_reply`, and `_handle_new_trip_reply` entirely. In their place, after `_handle_cancel_trip` and before `_clear_pending`, add:

```python
def _build_router_prompt(text: str, state: dict, config: dict, active: list) -> str:
    trips_summary = "\n".join(f"#{t['id']} {t['description']}" for t in active) or "none"
    draft = state.get("trip_draft")
    return (
        f"Shared config (Python dict): {config!r}\n\n"
        f"Active trips:\n{trips_summary}\n\n"
        f"Pending trip proposal (not yet confirmed): {draft!r}\n\n"
        f"User message: {text}"
    )


def _confirm_trip(chat_id, state: dict) -> str:
    trip = state["trip_draft"]
    trip_id = trips.create_trip(
        description=trip["description"],
        destinations=trip["destinations"],
        ideal_date=trip["ideal_date"],
        ideal_return_date=trip["ideal_return_date"],
        departure_range_before=trip.get("departure_range_before", 3),
        departure_range_after=trip.get("departure_range_after", 3),
        return_range_before=trip.get("return_range_before", 3),
        return_range_after=trip.get("return_range_after", 3),
        origins=trip.get("origins"),
        baseline_price_estimate=trip.get("baseline_price_estimate"),
    )
    _pending.pop(chat_id, None)
    return f"Trip #{trip_id} ({escape_md(trip['description'])}) is now being tracked."


def _execute_action(state: dict, parsed: dict, config: dict) -> str:
    action = parsed.get("action")
    fallback = "Sorry, I didn't understand that — try rephrasing, or /help."

    if action in ("propose_trip", "revise_trip"):
        trip = parsed.get("trip") or {}
        required = ("description", "destinations", "ideal_date", "ideal_return_date")
        if not all(trip.get(field) for field in required):
            return "I couldn't work out a full trip from that. Try rephrasing, or /cancel."
        for field in ("departure_range_before", "departure_range_after", "return_range_before", "return_range_after"):
            trip.setdefault(field, 3)
        state["trip_draft"] = trip
        return _format_proposal(trip, config)

    if action == "cancel_trip" and isinstance(parsed.get("trip_id"), int):
        trip_id = parsed["trip_id"]
        if trips.cancel_trip(trip_id):
            return f"Trip #{trip_id} cancelled."
        return f"No active trip with id {trip_id}."

    if action == "list_trips":
        return _handle_trips_list()

    if action == "set_config":
        changes = _apply_changes(config, parsed.get("config_edits") or {})
        if not changes:
            return "I didn't find any changes to make there. Try rephrasing, or /cancel."
        _save_config(config)
        return "\n".join(changes)

    if action == "show_config":
        return _format_config(config)

    if action == "help":
        return HELP_TEXT

    if action in ("answer", "unclear") and parsed.get("reply"):
        return parsed["reply"]

    return fallback


def _route(chat_id, text: str) -> str:
    state = _pending.setdefault(chat_id, {"session_id": None, "trip_draft": None})

    if state["trip_draft"] and text.strip().lower() == "yes":
        return _confirm_trip(chat_id, state)

    config = _load_config()
    active = trips.get_active_trips()
    prompt = _build_router_prompt(text, state, config, active)

    try:
        parsed, session_id = _relay_turn(prompt, prompt, _ROUTER_TASK_PROMPT, state["session_id"])
    except Exception as exc:
        logger.error("Router relay call failed: %s", exc)
        return f"Sorry, I couldn't process that: {escape_md(exc)}"

    state["session_id"] = session_id
    return _execute_action(state, parsed, config)
```

Replace `_clear_pending`:

```python
def _clear_pending(chat_id) -> None:
    _pending.pop(chat_id, None)
```

Replace `_dispatch`:

```python
def _dispatch(chat_id, text: str) -> str:
    stripped = text.strip()
    command = stripped.split()[0].lower() if stripped else ""

    if command == "/cancel":
        _clear_pending(chat_id)
        return "Cancelled."

    if command in ("/start", "/help"):
        _clear_pending(chat_id)
        return HELP_TEXT

    if command == "/get":
        _clear_pending(chat_id)
        return _handle_get()

    if command == "/trips":
        _clear_pending(chat_id)
        return _handle_trips_list()

    if command == "/cancel-trip":
        _clear_pending(chat_id)
        return _handle_cancel_trip(stripped.split()[1:])

    if command == "/set-config":
        _clear_pending(chat_id)
        _pending[chat_id] = {"session_id": None, "trip_draft": None}
        return "What would you like to change? Describe it in plain language."

    if command == "/new-trip":
        _clear_pending(chat_id)
        _pending[chat_id] = {"session_id": None, "trip_draft": None}
        return (
            "Where and when do you want to go? Describe it in plain "
            "language — a place, a kind of destination, specific dates, "
            "or a loose period."
        )

    if command.startswith("/"):
        return f"Unknown command: {escape_md(command)}\n\n{HELP_TEXT}"

    return _route(chat_id, stripped)
```

Keep `_get_updates`, `_poll_loop`, `start` unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_telegram_commands.py -v`
Expected: PASS (all tests)

Then run the full suite to confirm nothing else broke:

Run: `pytest tests/ -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add telegram_commands.py tests/test_telegram_commands.py
git commit -m "Replace /new-trip and /set-config follow-up handling with one conversational router"
```

---

## Self-Review

**Spec coverage:**
- Architecture and message flow (`_route`, slash commands unchanged) — Task 2.
- State model (`_pending` unified dict, no "kind" lock, fresh context every call) — Task 2.
- Router contract (the `action` JSON shape, retiring the old prompts/handlers) — Task 2.
- Proposal display always shows departure airports — Task 1.
- Error handling (relay failure, malformed output, ambiguous cancel, unknown config key, no extra confirmation on cancel, trip creation only via "yes") — Task 2, covered by `test_router_relay_failure_reports_error`, `test_malformed_router_output_falls_back_to_generic_message`, `test_cancel_trip_action_cancels_matched_trip`, `_apply_changes`'s existing unknown-key behavior (unchanged), and `test_yes_confirms_pending_trip_without_calling_relay`.
- Testing list from the spec — each bullet has a matching test in Task 2, plus the two `_format_proposal` tests from Task 1.

**Placeholder scan:** No TBD/TODO markers; every step shows the actual code or test.

**Type consistency:** `_format_proposal(proposal, config)` (Task 1) is called the same way in Task 2's `_execute_action`. `_pending[chat_id]` is always `{"session_id": ..., "trip_draft": ...}` everywhere it's read or written, in both tasks and all tests. `_route(chat_id, text)` matches its one call site in `_dispatch`.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-29-conversational-router.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
