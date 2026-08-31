"""Good-deal detection based on a trip's own historical prices."""

import statistics

import psycopg

import storage

MIN_HISTORY_FOR_PERCENTILE = 5
PERCENTILE_THRESHOLD = 20
BASELINE_DISCOUNT = 0.85


def _historical_prices(trip_id: int) -> list:
    conn = psycopg.connect(storage.DATABASE_URL)
    rows = conn.execute(
        "SELECT price_value FROM prices WHERE trip_id = %s", (trip_id,)
    ).fetchall()
    conn.close()
    return [row[0] for row in rows]


def is_good_deal(trip_id: int, price_value: float, baseline_price_estimate, passenger_count: int = 1) -> bool:
    history = _historical_prices(trip_id)

    if len(history) < MIN_HISTORY_FOR_PERCENTILE:
        if baseline_price_estimate is None:
            return False
        return price_value <= baseline_price_estimate * passenger_count * BASELINE_DISCOUNT

    percentile_cut = statistics.quantiles(history, n=100)[PERCENTILE_THRESHOLD - 1]
    return price_value <= percentile_cut


def cheapest_price(trip_id: int):
    history = _historical_prices(trip_id)
    return min(history) if history else None
