"""How much of each session a strict gap-free prefix rule can still see.

Review finding A-08: the session-anchored rule families (opening range,
opening drive, session VWAP) need every minute from 09:30 up to the signal
bar under the frozen grammar, so the first missing IEX minute removes every
later signal of that symbol-session.  Sessions and symbols with complete IEX
coverage are then over-represented in replay evidence.  This report measures
that selection per symbol so it can be stated next to any result, and so the
opt-in ``session_gap_mode: zero_volume`` (agent/contracts/rule.py) can be
judged on the corpus it would change.

Diagnostic only: it never changes a replay, a gate or an identity.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time
from statistics import median
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

SESSION_COVERAGE_SCHEMA = "session-prefix-coverage.v1"
_NEW_YORK = ZoneInfo("America/New_York")
_OPEN = time(9, 30)


def _stamp(row: Any) -> datetime | None:
    raw = row.get("timestamp") if isinstance(row, Mapping) else getattr(row, "timestamp", None)
    if isinstance(raw, str):
        try:
            raw = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(raw, datetime) or raw.tzinfo is None:
        return None
    return raw.astimezone(_NEW_YORK)


def _session_offsets(rows: Iterable[Any]) -> dict[Any, set[int]]:
    """Minute offsets from 09:30, by New York session date (regular hours)."""
    sessions: dict[Any, set[int]] = defaultdict(set)
    for row in rows:
        local = _stamp(row)
        if local is None or local.second or local.microsecond:
            continue
        opened = datetime.combine(local.date(), _OPEN, tzinfo=_NEW_YORK)
        offset = int((local - opened).total_seconds() // 60)
        if 0 <= offset < 390:
            sessions[local.date()].add(offset)
    return sessions


def _summarize(per_session: list[dict[str, Any]]) -> dict[str, Any]:
    observed = sum(item["observed_minutes"] for item in per_session)
    lost = sum(item["strict_lost_minutes"] for item in per_session)
    gaps = [item["first_gap_minute"] for item in per_session
            if item["first_gap_minute"] is not None]
    return {
        "sessions": len(per_session),
        "sessions_complete": sum(1 for item in per_session
                                 if item["first_gap_minute"] is None),
        "sessions_missing_open": sum(1 for item in per_session
                                     if not item["open_present"]),
        "sessions_with_gap": len(gaps),
        "median_first_gap_minute": median(gaps) if gaps else None,
        # Share of observed session minutes at or after the first missing
        # minute: where a strict-prefix session-anchored rule cannot signal.
        "strict_prefix_minutes_lost_share": (lost / observed) if observed else None,
    }


def prefix_gap_report(rows_by_symbol: Mapping[str, Iterable[Any]]) -> dict[str, Any]:
    """Per-symbol and universe coverage of the gap-free session prefix.

    A session's span runs from 09:30 to its last observed minute, so an early
    close or a quiet final minute is not counted as a gap.  ``first_gap_minute``
    is the earliest missing minute inside that span (0 means the 09:30 bar is
    missing); every observed minute at or after it is lost to a strict-prefix
    rule.
    """
    symbols: dict[str, Any] = {}
    every: list[dict[str, Any]] = []
    for symbol in sorted(rows_by_symbol):
        per_session = []
        for _day, offsets in sorted(_session_offsets(rows_by_symbol[symbol]).items()):
            span = max(offsets) + 1
            missing = sorted(set(range(span)) - offsets)
            first_gap = missing[0] if missing else None
            per_session.append({
                "open_present": 0 in offsets,
                "observed_minutes": len(offsets),
                "first_gap_minute": first_gap,
                "strict_lost_minutes": 0 if first_gap is None else
                sum(1 for offset in offsets if offset >= first_gap),
            })
        symbols[str(symbol)] = _summarize(per_session)
        every.extend(per_session)
    return {"schema": SESSION_COVERAGE_SCHEMA, "symbols": symbols,
            "universe": _summarize(every)}


__all__ = ["SESSION_COVERAGE_SCHEMA", "prefix_gap_report"]
