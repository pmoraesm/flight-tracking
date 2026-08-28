import sqlite3

import storage
import deals


def _seed_prices(trip_id, price_values):
    conn = sqlite3.connect(storage.DB_PATH)
    conn.executescript(storage._SCHEMA)
    for value in price_values:
        conn.execute(
            "INSERT INTO prices (checked_at, origin, destination, depart_date, "
            "price_value, trip_id) VALUES ('2026-01-01', 'AMS', 'GRU', "
            "'2026-07-11', ?, ?)",
            (value, trip_id),
        )
    conn.commit()
    conn.close()


def test_cold_start_no_baseline_never_alerts(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    _seed_prices(1, [])

    assert deals.is_good_deal(1, 500, baseline_price_estimate=None) is False


def test_cold_start_uses_baseline_discount(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    _seed_prices(1, [900, 950])  # only 2 samples, below the percentile minimum

    assert deals.is_good_deal(1, 849, baseline_price_estimate=1000) is True   # 1000*0.85=850
    assert deals.is_good_deal(1, 851, baseline_price_estimate=1000) is False


def test_percentile_path_kicks_in_at_five_samples(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    _seed_prices(1, [100, 200, 300, 400, 500])

    assert deals.is_good_deal(1, 100, baseline_price_estimate=None) is True
    assert deals.is_good_deal(1, 500, baseline_price_estimate=None) is False


def test_percentile_path_ignores_a_generous_baseline(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    _seed_prices(1, [100, 200, 300, 400, 500])

    # A generous baseline must not override the percentile check once
    # there's enough history.
    assert deals.is_good_deal(1, 500, baseline_price_estimate=10000) is False


def test_cheapest_price_returns_none_when_no_history(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    _seed_prices(1, [])

    assert deals.cheapest_price(1) is None


def test_cheapest_price_returns_minimum(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    _seed_prices(1, [500, 200, 800])

    assert deals.cheapest_price(1) == 200
