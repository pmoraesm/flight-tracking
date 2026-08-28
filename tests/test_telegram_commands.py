import requests
from unittest.mock import patch

import telegram_commands as tc


def setup_function():
    tc._pending_config.clear()


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
