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
