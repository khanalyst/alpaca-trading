"""Fit/replay-only path excursion telemetry.

This module measures what a position could have seen between the *actual*
entry and its observed exit (or configured deadline).  It deliberately does
not replay or authorize exits: the exit reason and conservative same-bar tie
decision already come from :mod:`agent.contracts.rule`.  Only contiguous,
completed OHLC bars are used, so a missing bar is reported as censoring rather
than being silently rolled forward.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from statistics import mean, median
from typing import Any, Mapping, Sequence


PATH_TELEMETRY_SCHEMA = "path-telemetry.v1"
PATH_AGGREGATE_SCHEMA = "path-telemetry-aggregate.v1"
TARGET_HOLD_REACHABILITY_SCHEMA = "target-hold-reachability.v1"

# This is intentionally a finite, preregistered ladder.  The factory's
# discovery shapes use the same values; keeping the diagnostic ladder here
# makes reachability a measurement of the already-audited search space rather
# than a continuous optimizer.
TARGET_HOLD_TARGET_LADDER: tuple[float, ...] = (
    0.25, 0.5, 1.0, 1.25, 2.0, 3.0, 5.0, 10.0)
TARGET_HOLD_HOLD_LADDER: tuple[int, ...] = (
    1, 10, 30, 45, 60, 90, 180, 240, 390)
TARGET_HOLD_MIN_USABLE = 30


def _value(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _datetime(value: Any) -> datetime | None:
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
    except (TypeError, ValueError, OverflowError):
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _bar_start(row: Any) -> datetime | None:
    return _datetime(_value(row, "timestamp", _value(row, "ts")))


def _bar_end(row: Any, start: datetime | None) -> datetime | None:
    explicit = _datetime(_value(row, "end"))
    if explicit is not None:
        return explicit
    interval = _number(_value(row, "interval_seconds", 60))
    if start is None or interval is None or interval <= 0:
        return None
    return start + timedelta(seconds=interval)


def _trade_symbol(trade: Mapping[str, Any]) -> str | None:
    value = trade.get("symbol")
    return str(value).upper() if value not in (None, "") else None


def _trade_session(trade: Mapping[str, Any]) -> str | None:
    value = trade.get("session_date")
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)[:10]


def _entry_price(trade: Mapping[str, Any]) -> float | None:
    # ``underlying_entry`` is the actual path anchor for both equity and an
    # option's underlying.  The quote ``entry_reference`` is an option premium
    # and is only a compatibility fallback when no underlying anchor exists.
    for key in ("underlying_entry", "actual_entry_price", "entry_price",
                "entry_reference", "plan_entry"):
        value = _number(trade.get(key))
        if value is not None and value > 0:
            return value
    return None


def _risk_unit(trade: Mapping[str, Any], entry: float) -> float | None:
    # The path is measured on underlying OHLC bars.  For an option trade the
    # premium-at-risk value is in dollars per contract and is not commensurate
    # with an underlying-price excursion, so prefer the underlying stop unit.
    for key in ("stop_distance", "planned_stop_distance",
                "realized_risk_per_unit"):
        value = _number(trade.get(key))
        if value is not None and value > 0:
            return value
    stop = _number(trade.get("stop_price"))
    if stop is not None and stop > 0:
        distance = abs(entry - stop)
        if distance > 0:
            return distance
    initial_stop = _number(trade.get("initial_stop_price"))
    if initial_stop is not None and initial_stop > 0:
        distance = abs(entry - initial_stop)
        if distance > 0:
            return distance
    return None


def _deadline(trade: Mapping[str, Any], entry_at: datetime | None,
              first_bar: Any | None, max_hold_bars: int | None) -> datetime | None:
    for key in ("deadline_timestamp", "hold_deadline", "deadline"):
        parsed = _datetime(trade.get(key))
        if parsed is not None:
            return parsed
    if entry_at is None:
        return None
    hold = max_hold_bars
    if hold is None:
        raw = trade.get("max_hold_bars")
        try:
            hold = int(raw) if raw is not None else None
        except (TypeError, ValueError, OverflowError):
            hold = None
    if hold is None or hold < 0:
        return None
    interval = _number(_value(first_bar, "interval_seconds", 60)) if first_bar is not None else 60.0
    if interval is None or interval <= 0:
        interval = 60.0
    # Match agent.contracts.rule.hold_deadline: the entry bar plus ``hold``
    # further completed bars are observable before the time exit.
    return entry_at + timedelta(seconds=float(hold + 1) * interval)


def _clean_bars(trade: Mapping[str, Any], bars: Sequence[Any]) -> list[tuple[datetime, datetime, Any]]:
    symbol = _trade_symbol(trade)
    session = _trade_session(trade)
    rows: list[tuple[datetime, datetime, Any]] = []
    for row in bars:
        row_symbol = _value(row, "symbol")
        if symbol is not None and row_symbol not in (None, "") and str(row_symbol).upper() != symbol:
            continue
        stamp = _bar_start(row)
        ended = _bar_end(row, stamp)
        if stamp is None or ended is None or ended <= stamp:
            continue
        row_session = _value(row, "session_date")
        if session is not None and row_session not in (None, ""):
            normalized = (row_session.isoformat() if hasattr(row_session, "isoformat")
                         else str(row_session)[:10])
            if normalized != session:
                continue
        if any(_number(_value(row, name)) is None for name in ("open", "high", "low", "close")):
            continue
        rows.append((stamp, ended, row))
    rows.sort(key=lambda item: (item[0], item[1]))
    return rows


def compute_path_telemetry(
        trade: Mapping[str, Any], bars: Sequence[Any], *,
        target_r: float | None = None,
        max_hold_bars: int | None = None) -> dict[str, Any]:
    """Measure directional MFE/MAE on one executed trade.

    A bar is eligible only when its completed end is at or before the observed
    exit/deadline.  The path begins at the first bar whose open is the actual
    entry boundary.  Once a timestamp discontinuity appears, the path stops at
    the preceding bar and marks the result as censored.  ``mae_bps`` and
    ``mae_r`` are signed adverse excursions (normally negative); MFE values are
    non-negative favorable excursions.
    """
    trade = dict(trade) if isinstance(trade, Mapping) else {}
    direction = str(trade.get("direction") or "").lower()
    if direction not in {"long", "short"}:
        direction = "unknown"
    entry = _entry_price(trade)
    entry_at = _datetime(trade.get("entry_timestamp"))
    exit_at = _datetime(trade.get("exit_timestamp"))
    source_rows = _clean_bars(trade, bars)
    first_bar = source_rows[0][2] if source_rows else None
    deadline = _deadline(trade, entry_at, first_bar, max_hold_bars)
    if target_r is None:
        target_r = _number(trade.get("target_r"))
    hold_value = max_hold_bars
    if hold_value is None:
        try:
            hold_value = int(trade["max_hold_bars"]) if trade.get("max_hold_bars") is not None else None
        except (TypeError, ValueError, OverflowError):
            hold_value = None

    # A configured deadline bounds the path; an observed exit bounds it too.
    horizon = exit_at
    if deadline is not None and (horizon is None or deadline < horizon):
        horizon = deadline
    path: list[tuple[datetime, datetime, Any]] = []
    entry_missing = False
    started = False
    gap_detected = False
    gap_count = 0
    gap_from: datetime | None = None
    gap_to: datetime | None = None
    expected_interval: float | None = None
    for stamp, ended, row in source_rows:
        if entry_at is not None and stamp < entry_at:
            continue
        if horizon is not None and ended > horizon:
            break
        if not started:
            if entry_at is not None and stamp > entry_at:
                entry_missing = True
            started = True
        if path:
            previous_stamp, previous_end, previous_row = path[-1]
            interval = _number(_value(previous_row, "interval_seconds", 60)) or 60.0
            expected_interval = expected_interval or interval
            delta = (stamp - previous_stamp).total_seconds()
            if delta > interval + 1e-6 or delta < interval - 1e-6:
                gap_detected = True
                gap_count += 1
                gap_from = previous_end
                gap_to = stamp
                break
            if stamp == previous_stamp:
                gap_detected = True
                gap_count += 1
                gap_from = previous_end
                gap_to = stamp
                break
        path.append((stamp, ended, row))

    observed = len(path)
    terminal_end = path[-1][1] if path else None
    # A gap, an absent entry boundary, or data ending before the configured
    # deadline means that the excursion is right-censored.  A normal exit at a
    # completed bar (including a same-bar stop/target tie) is not censored.
    right_censored = bool(gap_detected or entry_missing)
    censor_reason: str | None = None
    if gap_detected:
        censor_reason = "internal_gap"
    elif entry_missing:
        censor_reason = "entry_bar_missing"
    elif deadline is not None and (exit_at is None or exit_at > deadline):
        right_censored = True
        censor_reason = "deadline_not_observed"
    elif deadline is not None and terminal_end is not None and terminal_end < deadline and exit_at is None:
        right_censored = True
        censor_reason = "observed_data_end"

    # A hold interrupted by an outage/data end resolves on the last observed
    # bar.  Its selected exit timestamp therefore cannot reveal the missing
    # interval; preserve the replay's explicit censoring provenance.
    discontinuity = bool(trade.get("hold_discontinuity_exit") or
                         trade.get("hold_discontinuity"))
    discontinuity_kind = str(trade.get("hold_discontinuity_kind") or "")
    if discontinuity:
        right_censored = True
        censor_reason = ("internal_gap" if discontinuity_kind == "internal_gap"
                         else "observed_data_end" if discontinuity_kind == "observed_data_end"
                         else "hold_discontinuity")
        if discontinuity_kind == "internal_gap":
            gap_detected = True
            gap_count = max(gap_count, 1)

    mfe_price = mae_price = None
    mfe_bps = mae_bps = mfe_r = mae_r = None
    if entry is not None and direction in {"long", "short"} and path:
        favorable: list[float] = []
        adverse: list[float] = []
        for _stamp, _ended, row in path:
            high = float(_number(_value(row, "high")))
            low = float(_number(_value(row, "low")))
            if direction == "long":
                favorable.append(high - entry)
                adverse.append(low - entry)
            else:
                favorable.append(entry - low)
                adverse.append(entry - high)
        mfe_price = max(favorable)
        # MAE is a signed directional excursion; an adverse path is negative.
        mae_price = min(adverse)
        mfe_bps = mfe_price / entry * 10_000.0
        mae_bps = mae_price / entry * 10_000.0
        risk = _risk_unit(trade, entry)
        if risk is not None:
            mfe_r = mfe_price / risk
            mae_r = mae_price / risk

    tie_broken = bool(trade.get("tie_broken"))
    exit_reason = str(trade.get("exit_reason") or trade.get("hold_exit_reason") or "unknown")
    return {
        "schema": PATH_TELEMETRY_SCHEMA,
        "available": bool(path and entry is not None and direction in {"long", "short"}),
        "symbol": _trade_symbol(trade),
        "session_date": _trade_session(trade),
        "direction": direction,
        "entry_timestamp": entry_at.isoformat() if entry_at is not None else None,
        "exit_timestamp": exit_at.isoformat() if exit_at is not None else None,
        "deadline_timestamp": deadline.isoformat() if deadline is not None else None,
        "entry_price": entry,
        "risk_unit": _risk_unit(trade, entry) if entry is not None else None,
        "target_r": target_r,
        "max_hold_bars": hold_value,
        "exit_reason": exit_reason,
        "tie_broken": tie_broken,
        "observed_bars": observed,
        "completed_bars": observed,
        "expected_interval_seconds": expected_interval,
        "gap_detected": gap_detected,
        "gap_count": gap_count,
        "gap_from": gap_from.isoformat() if gap_from is not None else None,
        "gap_to": gap_to.isoformat() if gap_to is not None else None,
        "hold_discontinuity": discontinuity,
        "hold_discontinuity_kind": discontinuity_kind or None,
        "right_censored": right_censored,
        "censor_reason": censor_reason,
        "exit_observed": bool(exit_at is not None and terminal_end == exit_at),
        "mfe_price": mfe_price,
        "mae_price": mae_price,
        "mfe_bps": mfe_bps,
        "mae_bps": mae_bps,
        "mfe_r": mfe_r,
        "mae_r": mae_r,
        "mfe_at_exit_bps": mfe_bps,
        "mae_at_exit_bps": mae_bps,
        "mfe_at_exit_r": mfe_r,
        "mae_at_exit_r": mae_r,
    }


def _metric(values: Sequence[Any]) -> dict[str, Any]:
    clean = sorted(value for value in (_number(item) for item in values) if value is not None)
    return {"count": len(clean), "mean": mean(clean) if clean else None,
            "median": median(clean) if clean else None,
            "min": clean[0] if clean else None,
            "max": clean[-1] if clean else None}


def aggregate_path_telemetry(rows: Sequence[Any]) -> dict[str, Any]:
    """Aggregate path rows by target-R, hold cap, and exit reason."""
    groups: dict[tuple[Any, Any, str], list[Mapping[str, Any]]] = {}
    for raw in rows:
        item = raw.get("path_telemetry") if isinstance(raw, Mapping) else None
        item = item if isinstance(item, Mapping) else raw
        if not isinstance(item, Mapping) or not item.get("available"):
            continue
        target = _number(item.get("target_r"))
        hold = item.get("max_hold_bars")
        try:
            hold = int(hold) if hold is not None else None
        except (TypeError, ValueError, OverflowError):
            hold = None
        reason = str(item.get("exit_reason") or "unknown")
        key = (target, hold, reason)
        groups.setdefault(key, []).append(item)
    rendered: list[dict[str, Any]] = []
    for (target, hold, reason), items in sorted(groups.items(), key=lambda pair: (
            float("inf") if pair[0][0] is None else pair[0][0],
            float("inf") if pair[0][1] is None else pair[0][1], pair[0][2])):
        rendered.append({
            "target_r": target, "max_hold_bars": hold, "exit_reason": reason,
            "count": len(items),
            "right_censored": sum(bool(item.get("right_censored")) for item in items),
            "gapped": sum(bool(item.get("gap_detected")) for item in items),
            "observed_bars": _metric([item.get("observed_bars") for item in items]),
            "mfe_bps": _metric([item.get("mfe_bps") for item in items]),
            "mae_bps": _metric([item.get("mae_bps") for item in items]),
            "mfe_r": _metric([item.get("mfe_r") for item in items]),
            "mae_r": _metric([item.get("mae_r") for item in items]),
        })
    return {
        "schema": PATH_AGGREGATE_SCHEMA,
        "diagnostic_only": True,
        "group_by": ["target_r", "max_hold_bars", "exit_reason"],
        "trade_count": sum(item["count"] for item in rendered),
        "groups": rendered,
    }


def target_hold_reachability(
        rows: Sequence[Any], *, target_r: float | None = None,
        max_hold_bars: int | None = None,
        target_ladder: Sequence[float] = TARGET_HOLD_TARGET_LADDER,
        hold_ladder: Sequence[int] = TARGET_HOLD_HOLD_LADDER,
        min_usable: int = TARGET_HOLD_MIN_USABLE,
        ) -> dict[str, Any]:
    """Summarize fit-only target/hold reachability.

    The input is path telemetry, never market rows.  A row is usable only when
    its directional MFE/MAE in R and completed-bar count are finite and the
    path is neither gapped nor right-censored.  The recommendation is
    deliberately conservative: it can only lower an unreachable configured
    target on the finite ladder, and is emitted only with a repeated
    time-expiry/unreachable-target mismatch and at least ``min_usable`` usable
    paths.  No future bars, held-out metrics, or continuous optimization enter
    this result.
    """
    normalized_rows: list[dict[str, Any]] = []
    total = censored = unavailable = 0
    for raw in rows:
        item = raw.get("path_telemetry") if isinstance(raw, Mapping) else None
        item = item if isinstance(item, Mapping) else raw
        if not isinstance(item, Mapping):
            continue
        total += 1
        is_censored = bool(item.get("right_censored") or
                          item.get("gap_detected"))
        if is_censored:
            censored += 1
        if item.get("available") is False:
            unavailable += int(not is_censored)
            continue
        mfe = _number(item.get("mfe_r"))
        mae = _number(item.get("mae_r"))
        try:
            observed = int(item.get("observed_bars"))
        except (TypeError, ValueError, OverflowError):
            observed = -1
        if (mfe is None or mae is None or observed < 1 or is_censored):
            unavailable += int(not is_censored)
            continue
        normalized_rows.append({
            "mfe_r": mfe, "mae_r": mae, "observed_bars": observed,
            "exit_reason": str(item.get("exit_reason") or
                               item.get("hold_exit_reason") or "unknown"),
        })

    def finite_targets(values: Sequence[Any]) -> list[float]:
        result: set[float] = set()
        for value in values:
            parsed = _number(value)
            if parsed is not None and parsed > 0:
                result.add(round(parsed, 8))
        configured = _number(target_r)
        if configured is not None and configured > 0:
            result.add(round(configured, 8))
        return sorted(result)

    def finite_holds(values: Sequence[Any]) -> list[int]:
        result: set[int] = set()
        for value in values:
            try:
                parsed = int(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if parsed > 0:
                result.add(parsed)
        configured = max_hold_bars
        try:
            configured_int = int(configured) if configured is not None else None
        except (TypeError, ValueError, OverflowError):
            configured_int = None
        if configured_int is not None and configured_int > 0:
            result.add(configured_int)
        return sorted(result)

    targets = finite_targets(target_ladder)
    holds = finite_holds(hold_ladder)
    matrix: list[dict[str, Any]] = []
    for target in targets:
        for hold in holds:
            eligible = [item for item in normalized_rows
                        if item["observed_bars"] >= hold]
            reached = sum(item["mfe_r"] >= target for item in eligible)
            matrix.append({
                "target_r": target, "max_hold_bars": hold,
                "eligible": len(eligible), "reached": reached,
                "unreached": len(eligible) - reached,
                "reach_rate": reached / len(eligible) if eligible else None,
                "diagnostic_only": True,
            })

    configured_target = _number(target_r)
    try:
        configured_hold = int(max_hold_bars) if max_hold_bars is not None else None
    except (TypeError, ValueError, OverflowError):
        configured_hold = None
    configured = next((item for item in matrix
                       if configured_target is not None and
                       configured_hold is not None and
                       math.isclose(item["target_r"], configured_target,
                                    rel_tol=0.0, abs_tol=1e-8) and
                       item["max_hold_bars"] == configured_hold), None)
    expiry = [item for item in normalized_rows
              if item["exit_reason"] in {"time", "time_expiry"}]
    unreachable = ([item for item in expiry
                    if configured_target is not None and
                    item["mfe_r"] < configured_target])
    usable = len(normalized_rows)
    mismatch_count = len(unreachable)
    mismatch_rate = mismatch_count / usable if usable else 0.0
    # One deterministic evidence rule: thirty usable paths, at least ten
    # ordinary expiries below the configured target, and a 30% mismatch rate.
    # This avoids turning a handful of censored or ambiguous paths into a
    # mutation signal.
    adequate = usable >= max(TARGET_HOLD_MIN_USABLE, int(min_usable))
    genuine_mismatch = bool(
        adequate and configured_target is not None and configured_hold is not None
        and len(expiry) >= 10 and mismatch_count >= 10 and
        mismatch_rate >= 0.30)

    recommendation: dict[str, Any] | None = None
    status = "underpowered" if not adequate else "no_mismatch"
    if genuine_mismatch:
        # Pick the highest lower target with at least the evidence floor.  It
        # is a deterministic, one-coordinate remediation; the hold remains the
        # configured finite shape because the observed path contains no bars
        # beyond that cap from which a longer hold could be learned.
        lower = [item for item in matrix
                 if item["max_hold_bars"] == configured_hold and
                 item["target_r"] < configured_target and
                 item["eligible"] >= TARGET_HOLD_MIN_USABLE]
        lower = [item for item in lower
                 if (item["reach_rate"] is not None and
                     item["reach_rate"] >= 0.50)]
        if lower:
            chosen = max(lower, key=lambda item: item["target_r"])
            recommendation = {
                "target_r": chosen["target_r"],
                "max_hold_bars": configured_hold,
                "eligible": chosen["eligible"],
                "reach_rate": chosen["reach_rate"],
                "reason": "time_expiry_with_unreachable_configured_target",
                "diagnostic_only": True,
                "authorizing": False,
            }
            status = "recommendation"
        else:
            status = "ambiguous"

    return {
        "schema": TARGET_HOLD_REACHABILITY_SCHEMA,
        "scope": "fit_only",
        "diagnostic_only": True,
        "authorizing": False,
        "configured": {"target_r": configured_target,
                        "max_hold_bars": configured_hold},
        "total": total,
        "usable": usable,
        "censored": censored,
        "unavailable": unavailable,
        "counts": {"total": total, "usable": usable,
                   "censored": censored, "unavailable": unavailable},
        "min_usable": int(min_usable),
        "adequate": adequate,
        "expiry_count": len(expiry),
        "unreachable_count": mismatch_count,
        "unreachable_rate": mismatch_rate,
        "genuine_mismatch": genuine_mismatch,
        "status": status,
        "target_ladder": list(targets),
        "hold_ladder": list(holds),
        "matrix": matrix,
        "recommendation": recommendation,
    }


def render_path_telemetry_json(value: Mapping[str, Any]) -> str:
    """Serialize telemetry with stable compact JSON for artifact/report use."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def render_path_telemetry_svg(value: Mapping[str, Any]) -> str:
    """Render a dependency-free compact SVG summary of grouped MFE/MAE."""
    groups = value.get("groups") if isinstance(value, Mapping) else ()
    groups = groups if isinstance(groups, Sequence) and not isinstance(groups, (str, bytes)) else ()
    width, row_height, left = 640, 22, 210
    height = 30 + row_height * len(groups)
    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             '<title>Path excursion telemetry</title>',
             '<rect width="100%" height="100%" fill="white"/>']
    for index, item in enumerate(groups):
        y = 20 + index * row_height
        label = f"{item.get('target_r', '—')}R/{item.get('max_hold_bars', '—')}b/{item.get('exit_reason', 'unknown')}"
        label = (label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        mfe = _number((item.get("mfe_bps") or {}).get("median")) if isinstance(item.get("mfe_bps"), Mapping) else None
        mae = _number((item.get("mae_bps") or {}).get("median")) if isinstance(item.get("mae_bps"), Mapping) else None
        lines.append(f'<text x="8" y="{y}" font-size="11">{label}</text>')
        lines.append(f'<text x="{left}" y="{y}" font-size="11">MFE {mfe if mfe is not None else "—"} bps | MAE {mae if mae is not None else "—"} bps | n={item.get("count", 0)}</text>')
    lines.append("</svg>")
    return "".join(lines)


# Descriptive compatibility spellings for callers using builder/measure names.
measure_path_telemetry = compute_path_telemetry
path_telemetry = compute_path_telemetry
build_path_telemetry = compute_path_telemetry
summarize_path_telemetry = aggregate_path_telemetry
summarize_target_hold_reachability = target_hold_reachability
target_hold_geometry = target_hold_reachability


__all__ = [
    "PATH_AGGREGATE_SCHEMA", "PATH_TELEMETRY_SCHEMA",
    "TARGET_HOLD_HOLD_LADDER", "TARGET_HOLD_MIN_USABLE",
    "TARGET_HOLD_REACHABILITY_SCHEMA", "TARGET_HOLD_TARGET_LADDER",
    "aggregate_path_telemetry", "build_path_telemetry", "compute_path_telemetry",
    "measure_path_telemetry", "path_telemetry", "render_path_telemetry_json",
    "render_path_telemetry_svg", "summarize_path_telemetry",
    "summarize_target_hold_reachability", "target_hold_geometry",
    "target_hold_reachability",
]
