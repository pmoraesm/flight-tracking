"""Write flight price results to a local SQLite database."""

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("FLIGHT_DB_PATH", Path(__file__).parent / "prices.db"))

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

_initialized = False


def _get_connection() -> sqlite3.Connection:
    global _initialized
    conn = sqlite3.connect(DB_PATH)
    if not _initialized:
        conn.executescript(_SCHEMA)
        conn.commit()
        _initialized = True
    return conn


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
