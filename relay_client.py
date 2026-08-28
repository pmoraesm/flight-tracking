"""HTTP client for the claude-relay-server running on this VPS.

Resolves the Docker bridge gateway at runtime, sends a prompt (optionally
resuming a session), and extracts the final result and session id from
the relay's newline-delimited JSON response stream.
"""

import json
import socket
import struct

import requests

RELAY_PORT = 8811
FALLBACK_RELAY_HOST = "172.17.0.1"

PERSONA_PROMPT = (
    "You are the reasoning backend for a personal flight-price-tracking "
    "assistant. Your job is narrow: turn a user's plain-language request "
    "into a structured JSON answer, in the exact shape the current request "
    "specifies. Do not run shell commands, browse files, or make any "
    "change outside of what is asked — only answer with JSON. Use IATA "
    "airport codes for airports, translating city, region, or \"kind of "
    "destination\" descriptions yourself from your own knowledge. If a "
    "request is unclear or names something that doesn't exist, ask a "
    "short clarifying question in a clarification_needed field instead of "
    "guessing. Keep responses terse — no chit-chat, no explanation beyond "
    "what's asked."
)


def relay_host() -> str:
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


def extract_result(lines) -> dict:
    """Pull the final ResultMessage's result text and session_id out of an NDJSON stream."""
    result = ""
    session_id = None
    for line in lines:
        if not line:
            continue
        event = json.loads(line)
        if event.get("type") == "ResultMessage":
            result = event.get("result", "")
            session_id = event.get("session_id")
    return {"result": result, "session_id": session_id}


def extract_json(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}


def query(prompt: str, system_prompt=None, session_id=None) -> dict:
    """Send a prompt to the relay. Returns {"result": str, "session_id": str | None}.

    Pass system_prompt only on a conversation's first call; pass the
    session_id from that call's response on every following turn instead.
    """
    url = f"http://{relay_host()}:{RELAY_PORT}/v1/query"
    payload = {"prompt": prompt}
    if system_prompt:
        payload["system_prompt"] = system_prompt
    if session_id:
        payload["session_id"] = session_id

    with requests.post(url, json=payload, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        return extract_result(resp.iter_lines(decode_unicode=True))
