"""Flight price tracker — entry point."""

import logging
from pathlib import Path

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler

import deals
import storage
import tracker
import trips
import telegram_commands
from display import console
import display
from notifier import is_configured, notify_alerts

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

    try:
        active_trips = trips.get_active_trips()
    except Exception as exc:
        logger.error("Could not load active trips: %s", exc)
        return

    all_results = []
    all_alerts = []

    for trip in active_trips:
        try:
            merged = trips.merge_with_defaults(trip, config)

            for combo in tracker.search_flights(merged):
                if not combo:
                    continue
                storage.write_results(combo, trip_id=trip["id"])

                for result in combo:
                    result["trip_description"] = trip["description"]
                    result["trip_id"] = trip["id"]
                    result["is_good_deal"] = deals.is_good_deal(
                        trip["id"], result["price_value"], trip["baseline_price_estimate"]
                    )
                    if result["is_good_deal"]:
                        all_alerts.append(result)

                all_results.extend(combo)

        except Exception as exc:
            logger.error("Trip #%s (%s) failed: %s", trip["id"], trip["description"], exc)

    display.print_results(all_results)
    display.print_alerts(all_alerts)

    if is_configured():
        notify_alerts(all_alerts)


def main() -> None:
    console.print("[bold]Flight Tracker started.[/bold]")
    console.print(f"Config: [cyan]{CONFIG_PATH}[/cyan]")

    config = trips.migrate_config_file(CONFIG_PATH)
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
