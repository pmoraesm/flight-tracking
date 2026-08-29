"""Telegram bot commands: slash commands for direct actions, and one
relay-backed router for everything else.

/new-trip and /set-config start a short conversation by asking a
question; /cancel-trip, /trips, /get, /help act immediately. Every
other message — including replies inside an open conversation — goes
through _route(), which asks the Claude Code relay running on the same
VPS to pick one action (propose or revise a trip, cancel a trip, list
trips, edit or show the config, answer a question, or say it's
unclear) and runs it. The relay's shared persona is sent once per
chat (relay_client.PERSONA_PROMPT); every following turn resumes the
same session instead of resending it.
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
from notifier import is_configured, send_message, escape_md, _token, _chat_id

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
    "You can also just describe what you want in plain language, any "
    "time — start a trip, ask about one, cancel one, or change a "
    "setting.\n\n"
    "Changes apply on the next scheduled check, except interval\\_minutes, "
    "which needs a restart."
)

_ROUTER_TASK_PROMPT = (
    "You are the router for a flight-price-tracking Telegram bot. Every "
    "message you see is one turn in an ongoing conversation with one "
    "user. Decide what the user wants and respond with nothing but a "
    "single JSON object, shaped exactly like this:\n"
    '{"action": "propose_trip | revise_trip | cancel_trip | list_trips '
    '| set_config | show_config | help | answer | unclear", '
    '"trip": {"description": "a short 3-6 word label for this trip", '
    '"destinations": ["IATA", ...], "origins": ["IATA", ...] (omit if '
    'not mentioned), "ideal_date": "YYYY-MM-DD", "ideal_return_date": '
    '"YYYY-MM-DD", "departure_range_before": int, '
    '"departure_range_after": int, "return_range_before": int, '
    '"return_range_after": int, "baseline_price_estimate": a plausible '
    'round-trip economy fare in EUR for this route as a number}, '
    '"trip_id": int, '
    '"config_edits": {"sets": [{"key": "dotted.path", "value": "string"}], '
    '"add_origins": ["IATA"], "remove_origins": ["IATA"]}, '
    '"reply": "text"}\n'
    'Use "propose_trip" to start a new trip, or when there is no '
    'pending proposal to revise. Use "revise_trip" only to change a '
    "proposal already shown to the user, and carry over every field "
    "from it that the new message doesn't change. Use \"cancel_trip\" "
    "only when the request matches exactly one id in the active trips "
    "list given below; if it's ambiguous or matches none, use "
    '"unclear" instead and ask which trip in "reply". Use "set_config" '
    "for changes to the shared settings given below — dotted paths for "
    "nested keys, e.g. passengers.adults; IATA airport codes for "
    "origins, translating city or airport names yourself. Use "
    '"answer" to respond to a question about the pending proposal, the '
    'active trips, or the shared settings, using the data given below '
    '— put the answer in "reply". Use "unclear" when the request is '
    "ambiguous or names something that doesn't exist, and ask a short "
    'clarifying question in "reply". Omit fields that don\'t apply to '
    "the chosen action. For a loose date description (a month, \"a "
    "couple of weeks in December\"), pick a sensible ideal_date roughly "
    "in the middle of it and a return date matching the trip length "
    "implied, with ranges wide enough to cover the described period. "
    "Default range fields to 3 when not implied by the request."
)

_pending: dict = {}


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
        changes.append(f"Set {escape_md(key)} to {escape_md(target[leaf])}")

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


def _build_router_prompt(text: str, state: dict, config: dict, active: list) -> str:
    trips_summary = "\n".join(f"#{t['id']} {t['description']}" for t in active) or "none"
    draft = state.get("trip_draft")
    return (
        f"Shared config (Python dict): {config!r}\n\n"
        f"Active trips:\n{trips_summary}\n\n"
        f"Pending trip proposal (not yet confirmed): {draft!r}\n\n"
        f"User message: {text}"
    )


def _confirm_trip(chat_id, state: dict) -> str:
    trip = state["trip_draft"]
    trip_id = trips.create_trip(
        description=trip["description"],
        destinations=trip["destinations"],
        ideal_date=trip["ideal_date"],
        ideal_return_date=trip["ideal_return_date"],
        departure_range_before=trip.get("departure_range_before", 3),
        departure_range_after=trip.get("departure_range_after", 3),
        return_range_before=trip.get("return_range_before", 3),
        return_range_after=trip.get("return_range_after", 3),
        origins=trip.get("origins"),
        baseline_price_estimate=trip.get("baseline_price_estimate"),
    )
    _pending.pop(chat_id, None)
    return f"Trip #{trip_id} ({escape_md(trip['description'])}) is now being tracked."


def _execute_action(state: dict, parsed: dict, config: dict) -> str:
    action = parsed.get("action")
    fallback = "Sorry, I didn't understand that — try rephrasing, or /help."

    if action in ("propose_trip", "revise_trip"):
        trip = parsed.get("trip") or {}
        required = ("description", "destinations", "ideal_date", "ideal_return_date")
        if not all(trip.get(field) for field in required):
            return "I couldn't work out a full trip from that. Try rephrasing, or /cancel."
        for field in ("departure_range_before", "departure_range_after", "return_range_before", "return_range_after"):
            trip.setdefault(field, 3)
        state["trip_draft"] = trip
        return _format_proposal(trip, config)

    if action == "cancel_trip" and isinstance(parsed.get("trip_id"), int):
        trip_id = parsed["trip_id"]
        if trips.cancel_trip(trip_id):
            return f"Trip #{trip_id} cancelled."
        return f"No active trip with id {trip_id}."

    if action == "list_trips":
        return _handle_trips_list()

    if action == "set_config":
        changes = _apply_changes(config, parsed.get("config_edits") or {})
        if not changes:
            return "I didn't find any changes to make there. Try rephrasing, or /cancel."
        _save_config(config)
        return "\n".join(changes)

    if action == "show_config":
        return _format_config(config)

    if action == "help":
        return HELP_TEXT

    if action in ("answer", "unclear") and parsed.get("reply"):
        return parsed["reply"]

    return fallback


def _route(chat_id, text: str) -> str:
    state = _pending.setdefault(chat_id, {"session_id": None, "trip_draft": None})

    if state["trip_draft"] and text.strip().lower() == "yes":
        return _confirm_trip(chat_id, state)

    config = _load_config()
    active = trips.get_active_trips()
    prompt = _build_router_prompt(text, state, config, active)

    try:
        parsed, session_id = _relay_turn(prompt, prompt, _ROUTER_TASK_PROMPT, state["session_id"])
    except Exception as exc:
        logger.error("Router relay call failed: %s", exc)
        return f"Sorry, I couldn't process that: {escape_md(exc)}"

    state["session_id"] = session_id
    return _execute_action(state, parsed, config)


def _clear_pending(chat_id) -> None:
    _pending.pop(chat_id, None)


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
        _pending[chat_id] = {"session_id": None, "trip_draft": None}
        return "What would you like to change? Describe it in plain language."

    if command == "/new-trip":
        _clear_pending(chat_id)
        _pending[chat_id] = {"session_id": None, "trip_draft": None}
        return (
            "Where and when do you want to go? Describe it in plain "
            "language — a place, a kind of destination, specific dates, "
            "or a loose period."
        )

    if command.startswith("/"):
        return f"Unknown command: {escape_md(command)}\n\n{HELP_TEXT}"

    return _route(chat_id, stripped)


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
