# Travel Assistant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the single-trip flight tracker into a travel assistant that tracks several concurrent trip requests — including fuzzy ones like "a warm beach getaway in December" — and alerts on good deals inferred from each trip's own price history plus a one-time research step done at creation.

**Architecture:** Trip requests move out of `config.yaml` into a new `trips` table in the existing SQLite database. `tracker.py`'s search loop gains a destinations dimension and takes a merged trip record instead of the global config. A new `deals.py` flags good deals from each trip's own price history (falling back to a research-seeded baseline until there's enough history). The relay (`claude-relay-server`) resolves natural-language trip requests and config edits, using a shared persona prompt sent once per conversation and a resumed `session_id` on every follow-up turn.

**Tech Stack:** Python 3.11, SQLite (stdlib `sqlite3`), `ruamel.yaml` (comment-preserving config edits), `requests` (relay HTTP calls), `pytest` (dev-only, new).

**Spec:** `docs/superpowers/specs/2026-08-28-travel-assistant-design.md`

## Global Constraints

- No new production dependency beyond what's already in `requirements.txt` — `deals.py`'s percentile math uses the stdlib `statistics` module.
- `pytest` is a dev-only dependency (`requirements-dev.txt`), not added to `requirements.txt` or the Docker image.
- Trip and price data live in the same SQLite file (`storage.DB_PATH` / `FLIGHT_DB_PATH`) — no second database.
- The relay's shared persona (`relay_client.PERSONA_PROMPT`) is sent as `system_prompt` only on a conversation's first relay call; every following turn in that conversation resumes via `session_id` instead.
- `config.yaml` retains only shared defaults after this plan: `origins`, `seat`, `passengers`, `max_duration_hours`, `results_per_query`, `interval_minutes`.
- Every task's tests run via `pytest tests/ -v` from the repo root after `pip install -r requirements.txt -r requirements-dev.txt`.

---

## Task 1: `relay_client.py` — shared relay networking, session support, and persona prompt

**Files:**
- Create: `relay_client.py`
- Create: `requirements-dev.txt`
- Create: `pytest.ini`
- Create: `tests/test_relay_client.py`

**Interfaces:**
- Produces: `relay_client.RELAY_PORT: int`, `relay_client.FALLBACK_RELAY_HOST: str`, `relay_client.PERSONA_PROMPT: str`, `relay_client.relay_host() -> str`, `relay_client.extract_result(lines) -> dict` (`{"result": str, "session_id": str | None}`), `relay_client.extract_json(text: str) -> dict`, `relay_client.query(prompt: str, system_prompt=None, session_id=None) -> dict` (same shape as `extract_result`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_relay_client.py`:

```python
import json
from unittest.mock import patch, MagicMock

import relay_client


def test_relay_host_fallback_when_no_proc_net_route():
    with patch("builtins.open", side_effect=OSError):
        assert relay_client.relay_host() == relay_client.FALLBACK_RELAY_HOST


def test_extract_result_returns_result_and_session_id():
    lines = [
        json.dumps({"type": "AssistantMessage", "content": [{"text": "thinking"}]}),
        "",
        json.dumps({"type": "ResultMessage", "result": "hello", "session_id": "sess-1"}),
    ]
    out = relay_client.extract_result(lines)
    assert out == {"result": "hello", "session_id": "sess-1"}


def test_extract_json_from_prose():
    assert relay_client.extract_json('Sure! {"a": 1} done.') == {"a": 1}


def test_extract_json_no_json_returns_empty_dict():
    assert relay_client.extract_json("no brackets here") == {}


def _fake_response(result_text, session_id):
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.raise_for_status.return_value = None
    resp.iter_lines.return_value = [
        json.dumps({"type": "ResultMessage", "result": result_text, "session_id": session_id})
    ]
    return resp


def test_query_sends_session_id_when_provided_and_omits_system_prompt():
    with patch("relay_client.requests.post", return_value=_fake_response("ok", "sess-2")) as post:
        out = relay_client.query("hi", session_id="sess-1")

    assert out == {"result": "ok", "session_id": "sess-2"}
    _, kwargs = post.call_args
    assert kwargs["json"]["session_id"] == "sess-1"
    assert "system_prompt" not in kwargs["json"]


def test_query_sends_system_prompt_when_no_session_id():
    with patch("relay_client.requests.post", return_value=_fake_response("ok", "sess-1")) as post:
        relay_client.query("hi", system_prompt="be terse")

    _, kwargs = post.call_args
    assert kwargs["json"]["system_prompt"] == "be terse"
    assert "session_id" not in kwargs["json"]
```

Create `requirements-dev.txt`:

```
pytest==8.4.2
```

Create `pytest.ini`:

```ini
[pytest]
pythonpath = .
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pip install -r requirements.txt -r requirements-dev.txt && pytest tests/test_relay_client.py -v`
Expected: FAIL with "No module named 'relay_client'"

- [ ] **Step 3: Write the implementation**

Create `relay_client.py`:

```python
"""HTTP client for the claude-relay-server running on this VPS.

Resolves the Docker bridge gateway at runtime, sends a prompt (optionally
resuming a session), and extracts the final result and session id from
the relay's newline-delimited JSON response stream.
"""

import json
import socket
import struct

import requests

RELAY_PORT = 8811
FALLBACK_RELAY_HOST = "172.17.0.1"

PERSONA_PROMPT = (
    "You are the reasoning backend for a personal flight-price-tracking "
    "assistant. Your job is narrow: turn a user's plain-language request "
    "into a structured JSON answer, in the exact shape the current request "
    "specifies. Do not run shell commands, browse files, or make any "
    "change outside of what is asked — only answer with JSON. Use IATA "
    "airport codes for airports, translating city, region, or \"kind of "
    "destination\" descriptions yourself from your own knowledge. If a "
    "request is unclear or names something that doesn't exist, ask a "
    "short clarifying question in a clarification_needed field instead of "
    "guessing. Keep responses terse — no chit-chat, no explanation beyond "
    "what's asked."
)


def relay_host() -> str:
    """The Docker default-bridge gateway address, read from the routing table."""
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                fields = line.split()
                if fields[1] != "00000000" or not int(fields[3], 16) & 2:
                    continue
                return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    except OSError:
        pass
    return FALLBACK_RELAY_HOST


def extract_result(lines) -> dict:
    """Pull the final ResultMessage's result text and session_id out of an NDJSON stream."""
    result = ""
    session_id = None
    for line in lines:
        if not line:
            continue
        event = json.loads(line)
        if event.get("type") == "ResultMessage":
            result = event.get("result", "")
            session_id = event.get("session_id")
    return {"result": result, "session_id": session_id}


def extract_json(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}


def query(prompt: str, system_prompt=None, session_id=None) -> dict:
    """Send a prompt to the relay. Returns {"result": str, "session_id": str | None}.

    Pass system_prompt only on a conversation's first call; pass the
    session_id from that call's response on every following turn instead.
    """
    url = f"http://{relay_host()}:{RELAY_PORT}/v1/query"
    payload = {"prompt": prompt}
    if system_prompt:
        payload["system_prompt"] = system_prompt
    if session_id:
        payload["session_id"] = session_id

    with requests.post(url, json=payload, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        return extract_result(resp.iter_lines(decode_unicode=True))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_relay_client.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add relay_client.py requirements-dev.txt pytest.ini tests/test_relay_client.py
git commit -m "Add relay_client with session support and shared persona prompt"
```

---

## Task 2: `storage.py` — tag stored prices with `trip_id`

**Files:**
- Modify: `storage.py`
- Create: `tests/test_storage.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `storage.write_results(results: list[dict], trip_id: int) -> None` (replaces the old `write_results(results, config)` — `config` was already unused in the body).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_storage.py`:

```python
import sqlite3

import storage


def test_write_results_tags_rows_with_trip_id(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(storage, "_initialized", False)

    results = [{
        "origin": "AMS", "destination": "GRU", "depart_date": "2026-07-11",
        "return_date": "2026-08-08", "airline": "KLM", "departure": "10:00",
        "arrival": "20:00", "duration": "11h", "stops": 0, "price": "€1200",
        "price_value": 1200.0, "is_best": True,
    }]

    storage.write_results(results, trip_id=42)

    conn = sqlite3.connect(storage.DB_PATH)
    row = conn.execute("SELECT origin, price_value, trip_id FROM prices").fetchone()
    conn.close()

    assert row == ("AMS", 1200.0, 42)


def test_write_results_does_nothing_for_empty_list(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(storage, "_initialized", False)

    storage.write_results([], trip_id=1)

    assert not (tmp_path / "prices.db").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_storage.py -v`
Expected: FAIL — `TypeError: write_results() got an unexpected keyword argument 'trip_id'`

- [ ] **Step 3: Modify `storage.py`**

Replace the `_SCHEMA` constant:

```python
_SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at  TEXT NOT NULL,
    origin      TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT,
    airline     TEXT,
    departure   TEXT,
    arrival     TEXT,
    duration    TEXT,
    stops       INTEGER,
    price       TEXT,
    price_value REAL,
    is_best     INTEGER,
    trip_id     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_prices_lookup
    ON prices(origin, depart_date, return_date);
CREATE INDEX IF NOT EXISTS idx_prices_trip
    ON prices(trip_id);
"""
```

Replace `write_results`:

```python
def write_results(results: list[dict], trip_id: int) -> None:
    if not results:
        return

    now = datetime.now(timezone.utc).isoformat()

    rows = [
        (
            now,
            r["origin"],
            r["destination"],
            r["depart_date"],
            r.get("return_date"),
            r.get("airline"),
            r.get("departure"),
            r.get("arrival"),
            r.get("duration"),
            r.get("stops"),
            r.get("price"),
            float(r["price_value"]),
            int(r.get("is_best", False)),
            trip_id,
        )
        for r in results
    ]

    try:
        conn = _get_connection()
        conn.executemany(
            """
            INSERT INTO prices (
                checked_at, origin, destination, depart_date, return_date,
                airline, departure, arrival, duration, stops,
                price, price_value, is_best, trip_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
        conn.close()
        logger.info("SQLite: wrote %d rows to %s", len(rows), DB_PATH)
    except Exception as exc:
        logger.error("SQLite write failed: %s", exc)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_storage.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add storage.py tests/test_storage.py
git commit -m "Tag stored prices with trip_id"
```

---

## Task 3: `trips.py` — schema, CRUD, default-merging, and legacy config migration

**Files:**
- Create: `trips.py`
- Create: `tests/test_trips.py`

**Interfaces:**
- Consumes: `storage.DB_PATH` (same SQLite file).
- Produces: `trips.create_trip(description, destinations, ideal_date, ideal_return_date, departure_range_before, departure_range_after, return_range_before, return_range_after, origins=None, seat=None, passengers=None, max_duration_hours=None, results_per_query=None, baseline_price_estimate=None) -> int`, `trips.get_trip(trip_id: int) -> dict | None`, `trips.get_active_trips() -> list[dict]`, `trips.cancel_trip(trip_id: int) -> bool`, `trips.merge_with_defaults(trip: dict, config: dict) -> dict`, `trips.migrate_legacy_config(config: dict) -> dict`, `trips.migrate_config_file(config_path) -> dict`.
  A trip dict has keys: `id, description, destinations (list), origins (list|None), ideal_date, ideal_return_date, departure_range_before, departure_range_after, return_range_before, return_range_after, seat, passengers (dict|None), max_duration_hours, results_per_query, baseline_price_estimate, status, created_at`.
  A merged trip dict (from `merge_with_defaults`) has keys: `id, description, destinations, origins, ideal_date, ideal_return_date, departure_range_before, departure_range_after, return_range_before, return_range_after, seat, passengers, max_duration_hours, results_per_query` — all defaults resolved, no `None`s.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_trips.py`:

```python
from datetime import date, timedelta

import pytest

import storage
import trips


@pytest.fixture(autouse=True)
def scratch_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_prices.db"
    monkeypatch.setattr(storage, "DB_PATH", db_path)
    monkeypatch.setattr(trips, "DB_PATH", db_path)
    monkeypatch.setattr(trips, "_initialized", False)
    yield db_path


def _create(**overrides):
    defaults = dict(
        description="Trip", destinations=["BKK"],
        ideal_date="2026-12-05", ideal_return_date="2026-12-19",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
    )
    defaults.update(overrides)
    return trips.create_trip(**defaults)


def test_create_and_get_trip_round_trips_fields():
    trip_id = _create(description="Beach trip", destinations=["BKK", "HKT"],
                       passengers={"adults": 2})

    trip = trips.get_trip(trip_id)

    assert trip["description"] == "Beach trip"
    assert trip["destinations"] == ["BKK", "HKT"]
    assert trip["origins"] is None
    assert trip["passengers"] == {"adults": 2}
    assert trip["status"] == "active"


def test_get_trip_returns_none_for_unknown_id():
    assert trips.get_trip(999) is None


def test_get_active_trips_excludes_cancelled():
    active_id = _create(description="Active")
    cancelled_id = _create(description="Cancelled")
    trips.cancel_trip(cancelled_id)

    active = trips.get_active_trips()

    assert [t["id"] for t in active] == [active_id]


def test_get_active_trips_expires_trips_past_their_return_window():
    past_return = (date.today() - timedelta(days=10)).isoformat()
    trip_id = _create(ideal_date="2020-01-01", ideal_return_date=past_return)

    active = trips.get_active_trips()

    assert active == []
    assert trips.get_trip(trip_id)["status"] == "expired"


def test_cancel_trip_returns_false_for_unknown_id():
    assert trips.cancel_trip(999) is False


def test_merge_with_defaults_uses_trip_value_when_present():
    trip = {
        "id": 1, "description": "d", "destinations": ["BKK"],
        "origins": ["LHR"], "ideal_date": "2026-12-05",
        "ideal_return_date": "2026-12-19",
        "departure_range_before": 1, "departure_range_after": 1,
        "return_range_before": 1, "return_range_after": 1,
        "seat": "business", "passengers": {"adults": 1},
        "max_duration_hours": 10, "results_per_query": 5,
    }
    config = {"origins": ["AMS"], "seat": "economy", "passengers": {"adults": 2},
              "max_duration_hours": 16, "results_per_query": 3}

    merged = trips.merge_with_defaults(trip, config)

    assert merged["origins"] == ["LHR"]
    assert merged["seat"] == "business"
    assert merged["max_duration_hours"] == 10


def test_merge_with_defaults_falls_back_to_config():
    trip = {
        "id": 1, "description": "d", "destinations": ["BKK"],
        "origins": None, "ideal_date": "2026-12-05",
        "ideal_return_date": "2026-12-19",
        "departure_range_before": 1, "departure_range_after": 1,
        "return_range_before": 1, "return_range_after": 1,
        "seat": None, "passengers": None,
        "max_duration_hours": None, "results_per_query": None,
    }
    config = {"origins": ["AMS"], "seat": "economy", "passengers": {"adults": 2},
              "max_duration_hours": 16, "results_per_query": 3}

    merged = trips.merge_with_defaults(trip, config)

    assert merged["origins"] == ["AMS"]
    assert merged["seat"] == "economy"
    assert merged["max_duration_hours"] == 16


def test_migrate_legacy_config_creates_trip_and_strips_fields():
    config = {
        "destination": "GRU", "origins": ["AMS", "BRU"],
        "ideal_date": "2026-07-11", "ideal_return_date": "2026-08-08",
        "departure_range_before": 1, "departure_range_after": 2,
        "return_range_before": 3, "return_range_after": 7,
        "seat": "economy", "passengers": {"adults": 2},
        "price_alert_threshold": 2500, "max_duration_hours": 16,
        "results_per_query": 3, "interval_minutes": 60,
    }

    stripped = trips.migrate_legacy_config(config)

    assert "destination" not in stripped
    assert "price_alert_threshold" not in stripped
    assert stripped["origins"] == ["AMS", "BRU"]
    assert stripped["interval_minutes"] == 60

    active = trips.get_active_trips()
    assert len(active) == 1
    assert active[0]["destinations"] == ["GRU"]
    assert active[0]["description"] == "Migrated from config.yaml"


def test_migrate_legacy_config_is_noop_when_already_migrated():
    config = {"origins": ["AMS"], "interval_minutes": 60}

    result = trips.migrate_legacy_config(config)

    assert result == config
    assert trips.get_active_trips() == []


def test_migrate_config_file_writes_stripped_yaml_and_returns_dict(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        'destination: "GRU"\n'
        'origins:\n  - "AMS"\n'
        'ideal_date: "2026-07-11"\n'
        'ideal_return_date: "2026-08-08"\n'
        "departure_range_before: 1\n"
        "departure_range_after: 2\n"
        "return_range_before: 3\n"
        "return_range_after: 7\n"
        "price_alert_threshold: 2500\n"
        "interval_minutes: 60\n"
    )

    result = trips.migrate_config_file(config_path)

    assert "destination" not in result
    assert result["interval_minutes"] == 60

    on_disk = config_path.read_text()
    assert "destination" not in on_disk
    assert "price_alert_threshold" not in on_disk
    assert "interval_minutes: 60" in on_disk


def test_migrate_config_file_is_noop_when_already_migrated(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("origins:\n  - AMS\ninterval_minutes: 60\n")

    result = trips.migrate_config_file(config_path)

    assert result["interval_minutes"] == 60
    assert trips.get_active_trips() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_trips.py -v`
Expected: FAIL with "No module named 'trips'"

- [ ] **Step 3: Write the implementation**

Create `trips.py`:

```python
"""Trip requests: schema, CRUD, default-merging, and legacy config migration."""

import json
import logging
import sqlite3
from datetime import date, datetime, timezone

from ruamel.yaml import YAML

import storage

logger = logging.getLogger(__name__)

DB_PATH = storage.DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    description             TEXT NOT NULL,
    destinations            TEXT NOT NULL,
    origins                 TEXT,
    ideal_date              TEXT NOT NULL,
    ideal_return_date       TEXT NOT NULL,
    departure_range_before  INTEGER NOT NULL,
    departure_range_after   INTEGER NOT NULL,
    return_range_before     INTEGER NOT NULL,
    return_range_after      INTEGER NOT NULL,
    seat                    TEXT,
    passengers              TEXT,
    max_duration_hours      INTEGER,
    results_per_query       INTEGER,
    baseline_price_estimate REAL,
    status                  TEXT NOT NULL DEFAULT 'active',
    created_at              TEXT NOT NULL
);
"""

_LEGACY_KEYS = (
    "destination", "ideal_date", "ideal_return_date",
    "departure_range_before", "departure_range_after",
    "return_range_before", "return_range_after",
    "price_alert_threshold",
)

_yaml = YAML()
_yaml.preserve_quotes = True

_initialized = False


def _get_connection() -> sqlite3.Connection:
    global _initialized
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if not _initialized:
        conn.executescript(_SCHEMA)
        conn.commit()
        _initialized = True
    return conn


def _row_to_trip(row: sqlite3.Row) -> dict:
    trip = dict(row)
    trip["destinations"] = json.loads(trip["destinations"])
    trip["origins"] = json.loads(trip["origins"]) if trip["origins"] else None
    trip["passengers"] = json.loads(trip["passengers"]) if trip["passengers"] else None
    return trip


def create_trip(
    description: str,
    destinations: list,
    ideal_date: str,
    ideal_return_date: str,
    departure_range_before: int,
    departure_range_after: int,
    return_range_before: int,
    return_range_after: int,
    origins=None,
    seat=None,
    passengers=None,
    max_duration_hours=None,
    results_per_query=None,
    baseline_price_estimate=None,
) -> int:
    conn = _get_connection()
    cur = conn.execute(
        """
        INSERT INTO trips (
            description, destinations, origins, ideal_date, ideal_return_date,
            departure_range_before, departure_range_after,
            return_range_before, return_range_after,
            seat, passengers, max_duration_hours, results_per_query,
            baseline_price_estimate, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
        """,
        (
            description,
            json.dumps(destinations),
            json.dumps(origins) if origins else None,
            ideal_date,
            ideal_return_date,
            departure_range_before,
            departure_range_after,
            return_range_before,
            return_range_after,
            seat,
            json.dumps(passengers) if passengers else None,
            max_duration_hours,
            results_per_query,
            baseline_price_estimate,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    trip_id = cur.lastrowid
    conn.close()
    return trip_id


def get_trip(trip_id: int):
    conn = _get_connection()
    row = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    conn.close()
    return _row_to_trip(row) if row else None


def get_active_trips() -> list:
    _expire_overdue_trips()
    conn = _get_connection()
    rows = conn.execute("SELECT * FROM trips WHERE status = 'active'").fetchall()
    conn.close()
    return [_row_to_trip(row) for row in rows]


def cancel_trip(trip_id: int) -> bool:
    conn = _get_connection()
    cur = conn.execute(
        "UPDATE trips SET status = 'cancelled' WHERE id = ? AND status = 'active'",
        (trip_id,),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def _expire_overdue_trips(today=None) -> None:
    today = today or date.today().isoformat()
    today_ordinal = date.fromisoformat(today).toordinal()

    conn = _get_connection()
    rows = conn.execute(
        "SELECT id, ideal_return_date, return_range_after FROM trips WHERE status = 'active'"
    ).fetchall()
    for row in rows:
        last_return = date.fromisoformat(row["ideal_return_date"])
        cutoff = last_return.toordinal() + row["return_range_after"]
        if cutoff < today_ordinal:
            conn.execute("UPDATE trips SET status = 'expired' WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()


def merge_with_defaults(trip: dict, config: dict) -> dict:
    """Overlay a trip's own fields over config.yaml's shared defaults."""
    return {
        "id": trip["id"],
        "description": trip["description"],
        "destinations": trip["destinations"],
        "origins": trip["origins"] or config["origins"],
        "ideal_date": trip["ideal_date"],
        "ideal_return_date": trip["ideal_return_date"],
        "departure_range_before": trip["departure_range_before"],
        "departure_range_after": trip["departure_range_after"],
        "return_range_before": trip["return_range_before"],
        "return_range_after": trip["return_range_after"],
        "seat": trip["seat"] or config.get("seat", "economy"),
        "passengers": trip["passengers"] or config.get("passengers", {}),
        "max_duration_hours": (
            trip["max_duration_hours"]
            if trip["max_duration_hours"] is not None
            else config.get("max_duration_hours", 0)
        ),
        "results_per_query": (
            trip["results_per_query"]
            if trip["results_per_query"] is not None
            else config.get("results_per_query", 3)
        ),
    }


def migrate_legacy_config(config: dict) -> dict:
    """If config still has the old single-trip fields, create trip #1 from
    them and return a dict with those fields removed. Otherwise return
    config unchanged."""
    if "destination" not in config:
        return config

    create_trip(
        description="Migrated from config.yaml",
        destinations=[config["destination"]],
        ideal_date=config["ideal_date"],
        ideal_return_date=config["ideal_return_date"],
        departure_range_before=config.get("departure_range_before", 3),
        departure_range_after=config.get("departure_range_after", 3),
        return_range_before=config.get("return_range_before", 3),
        return_range_after=config.get("return_range_after", 3),
    )
    logger.info("Migrated legacy config.yaml trip into trips table.")

    return {k: v for k, v in config.items() if k not in _LEGACY_KEYS}


def migrate_config_file(config_path) -> dict:
    """Load config_path, migrate it if it still has the old single-trip
    fields, and write the stripped version back (preserving comments).
    Returns the up-to-date config dict either way."""
    with open(config_path) as f:
        config = _yaml.load(f)

    if "destination" not in config:
        return dict(config)

    stripped = migrate_legacy_config(dict(config))

    for key in _LEGACY_KEYS:
        config.pop(key, None)

    with open(config_path, "w") as f:
        _yaml.dump(config, f)

    return stripped
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_trips.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add trips.py tests/test_trips.py
git commit -m "Add trips module: schema, CRUD, default-merge, and config migration"
```

---

## Task 4: `deals.py` — good-deal detection from a trip's own price history

**Files:**
- Create: `deals.py`
- Create: `tests/test_deals.py`

**Interfaces:**
- Consumes: `storage.DB_PATH`, `prices.trip_id` / `prices.price_value` columns (Task 2).
- Produces: `deals.is_good_deal(trip_id: int, price_value: float, baseline_price_estimate) -> bool`, `deals.cheapest_price(trip_id: int) -> float | None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_deals.py`:

```python
import sqlite3

import storage
import deals


def _seed_prices(trip_id, price_values):
    conn = sqlite3.connect(storage.DB_PATH)
    conn.executescript(storage._SCHEMA)
    for value in price_values:
        conn.execute(
            "INSERT INTO prices (checked_at, origin, destination, depart_date, "
            "price_value, trip_id) VALUES ('2026-01-01', 'AMS', 'GRU', "
            "'2026-07-11', ?, ?)",
            (value, trip_id),
        )
    conn.commit()
    conn.close()


def test_cold_start_no_baseline_never_alerts(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    _seed_prices(1, [])

    assert deals.is_good_deal(1, 500, baseline_price_estimate=None) is False


def test_cold_start_uses_baseline_discount(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    _seed_prices(1, [900, 950])  # only 2 samples, below the percentile minimum

    assert deals.is_good_deal(1, 849, baseline_price_estimate=1000) is True   # 1000*0.85=850
    assert deals.is_good_deal(1, 851, baseline_price_estimate=1000) is False


def test_percentile_path_kicks_in_at_five_samples(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    _seed_prices(1, [100, 200, 300, 400, 500])

    assert deals.is_good_deal(1, 100, baseline_price_estimate=None) is True
    assert deals.is_good_deal(1, 500, baseline_price_estimate=None) is False


def test_percentile_path_ignores_a_generous_baseline(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    _seed_prices(1, [100, 200, 300, 400, 500])

    # A generous baseline must not override the percentile check once
    # there's enough history.
    assert deals.is_good_deal(1, 500, baseline_price_estimate=10000) is False


def test_cheapest_price_returns_none_when_no_history(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    _seed_prices(1, [])

    assert deals.cheapest_price(1) is None


def test_cheapest_price_returns_minimum(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    _seed_prices(1, [500, 200, 800])

    assert deals.cheapest_price(1) == 200
```

(`tests/test_deals.py` has 7 tests total.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_deals.py -v`
Expected: FAIL with "No module named 'deals'"

- [ ] **Step 3: Write the implementation**

Create `deals.py`:

```python
"""Good-deal detection based on a trip's own historical prices."""

import sqlite3
import statistics

import storage

MIN_HISTORY_FOR_PERCENTILE = 5
PERCENTILE_THRESHOLD = 20
BASELINE_DISCOUNT = 0.85


def _historical_prices(trip_id: int) -> list:
    conn = sqlite3.connect(storage.DB_PATH)
    rows = conn.execute(
        "SELECT price_value FROM prices WHERE trip_id = ?", (trip_id,)
    ).fetchall()
    conn.close()
    return [row[0] for row in rows]


def is_good_deal(trip_id: int, price_value: float, baseline_price_estimate) -> bool:
    history = _historical_prices(trip_id)

    if len(history) < MIN_HISTORY_FOR_PERCENTILE:
        if baseline_price_estimate is None:
            return False
        return price_value <= baseline_price_estimate * BASELINE_DISCOUNT

    percentile_cut = statistics.quantiles(history, n=100)[PERCENTILE_THRESHOLD - 1]
    return price_value <= percentile_cut


def cheapest_price(trip_id: int):
    history = _historical_prices(trip_id)
    return min(history) if history else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_deals.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add deals.py tests/test_deals.py
git commit -m "Add deals module: percentile-based good-deal detection"
```

---

## Task 5: `tracker.py` — search across multiple destinations per trip

**Files:**
- Modify: `tracker.py:135-224` (the `search_flights` function; helper functions above it are unchanged)
- Create: `tests/test_tracker.py`

**Interfaces:**
- Consumes: a merged trip dict from `trips.merge_with_defaults` (Task 3): `destinations, origins, seat, passengers, max_duration_hours, results_per_query, ideal_date, ideal_return_date`, and the four range fields.
- Produces: `tracker.search_flights(trip: dict)` — a generator yielding one combo (`list[dict]`) per origin × destination × depart_date × return_date, unchanged result dict shape (`origin, destination, depart_date, return_date, airline, departure, arrival, duration, stops, price, price_value, is_best, current_price_level, url`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tracker.py`:

```python
from unittest.mock import patch, MagicMock

import tracker


def _fake_flight(price="$100", duration="10h 0m", stops=0, is_best=True, name="Air"):
    flight = MagicMock()
    flight.price = price
    flight.duration = duration
    flight.stops = stops
    flight.is_best = is_best
    flight.name = name
    flight.departure = "10:00"
    flight.arrival = "20:00"
    return flight


def _base_trip(**overrides):
    trip = {
        "destinations": ["BKK"],
        "origins": ["AMS"],
        "seat": "economy",
        "results_per_query": 3,
        "passengers": {"adults": 1},
        "max_duration_hours": 0,
        "ideal_date": "2026-12-05",
        "ideal_return_date": "2026-12-19",
        "departure_range_before": 0,
        "departure_range_after": 0,
        "return_range_before": 0,
        "return_range_after": 0,
    }
    trip.update(overrides)
    return trip


def test_search_flights_loops_over_multiple_destinations():
    trip = _base_trip(destinations=["BKK", "HKT"])

    fake_result = MagicMock()
    fake_result.flights = [_fake_flight()]
    fake_result.current_price = "low"

    with patch("tracker.get_flights", return_value=fake_result) as mock_get_flights, \
         patch("tracker.time.sleep"):
        combos = list(tracker.search_flights(trip))

    # 1 origin × 2 destinations × 1 depart date × 1 return date = 2 combos
    assert mock_get_flights.call_count == 2
    assert len(combos) == 2
    destinations_seen = {combo[0]["destination"] for combo in combos}
    assert destinations_seen == {"BKK", "HKT"}


def test_search_flights_filters_by_max_duration():
    trip = _base_trip(max_duration_hours=5)

    fake_result = MagicMock()
    fake_result.flights = [_fake_flight(duration="14h 0m")]
    fake_result.current_price = "typical"

    with patch("tracker.get_flights", return_value=fake_result), \
         patch("tracker.time.sleep"):
        combos = list(tracker.search_flights(trip))

    assert combos == [[]]


def test_search_flights_skips_a_failing_combo_without_stopping_others():
    trip = _base_trip(destinations=["BKK", "HKT"])

    fake_result = MagicMock()
    fake_result.flights = [_fake_flight()]
    fake_result.current_price = "low"

    with patch("tracker.get_flights", side_effect=[Exception("boom"), fake_result]), \
         patch("tracker.time.sleep"):
        combos = list(tracker.search_flights(trip))

    assert len(combos) == 1
    assert combos[0][0]["destination"] == "HKT"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_tracker.py -v`
Expected: FAIL — `KeyError: 'destination'` (current code reads `config["destination"]` as a single value, not `trip["destinations"]`)

- [ ] **Step 3: Modify `tracker.py`**

Replace the body of `search_flights` (lines 135-224) with:

```python
def search_flights(trip: dict):
    """
    Search all origin × destination × depart_date × return_date combinations
    for a single (already default-merged) trip record. Yields lists of
    result dicts one combo at a time.
    """
    destinations = trip["destinations"]
    origins = trip["origins"]
    seat = trip.get("seat", "economy")
    results_per_query = trip.get("results_per_query", 3)

    pax_cfg = trip.get("passengers", {})
    passengers = Passengers(
        adults=pax_cfg.get("adults", 1),
        children=pax_cfg.get("children", 0),
    )

    max_duration_hours = trip.get("max_duration_hours", 0)
    depart_dates = _date_range(
        trip["ideal_date"],
        trip.get("departure_range_before", 3),
        trip.get("departure_range_after", 3),
    )
    return_dates = _date_range(
        trip["ideal_return_date"],
        trip.get("return_range_before", 3),
        trip.get("return_range_after", 3),
    )

    for origin in origins:
        for destination in destinations:
            for depart_date in depart_dates:
                for return_date in return_dates:
                    combo_label = f"{origin} {depart_date} → {destination} / back {return_date}"
                    try:
                        result = get_flights(
                            flight_data=[
                                FlightData(
                                    date=depart_date,
                                    from_airport=origin,
                                    to_airport=destination,
                                ),
                                FlightData(
                                    date=return_date,
                                    from_airport=destination,
                                    to_airport=origin,
                                ),
                            ],
                            trip="round-trip",
                            seat=seat,
                            passengers=passengers,
                        )

                        flights = [f for f in result.flights if _parse_price(f.price) > 0]

                        if max_duration_hours:
                            flights = [
                                f for f in flights
                                if _parse_duration_hours(f.duration) <= max_duration_hours
                            ]
                        flights = sorted(flights, key=lambda f: _parse_price(f.price))[:results_per_query]

                        url = _build_flights_url(origin, destination, depart_date, return_date, passengers, seat)
                        combo_results = [
                            {
                                "origin": origin,
                                "destination": destination,
                                "depart_date": depart_date,
                                "return_date": return_date,
                                "airline": flight.name,
                                "departure": flight.departure,
                                "arrival": flight.arrival,
                                "duration": flight.duration,
                                "stops": flight.stops,
                                "price": flight.price,
                                "price_value": _parse_price(flight.price),
                                "is_best": flight.is_best,
                                "current_price_level": result.current_price,
                                "url": url,
                            }
                            for flight in flights
                        ]

                        logger.info("OK  %s — %d flights found", combo_label, len(combo_results))
                        yield combo_results

                    except Exception as exc:
                        logger.warning("SKIP %s — %s", combo_label, exc)

                    time.sleep(random.uniform(3, 9))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_tracker.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add tracker.py tests/test_tracker.py
git commit -m "Search across multiple destinations per trip"
```

---

## Task 6: `display.py` — trip-aware terminal output

**Files:**
- Modify: `display.py` (whole file — both functions and the color helper)
- Create: `tests/test_display.py`

**Interfaces:**
- Consumes: result dicts annotated with `trip_description: str` and `is_good_deal: bool` (added by `main.py` in Task 8).
- Produces: `display.print_results(results: list[dict]) -> None`, `display.print_alerts(good_deals: list[dict]) -> None` (both drop the old `config` parameter).

**Note:** this file wasn't explicitly called out in the spec, but `print_results`/`print_alerts` read `config["destination"]`, `config["origins"]`, and `config["price_alert_threshold"]` today — all fields moving out of `config.yaml` in this plan. Left unchanged, the first post-migration `run_check()` would crash here.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_display.py`:

```python
import display


def test_print_results_groups_by_trip_and_shows_data():
    results = [{
        "trip_description": "Beach trip", "origin": "AMS", "destination": "BKK",
        "depart_date": "2026-12-05", "return_date": "2026-12-19",
        "airline": "TG", "departure": "10:00", "arrival": "20:00",
        "duration": "12h", "stops": 0, "price": "€650", "price_value": 650.0,
        "is_best": True, "current_price_level": "low", "is_good_deal": True,
    }]

    with display.console.capture() as capture:
        display.print_results(results)

    output = capture.get()
    assert "Beach trip" in output
    assert "AMS" in output
    assert "€650" in output


def test_print_results_handles_empty_list():
    with display.console.capture() as capture:
        display.print_results([])

    assert "No results found" in capture.get()


def test_print_alerts_shows_trip_description():
    good_deals = [{
        "trip_description": "Beach trip", "origin": "AMS", "destination": "BKK",
        "depart_date": "2026-12-05", "return_date": "2026-12-19",
        "airline": "TG", "duration": "12h", "stops": 0, "price": "€650",
        "price_value": 650.0,
    }]

    with display.console.capture() as capture:
        display.print_alerts(good_deals)

    assert "Beach trip" in capture.get()


def test_print_alerts_does_nothing_for_empty_list():
    with display.console.capture() as capture:
        display.print_alerts([])

    assert capture.get() == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_display.py -v`
Expected: FAIL — `KeyError: 'destination'` (current `print_results`/`print_alerts` require a `config` argument)

- [ ] **Step 3: Rewrite `display.py`**

```python
"""Rich terminal display for flight results."""

from datetime import datetime
from rich.console import Console
from rich.table import Table
from rich import box
from rich.text import Text

console = Console()


def _price_style(is_good_deal: bool) -> str:
    return "bold green" if is_good_deal else "white"


def print_results(results: list) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    console.rule(f"[bold cyan]Flight Tracker — {now}[/bold cyan]")

    if not results:
        console.print("[yellow]No results found.[/yellow]")
        return

    by_trip: dict = {}
    for r in results:
        by_trip.setdefault(r.get("trip_description", "Untitled trip"), []).append(r)

    for trip_description, trip_results in by_trip.items():
        console.print(f"[bold]{trip_description}[/bold]")

        by_origin: dict = {}
        for r in trip_results:
            by_origin.setdefault(r["origin"], []).append(r)

        for origin, flights in by_origin.items():
            destination = flights[0]["destination"]
            table = Table(
                title=f"[bold]{origin} → {destination}[/bold]",
                box=box.ROUNDED,
                show_lines=True,
                header_style="bold magenta",
            )
            table.add_column("Depart date", style="cyan", no_wrap=True)
            table.add_column("Return date", style="cyan", no_wrap=True)
            table.add_column("Airline", style="white")
            table.add_column("Departure", style="dim")
            table.add_column("Arrival", style="dim")
            table.add_column("Duration", style="dim")
            table.add_column("Stops", justify="center")
            table.add_column("Price", justify="right")
            table.add_column("Market", justify="center", style="dim")

            flights_sorted = sorted(flights, key=lambda f: (f["depart_date"], f["return_date"], f["price_value"]))

            for f in flights_sorted:
                price_style = _price_style(f.get("is_good_deal", False))
                s = f["stops"]
                stops_text = "Direct" if s == 0 else (f"{s} stop{'s' if isinstance(s, int) and s > 1 else ''}" if s != "Unknown" else "? stops")
                best_marker = " ★" if f.get("is_best") else ""

                table.add_row(
                    f["depart_date"],
                    f["return_date"],
                    f["airline"] + best_marker,
                    f["departure"],
                    f["arrival"],
                    f["duration"],
                    stops_text,
                    Text(f["price"], style=price_style),
                    f.get("current_price_level", ""),
                )

            console.print(table)
            console.print()


def print_alerts(good_deals: list) -> None:
    if not good_deals:
        return

    console.rule("[bold green] PRICE ALERT [/bold green]")
    for a in sorted(good_deals, key=lambda r: r["price_value"]):
        console.print(
            f"[bold green]★ {a.get('trip_description', '')} — {a['price']}[/bold green]  "
            f"{a['origin']} → {a['destination']}  "
            f"[cyan]{a['depart_date']}[/cyan] / back [cyan]{a['return_date']}[/cyan]  "
            f"{a['airline']}  {a['duration']}  "
            f"{'Direct' if a['stops'] == 0 else str(a['stops']) + ' stop(s)'}"
        )
    console.rule()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_display.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add display.py tests/test_display.py
git commit -m "Make terminal display trip-aware"
```

---

## Task 7: `notifier.py` — trip-aware alerts and per-trip summary

**Files:**
- Modify: `notifier.py:53-101` (`notify_alerts` and `notify_summary`)
- Create: `tests/test_notifier.py`

**Interfaces:**
- Consumes: result dicts annotated with `trip_description: str` (added by `main.py` in Task 8).
- Produces: `notifier.notify_alerts(good_deals: list[dict]) -> None`, `notifier.notify_summary(trip_summaries: list[dict]) -> None` (both drop the old `config` parameter and the threshold-based filtering — the caller now passes already-filtered, already-tagged data).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_notifier.py`:

```python
from unittest.mock import patch

import notifier


def test_notify_alerts_includes_trip_description_and_price_level():
    good_deals = [{
        "trip_description": "Beach trip", "origin": "AMS", "destination": "BKK",
        "depart_date": "2026-12-05", "return_date": "2026-12-19",
        "airline": "KLM", "duration": "12h", "stops": 0, "price": "€650",
        "price_value": 650.0, "url": "https://example.com",
        "current_price_level": "low",
    }]

    with patch("notifier.send_message") as mock_send:
        notifier.notify_alerts(good_deals)

    sent_text = mock_send.call_args[0][0]
    assert "Beach trip" in sent_text
    assert "€650" in sent_text
    assert "Google rates this: low" in sent_text


def test_notify_alerts_sends_nothing_for_empty_list():
    with patch("notifier.send_message") as mock_send:
        notifier.notify_alerts([])

    mock_send.assert_not_called()


def test_notify_summary_has_one_line_per_trip_sorted_by_price():
    trip_summaries = [
        {"trip_description": "Pricier trip", "price": "€900", "price_value": 900.0,
         "origin": "AMS", "destination": "GRU", "depart_date": "2026-07-11",
         "return_date": "2026-08-08", "airline": "KLM", "stops": 0, "url": ""},
        {"trip_description": "Cheaper trip", "price": "€650", "price_value": 650.0,
         "origin": "AMS", "destination": "BKK", "depart_date": "2026-12-05",
         "return_date": "2026-12-19", "airline": "TG", "stops": 1, "url": ""},
    ]

    with patch("notifier.send_message") as mock_send:
        notifier.notify_summary(trip_summaries)

    sent_text = mock_send.call_args[0][0]
    assert sent_text.index("Cheaper trip") < sent_text.index("Pricier trip")


def test_notify_summary_sends_nothing_for_empty_list():
    with patch("notifier.send_message") as mock_send:
        notifier.notify_summary([])

    mock_send.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_notifier.py -v`
Expected: FAIL — `KeyError: 'price_value'` on the old threshold-filter line, since no `config` argument is passed

- [ ] **Step 3: Modify `notifier.py`**

Replace `notify_alerts` and `notify_summary`:

```python
def notify_alerts(good_deals: list) -> None:
    """Send a Telegram message for each result flagged as a good deal."""
    if not good_deals:
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"✈️ *Flight Price Alert* — {now}\n"]

    for a in sorted(good_deals, key=lambda r: r["price_value"]):
        stops = "Direct" if a["stops"] == 0 else f"{a['stops']} stop(s)" if a["stops"] != "Unknown" else "? stops"
        link = f"\n  [Search on Google Flights]({a['url']})" if a.get("url") else ""
        level = a.get("current_price_level")
        level_note = f"\n  Google rates this: {level}" if level else ""
        lines.append(
            f"*{a['trip_description']}*\n"
            f"*{a['price']}* — {a['origin']} → {a['destination']}\n"
            f"  Depart: {a['depart_date']}  |  Return: {a['return_date']}\n"
            f"  {a['airline']}  |  {a['duration']}  |  {stops}{link}{level_note}\n"
        )

    send_message("\n".join(lines))


def notify_summary(trip_summaries: list) -> None:
    """Send one line per active trip: its cheapest fare found this cycle."""
    if not trip_summaries:
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"🔍 *Flight Check* — {now}\n"]

    for t in sorted(trip_summaries, key=lambda r: r["price_value"]):
        stops = "Direct" if t["stops"] == 0 else f"{t['stops']} stop(s)"
        link = f" [↗]({t['url']})" if t.get("url") else ""
        lines.append(
            f"*{t['trip_description']}*: {t['price']} — {t['origin']} → {t['destination']}, "
            f"{t['depart_date']} / back {t['return_date']} ({t['airline']}, {stops}){link}"
        )

    send_message("\n".join(lines))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_notifier.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add notifier.py tests/test_notifier.py
git commit -m "Make Telegram alerts and summary trip-aware"
```

---

## Task 8: `main.py` — restructure `run_check()` around active trips, and startup migration

**Files:**
- Modify: `main.py` (whole file)
- Create: `tests/test_main.py`

**Interfaces:**
- Consumes: `trips.get_active_trips()`, `trips.merge_with_defaults()`, `trips.migrate_config_file()` (Task 3); `tracker.search_flights()` (Task 5); `storage.write_results()` (Task 2); `deals.is_good_deal()` (Task 4); `display.print_results()` / `print_alerts()` (Task 6); `notifier.notify_alerts()` / `notify_summary()` (Task 7).
- Produces: `main.run_check() -> None`, `main.main() -> None` (unchanged public shape, new internal behavior).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_main.py`:

```python
from unittest.mock import patch

import main


def _trip(trip_id=1, description="Beach trip", baseline=None):
    return {
        "id": trip_id, "description": description, "destinations": ["BKK"],
        "origins": None, "ideal_date": "2026-12-05", "ideal_return_date": "2026-12-19",
        "departure_range_before": 1, "departure_range_after": 1,
        "return_range_before": 1, "return_range_after": 1,
        "seat": None, "passengers": None, "max_duration_hours": None,
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


def test_run_check_tags_results_and_routes_good_deals_to_alerts():
    trip = _trip(baseline=700)
    combo = [_result(500)]  # well under 700*0.85

    with patch("main.load_config", return_value={"origins": ["AMS"], "seat": "economy",
                                                  "passengers": {}, "max_duration_hours": 0,
                                                  "results_per_query": 3}), \
         patch("main.trips.get_active_trips", return_value=[trip]), \
         patch("main.tracker.search_flights", return_value=iter([combo])), \
         patch("main.storage.write_results") as mock_write, \
         patch("main.deals.is_good_deal", return_value=True), \
         patch("main.display.print_results") as mock_print_results, \
         patch("main.display.print_alerts") as mock_print_alerts, \
         patch("main.is_configured", return_value=True), \
         patch("main.notify_alerts") as mock_notify_alerts, \
         patch("main.notify_summary") as mock_notify_summary:
        main.run_check()

    mock_write.assert_called_once_with(combo, trip_id=1)

    all_results_arg = mock_print_results.call_args[0][0]
    assert all_results_arg[0]["trip_description"] == "Beach trip"
    assert all_results_arg[0]["is_good_deal"] is True

    alerts_arg = mock_print_alerts.call_args[0][0]
    assert len(alerts_arg) == 1
    assert alerts_arg[0]["trip_description"] == "Beach trip"

    mock_notify_alerts.assert_called_once()
    summary_arg = mock_notify_summary.call_args[0][0]
    assert summary_arg[0]["price_value"] == 500


def test_run_check_skips_a_trip_that_raises_and_continues():
    good_trip = _trip(trip_id=2, description="Good trip")
    bad_trip = _trip(trip_id=1, description="Bad trip")
    combo = [_result(500)]

    def fake_search(merged):
        if merged["id"] == 1:
            raise ValueError("bad date")
        return iter([combo])

    with patch("main.load_config", return_value={"origins": ["AMS"], "seat": "economy",
                                                  "passengers": {}, "max_duration_hours": 0,
                                                  "results_per_query": 3}), \
         patch("main.trips.get_active_trips", return_value=[bad_trip, good_trip]), \
         patch("main.tracker.search_flights", side_effect=fake_search), \
         patch("main.storage.write_results"), \
         patch("main.deals.is_good_deal", return_value=False), \
         patch("main.display.print_results") as mock_print_results, \
         patch("main.display.print_alerts"), \
         patch("main.is_configured", return_value=False):
        main.run_check()

    all_results_arg = mock_print_results.call_args[0][0]
    assert len(all_results_arg) == 1
    assert all_results_arg[0]["trip_description"] == "Good trip"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_main.py -v`
Expected: FAIL — `AttributeError: <module 'main'> does not have the attribute 'trips'` (or similar, since `run_check()` still uses the old single-config flow)

- [ ] **Step 3: Rewrite `main.py`**

```python
"""Flight price tracker — entry point."""

import logging
from pathlib import Path

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler

import deals
import storage
import tracker
import trips
import telegram_commands
from display import console, print_results, print_alerts
import display
from notifier import is_configured, notify_alerts, notify_summary

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


def run_check() -> None:
    config = load_config()

    console.print("\n[bold cyan]Starting price check…[/bold cyan]")

    all_results = []
    all_alerts = []
    trip_summaries = []

    for trip in trips.get_active_trips():
        try:
            merged = trips.merge_with_defaults(trip, config)
            trip_results = []

            for combo in tracker.search_flights(merged):
                if not combo:
                    continue
                storage.write_results(combo, trip_id=trip["id"])

                for result in combo:
                    result["trip_description"] = trip["description"]
                    result["is_good_deal"] = deals.is_good_deal(
                        trip["id"], result["price_value"], trip["baseline_price_estimate"]
                    )
                    if result["is_good_deal"]:
                        all_alerts.append(result)

                trip_results.extend(combo)
                all_results.extend(combo)

            if trip_results:
                cheapest = min(trip_results, key=lambda r: r["price_value"])
                trip_summaries.append(cheapest)

        except Exception as exc:
            logger.error("Trip #%s (%s) failed: %s", trip["id"], trip["description"], exc)

    print_results(all_results)
    print_alerts(all_alerts)

    if is_configured():
        notify_alerts(all_alerts)
        notify_summary(trip_summaries)


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

Note: `import display` (in addition to the existing `from display import console, print_results, print_alerts`) is included so the tests above can patch `main.display.print_results` / `main.display.print_alerts` — both names refer to the same functions; the plain `print_results`/`print_alerts` names already used inside `run_check()` keep working unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_main.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "Restructure run_check around active trips; migrate legacy config at startup"
```

---

## Task 9: `telegram_commands.py` — session-scoped `/set-config`

**Files:**
- Modify: `telegram_commands.py` (imports, prompt constants, `_pending_config`, `_parse_config_request`, `_handle_config_reply`, `_dispatch`; add `_relay_turn`, `_clear_pending`)
- Create: `tests/test_telegram_commands.py`

**Interfaces:**
- Consumes: `relay_client.query()`, `relay_client.extract_json()`, `relay_client.PERSONA_PROMPT` (Task 1).
- Produces: `telegram_commands._relay_turn(followup_prompt: str, full_prompt: str, task_prompt: str, session_id) -> tuple[dict, str]` (used by later tasks too), `telegram_commands._pending_config: dict` (chat_id → session_id or `None`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_telegram_commands.py`:

```python
import requests
from unittest.mock import patch

import telegram_commands as tc


def setup_function():
    tc._pending_config.clear()


def test_set_config_first_turn_sends_persona_and_context(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("origins:\n  - AMS\ninterval_minutes: 60\n")
    monkeypatch.setattr(tc, "CONFIG_PATH", config_path)

    chat_id = 1
    tc._dispatch(chat_id, "/set-config")

    with patch("telegram_commands.relay_client.query", return_value={
        "result": '{"sets": [{"key": "interval_minutes", "value": "90"}]}',
        "session_id": "sess-1",
    }) as mock_query:
        reply = tc._dispatch(chat_id, "set interval to 90 minutes")

    assert "Set interval_minutes to 90" in reply
    args, kwargs = mock_query.call_args
    assert "Current config" in args[0]
    assert kwargs["system_prompt"] is not None
    assert kwargs.get("session_id") is None
    assert chat_id not in tc._pending_config


def test_set_config_follow_up_turn_resumes_session_without_resending_persona(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("origins:\n  - AMS\ninterval_minutes: 60\n")
    monkeypatch.setattr(tc, "CONFIG_PATH", config_path)

    chat_id = 1
    tc._dispatch(chat_id, "/set-config")

    with patch("telegram_commands.relay_client.query", return_value={
        "result": '{"clarification_needed": "Which setting do you mean?"}',
        "session_id": "sess-1",
    }):
        tc._dispatch(chat_id, "lower the limit")

    assert tc._pending_config[chat_id] == "sess-1"

    with patch("telegram_commands.relay_client.query", return_value={
        "result": '{"sets": [{"key": "interval_minutes", "value": "90"}]}',
        "session_id": "sess-1",
    }) as mock_query:
        reply = tc._dispatch(chat_id, "the interval")

    assert "Set interval_minutes to 90" in reply
    args, kwargs = mock_query.call_args
    assert args[0] == "the interval"
    assert kwargs["session_id"] == "sess-1"
    assert kwargs.get("system_prompt") is None


def test_set_config_falls_back_to_fresh_session_when_resume_fails(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("origins:\n  - AMS\ninterval_minutes: 60\n")
    monkeypatch.setattr(tc, "CONFIG_PATH", config_path)

    chat_id = 1
    tc._pending_config[chat_id] = "stale-session"

    responses = [
        requests.RequestException("session expired"),
        {"result": '{"sets": [{"key": "interval_minutes", "value": "90"}]}', "session_id": "sess-new"},
    ]

    def fake_query(*args, **kwargs):
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    with patch("telegram_commands.relay_client.query", side_effect=fake_query) as mock_query:
        reply = tc._dispatch(chat_id, "set interval to 90 minutes")

    assert "Set interval_minutes to 90" in reply
    assert mock_query.call_count == 2

    first_args, first_kwargs = mock_query.call_args_list[0]
    assert first_kwargs.get("session_id") == "stale-session"
    assert "system_prompt" not in first_kwargs

    second_args, second_kwargs = mock_query.call_args_list[1]
    assert second_kwargs["system_prompt"] is not None
    assert "Current config" in second_args[0]


def test_cancel_clears_pending_config():
    chat_id = 1
    tc._pending_config[chat_id] = "sess-1"

    reply = tc._dispatch(chat_id, "/cancel")

    assert reply == "Cancelled."
    assert chat_id not in tc._pending_config
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_telegram_commands.py -v`
Expected: FAIL — `AttributeError: module 'telegram_commands' has no attribute 'relay_client'`

- [ ] **Step 3: Modify `telegram_commands.py`**

Replace the imports and module-level constants at the top of the file (everything from the docstring through `_pending_config`):

```python
"""Telegram bot commands to view and edit config.yaml, and to create and
manage trip requests, from a phone.

/set-config and /new-trip each start a short conversation: the bot asks
what to change or where to go, the user replies in plain language, and a
Claude Code relay running on the same VPS turns that reply into concrete
edits. The relay's shared persona is sent once per conversation
(relay_client.PERSONA_PROMPT); every following turn resumes the same
session instead of resending it.
"""

import logging
import threading
import time
from pathlib import Path

import requests
from ruamel.yaml import YAML

import relay_client
from notifier import is_configured, send_message, _token, _chat_id

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.yaml"
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

_yaml = YAML()
_yaml.preserve_quotes = True

HELP_TEXT = (
    "*Flight Tracker commands*\n\n"
    "/set-config — change a shared setting by describing it in plain language\n"
    "/get — show current shared settings\n"
    "/cancel — cancel a pending /set-config\n"
    "/help — show this message\n\n"
    "Changes apply on the next scheduled check, except interval_minutes, "
    "which needs a restart."
)

_CONFIG_TASK_PROMPT = (
    "You turn a user's plain-language request into edits for a flight "
    "tracker's config. Respond with nothing but a single JSON object, "
    "shaped exactly like this:\n"
    '{"sets": [{"key": "dotted.path", "value": "string"}], '
    '"add_origins": ["IATA"], "remove_origins": ["IATA"], '
    '"clarification_needed": "a question, only if the request is unclear"}\n'
    "Use dotted paths for nested keys, e.g. passengers.adults. Use IATA "
    "airport codes for origins, translating city or airport names yourself. "
    "Omit fields that don't apply. If the request is unclear or names a "
    "setting that doesn't exist, set clarification_needed instead of "
    "guessing."
)

_pending_config: dict = {}
```

`socket` and `struct` are dropped from the imports — `_relay_host` (the only user of either) moved to `relay_client.py` in Task 1.

Remove the old `_relay_host`, `_extract_result`, `_extract_json`, `_relay_query` function definitions entirely (now in `relay_client.py`). Remove the old `_CONFIG_SYSTEM_PROMPT` constant (replaced by `_CONFIG_TASK_PROMPT` above, which is combined with `relay_client.PERSONA_PROMPT` at call time).

Add `_relay_turn`, right after the constants (before `_load_config`):

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

Keep `_load_config`, `_save_config`, `_format_config`, `_coerce`, `_apply_changes`, `_handle_get` unchanged.

Replace `_parse_config_request`:

```python
def _parse_config_request(text: str, config: dict, session_id=None) -> tuple:
    full_prompt = f"Current config (Python dict): {config!r}\n\nUser request: {text}"
    return _relay_turn(text, full_prompt, _CONFIG_TASK_PROMPT, session_id)
```

Replace `_handle_config_reply`:

```python
def _handle_config_reply(text: str, session_id) -> tuple:
    """Returns (reply, still_pending, session_id)."""
    config = _load_config()

    try:
        parsed, session_id = _parse_config_request(text, config, session_id=session_id)
    except Exception as exc:
        logger.error("Claude config parse failed: %s", exc)
        return f"Sorry, I couldn't process that: {exc}", True, session_id

    clarification = parsed.get("clarification_needed")
    if clarification:
        return clarification, True, session_id

    changes = _apply_changes(config, parsed)
    if not changes:
        return "I didn't find any changes to make there. Try rephrasing, or /cancel.", True, session_id

    _save_config(config)
    return "\n".join(changes), False, session_id
```

Add `_clear_pending` and replace `_dispatch`:

```python
def _clear_pending(chat_id) -> None:
    _pending_config.pop(chat_id, None)


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

    if command == "/set-config":
        _clear_pending(chat_id)
        _pending_config[chat_id] = None
        return "What would you like to change? Describe it in plain language."

    if chat_id in _pending_config:
        reply, still_pending, session_id = _handle_config_reply(stripped, _pending_config[chat_id])
        if still_pending:
            _pending_config[chat_id] = session_id
        else:
            _pending_config.pop(chat_id, None)
        return reply

    if command.startswith("/"):
        return f"Unknown command: {command}\n\n{HELP_TEXT}"

    return HELP_TEXT
```

Keep `_get_updates`, `_poll_loop`, `start` unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_telegram_commands.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add telegram_commands.py tests/test_telegram_commands.py
git commit -m "Retrofit /set-config onto relay_client with session reuse"
```

---

## Task 10: `telegram_commands.py` — `/new-trip` conversational flow

**Files:**
- Modify: `telegram_commands.py` (imports, `HELP_TEXT`, `_clear_pending`, `_dispatch`; add `_NEW_TRIP_TASK_PROMPT`, `_pending_trip`, `_parse_trip_request`, `_format_proposal`, `_handle_new_trip_reply`)
- Modify: `tests/test_telegram_commands.py` (append tests)

**Interfaces:**
- Consumes: `trips.create_trip()` (Task 3), `_relay_turn()` (Task 9).
- Produces: `telegram_commands._pending_trip: dict` (chat_id → `{"session_id": str | None, "proposal": dict | None}`).

- [ ] **Step 1: Write the failing tests**

`tests/test_telegram_commands.py` already has a `def setup_function(): tc._pending_config.clear()` from Task 9 — replace that one definition with the version below (do not add a second `setup_function`; a module can only have one, and pytest would silently use only the last one anyway). Add the `import storage` / `import trips` lines near the file's existing imports, and append the new test functions after the existing ones:

```python
def setup_function():
    tc._pending_config.clear()
    tc._pending_trip.clear()


def test_new_trip_shows_proposal_and_waits_for_confirmation():
    chat_id = 2
    tc._dispatch(chat_id, "/new-trip")

    with patch("telegram_commands.relay_client.query", return_value={
        "result": (
            '{"description": "Beach getaway", "destinations": ["BKK", "HKT"], '
            '"ideal_date": "2026-12-05", "ideal_return_date": "2026-12-19", '
            '"departure_range_before": 3, "departure_range_after": 3, '
            '"return_range_before": 3, "return_range_after": 3, '
            '"baseline_price_estimate": 650}'
        ),
        "session_id": "sess-trip-1",
    }):
        reply = tc._dispatch(chat_id, "somewhere warm in SE Asia in December")

    assert "BKK" in reply and "HKT" in reply
    assert "yes" in reply.lower()
    assert tc._pending_trip[chat_id]["proposal"]["description"] == "Beach getaway"


def test_new_trip_confirmation_creates_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(trips, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(trips, "_initialized", False)

    chat_id = 2
    tc._pending_trip[chat_id] = {
        "session_id": "sess-trip-1",
        "proposal": {
            "description": "Beach getaway", "destinations": ["BKK", "HKT"],
            "ideal_date": "2026-12-05", "ideal_return_date": "2026-12-19",
            "departure_range_before": 3, "departure_range_after": 3,
            "return_range_before": 3, "return_range_after": 3,
            "baseline_price_estimate": 650,
        },
    }

    reply = tc._dispatch(chat_id, "yes")

    assert "Beach getaway" in reply
    assert chat_id not in tc._pending_trip
    active = trips.get_active_trips()
    assert len(active) == 1
    assert active[0]["description"] == "Beach getaway"


def test_new_trip_clarification_keeps_conversation_open():
    chat_id = 2
    tc._dispatch(chat_id, "/new-trip")

    with patch("telegram_commands.relay_client.query", return_value={
        "result": '{"clarification_needed": "Which month did you mean?"}',
        "session_id": "sess-trip-1",
    }):
        reply = tc._dispatch(chat_id, "sometime next year")

    assert reply == "Which month did you mean?"
    assert chat_id in tc._pending_trip
    assert tc._pending_trip[chat_id]["proposal"] is None


def test_new_trip_command_clears_a_pending_set_config():
    chat_id = 2
    tc._pending_config[chat_id] = "some-session"

    tc._dispatch(chat_id, "/new-trip")

    assert chat_id not in tc._pending_config
    assert chat_id in tc._pending_trip
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_telegram_commands.py -v`
Expected: FAIL — `AttributeError: module 'telegram_commands' has no attribute '_pending_trip'`

- [ ] **Step 3: Modify `telegram_commands.py`**

Add `import trips` to the import block (alongside `import relay_client`).

Add the new task prompt, right after `_CONFIG_TASK_PROMPT`:

```python
_NEW_TRIP_TASK_PROMPT = (
    "You turn a user's plain-language travel request into a structured "
    "trip proposal for a flight-price tracker. Respond with nothing but a "
    "single JSON object, shaped exactly like this:\n"
    '{"description": "a short 3-6 word label for this trip", '
    '"destinations": ["IATA", ...], "origins": ["IATA", ...] (omit if not '
    'mentioned), "ideal_date": "YYYY-MM-DD", "ideal_return_date": '
    '"YYYY-MM-DD", "departure_range_before": int, "departure_range_after": '
    'int, "return_range_before": int, "return_range_after": int, '
    '"baseline_price_estimate": a plausible round-trip economy fare in EUR '
    'for this route as a number, "clarification_needed": "a question, only '
    'if the request is unclear"}\n'
    "Pick 1 to 6 destination airports. For a loose date description (a "
    "month, \"a couple of weeks in December\"), pick a sensible ideal_date "
    "roughly in the middle of it and a return date matching the trip "
    "length implied, with ranges wide enough to cover the described "
    "period. Default range fields to 3 when not implied by the request."
)
```

Add `_pending_trip` next to `_pending_config`:

```python
_pending_config: dict = {}
_pending_trip: dict = {}
```

Replace `_clear_pending`:

```python
def _clear_pending(chat_id) -> None:
    _pending_config.pop(chat_id, None)
    _pending_trip.pop(chat_id, None)
```

Add `_parse_trip_request` and `_format_proposal` after `_parse_config_request`:

```python
def _parse_trip_request(text: str, session_id=None) -> tuple:
    full_prompt = f"User request: {text}"
    return _relay_turn(text, full_prompt, _NEW_TRIP_TASK_PROMPT, session_id)


def _format_proposal(proposal: dict) -> str:
    destinations = ", ".join(proposal["destinations"])
    lines = [
        f"Destinations: {destinations}",
        f"Dates: {proposal['ideal_date']} to {proposal['ideal_return_date']} "
        f"(-{proposal['departure_range_before']}/+{proposal['departure_range_after']}d "
        f"departure, -{proposal['return_range_before']}/+{proposal['return_range_after']}d return)",
    ]
    if proposal.get("origins"):
        lines.append(f"Origins: {', '.join(proposal['origins'])}")
    if proposal.get("baseline_price_estimate"):
        lines.append(f"Est. fare: ~€{proposal['baseline_price_estimate']:.0f}")
    lines.append("Reply 'yes' to start tracking, or describe what to change.")
    return "\n".join(lines)
```

Add `_handle_new_trip_reply` after `_handle_config_reply`:

```python
def _handle_new_trip_reply(chat_id, text: str) -> str:
    state = _pending_trip[chat_id]

    if state["proposal"] and text.strip().lower() == "yes":
        proposal = state["proposal"]
        trip_id = trips.create_trip(
            description=proposal["description"],
            destinations=proposal["destinations"],
            ideal_date=proposal["ideal_date"],
            ideal_return_date=proposal["ideal_return_date"],
            departure_range_before=proposal.get("departure_range_before", 3),
            departure_range_after=proposal.get("departure_range_after", 3),
            return_range_before=proposal.get("return_range_before", 3),
            return_range_after=proposal.get("return_range_after", 3),
            origins=proposal.get("origins"),
            baseline_price_estimate=proposal.get("baseline_price_estimate"),
        )
        _pending_trip.pop(chat_id, None)
        return f"Trip #{trip_id} ({proposal['description']}) is now being tracked."

    try:
        parsed, session_id = _parse_trip_request(text, session_id=state["session_id"])
    except Exception as exc:
        logger.error("Claude trip parse failed: %s", exc)
        return f"Sorry, I couldn't process that: {exc}"

    state["session_id"] = session_id

    clarification = parsed.get("clarification_needed")
    if clarification:
        return clarification

    required = ("description", "destinations", "ideal_date", "ideal_return_date")
    if not all(parsed.get(field) for field in required):
        return "I couldn't work out a full trip from that. Try rephrasing, or /cancel."

    state["proposal"] = parsed
    return _format_proposal(parsed)
```

Update `HELP_TEXT`:

```python
HELP_TEXT = (
    "*Flight Tracker commands*\n\n"
    "/new-trip — start tracking a new trip by describing it in plain language\n"
    "/set-config — change a shared setting by describing it in plain language\n"
    "/get — show current shared settings\n"
    "/cancel — cancel a pending /set-config or /new-trip\n"
    "/help — show this message\n\n"
    "Changes apply on the next scheduled check, except interval_minutes, "
    "which needs a restart."
)
```

Modify `_dispatch`: add a `/new-trip` branch right after the `/set-config` branch, and a pending-trip check right after the pending-config check:

```python
    if command == "/set-config":
        _clear_pending(chat_id)
        _pending_config[chat_id] = None
        return "What would you like to change? Describe it in plain language."

    if command == "/new-trip":
        _clear_pending(chat_id)
        _pending_trip[chat_id] = {"session_id": None, "proposal": None}
        return (
            "Where and when do you want to go? Describe it in plain "
            "language — a place, a kind of destination, specific dates, "
            "or a loose period."
        )

    if chat_id in _pending_config:
        reply, still_pending, session_id = _handle_config_reply(stripped, _pending_config[chat_id])
        if still_pending:
            _pending_config[chat_id] = session_id
        else:
            _pending_config.pop(chat_id, None)
        return reply

    if chat_id in _pending_trip:
        return _handle_new_trip_reply(chat_id, stripped)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_telegram_commands.py -v`
Expected: PASS (9 tests: the 4 from Task 9 plus 5 new ones)

- [ ] **Step 5: Commit**

```bash
git add telegram_commands.py tests/test_telegram_commands.py
git commit -m "Add /new-trip conversational flow"
```

---

## Task 11: `telegram_commands.py` — `/trips` and `/cancel-trip`

**Files:**
- Modify: `telegram_commands.py` (imports, `HELP_TEXT`, `_dispatch`; add `_handle_trips_list`, `_handle_cancel_trip`)
- Modify: `tests/test_telegram_commands.py` (append tests)

**Interfaces:**
- Consumes: `trips.get_active_trips()`, `trips.cancel_trip()` (Task 3), `deals.cheapest_price()` (Task 4).
- Produces: no new module-level names beyond the two handler functions above (both are internal, exercised through `_dispatch`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_telegram_commands.py`:

```python
import deals


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


def test_cancel_trip_unknown_id():
    reply = tc._dispatch(3, "/cancel-trip 999")
    assert "No active trip" in reply


def test_cancel_trip_missing_id():
    reply = tc._dispatch(3, "/cancel-trip")
    assert "Usage" in reply
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_telegram_commands.py -v`
Expected: FAIL — `/trips` and `/cancel-trip` fall through to the "Unknown command" branch instead of the expected replies

- [ ] **Step 3: Modify `telegram_commands.py`**

Add `import deals` to the import block.

Add `_handle_trips_list` and `_handle_cancel_trip` after `_handle_get`:

```python
def _handle_trips_list() -> str:
    active = trips.get_active_trips()
    if not active:
        return "No active trips."

    lines = []
    for trip in active:
        cheapest = deals.cheapest_price(trip["id"])
        price_note = f"€{cheapest:.0f}" if cheapest is not None else "no data yet"
        lines.append(
            f"#{trip['id']} {trip['description']} — "
            f"{trip['ideal_date']} to {trip['ideal_return_date']} — "
            f"cheapest so far: {price_note}"
        )
    return "\n".join(lines)


def _handle_cancel_trip(args: list) -> str:
    if not args or not args[0].isdigit():
        return "Usage: /cancel-trip <id>"

    trip_id = int(args[0])
    if trips.cancel_trip(trip_id):
        return f"Trip #{trip_id} cancelled."
    return f"No active trip with id {trip_id}."
```

Update `HELP_TEXT`:

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
    "Changes apply on the next scheduled check, except interval_minutes, "
    "which needs a restart."
)
```

Add two branches to `_dispatch`, right after the `/get` branch:

```python
    if command == "/trips":
        _clear_pending(chat_id)
        return _handle_trips_list()

    if command == "/cancel-trip":
        _clear_pending(chat_id)
        return _handle_cancel_trip(stripped.split()[1:])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_telegram_commands.py -v`
Expected: PASS (14 tests total in the file)

- [ ] **Step 5: Commit**

```bash
git add telegram_commands.py tests/test_telegram_commands.py
git commit -m "Add /trips and /cancel-trip commands"
```

---

## Final verification

- [ ] Run the full suite: `pytest tests/ -v` — expect all tests across every task to pass together (no cross-task regressions).
- [ ] Confirm no remaining reference to the old single-trip `config.yaml` fields (`destination`, `ideal_date`, `ideal_return_date`, the four range fields, `price_alert_threshold`) outside of `trips.py`'s migration code: `grep -rn "config\[.destination.\]\|price_alert_threshold" --include="*.py" .` should return nothing outside `trips.py`.
- [ ] On the VPS: rebuild and restart the container, confirm the startup log shows the legacy trip migrated (or, on a fresh deploy, confirm `/new-trip` creates the first trip), then exercise `/new-trip`, `/trips`, `/cancel-trip`, and a `/set-config` clarification loop over Telegram to confirm the relay session-reuse behavior end-to-end — this repo's automated tests mock every relay call, so this is the only way to confirm the real relay conversation and Google Flights search still work together.
