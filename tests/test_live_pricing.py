"""Unit tests for travel.live_pricing."""
import contextlib
import datetime as dt
from pathlib import Path
import sqlite3
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo
import pytest

from travel import live_pricing
from travel.offer_schemas import validate_offer

_ORIGINAL_SCRAPE_HOTELS = live_pricing._scrape_live_hotels_google
FIXTURE = Path(__file__).parent / "fixtures" / "google_travel_sample.html"

TODAY_IST = dt.datetime.now(ZoneInfo("Asia/Kolkata")).date()
DATE_FUTURE_1 = (TODAY_IST + dt.timedelta(days=30)).isoformat()
DATE_FUTURE_2 = (TODAY_IST + dt.timedelta(days=33)).isoformat()
DATE_FUTURE_3 = (TODAY_IST + dt.timedelta(days=36)).isoformat()
DATE_PAST = (TODAY_IST - dt.timedelta(days=5)).isoformat()


@pytest.fixture(autouse=True)
def mock_cache_db_and_offline_guard(tmp_path, monkeypatch):
    """Enforces 100% offline unit testing and isolates SQLite cache per test."""
    test_db = tmp_path / "test_pricing_cache.db"
    monkeypatch.setattr(live_pricing, "CACHE_DB_PATH", test_db)
    live_pricing._init_cache_db()

    def block_network(*args, **kwargs):
        raise RuntimeError("Network call attempted in unit test")

    if live_pricing.fast_flights:
        monkeypatch.setattr(live_pricing.fast_flights, "get_flights", block_network)
    monkeypatch.setattr(live_pricing, "_scrape_live_hotels_google", block_network)
    return test_db


# --- IATA Resolution and Consistency ---

@pytest.mark.parametrize("city,expected", [
    ("Delhi", "DEL"),
    ("  delhi ", "DEL"),
    ("New Delhi", "DEL"),
    ("Mumbai", "BOM"),
    ("Goa", "GOI"),
    ("North Goa", "GOX"),
    ("Udaipur", "UDR"),
    ("Kanpur", "KNU"),
    ("DEL", "DEL"),
    ("BOM", "BOM"),
    ("GOI", "GOI"),
    ("GOX", "GOX"),
])
def test_resolve_iata_valid(city, expected):
    assert live_pricing.resolve_iata(city) == expected


@pytest.mark.parametrize("invalid_input", ["xyz", "XYZ", "UnknownCity", "", None, 123])
def test_resolve_iata_invalid(invalid_input):
    assert live_pricing.resolve_iata(invalid_input) is None


def test_city_to_iata_consistency():
    for city, code in live_pricing.CITY_TO_IATA.items():
        assert code in live_pricing.IATA_TO_CANONICAL, f"Code {code} for '{city}' missing from IATA_TO_CANONICAL"


def test_valid_indian_airports_derived_from_iata_to_canonical():
    assert live_pricing.VALID_INDIAN_AIRPORTS == set(live_pricing.IATA_TO_CANONICAL.keys())


# --- Flight Input Validation (Parametrized) ---

@pytest.mark.parametrize("orig,dest,date,pax,expected_error", [
    ("DEL", "BOM", DATE_PAST, 1, "INVALID_ARGUMENTS"),
    ("DEL", "BOM", "invalid-date", 1, "INVALID_ARGUMENTS"),
    ("DEL", "DEL", DATE_FUTURE_1, 1, "INVALID_ARGUMENTS"),
    ("UNKNOWN_CODE", "BOM", DATE_FUTURE_1, 1, "UNKNOWN_AIRPORT"),
    ("DEL", "UNKNOWN_CODE", DATE_FUTURE_1, 1, "UNKNOWN_AIRPORT"),
    ("DEL", "BOM", DATE_FUTURE_1, 0, "INVALID_ARGUMENTS"),
    ("DEL", "BOM", DATE_FUTURE_1, 10, "INVALID_ARGUMENTS"),
    ("DEL", "BOM", DATE_FUTURE_1, -1, "INVALID_ARGUMENTS"),
    ("DEL", "BOM", DATE_FUTURE_1, "abc", "INVALID_ARGUMENTS"),
    ("DEL", "BOM", DATE_FUTURE_1, None, "INVALID_ARGUMENTS"),
    ("DEL", "BOM", DATE_FUTURE_1, 3.5, "INVALID_ARGUMENTS"),
])
def test_flight_input_validation_errors(orig, dest, date, pax, expected_error):
    res = live_pricing.search_live_flights(orig, dest, date, passengers=pax, use_cache=False)
    assert res.get("error") == expected_error


# --- Hotel Input Validation (Parametrized) ---

@pytest.mark.parametrize("city,ci,co,guests,expected_error", [
    ("", DATE_FUTURE_1, DATE_FUTURE_2, 2, "INVALID_ARGUMENTS"),
    ("Delhi", "bad-date", DATE_FUTURE_2, 2, "INVALID_ARGUMENTS"),
    ("Delhi", DATE_FUTURE_1, "bad-date", 2, "INVALID_ARGUMENTS"),
    ("Delhi", DATE_PAST, DATE_FUTURE_2, 2, "INVALID_ARGUMENTS"),
    ("Delhi", DATE_FUTURE_2, DATE_FUTURE_1, 2, "INVALID_ARGUMENTS"),  # check_out <= check_in
    ("Delhi", DATE_FUTURE_1, DATE_FUTURE_1, 2, "INVALID_ARGUMENTS"),
    ("Delhi", DATE_FUTURE_1, DATE_FUTURE_2, 0, "INVALID_ARGUMENTS"),
    ("Delhi", DATE_FUTURE_1, DATE_FUTURE_2, 10, "INVALID_ARGUMENTS"),
    ("Delhi", DATE_FUTURE_1, DATE_FUTURE_2, -1, "INVALID_ARGUMENTS"),
    ("Delhi", DATE_FUTURE_1, DATE_FUTURE_2, "abc", "INVALID_ARGUMENTS"),
    ("Delhi", DATE_FUTURE_1, DATE_FUTURE_2, None, "INVALID_ARGUMENTS"),
    ("Delhi", DATE_FUTURE_1, DATE_FUTURE_2, 2.5, "INVALID_ARGUMENTS"),
])
def test_hotel_input_validation_errors(city, ci, co, guests, expected_error):
    res = live_pricing.search_live_hotels(city, ci, co, guests=guests, use_cache=False)
    assert res.get("error") == expected_error


# --- Hotel Cap Filtering and Offer ID Uniqueness ---

def test_hotel_cap_filtering_with_live_data(monkeypatch):
    mock_hotels = [
        {"hotel": "Budget Inn", "price_per_night_inr": 1500, "deep_link": "http://..."},
        {"hotel": "Midtown Suites", "price_per_night_inr": 3500, "deep_link": "http://..."},
        {"hotel": "Luxury Palace", "price_per_night_inr": 7000, "deep_link": "http://..."},
    ]
    monkeypatch.setattr(live_pricing, "_scrape_live_hotels_google", lambda *a, **kw: mock_hotels)

    # Cap 4000: returns Budget Inn and Midtown Suites
    res = live_pricing.search_live_hotels("Delhi", DATE_FUTURE_1, DATE_FUTURE_2, max_price_per_night=4000, use_cache=False)
    assert "results" in res
    assert len(res["results"]) == 2
    assert [r["hotel"] for r in res["results"]] == ["Budget Inn", "Midtown Suites"]

    # Cap 1000: all options above cap -> returns empty results without fake fallback
    res_empty = live_pricing.search_live_hotels("Delhi", DATE_FUTURE_1, DATE_FUTURE_2, max_price_per_night=1000, use_cache=False)
    assert res_empty.get("error") is None
    assert res_empty["results"] == []
    assert "at or below ₹1000" in res_empty["message"]


def test_hotel_offer_ids_differ_across_dates_and_cities(monkeypatch):
    monkeypatch.setattr(live_pricing, "_scrape_live_hotels_google", lambda city, ci, co: [
        {"hotel": "Grand Palace", "price_per_night_inr": 3000, "deep_link": "http://..."}
    ])
    res_del_d1 = live_pricing.search_live_hotels("Delhi", DATE_FUTURE_1, DATE_FUTURE_2, use_cache=False)
    res_del_d2 = live_pricing.search_live_hotels("Delhi", DATE_FUTURE_2, DATE_FUTURE_3, use_cache=False)
    res_bom_d1 = live_pricing.search_live_hotels("Mumbai", DATE_FUTURE_1, DATE_FUTURE_2, use_cache=False)

    id_del_d1 = res_del_d1["results"][0]["offer_id"]
    id_del_d2 = res_del_d2["results"][0]["offer_id"]
    id_bom_d1 = res_bom_d1["results"][0]["offer_id"]

    assert id_del_d1 != id_del_d2
    assert id_del_d1 != id_bom_d1
    assert id_del_d2 != id_bom_d1


def test_hotel_offer_id_uses_iata_and_hotel_name_hash(monkeypatch):
    monkeypatch.setattr(live_pricing, "_scrape_live_hotels_google", lambda city, ci, co: [
        {"hotel": "Rajputana Palace", "price_per_night_inr": 3500, "deep_link": "http://..."},
        {"hotel": "Desert Camp", "price_per_night_inr": 2800, "deep_link": "http://..."},
    ])

    res_jaipur = live_pricing.search_live_hotels("Jaipur", DATE_FUTURE_1, DATE_FUTURE_2, use_cache=False)
    res_jaisalmer = live_pricing.search_live_hotels("Jaisalmer", DATE_FUTURE_1, DATE_FUTURE_2, use_cache=False)

    jaipur_id = res_jaipur["results"][0]["offer_id"]
    jaisalmer_id = res_jaisalmer["results"][0]["offer_id"]

    # Jaipur uses JAI, Jaisalmer uses JSA (no collision to JAI)
    assert jaipur_id.startswith("H-JAI-")
    assert jaisalmer_id.startswith("H-JSA-")

    # Hotel IDs use hash of hotel name instead of scrape index
    assert not jaipur_id.endswith("-1")
    assert not jaipur_id.endswith("-2")


# --- Cache Behavior: Hit, Miss, TTL Expiry, and Error Isolation ---

def test_cache_hit_miss_and_ttl_expiry(monkeypatch, tmp_path):
    mock_hotels = [{"hotel": "Hotel Taj", "price_per_night_inr": 4000, "deep_link": "http://..."}]
    monkeypatch.setattr(live_pricing, "_scrape_live_hotels_google", lambda *a, **kw: mock_hotels)

    # 1. First call -> Cache Miss
    res1 = live_pricing.search_live_hotels("Delhi", DATE_FUTURE_1, DATE_FUTURE_2, use_cache=True)
    assert res1["cached"] is False

    # 2. Second call -> Cache Hit
    res2 = live_pricing.search_live_hotels("Delhi", DATE_FUTURE_1, DATE_FUTURE_2, use_cache=True)
    assert res2["cached"] is True
    assert res2["results"][0]["hotel"] == "Hotel Taj"

    # 3. Simulate TTL expiry in sqlite db
    test_db = tmp_path / "test_pricing_cache.db"
    with contextlib.closing(sqlite3.connect(test_db)) as conn:
        with conn:
            conn.execute("UPDATE price_cache SET expires_at = datetime('now', '-1 hour')")

    # 4. Third call -> Expired -> Cache Miss
    res3 = live_pricing.search_live_hotels("Delhi", DATE_FUTURE_1, DATE_FUTURE_2, use_cache=True)
    assert res3["cached"] is False


def test_error_and_provider_failures_are_never_cached(monkeypatch, tmp_path):
    monkeypatch.setattr(live_pricing, "_scrape_live_hotels_google", MagicMock(side_effect=ConnectionError("fail")))

    res = live_pricing.search_live_hotels("Delhi", DATE_FUTURE_1, DATE_FUTURE_2, use_cache=True)
    assert res.get("error") == "PROVIDER_UNAVAILABLE"

    test_db = tmp_path / "test_pricing_cache.db"
    with contextlib.closing(sqlite3.connect(test_db)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM price_cache").fetchone()[0]
    assert count == 0


# --- Provider Offline / Empty Results (No Fabricated Fallbacks) ---

def test_flight_provider_failure_returns_provider_unavailable_without_fake_data(monkeypatch):
    import fast_flights
    monkeypatch.setattr(fast_flights, "get_flights", MagicMock(side_effect=ConnectionError("Network down")))

    res = live_pricing.search_live_flights("DEL", "BOM", DATE_FUTURE_1, use_cache=False)
    assert res.get("error") == "PROVIDER_UNAVAILABLE"
    assert "IndiGo" not in str(res)


def test_flight_empty_results_returns_real_empty_structure(monkeypatch):
    import fast_flights
    monkeypatch.setattr(fast_flights, "get_flights", lambda *a, **kw: [])

    res = live_pricing.search_live_flights("DEL", "BOM", DATE_FUTURE_1, use_cache=False)
    assert "error" not in res
    assert res["results"] == []
    assert "checked_at" in res
    assert "No flights found" in res["message"]
    assert res["cached"] is False
    assert "IndiGo" not in str(res)


def test_hotel_provider_failure_returns_provider_unavailable_without_fake_data(monkeypatch):
    monkeypatch.setattr(live_pricing, "_scrape_live_hotels_google", MagicMock(side_effect=ConnectionError("Google Travel down")))

    res = live_pricing.search_live_hotels("Udaipur", DATE_FUTURE_1, DATE_FUTURE_2, use_cache=False)
    assert res.get("error") == "PROVIDER_UNAVAILABLE"
    assert "Grand Residency" not in str(res)
    assert "Heritage Haveli" not in str(res)


def test_provider_error_class_and_http_status_503(monkeypatch):
    assert hasattr(live_pricing, "ProviderError")
    assert issubclass(live_pricing.ProviderError, Exception)

    class MockResponse:
        status_code = 503
        text = "Service Unavailable"

    mock_client = MagicMock()
    mock_client.get.return_value = MockResponse()
    monkeypatch.setattr(live_pricing.primp, "Client", lambda *a, **kw: mock_client)
    monkeypatch.setattr(live_pricing, "_scrape_live_hotels_google", _ORIGINAL_SCRAPE_HOTELS)

    with pytest.raises(live_pricing.ProviderError):
        live_pricing._scrape_live_hotels_google("Udaipur", DATE_FUTURE_1, DATE_FUTURE_2)

    # 200 with 3-card fixture returns parsed list
    class Mock200Response:
        status_code = 200
        text = HTML_3_CARDS_FIXTURE

    mock_client.get.return_value = Mock200Response()
    hotels = live_pricing._scrape_live_hotels_google("Udaipur", DATE_FUTURE_1, DATE_FUTURE_2)
    assert len(hotels) == 2
    assert hotels[0]["hotel"] == "Hotel Low"
    assert hotels[0]["price_per_night_inr"] == 2500


def test_hotel_parser_bug_propagates_without_disguise(monkeypatch):
    monkeypatch.setattr(live_pricing, "_scrape_live_hotels_google", MagicMock(side_effect=AttributeError("Parser bug in card selector")))
    with pytest.raises(AttributeError):
        live_pricing.search_live_hotels("Udaipur", DATE_FUTURE_1, DATE_FUTURE_2, use_cache=False)


def test_flight_create_query_failure_returns_structured_error(monkeypatch):
    import fast_flights
    monkeypatch.setattr(fast_flights, "create_query", MagicMock(side_effect=ValueError("Invalid query parameter")))

    res = live_pricing.search_live_flights("DEL", "BOM", DATE_FUTURE_1, use_cache=False)
    assert res.get("error") == "INVALID_ARGUMENTS"
    assert "Could not construct flight query" in res["message"]


def test_hotel_zero_cards_returns_provider_response_unreadable(monkeypatch):
    monkeypatch.setattr(live_pricing, "_scrape_live_hotels_google", lambda *a, **kw: [])

    res = live_pricing.search_live_hotels("Udaipur", DATE_FUTURE_1, DATE_FUTURE_2, use_cache=False)
    assert res.get("error") == "PROVIDER_RESPONSE_UNREADABLE"
    assert "Could not read hotel results from the provider page" in res["message"]


def test_flight_flights_not_found_returns_clear_message_and_link(monkeypatch):
    import fast_flights

    err_cls = getattr(fast_flights, "FlightsNotFound", getattr(fast_flights.exceptions, "FlightsNotFound", Exception))
    monkeypatch.setattr(fast_flights, "get_flights", MagicMock(side_effect=err_cls("Google returned errorHasStatus")))

    res = live_pricing.search_live_flights("DEL", "BOM", DATE_FUTURE_1, use_cache=False)
    assert res.get("error") is None
    assert res["results"] == []
    assert res["message"] == "Google returned no flight results for this search. This can mean no flights are available or a temporary provider error; check the search link."
    assert "deep_link" in res
    assert "google.com/travel/flights" in res["deep_link"]


# --- Live Offers Contain No Invented Fields ---

def _make_mock_flight_item(airline="IndiGo", dep_time=(11, 0), dur=120, stops=0, price=4000):
    start_dt = dt.datetime(2026, 11, 20, *dep_time)
    legs = []
    if stops == 0:
        arr_dt = start_dt + dt.timedelta(minutes=dur)
        leg = MagicMock()
        leg.departure = MagicMock(date=(start_dt.year, start_dt.month, start_dt.day), time=(start_dt.hour, start_dt.minute))
        leg.arrival = MagicMock(date=(arr_dt.year, arr_dt.month, arr_dt.day), time=(arr_dt.hour, arr_dt.minute))
        leg.duration = dur
        legs.append(leg)
    else:
        # Chain multi-leg items sequentially with a real layover gap
        layover_gap = 30
        total_layovers = stops * layover_gap
        if dur > total_layovers:
            total_flight_time = dur - total_layovers
        else:
            layover_gap = max(5, (dur // (stops + 1)) // 2)
            total_layovers = stops * layover_gap
            total_flight_time = dur - total_layovers

        flight_per_leg = total_flight_time // (stops + 1)
        cur_dt = start_dt
        for s in range(stops + 1):
            if s == stops:
                leg_arr = start_dt + dt.timedelta(minutes=dur)
                leg_dur = int((leg_arr - cur_dt).total_seconds() // 60)
            else:
                leg_dur = flight_per_leg
                leg_arr = cur_dt + dt.timedelta(minutes=leg_dur)

            leg = MagicMock()
            leg.departure = MagicMock(date=(cur_dt.year, cur_dt.month, cur_dt.day), time=(cur_dt.hour, cur_dt.minute))
            leg.arrival = MagicMock(date=(leg_arr.year, leg_arr.month, leg_arr.day), time=(leg_arr.hour, leg_arr.minute))
            leg.duration = leg_dur
            legs.append(leg)

            cur_dt = leg_arr + dt.timedelta(minutes=layover_gap)

    item = MagicMock()
    item.flights = legs
    item.airlines = [airline]
    item.price = price
    return item


def test_flight_parsing_with_fake_fast_flights(monkeypatch, caplog):
    import fast_flights

    out_item = _make_mock_flight_item(airline="Vistara", dep_time=(6, 45), dur=135, stops=0, price=5200)
    monkeypatch.setattr(fast_flights, "get_flights", lambda *a, **kw: [out_item])

    res = live_pricing.search_live_flights("DEL", "BOM", DATE_FUTURE_1, passengers=2, use_cache=False)
    assert res.get("error") is None
    assert len(res["results"]) == 1
    assert "Could not compute elapsed duration" not in caplog.text
    offer = res["results"][0]
    assert offer["airline"] == "Vistara"
    assert offer["departs"] == "06:45"
    assert offer["duration_min"] == 135
    assert offer["stops"] == 0
    assert offer["fare_per_person_inr"] == 5200
    assert offer["passengers"] == 2
    assert offer["total_inr"] == 10400
    assert offer["baggage"] is None
    assert offer["fare_rule"] is None
    assert offer["is_live"] is True


def test_live_hotel_offers_contain_no_invented_fields(monkeypatch):
    mock_hotels = [{
        "hotel": "Lake Palace",
        "price_per_night_inr": 15000,
        "deep_link": "https://google.com/travel/..."
    }]
    monkeypatch.setattr(live_pricing, "_scrape_live_hotels_google", lambda *a, **kw: mock_hotels)

    res = live_pricing.search_live_hotels("Udaipur", DATE_FUTURE_1, DATE_FUTURE_2, use_cache=False)
    assert len(res["results"]) > 0
    for offer in res["results"]:
        assert offer.get("room") is None
        assert offer.get("amenities") is None
        assert offer.get("rate_rule") is None
        assert offer.get("taxes_included") is None
        assert offer.get("taxes_estimate_inr") is None


# --- Round-trip Flights ---

def test_roundtrip_both_ok_includes_fare_basis_and_return_stops(monkeypatch, caplog):
    import fast_flights

    out_item = _make_mock_flight_item(airline="Air India", dep_time=(8, 0), dur=130, stops=0, price=5000)
    ret_item = _make_mock_flight_item(airline="IndiGo", dep_time=(18, 0), dur=140, stops=1, price=4500)

    monkeypatch.setattr(fast_flights, "get_flights", MagicMock(side_effect=[[out_item], [ret_item]]))

    res = live_pricing.search_live_flights("DEL", "BOM", DATE_FUTURE_1, passengers=2, return_date=DATE_FUTURE_2, use_cache=False)
    assert "results" in res
    assert len(res["results"]) == 1
    assert "Could not compute elapsed duration" not in caplog.text
    offer = res["results"][0]
    assert offer["fare_basis"] == "sum_of_one_way_fares"
    assert offer["return_stops"] == 1
    assert offer["stops"] == 0
    assert offer["fare_per_person_inr"] == 9500
    assert offer["passengers"] == 2
    assert offer["total_inr"] == 19000
    assert "tfs=" in offer["deep_link"]


def test_roundtrip_out_ok_return_empty_returns_empty_results(monkeypatch):
    import fast_flights

    out_item = _make_mock_flight_item()
    monkeypatch.setattr(fast_flights, "get_flights", MagicMock(side_effect=[[out_item], []]))

    res = live_pricing.search_live_flights("DEL", "BOM", DATE_FUTURE_1, passengers=1, return_date=DATE_FUTURE_2, use_cache=False)
    assert res.get("error") is None
    assert res["results"] == []
    assert "No return flights found" in res["message"] or "No flights found" in res["message"]


def test_roundtrip_out_ok_return_raises_returns_provider_error(monkeypatch):
    import fast_flights

    out_item = _make_mock_flight_item()
    monkeypatch.setattr(fast_flights, "get_flights", MagicMock(side_effect=[[out_item], ConnectionError("Return flight endpoint unreachable")]))

    res = live_pricing.search_live_flights("DEL", "BOM", DATE_FUTURE_1, passengers=1, return_date=DATE_FUTURE_2, use_cache=False)
    assert res.get("error") == "PROVIDER_UNAVAILABLE"
    assert "results" not in res


# --- Elapsed Duration and Error Handling ---

def test_programming_error_in_flight_processing_is_not_disguised_as_provider_offline(monkeypatch):
    import fast_flights

    bad_item = MagicMock()
    bad_item.flights = "not-a-list-of-legs"
    bad_item.airlines = ["IndiGo"]
    bad_item.price = 4500

    monkeypatch.setattr(fast_flights, "get_flights", lambda *a, **kw: [bad_item])

    with pytest.raises((TypeError, AttributeError)):
        live_pricing.search_live_flights("DEL", "BOM", DATE_FUTURE_1, use_cache=False)


def test_calc_elapsed_minutes_with_layover():
    leg1 = MagicMock()
    leg1.departure = MagicMock(date=(2026, 11, 20), time=(10, 0))
    leg1.arrival = MagicMock(date=(2026, 11, 20), time=(12, 0))
    leg1.duration = 120

    # 1.5 hour layover between 12:00 and 13:30
    leg2 = MagicMock()
    leg2.departure = MagicMock(date=(2026, 11, 20), time=(13, 30))
    leg2.arrival = MagicMock(date=(2026, 11, 20), time=(15, 30))
    leg2.duration = 120

    # Total elapsed should be 10:00 to 15:30 = 330 minutes
    dur = live_pricing._calc_elapsed_minutes([leg1, leg2])
    assert dur == 330


def test_fast_flights_missing_handled_gracefully(monkeypatch):
    monkeypatch.setattr(live_pricing, "fast_flights", None)
    res = live_pricing.search_live_flights("DEL", "BOM", DATE_FUTURE_1, use_cache=False)
    assert res.get("error") == "PROVIDER_UNAVAILABLE"
    assert "fast-flights" in res["message"] or "unavailable" in res["message"].lower()


# --- Contract Schema Validation (travel.offer_schemas) ---

def test_validate_offer_on_mocked_scenarios(monkeypatch):
    import fast_flights

    # 1. One-way flight offer
    out_item = _make_mock_flight_item(airline="Vistara", dep_time=(6, 45), dur=135, stops=0, price=5200)
    monkeypatch.setattr(fast_flights, "get_flights", lambda *a, **kw: [out_item])
    res_flight = live_pricing.search_live_flights("DEL", "BOM", DATE_FUTURE_1, passengers=2, use_cache=False)
    for offer in res_flight["results"]:
        problems = validate_offer(offer, kind="flight", roundtrip=False)
        assert problems == [], f"Flight offer schema problems: {problems}"

    # 2. Round-trip flight offer
    ret_item = _make_mock_flight_item(airline="IndiGo", dep_time=(18, 0), dur=140, stops=1, price=4500)
    monkeypatch.setattr(fast_flights, "get_flights", MagicMock(side_effect=[[out_item], [ret_item]]))
    res_rt = live_pricing.search_live_flights("DEL", "BOM", DATE_FUTURE_1, passengers=1, return_date=DATE_FUTURE_2, use_cache=False)
    for offer in res_rt["results"]:
        problems = validate_offer(offer, kind="flight", roundtrip=True)
        assert problems == [], f"Round-trip offer schema problems: {problems}"

    # 3. Hotel offer
    mock_hotels = [{"hotel": "Lake Palace", "price_per_night_inr": 15000, "deep_link": "https://google.com/travel/..."}]
    monkeypatch.setattr(live_pricing, "_scrape_live_hotels_google", lambda *a, **kw: mock_hotels)
    res_hotel = live_pricing.search_live_hotels("Udaipur", DATE_FUTURE_1, DATE_FUTURE_2, use_cache=False)
    for offer in res_hotel["results"]:
        problems = validate_offer(offer, kind="hotel")
        assert problems == [], f"Hotel offer schema problems: {problems}"


def test_validate_offer_catches_schema_violations():
    valid_flight = {
        "offer_id": "F-DEL-BOM-1", "airline": "IndiGo", "from": "DEL", "to": "BOM",
        "date": "2026-12-01", "departs": "10:00", "duration_min": 120, "stops": 0,
        "fare_per_person_inr": 4000, "passengers": 1, "total_inr": 4000,
        "baggage": None, "fare_rule": None, "is_live": True, "deep_link": "http://..."
    }

    # Missing key
    missing_key_offer = dict(valid_flight)
    del missing_key_offer["airline"]
    assert any("Missing required key" in p for p in validate_offer(missing_key_offer, "flight"))

    # Unexpected key
    unexpected_offer = dict(valid_flight, extra_bogus_field="surprise")
    assert any("Unexpected key" in p for p in validate_offer(unexpected_offer, "flight"))

    # Wrong type (string for int)
    wrong_type_offer = dict(valid_flight, duration_min="two hours")
    assert any("must be an int" in p for p in validate_offer(wrong_type_offer, "flight"))

    # None in non-nullable field
    none_non_nullable = dict(valid_flight, total_inr=None)
    assert any("is None but is not nullable" in p for p in validate_offer(none_non_nullable, "flight"))

    # Bool passed as int
    bool_as_int = dict(valid_flight, stops=True)
    assert any("must be an int" in p for p in validate_offer(bool_as_int, "flight"))

    # Non-dict offer
    assert "Offer must be a dictionary" in validate_offer("not-a-dict", "flight")[0]


HTML_3_CARDS_FIXTURE = """
<html>
<body>
  <div>
    <h2>Hotel NoPrice</h2>
    <p>Sold out / No rates available</p>
  </div>
  <c-wiz>
    <h2>Hotel Low</h2>
    <span>₹2,500</span>
  </c-wiz>
  <c-wiz>
    <h2>Hotel High</h2>
    <span>₹7,800</span>
  </c-wiz>
</body>
</html>
"""


def test_hotel_card_scoping_with_three_cards():
    hotels = live_pricing._parse_google_travel_hotels(HTML_3_CARDS_FIXTURE)
    names = [h["hotel"] for h in hotels]
    assert "Hotel NoPrice" not in names
    assert len(hotels) == 2
    assert hotels[0]["hotel"] == "Hotel Low"
    assert hotels[0]["price_per_night_inr"] == 2500
    assert hotels[1]["hotel"] == "Hotel High"
    assert hotels[1]["price_per_night_inr"] == 7800


def test_named_constants_defined():
    assert live_pricing.MIN_HOTEL_PRICE_INR == 600
    assert live_pricing.MAX_HOTEL_PRICE_INR == 300_000
    assert live_pricing.HOTEL_CANDIDATE_POOL == 20
    assert live_pricing.MAX_HOTEL_RESULTS == 4
    assert live_pricing.MAX_FLIGHT_RESULTS == 4
    assert live_pricing.CACHE_TTL_HOURS == 6
    assert live_pricing.HTTP_TIMEOUT_S == 12


@pytest.mark.parametrize("val,expected_num,should_succeed", [
    (3, 3, True),
    ("3", 3, True),
    ("  3  ", 3, True),
    (2.0, 2, True),
    (True, None, False),
    (False, None, False),
    (None, None, False),
    ("abc", None, False),
    (3.5, None, False),
    (0, None, False),
    (10, None, False),
    (-1, None, False),
    ("-1", None, False),
])
def test_validate_count_cases(val, expected_num, should_succeed):
    num, err = live_pricing._validate_count(val, "count")
    if should_succeed:
        assert err is None
        assert num == expected_num
        assert type(num) is int
    else:
        assert num is None
        assert err is not None
        assert err.get("error") == "INVALID_ARGUMENTS"


def test_card_tag_exact_cwiz_match():
    html_cwizard = """
    <html><body>
      <c-wizard>
        <h2>Hotel Wizard</h2>
        <span>₹4,000</span>
      </c-wizard>
    </body></html>
    """
    # Under card.tag == 'c-wiz', <c-wizard> without jscontroller will climb up to body and be discarded
    hotels = live_pricing._parse_google_travel_hotels(html_cwizard)
    assert len(hotels) == 0


@pytest.mark.parametrize("return_date,expected_msg", [
    ("invalid-date", "return_date must be in YYYY-MM-DD format"),
    ("2026/11/20", "return_date must be in YYYY-MM-DD format"),
    (DATE_FUTURE_1, "return_date cannot be before departure date"),
])
def test_flight_return_date_validation_errors(return_date, expected_msg):
    res = live_pricing.search_live_flights("DEL", "BOM", DATE_FUTURE_2, return_date=return_date, use_cache=False)
    assert res.get("error") == "INVALID_ARGUMENTS"
    assert expected_msg in res["message"]


@pytest.mark.parametrize("pax,is_valid,expected_count", [
    ("3", True, 3),
    (2.0, True, 2),
    (True, False, None),
])
def test_search_live_flights_validate_count_integration(monkeypatch, pax, is_valid, expected_count):
    import fast_flights
    out_item = _make_mock_flight_item()
    monkeypatch.setattr(fast_flights, "get_flights", lambda *a, **kw: [out_item])

    res = live_pricing.search_live_flights("DEL", "BOM", DATE_FUTURE_1, passengers=pax, use_cache=False)
    if is_valid:
        assert res.get("error") is None
        assert res["results"][0]["passengers"] == expected_count
    else:
        assert res.get("error") == "INVALID_ARGUMENTS"


@pytest.mark.parametrize("guests,is_valid,expected_count", [
    ("3", True, 3),
    (2.0, True, 2),
    (True, False, None),
])
def test_search_live_hotels_validate_count_integration(monkeypatch, guests, is_valid, expected_count):
    mock_hotels = [{"hotel": "Taj Palace", "price_per_night_inr": 5000, "deep_link": "http://..."}]
    monkeypatch.setattr(live_pricing, "_scrape_live_hotels_google", lambda *a, **kw: mock_hotels)

    res = live_pricing.search_live_hotels("Delhi", DATE_FUTURE_1, DATE_FUTURE_2, guests=guests, use_cache=False)
    if is_valid:
        assert res.get("error") is None
        assert res["results"][0]["guests"] == expected_count
    else:
        assert res.get("error") == "INVALID_ARGUMENTS"


def test_flights_cache_behavior(monkeypatch, tmp_path):
    import fast_flights
    test_db = tmp_path / "test_pricing_cache.db"

    out_item = _make_mock_flight_item(airline="Air India", dep_time=(8, 0), dur=120, stops=0, price=4000)
    ret_item = _make_mock_flight_item(airline="IndiGo", dep_time=(17, 0), dur=120, stops=0, price=4500)

    # (a) First call -> cached=False, second identical call -> cached=True
    monkeypatch.setattr(fast_flights, "get_flights", MagicMock(return_value=[out_item]))
    res1 = live_pricing.search_live_flights("DEL", "BOM", DATE_FUTURE_1, use_cache=True)
    assert res1.get("error") is None
    assert res1["cached"] is False

    res2 = live_pricing.search_live_flights("DEL", "BOM", DATE_FUTURE_1, use_cache=True)
    assert res2["cached"] is True
    assert res2["results"][0]["airline"] == "Air India"

    # (b) One-way and round-trip with same dates do not collide
    monkeypatch.setattr(fast_flights, "get_flights", MagicMock(side_effect=[[out_item], [ret_item]]))
    res_rt = live_pricing.search_live_flights("DEL", "BOM", DATE_FUTURE_1, return_date=DATE_FUTURE_2, use_cache=True)
    assert res_rt["cached"] is False
    assert res_rt["results"][0]["fare_basis"] == "sum_of_one_way_fares"

    # (c) Empty, error, and FlightsNotFound are never saved
    with contextlib.closing(sqlite3.connect(test_db)) as conn:
        with conn:
            conn.execute("DELETE FROM price_cache")

    # Empty
    monkeypatch.setattr(fast_flights, "get_flights", MagicMock(return_value=[]))
    res_empty = live_pricing.search_live_flights("DEL", "GOI", DATE_FUTURE_1, use_cache=True)
    assert res_empty["results"] == []

    # Error
    monkeypatch.setattr(fast_flights, "get_flights", MagicMock(side_effect=OSError("network failed")))
    res_err = live_pricing.search_live_flights("DEL", "GOI", DATE_FUTURE_1, use_cache=True)
    assert res_err.get("error") == "PROVIDER_UNAVAILABLE"

    # FlightsNotFound
    fnf_cls = getattr(fast_flights, "FlightsNotFound", Exception)
    monkeypatch.setattr(fast_flights, "get_flights", MagicMock(side_effect=fnf_cls("No flights")))
    res_fnf = live_pricing.search_live_flights("DEL", "GOI", DATE_FUTURE_1, use_cache=True)
    assert res_fnf["results"] == []

    with contextlib.closing(sqlite3.connect(test_db)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM price_cache WHERE category = 'flights'").fetchone()[0]
    assert count == 0


def test_price_heuristic_current_behaviour():
    html = """
    <html><body>
      <c-wiz>
        <h2>Hotel Discount</h2>
        <span><del>₹3,000</del> <span>₹2,500</span></span>
      </c-wiz>
    </body></html>
    """
    hotels = live_pricing._parse_google_travel_hotels(html)
    assert len(hotels) == 1
    # Current heuristic chooses min(valid_prices)
    assert hotels[0]["price_per_night_inr"] == 2500


def test_hotel_parsing_filters_and_deduplication():
    # (b) card with only ₹250 fee skipped (below floor MIN_HOTEL_PRICE_INR)
    html_cheap = """
    <html><body>
      <c-wiz>
        <h2>Hotel Cheap Fee</h2>
        <span>₹250</span>
      </c-wiz>
    </body></html>
    """
    assert live_pricing._parse_google_travel_hotels(html_cheap) == []

    # (c) duplicate hotel title deduped
    html_dup = """
    <html><body>
      <c-wiz>
        <h2>Grand Resort</h2>
        <span>₹3,500</span>
      </c-wiz>
      <c-wiz>
        <h2>Grand Resort</h2>
        <span>₹4,000</span>
      </c-wiz>
    </body></html>
    """
    hotels_dup = live_pricing._parse_google_travel_hotels(html_dup)
    assert len(hotels_dup) == 1
    assert hotels_dup[0]["hotel"] == "Grand Resort"

    # (d) "Things to do" heading excluded
    html_things = """
    <html><body>
      <c-wiz>
        <h2>Things to do in Jaipur</h2>
        <span>₹1,500</span>
      </c-wiz>
    </body></html>
    """
    assert live_pricing._parse_google_travel_hotels(html_things) == []

    # (e) HTML with no h2 -> []
    html_no_h2 = "<html><body><div><span>₹2,000</span></div></body></html>"
    assert live_pricing._parse_google_travel_hotels(html_no_h2) == []


def test_hotel_search_pool_limiting_and_total_calculation(monkeypatch):
    mock_hotels = [
        {"hotel": f"Hotel {i}", "price_per_night_inr": 2000 + i * 500, "deep_link": "http://..."}
        for i in range(6)
    ]
    monkeypatch.setattr(live_pricing, "_scrape_live_hotels_google", lambda *a, **kw: mock_hotels)

    # Exactly MAX_HOTEL_RESULTS returned
    res = live_pricing.search_live_hotels("Delhi", DATE_FUTURE_1, DATE_FUTURE_2, use_cache=False)
    assert len(res["results"]) == live_pricing.MAX_HOTEL_RESULTS

    # total_inr == rate * nights
    for offer in res["results"]:
        assert offer["total_inr"] == offer["price_per_night_inr"] * offer["nights"]
        assert offer["nights"] == 3

    # max_price_per_night below MIN_HOTEL_PRICE_INR -> clean empty result
    res_below_min = live_pricing.search_live_hotels("Delhi", DATE_FUTURE_1, DATE_FUTURE_2, max_price_per_night=500, use_cache=False)
    assert res_below_min["results"] == []
    assert "No hotels found at or below ₹500 per night" in res_below_min["message"]


def test_hotel_unfiltered_cache_reuse_across_budgets(monkeypatch):
    mock_hotels = [
        {"hotel": "Budget Inn", "price_per_night_inr": 2000, "deep_link": "http://..."},
        {"hotel": "Luxury Palace", "price_per_night_inr": 8000, "deep_link": "http://..."},
    ]
    mock_scrape = MagicMock(return_value=mock_hotels)
    monkeypatch.setattr(live_pricing, "_scrape_live_hotels_google", mock_scrape)

    # First call with budget 3000
    res1 = live_pricing.search_live_hotels("Delhi", DATE_FUTURE_1, DATE_FUTURE_2, max_price_per_night=3000, use_cache=True)
    assert res1["cached"] is False
    assert len(res1["results"]) == 1
    assert res1["results"][0]["hotel"] == "Budget Inn"
    assert mock_scrape.call_count == 1

    # Second call with different budget 10000 on same city/dates should reuse scrape from cache
    res2 = live_pricing.search_live_hotels("Delhi", DATE_FUTURE_1, DATE_FUTURE_2, max_price_per_night=10000, use_cache=True)
    assert res2["cached"] is True
    assert len(res2["results"]) == 2
    assert mock_scrape.call_count == 1, "Scraper should not be called again for same city and dates"





@pytest.mark.skipif(not FIXTURE.exists(), reason="needs real saved response")
def test_real_google_travel_sample_fixture_if_present():
    html = FIXTURE.read_text(encoding="utf-8")
    hotels = live_pricing._parse_google_travel_hotels(html)
    assert len(hotels) > 0
    for h in hotels:
        assert h["price_per_night_inr"] >= live_pricing.MIN_HOTEL_PRICE_INR
        assert h["hotel"]
