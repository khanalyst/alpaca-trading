"""Small, policy-neutral primitives for cross-arm evidence.

The protocol and shortlist are deliberately different policies, but they both
need to answer the same mechanical questions: which persisted rows describe
one opportunity, which rows resolve that opportunity, and how much of the
union of two arms can be paired.  Keeping those operations here prevents the
two lanes from slowly acquiring different identities or duplicate handling.

This module is stdlib-only.  It does not know anything about sample floors,
multiple-testing corrections, or labels.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


def _value(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def canonical_opportunity_key(row: Any) -> tuple:
    """Return the stable identity used to match the same opportunity.

    A generated proposal id is authoritative when present.  Older rows do not
    have one, so they use the completed signal timestamp and the semantic
    setup identity.  Cycle ids and wall-clock decision times are intentionally
    excluded whenever a signal timestamp exists: independent arms can observe
    the same market candle on different cycles.  A cycle is only a legacy
    fallback when no signal timestamp was persisted at all.
    """
    proposal_id = _value(row, "proposal_id")
    if proposal_id is not None and str(proposal_id) != "":
        return ("proposal", str(proposal_id))

    symbol = _value(row, "symbol")
    signal_ts = _value(row, "signal_ts")
    if signal_ts is None:
        signal_ts = _value(row, "cycle_id")
    if signal_ts is None:
        # Standalone legacy paper-trade rows often have neither a generated
        # proposal id nor a cycle.  Their entry/decision timestamp is the
        # only persisted unit identity; without this fallback every such row
        # would collapse into one false duplicate key.
        signal_ts = _value(row, "entry_ts",
                           _value(row, "decision_ts", _value(row, "ts")))
    return ("identity", symbol, signal_ts,
            _value(row, "direction"), _value(row, "setup_type"))


# Short aliases make the primitive convenient for callers without creating a
# second implementation.  ``opportunity_key`` is intentionally public; a few
# older integrations used that name while the protocol called it proposal key.
opportunity_key = canonical_opportunity_key
canonical_proposal_key = canonical_opportunity_key


@dataclass
class IndexedRows:
    """An index whose duplicate keys can never become resolved again."""

    seen_keys: set = field(default_factory=set)
    duplicate_keys: set = field(default_factory=set)
    duplicate_occurrences: list = field(default_factory=list)
    duplicate_rows: int = 0
    # The first row is retained only for classification (for example, to
    # distinguish a first PROPOSED row from a first VETOED row).  It is never
    # used to resurrect a key after that key becomes a duplicate.
    first_rows: dict = field(default_factory=dict)
    resolved: dict = field(default_factory=dict)
    resolved_ts: dict = field(default_factory=dict)
    resolved_rows: dict = field(default_factory=dict)

    @property
    def duplicates(self) -> int:
        """Number of duplicate rows (kept for compact report consumers)."""
        return self.duplicate_rows

    @property
    def duplicate_count(self) -> int:
        return self.duplicate_rows

    @property
    def unique_keys(self) -> set:
        """Keys with exactly one included row (duplicates removed)."""
        return self.seen_keys - self.duplicate_keys

    @property
    def duplicate_reasons(self) -> dict:
        """Stable, human-readable reasons keyed by duplicate identity."""
        return {
            key: "duplicate identity; every occurrence is excluded"
            for key in self.duplicate_keys
        }


def index_rows(
        rows: Iterable[Any], *,
        key_fn: Callable[[Any], Any] = canonical_opportunity_key,
        value_fn: Callable[[Any], Any] | None = None,
        timestamp_fn: Callable[[Any], float] | None = None,
        include_fn: Callable[[Any], bool] | None = None) -> IndexedRows:
    """Index rows by key, permanently excluding duplicate keys from resolve.

    ``seen_keys`` is populated before resolution.  On the second occurrence a
    key is moved to ``duplicate_keys`` and *all* previously resolved state is
    removed.  Further occurrences remain duplicates and can never re-add the
    key.  This matters for replay/ledger corruption where a third row used to
    silently resurrect a key after the second row removed it.

    ``include_fn`` controls which rows participate in the index; rows that do
    not satisfy it are ignored entirely.  A ``None`` value from ``value_fn``
    means unresolved, while an explicit falsey value (including ``0.0``) is a
    valid resolution.
    """
    result = IndexedRows()
    for row in rows or ():
        if include_fn is not None and not include_fn(row):
            continue
        key = key_fn(row)
        if key in result.seen_keys:
            result.duplicate_rows += 1
            result.duplicate_keys.add(key)
            result.duplicate_occurrences.append(key)
            result.resolved.pop(key, None)
            result.resolved_ts.pop(key, None)
            result.resolved_rows.pop(key, None)
            continue
        result.seen_keys.add(key)
        result.first_rows[key] = row
        if value_fn is None:
            continue
        value = value_fn(row)
        if value is None:
            continue
        result.resolved[key] = value
        result.resolved_rows[key] = row
        if timestamp_fn is not None:
            try:
                result.resolved_ts[key] = float(timestamp_fn(row))
            except (TypeError, ValueError, OverflowError):
                result.resolved_ts[key] = 0.0
    return result


def pair_sets(
        left_seen: Iterable[Any], right_seen: Iterable[Any],
        left_resolved: Iterable[Any] = (),
        right_resolved: Iterable[Any] = ()) -> dict:
    """Construct matched/union sets and coverage for two evidence arms.

    Inputs are accepted as any iterable (including :class:`IndexedRows`).
    Coverage is resolved matches over the union of eligible keys.  An empty
    union is reported as 0%, which is the fail-closed value for shortlist
    qualification; callers that need a vacuous 100% diagnostic can choose that
    policy at their boundary.
    """
    def _seen(value: Iterable[Any]) -> set:
        if isinstance(value, IndexedRows):
            return set(value.unique_keys)
        return set(value)

    def _resolved(value: Iterable[Any], fallback: Iterable[Any]) -> set:
        if isinstance(value, IndexedRows):
            return set(value.resolved)
        # Passing an index as the first argument is a useful shorthand; in
        # that form callers commonly leave resolved empty.
        if value == () and isinstance(fallback, IndexedRows):
            return set(fallback.resolved)
        return set(value)

    left_index = left_seen if isinstance(left_seen, IndexedRows) else None
    right_index = right_seen if isinstance(right_seen, IndexedRows) else None
    left_seen = _seen(left_seen)
    right_seen = _seen(right_seen)
    left_resolved = (set(left_index.resolved) if left_index is not None
                     and left_resolved == () else _resolved(
                         left_resolved, left_index))
    right_resolved = (set(right_index.resolved) if right_index is not None
                      and right_resolved == () else _resolved(
                          right_resolved, right_index))
    union = left_seen | right_seen
    matched = left_resolved & right_resolved
    return {
        "matched": matched,
        "union": union,
        "left_only": left_seen - right_seen,
        "right_only": right_seen - left_seen,
        "matched_n": len(matched),
        "union_n": len(union),
        "coverage_pct": (len(matched) / len(union) * 100.0
                          if union else 0.0),
    }


# Alternate descriptive name used by callers that want to emphasise that
# this is a set operation rather than a statistical comparison.
pair_opportunity_sets = pair_sets


def chronological_split(
        items: list, fraction: float = 0.7,
        key: Callable[[Any], Any] = lambda item: getattr(item, "ts", 0.0)) -> tuple:
    """Split sorted items into an early fit window and a later confirmation."""
    ordered = sorted(items, key=key)
    if len(ordered) < 2:
        return ordered, []
    cut = int(len(ordered) * fraction)
    cut = min(max(cut, 1), len(ordered) - 1)
    return ordered[:cut], ordered[cut:]


def common_time_cutoff(
        populations: list[list], fraction: float = 0.7,
        key: Callable[[Any], Any] = lambda item: getattr(item, "ts", 0.0)
        ) -> float | None:
    """Return one calendar boundary shared by all populations."""
    timestamps = sorted({float(key(item))
                         for population in populations for item in population})
    if len(timestamps) < 2:
        return None
    cut = int(len(timestamps) * fraction)
    cut = min(max(cut, 1), len(timestamps) - 1)
    return timestamps[cut]


def split_at_time(
        items: list, cutoff_ts: float,
        key: Callable[[Any], Any] = lambda item: getattr(item, "ts", 0.0)
        ) -> tuple[list, list]:
    """Split at a fixed timestamp, retaining chronological order."""
    ordered = sorted(items, key=key)
    return ([item for item in ordered if float(key(item)) < cutoff_ts],
            [item for item in ordered if float(key(item)) >= cutoff_ts])


def pair_coverage_pct(matched: int, union: int) -> float:
    """Policy-neutral coverage arithmetic for already-built pair sets."""
    return matched / union * 100.0 if union else 0.0


__all__ = [
    "IndexedRows", "canonical_opportunity_key", "canonical_proposal_key",
    "common_time_cutoff", "chronological_split", "index_rows",
    "opportunity_key", "pair_coverage_pct", "pair_opportunity_sets",
    "pair_sets", "split_at_time",
]
