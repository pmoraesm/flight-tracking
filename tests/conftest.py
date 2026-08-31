import os

import psycopg
import pytest

from flight_tracker import storage
from flight_tracker import trips

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://flight_tracker:flight_tracker_dev@localhost:5432/flight_tracker",
)


@pytest.fixture(autouse=True)
def truncate_tables(monkeypatch):
    monkeypatch.setattr(storage, "DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr(trips, "DATABASE_URL", TEST_DATABASE_URL)

    conn = psycopg.connect(TEST_DATABASE_URL)
    conn.execute("TRUNCATE TABLE prices, trips RESTART IDENTITY CASCADE")
    conn.commit()
    conn.close()
    yield
