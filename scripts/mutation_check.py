"""Mutation testing self-check script for travel.live_pricing.

Applies 14 targeted mutations to travel/live_pricing.py one at a time, runs the
offline unit test suite (tests/test_live_pricing.py), and reports whether each
mutation was CAUGHT or SURVIVED, guaranteeing original file restoration.
"""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
TARGET_FILE = ROOT / "travel" / "live_pricing.py"

MUTATIONS = [
    {
        "id": 1,
        "name": "card-scoping check allows body/html",
        "target": 'if card is None or card.tag in ("body", "html"):',
        "replacement": "if card is None:",
    },
    {
        "id": 2,
        "name": "flight cache key drops return_date",
        "target": 'cache_key = f"flights:{orig_code}:{dest_code}:{date}:{return_date or \'oneway\'}:{pax}"',
        "replacement": 'cache_key = f"flights:{orig_code}:{dest_code}:{date}:{\'oneway\'}:{pax}"',
    },
    {
        "id": 3,
        "name": "min(valid_prices) -> max",
        "target": "price_val = min(valid_prices)",
        "replacement": "price_val = max(valid_prices)",
    },
    {
        "id": 4,
        "name": "return_date < date check disabled",
        "target": 'if d_ret < d_out:\n            return {"error": "INVALID_ARGUMENTS", "message": "return_date cannot be before departure date"}',
        "replacement": 'if False and d_ret < d_out:\n            return {"error": "INVALID_ARGUMENTS", "message": "return_date cannot be before departure date"}',
    },
    {
        "id": 5,
        "name": "FlightsNotFound no longer handled",
        "target": "except flights_not_found as e:",
        "replacement": "except () as e:",
    },
    {
        "id": 6,
        "name": "hotel result cap removed",
        "target": "if len(results) >= MAX_HOTEL_RESULTS:\n            break",
        "replacement": "if False and len(results) >= MAX_HOTEL_RESULTS:\n            break",
    },
    {
        "id": 7,
        "name": "cap below MIN_HOTEL_PRICE_INR early-return disabled",
        "target": 'if cap < MIN_HOTEL_PRICE_INR:\n            return {"checked_at": now_iso, "results": [], "message": f"No hotels found at or below ₹{int(cap)} per night"}',
        "replacement": 'if False and cap < MIN_HOTEL_PRICE_INR:\n            return {"checked_at": now_iso, "results": [], "message": f"No hotels found at or below ₹{int(cap)} per night"}',
    },
    {
        "id": 8,
        "name": "flights never cached",
        "target": '_save_to_cache(cache_key, "flights", payload, ttl_hours=CACHE_TTL_HOURS)',
        "replacement": "pass",
    },
    {
        "id": 9,
        "name": "passenger multiplication removed",
        "target": "total = fare_per_person * pax",
        "replacement": "total = fare_per_person",
    },
    {
        "id": 10,
        "name": "hotel total ignores nights",
        "target": "total = price_per_night * nights",
        "replacement": "total = price_per_night",
    },
    {
        "id": 11,
        "name": "price floor set to 0",
        "target": "MIN_HOTEL_PRICE_INR = 600",
        "replacement": "MIN_HOTEL_PRICE_INR = 0",
    },
    {
        "id": 12,
        "name": "duplicate-title dedupe disabled",
        "target": "seen.add(title.lower())",
        "replacement": "pass",
    },
    {
        "id": 13,
        "name": "zero-cards -> returns empty success instead of PROVIDER_RESPONSE_UNREADABLE",
        "target": '"error": "PROVIDER_RESPONSE_UNREADABLE",\n                "message": "Could not read hotel results from the provider page; the layout may have changed or the request was blocked."',
        "replacement": '"checked_at": now_iso,\n                "results": [],\n                "source": "Google Travel Hotels (Live)",\n                "cached": False',
    },
    {
        "id": 14,
        "name": "FlightsNotFound mapped to a plain empty 'No flights found' message",
        "target": '"message": "Google returned no flight results for this search. This can mean no flights are available or a temporary provider error; check the search link.",',
        "replacement": '"message": f"No flights found from {orig_code} to {dest_code} on {date}",',
    },
]


def run_mutation_check():
    original_code = TARGET_FILE.read_text(encoding="utf-8")
    results = []

    print("=" * 75)
    print("  MUTATION TESTING SELF-CHECK (14 Mutations)")
    print("=" * 75)

    try:
        for m in MUTATIONS:
            m_id = m["id"]
            name = m["name"]
            target = m["target"]
            replacement = m["replacement"]

            if target not in original_code:
                print(f"❌ [CONFIG ERROR] Mutation #{m_id} target string not found in {TARGET_FILE.name}")
                results.append((m_id, name, "TARGET_NOT_FOUND"))
                continue

            # Apply mutation
            mutated = original_code.replace(target, replacement)
            TARGET_FILE.write_text(mutated, encoding="utf-8")

            # Run test suite offline
            proc = subprocess.run(
                ["uv", "run", "pytest", "tests/test_live_pricing.py", "-q"],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )

            # If tests failed, mutation was CAUGHT; if tests passed, it SURVIVED
            if proc.returncode != 0:
                print(f"  ✅ [CAUGHT]   Mutation #{m_id:2d}: {name}")
                results.append((m_id, name, "CAUGHT"))
            else:
                print(f"  ❌ [SURVIVED] Mutation #{m_id:2d}: {name}")
                results.append((m_id, name, "SURVIVED"))

            # Restore original code after each run
            TARGET_FILE.write_text(original_code, encoding="utf-8")

    finally:
        # Guarantee restoration under any condition
        TARGET_FILE.write_text(original_code, encoding="utf-8")

    print("\n" + "=" * 75)
    caught = sum(1 for _, _, status in results if status == "CAUGHT")
    survived = sum(1 for _, _, status in results if status == "SURVIVED")
    errors = sum(1 for _, _, status in results if status == "TARGET_NOT_FOUND")

    print(f"Summary: {caught}/14 CAUGHT | {survived}/14 SURVIVED | {errors} ERRORS")
    print("=" * 75)

    if survived > 0 or errors > 0 or caught != len(MUTATIONS):
        print("⚠️ Some mutations survived or failed to run. Additional tests needed.")
        sys.exit(1)
    else:
        print("🎉 ALL 14 MUTATIONS CAUGHT! Test suite verification succeeded.")
        sys.exit(0)


if __name__ == "__main__":
    run_mutation_check()
