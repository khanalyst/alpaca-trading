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
                           replay_bar_prefix,
                           replay_record_is_available)
from .maturity import causal_maturity_bars
from .stats import (DEFAULT_NULL_DRAWS, cluster_robust_mean_inference,
                    sign_flip_null_statistics)


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
    "revision_unresolved",
})
_MEASUREMENT_SCOPES = frozenset({
    "fit_only", "previously_examined_retrospective_diagnostic",
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
CONTROL_TIERS = (
    "cross_session_same_session_minute", "same_session_minute_band",
    "same_session_time_bucket", "same_session_entry_window",
)
# Outcome anchoring.  ``signal_close`` with ``bar_count`` is the original
# measurement and remains the default, so existing callers and stored results
# are unchanged.  The other anchors price the outcome where a runtime acting on
# the completed signal bar could first trade: the open of the next bar, or of
# the bar after it when a full minute of decision latency is allowed for.
# Their horizon is fixed on the wall clock (``wall_clock``): the outcome is the
# close of the bar that ends exactly ``horizon`` minutes after the entry
# instant, whatever happened to the minutes in between, so a missing minute can
# neither stretch the candidate's horizon nor shorten its control's.
OUTCOME_ANCHORS = ("signal_close", "next_bar_open", "skip_one_bar_open")
HORIZON_CLOCKS = ("bar_count", "wall_clock")
_ANCHOR_DELAY_MINUTES = {
    "signal_close": 0, "next_bar_open": 0, "skip_one_bar_open": 1,
}


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


def _row_has_revision_marker(row: Any) -> bool:
    """Refuse unresolved revision/version metadata in a causal prefix.

    The normalized market records do not carry a revision field.  Raw mapping
    callers sometimes do, however, and treating a versioned row as the final
    observation would make the result depend on an unresolved correction.  A
    resolved stream should project one clean row per market timestamp before
    it reaches this diagnostic.
    """
    for name in ("revision", "revision_id", "version", "version_id",
                 "supersedes", "superseded_by", "correction", "is_revision"):
        value = _value(row, name)
        if value is not None and value != "" and not (
                isinstance(value, bool) and value is False):
            return True
    return False


def _completed_record_is_available(
        row: Any, *, allow_backfill: bool,
        decision_at: datetime | None = None,
        ) -> tuple[bool, str | None, datetime | None]:
    """Resolve completed-bar visibility on the observation clock.

    ``replay_record_is_available(row, row.end)`` asks whether a record arrived
    by its market end.  Ordinary recorder rows may arrive a second after the
    end, while normalized historical fixtures can retain an ``as_of`` or
    ``observed_at`` timestamp at the bar timestamp.  Treat market end as the
    earliest defensible completion for a complete OHLC row, and move that
    completion time later when the forward observation arrives later.  The
    decision cutoff prevents a late signal from borrowing an outcome whose
    market interval had already closed.  The explicit backfill lane preserves
    its historical ``as_of`` semantics.
    """
    has_revision = _row_has_revision_marker(row)
    cutoff = _bar_end(row)
    if cutoff is None or has_revision:
        return False, "revision_unresolved" if has_revision else "future_bar_unavailable", None
    available = replay_available_at(
        row, allow_historical_backfill_diagnostics=allow_backfill)
    if available is None:
        return False, "future_bar_unavailable", None
    # A normalized row contains complete OHLC values.  Historical fixtures can
    # carry an observation timestamp earlier than the bar end, so use market
    # end as the earliest defensible completion; ordinary forward observations
    # arriving after the end extend that completion time.  Causality is then
    # enforced by the decision cutoff and contiguous market-time checks.
    completed_at = max(cutoff, available)
    if decision_at is not None:
        # A late record cannot make an already-finished market interval
        # actionable.  The strict end boundary also handles a revision that
        # was only observed after the original outcome had closed.
        if cutoff <= decision_at or completed_at < decision_at:
            return False, "future_bar_unavailable", available
    return True, None, available


def _feature_decision(
        rows: Sequence[Any], feature_start: int, signal_index: int, *,
        signal_end: datetime, allow_backfill: bool,
        ) -> tuple[datetime | None, str | None]:
    """Return the causal decision time for a feature prefix."""
    feature_rows = rows[feature_start:signal_index + 1]
    if any(_row_has_revision_marker(row) for row in feature_rows):
        return None, "revision_unresolved"
    available = [replay_available_at(
        row, allow_historical_backfill_diagnostics=allow_backfill)
                 for row in feature_rows]
    if any(item is None for item in available):
        if (not allow_backfill and any(
                historical_backfill_record(row) for row in feature_rows)):
            return None, "historical_backfill_excluded"
        return None, "feature_unavailable"
    return max([signal_end, *(item for item in available if item is not None)]), None


def _decision_at(
        rows: Sequence[Any], signal_index: int, policy: "_ControlPolicy",
        *, allow_backfill: bool,
        ) -> datetime | None:
    """Resolve a control's feature observation clock, if its prefix is usable."""
    signal_end = _bar_end(rows[signal_index])
    if signal_end is None:
        return None
    feature_start = (0 if policy.window is None else
                     max(0, signal_index + 1 - int(policy.window)))
    decision, reason = _feature_decision(
        rows, feature_start, signal_index, signal_end=signal_end,
        allow_backfill=allow_backfill)
    return decision if reason is None else None


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
        measurement_scope: str = "fit_only",
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
        "scope": measurement_scope,
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
        decision, decision_reason = _feature_decision(
            rows, feature_start, index, signal_end=signal_end,
            allow_backfill=allow_backfill)
        if decision is None:
            status_counts[str(decision_reason or "feature_unavailable")] += 1
            continue
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
        # The selected row records the first full bar at/after the causal
        # decision and remains in the internal hand-off for compatibility.
        # Signal quality is anchored to the completed signal close, so it must
        # not require that row's OHLC to have arrived by its own market end.
        eligible_prefix = True
        if spec["family"] == "cross_sectional_residual":
            context = {CROSS_SECTIONAL_BENCHMARK: replay_bar_prefix(
                (bars_by_symbol or {}).get(CROSS_SECTIONAL_BENCHMARK, ()),
                signal_timestamp=_timestamp(rows[index]),
                decision_timestamp=decision,
                allow_historical_backfill_diagnostics=allow_backfill)}
            trace = evaluate_rule_signal_trace(
                rows[:index + 1], spec, bars_by_symbol=context,
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
            signal = evaluate_rule_signal(
                rows[:index + 1], spec, bars_by_symbol=bars_by_symbol,
                symbol=symbol)
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
            "revision_unresolved", "entry_bar_unavailable",
            "entry_not_adjacent", "feature_gap", "invalid_session",
            "signal_end_unavailable", "insufficient_history",
        )
        reason = next((item for item in priority if status_counts.get(item)),
                      "insufficient_history")
        if reason == "revision_unresolved":
            return None, reason
        return None, _eligibility_classification(reason)
    return None, "data_incomplete"


def _forward_return(rows: Sequence[Any], *, entry_index: int, horizon: int,
                    entry_price: float, direction: str,
                    allow_backfill: bool,
                    decision_at: datetime | None = None,
                    ) -> tuple[float | None, str | None]:
    """Return a completed close-to-close outcome on a contiguous wall clock.

    ``entry_index`` is the first post-signal bar in the historical API.  The
    target therefore remains ``entry_index + horizon - 1`` even when a delayed
    decision selected a later executable entry row.  This keeps every horizon
    anchored to the signal close instead of silently shifting it with recorder
    latency.
    """
    future_index = entry_index + int(horizon) - 1
    if future_index >= len(rows):
        return None, "insufficient_future_bars"
    if not _contiguous(rows, entry_index, future_index + 1):
        return None, "future_gap"
    future = rows[future_index]
    complete, reason, _available = _completed_record_is_available(
        future, allow_backfill=allow_backfill, decision_at=decision_at)
    if not complete:
        return None, str(reason or "future_bar_unavailable")
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
        decision_at = _decision_at(
            rows, signal_index, policy, allow_backfill=allow_backfill)
        if decision_at is None:
            continue
        future_index = signal_index + int(horizon)
        if future_index >= len(rows) or not _contiguous(
                rows, signal_index, future_index + 1):
            continue
        signal_ok, _signal_reason, _signal_available = (
            _completed_record_is_available(
                rows[signal_index], allow_backfill=allow_backfill))
        future_ok, _future_reason, _future_available = (
            _completed_record_is_available(
                rows[future_index], allow_backfill=allow_backfill,
                decision_at=decision_at))
        if not signal_ok or not future_ok:
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


def _choose_controls(candidate_cell: tuple[str, str], candidate_minutes: float, *,
                     eligible: Mapping[tuple[str, str], Mapping[int, tuple]],
                     candidate_index: int,
                     tiers: Sequence[str] | None = None,
                     ) -> tuple[list[tuple[tuple[str, str], int, float]], str, str | None]:
    """Select the clock-matched null pool and name the tier it came from.

    The tier is the tightest one with any admissible member: the same minute
    on other sessions, then same-session fallbacks.  With ``tiers`` the
    selected tier must be one of them; a candidate whose tier is not is left
    unmatched (and the excluded tier is returned) rather than being matched
    on a looser tier, so it counts against coverage instead of voiding the
    whole measurement.
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
        choices = [(candidate_cell, item[0], item[1]) for item in picked]
    if not choices:
        return [], "none", None
    if tiers is not None and matching not in tiers:
        return [], "none", matching
    return choices, matching, None


def _control_return(candidate_cell: tuple[str, str], candidate_minutes: float, *,
                    eligible: Mapping[tuple[str, str], dict[int, tuple[int, float]]],
                    session_rows: Mapping[tuple[str, str], Sequence[Any]],
                    candidate_index: int, direction: str, horizon: int,
                    allow_backfill: bool,
                    policy: _ControlPolicy | None = None,
                    tiers: Sequence[str] | None = None,
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
    choices, matching, excluded = _choose_controls(
        candidate_cell, candidate_minutes, eligible=eligible,
        candidate_index=candidate_index, tiers=tiers)
    if not choices:
        if excluded is not None:
            return None, "control_tier_not_registered", {
                "matching": f"excluded_{excluded}", "pool": 0}
        return None, "no_matched_control", {"matching": "none", "pool": 0}
    values: list[float] = []
    minutes_used: list[float] = []
    decision_delays: list[float] = []
    outcome_delays: list[float] = []
    for cell, index, item_minutes in choices:
        rows = session_rows[cell]
        decision_at = (_decision_at(
            rows, index, policy, allow_backfill=allow_backfill)
                       if policy is not None else None)
        value, _reason = _forward_return(
            rows, entry_index=index + 1, horizon=horizon,
            entry_price=float(_value(rows[index], "close")),
            direction=direction, allow_backfill=allow_backfill,
            decision_at=decision_at)
        if value is not None:
            values.append(value)
            minutes_used.append(item_minutes)
            if decision_at is not None:
                signal_end = _bar_end(rows[index])
                if signal_end is not None:
                    decision_delays.append(max(
                        0.0, (decision_at - signal_end).total_seconds()))
            future = rows[index + horizon]
            available = replay_available_at(
                future, allow_historical_backfill_diagnostics=allow_backfill)
            future_end = _bar_end(future)
            if available is not None and future_end is not None:
                outcome_delays.append(max(
                    0.0, (available - future_end).total_seconds()))
    if not values:
        return None, "no_matched_control", {"matching": matching, "pool": 0}
    metadata = {"matching": matching, "pool": len(values),
                "mean_session_minute": mean(minutes_used)}
    if decision_delays:
        metadata["mean_decision_delay_seconds"] = mean(decision_delays)
    if outcome_delays:
        metadata["mean_outcome_observation_delay_seconds"] = mean(outcome_delays)
    return mean(values), None, metadata


def _row_at(rows: Sequence[Any], start: int, stamp: datetime) -> int | None:
    """Index of the row whose market timestamp is exactly ``stamp``."""
    for probe in range(max(0, int(start)), len(rows)):
        current = _timestamp(rows[probe])
        if current is None:
            continue
        if current == stamp:
            return probe
        if current > stamp:
            return None
    return None


def _clock_outcome(rows: Sequence[Any], *, signal_index: int, horizon: int,
                   anchor: str, direction: str, allow_backfill: bool,
                   decision_at: datetime | None,
                   signal_price: float | None = None,
                   ) -> tuple[float | None, str | None, int | None]:
    """Return an outcome whose entry and horizon are fixed on the wall clock.

    The entry instant is the end of the signal bar, plus one minute for the
    one-bar-skip variant.  ``signal_close`` prices it at the signal bar's
    close (the original, non-executable anchor, kept as a diagnostic); the
    open anchors price it at the open of the bar that starts exactly at that
    instant, which must exist and must still have been in progress when the
    decision was taken.  The outcome is the close of the bar that ends exactly
    ``horizon`` minutes after the entry instant.  Minutes missing in between
    do not move either end, and a missing entry or horizon bar is refused
    rather than replaced by a neighbour.
    """
    signal_end = _bar_end(rows[signal_index])
    if signal_end is None:
        return None, "signal_end_unavailable", None
    entry_at = signal_end + timedelta(minutes=_ANCHOR_DELAY_MINUTES[anchor])
    if anchor == "signal_close":
        entry_price = (signal_price if signal_price is not None
                       else _number(_value(rows[signal_index], "close")))
    else:
        entry_index = _row_at(rows, signal_index + 1, entry_at)
        if entry_index is None:
            return None, "entry_bar_missing", None
        entry_end = _bar_end(rows[entry_index])
        if (decision_at is not None and entry_end is not None and
                decision_at >= entry_end):
            return None, "entry_bar_elapsed_before_decision", None
        entry_price = _number(_value(rows[entry_index], "open"))
    exit_index = _row_at(rows, signal_index + 1,
                         entry_at + timedelta(minutes=int(horizon) - 1))
    if exit_index is None:
        return None, "horizon_bar_missing", None
    future = rows[exit_index]
    complete, reason, _available = _completed_record_is_available(
        future, allow_backfill=allow_backfill, decision_at=decision_at)
    if not complete:
        return None, str(reason or "future_bar_unavailable"), None
    future_close = _number(_value(future, "close"))
    if (future_close is None or future_close <= 0 or entry_price is None or
            entry_price <= 0):
        return None, "future_price_invalid", None
    sign = 1.0 if direction == "long" else -1.0
    return sign * (future_close / entry_price - 1.0) * 10_000.0, None, exit_index


def _clock_control_pool(rows: Sequence[Any], policy: _ControlPolicy, *,
                        horizon: int, anchor: str, allow_backfill: bool,
                        ) -> dict[int, tuple[int, float, float, float | None, float | None]]:
    """Admissible wall-clock null draws for one session, keyed by minute.

    Each value is ``(index, session_minute, long_return_bps,
    decision_delay_seconds, outcome_observation_delay_seconds)``.  The control
    uses exactly the candidate's anchor and horizon rule, so the two cannot
    differ by construction after a missing minute.
    """
    pool: dict[int, tuple[int, float, float, float | None, float | None]] = {}
    for signal_index in range(0, len(rows)):
        minutes = policy.admissible(rows, signal_index)
        if minutes is None:
            continue
        decision_at = _decision_at(
            rows, signal_index, policy, allow_backfill=allow_backfill)
        if decision_at is None:
            continue
        signal_ok, _reason, _available = _completed_record_is_available(
            rows[signal_index], allow_backfill=allow_backfill)
        if not signal_ok:
            continue
        value, _reason, exit_index = _clock_outcome(
            rows, signal_index=signal_index, horizon=horizon, anchor=anchor,
            direction="long", allow_backfill=allow_backfill,
            decision_at=decision_at)
        if value is None or exit_index is None:
            continue
        signal_end = _bar_end(rows[signal_index])
        decision_delay = (max(0.0, (decision_at - signal_end).total_seconds())
                          if signal_end is not None else None)
        available = replay_available_at(
            rows[exit_index], allow_historical_backfill_diagnostics=allow_backfill)
        exit_end = _bar_end(rows[exit_index])
        outcome_delay = (max(0.0, (available - exit_end).total_seconds())
                         if available is not None and exit_end is not None
                         else None)
        pool.setdefault(int(round(minutes)), (
            signal_index, minutes, value, decision_delay, outcome_delay))
    return pool


def _clock_control_return(
        candidate_cell: tuple[str, str], candidate_minutes: float, *,
        pools: Mapping[tuple[str, str], Mapping[int, tuple]],
        candidate_index: int, direction: str,
        tiers: Sequence[str] | None = None,
        ) -> tuple[float | None, str | None, dict[str, Any]]:
    """Average the wall-clock null pool matched to one candidate."""
    choices, matching, excluded = _choose_controls(
        candidate_cell, candidate_minutes, eligible=pools,
        candidate_index=candidate_index, tiers=tiers)
    if not choices:
        if excluded is not None:
            return None, "control_tier_not_registered", {
                "matching": f"excluded_{excluded}", "pool": 0}
        return None, "no_matched_control", {"matching": "none", "pool": 0}
    sign = 1.0 if direction == "long" else -1.0
    by_index = {(cell, item[0]): item for cell, minute_map in pools.items()
                if cell in {choice[0] for choice in choices}
                for item in minute_map.values()}
    values: list[float] = []
    minutes_used: list[float] = []
    decision_delays: list[float] = []
    outcome_delays: list[float] = []
    for cell, index, item_minutes in choices:
        item = by_index[(cell, index)]
        values.append(sign * float(item[2]))
        minutes_used.append(item_minutes)
        if item[3] is not None:
            decision_delays.append(float(item[3]))
        if item[4] is not None:
            outcome_delays.append(float(item[4]))
    metadata: dict[str, Any] = {"matching": matching, "pool": len(values),
                                "mean_session_minute": mean(minutes_used)}
    if decision_delays:
        metadata["mean_decision_delay_seconds"] = mean(decision_delays)
    if outcome_delays:
        metadata["mean_outcome_observation_delay_seconds"] = mean(outcome_delays)
    return mean(values), None, metadata


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


def _latency_summary(values: Sequence[float]) -> dict[str, Any]:
    clean = sorted(float(value) for value in values
                   if isinstance(value, (int, float)) and
                   not isinstance(value, bool) and math.isfinite(float(value)))
    if not clean:
        return {"count": 0, "mean_seconds": None, "median_seconds": None,
                "min_seconds": None, "max_seconds": None}
    return {"count": len(clean), "mean_seconds": mean(clean),
            "median_seconds": median(clean), "min_seconds": clean[0],
            "max_seconds": clean[-1]}


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


def _validate_precomputed_timing(
        rows: Sequence[Any], event: Mapping[str, Any],
        spec: Mapping[str, Any], *, allow_backfill: bool,
        ) -> tuple[dict[str, Any] | None, str | None]:
    """Validate the observation clock of a fit-prefix hand-off.

    The optional metadata avoids rescanning the predicate, but it cannot be
    allowed to bypass current source visibility.  Recheck the feature prefix,
    decision boundary, and selected post-signal row against the supplied bars;
    a stale or revised hand-off therefore fails closed while the evaluator is
    still invoked exactly once by the fit worker.
    """
    signal_index = int(event["signal_index"])
    entry_index = int(event["entry_index"])
    signal_end = _bar_end(rows[signal_index])
    if signal_end is None:
        return None, "precomputed_event_invalid"
    window = feature_window_bars(spec)
    feature_start = (0 if window is None else
                     max(0, signal_index + 1 - int(window)))
    decision, reason = _feature_decision(
        rows, feature_start, signal_index, signal_end=signal_end,
        allow_backfill=allow_backfill)
    if decision is None:
        return None, str(reason or "data_incomplete")
    entry_at = event.get("entry_at")
    if not isinstance(entry_at, datetime) or entry_at < decision:
        return None, "precomputed_event_invalid"
    expected_entry = next((probe for probe in range(signal_index + 1, len(rows))
                           if (_timestamp(rows[probe]) is not None and
                               _timestamp(rows[probe]) >= decision)), None)
    if expected_entry is None or expected_entry != entry_index:
        return None, "precomputed_event_invalid"
    return dict(event), None


def measure_signal_quality(
        bars: Sequence[Any], spec: Mapping[str, Any], *, policy: Any | None = None,
        horizons: Sequence[int] = DEFAULT_HORIZONS,
        cost_hurdle_bps: float | None = None,
        precomputed_first_signals: Sequence[Mapping[str, Any]] | None = None,
        eligibility_provenance: Mapping[str, Any] | None = None,
        bars_by_symbol: Mapping[str, Sequence[Any]] | None = None,
        measurement_scope: str = "fit_only",
        outcome_anchor: str = "signal_close",
        horizon_clock: str = "bar_count",
        control_tiers: Sequence[str] | None = None,
        delta_hurdle_bps: float | None = None,
        ) -> dict[str, Any]:
    """Measure conditional forward returns and a matched random-entry control.

    The defaults reproduce the original measurement exactly: the outcome is
    anchored at the signal bar's close and its horizon counts contiguous
    bars.  ``outcome_anchor`` (``next_bar_open`` or ``skip_one_bar_open``) and
    ``horizon_clock="wall_clock"`` select the executable, clock-defined
    outcome described at :data:`OUTCOME_ANCHORS`; ``signal_close`` with
    ``wall_clock`` is the close-anchored figure on the same clock.
    ``control_tiers`` restricts which control tier may match a candidate; a
    candidate whose tier is outside it is reported unmatched.
    ``delta_hurdle_bps`` adds clustered tests of the control-adjusted delta
    against that hurdle instead of against zero.

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
    if not isinstance(measurement_scope, str) or \
            measurement_scope not in _MEASUREMENT_SCOPES:
        raise ValueError(
            "measurement_scope must be fit_only or "
            "previously_examined_retrospective_diagnostic")
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
    if outcome_anchor not in OUTCOME_ANCHORS:
        raise ValueError(f"outcome_anchor must be one of {OUTCOME_ANCHORS}")
    if horizon_clock not in HORIZON_CLOCKS:
        raise ValueError(f"horizon_clock must be one of {HORIZON_CLOCKS}")
    legacy_outcome = (outcome_anchor, horizon_clock) == ("signal_close", "bar_count")
    if horizon_clock == "bar_count" and not legacy_outcome:
        raise ValueError("open anchors are defined on the wall clock only")
    if control_tiers is not None:
        control_tiers = tuple(str(item) for item in control_tiers)
        if not control_tiers or any(item not in CONTROL_TIERS
                                    for item in control_tiers):
            raise ValueError(f"control_tiers must be drawn from {CONTROL_TIERS}")
    if delta_hurdle_bps is not None:
        delta_hurdle_bps = float(delta_hurdle_bps)
        if not math.isfinite(delta_hurdle_bps):
            raise ValueError("delta_hurdle_bps must be finite")
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
            if event is not None:
                event, reason = _validate_precomputed_timing(
                    rows, event, normalized, allow_backfill=allow_backfill)
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
        # Session identity for each candidate and paired delta.  Intraday
        # events inside one session, across correlated ETFs, share that
        # session's shock; inference has to treat the session as the unit.
        candidate_sessions: list[str] = []
        delta_sessions: list[str] = []
        candidate_minutes: list[float] = []
        control_minutes: list[float] = []
        candidate_decision_delays: list[float] = []
        candidate_outcome_delays: list[float] = []
        control_decision_delays: list[float] = []
        control_outcome_delays: list[float] = []
        pool_sizes: list[int] = []
        matching_counts: Counter[str] = Counter()
        unavailable: Counter[str] = Counter()
        sessions: set[str] = set()
        symbols: set[str] = set()
        cells: set[str] = set()
        # Built once per horizon rather than once per candidate: the null pool
        # for a session does not depend on which candidate is asking for it.
        if legacy_outcome:
            eligible = {cell: _eligible_by_minute(
                rows, control_policy, horizon=horizon,
                allow_backfill=allow_backfill)
                for cell, rows in grouped.items()}
        else:
            eligible = {cell: _clock_control_pool(
                rows, control_policy, horizon=horizon, anchor=outcome_anchor,
                allow_backfill=allow_backfill)
                for cell, rows in grouped.items()}
        for symbol, session, rows, event in events:
            direction = str(event["signal"]["direction"])
            signal_index = int(event["signal_index"])
            signal_end = _bar_end(rows[signal_index])
            decision_at = event.get("entry_at")
            if not isinstance(decision_at, datetime):
                decision_at = signal_end
            if legacy_outcome:
                candidate, reason = _forward_return(
                    # Keep the outcome clock anchored to the signal close.  The
                    # selected entry row remains provenance for delayed
                    # decisions but must not shift the conditional-return
                    # horizon.
                    rows, entry_index=signal_index + 1, horizon=horizon,
                    entry_price=float(event["entry_price"]), direction=direction,
                    allow_backfill=allow_backfill, decision_at=decision_at)
                outcome_index = signal_index + horizon
            else:
                candidate, reason, clock_index = _clock_outcome(
                    rows, signal_index=signal_index, horizon=horizon,
                    anchor=outcome_anchor, direction=direction,
                    allow_backfill=allow_backfill, decision_at=decision_at,
                    signal_price=float(event["entry_price"]))
                outcome_index = (clock_index if clock_index is not None
                                 else len(rows))
            if candidate is None:
                unavailable[str(reason or "candidate_unavailable")] += 1
                continue
            candidates.append(candidate)
            candidate_sessions.append(str(session))
            sessions.add(session)
            symbols.add(symbol)
            cells.add(f"{session}:{symbol}")
            if signal_end is not None and decision_at is not None:
                candidate_decision_delays.append(max(
                    0.0, (decision_at - signal_end).total_seconds()))
            if outcome_index < len(rows):
                outcome_available = replay_available_at(
                    rows[outcome_index],
                    allow_historical_backfill_diagnostics=allow_backfill)
                outcome_end = _bar_end(rows[outcome_index])
                if outcome_available is not None and outcome_end is not None:
                    candidate_outcome_delays.append(max(
                        0.0, (outcome_available - outcome_end).total_seconds()))
            signal_stamp = _timestamp(rows[signal_index])
            minutes = (session_minutes(signal_stamp)
                       if signal_stamp is not None else 0.0)
            candidate_minutes.append(minutes)
            if legacy_outcome:
                control, control_reason, control_meta = _control_return(
                    (symbol, session), minutes, eligible=eligible,
                    session_rows=grouped,
                    candidate_index=int(event["signal_index"]),
                    direction=direction, horizon=horizon,
                    allow_backfill=allow_backfill, policy=control_policy,
                    tiers=control_tiers)
            else:
                control, control_reason, control_meta = _clock_control_return(
                    (symbol, session), minutes, pools=eligible,
                    candidate_index=int(event["signal_index"]),
                    direction=direction, tiers=control_tiers)
            matching_counts[str(control_meta["matching"])] += 1
            if control is None:
                unavailable[str(control_reason or "control_unavailable")] += 1
            else:
                controls.append(control)
                paired_deltas.append(candidate - control)
                delta_sessions.append(str(session))
                pool_sizes.append(int(control_meta["pool"]))
                control_minutes.append(float(control_meta["mean_session_minute"]))
                if control_meta.get("mean_decision_delay_seconds") is not None:
                    control_decision_delays.append(float(
                        control_meta["mean_decision_delay_seconds"]))
                if control_meta.get("mean_outcome_observation_delay_seconds") is not None:
                    control_outcome_delays.append(float(
                        control_meta["mean_outcome_observation_delay_seconds"]))
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
        delta_clustered = cluster_robust_mean_inference(
            paired_deltas, delta_sessions)
        after_hurdle_clustered = (cluster_robust_mean_inference(
            candidates, candidate_sessions, shift=float(hurdle))
            if hurdle is not None else None)
        # One-sided cluster sign-flip over whole sessions, seeded from the
        # data so the value is reproducible from its inputs alone.  It needs
        # no distributional assumption, so it stays readable at the small
        # cluster counts where a Student-t reference is least trustworthy.
        delta_sign_flip = (sign_flip_null_statistics(
            paired_deltas, delta_sessions, draws=DEFAULT_NULL_DRAWS)
            if len(set(delta_sessions)) >= 2 else None)
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
            # ``candidate_minus_control_t_stat`` above is event-level and
            # treats every intraday event as independent.  These are the
            # session-clustered counterparts and are the ones to read against
            # a threshold: Student-t with ``..._cluster_df`` degrees of
            # freedom, and a one-sided sign-flip p over whole sessions.
            "inference_cluster_unit": "session",
            "candidate_minus_control_cluster_stderr_bps":
                delta_clustered["stderr"],
            "candidate_minus_control_cluster_t_stat": delta_clustered["t_stat"],
            "candidate_minus_control_cluster_df": delta_clustered["df"],
            "candidate_minus_control_cluster_sign_flip_p_value": (
                delta_sign_flip["p_value"]
                if delta_sign_flip and delta_sign_flip.get("available") else None),
            "control_matching_counts": dict(sorted(matching_counts.items())),
            "control_pool_mean": mean(pool_sizes) if pool_sizes else None,
            "candidate_mean_session_minute": (
                mean(candidate_minutes) if candidate_minutes else None),
            "control_mean_session_minute": (
                mean(control_minutes) if control_minutes else None),
            "candidate_decision_delay_seconds": _latency_summary(
                candidate_decision_delays),
            "candidate_outcome_observation_delay_seconds": _latency_summary(
                candidate_outcome_delays),
            "control_decision_delay_seconds": _latency_summary(
                control_decision_delays),
            "control_outcome_observation_delay_seconds": _latency_summary(
                control_outcome_delays),
            "cost_hurdle_bps": hurdle,
            "mean_after_hurdle_bps": (
                mean(after_hurdle) if after_hurdle else None),
            "after_hurdle_t_stat": (
                _dispersion(candidates, shift=hurdle)["t_stat"]
                if hurdle is not None else None),
            "after_hurdle_cluster_t_stat": (
                after_hurdle_clustered["t_stat"]
                if after_hurdle_clustered is not None else None),
            "hurdle_exceedance_rate": (
                sum(value > hurdle for value in candidates) / len(candidates)
                if candidates and hurdle is not None else None),
            "unavailable_count": sum(unavailable.values()),
            "unavailable_reason_counts": dict(sorted(unavailable.items())),
            "session_clusters": len(sessions),
            "symbol_clusters": len(symbols),
            "session_symbol_cells": len(cells),
        }
        if delta_hurdle_bps is not None:
            # The same session-clustered tests, of the delta against the
            # hurdle rather than against zero.  The sign flip centres every
            # paired delta on the hurdle before flipping whole sessions.
            shifted_flip = (sign_flip_null_statistics(
                [value - delta_hurdle_bps for value in paired_deltas],
                delta_sessions, draws=DEFAULT_NULL_DRAWS)
                if len(set(delta_sessions)) >= 2 else None)
            horizon_metrics[f"{horizon}m"].update({
                "delta_hurdle_bps": delta_hurdle_bps,
                "candidate_minus_control_after_hurdle_cluster_t_stat": (
                    cluster_robust_mean_inference(
                        paired_deltas, delta_sessions,
                        shift=delta_hurdle_bps)["t_stat"]),
                "candidate_minus_control_after_hurdle_sign_flip_p_value": (
                    shifted_flip["p_value"]
                    if shifted_flip and shifted_flip.get("available")
                    else None),
            })
    # Build a compact summary from the actual event rejection categories.  A
    # precomputed fit hand-off may additionally supply detailed prefix counts;
    # those are copied only through the bounded summary helper.
    provenance = dict(eligibility_provenance or {})
    quality_provenance = _provenance_summary(
        provenance if provenance else None,
        total_cells=len(grouped),
        measurement_scope=measurement_scope,
        fallback_cells=(
            {**observed_cell_reasons,
             **{_cell_key(symbol, session): str(reason)
                for (symbol, session), reason in provenance_by_cell.items()}}
            if (observed_cell_reasons or provenance_by_cell) else None))
    result = {
        "schema": SIGNAL_QUALITY_SCHEMA,
        "scope": measurement_scope,
        "measurement_scope": measurement_scope,
        "authorizing": False,
        "diagnostic_only": True,
        "metric": "conditional_forward_return",
        "canonical_cross_sectional_ic": False,
        "event_policy": "first_actionable_signal_per_symbol_session",
        "return_basis": "signal_bar_close_to_contiguous_horizon_close",
        "timing_policy": {
            "measurement_scope": measurement_scope,
            "feature_prefix": "available_by_decision_timestamp",
            "decision_timestamp": "max(signal_bar_end, feature_observation_times)",
            "outcome": "completed_bar_observed_after_decision_timestamp",
            "entry_semantics": "conditional_signal_close_anchor",
            "historical_backfill": "excluded_by_default",
            "revision_policy": "unresolved_revision_or_version_refused",
        },
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
    if not legacy_outcome:
        result["return_basis"] = (
            f"{outcome_anchor}_to_wall_clock_horizon_close")
        result["outcome_anchor"] = outcome_anchor
        result["horizon_clock"] = horizon_clock
        result["timing_policy"] = {
            **result["timing_policy"],
            "entry_semantics": {
                "signal_close": "conditional_signal_close_anchor",
                "next_bar_open": "open_of_bar_starting_at_signal_end",
                "skip_one_bar_open": "open_of_bar_starting_one_minute_after_signal_end",
            }[outcome_anchor],
            "horizon": "close_of_bar_ending_horizon_minutes_after_entry_instant",
        }
    if control_tiers is not None:
        result["control_tiers"] = list(control_tiers)
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


__all__ = ["CONTROL_TIERS", "DEFAULT_HORIZONS", "HORIZON_CLOCKS",
           "OUTCOME_ANCHORS", "SIGNAL_QUALITY_ELIGIBILITY_SCHEMA",
           "SIGNAL_QUALITY_SCHEMA", "measure_signal_quality"]
