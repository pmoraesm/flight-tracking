import requests
from unittest.mock import patch

import deals
import storage
import trips
import telegram_commands as tc


def setup_function():
    tc._pending_config.clear()
    tc._pending_trip.clear()


def test_set_config_first_turn_sends_persona_and_context(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("origins:\n  - AMS\ninterval_minutes: 60\n")
    monkeypatch.setattr(tc, "CONFIG_PATH", config_path)

    chat_id = 1
    tc._dispatch(chat_id, "/set-config")

    with patch("telegram_commands.relay_client.query", return_value={
        "result": '{"sets": [{"key": "interval_minutes", "value": "90"}]}',
        "session_id": "sess-1",
    }) as mock_query:
        reply = tc._dispatch(chat_id, "set interval to 90 minutes")

    assert "Set interval_minutes to 90" in reply
    args, kwargs = mock_query.call_args
    assert "Current config" in args[0]
    assert kwargs["system_prompt"] is not None
    assert kwargs.get("session_id") is None
    assert chat_id not in tc._pending_config


def test_set_config_follow_up_turn_resumes_session_without_resending_persona(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("origins:\n  - AMS\ninterval_minutes: 60\n")
    monkeypatch.setattr(tc, "CONFIG_PATH", config_path)

    chat_id = 1
    tc._dispatch(chat_id, "/set-config")

    with patch("telegram_commands.relay_client.query", return_value={
        "result": '{"clarification_needed": "Which setting do you mean?"}',
        "session_id": "sess-1",
    }):
        tc._dispatch(chat_id, "lower the limit")

    assert tc._pending_config[chat_id] == "sess-1"

    with patch("telegram_commands.relay_client.query", return_value={
        "result": '{"sets": [{"key": "interval_minutes", "value": "90"}]}',
        "session_id": "sess-1",
    }) as mock_query:
        reply = tc._dispatch(chat_id, "the interval")

    assert "Set interval_minutes to 90" in reply
    args, kwargs = mock_query.call_args
    assert args[0] == "the interval"
    assert kwargs["session_id"] == "sess-1"
    assert kwargs.get("system_prompt") is None


def test_set_config_falls_back_to_fresh_session_when_resume_fails(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("origins:\n  - AMS\ninterval_minutes: 60\n")
    monkeypatch.setattr(tc, "CONFIG_PATH", config_path)

    chat_id = 1
    tc._pending_config[chat_id] = "stale-session"

    responses = [
        requests.RequestException("session expired"),
        {"result": '{"sets": [{"key": "interval_minutes", "value": "90"}]}', "session_id": "sess-new"},
    ]

    def fake_query(*args, **kwargs):
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    with patch("telegram_commands.relay_client.query", side_effect=fake_query) as mock_query:
        reply = tc._dispatch(chat_id, "set interval to 90 minutes")

    assert "Set interval_minutes to 90" in reply
    assert mock_query.call_count == 2

    first_args, first_kwargs = mock_query.call_args_list[0]
    assert first_kwargs.get("session_id") == "stale-session"
    assert "system_prompt" not in first_kwargs

    second_args, second_kwargs = mock_query.call_args_list[1]
    assert second_kwargs["system_prompt"] is not None
    assert "Current config" in second_args[0]


def test_cancel_clears_pending_config():
    chat_id = 1
    tc._pending_config[chat_id] = "sess-1"

    reply = tc._dispatch(chat_id, "/cancel")

    assert reply == "Cancelled."
    assert chat_id not in tc._pending_config


def test_new_trip_shows_proposal_and_waits_for_confirmation():
    chat_id = 2
    tc._dispatch(chat_id, "/new-trip")

    with patch("telegram_commands.relay_client.query", return_value={
        "result": (
            '{"description": "Beach getaway", "destinations": ["BKK", "HKT"], '
            '"ideal_date": "2026-12-05", "ideal_return_date": "2026-12-19", '
            '"departure_range_before": 3, "departure_range_after": 3, '
            '"return_range_before": 3, "return_range_after": 3, '
            '"baseline_price_estimate": 650}'
        ),
        "session_id": "sess-trip-1",
    }):
        reply = tc._dispatch(chat_id, "somewhere warm in SE Asia in December")

    assert "BKK" in reply and "HKT" in reply
    assert "yes" in reply.lower()
    assert tc._pending_trip[chat_id]["proposal"]["description"] == "Beach getaway"


def test_new_trip_confirmation_creates_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(trips, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(trips, "_initialized", False)

    chat_id = 2
    tc._pending_trip[chat_id] = {
        "session_id": "sess-trip-1",
        "proposal": {
            "description": "Beach getaway", "destinations": ["BKK", "HKT"],
            "ideal_date": "2026-12-05", "ideal_return_date": "2026-12-19",
            "departure_range_before": 3, "departure_range_after": 3,
            "return_range_before": 3, "return_range_after": 3,
            "baseline_price_estimate": 650,
        },
    }

    reply = tc._dispatch(chat_id, "yes")

    assert "Beach getaway" in reply
    assert chat_id not in tc._pending_trip
    active = trips.get_active_trips()
    assert len(active) == 1
    assert active[0]["description"] == "Beach getaway"


def test_new_trip_clarification_keeps_conversation_open():
    chat_id = 2
    tc._dispatch(chat_id, "/new-trip")

    with patch("telegram_commands.relay_client.query", return_value={
        "result": '{"clarification_needed": "Which month did you mean?"}',
        "session_id": "sess-trip-1",
    }):
        reply = tc._dispatch(chat_id, "sometime next year")

    assert reply == "Which month did you mean?"
    assert chat_id in tc._pending_trip
    assert tc._pending_trip[chat_id]["proposal"] is None


def test_new_trip_command_clears_a_pending_set_config():
    chat_id = 2
    tc._pending_config[chat_id] = "some-session"

    tc._dispatch(chat_id, "/new-trip")

    assert chat_id not in tc._pending_config
    assert chat_id in tc._pending_trip


def test_trips_list_shows_no_active_trips_message(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(trips, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(trips, "_initialized", False)

    reply = tc._dispatch(3, "/trips")

    assert reply == "No active trips."


def test_trips_list_shows_cheapest_price(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(trips, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(trips, "_initialized", False)

    trip_id = trips.create_trip(
        description="Beach getaway", destinations=["BKK"],
        ideal_date="2026-12-05", ideal_return_date="2026-12-19",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
    )
    storage.write_results([{
        "origin": "AMS", "destination": "BKK", "depart_date": "2026-12-05",
        "return_date": "2026-12-19", "price_value": 650.0, "price": "€650",
    }], trip_id=trip_id)

    reply = tc._dispatch(3, "/trips")

    assert "Beach getaway" in reply
    assert "€650" in reply


def test_cancel_trip_marks_trip_cancelled(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(trips, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(trips, "_initialized", False)

    trip_id = trips.create_trip(
        description="Beach getaway", destinations=["BKK"],
        ideal_date="2026-12-05", ideal_return_date="2026-12-19",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
    )

    reply = tc._dispatch(3, f"/cancel-trip {trip_id}")

    assert f"Trip #{trip_id} cancelled" in reply
    assert trips.get_active_trips() == []


def test_cancel_trip_unknown_id(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(trips, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(trips, "_initialized", False)

    reply = tc._dispatch(3, "/cancel-trip 999")
    assert "No active trip" in reply


def test_cancel_trip_missing_id():
    reply = tc._dispatch(3, "/cancel-trip")
    assert "Usage" in reply
