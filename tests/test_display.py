from flight_tracker import display


def test_print_results_groups_by_trip_and_shows_data():
    results = [{
        "trip_description": "Beach trip", "trip_id": 1, "origin": "AMS", "destination": "BKK",
        "depart_date": "2026-12-05", "return_date": "2026-12-19",
        "airline": "TG", "departure": "10:00", "arrival": "20:00",
        "duration": "12h", "stops": 0, "price": "€650", "price_value": 650.0,
        "is_best": True, "current_price_level": "low", "is_good_deal": True,
    }]

    with display.console.capture() as capture:
        display.print_results(results)

    output = capture.get()
    assert "Beach trip" in output
    assert "#1" in output
    assert "AMS" in output
    assert "€650" in output


def test_print_results_handles_empty_list():
    with display.console.capture() as capture:
        display.print_results([])

    assert "No results found" in capture.get()


def test_print_results_distinguishes_same_named_trips_by_id():
    results = [
        {
            "trip_description": "Beach trip", "trip_id": 1, "origin": "AMS", "destination": "BKK",
            "depart_date": "2026-12-05", "return_date": "2026-12-19",
            "airline": "TG", "departure": "10:00", "arrival": "20:00",
            "duration": "12h", "stops": 0, "price": "€650", "price_value": 650.0,
            "is_best": True, "current_price_level": "low", "is_good_deal": True,
        },
        {
            "trip_description": "Beach trip", "trip_id": 2, "origin": "LHR", "destination": "BKK",
            "depart_date": "2026-12-06", "return_date": "2026-12-20",
            "airline": "BA", "departure": "09:00", "arrival": "19:00",
            "duration": "13h", "stops": 1, "price": "€700", "price_value": 700.0,
            "is_best": False, "current_price_level": "typical", "is_good_deal": False,
        },
    ]

    with display.console.capture() as capture:
        display.print_results(results)

    output = capture.get()
    assert "#1 Beach trip" in output
    assert "#2 Beach trip" in output


def test_print_alerts_shows_trip_description():
    good_deals = [{
        "trip_description": "Beach trip", "trip_id": 1, "origin": "AMS", "destination": "BKK",
        "depart_date": "2026-12-05", "return_date": "2026-12-19",
        "airline": "TG", "duration": "12h", "stops": 0, "price": "€650",
        "price_value": 650.0,
    }]

    with display.console.capture() as capture:
        display.print_alerts(good_deals)

    output = capture.get()
    assert "Beach trip" in output
    assert "#1" in output


def test_print_alerts_does_nothing_for_empty_list():
    with display.console.capture() as capture:
        display.print_alerts([])

    assert capture.get() == ""
