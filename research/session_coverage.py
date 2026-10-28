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

SESSION_COVERAGE_SCHEMA = "session-prefix-coverage.v2"
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
    gaps = [item["first_internal_gap_minute"] for item in per_session
            if item["first_internal_gap_minute"] is not None]

    def share(key: str) -> float | None:
        return (sum(item[key] for item in per_session) / observed) if observed else None

    return {
        "sessions": len(per_session),
        "sessions_complete": sum(1 for item in per_session
                                 if item["open_present"] and
                                 item["first_internal_gap_minute"] is None),
        "sessions_missing_open": sum(1 for item in per_session
                                     if not item["open_present"]),
        "sessions_with_gap": len(gaps),
        "median_first_gap_minute": median(gaps) if gaps else None,
        # Share of observed minutes a strict-prefix rule cannot signal on.
        # Session VWAP families need adjacency from the session's first bar,
        # so they lose the minutes from the first gap *after* it; opening
        # range and opening drive need the 09:30 bar as well, so a missing
        # open costs them the whole session.
        "strict_prefix_minutes_lost_share": share("vwap_lost_minutes"),
        "opening_anchored_minutes_lost_share": share("opening_lost_minutes"),
    }


def prefix_gap_report(rows_by_symbol: Mapping[str, Iterable[Any]]) -> dict[str, Any]:
    """Per-symbol and universe coverage of the gap-free session prefix.

    A session's span runs from its first to its last observed minute, so an
    early close or a quiet final minute is not counted as a gap.
    ``first_gap_minute`` is the earliest missing minute inside that span; a
    missing 09:30 bar is reported separately (``sessions_missing_open``).
    """
    symbols: dict[str, Any] = {}
    every: list[dict[str, Any]] = []
    for symbol in sorted(rows_by_symbol):
        per_session = []
        for _day, offsets in sorted(_session_offsets(rows_by_symbol[symbol]).items()):
            first, last = min(offsets), max(offsets)
            missing = sorted(set(range(first, last + 1)) - offsets)
            internal = missing[0] if missing else None
            opening = 0 if first > 0 else internal
            per_session.append({
                "open_present": first == 0,
                "observed_minutes": len(offsets),
                "first_internal_gap_minute": internal,
                "vwap_lost_minutes": 0 if internal is None else
                sum(1 for offset in offsets if offset >= internal),
                "opening_lost_minutes": 0 if opening is None else
                sum(1 for offset in offsets if offset >= opening),
            })
        symbols[str(symbol)] = _summarize(per_session)
        every.extend(per_session)
    return {"schema": SESSION_COVERAGE_SCHEMA, "symbols": symbols,
            "universe": _summarize(every)}


__all__ = ["SESSION_COVERAGE_SCHEMA", "prefix_gap_report"]
