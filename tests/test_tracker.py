from unittest.mock import patch, MagicMock

import tracker


def _fake_flight(price="$100", duration="10h 0m", stops=0, is_best=True, name="Air"):
    flight = MagicMock()
    flight.price = price
    flight.duration = duration
    flight.stops = stops
    flight.is_best = is_best
    flight.name = name
    flight.departure = "10:00"
    flight.arrival = "20:00"
    return flight


def _base_trip(**overrides):
    trip = {
        "destinations": ["BKK"],
        "origins": ["AMS"],
        "seat": "economy",
        "results_per_query": 3,
        "passengers": {"adults": 1},
        "max_duration_hours": 0,
        "ideal_date": "2026-12-05",
        "ideal_return_date": "2026-12-19",
        "departure_range_before": 0,
        "departure_range_after": 0,
        "return_range_before": 0,
        "return_range_after": 0,
    }
    trip.update(overrides)
    return trip


def test_search_flights_loops_over_multiple_destinations():
    trip = _base_trip(destinations=["BKK", "HKT"])

    fake_result = MagicMock()
    fake_result.flights = [_fake_flight()]
    fake_result.current_price = "low"

    with patch("tracker.get_flights", return_value=fake_result) as mock_get_flights, \
         patch("tracker.time.sleep"):
        combos = list(tracker.search_flights(trip))

    # 1 origin × 2 destinations × 1 depart date × 1 return date = 2 combos
    assert mock_get_flights.call_count == 2
    assert len(combos) == 2
    destinations_seen = {combo[0]["destination"] for combo in combos}
    assert destinations_seen == {"BKK", "HKT"}


def test_search_flights_filters_by_max_duration():
    trip = _base_trip(max_duration_hours=5)

    fake_result = MagicMock()
    fake_result.flights = [_fake_flight(duration="14h 0m")]
    fake_result.current_price = "typical"

    with patch("tracker.get_flights", return_value=fake_result), \
         patch("tracker.time.sleep"):
        combos = list(tracker.search_flights(trip))

    assert combos == [[]]


def test_search_flights_skips_a_failing_combo_without_stopping_others():
    trip = _base_trip(destinations=["BKK", "HKT"])

    fake_result = MagicMock()
    fake_result.flights = [_fake_flight()]
    fake_result.current_price = "low"

    with patch("tracker.get_flights", side_effect=[Exception("boom"), fake_result]), \
         patch("tracker.time.sleep"):
        combos = list(tracker.search_flights(trip))

    assert len(combos) == 1
    assert combos[0][0]["destination"] == "HKT"
