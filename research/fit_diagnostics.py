"""Compact, fit-only diagnostics for the bounded rule factory.

The functions in this module are observational.  They never authorize a
candidate, alter an exit, or inspect held-out/sealed rows.  Their purpose is
to make sparse signal prefixes, execution economics, and behaviorally
identical parameter sets visible before the expensive full replay.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from statistics import mean, median
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from agent.contracts.rule import (
    CROSS_SECTIONAL_BENCHMARK, MIN_STOP_DISTANCE_BPS,
    cross_sectional_symbol_eligibility,
    evaluate_rule_signal_metadata,
    evaluate_rule_signal_trace, feature_window_bars, rule_variant_id,
    validate_rule_spec, SESSION_MINUTES,
)
from .costs import (STRESSED_COST_BASIS, STRESSED_COST_SCHEMA,
                    stressed_cost_usd)
from .market_data import (historical_backfill_record, record_available_at,
                           record_is_available, replay_available_at,
                           replay_record_is_available)
from .stats import clustered_mde_power_report
from .signal_quality import (SIGNAL_QUALITY_ELIGIBILITY_SCHEMA,
                              measure_signal_quality)
from .path_telemetry import (aggregate_path_telemetry, compute_path_telemetry,
                             target_hold_reachability)


FIT_DIAGNOSTICS_SCHEMA = "fit-diagnostics.v1"
FIT_BEHAVIOR_ALIAS_SCHEMA = "fit-behavior-alias.v1"
# Quantization is deliberately much tighter than an executable market tick.
# It merges only numerical representation noise in otherwise identical
# planned fit behavior, while producing a transitive, hashable equivalence
# relation (unlike order-dependent pairwise tolerance clustering).
FIT_BEHAVIOR_ALIAS_DECIMALS = 8
COST_STRESS_MULTIPLIERS = (9, 15, 25, 50)
_NY = ZoneInfo("America/New_York")
BAR_COVERAGE_SCHEMA = "bar-coverage.v1"
_MAX_GAP_SAMPLES = 8


def _row_value(row: Any, name: str, default: Any = None) -> Any:
    """Read a normalized field from either a dataclass or a mapping."""
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value is None:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _coverage_timestamp(row: Any) -> datetime | None:
    return _coerce_datetime(_row_value(row, "timestamp",
                                        _row_value(row, "ts")))


def _coverage_session(row: Any) -> str:
    supplied = _row_value(row, "session_date")
    if supplied is not None:
        if isinstance(supplied, date):
            return supplied.isoformat()
        text = str(supplied).strip()
        if text:
            return text[:10]
    return _session(row)


def _coverage_end(row: Any, stamp: datetime | None) -> datetime | None:
    explicit = _coerce_datetime(_row_value(row, "end"))
    if explicit is not None:
        return explicit
    interval = _row_value(row, "interval_seconds", 60)
    try:
        return stamp + timedelta(seconds=int(interval or 60)) if stamp else None
    except (TypeError, ValueError, OverflowError):
        return None


def _coverage_distribution(values: Sequence[float | int]) -> dict[str, Any]:
    clean = sorted(float(value) for value in values
                   if _number(value) is not None)
    if not clean:
        return {"count": 0, "min": None, "p25": None, "median": None,
                "p75": None, "max": None, "mean": None}

    def percentile(fraction: float) -> float:
        index = (len(clean) - 1) * fraction
        lower, upper = math.floor(index), math.ceil(index)
        if lower == upper:
            return clean[lower]
        return clean[lower] + (clean[upper] - clean[lower]) * (index - lower)

    return {"count": len(clean), "min": clean[0],
            "p25": percentile(.25), "median": percentile(.5),
            "p75": percentile(.75), "max": clean[-1],
            "mean": sum(clean) / len(clean)}


def _coverage_session_record(rows: Sequence[Any], *, symbol: str,
                             session: str) -> dict[str, Any]:
    """Describe one symbol/session without inferring missing edge bars."""
    ordered = sorted(rows, key=lambda row: _coverage_timestamp(row) or
                     datetime.min.replace(tzinfo=timezone.utc))
    stamps = [_coverage_timestamp(row) for row in ordered]
    valid_stamps = [stamp for stamp in stamps if stamp is not None]
    valid_ends = [_coverage_end(row, stamp) for row, stamp in zip(ordered, stamps)
                  if stamp is not None and _coverage_end(row, stamp) is not None]
    unique_stamps = sorted(set(valid_stamps))
    duplicate_bars = len(valid_stamps) - len(unique_stamps)
    interval_values = []
    for row in ordered:
        try:
            interval_values.append(int(_row_value(row, "interval_seconds", 60) or 60))
        except (TypeError, ValueError, OverflowError):
            interval_values.append(None)
    non_minute_bars = sum(value != 60 for value in interval_values)

    gap_intervals_sample: list[dict[str, Any]] = []
    gap_count = 0
    gap_minutes = 0.0
    max_gap = 0.0
    for previous, current in zip(unique_stamps, unique_stamps[1:]):
        seconds = (current - previous).total_seconds()
        if seconds <= 60:
            continue
        missing = max(0.0, seconds / 60.0 - 1.0)
        gap_count += 1
        gap_minutes += missing
        max_gap = max(max_gap, missing)
        if len(gap_intervals_sample) < _MAX_GAP_SAMPLES:
            gap_intervals_sample.append({
                "from": previous.isoformat(),
                "to": current.isoformat(),
                "elapsed_minutes": seconds / 60.0,
                "missing_minutes": missing,
            })

    metadata = []
    for row in ordered:
        opened = _coerce_datetime(_row_value(row, "session_open"))
        closed = _coerce_datetime(_row_value(row, "session_close"))
        metadata.append((opened, closed))
    exact_metadata = (bool(metadata) and
                      all(opened is not None and closed is not None
                          for opened, closed in metadata) and
                      len(set(metadata)) == 1)
    metadata_conflict = len(set(metadata)) > 1
    opened, closed = metadata[0] if metadata else (None, None)
    expected_minutes: int | None = None
    expected_source = "unknown"
    early_close: bool | None = None
    caveats: set[str] = set()
    if exact_metadata and opened is not None and closed is not None:
        duration = (closed - opened).total_seconds()
        if duration > 0 and duration % 60 == 0:
            expected_minutes = int(duration / 60)
            expected_source = "exact_session_calendar"
            local_open = opened.astimezone(_NY)
            local_close = closed.astimezone(_NY)
            # A shorter duration is not by itself an early close: a delayed
            # open can also shorten a session.  Exact close time is the
            # authoritative distinction, with non-standard opens called out
            # separately instead of being mislabeled.
            early_close = local_close.time().replace(tzinfo=None) < time(16, 0)
            if local_open.time().replace(tzinfo=None) != time(9, 30):
                caveats.add("non_standard_session_open")
            if early_close:
                caveats.add("early_close_exact_calendar")
            elif local_close.time().replace(tzinfo=None) != time(16, 0):
                caveats.add("non_standard_session_close")
        else:
            caveats.add("exact_session_calendar_malformed")
            caveats.add("early_close_unknown")
    else:
        caveats.add("exact_session_calendar_conflict" if metadata_conflict else
                    "exact_session_calendar_missing")
        caveats.add("early_close_unknown")
    if non_minute_bars:
        caveats.add("non_minute_bars")
    if duplicate_bars:
        caveats.add("duplicate_timestamps")
    if gap_count:
        caveats.add("internal_bar_gaps")

    observed = len(unique_stamps)
    if expected_minutes is None:
        status = "unknown_expected"
    elif gap_count:
        status = "gapped"
    elif observed < expected_minutes:
        status = "sparse"
    elif observed > expected_minutes:
        status = "overfull"
    else:
        status = "covered"
    coverage_ratio = (observed / expected_minutes
                      if expected_minutes is not None and expected_minutes > 0
                      else None)
    first = valid_stamps[0].isoformat() if valid_stamps else None
    last = valid_stamps[-1].isoformat() if valid_stamps else None
    span = 0.0
    if valid_stamps and valid_ends:
        span = max(valid_ends).timestamp() - min(valid_stamps).timestamp()
        span = max(0.0, span / 60.0)
    return {
        "schema": BAR_COVERAGE_SCHEMA,
        "symbol": symbol,
        "session_date": session,
        "status": status,
        "observed_bars": len(rows),
        "observed_minutes": observed,
        "duplicate_timestamps": duplicate_bars,
        "expected_minutes": expected_minutes,
        "expected_minutes_source": expected_source,
        "regular_session_minutes": SESSION_MINUTES,
        "early_close": early_close,
        "coverage_ratio": coverage_ratio,
        "observed_minus_expected_minutes": (
            observed - expected_minutes if expected_minutes is not None else None),
        "first_bar": first,
        "last_bar": last,
        "span_minutes": span,
        "gap_count": gap_count,
        "gap_minutes": gap_minutes,
        "max_gap_minutes": max_gap,
        # Keep detail bounded: totals/counts above retain the complete signal,
        # while a deterministic prefix of intervals makes a sparse corpus
        # inspectable without repeating potentially hundreds of rows.
        "gap_intervals_sample": gap_intervals_sample,
        "feeds": sorted({str(_row_value(row, "feed", "unknown"))
                          for row in ordered}),
        "providers": sorted({str(_row_value(row, "provider", "unknown"))
                              for row in ordered}),
        "caveats": sorted(caveats),
    }


def bar_coverage_telemetry(bars: Sequence[Any]) -> dict[str, Any]:
    """Return deterministic per-symbol/session sparse-bar telemetry.

    Exact ``session_open``/``session_close`` metadata is the only source used
    for an authoritative expected count.  Legacy or mixed metadata therefore
    reports an unknown expected count and an explicit early-close caveat rather
    than treating a 390-minute regular day as fact.
    """
    grouped: dict[tuple[str, str], list[Any]] = {}
    for row in bars:
        stamp = _coverage_timestamp(row)
        symbol = str(_row_value(row, "symbol", "")).upper()
        session = _coverage_session(row)
        if symbol and session and stamp is not None:
            grouped.setdefault((symbol, session), []).append(row)
    records = [
        _coverage_session_record(rows, symbol=symbol, session=session)
        for (symbol, session), rows in sorted(grouped.items())
    ]
    nested: dict[str, dict[str, dict[str, Any]]] = {}
    for record in records:
        nested.setdefault(record["symbol"], {})[record["session_date"]] = record
    by_symbol: dict[str, dict[str, Any]] = {}
    for symbol in sorted(nested):
        symbol_records = [nested[symbol][session]
                          for session in sorted(nested[symbol])]
        expected = [item["expected_minutes"] for item in symbol_records
                    if item["expected_minutes"] is not None]
        known_records = [item for item in symbol_records
                         if item["expected_minutes"] is not None]
        observed = sum(int(item["observed_minutes"]) for item in symbol_records)
        observed_known = sum(int(item["observed_minutes"]) for item in known_records)
        expected_total = sum(expected) if expected else None
        by_symbol[symbol] = {
            "symbol": symbol,
            "session_count": len(symbol_records),
            "observed_bars": sum(int(item["observed_bars"]) for item in symbol_records),
            "observed_minutes": observed,
            "observed_minutes_known_sessions": observed_known,
            "expected_minutes": expected_total,
            "expected_minutes_known_sessions": len(expected),
            # Unknown-calendar sessions are not silently treated as complete
            # or incomplete against a guessed 390-minute day.
            "coverage_ratio": (observed_known / expected_total
                                if expected_total else None),
            "gap_sessions": sum(bool(item["gap_count"]) for item in symbol_records),
            "unknown_expected_sessions": sum(
                item["expected_minutes"] is None for item in symbol_records),
            "bar_count_distribution": _coverage_distribution(
                [item["observed_bars"] for item in symbol_records]),
            "minute_count_distribution": _coverage_distribution(
                [item["observed_minutes"] for item in symbol_records]),
        }
    known_expected = [item["expected_minutes"] for item in records
                      if item["expected_minutes"] is not None]
    known_records = [item for item in records
                     if item["expected_minutes"] is not None]
    observed_all = sum(int(item["observed_minutes"]) for item in records)
    observed_total = sum(int(item["observed_minutes"]) for item in known_records)
    expected_total = sum(known_expected) if known_expected else None
    caveats = sorted({caveat for item in records for caveat in item["caveats"]})
    return {
        "schema": BAR_COVERAGE_SCHEMA,
        "scope": "input_bars",
        "session_count": len(records),
        "symbol_count": len(nested),
        "observed_bars": sum(int(item["observed_bars"]) for item in records),
        "observed_minutes": observed_all,
        "observed_minutes_known_sessions": observed_total,
        "expected_minutes": expected_total,
        "expected_minutes_known_sessions": len(known_expected),
        "expected_minutes_complete": bool(records) and len(known_expected) == len(records),
        "coverage_ratio": (observed_total / expected_total
                            if expected_total else None),
        "gap_sessions": sum(bool(item["gap_count"]) for item in records),
        "unknown_expected_sessions": sum(
            item["expected_minutes"] is None for item in records),
        "bar_count_distribution": _coverage_distribution(
            [item["observed_bars"] for item in records]),
        "minute_count_distribution": _coverage_distribution(
            [item["observed_minutes"] for item in records]),
        "gap_minutes_distribution": _coverage_distribution(
            [item["gap_minutes"] for item in records]),
        "by_symbol_session": nested,
        "by_symbol": by_symbol,
        "caveats": caveats,
    }


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _timestamp(row: Any) -> datetime | None:
    raw = row.get("timestamp", row.get("ts")) if isinstance(row, Mapping) else getattr(row, "timestamp", None)
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        text = str(raw)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _session(row: Any) -> str:
    stamp = _timestamp(row)
    return stamp.astimezone(_NY).date().isoformat() if stamp else ""


def _bar_end(row: Any) -> datetime | None:
    end = getattr(row, "end", None)
    if isinstance(end, datetime):
        return end
    stamp = _timestamp(row)
    interval = getattr(row, "interval_seconds", None)
    if interval is None and isinstance(row, Mapping):
        interval = row.get("interval_seconds", 60)
    try:
        return stamp + timedelta(seconds=int(interval or 60)) if stamp else None
    except (TypeError, ValueError, OverflowError):
        return None


def _session_bars_valid(rows: Sequence[Any]) -> bool:
    """Mirror replay's stream validity checks without repairing input order."""
    if not rows:
        return False
    seen: set[datetime] = set()
    previous: datetime | None = None
    for row in rows:
        stamp = _timestamp(row)
        interval = getattr(row, "interval_seconds", None)
        if interval is None and isinstance(row, Mapping):
            interval = row.get("interval_seconds", 60)
        if stamp is None or int(interval or 0) != 60 or stamp in seen:
            return False
        if previous is not None and stamp <= previous:
            return False
        seen.add(stamp)
        previous = stamp
    return True


def _session_metadata_valid(rows: Sequence[Any]) -> bool:
    """Mirror replay's exact-calendar boundary check for one session."""
    metadata: set[tuple[datetime | None, datetime | None]] = set()
    for row in rows:
        opened = getattr(row, "session_open", None)
        closed = getattr(row, "session_close", None)
        if isinstance(row, Mapping):
            opened = row.get("session_open", opened)
            closed = row.get("session_close", closed)
        metadata.add((opened, closed))
    if not metadata:
        return False
    # ``replay_policy_for_bars`` rejects mixed presence or conflicting exact
    # calendars before signal evaluation.  A wholly legacy stream with no
    # calendar metadata remains admissible, matching that resolver.
    has_missing = any(opened is None or closed is None
                      for opened, closed in metadata)
    if has_missing:
        return len(metadata) == 1
    if len(metadata) != 1:
        return False
    opened, closed = next(iter(metadata))
    try:
        return all((_timestamp(row) is not None and
                    _bar_end(row) is not None and
                    _timestamp(row) >= opened and _bar_end(row) <= closed)
                   for row in rows)
    except TypeError:
        return False


def _contiguous(rows: Sequence[Any], start: int, stop: int) -> bool:
    if start < 0 or stop > len(rows) or start >= stop:
        return False
    stamps = [_timestamp(row) for row in rows[start:stop]]
    return all(left is not None and right is not None and
               right - left == timedelta(minutes=1)
               for left, right in zip(stamps, stamps[1:]))


def _quantiles(values: Sequence[float]) -> dict[str, Any]:
    clean = sorted(value for value in (_number(item) for item in values)
                   if value is not None)
    if not clean:
        return {"count": 0, "min": None, "p25": None, "median": None,
                "p75": None, "max": None, "mean": None}
    def percentile(fraction: float) -> float:
        index = (len(clean) - 1) * fraction
        lower, upper = math.floor(index), math.ceil(index)
        if lower == upper:
            return clean[lower]
        return clean[lower] + (clean[upper] - clean[lower]) * (index - lower)
    return {"count": len(clean), "min": clean[0],
            "p25": percentile(.25), "median": percentile(.5),
            "p75": percentile(.75), "max": clean[-1],
            "mean": sum(clean) / len(clean)}


def _ratio_summary(values: Sequence[float], *, unit: str) -> dict[str, Any]:
    result = _quantiles(values)
    result["unit"] = unit
    return result


def _flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _fit_evidence_key(bars: Sequence[Any], *, policy: Any | None) -> str:
    """Bind an alias fingerprint to the exact fit corpus and policy view."""
    names = (
        "provider", "feed", "symbol", "timestamp", "as_of", "observed_at",
        "source_mode", "interval_seconds", "open", "high", "low", "close",
        "volume", "session_open", "session_close",
    )
    vectors = []
    for row in bars:
        identity = getattr(row, "identity", None)
        values = {}
        for name in names:
            value = (row.get(name) if isinstance(row, Mapping) else
                     getattr(row, name, None))
            if value is None and identity is not None:
                value = getattr(identity, name, None)
            values[name] = value.isoformat() if isinstance(value, datetime) else value
        vectors.append(values)
    vectors.sort(key=_canonical)
    return _digest({
        "bars": vectors,
        "diagnostic_backfill": _diagnostic_backfill_enabled(policy),
    })


def _planned_vector(metadata: Mapping[str, Any], *, full: bool,
                    decimals: int = 10) -> dict[str, Any]:
    def rounded(value: Any) -> Any:
        number = _number(value)
        if number is None:
            return value
        return round(number, int(decimals))
    vector = {
        "session_date": str(metadata.get("session_date") or ""),
        "symbol": str(metadata.get("symbol") or "").upper(),
        "direction": metadata.get("direction"),
        "signal_timestamp": metadata.get("signal_timestamp"),
        "entry_price": rounded(metadata.get("entry_price")),
    }
    if metadata.get("candidate_behavior_identity") is not None:
        vector.update({
            "benchmark_symbol": metadata.get("benchmark_symbol"),
            "market_context_digest": metadata.get("market_context_digest"),
            "candidate_behavior_identity": metadata.get(
                "candidate_behavior_identity"),
        })
    if full:
        vector.update({
            "stop_distance": rounded(metadata.get("planned_stop_distance",
                                               metadata.get("stop_distance"))),
            "target_distance": rounded(metadata.get("planned_target_distance")),
            "target_r": rounded(metadata.get("target_r")),
            "max_hold_bars": metadata.get("planned_hold_bars",
                                           metadata.get("max_hold_bars")),
        })
        if metadata.get("breakeven_r") is not None:
            vector["breakeven_r"] = rounded(metadata.get("breakeven_r"))
    return vector


def _diagnostic_backfill_enabled(policy: Any | None) -> bool:
    """Resolve the explicit, non-authorizing historical-backfill switch."""
    if isinstance(policy, Mapping):
        return policy.get("allow_historical_backfill_diagnostics") is True
    return bool(getattr(policy, "allow_historical_backfill_diagnostics", False))


_PREFIX_DATA_INELIGIBLE_REASONS = frozenset({
    "historical_backfill_excluded", "feature_unavailable",
})
_PREFIX_DATA_INCOMPLETE_REASONS = frozenset({
    "insufficient_history", "signal_end_unavailable", "feature_gap",
    "entry_not_adjacent", "entry_bar_unavailable", "invalid_session",
})


def _prefix_cell_classification(*, eligible: int, signals: int,
                                reasons: Mapping[str, int]) -> str:
    """Classify one cell without turning missing data into no-edge evidence."""
    if signals > 0:
        return "actionable_signal"
    if eligible > 0:
        return "predicate_no_actionable_signal"
    if any(reasons.get(key, 0) for key in _PREFIX_DATA_INELIGIBLE_REASONS):
        return "data_ineligible"
    if any(reasons.get(key, 0) for key in _PREFIX_DATA_INCOMPLETE_REASONS):
        return "data_incomplete"
    return "data_incomplete"


def _prefix_provenance(
        *, cell_records: Mapping[str, Mapping[str, Any]],
        total_prefixes: int, eligible_prefixes: int,
        signal_prefixes: int, prefix_status: Mapping[str, int],
        ) -> dict[str, Any]:
    """Create bounded prefix eligibility provenance for downstream screens."""
    classifications = Counter(
        str(item.get("classification") or "data_incomplete")
        for item in cell_records.values())
    reason_counts: Counter[str] = Counter()
    for item in cell_records.values():
        raw = item.get("reason_counts", {})
        if isinstance(raw, Mapping):
            for reason, count in raw.items():
                try:
                    normalized = int(count)
                except (TypeError, ValueError, OverflowError):
                    continue
                if normalized > 0:
                    reason_counts[str(reason)] += normalized
    if classifications.get("data_ineligible") and classifications.get("data_incomplete"):
        status = "mixed_data_incomplete"
    elif classifications.get("data_ineligible"):
        status = "data_ineligible"
    elif classifications.get("data_incomplete"):
        status = "data_incomplete"
    elif classifications.get("actionable_signal"):
        status = "actionable_signal"
    elif classifications.get("predicate_no_actionable_signal"):
        status = "predicate_no_actionable_signal"
    else:
        status = "data_incomplete"
    # ``by_cell`` is an internal hand-off and remains bounded even when a fit
    # partition contains thousands of symbol/session cells.  Aggregate counts
    # retain the complete evidence needed for screen fail-open behavior.
    bounded_cells = {
        str(key): {
            "classification": str(value.get("classification") or
                                  "data_incomplete"),
            "reason": str(value.get("reason") or "data_incomplete"),
            "eligible_prefixes": int(value.get("eligible_prefixes", 0)),
            "signal_prefixes": int(value.get("signal_prefixes", 0)),
        }
        for key, value in sorted(cell_records.items())[:512]
    }
    truncated = len(cell_records) > len(bounded_cells)
    if truncated:
        reason_counts["provenance_cells_truncated"] += (
            len(cell_records) - len(bounded_cells))
    return {
        "schema": SIGNAL_QUALITY_ELIGIBILITY_SCHEMA,
        "scope": "fit_only",
        "authorizing": False,
        "diagnostic_only": True,
        "status": status,
        "classification": status,
        "total_cells": len(cell_records),
        "total_prefixes": int(total_prefixes),
        "eligible_cells": sum(
            item.get("eligible_prefixes", 0) > 0
            for item in cell_records.values()),
        "eligible_prefixes": int(eligible_prefixes),
        "signal_cells": sum(
            item.get("signal_prefixes", 0) > 0
            for item in cell_records.values()),
        "signal_prefixes": int(signal_prefixes),
        "data_ineligible_cells": int(classifications.get("data_ineligible", 0)),
        "data_incomplete_cells": int(classifications.get("data_incomplete", 0)),
        "predicate_no_actionable_cells": int(
            classifications.get("predicate_no_actionable_signal", 0)),
        "reason_counts": dict(sorted(reason_counts.items())[:64]),
        "prefix_counts": dict(sorted((str(key), int(value))
                                      for key, value in prefix_status.items())),
        "truncated": truncated,
        "by_cell": bounded_cells,
    }


def _immutable_market_context(
        bars: Sequence[Any],
        bars_by_symbol: Mapping[str, Sequence[Any]] | None,
        ) -> Mapping[str, tuple[Any, ...]]:
    """Freeze supplied market context, or derive it from the fit corpus."""
    if bars_by_symbol is None:
        derived: dict[str, list[Any]] = {}
        for row in bars:
            symbol = str(_row_value(row, "symbol", "")).strip().upper()
            if symbol:
                derived.setdefault(symbol, []).append(row)
        source: Mapping[str, Sequence[Any]] = derived
    elif isinstance(bars_by_symbol, Mapping):
        source = bars_by_symbol
    else:
        return MappingProxyType({})
    frozen: dict[str, tuple[Any, ...]] = {}
    for raw_symbol, raw_rows in source.items():
        symbol = str(raw_symbol).strip().upper()
        if (not symbol or symbol in frozen or isinstance(raw_rows, (str, bytes)) or
                not isinstance(raw_rows, Sequence)):
            if symbol:
                frozen[symbol] = ()
            continue
        frozen[symbol] = tuple(raw_rows)
    return MappingProxyType(frozen)


def _fit_prefixes(bars: Sequence[Any], spec: Mapping[str, Any], *,
                  policy: Any | None = None,
                  bars_by_symbol: Mapping[str, Sequence[Any]] | None = None,
                  ) -> dict[str, Any]:
    """Collect first-signal metadata and a compact all-prefix predicate funnel."""
    allow_backfill = _diagnostic_backfill_enabled(policy)
    market_context = _immutable_market_context(bars, bars_by_symbol)
    grouped: dict[tuple[str, str], list[Any]] = {}
    for row in bars:
        day = _session(row)
        symbol = str(row.get("symbol", "") if isinstance(row, Mapping)
                     else getattr(row, "symbol", "")).upper()
        if day and symbol:
            grouped.setdefault((day, symbol), []).append(row)
    needed = feature_window_bars(spec)
    if needed is None:
        needed = max(int(spec["lookback"]) + 1,
                     int(spec["atr_period"]) + 1)
    feature_window = feature_window_bars(spec)
    total_prefixes = eligible_prefixes = 0
    first_signals: list[dict[str, Any]] = []
    eligible_sessions = 0
    prefix_status: Counter[str] = Counter()
    stage_counts: dict[str, Counter[str]] = {}
    terminal_stages: Counter[str] = Counter()
    terminal_reasons: Counter[str] = Counter()
    signal_prefixes = 0
    cell_records: dict[str, dict[str, Any]] = {}
    for (day, symbol), rows in sorted(grouped.items()):
        cell = f"{symbol}|{day}"
        cell_status: Counter[str] = Counter()
        cell_eligible = 0
        cell_signals = 0
        if not _session_bars_valid(rows) or not _session_metadata_valid(rows):
            total_prefixes += max(0, len(rows) - 2)
            prefix_status["invalid_session"] += max(0, len(rows) - 2)
            cell_status["invalid_session"] += max(0, len(rows) - 2)
            cell_records[cell] = {
                "classification": "data_incomplete",
                "reason": "invalid_session",
                "reason_counts": dict(cell_status),
                "eligible_prefixes": 0,
                "signal_prefixes": 0,
            }
            continue
        rows = sorted(rows, key=lambda item: _timestamp(item) or datetime.min.replace(tzinfo=timezone.utc))
        first = None
        session_had_eligible_prefix = False
        # Replay evaluates a completed signal bar only when an immediate next
        # one-minute entry bar exists.  Keep this boundary identical so a
        # removed minute cannot become a synthetic entry opportunity.
        for index in range(1, max(1, len(rows) - 1)):
            total_prefixes += 1
            if index + 1 < int(needed):
                prefix_status["insufficient_history"] += 1
                cell_status["insufficient_history"] += 1
                continue
            signal_end = _bar_end(rows[index])
            if signal_end is None:
                prefix_status["signal_end_unavailable"] += 1
                cell_status["signal_end_unavailable"] += 1
                continue
            feature_start = (0 if feature_window is None else
                             max(0, index + 1 - int(feature_window)))
            feature_rows = rows[feature_start:index + 1]
            if (not allow_backfill and any(
                    historical_backfill_record(item)
                    for item in feature_rows)):
                prefix_status["historical_backfill_excluded"] += 1
                cell_status["historical_backfill_excluded"] += 1
                continue
            available = [replay_available_at(
                            item,
                            allow_historical_backfill_diagnostics=allow_backfill)
                         for item in feature_rows]
            if any(item is None for item in available):
                reason = (
                    "historical_backfill_excluded"
                    if (not allow_backfill and any(
                        historical_backfill_record(item)
                        for item in feature_rows)) else
                    "feature_unavailable")
                prefix_status[reason] += 1
                cell_status[reason] += 1
                continue
            decision_timestamp = max([signal_end, *available])
            if not _contiguous(rows, feature_start, index + 1):
                prefix_status["feature_gap"] += 1
                cell_status["feature_gap"] += 1
                continue
            entry = rows[index + 1]
            if _timestamp(entry) != signal_end and decision_timestamp <= signal_end:
                prefix_status["entry_not_adjacent"] += 1
                cell_status["entry_not_adjacent"] += 1
                continue
            entry_at = signal_end if decision_timestamp <= signal_end else decision_timestamp
            entry_index = next((probe for probe in range(index + 1, len(rows))
                                if (_timestamp(rows[probe]) is not None and
                                    _timestamp(rows[probe]) >= entry_at)), None)
            if entry_index is None:
                prefix_status["entry_bar_unavailable"] += 1
                cell_status["entry_bar_unavailable"] += 1
                continue
            entry = rows[entry_index]
            eligible_prefixes += 1
            cell_eligible += 1
            prefix_status["eligible"] += 1
            session_had_eligible_prefix = True
            if spec["family"] == "cross_sectional_residual":
                trace = evaluate_rule_signal_trace(
                    rows[:index + 1], spec, bars_by_symbol=market_context,
                    symbol=symbol)
            else:
                trace = evaluate_rule_signal_trace(rows[:index + 1], spec)
            stages = trace.get("stages") or []
            for item in stages:
                name = str(item.get("stage") or "unknown")
                counts = stage_counts.setdefault(name, Counter())
                counts["tested"] += 1
                counts["passed" if item.get("passed") is True else "failed"] += 1
            if stages:
                terminal = stages[-1]
                terminal_stages[str(terminal.get("stage") or "unknown")] += 1
                terminal_reasons[str(terminal.get("reason") or "unknown")] += 1
            if trace.get("signal") is not None:
                signal_prefixes += 1
                cell_signals += 1
            if trace.get("signal") is not None and first is None:
                metadata = (evaluate_rule_signal_metadata(
                    rows[:index + 1], spec, bars_by_symbol=market_context,
                    symbol=symbol)
                    if spec["family"] == "cross_sectional_residual" else
                    evaluate_rule_signal_metadata(rows[:index + 1], spec))
            else:
                metadata = None
            if metadata is not None:
                first = {**metadata, "session_date": day, "symbol": symbol,
                         # These indices are an internal hand-off to the
                         # signal-quality diagnostic. They refer to this
                         # sorted symbol/session slice and avoid evaluating
                         # every prefix a second time. No bar rows are
                         # retained in the metadata contract.
                         "session": day,
                         "signal_index": index,
                         "entry_index": entry_index,
                         "decision_timestamp": decision_timestamp.isoformat(),
                         "entry_timestamp": entry_at.isoformat(),
                         # Fit probes carry no executable quote index.  Keep
                         # the planned signal/economics, but make the pricing
                         # requirement explicit instead of reading delayed
                         # entry-bar OHLC as a fabricated fill.
                         "entry_pricing": ("bar" if replay_record_is_available(
                                                entry, _timestamp(entry),
                                                allow_historical_backfill_diagnostics=allow_backfill)
                                            else "quote_required"),
                         "entry_bar_available": replay_record_is_available(
                             entry, _timestamp(entry),
                             allow_historical_backfill_diagnostics=allow_backfill)}
        if first is not None:
            first_signals.append(first)
        if session_had_eligible_prefix:
            eligible_sessions += 1
        cell_records[cell] = {
            "classification": _prefix_cell_classification(
                eligible=cell_eligible, signals=cell_signals,
                reasons=cell_status),
            "reason": ("actionable_signal" if cell_signals else
                       "no_actionable_signal" if cell_eligible else
                       next((reason for reason in (
                           "historical_backfill_excluded", "feature_unavailable",
                           "entry_bar_unavailable", "entry_not_adjacent",
                           "feature_gap", "invalid_session",
                           "signal_end_unavailable", "insufficient_history")
                            if cell_status.get(reason)),
                            "data_incomplete")),
            "reason_counts": dict(cell_status),
            "eligible_prefixes": cell_eligible,
            "signal_prefixes": cell_signals,
        }
    eligibility_by_symbol: dict[str, dict[str, Any]] = {}
    if spec["family"] == "cross_sectional_residual":
        symbol_rows: dict[str, list[Any]] = {}
        for (day, symbol), rows in grouped.items():
            symbol_rows.setdefault(symbol, []).extend(rows)
        for symbol, rows in sorted(symbol_rows.items()):
            eligibility_by_symbol[symbol] = {
                **cross_sectional_symbol_eligibility(
                    symbol, rows=rows, spec=spec),
                "session_count": len({
                    _session(row) for row in rows if _session(row)}),
                "signal_count": sum(
                    1 for item in first_signals
                    if str(item.get("symbol", "")).upper() == symbol),
            }
    funnel = {
        name: {"tested": int(counts["tested"]),
               "passed": int(counts["passed"]),
               "failed": int(counts["failed"]),
               "pass_rate": (counts["passed"] / counts["tested"]
                             if counts["tested"] else None)}
        for name, counts in stage_counts.items()
    }
    eligibility_provenance = _prefix_provenance(
        cell_records=cell_records,
        total_prefixes=total_prefixes,
        eligible_prefixes=eligible_prefixes,
        signal_prefixes=signal_prefixes,
        prefix_status=prefix_status)
    return {"total_prefixes": total_prefixes,
            "eligible_prefixes": eligible_prefixes,
            "eligible_sessions": eligible_sessions,
            "first_signals": first_signals,
            "needed_prefix_bars": int(needed),
            "signal_prefixes": signal_prefixes,
            "prefix_status_counts": dict(sorted(prefix_status.items())),
            "predicate_funnel": funnel,
            "terminal_stage_counts": dict(sorted(terminal_stages.items())),
            "terminal_reason_counts": dict(sorted(terminal_reasons.items())),
            "eligibility_by_symbol": eligibility_by_symbol,
            "eligibility_provenance": eligibility_provenance}


def _risk_value(row: Mapping[str, Any]) -> float | None:
    return _number(row.get("risk_usd", row.get(
        "delivered_risk_usd", row.get("delivered_risk",
                                       row.get("risk_budget")))))


def _configured_cost_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values: list[float] = []
    risks: list[float] = []
    for row in rows:
        if row.get("no_trade") is True:
            continue
        cost = _number(row.get("costs", row.get("cost",
                                              row.get("transaction_cost"))))
        risk = _risk_value(row)
        if cost is None or risk is None or risk <= 0:
            continue
        values.append(cost)
        risks.append(risk)
    total = sum(values)
    risk_total = sum(risks)
    return {"total_cost": total, "mean_cost": total / len(values) if values else None,
            "cost_to_risk_ratio": total / risk_total if risk_total > 0 else None,
            "eligible_rows": len(values), "unit": "usd"}


def _record_identity(record: Any, *, leg: str | None = None) -> tuple[str | None, str | None]:
    """Return provider/feed without assuming a concrete market-record type."""
    if leg is not None and isinstance(record, Mapping):
        provider = record.get(f"{leg}_provider")
        feed = record.get(f"{leg}_feed", record.get(f"{leg}_option_feed"))
    elif isinstance(record, Mapping):
        provider = record.get("provider")
        feed = record.get("feed", record.get("feed_id"))
    else:
        provider = getattr(record, "provider", None)
        feed = getattr(record, "feed", None)
        identity = getattr(record, "identity", None)
        if provider is None and identity is not None:
            provider = getattr(identity, "provider", None)
        if feed is None and identity is not None:
            feed = getattr(identity, "feed", None)
    provider = str(provider).strip() if provider not in (None, "") else None
    feed = str(feed).strip() if feed not in (None, "") else None
    return provider, feed


def _provenance_summary(records: Sequence[Any], *, fill_rows: bool = False) -> dict[str, Any]:
    """Aggregate provider/feed identity for fit evidence, never as a gate."""
    providers: Counter[str] = Counter()
    feeds: Counter[str] = Counter()
    pairs: Counter[str] = Counter()
    unknown = 0
    observations = 0
    for record in records:
        legs = ("entry", "exit") if fill_rows else (None,)
        for leg in legs:
            provider, feed = _record_identity(record, leg=leg)
            observations += 1
            if provider is None or feed is None:
                unknown += 1
                continue
            providers[provider] += 1
            feeds[feed] += 1
            pairs[f"{provider}/{feed}"] += 1
    return {
        "observations": observations,
        "providers": dict(sorted(providers.items())),
        "feeds": dict(sorted(feeds.items())),
        "provider_feed": dict(sorted(pairs.items())),
        "unknown": unknown,
        "diagnostic_only": True,
    }


def _entry_pricing_summary(signals: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    for item in signals:
        source = str(item.get("entry_pricing") or "unknown").strip().lower()
        if source not in {"bar", "quote_required"}:
            source = "unknown"
        counts[source] += 1
    return {
        "signals": len(signals),
        "source_counts": dict(sorted(counts.items())),
        "bar_available": counts.get("bar", 0),
        "quote_required": counts.get("quote_required", 0),
        "unknown": counts.get("unknown", 0),
        "diagnostic_only": True,
    }


def _realized_fill_pricing(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Describe how replayed trades were actually priced.

    Planned signal metadata can only say whether a boundary bar was visible or
    whether an executable quote would be required.  Account rows carry the
    realized fill source for each leg.  Keeping those two concepts separate
    prevents a bar-priced intrabar stop (where OHLC has no exact trigger time)
    from being mistaken for a failed quote lookup.
    """
    executed = [row for row in rows if row.get("no_trade") is not True]
    entry_sources: Counter[str] = Counter()
    exit_sources: Counter[str] = Counter()
    source_pairs: Counter[str] = Counter()
    entry_feeds: Counter[str] = Counter()
    exit_feeds: Counter[str] = Counter()
    entry_providers: Counter[str] = Counter()
    exit_providers: Counter[str] = Counter()
    pnl_by_pair: dict[str, float] = {}
    entry_ages: list[float] = []
    exit_ages: list[float] = []
    for row in executed:
        entry = str(row.get("entry_fill_source") or "unknown").strip().lower()
        exit_source = str(row.get("exit_fill_source") or "unknown").strip().lower()
        entry = entry if entry in {"bar", "quote"} else "unknown"
        exit_source = exit_source if exit_source in {"bar", "quote"} else "unknown"
        pair = f"{entry}->{exit_source}"
        entry_sources[entry] += 1
        exit_sources[exit_source] += 1
        source_pairs[pair] += 1
        pnl = _number(row.get("net_pnl"))
        if pnl is not None:
            pnl_by_pair[pair] = pnl_by_pair.get(pair, 0.0) + pnl
        for counter, raw in (
                (entry_feeds, row.get("entry_feed", row.get("entry_option_feed"))),
                (exit_feeds, row.get("exit_feed", row.get("exit_option_feed"))),
                (entry_providers, row.get("entry_provider")),
                (exit_providers, row.get("exit_provider"))):
            if raw not in (None, ""):
                counter[str(raw)] += 1
        entry_age = _number(row.get("entry_quote_age_seconds"))
        exit_age = _number(row.get("exit_quote_age_seconds"))
        if entry == "quote" and entry_age is not None:
            entry_ages.append(entry_age)
        if exit_source == "quote" and exit_age is not None:
            exit_ages.append(exit_age)
    both_quote = source_pairs.get("quote->quote", 0)
    both_bar = source_pairs.get("bar->bar", 0)
    mixed = sum(count for pair, count in source_pairs.items()
                if pair in {"quote->bar", "bar->quote"})
    return {
        "executed_rows": len(executed),
        "entry_source_counts": dict(sorted(entry_sources.items())),
        "exit_source_counts": dict(sorted(exit_sources.items())),
        "source_pair_counts": dict(sorted(source_pairs.items())),
        "both_quote": both_quote,
        "both_bar": both_bar,
        "mixed": mixed,
        "entry_quote_age_seconds": _quantiles(entry_ages),
        "exit_quote_age_seconds": _quantiles(exit_ages),
        "entry_feed_counts": dict(sorted(entry_feeds.items())),
        "exit_feed_counts": dict(sorted(exit_feeds.items())),
        "entry_provider_counts": dict(sorted(entry_providers.items())),
        "exit_provider_counts": dict(sorted(exit_providers.items())),
        "net_pnl_by_source_pair": dict(sorted(pnl_by_pair.items())),
        "intrabar_bar_exit_caveat": (
            "bar exits can be intentional for intrabar stop/target events "
            "whose exact quote trigger time is unavailable"),
        "authorizing": False,
        "diagnostic_only": True,
    }


def _expected_cost_hurdle_bps(costs: Any, *, vehicle: str,
                              executable_quotes: bool = False) -> float | None:
    """Return the configured symmetric bar-reference hurdle when meaningful."""
    if vehicle != "equity" or costs is None:
        return None
    round_trip = getattr(costs, "round_trip_cost", None)
    if not callable(round_trip):
        return None
    try:
        value = float(round_trip(
            1.0, 1.0, 1.0, 1.0, vehicle="equity",
            executable_quotes=bool(executable_quotes)) * 10_000.0)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _stress_controls(risk_config: Mapping[str, Any] | None) -> tuple[float | None, float | None]:
    """Resolve configured stress controls while retaining fail-closed unknowns."""
    source = risk_config if isinstance(risk_config, Mapping) else {}
    risk = source.get("risk", source)
    risk = risk if isinstance(risk, Mapping) else {}
    scenario = _number(risk.get("stressed_cost_scenario_bps"))
    limit = _number(risk.get("max_stressed_cost_to_risk_ratio"))
    if scenario is None and "stressed_cost_scenario_bps" not in risk:
        scenario = 25.0
    if limit is None and "max_stressed_cost_to_risk_ratio" not in risk:
        limit = 0.30
    if scenario not in {9.0, 15.0, 25.0, 50.0}:
        scenario = None
    if limit is not None and limit < 0:
        limit = None
    return scenario, limit


def _stressed_cost_summary(rows: Sequence[Mapping[str, Any]], scenario_bps: float,
                           *, vehicle: str, costs: Any,
                           configured_limit: float | None = None) -> dict[str, Any]:
    values: list[float] = []
    risks: list[float] = []
    status_counts = {"pass": 0, "fail": 0, "unknown": 0}
    considered = 0
    for row in rows:
        if row.get("no_trade") is True:
            continue
        considered += 1
        quantity = _number(row.get("quantity", row.get("contracts", 1.0))) or 1.0
        multiplier = _number(row.get(
            "contract_multiplier",
            row.get("multiplier", 100.0 if vehicle == "option" else 1.0))) or 1.0
        plan_entry = _number(row.get("plan_entry", row.get(
            "entry_price", row.get("entry_reference"))))
        notional = _number(row.get("planned_notional"))
        if notional is None and plan_entry is not None:
            notional = abs(plan_entry) * abs(quantity) * abs(multiplier)
        risk = _risk_value(row)
        if notional is None or risk is None or risk <= 0:
            status_counts["unknown"] += 1
            continue
        try:
            stress = stressed_cost_usd(
                entry_notional=notional, scenario_bps=scenario_bps,
                vehicle=vehicle, quantity=quantity, costs=costs)
        except (TypeError, ValueError, OverflowError):
            status_counts["unknown"] += 1
            continue
        values.append(stress)
        risks.append(risk)
        if configured_limit is None:
            status_counts["unknown"] += 1
        elif stress / risk <= configured_limit:
            status_counts["pass"] += 1
        else:
            status_counts["fail"] += 1
    total = sum(values)
    risk_total = sum(risks)
    return {"bps": float(scenario_bps), "total_cost": total,
            "mean_cost": total / len(values) if values else None,
            "cost_to_risk_ratio": total / risk_total if risk_total > 0 else None,
            "eligible_rows": len(values), "rows_considered": considered,
            "row_status": status_counts,
            "row_counts": dict(status_counts),
            "pass_rows": status_counts["pass"],
            "fail_rows": status_counts["fail"],
            "unknown_rows": status_counts["unknown"],
            "configured_limit": configured_limit,
            "basis_schema": STRESSED_COST_SCHEMA,
            "basis": dict(STRESSED_COST_BASIS), "unit": "usd"}


def _risk_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    intended: list[float] = []
    planned_risk: list[float] = []
    delivered: list[float] = []
    planned_to_budget: list[float] = []
    delivered_to_planned: list[float] = []
    opportunity_budgets: list[float] = []
    opportunity_planned: list[float] = []
    opportunity_utilization: list[float] = []
    cap_binding = cap_evaluated = 0
    risk_sized_quantities: list[float] = []
    cap_quantities: list[float] = []
    for row in rows:
        budget = _number(row.get("risk_budget", row.get(
            "intended_risk_usd", row.get("intended_risk"))))
        actual = _number(row.get("risk_usd", row.get(
            "delivered_risk_usd", row.get("delivered_risk"))))
        planned = _number(row.get("planned_risk_usd", row.get(
            "nominal_risk_usd", actual)))
        if budget is not None and budget >= 0:
            opportunity_budgets.append(budget)
        if planned is not None and planned >= 0:
            opportunity_planned.append(planned)
        utilization = _number(row.get("risk_budget_utilization"))
        if utilization is None and planned is not None and budget not in {None, 0.0}:
            utilization = planned / float(budget)
        if utilization is not None and utilization >= 0:
            opportunity_utilization.append(utilization)
        if row.get("notional_cap_quantity") is not None:
            cap_evaluated += 1
            cap_binding += int(row.get("notional_cap_binding") is True)
        risk_quantity = _number(row.get("risk_sized_quantity"))
        cap_quantity = _number(row.get("notional_cap_quantity"))
        if risk_quantity is not None:
            risk_sized_quantities.append(risk_quantity)
        if cap_quantity is not None:
            cap_quantities.append(cap_quantity)
        if row.get("no_trade") is True:
            continue
        if budget is not None and budget >= 0:
            intended.append(budget)
        if actual is not None and actual >= 0:
            delivered.append(actual)
        if planned is not None and planned >= 0:
            planned_risk.append(planned)
        if planned is not None and planned >= 0 and budget not in {None, 0.0}:
            planned_to_budget.append(planned / float(budget))
        if planned is not None and planned > 0 and actual is not None and actual >= 0:
            delivered_to_planned.append(actual / planned)
    configured_summary = _ratio_summary(intended, unit="risk_usd")
    planned_summary = _ratio_summary(planned_risk, unit="risk_usd")
    capped_summary = _ratio_summary(delivered, unit="risk_usd")
    result = {
        # Explicit current names make the notional-cap interaction readable.
        # Keep the older aliases because the fit-diagnostics schema is
        # append-only and existing report consumers already understand them.
        "configured": configured_summary,
        "planned": planned_summary,
        "capped_delivered": capped_summary,
        "intended": configured_summary,
        "delivered": capped_summary,
    }
    configured_ratio = _ratio_summary(planned_to_budget, unit="ratio")
    delivered_ratio = _ratio_summary(delivered_to_planned, unit="ratio")
    result["planned_to_configured"] = configured_ratio
    result["delivered_to_configured"] = configured_ratio
    result["delivered_to_intended"] = configured_ratio
    result["delivered_to_planned"] = delivered_ratio
    result["notional_cap"] = {
        "evaluated_rows": cap_evaluated,
        "binding_rows": cap_binding,
        "binding_rate": cap_binding / cap_evaluated if cap_evaluated else None,
        "risk_sized_quantity": _quantiles(risk_sized_quantities),
        "cap_quantity": _quantiles(cap_quantities),
        "diagnostic_only": True,
        "authorizing": False,
    }
    result["sizing_opportunities"] = {
        "rows": len(opportunity_planned),
        "configured_budget": _ratio_summary(
            opportunity_budgets, unit="risk_usd"),
        "planned_risk": _ratio_summary(opportunity_planned, unit="risk_usd"),
        "planned_to_budget": _ratio_summary(
            opportunity_utilization, unit="ratio"),
        "diagnostic_only": True,
        "authorizing": False,
    }
    return result


def _exit_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    trades = [row for row in rows if row.get("no_trade") is not True]
    reasons = Counter(str(row.get("exit_reason") or "unknown") for row in trades)
    ties = sum(_flag(row.get("tie_broken")) for row in trades)
    entry_gaps = sum(_flag(row.get("entry_gap_fill")) for row in trades)
    exit_gaps = sum(_flag(row.get("exit_gap_fill")) for row in trades)
    discontinuity_exits = sum(
        _flag(row.get("hold_discontinuity_exit",
                    row.get("hold_discontinuity"))) for row in trades)
    time_expiry_exits = sum(
        str(row.get("hold_exit_reason") or "") == "time_expiry"
        for row in trades)
    count = len(trades)
    return {
        "trades": count,
        "reasons": dict(sorted(reasons.items())),
        "reason_rates": {key: value / count for key, value in sorted(reasons.items())}
                         if count else {},
        "ties": ties, "tie_rate": ties / count if count else 0.0,
        "entry_gaps": entry_gaps,
        "entry_gap_rate": entry_gaps / count if count else 0.0,
        "exit_gaps": exit_gaps,
        "exit_gap_rate": exit_gaps / count if count else 0.0,
        # ``exit_reason`` remains the compatibility field.  These additive
        # counters make a time-like exit caused by a sparse hold explicit.
        "hold_discontinuity_exits": discontinuity_exits,
        "hold_discontinuity_exit_rate": (discontinuity_exits / count
                                          if count else 0.0),
        "time_expiry_exits": time_expiry_exits,
        "time_expiry_rate": time_expiry_exits / count if count else 0.0,
        "hold_termination_counts": {
            "discontinuity": discontinuity_exits,
            "time_expiry": time_expiry_exits,
        },
        "diagnostic_only": True,
        "authorizing": False,
    }


def _execution_rejection_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate fit-only execution outcomes, including rejected rows.

    Risk/cost summaries intentionally operate on executable fills and
    therefore skip ``no_trade`` rows.  Proposal diagnostics still need to
    distinguish a genuinely sparse signal from a populated fit whose every
    opportunity was refused at execution, so retain a compact reason count
    here.  The input is already the fit partition; no held-out/sealed data is
    consulted.
    """
    no_trade = [row for row in rows if row.get("no_trade") is True]
    no_signal = [row for row in no_trade
                 if row.get("execution_disposition") == "no_signal"]
    refused = [row for row in no_trade
               if (row.get("execution_disposition") == "refused" or
                   (not row.get("execution_disposition") and
                    row.get("reject_reason")))]
    unclassified = [row for row in no_trade
                    if (row.get("execution_disposition") not in {
                            "no_signal", "refused"} and
                        not row.get("reject_reason"))]
    reasons = Counter(str(row.get("reject_reason") or "unknown")
                      for row in refused)
    explicit = sum(1 for row in refused
                   if str(row.get("reject_reason") or "").strip())
    executed = len(rows) - len(no_trade)
    blocked = (bool(refused) and executed == 0 and not unclassified and
               explicit == len(refused) and
               all(row.get("signal_opportunity") is not False
                   for row in refused))
    result = {
        "rows": len(rows),
        "executed_rows": executed,
        "no_trade_rows": len(no_trade),
        "no_signal_rows": len(no_signal),
        "unclassified_no_trade_rows": len(unclassified),
        "explicit_rejections": explicit,
        "reject_reason_counts": dict(sorted(reasons.items())),
        "execution_blocked": blocked,
        "diagnostic_only": True,
        "authorizing": False,
    }
    return result


def _mde(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    deltas: list[float] = []
    clusters: list[str] = []
    for row in rows:
        if row.get("no_trade") is True:
            continue
        value = _number(row.get("r_multiple"))
        cluster = str(row.get("session_date") or "")
        if value is not None and cluster:
            deltas.append(value)
            clusters.append(cluster)
    if not deltas:
        return {"available": False, "reason": "no_valid_fit_effect",
                "effect_unit": "r_multiple_per_trade",
                "cluster_unit": "session", "diagnostic_only": True,
                "authorizing": False}
    return clustered_mde_power_report(
        deltas, clusters, target_effect=.05, alpha=.05,
        effect_unit="r_multiple_per_trade", cluster_unit="session")


def measure_fit_diagnostics(
        bars: Sequence[Any], spec: Mapping[str, Any], *,
        account_rows: Sequence[Mapping[str, Any]] = (),
        costs: Any | None = None, vehicle: str | None = None,
        risk_config: Mapping[str, Any] | None = None,
        config: Mapping[str, Any] | None = None,
        policy: Any | None = None,
        bars_by_symbol: Mapping[str, Sequence[Any]] | None = None,
        ) -> dict[str, Any]:
    """Measure compact fit-only behavior and execution summaries.

    ``bars`` and ``account_rows`` must already be the fit/development slice.
    This function intentionally has no partitioning or sealed-window access;
    callers own that boundary.
    """
    normalized = validate_rule_spec(spec)
    bar_rows = list(bars)
    market_context = _immutable_market_context(bar_rows, bars_by_symbol)
    prefix = _fit_prefixes(
        bar_rows, normalized, policy=policy, bars_by_symbol=market_context)
    signals = prefix["first_signals"]
    entry_vectors = [_planned_vector(item, full=False) for item in signals]
    full_vectors = [_planned_vector(item, full=True) for item in signals]
    alias_entry_vectors = [
        _planned_vector(item, full=False,
                        decimals=FIT_BEHAVIOR_ALIAS_DECIMALS)
        for item in signals]
    alias_full_vectors = [
        _planned_vector(item, full=True,
                        decimals=FIT_BEHAVIOR_ALIAS_DECIMALS)
        for item in signals]
    eligible = int(prefix["eligible_prefixes"])
    total = int(prefix["total_prefixes"])
    floor_count = sum(bool(item.get("floor_binding")) for item in signals)
    atr_values = [item.get("atr_bps") for item in signals]
    planned = {
        "stop_distance": _quantiles([item.get("planned_stop_distance") for item in signals]),
        "target_distance": _quantiles([item.get("planned_target_distance") for item in signals]),
        "target_r": _quantiles([item.get("target_r") for item in signals]),
        "hold_bars": _quantiles([item.get("planned_hold_bars") for item in signals]),
    }
    if normalized.get("breakeven_r") is not None:
        planned["breakeven_r"] = normalized["breakeven_r"]
    rows = [dict(row) for row in account_rows if isinstance(row, Mapping)]
    # ``simulate_account`` attaches path telemetry to executed rows.  Keep
    # this aggregate compact and fit-only; callers supplying legacy rows get
    # an empty diagnostic rather than an inferred path.
    path_rows: list[Mapping[str, Any]] = []
    for row in rows:
        nested = row.get("path_telemetry")
        if isinstance(nested, Mapping):
            path_rows.append(nested)
        elif row.get("entry_timestamp") is not None:
            measured = compute_path_telemetry(row, bar_rows)
            if measured.get("available"):
                path_rows.append(measured)
    path_telemetry = aggregate_path_telemetry(path_rows)
    target_hold = target_hold_reachability(
        path_rows,
        target_r=normalized.get("target_r"),
        max_hold_bars=normalized.get("max_hold_bars"),
    )
    controls_input = risk_config if risk_config is not None else config
    stress_scenario, stress_limit = _stress_controls(controls_input)
    resolved_vehicle = vehicle or next(
        (str(row.get("vehicle")) for row in rows
         if str(row.get("vehicle")) in {"equity", "option"}), "equity")
    configured = _configured_cost_summary(rows)
    stressed = {str(int(scenario_bps)): _stressed_cost_summary(
        rows, scenario_bps, vehicle=resolved_vehicle, costs=costs,
        configured_limit=stress_limit)
                for scenario_bps in COST_STRESS_MULTIPLIERS}
    provenance = _provenance_summary(bar_rows)
    if rows:
        provenance["fills"] = _provenance_summary(rows, fill_rows=True)
    pricing = _entry_pricing_summary(signals)
    realized_pricing = _realized_fill_pricing(rows)
    execution_rejections = _execution_rejection_summary(rows)
    signal_quality = measure_signal_quality(
        bar_rows, normalized, policy=policy,
        cost_hurdle_bps=_expected_cost_hurdle_bps(
            costs, vehicle=resolved_vehicle),
        precomputed_first_signals=(
            None if normalized["family"] == "cross_sectional_residual" else
            signals),
        eligibility_provenance=(
            None if normalized["family"] == "cross_sectional_residual" else
            prefix.get("eligibility_provenance")),
        bars_by_symbol=market_context)
    expected_cost = {
        "bar_reference_round_trip_bps": _expected_cost_hurdle_bps(
            costs, vehicle=resolved_vehicle, executable_quotes=False),
        "executable_quote_round_trip_bps": _expected_cost_hurdle_bps(
            costs, vehicle=resolved_vehicle, executable_quotes=True),
        "stress_scenario_bps": stress_scenario,
        "expected_cost_independent_of_stress": True,
        "authorizing": False,
        "diagnostic_only": True,
    }
    configured_stress = (stressed.get(str(int(stress_scenario)), {})
                         if stress_scenario is not None else {})
    configured_status = configured_stress.get(
        "row_status", {"pass": 0, "fail": 0, "unknown": len(rows)})
    required_stop_bps = (
        float(stress_scenario) / float(stress_limit)
        if (resolved_vehicle == "equity" and stress_scenario is not None and
            stress_limit is not None and stress_limit > 0) else None)
    fit_diagnostics = {
        "schema": FIT_DIAGNOSTICS_SCHEMA,
        "scope": "fit_only",
        "authorizing": False,
        "diagnostic_only": True,
        "variant_id": rule_variant_id(normalized),
        "eligible_prefix": {
            "eligible": eligible, "total": total,
            "rate": eligible / total if total else 0.0,
            "needed_prefix_bars": prefix["needed_prefix_bars"],
            "status_counts": prefix["prefix_status_counts"],
            "eligibility_provenance": dict(
                prefix.get("eligibility_provenance") or {}),
        },
        "eligibility_provenance": dict(
            prefix.get("eligibility_provenance") or {}),
        "first_signal": {
            "signals": len(signals),
            "eligible_sessions": int(prefix["eligible_sessions"]),
            "rate": (len(signals) / int(prefix["eligible_sessions"])
                     if prefix["eligible_sessions"] else 0.0),
            "session_rate": (len(signals) / int(prefix["eligible_sessions"])
                              if prefix["eligible_sessions"] else 0.0),
            "prefix_rate": (len(signals) / eligible if eligible else 0.0),
            "session_count": len({_session(row) for row in bar_rows if _session(row)}),
            "signal_prefixes": int(prefix["signal_prefixes"]),
        },
        "predicate_funnel": {
            "schema": "rule-predicate-funnel.v1",
            "scope": "eligible_fit_prefixes",
            "stages": prefix["predicate_funnel"],
            "terminal_stage_counts": prefix["terminal_stage_counts"],
            "terminal_reason_counts": prefix["terminal_reason_counts"],
            "authorizing": False,
            "diagnostic_only": True,
        },
        "signal_quality": signal_quality,
        "expected_cost": expected_cost,
        "atr_bps": _quantiles(atr_values),
        "floor_30bps": {
            "bps": float(MIN_STOP_DISTANCE_BPS),
            "binding": floor_count, "signals": len(signals),
            "rate": floor_count / len(signals) if signals else 0.0,
        },
        "planned": planned,
        "vehicle": resolved_vehicle,
        "provenance": provenance,
        "entry_pricing": pricing,
        "realized_fill_pricing": realized_pricing,
        "pricing": {"entry": dict(pricing),
                    "realized": dict(realized_pricing),
                    "diagnostic_only": True},
        "risk_controls": {
            "stressed_cost_scenario_bps": stress_scenario,
            "max_stressed_cost_to_risk_ratio": stress_limit,
            "scenario_bps": stress_scenario,
            "limit": stress_limit,
            "required_static_stop_distance_bps": required_stop_bps,
            "grammar_stop_floor_bps": float(MIN_STOP_DISTANCE_BPS),
            "grammar_stop_floor_admissible": (
                None if required_stop_bps is None else
                float(MIN_STOP_DISTANCE_BPS) >= required_stop_bps),
            "configured_stress": {
                "scenario_bps": stress_scenario,
                "max_cost_to_risk_ratio": stress_limit,
            },
            "basis_schema": STRESSED_COST_SCHEMA,
            "basis": dict(STRESSED_COST_BASIS),
            "row_status": configured_status,
            "row_counts": dict(configured_status),
            "pass_rows": configured_status["pass"],
            "fail_rows": configured_status["fail"],
            "unknown_rows": configured_status["unknown"],
            "authorizing": False,
            "diagnostic_only": True,
        },
        # Unlike ``risk_controls`` (which is fill-only), this aggregate keeps
        # every fit no-trade row and its explicit reason.  It is descriptive
        # context for proposal ordering, never an authorization signal.
        "execution_rejections": execution_rejections,
        "cost_to_risk": {"configured": configured, "stressed": stressed},
        "risk": _risk_summary(rows),
        "exits": _exit_summary(rows),
        "path_telemetry": path_telemetry,
        # This is a compact, fit-only projection of path telemetry.  It
        # contains no rows and is descriptive only; mutation code may use it
        # solely after coordinate exhaustion and all normal gates still run.
        "target_hold_reachability": target_hold,
        "exit_grammar": audit_exit_grammar(normalized),
        "mde_power": _mde(rows),
        "behavior_fingerprint": {
            "fit_evidence_key": _fit_evidence_key(
                bar_rows, policy=policy),
            "entry": _digest(entry_vectors),
            "full": _digest(full_vectors),
            "entry_alias_key": _digest(alias_entry_vectors),
            "full_alias_key": _digest(alias_full_vectors),
            "alias_schema": FIT_BEHAVIOR_ALIAS_SCHEMA,
            "alias_numeric_decimals": FIT_BEHAVIOR_ALIAS_DECIMALS,
            "signal_count": len(signals),
            "planned_vector_count": len(full_vectors),
        },
    }
    if normalized["family"] == "cross_sectional_residual":
        context_rejections = {
            reason: int(count)
            for reason, count in prefix["terminal_reason_counts"].items()
            if str(reason).startswith(("benchmark_context_",
                                       "subject_context_",
                                       "subject_ineligible:")) and
            not str(reason).startswith("subject_context_ineligible:")
        }
        reason = (max(sorted(context_rejections),
                      key=context_rejections.get)
                  if context_rejections else "synchronized_context")
        fit_diagnostics["market_context"] = {
            "benchmark_symbol": CROSS_SECTIONAL_BENCHMARK,
            "status": ("unknown" if not signals and context_rejections else
                       "partial" if context_rejections else "complete"),
            "reason": reason,
            "rejection_counts": dict(sorted(context_rejections.items())),
        }
        eligibility_by_symbol = dict(prefix.get("eligibility_by_symbol") or {})
        fit_diagnostics["eligibility"] = {
            "schema": "cross-sectional-eligibility.v1",
            "benchmark_symbol": CROSS_SECTIONAL_BENCHMARK,
            "by_symbol": eligibility_by_symbol,
            "symbol_counts": {
                "total": len(eligibility_by_symbol),
                "eligible": sum(bool(item["eligible"])
                                for item in eligibility_by_symbol.values()),
                "ineligible": sum(not bool(item["eligible"])
                                  for item in eligibility_by_symbol.values()),
            },
            "event_symbols": sorted(
                symbol for symbol, item in eligibility_by_symbol.items()
                if item.get("signal_count", 0)),
            "authorizing": False,
            "diagnostic_only": True,
        }
    # Historical backfill can be inspected only through the explicit policy
    # above. Keep that provenance visible and permanently non-authorizing so a
    # diagnostic prefix cannot advance a proof or emit an authorization.
    backfill_rows = [row for row in bar_rows if historical_backfill_record(row)]
    fit_diagnostics["historical_backfill"] = {
        "rows": len(backfill_rows),
        "diagnostic_policy": bool(_diagnostic_backfill_enabled(policy)),
        "included": bool(backfill_rows and
                          _diagnostic_backfill_enabled(policy)),
        "authorizing": False,
        "diagnostic_only": True,
    }
    # Stable descriptive aliases keep downstream reports readable while the
    # canonical sections above remain versioned and compact.
    fit_diagnostics["30bps_floor_binding"] = dict(fit_diagnostics["floor_30bps"])
    fit_diagnostics["planned_effective"] = dict(planned)
    fit_diagnostics["cost_to_risk_stressed"] = dict(stressed)
    return fit_diagnostics


def _record_spec(record: Any) -> tuple[dict[str, Any], str, str]:
    if isinstance(record, Mapping) and isinstance(record.get("rule_spec"), Mapping):
        spec = validate_rule_spec(record["rule_spec"])
        return spec, str(record.get("variant_id") or rule_variant_id(spec)), str(record.get("source") or "")
    spec = validate_rule_spec(record)
    return spec, rule_variant_id(spec), ""


def collapse_behavior_aliases(
        records: Sequence[Any], *,
        diagnostics: Mapping[str, Mapping[str, Any]] | None = None,
        freeze: bool = False,
        ) -> dict[str, Any]:
    """Return deterministic fit-behavior aliases and a kept candidate list.

    A zero-signal fingerprint is never collapsed.  For a non-empty full
    fingerprint, the canonical member is deterministic-source first, then the
    lexicographically smallest variant id/candidate key.  The default remains
    measurement-only for compatibility.  ``freeze=True`` applies the fixed
    fit-only equivalence rule before any held-out replay: one representative
    remains in ``kept`` and all exclusions are returned with their canonical
    candidate.  A diagnostic without an explicit ``scope=fit_only`` can never
    authorize that frozen reduction.
    """
    diagnostics = diagnostics or {}
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(records):
        spec, variant_id, source = _record_spec(item)
        candidate_key = str(
            item.get("candidate_key") or variant_id
            if isinstance(item, Mapping) else variant_id)
        diagnostic = diagnostics.get(candidate_key) or diagnostics.get(variant_id)
        if diagnostic is None and isinstance(item, Mapping):
            diagnostic = item.get("fit_diagnostics")
        diagnostic = diagnostic if isinstance(diagnostic, Mapping) else {}
        fingerprint = diagnostic.get("behavior_fingerprint") or {}
        try:
            signal_count = int(fingerprint.get("signal_count") or 0)
            planned_vector_count = int(
                fingerprint.get("planned_vector_count") or 0)
        except (TypeError, ValueError, OverflowError):
            signal_count = planned_vector_count = 0
        fit_only = diagnostic.get("scope") == "fit_only"
        fit_evidence_key = str(fingerprint.get("fit_evidence_key") or "")
        alias_schema = str(fingerprint.get("alias_schema") or
                           "legacy-exact-fit-behavior")
        try:
            alias_decimals = int(fingerprint.get(
                "alias_numeric_decimals", 10))
        except (TypeError, ValueError, OverflowError):
            alias_decimals = 10
        normalized.append({"record": item, "spec": spec, "variant_id": variant_id,
                           "candidate_key": candidate_key,
                           "record_key": f"{candidate_key}\x1f{index}",
                           "family": str(spec.get("family") or ""),
                           "source": source, "diagnostic": diagnostic,
                           "fit_only": fit_only,
                           "fit_evidence_key": fit_evidence_key,
                           "alias_schema": alias_schema,
                           "alias_decimals": alias_decimals,
                           "entry": str(fingerprint.get("entry_alias_key") or
                                        fingerprint.get("entry") or ""),
                           "full": str(fingerprint.get("full_alias_key") or
                                       fingerprint.get("full") or ""),
                           "signal_count": signal_count,
                           "planned_vector_count": planned_vector_count})
    groups: dict[tuple[str, str, int, str, str], list[dict[str, Any]]] = {}
    for item in normalized:
        eligible_scope = item["fit_only"] if freeze else (
            item["diagnostic"].get("scope") in {None, "fit_only"})
        if (eligible_scope and (item["fit_evidence_key"] or not freeze) and
                item["signal_count"] > 0 and
                item["planned_vector_count"] == item["signal_count"] and
                item["full"] and item["entry"]):
            groups.setdefault((item["fit_evidence_key"], item["alias_schema"],
                               item["alias_decimals"],
                               item["full"], item["entry"]), []).append(item)
    kept_record_keys = {item["record_key"] for item in normalized}
    proposed_exclusions: list[dict[str, Any]] = []
    full_aliases: list[dict[str, Any]] = []
    parameter_collapse: list[dict[str, Any]] = []
    for (fit_evidence_key, alias_schema, alias_decimals,
         full, entry), members in sorted(groups.items()):
        if len(members) < 2:
            continue
        canonical = sorted(members, key=lambda item: (
            0 if item["source"] in {
                "deterministic", "deterministic_template", "carried_forward"} else 1,
            item["variant_id"], item["candidate_key"]))[0]
        aliases = [item for item in members if item is not canonical]
        for item in aliases:
            exclusion = {
                "candidate_key": item["candidate_key"],
                "variant_id": item["variant_id"],
                "family": item["family"],
                "canonical_candidate_key": canonical["candidate_key"],
                "canonical_variant_id": canonical["variant_id"],
                "canonical_family": canonical["family"],
                "entry_fingerprint": entry,
                "full_fingerprint": full,
                "fit_evidence_key": fit_evidence_key,
                "alias_schema": alias_schema,
                "alias_numeric_decimals": alias_decimals,
                "selection_scope": "fit_only",
                "reason": "fit_behavioral_alias_frozen" if freeze else
                          "fit_behavioral_alias_proposed",
            }
            proposed_exclusions.append(exclusion)
            if freeze:
                kept_record_keys.discard(item["record_key"])
        full_aliases.append({"entry_fingerprint": entry,
                             "full_fingerprint": full,
                             "fit_evidence_key": fit_evidence_key,
                             "alias_schema": alias_schema,
                             "alias_numeric_decimals": alias_decimals,
                             "canonical_candidate_key": canonical["candidate_key"],
                             "canonical_variant_id": canonical["variant_id"],
                             "canonical_family": canonical["family"],
                             "candidate_keys": sorted(
                                 item["candidate_key"] for item in members),
                             "variant_ids": sorted(item["variant_id"] for item in members),
                             "families": sorted({item["family"] for item in members}),
                             "signal_count": max(item["signal_count"] for item in members)})
        fields = sorted({key for item in members for key in set(item["spec"])
                         if any(item["spec"].get(key) != other["spec"].get(key)
                                for other in members)})
        parameter_collapse.append({
                                   "canonical_candidate_key": canonical["candidate_key"],
                                   "canonical_variant_id": canonical["variant_id"],
                                   "candidate_keys": sorted(
                                       item["candidate_key"] for item in members),
                                   "variant_ids": sorted(item["variant_id"] for item in members),
                                   "fields": fields})
    kept = [item["record"] for item in normalized
            if item["record_key"] in kept_record_keys]
    entry_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in normalized:
        if (item["fit_only"] and item["signal_count"] > 0 and item["entry"]):
            entry_groups.setdefault(
                (item["fit_evidence_key"], item["entry"]), []).append(item)
    entry_aliases = [
        {"fit_evidence_key": fit_evidence_key,
         "entry_fingerprint": key,
         "candidate_keys": sorted(item["candidate_key"] for item in value),
         "variant_ids": sorted(item["variant_id"] for item in value),
         "families": sorted({item["family"] for item in value})}
        for (fit_evidence_key, key), value in sorted(entry_groups.items())
        if len(value) > 1]
    excluded = list(proposed_exclusions) if freeze else []
    return {"kept": kept, "excluded": excluded,
            "proposed_exclusions": proposed_exclusions,
            "entry_aliases": entry_aliases, "full_aliases": full_aliases,
            "parameter_collapse": parameter_collapse,
            "signals_present": any(item["signal_count"] > 0 for item in normalized),
            "dedup_status": ("fit_preregistered_frozen" if freeze else
                             "diagnostic_only"),
            "selection_scope": "fit_only",
            "alias_schema": FIT_BEHAVIOR_ALIAS_SCHEMA,
            "alias_numeric_decimals": FIT_BEHAVIOR_ALIAS_DECIMALS,
            "requires_operator_review": bool(proposed_exclusions) and not freeze,
            "intended_variant_count": len(normalized),
            "kept_variant_count": len(kept)}


def filter_behavior_aliases(specs: Sequence[Mapping[str, Any]],
                            diagnostics: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Convenience wrapper for factory tasks that hold specs by variant id."""
    return collapse_behavior_aliases(
        [{"rule_spec": spec, "variant_id": rule_variant_id(spec),
          "source": "deterministic" if index == 0 else ""}
         for index, spec in enumerate(specs)], diagnostics=diagnostics)


def fit_behavior_fingerprint(bars: Sequence[Any],
                             spec: Mapping[str, Any]) -> dict[str, Any]:
    """Return just the deterministic entry/full fit behavior identity."""
    return dict(measure_fit_diagnostics(bars, spec)["behavior_fingerprint"])


def audit_exit_grammar(spec: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Describe the currently executable exit grammar for operator review."""
    candidate = spec if isinstance(spec, Mapping) else {}
    try:
        normalized = validate_rule_spec(candidate) if candidate else None
    except (TypeError, ValueError):
        normalized = None
    breakeven = (normalized.get("breakeven_r")
                 if isinstance(normalized, Mapping) else None)
    unsupported = sorted(
        str(key) for key in candidate
        if any(marker in str(key).lower() for marker in (
            "exit_mode", "trailing", "partial_exit", "exit_template",
            "take_profit", "stop_loss")) and str(key) not in {
                "max_hold_bars"})
    result = {
        "schema": ("completed-close-breakeven-bracket.v1" if breakeven is not None
                   else "fixed-atr-floor-bracket-r-target-bar-cap.v1"),
        "supported_exit_modes": [
            "atr_floor_bracket", "r_target", "bar_cap"],
        "bracket": "ATR stop with 30 bps floor",
        "target": "configured R multiple",
        "hold_cap": "configured max_hold_bars",
        "executable_exit_templates_added": breakeven is not None,
        "requires_operator_review": breakeven is None,
        "authorizing": False,
    }
    if breakeven is not None:
        result["supported_exit_modes"].append("completed_close_breakeven")
        result.update({
            "breakeven_r": breakeven,
            "breakeven_trigger": "completed close; amended stop is active next bar",
            "vehicle_support": {"equity": True, "option": False},
        })
    result["unsupported_requested_exit_fields"] = unsupported
    result["unsupported_exit_requested"] = bool(unsupported)
    result["status"] = ("rejected_unsupported_exit_grammar" if unsupported else
                         "supported_breakeven_grammar" if breakeven is not None else
                         "supported_fixed_grammar")
    return result


# Descriptive compatibility spellings for callers that use a builder-style
# diagnostic API.  All resolve to the same fit-only, non-authorizing routine.
build_fit_diagnostics = measure_fit_diagnostics
fit_only_diagnostics = measure_fit_diagnostics
collapse_aliases = collapse_behavior_aliases
diagnose_fit = measure_fit_diagnostics
fit_behavior_diagnostics = measure_fit_diagnostics


__all__ = [
    "BAR_COVERAGE_SCHEMA", "COST_STRESS_MULTIPLIERS", "FIT_BEHAVIOR_ALIAS_DECIMALS",
    "FIT_BEHAVIOR_ALIAS_SCHEMA", "FIT_DIAGNOSTICS_SCHEMA",
    "audit_exit_grammar", "build_fit_diagnostics", "collapse_aliases",
    "collapse_behavior_aliases", "diagnose_fit", "filter_behavior_aliases",
    "fit_behavior_diagnostics", "fit_behavior_fingerprint",
    "fit_only_diagnostics", "measure_fit_diagnostics", "bar_coverage_telemetry",
]
