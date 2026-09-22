# Design: Add Skyscanner as a Second Flight Search Source

## Purpose

Today the tracker gets flight prices only from Google Flights, through the
`fast-flights` library. This design adds Skyscanner as a second source. Both
sources will run for every trip. The user will see results from both, tagged
by source, in the same alerts and history.

## Goals

- Add Skyscanner search alongside Google Flights, not instead of it.
- Keep the two sources independent. A failure in one source must not block
  the other source's results.
- Keep the result shape and the deal-detection logic the same across
  sources, so `deals.py` and `display.py` need no source-specific branches.
- Make each source easy to test on its own.

## Non-goals

- Per-trip source selection. All trips search all sources.
- A combined cross-source result limit. Each source keeps its own top
  `results_per_query` cheapest flights per combo.
- A generic plugin system for arbitrary future sources. Two sources is
  enough to justify a shared interface, not a plugin loader.

## Skyscanner access

The tracker will use the `irrisolto/skyscanner` Python library. This library
queries the reverse-engineered Skyscanner Android app API. It needs no API
key. As of September 2025 it is active and not archived (48 stars).

Its search call takes an origin airport, a destination airport, a depart
date, an optional return date, a cabin class, an adult count, and a list of
child ages. This matches the current Google Flights parameters closely. A
full itinerary (price, airline, stops, duration, times, link) may need a
follow-up call per itinerary through `get_itinerary_details()`. The
implementation plan must confirm this against the library's real behavior,
since the library's own documentation does not give full field detail.

## Module layout

The current `tracker.py` holds all Google Flights logic: HTML parsing,
consent-form handling, price parsing, and the trip search loop. This design
splits it into a `flight_tracker/sources/` package:

```
flight_tracker/sources/
  __init__.py        # SOURCES list; used by tracker.py
  base.py             # shared helpers: date range, price parsing, duration parsing
  google_flights.py   # current tracker.py logic, moved here unchanged
  skyscanner.py        # new: wraps the irrisolto/skyscanner library
```

`tracker.py` stays as the orchestrator. It keeps the loop over origin,
destination, depart date, and return date. For each combo, it calls every
source in `sources.SOURCES` in turn.

## Source interface

Each source module exposes one function with this signature:

```python
def search(
    origin: str,
    destination: str,
    depart_date: str,
    return_date: str,
    passengers: dict,
    seat: str,
    max_duration_hours: int,
    results_per_query: int,
) -> list[dict]:
    ...
```

Each function returns a list of result dicts in the shared shape already
used today: `airline`, `departure`, `arrival`, `duration`, `stops`, `price`,
`price_value`, `is_best`, `current_price_level`, `url`. Each dict also
carries a new `source` field, set to `"google_flights"` or `"skyscanner"`.

Each function keeps its own price filtering, duration filtering, sorting,
and trimming to `results_per_query`. This is the same work
`tracker.search_flights()` does today, moved into `google_flights.search()`
unchanged, and mirrored in `skyscanner.search()`.

Skyscanner's library has no direct equivalent of Google Flights' "is best
flight" flag. `skyscanner.search()` sets `is_best` to `False` on every
result.

## Orchestration and error handling

`tracker.search_flights()` keeps its current per-combo loop. Inside each
combo, it wraps each source call in its own try/except block, not one block
around the whole combo. A Skyscanner error for a combo logs a warning and
skips only Skyscanner's results for that combo; Google Flights' results for
the same combo still get returned. The reverse also holds.

The results from both sources for a combo get joined into one list before
being yielded, in the same shape `tracker.search_flights()` yields today.
This keeps `main.py`'s loop over `tracker.search_flights(merged)` unchanged.

The random sleep between combos (`time.sleep(random.uniform(3, 9))`) stays
once per combo, after both sources have been tried, not once per source.

## Storage change

`schema.sql`'s `prices` table gets a new column:

```sql
source TEXT NOT NULL DEFAULT 'google_flights'
```

The default backfills existing rows as Google Flights results, with no
separate backfill script needed. `storage.write_results()` reads
`r.get("source", "google_flights")` per result row and stores it in the new
column.

## Deals and display

`deals.is_good_deal()` compares by `price_value` only. It needs no change,
since both sources produce the same shape.

`display.py` gets the source name added to its per-result output line, so
the user can tell where a price came from. No other display logic changes.

## Testing

- `tests/sources/test_google_flights.py`: move the relevant cases from
  today's `tests/test_tracker.py`, unchanged in behavior.
- `tests/sources/test_skyscanner.py`: new tests that stub the
  `irrisolto/skyscanner` library calls and check price parsing, filtering,
  and the `source` tag.
- `tests/test_tracker.py`: keep, but narrow it to orchestration only —
  check that `search_flights()` merges two fake sources' results into one
  combo list, that a source's exception does not drop the other source's
  results, and that both results carry the correct `source` value.

## Open questions for the implementation plan

- Confirm whether `irrisolto/skyscanner` needs a second call
  (`get_itinerary_details()`) per itinerary to get price, airline, stops,
  and duration, and account for the added request cost if so.
- Confirm the library's rate-limit or retry behavior under the tracker's
  polling interval.
