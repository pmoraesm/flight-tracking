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
