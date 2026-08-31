"""One-time script: copy prices.db (SQLite) into PostgreSQL.

Run by hand during cutover, with the bot stopped. Reads the Postgres
target from DATABASE_URL (via storage.py); takes the source SQLite path
as a command-line argument.

Usage: python -m scripts.migrate_to_postgres /data/prices.db
"""

import json
import sqlite3
import sys

import psycopg
from psycopg.types.json import Json

from flight_tracker import storage


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


def _migrate_prices(sqlite_conn, pg_conn) -> None:
    rows = sqlite_conn.execute("SELECT * FROM prices ORDER BY id").fetchall()
    with pg_conn.cursor() as cur:
        for row in rows:
            stops = row["stops"]
            if not isinstance(stops, int):
                stops = None
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
                    row["departure"], row["arrival"], row["duration"], stops,
                    row["price"], row["price_value"],
                    bool(row["is_best"]) if row["is_best"] is not None else None,
                    row["trip_id"],
                ),
            )


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


def migrate(sqlite_path: str, database_url: str) -> None:
    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row
    pg_conn = psycopg.connect(database_url)

    try:
        _migrate_trips(sqlite_conn, pg_conn)
        _migrate_prices(sqlite_conn, pg_conn)
        _reset_sequences(pg_conn)
        pg_conn.commit()
    finally:
        sqlite_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python -m scripts.migrate_to_postgres <path-to-prices.db>")
        sys.exit(1)
    migrate(sys.argv[1], storage.DATABASE_URL)
