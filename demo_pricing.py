"""Interactive demo of live travel pricing for Indian trips.

Run this script to test live pricing:
    uv run python demo_pricing.py
"""
import datetime as dt
import time
from zoneinfo import ZoneInfo

from travel.live_pricing import search_live_flights, search_live_hotels


EXPECTED_FLIGHT_KEYS = {
    "offer_id", "airline", "from", "to", "date", "departs", "duration_min",
    "stops", "fare_per_person_inr", "passengers", "total_inr", "baggage", "fare_rule"
}

EXPECTED_HOTEL_KEYS = {
    "offer_id", "hotel", "city", "room", "price_per_night_inr", "nights",
    "guests", "check_in", "check_out", "amenities", "rate_rule", "total_inr"
}


def print_divider(title: str):
    print("\n" + "=" * 75)
    print(f"  {title}")
    print("=" * 75)


def validate_offer_schema(offers, expected_keys, category_name):
    if not offers:
        print(f"ℹ️  No {category_name} offers to validate.")
        return True

    all_valid = True
    for idx, offer in enumerate(offers, 1):
        if not isinstance(offer, dict):
            print(f"❌ {category_name} offer [{idx}] is not a dictionary: {offer}")
            all_valid = False
            continue

        missing = expected_keys - set(offer.keys())
        if missing:
            print(f"❌ {category_name} offer [{idx}] ({offer.get('offer_id')}) missing keys: {missing}")
            all_valid = False

    return all_valid


def main():
    today_ist = dt.datetime.now(ZoneInfo("Asia/Kolkata")).date()
    date_flight = (today_ist + dt.timedelta(days=30)).isoformat()
    check_in = (today_ist + dt.timedelta(days=30)).isoformat()
    check_out = (today_ist + dt.timedelta(days=33)).isoformat()

    print_divider("TRAVEL ADVISOR - LIVE PRICING ENGINE DEMO")
    print(f"Today (Asia/Kolkata): {today_ist}")
    print("Testing live domestic pricing with real data and honest failure/cache reporting.\n")

    # 1. LIVE FLIGHT TEST
    origin = "Delhi"
    destination = "Goa"
    passengers = 1

    print(f"✈️  [TEST 1] Searching LIVE flights from {origin} to {destination} on {date_flight} ({passengers} passenger)...")
    t0 = time.perf_counter()
    flight_data = search_live_flights(origin, destination, date_flight, passengers=passengers, use_cache=False)
    elapsed = time.perf_counter() - t0

    if "error" in flight_data:
        print(f"❌ Flight search failed: [{flight_data.get('error')}] {flight_data.get('message')}")
    elif not flight_data.get("results"):
        print(f"ℹ️  No flights found: {flight_data.get('message', 'Empty results')}")
        print(f"⏱️ Checked at: {flight_data.get('checked_at')}")
    else:
        print(f"✅ Found {len(flight_data['results'])} live flight offers in {elapsed:.2f} seconds!")
        print(f"📡 Source: {flight_data.get('source')}")
        print(f"⏱️ Checked at: {flight_data.get('checked_at')}\n")

        for idx, f in enumerate(flight_data.get("results", []), 1):
            if not f.get("is_live"):
                print("  ⚠️ WARNING: Result is marked not live!")
            stops_text = "Non-stop" if f.get("stops") == 0 else f"{f.get('stops')} stop"
            print(f"  [{idx}] {f.get('airline')} ({f.get('from')} ➔ {f.get('to')}) · {stops_text}")
            print(f"      Departure: {f.get('departs')} | Duration: {f.get('duration_min')} mins")
            print(f"      Price: ₹{f.get('fare_per_person_inr', 0):,} per person (Total: ₹{f.get('total_inr', 0):,})")
            print(f"      Baggage: {f.get('baggage')} | Fare rule: {f.get('fare_rule')}")
            if "deep_link" in f:
                print(f"      🔗 Verified Live Link: {f.get('deep_link')}")
            print()

    # 2. CACHING SPEED TEST
    print_divider("⚡ [TEST 2] SMART CACHE VERIFICATION")
    print(f"Re-querying exact search ({origin} -> {destination} on {date_flight})...")
    t0 = time.perf_counter()
    cached_result = search_live_flights(origin, destination, date_flight, passengers=passengers, use_cache=True)
    cache_elapsed = time.perf_counter() - t0

    if cached_result.get("cached") is True:
        print(f"✅ Cache hit! Returned in {cache_elapsed * 1000:.2f} ms")
        print("💡 Cached response returned with zero network latency.")
    else:
        print(f"ℹ️  Cache miss or not cached: {cached_result.get('message', 'New request performed')}")

    # 3. LIVE HOTEL SEARCH TEST
    print_divider("🏨 [TEST 3] LIVE HOTEL SEARCH (GOOGLE TRAVEL)")
    city = "Udaipur"
    guests = 2

    print(f"Searching LIVE hotels in {city} ({check_in} to {check_out}, {guests} guests)...")
    t0 = time.perf_counter()
    hotel_data = search_live_hotels(city, check_in, check_out, guests=guests, use_cache=False)
    elapsed_hotel = time.perf_counter() - t0

    if "error" in hotel_data:
        print(f"❌ Hotel search failed: [{hotel_data.get('error')}] {hotel_data.get('message')}")
    elif not hotel_data.get("results"):
        print(f"ℹ️  No hotels found: {hotel_data.get('message', 'Empty results')}")
        print(f"⏱️ Checked at: {hotel_data.get('checked_at')}")
    else:
        print(f"✅ Found {len(hotel_data['results'])} live hotel offers in {elapsed_hotel:.2f} seconds!")
        print(f"📡 Source: {hotel_data.get('source')}\n")

        for idx, h in enumerate(hotel_data.get("results", []), 1):
            if not h.get("is_live"):
                print("  ⚠️ WARNING: Result is marked not live!")
            print(f"  [{idx}] {h.get('hotel')} ({h.get('city')})")
            print(f"      Rate: ₹{h.get('price_per_night_inr', 0):,} / night | Total for {h.get('nights')} nights: ₹{h.get('total_inr', 0):,}")
            print(f"      Room: {h.get('room')} | Amenities: {h.get('amenities')} | Cancellation: {h.get('rate_rule')}")
            if "deep_link" in h:
                print(f"      🔗 Live Google Travel Link: {h.get('deep_link')}")
            print()

    # 4. COMPATIBILITY AUDIT WITH AI TOOL CONTRACT
    print_divider("🔍 [TEST 4] COMPATIBILITY AUDIT WITH AI TOOL CONTRACT")
    flight_offers = flight_data.get("results", []) if isinstance(flight_data, dict) else []
    hotel_offers = hotel_data.get("results", []) if isinstance(hotel_data, dict) else []

    flight_valid = validate_offer_schema(flight_offers, EXPECTED_FLIGHT_KEYS, "Flight")
    hotel_valid = validate_offer_schema(hotel_offers, EXPECTED_HOTEL_KEYS, "Hotel")

    if flight_valid and hotel_valid and (flight_offers or hotel_offers):
        print(f"🎉 100% of examined live offers match expected schema ({len(flight_offers)} flights, {len(hotel_offers)} hotels).")
    elif not flight_offers and not hotel_offers:
        print("ℹ️  No live offers were returned to audit schema (providers may be offline or returned zero results).")
    else:
        print("⚠️  Schema mismatches were detected in offers above.")

    print("\n" + "=" * 75)


if __name__ == "__main__":
    main()
