"""Fit-only conditional forward-return diagnostics for bounded rule signals.

This module deliberately measures signal quality before position sizing and
bracket exits.  It cannot authorize a candidate and it emits aggregates only.
The existing proof path remains responsible for held-out replay, costs, gates,
multiple testing, and paper promotion.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from statistics import mean, median, stdev
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from agent.contracts.rule import (
    CROSS_SECTIONAL_BENCHMARK, entry_window_bounds, evaluate_rule_signal,
    evaluate_rule_signal_trace, feature_window_bars, rule_variant_id,
    cross_sectional_symbol_eligibility,
    session_minutes, validate_rule_spec,
)
from .market_data import (historical_backfill_record, replay_available_at,
                           replay_record_is_available)
from .maturity import causal_maturity_bars


SIGNAL_QUALITY_SCHEMA = "signal-quality.v2"
SIGNAL_QUALITY_ELIGIBILITY_SCHEMA = "signal-quality-eligibility.v1"
DEFAULT_HORIZONS = (5, 15, 30, 60, 120, 390)
_NY = ZoneInfo("America/New_York")
_DATA_INELIGIBLE_REASONS = frozenset({
    "historical_backfill_excluded", "feature_unavailable",
})
_DATA_INCOMPLETE_REASONS = frozenset({
    "insufficient_history", "signal_end_unavailable", "feature_gap",
    "entry_not_adjacent", "entry_bar_unavailable", "invalid_session",
})
# Intraday returns carry strong time-of-day structure, and every family in the
# catalog fires on a concentrated part of the session: opening-anchored rules
# cannot signal before their range completes, the VWAP families need a session
# prefix, and every discovery variant sets an explicit entry window.  A null
# drawn uniformly across the session therefore measures the difference between
# two clocks rather than the value of the predicate, and reports the gap as an
# edge.  The null is matched on the clock instead: the same instrument, at the
# same session minute, on every other session in the corpus.
#
# The tiers below only exist for corpora too small to supply a cross-session
# match.  They are ordered tightest-first and still cannot fully remove the
# gap, because a rule that fires as early as its window allows sits ahead of
# any same-session draw spread across that window; whichever tier is used,
# ``control_mean_session_minute`` reports the residual clock gap so it stays
# visible rather than being absorbed into the result.
_TIME_BUCKETS = ((60.0, "opening_0_60m"), (300.0, "midday_60_300m"))
_LAST_BUCKET = "close_300m_plus"
_CONTROL_MINUTE_BAND = 20.0


def _time_bucket(minutes: float) -> str:
    for edge, name in _TIME_BUCKETS:
        if minutes < edge:
            return name
    return _LAST_BUCKET


def _value(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _timestamp(row: Any) -> datetime | None:
    raw = _value(row, "timestamp", _value(row, "ts"))
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if raw is None:
        return None
    try:
        text = str(raw)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _bar_end(row: Any) -> datetime | None:
    raw = _value(row, "end")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if raw is not None:
        try:
            text = str(raw)
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            parsed = datetime.fromisoformat(text)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None
    stamp = _timestamp(row)
    try:
        seconds = int(_value(row, "interval_seconds", 60) or 60)
    except (TypeError, ValueError, OverflowError):
        return None
    return stamp + timedelta(seconds=seconds) if stamp is not None else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _symbol(row: Any) -> str:
    return str(_value(row, "symbol", "")).strip().upper()


def _session(row: Any) -> str:
    raw = _value(row, "session_date")
    if isinstance(raw, date):
        return raw.isoformat()
    if raw not in (None, ""):
        return str(raw)[:10]
    stamp = _timestamp(row)
    return stamp.astimezone(_NY).date().isoformat() if stamp else ""


def _allow_backfill(policy: Any | None) -> bool:
    if isinstance(policy, Mapping):
        return policy.get("allow_historical_backfill_diagnostics") is True
    return bool(getattr(policy, "allow_historical_backfill_diagnostics", False))


def _contiguous(rows: Sequence[Any], start: int, stop: int) -> bool:
    if start < 0 or start >= stop or stop > len(rows):
        return False
    stamps = [_timestamp(row) for row in rows[start:stop]]
    return all(left is not None and right is not None and
               right - left == timedelta(minutes=1)
               for left, right in zip(stamps, stamps[1:]))


def _mature_prefix(spec: Mapping[str, Any], window: int | None) -> int:
    """Backward-compatible wrapper around the shared maturity contract."""
    del window
    return causal_maturity_bars(spec)


def _eligibility_classification(reason: Any) -> str:
    """Map a prefix failure to the conservative screen classification.

    The detailed reason remains in the fit-prefix provenance.  The compact
    signal-quality hand-off only needs to tell the screen whether a cell was
    unusable data, incomplete data, or a predicate that was actually tested.
    """
    value = str(reason or "").strip()
    if value.startswith("subject_context_ineligible:"):
        # Structural universe policy is a predicate refusal, not missing
        # market data.  In particular SPY's self-reference is expected when a
        # synchronized residual corpus includes the benchmark rows.
        return "predicate_no_actionable_signal"
    if value.startswith(("benchmark_context_", "subject_context_")):
        # Context failures remain explicitly unknown to screening.  They must
        # not be collapsed into generic incomplete history, which can obscure
        # a missing benchmark alongside otherwise complete subject data.
        return "context_unknown"
    if value in _DATA_INELIGIBLE_REASONS or value == "data_ineligible":
        return "data_ineligible"
    if value in _DATA_INCOMPLETE_REASONS or value == "data_incomplete":
        return "data_incomplete"
    if value == "context_unknown":
        return "context_unknown"
    if value == "no_actionable_signal":
        return "predicate_no_actionable_signal"
    if value in {"actionable_signal", "signal"}:
        return "actionable_signal"
    return "data_incomplete"


def _precomputed_rejection_reason(reason: Any) -> str:
    """Preserve the legacy predicate rejection key for screen compatibility."""
    value = str(reason or "").strip()
    if value in {"no_actionable_signal", "predicate_no_actionable_signal"}:
        return "no_actionable_signal"
    return _eligibility_classification(value)


def _cell_key(symbol: Any, session: Any) -> str:
    return f"{str(symbol or '').strip().upper()}|{str(session or '')[:10]}"


def _provenance_summary(
        provenance: Any, *, total_cells: int,
        fallback_cells: Mapping[str, str] | None = None,
        ) -> dict[str, Any]:
    """Return a bounded, JSON-safe eligibility summary.

    ``by_cell`` is an internal worker seam and is intentionally omitted from
    this public aggregate.  Counts and the bounded reason vocabulary are
    enough for a screen to fail open without persisting market rows or an
    unbounded list of cells.
    """
    raw = provenance if isinstance(provenance, Mapping) else {}
    raw_counts = raw.get("prefix_counts", raw.get("status_counts", {}))
    prefix_counts: Counter[str] = Counter()
    if isinstance(raw_counts, Mapping):
        for key, value in raw_counts.items():
            try:
                count = int(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if count >= 0:
                prefix_counts[str(key)] += count
    raw_reasons = raw.get("reason_counts", {})
    reason_counts: Counter[str] = Counter()
    if isinstance(raw_reasons, Mapping):
        for key, value in raw_reasons.items():
            try:
                count = int(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if count >= 0:
                reason_counts[str(key)] += count
    # A direct scan has no fit-prefix object.  Populate a compact reason
    # summary from the per-cell fallback classifications collected by the
    # caller instead of pretending those cells were predicate tests.
    if fallback_cells:
        for reason in fallback_cells.values():
            reason_counts[str(reason)] += 1
    normalized_total = max(0, int(total_cells))
    raw_eligible = raw.get("eligible_cells", raw.get("eligible_sessions", 0))
    raw_signals = raw.get("signal_cells", raw.get("signal_prefixes", 0))
    try:
        eligible_cells = max(0, int(raw_eligible))
    except (TypeError, ValueError, OverflowError):
        eligible_cells = 0
    try:
        signal_cells = max(0, int(raw_signals))
    except (TypeError, ValueError, OverflowError):
        signal_cells = 0
    classifications = Counter()
    raw_classification = str(raw.get("classification", "")).strip()
    if raw_classification:
        classifications[raw_classification] += 1
    for reason in fallback_cells.values() if fallback_cells else ():
        classifications[_eligibility_classification(reason)] += 1
    # Prefix ``reason_counts`` are prefix-level observations, not cell-level
    # counts.  Prefer the explicit cell totals from the fit hand-off; for a
    # direct scan derive them from the observed cell classifications.
    try:
        raw_data_ineligible = max(0, int(raw.get("data_ineligible_cells", 0)))
    except (TypeError, ValueError, OverflowError):
        raw_data_ineligible = 0
    try:
        raw_data_incomplete = max(0, int(raw.get("data_incomplete_cells", 0)))
    except (TypeError, ValueError, OverflowError):
        raw_data_incomplete = 0
    fallback_classifications = Counter(
        _eligibility_classification(reason)
        for reason in (fallback_cells.values() if fallback_cells else ()))
    data_ineligible = (raw_data_ineligible or
                       fallback_classifications.get("data_ineligible", 0))
    data_incomplete = (raw_data_incomplete or
                       fallback_classifications.get("data_incomplete", 0))
    context_unknown = fallback_classifications.get("context_unknown", 0)
    if raw_classification == "context_unknown":
        context_unknown = max(context_unknown, 1)
    raw_context_unknown = raw.get("context_unknown_cells", 0)
    try:
        context_unknown = max(context_unknown, int(raw_context_unknown))
    except (TypeError, ValueError, OverflowError):
        pass
    if data_ineligible and data_incomplete:
        status = "mixed_data_incomplete"
    elif data_ineligible:
        status = "data_ineligible"
    elif data_incomplete:
        status = "data_incomplete"
    elif signal_cells or classifications.get("actionable_signal"):
        status = "actionable_signal"
    elif normalized_total and (
            classifications.get("predicate_no_actionable_signal") or
            raw_classification == "predicate_no_actionable_signal"):
        status = "predicate_no_actionable_signal"
    elif not normalized_total:
        status = "data_incomplete"
    elif context_unknown:
        status = "context_unknown"
    else:
        status = "unknown"
    # Keep only a deterministic, small vocabulary in durable output.  The
    # full prefix-status counts remain available in fit diagnostics.
    bounded_reasons = dict(sorted(reason_counts.items())[:32])
    bounded_prefix = dict(sorted(prefix_counts.items())[:32])
    truncated = bool(raw.get("truncated")) or (
        "provenance_cells_truncated" in reason_counts)
    def _safe_count(key: str) -> int:
        value = raw.get(key, 0)
        try:
            if isinstance(value, bool):
                return 0
            parsed = int(value)
            return max(0, parsed) if float(parsed) == float(value) else 0
        except (TypeError, ValueError, OverflowError):
            return 0
    return {
        "schema": SIGNAL_QUALITY_ELIGIBILITY_SCHEMA,
        "scope": "fit_only",
        "authorizing": False,
        "diagnostic_only": True,
        "status": status,
        "classification": status,
        "total_cells": normalized_total,
        "eligible_cells": min(eligible_cells, normalized_total),
        "signal_cells": min(signal_cells, normalized_total),
        "data_ineligible_cells": int(data_ineligible),
        "data_incomplete_cells": int(data_incomplete),
        "context_unknown_cells": int(context_unknown),
        "mature_prefixes": _safe_count("mature_prefixes"),
        "evaluator_prefixes": _safe_count("evaluator_prefixes"),
        "truncated": truncated,
        "reason_counts": bounded_reasons,
        "prefix_counts": bounded_prefix,
    }


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _immutable_market_context(
        bars: Sequence[Any],
        bars_by_symbol: Mapping[str, Sequence[Any]] | None,
        ) -> Mapping[str, tuple[Any, ...]]:
    """Freeze caller context, or derive it deterministically from the corpus."""
    if bars_by_symbol is None:
        derived: dict[str, list[Any]] = {}
        for row in bars:
            symbol = _symbol(row)
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


def _first_event(rows: Sequence[Any], spec: Mapping[str, Any], *,
                 allow_backfill: bool,
                 bars_by_symbol: Mapping[str, Sequence[Any]] | None = None,
                 symbol: str | None = None,
                 ) -> tuple[dict[str, Any] | None, str | None]:
    """Return the first causal signal/entry pair for one symbol-session."""
    window = feature_window_bars(spec)
    minimum_prefix = causal_maturity_bars(spec)
    context_reason: str | None = None
    status_counts: Counter[str] = Counter()
    eligible_prefix = False
    if not rows:
        return None, "data_incomplete"
    # A malformed session is a data refusal, not evidence that its predicate
    # never fired.  The fit-prefix path performs the same check with richer
    # per-cell counters; this guard keeps direct signal-quality calls aligned.
    seen: set[datetime] = set()
    previous: datetime | None = None
    invalid_session = False
    for row in rows:
        stamp = _timestamp(row)
        try:
            interval = int(_value(row, "interval_seconds", 60) or 0)
        except (TypeError, ValueError, OverflowError):
            interval = 0
        if stamp is None or interval != 60 or stamp in seen or (
                previous is not None and stamp <= previous):
            status_counts["invalid_session"] += 1
            invalid_session = True
        if stamp is not None:
            seen.add(stamp)
            previous = stamp
    if invalid_session:
        # Once a session's chronology/interval contract is broken, no prefix
        # can be interpreted as an admissible predicate observation.  Stop
        # before evaluating the rule so malformed data cannot look like a
        # clean no-actionable-signal result.
        return None, "data_incomplete"
    for index in range(1, max(1, len(rows) - 1)):
        if index + 1 < minimum_prefix:
            status_counts["insufficient_history"] += 1
            continue
        feature_start = 0 if window is None else max(0, index + 1 - int(window))
        feature_rows = rows[feature_start:index + 1]
        if not _contiguous(rows, feature_start, index + 1):
            status_counts["feature_gap"] += 1
            continue
        signal_end = _bar_end(rows[index])
        if signal_end is None:
            status_counts["signal_end_unavailable"] += 1
            continue
        if (not allow_backfill and any(historical_backfill_record(row)
                                       for row in feature_rows)):
            # A labelled historical backfill is outside the default
            # diagnostic view.  Do not let its delayed observation timestamp
            # turn into the less informative ``entry_bar_unavailable``.
            status_counts["historical_backfill_excluded"] += 1
            continue
        available = [replay_available_at(
            row, allow_historical_backfill_diagnostics=allow_backfill)
            for row in feature_rows]
        if any(item is None for item in available):
            if (not allow_backfill and any(historical_backfill_record(row)
                                           for row in feature_rows)):
                status_counts["historical_backfill_excluded"] += 1
            else:
                status_counts["feature_unavailable"] += 1
            continue
        decision = max([signal_end, *(item for item in available if item is not None)])
        next_row = rows[index + 1]
        if _timestamp(next_row) != signal_end and decision <= signal_end:
            status_counts["entry_not_adjacent"] += 1
            continue
        entry_at = signal_end if decision <= signal_end else decision
        entry_index = next((probe for probe in range(index + 1, len(rows))
                            if (_timestamp(rows[probe]) is not None and
                                _timestamp(rows[probe]) >= entry_at)), None)
        if entry_index is None:
            status_counts["entry_bar_unavailable"] += 1
            continue
        selected_entry = rows[entry_index]
        entry_cutoff = _bar_end(selected_entry) or _timestamp(selected_entry)
        if not replay_record_is_available(
                selected_entry, entry_cutoff,
                allow_historical_backfill_diagnostics=allow_backfill):
            status_counts["entry_bar_unavailable"] += 1
            continue
        eligible_prefix = True
        if spec["family"] == "cross_sectional_residual":
            trace = evaluate_rule_signal_trace(
                rows[:index + 1], spec, bars_by_symbol=bars_by_symbol,
                symbol=symbol)
            signal = trace.get("signal")
            if signal is None:
                reason = str((trace.get("stages") or [{}])[-1].get(
                    "reason") or "")
                if reason.startswith(("benchmark_context_",
                                      "subject_context_",
                                      "subject_ineligible:")):
                    context_reason = reason
        else:
            signal = evaluate_rule_signal(rows[:index + 1], spec)
        if signal is None:
            continue
        # This is a pre-execution signal-quality measurement, not a fabricated
        # fill.  Anchor the return to the completed signal close, which is
        # observable at ``signal_end`` even when the next bar's opening print
        # was delivered only with its completed OHLC record.  Expected costs
        # remain a separately reported hurdle.
        entry_price = _number(signal.get("entry_price", _value(rows[index], "close")))
        if entry_price is None or entry_price <= 0:
            return None, "entry_price_invalid"
        return ({"signal": signal, "signal_index": index,
                 "entry_index": entry_index, "entry_at": entry_at,
                 "entry_price": entry_price}, None)
    if context_reason:
        return None, context_reason
    if eligible_prefix:
        return None, "no_actionable_signal"
    if status_counts:
        # Preserve the category (rather than allowing a data refusal to look
        # like a predicate result).  Detailed counts are carried by the fit
        # provenance hand-off when available.
        priority = (
            "historical_backfill_excluded", "feature_unavailable",
            "entry_bar_unavailable", "entry_not_adjacent", "feature_gap",
            "invalid_session", "signal_end_unavailable", "insufficient_history",
        )
        reason = next((item for item in priority if status_counts.get(item)),
                      "insufficient_history")
        return None, _eligibility_classification(reason)
    return None, "data_incomplete"


def _forward_return(rows: Sequence[Any], *, entry_index: int, horizon: int,
                    entry_price: float, direction: str,
                    allow_backfill: bool) -> tuple[float | None, str | None]:
    future_index = entry_index + int(horizon) - 1
    if future_index >= len(rows):
        return None, "insufficient_future_bars"
    if not _contiguous(rows, entry_index, future_index + 1):
        return None, "future_gap"
    future = rows[future_index]
    cutoff = _bar_end(future)
    if cutoff is None or not replay_record_is_available(
            future, cutoff,
            allow_historical_backfill_diagnostics=allow_backfill):
        return None, "future_bar_unavailable"
    future_close = _number(_value(future, "close"))
    if future_close is None or future_close <= 0 or entry_price <= 0:
        return None, "future_price_invalid"
    sign = 1.0 if direction == "long" else -1.0
    return sign * (future_close / entry_price - 1.0) * 10_000.0, None


class _ControlPolicy:
    """The bars a rule was admissible to enter on, reused across candidates.

    Admissibility mirrors the executable evaluator: a mature prefix, a
    contiguous feature window, and a timestamp inside the spec's own entry
    window.  Anything outside that set is a bar the rule could never have
    traded, so including it in the null measures the session's clock instead
    of the predicate.
    """

    __slots__ = ("minimum_prefix", "window", "after", "before")

    def __init__(self, spec: Mapping[str, Any]) -> None:
        # ``feature_window_bars`` is the executable rule's dependency
        # declaration.  It includes a slow lookback only for families or
        # confirmations that actually consume it; merely having the field in
        # every normalized spec must not make unrelated families wait for it.
        # Session-anchored families return ``None`` because their input is the
        # complete session prefix rather than a bounded trailing window.
        self.window = feature_window_bars(spec)
        self.minimum_prefix = _mature_prefix(spec, self.window)
        self.after, self.before = entry_window_bounds(spec)

    def admissible(self, rows: Sequence[Any], index: int) -> float | None:
        """Return the bar's session minute when the rule could enter on it."""
        # Never shorten a declared dependency for a small corpus.  Returning
        # no eligible controls is the truthful underpowered result; evaluating
        # with a shorter prefix would silently change the rule being measured.
        if index + 1 < self.minimum_prefix:
            return None
        feature_start = (0 if self.window is None
                         else max(0, index + 1 - int(self.window)))
        if not _contiguous(rows, feature_start, index + 1):
            return None
        stamp = _timestamp(rows[index])
        if stamp is None:
            return None
        minutes = session_minutes(stamp)
        return minutes if self.after <= minutes < self.before else None


def _control_indices(rows: Sequence[Any], policy: _ControlPolicy, *,
                     horizon: int, allow_backfill: bool) -> list[tuple[int, float]]:
    """Admissible null-draw indices and their session minutes."""
    eligible: list[tuple[int, float]] = []
    for signal_index in range(0, len(rows)):
        minutes = policy.admissible(rows, signal_index)
        if minutes is None:
            continue
        future_index = signal_index + int(horizon)
        if future_index >= len(rows) or not _contiguous(
                rows, signal_index, future_index + 1):
            continue
        signal_at = _bar_end(rows[signal_index])
        future_at = _bar_end(rows[future_index])
        if (signal_at is None or future_at is None or
                not replay_record_is_available(
                    rows[signal_index], signal_at,
                    allow_historical_backfill_diagnostics=allow_backfill) or
                not replay_record_is_available(
                    rows[future_index], future_at,
                    allow_historical_backfill_diagnostics=allow_backfill)):
            continue
        entry_price = _number(_value(rows[signal_index], "close"))
        future_price = _number(_value(rows[future_index], "close"))
        if (entry_price is not None and entry_price > 0 and
                future_price is not None and future_price > 0):
            eligible.append((signal_index, minutes))
    return eligible


def _eligible_by_minute(rows: Sequence[Any], policy: _ControlPolicy, *,
                        horizon: int, allow_backfill: bool) -> dict[int, tuple[int, float]]:
    """Admissible null-draw bars for one session, keyed by session minute."""
    eligible: dict[int, tuple[int, float]] = {}
    for index, minutes in _control_indices(rows, policy, horizon=horizon,
                                           allow_backfill=allow_backfill):
        eligible.setdefault(int(round(minutes)), (index, minutes))
    return eligible


def _control_return(candidate_cell: tuple[str, str], candidate_minutes: float, *,
                    eligible: Mapping[tuple[str, str], dict[int, tuple[int, float]]],
                    session_rows: Mapping[tuple[str, str], Sequence[Any]],
                    candidate_index: int, direction: str, horizon: int,
                    allow_backfill: bool
                    ) -> tuple[float | None, str | None, dict[str, Any]]:
    """Average every admissible null draw whose clock matches the candidate.

    The null has to answer "does the predicate beat entering at a comparable
    time?", so the comparable time is matched exactly: the same instrument, at
    the same minute of the session, on every *other* session in the corpus.  A
    same-session draw cannot do this — a rule that fires at the earliest bar
    its own window allows sits systematically ahead of any draw spread across
    that window, and intraday drift over that gap alone reads as an edge.
    Same-session tiers remain as a fallback for corpora too small to supply a
    cross-session match, and the tier used is reported.

    Averaging the whole pool rather than selecting one member keeps the null
    deterministic without spending sample: a single draw carries the same
    variance as the candidate itself and inflates the paired standard error by
    roughly the square root of two.
    """
    symbol, session = candidate_cell
    minute_key = int(round(candidate_minutes))
    choices: list[tuple[tuple[str, str], int, float]] = []
    for cell, minute_map in eligible.items():
        if cell[0] != symbol or cell[1] == session:
            continue
        found = minute_map.get(minute_key)
        if found is not None:
            choices.append((cell, found[0], found[1]))
    matching = "cross_session_same_session_minute"
    if not choices:
        # Fallbacks stay inside the candidate's own session, tightest first.
        own = [item for item in eligible.get(candidate_cell, {}).values()
               if item[0] != candidate_index]
        near = [item for item in own
                if abs(item[1] - candidate_minutes) <= _CONTROL_MINUTE_BAND]
        bucket = _time_bucket(candidate_minutes)
        same_bucket = [item for item in own
                       if _time_bucket(item[1]) == bucket]
        picked, matching = ((near, "same_session_minute_band") if near else
                            (same_bucket, "same_session_time_bucket")
                            if same_bucket else (own, "same_session_entry_window"))
        choices = [(candidate_cell, index, minutes) for index, minutes in picked]
    if not choices:
        return None, "no_matched_control", {"matching": "none", "pool": 0}
    values: list[float] = []
    minutes_used: list[float] = []
    for cell, index, item_minutes in choices:
        rows = session_rows[cell]
        value, _reason = _forward_return(
            rows, entry_index=index + 1, horizon=horizon,
            entry_price=float(_value(rows[index], "close")),
            direction=direction, allow_backfill=allow_backfill)
        if value is not None:
            values.append(value)
            minutes_used.append(item_minutes)
    if not values:
        return None, "no_matched_control", {"matching": matching, "pool": 0}
    return mean(values), None, {"matching": matching, "pool": len(values),
                                "mean_session_minute": mean(minutes_used)}


def _summary(values: Sequence[float]) -> tuple[float | None, float | None, float | None]:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    if not clean:
        return None, None, None
    return mean(clean), median(clean), sum(value > 0 for value in clean) / len(clean)


def _dispersion(values: Sequence[float], *, shift: float = 0.0) -> dict[str, Any]:
    """Sample dispersion for one aggregate, so a mean can be read with its error.

    A point estimate with no error term is what let a 47-trade replay read as
    a finding.  Every mean this screen reports therefore carries the standard
    deviation, standard error, and t-statistic that say how much of it is
    distinguishable from zero.
    """
    clean = [float(value) for value in values if math.isfinite(float(value))]
    count = len(clean)
    if not count:
        return {"stdev_bps": None, "stderr_bps": None, "t_stat": None}
    deviation = stdev(clean) if count > 1 else None
    error = (deviation / math.sqrt(count)
             if deviation is not None and deviation > 0 else None)
    centre = mean(clean) - float(shift)
    return {"stdev_bps": deviation, "stderr_bps": error,
            "t_stat": (centre / error) if error else None}


def _event_index(value: Any) -> int | None:
    """Return a strict non-negative row index from precomputed metadata."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _precomputed_event(
        rows: Sequence[Any], item: Any, *, symbol: str, session: str,
        ) -> tuple[dict[str, Any] | None, str | None]:
    """Validate one internal first-signal hand-off without re-evaluating it.

    ``_fit_prefixes`` owns signal evaluation and supplies only compact
    metadata. The row/index checks below make the optional fast path fail
    closed if a caller passes stale or fabricated metadata; direct callers
    continue to use :func:`_first_event` when the argument is omitted.
    """
    if not isinstance(item, Mapping):
        return None, "precomputed_event_invalid"
    item_symbol = _symbol(item)
    item_session = str(item.get("session", item.get("session_date", "")))[:10]
    if item_symbol != symbol or item_session != session:
        return None, "precomputed_event_invalid"
    signal_index = _event_index(item.get("signal_index"))
    entry_index = _event_index(item.get("entry_index"))
    if (signal_index is None or entry_index is None or
            signal_index >= len(rows) or entry_index >= len(rows) or
            signal_index < 1 or entry_index <= signal_index):
        return None, "precomputed_event_invalid"

    signal_ts = _number(item.get("signal_ts"))
    signal_stamp = _timestamp(rows[signal_index])
    if signal_ts is None or signal_stamp is None:
        return None, "precomputed_event_invalid"
    try:
        supplied_stamp = datetime.fromtimestamp(signal_ts, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None, "precomputed_event_invalid"
    if supplied_stamp != signal_stamp:
        return None, "precomputed_event_invalid"

    direction = str(item.get("direction", "")).strip().lower()
    if direction not in {"long", "short"}:
        return None, "precomputed_event_invalid"
    entry_price = _number(item.get("entry_price"))
    signal_close = _number(_value(rows[signal_index], "close"))
    if (entry_price is None or entry_price <= 0 or signal_close is None or
            signal_close <= 0 or not math.isclose(
                entry_price, signal_close, rel_tol=0.0, abs_tol=1e-12)):
        return None, "precomputed_event_invalid"

    # ``entry_timestamp`` is emitted by _fit_prefixes as the decision-time
    # boundary. Validate it when present, while allowing the compact shape
    # (which needs only the required index fields) for internal callers.
    entry_at = None
    raw_entry_timestamp = item.get("entry_timestamp")
    if raw_entry_timestamp not in (None, ""):
        entry_at = _timestamp({"timestamp": raw_entry_timestamp})
        entry_stamp = _timestamp(rows[entry_index])
        if entry_at is None or entry_stamp is None or entry_stamp < entry_at:
            return None, "precomputed_event_invalid"
    if entry_at is None:
        entry_at = _timestamp(rows[entry_index])

    # Keep the event shape used by the scanned path. Only signal metadata is
    # retained; raw bars never cross this diagnostic hand-off.
    signal = {
        "direction": direction,
        "signal_ts": signal_ts,
        "entry_price": entry_price,
    }
    return ({"signal": signal, "signal_index": signal_index,
             "entry_index": entry_index, "entry_at": entry_at,
             "entry_price": entry_price}, None)


def measure_signal_quality(
        bars: Sequence[Any], spec: Mapping[str, Any], *, policy: Any | None = None,
        horizons: Sequence[int] = DEFAULT_HORIZONS,
        cost_hurdle_bps: float | None = None,
        precomputed_first_signals: Sequence[Mapping[str, Any]] | None = None,
        eligibility_provenance: Mapping[str, Any] | None = None,
        bars_by_symbol: Mapping[str, Sequence[Any]] | None = None,
        ) -> dict[str, Any]:
    """Measure conditional forward returns and a matched random-entry control.

    ``precomputed_first_signals`` is an optional internal hand-off from
    :func:`research.fit_diagnostics._fit_prefixes`. When omitted, the
    historical prefix scan remains authoritative. When supplied, every
    event is validated against its sorted symbol/session rows and malformed
    metadata is rejected rather than silently falling back to a scan.
    ``eligibility_provenance`` is the matching compact hand-off from that
    prefix scan.  It is required to distinguish an empty event list caused by
    unavailable/incomplete data from a genuinely tested predicate with no
    actionable signal.
    """
    normalized = validate_rule_spec(spec)
    market_context = _immutable_market_context(bars, bars_by_symbol)
    requested = tuple(int(value) for value in horizons)
    if not requested or any(value <= 0 for value in requested) or \
            len(set(requested)) != len(requested):
        raise ValueError("signal-quality horizons must be unique positive integers")
    if cost_hurdle_bps is not None:
        cost_hurdle_bps = float(cost_hurdle_bps)
        if not math.isfinite(cost_hurdle_bps) or cost_hurdle_bps < 0:
            raise ValueError("cost_hurdle_bps must be finite and non-negative")
    allow_backfill = _allow_backfill(policy)
    grouped: dict[tuple[str, str], list[Any]] = {}
    for row in bars:
        symbol, session = _symbol(row), _session(row)
        if symbol and session:
            grouped.setdefault((symbol, session), []).append(row)
    for rows in grouped.values():
        rows.sort(key=lambda item: _timestamp(item) or
                  datetime.min.replace(tzinfo=timezone.utc))

    eligibility_by_symbol: dict[str, dict[str, Any]] = {}
    if normalized["family"] == "cross_sectional_residual":
        symbol_rows: dict[str, list[Any]] = {}
        for (symbol, _session_name), rows in grouped.items():
            symbol_rows.setdefault(symbol, []).extend(rows)
        for symbol, rows in sorted(symbol_rows.items()):
            eligibility_by_symbol[symbol] = {
                **cross_sectional_symbol_eligibility(
                    symbol, rows=rows, spec=normalized),
                "session_count": len({
                    _session(row) for row in rows if _session(row)}),
                "event_count": 0,
            }

    events: list[tuple[str, str, Sequence[Any], dict[str, Any]]] = []
    event_reasons: Counter[str] = Counter()
    event_time_buckets: Counter[str] = Counter()
    observed_cell_reasons: dict[str, str] = {}
    precomputed_by_cell: dict[tuple[str, str], dict[str, Any]] = {}
    precomputed_invalid_cells: set[tuple[str, str]] = set()
    # The fit-prefix worker includes a bounded internal cell map so an empty
    # precomputed event list can retain the true prefix failure category.  A
    # malformed/missing map is conservative: supplied precomputed metadata
    # cannot prove a complete no-signal hypothesis without it.
    provenance_by_cell: dict[tuple[str, str], str] = {}
    provenance_supplied = eligibility_provenance is not None
    if isinstance(eligibility_provenance, Mapping):
        raw_cells = eligibility_provenance.get("by_cell", {})
        if isinstance(raw_cells, Mapping):
            for raw_key, raw_value in raw_cells.items():
                if isinstance(raw_value, Mapping):
                    if isinstance(raw_key, (tuple, list)) and len(raw_key) == 2:
                        symbol, session = raw_key
                    else:
                        pieces = str(raw_key).split("|", 1)
                        if len(pieces) != 2:
                            continue
                        symbol, session = pieces
                    cell = (str(symbol).strip().upper(), str(session)[:10])
                    if cell[0] and cell[1]:
                        provenance_by_cell[cell] = str(
                            raw_value.get("reason") or
                            raw_value.get("classification") or
                            raw_value.get("status") or "data_incomplete")
    elif provenance_supplied:
        # Keep the distinction visible in the output even when an untrusted
        # caller supplied the wrong shape.
        event_reasons["data_incomplete"] += 0
    if precomputed_first_signals is not None:
        if isinstance(precomputed_first_signals, Mapping):
            # A mapping is not a supported event sequence. Treating each
            # mapping key as an event could make malformed data look valid.
            precomputed_items: Sequence[Any] = ()
            event_reasons["precomputed_event_invalid"] += 1
        else:
            try:
                precomputed_items = tuple(precomputed_first_signals)
            except TypeError:
                precomputed_items = ()
                event_reasons["precomputed_event_invalid"] += 1
        for item in precomputed_items:
            if isinstance(item, Mapping):
                item_symbol = _symbol(item)
                item_session = str(
                    item.get("session", item.get("session_date", "")))[:10]
                cell = (item_symbol, item_session)
            else:
                cell = ("", "")
            rows = grouped.get(cell)
            if not rows:
                event_reasons["precomputed_event_invalid"] += 1
                continue
            event, reason = _precomputed_event(
                rows, item, symbol=cell[0], session=cell[1])
            if event is None or cell in precomputed_by_cell:
                precomputed_invalid_cells.add(cell)
                if cell in precomputed_by_cell:
                    precomputed_by_cell.pop(cell, None)
                continue
            precomputed_by_cell[cell] = event

    for (symbol, session), rows in sorted(grouped.items()):
        if precomputed_first_signals is None:
            event, reason = _first_event(
                rows, normalized, allow_backfill=allow_backfill,
                bars_by_symbol=market_context, symbol=symbol)
        elif (symbol, session) in precomputed_invalid_cells:
            event, reason = None, "precomputed_event_invalid"
        else:
            event = precomputed_by_cell.get((symbol, session))
            reason = None if event is not None else (
                _precomputed_rejection_reason(
                    provenance_by_cell.get((symbol, session)))
                if (symbol, session) in provenance_by_cell else
                "data_incomplete" if provenance_supplied else
                "no_actionable_signal")
        if (event is not None and normalized["family"] ==
                "cross_sectional_residual" and
                not eligibility_by_symbol.get(symbol, {}).get("eligible", False)):
            # A stale/externally supplied fit hand-off cannot bypass the
            # current structural eligibility policy.
            event = None
            reason = ("subject_context_ineligible:" +
                      str(eligibility_by_symbol.get(symbol, {}).get(
                          "reason", "symbol_not_in_default_eligibility")))
        if event is None:
            normalized_reason = str(reason or "unknown")
            event_reasons[normalized_reason] += 1
            observed_cell_reasons[_cell_key(symbol, session)] = normalized_reason
            continue
        events.append((symbol, session, rows, event))
        if symbol in eligibility_by_symbol:
            eligibility_by_symbol[symbol]["event_count"] += 1
        signal_stamp = datetime.fromtimestamp(
            float(event["signal"]["signal_ts"]), timezone.utc).astimezone(_NY)
        minutes = (signal_stamp.hour * 60 + signal_stamp.minute) - (9 * 60 + 30)
        bucket = ("opening_0_60m" if minutes < 60 else
                  "midday_60_300m" if minutes < 300 else "close_300m_plus")
        event_time_buckets[bucket] += 1

    control_policy = _ControlPolicy(normalized)
    horizon_metrics: dict[str, Any] = {}
    event_vectors: list[dict[str, Any]] = []
    for horizon in requested:
        candidates: list[float] = []
        controls: list[float] = []
        paired_deltas: list[float] = []
        candidate_minutes: list[float] = []
        control_minutes: list[float] = []
        pool_sizes: list[int] = []
        matching_counts: Counter[str] = Counter()
        unavailable: Counter[str] = Counter()
        sessions: set[str] = set()
        symbols: set[str] = set()
        cells: set[str] = set()
        # Built once per horizon rather than once per candidate: the null pool
        # for a session does not depend on which candidate is asking for it.
        eligible = {cell: _eligible_by_minute(
            rows, control_policy, horizon=horizon, allow_backfill=allow_backfill)
            for cell, rows in grouped.items()}
        for symbol, session, rows, event in events:
            direction = str(event["signal"]["direction"])
            candidate, reason = _forward_return(
                rows, entry_index=int(event["entry_index"]), horizon=horizon,
                entry_price=float(event["entry_price"]), direction=direction,
                allow_backfill=allow_backfill)
            if candidate is None:
                unavailable[str(reason or "candidate_unavailable")] += 1
                continue
            candidates.append(candidate)
            sessions.add(session)
            symbols.add(symbol)
            cells.add(f"{session}:{symbol}")
            signal_stamp = _timestamp(rows[int(event["signal_index"])])
            minutes = (session_minutes(signal_stamp)
                       if signal_stamp is not None else 0.0)
            candidate_minutes.append(minutes)
            control, control_reason, control_meta = _control_return(
                (symbol, session), minutes, eligible=eligible,
                session_rows=grouped,
                candidate_index=int(event["signal_index"]),
                direction=direction, horizon=horizon,
                allow_backfill=allow_backfill)
            matching_counts[str(control_meta["matching"])] += 1
            if control is None:
                unavailable[str(control_reason or "control_unavailable")] += 1
            else:
                controls.append(control)
                paired_deltas.append(candidate - control)
                pool_sizes.append(int(control_meta["pool"]))
                control_minutes.append(float(control_meta["mean_session_minute"]))
            event_vectors.append({
                "symbol": symbol, "session": session, "horizon": horizon,
                "direction": direction, "signal_index": event["signal_index"],
                "entry_index": event["entry_index"],
                "control_matching": control_meta["matching"],
                "control_pool": control_meta["pool"],
            })
        candidate_mean, candidate_median, positive_rate = _summary(candidates)
        control_mean, _control_median, _control_positive = _summary(controls)
        hurdle = cost_hurdle_bps
        after_hurdle = ([value - hurdle for value in candidates]
                        if hurdle is not None else [])
        candidate_dispersion = _dispersion(candidates)
        delta_dispersion = _dispersion(paired_deltas)
        horizon_metrics[f"{horizon}m"] = {
            "horizon_minutes": horizon,
            "candidate_count": len(candidates),
            "matched_count": len(paired_deltas),
            "mean_forward_return_bps": candidate_mean,
            "median_forward_return_bps": candidate_median,
            "positive_rate": positive_rate,
            "forward_return_stdev_bps": candidate_dispersion["stdev_bps"],
            "forward_return_stderr_bps": candidate_dispersion["stderr_bps"],
            "forward_return_t_stat": candidate_dispersion["t_stat"],
            "control_mean_forward_return_bps": control_mean,
            "candidate_minus_control_bps": (
                mean(paired_deltas) if paired_deltas else None),
            "candidate_minus_control_stdev_bps": delta_dispersion["stdev_bps"],
            "candidate_minus_control_stderr_bps": delta_dispersion["stderr_bps"],
            "candidate_minus_control_t_stat": delta_dispersion["t_stat"],
            "control_matching_counts": dict(sorted(matching_counts.items())),
            "control_pool_mean": mean(pool_sizes) if pool_sizes else None,
            "candidate_mean_session_minute": (
                mean(candidate_minutes) if candidate_minutes else None),
            "control_mean_session_minute": (
                mean(control_minutes) if control_minutes else None),
            "cost_hurdle_bps": hurdle,
            "mean_after_hurdle_bps": (
                mean(after_hurdle) if after_hurdle else None),
            "after_hurdle_t_stat": (
                _dispersion(candidates, shift=hurdle)["t_stat"]
                if hurdle is not None else None),
            "hurdle_exceedance_rate": (
                sum(value > hurdle for value in candidates) / len(candidates)
                if candidates and hurdle is not None else None),
            "unavailable_count": sum(unavailable.values()),
            "unavailable_reason_counts": dict(sorted(unavailable.items())),
            "session_clusters": len(sessions),
            "symbol_clusters": len(symbols),
            "session_symbol_cells": len(cells),
        }
    # Build a compact summary from the actual event rejection categories.  A
    # precomputed fit hand-off may additionally supply detailed prefix counts;
    # those are copied only through the bounded summary helper.
    provenance = dict(eligibility_provenance or {})
    quality_provenance = _provenance_summary(
        provenance if provenance else None,
        total_cells=len(grouped),
        fallback_cells=(
            {**observed_cell_reasons,
             **{_cell_key(symbol, session): str(reason)
                for (symbol, session), reason in provenance_by_cell.items()}}
            if (observed_cell_reasons or provenance_by_cell) else None))
    result = {
        "schema": SIGNAL_QUALITY_SCHEMA,
        "scope": "fit_only",
        "authorizing": False,
        "diagnostic_only": True,
        "metric": "conditional_forward_return",
        "canonical_cross_sectional_ic": False,
        "event_policy": "first_actionable_signal_per_symbol_session",
        "return_basis": "signal_bar_close_to_horizon_close_with_next_bar_lag",
        "control_policy": (
            "same_symbol_admissible_entry_bars_at_the_same_session_minute"
            "_across_other_sessions_averaged_over_pool"),
        "variant_id": rule_variant_id(normalized),
        "horizons": list(requested),
        "event_count": len(events),
        "session_count": len({session for _symbol_name, session, _rows, _event in events}),
        "symbol_count": len({symbol for symbol, _session_name, _rows, _event in events}),
        "event_rejection_counts": dict(sorted(event_reasons.items())),
        "event_time_bucket_counts": dict(sorted(event_time_buckets.items())),
        "event_digest": _digest(sorted(event_vectors, key=_canonical)),
        "horizon_metrics": horizon_metrics,
        "eligibility_provenance": quality_provenance,
    }
    if normalized["family"] == "cross_sectional_residual":
        context_rejections = {
            reason: count for reason, count in event_reasons.items()
            if reason.startswith(("benchmark_context_", "subject_context_",
                                  "subject_ineligible:")) and
            not reason.startswith("subject_context_ineligible:")
        }
        result["market_context"] = {
            "benchmark_symbol": CROSS_SECTIONAL_BENCHMARK,
            "status": ("unknown" if not events and context_rejections else
                       "partial" if context_rejections else "complete"),
            "reason": (max(sorted(context_rejections),
                           key=context_rejections.get)
                       if context_rejections else "synchronized_context"),
            "rejection_counts": dict(sorted(context_rejections.items())),
        }
        result["eligibility"] = {
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
                if item["event_count"]),
            "authorizing": False,
            "diagnostic_only": True,
        }
    return result


__all__ = ["DEFAULT_HORIZONS", "SIGNAL_QUALITY_ELIGIBILITY_SCHEMA",
           "SIGNAL_QUALITY_SCHEMA", "measure_signal_quality"]
