"""One-time script: fill in trips.passengers for rows still NULL under the
old shared-default-only scheme.

Every trip used to leave passengers NULL and fall back to config.yaml's
shared default at check time, with no record of what that default was
kept on the row. This backfills the value that was actually in effect —
2 adults, 1 child — before config.yaml's default changed to 1 adult, so
existing trips keep the passenger count their price history was actually
gathered against.

Run by hand once, after deploying the resolve-and-store change.

Usage: python backfill_passengers.py
"""

import psycopg
from psycopg.types.json import Json

import storage

PRE_CHANGE_DEFAULT = {"adults": 2, "children": 1, "child_age": 6}


def backfill(database_url: str, passengers: dict) -> int:
    """Set passengers on every trip row that still has it NULL. Returns the row count updated."""
    conn = psycopg.connect(database_url)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE trips SET passengers = %s WHERE passengers IS NULL",
            (Json(passengers),),
        )
        updated = cur.rowcount
    conn.commit()
    conn.close()
    return updated


if __name__ == "__main__":
    count = backfill(storage.DATABASE_URL, PRE_CHANGE_DEFAULT)
    print(f"Backfilled passengers on {count} trip(s).")
