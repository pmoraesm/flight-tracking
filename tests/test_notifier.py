from unittest.mock import MagicMock, patch

import requests

import notifier


def test_notify_alerts_includes_trip_description_and_price_level():
    good_deals = [{
        "trip_description": "Beach trip", "trip_id": 1, "origin": "AMS", "destination": "BKK",
        "depart_date": "2026-12-05", "return_date": "2026-12-19",
        "airline": "KLM", "duration": "12h", "stops": 0, "price": "€650",
        "price_value": 650.0, "url": "https://example.com",
        "current_price_level": "low",
    }]

    with patch("notifier.send_message") as mock_send:
        notifier.notify_alerts(good_deals)

    sent_text = mock_send.call_args[0][0]
    assert "Beach trip" in sent_text
    assert "#1" in sent_text
    assert "€650" in sent_text
    assert "Google rates this: low" in sent_text


def test_notify_alerts_sends_nothing_for_empty_list():
    with patch("notifier.send_message") as mock_send:
        notifier.notify_alerts([])

    mock_send.assert_not_called()


def test_notify_alerts_distinguishes_same_named_trips_by_id():
    good_deals = [
        {
            "trip_description": "Beach trip", "trip_id": 1, "origin": "AMS", "destination": "BKK",
            "depart_date": "2026-12-05", "return_date": "2026-12-19",
            "airline": "KLM", "duration": "12h", "stops": 0, "price": "€650",
            "price_value": 650.0, "url": "",
        },
        {
            "trip_description": "Beach trip", "trip_id": 2, "origin": "LHR", "destination": "BKK",
            "depart_date": "2026-12-06", "return_date": "2026-12-20",
            "airline": "BA", "duration": "13h", "stops": 1, "price": "€700",
            "price_value": 700.0, "url": "",
        },
    ]

    with patch("notifier.send_message") as mock_send:
        notifier.notify_alerts(good_deals)

    sent_text = mock_send.call_args[0][0]
    assert "#1 Beach trip" in sent_text
    assert "#2 Beach trip" in sent_text


def test_notify_summary_has_one_line_per_trip_sorted_by_price():
    trip_summaries = [
        {"trip_description": "Pricier trip", "trip_id": 1, "price": "€900", "price_value": 900.0,
         "origin": "AMS", "destination": "GRU", "depart_date": "2026-07-11",
         "return_date": "2026-08-08", "airline": "KLM", "stops": 0, "url": ""},
        {"trip_description": "Cheaper trip", "trip_id": 2, "price": "€650", "price_value": 650.0,
         "origin": "AMS", "destination": "BKK", "depart_date": "2026-12-05",
         "return_date": "2026-12-19", "airline": "TG", "stops": 1, "url": ""},
    ]

    with patch("notifier.send_message") as mock_send:
        notifier.notify_summary(trip_summaries)

    sent_text = mock_send.call_args[0][0]
    assert sent_text.index("Cheaper trip") < sent_text.index("Pricier trip")


def test_notify_summary_distinguishes_same_named_trips_by_id():
    trip_summaries = [
        {"trip_description": "Beach trip", "trip_id": 1, "price": "€900", "price_value": 900.0,
         "origin": "AMS", "destination": "GRU", "depart_date": "2026-07-11",
         "return_date": "2026-08-08", "airline": "KLM", "stops": 0, "url": ""},
        {"trip_description": "Beach trip", "trip_id": 2, "price": "€650", "price_value": 650.0,
         "origin": "AMS", "destination": "BKK", "depart_date": "2026-12-05",
         "return_date": "2026-12-19", "airline": "TG", "stops": 1, "url": ""},
    ]

    with patch("notifier.send_message") as mock_send:
        notifier.notify_summary(trip_summaries)

    sent_text = mock_send.call_args[0][0]
    assert "#1 Beach trip" in sent_text
    assert "#2 Beach trip" in sent_text


def test_notify_summary_sends_nothing_for_empty_list():
    with patch("notifier.send_message") as mock_send:
        notifier.notify_summary([])

    mock_send.assert_not_called()


def test_send_message_retries_without_markdown_on_http_error(monkeypatch):
    monkeypatch.setattr(notifier, "_token", lambda: "test-token")
    monkeypatch.setattr(notifier, "_chat_id", lambda: "test-chat")

    failing_resp = MagicMock()
    failing_resp.raise_for_status.side_effect = requests.HTTPError(response=failing_resp)
    succeeding_resp = MagicMock()
    succeeding_resp.raise_for_status.return_value = None

    with patch("notifier.requests.post", side_effect=[failing_resp, succeeding_resp]) as mock_post:
        result = notifier.send_message("*unbalanced markdown")

    assert result is True
    assert mock_post.call_count == 2

    first_kwargs = mock_post.call_args_list[0].kwargs
    assert first_kwargs["json"]["parse_mode"] == "Markdown"

    second_kwargs = mock_post.call_args_list[1].kwargs
    assert "parse_mode" not in second_kwargs["json"]


def test_send_message_does_not_retry_on_connection_error(monkeypatch):
    monkeypatch.setattr(notifier, "_token", lambda: "test-token")
    monkeypatch.setattr(notifier, "_chat_id", lambda: "test-chat")

    with patch(
        "notifier.requests.post",
        side_effect=requests.ConnectionError("connection refused"),
    ) as mock_post:
        result = notifier.send_message("hello")

    assert result is False
    assert mock_post.call_count == 1
