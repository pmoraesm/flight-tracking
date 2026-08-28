from datetime import date, timedelta

import pytest

import storage
import trips


@pytest.fixture(autouse=True)
def scratch_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_prices.db"
    monkeypatch.setattr(storage, "DB_PATH", db_path)
    monkeypatch.setattr(trips, "DB_PATH", db_path)
    monkeypatch.setattr(trips, "_initialized", False)
    yield db_path


def _create(**overrides):
    defaults = dict(
        description="Trip", destinations=["BKK"],
        ideal_date="2026-12-05", ideal_return_date="2026-12-19",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
    )
    defaults.update(overrides)
    return trips.create_trip(**defaults)


def test_create_and_get_trip_round_trips_fields():
    trip_id = _create(description="Beach trip", destinations=["BKK", "HKT"],
                       passengers={"adults": 2})

    trip = trips.get_trip(trip_id)

    assert trip["description"] == "Beach trip"
    assert trip["destinations"] == ["BKK", "HKT"]
    assert trip["origins"] is None
    assert trip["passengers"] == {"adults": 2}
    assert trip["status"] == "active"


def test_get_trip_returns_none_for_unknown_id():
    assert trips.get_trip(999) is None


def test_get_active_trips_excludes_cancelled():
    active_id = _create(description="Active")
    cancelled_id = _create(description="Cancelled")
    trips.cancel_trip(cancelled_id)

    active = trips.get_active_trips()

    assert [t["id"] for t in active] == [active_id]


def test_get_active_trips_expires_trips_past_their_return_window():
    past_return = (date.today() - timedelta(days=10)).isoformat()
    trip_id = _create(ideal_date="2020-01-01", ideal_return_date=past_return)

    active = trips.get_active_trips()

    assert active == []
    assert trips.get_trip(trip_id)["status"] == "expired"


def test_cancel_trip_returns_false_for_unknown_id():
    assert trips.cancel_trip(999) is False


def test_merge_with_defaults_uses_trip_value_when_present():
    trip = {
        "id": 1, "description": "d", "destinations": ["BKK"],
        "origins": ["LHR"], "ideal_date": "2026-12-05",
        "ideal_return_date": "2026-12-19",
        "departure_range_before": 1, "departure_range_after": 1,
        "return_range_before": 1, "return_range_after": 1,
        "seat": "business", "passengers": {"adults": 1},
        "max_duration_hours": 10, "results_per_query": 5,
    }
    config = {"origins": ["AMS"], "seat": "economy", "passengers": {"adults": 2},
              "max_duration_hours": 16, "results_per_query": 3}

    merged = trips.merge_with_defaults(trip, config)

    assert merged["origins"] == ["LHR"]
    assert merged["seat"] == "business"
    assert merged["max_duration_hours"] == 10


def test_merge_with_defaults_falls_back_to_config():
    trip = {
        "id": 1, "description": "d", "destinations": ["BKK"],
        "origins": None, "ideal_date": "2026-12-05",
        "ideal_return_date": "2026-12-19",
        "departure_range_before": 1, "departure_range_after": 1,
        "return_range_before": 1, "return_range_after": 1,
        "seat": None, "passengers": None,
        "max_duration_hours": None, "results_per_query": None,
    }
    config = {"origins": ["AMS"], "seat": "economy", "passengers": {"adults": 2},
              "max_duration_hours": 16, "results_per_query": 3}

    merged = trips.merge_with_defaults(trip, config)

    assert merged["origins"] == ["AMS"]
    assert merged["seat"] == "economy"
    assert merged["max_duration_hours"] == 16


def test_migrate_legacy_config_creates_trip_and_strips_fields():
    config = {
        "destination": "GRU", "origins": ["AMS", "BRU"],
        "ideal_date": "2026-12-11", "ideal_return_date": "2026-12-25",
        "departure_range_before": 1, "departure_range_after": 2,
        "return_range_before": 3, "return_range_after": 7,
        "seat": "economy", "passengers": {"adults": 2},
        "price_alert_threshold": 2500, "max_duration_hours": 16,
        "results_per_query": 3, "interval_minutes": 60,
    }

    stripped = trips.migrate_legacy_config(config)

    assert "destination" not in stripped
    assert "price_alert_threshold" not in stripped
    assert stripped["origins"] == ["AMS", "BRU"]
    assert stripped["interval_minutes"] == 60

    active = trips.get_active_trips()
    assert len(active) == 1
    assert active[0]["destinations"] == ["GRU"]
    assert active[0]["description"] == "Migrated from config.yaml"


def test_migrate_legacy_config_is_noop_when_already_migrated():
    config = {"origins": ["AMS"], "interval_minutes": 60}

    result = trips.migrate_legacy_config(config)

    assert result == config
    assert trips.get_active_trips() == []


def test_migrate_config_file_writes_stripped_yaml_and_returns_dict(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        'destination: "GRU"\n'
        'origins:\n  - "AMS"\n'
        'ideal_date: "2026-12-11"\n'
        'ideal_return_date: "2026-12-25"\n'
        "departure_range_before: 1\n"
        "departure_range_after: 2\n"
        "return_range_before: 3\n"
        "return_range_after: 7\n"
        "price_alert_threshold: 2500\n"
        "interval_minutes: 60\n"
    )

    result = trips.migrate_config_file(config_path)

    assert "destination" not in result
    assert result["interval_minutes"] == 60

    on_disk = config_path.read_text()
    assert "destination" not in on_disk
    assert "price_alert_threshold" not in on_disk
    assert "interval_minutes: 60" in on_disk


def test_migrate_config_file_is_noop_when_already_migrated(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("origins:\n  - AMS\ninterval_minutes: 60\n")

    result = trips.migrate_config_file(config_path)

    assert result["interval_minutes"] == 60
    assert trips.get_active_trips() == []
