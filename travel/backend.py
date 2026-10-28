"""Deterministic mock travel backend for data generation.

Facts it returns (fares, hotel names, visa outcomes, advisories) are randomised per conversation on purpose,
so the student learns to relay tool results rather than memorise invented facts about real places.
"""
import datetime as dt
import json
import logging
import os
import random

import functools

from travel import india, kb

logger = logging.getLogger(__name__)

_KB = functools.lru_cache(maxsize=1)(kb.load)

AIRLINES = ["IndiGo", "Air India", "Akasa Air", "SpiceJet", "Air India Express"]
INTL_AIRLINES = ["Air India", "IndiGo", "Emirates", "Singapore Airlines", "Qatar Airways", "Etihad Airways", "Thai Airways", "Lufthansa"]
FOREIGN = ("dubai", "uae", "singapore", "bali", "indonesia", "hanoi", "vietnam", "colombo", "sri lanka", "kathmandu", "nepal", "paro",
           "bhutan", "male", "maldives", "tokyo", "japan", "kuala lumpur", "malaysia", "baku", "almaty", "bangkok", "thailand", "london",
           "new york", "usa", "paris", "frankfurt", "doha", "abu dhabi", "toronto", "sydney", "melbourne", "hong kong")
FARE_RULES = ["Non-refundable", "Refundable with ₹{fee} cancellation fee", "Change fee ₹{fee}; non-refundable"]
ACCESS_FEATURES = ["step-free entrance", "wheelchair-accessible room", "roll-in shower", "grab bars in bathroom", "lift to all floors"]
PLAIN_AMENITIES = ["free wifi", "restaurant", "24-hour front desk", "airport shuttle", "rooftop cafe", "parking", "elevator", "breakfast buffet"]


WRITE_TOOLS = {"create_booking"}


class Backend:
    """config keys (all optional):
    fail_search: "timeout" | "empty"        booking_status: "CONFIRMED" | "PENDING" | "FAILED_PAYMENT" | "SOLD_OUT"
    hotel_access: "none" | "partial" | "full"  two_suppliers: bool    non_refundable: bool    taxes_excluded: bool
    booking_record: dict | None (for get_booking; None -> NOT_FOUND)    visa: "required" | "evisa" | "exempt" | "on_arrival"
    advisory: "none" | "weather" | "unrest" | "closure"
    """

    def __init__(self, config, seed):
        self.cfg = config
        self.rng = random.Random(seed)
        self.offers = {}
        self.now = config.get("now", "2026-10-10T11:00+05:30")

    # ---- dispatch -------------------------------------------------------------------------------
    def call(self, name, args_json):
        try:
            args = json.loads(args_json) if isinstance(args_json, str) else dict(args_json)
        except json.JSONDecodeError:
            return {"error": "INVALID_ARGUMENTS", "message": "Arguments were not valid JSON"}
        fn = getattr(self, name, None)
        if fn is None:
            return {"error": "UNKNOWN_TOOL"}
        try:
            return fn(**{k: v for k, v in args.items() if v is not None})
        except Exception:
            logger.exception("Tool execution failed for '%s'", name)
            if name in WRITE_TOOLS:
                message = "The booking outcome is not confirmed. Please check the booking status before retrying."
            else:
                message = "The service encountered an error while processing the request"
            return {"error": "INTERNAL_ERROR", "message": message}

    def _search_failure(self):
        mode = self.cfg.get("fail_search")
        if mode == "timeout":
            return {"error": "UPSTREAM_TIMEOUT", "message": "Supplier search did not respond within 30s"}
        if mode == "empty":
            return {"checked_at": self.now, "results": [], "message": "No availability for the requested dates"}
        return None

    # ---- tools ----------------------------------------------------------------------------------
    def search_flights(self, origin, destination, date, passengers, return_date=None, **_):
        if (f := self._search_failure()):
            return f
        if os.environ.get("LIVE_PRICING") == "1" or self.cfg.get("live_pricing"):
            from travel import live_pricing
            payload = live_pricing.search_live_flights(origin, destination, date, passengers, return_date)
            for offer in payload.get("results", []):
                self.offers[offer["offer_id"]] = offer
            return payload
        pax = max(1, int(passengers)) if str(passengers).isdigit() else 1
        intl = any(f in f"{origin} {destination}".lower() for f in FOREIGN)
        results = []
        for k in range(self.rng.randint(2, 3)):
            dep_h = self.rng.choice([6, 7, 9, 11, 14, 17, 19, 21])
            dur = self.rng.choice([240, 330, 420, 600, 900]) if intl else self.rng.choice([75, 90, 120, 150, 165])
            fare = self.rng.randrange(18000, 65000, 100) if intl else self.rng.randrange(3200, 14000, 50)
            fee = self.rng.choice([1500, 2500, 3000, 3500])
            oid = f"F-{self.rng.randint(100, 999)}"
            offer = {"offer_id": oid, "airline": self.rng.choice(INTL_AIRLINES if intl else AIRLINES), "from": origin, "to": destination, "date": date,
                     "departs": f"{dep_h:02d}:{self.rng.choice(['05', '20', '35', '50'])}", "duration_min": dur,
                     "stops": self.rng.choice([0, 0, 1]), "fare_per_person_inr": fare, "passengers": pax,
                     "total_inr": fare * pax, "baggage": "15 kg check-in, 7 kg cabin",
                     "fare_rule": self.rng.choice(FARE_RULES).format(fee=india.inr(fee))}
            if return_date:
                offer["return_date"] = return_date
            self.offers[oid] = offer
            results.append(offer)
        return {"checked_at": self.now, "results": results}

    def search_hotels(self, city, check_in, check_out, guests, max_price_per_night=None, **_):
        if (f := self._search_failure()):
            return f
        if os.environ.get("LIVE_PRICING") == "1" or self.cfg.get("live_pricing"):
            from travel import live_pricing
            payload = live_pricing.search_live_hotels(city, check_in, check_out, guests, max_price_per_night)
            for offer in payload.get("results", []):
                self.offers[offer["offer_id"]] = offer
            return payload
        try:
            nights = max(1, (dt.date.fromisoformat(check_out) - dt.date.fromisoformat(check_in)).days)
        except ValueError:
            return {"error": "INVALID_ARGUMENTS", "message": "check_in and check_out must be YYYY-MM-DD"}
        cap = None
        if max_price_per_night not in (None, ""):
            try:
                cap = int(float(max_price_per_night))
            except (TypeError, ValueError):
                return {"error": "INVALID_ARGUMENTS", "message": "max_price_per_night must be a number in INR"}
            if cap < 900:
                return {"checked_at": self.now, "results": [], "message": f"No hotels at or below ₹{cap} per night"}
        results = []
        for k in range(self.rng.randint(2, 3)):
            price = self.rng.randrange(1800, 12000, 100) if cap is None else self.rng.randrange(int(cap * 0.6) // 100 * 100, cap + 1, 100)
            amen = self.rng.sample(PLAIN_AMENITIES, 3)
            access = self.cfg.get("hotel_access", "unspecified")
            if access == "full" and k == 0:
                amen += ACCESS_FEATURES[:4]
            elif access == "partial":
                amen += ["elevator"] if "elevator" not in amen else []
            non_ref = self.cfg.get("non_refundable") if k == 0 else self.rng.random() < 0.3
            ci = dt.date.fromisoformat(check_in)
            rule = "NON_REFUNDABLE" if non_ref else f"Free cancellation until {(ci - dt.timedelta(days=self.rng.choice([1, 2, 3]))).isoformat()} 23:59"
            oid = f"H-{self.rng.randint(100, 999)}"
            offer = {"offer_id": oid, "hotel": india.hotel_name(self.rng, city), "city": city, "room": self.rng.choice(
                     ["Standard Double", "Deluxe Room", "Superior Twin", "Family Room"]), "price_per_night_inr": price,
                     "nights": nights, "guests": guests, "check_in": check_in, "check_out": check_out,
                     "amenities": sorted(set(amen)), "rate_rule": rule}
            if self.cfg.get("taxes_excluded") and k == 0:
                offer.update(total_inr=price * nights, taxes_included=False, taxes_estimate_inr=round(price * nights * 0.12))
            else:
                offer.update(total_inr=price * nights, taxes_included=True)
            self.offers[oid] = offer
            results.append(offer)
        if self.cfg.get("two_suppliers"):
            twin = dict(results[0])
            twin["offer_id"] = f"H-{self.rng.randint(100, 999)}"
            cheaper = round(twin["price_per_night_inr"] * 0.9 / 100) * 100
            twin.update(supplier="SupplierB", price_per_night_inr=cheaper, total_inr=cheaper * nights, rate_rule="NON_REFUNDABLE")
            results[0]["supplier"] = "SupplierA"
            results[0]["rate_rule"] = f"Free cancellation until {(ci - dt.timedelta(days=3)).isoformat()} 23:59"
            self.offers[twin["offer_id"]] = twin
            results.insert(1, twin)
        return {"checked_at": self.now, "results": results}

    def get_offer(self, offer_id):
        return self.offers.get(offer_id)

    def create_booking(self, offer_id, travelers, total_price_inr, customer_confirmed_terms, **_):
        offer = self.offers.get(offer_id)
        if offer is None:
            return {"status": "FAILED", "reason": "UNKNOWN_OFFER", "charged": False}
        if customer_confirmed_terms is not True:
            return {"status": "FAILED", "reason": "CONFIRMATION_REQUIRED", "charged": False}
        expected = offer["total_inr"] + (offer.get("taxes_estimate_inr") or 0)
        if abs(float(total_price_inr) - expected) > 1 and abs(float(total_price_inr) - offer["total_inr"]) > 1:
            return {"status": "FAILED", "reason": "PRICE_MISMATCH", "current_total_inr": expected, "charged": False}
        status = self.cfg.get("booking_status", "CONFIRMED")
        if status == "PENDING":
            return {"status": "PENDING_SUPPLIER_CONFIRMATION", "request_id": f"REQ-{self.rng.randint(1000, 9999)}",
                    "payment": "AUTHORISED_NOT_CAPTURED"}
        if status == "FAILED_PAYMENT":
            return {"status": "FAILED", "reason": "PAYMENT_DECLINED", "charged": False}
        if status == "SOLD_OUT":
            return {"status": "FAILED", "reason": "NO_LONGER_AVAILABLE", "charged": False}
        return {"status": "CONFIRMED", "booking_ref": f"TRV-{self.rng.randint(1000, 9999)}", "charged_inr": expected}

    def get_booking(self, booking_ref, **_):
        rec = self.cfg.get("booking_record")
        if not rec or rec.get("booking_ref") != booking_ref:
            return {"status": "NOT_FOUND", "booking_ref": booking_ref}
        return rec

    def get_entry_requirements(self, passport_country, destination_country, purpose=None, transit_countries=None, **_):
        kind = self.cfg.get("visa") or self.rng.choice(["required", "evisa", "exempt", "on_arrival"])
        days = self.rng.choice([14, 15, 30, 60, 90])
        summary = {"required": "Visa required before travel; apply through the destination's official visa portal or embassy.",
                   "evisa": f"e-Visa required; apply online before travel. Stay up to {days} days.",
                   "exempt": f"Visa not required for tourism stays up to {days} days.",
                   "on_arrival": f"Visa on arrival available for tourism, up to {days} days; fee payable on arrival."}[kind]
        return {"source": "official government portal", "checked_at": self.now, "passport_country": passport_country,
                "destination_country": destination_country, "result": summary,
                "also_required": "Passport valid at least 6 months beyond arrival; return or onward ticket.",
                "note": "Entry is decided by border officials on arrival."}

    def get_travel_advisory(self, destination, **_):
        kind = self.cfg.get("advisory") or self.rng.choice(["none", "weather", "closure", "unrest"])
        text = {"none": "No current official advisories.",
                "weather": "Heavy rain alert for hill districts this week; landslides possible on mountain roads.",
                "closure": "Some high-altitude roads closed for the season; check the state transport department before travel.",
                "unrest": "Local restrictions in parts of the region; follow district administration notices."}[kind]
        return {"source": "official state and central advisories", "checked_at": self.now, "destination": destination, "advisory": text}

    def get_destination_info(self, destination, **_):
        entry = kb.lookup(_KB(), destination)
        if entry is None:
            return {"found": False, "destination": destination,
                    "message": "No travel-guide entry for this place. It may not exist, or it may be too small to be covered."}
        return {"found": True, **entry}

    def handoff_to_human(self, summary, urgency="normal", **_):
        return {"status": "TRANSFERRED", "queue": urgency, "expected_wait_minutes": 2 if urgency == "urgent" else self.rng.choice([5, 10, 15])}
