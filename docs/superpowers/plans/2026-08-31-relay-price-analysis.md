# Relay price-analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a second, SQL-backed relay session so the Telegram bot can answer open-ended questions about price history.

**Architecture:** The general router gains one new action, `analyze`. When it fires, `_execute_action` starts or resumes a second relay session that carries its own system prompt — one that grants `psql` access to a read-only Postgres role and forbids the general router's "no shell commands" rule. The second session's id lives in `_pending[chat_id]["analysis_session_id"]`, alongside the existing `session_id`.

**Tech Stack:** Python, pytest, `relay_client.query` (mocked in tests), `unittest.mock.patch`.

**Spec:** `docs/superpowers/specs/2026-08-31-relay-price-analysis-design.md`

## Global Constraints

- This plan covers code in `telegram_commands.py` and its test file only. `relay_client.py`, `storage.py`, `trips.py`, and `deals.py` do not change.
- The read-only Postgres role name is `flight_tracker_readonly`. The analysis persona prompt names it exactly this way.
- The analysis persona prompt connects with `psql -h 127.0.0.1 -U flight_tracker_readonly -d flight_tracker` and never states a password.
- The `trips` and `prices` table schemas in the analysis persona prompt must list every column named in the spec, in the spec's order.
- Infrastructure steps from the spec — publishing the Postgres port to `127.0.0.1`, creating the `flight_tracker_readonly` role, and writing `~/.pgpass` — are manual rollout steps outside this repo. No task in this plan performs them.

## A note on one spec deviation

The spec says `_relay_turn`'s existing signature needs no change to support a second session. Tracing the current code shows this is not quite right: `_relay_turn` always prepends `relay_client.PERSONA_PROMPT` — the general router persona, which says "Do not run shell commands" — to whatever `task_prompt` it is given. Sent unchanged, the analysis session's system prompt would tell the model not to run shell commands and then, a few lines later, tell it to run `psql`. That directly undermines the feature's goal.

Task 1 below fixes this with a minimal, backward-compatible change: `_relay_turn` gains an optional `persona` parameter defaulting to `relay_client.PERSONA_PROMPT`, so every existing call site (the general router) is unaffected. The analysis branch passes `persona=""` so its system prompt is `_ANALYSIS_PERSONA_PROMPT` alone.

---

### Task 1: Let `_relay_turn` take a persona override

**Files:**
- Modify: `telegram_commands.py:105-122` (`_relay_turn`)
- Test: `tests/test_telegram_commands.py`

**Interfaces:**
- Produces: `_relay_turn(followup_prompt, full_prompt, task_prompt, session_id, persona=relay_client.PERSONA_PROMPT) -> tuple[dict, str | None]` — unchanged return shape; `persona=""` sends `task_prompt` alone as `system_prompt`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_telegram_commands.py`:

```python
def test_relay_turn_default_persona_includes_general_persona(monkeypatch):
    with patch("telegram_commands.relay_client.query", return_value={
        "result": "{}", "session_id": "sess-1",
    }) as mock_query:
        tc._relay_turn("hello", "hello", "TASK PROMPT", None)

    args, kwargs = mock_query.call_args
    assert kwargs["system_prompt"] == f"{tc.relay_client.PERSONA_PROMPT}\n\nTASK PROMPT"


def test_relay_turn_with_empty_persona_sends_task_prompt_only(monkeypatch):
    with patch("telegram_commands.relay_client.query", return_value={
        "result": "{}", "session_id": "sess-1",
    }) as mock_query:
        tc._relay_turn("hello", "hello", "TASK PROMPT ONLY", None, persona="")

    args, kwargs = mock_query.call_args
    assert kwargs["system_prompt"] == "TASK PROMPT ONLY"
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `pytest tests/test_telegram_commands.py -k relay_turn -v`
Expected: `test_relay_turn_with_empty_persona_sends_task_prompt_only` FAILS with a `TypeError` about an unexpected keyword argument `persona`. `test_relay_turn_default_persona_includes_general_persona` should already PASS (it only exercises current behavior) — confirm that too.

- [ ] **Step 3: Add the `persona` parameter**

In `telegram_commands.py`, replace:

```python
def _relay_turn(followup_prompt: str, full_prompt: str, task_prompt: str, session_id) -> tuple:
    """One turn of a relay conversation.

    On the first turn (no session_id) or if resuming session_id fails,
    sends full_prompt with the persona + task_prompt as system_prompt. On
    a successful resume, sends only followup_prompt — the relay's session
    already has everything else.
    """
    if session_id:
        try:
            response = relay_client.query(followup_prompt, session_id=session_id)
            return relay_client.extract_json(response["result"]), response["session_id"]
        except requests.RequestException:
            logger.warning("Relay session %s failed to resume — starting fresh.", session_id)

    system_prompt = f"{relay_client.PERSONA_PROMPT}\n\n{task_prompt}"
    response = relay_client.query(full_prompt, system_prompt=system_prompt)
    return relay_client.extract_json(response["result"]), response["session_id"]
```

with:

```python
def _relay_turn(
    followup_prompt: str, full_prompt: str, task_prompt: str, session_id,
    persona: str = relay_client.PERSONA_PROMPT,
) -> tuple:
    """One turn of a relay conversation.

    On the first turn (no session_id) or if resuming session_id fails,
    sends full_prompt with persona + task_prompt as system_prompt (just
    task_prompt if persona is ""). On a successful resume, sends only
    followup_prompt — the relay's session already has everything else.
    """
    if session_id:
        try:
            response = relay_client.query(followup_prompt, session_id=session_id)
            return relay_client.extract_json(response["result"]), response["session_id"]
        except requests.RequestException:
            logger.warning("Relay session %s failed to resume — starting fresh.", session_id)

    system_prompt = f"{persona}\n\n{task_prompt}" if persona else task_prompt
    response = relay_client.query(full_prompt, system_prompt=system_prompt)
    return relay_client.extract_json(response["result"]), response["session_id"]
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `pytest tests/test_telegram_commands.py -v`
Expected: all tests PASS, including the two new ones and every pre-existing test (the default parameter value keeps every current call site unchanged).

- [ ] **Step 5: Commit**

```bash
git add telegram_commands.py tests/test_telegram_commands.py
git commit -m "Let _relay_turn override the general router persona"
```

---

### Task 2: Add the `analyze` action and its persona prompt

**Files:**
- Modify: `telegram_commands.py:37-52` (`HELP_TEXT` — deferred to Task 4, not touched here)
- Modify: `telegram_commands.py:54-100` (`_ROUTER_TASK_PROMPT`)
- Modify: `telegram_commands.py` (new module-level `_ANALYSIS_PERSONA_PROMPT`, placed after `_ROUTER_TASK_PROMPT`)
- Modify: `telegram_commands.py:409` (`_route`'s `_pending.setdefault` default dict)
- Modify: `telegram_commands.py:463-475` (`/set-config` and `/new-trip` blocks in `_dispatch`)
- Test: `tests/test_telegram_commands.py`

**Interfaces:**
- Consumes: nothing new from Task 1.
- Produces: `_ANALYSIS_PERSONA_PROMPT: str` — the full analysis-session system prompt, consumed by Task 3. `_pending[chat_id]` dicts now always carry a fourth key, `analysis_session_id`, consumed by Task 3 and Task 4.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_telegram_commands.py`:

```python
def test_router_prompt_lists_analyze_action():
    assert "analyze" in tc._ROUTER_TASK_PROMPT


def test_analysis_persona_names_the_readonly_role_and_no_password():
    assert "flight_tracker_readonly" in tc._ANALYSIS_PERSONA_PROMPT
    assert "password" not in tc._ANALYSIS_PERSONA_PROMPT.lower().replace("no password needed", "")
```

Update the existing `test_new_trip_command_resets_prior_pending_state` (it currently asserts a 3-key dict; it must become 4-key once `/new-trip` starts producing `analysis_session_id` too):

```python
def test_new_trip_command_resets_prior_pending_state():
    chat_id = 2
    tc._pending[chat_id] = {
        "session_id": "some-session", "trip_draft": {"description": "old"}, "trip_draft_id": 7,
    }

    tc._dispatch(chat_id, "/new-trip")

    assert tc._pending[chat_id] == {
        "session_id": None, "trip_draft": None, "trip_draft_id": None,
        "analysis_session_id": None,
    }
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `pytest tests/test_telegram_commands.py -k "analyze_action or analysis_persona or new_trip_command_resets" -v`
Expected: `test_router_prompt_lists_analyze_action` and `test_analysis_persona_names_the_readonly_role_and_no_password` FAIL with `AttributeError` (no `_ANALYSIS_PERSONA_PROMPT` yet, and `"analyze"` is not yet in `_ROUTER_TASK_PROMPT`). `test_new_trip_command_resets_prior_pending_state` FAILS on the dict comparison (missing `analysis_session_id`).

- [ ] **Step 3: Update `_ROUTER_TASK_PROMPT` and add `_ANALYSIS_PERSONA_PROMPT`**

In `telegram_commands.py`, replace the whole `_ROUTER_TASK_PROMPT` assignment with:

```python
_ROUTER_TASK_PROMPT = (
    "You are the router for a flight-price-tracking Telegram bot. Every "
    "message you see is one turn in an ongoing conversation with one "
    "user. Decide what the user wants and respond with nothing but a "
    "single JSON object, shaped exactly like this:\n"
    '{"action": "propose_trip | revise_trip | edit_trip | cancel_trip | '
    'list_trips | list_trip_history | set_config | show_config | help | '
    'answer | analyze | unclear", '
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
    '"query": "text", '
    '"reply": "text"}\n'
    'Use "propose_trip" to start a new trip, or when there is no '
    'pending proposal to revise or edit. Use "edit_trip" to change an '
    "already-tracked trip from the active trips list given below — set "
    '"trip_id" to that trip\'s id, and base "trip" on that trip\'s '
    "current full data with only the requested fields changed, keeping "
    "everything else identical. Use \"revise_trip\" to keep refining a "
    "proposal already shown to the user this conversation (started by "
    'either "propose_trip" or "edit_trip"), carrying over every field '
    "from it — and its trip_id, if it had one — that the new message "
    "doesn't change. Use \"cancel_trip\" only when the request matches "
    "exactly one id in the active trips list; if it's ambiguous or "
    'matches none, use "unclear" instead and ask which trip in '
    '"reply". Use "list_trip_history" to list cancelled or finished '
    'trips. Use "set_config" '
    "for changes to the shared settings given below — dotted paths for "
    "nested keys, e.g. passengers.adults; IATA airport codes for "
    "origins, translating city or airport names yourself. Use "
    '"answer" to respond to a question about the pending proposal, the '
    'active trips, or the shared settings, using the data given below '
    '— put the answer in "reply". Use "analyze" when the user is '
    "asking a question about price history, trends, or any "
    "data-driven analysis of trips — anything that needs real "
    "historical numbers to answer well, not just the active trips "
    'list already given to you. Put a self-contained restatement of '
    'the question in "query", resolving any references ("that trip", '
    '"the Sao Paulo one") against the active trips list above so it '
    "can be understood without this conversation's context. Use "
    '"unclear" when the request is '
    "ambiguous or names something that doesn't exist, and ask a short "
    'clarifying question in "reply". Omit fields that don\'t apply to '
    "the chosen action. For a loose date description (a month, \"a "
    "couple of weeks in December\"), pick a sensible ideal_date roughly "
    "in the middle of it and a return date matching the trip length "
    "implied, with ranges wide enough to cover the described period. "
    "Default range fields to 3 when not implied by the request."
)

_ANALYSIS_PERSONA_PROMPT = (
    "You are the data-analysis backend for a personal "
    "flight-price-tracking assistant. You have read-only access to its "
    "PostgreSQL database via `psql -h 127.0.0.1 -U "
    "flight_tracker_readonly -d flight_tracker` (no password needed — "
    "already configured). The schema:\n\n"
    "`trips(id, description, destinations JSONB, origins JSONB, "
    "ideal_date DATE, ideal_return_date DATE, departure_range_before "
    "INT, departure_range_after INT, return_range_before INT, "
    "return_range_after INT, seat TEXT, passengers JSONB, "
    "max_duration_hours INT, results_per_query INT, "
    "baseline_price_estimate DOUBLE PRECISION, status TEXT, created_at "
    "TIMESTAMPTZ)`\n\n"
    "`prices(id, checked_at TIMESTAMPTZ, origin TEXT, destination "
    "TEXT, depart_date DATE, return_date DATE, airline TEXT, "
    "departure TEXT, arrival TEXT, duration TEXT, stops INT, price "
    "TEXT, price_value DOUBLE PRECISION, is_best BOOLEAN, trip_id INT "
    "REFERENCES trips)`\n\n"
    "Answer the user's question by querying this data. Use SELECT "
    "only — you have no write access, and none is needed. If the "
    "database is unreachable or a query fails, say so plainly in your "
    "reply rather than retrying indefinitely. Respond with nothing "
    'but a single JSON object: {"reply": "text"}.'
)
```

- [ ] **Step 4: Add `analysis_session_id` to every `_pending` default dict**

In `telegram_commands.py`, in `_route`, replace:

```python
    state = _pending.setdefault(chat_id, {"session_id": None, "trip_draft": None, "trip_draft_id": None})
```

with:

```python
    state = _pending.setdefault(
        chat_id,
        {"session_id": None, "trip_draft": None, "trip_draft_id": None, "analysis_session_id": None},
    )
```

In `_dispatch`, replace both of these blocks (they are identical apart from the returned message):

```python
    if command == "/set-config":
        _clear_pending(chat_id)
        _pending[chat_id] = {"session_id": None, "trip_draft": None, "trip_draft_id": None}
        return "What would you like to change? Describe it in plain language."

    if command == "/new-trip":
        _clear_pending(chat_id)
        _pending[chat_id] = {"session_id": None, "trip_draft": None, "trip_draft_id": None}
        return (
            "Where and when do you want to go? Describe it in plain "
            "language — a place, a kind of destination, specific dates, "
            "or a loose period."
        )
```

with:

```python
    if command == "/set-config":
        _clear_pending(chat_id)
        _pending[chat_id] = {
            "session_id": None, "trip_draft": None, "trip_draft_id": None,
            "analysis_session_id": None,
        }
        return "What would you like to change? Describe it in plain language."

    if command == "/new-trip":
        _clear_pending(chat_id)
        _pending[chat_id] = {
            "session_id": None, "trip_draft": None, "trip_draft_id": None,
            "analysis_session_id": None,
        }
        return (
            "Where and when do you want to go? Describe it in plain "
            "language — a place, a kind of destination, specific dates, "
            "or a loose period."
        )
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `pytest tests/test_telegram_commands.py -v`
Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add telegram_commands.py tests/test_telegram_commands.py
git commit -m "Add the analyze action and its persona prompt to the router"
```

---

### Task 3: `_execute_action`'s `analyze` branch

**Files:**
- Modify: `telegram_commands.py:346-405` (`_execute_action`)
- Test: `tests/test_telegram_commands.py`

**Interfaces:**
- Consumes: `_relay_turn(..., persona="")` from Task 1; `_ANALYSIS_PERSONA_PROMPT` and `state["analysis_session_id"]` from Task 2.
- Produces: `_execute_action` now handles `parsed["action"] == "analyze"`, reading `parsed["query"]` and writing `state["analysis_session_id"]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_telegram_commands.py`:

```python
def test_analyze_action_starts_separate_analysis_session(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    chat_id = 5
    responses = [
        {"result": '{"action": "analyze", "query": "What was the cheapest fare to GRU?"}', "session_id": "sess-router-1"},
        {"result": '{"reply": "The cheapest fare to GRU was €410 on 2026-11-02."}', "session_id": "sess-analysis-1"},
    ]

    def fake_query(*args, **kwargs):
        return responses.pop(0)

    with patch("telegram_commands.relay_client.query", side_effect=fake_query) as mock_query:
        reply = tc._dispatch(chat_id, "what's the cheapest fare to Sao Paulo been?")

    assert reply == "The cheapest fare to GRU was €410 on 2026-11-02."
    assert tc._pending[chat_id]["session_id"] == "sess-router-1"
    assert tc._pending[chat_id]["analysis_session_id"] == "sess-analysis-1"
    assert mock_query.call_count == 2

    analysis_args, analysis_kwargs = mock_query.call_args_list[1]
    assert analysis_args[0] == "What was the cheapest fare to GRU?"
    assert analysis_kwargs["system_prompt"] == tc._ANALYSIS_PERSONA_PROMPT


def test_analyze_action_resumes_existing_analysis_session(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    chat_id = 5
    tc._pending[chat_id] = {
        "session_id": "sess-router-1", "trip_draft": None, "trip_draft_id": None,
        "analysis_session_id": "sess-analysis-1",
    }

    responses = [
        {"result": '{"action": "analyze", "query": "And the most expensive one?"}', "session_id": "sess-router-1"},
        {"result": '{"reply": "The most expensive fare was €900."}', "session_id": "sess-analysis-1"},
    ]

    def fake_query(*args, **kwargs):
        return responses.pop(0)

    with patch("telegram_commands.relay_client.query", side_effect=fake_query) as mock_query:
        reply = tc._dispatch(chat_id, "and the most expensive one?")

    assert reply == "The most expensive fare was €900."
    analysis_args, analysis_kwargs = mock_query.call_args_list[1]
    assert analysis_kwargs["session_id"] == "sess-analysis-1"
    assert "system_prompt" not in analysis_kwargs


def test_analyze_action_malformed_output_falls_back(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    chat_id = 5
    responses = [
        {"result": '{"action": "analyze", "query": "trend?"}', "session_id": "sess-router-1"},
        {"result": "not json", "session_id": "sess-analysis-1"},
    ]

    def fake_query(*args, **kwargs):
        return responses.pop(0)

    with patch("telegram_commands.relay_client.query", side_effect=fake_query):
        reply = tc._dispatch(chat_id, "what's the trend?")

    assert "didn't understand" in reply
    assert tc._pending[chat_id]["analysis_session_id"] == "sess-analysis-1"


def test_analyze_action_relay_failure_reports_error(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    chat_id = 5
    responses = [
        {"result": '{"action": "analyze", "query": "trend?"}', "session_id": "sess-router-1"},
    ]

    def fake_query(*args, **kwargs):
        if responses:
            return responses.pop(0)
        raise requests.RequestException("boom")

    with patch("telegram_commands.relay_client.query", side_effect=fake_query):
        reply = tc._dispatch(chat_id, "what's the trend?")

    assert "couldn't process that" in reply
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `pytest tests/test_telegram_commands.py -k analyze_action -v`
Expected: all four FAIL — `_execute_action` has no `analyze` branch yet, so the router's `"analyze"` classification falls through to the generic fallback (`"didn't understand"`) instead of triggering a second relay call, and `mock_query.call_count` is `1`, not `2`.

- [ ] **Step 3: Add the `analyze` branch**

In `telegram_commands.py`, in `_execute_action`, insert this block right after the `help` branch (`if action == "help": return HELP_TEXT`) and before `if action in ("answer", "unclear") and parsed.get("reply"):`:

```python
    if action == "analyze":
        query_text = parsed.get("query", "")
        try:
            analysis_result, analysis_session_id = _relay_turn(
                query_text, query_text, _ANALYSIS_PERSONA_PROMPT,
                state.get("analysis_session_id"), persona="",
            )
        except Exception as exc:
            logger.error("Analysis relay call failed: %s", exc)
            return f"Sorry, I couldn't process that: {escape_md(exc)}"
        state["analysis_session_id"] = analysis_session_id
        return analysis_result.get("reply") or fallback
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `pytest tests/test_telegram_commands.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add telegram_commands.py tests/test_telegram_commands.py
git commit -m "Handle the analyze action by starting a second relay session"
```

---

### Task 4: `/analyze` command and help text

**Files:**
- Modify: `telegram_commands.py:37-52` (`HELP_TEXT`)
- Modify: `telegram_commands.py` (`_dispatch`, new `/analyze` block)
- Test: `tests/test_telegram_commands.py`

**Interfaces:**
- Consumes: `_clear_pending` (already exists); the four-key `_pending` shape from Task 2.
- Produces: `/analyze` as a recognized command in `_dispatch`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_telegram_commands.py`:

```python
def test_analyze_command_resets_prior_pending_state():
    chat_id = 6
    tc._pending[chat_id] = {
        "session_id": "some-session", "trip_draft": {"description": "old"},
        "trip_draft_id": 7, "analysis_session_id": "old-analysis-session",
    }

    reply = tc._dispatch(chat_id, "/analyze")

    assert reply == "What would you like to know about your price history?"
    assert tc._pending[chat_id] == {
        "session_id": None, "trip_draft": None, "trip_draft_id": None,
        "analysis_session_id": None,
    }


def test_help_text_mentions_analyze_command():
    assert "/analyze" in tc.HELP_TEXT
```

- [ ] **Step 2: Run the tests to see them fail**

Run: `pytest tests/test_telegram_commands.py -k "analyze_command or help_text_mentions_analyze" -v`
Expected: `test_analyze_command_resets_prior_pending_state` FAILS — `/analyze` is not yet a recognized command, so `_dispatch` routes it through `_route` instead, calling the (unmocked) relay. `test_help_text_mentions_analyze_command` FAILS — `HELP_TEXT` does not mention `/analyze` yet.

- [ ] **Step 3: Add the `/analyze` command and the help line**

In `telegram_commands.py`, replace `HELP_TEXT`:

```python
HELP_TEXT = (
    "*Flight Tracker commands*\n\n"
    "/new-trip — start tracking a new trip by describing it in plain language\n"
    "/trips — list active trips and their cheapest price so far\n"
    "/trip-history — list cancelled or finished trips\n"
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

with:

```python
HELP_TEXT = (
    "*Flight Tracker commands*\n\n"
    "/new-trip — start tracking a new trip by describing it in plain language\n"
    "/trips — list active trips and their cheapest price so far\n"
    "/trip-history — list cancelled or finished trips\n"
    "/cancel-trip <id> — stop tracking a trip\n"
    "/set-config — change a shared setting by describing it in plain language\n"
    "/get — show current shared settings\n"
    "/analyze — ask a question about your price history\n"
    "/cancel — cancel a pending /set-config, /new-trip, or /analyze\n"
    "/help — show this message\n\n"
    "You can also just describe what you want in plain language, any "
    "time — start a trip, ask about one, cancel one, change a "
    "setting, or ask about your price history.\n\n"
    "Changes apply on the next scheduled check, except interval\\_minutes, "
    "which needs a restart."
)
```

In `_dispatch`, add this block right after the `/new-trip` block and before `if command.startswith("/"):`:

```python
    if command == "/analyze":
        _clear_pending(chat_id)
        _pending[chat_id] = {
            "session_id": None, "trip_draft": None, "trip_draft_id": None,
            "analysis_session_id": None,
        }
        return "What would you like to know about your price history?"
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `pytest tests/test_telegram_commands.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add telegram_commands.py tests/test_telegram_commands.py
git commit -m "Add the /analyze command"
```

---

## After this plan lands

The spec's rollout checklist still needs a person, not code, to:

- Publish `returnhub-postgres-1`'s Postgres port to `127.0.0.1:5432` only.
- Create the `flight_tracker_readonly` role with `SELECT` only on `trips` and `prices`.
- Write `~/.pgpass` (mode `0600`) for the relay process's user.
- Have a real conversation with `/analyze` against the live relay to confirm the model writes correct SQL and gives good answers — this cannot be checked by pytest.
