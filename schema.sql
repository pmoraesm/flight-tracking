CREATE TABLE trips (
    id                      INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    description             TEXT NOT NULL,
    destinations            JSONB NOT NULL,
    origins                 JSONB,
    ideal_date              DATE NOT NULL,
    ideal_return_date       DATE NOT NULL,
    departure_range_before  INTEGER NOT NULL,
    departure_range_after   INTEGER NOT NULL,
    return_range_before     INTEGER NOT NULL,
    return_range_after      INTEGER NOT NULL,
    seat                    TEXT,
    passengers              JSONB,
    max_duration_hours      INTEGER,
    results_per_query       INTEGER,
    baseline_price_estimate REAL,
    status                  TEXT NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active', 'cancelled', 'expired')),
    created_at              TIMESTAMPTZ NOT NULL
);

CREATE TABLE prices (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    checked_at  TIMESTAMPTZ NOT NULL,
    origin      TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart_date DATE NOT NULL,
    return_date DATE,
    airline     TEXT,
    departure   TEXT,
    arrival     TEXT,
    duration    TEXT,
    stops       INTEGER,
    price       TEXT,
    price_value REAL,
    is_best     BOOLEAN,
    trip_id     INTEGER REFERENCES trips(id)
);

CREATE INDEX idx_prices_lookup ON prices(origin, depart_date, return_date);
CREATE INDEX idx_prices_trip ON prices(trip_id);
