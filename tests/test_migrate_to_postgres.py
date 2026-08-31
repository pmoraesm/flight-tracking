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


def test_migrate_converts_non_integer_stops_to_null(tmp_path):
    """tracker.py's scraper sometimes can't determine a flight's stop count
    and stores the string 'Unknown' instead of an int — SQLite's dynamic
    typing allows this despite the declared INTEGER column, but Postgres's
    real INTEGER column rejects it outright. NULL is the correct target
    value: it's the same "we don't know" meaning, and the column is
    nullable."""
    sqlite_path = tmp_path / "prices.db"
    _build_sqlite_fixture(sqlite_path)
    conn = sqlite3.connect(sqlite_path)
    conn.execute(
        "INSERT INTO prices (id, checked_at, origin, destination, depart_date, "
        "stops, price_value, trip_id) VALUES "
        "(4, '2026-01-01T00:00:00+00:00', 'AMS', 'GRU', '2026-07-11', "
        "'Unknown', 950.0, 7)"
    )
    conn.commit()
    conn.close()

    migrate_to_postgres.migrate(str(sqlite_path), storage.DATABASE_URL)

    pg_conn = psycopg.connect(storage.DATABASE_URL)
    stops = pg_conn.execute("SELECT stops FROM prices WHERE id = 4").fetchone()[0]
    pg_conn.close()
    assert stops is None


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
