# Design: Jev Deal Check for Price Alerts

## Problem

The tracker sends Telegram alerts that the user does not trust.

- One message lists many flights, because every "good deal" result of a check goes into it.
- The rule for a good deal is too loose. `deals.is_good_deal()` compares a price with the 20th percentile of the whole trip history. That history mixes all origins, destinations, and dates.
- Nothing stops the same deal from alerting on every hourly check.

## Goals

- Send an alert only for a very good deal.
- Show one flight per trip in an alert.
- Send at most one alert per trip in each cooldown period.
- Use Jev (TypeSafe, System One) to decide whether a flight is a very good deal.

## Non-goals

- Change how flights are searched or stored.
- Add a second search source.
- Fix the cancelled-trip race in `run_check()`. This is a separate bug.
- Compare currencies. Prices are compared as numbers, and the baseline estimate is in EUR, as today.

## Decisions

These decisions come from the brainstorming questions.

| Topic | Decision |
| --- | --- |
| What makes a very good deal | All four: far below the usual route price, lowest price ever seen for the trip, big saving against the estimate, and a good flight. |
| Alert size | One flight per trip. |
| Repeat alerts | Cooldown. Default 24 hours. |
| Who decides | Jev decides. Code computes the facts and combines two answers. |
| Client | HTTP with `requests`. No SDK. No Python upgrade. |

The SDK `typesafe-sdk` needs Python 3.10 or newer. The local `.venv` uses Python 3.9. The Docker image uses Python 3.11. HTTP avoids the difference.

## Flow

`run_check()` does these steps for each active trip.

1. Record `check_started_at` in UTC before the search starts.
2. Search and store prices, as today.
3. If the trip has an alert inside the cooldown, stop here for this trip. No Jev call is made.
4. Pick the candidates: the cheapest `candidates_per_trip` flights of this check.
5. For each candidate, build the state and ask Jev two yes/no questions in one request.
6. A candidate passes when both answers meet the limits in `config.yaml`. The console table marks each passing flight with `is_good_deal`.
7. Pick the cheapest passing candidate. If two have the same price, pick the one with the higher price answer.
8. Add the flight to the alert list. After Telegram accepts the message, record the alert.

## Parts

| File | Change |
| --- | --- |
| `flight_tracker/jev_client.py` | New. One function, `judge(state, questions)`. It sends one request to Jev and returns the answers. |
| `flight_tracker/deals.py` | Replace `is_good_deal()` with fact building, the Jev call, and candidate selection. Keep `cheapest_price()`. |
| `flight_tracker/storage.py` | Add `last_alert_time(trip_id)` and `record_alerts(alerts)`. |
| `flight_tracker/notifier.py` | `notify_alerts()` returns `True` when Telegram accepted the message, and `False` if not. |
| `main.py` | Run the flow above. |
| `schema.sql` | Add the `alerts` table. |
| `config.yaml` | Add the `deal_check` block. |
| `scripts/replay_deal_check.py` | New. A read-only script to test the check on stored prices. |

The old percentile rule and its constants are deleted. They are not kept as a fallback.

## Facts

Code computes every number. Code then writes sentences. Jev does no arithmetic, because the Jev docs say it is weak with numbers.

**History rules.** Only rows with `checked_at` before `check_started_at` count. Without this rule, a price compares with itself.

- Route history: rows for the same `trip_id`, `origin`, and `destination`, from the last `history_days` days. The reference value is the median of `price_value`.
- Trip low: the minimum `price_value` of all rows for the `trip_id`.
- Estimate: `baseline_price_estimate` multiplied by the passenger count.

**Labels.** Fixed cut points in code set each label. They are not in `config.yaml`.

| Fact | Label | Rule |
| --- | --- | --- |
| Route and estimate | far below usual | 25% or more below the reference |
| Route and estimate | somewhat below usual | 10% to less than 25% below |
| Route and estimate | about the same | within 10% of the reference |
| Route and estimate | above usual | more than 10% above |
| Trip low | new lowest | equal to or below the trip low |
| Trip low | close to the lowest | up to 5% above the trip low |
| Trip low | well above the lowest | more than 5% above |

A fact sentence has the label, the exact percent, and the reference. Example:

`Route: far below usual. 38% below the median of 212 prices seen for AMS to GRU in the last 30 days.`

When the route has fewer than `min_route_history` rows, the route sentence says "Not enough history: only 3 prices seen for AMS to GRU." When `baseline_price_estimate` is empty, the estimate sentence says "No estimate available."

## Jev request

One request for each candidate. `POST https://api.typesafe.ai/v1/systemone` with the header `Authorization: Bearer <TYPESAFE_API_KEY>`. The model is pinned to `deal_check.model`.

State in `facts` mode:

```json
{
  "flight": {
    "route": "AMS to GRU",
    "depart_date": "2026-07-11",
    "return_date": "2026-08-08",
    "airline": "KLM",
    "stops": 1,
    "duration": "14h 30m",
    "departure": "10:05",
    "arrival": "18:40",
    "price": "612 for 2 passengers"
  },
  "price_facts": {
    "route": "far below usual. 38% below the median of 212 prices seen for AMS to GRU in the last 30 days.",
    "trip_low": "new lowest. The lowest of 340 prices recorded for this trip.",
    "estimate": "far below usual. 31% below the estimated normal fare of 890 for 2 passengers."
  }
}
```

In `raw` mode the state has only `flight`. This mode exists to compare with `facts` mode.

Questions (first wording, to tune with the replay script):

```json
{
  "price_is_very_good": {
    "type": "noul",
    "instructions": "Judging by the labels and sentences in `price_facts`, is this price a very good deal, well below what is normal for this trip?",
    "criteria": {
      "true": "At least one fact shows the price far below usual, or a new lowest price for the trip, and no fact shows the price above usual.",
      "false": "The facts show a price near or above usual, or there is too little history to show the price is low."
    }
  },
  "flight_is_acceptable": {
    "type": "noul",
    "instructions": "Judging by `flight`, is this an acceptable trip for a traveller who wants a reasonable journey?",
    "criteria": {
      "true": "Few stops, a sensible total duration for the route, and no very awkward departure or arrival time.",
      "false": "Many stops, a very long duration for the route, or a very awkward departure or arrival time."
    }
  }
}
```

In `raw` mode, the price question says: "Judging by `flight.price` and your general knowledge of fares on this route, is this price a very good deal?"

The client uses a timeout of 15 seconds. It retries up to 2 times on `429` and `5xx` replies, and it respects `Retry-After`. After the last retry, it raises `JevError`.

## Alerts and cooldown

New table:

```sql
CREATE TABLE alerts (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trip_id     INTEGER NOT NULL REFERENCES trips(id),
    sent_at     TIMESTAMPTZ NOT NULL,
    origin      TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart_date DATE NOT NULL,
    return_date DATE,
    airline     TEXT,
    price_value DOUBLE PRECISION NOT NULL
);

CREATE INDEX idx_alerts_trip_sent ON alerts(trip_id, sent_at DESC);
```

A trip is in cooldown when its latest `sent_at` is less than `alert_cooldown_hours` ago. A cheaper price does not end the cooldown. A large price drop can wait up to 24 hours for an alert. This follows the choice made in brainstorming.

## Config

New block in `config.yaml`:

```yaml
deal_check:
  model: jev-1.13.0
  state_mode: facts          # facts or raw
  candidates_per_trip: 5
  min_price_noul: 0.85
  min_flight_noul: 0.70
  alert_cooldown_hours: 24
  history_days: 30
  min_route_history: 5
```

`TYPESAFE_API_KEY` goes in `.env`. Create the key at console.typesafe.ai. `docker-compose.yml` already passes `.env` to the container.

## Failure handling

If a Jev call raises `JevError`, code logs the error and sends no alert for that candidate. Prices are still stored. The next check tries again. A missing or bad key gives silence, not spam.

If `TYPESAFE_API_KEY` is empty, `run_check()` logs one error per check and skips the deal check.

## Replay script

`scripts/replay_deal_check.py` is read-only. It sends no Telegram message and writes nothing to the database.

- Arguments: `--trip-id`, `--limit` (default 50 rows), `--days` (default 14).
- For each stored row, it builds the facts from only the earlier rows.
- It asks Jev in `facts` mode and in `raw` mode.
- It prints one line per row with the flight, the price, both answer pairs, and whether the row would alert.

Use the output to tune `min_price_noul`, `min_flight_noul`, the question wording, and `state_mode` before the check goes live.

## Tests

- `jev_client`: success, `429` with retry, `5xx` with retry, timeout, malformed reply, missing key. All use a mocked HTTP layer.
- Facts: each label, the empty-history sentence, the empty-estimate sentence, and the rule that the current check is not in the history.
- Selection: the cheapest passing candidate wins, the tie rule, no passing candidate, and limits at exact values.
- Cooldown: an alert inside and outside the window. Alerts are recorded only when Telegram accepted the message.
- `run_check()`: one test with a fake Jev. It checks one alert per trip and no alert for a trip in cooldown.
- Remove the old `is_good_deal` tests. Keep the `cheapest_price` tests.

Tests that use the database follow the existing pattern in `tests/test_deals.py`.

## Deployment

1. Run the `CREATE TABLE alerts` and `CREATE INDEX` statements on the production database. The project has no migration tool, and `schema.sql` only runs on a new database.
2. Add `TYPESAFE_API_KEY` to `.env` on the VPS.
3. Run the replay script and tune the limits.
4. Add the `deal_check` block to `config.yaml`, then rebuild the container.

## Risks and open points

- **Jev and numbers.** The Jev docs say the model struggles with numeric precision. Labels reduce this risk. The replay script measures it.
- **Limits are guesses.** The defaults 0.85 and 0.70 are starting points. They are not tested on your data.
- **Silent failure.** A Jev outage gives no alerts and no warning. A daily Telegram warning is possible, but it is not in this design.
- **Data sent out.** Flight details and price facts go to TypeSafe. They contain no personal data.
- **Pick rule.** The cheapest passing flight is the alert. It may not be the best flight of the passing set. Tune this if the replay shows a problem.
- **Cancelled trips.** A check that is already running can still alert for a trip cancelled during the check. This bug is not fixed here.
