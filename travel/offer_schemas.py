"""Producer-side contract v1. Defined by live_pricing, not verified against the consumer. Null means UNKNOWN, never guess or fill it."""
from typing import Any, Dict, List, Literal, Set


FLIGHT_REQUIRED_KEYS: frozenset[str] = frozenset({
    "offer_id",
    "airline",
    "from",
    "to",
    "date",
    "departs",
    "duration_min",
    "stops",
    "fare_per_person_inr",
    "passengers",
    "total_inr",
    "baggage",
    "fare_rule",
    "is_live",
    "deep_link",
})

FLIGHT_ROUNDTRIP_EXTRA_KEYS: frozenset[str] = frozenset({
    "return_date",
    "return_departs",
    "return_duration_min",
    "return_stops",
    "fare_basis",
})

FLIGHT_NULLABLE_FIELDS: frozenset[str] = frozenset({
    "baggage",
    "fare_rule",
})

HOTEL_REQUIRED_KEYS: frozenset[str] = frozenset({
    "offer_id",
    "hotel",
    "city",
    "room",
    "price_per_night_inr",
    "nights",
    "guests",
    "check_in",
    "check_out",
    "amenities",
    "rate_rule",
    "total_inr",
    "taxes_included",
    "taxes_estimate_inr",
    "is_live",
    "deep_link",
})

HOTEL_NULLABLE_FIELDS: frozenset[str] = frozenset({
    "room",
    "amenities",
    "rate_rule",
    "taxes_included",
    "taxes_estimate_inr",
})

FIELD_TYPES: Dict[str, Any] = {
    # String identifiers, text, dates
    "offer_id": str,
    "airline": str,
    "from": str,
    "to": str,
    "date": str,
    "departs": str,
    "hotel": str,
    "city": str,
    "check_in": str,
    "check_out": str,
    "deep_link": str,
    "room": str,
    "rate_rule": str,
    "fare_rule": str,
    "baggage": str,
    "return_date": str,
    "return_departs": str,
    "fare_basis": str,
    # Integer values: money, counts, durations (never bool)
    "duration_min": int,
    "return_duration_min": int,
    "stops": int,
    "return_stops": int,
    "fare_per_person_inr": int,
    "passengers": int,
    "total_inr": int,
    "price_per_night_inr": int,
    "nights": int,
    "guests": int,
    "taxes_estimate_inr": int,
    # Boolean flags
    "is_live": bool,
    "taxes_included": bool,
    # Collections
    "amenities": list,
}


def validate_offer(
    offer: Dict[str, Any],
    kind: Literal["flight", "hotel"],
    roundtrip: bool = False
) -> List[str]:
    """Validate an offer dictionary against the producer contract.
    
    Returns a list of human-readable problems. An empty list means the offer is valid.
    """
    if not isinstance(offer, dict):
        return [f"Offer must be a dictionary, got {type(offer).__name__}"]

    problems: List[str] = []

    if kind == "flight":
        required_keys: Set[str] = set(FLIGHT_REQUIRED_KEYS)
        if roundtrip:
            required_keys |= set(FLIGHT_ROUNDTRIP_EXTRA_KEYS)
        nullable_fields = FLIGHT_NULLABLE_FIELDS
    elif kind == "hotel":
        required_keys = set(HOTEL_REQUIRED_KEYS)
        nullable_fields = HOTEL_NULLABLE_FIELDS
    else:
        return [f"Unknown offer kind '{kind}', expected 'flight' or 'hotel'"]

    offer_keys = set(offer.keys())

    missing = sorted(required_keys - offer_keys)
    if missing:
        problems.append(f"Missing required key(s): {', '.join(missing)}")

    unexpected = sorted(offer_keys - required_keys)
    if unexpected:
        problems.append(f"Unexpected key(s): {', '.join(unexpected)}")

    for key, val in offer.items():
        if key not in required_keys:
            continue

        if val is None:
            if key not in nullable_fields:
                problems.append(f"Field '{key}' is None but is not nullable")
            continue

        expected_type = FIELD_TYPES.get(key)
        if expected_type is None:
            continue

        if expected_type is int:
            if isinstance(val, bool) or not isinstance(val, int):
                problems.append(f"Field '{key}' must be an int (got {type(val).__name__}: {val!r})")
        elif expected_type is bool:
            if not isinstance(val, bool):
                problems.append(f"Field '{key}' must be a bool (got {type(val).__name__}: {val!r})")
        elif expected_type is str:
            if not isinstance(val, str):
                problems.append(f"Field '{key}' must be a str (got {type(val).__name__}: {val!r})")
        elif expected_type is list:
            if not isinstance(val, list):
                problems.append(f"Field '{key}' must be a list (got {type(val).__name__}: {val!r})")
            elif not all(isinstance(item, str) for item in val):
                problems.append(f"Field '{key}' must be a list of str (contains non-str items)")

    return problems
