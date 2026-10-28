"""Live pricing module for flights and hotels in India.

Features:
- Live domestic flight searches via Google Flights RPC (fast-flights).
- Live domestic hotel searches via Google Travel HTML parsing with card-scoped price extraction.
- Local SQLite cache with connection management and configurable TTL (default 6h).
- Validation of IATA airport codes, Indian cities, and travel dates.
- Support for one-way and paired round-trip flights.
- Google Flights protobuf (tfs) and Google Travel deep links.

Notes:
- Results come from scraping Google Flights and Google Travel.
- Hotel prices reflect Google's default occupancy; guest count is validated and echoed in offers,
  not sent in the scraping query.
- Production commercial deployments should use licensed provider APIs (e.g., Amadeus Self-Service,
  Duffel, or official aggregator partner feeds).
"""
import contextlib
import datetime as dt
import hashlib
import json
import logging
import pathlib
import re
import sqlite3
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import primp
from selectolax.lexbor import LexborHTMLParser

try:
    import fast_flights
    import fast_flights.exceptions as _ff_exceptions
except ImportError:
    fast_flights = None
    _ff_exceptions = None

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """Raised when a remote travel provider returns an HTTP error or unexpected status."""
    pass


# Explicit canonical display names for airport codes (single source of truth)
IATA_TO_CANONICAL = {
    "DEL": "Delhi",
    "BOM": "Mumbai",
    "BLR": "Bengaluru",
    "GOI": "Goa (Dabolim)",
    "GOX": "Goa (Mopa)",
    "CCU": "Kolkata",
    "MAA": "Chennai",
    "HYD": "Hyderabad",
    "PNQ": "Pune",
    "AMD": "Ahmedabad",
    "JAI": "Jaipur",
    "UDR": "Udaipur",
    "COK": "Kochi",
    "TRV": "Thiruvananthapuram",
    "LKO": "Lucknow",
    "IXC": "Chandigarh",
    "ATQ": "Amritsar",
    "VNS": "Varanasi",
    "SXR": "Srinagar",
    "IXL": "Leh",
    "DED": "Dehradun",
    "IXB": "Bagdogra",
    "GAU": "Guwahati",
    "IXZ": "Port Blair",
    "BBI": "Bhubaneswar",
    "IDR": "Indore",
    "BHO": "Bhopal",
    "JDH": "Jodhpur",
    "JSA": "Jaisalmer",
    "IXE": "Mangalore",
    "CJB": "Coimbatore",
    "IXM": "Madurai",
    "VTZ": "Visakhapatnam",
    "PAT": "Patna",
    "IXR": "Ranchi",
    "AGR": "Agra",
    "KNU": "Kanpur",
    "TIR": "Tirupati",
    "BDQ": "Vadodara",
    "RPR": "Raipur",
    "NAG": "Nagpur",
    "GWL": "Gwalior",
    "BKB": "Bikaner",
    "AJL": "Aizawl",
    "IMF": "Imphal",
    "DMU": "Dimapur",
    "DIB": "Dibrugarh",
    "TEZ": "Tezpur",
    "JRH": "Jorhat",
    "IXA": "Agartala",
}

# Derived valid Indian airport codes
VALID_INDIAN_AIRPORTS = set(IATA_TO_CANONICAL.keys())

# Mapping Indian cities to primary airport codes
CITY_TO_IATA = {
    "delhi": "DEL", "new delhi": "DEL", "ncr": "DEL",
    "mumbai": "BOM", "bombay": "BOM",
    "bengaluru": "BLR", "bangalore": "BLR",
    "goa": "GOI", "north goa": "GOX", "south goa": "GOI", "dabolim": "GOI", "mopa": "GOX",
    "kolkata": "CCU", "calcutta": "CCU",
    "chennai": "MAA", "madras": "MAA",
    "hyderabad": "HYD",
    "pune": "PNQ",
    "ahmedabad": "AMD",
    "jaipur": "JAI",
    "jaisalmer": "JSA",
    "udaipur": "UDR",
    "kochi": "COK", "cochin": "COK",
    "thiruvananthapuram": "TRV", "trivandrum": "TRV",
    "lucknow": "LKO",
    "chandigarh": "IXC",
    "amritsar": "ATQ",
    "varanasi": "VNS",
    "srinagar": "SXR",
    "leh": "IXL", "ladakh": "IXL",
    "dehradun": "DED", "rishikesh": "DED", "haridwar": "DED",
    "bagdogra": "IXB", "darjeeling": "IXB", "siliguri": "IXB",
    "guwahati": "GAU", "shillong": "GAU",
    "port blair": "IXZ", "andaman": "IXZ", "havelock island": "IXZ",
    "bhubaneswar": "BBI", "puri": "BBI",
    "indore": "IDR",
    "bhopal": "BHO",
    "jodhpur": "JDH",
    "mangalore": "IXE", "mangaluru": "IXE", "udupi": "IXE",
    "coimbatore": "CJB", "ooty": "CJB",
    "madurai": "IXM",
    "visakhapatnam": "VTZ", "vizag": "VTZ",
    "patna": "PAT",
    "ranchi": "IXR",
    "agra": "AGR",
    "kanpur": "KNU",
    "tirupati": "TIR",
    "vadodara": "BDQ",
    "raipur": "RPR",
    "nagpur": "NAG",
    "gwalior": "GWL",
    "bikaner": "BKB",
    "aizawl": "AJL",
    "imphal": "IMF",
    "dimapur": "DMU",
    "dibrugarh": "DIB",
    "tezpur": "TEZ",
    "jorhat": "JRH",
    "agartala": "IXA",
}

# Cache Database setup
CACHE_DB_PATH = pathlib.Path(__file__).resolve().parent.parent / "data" / "cache" / "pricing_cache.db"
_cache_db_initialized = False


def _init_cache_db():
    """Initializes SQLite cache schema lazily wrapped in try/except."""
    try:
        CACHE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(CACHE_DB_PATH)) as conn:
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS price_cache (
                        cache_key TEXT PRIMARY KEY,
                        category TEXT,
                        data_json TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        expires_at TIMESTAMP
                    )
                """)
    except sqlite3.Error as e:
        logger.warning("Could not initialize cache database: %s", e)


def _ensure_cache_db():
    global _cache_db_initialized
    if not _cache_db_initialized:
        _init_cache_db()
        _cache_db_initialized = True


def _get_from_cache(cache_key: str) -> Optional[Dict[str, Any]]:
    _ensure_cache_db()
    try:
        with contextlib.closing(sqlite3.connect(CACHE_DB_PATH)) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT data_json FROM price_cache 
                WHERE cache_key = ? AND expires_at > CURRENT_TIMESTAMP
            """, (cache_key,))
            row = cur.fetchone()
            if row:
                return json.loads(row[0])
    except sqlite3.Error as e:
        logger.warning("Cache read error: %s", e)
    except json.JSONDecodeError as e:
        logger.warning("Cache JSON decode error: %s", e)
    return None


# Constants for pricing, scraping, limits, and caching
MIN_HOTEL_PRICE_INR = 600
MAX_HOTEL_PRICE_INR = 300_000
HOTEL_CANDIDATE_POOL = 20
MAX_HOTEL_RESULTS = 4
MAX_FLIGHT_RESULTS = 4
CACHE_TTL_HOURS = 6
HTTP_TIMEOUT_S = 12


def _save_to_cache(cache_key: str, category: str, data: Dict[str, Any], ttl_hours: int = CACHE_TTL_HOURS):
    _ensure_cache_db()
    try:
        with contextlib.closing(sqlite3.connect(CACHE_DB_PATH)) as conn:
            with conn:
                conn.execute("""
                    INSERT OR REPLACE INTO price_cache (cache_key, category, data_json, expires_at)
                    VALUES (?, ?, ?, datetime(CURRENT_TIMESTAMP, '+' || ? || ' hours'))
                """, (cache_key, category, json.dumps(data, ensure_ascii=False), ttl_hours))
    except sqlite3.Error as e:
        logger.warning("Cache write error: %s", e)


def _validate_count(val: Any, name: str) -> tuple[Optional[int], Optional[Dict[str, Any]]]:
    """Validates passenger or guest count strictly between 1 and 9."""
    if not isinstance(val, bool):
        if (isinstance(val, (int, float)) and val == int(val)) or (isinstance(val, str) and val.strip().isdigit()):
            ival = int(val)
            if 1 <= ival <= 9:
                return ival, None
    return None, {"error": "INVALID_ARGUMENTS", "message": f"{name} must be an integer between 1 and 9"}


def get_current_ist_time() -> str:
    """Returns current Indian Standard Time in ISO 8601 format."""
    return dt.datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="minutes")


def resolve_iata(name_or_code: str) -> Optional[str]:
    """Resolve an Indian city name or code to a 3-letter IATA code. Returns None if unknown."""
    if not name_or_code or not isinstance(name_or_code, str):
        return None
    raw = name_or_code.strip()
    lower = raw.lower()
    if lower in CITY_TO_IATA:
        return CITY_TO_IATA[lower]
    upper = raw.upper()
    if upper in VALID_INDIAN_AIRPORTS or upper in IATA_TO_CANONICAL:
        return upper
    return None


def _calc_elapsed_minutes(legs) -> int:
    """Calculates true journey duration from departure of first leg to arrival of last leg."""
    try:
        dep = dt.datetime(*legs[0].departure.date, *legs[0].departure.time)
        arr = dt.datetime(*legs[-1].arrival.date, *legs[-1].arrival.time)
        return int((arr - dep).total_seconds() // 60)
    except (AttributeError, TypeError, ValueError) as e:
        logger.warning("Could not compute elapsed duration from leg timestamps (%s); summing leg durations", e)
        return sum(getattr(leg, "duration", 0) for leg in legs)


def search_live_flights(
    origin: str,
    destination: str,
    date: str,
    passengers: int = 1,
    return_date: Optional[str] = None,
    use_cache: bool = True
) -> Dict[str, Any]:
    """Fetch live Indian domestic flight fares using fast-flights (Google Flights).
    
    Conforms to the travel advisor tool contract in travel/tools.py.
    """
    now_iso = get_current_ist_time()
    today_ist = dt.datetime.now(ZoneInfo("Asia/Kolkata")).date()

    # Validate origin and destination
    orig_code = resolve_iata(origin)
    if not orig_code:
        return {"error": "UNKNOWN_AIRPORT", "message": f"Could not find an Indian airport for origin '{origin}'"}
    dest_code = resolve_iata(destination)
    if not dest_code:
        return {"error": "UNKNOWN_AIRPORT", "message": f"Could not find an Indian airport for destination '{destination}'"}
    if orig_code == dest_code:
        return {"error": "INVALID_ARGUMENTS", "message": "Origin and destination cannot be the same airport"}

    # Validate passengers strictly (1-9)
    pax, err = _validate_count(passengers, "passengers")
    if err:
        return err

    # Validate travel dates
    try:
        d_out = dt.date.fromisoformat(date)
    except (TypeError, ValueError):
        return {"error": "INVALID_ARGUMENTS", "message": "Flight date must be in YYYY-MM-DD format"}
    if d_out < today_ist:
        return {"error": "INVALID_ARGUMENTS", "message": f"Flight date {date} is in the past"}

    d_ret = None
    if return_date:
        try:
            d_ret = dt.date.fromisoformat(return_date)
        except (TypeError, ValueError):
            return {"error": "INVALID_ARGUMENTS", "message": "return_date must be in YYYY-MM-DD format"}
        if d_ret < d_out:
            return {"error": "INVALID_ARGUMENTS", "message": "return_date cannot be before departure date"}

    # Cache Key incorporates return_date to prevent one-way vs round-trip collision
    cache_key = f"flights:{orig_code}:{dest_code}:{date}:{return_date or 'oneway'}:{pax}"
    if use_cache:
        cached = _get_from_cache(cache_key)
        if cached:
            cached["cached"] = True
            return cached

    if fast_flights is None:
        logger.error("fast-flights dependency is not installed")
        return {"error": "PROVIDER_UNAVAILABLE", "message": "Flight search provider is currently unavailable (fast-flights dependency missing)."}

    fnf_list = []
    if fast_flights and hasattr(fast_flights, "FlightsNotFound"):
        fnf_list.append(getattr(fast_flights, "FlightsNotFound"))
    if _ff_exceptions and hasattr(_ff_exceptions, "FlightsNotFound"):
        fnf_list.append(getattr(_ff_exceptions, "FlightsNotFound"))
    flights_not_found = tuple(fnf_list)
    provider_errors = (primp.PrimpError, OSError)

    passengers_obj = fast_flights.Passengers(adults=pax) if pax > 1 else None

    # Fetch outbound flights
    try:
        q_out = fast_flights.create_query(
            flights=[fast_flights.FlightQuery(date=date, from_airport=orig_code, to_airport=dest_code)],
            currency="INR",
            trip="one-way",
            passengers=passengers_obj
        )
    except Exception as e:
        logger.exception("Failed to build flight query for %s -> %s on %s: %s", orig_code, dest_code, date, e)
        return {"error": "INVALID_ARGUMENTS", "message": f"Could not construct flight query: {e}"}

    tfs_out = q_out.params().get("tfs")
    try:
        flight_results_out = fast_flights.get_flights(q_out)
    except flights_not_found as e:
        logger.warning("Google Flights returned FlightsNotFound (errorHasStatus/no results): %s", e)
        deep_link = (
            f"https://www.google.com/travel/flights?tfs={tfs_out}&curr=INR"
            if tfs_out
            else f"https://www.google.com/travel/flights?q=oneway+flights+from+{orig_code}+to+{dest_code}+on+{date}&curr=INR"
        )
        return {
            "checked_at": now_iso,
            "results": [],
            "message": "Google returned no flight results for this search. This can mean no flights are available or a temporary provider error; check the search link.",
            "source": "Google Flights (Live)",
            "cached": False,
            "deep_link": deep_link,
        }
    except provider_errors as e:
        logger.exception("Flight provider search failed for %s -> %s on %s: %s", orig_code, dest_code, date, e)
        return {"error": "PROVIDER_UNAVAILABLE", "message": "Flight search provider is currently unavailable. Please try again later."}

    results = []
    date_clean = date.replace("-", "")

    if return_date:
        # For round-trip: fetch return flights and pair them cleanly
        try:
            q_ret = fast_flights.create_query(
                flights=[fast_flights.FlightQuery(date=return_date, from_airport=dest_code, to_airport=orig_code)],
                currency="INR",
                trip="one-way",
                passengers=passengers_obj
            )
        except Exception as e:
            logger.exception("Failed to build return flight query for %s -> %s on %s: %s", dest_code, orig_code, return_date, e)
            return {"error": "INVALID_ARGUMENTS", "message": f"Could not construct return flight query: {e}"}

        try:
            flight_results_ret = fast_flights.get_flights(q_ret)
        except flights_not_found as e:
            logger.warning("Google Flights returned FlightsNotFound for return query: %s", e)
            tfs_rt = None
            try:
                q_rt = fast_flights.create_query(
                    flights=[
                        fast_flights.FlightQuery(date=date, from_airport=orig_code, to_airport=dest_code),
                        fast_flights.FlightQuery(date=return_date, from_airport=dest_code, to_airport=orig_code),
                    ],
                    trip="round-trip",
                    currency="INR",
                    passengers=passengers_obj,
                )
                tfs_rt = q_rt.params().get("tfs")
            except (KeyError, ValueError, AttributeError) as e:
                logger.warning("Could not generate round-trip tfs link: %s", e)

            roundtrip_link = (
                f"https://www.google.com/travel/flights?tfs={tfs_rt}&curr=INR"
                if tfs_rt
                else f"https://www.google.com/travel/flights?q=roundtrip+flights+from+{orig_code}+to+{dest_code}+from+{date}+to+{return_date}&curr=INR"
            )
            return {
                "checked_at": now_iso,
                "results": [],
                "message": "Google returned no flight results for this search. This can mean no flights are available or a temporary provider error; check the search link.",
                "source": "Google Flights (Live)",
                "cached": False,
                "deep_link": roundtrip_link,
            }
        except provider_errors as e:
            logger.exception("Return flight provider search failed for %s -> %s on %s: %s", dest_code, orig_code, return_date, e)
            return {"error": "PROVIDER_UNAVAILABLE", "message": "Return flight search provider is currently unavailable. Please try again later."}

        out_list = list(flight_results_out)[:MAX_FLIGHT_RESULTS]
        ret_list = list(flight_results_ret)[:MAX_FLIGHT_RESULTS]

        if not ret_list:
            return {
                "checked_at": now_iso,
                "results": [],
                "message": f"No return flights found from {dest_code} to {orig_code} on {return_date}",
                "source": "Google Flights (Live)",
                "cached": False,
            }

        # Deep link for round-trip search using tfs parameter if available
        tfs_rt = None
        try:
            q_rt = fast_flights.create_query(
                flights=[
                    fast_flights.FlightQuery(date=date, from_airport=orig_code, to_airport=dest_code),
                    fast_flights.FlightQuery(date=return_date, from_airport=dest_code, to_airport=orig_code),
                ],
                trip="round-trip",
                currency="INR",
                passengers=passengers_obj,
            )
            tfs_rt = q_rt.params().get("tfs")
        except (KeyError, ValueError, AttributeError) as e:
            logger.warning("Could not generate round-trip tfs link: %s", e)

        roundtrip_link = (
            f"https://www.google.com/travel/flights?tfs={tfs_rt}&curr=INR"
            if tfs_rt
            else f"https://www.google.com/travel/flights?q=roundtrip+flights+from+{orig_code}+to+{dest_code}+from+{date}+to+{return_date}&curr=INR"
        )

        for idx, (out_item, ret_item) in enumerate(zip(out_list, ret_list)):
            out_legs, ret_legs = out_item.flights, ret_item.flights
            airline = out_item.airlines[0] if out_item.airlines else "Domestic Airline"
            if ret_item.airlines and ret_item.airlines[0] != airline:
                airline = f"{airline} + {ret_item.airlines[0]}"

            out_dep = f"{out_legs[0].departure.time[0]:02d}:{out_legs[0].departure.time[1]:02d}"
            ret_dep = f"{ret_legs[0].departure.time[0]:02d}:{ret_legs[0].departure.time[1]:02d}"
            out_dur = _calc_elapsed_minutes(out_legs)
            ret_dur = _calc_elapsed_minutes(ret_legs)
            fare_per_person = int(out_item.price + ret_item.price)
            total = fare_per_person * pax
            oid = f"F-{orig_code}-{dest_code}-{date_clean}-RT-{idx + 1}"

            results.append({
                "offer_id": oid,
                "airline": airline,
                "from": orig_code,
                "to": dest_code,
                "date": date,
                "return_date": return_date,
                "departs": out_dep,
                "return_departs": ret_dep,
                "duration_min": out_dur,
                "return_duration_min": ret_dur,
                "stops": len(out_legs) - 1,
                "return_stops": len(ret_legs) - 1,
                "fare_basis": "sum_of_one_way_fares",
                "fare_per_person_inr": fare_per_person,
                "passengers": pax,
                "total_inr": total,
                "baggage": None,
                "fare_rule": None,
                "is_live": True,
                "deep_link": roundtrip_link,
            })

    else:
        # One-way search
        deep_link = (
            f"https://www.google.com/travel/flights?tfs={tfs_out}&curr=INR"
            if tfs_out
            else f"https://www.google.com/travel/flights?q=oneway+flights+from+{orig_code}+to+{dest_code}+on+{date}&curr=INR"
        )

        for idx, item in enumerate(list(flight_results_out)[:MAX_FLIGHT_RESULTS]):
            legs = item.flights
            airline = item.airlines[0] if item.airlines else "Domestic Airline"
            dep_time = f"{legs[0].departure.time[0]:02d}:{legs[0].departure.time[1]:02d}"
            dur_mins = _calc_elapsed_minutes(legs)
            stops = len(legs) - 1
            fare_per_person = int(item.price)
            total = fare_per_person * pax
            oid = f"F-{orig_code}-{dest_code}-{date_clean}-OW-{idx + 1}"

            results.append({
                "offer_id": oid,
                "airline": airline,
                "from": orig_code,
                "to": dest_code,
                "date": date,
                "departs": dep_time,
                "duration_min": dur_mins,
                "stops": stops,
                "fare_per_person_inr": fare_per_person,
                "passengers": pax,
                "total_inr": total,
                "baggage": None,
                "fare_rule": None,
                "is_live": True,
                "deep_link": deep_link,
            })

    if not results:
        return {
            "checked_at": now_iso,
            "results": [],
            "message": f"No flights found from {orig_code} to {dest_code} on {date}",
            "source": "Google Flights (Live)",
            "cached": False
        }

    payload = {
        "checked_at": now_iso,
        "results": results,
        "source": "Google Flights (Live)",
        "cached": False
    }

    _save_to_cache(cache_key, "flights", payload, ttl_hours=CACHE_TTL_HOURS)
    return payload


def _build_hotel_offer(
    offer_id: str,
    hotel: str,
    city: str,
    room: Optional[str] = None,
    price_per_night: int = 0,
    nights: int = 1,
    guests: int = 1,
    check_in: str = "",
    check_out: str = "",
    amenities: Optional[List[str]] = None,
    deep_link: str = "",
    is_live: bool = True,
    rate_rule: Optional[str] = None,
    taxes_included: Optional[bool] = None,
    taxes_estimate_inr: Optional[int] = None,
) -> Dict[str, Any]:
    """Helper factory for hotel offer dictionaries."""
    total = price_per_night * nights
    return {
        "offer_id": offer_id,
        "hotel": hotel,
        "city": city,
        "room": room,
        "price_per_night_inr": price_per_night,
        "nights": nights,
        "guests": guests,
        "check_in": check_in,
        "check_out": check_out,
        "amenities": amenities,
        "rate_rule": rate_rule,
        "total_inr": total,
        "taxes_included": taxes_included,
        "taxes_estimate_inr": taxes_estimate_inr,
        "is_live": is_live,
        "deep_link": deep_link,
    }


def _parse_google_travel_hotels(html: str, deep_link_url: str = "") -> List[Dict[str, Any]]:
    """Parse hotel names and nightly rates from Google Travel search results HTML."""
    tree = LexborHTMLParser(html)
    hotels = []
    seen = set()

    for h2 in tree.css("h2"):
        title = h2.text().strip()
        if not title or len(title) < 4 or title.lower() in seen:
            continue
        if any(bad in title for bad in [
            "Things to do", "Featured", "People also search", "Frequently asked",
            "Popular filters", "Explore", "Questions", "Hotels in"
        ]):
            continue

        # Scope strictly to the enclosing card container
        card = h2.parent
        while card and card.tag not in ("body", "html"):
            if card.attributes.get("jscontroller") or card.tag == "c-wiz":
                break
            card = card.parent

        if card is None or card.tag in ("body", "html"):
            continue

        # Search for prices ONLY inside this card's text, never climbing higher
        card_text = card.text()
        price_matches = re.findall(r"₹\s?([\d,]+)", card_text)
        if not price_matches:
            continue  # Sold out / no price on this card

        valid_prices = []
        for p_str in price_matches:
            try:
                val = int(p_str.replace(",", ""))
                if MIN_HOTEL_PRICE_INR <= val <= MAX_HOTEL_PRICE_INR:
                    valid_prices.append(val)
            except ValueError:
                continue

        if not valid_prices:
            continue

        distinct_prices = sorted(set(valid_prices))
        if len(distinct_prices) > 1:
            logger.debug("Hotel %s has multiple distinct valid prices: %s", title, distinct_prices)

        price_val = min(valid_prices)
        seen.add(title.lower())
        hotels.append({
            "hotel": title,
            "price_per_night_inr": price_val,
            "deep_link": deep_link_url
        })
        if len(hotels) >= HOTEL_CANDIDATE_POOL:  # Collect candidate pool for filtering
            break

    if not hotels:
        logger.warning("Could not read hotel results from provider HTML (len=%d); layout may have changed or request was blocked", len(html))

    return hotels


def _scrape_live_hotels_google(city: str, check_in: str, check_out: str) -> List[Dict[str, Any]]:
    """Scrapes live hotel rates from Google Travel strictly isolating card subtrees."""
    client = primp.Client(impersonate="chrome_145", impersonate_os="macos", referer=True, cookie_store=True)
    query = f"hotels in {city} from {check_in} to {check_out}"
    url = f"https://www.google.com/travel/search?q={query.replace(' ', '+')}&curr=INR"
    res = client.get(url, timeout=HTTP_TIMEOUT_S)
    if res.status_code != 200:
        raise ProviderError(f"Google Travel returned status {res.status_code}")

    return _parse_google_travel_hotels(res.text, deep_link_url=url)


def search_live_hotels(
    city: str,
    check_in: str,
    check_out: str,
    guests: int = 2,
    max_price_per_night: Optional[float] = None,
    use_cache: bool = True
) -> Dict[str, Any]:
    """Fetch live hotel availability for Indian destinations with real Google Travel prices.
    
    Conforms to the travel advisor tool contract in travel/tools.py.
    """
    clean_city = (city or "").strip().title()
    if not clean_city:
        return {"error": "INVALID_ARGUMENTS", "message": "City name is required"}

    # Validate guests strictly (1-9)
    guest_count, err = _validate_count(guests, "guests")
    if err:
        return err

    now_iso = get_current_ist_time()
    today_ist = dt.datetime.now(ZoneInfo("Asia/Kolkata")).date()

    # Validate dates strictly
    try:
        ci = dt.date.fromisoformat(check_in)
        co = dt.date.fromisoformat(check_out)
    except (ValueError, TypeError):
        return {"error": "INVALID_ARGUMENTS", "message": "check_in and check_out must be YYYY-MM-DD"}

    if ci < today_ist:
        return {"error": "INVALID_ARGUMENTS", "message": f"check_in date {check_in} is in the past"}
    if co <= ci:
        return {"error": "INVALID_ARGUMENTS", "message": "check_out must be strictly after check_in"}
    nights = (co - ci).days

    # Parse and validate max_price_per_night cap
    cap = None
    if max_price_per_night not in (None, ""):
        try:
            cap = float(max_price_per_night)
        except (TypeError, ValueError):
            return {"error": "INVALID_ARGUMENTS", "message": "max_price_per_night must be a number in INR"}
        if cap < MIN_HOTEL_PRICE_INR:
            return {"checked_at": now_iso, "results": [], "message": f"No hotels found at or below ₹{int(cap)} per night"}

    cache_key = f"hotels_scrape:{clean_city}:{check_in}:{check_out}"
    live_hotels = None
    is_cached = False
    data_checked_at = now_iso

    if use_cache:
        cached_entry = _get_from_cache(cache_key)
        if cached_entry and isinstance(cached_entry.get("hotels"), list):
            live_hotels = cached_entry["hotels"]
            data_checked_at = cached_entry.get("checked_at") or now_iso
            is_cached = True

    if live_hotels is None:
        # Attempt live scraping from Google Travel
        try:
            live_hotels = _scrape_live_hotels_google(clean_city, check_in, check_out)
        except (ProviderError, primp.PrimpError, OSError) as e:
            logger.exception("Hotel provider search failed for %s from %s to %s: %s", clean_city, check_in, check_out, e)
            return {"error": "PROVIDER_UNAVAILABLE", "message": "Hotel search provider is currently unavailable. Please try again later."}

        if not live_hotels:
            return {
                "error": "PROVIDER_RESPONSE_UNREADABLE",
                "message": "Could not read hotel results from the provider page; the layout may have changed or the request was blocked."
            }

        _save_to_cache(
            cache_key,
            "hotels",
            {"hotels": live_hotels, "checked_at": now_iso},
            ttl_hours=CACHE_TTL_HOURS,
        )

    city_code = resolve_iata(clean_city) or clean_city[:3].upper()
    date_clean = check_in.replace("-", "")
    results = []

    for item in live_hotels:
        rate = item["price_per_night_inr"]
        if cap is not None and rate > cap:
            continue
        hotel_hash = hashlib.sha256(item["hotel"].lower().strip().encode("utf-8")).hexdigest()[:6]
        oid = f"H-{city_code}-{date_clean}-{hotel_hash}"
        results.append(_build_hotel_offer(
            offer_id=oid,
            hotel=item["hotel"],
            city=clean_city,
            room=None,
            price_per_night=rate,
            nights=nights,
            guests=guest_count,
            check_in=check_in,
            check_out=check_out,
            amenities=None,
            deep_link=item["deep_link"],
            is_live=True,
            rate_rule=None,
            taxes_included=None,
            taxes_estimate_inr=None,
        ))
        if len(results) >= MAX_HOTEL_RESULTS:
            break

    # If a cap was specified and all available hotels were above it
    if cap is not None and not results:
        return {
            "checked_at": data_checked_at,
            "results": [],
            "message": f"No hotels found in {clean_city} at or below ₹{int(cap)} per night",
            "source": "Google Travel Hotels (Live)",
            "cached": is_cached
        }

    if not results:
        return {
            "checked_at": data_checked_at,
            "results": [],
            "message": f"No hotels found in {clean_city} for {check_in} to {check_out}",
            "source": "Google Travel Hotels (Live)",
            "cached": is_cached
        }

    return {
        "checked_at": data_checked_at,
        "results": results,
        "source": "Google Travel Hotels (Live)",
        "cached": is_cached
    }
