"""Telegram notifications for flight price alerts."""

import logging
import os
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _token():
    return os.getenv("TELEGRAM_BOT_TOKEN")


def _chat_id():
    return os.getenv("TELEGRAM_CHAT_ID")


def is_configured() -> bool:
    return bool(_token() and _chat_id())


def escape_md(value) -> str:
    """Escape characters that Telegram's legacy Markdown parser reads as formatting.

    Apply this to any dynamic value (config keys, trip descriptions, error
    text) before it goes into a message — an unescaped lone `_` or `*` in
    such text leaves an unclosed entity and Telegram rejects the whole
    message with a 400.
    """
    text = str(value)
    for char in ("\\", "_", "*", "`", "["):
        text = text.replace(char, "\\" + char)
    return text


def send_message(text: str) -> bool:
    """Send a plain or Markdown message to the configured Telegram chat."""
    token = _token()
    chat_id = _chat_id()

    if not token or not chat_id:
        logger.warning("Telegram not configured — skipping notification.")
        return False

    url = TELEGRAM_API.format(token=token)
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        try:
            resp.raise_for_status()
        except requests.HTTPError:
            if "parse_mode" not in payload:
                raise
            logger.warning(
                "Telegram send failed with formatted text (%s) — retrying as plain text.",
                resp.status_code,
            )
            payload = {k: v for k, v in payload.items() if k != "parse_mode"}
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.error("Telegram send failed: %s", exc)
        return False


def notify_alerts(good_deals: list) -> None:
    """Send a Telegram message for each result flagged as a good deal."""
    if not good_deals:
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"✈️ *Flight Price Alert* — {now}\n"]

    for a in sorted(good_deals, key=lambda r: r["price_value"]):
        stops = "Direct" if a["stops"] == 0 else f"{a['stops']} stop(s)" if a["stops"] != "Unknown" else "? stops"
        link = f"\n  [Search on Google Flights]({a['url']})" if a.get("url") else ""
        level = a.get("current_price_level")
        level_note = f"\n  Google rates this: {level}" if level else ""
        lines.append(
            f"*#{a.get('trip_id', '?')} {a['trip_description']}*\n"
            f"*{a['price']}* — {a['origin']} → {a['destination']}\n"
            f"  Depart: {a['depart_date']}  |  Return: {a['return_date']}\n"
            f"  {a['airline']}  |  {a['duration']}  |  {stops}{link}{level_note}\n"
        )

    send_message("\n".join(lines))


def notify_summary(trip_summaries: list) -> None:
    """Send one line per active trip: its cheapest fare found this cycle."""
    if not trip_summaries:
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"🔍 *Flight Check* — {now}\n"]

    for t in sorted(trip_summaries, key=lambda r: r["price_value"]):
        stops = "Direct" if t["stops"] == 0 else f"{t['stops']} stop(s)"
        link = f" [↗]({t['url']})" if t.get("url") else ""
        lines.append(
            f"*#{t.get('trip_id', '?')} {t['trip_description']}*: {t['price']} — {t['origin']} → {t['destination']}, "
            f"{t['depart_date']} / back {t['return_date']} ({t['airline']}, {stops}){link}"
        )

    send_message("\n".join(lines))
