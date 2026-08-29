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
from statistics import mean, median
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from agent.contracts.rule import (
    evaluate_rule_signal, feature_window_bars, rule_variant_id,
    validate_rule_spec,
)
from .market_data import replay_available_at, replay_record_is_available


SIGNAL_QUALITY_SCHEMA = "signal-quality.v1"
DEFAULT_HORIZONS = (5, 15, 30, 60, 120, 390)
_NY = ZoneInfo("America/New_York")


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


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _first_event(rows: Sequence[Any], spec: Mapping[str, Any], *,
                 allow_backfill: bool) -> tuple[dict[str, Any] | None, str | None]:
    """Return the first causal signal/entry pair for one symbol-session."""
    window = feature_window_bars(spec)
    for index in range(1, max(1, len(rows) - 1)):
        feature_start = 0 if window is None else max(0, index + 1 - int(window))
        feature_rows = rows[feature_start:index + 1]
        if not _contiguous(rows, feature_start, index + 1):
            continue
        signal_end = _bar_end(rows[index])
        if signal_end is None:
            continue
        available = [replay_available_at(
            row, allow_historical_backfill_diagnostics=allow_backfill)
            for row in feature_rows]
        if any(item is None for item in available):
            continue
        decision = max([signal_end, *(item for item in available if item is not None)])
        next_row = rows[index + 1]
        if _timestamp(next_row) != signal_end and decision <= signal_end:
            continue
        entry_at = signal_end if decision <= signal_end else decision
        entry_index = next((probe for probe in range(index + 1, len(rows))
                            if (_timestamp(rows[probe]) is not None and
                                _timestamp(rows[probe]) >= entry_at)), None)
        if entry_index is None:
            continue
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
    return None, "no_actionable_signal"


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


def _control_indices(rows: Sequence[Any], *, horizon: int,
                     allow_backfill: bool) -> list[int]:
    eligible: list[int] = []
    for signal_index in range(0, len(rows)):
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
            eligible.append(signal_index)
    return eligible


def _control_return(rows: Sequence[Any], *, candidate_index: int,
                    direction: str, horizon: int, seed_key: str,
                    allow_backfill: bool) -> tuple[float | None, str | None, int | None]:
    choices = _control_indices(rows, horizon=horizon,
                               allow_backfill=allow_backfill)
    alternatives = [index for index in choices if index != candidate_index]
    if alternatives:
        choices = alternatives
    if not choices:
        return None, "no_matched_control", None
    selector = int(_digest({"seed": seed_key, "horizon": horizon})[:16], 16)
    index = choices[selector % len(choices)]
    entry_price = float(_value(rows[index], "close"))
    value, reason = _forward_return(
        rows, entry_index=index + 1, horizon=horizon, entry_price=entry_price,
        direction=direction, allow_backfill=allow_backfill)
    return value, reason, index


def _summary(values: Sequence[float]) -> tuple[float | None, float | None, float | None]:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    if not clean:
        return None, None, None
    return mean(clean), median(clean), sum(value > 0 for value in clean) / len(clean)


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
        ) -> dict[str, Any]:
    """Measure conditional forward returns and a matched random-entry control.

    ``precomputed_first_signals`` is an optional internal hand-off from
    :func:`research.fit_diagnostics._fit_prefixes`. When omitted, the
    historical prefix scan remains authoritative. When supplied, every
    event is validated against its sorted symbol/session rows and malformed
    metadata is rejected rather than silently falling back to a scan.
    """
    normalized = validate_rule_spec(spec)
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

    events: list[tuple[str, str, Sequence[Any], dict[str, Any]]] = []
    event_reasons: Counter[str] = Counter()
    event_time_buckets: Counter[str] = Counter()
    precomputed_by_cell: dict[tuple[str, str], dict[str, Any]] = {}
    precomputed_invalid_cells: set[tuple[str, str]] = set()
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
                rows, normalized, allow_backfill=allow_backfill)
        elif (symbol, session) in precomputed_invalid_cells:
            event, reason = None, "precomputed_event_invalid"
        else:
            event = precomputed_by_cell.get((symbol, session))
            reason = None if event is not None else "no_actionable_signal"
        if event is None:
            event_reasons[str(reason or "unknown")] += 1
            continue
        events.append((symbol, session, rows, event))
        signal_stamp = datetime.fromtimestamp(
            float(event["signal"]["signal_ts"]), timezone.utc).astimezone(_NY)
        minutes = (signal_stamp.hour * 60 + signal_stamp.minute) - (9 * 60 + 30)
        bucket = ("opening_0_60m" if minutes < 60 else
                  "midday_60_300m" if minutes < 300 else "close_300m_plus")
        event_time_buckets[bucket] += 1

    horizon_metrics: dict[str, Any] = {}
    event_vectors: list[dict[str, Any]] = []
    for horizon in requested:
        candidates: list[float] = []
        controls: list[float] = []
        paired_deltas: list[float] = []
        unavailable: Counter[str] = Counter()
        sessions: set[str] = set()
        symbols: set[str] = set()
        cells: set[str] = set()
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
            seed_key = _digest({
                "variant_id": rule_variant_id(normalized), "symbol": symbol,
                "session": session, "signal_index": event["signal_index"],
            })
            control, control_reason, control_index = _control_return(
                rows, candidate_index=int(event["signal_index"]),
                direction=direction, horizon=horizon, seed_key=seed_key,
                allow_backfill=allow_backfill)
            if control is None:
                unavailable[str(control_reason or "control_unavailable")] += 1
            else:
                controls.append(control)
                paired_deltas.append(candidate - control)
            event_vectors.append({
                "symbol": symbol, "session": session, "horizon": horizon,
                "direction": direction, "signal_index": event["signal_index"],
                "entry_index": event["entry_index"],
                "control_index": control_index,
            })
        candidate_mean, candidate_median, positive_rate = _summary(candidates)
        control_mean, _control_median, _control_positive = _summary(controls)
        hurdle = cost_hurdle_bps
        after_hurdle = ([value - hurdle for value in candidates]
                        if hurdle is not None else [])
        horizon_metrics[f"{horizon}m"] = {
            "horizon_minutes": horizon,
            "candidate_count": len(candidates),
            "matched_count": len(paired_deltas),
            "mean_forward_return_bps": candidate_mean,
            "median_forward_return_bps": candidate_median,
            "positive_rate": positive_rate,
            "control_mean_forward_return_bps": control_mean,
            "candidate_minus_control_bps": (
                mean(paired_deltas) if paired_deltas else None),
            "cost_hurdle_bps": hurdle,
            "mean_after_hurdle_bps": (
                mean(after_hurdle) if after_hurdle else None),
            "hurdle_exceedance_rate": (
                sum(value > hurdle for value in candidates) / len(candidates)
                if candidates and hurdle is not None else None),
            "unavailable_count": sum(unavailable.values()),
            "unavailable_reason_counts": dict(sorted(unavailable.items())),
            "session_clusters": len(sessions),
            "symbol_clusters": len(symbols),
            "session_symbol_cells": len(cells),
        }
    return {
        "schema": SIGNAL_QUALITY_SCHEMA,
        "scope": "fit_only",
        "authorizing": False,
        "diagnostic_only": True,
        "metric": "conditional_forward_return",
        "canonical_cross_sectional_ic": False,
        "event_policy": "first_actionable_signal_per_symbol_session",
        "return_basis": "signal_bar_close_to_horizon_close_with_next_bar_lag",
        "control_policy": "deterministic_same_symbol_session_random_entry",
        "variant_id": rule_variant_id(normalized),
        "horizons": list(requested),
        "event_count": len(events),
        "session_count": len({session for _symbol_name, session, _rows, _event in events}),
        "symbol_count": len({symbol for symbol, _session_name, _rows, _event in events}),
        "event_rejection_counts": dict(sorted(event_reasons.items())),
        "event_time_bucket_counts": dict(sorted(event_time_buckets.items())),
        "event_digest": _digest(sorted(event_vectors, key=_canonical)),
        "horizon_metrics": horizon_metrics,
    }


__all__ = ["DEFAULT_HORIZONS", "SIGNAL_QUALITY_SCHEMA",
           "measure_signal_quality"]
