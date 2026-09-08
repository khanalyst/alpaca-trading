"""Explicit local and broker timestamps; missing fill time stays unknown."""
from datetime import datetime
from collections.abc import Mapping
import math

TIMING_FIELDS = ("decision_ts", "request_sent_ts", "response_received_ts",
                 "submit_roundtrip_ms", "broker_submitted_ts", "broker_filled_ts")


def timestamp(value):
    try:
        if isinstance(value, datetime):
            result = value.timestamp() if value.tzinfo else None
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            result = parsed.timestamp() if parsed.tzinfo else None
        else:
            result = float(value) if not isinstance(value, bool) else None
        return result if result is not None and math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def broker_timing(order):
    def value(key):
        return order.get(key) if isinstance(order, Mapping) else getattr(order, key, None)
    raw = value("raw")
    raw = raw if isinstance(raw, Mapping) else {}
    return {"broker_submitted_ts": timestamp(value("submitted_at")
                                             or raw.get("submitted_at")),
            # updated_at may describe a cancellation or replacement, so it
            # must never stand in for an absent broker fill timestamp.
            "broker_filled_ts": timestamp(value("filled_at")
                                          or raw.get("filled_at"))}
