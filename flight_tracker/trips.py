"""Trip requests: CRUD, default-merging, and legacy config migration."""

import logging
from datetime import date, datetime, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json
from ruamel.yaml import YAML

from . import storage

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
    trip["created_at"] = trip["created_at"].isoformat()
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
