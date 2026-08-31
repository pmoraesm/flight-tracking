import json
from unittest.mock import patch, MagicMock

from flight_tracker import relay_client


def test_relay_host_fallback_when_no_proc_net_route():
    with patch("builtins.open", side_effect=OSError):
        assert relay_client.relay_host() == relay_client.FALLBACK_RELAY_HOST


def test_extract_result_returns_result_and_session_id():
    lines = [
        json.dumps({"type": "AssistantMessage", "content": [{"text": "thinking"}]}),
        "",
        json.dumps({"type": "ResultMessage", "result": "hello", "session_id": "sess-1"}),
    ]
    out = relay_client.extract_result(lines)
    assert out == {"result": "hello", "session_id": "sess-1"}


def test_extract_json_from_prose():
    assert relay_client.extract_json('Sure! {"a": 1} done.') == {"a": 1}


def test_extract_json_no_json_returns_empty_dict():
    assert relay_client.extract_json("no brackets here") == {}


def _fake_response(result_text, session_id):
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.raise_for_status.return_value = None
    resp.iter_lines.return_value = [
        json.dumps({"type": "ResultMessage", "result": result_text, "session_id": session_id})
    ]
    return resp


def test_query_sends_session_id_when_provided_and_omits_system_prompt():
    with patch("flight_tracker.relay_client.requests.post", return_value=_fake_response("ok", "sess-2")) as post:
        out = relay_client.query("hi", session_id="sess-1")

    assert out == {"result": "ok", "session_id": "sess-2"}
    _, kwargs = post.call_args
    assert kwargs["json"]["session_id"] == "sess-1"
    assert "system_prompt" not in kwargs["json"]


def test_query_sends_system_prompt_when_no_session_id():
    with patch("flight_tracker.relay_client.requests.post", return_value=_fake_response("ok", "sess-1")) as post:
        relay_client.query("hi", system_prompt="be terse")

    _, kwargs = post.call_args
    assert kwargs["json"]["system_prompt"] == "be terse"
    assert "session_id" not in kwargs["json"]
