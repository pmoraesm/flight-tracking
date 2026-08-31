from datetime import date, timedelta

from flight_tracker import trips


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


def test_get_trip_returns_ideal_dates_as_plain_strings():
    trip_id = _create(ideal_date="2026-12-05", ideal_return_date="2026-12-19")

    trip = trips.get_trip(trip_id)

    assert trip["ideal_date"] == "2026-12-05"
    assert trip["ideal_return_date"] == "2026-12-19"
    assert isinstance(trip["ideal_date"], str)


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


def test_get_active_trips_logs_warning_when_expiring_a_trip(caplog):
    past_return = (date.today() - timedelta(days=10)).isoformat()
    trip_id = _create(ideal_date="2020-01-01", ideal_return_date=past_return)

    with caplog.at_level("WARNING"):
        trips.get_active_trips()

    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any(str(trip_id) in msg and "expired" in msg for msg in warnings)


def test_cancel_trip_returns_false_for_unknown_id():
    assert trips.cancel_trip(999) is False


def test_update_trip_changes_fields_and_returns_true():
    trip_id = _create(description="Original", destinations=["BKK"])

    updated = trips.update_trip(
        trip_id, description="Updated", destinations=["BKK", "HKT"],
        ideal_date="2026-12-05", ideal_return_date="2026-12-19",
        departure_range_before=5, departure_range_after=5,
        return_range_before=1, return_range_after=1,
    )

    assert updated is True
    trip = trips.get_trip(trip_id)
    assert trip["description"] == "Updated"
    assert trip["destinations"] == ["BKK", "HKT"]
    assert trip["departure_range_before"] == 5


def test_update_trip_returns_false_for_unknown_id():
    updated = trips.update_trip(
        999, description="d", destinations=["BKK"],
        ideal_date="2026-12-05", ideal_return_date="2026-12-19",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
    )

    assert updated is False


def test_update_trip_returns_false_for_cancelled_trip():
    trip_id = _create()
    trips.cancel_trip(trip_id)

    updated = trips.update_trip(
        trip_id, description="d", destinations=["BKK"],
        ideal_date="2026-12-05", ideal_return_date="2026-12-19",
        departure_range_before=1, departure_range_after=1,
        return_range_before=1, return_range_after=1,
    )

    assert updated is False


def test_get_inactive_trips_returns_cancelled_and_expired_only():
    active_id = _create(description="Active")
    cancelled_id = _create(description="Cancelled")
    trips.cancel_trip(cancelled_id)
    past_return = (date.today() - timedelta(days=10)).isoformat()
    expired_id = _create(description="Expired", ideal_date="2020-01-01", ideal_return_date=past_return)

    inactive = trips.get_inactive_trips()

    assert active_id not in [t["id"] for t in inactive]
    assert {t["id"] for t in inactive} == {cancelled_id, expired_id}


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
