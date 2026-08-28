"""Telegram bot commands to view and edit config.yaml from a phone.

/set-config starts a short conversation: the bot asks what to change, the
user replies in plain language, and a Claude Code relay running on the
same VPS turns that reply into concrete config edits.
"""

import json
import logging
import socket
import struct
import threading
import time
from pathlib import Path

import requests
from ruamel.yaml import YAML

from notifier import is_configured, send_message, _token, _chat_id

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.yaml"
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
RELAY_PORT = 8811
FALLBACK_RELAY_HOST = "172.17.0.1"

_yaml = YAML()
_yaml.preserve_quotes = True

HELP_TEXT = (
    "*Flight Tracker commands*\n\n"
    "/set-config — change a setting by describing it in plain language\n"
    "/get — show current settings\n"
    "/cancel — cancel a pending /set-config\n"
    "/help — show this message\n\n"
    "Changes apply on the next scheduled check, except interval_minutes, "
    "which needs a restart."
)

_CONFIG_SYSTEM_PROMPT = (
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
    "guessing. Do not run any commands or edit any files — only answer "
    "with the JSON object."
)

_pending_config: set = set()


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


def _relay_host() -> str:
    """The Docker default-bridge gateway address, read from the routing table."""
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                fields = line.split()
                if fields[1] != "00000000" or not int(fields[3], 16) & 2:
                    continue
                return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    except OSError:
        pass
    return FALLBACK_RELAY_HOST


def _extract_result(lines) -> str:
    """Pull the final ResultMessage's result text out of the relay's NDJSON stream."""
    result = ""
    for line in lines:
        if not line:
            continue
        event = json.loads(line)
        if event.get("type") == "ResultMessage":
            result = event.get("result", "")
    return result


def _extract_json(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}


def _relay_query(prompt: str, system_prompt=None) -> str:
    url = f"http://{_relay_host()}:{RELAY_PORT}/v1/query"
    payload = {"prompt": prompt}
    if system_prompt:
        payload["system_prompt"] = system_prompt

    with requests.post(url, json=payload, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        return _extract_result(resp.iter_lines(decode_unicode=True))


def _parse_config_request(text: str, config: dict) -> dict:
    prompt = f"Current config (Python dict): {config!r}\n\nUser request: {text}"
    result_text = _relay_query(prompt, system_prompt=_CONFIG_SYSTEM_PROMPT)
    return _extract_json(result_text)


def _handle_get() -> str:
    return _format_config(_load_config())


def _handle_config_reply(text: str) -> tuple[str, bool]:
    """Returns (reply, still_pending)."""
    config = _load_config()

    try:
        parsed = _parse_config_request(text, config)
    except Exception as exc:
        logger.error("Claude config parse failed: %s", exc)
        return f"Sorry, I couldn't process that: {exc}", True

    clarification = parsed.get("clarification_needed")
    if clarification:
        return clarification, True

    changes = _apply_changes(config, parsed)
    if not changes:
        return "I didn't find any changes to make there. Try rephrasing, or /cancel.", True

    _save_config(config)
    return "\n".join(changes), False


def _dispatch(chat_id, text: str) -> str:
    stripped = text.strip()
    command = stripped.split()[0].lower() if stripped else ""

    if command == "/cancel":
        _pending_config.discard(chat_id)
        return "Cancelled."

    if command in ("/start", "/help"):
        _pending_config.discard(chat_id)
        return HELP_TEXT

    if command == "/get":
        _pending_config.discard(chat_id)
        return _handle_get()

    if command == "/set-config":
        _pending_config.add(chat_id)
        return "What would you like to change? Describe it in plain language."

    if chat_id in _pending_config:
        reply, still_pending = _handle_config_reply(stripped)
        if not still_pending:
            _pending_config.discard(chat_id)
        return reply

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
