"""Flight price tracker — entry point."""

import logging
import time
from pathlib import Path

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler

from tracker import search_flights
from display import console, print_results, print_alerts
from notifier import is_configured, notify_alerts, notify_summary
from storage import write_results
import telegram_commands

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def run_check() -> None:
    config = load_config()

    console.print("\n[bold cyan]Starting price check…[/bold cyan]")
    results = []

    for combo in search_flights(config):
        if combo:
            results.extend(combo)
            write_results(combo, config)

    print_results(results, config)
    print_alerts(results, config)

    if is_configured():
        notify_alerts(results, config)   # sends only if below threshold
        notify_summary(results, config)  # sends best price per origin


def main() -> None:
    console.print("[bold]Flight Tracker started.[/bold]")
    console.print(f"Config: [cyan]{CONFIG_PATH}[/cyan]")

    config = load_config()
    interval = config.get("interval_minutes", 60)

    telegram_commands.start()

    # Run immediately on startup
    run_check()

    scheduler = BlockingScheduler()
    scheduler.add_job(run_check, "interval", minutes=interval)
    console.print(f"\n[dim]Next check in {interval} minutes. Press Ctrl+C to stop.[/dim]\n")

    try:
        scheduler.start()
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")


if __name__ == "__main__":
    main()
