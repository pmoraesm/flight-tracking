# Jev Deal Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send a Telegram alert only for a very good deal, one flight per trip, with a cooldown between alerts, using Jev to judge the deal.

**Architecture:** For each trip, code computes exact price facts from stored history and writes them as plain sentences. Jev answers two yes/no questions per candidate flight over HTTP. Code combines the answers, picks the cheapest passing flight, applies a per-trip cooldown, and records sent alerts in a new `alerts` table.

**Tech Stack:** Python 3.9 (local venv) and 3.11 (Docker), `requests`, `psycopg` 3, PostgreSQL, pytest. TypeSafe System One HTTP API (model `jev-1.13.0`).

**Spec:** `docs/superpowers/specs/2026-09-28-jev-deal-check-design.md`

## Global Constraints

- The code must run on Python 3.9 (local `.venv`) and Python 3.11 (Docker image). Do not use `X | Y` type syntax. Use `Optional[X]`.
- Use HTTP through `requests`. Do not add the `typesafe-sdk` package. Do not add any dependency to `requirements.txt`.
- The Jev model is pinned to `jev-1.13.0` in `deal_check.model`.
- Label cut points are constants in code. They are not in `config.yaml`.
- Only price rows with `checked_at` before `check_started_at` count as history.
- Delete the old percentile rule (`is_good_deal`) and its constants. Do not keep a fallback.
- `TYPESAFE_API_KEY` is read from the environment, loaded from `.env`.
- Write commit messages in short, active sentences. End each commit message with the line `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.
- Do not push to the remote.
- Do not add code comments unless a reason is not obvious.

## Prerequisites

The test suite needs a PostgreSQL database at `localhost:5432`. The `tests/conftest.py` fixture truncates tables before every test, so even tests without database code need it.

- [ ] **Step 1: Start Docker Desktop, then start the dev database**

Run: `cd /Users/paulo/Documents/repos/flight_tracking && docker compose --profile dev up -d postgres`
Expected: the container `flight-tracker-postgres` is running. Check with `docker ps --format '{{.Names}}'`.

- [ ] **Step 2: Run the baseline test suite**

Run: `source .venv/bin/activate && python -m pytest -q`
Expected: all tests pass. If a test fails now, stop and report it. Do not continue on a red baseline.

## File Structure

| File | Responsibility |
| --- | --- |
| `flight_tracker/jev_client.py` (new) | One HTTP call to Jev. Retries, errors, key handling. |
| `flight_tracker/deals.py` | Settings, exact price facts, Jev questions, candidate selection, cooldown. |
| `flight_tracker/storage.py` | `record_alerts()` and `last_alert_time()`. |
| `flight_tracker/notifier.py` | `notify_alerts()` returns a `bool`. |
| `main.py` | The per-trip flow in `run_check()`. |
| `schema.sql` | The `alerts` table. |
| `config.yaml` | The `deal_check` block. |
| `scripts/replay_deal_check.py` (new) | Read-only replay of stored prices through Jev. |

---

### Task 1: Jev client

**Files:**
- Create: `flight_tracker/jev_client.py`
- Test: `tests/test_jev_client.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces:
  - `class JevError(Exception)`
  - `is_configured() -> bool`
  - `judge(state, questions: dict, model: str) -> dict` returns `{question_id: float}`, the yes probability of each Noul question. It raises `JevError` on any failure.
  - `TIMEOUT_SECONDS`, `API_URL`, `_api_key()`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_jev_client.py`:

```python
from unittest.mock import MagicMock, patch

import pytest
import requests

from flight_tracker import jev_client

QUESTIONS = {
    "price_is_very_good": {"type": "noul", "instructions": "Is the price good?"},
    "flight_is_acceptable": {"type": "noul", "instructions": "Is the flight fine?"},
}


def _response(status=200, body=None, headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.json.return_value = body
    return resp


def _ok_body(price=0.93, flight=0.81):
    return {
        "model": "jev-1.13.0",
        "answers": {
            "price_is_very_good": {"type": "noul", "noul": price},
            "flight_is_acceptable": {"type": "noul", "noul": flight},
        },
    }


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setattr(jev_client, "_api_key", lambda: "test-key")


def test_judge_returns_noul_probabilities_by_question_id():
    with patch("flight_tracker.jev_client.requests.post", return_value=_response(body=_ok_body())):
        answers = jev_client.judge({"flight": {}}, QUESTIONS, "jev-1.13.0")

    assert answers == {"price_is_very_good": 0.93, "flight_is_acceptable": 0.81}


def test_judge_sends_bearer_key_model_state_and_questions():
    with patch("flight_tracker.jev_client.requests.post", return_value=_response(body=_ok_body())) as post:
        jev_client.judge({"flight": {"route": "AMS to GRU"}}, QUESTIONS, "jev-1.13.0")

    args, kwargs = post.call_args
    assert args[0] == "https://api.typesafe.ai/v1/systemone"
    assert kwargs["headers"] == {"Authorization": "Bearer test-key"}
    assert kwargs["json"] == {
        "state": {"flight": {"route": "AMS to GRU"}},
        "model": "jev-1.13.0",
        "questions": QUESTIONS,
    }
    assert kwargs["timeout"] == jev_client.TIMEOUT_SECONDS


def test_judge_raises_when_the_key_is_missing(monkeypatch):
    monkeypatch.setattr(jev_client, "_api_key", lambda: None)

    with patch("flight_tracker.jev_client.requests.post") as post:
        with pytest.raises(jev_client.JevError):
            jev_client.judge({}, QUESTIONS, "jev-1.13.0")

    post.assert_not_called()


def test_judge_retries_a_429_and_waits_for_retry_after():
    responses = [_response(status=429, headers={"Retry-After": "3"}), _response(body=_ok_body())]

    with patch("flight_tracker.jev_client.requests.post", side_effect=responses) as post, \
         patch("flight_tracker.jev_client.time.sleep") as sleep:
        answers = jev_client.judge({}, QUESTIONS, "jev-1.13.0")

    assert post.call_count == 2
    sleep.assert_called_once_with(3.0)
    assert answers["price_is_very_good"] == 0.93


def test_judge_retries_a_5xx_with_growing_backoff():
    responses = [_response(status=503), _response(status=500), _response(body=_ok_body())]

    with patch("flight_tracker.jev_client.requests.post", side_effect=responses) as post, \
         patch("flight_tracker.jev_client.time.sleep") as sleep:
        jev_client.judge({}, QUESTIONS, "jev-1.13.0")

    assert post.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1.0, 2.0]


def test_judge_raises_after_the_last_retry():
    responses = [_response(status=503)] * 3

    with patch("flight_tracker.jev_client.requests.post", side_effect=responses) as post, \
         patch("flight_tracker.jev_client.time.sleep"):
        with pytest.raises(jev_client.JevError, match="503"):
            jev_client.judge({}, QUESTIONS, "jev-1.13.0")

    assert post.call_count == 3


def test_judge_does_not_retry_a_401():
    with patch("flight_tracker.jev_client.requests.post", return_value=_response(status=401)) as post, \
         patch("flight_tracker.jev_client.time.sleep") as sleep:
        with pytest.raises(jev_client.JevError, match="401"):
            jev_client.judge({}, QUESTIONS, "jev-1.13.0")

    assert post.call_count == 1
    sleep.assert_not_called()


def test_judge_raises_on_a_timeout_without_retry():
    with patch("flight_tracker.jev_client.requests.post", side_effect=requests.Timeout("slow")) as post:
        with pytest.raises(jev_client.JevError):
            jev_client.judge({}, QUESTIONS, "jev-1.13.0")

    assert post.call_count == 1


@pytest.mark.parametrize("body", [
    {},
    {"answers": {"price_is_very_good": {"type": "noul", "noul": 0.9}}},
    {"answers": {
        "price_is_very_good": {"type": "noul", "noul": "high"},
        "flight_is_acceptable": {"type": "noul", "noul": 0.5},
    }},
    {"answers": {
        "price_is_very_good": {"type": "noul", "noul": 1.5},
        "flight_is_acceptable": {"type": "noul", "noul": 0.5},
    }},
])
def test_judge_raises_on_an_unusable_reply(body):
    with patch("flight_tracker.jev_client.requests.post", return_value=_response(body=body)):
        with pytest.raises(jev_client.JevError):
            jev_client.judge({}, QUESTIONS, "jev-1.13.0")


def test_judge_raises_when_the_reply_is_not_json():
    resp = _response()
    resp.json.side_effect = ValueError("no json")

    with patch("flight_tracker.jev_client.requests.post", return_value=resp):
        with pytest.raises(jev_client.JevError):
            jev_client.judge({}, QUESTIONS, "jev-1.13.0")


def test_is_configured_follows_the_key(monkeypatch):
    assert jev_client.is_configured() is True

    monkeypatch.setattr(jev_client, "_api_key", lambda: None)
    assert jev_client.is_configured() is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `source .venv/bin/activate && python -m pytest tests/test_jev_client.py -v`
Expected: collection error, `ImportError: cannot import name 'jev_client' from 'flight_tracker'`.

- [ ] **Step 3: Write the implementation**

Create `flight_tracker/jev_client.py`:

```python
"""HTTP client for the TypeSafe System One API (Jev)."""

import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

logger = logging.getLogger(__name__)

API_URL = "https://api.typesafe.ai/v1/systemone"
TIMEOUT_SECONDS = 15
MAX_RETRIES = 2
BACKOFF_SECONDS = 1.0
MAX_RETRY_AFTER_SECONDS = 30.0
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class JevError(Exception):
    """Raised when a Jev request cannot give usable answers."""


def _api_key() -> Optional[str]:
    return os.getenv("TYPESAFE_API_KEY")


def is_configured() -> bool:
    return bool(_api_key())


def _retry_delay(resp, attempt: int) -> float:
    try:
        return min(float(resp.headers.get("Retry-After")), MAX_RETRY_AFTER_SECONDS)
    except (TypeError, ValueError):
        return BACKOFF_SECONDS * (2 ** attempt)


def _noul_value(answer) -> float:
    value = answer["noul"]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise ValueError(f"noul value out of range: {value!r}")
    return float(value)


def judge(state, questions: dict, model: str) -> dict:
    """Ask Noul questions about one state. Returns {question_id: probability of yes}."""
    key = _api_key()
    if not key:
        raise JevError("TYPESAFE_API_KEY is not set")

    payload = {"state": state, "model": model, "questions": questions}
    headers = {"Authorization": f"Bearer {key}"}

    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(API_URL, json=payload, headers=headers, timeout=TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise JevError(f"Jev request failed: {exc}") from exc

        if resp.status_code not in RETRYABLE_STATUSES or attempt == MAX_RETRIES:
            break
        time.sleep(_retry_delay(resp, attempt))

    if resp.status_code != 200:
        raise JevError(f"Jev returned HTTP {resp.status_code}")

    try:
        answers = resp.json()["answers"]
        return {question_id: _noul_value(answers[question_id]) for question_id in questions}
    except (ValueError, KeyError, TypeError) as exc:
        raise JevError(f"Jev returned an unusable reply: {exc}") from exc
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest tests/test_jev_client.py -v`
Expected: all 14 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add flight_tracker/jev_client.py tests/test_jev_client.py
git commit -m "$(cat <<'EOF'
Add a client for the TypeSafe Jev API

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Alerts table and storage functions

**Files:**
- Modify: `schema.sql`
- Modify: `flight_tracker/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces:
  - `storage.record_alerts(alerts: list[dict]) -> None`. Each dict needs `trip_id`, `origin`, `destination`, `depart_date`, `price_value`. Optional keys: `return_date`, `airline`.
  - `storage.last_alert_time(trip_id: int)` returns a timezone-aware `datetime`, or `None` when the trip has no alert.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_storage.py`. Also add `from datetime import datetime` as the first import line of the file.

```python
def _alert(trip_id, price_value=612.0):
    return {
        "trip_id": trip_id, "origin": "AMS", "destination": "GRU",
        "depart_date": "2026-07-11", "return_date": "2026-08-08",
        "airline": "KLM", "price_value": price_value,
    }


def test_last_alert_time_is_none_without_alerts():
    trip_id = _create_trip_row()

    assert storage.last_alert_time(trip_id) is None


def test_record_alerts_stores_rows_and_last_alert_time_returns_a_datetime():
    trip_id = _create_trip_row()

    storage.record_alerts([_alert(trip_id), _alert(trip_id, 590.0)])

    conn = psycopg.connect(storage.DATABASE_URL)
    rows = conn.execute(
        "SELECT origin, airline, price_value FROM alerts WHERE trip_id = %s ORDER BY price_value",
        (trip_id,),
    ).fetchall()
    conn.close()

    assert rows == [("AMS", "KLM", 590.0), ("AMS", "KLM", 612.0)]
    last = storage.last_alert_time(trip_id)
    assert isinstance(last, datetime)
    assert last.tzinfo is not None


def test_last_alert_time_is_per_trip():
    first = _create_trip_row()
    second = _create_trip_row()

    storage.record_alerts([_alert(first)])

    assert storage.last_alert_time(second) is None


def test_record_alerts_does_nothing_for_empty_list():
    storage.record_alerts([])

    conn = psycopg.connect(storage.DATABASE_URL)
    count = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    conn.close()

    assert count == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `source .venv/bin/activate && python -m pytest tests/test_storage.py -v`
Expected: the four new tests FAIL with `AttributeError: module 'flight_tracker.storage' has no attribute 'last_alert_time'` (or `record_alerts`).

- [ ] **Step 3: Add the table to `schema.sql`**

Append to the end of `schema.sql`:

```sql

CREATE TABLE alerts (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trip_id     INTEGER NOT NULL REFERENCES trips(id),
    sent_at     TIMESTAMPTZ NOT NULL,
    origin      TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart_date DATE NOT NULL,
    return_date DATE,
    airline     TEXT,
    price_value DOUBLE PRECISION NOT NULL
);

CREATE INDEX idx_alerts_trip_sent ON alerts(trip_id, sent_at DESC);
```

- [ ] **Step 4: Apply the table to the running dev database**

`schema.sql` runs only when the dev volume is new, so apply the table by hand:

```bash
docker exec -i flight-tracker-postgres psql -U flight_tracker -d flight_tracker <<'SQL'
CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trip_id     INTEGER NOT NULL REFERENCES trips(id),
    sent_at     TIMESTAMPTZ NOT NULL,
    origin      TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart_date DATE NOT NULL,
    return_date DATE,
    airline     TEXT,
    price_value DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_trip_sent ON alerts(trip_id, sent_at DESC);
SQL
```

Expected: `CREATE TABLE` and `CREATE INDEX` (or `NOTICE ... already exists, skipping`).

The `TRUNCATE ... trips CASCADE` in `tests/conftest.py` also empties `alerts`, because `alerts` references `trips`. Do not change `conftest.py`.

- [ ] **Step 5: Write the implementation**

Append to `flight_tracker/storage.py`:

```python


def record_alerts(alerts: list[dict]) -> None:
    if not alerts:
        return

    now = datetime.now(timezone.utc)

    rows = [
        (
            a["trip_id"],
            now,
            a["origin"],
            a["destination"],
            a["depart_date"],
            a.get("return_date"),
            a.get("airline"),
            float(a["price_value"]),
        )
        for a in alerts
    ]

    try:
        conn = _get_connection()
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO alerts (
                    trip_id, sent_at, origin, destination, depart_date,
                    return_date, airline, price_value
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                rows,
            )
        conn.commit()
        conn.close()
        logger.info("Postgres: recorded %d alerts", len(rows))
    except Exception as exc:
        logger.error("Postgres alert record failed: %s", exc)


def last_alert_time(trip_id: int):
    conn = _get_connection()
    row = conn.execute(
        "SELECT max(sent_at) AS last_sent FROM alerts WHERE trip_id = %s", (trip_id,)
    ).fetchone()
    conn.close()
    return row["last_sent"]
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest tests/test_storage.py -v`
Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add schema.sql flight_tracker/storage.py tests/test_storage.py
git commit -m "$(cat <<'EOF'
Add an alerts table and functions to record and read alerts

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `notify_alerts` returns whether Telegram accepted the message

**Files:**
- Modify: `flight_tracker/notifier.py:75-95`
- Test: `tests/test_notifier.py`

**Interfaces:**
- Consumes: `notifier.send_message(text: str) -> bool` (exists).
- Produces: `notifier.notify_alerts(good_deals: list) -> bool`. It returns `True` when Telegram accepted the message. It returns `False` for an empty list or a rejected message.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_notifier.py`:

```python
def _one_alert():
    return [{
        "trip_description": "Beach trip", "trip_id": 1, "origin": "AMS", "destination": "BKK",
        "depart_date": "2026-12-05", "return_date": "2026-12-19",
        "airline": "KLM", "duration": "12h", "stops": 0, "price": "€650",
        "price_value": 650.0, "url": "",
    }]


def test_notify_alerts_returns_true_when_the_message_was_sent():
    with patch("flight_tracker.notifier.send_message", return_value=True):
        assert notifier.notify_alerts(_one_alert()) is True


def test_notify_alerts_returns_false_when_telegram_rejects_the_message():
    with patch("flight_tracker.notifier.send_message", return_value=False):
        assert notifier.notify_alerts(_one_alert()) is False


def test_notify_alerts_returns_false_for_an_empty_list():
    with patch("flight_tracker.notifier.send_message") as mock_send:
        assert notifier.notify_alerts([]) is False

    mock_send.assert_not_called()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `source .venv/bin/activate && python -m pytest tests/test_notifier.py -v`
Expected: the three new tests FAIL with `assert None is True` (or `is False`).

- [ ] **Step 3: Write the implementation**

In `flight_tracker/notifier.py`, replace this text:

```python
def notify_alerts(good_deals: list) -> None:
    """Send a Telegram message for each result flagged as a good deal."""
    if not good_deals:
        return
```

with:

```python
def notify_alerts(good_deals: list) -> bool:
    """Send one Telegram message for the flagged results. True when Telegram accepted it."""
    if not good_deals:
        return False
```

Then, in the same function, replace the last line:

```python
    send_message("\n".join(lines))


def notify_summary(trip_summaries: list) -> None:
```

with:

```python
    return send_message("\n".join(lines))


def notify_summary(trip_summaries: list) -> None:
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest tests/test_notifier.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add flight_tracker/notifier.py tests/test_notifier.py
git commit -m "$(cat <<'EOF'
Return from notify_alerts whether Telegram accepted the message

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Settings and exact price facts

**Files:**
- Modify: `flight_tracker/deals.py`
- Test: `tests/test_deals.py`

**Interfaces:**
- Consumes: `storage.DATABASE_URL` (exists).
- Produces (all in `flight_tracker/deals.py`):
  - `DEFAULT_SETTINGS: dict` with keys `model`, `state_mode`, `candidates_per_trip`, `min_price_noul`, `min_flight_noul`, `alert_cooldown_hours`, `history_days`, `min_route_history`.
  - `load_settings(config: dict) -> dict`. It raises `ValueError` when `state_mode` is not `"facts"` or `"raw"`.
  - `percent_below(price_value: float, reference: float) -> float`
  - `discount_label(pct_below: float) -> str`
  - `trip_low_label(price_value: float, trip_low: float) -> str`
  - `route_sentence(prices: list, price_value: float, origin: str, destination: str, history_days: int, min_route_history: int) -> str`
  - `trip_low_sentence(price_value: float, trip_low: Optional[float], count: int) -> str`
  - `estimate_sentence(price_value: float, baseline_price_estimate, passenger_count: int) -> str`
  - `route_prices(trip_id: int, origin: str, destination: str, before: datetime, since: datetime) -> list`
  - `trip_low_and_count(trip_id: int, before: datetime) -> tuple`
  - `price_facts(result: dict, trip_id: int, baseline_price_estimate, passenger_count: int, check_started_at: datetime, settings: dict) -> dict` with keys `route`, `trip_low`, `estimate`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_deals.py`, replace the import block at the top:

```python
import psycopg

from flight_tracker import storage
from flight_tracker import trips
from flight_tracker import deals
```

with:

```python
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from flight_tracker import storage
from flight_tracker import trips
from flight_tracker import deals
```

Then append to the end of `tests/test_deals.py`:

```python
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def _seed_rows(trip_id, rows):
    conn = psycopg.connect(storage.DATABASE_URL)
    for checked_at, origin, destination, price_value in rows:
        conn.execute(
            "INSERT INTO prices (checked_at, origin, destination, depart_date, "
            "price_value, trip_id) VALUES (%s, %s, %s, '2026-07-11', %s, %s)",
            (checked_at, origin, destination, price_value, trip_id),
        )
    conn.commit()
    conn.close()


def test_load_settings_uses_defaults_without_a_block():
    assert deals.load_settings({}) == deals.DEFAULT_SETTINGS
    assert deals.load_settings({"deal_check": None}) == deals.DEFAULT_SETTINGS


def test_load_settings_overrides_only_the_given_keys():
    settings = deals.load_settings({"deal_check": {"min_price_noul": 0.9, "state_mode": "raw"}})

    assert settings["min_price_noul"] == 0.9
    assert settings["state_mode"] == "raw"
    assert settings["min_flight_noul"] == 0.70


def test_load_settings_rejects_an_unknown_state_mode():
    with pytest.raises(ValueError, match="state_mode"):
        deals.load_settings({"deal_check": {"state_mode": "guess"}})


def test_percent_below_is_positive_below_the_reference():
    assert deals.percent_below(612, 1000) == pytest.approx(38.8)
    assert deals.percent_below(1100, 1000) == pytest.approx(-10.0)


@pytest.mark.parametrize("pct_below, expected", [
    (38, "far below usual"),
    (25, "far below usual"),
    (24.9, "somewhat below usual"),
    (10, "somewhat below usual"),
    (9.9, "about the same"),
    (0, "about the same"),
    (-10, "about the same"),
    (-10.1, "above usual"),
])
def test_discount_label_cut_points(pct_below, expected):
    assert deals.discount_label(pct_below) == expected


@pytest.mark.parametrize("price, low, expected", [
    (500, 500, "new lowest"),
    (450, 500, "new lowest"),
    (525, 500, "close to the lowest"),
    (526, 500, "well above the lowest"),
])
def test_trip_low_label_cut_points(price, low, expected):
    assert deals.trip_low_label(price, low) == expected


def test_route_sentence_far_below_the_median():
    sentence = deals.route_sentence([1000.0] * 10, 620.0, "AMS", "GRU", 30, 5)

    assert sentence == (
        "far below usual. 38% below the median of 10 prices seen for AMS to GRU "
        "in the last 30 days."
    )


def test_route_sentence_above_the_median():
    sentence = deals.route_sentence([100.0] * 6, 130.0, "AMS", "GRU", 30, 5)

    assert sentence == (
        "above usual. 30% above the median of 6 prices seen for AMS to GRU "
        "in the last 30 days."
    )


def test_route_sentence_reports_thin_history():
    assert deals.route_sentence([100.0, 110.0, 120.0], 90.0, "AMS", "GRU", 30, 5) == (
        "Not enough history: only 3 prices seen for AMS to GRU."
    )
    assert deals.route_sentence([100.0], 90.0, "AMS", "GRU", 30, 5) == (
        "Not enough history: only 1 price seen for AMS to GRU."
    )


def test_trip_low_sentence_for_a_new_lowest_price():
    assert deals.trip_low_sentence(480.0, 500.0, 340) == (
        "new lowest. The lowest of 340 prices recorded for this trip."
    )


def test_trip_low_sentence_for_a_price_above_the_lowest():
    assert deals.trip_low_sentence(525.0, 500.0, 340) == (
        "close to the lowest. 5% above the lowest of 340 prices recorded for this trip (500)."
    )


def test_trip_low_sentence_without_history():
    assert deals.trip_low_sentence(480.0, None, 0) == "No earlier prices recorded for this trip."


def test_estimate_sentence_far_below_the_estimate():
    assert deals.estimate_sentence(612.0, 445.0, 2) == (
        "far below usual. 31% below the estimated normal fare of 890 for 2 passengers."
    )


def test_estimate_sentence_for_one_passenger_and_missing_estimate():
    assert deals.estimate_sentence(890.0, 1000.0, 1) == (
        "somewhat below usual. 11% below the estimated normal fare of 1000 for 1 passenger."
    )
    assert deals.estimate_sentence(900.0, None, 1) == "No estimate available."
    assert deals.estimate_sentence(900.0, 0, 1) == "No estimate available."


def test_route_prices_filters_by_route_window_and_check_start():
    trip_id = _make_trip()
    _seed_rows(trip_id, [
        (NOW - timedelta(days=1), "AMS", "GRU", 900.0),
        (NOW - timedelta(days=29), "AMS", "GRU", 950.0),
        (NOW - timedelta(days=31), "AMS", "GRU", 800.0),
        (NOW - timedelta(days=1), "BRU", "GRU", 700.0),
        (NOW - timedelta(days=1), "AMS", "BKK", 600.0),
        (NOW, "AMS", "GRU", 500.0),
    ])

    prices = deals.route_prices(trip_id, "AMS", "GRU", before=NOW, since=NOW - timedelta(days=30))

    assert sorted(prices) == [900.0, 950.0]


def test_trip_low_and_count_ignores_the_current_check():
    trip_id = _make_trip()
    _seed_rows(trip_id, [
        (NOW - timedelta(days=2), "AMS", "GRU", 900.0),
        (NOW - timedelta(days=1), "BRU", "GRU", 700.0),
        (NOW, "AMS", "GRU", 100.0),
    ])

    assert deals.trip_low_and_count(trip_id, before=NOW) == (700.0, 2)


def test_trip_low_and_count_without_history():
    trip_id = _make_trip()

    assert deals.trip_low_and_count(trip_id, before=NOW) == (None, 0)


def test_price_facts_combines_the_three_sentences():
    trip_id = _make_trip()
    _seed_rows(trip_id, [
        (NOW - timedelta(days=day), "AMS", "GRU", 1000.0) for day in range(1, 7)
    ])
    result = {"origin": "AMS", "destination": "GRU", "price_value": 620.0}

    facts = deals.price_facts(result, trip_id, 445.0, 2, NOW, deals.DEFAULT_SETTINGS)

    assert facts == {
        "route": (
            "far below usual. 38% below the median of 6 prices seen for AMS to GRU "
            "in the last 30 days."
        ),
        "trip_low": "new lowest. The lowest of 6 prices recorded for this trip.",
        "estimate": (
            "far below usual. 30% below the estimated normal fare of 890 for 2 passengers."
        ),
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `source .venv/bin/activate && python -m pytest tests/test_deals.py -v`
Expected: the new tests FAIL with `AttributeError: module 'flight_tracker.deals' has no attribute 'load_settings'` (and similar). The seven old tests still PASS.

- [ ] **Step 3: Add the imports to `deals.py`**

In `flight_tracker/deals.py`, replace:

```python
import statistics

import psycopg
```

with:

```python
import statistics
from datetime import datetime, timedelta
from typing import Optional

import psycopg
```

- [ ] **Step 4: Append the implementation to `deals.py`**

Append to the end of `flight_tracker/deals.py`:

```python


DEFAULT_SETTINGS = {
    "model": "jev-1.13.0",
    "state_mode": "facts",
    "candidates_per_trip": 5,
    "min_price_noul": 0.85,
    "min_flight_noul": 0.70,
    "alert_cooldown_hours": 24,
    "history_days": 30,
    "min_route_history": 5,
}

STATE_MODES = ("facts", "raw")


def load_settings(config: dict) -> dict:
    settings = dict(DEFAULT_SETTINGS)
    settings.update(config.get("deal_check") or {})
    if settings["state_mode"] not in STATE_MODES:
        raise ValueError(
            f"state_mode must be one of {STATE_MODES}, got {settings['state_mode']!r}"
        )
    return settings


def percent_below(price_value: float, reference: float) -> float:
    return (reference - price_value) * 100 / reference


def discount_label(pct_below: float) -> str:
    if pct_below >= 25:
        return "far below usual"
    if pct_below >= 10:
        return "somewhat below usual"
    if pct_below >= -10:
        return "about the same"
    return "above usual"


def trip_low_label(price_value: float, trip_low: float) -> str:
    if price_value <= trip_low:
        return "new lowest"
    if price_value * 100 <= trip_low * 105:
        return "close to the lowest"
    return "well above the lowest"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def route_sentence(
    prices: list, price_value: float, origin: str, destination: str,
    history_days: int, min_route_history: int,
) -> str:
    if len(prices) < min_route_history:
        return (
            f"Not enough history: only {_plural(len(prices), 'price')} "
            f"seen for {origin} to {destination}."
        )
    pct = percent_below(price_value, statistics.median(prices))
    direction = "below" if pct >= 0 else "above"
    return (
        f"{discount_label(pct)}. {abs(pct):.0f}% {direction} the median of "
        f"{len(prices)} prices seen for {origin} to {destination} "
        f"in the last {history_days} days."
    )


def trip_low_sentence(price_value: float, trip_low: Optional[float], count: int) -> str:
    if trip_low is None or count == 0:
        return "No earlier prices recorded for this trip."
    label = trip_low_label(price_value, trip_low)
    if label == "new lowest":
        return f"{label}. The lowest of {count} prices recorded for this trip."
    pct_above = (price_value - trip_low) * 100 / trip_low
    return (
        f"{label}. {pct_above:.0f}% above the lowest of {count} prices "
        f"recorded for this trip ({trip_low:.0f})."
    )


def estimate_sentence(price_value: float, baseline_price_estimate, passenger_count: int) -> str:
    if not baseline_price_estimate:
        return "No estimate available."
    estimate = baseline_price_estimate * passenger_count
    pct = percent_below(price_value, estimate)
    direction = "below" if pct >= 0 else "above"
    return (
        f"{discount_label(pct)}. {abs(pct):.0f}% {direction} the estimated normal fare "
        f"of {estimate:.0f} for {_plural(passenger_count, 'passenger')}."
    )


def route_prices(
    trip_id: int, origin: str, destination: str, before: datetime, since: datetime,
) -> list:
    conn = psycopg.connect(storage.DATABASE_URL)
    rows = conn.execute(
        "SELECT price_value FROM prices "
        "WHERE trip_id = %s AND origin = %s AND destination = %s "
        "AND checked_at < %s AND checked_at >= %s AND price_value IS NOT NULL",
        (trip_id, origin, destination, before, since),
    ).fetchall()
    conn.close()
    return [row[0] for row in rows]


def trip_low_and_count(trip_id: int, before: datetime) -> tuple:
    conn = psycopg.connect(storage.DATABASE_URL)
    row = conn.execute(
        "SELECT min(price_value), count(price_value) FROM prices "
        "WHERE trip_id = %s AND checked_at < %s",
        (trip_id, before),
    ).fetchone()
    conn.close()
    return row[0], row[1]


def price_facts(
    result: dict, trip_id: int, baseline_price_estimate, passenger_count: int,
    check_started_at: datetime, settings: dict,
) -> dict:
    since = check_started_at - timedelta(days=settings["history_days"])
    route = route_prices(
        trip_id, result["origin"], result["destination"], check_started_at, since
    )
    trip_low, trip_count = trip_low_and_count(trip_id, check_started_at)
    price_value = result["price_value"]
    return {
        "route": route_sentence(
            route, price_value, result["origin"], result["destination"],
            settings["history_days"], settings["min_route_history"],
        ),
        "trip_low": trip_low_sentence(price_value, trip_low, trip_count),
        "estimate": estimate_sentence(price_value, baseline_price_estimate, passenger_count),
    }
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest tests/test_deals.py -v`
Expected: all tests PASS (old and new).

- [ ] **Step 6: Commit**

```bash
git add flight_tracker/deals.py tests/test_deals.py
git commit -m "$(cat <<'EOF'
Compute exact price facts and deal check settings

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Jev questions, candidate judging, selection, and cooldown

**Files:**
- Modify: `flight_tracker/deals.py`
- Test: `tests/test_deals.py`

**Interfaces:**
- Consumes:
  - `jev_client.judge(state, questions: dict, model: str) -> dict` and `jev_client.JevError` (Task 1).
  - `storage.last_alert_time(trip_id: int)` (Task 2).
  - `price_facts(...)`, `DEFAULT_SETTINGS` (Task 4).
- Produces (all in `flight_tracker/deals.py`):
  - `flight_state(result: dict, passenger_count: int) -> dict`
  - `build_state(result: dict, facts: Optional[dict], passenger_count: int, mode: str) -> dict`
  - `build_questions(mode: str) -> dict` with keys `price_is_very_good` and `flight_is_acceptable`.
  - `judge_candidate(result: dict, trip_id: int, baseline_price_estimate, passenger_count: int, check_started_at: datetime, settings: dict) -> dict` returns `{"price": float, "flight": float}`.
  - `passes(verdict: dict, settings: dict) -> bool`
  - `pick_best(passing: list) -> Optional[dict]`. `passing` is a list of `(result, verdict)` pairs.
  - `in_cooldown(trip_id: int, now: datetime, settings: dict) -> bool`
  - `evaluate_trip(trip_id: int, results: list, baseline_price_estimate, passenger_count: int, check_started_at: datetime, settings: dict) -> tuple` returns `(passing_results: list, best: Optional[dict])`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_deals.py`, add this line to the import block, after `import pytest`:

```python
from unittest.mock import patch
```

Then append to the end of `tests/test_deals.py`:

```python
RESULT = {
    "origin": "AMS", "destination": "GRU", "depart_date": "2026-07-11",
    "return_date": "2026-08-08", "airline": "KLM", "departure": "10:05",
    "arrival": "18:40", "duration": "14h 30m", "stops": 1, "price": "€620",
    "price_value": 620.0,
}


def _r(price, airline="KLM"):
    return dict(RESULT, price_value=float(price), airline=airline)


def test_flight_state_has_named_fields_and_a_price_with_passengers():
    assert deals.flight_state(RESULT, 2) == {
        "route": "AMS to GRU", "depart_date": "2026-07-11", "return_date": "2026-08-08",
        "airline": "KLM", "stops": 1, "duration": "14h 30m", "departure": "10:05",
        "arrival": "18:40", "price": "620 for 2 passengers",
    }


def test_build_state_adds_price_facts_only_in_facts_mode():
    facts = {"route": "r", "trip_low": "t", "estimate": "e"}

    assert deals.build_state(RESULT, facts, 1, "facts")["price_facts"] == facts
    assert "price_facts" not in deals.build_state(RESULT, None, 1, "raw")


def test_build_questions_has_two_nouls_in_both_modes():
    for mode in ("facts", "raw"):
        questions = deals.build_questions(mode)

        assert set(questions) == {"price_is_very_good", "flight_is_acceptable"}
        assert all(question["type"] == "noul" for question in questions.values())


def test_build_questions_facts_mode_points_the_price_question_at_price_facts():
    price = deals.build_questions("facts")["price_is_very_good"]

    assert "`price_facts`" in price["instructions"]
    assert "true" in price["criteria"] and "false" in price["criteria"]


def test_build_questions_raw_mode_points_the_price_question_at_the_flight_price():
    price = deals.build_questions("raw")["price_is_very_good"]

    assert "`flight.price`" in price["instructions"]
    assert "criteria" not in price


def test_judge_candidate_sends_the_facts_state_and_returns_both_answers():
    trip_id = _make_trip()
    answers = {"price_is_very_good": 0.93, "flight_is_acceptable": 0.81}

    with patch("flight_tracker.deals.jev_client.judge", return_value=answers) as judge:
        verdict = deals.judge_candidate(RESULT, trip_id, 445.0, 2, NOW, deals.DEFAULT_SETTINGS)

    assert verdict == {"price": 0.93, "flight": 0.81}
    state, questions, model = judge.call_args.args
    assert set(state) == {"flight", "price_facts"}
    assert set(questions) == {"price_is_very_good", "flight_is_acceptable"}
    assert model == "jev-1.13.0"


def test_judge_candidate_in_raw_mode_skips_the_history_queries():
    settings = dict(deals.DEFAULT_SETTINGS, state_mode="raw")
    answers = {"price_is_very_good": 0.5, "flight_is_acceptable": 0.5}

    with patch("flight_tracker.deals.jev_client.judge", return_value=answers) as judge, \
         patch("flight_tracker.deals.price_facts") as facts:
        deals.judge_candidate(RESULT, 1, None, 1, NOW, settings)

    facts.assert_not_called()
    assert set(judge.call_args.args[0]) == {"flight"}


def test_passes_needs_both_answers_at_the_limits():
    settings = deals.DEFAULT_SETTINGS

    assert deals.passes({"price": 0.85, "flight": 0.70}, settings) is True
    assert deals.passes({"price": 0.84, "flight": 0.90}, settings) is False
    assert deals.passes({"price": 0.99, "flight": 0.69}, settings) is False


def test_pick_best_takes_the_cheapest_passing_result():
    passing = [
        (_r(700, "A"), {"price": 0.99, "flight": 0.9}),
        (_r(650, "B"), {"price": 0.90, "flight": 0.8}),
    ]

    assert deals.pick_best(passing)["airline"] == "B"


def test_pick_best_breaks_a_price_tie_with_the_higher_price_answer():
    passing = [
        (_r(650, "A"), {"price": 0.88, "flight": 0.9}),
        (_r(650, "B"), {"price": 0.97, "flight": 0.8}),
    ]

    assert deals.pick_best(passing)["airline"] == "B"


def test_pick_best_returns_none_for_no_passing_results():
    assert deals.pick_best([]) is None


def test_in_cooldown_is_false_without_an_alert():
    trip_id = _make_trip()

    assert deals.in_cooldown(trip_id, NOW, deals.DEFAULT_SETTINGS) is False


def test_in_cooldown_is_true_inside_the_window_and_false_after():
    trip_id = _make_trip()
    storage.record_alerts([{
        "trip_id": trip_id, "origin": "AMS", "destination": "GRU",
        "depart_date": "2026-07-11", "return_date": "2026-08-08",
        "airline": "KLM", "price_value": 600.0,
    }])
    sent = storage.last_alert_time(trip_id)
    settings = dict(deals.DEFAULT_SETTINGS, alert_cooldown_hours=24)

    assert deals.in_cooldown(trip_id, sent + timedelta(hours=23), settings) is True
    assert deals.in_cooldown(trip_id, sent + timedelta(hours=24), settings) is False


def test_evaluate_trip_judges_only_the_cheapest_candidates():
    results = [_r(price) for price in (900, 500, 700, 600, 800, 400, 300)]
    settings = dict(deals.DEFAULT_SETTINGS, candidates_per_trip=3)
    verdict = {"price": 0.0, "flight": 0.0}

    with patch("flight_tracker.deals.judge_candidate", return_value=verdict) as judge:
        passing, best = deals.evaluate_trip(1, results, None, 1, NOW, settings)

    assert [call.args[0]["price_value"] for call in judge.call_args_list] == [300.0, 400.0, 500.0]
    assert passing == []
    assert best is None


def test_evaluate_trip_returns_passing_results_and_the_cheapest_as_best():
    results = [_r(500, "A"), _r(600, "B"), _r(700, "C")]
    verdicts = {
        "A": {"price": 0.5, "flight": 0.9},
        "B": {"price": 0.9, "flight": 0.9},
        "C": {"price": 0.95, "flight": 0.8},
    }

    with patch("flight_tracker.deals.judge_candidate", side_effect=lambda r, *a: verdicts[r["airline"]]):
        passing, best = deals.evaluate_trip(1, results, None, 1, NOW, deals.DEFAULT_SETTINGS)

    assert [r["airline"] for r in passing] == ["B", "C"]
    assert best["airline"] == "B"


def test_evaluate_trip_skips_a_candidate_when_jev_fails(caplog):
    results = [_r(500, "A"), _r(600, "B")]

    def fake(result, *args):
        if result["airline"] == "A":
            raise deals.jev_client.JevError("HTTP 503")
        return {"price": 0.9, "flight": 0.9}

    with patch("flight_tracker.deals.judge_candidate", side_effect=fake), \
         caplog.at_level("ERROR"):
        passing, best = deals.evaluate_trip(1, results, None, 1, NOW, deals.DEFAULT_SETTINGS)

    assert best["airline"] == "B"
    assert any("HTTP 503" in record.message for record in caplog.records)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `source .venv/bin/activate && python -m pytest tests/test_deals.py -v`
Expected: the new tests FAIL with `AttributeError: module 'flight_tracker.deals' has no attribute 'flight_state'` (and similar).

- [ ] **Step 3: Add the imports to `deals.py`**

In `flight_tracker/deals.py`, replace:

```python
import statistics
from datetime import datetime, timedelta
```

with:

```python
import logging
import statistics
from datetime import datetime, timedelta
```

Then replace:

```python
from . import storage

MIN_HISTORY_FOR_PERCENTILE = 5
```

with:

```python
from . import jev_client
from . import storage

logger = logging.getLogger(__name__)

MIN_HISTORY_FOR_PERCENTILE = 5
```

- [ ] **Step 4: Append the implementation to `deals.py`**

Append to the end of `flight_tracker/deals.py`:

```python


_FLIGHT_QUESTION = {
    "type": "noul",
    "instructions": (
        "Judging by `flight`, is this an acceptable trip for a traveller "
        "who wants a reasonable journey?"
    ),
    "criteria": {
        "true": (
            "Few stops, a sensible total duration for the route, and no very "
            "awkward departure or arrival time."
        ),
        "false": (
            "Many stops, a very long duration for the route, or a very awkward "
            "departure or arrival time."
        ),
    },
}

_FACTS_PRICE_QUESTION = {
    "type": "noul",
    "instructions": (
        "Judging by the labels and sentences in `price_facts`, is this price a very "
        "good deal, well below what is normal for this trip?"
    ),
    "criteria": {
        "true": (
            "At least one fact shows the price far below usual, or a new lowest price "
            "for the trip, and no fact shows the price above usual."
        ),
        "false": (
            "The facts show a price near or above usual, or there is too little "
            "history to show the price is low."
        ),
    },
}

_RAW_PRICE_QUESTION = {
    "type": "noul",
    "instructions": (
        "Judging by `flight.price` and your general knowledge of fares on this "
        "route, is this price a very good deal?"
    ),
}


def flight_state(result: dict, passenger_count: int) -> dict:
    return {
        "route": f"{result['origin']} to {result['destination']}",
        "depart_date": result["depart_date"],
        "return_date": result["return_date"],
        "airline": result["airline"],
        "stops": result["stops"],
        "duration": result["duration"],
        "departure": result["departure"],
        "arrival": result["arrival"],
        "price": f"{result['price_value']:.0f} for {_plural(passenger_count, 'passenger')}",
    }


def build_state(result: dict, facts: Optional[dict], passenger_count: int, mode: str) -> dict:
    state = {"flight": flight_state(result, passenger_count)}
    if mode == "facts":
        state["price_facts"] = facts
    return state


def build_questions(mode: str) -> dict:
    price_question = _RAW_PRICE_QUESTION if mode == "raw" else _FACTS_PRICE_QUESTION
    return {
        "price_is_very_good": price_question,
        "flight_is_acceptable": _FLIGHT_QUESTION,
    }


def judge_candidate(
    result: dict, trip_id: int, baseline_price_estimate, passenger_count: int,
    check_started_at: datetime, settings: dict,
) -> dict:
    mode = settings["state_mode"]
    facts = None
    if mode == "facts":
        facts = price_facts(
            result, trip_id, baseline_price_estimate, passenger_count,
            check_started_at, settings,
        )
    state = build_state(result, facts, passenger_count, mode)
    answers = jev_client.judge(state, build_questions(mode), settings["model"])
    return {
        "price": answers["price_is_very_good"],
        "flight": answers["flight_is_acceptable"],
    }


def passes(verdict: dict, settings: dict) -> bool:
    return (
        verdict["price"] >= settings["min_price_noul"]
        and verdict["flight"] >= settings["min_flight_noul"]
    )


def pick_best(passing: list) -> Optional[dict]:
    if not passing:
        return None
    return min(passing, key=lambda pair: (pair[0]["price_value"], -pair[1]["price"]))[0]


def in_cooldown(trip_id: int, now: datetime, settings: dict) -> bool:
    last = storage.last_alert_time(trip_id)
    if last is None:
        return False
    return now - last < timedelta(hours=settings["alert_cooldown_hours"])


def evaluate_trip(
    trip_id: int, results: list, baseline_price_estimate, passenger_count: int,
    check_started_at: datetime, settings: dict,
) -> tuple:
    """Judge the cheapest candidates of one trip. Returns (passing results, best or None)."""
    candidates = sorted(results, key=lambda r: r["price_value"])[: settings["candidates_per_trip"]]

    passing = []
    for result in candidates:
        try:
            verdict = judge_candidate(
                result, trip_id, baseline_price_estimate, passenger_count,
                check_started_at, settings,
            )
        except jev_client.JevError as exc:
            logger.error(
                "Jev check failed for trip #%s %s to %s %s: %s",
                trip_id, result["origin"], result["destination"], result["depart_date"], exc,
            )
            continue

        logger.info(
            "Trip #%s %s to %s %s %s at %.0f: price=%.2f flight=%.2f",
            trip_id, result["origin"], result["destination"], result["depart_date"],
            result["airline"], result["price_value"], verdict["price"], verdict["flight"],
        )
        if passes(verdict, settings):
            passing.append((result, verdict))

    return [result for result, _ in passing], pick_best(passing)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest tests/test_deals.py -v`
Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add flight_tracker/deals.py tests/test_deals.py
git commit -m "$(cat <<'EOF'
Judge candidate flights with Jev and pick the best one

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Wire the deal check into `run_check()` and remove the old rule

**Files:**
- Modify: `main.py`
- Modify: `flight_tracker/deals.py`
- Modify: `tests/test_deals.py`
- Modify: `tests/test_main.py`

**Interfaces:**
- Consumes:
  - `deals.load_settings`, `deals.in_cooldown`, `deals.evaluate_trip` (Tasks 4, 5).
  - `jev_client.is_configured()` (Task 1).
  - `storage.record_alerts` (Task 2).
  - `notifier.notify_alerts(...) -> bool` (Task 3).
- Produces: `main.run_check()` with the new flow. `deals.is_good_deal` no longer exists.

- [ ] **Step 1: Replace `tests/test_main.py`**

Overwrite `tests/test_main.py` with:

```python
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import main
from flight_tracker import storage
from flight_tracker import trips

CONFIG = {
    "origins": ["AMS"], "seat": "economy", "passengers": {},
    "max_duration_hours": 0, "results_per_query": 3,
}


def _trip(trip_id=1, description="Beach trip", baseline=None, passengers=None):
    return {
        "id": trip_id, "description": description, "destinations": ["BKK"],
        "origins": None, "ideal_date": "2026-12-05", "ideal_return_date": "2026-12-19",
        "departure_range_before": 1, "departure_range_after": 1,
        "return_range_before": 1, "return_range_after": 1,
        "seat": None, "passengers": passengers, "max_duration_hours": None,
        "results_per_query": None, "baseline_price_estimate": baseline,
        "status": "active", "created_at": "2026-01-01T00:00:00+00:00",
    }


def _result(price_value, origin="AMS", destination="BKK"):
    return {
        "origin": origin, "destination": destination, "depart_date": "2026-12-05",
        "return_date": "2026-12-19", "airline": "TG", "departure": "10:00",
        "arrival": "20:00", "duration": "12h", "stops": 0,
        "price": f"€{price_value:.0f}", "price_value": price_value,
        "is_best": True, "current_price_level": "low", "url": "https://example.com",
    }


def _run(active, search, *, config=None, jev_ready=True, cooldown=False,
         evaluation=None, telegram=True, notified=True):
    config = CONFIG if config is None else config
    with patch("main.load_config", return_value=config), \
         patch("main.trips.get_active_trips", return_value=active), \
         patch("main.tracker.search_flights", side_effect=search), \
         patch("main.storage.write_results") as write, \
         patch("main.storage.record_alerts") as record, \
         patch("main.jev_client.is_configured", return_value=jev_ready), \
         patch("main.deals.in_cooldown", return_value=cooldown) as in_cooldown, \
         patch("main.deals.evaluate_trip", return_value=evaluation or ([], None)) as evaluate, \
         patch("main.display.print_results") as print_results, \
         patch("main.display.print_alerts") as print_alerts, \
         patch("main.is_configured", return_value=telegram), \
         patch("main.notify_alerts", return_value=notified) as notify:
        main.run_check()
    return SimpleNamespace(
        write=write, record=record, in_cooldown=in_cooldown, evaluate=evaluate,
        print_results=print_results, print_alerts=print_alerts, notify=notify,
    )


def test_run_check_tags_results_and_alerts_the_best_flight():
    trip = _trip(baseline=700)
    combo = [_result(500), _result(520)]
    best = combo[0]

    ran = _run([trip], lambda merged: iter([combo]), evaluation=([combo[0]], best))

    ran.write.assert_called_once_with(combo, trip_id=1)
    results = ran.print_results.call_args[0][0]
    assert [r["trip_description"] for r in results] == ["Beach trip", "Beach trip"]
    assert [r["trip_id"] for r in results] == [1, 1]
    assert [r["is_good_deal"] for r in results] == [True, False]
    ran.print_alerts.assert_called_once_with([best])
    ran.notify.assert_called_once_with([best])
    ran.record.assert_called_once_with([best])


def test_run_check_passes_passenger_count_and_baseline_to_evaluate_trip():
    trip = _trip(baseline=500, passengers={"adults": 2, "children": 1})
    combo = [_result(1000)]

    ran = _run([trip], lambda merged: iter([combo]))

    args = ran.evaluate.call_args.args
    assert args[0] == 1
    assert args[1] == combo
    assert args[2] == 500
    assert args[3] == 3
    assert isinstance(args[4], datetime)
    assert args[5]["candidates_per_trip"] == 5


def test_run_check_skips_the_jev_check_for_a_trip_in_cooldown():
    ran = _run([_trip()], lambda merged: iter([[_result(500)]]), cooldown=True)

    ran.evaluate.assert_not_called()
    ran.notify.assert_not_called()


def test_run_check_skips_the_jev_check_when_a_trip_has_no_results():
    ran = _run([_trip()], lambda merged: iter([[]]))

    ran.in_cooldown.assert_not_called()
    ran.evaluate.assert_not_called()


def test_run_check_skips_the_deal_check_when_the_key_is_missing(caplog):
    with caplog.at_level("ERROR"):
        ran = _run([_trip()], lambda merged: iter([[_result(500)]]), jev_ready=False)

    ran.evaluate.assert_not_called()
    ran.print_results.assert_called_once()
    assert any("TYPESAFE_API_KEY" in record.message for record in caplog.records)


def test_run_check_skips_the_deal_check_for_invalid_settings(caplog):
    config = dict(CONFIG, deal_check={"state_mode": "guess"})

    with caplog.at_level("ERROR"):
        ran = _run([_trip()], lambda merged: iter([[_result(500)]]), config=config)

    ran.evaluate.assert_not_called()
    assert any("state_mode" in record.message for record in caplog.records)


def test_run_check_does_not_record_alerts_when_telegram_rejects_the_message():
    combo = [_result(500)]

    ran = _run(
        [_trip()], lambda merged: iter([combo]),
        evaluation=([combo[0]], combo[0]), notified=False,
    )

    ran.notify.assert_called_once()
    ran.record.assert_not_called()


def test_run_check_sends_nothing_when_telegram_is_not_configured():
    combo = [_result(500)]

    ran = _run(
        [_trip()], lambda merged: iter([combo]),
        evaluation=([combo[0]], combo[0]), telegram=False,
    )

    ran.notify.assert_not_called()
    ran.record.assert_not_called()


def test_run_check_skips_a_trip_that_raises_and_continues():
    good_trip = _trip(trip_id=2, description="Good trip")
    bad_trip = _trip(trip_id=1, description="Bad trip")
    combo = [_result(500)]

    def fake_search(merged):
        if merged["id"] == 1:
            raise ValueError("bad date")
        return iter([combo])

    ran = _run([bad_trip, good_trip], fake_search)

    results = ran.print_results.call_args[0][0]
    assert len(results) == 1
    assert results[0]["trip_description"] == "Good trip"


def test_run_check_never_sends_the_unconditional_summary():
    """The routine per-check summary is disabled: main no longer holds a
    reference to notify_summary at all, so there is no path left that
    could call it."""
    assert not hasattr(main, "notify_summary")


def test_run_check_logs_and_returns_when_loading_trips_fails(caplog):
    with patch("main.load_config", return_value={}), \
         patch("main.trips.get_active_trips", side_effect=RuntimeError("db down")), \
         patch("main.display.print_results") as mock_print_results:
        with caplog.at_level("ERROR"):
            main.run_check()

    mock_print_results.assert_not_called()
    assert any("db down" in r.message for r in caplog.records)


def test_run_check_end_to_end_alerts_once_then_respects_the_cooldown():
    trip_id = trips.create_trip(
        description="Beach trip", destinations=["BKK"],
        ideal_date="2099-12-05", ideal_return_date="2099-12-19",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
        baseline_price_estimate=700,
    )
    combo = [_result(500), _result(650)]
    verdict = {"price_is_very_good": 0.95, "flight_is_acceptable": 0.9}

    with patch("main.load_config", return_value=CONFIG), \
         patch("main.tracker.search_flights",
               side_effect=lambda merged: iter([[dict(r) for r in combo]])), \
         patch("main.jev_client.is_configured", return_value=True), \
         patch("flight_tracker.jev_client.judge", return_value=verdict), \
         patch("main.display.print_results"), \
         patch("main.display.print_alerts"), \
         patch("main.is_configured", return_value=True), \
         patch("main.notify_alerts", return_value=True) as notify:
        main.run_check()
        main.run_check()

    notify.assert_called_once()
    sent = notify.call_args[0][0]
    assert len(sent) == 1
    assert sent[0]["price_value"] == 500
    assert sent[0]["trip_id"] == trip_id
    assert storage.last_alert_time(trip_id) is not None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `source .venv/bin/activate && python -m pytest tests/test_main.py -v`
Expected: the tests FAIL. The first error is `AttributeError: <module 'main'> does not have the attribute 'jev_client'`.

- [ ] **Step 3: Replace `main.py`**

Overwrite `main.py` with:

```python
"""Flight price tracker — entry point."""

import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler

from flight_tracker import deals
from flight_tracker import jev_client
from flight_tracker import storage
from flight_tracker import tracker
from flight_tracker import trips
from flight_tracker import telegram_commands
from flight_tracker.display import console
from flight_tracker import display
from flight_tracker.notifier import is_configured, notify_alerts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _deal_check_settings(config: dict):
    try:
        settings = deals.load_settings(config)
    except ValueError as exc:
        logger.error("Invalid deal_check settings: %s — deal check skipped.", exc)
        return None
    if not jev_client.is_configured():
        logger.error("TYPESAFE_API_KEY is not set — deal check skipped.")
        return None
    return settings


def run_check() -> None:
    config = load_config()
    check_started_at = datetime.now(timezone.utc)

    console.print("\n[bold cyan]Starting price check…[/bold cyan]")

    try:
        active_trips = trips.get_active_trips()
    except Exception as exc:
        logger.error("Could not load active trips: %s", exc)
        return

    settings = _deal_check_settings(config)

    all_results = []
    all_alerts = []

    for trip in active_trips:
        try:
            merged = trips.merge_with_defaults(trip, config)
            passenger_count = merged["passengers"].get("adults", 1) + merged["passengers"].get("children", 0)

            trip_results = []
            for combo in tracker.search_flights(merged):
                if not combo:
                    continue
                storage.write_results(combo, trip_id=trip["id"])

                for result in combo:
                    result["trip_description"] = trip["description"]
                    result["trip_id"] = trip["id"]
                    result["is_good_deal"] = False

                trip_results.extend(combo)
                all_results.extend(combo)

            if not settings or not trip_results:
                continue
            if deals.in_cooldown(trip["id"], datetime.now(timezone.utc), settings):
                continue

            passing, best = deals.evaluate_trip(
                trip["id"], trip_results, trip["baseline_price_estimate"],
                passenger_count, check_started_at, settings,
            )
            for result in passing:
                result["is_good_deal"] = True
            if best:
                all_alerts.append(best)

        except Exception as exc:
            logger.error("Trip #%s (%s) failed: %s", trip["id"], trip["description"], exc)

    display.print_results(all_results)
    display.print_alerts(all_alerts)

    if all_alerts and is_configured():
        if notify_alerts(all_alerts):
            storage.record_alerts(all_alerts)


def main() -> None:
    console.print("[bold]Flight Tracker started.[/bold]")
    console.print(f"Config: [cyan]{CONFIG_PATH}[/cyan]")

    config = trips.migrate_config_file(CONFIG_PATH)
    interval = config.get("interval_minutes", 60)

    telegram_commands.start()

    # Run immediately on startup
    run_check()

    scheduler = BlockingScheduler()
    scheduler.add_job(run_check, "interval", minutes=interval)
    console.print(f"\n[dim]Next check in {interval} minutes. Press Ctrl+C to stop.[/dim]\n")

    try:
        scheduler.start()
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the `test_main.py` tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest tests/test_main.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Remove the old rule tests from `tests/test_deals.py`**

Run this from the repo root. It deletes the five `is_good_deal` tests and keeps every other test:

```bash
source .venv/bin/activate && python - <<'EOF'
import pathlib

path = pathlib.Path("tests/test_deals.py")
text = path.read_text()
start = text.index("def test_cold_start_no_baseline_never_alerts")
end = text.index("def test_cheapest_price_returns_none_when_no_history")
path.write_text(text[:start] + text[end:])
EOF
grep -c "is_good_deal" tests/test_deals.py
```

Expected: the `grep` prints `0`.

- [ ] **Step 6: Remove the old rule from `deals.py`**

In `flight_tracker/deals.py`, replace:

```python
"""Good-deal detection based on a trip's own historical prices."""
```

with:

```python
"""Good-deal detection: exact price facts computed in code, judged by Jev."""
```

Then replace:

```python
logger = logging.getLogger(__name__)

MIN_HISTORY_FOR_PERCENTILE = 5
PERCENTILE_THRESHOLD = 20
BASELINE_DISCOUNT = 0.85
```

with:

```python
logger = logging.getLogger(__name__)
```

Then delete the whole `is_good_deal` function, with the two blank lines after it. Replace this text:

```python
def is_good_deal(trip_id: int, price_value: float, baseline_price_estimate, passenger_count: int = 1) -> bool:
    history = _historical_prices(trip_id)

    if len(history) < MIN_HISTORY_FOR_PERCENTILE:
        if baseline_price_estimate is None:
            return False
        return price_value <= baseline_price_estimate * passenger_count * BASELINE_DISCOUNT

    percentile_cut = statistics.quantiles(history, n=100)[PERCENTILE_THRESHOLD - 1]
    return price_value <= percentile_cut


def cheapest_price(trip_id: int):
```

with:

```python
def cheapest_price(trip_id: int):
```

- [ ] **Step 7: Run the full test suite**

Run: `source .venv/bin/activate && python -m pytest -q`
Expected: all tests PASS. Then check that nothing uses the old rule:

Run: `grep -rn "is_good_deal(" --include='*.py' . | grep -v "^./.venv"`
Expected: no output.

- [ ] **Step 8: Commit**

```bash
git add main.py flight_tracker/deals.py tests/test_main.py tests/test_deals.py
git commit -m "$(cat <<'EOF'
Send one Jev-checked alert per trip with a cooldown

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Replay script

**Files:**
- Create: `scripts/replay_deal_check.py`
- Test: `tests/test_replay_deal_check.py`

**Interfaces:**
- Consumes:
  - `deals.judge_candidate(...)`, `deals.passes(verdict, settings)`, `deals.load_settings(config)` (Tasks 4, 5).
  - `jev_client.JevError`, `jev_client.is_configured()` (Task 1).
- Produces:
  - `fetch_rows(database_url: str, trip_id: Optional[int], days: int, limit: int) -> list[dict]`
  - `replay(database_url: str, trip_id: Optional[int], days: int, limit: int, settings: dict) -> list[dict]`. Each entry has the keys `trip_id`, `checked_at`, `result`, `facts`, `raw`, `facts_alert`, `raw_alert`. A `facts` or `raw` value is `{"price": float, "flight": float}`, or `{"error": str}` when Jev failed.
  - `format_entry(entry: dict) -> str`
  - `main() -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_replay_deal_check.py`:

```python
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import psycopg

from flight_tracker import deals
from flight_tracker import jev_client
from flight_tracker import storage
from flight_tracker import trips
from scripts import replay_deal_check


def _make_trip() -> int:
    return trips.create_trip(
        description="Trip", destinations=["GRU"],
        ideal_date="2026-07-11", ideal_return_date="2026-08-08",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
        baseline_price_estimate=1000,
    )


def _seed(trip_id, rows):
    conn = psycopg.connect(storage.DATABASE_URL)
    for checked_at, price_value in rows:
        conn.execute(
            "INSERT INTO prices (checked_at, origin, destination, depart_date, airline, "
            "stops, price_value, trip_id) VALUES (%s, 'AMS', 'GRU', '2026-07-11', 'KLM', 1, %s, %s)",
            (checked_at, price_value, trip_id),
        )
    conn.commit()
    conn.close()


def _seed_history(trip_id):
    now = datetime.now(timezone.utc)
    rows = [(now - timedelta(days=day), 1000.0) for day in range(2, 8)]
    rows.append((now - timedelta(hours=1), 600.0))
    _seed(trip_id, rows)


def test_replay_judges_each_row_in_both_modes_using_only_earlier_history():
    trip_id = _make_trip()
    _seed_history(trip_id)
    seen = []

    def fake_judge(state, questions, model):
        seen.append(state)
        price = 0.95 if "price_facts" in state else 0.2
        return {"price_is_very_good": price, "flight_is_acceptable": 0.9}

    with patch("flight_tracker.jev_client.judge", side_effect=fake_judge):
        entries = replay_deal_check.replay(
            storage.DATABASE_URL, trip_id, 14, 50, deals.DEFAULT_SETTINGS
        )

    assert len(entries) == 7
    newest = entries[0]
    assert newest["result"]["price_value"] == 600.0
    assert newest["facts_alert"] is True
    assert newest["raw_alert"] is False
    routes = [state["price_facts"]["route"] for state in seen if "price_facts" in state]
    assert any(
        route.startswith("far below usual. 40% below the median of 6 prices")
        for route in routes
    )


def test_replay_respects_the_row_limit():
    trip_id = _make_trip()
    _seed_history(trip_id)
    answers = {"price_is_very_good": 0.5, "flight_is_acceptable": 0.5}

    with patch("flight_tracker.jev_client.judge", return_value=answers):
        entries = replay_deal_check.replay(
            storage.DATABASE_URL, trip_id, 14, 2, deals.DEFAULT_SETTINGS
        )

    assert len(entries) == 2


def test_replay_marks_a_row_as_not_alerting_when_jev_fails():
    trip_id = _make_trip()
    _seed_history(trip_id)

    with patch("flight_tracker.jev_client.judge", side_effect=jev_client.JevError("HTTP 503")):
        entries = replay_deal_check.replay(
            storage.DATABASE_URL, trip_id, 14, 1, deals.DEFAULT_SETTINGS
        )

    assert entries[0]["facts"] == {"error": "HTTP 503"}
    assert entries[0]["facts_alert"] is False
    assert entries[0]["raw_alert"] is False


def test_format_entry_shows_both_verdicts():
    entry = {
        "trip_id": 3,
        "checked_at": datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc),
        "result": {
            "origin": "AMS", "destination": "GRU", "depart_date": "2026-07-11",
            "airline": "KLM", "stops": 1, "price_value": 612.0,
        },
        "facts": {"price": 0.95, "flight": 0.9},
        "raw": {"error": "HTTP 503"},
        "facts_alert": True,
        "raw_alert": False,
    }

    line = replay_deal_check.format_entry(entry)

    assert line == (
        "#3 2026-09-28 12:00 AMS->GRU 2026-07-11 KLM 1 stops 612 | "
        "facts: price=0.95 flight=0.90 ALERT | raw: error: HTTP 503 -"
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `source .venv/bin/activate && python -m pytest tests/test_replay_deal_check.py -v`
Expected: collection error, `ImportError: cannot import name 'replay_deal_check' from 'scripts'`.

- [ ] **Step 3: Write the implementation**

Create `scripts/replay_deal_check.py`:

```python
"""Read-only replay: run stored prices through the Jev deal check.

For each stored price, prints the verdicts of the facts mode and the raw
mode side by side. It sends no Telegram message and writes nothing to the
database. Use the output to tune the limits and the state mode in the
deal_check block of config.yaml.

Usage: python -m scripts.replay_deal_check [--trip-id N] [--days 14] [--limit 50]
"""

import argparse
from pathlib import Path
from typing import Optional

import psycopg
import yaml
from psycopg.rows import dict_row

from flight_tracker import deals
from flight_tracker import jev_client
from flight_tracker import storage

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def fetch_rows(database_url: str, trip_id: Optional[int], days: int, limit: int) -> list:
    conn = psycopg.connect(database_url, row_factory=dict_row)
    rows = conn.execute(
        """
        SELECT p.checked_at, p.origin, p.destination, p.depart_date, p.return_date,
               p.airline, p.departure, p.arrival, p.duration, p.stops, p.price_value,
               p.trip_id, t.baseline_price_estimate, t.passengers
        FROM prices p JOIN trips t ON t.id = p.trip_id
        WHERE p.checked_at >= now() - make_interval(days => %s)
          AND (%s::int IS NULL OR p.trip_id = %s)
        ORDER BY p.checked_at DESC, p.price_value ASC
        LIMIT %s
        """,
        (days, trip_id, trip_id, limit),
    ).fetchall()
    conn.close()
    return rows


def _to_result(row: dict) -> dict:
    return {
        "origin": row["origin"],
        "destination": row["destination"],
        "depart_date": row["depart_date"].isoformat(),
        "return_date": row["return_date"].isoformat() if row["return_date"] else None,
        "airline": row["airline"],
        "departure": row["departure"],
        "arrival": row["arrival"],
        "duration": row["duration"],
        "stops": row["stops"],
        "price_value": row["price_value"],
    }


def _passenger_count(row: dict) -> int:
    passengers = row["passengers"] or {}
    return passengers.get("adults", 1) + passengers.get("children", 0)


def _verdict(result: dict, row: dict, passenger_count: int, settings: dict) -> dict:
    try:
        return deals.judge_candidate(
            result, row["trip_id"], row["baseline_price_estimate"], passenger_count,
            row["checked_at"], settings,
        )
    except jev_client.JevError as exc:
        return {"error": str(exc)}


def _would_alert(verdict: dict, settings: dict) -> bool:
    return "error" not in verdict and deals.passes(verdict, settings)


def replay(
    database_url: str, trip_id: Optional[int], days: int, limit: int, settings: dict,
) -> list:
    entries = []
    for row in fetch_rows(database_url, trip_id, days, limit):
        result = _to_result(row)
        passenger_count = _passenger_count(row)
        facts = _verdict(result, row, passenger_count, dict(settings, state_mode="facts"))
        raw = _verdict(result, row, passenger_count, dict(settings, state_mode="raw"))
        entries.append({
            "trip_id": row["trip_id"],
            "checked_at": row["checked_at"],
            "result": result,
            "facts": facts,
            "raw": raw,
            "facts_alert": _would_alert(facts, settings),
            "raw_alert": _would_alert(raw, settings),
        })
    return entries


def _describe(verdict: dict) -> str:
    if "error" in verdict:
        return f"error: {verdict['error']}"
    return f"price={verdict['price']:.2f} flight={verdict['flight']:.2f}"


def format_entry(entry: dict) -> str:
    result = entry["result"]
    return (
        f"#{entry['trip_id']} {entry['checked_at']:%Y-%m-%d %H:%M} "
        f"{result['origin']}->{result['destination']} {result['depart_date']} "
        f"{result['airline']} {result['stops']} stops {result['price_value']:.0f} | "
        f"facts: {_describe(entry['facts'])} {'ALERT' if entry['facts_alert'] else '-'} | "
        f"raw: {_describe(entry['raw'])} {'ALERT' if entry['raw_alert'] else '-'}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay stored prices through the Jev deal check.")
    parser.add_argument("--trip-id", type=int, default=None)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()

    if not jev_client.is_configured():
        raise SystemExit("TYPESAFE_API_KEY is not set.")

    with open(CONFIG_PATH) as f:
        settings = deals.load_settings(yaml.safe_load(f))

    entries = replay(storage.DATABASE_URL, args.trip_id, args.days, args.limit, settings)
    for entry in entries:
        print(format_entry(entry))

    facts_count = sum(entry["facts_alert"] for entry in entries)
    raw_count = sum(entry["raw_alert"] for entry in entries)
    print(
        f"\n{len(entries)} rows. Facts mode passes {facts_count}. Raw mode passes {raw_count}."
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `source .venv/bin/activate && python -m pytest tests/test_replay_deal_check.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/replay_deal_check.py tests/test_replay_deal_check.py
git commit -m "$(cat <<'EOF'
Add a read-only script to replay stored prices through Jev

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Config block and final verification

**Files:**
- Modify: `config.yaml`
- Test: `tests/test_deals.py`

**Interfaces:**
- Consumes: `deals.load_settings`, `deals.DEFAULT_SETTINGS` (Task 4).
- Produces: a `deal_check` block in `config.yaml` that equals the defaults.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_deals.py`:

```python
def test_repo_config_deal_check_block_matches_the_defaults():
    config_path = Path(__file__).parent.parent / "config.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    assert "deal_check" in config
    assert deals.load_settings(config) == deals.DEFAULT_SETTINGS
```

In the import block at the top of `tests/test_deals.py`, add these lines. Put `from pathlib import Path` after the `from datetime import ...` line. Put `import yaml` after `import pytest`.

```python
from pathlib import Path
```

```python
import yaml
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `source .venv/bin/activate && python -m pytest tests/test_deals.py::test_repo_config_deal_check_block_matches_the_defaults -v`
Expected: FAIL with `assert 'deal_check' in {...}`.

- [ ] **Step 3: Add the block to `config.yaml`**

In `config.yaml`, replace:

```yaml
# Scheduler interval in minutes
interval_minutes: 60
```

with:

```yaml
# Scheduler interval in minutes
interval_minutes: 60

# Jev deal check: decides which prices are worth an alert
deal_check:
  model: jev-1.13.0          # pinned, so a new Jev release cannot change your limits
  state_mode: facts          # facts or raw
  candidates_per_trip: 5     # cheapest flights of a check that go to Jev
  min_price_noul: 0.85       # lowest Jev yes-probability for "very good price"
  min_flight_noul: 0.70      # lowest Jev yes-probability for "acceptable flight"
  alert_cooldown_hours: 24   # one alert per trip in this time
  history_days: 30           # days of route history for the price facts
  min_route_history: 5       # fewer stored prices than this: no route comparison
```

- [ ] **Step 4: Run the full test suite**

Run: `source .venv/bin/activate && python -m pytest -q`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add config.yaml tests/test_deals.py
git commit -m "$(cat <<'EOF'
Add the deal_check block to the shared config

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Deployment checklist (run by the user, not by an agent)

These steps change the production server. Do them in this order.

1. **Create the table.** Run the `CREATE TABLE alerts` and `CREATE INDEX idx_alerts_trip_sent` statements from `schema.sql` against the production database with `psql`. The project has no migration tool.
2. **Add the key.** Create an API key at console.typesafe.ai. Add `TYPESAFE_API_KEY=<key>` to `.env` on the VPS.
3. **Build the new image.** Run `docker compose build`. The running container still uses the old code.
4. **Replay.** Run `docker compose run --rm flight-tracker python -m scripts.replay_deal_check --limit 30`. The script sends no message and writes nothing. Compare the `facts` and `raw` verdicts. Change `min_price_noul`, `min_flight_noul`, or `state_mode` in `config.yaml` until the `ALERT` rows look right to you.
5. **Go live.** Run `docker compose up -d`. The container restarts with the new code and your tuned `config.yaml`.
6. **Watch the log.** Each judged flight logs a line with its two Jev answers. A line `TYPESAFE_API_KEY is not set` means the key did not reach the container.
