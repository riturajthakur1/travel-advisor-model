"""Unit tests for travel.live_pricing."""
import datetime as dt
from zoneinfo import ZoneInfo
import pytest

from travel import live_pricing


def test_resolve_iata_known_cities():
    assert live_pricing.resolve_iata("Delhi") == "DEL"
    assert live_pricing.resolve_iata("New Delhi") == "DEL"
    assert live_pricing.resolve_iata("Mumbai") == "BOM"
    assert live_pricing.resolve_iata("Goa") == "GOI"
    assert live_pricing.resolve_iata("North Goa") == "GOX"
    assert live_pricing.resolve_iata("Udaipur") == "UDR"
    assert live_pricing.resolve_iata("Kanpur") == "KNU"


def test_resolve_iata_rejects_unknown_and_random_3_letters():
    assert live_pricing.resolve_iata("xyz") is None
    assert live_pricing.resolve_iata("XYZ") is None
    assert live_pricing.resolve_iata("UnknownCity") is None
    assert live_pricing.resolve_iata("") is None
    assert live_pricing.resolve_iata(None) is None


def test_resolve_iata_valid_known_airport_codes():
    assert live_pricing.resolve_iata("DEL") == "DEL"
    assert live_pricing.resolve_iata("BOM") == "BOM"
    assert live_pricing.resolve_iata("GOI") == "GOI"
    assert live_pricing.resolve_iata("GOX") == "GOX"


def test_iata_canonical_mapping_no_collisions():
    assert live_pricing.IATA_TO_CANONICAL["DEL"] == "Delhi"
    assert live_pricing.IATA_TO_CANONICAL["BOM"] == "Mumbai"
    assert live_pricing.IATA_TO_CANONICAL["CJB"] == "Coimbatore"
    assert live_pricing.IATA_TO_CANONICAL["GOI"] == "Goa (Dabolim)"
    assert live_pricing.IATA_TO_CANONICAL["GOX"] == "Goa (Mopa)"


def test_flight_validation_rejects_past_dates():
    res = live_pricing.search_live_flights("DEL", "BOM", "2020-01-01", use_cache=False)
    assert "error" in res
    assert "past" in res["message"].lower()


def test_flight_validation_rejects_invalid_date_format():
    res = live_pricing.search_live_flights("DEL", "BOM", "not-a-date", use_cache=False)
    assert "error" in res
    assert "YYYY-MM-DD" in res["message"]


def test_flight_validation_rejects_unknown_airports():
    res = live_pricing.search_live_flights("NonExistentCity", "BOM", "2026-11-20", use_cache=False)
    assert res.get("error") == "UNKNOWN_AIRPORT"


def test_flight_validation_rejects_same_origin_and_dest():
    res = live_pricing.search_live_flights("DEL", "Delhi", "2026-11-20", use_cache=False)
    assert res.get("error") == "INVALID_ARGUMENTS"


def test_hotel_validation_rejects_bad_dates():
    res = live_pricing.search_live_hotels("Udaipur", "not-a-date", "2026-11-25", use_cache=False)
    assert res.get("error") == "INVALID_ARGUMENTS"

    # check_out before check_in
    res2 = live_pricing.search_live_hotels("Udaipur", "2026-11-25", "2026-11-20", use_cache=False)
    assert res2.get("error") == "INVALID_ARGUMENTS"
    assert "strictly after" in res2["message"]


def test_hotel_budget_cap_under_all_options_returns_clean_empty():
    # Searching with max_price_per_night=500 (lower than any valid hotel)
    res = live_pricing.search_live_hotels("Udaipur", "2026-11-20", "2026-11-23", max_price_per_night=500, use_cache=False)
    assert "results" in res
    assert len(res["results"]) == 0
    assert "message" in res


def test_offer_ids_include_date_to_prevent_cross_date_collisions():
    offer1 = live_pricing._build_hotel_offer("H-UDR-20261120-1", "Hotel A", "Udaipur", "Deluxe", 2000, 2, 2, "2026-11-20", "2026-11-22", [], "http://...", True)
    offer2 = live_pricing._build_hotel_offer("H-UDR-20261210-1", "Hotel A", "Udaipur", "Deluxe", 2000, 2, 2, "2026-12-10", "2026-12-12", [], "http://...", True)
    assert offer1["offer_id"] != offer2["offer_id"]


@pytest.fixture(autouse=True)
def mock_cache_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_pricing_cache.db"
    monkeypatch.setattr(live_pricing, "CACHE_DB_PATH", test_db)
    live_pricing._init_cache_db()


def test_flight_provider_failure_returns_provider_unavailable_without_fake_data(monkeypatch):
    import fast_flights
    def raise_err(*args, **kwargs):
        raise ConnectionError("Network down")
    monkeypatch.setattr(fast_flights, "get_flights", raise_err)

    res = live_pricing.search_live_flights("DEL", "BOM", "2026-11-20", use_cache=False)
    assert res.get("error") == "PROVIDER_UNAVAILABLE"
    assert "IndiGo" not in str(res)


def test_flight_empty_results_returns_real_empty_structure(monkeypatch):
    import fast_flights
    monkeypatch.setattr(fast_flights, "get_flights", lambda *a, **kw: [])

    res = live_pricing.search_live_flights("DEL", "BOM", "2026-11-20", use_cache=False)
    assert "error" not in res
    assert res["results"] == []
    assert "checked_at" in res
    assert "No flights found" in res["message"]
    assert res["cached"] is False
    assert "IndiGo" not in str(res)


def test_hotel_provider_failure_returns_provider_unavailable_without_fake_data(monkeypatch):
    def raise_err(*args, **kwargs):
        raise ConnectionError("Google Travel down")
    monkeypatch.setattr(live_pricing, "_scrape_live_hotels_google", raise_err)

    res = live_pricing.search_live_hotels("Udaipur", "2026-11-20", "2026-11-23", use_cache=False)
    assert res.get("error") == "PROVIDER_UNAVAILABLE"
    assert "Grand Residency" not in str(res)
    assert "Heritage Haveli" not in str(res)


def test_hotel_empty_results_returns_real_empty_structure(monkeypatch):
    monkeypatch.setattr(live_pricing, "_scrape_live_hotels_google", lambda *a, **kw: [])

    res = live_pricing.search_live_hotels("Udaipur", "2026-11-20", "2026-11-23", use_cache=False)
    assert "error" not in res
    assert res["results"] == []
    assert "checked_at" in res
    assert "No hotels found" in res["message"]
    assert res["cached"] is False
    assert "Grand Residency" not in str(res)


def test_live_flight_offers_contain_no_invented_fields(monkeypatch):
    from unittest.mock import MagicMock
    import fast_flights

    mock_dep = MagicMock()
    mock_dep.time = (10, 30)
    mock_flight = MagicMock()
    mock_flight.departure = mock_dep
    mock_flight.duration = 120

    mock_result = MagicMock()
    mock_result.flights = [mock_flight]
    mock_result.airlines = ["IndiGo"]
    mock_result.price = 4500

    monkeypatch.setattr(fast_flights, "get_flights", lambda *a, **kw: [mock_result])

    res = live_pricing.search_live_flights("DEL", "BOM", "2026-11-20", use_cache=False)
    assert len(res["results"]) > 0
    for offer in res["results"]:
        assert offer.get("baggage") is None
        assert offer.get("fare_rule") is None


def test_live_hotel_offers_contain_no_invented_fields(monkeypatch):
    mock_hotels = [{
        "hotel": "Lake Palace",
        "price_per_night_inr": 15000,
        "deep_link": "https://google.com/travel/..."
    }]
    monkeypatch.setattr(live_pricing, "_scrape_live_hotels_google", lambda *a, **kw: mock_hotels)

    res = live_pricing.search_live_hotels("Udaipur", "2026-11-20", "2026-11-23", use_cache=False)
    assert len(res["results"]) > 0
    for offer in res["results"]:
        assert offer.get("room") is None
        assert offer.get("amenities") is None
        assert offer.get("rate_rule") is None
        assert offer.get("taxes_included") is None
        assert offer.get("taxes_estimate_inr") is None


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
    # Test hand-written 3-card fixture:
    # 1. Hotel NoPrice: has no price, must be skipped and NOT inherit neighbour's price
    # 2. Hotel Low: price 2500
    # 3. Hotel High: price 7800
    hotels = live_pricing._parse_google_travel_hotels(HTML_3_CARDS_FIXTURE)
    names = [h["hotel"] for h in hotels]
    assert "Hotel NoPrice" not in names
    assert len(hotels) == 2
    assert hotels[0]["hotel"] == "Hotel Low"
    assert hotels[0]["price_per_night_inr"] == 2500
    assert hotels[1]["hotel"] == "Hotel High"
    assert hotels[1]["price_per_night_inr"] == 7800


# NOTE: Save one real Google Travel response as `tests/fixtures/google_travel_sample.html`
# and this test will validate parser behavior against real-world Google Travel markup.
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


def _make_mock_flight_item(airline="IndiGo", dep_time=(11, 0), dur=120, stops=0, price=4000):
    from unittest.mock import MagicMock
    legs = []
    for _ in range(stops + 1):
        leg = MagicMock()
        leg.departure = MagicMock(time=dep_time)
        leg.duration = dur // (stops + 1)
        legs.append(leg)
    item = MagicMock()
    item.flights = legs
    item.airlines = [airline]
    item.price = price
    return item


def test_roundtrip_both_ok_includes_fare_basis_and_return_stops(monkeypatch):
    import fast_flights

    out_item = _make_mock_flight_item(airline="Air India", dep_time=(8, 0), dur=130, stops=0, price=5000)
    ret_item = _make_mock_flight_item(airline="IndiGo", dep_time=(18, 0), dur=140, stops=1, price=4500)

    def mock_get_flights(query):
        # Determine whether it's outbound or return
        # Outbound query has DEL -> BOM; Return has BOM -> DEL
        if "DEL" in str(query.flight_data[0].from_airport):
            return [out_item]
        else:
            return [ret_item]

    monkeypatch.setattr(fast_flights, "get_flights", mock_get_flights)

    res = live_pricing.search_live_flights("DEL", "BOM", "2026-11-20", passengers=1, return_date="2026-11-25", use_cache=False)
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

    res = live_pricing.search_live_flights("DEL", "BOM", "2026-11-20", passengers=1, return_date="2026-11-25", use_cache=False)
    assert res.get("error") is None
    assert res["results"] == []
    assert "No return flights found" in res["message"] or "No flights found" in res["message"]
    assert "IndiGo" not in str(res["results"])


def test_roundtrip_out_ok_return_raises_returns_provider_error(monkeypatch):
    import fast_flights

    out_item = _make_mock_flight_item()

    def mock_get_flights(query):
        if "DEL" in str(query.flight_data[0].from_airport):
            return [out_item]
        raise ConnectionError("Return flight endpoint unreachable")

    monkeypatch.setattr(fast_flights, "get_flights", mock_get_flights)

    res = live_pricing.search_live_flights("DEL", "BOM", "2026-11-20", passengers=1, return_date="2026-11-25", use_cache=False)
    assert res.get("error") == "PROVIDER_UNAVAILABLE"
    assert "results" not in res


def test_programming_error_in_flight_processing_is_not_disguised_as_provider_offline(monkeypatch):
    from unittest.mock import MagicMock
    import fast_flights

    # Return a malformed object that causes an AttributeError or TypeError during post-processing
    bad_item = MagicMock()
    bad_item.flights = "not-a-list-of-legs"  # causing len() or index error
    bad_item.airlines = ["IndiGo"]
    bad_item.price = 4500

    monkeypatch.setattr(fast_flights, "get_flights", lambda *a, **kw: [bad_item])

    with pytest.raises((TypeError, AttributeError)):
        live_pricing.search_live_flights("DEL", "BOM", "2026-11-20", use_cache=False)


def test_calc_elapsed_minutes_with_layover():
    from unittest.mock import MagicMock
    leg1 = MagicMock()
    leg1.departure = MagicMock(date=(2026, 11, 20), time=(10, 0))
    leg1.arrival = MagicMock(date=(2026, 11, 20), time=(12, 0))
    leg1.duration = 120

    # 1.5 hour layover between 12:00 and 13:30
    leg2 = MagicMock()
    leg2.departure = MagicMock(date=(2026, 11, 20), time=(13, 30))
    leg2.arrival = MagicMock(date=(2026, 11, 20), time=(15, 30))
    leg2.duration = 120

    # Total elapsed should be 10:00 to 15:30 = 330 minutes (not 120 + 120 = 240)
    dur = live_pricing._calc_elapsed_minutes([leg1, leg2])
    assert dur == 330


def test_fast_flights_missing_handled_gracefully(monkeypatch):
    monkeypatch.setattr(live_pricing, "fast_flights", None)
    res = live_pricing.search_live_flights("DEL", "BOM", "2026-11-20", use_cache=False)
    assert res.get("error") == "PROVIDER_UNAVAILABLE"
    assert "fast-flights" in res["message"] or "unavailable" in res["message"].lower()





