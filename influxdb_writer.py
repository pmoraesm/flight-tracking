"""Write flight price results to InfluxDB v1."""

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_client = None


def _get_client(config: dict):
    global _client
    if _client is not None:
        return _client

    cfg = config.get("influxdb")
    if not cfg:
        return None

    try:
        from influxdb import InfluxDBClient
        _client = InfluxDBClient(
            host=cfg.get("host", "localhost"),
            port=cfg.get("port", 8086),
            username=cfg.get("username"),
            password=cfg.get("password"),
            database=cfg.get("database"),
        )
        logger.info("InfluxDB connected: %s:%s/%s", cfg["host"], cfg.get("port", 8086), cfg["database"])
    except Exception as exc:
        logger.error("InfluxDB connection failed: %s", exc)

    return _client


def write_results(results: list[dict], config: dict) -> None:
    client = _get_client(config)
    if not client or not results:
        return

    now = datetime.now(timezone.utc).isoformat()

    points = [
        {
            "measurement": "flight_price",
            "tags": {
                "origin": r["origin"],
                "destination": r["destination"],
                "airline": r["airline"],
                "depart_date": r["depart_date"],
                "return_date": r.get("return_date", ""),
                "stops": str(r["stops"]),
            },
            "time": now,
            "fields": {
                "price_value": float(r["price_value"]),
                "price": r["price"],
                "duration": r["duration"],
                "is_best": int(r.get("is_best", False)),
                "url": r.get("url", ""),
                "depart_date_str": r["depart_date"],
                "return_date_str": r.get("return_date", ""),
            },
        }
        for r in results
    ]

    try:
        client.write_points(points)
        logger.info("InfluxDB: wrote %d points", len(points))
    except Exception as exc:
        logger.error("InfluxDB write failed: %s", exc)
