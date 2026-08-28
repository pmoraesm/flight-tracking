from unittest.mock import patch

import notifier


def test_notify_alerts_includes_trip_description_and_price_level():
    good_deals = [{
        "trip_description": "Beach trip", "origin": "AMS", "destination": "BKK",
        "depart_date": "2026-12-05", "return_date": "2026-12-19",
        "airline": "KLM", "duration": "12h", "stops": 0, "price": "€650",
        "price_value": 650.0, "url": "https://example.com",
        "current_price_level": "low",
    }]

    with patch("notifier.send_message") as mock_send:
        notifier.notify_alerts(good_deals)

    sent_text = mock_send.call_args[0][0]
    assert "Beach trip" in sent_text
    assert "€650" in sent_text
    assert "Google rates this: low" in sent_text


def test_notify_alerts_sends_nothing_for_empty_list():
    with patch("notifier.send_message") as mock_send:
        notifier.notify_alerts([])

    mock_send.assert_not_called()


def test_notify_summary_has_one_line_per_trip_sorted_by_price():
    trip_summaries = [
        {"trip_description": "Pricier trip", "price": "€900", "price_value": 900.0,
         "origin": "AMS", "destination": "GRU", "depart_date": "2026-07-11",
         "return_date": "2026-08-08", "airline": "KLM", "stops": 0, "url": ""},
        {"trip_description": "Cheaper trip", "price": "€650", "price_value": 650.0,
         "origin": "AMS", "destination": "BKK", "depart_date": "2026-12-05",
         "return_date": "2026-12-19", "airline": "TG", "stops": 1, "url": ""},
    ]

    with patch("notifier.send_message") as mock_send:
        notifier.notify_summary(trip_summaries)

    sent_text = mock_send.call_args[0][0]
    assert sent_text.index("Cheaper trip") < sent_text.index("Pricier trip")


def test_notify_summary_sends_nothing_for_empty_list():
    with patch("notifier.send_message") as mock_send:
        notifier.notify_summary([])

    mock_send.assert_not_called()
