"""Compact, fit-only diagnostics for the bounded rule factory.

The functions in this module are observational.  They never authorize a
candidate, alter an exit, or inspect held-out/sealed rows.  Their purpose is
to make sparse signal prefixes, execution economics, and behaviorally
identical parameter sets visible before the expensive full replay.
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
    MIN_STOP_DISTANCE_BPS, evaluate_rule_signal_metadata, feature_window_bars,
    rule_variant_id, validate_rule_spec,
)
from .costs import (STRESSED_COST_BASIS, STRESSED_COST_SCHEMA,
                    stressed_cost_usd)
from .market_data import record_available_at, record_is_available
from .stats import clustered_mde_power_report


FIT_DIAGNOSTICS_SCHEMA = "fit-diagnostics.v1"
COST_STRESS_MULTIPLIERS = (9, 15, 25, 50)
_NY = ZoneInfo("America/New_York")


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


def _planned_vector(metadata: Mapping[str, Any], *, full: bool) -> dict[str, Any]:
    def rounded(value: Any) -> Any:
        number = _number(value)
        if number is None:
            return value
        return round(number, 10)
    vector = {
        "session_date": str(metadata.get("session_date") or ""),
        "symbol": str(metadata.get("symbol") or "").upper(),
        "direction": metadata.get("direction"),
        "signal_timestamp": metadata.get("signal_timestamp"),
        "entry_price": rounded(metadata.get("entry_price")),
    }
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


def _fit_prefixes(bars: Sequence[Any], spec: Mapping[str, Any]) -> dict[str, Any]:
    """Collect only first-signal metadata from the fit bars."""
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
    for (day, symbol), rows in sorted(grouped.items()):
        if not _session_bars_valid(rows) or not _session_metadata_valid(rows):
            total_prefixes += max(0, len(rows) - 2)
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
                continue
            signal_end = _bar_end(rows[index])
            if signal_end is None:
                continue
            feature_start = (0 if feature_window is None else
                             max(0, index + 1 - int(feature_window)))
            feature_rows = rows[feature_start:index + 1]
            available = [record_available_at(item)
                         for item in feature_rows
                         if record_available_at(item) is not None]
            if len(available) != len(feature_rows):
                continue
            decision_timestamp = max([signal_end, *available])
            if not _contiguous(rows, feature_start, index + 1):
                continue
            entry = rows[index + 1]
            if _timestamp(entry) != signal_end and decision_timestamp <= signal_end:
                continue
            entry_at = signal_end if decision_timestamp <= signal_end else decision_timestamp
            entry_index = next((probe for probe in range(index + 1, len(rows))
                                if (_timestamp(rows[probe]) is not None and
                                    _timestamp(rows[probe]) >= entry_at)), None)
            if entry_index is None:
                continue
            entry = rows[entry_index]
            eligible_prefixes += 1
            session_had_eligible_prefix = True
            metadata = evaluate_rule_signal_metadata(rows[:index + 1], spec)
            if metadata is not None and first is None:
                first = {**metadata, "session_date": day, "symbol": symbol,
                         "decision_timestamp": decision_timestamp.isoformat(),
                         "entry_timestamp": entry_at.isoformat(),
                         # Fit probes carry no executable quote index.  Keep
                         # the planned signal/economics, but make the pricing
                         # requirement explicit instead of reading delayed
                         # entry-bar OHLC as a fabricated fill.
                         "entry_pricing": ("bar" if
                                            record_is_available(
                                                entry, _timestamp(entry))
                                            else "quote_required"),
                         "entry_bar_available": record_is_available(
                             entry, _timestamp(entry))}
        if first is not None:
            first_signals.append(first)
        if session_had_eligible_prefix:
            eligible_sessions += 1
    return {"total_prefixes": total_prefixes,
            "eligible_prefixes": eligible_prefixes,
            "eligible_sessions": eligible_sessions,
            "first_signals": first_signals,
            "needed_prefix_bars": int(needed)}


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
    delivered: list[float] = []
    paired: list[float] = []
    for row in rows:
        if row.get("no_trade") is True:
            continue
        planned = _number(row.get("risk_budget", row.get(
            "intended_risk_usd", row.get("intended_risk"))))
        actual = _number(row.get("risk_usd", row.get(
            "delivered_risk_usd", row.get("delivered_risk"))))
        if planned is not None and planned >= 0:
            intended.append(planned)
        if actual is not None and actual >= 0:
            delivered.append(actual)
        if (planned is not None and planned > 0 and actual is not None
                and actual >= 0):
            paired.append(actual / planned)
    result = {"intended": _ratio_summary(intended, unit="risk_usd"),
              "delivered": _ratio_summary(delivered, unit="risk_usd")}
    if intended and delivered:
        result["delivered_to_intended"] = _ratio_summary(paired, unit="ratio")
    else:
        result["delivered_to_intended"] = _ratio_summary([], unit="ratio")
    return result


def _exit_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    trades = [row for row in rows if row.get("no_trade") is not True]
    reasons = Counter(str(row.get("exit_reason") or "unknown") for row in trades)
    ties = sum(_flag(row.get("tie_broken")) for row in trades)
    entry_gaps = sum(_flag(row.get("entry_gap_fill")) for row in trades)
    exit_gaps = sum(_flag(row.get("exit_gap_fill")) for row in trades)
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
    reasons = Counter(str(row.get("reject_reason") or "unknown")
                      for row in no_trade)
    explicit = sum(1 for row in no_trade
                   if str(row.get("reject_reason") or "").strip())
    executed = len(rows) - len(no_trade)
    blocked = bool(no_trade) and executed == 0 and explicit == len(no_trade)
    result = {
        "rows": len(rows),
        "executed_rows": executed,
        "no_trade_rows": len(no_trade),
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
        config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Measure compact fit-only behavior and execution summaries.

    ``bars`` and ``account_rows`` must already be the fit/development slice.
    This function intentionally has no partitioning or sealed-window access;
    callers own that boundary.
    """
    normalized = validate_rule_spec(spec)
    prefix = _fit_prefixes(list(bars), normalized)
    signals = prefix["first_signals"]
    entry_vectors = [_planned_vector(item, full=False) for item in signals]
    full_vectors = [_planned_vector(item, full=True) for item in signals]
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
    provenance = _provenance_summary(bars)
    if rows:
        provenance["fills"] = _provenance_summary(rows, fill_rows=True)
    pricing = _entry_pricing_summary(signals)
    execution_rejections = _execution_rejection_summary(rows)
    configured_stress = (stressed.get(str(int(stress_scenario)), {})
                         if stress_scenario is not None else {})
    configured_status = configured_stress.get(
        "row_status", {"pass": 0, "fail": 0, "unknown": len(rows)})
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
        },
        "first_signal": {
            "signals": len(signals),
            "eligible_sessions": int(prefix["eligible_sessions"]),
            "rate": (len(signals) / int(prefix["eligible_sessions"])
                     if prefix["eligible_sessions"] else 0.0),
            "session_rate": (len(signals) / int(prefix["eligible_sessions"])
                              if prefix["eligible_sessions"] else 0.0),
            "prefix_rate": (len(signals) / eligible if eligible else 0.0),
            "session_count": len({_session(row) for row in bars if _session(row)}),
        },
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
        "pricing": {"entry": dict(pricing), "diagnostic_only": True},
        "risk_controls": {
            "stressed_cost_scenario_bps": stress_scenario,
            "max_stressed_cost_to_risk_ratio": stress_limit,
            "scenario_bps": stress_scenario,
            "limit": stress_limit,
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
        "exit_grammar": audit_exit_grammar(normalized),
        "mde_power": _mde(rows),
        "behavior_fingerprint": {
            "entry": _digest(entry_vectors),
            "full": _digest(full_vectors),
            "entry_alias_key": _digest(entry_vectors),
            "full_alias_key": _digest(full_vectors),
            "signal_count": len(signals),
            "planned_vector_count": len(full_vectors),
        },
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
        records: Sequence[Any], *, diagnostics: Mapping[str, Mapping[str, Any]] | None = None
        ) -> dict[str, Any]:
    """Return deterministic full-behavior aliases and a kept candidate list.

    A zero-signal fingerprint is never collapsed.  For a non-empty full
    fingerprint, the canonical member is deterministic-source first, then the
    lexicographically smallest variant id.  Alias groups are diagnostic-only
    proposals: every intended member remains in ``kept`` for full replay and
    multiple-testing correction.
    """
    diagnostics = diagnostics or {}
    normalized: list[dict[str, Any]] = []
    for item in records:
        spec, variant_id, source = _record_spec(item)
        diagnostic = diagnostics.get(variant_id)
        if diagnostic is None and isinstance(item, Mapping):
            diagnostic = item.get("fit_diagnostics")
        diagnostic = diagnostic if isinstance(diagnostic, Mapping) else {}
        fingerprint = diagnostic.get("behavior_fingerprint") or {}
        normalized.append({"record": item, "spec": spec, "variant_id": variant_id,
                           "source": source, "diagnostic": diagnostic,
                           "entry": str(fingerprint.get("entry") or ""),
                           "full": str(fingerprint.get("full") or ""),
                           "signal_count": int(fingerprint.get("signal_count") or 0)})
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in normalized:
        if item["signal_count"] > 0 and item["full"]:
            groups.setdefault((item["full"], item["entry"]), []).append(item)
    # Measurement-first policy: aliases are proposed for review, never removed
    # from the intended replay/BH family in this stage.  Keeping this separate
    # from ``kept`` makes the no-mutation invariant explicit to callers.
    kept_ids = {item["variant_id"] for item in normalized}
    proposed_exclusions: list[dict[str, Any]] = []
    full_aliases: list[dict[str, Any]] = []
    parameter_collapse: list[dict[str, Any]] = []
    for (full, entry), members in sorted(groups.items()):
        if len(members) < 2:
            continue
        canonical = sorted(members, key=lambda item: (
            0 if item["source"] in {"deterministic", "carried_forward"} else 1,
            item["variant_id"]))[0]
        aliases = [item for item in members if item is not canonical]
        for item in aliases:
            proposed_exclusions.append({
                "variant_id": item["variant_id"],
                "canonical_variant_id": canonical["variant_id"],
                "entry_fingerprint": entry,
                "full_fingerprint": full,
                "reason": "fit_behavioral_alias_proposed",
            })
        full_aliases.append({"entry_fingerprint": entry,
                             "full_fingerprint": full,
                             "canonical_variant_id": canonical["variant_id"],
                             "variant_ids": sorted(item["variant_id"] for item in members),
                             "signal_count": max(item["signal_count"] for item in members)})
        fields = sorted({key for item in members for key in set(item["spec"])
                         if any(item["spec"].get(key) != other["spec"].get(key)
                                for other in members)})
        parameter_collapse.append({"canonical_variant_id": canonical["variant_id"],
                                   "variant_ids": sorted(item["variant_id"] for item in members),
                                   "fields": fields})
    kept = [item["record"] for item in normalized if item["variant_id"] in kept_ids]
    entry_groups: dict[str, list[str]] = {}
    for item in normalized:
        if item["signal_count"] > 0 and item["entry"]:
            entry_groups.setdefault(item["entry"], []).append(item["variant_id"])
    entry_aliases = [{"entry_fingerprint": key, "variant_ids": sorted(value)}
                     for key, value in sorted(entry_groups.items()) if len(value) > 1]
    return {"kept": kept, "excluded": [],
            "proposed_exclusions": proposed_exclusions,
            "entry_aliases": entry_aliases, "full_aliases": full_aliases,
            "parameter_collapse": parameter_collapse,
            "signals_present": any(item["signal_count"] > 0 for item in normalized),
            "dedup_status": "diagnostic_only",
            "requires_operator_review": bool(proposed_exclusions),
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
    "COST_STRESS_MULTIPLIERS", "FIT_DIAGNOSTICS_SCHEMA",
    "audit_exit_grammar", "build_fit_diagnostics", "collapse_aliases",
    "collapse_behavior_aliases", "diagnose_fit", "filter_behavior_aliases",
    "fit_behavior_diagnostics", "fit_behavior_fingerprint",
    "fit_only_diagnostics", "measure_fit_diagnostics",
]
