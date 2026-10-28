"""Unit tests for travel.live_pricing."""
import datetime as dt
import sqlite3
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo
import pytest

from travel import live_pricing
from travel.offer_schemas import validate_offer

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
    with sqlite3.connect(test_db) as conn:
        conn.execute("UPDATE price_cache SET expires_at = datetime('now', '-1 hour')")

    # 4. Third call -> Expired -> Cache Miss
    res3 = live_pricing.search_live_hotels("Delhi", DATE_FUTURE_1, DATE_FUTURE_2, use_cache=True)
    assert res3["cached"] is False


def test_error_and_provider_failures_are_never_cached(monkeypatch, tmp_path):
    monkeypatch.setattr(live_pricing, "_scrape_live_hotels_google", MagicMock(side_effect=ConnectionError("fail")))

    res = live_pricing.search_live_hotels("Delhi", DATE_FUTURE_1, DATE_FUTURE_2, use_cache=True)
    assert res.get("error") == "PROVIDER_UNAVAILABLE"

    test_db = tmp_path / "test_pricing_cache.db"
    with sqlite3.connect(test_db) as conn:
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
    legs = []
    for _ in range(stops + 1):
        leg = MagicMock()
        leg.departure = MagicMock(date=(2026, 11, 20), time=dep_time)
        leg.arrival = MagicMock(date=(2026, 11, 20), time=(dep_time[0] + dur // 60, dep_time[1] + dur % 60))
        leg.duration = dur // (stops + 1)
        legs.append(leg)
    item = MagicMock()
    item.flights = legs
    item.airlines = [airline]
    item.price = price
    return item


def test_flight_parsing_with_fake_fast_flights(monkeypatch):
    import fast_flights

    out_item = _make_mock_flight_item(airline="Vistara", dep_time=(6, 45), dur=135, stops=0, price=5200)
    monkeypatch.setattr(fast_flights, "get_flights", lambda *a, **kw: [out_item])

    res = live_pricing.search_live_flights("DEL", "BOM", DATE_FUTURE_1, passengers=2, use_cache=False)
    assert res.get("error") is None
    assert len(res["results"]) == 1
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

def test_roundtrip_both_ok_includes_fare_basis_and_return_stops(monkeypatch):
    import fast_flights

    out_item = _make_mock_flight_item(airline="Air India", dep_time=(8, 0), dur=130, stops=0, price=5000)
    ret_item = _make_mock_flight_item(airline="IndiGo", dep_time=(18, 0), dur=140, stops=1, price=4500)

    def mock_get_flights(query):
        if "DEL" in str(query.flight_data[0].from_airport):
            return [out_item]
        else:
            return [ret_item]

    monkeypatch.setattr(fast_flights, "get_flights", mock_get_flights)

    res = live_pricing.search_live_flights("DEL", "BOM", DATE_FUTURE_1, passengers=1, return_date=DATE_FUTURE_2, use_cache=False)
    assert "results" in res
    assert len(res["results"]) == 1
    offer = res["results"][0]
    assert offer["fare_basis"] == "sum_of_one_way_fares"
    assert offer["return_stops"] == 1
    assert offer["stops"] == 0
    assert offer["fare_per_person_inr"] == 9500
    assert "tfs=" in offer["deep_link"]


def test_roundtrip_out_ok_return_empty_returns_empty_results(monkeypatch):
    import fast_flights

    out_item = _make_mock_flight_item()

    def mock_get_flights(query):
        if "DEL" in str(query.flight_data[0].from_airport):
            return [out_item]
        return []

    monkeypatch.setattr(fast_flights, "get_flights", mock_get_flights)

    res = live_pricing.search_live_flights("DEL", "BOM", DATE_FUTURE_1, passengers=1, return_date=DATE_FUTURE_2, use_cache=False)
    assert res.get("error") is None
    assert res["results"] == []
    assert "No return flights found" in res["message"] or "No flights found" in res["message"]


def test_roundtrip_out_ok_return_raises_returns_provider_error(monkeypatch):
    import fast_flights

    out_item = _make_mock_flight_item()

    def mock_get_flights(query):
        if "DEL" in str(query.flight_data[0].from_airport):
            return [out_item]
        raise ConnectionError("Return flight endpoint unreachable")

    monkeypatch.setattr(fast_flights, "get_flights", mock_get_flights)

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
    def mock_get_flights(query):
        if "DEL" in str(query.flight_data[0].from_airport):
            return [out_item]
        return [ret_item]
    monkeypatch.setattr(fast_flights, "get_flights", mock_get_flights)
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


@pytest.mark.skip(reason="needs real saved response")
def test_real_google_travel_sample_fixture_if_present():
    import pathlib
    fixture_path = pathlib.Path("tests/fixtures/google_travel_sample.html")
    if not fixture_path.exists():
        return
    html = fixture_path.read_text(encoding="utf-8")
    hotels = live_pricing._parse_google_travel_hotels(html)
    assert len(hotels) > 0
    for h in hotels:
        assert h["price_per_night_inr"] >= 600
        assert h["hotel"]
