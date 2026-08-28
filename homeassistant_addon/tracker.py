"""Core flight search logic using fast-flights."""

import re
import time
import random
import logging
from datetime import date, timedelta

import fast_flights.core as _ff_core
from fast_flights import FlightData, Passengers, TFSData, get_flights
from fast_flights.primp import Client
from selectolax.lexbor import LexborHTMLParser

logger = logging.getLogger(__name__)

_CONSENT_HOST = "consent.google.com"
_CONSENT_SAVE = "https://consent.google.com/save"
_FLIGHTS_URL = "https://www.google.com/travel/flights"

# Stores the raw HTML of the last fetch so we can extract fields not parsed by fast-flights
_last_response_html: str = ""


def _accept_consent(client: Client, consent_res) -> object:
    """POST the 'Accept all' form on the Google consent page and return the final response."""
    parser = LexborHTMLParser(consent_res.text)
    accept_form = None
    for form in parser.css("form"):
        inputs = {i.attributes.get("name"): i.attributes.get("value", "") for i in form.css("input")}
        if inputs.get("set_sc") == "true":
            accept_form = inputs
            break

    if not accept_form:
        raise RuntimeError("Could not find Accept all form on consent page")

    res = client.post(_CONSENT_SAVE, data=accept_form)
    return res


def _patch_fetch_with_consent() -> None:
    """Monkey-patch fast-flights to auto-accept Google GDPR consent and capture raw HTML."""
    def _fetch_with_consent(params: dict):
        global _last_response_html
        client = Client(impersonate="chrome_131", verify=False)
        res = client.get(_FLIGHTS_URL, params=params)
        assert res.status_code == 200, f"{res.status_code} Result: {res.text_markdown}"

        if _CONSENT_HOST in str(res.url):
            logger.debug("Google consent page detected — auto-accepting...")
            _accept_consent(client, res)
            res = client.get(_FLIGHTS_URL, params=params)
            assert res.status_code == 200, f"{res.status_code} Result: {res.text_markdown}"

        _last_response_html = res.text
        return res

    _ff_core.fetch = _fetch_with_consent


_patch_fetch_with_consent()


def _extract_departure_airports(html: str) -> list[str]:
    """Extract departure airport codes per flight from the raw Google Flights HTML.

    Google Flights renders each flight's departure airport in:
      div.G2WY5c.sSHqwe.ogfYpf.tPgKwe > div
    in the same order as the flight list items.

    Mirrors fast-flights' iteration logic: first group takes all items,
    subsequent groups skip the last item. Toggle/dominated items (ssk contains
    "toggle") are skipped as they are UI controls, not flights.
    """
    parser = LexborHTMLParser(html)
    airports = []
    for i, fl in enumerate(parser.css('div[jsname="IWWDBc"], div[jsname="YdtKid"]')):
        items = fl.css("ul.Rk10dc li")
        if i > 0:
            items = items[:-1]
        for item in items:
            ssk = item.attributes.get("ssk", "")
            if "toggle" in ssk:
                continue
            node = item.css_first("div.G2WY5c.sSHqwe.ogfYpf.tPgKwe div")
            airports.append(node.text(strip=True) if node else "")
    return airports


def _date_range(ideal: str, before: int, after: int) -> list[str]:
    """Return list of date strings from ideal - before to ideal + after."""
    base = date.fromisoformat(ideal)
    return [
        (base + timedelta(days=offset)).isoformat()
        for offset in range(-before, after + 1)
    ]


def _parse_duration_hours(duration_str: str) -> float:
    """Parse a duration string like '14h 30m' or '9h 5m' into total hours."""
    hours = minutes = 0
    h_match = re.search(r"(\d+)\s*h", duration_str)
    m_match = re.search(r"(\d+)\s*m", duration_str)
    if h_match:
        hours = int(h_match.group(1))
    if m_match:
        minutes = int(m_match.group(1))
    return hours + minutes / 60


def _parse_price(price_str: str) -> float:
    """Extract numeric value from a price string like '$1,234' or '€900'."""
    digits = re.sub(r"[^\d.]", "", price_str)
    try:
        return float(digits)
    except ValueError:
        return float("inf")


def _build_flights_url(origins: str, destination: str, depart_date: str, return_date: str, passengers: Passengers, seat: str) -> str:
    """Build a real Google Flights deep-link using the TFS protobuf encoding."""
    tfs = TFSData.from_interface(
        flight_data=[
            FlightData(date=depart_date, from_airport=origins, to_airport=destination),
            FlightData(date=return_date, from_airport=destination, to_airport=origins),
        ],
        trip="round-trip",
        passengers=passengers,
        seat=seat,
    )
    tfs_b64 = tfs.as_b64().decode()
    return f"https://www.google.com/travel/flights?tfs={tfs_b64}"


def search_flights(config: dict):
    """
    Search all origin × depart_date × return_date combinations.
    Yields lists of result dicts one combo at a time.
    """
    destination = config["destination"]
    origins = config["origins"]
    seat = config.get("seat", "economy")
    results_per_query = config.get("results_per_query", 3)

    pax_cfg = config.get("passengers", {})
    passengers = Passengers(
        adults=pax_cfg.get("adults", 1),
        children=pax_cfg.get("children", 0),
    )

    max_duration_hours = config.get("max_duration_hours", 0)
    depart_dates = _date_range(
        config["ideal_date"],
        config.get("departure_range_before", 3),
        config.get("departure_range_after", 3),
    )
    return_dates = _date_range(
        config["ideal_return_date"],
        config.get("return_range_before", 3),
        config.get("return_range_after", 3),
    )

    for origin in origins:
        for depart_date in depart_dates:
            for return_date in return_dates:
                combo_label = f"{origin} {depart_date} → {destination} / back {return_date}"
                try:
                    result = get_flights(
                        flight_data=[
                            FlightData(
                                date=depart_date,
                                from_airport=origin,
                                to_airport=destination,
                            ),
                            FlightData(
                                date=return_date,
                                from_airport=destination,
                                to_airport=origin,
                            ),
                        ],
                        trip="round-trip",
                        seat=seat,
                        passengers=passengers,
                    )

                    # Filter out toggle/placeholder items injected by Google (price=0)
                    flights = [f for f in result.flights if _parse_price(f.price) > 0]

                    if max_duration_hours:
                        flights = [
                            f for f in flights
                            if _parse_duration_hours(f.duration) <= max_duration_hours
                        ]
                    flights = sorted(flights, key=lambda f: _parse_price(f.price))[:results_per_query]

                    url = _build_flights_url(origin, destination, depart_date, return_date, passengers, seat)
                    combo_results = [
                        {
                            "origin": origin,
                            "destination": destination,
                            "depart_date": depart_date,
                            "return_date": return_date,
                            "airline": flight.name,
                            "departure": flight.departure,
                            "arrival": flight.arrival,
                            "duration": flight.duration,
                            "stops": flight.stops,
                            "price": flight.price,
                            "price_value": _parse_price(flight.price),
                            "is_best": flight.is_best,
                            "current_price_level": result.current_price,
                            "url": url,
                        }
                        for flight in flights
                    ]

                    logger.info("OK  %s — %d flights found", combo_label, len(combo_results))
                    yield combo_results

                except Exception as exc:
                    logger.warning("SKIP %s — %s", combo_label, exc)

                # Randomized delay to avoid bot detection
                time.sleep(random.uniform(3, 9))
