import sqlite3

import storage


def test_write_results_tags_rows_with_trip_id(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(storage, "_initialized", False)

    results = [{
        "origin": "AMS", "destination": "GRU", "depart_date": "2026-07-11",
        "return_date": "2026-08-08", "airline": "KLM", "departure": "10:00",
        "arrival": "20:00", "duration": "11h", "stops": 0, "price": "€1200",
        "price_value": 1200.0, "is_best": True,
    }]

    storage.write_results(results, trip_id=42)

    conn = sqlite3.connect(storage.DB_PATH)
    row = conn.execute("SELECT origin, price_value, trip_id FROM prices").fetchone()
    conn.close()

    assert row == ("AMS", 1200.0, 42)


def test_write_results_does_nothing_for_empty_list(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(storage, "_initialized", False)

    storage.write_results([], trip_id=1)

    assert not (tmp_path / "prices.db").exists()
