import psycopg
import requests
from unittest.mock import patch

import deals
import storage
import trips
import telegram_commands as tc


def setup_function():
    tc._pending.clear()


def _router_env(tmp_path, monkeypatch, origins=("AMS",)):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("origins:\n" + "".join(f"  - {o}\n" for o in origins))
    monkeypatch.setattr(tc, "CONFIG_PATH", config_path)


def test_format_proposal_shows_configured_origins_when_not_overridden():
    proposal = {
        "destinations": ["GRU"],
        "ideal_date": "2026-12-14", "ideal_return_date": "2026-12-24",
        "departure_range_before": 13, "departure_range_after": 17,
        "return_range_before": 13, "return_range_after": 17,
        "baseline_price_estimate": 750,
    }
    config = {"origins": ["AMS", "BRU", "EIN"]}

    reply = tc._format_proposal(proposal, config)

    assert "Departure airports: AMS, BRU, EIN (from shared settings)" in reply


def test_format_proposal_shows_trip_specific_origins_when_set():
    proposal = {
        "destinations": ["GRU"], "origins": ["CDG", "ORY"],
        "ideal_date": "2026-12-14", "ideal_return_date": "2026-12-24",
        "departure_range_before": 13, "departure_range_after": 17,
        "return_range_before": 13, "return_range_after": 17,
    }
    config = {"origins": ["AMS", "BRU", "EIN"]}

    reply = tc._format_proposal(proposal, config)

    assert "Departure airports: CDG, ORY" in reply
    assert "from shared settings" not in reply


def test_propose_trip_from_free_text_with_no_pending_state(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch, origins=("AMS", "BRU"))

    chat_id = 2
    with patch("telegram_commands.relay_client.query", return_value={
        "result": (
            '{"action": "propose_trip", "trip": {'
            '"description": "Beach getaway", "destinations": ["BKK", "HKT"], '
            '"ideal_date": "2026-12-05", "ideal_return_date": "2026-12-19", '
            '"departure_range_before": 3, "departure_range_after": 3, '
            '"return_range_before": 3, "return_range_after": 3, '
            '"baseline_price_estimate": 650}}'
        ),
        "session_id": "sess-trip-1",
    }) as mock_query:
        reply = tc._dispatch(chat_id, "somewhere warm in SE Asia in December")

    assert "BKK" in reply and "HKT" in reply
    assert "Departure airports: AMS, BRU (from shared settings)" in reply
    assert "yes" in reply.lower()
    assert tc._pending[chat_id]["trip_draft"]["description"] == "Beach getaway"
    args, kwargs = mock_query.call_args
    assert kwargs["system_prompt"] is not None


def test_propose_trip_missing_range_field_defaults_instead_of_crashing(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    chat_id = 2
    with patch("telegram_commands.relay_client.query", return_value={
        "result": (
            '{"action": "propose_trip", "trip": {'
            '"description": "Beach getaway", "destinations": ["BKK", "HKT"], '
            '"ideal_date": "2026-12-05", "ideal_return_date": "2026-12-19", '
            '"departure_range_after": 3, '
            '"return_range_before": 3, "return_range_after": 3, '
            '"baseline_price_estimate": 650}}'
        ),
        "session_id": "sess-trip-1",
    }):
        reply = tc._dispatch(chat_id, "somewhere warm in SE Asia in December")

    assert not reply.startswith("Error:")
    assert "BKK" in reply and "HKT" in reply
    assert "-3/+3d departure" in reply
    assert tc._pending[chat_id]["trip_draft"]["departure_range_before"] == 3


def test_propose_trip_missing_required_field_shows_generic_message(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    with patch("telegram_commands.relay_client.query", return_value={
        "result": '{"action": "propose_trip", "trip": {"destinations": ["BKK"]}}',
        "session_id": "sess-1",
    }):
        reply = tc._dispatch(2, "somewhere")

    assert "couldn't work out a full trip" in reply


def test_revise_trip_resumes_session_without_resending_persona(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    chat_id = 2
    tc._pending[chat_id] = {
        "session_id": "sess-trip-1",
        "trip_draft": {
            "description": "Beach getaway", "destinations": ["BKK", "HKT"],
            "ideal_date": "2026-12-05", "ideal_return_date": "2026-12-19",
            "departure_range_before": 3, "departure_range_after": 3,
            "return_range_before": 3, "return_range_after": 3,
            "baseline_price_estimate": 650,
        },
    }

    with patch("telegram_commands.relay_client.query", return_value={
        "result": (
            '{"action": "revise_trip", "trip": {'
            '"description": "Beach getaway", "destinations": ["BKK"], '
            '"ideal_date": "2026-12-05", "ideal_return_date": "2026-12-19", '
            '"departure_range_before": 3, "departure_range_after": 3, '
            '"return_range_before": 3, "return_range_after": 3, '
            '"baseline_price_estimate": 650}}'
        ),
        "session_id": "sess-trip-1",
    }) as mock_query:
        reply = tc._dispatch(chat_id, "drop HKT, just BKK")

    assert "BKK" in reply and "HKT" not in reply
    args, kwargs = mock_query.call_args
    assert kwargs["session_id"] == "sess-trip-1"
    assert kwargs.get("system_prompt") is None


def test_resumed_turn_still_carries_the_json_contract_instructions(tmp_path, monkeypatch):
    """A resumed relay session doesn't reliably keep enforcing the first
    turn's system_prompt — observed live, a resumed session drifted into
    plain prose instead of JSON. Every turn must re-assert the contract
    in the prompt text itself, not rely on session memory for it."""
    _router_env(tmp_path, monkeypatch)

    chat_id = 1
    tc._pending[chat_id] = {"session_id": "sess-1", "trip_draft": None}

    with patch("telegram_commands.relay_client.query", return_value={
        "result": '{"action": "list_trips"}',
        "session_id": "sess-1",
    }) as mock_query:
        tc._dispatch(chat_id, "what am I tracking?")

    args, kwargs = mock_query.call_args
    assert '"action"' in args[0]
    assert "propose_trip" in args[0]


def test_yes_confirms_pending_trip_without_calling_relay(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    chat_id = 2
    tc._pending[chat_id] = {
        "session_id": "sess-trip-1",
        "trip_draft": {
            "description": "Beach getaway", "destinations": ["BKK", "HKT"],
            "ideal_date": "2026-12-05", "ideal_return_date": "2026-12-19",
            "departure_range_before": 3, "departure_range_after": 3,
            "return_range_before": 3, "return_range_after": 3,
            "baseline_price_estimate": 650,
        },
    }

    with patch("telegram_commands.relay_client.query") as mock_query:
        reply = tc._dispatch(chat_id, "yes")

    mock_query.assert_not_called()
    assert "Beach getaway" in reply
    assert chat_id not in tc._pending
    active = trips.get_active_trips()
    assert len(active) == 1
    assert active[0]["description"] == "Beach getaway"


def test_cancel_trip_action_cancels_matched_trip(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    trip_id = trips.create_trip(
        description="Rio getaway", destinations=["GIG"],
        ideal_date="2026-12-05", ideal_return_date="2026-12-19",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
    )

    with patch("telegram_commands.relay_client.query", return_value={
        "result": f'{{"action": "cancel_trip", "trip_id": {trip_id}}}',
        "session_id": "sess-1",
    }):
        reply = tc._dispatch(3, "cancel my Rio trip")

    assert f"Trip #{trip_id} cancelled" in reply
    assert trips.get_active_trips() == []


def test_list_trips_action_returns_active_trips(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    trips.create_trip(
        description="Beach getaway", destinations=["BKK"],
        ideal_date="2026-12-05", ideal_return_date="2026-12-19",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
    )

    with patch("telegram_commands.relay_client.query", return_value={
        "result": '{"action": "list_trips"}',
        "session_id": "sess-1",
    }):
        reply = tc._dispatch(3, "what trips am I tracking?")

    assert "Beach getaway" in reply


def test_set_config_action_applies_edit(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)
    tc.CONFIG_PATH.write_text("origins:\n  - AMS\ninterval_minutes: 60\n")

    chat_id = 1
    with patch("telegram_commands.relay_client.query", return_value={
        "result": (
            '{"action": "set_config", "config_edits": '
            '{"sets": [{"key": "interval_minutes", "value": "90"}]}}'
        ),
        "session_id": "sess-1",
    }) as mock_query:
        reply = tc._dispatch(chat_id, "set interval to 90 minutes")

    assert "Set interval\\_minutes to 90" in reply
    args, kwargs = mock_query.call_args
    assert "Shared config" in args[0]
    assert kwargs["system_prompt"] is not None


def test_show_config_action(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    with patch("telegram_commands.relay_client.query", return_value={
        "result": '{"action": "show_config"}',
        "session_id": "sess-1",
    }):
        reply = tc._dispatch(1, "what are my current settings?")

    assert "origins" in reply


def test_help_action_returns_help_text(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    with patch("telegram_commands.relay_client.query", return_value={
        "result": '{"action": "help"}',
        "session_id": "sess-1",
    }):
        reply = tc._dispatch(1, "what can you do?")

    assert reply == tc.HELP_TEXT


def test_answer_action_responds_to_a_question_without_losing_the_draft(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch, origins=("AMS", "BRU"))

    chat_id = 2
    draft = {
        "description": "Beach getaway", "destinations": ["GRU"],
        "ideal_date": "2026-12-14", "ideal_return_date": "2026-12-24",
        "departure_range_before": 13, "departure_range_after": 17,
        "return_range_before": 13, "return_range_after": 17,
        "baseline_price_estimate": 750,
    }
    tc._pending[chat_id] = {"session_id": "sess-trip-1", "trip_draft": draft}

    with patch("telegram_commands.relay_client.query", return_value={
        "result": '{"action": "answer", "reply": "AMS and BRU, from your shared settings."}',
        "session_id": "sess-trip-1",
    }):
        reply = tc._dispatch(chat_id, "What are the departure airports?")

    assert reply == "AMS and BRU, from your shared settings."
    assert tc._pending[chat_id]["trip_draft"] == draft


def test_malformed_router_output_falls_back_to_generic_message(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    with patch("telegram_commands.relay_client.query", return_value={
        "result": '{"action": "not_a_real_action"}',
        "session_id": "sess-1",
    }):
        reply = tc._dispatch(1, "asdkjhasd")

    assert "didn't understand" in reply


def test_router_relay_failure_reports_error(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    with patch("telegram_commands.relay_client.query", side_effect=requests.RequestException("boom")):
        reply = tc._dispatch(1, "anything")

    assert "couldn't process that" in reply


def test_router_falls_back_to_fresh_session_when_resume_fails(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    chat_id = 1
    tc._pending[chat_id] = {"session_id": "stale-session", "trip_draft": None}

    responses = [
        requests.RequestException("session expired"),
        {"result": '{"action": "show_config"}', "session_id": "sess-new"},
    ]

    def fake_query(*args, **kwargs):
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    with patch("telegram_commands.relay_client.query", side_effect=fake_query) as mock_query:
        reply = tc._dispatch(chat_id, "what are my settings?")

    assert "origins" in reply
    assert mock_query.call_count == 2

    first_args, first_kwargs = mock_query.call_args_list[0]
    assert first_kwargs.get("session_id") == "stale-session"
    assert "system_prompt" not in first_kwargs

    second_args, second_kwargs = mock_query.call_args_list[1]
    assert second_kwargs["system_prompt"] is not None


def test_cancel_clears_pending_state():
    chat_id = 1
    tc._pending[chat_id] = {"session_id": "sess-1", "trip_draft": None}

    reply = tc._dispatch(chat_id, "/cancel")

    assert reply == "Cancelled."
    assert chat_id not in tc._pending


def test_router_prompt_lists_analyze_action():
    assert "analyze" in tc._ROUTER_TASK_PROMPT


def test_analysis_persona_names_the_readonly_role_and_no_password():
    assert "flight_tracker_readonly" in tc._ANALYSIS_PERSONA_PROMPT
    assert "password" not in tc._ANALYSIS_PERSONA_PROMPT.lower().replace("no password needed", "")


def test_new_trip_command_resets_prior_pending_state():
    chat_id = 2
    tc._pending[chat_id] = {
        "session_id": "some-session", "trip_draft": {"description": "old"}, "trip_draft_id": 7,
    }

    tc._dispatch(chat_id, "/new-trip")

    assert tc._pending[chat_id] == {
        "session_id": None, "trip_draft": None, "trip_draft_id": None,
        "analysis_session_id": None,
    }


def test_trips_list_shows_no_active_trips_message():
    reply = tc._dispatch(3, "/trips")

    assert reply == "No active trips."


def test_trips_list_shows_cheapest_price():
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


def test_cancel_trip_marks_trip_cancelled():
    trip_id = trips.create_trip(
        description="Beach getaway", destinations=["BKK"],
        ideal_date="2026-12-05", ideal_return_date="2026-12-19",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
    )

    reply = tc._dispatch(3, f"/cancel-trip {trip_id}")

    assert f"Trip #{trip_id} cancelled" in reply
    assert trips.get_active_trips() == []


def test_cancel_trip_unknown_id():
    reply = tc._dispatch(3, "/cancel-trip 999")
    assert "No active trip" in reply


def test_cancel_trip_missing_id():
    reply = tc._dispatch(3, "/cancel-trip")
    assert "Usage" in reply


def test_get_command_does_not_clear_pending_draft(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    chat_id = 3
    draft = {"description": "Beach getaway"}
    tc._pending[chat_id] = {"session_id": "sess-1", "trip_draft": draft}

    tc._dispatch(chat_id, "/get")

    assert tc._pending[chat_id] == {"session_id": "sess-1", "trip_draft": draft}


def test_trips_command_does_not_clear_pending_draft(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    chat_id = 3
    draft = {"description": "Beach getaway"}
    tc._pending[chat_id] = {"session_id": "sess-1", "trip_draft": draft}

    tc._dispatch(chat_id, "/trips")

    assert tc._pending[chat_id] == {"session_id": "sess-1", "trip_draft": draft}


def test_cancel_trip_command_does_not_clear_pending_draft():
    chat_id = 3
    draft = {"description": "Beach getaway"}
    tc._pending[chat_id] = {"session_id": "sess-1", "trip_draft": draft}

    tc._dispatch(chat_id, "/cancel-trip 999")

    assert tc._pending[chat_id] == {"session_id": "sess-1", "trip_draft": draft}


def test_help_command_does_not_clear_pending_draft(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    chat_id = 3
    draft = {"description": "Beach getaway"}
    tc._pending[chat_id] = {"session_id": "sess-1", "trip_draft": draft}

    tc._dispatch(chat_id, "/help")

    assert tc._pending[chat_id] == {"session_id": "sess-1", "trip_draft": draft}


def test_cancel_trip_action_accepts_string_trip_id(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    trip_id = trips.create_trip(
        description="Rio getaway", destinations=["GIG"],
        ideal_date="2026-12-05", ideal_return_date="2026-12-19",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
    )

    with patch("telegram_commands.relay_client.query", return_value={
        "result": f'{{"action": "cancel_trip", "trip_id": "{trip_id}"}}',
        "session_id": "sess-1",
    }):
        reply = tc._dispatch(3, "cancel my Rio trip")

    assert f"Trip #{trip_id} cancelled" in reply
    assert trips.get_active_trips() == []


def test_bare_clarification_needed_field_is_treated_as_unclear_reply(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    with patch("telegram_commands.relay_client.query", return_value={
        "result": '{"clarification_needed": "Which trip?"}',
        "session_id": "sess-1",
    }):
        reply = tc._dispatch(1, "cancel it")

    assert reply == "Which trip?"


def test_edit_trip_action_shows_proposal_and_waits_for_confirmation(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    trip_id = trips.create_trip(
        description="Sao Paulo Trip", destinations=["GRU"],
        ideal_date="2026-12-15", ideal_return_date="2026-12-29",
        departure_range_before=14, departure_range_after=16,
        return_range_before=14, return_range_after=16,
    )

    with patch("telegram_commands.relay_client.query", return_value={
        "result": (
            '{"action": "edit_trip", "trip_id": %d, "trip": {'
            '"description": "Sao Paulo Trip", "destinations": ["GRU"], '
            '"ideal_date": "2026-12-15", "ideal_return_date": "2026-12-29", '
            '"departure_range_before": 5, "departure_range_after": 5, '
            '"return_range_before": 14, "return_range_after": 16}}' % trip_id
        ),
        "session_id": "sess-1",
    }):
        reply = tc._dispatch(2, "make the departure offset +-5 days")

    assert "-5/+5d departure" in reply
    assert "yes" in reply.lower()
    assert tc._pending[2]["trip_draft_id"] == trip_id
    assert tc._pending[2]["trip_draft"]["departure_range_before"] == 5


def test_edit_trip_action_accepts_string_trip_id(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    trip_id = trips.create_trip(
        description="Sao Paulo Trip", destinations=["GRU"],
        ideal_date="2026-12-15", ideal_return_date="2026-12-29",
        departure_range_before=14, departure_range_after=16,
        return_range_before=14, return_range_after=16,
    )

    with patch("telegram_commands.relay_client.query", return_value={
        "result": (
            '{"action": "edit_trip", "trip_id": "%d", "trip": {'
            '"description": "Sao Paulo Trip", "destinations": ["GRU"], '
            '"ideal_date": "2026-12-15", "ideal_return_date": "2026-12-29", '
            '"departure_range_before": 5, "departure_range_after": 5, '
            '"return_range_before": 14, "return_range_after": 16}}' % trip_id
        ),
        "session_id": "sess-1",
    }):
        tc._dispatch(2, "make the departure offset +-5 days")

    assert tc._pending[2]["trip_draft_id"] == trip_id


def test_yes_confirms_trip_edit_by_updating_not_creating(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    trip_id = trips.create_trip(
        description="Sao Paulo Trip", destinations=["GRU"],
        ideal_date="2026-12-15", ideal_return_date="2026-12-29",
        departure_range_before=14, departure_range_after=16,
        return_range_before=14, return_range_after=16,
    )

    chat_id = 2
    tc._pending[chat_id] = {
        "session_id": "sess-1",
        "trip_draft": {
            "description": "Sao Paulo Trip", "destinations": ["GRU"],
            "ideal_date": "2026-12-15", "ideal_return_date": "2026-12-29",
            "departure_range_before": 5, "departure_range_after": 5,
            "return_range_before": 14, "return_range_after": 16,
        },
        "trip_draft_id": trip_id,
    }

    with patch("telegram_commands.relay_client.query") as mock_query:
        reply = tc._dispatch(chat_id, "yes")

    mock_query.assert_not_called()
    assert f"Trip #{trip_id}" in reply
    assert "updated" in reply.lower()
    assert chat_id not in tc._pending
    updated = trips.get_trip(trip_id)
    assert updated["departure_range_before"] == 5
    assert len(trips.get_active_trips()) == 1


def test_propose_trip_after_an_edit_does_not_carry_over_trip_draft_id(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    chat_id = 2
    tc._pending[chat_id] = {
        "session_id": "sess-1",
        "trip_draft": {"description": "old edit draft"},
        "trip_draft_id": 4,
    }

    with patch("telegram_commands.relay_client.query", return_value={
        "result": (
            '{"action": "propose_trip", "trip": {'
            '"description": "Beach getaway", "destinations": ["BKK"], '
            '"ideal_date": "2026-12-05", "ideal_return_date": "2026-12-19", '
            '"departure_range_before": 3, "departure_range_after": 3, '
            '"return_range_before": 3, "return_range_after": 3}}'
        ),
        "session_id": "sess-1",
    }):
        tc._dispatch(chat_id, "actually, find me somewhere new")

    assert tc._pending[chat_id]["trip_draft_id"] is None


def test_trip_history_command_lists_cancelled_and_expired_trips():
    trip_id = trips.create_trip(
        description="Old Rio trip", destinations=["GIG"],
        ideal_date="2026-12-05", ideal_return_date="2026-12-19",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
    )
    trips.cancel_trip(trip_id)

    reply = tc._dispatch(3, "/trip-history")

    assert "Old Rio trip" in reply
    assert "GIG" in reply
    assert "cancelled" in reply.lower()


def test_trip_history_command_shows_empty_message():
    reply = tc._dispatch(3, "/trip-history")

    assert reply == "No cancelled or finished trips."


def test_trip_history_command_does_not_clear_pending_draft():
    chat_id = 3
    draft = {"description": "Beach getaway"}
    tc._pending[chat_id] = {"session_id": "sess-1", "trip_draft": draft}

    tc._dispatch(chat_id, "/trip-history")

    assert tc._pending[chat_id] == {"session_id": "sess-1", "trip_draft": draft}


def test_trip_history_action_via_router(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    trip_id = trips.create_trip(
        description="Old Rio trip", destinations=["GIG"],
        ideal_date="2026-12-05", ideal_return_date="2026-12-19",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
    )
    trips.cancel_trip(trip_id)

    with patch("telegram_commands.relay_client.query", return_value={
        "result": '{"action": "list_trip_history"}',
        "session_id": "sess-1",
    }):
        reply = tc._dispatch(3, "what trips have I cancelled?")

    assert "Old Rio trip" in reply


def test_trips_list_reports_database_error(monkeypatch):
    monkeypatch.setattr(tc.trips, "get_active_trips", lambda: (_ for _ in ()).throw(psycopg.OperationalError("down")))

    reply = tc._dispatch(3, "/trips")

    assert "couldn't reach the database" in reply


def test_trip_history_reports_database_error(monkeypatch):
    monkeypatch.setattr(tc.trips, "get_inactive_trips", lambda: (_ for _ in ()).throw(psycopg.OperationalError("down")))

    reply = tc._dispatch(3, "/trip-history")

    assert "couldn't reach the database" in reply


def test_cancel_trip_reports_database_error(monkeypatch):
    monkeypatch.setattr(tc.trips, "cancel_trip", lambda trip_id: (_ for _ in ()).throw(psycopg.OperationalError("down")))

    reply = tc._dispatch(3, "/cancel-trip 7")

    assert "couldn't reach the database" in reply


def test_confirm_trip_keeps_draft_when_create_trip_raises_db_error(monkeypatch):
    monkeypatch.setattr(tc.trips, "create_trip", lambda **kwargs: (_ for _ in ()).throw(psycopg.OperationalError("down")))

    chat_id = 2
    draft = {
        "description": "Beach getaway", "destinations": ["BKK", "HKT"],
        "ideal_date": "2026-12-05", "ideal_return_date": "2026-12-19",
        "departure_range_before": 3, "departure_range_after": 3,
        "return_range_before": 3, "return_range_after": 3,
        "baseline_price_estimate": 650,
    }
    tc._pending[chat_id] = {"session_id": "sess-trip-1", "trip_draft": draft}

    with patch("telegram_commands.relay_client.query") as mock_query:
        reply = tc._dispatch(chat_id, "yes")

    mock_query.assert_not_called()
    assert "couldn't reach the database" in reply
    assert chat_id in tc._pending
    assert tc._pending[chat_id]["trip_draft"] == draft


def test_relay_turn_default_persona_includes_general_persona(monkeypatch):
    with patch("telegram_commands.relay_client.query", return_value={
        "result": "{}", "session_id": "sess-1",
    }) as mock_query:
        tc._relay_turn("hello", "hello", "TASK PROMPT", None)

    args, kwargs = mock_query.call_args
    assert kwargs["system_prompt"] == f"{tc.relay_client.PERSONA_PROMPT}\n\nTASK PROMPT"


def test_relay_turn_with_empty_persona_sends_task_prompt_only(monkeypatch):
    with patch("telegram_commands.relay_client.query", return_value={
        "result": "{}", "session_id": "sess-1",
    }) as mock_query:
        tc._relay_turn("hello", "hello", "TASK PROMPT ONLY", None, persona="")

    args, kwargs = mock_query.call_args
    assert kwargs["system_prompt"] == "TASK PROMPT ONLY"


def test_analyze_action_starts_separate_analysis_session(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    chat_id = 5
    responses = [
        {"result": '{"action": "analyze", "query": "What was the cheapest fare to GRU?"}', "session_id": "sess-router-1"},
        {"result": '{"reply": "The cheapest fare to GRU was €410 on 2026-11-02."}', "session_id": "sess-analysis-1"},
    ]

    def fake_query(*args, **kwargs):
        return responses.pop(0)

    with patch("telegram_commands.relay_client.query", side_effect=fake_query) as mock_query:
        reply = tc._dispatch(chat_id, "what's the cheapest fare to Sao Paulo been?")

    assert reply == "The cheapest fare to GRU was €410 on 2026-11-02."
    assert tc._pending[chat_id]["session_id"] == "sess-router-1"
    assert tc._pending[chat_id]["analysis_session_id"] == "sess-analysis-1"
    assert mock_query.call_count == 2

    analysis_args, analysis_kwargs = mock_query.call_args_list[1]
    assert analysis_args[0] == "What was the cheapest fare to GRU?"
    assert analysis_kwargs["system_prompt"] == tc._ANALYSIS_PERSONA_PROMPT


def test_analyze_action_resumes_existing_analysis_session(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    chat_id = 5
    tc._pending[chat_id] = {
        "session_id": "sess-router-1", "trip_draft": None, "trip_draft_id": None,
        "analysis_session_id": "sess-analysis-1",
    }

    responses = [
        {"result": '{"action": "analyze", "query": "And the most expensive one?"}', "session_id": "sess-router-1"},
        {"result": '{"reply": "The most expensive fare was €900."}', "session_id": "sess-analysis-1"},
    ]

    def fake_query(*args, **kwargs):
        return responses.pop(0)

    with patch("telegram_commands.relay_client.query", side_effect=fake_query) as mock_query:
        reply = tc._dispatch(chat_id, "and the most expensive one?")

    assert reply == "The most expensive fare was €900."
    analysis_args, analysis_kwargs = mock_query.call_args_list[1]
    assert analysis_kwargs["session_id"] == "sess-analysis-1"
    assert "system_prompt" not in analysis_kwargs


def test_analyze_action_malformed_output_falls_back(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    chat_id = 5
    responses = [
        {"result": '{"action": "analyze", "query": "trend?"}', "session_id": "sess-router-1"},
        {"result": "not json", "session_id": "sess-analysis-1"},
    ]

    def fake_query(*args, **kwargs):
        return responses.pop(0)

    with patch("telegram_commands.relay_client.query", side_effect=fake_query):
        reply = tc._dispatch(chat_id, "what's the trend?")

    assert "didn't understand" in reply
    assert tc._pending[chat_id]["analysis_session_id"] == "sess-analysis-1"


def test_analyze_action_relay_failure_reports_error(tmp_path, monkeypatch):
    _router_env(tmp_path, monkeypatch)

    chat_id = 5
    responses = [
        {"result": '{"action": "analyze", "query": "trend?"}', "session_id": "sess-router-1"},
    ]

    def fake_query(*args, **kwargs):
        if responses:
            return responses.pop(0)
        raise requests.RequestException("boom")

    with patch("telegram_commands.relay_client.query", side_effect=fake_query):
        reply = tc._dispatch(chat_id, "what's the trend?")

    assert "couldn't process that" in reply


def test_analyze_command_resets_prior_pending_state():
    chat_id = 6
    tc._pending[chat_id] = {
        "session_id": "some-session", "trip_draft": {"description": "old"},
        "trip_draft_id": 7, "analysis_session_id": "old-analysis-session",
    }

    reply = tc._dispatch(chat_id, "/analyze")

    assert reply == "What would you like to know about your price history?"
    assert tc._pending[chat_id] == {
        "session_id": None, "trip_draft": None, "trip_draft_id": None,
        "analysis_session_id": None,
    }


def test_help_text_mentions_analyze_command():
    assert "/analyze" in tc.HELP_TEXT
