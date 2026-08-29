"""Telegram bot commands to view and edit config.yaml, and to create and
manage trip requests, from a phone.

/set-config and /new-trip each start a short conversation: the bot asks
what to change or where to go, the user replies in plain language, and a
Claude Code relay running on the same VPS turns that reply into concrete
edits. The relay's shared persona is sent once per conversation
(relay_client.PERSONA_PROMPT); every following turn resumes the same
session instead of resending it.
"""

import logging
import threading
import time
from pathlib import Path

import requests
from ruamel.yaml import YAML

import deals
import relay_client
import trips
from notifier import is_configured, send_message, _token, _chat_id

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.yaml"
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

_yaml = YAML()
_yaml.preserve_quotes = True

HELP_TEXT = (
    "*Flight Tracker commands*\n\n"
    "/new-trip — start tracking a new trip by describing it in plain language\n"
    "/trips — list active trips and their cheapest price so far\n"
    "/cancel-trip <id> — stop tracking a trip\n"
    "/set-config — change a shared setting by describing it in plain language\n"
    "/get — show current shared settings\n"
    "/cancel — cancel a pending /set-config or /new-trip\n"
    "/help — show this message\n\n"
    "Changes apply on the next scheduled check, except interval_minutes, "
    "which needs a restart."
)

_CONFIG_TASK_PROMPT = (
    "You turn a user's plain-language request into edits for a flight "
    "tracker's config. Respond with nothing but a single JSON object, "
    "shaped exactly like this:\n"
    '{"sets": [{"key": "dotted.path", "value": "string"}], '
    '"add_origins": ["IATA"], "remove_origins": ["IATA"], '
    '"clarification_needed": "a question, only if the request is unclear"}\n'
    "Use dotted paths for nested keys, e.g. passengers.adults. Use IATA "
    "airport codes for origins, translating city or airport names yourself. "
    "Omit fields that don't apply. If the request is unclear or names a "
    "setting that doesn't exist, set clarification_needed instead of "
    "guessing."
)

_NEW_TRIP_TASK_PROMPT = (
    "You turn a user's plain-language travel request into a structured "
    "trip proposal for a flight-price tracker. Respond with nothing but a "
    "single JSON object, shaped exactly like this:\n"
    '{"description": "a short 3-6 word label for this trip", '
    '"destinations": ["IATA", ...], "origins": ["IATA", ...] (omit if not '
    'mentioned), "ideal_date": "YYYY-MM-DD", "ideal_return_date": '
    '"YYYY-MM-DD", "departure_range_before": int, "departure_range_after": '
    'int, "return_range_before": int, "return_range_after": int, '
    '"baseline_price_estimate": a plausible round-trip economy fare in EUR '
    'for this route as a number, "clarification_needed": "a question, only '
    'if the request is unclear"}\n'
    "Pick 1 to 6 destination airports. For a loose date description (a "
    "month, \"a couple of weeks in December\"), pick a sensible ideal_date "
    "roughly in the middle of it and a return date matching the trip "
    "length implied, with ranges wide enough to cover the described "
    "period. Default range fields to 3 when not implied by the request."
)

_pending_config: dict = {}
_pending_trip: dict = {}


def _relay_turn(followup_prompt: str, full_prompt: str, task_prompt: str, session_id) -> tuple:
    """One turn of a relay conversation.

    On the first turn (no session_id) or if resuming session_id fails,
    sends full_prompt with the persona + task_prompt as system_prompt. On
    a successful resume, sends only followup_prompt — the relay's session
    already has everything else.
    """
    if session_id:
        try:
            response = relay_client.query(followup_prompt, session_id=session_id)
            return relay_client.extract_json(response["result"]), response["session_id"]
        except requests.RequestException:
            logger.warning("Relay session %s failed to resume — starting fresh.", session_id)

    system_prompt = f"{relay_client.PERSONA_PROMPT}\n\n{task_prompt}"
    response = relay_client.query(full_prompt, system_prompt=system_prompt)
    return relay_client.extract_json(response["result"]), response["session_id"]


def _load_config():
    with open(CONFIG_PATH) as f:
        return _yaml.load(f)


def _save_config(data) -> None:
    with open(CONFIG_PATH, "w") as f:
        _yaml.dump(data, f)


def _format_config(data) -> str:
    return "\n".join(f"*{key}*: `{value}`" for key, value in data.items())


def _coerce(value: str):
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            continue
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value


def _apply_changes(config, parsed: dict) -> list[str]:
    """Mutate config in place per Claude's parsed instructions. Returns a change log."""
    changes = []

    for item in parsed.get("sets", []):
        key = item.get("key", "")
        path = key.split(".")
        target = config
        for part in path[:-1]:
            if not isinstance(target, dict) or part not in target:
                target = None
                break
            target = target[part]

        leaf = path[-1]
        if not isinstance(target, dict) or leaf not in target:
            changes.append(f"Unknown key: {key}")
            continue

        target[leaf] = _coerce(item.get("value", ""))
        changes.append(f"Set {key} to {target[leaf]}")

    origins = config.setdefault("origins", [])
    for code in parsed.get("add_origins", []):
        code = code.upper()
        if code in origins:
            changes.append(f"{code} is already in origins.")
        else:
            origins.append(code)
            changes.append(f"Added {code} to origins.")

    for code in parsed.get("remove_origins", []):
        code = code.upper()
        if code in origins:
            origins.remove(code)
            changes.append(f"Removed {code} from origins.")
        else:
            changes.append(f"{code} is not in origins.")

    return changes


def _parse_config_request(text: str, config: dict, session_id=None) -> tuple:
    full_prompt = f"Current config (Python dict): {config!r}\n\nUser request: {text}"
    return _relay_turn(text, full_prompt, _CONFIG_TASK_PROMPT, session_id)


def _parse_trip_request(text: str, session_id=None) -> tuple:
    full_prompt = f"User request: {text}"
    return _relay_turn(text, full_prompt, _NEW_TRIP_TASK_PROMPT, session_id)


def _format_proposal(proposal: dict, config: dict) -> str:
    destinations = ", ".join(proposal["destinations"])
    origins = proposal.get("origins")
    if origins:
        airports_line = f"Departure airports: {', '.join(origins)}"
    else:
        airports_line = (
            f"Departure airports: {', '.join(config.get('origins', []))} "
            "(from shared settings)"
        )
    lines = [
        f"Destinations: {destinations}",
        f"Dates: {proposal['ideal_date']} to {proposal['ideal_return_date']} "
        f"(-{proposal['departure_range_before']}/+{proposal['departure_range_after']}d "
        f"departure, -{proposal['return_range_before']}/+{proposal['return_range_after']}d return)",
        airports_line,
    ]
    if proposal.get("baseline_price_estimate"):
        lines.append(f"Est. fare: ~€{proposal['baseline_price_estimate']:.0f}")
    lines.append("Reply 'yes' to start tracking, or describe what to change.")
    return "\n".join(lines)


def _handle_get() -> str:
    return _format_config(_load_config())


def _handle_trips_list() -> str:
    active = trips.get_active_trips()
    if not active:
        return "No active trips."

    lines = []
    for trip in active:
        cheapest = deals.cheapest_price(trip["id"])
        price_note = f"€{cheapest:.0f}" if cheapest is not None else "no data yet"
        lines.append(
            f"#{trip['id']} {trip['description']} — "
            f"{trip['ideal_date']} to {trip['ideal_return_date']} — "
            f"cheapest so far: {price_note}"
        )
    return "\n".join(lines)


def _handle_cancel_trip(args: list) -> str:
    if not args or not args[0].isdigit():
        return "Usage: /cancel-trip <id>"

    trip_id = int(args[0])
    if trips.cancel_trip(trip_id):
        return f"Trip #{trip_id} cancelled."
    return f"No active trip with id {trip_id}."


def _handle_config_reply(text: str, session_id) -> tuple:
    """Returns (reply, still_pending, session_id)."""
    config = _load_config()

    try:
        parsed, session_id = _parse_config_request(text, config, session_id=session_id)
    except Exception as exc:
        logger.error("Claude config parse failed: %s", exc)
        return f"Sorry, I couldn't process that: {exc}", True, session_id

    clarification = parsed.get("clarification_needed")
    if clarification:
        return clarification, True, session_id

    changes = _apply_changes(config, parsed)
    if not changes:
        return "I didn't find any changes to make there. Try rephrasing, or /cancel.", True, session_id

    _save_config(config)
    return "\n".join(changes), False, session_id


def _handle_new_trip_reply(chat_id, text: str) -> str:
    state = _pending_trip[chat_id]

    if state["proposal"] and text.strip().lower() == "yes":
        proposal = state["proposal"]
        trip_id = trips.create_trip(
            description=proposal["description"],
            destinations=proposal["destinations"],
            ideal_date=proposal["ideal_date"],
            ideal_return_date=proposal["ideal_return_date"],
            departure_range_before=proposal.get("departure_range_before", 3),
            departure_range_after=proposal.get("departure_range_after", 3),
            return_range_before=proposal.get("return_range_before", 3),
            return_range_after=proposal.get("return_range_after", 3),
            origins=proposal.get("origins"),
            baseline_price_estimate=proposal.get("baseline_price_estimate"),
        )
        _pending_trip.pop(chat_id, None)
        return f"Trip #{trip_id} ({proposal['description']}) is now being tracked."

    try:
        parsed, session_id = _parse_trip_request(text, session_id=state["session_id"])
    except Exception as exc:
        logger.error("Claude trip parse failed: %s", exc)
        return f"Sorry, I couldn't process that: {exc}"

    state["session_id"] = session_id

    clarification = parsed.get("clarification_needed")
    if clarification:
        return clarification

    required = ("description", "destinations", "ideal_date", "ideal_return_date")
    if not all(parsed.get(field) for field in required):
        return "I couldn't work out a full trip from that. Try rephrasing, or /cancel."

    for field in ("departure_range_before", "departure_range_after", "return_range_before", "return_range_after"):
        parsed.setdefault(field, 3)

    state["proposal"] = parsed
    return _format_proposal(parsed, _load_config())


def _clear_pending(chat_id) -> None:
    _pending_config.pop(chat_id, None)
    _pending_trip.pop(chat_id, None)


def _dispatch(chat_id, text: str) -> str:
    stripped = text.strip()
    command = stripped.split()[0].lower() if stripped else ""

    if command == "/cancel":
        _clear_pending(chat_id)
        return "Cancelled."

    if command in ("/start", "/help"):
        _clear_pending(chat_id)
        return HELP_TEXT

    if command == "/get":
        _clear_pending(chat_id)
        return _handle_get()

    if command == "/trips":
        _clear_pending(chat_id)
        return _handle_trips_list()

    if command == "/cancel-trip":
        _clear_pending(chat_id)
        return _handle_cancel_trip(stripped.split()[1:])

    if command == "/set-config":
        _clear_pending(chat_id)
        _pending_config[chat_id] = None
        return "What would you like to change? Describe it in plain language."

    if command == "/new-trip":
        _clear_pending(chat_id)
        _pending_trip[chat_id] = {"session_id": None, "proposal": None}
        return (
            "Where and when do you want to go? Describe it in plain "
            "language — a place, a kind of destination, specific dates, "
            "or a loose period."
        )

    if chat_id in _pending_config:
        reply, still_pending, session_id = _handle_config_reply(stripped, _pending_config[chat_id])
        if still_pending:
            _pending_config[chat_id] = session_id
        else:
            _pending_config.pop(chat_id, None)
        return reply

    if chat_id in _pending_trip:
        return _handle_new_trip_reply(chat_id, stripped)

    if command.startswith("/"):
        return f"Unknown command: {command}\n\n{HELP_TEXT}"

    return HELP_TEXT


def _get_updates(offset=None):
    url = TELEGRAM_API.format(token=_token(), method="getUpdates")
    params = {"timeout": 30}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(url, params=params, timeout=40)
    resp.raise_for_status()
    return resp.json()["result"]


def _poll_loop() -> None:
    chat_id = _chat_id()
    offset = None

    logger.info("Telegram command listener started.")
    while True:
        try:
            updates = _get_updates(offset)
        except requests.RequestException as exc:
            logger.error("Telegram getUpdates failed: %s", exc)
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message", {})
            msg_chat_id = message.get("chat", {}).get("id")
            if str(msg_chat_id) != str(chat_id):
                continue

            text = message.get("text", "")
            if not text:
                continue

            try:
                reply = _dispatch(msg_chat_id, text)
            except Exception as exc:
                logger.error("Command %r failed: %s", text, exc)
                reply = f"Error: {exc}"

            send_message(reply)


def start() -> None:
    """Start the Telegram command listener in a background thread, if configured."""
    if not is_configured():
        logger.warning("Telegram not configured — command listener disabled.")
        return

    threading.Thread(target=_poll_loop, daemon=True).start()
