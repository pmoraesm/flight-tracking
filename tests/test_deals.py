import psycopg

from flight_tracker import storage
from flight_tracker import trips
from flight_tracker import deals


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


def test_cold_start_baseline_scales_with_passenger_count():
    trip_id = _make_trip()
    _seed_prices(trip_id, [900, 950])  # only 2 samples, below the percentile minimum

    # baseline is per-person (500); 2 passengers -> 1000 total, discount cut is 850
    assert deals.is_good_deal(trip_id, 849, baseline_price_estimate=500, passenger_count=2) is True
    assert deals.is_good_deal(trip_id, 999, baseline_price_estimate=500, passenger_count=2) is False


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
