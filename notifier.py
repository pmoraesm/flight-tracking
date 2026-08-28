"""Telegram notifications for flight price alerts."""

import logging
import os
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _token():
    return os.getenv("TELEGRAM_BOT_TOKEN")


def _chat_id():
    return os.getenv("TELEGRAM_CHAT_ID")


def is_configured() -> bool:
    return bool(_token() and _chat_id())


def send_message(text: str) -> bool:
    """Send a plain or Markdown message to the configured Telegram chat."""
    token = _token()
    chat_id = _chat_id()

    if not token or not chat_id:
        logger.warning("Telegram not configured — skipping notification.")
        return False

    url = TELEGRAM_API.format(token=token)
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.error("Telegram send failed: %s", exc)
        return False


def notify_alerts(results: list[dict], config: dict) -> None:
    """Send a Telegram message for each result below the price threshold."""
    threshold = config.get("price_alert_threshold", 0)
    if not threshold:
        return

    alerts = [r for r in results if r["price_value"] <= threshold]
    if not alerts:
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"✈️ *Flight Price Alert* — {now}\n"]

    for a in sorted(alerts, key=lambda r: r["price_value"]):
        stops = "Direct" if a["stops"] == 0 else f"{a['stops']} stop(s)" if a["stops"] != "Unknown" else "? stops"
        link = f"\n  [Search on Google Flights]({a['url']})" if a.get("url") else ""
        lines.append(
            f"*{a['price']}* — {a['origin']} → {a['destination']}\n"
            f"  Depart: {a['depart_date']}  |  Return: {a['return_date']}\n"
            f"  {a['airline']}  |  {a['duration']}  |  {stops}{link}\n"
        )

    send_message("\n".join(lines))


def notify_summary(results: list[dict], config: dict) -> None:
    """Send a brief summary of the cheapest find per origin."""
    if not results:
        return

    # Best (cheapest) result per origin
    best_by_origin: dict[str, dict] = {}
    for r in results:
        origin = r["origin"]
        if origin not in best_by_origin or r["price_value"] < best_by_origin[origin]["price_value"]:
            best_by_origin[origin] = r

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"🔍 *Flight Check* — {now}\n"]

    for origin, r in sorted(best_by_origin.items(), key=lambda x: x[1]["price_value"]):
        stops = "Direct" if r["stops"] == 0 else f"{r['stops']} stop(s)"
        link = f" [↗]({r['url']})" if r.get("url") else ""
        lines.append(
            f"*{origin}*: {r['price']} — {r['depart_date']} / back {r['return_date']} "
            f"({r['airline']}, {stops}){link}"
        )

    send_message("\n".join(lines))
