"""Convert Home Assistant /data/options.json into flight tracker config files."""

import json
import yaml
from pathlib import Path

OPTIONS_PATH = Path("/data/options.json")
CONFIG_PATH = Path("/app/config.yaml")
ENV_PATH = Path("/app/.env")


def main() -> None:
    with open(OPTIONS_PATH) as f:
        opts = json.load(f)

    config = {
        "destination": opts["destination"],
        "origins": opts["origins"],
        "ideal_date": opts["ideal_date"],
        "ideal_return_date": opts["ideal_return_date"],
        "departure_range_before": opts["departure_range_before"],
        "departure_range_after": opts["departure_range_after"],
        "return_range_before": opts["return_range_before"],
        "return_range_after": opts["return_range_after"],
        "seat": opts["seat"],
        "passengers": {
            "adults": opts["adults"],
            "children": opts["children"],
        },
        "price_alert_threshold": opts["price_alert_threshold"],
        "max_duration_hours": opts["max_duration_hours"],
        "results_per_query": opts["results_per_query"],
        "interval_minutes": opts["interval_minutes"],
        "influxdb": {
            "host": opts.get("influxdb_host", "a0d7b954-influxdb"),
            "port": opts.get("influxdb_port", 8086),
            "database": opts.get("influxdb_database", "flight_tracker"),
            "username": opts.get("influxdb_username", ""),
            "password": opts.get("influxdb_password", ""),
        },
    }

    with open(CONFIG_PATH, "w") as f:
        yaml.dump(config, f, default_flow_style=False)

    with open(ENV_PATH, "w") as f:
        f.write(f"TELEGRAM_BOT_TOKEN={opts.get('telegram_bot_token', '')}\n")
        f.write(f"TELEGRAM_CHAT_ID={opts.get('telegram_chat_id', '')}\n")

    print("Config generated from HA options.")


if __name__ == "__main__":
    main()
