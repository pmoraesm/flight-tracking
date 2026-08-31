from unittest.mock import patch

import main


def _trip(trip_id=1, description="Beach trip", baseline=None):
    return {
        "id": trip_id, "description": description, "destinations": ["BKK"],
        "origins": None, "ideal_date": "2026-12-05", "ideal_return_date": "2026-12-19",
        "departure_range_before": 1, "departure_range_after": 1,
        "return_range_before": 1, "return_range_after": 1,
        "seat": None, "passengers": None, "max_duration_hours": None,
        "results_per_query": None, "baseline_price_estimate": baseline,
        "status": "active", "created_at": "2026-01-01T00:00:00+00:00",
    }


def _result(price_value, origin="AMS", destination="BKK"):
    return {
        "origin": origin, "destination": destination, "depart_date": "2026-12-05",
        "return_date": "2026-12-19", "airline": "TG", "departure": "10:00",
        "arrival": "20:00", "duration": "12h", "stops": 0,
        "price": f"€{price_value:.0f}", "price_value": price_value,
        "is_best": True, "current_price_level": "low", "url": "https://example.com",
    }


def test_run_check_tags_results_and_routes_good_deals_to_alerts():
    trip = _trip(baseline=700)
    combo = [_result(500)]  # well under 700*0.85

    with patch("main.load_config", return_value={"origins": ["AMS"], "seat": "economy",
                                                  "passengers": {}, "max_duration_hours": 0,
                                                  "results_per_query": 3}), \
         patch("main.trips.get_active_trips", return_value=[trip]), \
         patch("main.tracker.search_flights", return_value=iter([combo])), \
         patch("main.storage.write_results") as mock_write, \
         patch("main.deals.is_good_deal", return_value=True), \
         patch("main.display.print_results") as mock_print_results, \
         patch("main.display.print_alerts") as mock_print_alerts, \
         patch("main.is_configured", return_value=True), \
         patch("main.notify_alerts") as mock_notify_alerts:
        main.run_check()

    mock_write.assert_called_once_with(combo, trip_id=1)

    all_results_arg = mock_print_results.call_args[0][0]
    assert all_results_arg[0]["trip_description"] == "Beach trip"
    assert all_results_arg[0]["trip_id"] == 1
    assert all_results_arg[0]["is_good_deal"] is True

    alerts_arg = mock_print_alerts.call_args[0][0]
    assert len(alerts_arg) == 1
    assert alerts_arg[0]["trip_description"] == "Beach trip"

    mock_notify_alerts.assert_called_once()


def test_run_check_skips_a_trip_that_raises_and_continues():
    good_trip = _trip(trip_id=2, description="Good trip")
    bad_trip = _trip(trip_id=1, description="Bad trip")
    combo = [_result(500)]

    def fake_search(merged):
        if merged["id"] == 1:
            raise ValueError("bad date")
        return iter([combo])

    with patch("main.load_config", return_value={"origins": ["AMS"], "seat": "economy",
                                                  "passengers": {}, "max_duration_hours": 0,
                                                  "results_per_query": 3}), \
         patch("main.trips.get_active_trips", return_value=[bad_trip, good_trip]), \
         patch("main.tracker.search_flights", side_effect=fake_search), \
         patch("main.storage.write_results"), \
         patch("main.deals.is_good_deal", return_value=False), \
         patch("main.display.print_results") as mock_print_results, \
         patch("main.display.print_alerts"), \
         patch("main.is_configured", return_value=False):
        main.run_check()

    all_results_arg = mock_print_results.call_args[0][0]
    assert len(all_results_arg) == 1
    assert all_results_arg[0]["trip_description"] == "Good trip"


def test_run_check_never_sends_the_unconditional_summary():
    """The routine per-check summary is disabled: main no longer holds a
    reference to notify_summary at all, so there is no path left that
    could call it."""
    assert not hasattr(main, "notify_summary")


def test_run_check_logs_and_returns_when_loading_trips_fails(caplog):
    with patch("main.load_config", return_value={}), \
         patch("main.trips.get_active_trips", side_effect=RuntimeError("db down")), \
         patch("main.display.print_results") as mock_print_results:
        with caplog.at_level("ERROR"):
            main.run_check()

    mock_print_results.assert_not_called()
    assert any("db down" in r.message for r in caplog.records)
