"""Helper script to save real Google Travel HTML response for offline fixture testing.

DO NOT RUN AUTOMATICALLY IN TEST SUITES.
Run manually when refreshing the offline fixture:
    uv run python scripts/save_google_travel_fixture.py
"""
import datetime as dt
from pathlib import Path
import re
from zoneinfo import ZoneInfo

import primp

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "google_travel_sample.html"


def main():
    today_ist = dt.datetime.now(ZoneInfo("Asia/Kolkata")).date()
    check_in = (today_ist + dt.timedelta(days=30)).isoformat()
    check_out = (today_ist + dt.timedelta(days=33)).isoformat()
    city = "Udaipur"

    print(f"Connecting to Google Travel for {city} ({check_in} to {check_out})...")
    client = primp.Client(
        impersonate="chrome_145",
        impersonate_os="macos",
        referer=True,
        cookie_store=True,
    )
    query = f"hotels in {city} from {check_in} to {check_out}"
    url = f"https://www.google.com/travel/search?q={query.replace(' ', '+')}&curr=INR"

    res = client.get(url, timeout=12)

    status = res.status_code
    text = res.text
    length = len(text)
    has_rupee = "₹" in text
    has_h2 = bool(re.search(r"<h2[^>]*>.*?</h2>", text, re.IGNORECASE | re.DOTALL))

    print(f"Status Code: {status}")
    print(f"Response Length: {length:,} characters")
    print(f"Contains '₹': {has_rupee}")
    print(f"Contains <h2> text: {has_h2}")

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(text, encoding="utf-8")
    print(f"Saved raw response to: {FIXTURE_PATH}")


if __name__ == "__main__":
    main()
