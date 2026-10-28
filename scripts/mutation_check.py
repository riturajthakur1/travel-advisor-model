"""Mutation testing self-check script for travel.live_pricing.

Features:
1. Baseline Gate: Verifies target tests pass on untouched code before mutating.
2. True Temp-Dir Isolation: Copies the package to a fresh temp directory per mutation,
   runs pytest inside the temp directory, and automatically cleans it up.
   The working repository is never modified in place.
3. Clean Git Check: Refuses to run if travel/live_pricing.py has uncommitted changes.
4. Strict Pattern Matching: Exits non-zero immediately if any mutation target pattern is not found.
5. Targeted Scope: Runs only tests/test_live_pricing.py and offer-schema tests.
6. Self-test: Supports --self-test to prove that a deliberate no-op mutation is reported SURVIVED.
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
TARGET_FILE = ROOT / "travel" / "live_pricing.py"

TARGET_TEST_FILES = ["tests/test_live_pricing.py"]
if (ROOT / "tests" / "test_offer_schemas.py").exists():
    TARGET_TEST_FILES.append("tests/test_offer_schemas.py")

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
        "target": 'if not live_hotels:\n            return {\n                "error": "PROVIDER_RESPONSE_UNREADABLE",\n                "message": "Could not read hotel results from the provider page; the layout may have changed or the request was blocked."\n            }',
        "replacement": 'if not live_hotels:\n            return {\n                "checked_at": now_iso,\n                "results": [],\n                "source": "Google Travel Hotels (Live)",\n                "cached": is_cached\n            }',
    },
    {
        "id": 14,
        "name": "FlightsNotFound mapped to a plain empty 'No flights found' message",
        "target": '"message": "Google returned no flight results for this search. This can mean no flights are available or a temporary provider error; check the search link.",',
        "replacement": '"message": f"No flights found from {orig_code} to {dest_code} on {date}",',
    },
]

SELF_TEST_MUTATION = {
    "id": 0,
    "name": "deliberate no-op mutation (checker self-test)",
    "target": "# Constants for pricing, scraping, limits, and caching",
    "replacement": "# Constants for pricing, scraping, limits, and caching (no-op mutation)",
}


def check_git_status():
    proc = subprocess.run(
        ["git", "status", "--porcelain", "travel/live_pricing.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.stdout.strip():
        print("❌ [GIT STATUS ERROR] travel/live_pricing.py has uncommitted changes:")
        print(proc.stdout.strip())
        print("Please commit or clean working tree before running mutation check.")
        sys.exit(1)


def check_baseline(test_files):
    print("Verifying baseline test suite on untouched repository...")
    proc = subprocess.run(
        ["uv", "run", "pytest"] + test_files + ["-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print("❌ [BASELINE GATE FAILED] Target tests failed on untouched code:")
        print(proc.stdout)
        print(proc.stderr)
        sys.exit(1)
    print("✅ Baseline gate passed: all target tests green.\n")


def execute_mutation_in_temp_dir(mutation, test_files):
    with tempfile.TemporaryDirectory() as td:
        tmp_dir = Path(td)
        shutil.copytree(ROOT / "travel", tmp_dir / "travel")
        shutil.copytree(ROOT / "tests", tmp_dir / "tests")
        shutil.copy(ROOT / "pyproject.toml", tmp_dir / "pyproject.toml")

        target_file = tmp_dir / "travel" / "live_pricing.py"
        code = target_file.read_text(encoding="utf-8")

        target = mutation["target"]
        replacement = mutation["replacement"]

        if target not in code:
            print(f"❌ [CONFIG ERROR] Pattern not found for Mutation #{mutation['id']} ({mutation['name']}):")
            print(f"   Target: {target!r}")
            sys.exit(1)

        mutated_code = code.replace(target, replacement)
        target_file.write_text(mutated_code, encoding="utf-8")

        proc = subprocess.run(
            ["uv", "run", "pytest"] + test_files + ["-q"],
            cwd=tmp_dir,
            capture_output=True,
            text=True,
        )

        # If tests failed (proc.returncode != 0), the mutation was CAUGHT
        # If tests passed (proc.returncode == 0), the mutation SURVIVED
        return proc.returncode != 0


def run_self_test(test_files):
    print("=" * 75)
    print("  MUTATION CHECKER SELF-TEST (Deliberate No-Op Mutation)")
    print("=" * 75)
    check_git_status()
    check_baseline(test_files)

    is_caught = execute_mutation_in_temp_dir(SELF_TEST_MUTATION, test_files)
    if not is_caught:
        print(f"  ❌ [SURVIVED] Mutation # 0: {SELF_TEST_MUTATION['name']}")
        print("\n" + "=" * 75)
        print("✅ Self-test PASSED: Deliberate no-op mutation was successfully reported SURVIVED.")
        print("   This proves the checker correctly identifies non-breaking/surviving mutations.")
        print("=" * 75)
        sys.exit(0)
    else:
        print(f"  ✅ [CAUGHT]   Mutation # 0: {SELF_TEST_MUTATION['name']}")
        print("\n" + "=" * 75)
        print("❌ Self-test FAILED: Deliberate no-op was unexpectedly caught!")
        print("=" * 75)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Mutation test runner for live_pricing.py")
    parser.add_argument("--self-test", action="store_true", help="Run self-test with deliberate no-op mutation")
    args = parser.parse_args()

    if args.self_test:
        run_self_test(TARGET_TEST_FILES)
        return

    print("=" * 75)
    print("  MUTATION TESTING SELF-CHECK (14 Mutations)")
    print("=" * 75)

    check_git_status()
    check_baseline(TARGET_TEST_FILES)

    results = []
    for m in MUTATIONS:
        m_id = m["id"]
        name = m["name"]
        caught = execute_mutation_in_temp_dir(m, TARGET_TEST_FILES)
        if caught:
            print(f"  ✅ [CAUGHT]   Mutation #{m_id:2d}: {name}")
            results.append((m_id, name, "CAUGHT"))
        else:
            print(f"  ❌ [SURVIVED] Mutation #{m_id:2d}: {name}")
            results.append((m_id, name, "SURVIVED"))

    print("\n" + "=" * 75)
    caught_count = sum(1 for _, _, status in results if status == "CAUGHT")
    survived_count = sum(1 for _, _, status in results if status == "SURVIVED")

    print(f"Summary: {caught_count}/{len(MUTATIONS)} CAUGHT | {survived_count}/{len(MUTATIONS)} SURVIVED")
    print("=" * 75)

    if survived_count > 0 or caught_count != len(MUTATIONS):
        print("⚠️ Some mutations survived. Test suite needs strengthening.")
        sys.exit(1)
    else:
        print("🎉 ALL 14 MUTATIONS CAUGHT! Baseline is green and tests are verified.")
        sys.exit(0)


if __name__ == "__main__":
    main()
