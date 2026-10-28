import logging
from unittest.mock import MagicMock
import pytest

from travel import backend, harness, live_pricing


@pytest.fixture
def live_hotel_offer(monkeypatch):
    mock_hotels = [{"hotel": "Taj Lake Palace", "price_per_night_inr": 12000, "deep_link": "https://google.com/travel/..."}]
    monkeypatch.setattr(live_pricing, "_scrape_live_hotels_google", lambda *a, **kw: mock_hotels)
    res = live_pricing.search_live_hotels("Udaipur", "2026-12-01", "2026-12-03", use_cache=False)
    assert res.get("results")
    offer = res["results"][0]
    # Verify the live hotel offer has all the unknown/None fields as produced by live_pricing
    assert offer.get("room") is None
    assert offer.get("amenities") is None
    assert offer.get("rate_rule") is None
    assert offer.get("taxes_included") is None
    assert offer.get("taxes_estimate_inr") is None
    return offer


def test_backend_booking_validation_with_none_taxes_does_not_raise(live_hotel_offer):
    """a) backend booking validation of that offer does NOT raise (the backend.py:154 crash)
    and expects total == offer['total_inr'] when taxes are unknown."""
    b = backend.Backend(config={}, seed=42)
    b.offers[live_hotel_offer["offer_id"]] = live_hotel_offer

    # Calling create_booking with total_price_inr == offer["total_inr"] must not crash with TypeError
    res = b.create_booking(
        offer_id=live_hotel_offer["offer_id"],
        travelers=2,
        total_price_inr=live_hotel_offer["total_inr"],
        customer_confirmed_terms=True,
    )
    assert res.get("status") == "CONFIRMED"
    assert res.get("charged_inr") == live_hotel_offer["total_inr"]


def test_harness_summary_no_literal_none(live_hotel_offer):
    """b) harness offer summary text contains no literal 'None'."""
    text = harness.summary(live_hotel_offer, travellers=2)
    assert "None" not in text


def test_harness_summary_taxes_included_none_not_taxes_included(live_hotel_offer):
    """c) with taxes_included=None the summary does NOT say 'taxes included';
    it says tax treatment is not confirmed."""
    assert live_hotel_offer.get("taxes_included") is None
    text = harness.summary(live_hotel_offer, travellers=2)
    assert "taxes included" not in text
    assert "tax treatment is not confirmed" in text


def test_harness_summary_taxes_included_false_without_estimate(live_hotel_offer):
    """d) taxes_included=False with taxes_estimate_inr=None does not raise and says extra
    taxes apply, amount not confirmed."""
    offer = dict(live_hotel_offer, taxes_included=False, taxes_estimate_inr=None)
    text = harness.summary(offer, travellers=2)
    assert "taxes are extra; the amount is not confirmed" in text


def test_harness_summary_taxes_included_states(live_hotel_offer):
    """e) taxes_included=False with an estimate -> 'includes about <amount> estimated taxes'
    (existing behaviour); taxes_included=True -> 'taxes included' (existing behaviour)."""
    offer_est = dict(live_hotel_offer, taxes_included=False, taxes_estimate_inr=1500)
    text_est = harness.summary(offer_est, travellers=2)
    assert "includes about ₹1,500 estimated taxes" in text_est

    offer_inc = dict(live_hotel_offer, taxes_included=True, taxes_estimate_inr=None)
    text_inc = harness.summary(offer_inc, travellers=2)
    assert "taxes included" in text_inc


def test_harness_summary_room_handling(live_hotel_offer):
    """f) room=None -> product text is just the hotel name; room='Deluxe' -> 'Hotel, Deluxe'."""
    offer_none = dict(live_hotel_offer, room=None)
    text_none = harness.summary(offer_none, travellers=2)
    assert "- Taj Lake Palace\n" in text_none

    offer_room = dict(live_hotel_offer, room="Deluxe")
    text_room = harness.summary(offer_room, travellers=2)
    assert "- Taj Lake Palace, Deluxe\n" in text_room


def test_flight_offer_with_none_fields_regression_guard():
    """Regression guard (not proof): verifies that flight offer summaries do not
    leak 'None' when optional fields like baggage and fare_rule are None.
    Note: baggage is not currently formatted into customer-facing text in harness.py
    (only fare_rule is resolved via _rule()), so this test guards future formatters
    from inadvertently displaying 'None' if baggage is integrated into the summary."""
    flight_offer = {
        "offer_id": "F-1",
        "airline": "Air India",
        "from": "DEL",
        "to": "BOM",
        "date": "2026-12-01",
        "departs": "08:00",
        "duration_min": 130,
        "stops": 0,
        "fare_per_person_inr": 5000,
        "passengers": 1,
        "total_inr": 5000,
        "baggage": None,
        "fare_rule": None,
        "is_live": True,
        "deep_link": "https://google.com/travel/flights",
    }
    text = harness.summary(flight_offer, travellers=1)
    assert "None" not in text


def test_backend_call_tool_exception_returns_internal_error(caplog):
    """h) backend.call: a read tool function that raises returns
    {'error': 'INTERNAL_ERROR', 'message': <generic, no traceback or exception text>}
    and logs via logger.exception; the turn does not crash."""
    b = backend.Backend(config={}, seed=42)

    def exploding_tool(**kwargs):
        raise RuntimeError("database connection crashed with secret_traceback_token")

    b.exploding_tool = exploding_tool

    with caplog.at_level(logging.ERROR):
        res = b.call("exploding_tool", {})

    assert res.get("error") == "INTERNAL_ERROR"
    assert "database connection crashed" not in str(res)
    assert "secret_traceback_token" not in str(res)
    assert "traceback" not in str(res).lower()
    assert res.get("message") == "The service encountered an error while processing the request"

    # Ensure logger.exception was recorded
    assert any("secret_traceback_token" in record.message or (record.exc_info and "secret_traceback_token" in str(record.exc_info))
               for record in caplog.records)


def test_backend_call_write_tool_exception_warns_outcome_not_confirmed(caplog):
    """Step 4: backend.call for create_booking (write tool) returns an INTERNAL_ERROR
    message stating that the outcome is not confirmed and that booking status must be
    checked before retrying, without leaking any exception traceback."""
    b = backend.Backend(config={}, seed=42)

    def exploding_create_booking(**kwargs):
        raise RuntimeError("network dropped during gateway commit with secret_payment_token")

    b.create_booking = exploding_create_booking

    with caplog.at_level(logging.ERROR):
        res = b.call("create_booking", {"offer_id": "H-1", "travelers": 2, "total_price_inr": 10000, "customer_confirmed_terms": True})

    assert res.get("error") == "INTERNAL_ERROR"
    assert "network dropped" not in str(res)
    assert "secret_payment_token" not in str(res)
    assert "traceback" not in str(res).lower()
    msg = res.get("message", "").lower()
    assert "not confirmed" in msg
    assert "check" in msg
    assert "before retrying" in msg


def test_integration_live_hotel_and_booking_through_backend_call(monkeypatch):
    """Step 2: Integration test going through backend.call for search_hotels with
    mocked scraper, then create_booking with correct total and all unknown fields None.
    Asserts no exception, confirmed status, and no literal 'None' in customer text."""
    mock_hotels = [{"hotel": "Taj Lake Palace", "price_per_night_inr": 12000, "deep_link": "https://google.com/travel/..."}]
    monkeypatch.setattr(live_pricing, "_scrape_live_hotels_google", lambda *a, **kw: mock_hotels)

    b = backend.Backend(config={"live_pricing": True}, seed=42)

    # 1. search_hotels through backend.call
    search_payload = b.call("search_hotels", {
        "city": "Udaipur",
        "check_in": "2026-12-01",
        "check_out": "2026-12-03",
        "guests": 2
    })
    assert search_payload.get("error") is None
    assert len(search_payload["results"]) == 1
    offer = search_payload["results"][0]
    assert offer.get("room") is None
    assert offer.get("amenities") is None
    assert offer.get("rate_rule") is None
    assert offer.get("taxes_included") is None
    assert offer.get("taxes_estimate_inr") is None

    # Summary customer-facing text check
    summary_text = harness.summary(offer, travellers=2)
    assert "None" not in summary_text
    assert "Taj Lake Palace" in summary_text

    # 2. create_booking through backend.call
    booking_res = b.call("create_booking", {
        "offer_id": offer["offer_id"],
        "travelers": 2,
        "total_price_inr": offer["total_inr"],
        "customer_confirmed_terms": True,
    })
    assert booking_res.get("error") is None
    assert booking_res.get("status") in ("CONFIRMED", "PENDING_SUPPLIER_CONFIRMATION")
    assert booking_res.get("charged_inr") == offer["total_inr"]

    # Customer-facing status text check
    confirmed_text = harness.status_text(booking_res, offer)
    assert "None" not in confirmed_text
    assert "TRV-" in confirmed_text or "REQ-" in confirmed_text


def test_integration_live_flight_and_booking_through_backend_call(monkeypatch):
    """Step 2: Integration test going through backend.call for search_flights with
    mocked flight results, then create_booking with correct total.
    Asserts no exception, confirmed status, and no literal 'None' in customer text."""
    import datetime as dt
    import fast_flights
    from tests.test_live_pricing import _make_mock_flight_item

    mock_item = _make_mock_flight_item(airline="Vistara", dep_time=(6, 45), dur=135, stops=0, price=5200)
    monkeypatch.setattr(fast_flights, "get_flights", lambda *a, **kw: [mock_item])

    b = backend.Backend(config={"live_pricing": True}, seed=42)

    # 1. search_flights through backend.call
    search_payload = b.call("search_flights", {
        "origin": "DEL",
        "destination": "BOM",
        "date": "2026-12-01",
        "passengers": 1
    })
    assert search_payload.get("error") is None
    assert len(search_payload["results"]) == 1
    offer = search_payload["results"][0]
    assert offer.get("baggage") is None
    assert offer.get("fare_rule") is None

    # Summary customer-facing text check
    summary_text = harness.summary(offer, travellers=1)
    assert "None" not in summary_text
    assert "Vistara" in summary_text

    # 2. create_booking through backend.call
    booking_res = b.call("create_booking", {
        "offer_id": offer["offer_id"],
        "travelers": 1,
        "total_price_inr": offer["total_inr"],
        "customer_confirmed_terms": True,
    })
    assert booking_res.get("error") is None
    assert booking_res.get("status") in ("CONFIRMED", "PENDING_SUPPLIER_CONFIRMATION")
    assert booking_res.get("charged_inr") == offer["total_inr"]

    # Customer-facing status text check
    confirmed_text = harness.status_text(booking_res, offer)
    assert "None" not in confirmed_text
    assert "TRV-" in confirmed_text or "REQ-" in confirmed_text
