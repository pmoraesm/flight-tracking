from scripts import backfill_passengers
from flight_tracker import storage
from flight_tracker import trips


def test_backfill_sets_passengers_only_on_null_rows():
    with_override = trips.create_trip(
        description="Has its own passengers", destinations=["GRU"],
        ideal_date="2026-07-11", ideal_return_date="2026-08-08",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
        passengers={"adults": 4},
    )
    without_override = trips.create_trip(
        description="Falls back to config", destinations=["BKK"],
        ideal_date="2026-07-11", ideal_return_date="2026-08-08",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
    )

    updated = backfill_passengers.backfill(storage.DATABASE_URL, {"adults": 2, "children": 1})

    assert updated == 1
    assert trips.get_trip(with_override)["passengers"] == {"adults": 4}
    assert trips.get_trip(without_override)["passengers"] == {"adults": 2, "children": 1}


def test_backfill_returns_zero_when_nothing_to_update():
    trips.create_trip(
        description="Already set", destinations=["GRU"],
        ideal_date="2026-07-11", ideal_return_date="2026-08-08",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
        passengers={"adults": 1},
    )

    updated = backfill_passengers.backfill(storage.DATABASE_URL, {"adults": 2, "children": 1})

    assert updated == 0
