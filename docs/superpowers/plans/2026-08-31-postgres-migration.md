# PostgreSQL Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move trip and price storage from a local SQLite file to PostgreSQL, with no functional change to any bot command.

**Architecture:** `storage.py`, `trips.py`, and `deals.py` switch from `sqlite3` to `psycopg` (psycopg3), each keeping its own one-connection-per-call style against a `DATABASE_URL`. A new `schema.sql` file replaces the old create-tables-on-first-connection pattern. A local `docker-compose` Postgres service backs development and tests. A one-time `migrate_to_postgres.py` script copies the live SQLite data across, preserving IDs. Docker networking changes let the production container reach `returnhub-postgres-1` on the VPS without losing its existing route to `claude-relay-server`.

**Tech Stack:** Python 3.11, PostgreSQL 16, `psycopg[binary]` 3.2.13, `docker-compose`, `pytest` (dev-only).

**Spec:** `docs/superpowers/specs/2026-08-31-postgres-migration-design.md`

## Global Constraints

- Use `psycopg` (psycopg3) with raw SQL only — no ORM, matching the existing `sqlite3` style.
- Keep one connection per function call in every file — do not add a connection pool.
- Preserve trip IDs exactly across the migration — `prices.trip_id` must keep pointing at the right row.
- `deals.py` keeps connecting to Postgres on its own, not through a shared `storage.py` helper.
- `schema.sql` is the single source of the schema — remove the old "create tables if missing" pattern from `storage.py` and `trips.py`.
- `DATABASE_URL` replaces `FLIGHT_DB_PATH` as the one environment variable for storage config. It stays out of git, the same as `TELEGRAM_BOT_TOKEN`.
- Local development and every test run point at the local `docker-compose` Postgres service — never the VPS instance.
- Run every task's tests with `pytest tests/ -v` from the repo root, with the local Postgres service up (`docker compose --profile dev up -d postgres`).

---

## Task 1: `schema.sql` and the local dev/test Postgres service

**Files:**
- Create: `schema.sql`
- Modify: `docker-compose.yml`

**Interfaces:**
- Produces: a running local Postgres, reachable at `postgresql://flight_tracker:flight_tracker_dev@localhost:5432/flight_tracker`, with `schema.sql` already applied.

- [ ] **Step 1: Write `schema.sql`**

```sql
CREATE TABLE trips (
    id                      INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    description             TEXT NOT NULL,
    destinations            JSONB NOT NULL,
    origins                 JSONB,
    ideal_date              DATE NOT NULL,
    ideal_return_date       DATE NOT NULL,
    departure_range_before  INTEGER NOT NULL,
    departure_range_after   INTEGER NOT NULL,
    return_range_before     INTEGER NOT NULL,
    return_range_after      INTEGER NOT NULL,
    seat                    TEXT,
    passengers              JSONB,
    max_duration_hours      INTEGER,
    results_per_query       INTEGER,
    baseline_price_estimate REAL,
    status                  TEXT NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active', 'cancelled', 'expired')),
    created_at              TIMESTAMPTZ NOT NULL
);

CREATE TABLE prices (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    checked_at  TIMESTAMPTZ NOT NULL,
    origin      TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart_date DATE NOT NULL,
    return_date DATE,
    airline     TEXT,
    departure   TEXT,
    arrival     TEXT,
    duration    TEXT,
    stops       INTEGER,
    price       TEXT,
    price_value REAL,
    is_best     BOOLEAN,
    trip_id     INTEGER REFERENCES trips(id)
);

CREATE INDEX idx_prices_lookup ON prices(origin, depart_date, return_date);
CREATE INDEX idx_prices_trip ON prices(trip_id);
```

- [ ] **Step 2: Add the local dev/test `postgres` service to `docker-compose.yml`**

Add a `postgres` service under `services:`, and its volume under `volumes:`. Leave `flight-tracker`'s service definition as-is for this task — Task 8 changes its networking.

```yaml
services:
  flight-tracker:
    build: .
    container_name: flight-tracker
    restart: unless-stopped
    network_mode: bridge
    env_file:
      - .env
    volumes:
      - flight-data:/data
      - ./config.yaml:/app/config.yaml

  postgres:
    image: postgres:16-alpine
    container_name: flight-tracker-postgres
    profiles: ["dev"]
    environment:
      POSTGRES_DB: flight_tracker
      POSTGRES_USER: flight_tracker
      POSTGRES_PASSWORD: flight_tracker_dev
    ports:
      - "5432:5432"
    volumes:
      - flight-tracker-postgres-data:/var/lib/postgresql/data
      - ./schema.sql:/docker-entrypoint-initdb.d/schema.sql

volumes:
  flight-data:
  flight-tracker-postgres-data:
```

The `profiles: ["dev"]` line keeps this service out of a plain `docker compose up -d` — the command the VPS uses — so it never starts there.

- [ ] **Step 3: Start the service and verify the schema applied**

Run:
```bash
docker compose --profile dev up -d postgres
sleep 3
docker compose exec postgres psql -U flight_tracker -d flight_tracker -c "\dt"
```
Expected: a table list showing `trips` and `prices`.

Run:
```bash
docker compose exec postgres psql -U flight_tracker -d flight_tracker -c "\d prices"
```
Expected: `trip_id` shows a foreign-key reference to `trips(id)`, and `is_best` shows type `boolean`.

- [ ] **Step 4: Verify the status check constraint**

Run:
```bash
docker compose exec postgres psql -U flight_tracker -d flight_tracker -c \
  "INSERT INTO trips (description, destinations, ideal_date, ideal_return_date, departure_range_before, departure_range_after, return_range_before, return_range_after, status, created_at) VALUES ('t', '[\"GRU\"]', '2026-01-01', '2026-01-10', 1, 1, 1, 1, 'bogus', now());"
```
Expected: an error naming the `status` check constraint. Then clean the table for later tasks:
```bash
docker compose exec postgres psql -U flight_tracker -d flight_tracker -c "TRUNCATE trips, prices RESTART IDENTITY CASCADE;"
```

- [ ] **Step 5: Commit**

```bash
git add schema.sql docker-compose.yml
git commit -m "Add PostgreSQL schema and local dev/test Postgres service"
```

---

## Task 2: `storage.py` on psycopg, shared test fixture, and dependency setup

**Files:**
- Modify: `storage.py`
- Modify: `requirements.txt`
- Modify: `.env.example`
- Create: `tests/conftest.py`
- Modify: `tests/test_storage.py`

**Interfaces:**
- Consumes: the local Postgres service from Task 1.
- Produces: `storage.DATABASE_URL: str`, `storage._get_connection() -> psycopg.Connection` (row factory `psycopg.rows.dict_row`), `storage.write_results(results: list[dict], trip_id: int) -> None` (unchanged signature). `tests/conftest.py`'s autouse `truncate_tables` fixture and its `TEST_DATABASE_URL` constant — every later test file relies on this fixture instead of its own SQLite setup.

- [ ] **Step 1: Add the dependency**

Add this line to `requirements.txt`:
```
psycopg[binary]==3.2.13
```

- [ ] **Step 2: Update `.env.example`**

Replace the `FLIGHT_DB_PATH` block with:
```
TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxYZ
TELEGRAM_CHAT_ID=987654321

# PostgreSQL connection string. Locally, this points at the docker-compose
# "postgres" dev service (docker compose --profile dev up -d postgres). On
# the VPS, this points at the new role/database inside returnhub-postgres-1
# and must never be committed here.
DATABASE_URL=postgresql://flight_tracker:flight_tracker_dev@localhost:5432/flight_tracker

# /set-config talks to the claude-relay-server on this VPS over the Docker
# bridge network — no key needed here. See docker-compose.yml's
# network_mode: bridge, required for the relay to accept the connection.
```

- [ ] **Step 3: Write `tests/conftest.py`**

```python
import os

import psycopg
import pytest

import storage
import trips

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://flight_tracker:flight_tracker_dev@localhost:5432/flight_tracker",
)


@pytest.fixture(autouse=True)
def truncate_tables(monkeypatch):
    monkeypatch.setattr(storage, "DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr(trips, "DATABASE_URL", TEST_DATABASE_URL)

    conn = psycopg.connect(TEST_DATABASE_URL)
    conn.execute("TRUNCATE TABLE prices, trips RESTART IDENTITY CASCADE")
    conn.commit()
    conn.close()
    yield
```

This fixture depends on `trips.DATABASE_URL` existing, which Task 3 adds. Until Task 3 lands, `monkeypatch.setattr(trips, "DATABASE_URL", ...)` fails with `AttributeError`, so add a placeholder module-level `DATABASE_URL = storage.DATABASE_URL` line at the top of `trips.py` in Step 4 below, ahead of the rest of Task 3's changes.

- [ ] **Step 4: Add a `DATABASE_URL` alias to `trips.py` so the fixture resolves**

At the top of `trips.py`, change:
```python
DB_PATH = storage.DB_PATH
```
to:
```python
DATABASE_URL = storage.DATABASE_URL
```
Leave the rest of `trips.py` untouched for now — Task 3 finishes its migration.

- [ ] **Step 5: Rewrite `storage.py`**

```python
"""Write flight price results to PostgreSQL."""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://flight_tracker:flight_tracker_dev@localhost:5432/flight_tracker",
)


def _get_connection() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


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
            bool(r.get("is_best", False)),
            trip_id,
        )
        for r in results
    ]

    try:
        conn = _get_connection()
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO prices (
                    checked_at, origin, destination, depart_date, return_date,
                    airline, departure, arrival, duration, stops,
                    price, price_value, is_best, trip_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                rows,
            )
        conn.commit()
        conn.close()
        logger.info("Postgres: wrote %d rows for trip #%s", len(rows), trip_id)
    except Exception as exc:
        logger.error("Postgres write failed: %s", exc)
```

`load_dotenv` runs here, not only in `notifier.py`, because `main.py` imports `storage` (via `deals.py`) before it imports `telegram_commands.py` — which is what pulls in `notifier.py` and its own `load_dotenv` call. Without this line, `storage.py` would read `DATABASE_URL` before `.env` was ever loaded.

- [ ] **Step 6: Rewrite `tests/test_storage.py`**

```python
import psycopg
from psycopg.types.json import Json

import storage


def _create_trip_row() -> int:
    conn = psycopg.connect(storage.DATABASE_URL)
    row = conn.execute(
        """
        INSERT INTO trips (
            description, destinations, ideal_date, ideal_return_date,
            departure_range_before, departure_range_after,
            return_range_before, return_range_after, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
        RETURNING id
        """,
        ("Trip", Json(["GRU"]), "2026-07-11", "2026-08-08", 1, 1, 1, 1),
    ).fetchone()
    conn.commit()
    conn.close()
    return row[0]


def test_write_results_tags_rows_with_trip_id():
    trip_id = _create_trip_row()
    results = [{
        "origin": "AMS", "destination": "GRU", "depart_date": "2026-07-11",
        "return_date": "2026-08-08", "airline": "KLM", "departure": "10:00",
        "arrival": "20:00", "duration": "11h", "stops": 0, "price": "€1200",
        "price_value": 1200.0, "is_best": True,
    }]

    storage.write_results(results, trip_id=trip_id)

    conn = psycopg.connect(storage.DATABASE_URL)
    row = conn.execute("SELECT origin, price_value, trip_id FROM prices").fetchone()
    conn.close()

    assert row == ("AMS", 1200.0, trip_id)


def test_write_results_does_nothing_for_empty_list():
    storage.write_results([], trip_id=None)

    conn = psycopg.connect(storage.DATABASE_URL)
    count = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    conn.close()

    assert count == 0
```

- [ ] **Step 7: Run the tests**

Run: `docker compose --profile dev up -d postgres && pytest tests/test_storage.py -v`
Expected: both tests PASS.

- [ ] **Step 8: Commit**

```bash
git add storage.py requirements.txt .env.example tests/conftest.py tests/test_storage.py trips.py
git commit -m "Migrate storage.py to PostgreSQL and add shared Postgres test fixture"
```

---

## Task 3: `trips.py` on psycopg

**Files:**
- Modify: `trips.py`
- Modify: `tests/test_trips.py`

**Interfaces:**
- Consumes: `storage.DATABASE_URL`, `storage._get_connection`-style pattern from Task 2.
- Produces (unchanged signatures): `trips.create_trip(...) -> int`, `trips.get_trip(trip_id) -> dict | None`, `trips.get_active_trips() -> list[dict]`, `trips.get_inactive_trips() -> list[dict]`, `trips.cancel_trip(trip_id) -> bool`, `trips.update_trip(...) -> bool`, `trips.merge_with_defaults(trip, config) -> dict`, `trips.migrate_legacy_config(config) -> dict`, `trips.migrate_config_file(path) -> dict`. A returned trip dict's `ideal_date` and `ideal_return_date` stay plain `str` values, matching the old SQLite behavior, even though the column type is now `DATE` — every other file in the codebase (`tracker.py`, `telegram_commands.py`) reads these fields as strings and is not touched by this migration.

- [ ] **Step 1: Rewrite `trips.py`**

```python
"""Trip requests: CRUD, default-merging, and legacy config migration."""

import logging
from datetime import date, datetime, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json
from ruamel.yaml import YAML

import storage

logger = logging.getLogger(__name__)

DATABASE_URL = storage.DATABASE_URL

_LEGACY_KEYS = (
    "destination", "ideal_date", "ideal_return_date",
    "departure_range_before", "departure_range_after",
    "return_range_before", "return_range_after",
    "price_alert_threshold",
)

_yaml = YAML()
_yaml.preserve_quotes = True


def _get_connection() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def _row_to_trip(row: dict) -> dict:
    trip = dict(row)
    trip["ideal_date"] = trip["ideal_date"].isoformat()
    trip["ideal_return_date"] = trip["ideal_return_date"].isoformat()
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
    row = conn.execute(
        """
        INSERT INTO trips (
            description, destinations, origins, ideal_date, ideal_return_date,
            departure_range_before, departure_range_after,
            return_range_before, return_range_after,
            seat, passengers, max_duration_hours, results_per_query,
            baseline_price_estimate, status, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s)
        RETURNING id
        """,
        (
            description,
            Json(destinations),
            Json(origins) if origins else None,
            ideal_date,
            ideal_return_date,
            departure_range_before,
            departure_range_after,
            return_range_before,
            return_range_after,
            seat,
            Json(passengers) if passengers else None,
            max_duration_hours,
            results_per_query,
            baseline_price_estimate,
            datetime.now(timezone.utc).isoformat(),
        ),
    ).fetchone()
    conn.commit()
    trip_id = row["id"]
    conn.close()
    return trip_id


def get_trip(trip_id: int):
    conn = _get_connection()
    row = conn.execute("SELECT * FROM trips WHERE id = %s", (trip_id,)).fetchone()
    conn.close()
    return _row_to_trip(row) if row else None


def get_active_trips() -> list:
    _expire_overdue_trips()
    conn = _get_connection()
    rows = conn.execute("SELECT * FROM trips WHERE status = 'active'").fetchall()
    conn.close()
    return [_row_to_trip(row) for row in rows]


def get_inactive_trips() -> list:
    """Cancelled or expired trips, most recently created first."""
    _expire_overdue_trips()
    conn = _get_connection()
    rows = conn.execute(
        "SELECT * FROM trips WHERE status != 'active' ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [_row_to_trip(row) for row in rows]


def cancel_trip(trip_id: int) -> bool:
    conn = _get_connection()
    cur = conn.execute(
        "UPDATE trips SET status = 'cancelled' WHERE id = %s AND status = 'active'",
        (trip_id,),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def update_trip(
    trip_id: int,
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
) -> bool:
    conn = _get_connection()
    cur = conn.execute(
        """
        UPDATE trips SET
            description = %s, destinations = %s, origins = %s, ideal_date = %s,
            ideal_return_date = %s, departure_range_before = %s, departure_range_after = %s,
            return_range_before = %s, return_range_after = %s, seat = %s, passengers = %s,
            max_duration_hours = %s, results_per_query = %s, baseline_price_estimate = %s
        WHERE id = %s AND status = 'active'
        """,
        (
            description,
            Json(destinations),
            Json(origins) if origins else None,
            ideal_date,
            ideal_return_date,
            departure_range_before,
            departure_range_after,
            return_range_before,
            return_range_after,
            seat,
            Json(passengers) if passengers else None,
            max_duration_hours,
            results_per_query,
            baseline_price_estimate,
            trip_id,
        ),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def _expire_overdue_trips(today=None) -> None:
    today = today or date.today()
    today_ordinal = today.toordinal()

    conn = _get_connection()
    rows = conn.execute(
        "SELECT id, ideal_return_date, return_range_after FROM trips WHERE status = 'active'"
    ).fetchall()
    for row in rows:
        cutoff = row["ideal_return_date"].toordinal() + row["return_range_after"]
        if cutoff < today_ordinal:
            logger.warning(
                "Trip #%s expired (return window ended %s + %s days)",
                row["id"], row["ideal_return_date"], row["return_range_after"],
            )
            conn.execute("UPDATE trips SET status = 'expired' WHERE id = %s", (row["id"],))
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

- [ ] **Step 2: Rewrite `tests/test_trips.py`**

Remove the `scratch_db` fixture — `tests/conftest.py`'s autouse `truncate_tables` fixture covers it now. Keep every other test body unchanged; only the fixture and imports change:

```python
from datetime import date, timedelta

import trips


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


def test_get_trip_returns_ideal_dates_as_plain_strings():
    trip_id = _create(ideal_date="2026-12-05", ideal_return_date="2026-12-19")

    trip = trips.get_trip(trip_id)

    assert trip["ideal_date"] == "2026-12-05"
    assert trip["ideal_return_date"] == "2026-12-19"
    assert isinstance(trip["ideal_date"], str)


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


def test_get_active_trips_logs_warning_when_expiring_a_trip(caplog):
    past_return = (date.today() - timedelta(days=10)).isoformat()
    trip_id = _create(ideal_date="2020-01-01", ideal_return_date=past_return)

    with caplog.at_level("WARNING"):
        trips.get_active_trips()

    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any(str(trip_id) in msg and "expired" in msg for msg in warnings)


def test_cancel_trip_returns_false_for_unknown_id():
    assert trips.cancel_trip(999) is False


def test_update_trip_changes_fields_and_returns_true():
    trip_id = _create(description="Original", destinations=["BKK"])

    updated = trips.update_trip(
        trip_id, description="Updated", destinations=["BKK", "HKT"],
        ideal_date="2026-12-05", ideal_return_date="2026-12-19",
        departure_range_before=5, departure_range_after=5,
        return_range_before=1, return_range_after=1,
    )

    assert updated is True
    trip = trips.get_trip(trip_id)
    assert trip["description"] == "Updated"
    assert trip["destinations"] == ["BKK", "HKT"]
    assert trip["departure_range_before"] == 5


def test_update_trip_returns_false_for_unknown_id():
    updated = trips.update_trip(
        999, description="d", destinations=["BKK"],
        ideal_date="2026-12-05", ideal_return_date="2026-12-19",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
    )

    assert updated is False


def test_update_trip_returns_false_for_cancelled_trip():
    trip_id = _create()
    trips.cancel_trip(trip_id)

    updated = trips.update_trip(
        trip_id, description="d", destinations=["BKK"],
        ideal_date="2026-12-05", ideal_return_date="2026-12-19",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
    )

    assert updated is False


def test_get_inactive_trips_returns_cancelled_and_expired_only():
    active_id = _create(description="Active")
    cancelled_id = _create(description="Cancelled")
    trips.cancel_trip(cancelled_id)
    past_return = (date.today() - timedelta(days=10)).isoformat()
    expired_id = _create(description="Expired", ideal_date="2020-01-01", ideal_return_date=past_return)

    inactive = trips.get_inactive_trips()

    assert active_id not in [t["id"] for t in inactive]
    assert {t["id"] for t in inactive} == {cancelled_id, expired_id}


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
        "ideal_date": "2026-12-11", "ideal_return_date": "2026-12-25",
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
        'ideal_date: "2026-12-11"\n'
        'ideal_return_date: "2026-12-25"\n'
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

- [ ] **Step 3: Run the tests**

Run: `pytest tests/test_trips.py -v`
Expected: every test PASSES, including the new `test_get_trip_returns_ideal_dates_as_plain_strings`.

- [ ] **Step 4: Commit**

```bash
git add trips.py tests/test_trips.py
git commit -m "Migrate trips.py to PostgreSQL"
```

---

## Task 4: `deals.py` on psycopg

**Files:**
- Modify: `deals.py`
- Modify: `tests/test_deals.py`

**Interfaces:**
- Consumes: `storage.DATABASE_URL`, `trips.create_trip` from Task 3.
- Produces (unchanged signatures): `deals.is_good_deal(trip_id, price_value, baseline_price_estimate) -> bool`, `deals.cheapest_price(trip_id) -> float | None`.

- [ ] **Step 1: Rewrite `deals.py`**

```python
"""Good-deal detection based on a trip's own historical prices."""

import statistics

import psycopg

import storage

MIN_HISTORY_FOR_PERCENTILE = 5
PERCENTILE_THRESHOLD = 20
BASELINE_DISCOUNT = 0.85


def _historical_prices(trip_id: int) -> list:
    conn = psycopg.connect(storage.DATABASE_URL)
    rows = conn.execute(
        "SELECT price_value FROM prices WHERE trip_id = %s", (trip_id,)
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

- [ ] **Step 2: Rewrite `tests/test_deals.py`**

```python
import psycopg

import storage
import trips
import deals


def _make_trip() -> int:
    return trips.create_trip(
        description="Trip", destinations=["GRU"],
        ideal_date="2026-07-11", ideal_return_date="2026-08-08",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
    )


def _seed_prices(trip_id, price_values):
    conn = psycopg.connect(storage.DATABASE_URL)
    for value in price_values:
        conn.execute(
            "INSERT INTO prices (checked_at, origin, destination, depart_date, "
            "price_value, trip_id) VALUES ('2026-01-01', 'AMS', 'GRU', "
            "'2026-07-11', %s, %s)",
            (value, trip_id),
        )
    conn.commit()
    conn.close()


def test_cold_start_no_baseline_never_alerts():
    trip_id = _make_trip()
    _seed_prices(trip_id, [])

    assert deals.is_good_deal(trip_id, 500, baseline_price_estimate=None) is False


def test_cold_start_uses_baseline_discount():
    trip_id = _make_trip()
    _seed_prices(trip_id, [900, 950])  # only 2 samples, below the percentile minimum

    assert deals.is_good_deal(trip_id, 849, baseline_price_estimate=1000) is True   # 1000*0.85=850
    assert deals.is_good_deal(trip_id, 851, baseline_price_estimate=1000) is False


def test_percentile_path_kicks_in_at_five_samples():
    trip_id = _make_trip()
    _seed_prices(trip_id, [100, 200, 300, 400, 500])

    assert deals.is_good_deal(trip_id, 100, baseline_price_estimate=None) is True
    assert deals.is_good_deal(trip_id, 500, baseline_price_estimate=None) is False


def test_percentile_path_ignores_a_generous_baseline():
    trip_id = _make_trip()
    _seed_prices(trip_id, [100, 200, 300, 400, 500])

    # A generous baseline must not override the percentile check once
    # there's enough history.
    assert deals.is_good_deal(trip_id, 500, baseline_price_estimate=10000) is False


def test_cheapest_price_returns_none_when_no_history():
    trip_id = _make_trip()
    _seed_prices(trip_id, [])

    assert deals.cheapest_price(trip_id) is None


def test_cheapest_price_returns_minimum():
    trip_id = _make_trip()
    _seed_prices(trip_id, [500, 200, 800])

    assert deals.cheapest_price(trip_id) == 200
```

- [ ] **Step 3: Run the tests**

Run: `pytest tests/test_deals.py -v`
Expected: every test PASSES.

- [ ] **Step 4: Commit**

```bash
git add deals.py tests/test_deals.py
git commit -m "Migrate deals.py to PostgreSQL"
```

---

## Task 5: `main.py` — keep a database error from crashing startup

**Files:**
- Modify: `main.py`
- Modify: `tests/test_main.py`

**Interfaces:**
- Consumes: `trips.get_active_trips()` from Task 3.
- Produces: `main.run_check() -> None`, now logging and returning early if loading active trips fails, instead of raising.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_main.py`:
```python
def test_run_check_logs_and_returns_when_loading_trips_fails(caplog):
    with patch("main.load_config", return_value={}), \
         patch("main.trips.get_active_trips", side_effect=RuntimeError("db down")), \
         patch("main.display.print_results") as mock_print_results:
        with caplog.at_level("ERROR"):
            main.run_check()

    mock_print_results.assert_not_called()
    assert any("db down" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_main.py::test_run_check_logs_and_returns_when_loading_trips_fails -v`
Expected: FAIL — `RuntimeError: db down` propagates instead of being caught.

- [ ] **Step 3: Update `run_check()` in `main.py`**

Change:
```python
def run_check() -> None:
    config = load_config()

    console.print("\n[bold cyan]Starting price check…[/bold cyan]")

    all_results = []
    all_alerts = []
    trip_summaries = []

    for trip in trips.get_active_trips():
```
to:
```python
def run_check() -> None:
    config = load_config()

    console.print("\n[bold cyan]Starting price check…[/bold cyan]")

    try:
        active_trips = trips.get_active_trips()
    except Exception as exc:
        logger.error("Could not load active trips: %s", exc)
        return

    all_results = []
    all_alerts = []
    trip_summaries = []

    for trip in active_trips:
```

- [ ] **Step 4: Run it to verify it passes**

Run: `pytest tests/test_main.py -v`
Expected: every test in the file PASSES.

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "Keep a database error from crashing the price-check startup run"
```

---

## Task 6: `telegram_commands.py` — report database errors like relay errors

**Files:**
- Modify: `telegram_commands.py`
- Modify: `tests/test_telegram_commands.py`

**Interfaces:**
- Consumes: `trips.*` from Task 3, `deals.cheapest_price` from Task 4, `psycopg.Error`.
- Produces: `telegram_commands._handle_trips_list() -> str`, `telegram_commands._handle_trip_history() -> str`, `telegram_commands._handle_cancel_trip(args) -> str`, `telegram_commands._confirm_trip(chat_id, state) -> str`, `telegram_commands._route(chat_id, text) -> str`, `telegram_commands._execute_action(state, parsed, config) -> str` — all unchanged signatures, now returning `"Sorry, I couldn't reach the database. Try again shortly."` and logging on a `psycopg.Error` instead of raising into `_poll_loop`.

- [ ] **Step 1: Add the `psycopg` import**

At the top of `telegram_commands.py`, after `import requests`:
```python
import psycopg
```

- [ ] **Step 2: Wrap `_handle_trips_list`**

Replace:
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
```
with:
```python
def _handle_trips_list() -> str:
    try:
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
    except psycopg.Error as exc:
        logger.error("Could not load active trips: %s", exc)
        return "Sorry, I couldn't reach the database. Try again shortly."
```

- [ ] **Step 3: Wrap `_handle_trip_history`**

Replace:
```python
def _handle_trip_history() -> str:
    inactive = trips.get_inactive_trips()
    if not inactive:
        return "No cancelled or finished trips."

    lines = []
    for trip in inactive:
        destinations = ", ".join(trip["destinations"])
        lines.append(
            f"#{trip['id']} {escape_md(trip['description'])} ({trip['status']}) — "
            f"{destinations} — {trip['ideal_date']} to {trip['ideal_return_date']}"
        )
    return "\n".join(lines)
```
with:
```python
def _handle_trip_history() -> str:
    try:
        inactive = trips.get_inactive_trips()
        if not inactive:
            return "No cancelled or finished trips."

        lines = []
        for trip in inactive:
            destinations = ", ".join(trip["destinations"])
            lines.append(
                f"#{trip['id']} {escape_md(trip['description'])} ({trip['status']}) — "
                f"{destinations} — {trip['ideal_date']} to {trip['ideal_return_date']}"
            )
        return "\n".join(lines)
    except psycopg.Error as exc:
        logger.error("Could not load trip history: %s", exc)
        return "Sorry, I couldn't reach the database. Try again shortly."
```

- [ ] **Step 4: Wrap `_handle_cancel_trip`**

Replace:
```python
def _handle_cancel_trip(args: list) -> str:
    if not args or not args[0].isdigit():
        return "Usage: /cancel-trip <id>"

    trip_id = int(args[0])
    if trips.cancel_trip(trip_id):
        return f"Trip #{trip_id} cancelled."
    return f"No active trip with id {trip_id}."
```
with:
```python
def _handle_cancel_trip(args: list) -> str:
    if not args or not args[0].isdigit():
        return "Usage: /cancel-trip <id>"

    trip_id = int(args[0])
    try:
        cancelled = trips.cancel_trip(trip_id)
    except psycopg.Error as exc:
        logger.error("Could not cancel trip #%s: %s", trip_id, exc)
        return "Sorry, I couldn't reach the database. Try again shortly."

    if cancelled:
        return f"Trip #{trip_id} cancelled."
    return f"No active trip with id {trip_id}."
```

- [ ] **Step 5: Wrap `_confirm_trip`**

Replace the whole function:
```python
def _confirm_trip(chat_id, state: dict) -> str:
    trip = state["trip_draft"]
    edit_id = state.get("trip_draft_id")
    _pending.pop(chat_id, None)

    if edit_id:
        updated = trips.update_trip(
            edit_id,
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
        if not updated:
            return f"Trip #{edit_id} is no longer active — nothing to update."
        return f"Trip #{edit_id} ({escape_md(trip['description'])}) updated."

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
    return f"Trip #{trip_id} ({escape_md(trip['description'])}) is now being tracked."
```
with:
```python
def _confirm_trip(chat_id, state: dict) -> str:
    trip = state["trip_draft"]
    edit_id = state.get("trip_draft_id")
    _pending.pop(chat_id, None)

    try:
        if edit_id:
            updated = trips.update_trip(
                edit_id,
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
            if not updated:
                return f"Trip #{edit_id} is no longer active — nothing to update."
            return f"Trip #{edit_id} ({escape_md(trip['description'])}) updated."

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
        return f"Trip #{trip_id} ({escape_md(trip['description'])}) is now being tracked."
    except psycopg.Error as exc:
        logger.error("Could not save trip: %s", exc)
        return "Sorry, I couldn't reach the database. Try again shortly."
```

- [ ] **Step 6: Wrap the `trips.get_active_trips()` call in `_route`**

Replace:
```python
    config = _load_config()
    active = trips.get_active_trips()
```
with:
```python
    config = _load_config()
    try:
        active = trips.get_active_trips()
    except psycopg.Error as exc:
        logger.error("Could not load active trips: %s", exc)
        return "Sorry, I couldn't reach the database. Try again shortly."
```

- [ ] **Step 7: Wrap the inline `cancel_trip` branch in `_execute_action`**

Replace:
```python
    trip_id = _coerce_trip_id(parsed.get("trip_id"))
    if action == "cancel_trip" and trip_id is not None:
        if trips.cancel_trip(trip_id):
            return f"Trip #{trip_id} cancelled."
        return f"No active trip with id {trip_id}."
```
with:
```python
    trip_id = _coerce_trip_id(parsed.get("trip_id"))
    if action == "cancel_trip" and trip_id is not None:
        try:
            cancelled = trips.cancel_trip(trip_id)
        except psycopg.Error as exc:
            logger.error("Could not cancel trip #%s: %s", trip_id, exc)
            return "Sorry, I couldn't reach the database. Try again shortly."
        if cancelled:
            return f"Trip #{trip_id} cancelled."
        return f"No active trip with id {trip_id}."
```

- [ ] **Step 8: Update `tests/test_telegram_commands.py`**

Remove every `monkeypatch.setattr(storage, "DB_PATH", ...)`, `monkeypatch.setattr(storage, "_initialized", False)`, `monkeypatch.setattr(trips, "DB_PATH", ...)`, `monkeypatch.setattr(trips, "_initialized", False)`, and `storage._get_connection().close()` line — `tests/conftest.py`'s autouse fixture now handles this for every test in the file. `_router_env` becomes:
```python
def _router_env(tmp_path, monkeypatch, origins=("AMS",)):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("origins:\n" + "".join(f"  - {o}\n" for o in origins))
    monkeypatch.setattr(tc, "CONFIG_PATH", config_path)
```
Every test that used to open with the four `monkeypatch.setattr(storage/trips, ...)` lines directly (`test_trips_list_shows_no_active_trips_message`, `test_trips_list_shows_cheapest_price`, `test_cancel_trip_marks_trip_cancelled`, `test_cancel_trip_unknown_id`, `test_cancel_trip_command_does_not_clear_pending_draft`, `test_trip_history_command_lists_cancelled_and_expired_trips`, `test_trip_history_command_shows_empty_message`) drops those lines and its now-unused `tmp_path, monkeypatch` parameters where nothing else in the test needs them.

Add three new tests, near the end of the file:
```python
def test_trips_list_reports_database_error(monkeypatch):
    monkeypatch.setattr(tc.trips, "get_active_trips", lambda: (_ for _ in ()).throw(psycopg.OperationalError("down")))

    reply = tc._dispatch(3, "/trips")

    assert "couldn't reach the database" in reply


def test_trip_history_reports_database_error(monkeypatch):
    monkeypatch.setattr(tc.trips, "get_inactive_trips", lambda: (_ for _ in ()).throw(psycopg.OperationalError("down")))

    reply = tc._dispatch(3, "/trip-history")

    assert "couldn't reach the database" in reply


def test_cancel_trip_reports_database_error(monkeypatch):
    monkeypatch.setattr(tc.trips, "cancel_trip", lambda trip_id: (_ for _ in ()).throw(psycopg.OperationalError("down")))

    reply = tc._dispatch(3, "/cancel-trip 7")

    assert "couldn't reach the database" in reply
```
Add `import psycopg` near the top of the file, alongside the existing `import requests`.

- [ ] **Step 9: Run the tests**

Run: `pytest tests/test_telegram_commands.py -v`
Expected: every test PASSES, including the three new database-error tests.

- [ ] **Step 10: Commit**

```bash
git add telegram_commands.py tests/test_telegram_commands.py
git commit -m "Report database errors from Telegram commands the same way relay errors are reported"
```

---

## Task 7: `migrate_to_postgres.py` — one-time SQLite-to-Postgres data migration

**Files:**
- Create: `migrate_to_postgres.py`
- Create: `tests/test_migrate_to_postgres.py`

**Interfaces:**
- Consumes: `storage.DATABASE_URL` from Task 2.
- Produces: `migrate_to_postgres.migrate(sqlite_path: str, database_url: str) -> None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_migrate_to_postgres.py`:
```python
import sqlite3

import psycopg

import migrate_to_postgres
import storage
import trips


def _build_sqlite_fixture(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE trips (
            id INTEGER PRIMARY KEY, description TEXT NOT NULL,
            destinations TEXT NOT NULL, origins TEXT, ideal_date TEXT NOT NULL,
            ideal_return_date TEXT NOT NULL, departure_range_before INTEGER NOT NULL,
            departure_range_after INTEGER NOT NULL, return_range_before INTEGER NOT NULL,
            return_range_after INTEGER NOT NULL, seat TEXT, passengers TEXT,
            max_duration_hours INTEGER, results_per_query INTEGER,
            baseline_price_estimate REAL, status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        );
        CREATE TABLE prices (
            id INTEGER PRIMARY KEY, checked_at TEXT NOT NULL, origin TEXT NOT NULL,
            destination TEXT NOT NULL, depart_date TEXT NOT NULL, return_date TEXT,
            airline TEXT, departure TEXT, arrival TEXT, duration TEXT, stops INTEGER,
            price TEXT, price_value REAL, is_best INTEGER, trip_id INTEGER
        );
    """)
    conn.execute(
        "INSERT INTO trips (id, description, destinations, ideal_date, "
        "ideal_return_date, departure_range_before, departure_range_after, "
        "return_range_before, return_range_after, status, created_at) "
        "VALUES (7, 'Old trip', '[\"GRU\"]', '2026-07-11', '2026-08-08', "
        "1, 1, 1, 1, 'active', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO prices (id, checked_at, origin, destination, depart_date, "
        "price_value, is_best, trip_id) VALUES "
        "(3, '2026-01-01T00:00:00+00:00', 'AMS', 'GRU', '2026-07-11', 900.0, 1, 7)"
    )
    conn.commit()
    conn.close()


def test_migrate_preserves_ids_and_trip_reference(tmp_path):
    sqlite_path = tmp_path / "prices.db"
    _build_sqlite_fixture(sqlite_path)

    migrate_to_postgres.migrate(str(sqlite_path), storage.DATABASE_URL)

    trip = trips.get_trip(7)
    assert trip["description"] == "Old trip"

    conn = psycopg.connect(storage.DATABASE_URL)
    price_row = conn.execute(
        "SELECT id, trip_id, price_value FROM prices WHERE id = 3"
    ).fetchone()
    conn.close()
    assert price_row == (3, 7, 900.0)


def test_migrate_resets_sequence_so_new_trip_gets_higher_id(tmp_path):
    sqlite_path = tmp_path / "prices.db"
    _build_sqlite_fixture(sqlite_path)

    migrate_to_postgres.migrate(str(sqlite_path), storage.DATABASE_URL)

    new_id = trips.create_trip(
        description="New trip", destinations=["BKK"],
        ideal_date="2026-09-01", ideal_return_date="2026-09-15",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
    )

    assert new_id > 7
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_migrate_to_postgres.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'migrate_to_postgres'`.

- [ ] **Step 3: Write `migrate_to_postgres.py`**

```python
"""One-time script: copy prices.db (SQLite) into PostgreSQL.

Run by hand during cutover, with the bot stopped. Reads the Postgres
target from DATABASE_URL (via storage.py); takes the source SQLite path
as a command-line argument.

Usage: python migrate_to_postgres.py /data/prices.db
"""

import json
import sqlite3
import sys

import psycopg
from psycopg.types.json import Json

import storage


def _migrate_trips(sqlite_conn, pg_conn) -> None:
    rows = sqlite_conn.execute("SELECT * FROM trips ORDER BY id").fetchall()
    with pg_conn.cursor() as cur:
        for row in rows:
            cur.execute(
                """
                INSERT INTO trips (
                    id, description, destinations, origins, ideal_date,
                    ideal_return_date, departure_range_before, departure_range_after,
                    return_range_before, return_range_after, seat, passengers,
                    max_duration_hours, results_per_query, baseline_price_estimate,
                    status, created_at
                ) OVERRIDING SYSTEM VALUE VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    row["id"], row["description"],
                    Json(json.loads(row["destinations"])),
                    Json(json.loads(row["origins"])) if row["origins"] else None,
                    row["ideal_date"], row["ideal_return_date"],
                    row["departure_range_before"], row["departure_range_after"],
                    row["return_range_before"], row["return_range_after"],
                    row["seat"],
                    Json(json.loads(row["passengers"])) if row["passengers"] else None,
                    row["max_duration_hours"], row["results_per_query"],
                    row["baseline_price_estimate"], row["status"], row["created_at"],
                ),
            )
    pg_conn.commit()


def _migrate_prices(sqlite_conn, pg_conn) -> None:
    rows = sqlite_conn.execute("SELECT * FROM prices ORDER BY id").fetchall()
    with pg_conn.cursor() as cur:
        for row in rows:
            cur.execute(
                """
                INSERT INTO prices (
                    id, checked_at, origin, destination, depart_date, return_date,
                    airline, departure, arrival, duration, stops, price,
                    price_value, is_best, trip_id
                ) OVERRIDING SYSTEM VALUE VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    row["id"], row["checked_at"], row["origin"], row["destination"],
                    row["depart_date"], row["return_date"], row["airline"],
                    row["departure"], row["arrival"], row["duration"], row["stops"],
                    row["price"], row["price_value"],
                    bool(row["is_best"]) if row["is_best"] is not None else None,
                    row["trip_id"],
                ),
            )
    pg_conn.commit()


def _reset_sequences(pg_conn) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT setval(pg_get_serial_sequence('trips', 'id'), "
            "COALESCE((SELECT MAX(id) FROM trips), 0) + 1, false)"
        )
        cur.execute(
            "SELECT setval(pg_get_serial_sequence('prices', 'id'), "
            "COALESCE((SELECT MAX(id) FROM prices), 0) + 1, false)"
        )
    pg_conn.commit()


def migrate(sqlite_path: str, database_url: str) -> None:
    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row
    pg_conn = psycopg.connect(database_url)

    try:
        _migrate_trips(sqlite_conn, pg_conn)
        _migrate_prices(sqlite_conn, pg_conn)
        _reset_sequences(pg_conn)
    finally:
        sqlite_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python migrate_to_postgres.py <path-to-prices.db>")
        sys.exit(1)
    migrate(sys.argv[1], storage.DATABASE_URL)
```

- [ ] **Step 4: Run it to verify it passes**

Run: `pytest tests/test_migrate_to_postgres.py -v`
Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add migrate_to_postgres.py tests/test_migrate_to_postgres.py
git commit -m "Add one-time SQLite-to-Postgres data migration script"
```

---

## Task 8: VPS cutover — networking, role/database creation, and rollout runbook

**Files:**
- Modify: `docker-compose.yml`
- Modify: `.env.example`

**Interfaces:** None — this task is configuration plus a manual runbook. Nothing here has an automated test; the design spec calls for live-container verification, not a mock.

- [ ] **Step 1: Change `flight-tracker`'s networking in `docker-compose.yml`**

Replace:
```yaml
  flight-tracker:
    build: .
    container_name: flight-tracker
    restart: unless-stopped
    network_mode: bridge
    env_file:
      - .env
    volumes:
      - flight-data:/data
      - ./config.yaml:/app/config.yaml
```
with:
```yaml
  flight-tracker:
    build: .
    container_name: flight-tracker
    restart: unless-stopped
    networks:
      - bridge
      - returnhub_default
    env_file:
      - .env
    volumes:
      - flight-data:/data
      - ./config.yaml:/app/config.yaml
```
and add a top-level `networks:` block, alongside the existing `volumes:` block:
```yaml
networks:
  bridge:
    name: bridge
    external: true
  returnhub_default:
    external: true
```
Naming the `bridge` network entry `name: bridge` attaches the container to the real Docker default bridge network by its actual name — this keeps `relay_client.py`'s `172.17.0.1` gateway lookup working. `network_mode: bridge` cannot be combined with a `networks:` list, so this task removes it in favor of naming the same network explicitly.

- [ ] **Step 2: Update the relay comment in `.env.example`**

Replace:
```
# /set-config talks to the claude-relay-server on this VPS over the Docker
# bridge network — no key needed here. See docker-compose.yml's
# network_mode: bridge, required for the relay to accept the connection.
```
with:
```
# /set-config talks to the claude-relay-server on this VPS over the Docker
# bridge network — no key needed here. See docker-compose.yml's "bridge"
# entry under networks:, required for the relay to accept the connection.
```

- [ ] **Step 3: Commit the config changes**

```bash
git add docker-compose.yml .env.example
git commit -m "Attach flight-tracker to returnhub_default alongside the default bridge network"
```

- [ ] **Step 4: On the VPS, create the role and database inside `returnhub-postgres-1`**

Run, using that container's existing superuser (never store this password in the repo):
```bash
docker exec -it returnhub-postgres-1 psql -U postgres -c "
    CREATE ROLE flight_tracker WITH LOGIN PASSWORD '<generate a strong password>';
    CREATE DATABASE flight_tracker OWNER flight_tracker;
"
```

- [ ] **Step 5: Apply the schema**

From the flight-tracker checkout on the VPS host:
```bash
docker exec -i returnhub-postgres-1 psql -U flight_tracker -d flight_tracker < schema.sql
```

- [ ] **Step 6: Stop the bot and run the data migration**

```bash
docker compose stop flight-tracker
docker cp flight-data:/data/prices.db ./prices.db   # or the volume's host path, if bind-mounted
python3 migrate_to_postgres.py ./prices.db
```
`migrate_to_postgres.py` reads its Postgres target from `DATABASE_URL`, so export it first, matching the value about to go into `.env`:
```bash
export DATABASE_URL="postgresql://flight_tracker:<password>@returnhub-postgres-1:5432/flight_tracker"
```

- [ ] **Step 7: Add `DATABASE_URL` to the VPS's `.env`**

Add the same `DATABASE_URL` line from Step 6 to flight-tracker's `.env` on the VPS (gitignored, never in the repo).

- [ ] **Step 8: Confirm the `returnhub_default` network exists, then rebuild and restart**

```bash
docker network ls | grep returnhub_default
docker compose up -d --build flight-tracker
```

- [ ] **Step 9: Verify connectivity directly against the running containers**

```bash
docker exec flight-tracker python3 -c "import socket; print(socket.gethostbyname('returnhub-postgres-1'))"
```
Expected: an IP address, not a `socket.gaierror`.

```bash
docker exec flight-tracker python3 -c "import relay_client; print(relay_client.relay_host())"
```
Expected: `172.17.0.1` (or the live default-bridge gateway address) — not `relay_client.FALLBACK_RELAY_HOST` if the real lookup should have succeeded.

```bash
docker logs -f flight-tracker
```
Expected: a normal startup log, with no `Could not load active trips` or `Postgres write failed` error, and the next scheduled check completing.

- [ ] **Step 10: Send a real Telegram command as a final check**

Send `/trips` to the bot from the configured Telegram chat. Expected: the current active trips list, not the "couldn't reach the database" fallback message from Task 6.
