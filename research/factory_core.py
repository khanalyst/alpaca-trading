"""Deterministic, behavior-preserving strategy-factory primitives.

This module contains the bounded hypothesis catalog, deterministic simulation,
diagnostics, and mutation/replacement logic used by the strategy-factory
orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import math
from statistics import mean
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from agent.contracts.rule import (
    CROSS_SECTIONAL_BENCHMARK, MIN_STOP_DISTANCE_BPS, RULE_FAMILIES,
    RULE_SCHEMA_V2, RULE_SCHEMA_V3, RULE_SCHEMA_V4, SESSION_MINUTES,
    V2_DEFAULT_EXTENSIONS, V3_DEFAULT_EXTENSIONS, V4_DEFAULT_EXTENSIONS,
    canonical_exit_reason, completed_bar_exit_transition, evaluate_rule_signal,
    evaluate_rule_signal_trace, frozen_target_reference,
    exit_deadline, feature_window_bars, thesis_exit_deadline,
    rule_semantic_signature,
    initialize_exit_state, rule_behavior_identity, rule_variant_id,
    rule_vehicle_executable,
    validate_rule_spec,
)
from agent.contracts.risk_geometry import (
    RiskGeometryError, effective_stop_distance, equity_price_increment,
    quantize_equity_bracket,
)
from .costs import (BAR, QUOTE, RESTING_BRACKET,
                    DIAGNOSTIC_BAR_FALLBACK,
                    DIAGNOSTIC_HISTORICAL_BACKFILL,
                    RESTING_BRACKET_FILL_SCHEMA, STRESSED_COST_BASIS,
                    STRESSED_COST_SCHEMA, CostError, CostModel, ReplayPolicy,
                    check_entry_slippage, check_stressed_cost_plan,
                    index_quotes, quote_fill_record, resting_bracket_fill_claim,
                    stressed_cost_usd,
                    replay_policy_for_bars)
from .edge_ledger import content_hash
from .factory_ledger import FactoryError
from .gates import max_drawdown_of
from .market_data import (OptionSnapshot, QuoteSnapshot, UnderlyingBar,
                          historical_backfill_record, option_has_liquidity,
                          replay_available_at, replay_open_is_available,
                          replay_record_is_available)
from .maturity import causal_maturity_bars
from .path_telemetry import compute_path_telemetry


# `risk.max_position_notional_pct` in the checked runtime config.  Research
# caps on the same percentage of the account, anchored to the same price.
NOTIONAL_CAP_PCT = 25.0

# The recorder samples on a 60s cycle (`deploy/recorder.py --interval`), so a
# snapshot within one cycle of the fill instant is the best quote that existed
# and is priced as executable.  Past that the quote is an approximation: it is
# still used, but charged the modelled half-spread instead of being trusted as
# a fill.  Beyond five cycles it is not a fill price at all and the observation
# is rejected explicitly rather than priced off a quote from another regime.
# Runtime execution rejects market data older than one recorder cycle.  These
# constants are retained as named provenance for callers that persist replay
# assumptions; an explicit ReplayPolicy may tighten them further.
FRESH_OPTION_QUOTE_SECONDS = 30.0
MAX_OPTION_QUOTE_STALENESS_SECONDS = 30.0

# The default catalog covers every bounded family, including the contextual
# SPY-relative rule appended at slot twelve.
DEFAULT_STRATEGIES = 12
DEFAULT_VARIANTS = 4
MAX_STRATEGIES = len(RULE_FAMILIES)
MAX_VARIANTS = 8


@dataclass(frozen=True)
class StrategyHypothesis:
    hypothesis_id: str
    slot: int
    generation: int
    vehicle: str
    family: str
    thesis: str
    falsification: str
    rule_spec: dict[str, Any]
    parent_hypothesis_id: str | None = None
    not_before: str | None = None


def _hypothesis_id(vehicle: str, slot: int, generation: int,
                   spec: Mapping[str, Any]) -> str:
    return f"hyp.{vehicle}.{slot:02d}.{generation:02d}.{content_hash(spec)[:12]}"


def _thesis(spec: Mapping[str, Any]) -> str:
    family = str(spec["family"]).replace("_", " ")
    confirmation = str(spec.get("confirmation", "none")).replace("_", " ")
    suffix = "" if confirmation == "none" else f" confirmed by {confirmation}"
    mechanisms = {
        "opening_range_breakout": (
            "overnight and opening-auction inventory imbalance persists after "
            "price escapes the initial price-discovery range"),
        "opening_range_fade": (
            "an opening liquidity overshoot reverses after the initial auction "
            "imbalance is exhausted"),
        "momentum_continuation": (
            "slow institutional execution causes an initial intraday displacement "
            "to continue rather than clear immediately"),
        "mean_reversion": (
            "a short-lived liquidity shock mean-reverts when it is not supported "
            "by persistent directional flow"),
        "trend_pullback": (
            "persistent order flow survives temporary profit-taking near the "
            "fast trend and resumes in the prevailing direction"),
        "volatility_breakout": (
            "compressed trading stores latent orders whose release creates "
            "directional follow-through"),
        "volume_breakout": (
            "price displacement backed by abnormal participation reflects "
            "information-bearing flow rather than thin-market noise"),
        "vwap_reversion": (
            "temporary price impact away from the session's volume-weighted fair "
            "value decays as liquidity replenishes"),
        "vwap_trend": (
            "an advancing volume-weighted fair value reveals persistent "
            "institutional accumulation or distribution"),
        "range_expansion": (
            "an abnormal realized-range expansion marks a volatility-regime "
            "transition with short-horizon directional persistence"),
        "opening_drive": (
            "a one-sided opening auction establishes a directional inventory "
            "transfer that continues after the opening window"),
        "cross_sectional_residual": (
            "an eligible equity ETF's synchronized short-horizon return "
            "relative to SPY identifies directional relative momentum; this "
            "single-leg signal is not a beta-neutral or hedged residual"),
    }
    mechanism = mechanisms.get(str(spec["family"]),
                               "the specified completed-bar condition captures persistent flow")
    return (f"{family.title()}{suffix} tests whether {mechanism}, producing "
            "positive expectancy after executable costs.")


def _falsification(spec: Mapping[str, Any]) -> str:
    return (
        f"Reject the {str(spec['family']).replace('_', ' ')} mechanism if its "
        "fit-only conditional forward returns do not beat deterministic "
        "same-symbol/session random entries across useful horizons, or if "
        "held-out expectancy fails executable costs and multiple-test correction."
    )


# One template per family, in ``RULE_FAMILIES`` order.  Initial seeding and
# every later reseed start a fresh family from its own template rather than
# from an exhausted lineage's tuned parameters.
FAMILY_TEMPLATES: tuple[dict[str, Any], ...] = (
    {"family": "opening_range_breakout", "range_minutes": 15,
     "threshold_bps": 5.0, "confirmation": "volume"},
    {"family": "opening_range_fade", "range_minutes": 20,
     "threshold_bps": 8.0, "target_r": 1.5, "confirmation": "none"},
    {"family": "momentum_continuation", "lookback": 12,
     "threshold_bps": 18.0, "confirmation": "volume"},
    {"family": "mean_reversion", "lookback": 20,
     "zscore": 1.5, "target_r": 1.5, "confirmation": "volatility"},
    {"family": "trend_pullback", "lookback": 10,
     "slow_lookback": 35, "threshold_bps": 15.0, "confirmation": "trend"},
    {"family": "volatility_breakout", "lookback": 12,
     "compression_bps": 55.0, "threshold_bps": 5.0, "confirmation": "volume"},
    {"family": "volume_breakout", "lookback": 15,
     "volume_multiplier": 1.5, "threshold_bps": 5.0, "confirmation": "trend"},
    {"family": "vwap_reversion", "lookback": 20,
     "threshold_bps": 25.0, "target_r": 1.5, "confirmation": "none"},
    {"family": "vwap_trend", "lookback": 15,
     "threshold_bps": 8.0, "confirmation": "volume"},
    {"family": "range_expansion", "lookback": 20,
     "volume_multiplier": 2.0, "threshold_bps": 5.0, "confirmation": "none"},
    {"family": "opening_drive", "range_minutes": 30,
     "threshold_bps": 30.0, "confirmation": "volume"},
    {"family": "cross_sectional_residual", "lookback": 15,
     "threshold_bps": 12.0, "confirmation": "none"},
)

# Exit search axes are grouped by mechanism without changing the neutral root
# specification or multiplying another discovery dimension.  Reversion
# families reach fair-value targets and shorter holds first; breakout/trend
# families reach wider targets, longer holds, and trailing ratchets first.
REVERSION_FAMILIES = frozenset({
    "opening_range_fade", "mean_reversion", "vwap_reversion",
})
TREND_BREAKOUT_FAMILIES = frozenset(
    family for family in RULE_FAMILIES if family not in REVERSION_FAMILIES)


def family_template(family: str) -> dict[str, Any]:
    for template in FAMILY_TEMPLATES:
        if template["family"] == family:
            # The raw catalog remains v1 so its historical content-addressed
            # IDs stay readable.  This family template is v2's documented
            # no-op extension form; ``template_hypothesis`` promotes new
            # equity factory roots to v4 without rewriting persisted v1-v3
            # specifications.
            return {**dict(template), "schema": RULE_SCHEMA_V2,
                    **V2_DEFAULT_EXTENSIONS}
    raise FactoryError(f"unknown rule family: {family!r}")


def template_hypothesis(slot: int, *, vehicle: str = "equity",
                        generation: int = 0) -> StrategyHypothesis:
    """Return the generation-zero hypothesis a slot starts from."""
    if not 0 <= int(slot) < MAX_STRATEGIES:
        raise FactoryError(f"slot must be between 0 and {MAX_STRATEGIES - 1}")
    # The raw catalog remains available above for legacy v1/v2
    # content-addressed IDs. New equity roots use v4's neutral no-op form so
    # their identity remains stable; deterministic family-aware exit
    # hypotheses are scheduled by the coordinate pool instead. Option roots
    # stay on executable v2.
    authored = family_template(FAMILY_TEMPLATES[int(slot)]["family"])
    if str(vehicle) == "equity":
        # Equity is the only vehicle with a parity-safe runtime amendment path.
        # The ordinary coordinate neighborhood can deterministically activate
        # every bounded exit policy and v3 breakeven semantics one field at a
        # time without changing the root's executable behavior.
        authored.update({"schema": RULE_SCHEMA_V4,
                         **V3_DEFAULT_EXTENSIONS,
                         **V4_DEFAULT_EXTENSIONS})
    spec = validate_rule_spec(authored)
    return StrategyHypothesis(
        _hypothesis_id(vehicle, int(slot), int(generation), spec), int(slot),
        int(generation), vehicle, spec["family"], _thesis(spec),
        _falsification(spec), spec, None, None,
    )


def initial_hypotheses(count: int = DEFAULT_STRATEGIES, *,
                       vehicle: str = "equity") -> list[StrategyHypothesis]:
    if not 1 <= int(count) <= MAX_STRATEGIES:
        raise FactoryError(f"strategies must be between 1 and {MAX_STRATEGIES}")
    return [template_hypothesis(slot, vehicle=vehicle)
            for slot in range(int(count))]


def _session(row: UnderlyingBar) -> str:
    return row.timestamp.astimezone(ZoneInfo("America/New_York")).date().isoformat()


def _available(row: Any, policy: ReplayPolicy) -> datetime | None:
    return replay_available_at(
        row,
        allow_historical_backfill_diagnostics=(
            policy.allow_historical_backfill_diagnostics),
    )


def _visible(row: Any, cutoff: datetime,
             policy: ReplayPolicy | None = None) -> bool:
    resolved = ReplayPolicy() if policy is None else policy
    return replay_record_is_available(
        row, cutoff,
        allow_historical_backfill_diagnostics=(
            resolved.allow_historical_backfill_diagnostics),
    )


def _open_visible(row: Any, cutoff: datetime,
                  policy: ReplayPolicy | None = None) -> bool:
    resolved = ReplayPolicy() if policy is None else policy
    return replay_open_is_available(
        row, cutoff,
        allow_historical_backfill_diagnostics=(
            resolved.allow_historical_backfill_diagnostics),
    )


def _option_at(snapshots: Sequence[OptionSnapshot], *, symbol: str, day: date,
               direction: str, cutoff: datetime, contract_symbol: str | None = None,
               policy: ReplayPolicy | Mapping[str, Any] | None = None) -> OptionSnapshot | None:
    right = "call" if direction == "long" else "put"
    # A pinned exit lookup always has at least the entry snapshot available, so
    # without this bound the "no quote" branch is unreachable and a contract
    # that stopped being quoted at 10:05 silently prices a 15:30 exit off its
    # last morning bid.  Bounding staleness turns that fabrication back into a
    # visible rejection.
    resolved_policy = (ReplayPolicy() if policy is None else
                       (ReplayPolicy.from_config(policy)
                        if isinstance(policy, Mapping) else policy))
    max_age = resolved_policy.max_market_data_age_seconds
    floor = cutoff.timestamp() - max_age
    latest = sorted(snapshots, key=lambda item: (item.timestamp, item.contract.symbol))
    eligible = [snap for snap in snapshots
                if snap.contract.underlying.upper() == symbol.upper()
                and str(snap.contract.feed).lower() == "opra"
                and str(snap.identity.feed).lower() == "opra"
                and snap.session_date == day and snap.timestamp <= cutoff
                and snap.timestamp.timestamp() >= floor
                and _visible(snap, cutoff, resolved_policy)
                and snap.bid > 0 and snap.ask > 0
                # Runtime rejects 0DTE even when a configured lower bound is
                # zero; research must preserve that hard executable boundary.
                and (snap.contract.expiration - day).days >= max(
                    1, resolved_policy.options_min_dte)
                and (snap.contract.expiration - day).days <= resolved_policy.options_max_dte
                and ((snap.ask - snap.bid) / ((snap.ask + snap.bid) / 2.0) * 100.0
                     <= resolved_policy.options_max_spread_pct)
                and option_has_liquidity(snap)
                and (contract_symbol is None
                     or snap.contract.symbol == contract_symbol)
                and (contract_symbol is not None or snap.contract.right.lower() == right)]
    if not eligible:
        return None
    if contract_symbol is not None:
        return max(eligible, key=lambda item: (
            item.timestamp, item.identity.as_of, item.bid, -item.ask))
    # Snapshot input may be a set/dict-derived sequence.  Never use sequence
    # order as a proxy for recency when choosing moneyness.
    latest_spot = next((item.underlying_price for item in reversed(latest)
                        if item in eligible and item.underlying_price), None)
    spot = latest_spot
    return min(eligible, key=lambda item: (
        abs(item.contract.strike - (spot or item.contract.strike)),
        (item.ask - item.bid) / item.ask, -item.timestamp.timestamp(),
        item.contract.symbol))


def _option_liquid(snapshot: OptionSnapshot) -> bool:
    """Backward-compatible private alias for the shared runtime rule."""
    return option_has_liquidity(snapshot)


def _historical_evidence(record: object | None) -> bool:
    """Recognize normalized records and retained executable quote fills."""
    return bool(record is not None and (
        historical_backfill_record(record) or
        str(getattr(record, "source_mode", "") or "").strip().lower() ==
        "historical_backfill"))


def _unpriced(signal_bar: UnderlyingBar, entry_bar: UnderlyingBar, day: date,
              direction: str, reason: str, *, contract: str | None = None,
              decision_timestamp: datetime | None = None,
              entry_timestamp: datetime | None = None,
              stage: str = "pricing",
              signal_opportunity: bool = True,
              detail: Mapping[str, Any] | None = None,
              telemetry: Mapping[str, Any] | None = None) -> dict:
    """Mark a real signal that has no honest fill price.

    Returning ``None`` here would make the observation indistinguishable from a
    session that never signalled, which deletes exactly the trades whose
    contract stopped being quoted — the least random subset there is.
    """
    result = {
        "unpriced_reason": reason, "direction": direction,
        "contract": contract, "execution_disposition": "refused",
        "signal_opportunity": bool(signal_opportunity),
        "reject_stage": str(stage), "reject_detail": dict(detail or {}),
        "session_date": day.isoformat(),
        "signal_bar_feed": signal_bar.feed,
        "signal_bar_provider": signal_bar.provider,
        "entry_bar_feed": entry_bar.feed,
        "entry_bar_provider": entry_bar.provider,
        "signal_timestamp": signal_bar.end.isoformat(),
        "decision_timestamp": (
            (decision_timestamp or signal_bar.end).isoformat()),
        "entry_timestamp": (
            (entry_timestamp or entry_bar.timestamp).isoformat()),
    }
    if telemetry:
        result.update(dict(telemetry))
    return result


def _no_signal(session_bars: Sequence[UnderlyingBar], *,
               reason: str = "rule_not_triggered",
               prefix_status: str | None = None,
               detail: Mapping[str, Any] | None = None) -> dict:
    """Return the explicit terminal disposition for a valid zero-signal day."""
    first = session_bars[0]
    result = {
        "execution_disposition": "no_signal",
        "signal_opportunity": False,
        "no_signal_reason": str(reason),
        "session_date": first.session_date.isoformat(),
        "signal_bar_feed": first.feed,
        "signal_bar_provider": first.provider,
    }
    if prefix_status is not None:
        result["prefix_status"] = str(prefix_status)
    if detail is not None:
        result["prefix_detail"] = dict(detail)
    return result


def _coerce_policy(policy: ReplayPolicy | Mapping[str, Any] | None) -> ReplayPolicy:
    if policy is None:
        # Omitted policy is the checked runtime policy, not a permissive
        # historical-fixture mode.  Tests/diagnostics that need bar fallback
        # must opt out explicitly with ReplayPolicy(strict_market_data=False).
        return ReplayPolicy()
    return ReplayPolicy.from_config(policy) if isinstance(policy, Mapping) else policy


def _session_bars_valid(rows: Sequence[UnderlyingBar]) -> bool:
    """Reject malformed replay streams without repairing them by sorting.

    Adjacency is deliberately *not* required across the whole session.  A
    recorder that misses one low-volume minute at 15:40 has not invalidated a
    signal computed at 10:05, and rejecting the session for it discards a
    quarter of the sample on real data.  :func:`_contiguous` instead enforces
    adjacency over exactly the bars each signal and each hold actually reads.
    """
    if not rows:
        return False
    seen: set[datetime] = set()
    previous: datetime | None = None
    for row in rows:
        if row.interval_seconds != 60 or row.timestamp in seen:
            return False
        if previous is not None and row.timestamp <= previous:
            return False
        seen.add(row.timestamp)
        previous = row.timestamp
    return True


def _contiguous(rows: Sequence[UnderlyingBar], start: int, stop: int) -> bool:
    """True when ``rows[start:stop]`` are consecutive one-minute bars."""
    if start < 0 or stop > len(rows) or start >= stop:
        return False
    return all(right.timestamp - left.timestamp == timedelta(minutes=1)
               for left, right in zip(rows[start:stop - 1], rows[start + 1:stop]))


def _immutable_bars_by_symbol(
        value: Mapping[str, Sequence[UnderlyingBar]] | None,
) -> Mapping[str, tuple[UnderlyingBar, ...]]:
    """Freeze caller-owned market context without sorting or repairing it."""
    if not isinstance(value, Mapping):
        return MappingProxyType({})
    frozen: dict[str, tuple[UnderlyingBar, ...]] = {}
    for raw_symbol, raw_rows in value.items():
        symbol = str(raw_symbol).strip().upper()
        if (not symbol or isinstance(raw_rows, (str, bytes)) or
                not isinstance(raw_rows, Sequence) or symbol in frozen):
            if symbol:
                frozen[symbol] = ()
            continue
        frozen[symbol] = tuple(raw_rows)
    return MappingProxyType(frozen)


def _visible_rule_context(
        bars_by_symbol: Mapping[str, Sequence[UnderlyingBar]], *,
        day: date, cutoff: datetime, policy: ReplayPolicy,
) -> Mapping[str, tuple[UnderlyingBar, ...]]:
    """Return the immutable benchmark prefix observable at ``cutoff``."""
    benchmark = bars_by_symbol.get(CROSS_SECTIONAL_BENCHMARK, ())
    visible = tuple(
        row for row in benchmark
        if row.session_date == day and row.timestamp <= cutoff and
        _visible(row, cutoff + timedelta(minutes=1), policy)
    )
    return MappingProxyType({CROSS_SECTIONAL_BENCHMARK: visible})


def _at_or_before_force_flat(timestamp: datetime, policy: ReplayPolicy) -> bool:
    if policy.force_flat_time is None:
        return True
    local = timestamp.astimezone(ZoneInfo("America/New_York"))
    return local.time() < policy.force_flat_time


def _simulate_trade(session_bars: Sequence[UnderlyingBar], spec: Mapping[str, Any],
                    snapshots: Sequence[OptionSnapshot], vehicle: str,
                    quotes: Mapping[str, Sequence[QuoteSnapshot]] | None = None,
                    policy: ReplayPolicy | Mapping[str, Any] | None = None,
                    bars_by_symbol: Mapping[
                        str, Sequence[UnderlyingBar]] | None = None) -> dict | None:
    resolved_policy = _coerce_policy(policy)
    if not session_bars:
        return None
    if not _session_bars_valid(session_bars):
        return _unpriced(
            session_bars[0], session_bars[0], session_bars[0].session_date,
            "unknown", "invalid_session_bars", stage="data_validation",
            signal_opportunity=False)
    spec = validate_rule_spec(spec)
    if not rule_vehicle_executable(spec, vehicle):
        reason = ("cross_sectional_residual is executable only for equity shares"
                  if spec["family"] == "cross_sectional_residual" else
                  f"{spec['schema']} is not executable for options")
        return _unpriced(
            session_bars[0], session_bars[0], session_bars[0].session_date,
            "unknown", reason,
            stage="rule_eligibility", signal_opportunity=False)
    market_context = _immutable_bars_by_symbol(bars_by_symbol)
    try:
        resolved_policy = replay_policy_for_bars(
            resolved_policy, session_bars,
            session_date=(session_bars[0].session_date if session_bars else None))
    except CostError as exc:
        return _unpriced(session_bars[0], session_bars[0],
                         session_bars[0].session_date,
                         "unknown", str(exc), stage="data_validation",
                         signal_opportunity=False)
    metadata = (session_bars[0].session_open, session_bars[0].session_close)
    if metadata[0] is not None and metadata[1] is not None:
        if any(bar.timestamp < metadata[0] or bar.end > metadata[1]
               for bar in session_bars):
            return _unpriced(session_bars[0], session_bars[0],
                             session_bars[0].session_date, "unknown",
                             "bar_outside_exact_session",
                             stage="data_validation", signal_opportunity=False)
    # ``None`` means the family retains a session-open anchor, so its window
    # starts at the session's first bar rather than a trailing offset.
    window = feature_window_bars(spec)
    minimum_prefix = causal_maturity_bars(spec)
    # A signal needs its complete causal prefix plus an entry bar.  Report a
    # short session as missing evidence, never as a tested predicate that did
    # not fire.
    if len(session_bars) <= minimum_prefix:
        return _unpriced(
            session_bars[0], session_bars[-1],
            session_bars[0].session_date, "unknown",
            "insufficient_history", stage="data_validation",
            signal_opportunity=False,
            detail={"prefix_status": "insufficient_history",
                    "available_bars": len(session_bars),
                    "needed_prefix_bars": minimum_prefix,
                    "entry_bar_required": True})
    last_refusal: dict | None = None
    evaluated_prefixes = 0
    gapped_prefixes = 0
    cross_context_refusals = 0
    cross_context_valid_prefixes = 0
    last_cross_reason: str | None = None
    last_cross_metadata: dict[str, Any] = {}
    context_evidence: list[UnderlyingBar] = []
    for index in range(1, len(session_bars) - 1):
        if index + 1 < minimum_prefix:
            continue
        signal_bar = session_bars[index]
        # The bars a signal is computed from must be consecutive: a gap inside
        # the feature window stretches a fixed lookback across an outage and
        # silently evaluates a different statistic than the spec names.
        feature_start = (0 if window is None
                         else max(0, index + 1 - int(window)))
        if not _contiguous(session_bars, feature_start, index + 1):
            gapped_prefixes += 1
            continue
        evaluated_prefixes += 1
        if spec["family"] == "cross_sectional_residual":
            context = _visible_rule_context(
                market_context, day=signal_bar.session_date,
                cutoff=signal_bar.timestamp, policy=resolved_policy)
            context_evidence.extend(
                bar for rows in context.values() for bar in rows)
            trace = evaluate_rule_signal_trace(
                session_bars[:index + 1], spec,
                bars_by_symbol=context, symbol=signal_bar.symbol)
            signal = trace["signal"]
            last_cross_metadata = dict(trace.get("market_context") or {})
            if signal is None:
                reason = str((trace.get("stages") or [{}])[-1].get("reason") or "")
                if reason.startswith(("benchmark_context_", "subject_context_")):
                    cross_context_refusals += 1
                    last_cross_reason = reason
                else:
                    cross_context_valid_prefixes += 1
        else:
            signal = evaluate_rule_signal(session_bars[:index + 1], spec)
        if signal is None:
            continue
        if spec["family"] == "cross_sectional_residual":
            cross_context_valid_prefixes += 1
        entry_bar = session_bars[index + 1]
        available = [_available(item, resolved_policy)
                     for item in session_bars[feature_start:index + 1]
                     if _available(item, resolved_policy) is not None]
        signal_ready = max([signal_bar.end, *available], default=None)
        if signal_ready is None:
            last_refusal = _unpriced(
                signal_bar, entry_bar, signal_bar.session_date,
                str(signal.get("direction") or "unknown"),
                "signal_not_observable", stage="signal_visibility")
            continue
        boundary = signal_bar.end
        entry_at = boundary if signal_ready <= boundary else signal_ready
        # Keep the immediate next-bar contract when the signal was actionable
        # at the boundary.  A delayed signal can legitimately enter on the
        # first full bar after an outage because its decision did not exist at
        # the missing next-bar boundary.
        if entry_bar.timestamp != boundary and signal_ready <= boundary:
            last_refusal = _unpriced(
                signal_bar, entry_bar, signal_bar.session_date,
                str(signal.get("direction") or "unknown"),
                "entry_bar_not_adjacent", stage="entry_causality",
                decision_timestamp=signal_ready,
                entry_timestamp=boundary)
            continue
        entry_index = next((probe for probe in range(index + 1, len(session_bars))
                            if session_bars[probe].timestamp >= entry_at), None)
        if entry_index is None:
            last_refusal = _unpriced(
                signal_bar, entry_bar, signal_bar.session_date,
                str(signal.get("direction") or "unknown"),
                "no_entry_bar_after_signal", stage="entry_causality",
                decision_timestamp=signal_ready,
                entry_timestamp=entry_at)
            continue
        entry_bar = session_bars[entry_index]
        # The completed bar record is not visible until its end, but its open
        # is the boundary observation used for next-bar entry.  Never consume
        # the entry bar's high/low/close before that bar ends; executable entry
        # pricing below still requires a point-in-time quote at this boundary.
        local_entry = entry_at.astimezone(ZoneInfo("America/New_York"))
        if (resolved_policy.latest_entry_time is not None and
                local_entry.time() > resolved_policy.latest_entry_time):
            last_refusal = _unpriced(
                signal_bar, entry_bar, signal_bar.session_date,
                str(signal.get("direction") or "unknown"),
                "past_latest_entry_time", stage="entry_policy",
                decision_timestamp=signal_ready,
                entry_timestamp=entry_at)
            continue
        if not _at_or_before_force_flat(entry_at, resolved_policy):
            last_refusal = _unpriced(
                signal_bar, entry_bar, signal_bar.session_date,
                str(signal.get("direction") or "unknown"),
                "entry_at_or_after_force_flat", stage="entry_policy",
                decision_timestamp=signal_ready,
                entry_timestamp=entry_at)
            continue
        direction = signal["direction"]
        # A completed recorder bar is normally observed at its end, so it
        # cannot authorize the opening print at the entry boundary.  A fresh
        # executable quote is an independent boundary observation and is used
        # as the fill/underlying anchor when available.  Bar fallback remains
        # explicit and therefore requires the bar itself to be visible at its
        # timestamp; delayed OHLC never becomes an entry by hindsight.
        entry_bar_visible = _open_visible(
            entry_bar, entry_bar.timestamp, resolved_policy)
        entry_underlying: float | None = (
            float(entry_bar.open) if entry_bar_visible else None)
        entry_ref: float | None = entry_underlying
        # Preserve the point-in-time signal/bar reference separately from the
        # executable quote.  ``entry_reference`` remains the historical fill
        # field (and therefore keeps existing report contracts); the parity
        # check in ``simulate_account`` compares this boundary reference to
        # the quote before pricing the fill.
        # Slippage is measured from the authored signal reference to the
        # executable quote.  Keep this distinct from ``entry_ref`` so a gap
        # cannot be mistaken for an ordinary bar/quote mark.
        entry_slippage_reference: float | None = None
        entry_source = BAR
        entry_feed = entry_provider = None
        entry_age = 0.0
        entry_snap: OptionSnapshot | None = None
        entry_evidence: object | None = None
        if vehicle == "equity":
            quoted = quote_fill_record(
                quotes, symbol=signal_bar.symbol, at=entry_at,
                side="buy" if direction == "long" else "sell",
                max_age_seconds=resolved_policy.max_market_data_age_seconds,
                session_date=signal_bar.session_date,
                allow_historical_backfill_diagnostics=(
                    resolved_policy.allow_historical_backfill_diagnostics))
            if quoted is not None:
                entry_evidence = quoted
                entry_underlying = quoted.price
                entry_ref, entry_source = quoted.price, QUOTE
                entry_feed, entry_provider = quoted.feed, quoted.provider
                entry_age = max(
                    0.0, (entry_at - quoted.timestamp).total_seconds())
            elif resolved_policy.strict_market_data:
                return _unpriced(signal_bar, entry_bar,
                                 signal_bar.session_date, direction,
                                 "no fresh equity quote at entry",
                                 decision_timestamp=signal_ready,
                                 entry_timestamp=entry_at,
                                 stage="entry_pricing")
            elif not entry_bar_visible:
                last_refusal = _unpriced(
                    signal_bar, entry_bar, signal_bar.session_date, direction,
                    "entry_bar_not_visible", stage="entry_pricing",
                    decision_timestamp=signal_ready,
                    entry_timestamp=entry_at)
                continue
        elif vehicle == "option":
            entry_snap = _option_at(
                snapshots, symbol=signal_bar.symbol,
                day=signal_bar.session_date, direction=direction,
                cutoff=entry_at, policy=resolved_policy)
            if entry_snap is None:
                return _unpriced(signal_bar, entry_bar, signal_bar.session_date,
                                 direction, "no option quote within staleness bound at entry",
                                 decision_timestamp=signal_ready,
                                 entry_timestamp=entry_at,
                                 stage="entry_pricing")
            # OPRA carries the underlying spot used to select the contract.
            # If that point-in-time spot is absent, only a boundary-visible bar
            # may supply the fallback; a delayed bar's open is never consumed.
            if entry_snap.underlying_price and entry_snap.underlying_price > 0:
                entry_underlying = float(entry_snap.underlying_price)
            elif entry_bar_visible:
                entry_underlying = float(entry_bar.open)
            else:
                return _unpriced(signal_bar, entry_bar, signal_bar.session_date,
                                 direction, "entry_bar_not_visible",
                                 decision_timestamp=signal_ready,
                                 entry_timestamp=entry_at,
                                 stage="entry_pricing")
            entry_ref = entry_snap.ask
            entry_evidence = entry_snap
            entry_source = QUOTE
            entry_feed = str(entry_snap.identity.feed)
            entry_provider = str(entry_snap.identity.provider)
            entry_age = max(
                0.0, (entry_at - entry_snap.timestamp).total_seconds())
        if entry_underlying is None or entry_ref is None:
            last_refusal = _unpriced(
                signal_bar, entry_bar, signal_bar.session_date, direction,
                "entry_price_unavailable", stage="entry_pricing",
                decision_timestamp=signal_ready,
                entry_timestamp=entry_at)
            continue
        authored_distance = float(signal["stop_distance"])
        # ``plan_entry`` is the authored signal reference.  The executable
        # quote/bar mark is a separate anchor: runtime sizes and validates the
        # broker bracket against that value, while preserving authored legs.
        plan_entry = float(signal["entry_price"])
        entry_slippage_reference = plan_entry
        authored_stop = float(signal["stop_price"])
        authored_target = float(signal["target_price"])
        stop = authored_stop
        target = authored_target
        distance = authored_distance
        stop_floor_bps = MIN_STOP_DISTANCE_BPS
        stop_floor_binding = False
        stop_geometry_scenario = None
        stop_geometry_activation_reason = "stress_disabled"
        if vehicle == "equity":
            # Quantization is broker-facing geometry.  Anchor it to the same
            # executable quote used by ``_risk_order``; a quote gap may make
            # the authored bracket invalid, in which case fail closed rather
            # than replaying a trade the runtime cannot submit.
            try:
                stop, target, distance = quantize_equity_bracket(
                    entry_ref, stop, target, direction)
            except RiskGeometryError:
                return _unpriced(
                    signal_bar, entry_bar, signal_bar.session_date, direction,
                    "broker_tick_geometry_invalid", stage="risk_geometry",
                    decision_timestamp=signal_ready,
                    entry_timestamp=entry_at)
        stress_geometry_requested = (
            resolved_policy.stressed_cost_scenario_bps is not None or
            resolved_policy.max_stressed_cost_to_risk_ratio is not None or
            resolved_policy.stressed_cost_calibration_enabled)
        if vehicle == "equity" and stress_geometry_requested:
            stop_geometry_scenario, stop_geometry_activation_reason = (
                resolved_policy.resolve_stress_scenario(
                    signal_bar.symbol, entry_at, vehicle="equity"))
            if (stop_geometry_scenario is None or
                    resolved_policy.max_stressed_cost_to_risk_ratio is None):
                return _unpriced(
                    signal_bar, entry_bar, signal_bar.session_date, direction,
                    "stressed_cost_invalid", stage="risk_geometry",
                    decision_timestamp=signal_ready,
                    entry_timestamp=entry_at)
            try:
                broker_normalized_distance = distance
                effective_distance, stop_floor_bps = effective_stop_distance(
                    entry_ref, distance,
                    base_floor_bps=MIN_STOP_DISTANCE_BPS,
                    scenario_bps=stop_geometry_scenario,
                    max_cost_to_risk_ratio=(
                        resolved_policy.max_stressed_cost_to_risk_ratio),
                    minimum_increment=equity_price_increment(entry_ref))
            except RiskGeometryError:
                return _unpriced(
                    signal_bar, entry_bar, signal_bar.session_date, direction,
                    "stressed_cost_invalid", stage="risk_geometry",
                    decision_timestamp=signal_ready,
                    entry_timestamp=entry_at)
            # Stressed geometry is an admission veto.  The floor calculation
            # is intentionally never copied back into ``distance`` or used to
            # rebuild either authored bracket leg: widening a stop changes the
            # strategy's risk and target semantics.
            stop_floor_binding = effective_distance > broker_normalized_distance + 1e-12
            if stop_floor_binding:
                geometry_telemetry = {
                    "authored_stop_distance": authored_distance,
                    "authored_stop_distance_bps": (
                        authored_distance / plan_entry * 10_000.0),
                    "effective_stop_floor_bps": stop_floor_bps,
                    "stress_floor_binding": True,
                    "stop_geometry_scenario_bps": stop_geometry_scenario,
                    "stop_geometry_max_cost_to_risk_ratio": (
                        resolved_policy.max_stressed_cost_to_risk_ratio),
                    "stop_geometry_activation_reason": (
                        stop_geometry_activation_reason),
                }
                return _unpriced(
                    signal_bar, entry_bar, signal_bar.session_date, direction,
                    "stressed_cost_risk_limit", stage="risk_geometry",
                    decision_timestamp=signal_ready,
                    entry_timestamp=entry_at, telemetry=geometry_telemetry)
            if (not math.isfinite(stop) or stop <= 0 or
                    not math.isfinite(target) or target <= 0 or
                    (direction == "long" and not (stop < entry_ref < target)) or
                    (direction == "short" and not (target < entry_ref < stop))):
                return _unpriced(
                    signal_bar, entry_bar, signal_bar.session_date, direction,
                    "stressed_cost_invalid", stage="risk_geometry",
                    decision_timestamp=signal_ready,
                    entry_timestamp=entry_at)
        if vehicle == "equity":
            try:
                stop, target, distance = quantize_equity_bracket(
                    entry_ref, stop, target, direction)
            except RiskGeometryError:
                return _unpriced(
                    signal_bar, entry_bar, signal_bar.session_date, direction,
                    "broker_tick_geometry_invalid", stage="risk_geometry",
                    decision_timestamp=signal_ready,
                    entry_timestamp=entry_at)
        # Sizing reproduces ``RiskEngine.size_shares`` against the executable
        # entry/stop geometry.  A quote gap therefore reduces quantity before
        # the trade is admitted instead of overspending the authored budget.
        real_risk = max(0.0, entry_underlying - stop if direction == "long"
                        else stop - entry_underlying)
        force_flat_ts = None
        if resolved_policy.force_flat_time is not None:
            local_entry = entry_at.astimezone(ZoneInfo("America/New_York"))
            force_flat_ts = local_entry.replace(
                hour=resolved_policy.force_flat_time.hour,
                minute=resolved_policy.force_flat_time.minute,
                second=resolved_policy.force_flat_time.second,
                microsecond=resolved_policy.force_flat_time.microsecond,
            ).timestamp()
        deadline_contract = exit_deadline(
            entry_at, spec, force_flat_ts=force_flat_ts)
        deadline = (None if deadline_contract is None else
                    float(deadline_contract["timestamp"]))
        deadline_reason = (None if deadline_contract is None else
                           str(deadline_contract["reason"]))
        thesis_deadline = thesis_exit_deadline(entry_at, spec)
        last_index = entry_index
        # The existing replay intentionally resolves a hold on the last
        # observed bar when the next bar is non-adjacent.  Keep that P&L path
        # unchanged, but retain why the time-like exit happened so sparse
        # corpora are not mistaken for ordinary hold expiry.
        hold_discontinuity = False
        hold_discontinuity_kind: str | None = None
        hold_discontinuity_from: datetime | None = None
        hold_discontinuity_to: datetime | None = None
        hold_discontinuity_gap_minutes: float | None = None
        hold_discontinuity_gap_seconds: float | None = None
        for probe in range(entry_index + 1, len(session_bars)):
            # The hold never crosses an outage.  Treating the next recorded
            # minute as adjacent would let a stop or target "trigger" on a bar
            # the position could not have been carried into; the position is
            # resolved on the last observed bar instead.
            if (session_bars[probe].timestamp -
                    session_bars[probe - 1].timestamp != timedelta(minutes=1)):
                gap_seconds = (session_bars[probe].timestamp -
                               session_bars[probe - 1].timestamp).total_seconds()
                # A gap wholly after the configured hold/close boundary
                # cannot have changed the hold.  Conversely, the next bar may
                # be beyond the deadline when the missing interval spans the
                # terminal boundary; the previous bar's end below is the
                # relevant causal reference.
                previous_end = session_bars[probe - 1].end
                # The missing interval affects the hold when the last
                # contiguous bar still ended before its deadline.  The next
                # observed bar may itself be beyond that deadline — that is
                # precisely how a gap spanning the terminal boundary is
                # observed.  Use the same strict force-flat boundary for the
                # previous bar's end so a trailing post-close gap is not
                # misclassified as a hold discontinuity.
                gap_affects_hold = (
                    gap_seconds > 60.0 and
                    (deadline is None or previous_end.timestamp() < deadline) and
                    _at_or_before_force_flat(previous_end, resolved_policy))
                if gap_affects_hold:
                    hold_discontinuity = True
                    hold_discontinuity_kind = "internal_gap"
                    hold_discontinuity_from = session_bars[probe - 1].end
                    hold_discontinuity_to = session_bars[probe].timestamp
                    hold_discontinuity_gap_seconds = gap_seconds - 60.0
                    hold_discontinuity_gap_minutes = max(
                        0.0, hold_discontinuity_gap_seconds / 60.0)
                break
            if (deadline is not None and
                    session_bars[probe].end.timestamp() > deadline):
                break
            if not _at_or_before_force_flat(session_bars[probe].timestamp,
                                            resolved_policy):
                break
            last_index = probe
        # If the observed symbol/session simply ends while the configured hold
        # is still live, the terminal bar is right-censored data, not an
        # ordinary time-cap expiry.  There is no following timestamp from
        # which to infer a gap duration, so keep that duration explicitly
        # unknown and distinguish it from an observed internal gap.
        terminal_end = session_bars[last_index].end
        terminal_session_close = session_bars[last_index].session_close
        terminal_is_calendar_close = (
            terminal_session_close is not None and
            terminal_end >= terminal_session_close)
        if (not hold_discontinuity and last_index == len(session_bars) - 1 and
                not terminal_is_calendar_close and
                (deadline is None or terminal_end.timestamp() < deadline) and
                _at_or_before_force_flat(terminal_end, resolved_policy)):
            hold_discontinuity = True
            hold_discontinuity_kind = "observed_data_end"
            hold_discontinuity_from = terminal_end
        exit_bar = session_bars[last_index]
        exit_ref = float(exit_bar.close)
        exit_at = exit_bar.end
        pricing_cutoff = exit_at
        deadline_reached = (not hold_discontinuity and deadline is not None and
                            abs(exit_at.timestamp() - deadline) <= 1e-9)
        canonical_reason = (deadline_reason if deadline_reached else
                            canonical_exit_reason("time"))
        reason = ("exit_before" if canonical_reason == "thesis_deadline"
                  else "time")
        tie = False
        gapped = False
        exit_gapped = False
        exit_state = initialize_exit_state(
            direction, entry_underlying, stop, target,
            breakeven_r=spec.get("breakeven_r"),
            trailing_stop_r=spec.get("trailing_stop_r"),
            target_mode=spec.get("target_mode", "fixed_r"),
            target_lookback=spec.get("target_lookback"),
            exit_before_ts=thesis_deadline)
        # The scan starts at the entry bar: the broker bracket is live from the
        # fill.  The shared transition owns entry-gap, later-gap, stop-wins tie,
        # and completed-close breakeven ordering for replay, null, and runtime.
        for bar in session_bars[entry_index:last_index + 1]:
            if _available(bar, resolved_policy) is None:
                continue
            transition = completed_bar_exit_transition(exit_state, bar)
            exit_state = transition["state"]
            resolved = transition["exit"]
            if resolved is None:
                continue
            reason = str(resolved["reason"])
            canonical_reason = canonical_exit_reason(reason)
            tie = bool(resolved.get("tie_broken"))
            gapped = bool(resolved.get("entry_gap"))
            exit_gapped = bool(resolved.get("gapped")) and not gapped
            exit_ref = float(resolved["price"])
            exit_bar = bar
            exit_at = bar.end
            pricing_cutoff = bar.timestamp
            break
        hold_discontinuity_exit = hold_discontinuity and reason == "time"
        canonical_reason = canonical_exit_reason(
            canonical_reason, discontinuity=hold_discontinuity_exit)
        day = signal_bar.session_date
        multiplier = 1
        contract = None
        exit_source = BAR
        exit_fill_schema = None
        exit_fill_claim = None
        exit_feed = exit_provider = None
        exit_age = 0.0
        exit_evidence: object | None = None
        if vehicle == "equity":
            if reason in {"time", "exit_before"} or gapped or exit_gapped:
                quoted_exit = quote_fill_record(
                    quotes, symbol=signal_bar.symbol, at=pricing_cutoff,
                    side="sell" if direction == "long" else "buy",
                    max_age_seconds=resolved_policy.max_market_data_age_seconds,
                    session_date=day,
                    allow_historical_backfill_diagnostics=(
                        resolved_policy.allow_historical_backfill_diagnostics))
                if quoted_exit is not None:
                    exit_evidence = quoted_exit
                    exit_ref, exit_source = quoted_exit.price, QUOTE
                    exit_feed, exit_provider = quoted_exit.feed, quoted_exit.provider
                    exit_age = max(
                        0.0, (pricing_cutoff - quoted_exit.timestamp).total_seconds())
                elif resolved_policy.strict_market_data:
                    return _unpriced(signal_bar, entry_bar, day, direction,
                                     "no fresh equity quote at exit",
                                     decision_timestamp=signal_ready,
                                     entry_timestamp=entry_at,
                                     stage="exit_pricing")
        # A non-gap stop/target is the broker-resident bracket leg observed by
        # the completed exact-feed bar.  There is no executable quote at the
        # unknown trigger instant, so retain the planned level and let the
        # cost model charge its ordinary adverse spread/slippage.
        if (vehicle == "equity" and reason in {"stop", "target"} and
                not gapped and not exit_gapped):
            exit_source = RESTING_BRACKET
            exit_fill_schema = RESTING_BRACKET_FILL_SCHEMA
            # ``completed_bar_exit_transition`` may amend the protective leg
            # (for example a breakeven stop) before this bar triggers it.  The
            # resting claim is about the active broker leg, not merely the
            # originally authored stop used for sizing.
            active_stop = float(exit_state.get("active_stop_price", stop))
            exit_fill_claim = resting_bracket_fill_claim(
                exit_reason=reason, exit_reference=exit_ref,
                stop_price=active_stop, target_price=target,
                bar_timestamp=exit_bar.timestamp.isoformat(),
                bar_feed=exit_bar.feed, bar_provider=exit_bar.provider,
                tie_broken=tie)
        entry_option_feed = exit_option_feed = None
        if vehicle == "option":
            assert entry_snap is not None
            exit_snap = _option_at(snapshots, symbol=signal_bar.symbol, day=day,
                                   direction=direction,
                                   cutoff=(pricing_cutoff if
                                           resolved_policy.strict_market_data else
                                           exit_bar.end),
                                   contract_symbol=entry_snap.contract.symbol,
                                   policy=resolved_policy)
            if exit_snap is None:
                return _unpriced(signal_bar, entry_bar, day, direction,
                                 "entry contract stopped being quoted before exit",
                                 contract=entry_snap.contract.symbol,
                                 decision_timestamp=signal_ready,
                                 entry_timestamp=entry_at,
                                 stage="exit_pricing")
            exit_evidence = exit_snap
            contract = entry_snap.contract.symbol
            entry_option_feed = str(entry_snap.contract.feed).lower()
            exit_option_feed = str(exit_snap.contract.feed).lower()
            entry_feed = str(entry_snap.identity.feed)
            exit_feed = str(exit_snap.identity.feed)
            entry_provider = str(entry_snap.identity.provider)
            exit_provider = str(exit_snap.identity.provider)
            exit_ref = exit_snap.bid
            exit_source = QUOTE
            multiplier = entry_snap.contract.multiplier
            # A snapshot inside the exit bar but after a level-triggered instant
            # is not stale, it is simply the bar's quote; only genuinely older
            # quotes carry an age.
            exit_age = max(0.0, (pricing_cutoff - exit_snap.timestamp).total_seconds())
        trade_row = {
            "vehicle": vehicle, "symbol": signal_bar.symbol,
            "execution_disposition": "candidate",
            "signal_opportunity": True,
            "session_date": day.isoformat(), "direction": direction,
            "signal_timestamp": signal_bar.end.isoformat(),
            "decision_timestamp": signal_ready.isoformat(),
            "entry_timestamp": entry_at.isoformat(),
            # A gap exit happens at the bar open; an intrabar level exit is
            # conservatively represented at the completed bar cutoff used for
            # its bar-level price.  Never imply a quote after the trigger.
            "exit_timestamp": exit_at.isoformat(),
            # Bar provenance remains available for diagnostic/bar-fallback
            # rows; quote leg provenance below is reserved for the source that
            # actually priced each executable boundary.
            "signal_bar_feed": signal_bar.feed,
            "signal_bar_provider": signal_bar.provider,
            "entry_bar_feed": entry_bar.feed,
            "entry_bar_provider": entry_bar.provider,
            "exit_bar_feed": exit_bar.feed,
            "exit_bar_provider": exit_bar.provider,
            "entry_reference": entry_ref, "exit_reference": exit_ref,
            "entry_slippage_reference": entry_slippage_reference,
            "underlying_entry": entry_underlying, "stop_price": stop,
            "active_stop_price": float(
                exit_state.get("active_stop_price", stop)),
            "target_price": target, "exit_reason": reason, "tie_broken": tie,
            "canonical_exit_reason": canonical_reason,
            "target_r": float(spec.get("target_r")) if spec.get("target_r") is not None else None,
            "rule_schema": spec.get("schema"),
            "target_mode": spec.get("target_mode", "fixed_r"),
            "target_reference": signal.get("target_reference"),
            "target_lookback": spec.get("target_lookback"),
            "trailing_stop_r": spec.get("trailing_stop_r"),
            "exit_before_minutes": spec.get("exit_before_minutes"),
            "exit_before_ts": thesis_deadline,
            "max_hold_bars": int(spec.get("max_hold_bars")) if spec.get("max_hold_bars") is not None else None,
            "deadline_timestamp": (
                datetime.fromtimestamp(float(deadline), timezone.utc).isoformat()
                if deadline is not None else None),
            # ``exit_reason`` is a long-standing consumer field and remains
            # ``time`` for both cases.  These additive fields distinguish a
            # sparse-hold termination from a normal configured expiry without
            # changing the selected bar, fill, or P&L.
            "hold_discontinuity": hold_discontinuity_exit,
            "hold_discontinuity_exit": hold_discontinuity_exit,
            "hold_discontinuity_kind": (
                hold_discontinuity_kind if hold_discontinuity_exit else None),
            "hold_discontinuity_from": (
                hold_discontinuity_from.isoformat()
                if hold_discontinuity_exit and
                hold_discontinuity_from is not None else None),
            "hold_discontinuity_to": (
                hold_discontinuity_to.isoformat()
                if hold_discontinuity_exit and
                hold_discontinuity_to is not None else None),
            "hold_discontinuity_gap_seconds": (
                hold_discontinuity_gap_seconds
                if hold_discontinuity_exit else None),
            "hold_discontinuity_gap_minutes": (
                hold_discontinuity_gap_minutes
                if hold_discontinuity_exit else None),
            "hold_exit_reason": ("discontinuity"
                                 if hold_discontinuity_exit else
                                 "session_force_flat"
                                 if canonical_reason == "session_force_flat" else
                                 "time_expiry" if reason == "time" else
                                 "thesis_deadline" if reason == "exit_before"
                                 else reason),
            "exit_reason_detail": ("discontinuity"
                                   if hold_discontinuity_exit else
                                   "session_force_flat"
                                   if canonical_reason == "session_force_flat" else
                                   "time_expiry" if reason == "time" else
                                   "thesis_deadline" if reason == "exit_before"
                                   else reason),
            "contract": contract, "contract_multiplier": multiplier,
            "stop_distance": distance,
            "authored_stop_price": authored_stop,
            "authored_target_price": authored_target,
            "authored_stop_distance": authored_distance,
            "authored_stop_distance_bps": (
                authored_distance / plan_entry * 10_000.0),
            "effective_stop_floor_bps": stop_floor_bps,
            "stress_floor_binding": stop_floor_binding,
            "stop_geometry_scenario_bps": stop_geometry_scenario,
            "stop_geometry_max_cost_to_risk_ratio": (
                resolved_policy.max_stressed_cost_to_risk_ratio),
            "stop_geometry_activation_reason": (
                stop_geometry_activation_reason),
            "entry_gap_fill": gapped,
            "exit_gap_fill": exit_gapped,
            "entry_fill_source": entry_source, "exit_fill_source": exit_source,
            "exit_fill_schema": exit_fill_schema,
            "exit_fill_claim": exit_fill_claim,
            "exit_fill_bar_timestamp": (
                exit_bar.timestamp.isoformat()
                if exit_source == RESTING_BRACKET else None),
            "entry_quote_age_seconds": entry_age,
            "exit_quote_age_seconds": exit_age,
            "entry_feed": entry_feed,
            "exit_feed": exit_feed,
            "entry_provider": entry_provider,
            "exit_provider": exit_provider,
            "entry_option_feed": entry_option_feed,
            "exit_option_feed": exit_option_feed,
            # The price the runtime plans and caps notional against; the fill
            # reference above may have gapped away from it.
            "plan_entry": plan_entry,
            "authored_entry_reference": plan_entry,
            "executable_entry_reference": entry_ref,
            "risk_per_unit": (entry_ref * multiplier if vehicle == "option"
                              else distance),
            # A long option's maximum loss is the premium actually paid, so its
            # nominal and realized risk are the same number.
            "realized_risk_per_unit": (entry_ref * multiplier
                                       if vehicle == "option" else real_risk),
            "evidence_mode": (
                DIAGNOSTIC_HISTORICAL_BACKFILL
                if any(
                    _historical_evidence(item)
                    for item in (
                        *(bar for bar in session_bars
                          if bar.timestamp <= exit_bar.timestamp),
                        *context_evidence,
                        entry_evidence,
                        exit_evidence,
                    ))
                else "forward_observed"
            ),
        }
        if spec["family"] == "cross_sectional_residual":
            trade_row.update({
                key: signal.get(key) for key in (
                    "benchmark_symbol", "symbol_return", "benchmark_return",
                    "residual_return", "market_context_digest",
                    "candidate_behavior_identity")
            })
        if spec.get("breakeven_r") is not None:
            trade_row.update({
                "breakeven_r": spec["breakeven_r"],
                "initial_stop_price": exit_state["initial_stop_price"],
                "active_stop_price": exit_state["active_stop_price"],
                "breakeven_armed_at": exit_state.get("breakeven_armed_at"),
                "breakeven_armed_epoch": exit_state.get("breakeven_armed_epoch"),
            })
        if spec.get("schema") == RULE_SCHEMA_V4:
            trade_row.update({
                "initial_stop_price": exit_state["initial_stop_price"],
                "active_stop_price": exit_state["active_stop_price"],
                "trailing_stop_r": exit_state.get("trailing_stop_r"),
                "target_mode": exit_state.get("target_mode", "fixed_r"),
                "target_lookback": exit_state.get("target_lookback"),
                "exit_before_ts": exit_state.get("exit_before_ts"),
            })
        trade_row["path_telemetry"] = compute_path_telemetry(
            trade_row, session_bars)
        return trade_row
    if last_refusal is not None:
        return last_refusal
    if evaluated_prefixes:
        reason = (last_cross_reason
                  if (spec["family"] == "cross_sectional_residual" and
                      cross_context_refusals and not cross_context_valid_prefixes)
                  else "rule_not_triggered")
        result = _no_signal(
            session_bars, prefix_status="valid_prefix_no_signal",
            detail={"gapped_prefixes": gapped_prefixes,
                    "evaluated_prefixes": evaluated_prefixes,
                    **({"context_refusals": cross_context_refusals,
                        "context_valid_prefixes": cross_context_valid_prefixes}
                       if spec["family"] == "cross_sectional_residual" else {})},
            reason=reason)
        if spec["family"] == "cross_sectional_residual":
            result.update(last_cross_metadata)
            result.setdefault(
                "candidate_behavior_identity",
                rule_behavior_identity(
                    spec, market_context_digest=result.get(
                        "market_context_digest")))
        return result
    if gapped_prefixes:
        return _unpriced(
            session_bars[0], session_bars[0], session_bars[0].session_date,
            "unknown", "no_contiguous_feature_window",
            stage="data_validation", signal_opportunity=False,
            detail={"prefix_status": "no_contiguous_feature_window",
                    "gapped_prefixes": gapped_prefixes,
                    "evaluated_prefixes": evaluated_prefixes})
    return _no_signal(session_bars)


def _fresh(raw: Mapping[str, Any], leg: str,
           max_age_seconds: float | None = None) -> bool:
    limit = FRESH_OPTION_QUOTE_SECONDS if max_age_seconds is None else float(max_age_seconds)
    return float(raw.get(f"{leg}_quote_age_seconds") or 0.0) <= limit


def _visible_bar_mark_record(
        rows: Sequence[UnderlyingBar], cutoff: datetime,
        policy: ReplayPolicy | None = None,
) -> tuple[float, UnderlyingBar] | None:
    """Return the last observable bar price and its provenance record.

    A bar's open is the boundary observation used for an entry at its
    timestamp (the same convention used by :func:`_simulate_trade`).  A
    partial bar otherwise contributes nothing until its completed record is
    visible, so its close/high/low can never leak into an earlier mark.
    """
    for row in reversed(rows):
        if row.timestamp == cutoff and _open_visible(row, cutoff, policy):
            return float(row.open), row
        if row.end <= cutoff and _visible(row, row.end, policy):
            return float(row.close), row
    return None


def _visible_bar_mark(rows: Sequence[UnderlyingBar], cutoff: datetime,
                      policy: ReplayPolicy | None = None) -> float | None:
    """Backward-compatible price-only view of the selected visible bar."""
    selected = _visible_bar_mark_record(rows, cutoff, policy)
    return None if selected is None else selected[0]


def simulate_account(bars: Sequence[UnderlyingBar], snapshots: Sequence[OptionSnapshot],
                     spec: Mapping[str, Any], *, vehicle: str, account_id: str,
                     starting_cash: float = 100_000.0, risk_pct: float = .5,
                     costs: CostModel | None = None,
                     cost_resolver: Callable[[Mapping[str, Any]], CostModel] | None = None,
                     quotes: Sequence[QuoteSnapshot] | None = None,
                     policy: ReplayPolicy | Mapping[str, Any] | None = None,
                     bars_by_symbol: Mapping[
                         str, Sequence[UnderlyingBar]] | None = None) -> dict:
    """Replay one variant in an event-ordered isolated cash/equity book.

    ``costs`` remains the compatibility/default model.  A diagnostic caller
    may additionally supply ``cost_resolver`` to select a validated model from
    immutable opportunity fields such as symbol, entry timestamp, and sized
    quantity.  The resolver is invoked inside account simulation before the
    stress check and fills; it cannot mutate the authored rule or policy.
    """
    spec = validate_rule_spec(spec)
    base_model = costs or CostModel()

    def resolve_cost_model(context: Mapping[str, Any]) -> CostModel:
        if cost_resolver is None:
            return base_model
        resolved = cost_resolver(context)
        if not isinstance(resolved, CostModel):
            raise CostError("cost_resolver must return a CostModel")
        return resolved
    # Keep the explicit risk_pct argument meaningful for direct callers while
    # still applying the safe ReplayPolicy defaults when no policy is passed.
    resolved_policy = (_coerce_policy(policy) if policy is not None else
                       ReplayPolicy(risk_per_trade_pct=float(risk_pct)))
    quote_index = index_quotes(quotes)
    grouped: dict[tuple[str, date], list[UnderlyingBar]] = {}
    for bar in bars:
        grouped.setdefault((bar.symbol, bar.session_date), []).append(bar)
    rows: list[dict] = []
    candidates: list[dict] = []
    if bars_by_symbol is None:
        derived_context: dict[str, list[UnderlyingBar]] = {}
        for bar in bars:
            derived_context.setdefault(str(bar.symbol).upper(), []).append(bar)
        market_context = _immutable_bars_by_symbol(derived_context)
    else:
        market_context = _immutable_bars_by_symbol(bars_by_symbol)
    for (symbol, day), session_bars in sorted(
            grouped.items(), key=lambda item: (item[0][1], item[0][0])):
        raw = _simulate_trade(session_bars, spec, snapshots, vehicle,
                              quotes=quote_index, policy=resolved_policy,
                              bars_by_symbol=market_context)
        behavior_identity = rule_variant_id(spec)
        if spec["family"] == "cross_sectional_residual":
            raw = dict(raw) if isinstance(raw, Mapping) else raw
            context_digest = (raw.get("market_context_digest")
                              if isinstance(raw, Mapping) else None)
            behavior_identity = rule_behavior_identity(
                spec, market_context_digest=context_digest)
            if isinstance(raw, dict):
                raw.setdefault("benchmark_symbol", CROSS_SECTIONAL_BENCHMARK)
                raw.setdefault("candidate_behavior_identity", behavior_identity)
        opportunity = (
            f"{behavior_identity}:{vehicle}:{symbol}:{day.isoformat()}")
        if raw is None:
            # A non-empty grouped session must always have a terminal
            # disposition. Keep an internal contract breach visible and
            # unevaluable instead of silently turning it into no signal.
            rows.append({
                "vehicle": vehicle, "symbol": symbol,
                "session_date": day.isoformat(), "opportunity_id": opportunity,
                "net_pnl": 0.0, "return_value": 0.0, "no_trade": True,
                "execution_disposition": "refused",
                "signal_opportunity": False,
                "reject_stage": "simulation_contract",
                "reject_reason": "simulation_missing_disposition",
            })
            continue
        if raw.get("execution_disposition") == "no_signal":
            row = {"vehicle": vehicle, "symbol": symbol,
                   "session_date": day.isoformat(), "opportunity_id": opportunity,
                   "net_pnl": 0.0, "return_value": 0.0, "no_trade": True}
            row.update(raw)
            rows.append(row)
            continue
        if raw.get("unpriced_reason"):
            row = {"vehicle": vehicle, "symbol": symbol,
                   "session_date": day.isoformat(), "opportunity_id": opportunity,
                   "net_pnl": 0.0, "return_value": 0.0, "no_trade": True}
            row.update({key: value for key, value in raw.items()
                        if key != "unpriced_reason"})
            row["reject_reason"] = str(raw["unpriced_reason"])
            rows.append(row)
            continue
        candidates.append({**raw, "opportunity_id": opportunity,
                           "_symbol": symbol, "_day": day})

    cash = float(starting_cash)
    peak = cash
    drawdown = 0.0
    active: list[dict] = []
    realized_by_day: dict[str, float] = {}
    day_start_equity: dict[str, float] = {}
    cash_has_historical_evidence = False

    def realize_until(timestamp: datetime) -> None:
        nonlocal cash, peak, drawdown, cash_has_historical_evidence
        closing = [item for item in active
                   if datetime.fromisoformat(item["exit_timestamp"]) <= timestamp]
        for item in sorted(closing, key=lambda value: (
                value["exit_timestamp"], value["symbol"])):
            cash += float(item["net_pnl"])
            if item.get("evidence_mode") == DIAGNOSTIC_HISTORICAL_BACKFILL:
                cash_has_historical_evidence = True
            day_key = item["session_date"]
            realized_by_day[day_key] = realized_by_day.get(day_key, 0.0) + float(item["net_pnl"])
            peak = max(peak, cash)
            drawdown = max(drawdown, peak - cash)
            active.remove(item)

    mark_diagnostics: list[dict[str, Any]] = []

    def mark_active(timestamp: datetime) -> tuple[float | None, bool]:
        """Mark open positions from information visible at ``timestamp``.

        Equity marks prefer the executable side of a fresh recorded quote and
        fall back to the exact bar open/last completed close.  Long options
        use the visible bid for liquidation.  Closed rows are removed by
        ``realize_until`` before this function runs, so realized P&L and
        unrealized P&L cannot be counted twice.  The provenance flag follows
        both the selected market records and any earlier diagnostic sizing
        already embedded in an active position or realized cash balance.
        """
        unrealized = 0.0
        missing: list[str] = []
        historical_evidence = cash_has_historical_evidence
        for item in active:
            historical_evidence = bool(
                historical_evidence or
                item.get("evidence_mode") == DIAGNOSTIC_HISTORICAL_BACKFILL)
            symbol = str(item.get("symbol", "")).upper()
            direction = str(item.get("direction", "long"))
            mark: float | None = None
            if vehicle == "option":
                snap = _option_at(
                    snapshots, symbol=symbol,
                    day=date.fromisoformat(str(item["session_date"])),
                    direction=direction, cutoff=timestamp,
                    contract_symbol=item.get("contract"), policy=resolved_policy)
                if snap is not None:
                    mark = float(snap.bid)
                    historical_evidence = bool(
                        historical_evidence or _historical_evidence(snap))
            else:
                side = "sell" if direction == "long" else "buy"
                quoted_mark = quote_fill_record(
                    quote_index, symbol=symbol, at=timestamp, side=side,
                    max_age_seconds=resolved_policy.max_market_data_age_seconds,
                    session_date=date.fromisoformat(str(item["session_date"])),
                    allow_historical_backfill_diagnostics=(
                        resolved_policy.allow_historical_backfill_diagnostics))
                if quoted_mark is not None:
                    mark = float(quoted_mark.price)
                    historical_evidence = bool(
                        historical_evidence or
                        _historical_evidence(quoted_mark))
                else:
                    bar_mark = _visible_bar_mark_record(
                        market_context.get(symbol, ()), timestamp,
                        resolved_policy,
                    )
                    if bar_mark is not None:
                        mark, mark_record = bar_mark
                        historical_evidence = bool(
                            historical_evidence or
                            _historical_evidence(mark_record))
            if mark is None:
                # Do not substitute entry price (or a future exit) when the
                # active position has no visible mark. Account capacity and
                # open-risk are unknown; fail closed until a fresh mark is
                # observable.
                missing.append(symbol)
                continue
            quantity = float(item.get("quantity", 0.0))
            multiplier = float(item.get("contract_multiplier", 1))
            entry = float(item["entry_price"])
            gross = ((mark - entry) if direction == "long" else
                     (entry - mark)) * quantity * multiplier
            active_model = resolve_cost_model(item)
            entry_fees = active_model.fees(
                entry, entry, quantity, multiplier, vehicle=vehicle) / 2.0
            unrealized += gross - entry_fees
        if missing:
            mark_diagnostics.append({
                "timestamp": timestamp.isoformat(),
                "symbols": sorted(set(missing)),
                "reason": "active position mark unavailable",
            })
            return None, historical_evidence
        return unrealized, historical_evidence

    for raw in sorted(candidates, key=lambda item: (
            item["entry_timestamp"], item["_symbol"], item["_day"])):
        entry_at = datetime.fromisoformat(raw["entry_timestamp"])
        realize_until(entry_at)
        symbol, day, opportunity = raw["_symbol"], raw["_day"], raw["opportunity_id"]
        day_key = day.isoformat()
        day_start_equity.setdefault(day_key, cash)
        unrealized, mark_has_historical_evidence = mark_active(entry_at)
        if unrealized is None:
            rows.append({
                "vehicle": vehicle, "symbol": symbol,
                "session_date": day.isoformat(), "opportunity_id": opportunity,
                "no_trade": True,
                "execution_disposition": "refused",
                "signal_opportunity": True,
                "reject_stage": "mark_data",
                "reject_reason": "active position mark unavailable",
                "diagnostic_only": True,
                "authorizing": False,
                "mark_data_unavailable": True,
                "mark_data_diagnostics": list(mark_diagnostics[-1:]),
            })
            continue
        current_equity = cash + unrealized
        effective_risk_pct = float(resolved_policy.risk_per_trade_pct)
        risk_budget = max(0.0, current_equity * effective_risk_pct / 100.0)
        per_unit = max(float(raw["risk_per_unit"]), 1e-9)
        risk_sized_quantity = math.floor(risk_budget / per_unit)
        quantity = risk_sized_quantity
        notional_cap_quantity: int | None = None
        position_notional_cap_usd: float | None = None
        if vehicle == "equity":
            notional_pct = (NOTIONAL_CAP_PCT if
                            resolved_policy.max_position_notional_pct is None else
                            resolved_policy.max_position_notional_pct)
            position_notional_cap_usd = max(
                0.0, current_equity * float(notional_pct) / 100.0)
            notional_cap_quantity = math.floor(
                position_notional_cap_usd /
                max(float(raw["entry_reference"]), 1e-9))
            quantity = min(quantity, notional_cap_quantity)
        elif resolved_policy.max_position_notional_pct is not None:
            position_notional_cap_usd = max(
                0.0, current_equity *
                float(resolved_policy.max_position_notional_pct) / 100.0)
            notional_cap_quantity = math.floor(
                position_notional_cap_usd /
                max(float(raw["entry_reference"]) *
                    int(raw["contract_multiplier"]), 1e-9))
            quantity = min(quantity, notional_cap_quantity)
        multiplier = int(raw["contract_multiplier"])
        nominal_risk_usd = quantity * float(raw["risk_per_unit"])
        realized_risk_usd = quantity * float(raw["realized_risk_per_unit"])
        # Runtime brackets size and stress-check against the executable quote
        # (the authored reference remains available as telemetry). Equity
        # notional therefore uses ``entry_reference`` while options use
        # premium × multiplier × contracts.
        entry_notional = ((float(raw["entry_reference"]) * quantity)
                          if vehicle == "equity" else
                          (float(raw["entry_reference"]) * quantity * multiplier))
        stress_enabled = (
            resolved_policy.stressed_cost_scenario_bps is not None or
            resolved_policy.max_stressed_cost_to_risk_ratio is not None or
            resolved_policy.stressed_cost_calibration_enabled)
        stress_scenario, stress_activation_reason = (
            resolved_policy.resolve_stress_scenario(
                symbol, raw.get("entry_timestamp"), vehicle=vehicle))
        # Keep direct ReplayPolicy fixtures behaviour-compatible when both
        # controls are omitted, while validated runtime policies expose the
        # nominal risk unit used by RiskEngine.
        risk_usd = nominal_risk_usd if stress_enabled else realized_risk_usd
        sizing_telemetry = {
            "risk_budget": float(risk_budget),
            "risk_sized_quantity": int(risk_sized_quantity),
            "notional_cap_quantity": notional_cap_quantity,
            "notional_cap_usd": position_notional_cap_usd,
            "notional_cap_binding": bool(
                notional_cap_quantity is not None and
                notional_cap_quantity < risk_sized_quantity),
            "planned_risk_usd": float(nominal_risk_usd),
            "realized_risk_usd": float(realized_risk_usd),
            "risk_budget_utilization": (
                float(nominal_risk_usd / risk_budget)
                if risk_budget > 0 else None),
            "planned_notional": float(entry_notional),
        }
        cost_context = {
            **{key: value for key, value in raw.items()
               if not str(key).startswith("_")},
            "vehicle": vehicle,
            "symbol": symbol,
            "session_date": day_key,
            "cost_leg": "entry",
            "cost_timestamp": raw.get("entry_timestamp"),
            "quantity": quantity,
            "shares": quantity if vehicle == "equity" else None,
            "contracts": quantity if vehicle == "option" else None,
            "entry_notional": float(entry_notional),
        }
        model = resolve_cost_model(cost_context)
        sizing_telemetry.update({
            "cost_model_provenance": model.provenance,
            "entry_cost_model_provenance": model.provenance,
            "cost_model_spread_bps": model.spread_bps,
            "cost_model_slippage_bps": model.slippage_bps,
            "cost_model_fee_bps": model.fee_bps,
            "entry_cost_model_spread_bps": model.spread_bps,
            "entry_cost_model_slippage_bps": model.slippage_bps,
            "entry_cost_model_fee_bps": model.fee_bps,
        })
        reject_reason = None
        reject_stage = None
        if (resolved_policy.max_concurrent_positions is not None and
                len(active) >= resolved_policy.max_concurrent_positions):
            reject_reason = "max concurrent positions reached"
            reject_stage = "position_limit"
        elif (resolved_policy.max_gross_exposure_pct is not None and
              sum(float(item.get("entry_notional", 0.0)) for item in active) + entry_notional >
              current_equity * float(resolved_policy.max_gross_exposure_pct) / 100.0):
            reject_reason = "buying power/notional limit reached"
            reject_stage = "gross_exposure_limit"
        elif (resolved_policy.max_open_risk_pct is not None and
              sum(float(item.get("risk_usd", 0.0)) for item in active) + risk_usd >
              current_equity * float(resolved_policy.max_open_risk_pct) / 100.0):
            reject_reason = "max open risk reached"
            reject_stage = "open_risk_limit"
        elif (resolved_policy.daily_loss_limit_pct is not None and
              current_equity - day_start_equity[day_key] <=
              -day_start_equity[day_key] *
              float(resolved_policy.daily_loss_limit_pct) / 100.0):
            reject_reason = "daily loss limit reached"
            reject_stage = "daily_loss_limit"
        if quantity <= 0:
            reject_reason = reject_reason or "isolated account risk budget cannot fund one unit"
            reject_stage = reject_stage or "position_sizing"
        stress_telemetry: dict[str, Any] = {}
        if reject_reason is None and stress_enabled:
            plan = {
                "execution_profile": "options" if vehicle == "option" else "shares",
                "contracts": quantity if vehicle == "option" else None,
                "shares": quantity if vehicle == "equity" else None,
                "notional": entry_notional,
                "risk_usd": nominal_risk_usd,
            }
            checked, stress_reason = check_stressed_cost_plan(
                plan,
                scenario_bps=stress_scenario,
                max_ratio=resolved_policy.max_stressed_cost_to_risk_ratio,
                costs=model,
            )
            if stress_reason is not None:
                reject_reason = stress_reason
                reject_stage = "cost_stress"
                stress_telemetry = {
                    "vehicle": vehicle,
                    "stressed_cost_vehicle": vehicle,
                    "stressed_cost_schema": STRESSED_COST_SCHEMA,
                    "stressed_cost_basis": dict(STRESSED_COST_BASIS),
                    "stressed_cost_entry_notional": float(entry_notional),
                    "entry_notional": float(entry_notional),
                    "stressed_cost_scenario_bps": stress_scenario,
                    "stressed_cost_activation_reason": stress_activation_reason,
                    "max_stressed_cost_to_risk_ratio": (
                        resolved_policy.max_stressed_cost_to_risk_ratio),
                    "stressed_cost_risk_usd": float(nominal_risk_usd),
                    "risk_usd": float(nominal_risk_usd),
                }
                try:
                    if (stress_scenario is not None and
                            resolved_policy.max_stressed_cost_to_risk_ratio is not None and
                            nominal_risk_usd > 0 and entry_notional > 0):
                        stressed = stressed_cost_usd(
                            entry_notional=entry_notional,
                            scenario_bps=stress_scenario,
                            vehicle=vehicle, quantity=quantity, costs=model)
                        stress_telemetry.update({
                            "stressed_cost_usd": float(stressed),
                            "stressed_cost_to_risk_ratio": float(
                                stressed / nominal_risk_usd),
                        })
                except (CostError, TypeError, ValueError, OverflowError,
                        ZeroDivisionError):
                    pass
        if reject_reason:
            rows.append({"vehicle": vehicle, "symbol": symbol,
                         "session_date": day.isoformat(), "opportunity_id": opportunity,
                         "net_pnl": 0.0, "return_value": 0.0, "no_trade": True,
                         "execution_disposition": "refused",
                         "signal_opportunity": True,
                         "reject_stage": reject_stage or "account_policy",
                         "reject_reason": reject_reason,
                         **sizing_telemetry,
                         **stress_telemetry})
            continue
        # An executable quote is already the selected fill price, but the
        # runtime refuses an adverse quote beyond its configured cap.  Apply
        # the same pure helper here before charging modelled slippage so a
        # factory account cannot trade an opportunity the runtime would have
        # rejected.  Missing boundary references are accepted: strict replay
        # may legitimately have a quote without a visible bar anchor.
        slippage_telemetry = None
        if (vehicle == "equity" and raw.get("entry_fill_source") == QUOTE and
                raw.get("entry_slippage_reference") is not None):
            slippage_telemetry, slippage_reason = check_entry_slippage(
                "buy" if raw.get("direction") == "long" else "sell",
                raw.get("entry_slippage_reference"), raw.get("entry_reference"),
                model.max_slippage_bps)
            if slippage_reason is not None:
                rows.append({"vehicle": vehicle, "symbol": symbol,
                             "session_date": day.isoformat(),
                             "opportunity_id": opportunity,
                             "net_pnl": 0.0, "return_value": 0.0,
                             "no_trade": True,
                             "execution_disposition": "refused",
                             "signal_opportunity": True,
                             "reject_stage": "entry_slippage",
                             "reject_reason": slippage_reason,
                             "entry_slippage": slippage_telemetry})
                continue
        execution_direction = "long" if vehicle == "option" else raw["direction"]
        exit_model = resolve_cost_model({
            **cost_context,
            "cost_leg": "exit",
            "cost_timestamp": raw.get("exit_timestamp"),
        })
        entry = model.execution_price(
            raw["entry_reference"], execution_direction, entry=True,
            executable_quote=(vehicle == "option" and _fresh(
                raw, "entry", resolved_policy.max_market_data_age_seconds)) or
            raw.get("entry_fill_source") == QUOTE)
        exit_price = exit_model.execution_price(
            raw["exit_reference"], execution_direction, entry=False,
            executable_quote=(vehicle == "option" and _fresh(
                raw, "exit", resolved_policy.max_market_data_age_seconds)) or
            raw.get("exit_fill_source") == QUOTE)
        gross = ((exit_price - entry) if execution_direction == "long" else
                 (entry - exit_price)) * quantity * multiplier
        entry_fees = model.fees(
            entry, entry, quantity, multiplier, vehicle=vehicle) / 2.0
        exit_fees = exit_model.fees(
            exit_price, exit_price, quantity, multiplier, vehicle=vehicle) / 2.0
        fees = entry_fees + exit_fees
        net = gross - fees
        row = {key: value for key, value in raw.items() if not key.startswith("_")}
        if (mark_has_historical_evidence or
                str(row.get("evidence_mode") or "").strip().lower() ==
                DIAGNOSTIC_HISTORICAL_BACKFILL):
            row["evidence_mode"] = DIAGNOSTIC_HISTORICAL_BACKFILL
        row.update({"quantity": quantity, "entry_price": entry, "exit_price": exit_price,
                    "gross_pnl": gross, "costs": fees, "net_pnl": net,
                    "risk_usd": risk_usd,
                    "nominal_risk_usd": nominal_risk_usd,
                    "r_multiple": net / risk_usd if risk_usd > 0 else None,
                    "return_value": net / cash if cash > 0 else 0.0,
                    "no_trade": False, "entry_notional": entry_notional,
                    "execution_disposition": "executed",
                    "signal_opportunity": True,
                    "exit_cost_model_provenance": exit_model.provenance,
                    "exit_cost_model_spread_bps": exit_model.spread_bps,
                    "exit_cost_model_slippage_bps": exit_model.slippage_bps,
                    "exit_cost_model_fee_bps": exit_model.fee_bps,
                    **sizing_telemetry})
        if slippage_telemetry is not None:
            row["entry_slippage"] = slippage_telemetry
        if stress_enabled:
            # ``checked`` is populated on the pass path above.  Recompute the
            # small pure seam here only to carry its canonical telemetry into
            # the persisted trade row; this cannot change acceptance.
            checked, _ = check_stressed_cost_plan(
                {"execution_profile": "options" if vehicle == "option" else "shares",
                 "contracts": quantity if vehicle == "option" else None,
                 "shares": quantity if vehicle == "equity" else None,
                 "notional": entry_notional, "risk_usd": nominal_risk_usd},
                scenario_bps=stress_scenario,
                max_ratio=resolved_policy.max_stressed_cost_to_risk_ratio,
                costs=model)
            if checked is not None:
                row.update({key: value for key, value in checked.items()
                            if key.startswith("stressed_cost_") or
                            key == "max_stressed_cost_to_risk_ratio"})
                row["stressed_cost_activation_reason"] = stress_activation_reason
        bar_fallback_diagnostic = (
            not resolved_policy.strict_market_data and
            not (str(row.get("entry_fill_source") or "").strip().lower() == QUOTE and
                 str(row.get("exit_fill_source") or "").strip().lower()
                 in {QUOTE, RESTING_BRACKET}))
        if bar_fallback_diagnostic:
            if str(row.get("evidence_mode") or "").strip().lower() != DIAGNOSTIC_HISTORICAL_BACKFILL:
                row["evidence_mode"] = DIAGNOSTIC_BAR_FALLBACK
        if (bool(row.get("hold_discontinuity_exit",
                       row.get("hold_discontinuity"))) or
                resolved_policy.allow_historical_backfill_diagnostics or
                bar_fallback_diagnostic or
                str(row.get("evidence_mode") or "").strip().lower() ==
                DIAGNOSTIC_HISTORICAL_BACKFILL or
                str(row.get("evidence_mode") or "").strip().lower() ==
                DIAGNOSTIC_BAR_FALLBACK):
            # Sparse-hold exits remain executable/accounting diagnostics, but
            # cannot authorize directional P&L or strategy selection.  The
            # same boundary applies to historical backfill and non-strict bar
            # fallback evidence even when mechanics produced an ordinary exit.
            row.update({"diagnostic_only": True, "authorizing": False,
                        "directional_authorizing": False})
            if (resolved_policy.allow_historical_backfill_diagnostics and
                    str(row.get("evidence_mode") or "").strip().lower() not in {
                        DIAGNOSTIC_HISTORICAL_BACKFILL,
                        DIAGNOSTIC_BAR_FALLBACK,
                    } and not bool(row.get(
                        "hold_discontinuity_exit", row.get("hold_discontinuity")))):
                row["diagnostic_reason"] = "diagnostic_backfill_policy"
        else:
            row.setdefault("directional_authorizing", True)
        rows.append(row)
        active.append(row)
    if active:
        realize_until(max(datetime.fromisoformat(item["exit_timestamp"]) for item in active))
    rows.sort(key=lambda row: (str(row.get("session_date", "")),
                               str(row.get("symbol", "")),
                               str(row.get("entry_timestamp", ""))))
    for row in rows:
        disposition = str(row.get("execution_disposition") or "")
        if disposition not in {"executed", "refused", "no_signal"}:
            raise RuntimeError("factory row has no terminal execution disposition")
        if (disposition == "refused" and
                not str(row.get("reject_reason") or "").strip()):
            raise RuntimeError("factory refusal has no durable reason")
        if disposition == "no_signal" and row.get("signal_opportunity") is not False:
            raise RuntimeError("no-signal row is marked as a signal opportunity")
    executed = [row for row in rows if row.get("no_trade") is not True]
    authorizing = [row for row in executed
                   if row.get("directional_authorizing", True) is not False]
    authorizing_pnl = sum(float(row.get("net_pnl", 0.0)) for row in authorizing)
    return {"account_id": account_id, "starting_cash": float(starting_cash),
            "ending_equity": cash, "realized_pnl": cash - float(starting_cash),
            "authorizing_realized_pnl": authorizing_pnl,
            "authorizing_trades": len(authorizing),
            "mark_diagnostics": mark_diagnostics,
            "max_drawdown": drawdown, "trades": len(executed), "rows": rows}


def diagnose(rows: Sequence[Mapping], *, starting_cash: float = 100_000.0,
             diagnostic_only: bool = False) -> dict:
    if not isinstance(diagnostic_only, bool):
        raise TypeError("diagnostic_only must be boolean")
    all_trades = [row for row in rows if row.get("no_trade") is not True]
    authorizing_trades = [row for row in all_trades
                          if row.get("directional_authorizing", True) is not False and
                          str(row.get("evidence_mode") or "").strip().lower()
                          not in {DIAGNOSTIC_HISTORICAL_BACKFILL,
                                  DIAGNOSTIC_BAR_FALLBACK}]
    has_non_authorizing = len(authorizing_trades) != len(all_trades)
    # Diagnostic factory analysis intentionally retains every executed row so
    # historical reachability/P&L remains useful telemetry.  The separate
    # authorizing projection above is still what default diagnosis and
    # promotion-facing summaries consume.
    trades = all_trades if diagnostic_only else authorizing_trades
    pnl = [float(row.get("net_pnl", 0.0)) for row in trades]
    wins = [value for value in pnl if value > 0]
    losses = [value for value in pnl if value < 0]
    sessions = {row.get("session_date") for row in rows}
    expectancy = mean(pnl) if pnl else 0.0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses else (float("inf") if wins else 0.0)
    # Directional authorization excludes sparse-hold diagnostics just like
    # expectancy/P&L; retain their telemetry below without letting their sign
    # alter the gate's drawdown decision.
    drawdown = max_drawdown_of(trades)
    # A fit can have plenty of actionable opportunities while every one is
    # refused by an explicit execution/risk boundary.  Preserve that signal
    # for the bounded search loop; a plain zero-signal stream has no reason
    # code and remains the ordinary ``insufficient_signals`` diagnosis.
    no_trade_rows = [row for row in rows if row.get("no_trade") is True]
    no_signal_rows = [row for row in no_trade_rows
                      if row.get("execution_disposition") == "no_signal"]
    refused_rows = [row for row in no_trade_rows
                    if (row.get("execution_disposition") == "refused" or
                        (not row.get("execution_disposition") and
                         row.get("reject_reason")))]
    unclassified_rows = [row for row in no_trade_rows
                         if (row.get("execution_disposition") not in {
                                 "no_signal", "refused"} and
                             not row.get("reject_reason"))]
    reject_reasons = [str(row.get("reject_reason") or "").strip()
                      for row in refused_rows]
    execution_blocked = (bool(refused_rows) and not trades and
                         not unclassified_rows and all(reject_reasons) and
                         all(row.get("signal_opportunity") is not False
                             for row in refused_rows))
    if execution_blocked:
        failure = "execution_blocked"
    elif len(trades) < max(3, len(sessions) // 3):
        failure = "insufficient_signals"
    elif expectancy <= 0:
        failure = "negative_expectancy"
    elif len(wins) / len(trades) < .35:
        failure = "low_win_rate"
    elif profit_factor < 1.1:
        failure = "poor_payoff"
    elif drawdown > starting_cash * .05:
        failure = "excess_drawdown"
    else:
        failure = "none"
    discontinuity_rows = [row for row in all_trades if bool(
        row.get("hold_discontinuity_exit", row.get("hold_discontinuity")))]
    discontinuity_exits = len(discontinuity_rows)
    time_expiry_exits = sum(
        row.get("hold_exit_reason") == "time_expiry" for row in trades)
    return {
        "primary_failure": failure, "trades": len(trades),
        "authorizing_trades": len(authorizing_trades),
        "executed_trades": len(all_trades),
        "sessions": len(sessions), "net_pnl": sum(pnl), "expectancy": expectancy,
        "directional_authorizing": not diagnostic_only and not has_non_authorizing,
        "authorizing": not diagnostic_only and not has_non_authorizing,
        "diagnostic_only": bool(diagnostic_only),
        "execution_blocked": execution_blocked,
        "execution_rejection_count": len(refused_rows),
        "no_signal_count": len(no_signal_rows),
        "unclassified_no_trade_count": len(unclassified_rows),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "profit_factor": profit_factor if math.isfinite(profit_factor) else 999.0,
        "max_drawdown": drawdown,
        "stop_rate": (sum(row.get("exit_reason") == "stop" for row in trades) / len(trades)
                      if trades else 0.0),
        "target_rate": (sum(row.get("exit_reason") == "target" for row in trades) / len(trades)
                        if trades else 0.0),
        "hold_telemetry": {
            "discontinuity_exits": discontinuity_exits,
            "discontinuity_exit_rate": (
                discontinuity_exits / len(all_trades) if all_trades else 0.0),
            "diagnostic_trade_count": len(discontinuity_rows),
            "time_expiry_exits": time_expiry_exits,
            "time_expiry_rate": (time_expiry_exits / len(trades)
                                  if trades else 0.0),
            "diagnostic_only": True,
            "authorizing": False,
        },
    }


def _safe_variant(spec: Mapping[str, Any], **changes) -> dict:
    candidate = dict(spec); candidate.update(changes)
    return validate_rule_spec(candidate)


def spec_delta(root: Mapping[str, Any], spec: Mapping[str, Any]) -> dict[str, Any]:
    """The fields a variant changed relative to its root, and to what."""
    delta: dict[str, Any] = {}
    for key in sorted(set(root) | set(spec)):
        before, after = root.get(key), spec.get(key)
        if before != after:
            delta[key] = {"from": before, "to": after}
    return delta


def _change_phrase(delta: Mapping[str, Any]) -> str:
    if not delta:
        return "no parameter changed"
    return "; ".join(
        f"{key} {value['from']}→{value['to']}" for key, value in delta.items())


def mutation_reason(root: Mapping[str, Any], spec: Mapping[str, Any],
                    diagnostic: Mapping[str, Any], *, swept: bool = False) -> str:
    """State why a deterministic mutation was made, in the same shape as an
    LLM-authored reason.

    Recording this for the deterministic path too is what makes the lesson
    ledger comparable: the feedback loop can then show that a tuned reason
    outperformed (or failed to outperform) the fixed mutation table, instead of
    only ever grading the model against itself.
    """
    delta = spec_delta(root, spec)
    if not delta:
        return ("Unmutated root, kept as the null calibration its own variants "
                "are measured against.")
    failure = str(diagnostic.get("primary_failure") or "none")
    if swept:
        return (f"Deterministic sweep fill with no diagnosis behind it: "
                f"{_change_phrase(delta)}.")[:_REASON_LIMIT]
    return (f"Deterministic response to {failure}: "
            f"{_change_phrase(delta)}.")[:_REASON_LIMIT]


# Mirrors ``research.llm_strategy.MAX_REASON_CHARS`` without importing the
# optional adapter module into the deterministic core; ``test_llm_tuning``
# pins the two together.
_REASON_LIMIT = 240


def mutate_from_diagnosis(spec: Mapping[str, Any], diagnostic: Mapping[str, Any],
                          count: int = DEFAULT_VARIANTS) -> list[dict]:
    """Create bounded variants from an explicit failure diagnosis."""
    return [item[0] for item in mutate_with_reasons(spec, diagnostic, count)]


_COORDINATE_FIELDS = (
    "threshold_bps", "target_r", "breakeven_r", "stop_atr", "max_hold_bars",
    "target_mode", "target_lookback", "trailing_stop_r", "exit_before_minutes",
    "lookback", "slow_lookback", "range_minutes", "zscore",
    "volume_multiplier", "compression_bps", "atr_period", "side",
    "confirmation", "entry_after_minutes", "entry_before_minutes",
    "min_atr_bps", "max_atr_bps", "confirmations",
)
_FAILURE_FIELD_PRIORITY = {
    "insufficient_signals": (
        "threshold_bps", "confirmation", "lookback", "range_minutes",
        "zscore", "volume_multiplier", "entry_before_minutes", "stop_atr",
        "min_atr_bps"),
    "execution_blocked": (
        "stop_atr", "min_atr_bps", "threshold_bps", "entry_before_minutes",
        "lookback", "range_minutes"),
    "negative_expectancy": (
        "threshold_bps", "target_r", "breakeven_r", "stop_atr", "max_hold_bars",
        "confirmation", "side", "target_mode", "target_lookback",
        "trailing_stop_r", "exit_before_minutes"),
    "poor_payoff": (
        "target_r", "breakeven_r", "stop_atr", "target_mode",
        "target_lookback", "trailing_stop_r", "exit_before_minutes",
        "max_hold_bars", "threshold_bps"),
    "low_win_rate": (
        "target_r", "breakeven_r", "threshold_bps", "max_hold_bars",
        "confirmation", "target_mode", "target_lookback",
        "trailing_stop_r", "exit_before_minutes"),
    "excess_drawdown": (
        "stop_atr", "max_hold_bars", "side", "threshold_bps",
        "confirmation", "trailing_stop_r", "target_mode",
        "exit_before_minutes"),
}
_ZERO_AXIS_STEPS = {
    "threshold_bps": (5.0, 10.0),
    "min_atr_bps": (5.0, 15.0),
    "entry_after_minutes": (30, 60),
}
# Stop distance is the economic lever for the stressed-cost boundary.  A
# local +/-20% nudge around a one-ATR root never reaches the several-ATR
# distance needed for a 25 bps / 30% cost-to-risk gate on SPY-like ATRs, so
# expose the audited grammar span explicitly while retaining one-coordinate
# mutations.  Values are ordered from tight to wide for deterministic search.
_STOP_ATR_LADDER = (0.2, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 10.0)
# The min-ATR axis shares the same preregistered large-axis values as the
# LLM lane.  It is exposed by the deterministic lane only for an explicit
# execution/cost-stress diagnosis; ordinary roots retain their local nudge.
_MIN_ATR_BPS_LADDER = (
    0.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0,
    75.0, 100.0, 150.0, 200.0, 300.0, 500.0, 1_000.0, 2_000.0,
)
_TARGET_MODE_LADDER = ("fixed_r", "session_vwap", "rolling_mean")
_TARGET_LOOKBACK_LADDER = (2, 5, 10, 20, 40, 60, 90, 120)
_TRAILING_STOP_R_LADDER = (None, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 10.0)
_EXIT_BEFORE_MINUTES_LADDER = (None, 30, 60, 120, 180, 240, 300, 360, 389)
_REVERSION_TARGET_R_LADDER = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
_TREND_TARGET_R_LADDER = (1.0, 1.5, 2.0, 2.5, 3.0, 5.0)
_REVERSION_HOLD_LADDER = (15, 30, 45, 60, 90)
_TREND_HOLD_LADDER = (45, 60, 90, 120, 180, 240)


def _family_exit_hypothesis(root: Mapping[str, Any]) -> dict | None:
    """Return one early, auditable family-specific exit coordinate.

    Roots remain at neutral v4 defaults so their content identities and
    synthetic-control relationship stay stable.  Reversion families spend one
    early coordinate on a frozen fair-value target; breakout/trend families
    spend one on a completed-close trailing ratchet.  The neutral root and the
    remaining first-batch members retain fixed-R behavior, so an unavailable
    fair-value reference cannot suppress the entire batch.
    """
    if root.get("schema") != RULE_SCHEMA_V4:
        return None
    family = str(root.get("family") or "")
    if family == "mean_reversion":
        change = {"target_mode": "rolling_mean"}
    elif family in {"opening_range_fade", "vwap_reversion"}:
        change = {"target_mode": "session_vwap"}
    elif family in TREND_BREAKOUT_FAMILIES:
        change = {"trailing_stop_r": 1.5}
    else:
        return None
    try:
        candidate = _safe_variant(root, **change)
    except (TypeError, ValueError):
        return None
    return candidate if len(spec_delta(root, candidate)) == 1 else None


def _stressed_cost_diagnostic(diagnostic: Mapping[str, Any] | None) -> bool:
    """Whether a fit-only diagnosis explicitly opens the stress ladder."""
    if not isinstance(diagnostic, Mapping):
        return False
    if diagnostic.get("execution_blocked") is True or \
            str(diagnostic.get("primary_failure") or "") == "execution_blocked":
        return True

    def walk(value: Any, key: str = "") -> bool:
        normalized = "".join(char if char.isalnum() else "_"
                              for char in str(key).lower()).strip("_")
        if normalized == "grammar_stop_floor_admissible" and value is False:
            return True
        if normalized in {"required_stop_distance_bps", "effective_stop_floor_bps"}:
            try:
                if float(value) > float(MIN_STOP_DISTANCE_BPS):
                    return True
            except (TypeError, ValueError, OverflowError):
                pass
        if normalized in {"cost_stress", "cost_stressed",
                          "stressed_cost_rejection", "stressed_cost_blocked"}:
            return value is True or (isinstance(value, (int, float)) and
                                     not isinstance(value, bool) and value > 0)
        if ("stressed_cost_risk" in normalized or
                "stressed_cost_rejection" in normalized or
                "stressed_cost_blocked" in normalized):
            return value is not False and value is not None and (
                not isinstance(value, (int, float)) or value > 0)
        if isinstance(value, str):
            text = value.lower().replace("-", "_").replace(" ", "_")
            return any(token in text for token in (
                "cost_stress", "stressed_cost", "execution_blocked"))
        if isinstance(value, Mapping):
            return any(walk(item, str(name)) for name, item in value.items())
        if isinstance(value, (list, tuple)):
            return any(walk(item, normalized) for item in value)
        return False

    return walk(diagnostic)


def _coordinate_values(root: Mapping[str, Any], field: str,
                       diagnostic: Mapping[str, Any] | None = None) -> list[Any]:
    """Return two bounded directions for one executable field.

    Validation remains the source of truth for bounds.  This helper merely
    proposes neighboring values; invalid boundary points are discarded by the
    same rule validator used for every other authored strategy.
    """
    value = root.get(field)
    family = str(root.get("family") or "")
    reversion = family in REVERSION_FAMILIES
    if field == "side":
        return [item for item in ("both", "long", "short") if item != value]
    if field == "confirmation":
        return [item for item in ("none", "trend", "volume", "volatility")
                if item != value]
    if field == "target_mode":
        ordered = (("session_vwap", "rolling_mean", "fixed_r") if reversion
                   else _TARGET_MODE_LADDER)
        return [item for item in ordered if item != value]
    if field == "target_lookback":
        ordered = ((5, 10, 20, 30, 45, 60) if reversion else
                   (10, 20, 40, 60, 90, 120))
        return [item for item in ordered if item != value]
    if field == "trailing_stop_r":
        ordered = ((None, 0.5, 1.0, 1.5, 2.0) if reversion else
                   (None, 1.0, 1.5, 2.0, 3.0, 4.0))
        return [item for item in ordered if item != value]
    if field == "exit_before_minutes":
        return [item for item in _EXIT_BEFORE_MINUTES_LADDER if item != value]
    if field == "confirmations":
        current = list(value or ())
        values: list[list[str]] = []
        if current:
            for item in current:
                values.append([entry for entry in current if entry != item])
        for item in ("trend", "volume", "volatility"):
            if item not in current:
                values.append([*current, item])
        return values
    if field == "breakeven_r":
        target = float(root["target_r"])
        candidates: list[float | None] = [None, 0.0]
        candidates.extend(round(target * fraction, 8)
                          for fraction in (.25, .5, .75))
        return [candidate for candidate in candidates
                if candidate != value and
                (candidate is None or candidate < target)]
    if field == "stop_atr":
        return [value for value in _STOP_ATR_LADDER
                if float(value) != float(root.get(field))]
    if field == "target_r":
        ladder = (_REVERSION_TARGET_R_LADDER if reversion else
                  _TREND_TARGET_R_LADDER)
        return [candidate for candidate in ladder
                if float(candidate) != float(value)]
    if field == "max_hold_bars":
        ladder = (_REVERSION_HOLD_LADDER if reversion else
                  _TREND_HOLD_LADDER)
        return [candidate for candidate in ladder if int(candidate) != int(value)]
    if field == "min_atr_bps" and _stressed_cost_diagnostic(diagnostic):
        return [candidate for candidate in _MIN_ATR_BPS_LADDER
                if float(candidate) != float(value)]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return []
    if field in _ZERO_AXIS_STEPS and float(value) == 0.0:
        return list(_ZERO_AXIS_STEPS[field])
    if field == "entry_before_minutes" and int(value) >= SESSION_MINUTES:
        return [max(1, int(value) - 60), max(1, int(value) - 30)]
    if isinstance(value, int):
        step = max(1, int(round(abs(value) * .2)))
        return [value - step, value + step]
    step = max(.25, abs(float(value)) * .2)
    return [float(value) - step, float(value) + step]


def coordinate_mutation_pool(
        spec: Mapping[str, Any], diagnostic: Mapping[str, Any]
        ) -> list[tuple[dict, str]]:
    """Return the complete deterministic one-factor neighborhood.

    Every child differs from the root in exactly one executable field.  The
    pool is intentionally larger than one worker batch: later cycles can skip
    graded failures and continue through the remaining axes before the
    hypothesis is eligible for interaction tests or replacement.
    """
    root = validate_rule_spec(spec)
    failure = str(diagnostic.get("primary_failure") or "none")
    priority = list(_FAILURE_FIELD_PRIORITY.get(failure, ()))
    fields = [*priority, *(item for item in _COORDINATE_FIELDS
                           if item not in priority)]
    root_signature = rule_semantic_signature(root)
    seen = {rule_variant_id(root)}
    variants: list[tuple[dict, str]] = [
        (root, mutation_reason(root, root, diagnostic))]
    for field in fields:
        if field not in root:
            continue
        for value in _coordinate_values(root, field, diagnostic):
            try:
                candidate = _safe_variant(root, **{field: value})
            except (TypeError, ValueError):
                continue
            variant_id = rule_variant_id(candidate)
            if (variant_id in seen or
                    rule_semantic_signature(candidate) == root_signature):
                continue
            delta = spec_delta(root, candidate)
            if len(delta) != 1:
                continue
            seen.add(variant_id)
            variants.append((candidate,
                             mutation_reason(root, candidate, diagnostic)))
    preferred = _family_exit_hypothesis(root)
    if preferred is not None:
        preferred_id = rule_variant_id(preferred)
        for index, (candidate, _reason) in enumerate(variants[1:], start=1):
            if rule_variant_id(candidate) == preferred_id:
                variants.insert(1, variants.pop(index))
                break
    return variants


def _measured_axis_values(
        root: Mapping[str, Any], lessons: Sequence[Mapping[str, Any]],
        field: str) -> list[tuple[float, str]]:
    """Return validated, deterministic values measured for one coordinate."""
    values: dict[float, str] = {}
    for lesson in lessons:
        if not isinstance(lesson, Mapping):
            continue
        changed = lesson.get("tried") or lesson.get("changed") or {}
        if not isinstance(changed, Mapping) or len(changed) != 1:
            continue
        change = changed.get(field)
        if not isinstance(change, Mapping) or "to" not in change:
            continue
        try:
            value = float(change["to"])
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(value) or value == float(root.get(field)):
            continue
        try:
            candidate = _safe_variant(root, **{field: value})
        except (TypeError, ValueError):
            continue
        if float(candidate.get(field)) != value:
            continue
        lesson_id = str(lesson.get("id") or lesson.get("lesson_id") or "")
        # Keep the lexicographically first durable lesson for a measured
        # value. Numeric ordering below is independent of database or
        # provider sequence and keeps neutral lessons deterministic.
        previous = values.get(value)
        if previous is None or lesson_id < previous:
            values[value] = lesson_id
    return sorted(values.items(), key=lambda item: (item[0], item[1]))


def _blocked_stress_pair(
        root: Mapping[str, Any], lessons: Sequence[Mapping[str, Any]], *,
        diagnostic: Mapping[str, Any] | None,
        risk_config: Mapping[str, Any] | None,
        coordinate_exhausted: bool,
        ) -> tuple[dict, str] | None:
    """Choose one measured ATR interaction for an exhausted blocked fit."""
    if (not coordinate_exhausted or
            not isinstance(diagnostic, Mapping) or
            str(diagnostic.get("primary_failure") or "") !=
            "execution_blocked"):
        return None
    if "min_atr_bps" not in root or "stop_atr" not in root:
        return None
    mins = _measured_axis_values(root, lessons, "min_atr_bps")
    stops = _measured_axis_values(root, lessons, "stop_atr")
    if not mins or not stops:
        return None
    pairs = [(minimum, stop, minimum_id, stop_id)
             for minimum, minimum_id in mins
             for stop, stop_id in stops]
    controls = None
    if isinstance(risk_config, Mapping):
        scenario = risk_config.get("stressed_cost_scenario_bps")
        limit = risk_config.get("max_stressed_cost_to_risk_ratio")
        if not isinstance(scenario, bool) and not isinstance(limit, bool):
            try:
                scenario_value = float(scenario)
                limit_value = float(limit)
            except (TypeError, ValueError, OverflowError):
                pass
            else:
                if (math.isfinite(scenario_value) and
                        math.isfinite(limit_value) and
                        scenario_value >= 0.0 and limit_value > 0.0):
                    controls = (scenario_value, limit_value)
    chosen = None
    if controls is not None:
        scenario, limit = controls
        required_product = scenario / limit
        chosen = next((pair for pair in pairs
                       if pair[0] * pair[1] >= required_product), None)
    if chosen is None and controls is None:
        # This is a preference only. Values are still required to have been
        # measured and validated above; no stress or risk value is invented.
        chosen = next((pair for pair in pairs
                       if math.isclose(pair[0], 15.0, rel_tol=0.0,
                                       abs_tol=1e-12) and
                       math.isclose(pair[1], 6.0, rel_tol=0.0,
                                    abs_tol=1e-12)), None)
    if chosen is None:
        # A configured geometry with no clearing pair remains a bounded,
        # measured experiment; selecting its first deterministic pair does
        # not claim that execution will pass.
        chosen = pairs[0]
    minimum, stop, minimum_id, stop_id = chosen
    try:
        candidate = _safe_variant(root, min_atr_bps=minimum, stop_atr=stop)
    except (TypeError, ValueError):
        return None
    if len(spec_delta(root, candidate)) != 2:
        return None
    reason = (
        "Bounded execution-blocked ATR interaction: min_atr_bps from lesson "
        f"{minimum_id or 'recorded'}; stop_atr from lesson "
        f"{stop_id or 'recorded'}."
    )[:_REASON_LIMIT]
    return candidate, reason


def interaction_mutation_pool(
        spec: Mapping[str, Any], lessons: Sequence[Mapping[str, Any]], *,
        limit: int = 12, diagnostic: Mapping[str, Any] | None = None,
        risk_config: Mapping[str, Any] | None = None,
        coordinate_exhausted: bool = True) -> list[tuple[dict, str]]:
    """Combine only the strongest previously measured one-factor changes.

    Interaction search is unavailable until coordinate lessons exist.  It
    pairs distinct fields, keeps each authored value exactly as measured, and
    never changes family or grammar.  The result therefore preserves the good
    parts of the best near-misses without opening an unconstrained grid.
    """
    root = validate_rule_spec(spec)
    ranked: list[tuple[float, str, Any, str]] = []
    for lesson in lessons:
        changed = lesson.get("tried") or lesson.get("changed") or {}
        if not isinstance(changed, Mapping) or len(changed) != 1:
            continue
        field, change = next(iter(changed.items()))
        if field in {"family", "schema"} or not isinstance(change, Mapping):
            continue
        if "to" not in change:
            continue
        try:
            score = float(lesson.get("heldout_delta",
                                     lesson.get("fit_delta")))
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(score):
            continue
        ranked.append((score, str(field), change["to"],
                       str(lesson.get("id") or lesson.get("lesson_id") or "")))
    # Retain one best measured value per field.  Positive relative improvements
    # rank first, while the closest negative near-misses remain usable when no
    # coordinate helped enough on its own.
    # Ties are resolved by the diagnosed failure's field priority, then by
    # the numeric value itself (never its string rendering, where ``10``
    # would sort before ``5``).  This keeps a generic interaction batch
    # deterministic while still spending its bounded allowance on the axes
    # most likely to address the observed failure.
    failure = str((diagnostic or {}).get("primary_failure") or "")
    priority = {field: index for index, field in enumerate(
        _FAILURE_FIELD_PRIORITY.get(failure, ())) }

    def value_key(value: Any) -> tuple[int, Any]:
        if isinstance(value, bool):
            return (1, str(value))
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            return (1, str(value))
        return ((0, numeric) if math.isfinite(numeric)
                else (1, str(value)))

    def lesson_key(item: tuple[float, str, Any, str]) -> tuple[Any, ...]:
        score, field, value, lesson_id = item
        return (-score, priority.get(field, len(priority)), field,
                value_key(value), lesson_id)

    best: dict[str, tuple[float, Any, str]] = {}
    for score, field, value, lesson_id in sorted(
            ranked, key=lesson_key):
        best.setdefault(field, (score, value, lesson_id))
    selected = sorted(
        best.items(),
        key=lambda item: (-item[1][0], priority.get(item[0], len(priority)),
                          item[0], value_key(item[1][1]), item[1][2]))[:6]
    root_signature = rule_semantic_signature(root)
    seen = {rule_variant_id(root)}
    variants: list[tuple[dict, str]] = []
    geometry_pair = _target_hold_geometry_pair(
        root, diagnostic, lessons, coordinate_exhausted=coordinate_exhausted)
    geometry_pair_id = (rule_variant_id(geometry_pair[0])
                        if geometry_pair is not None else None)
    if geometry_pair is not None:
        variants.append(geometry_pair)
        seen.add(geometry_pair_id)
        if len(variants) >= max(0, int(limit)):
            return variants[:max(0, int(limit))]
    blocked_pair = _blocked_stress_pair(
        root, lessons, diagnostic=diagnostic,
        risk_config=risk_config, coordinate_exhausted=coordinate_exhausted)
    blocked_pair_id = (rule_variant_id(blocked_pair[0])
                       if blocked_pair is not None else None)
    blocked_execution = (
        isinstance(diagnostic, Mapping) and
        str(diagnostic.get("primary_failure") or "") == "execution_blocked")
    for left_index, (left, (_ls, left_value, left_lesson)) in enumerate(selected):
        for right, (_rs, right_value, right_lesson) in selected[left_index + 1:]:
            try:
                candidate = _safe_variant(
                    root, **{left: left_value, right: right_value})
            except (TypeError, ValueError):
                continue
            variant_id = rule_variant_id(candidate)
            if (variant_id in seen or
                    rule_semantic_signature(candidate) == root_signature or
                    len(spec_delta(root, candidate)) != 2):
                continue
            if geometry_pair is not None and {left, right} == {
                    "target_r", "max_hold_bars"}:
                # The dedicated geometry selector owns this pair and emits at
                # most one recommendation.
                continue
            if ((blocked_pair is not None or blocked_execution) and
                    {left, right} == {"min_atr_bps", "stop_atr"}):
                # Keep exactly one measured ATR interaction; the dedicated
                # selector below chooses the geometry-aware member.
                continue
            seen.add(variant_id)
            reason = (
                f"Bounded interaction after coordinate evidence: {left} from "
                f"lesson {left_lesson or 'recorded'}; {right} from lesson "
                f"{right_lesson or 'recorded'}.")[:_REASON_LIMIT]
            variants.append((candidate, reason))
            if blocked_pair is None and len(variants) >= max(0, int(limit)):
                return variants
    if blocked_pair is None:
        return variants[:max(0, int(limit))]
    # The dedicated pair is first so a bounded interaction batch cannot spend
    # its entire allowance on weaker generic combinations. Trim only after
    # inserting it, preserving the existing pool cap and identity semantics.
    variants = ([blocked_pair] + [item for item in variants
                                  if rule_variant_id(item[0]) not in {
                                      blocked_pair_id, geometry_pair_id}])
    if geometry_pair is not None:
        variants = [geometry_pair] + [item for item in variants
                                      if rule_variant_id(item[0]) != geometry_pair_id]
    return variants[:max(0, int(limit))]


def mutate_with_reasons(spec: Mapping[str, Any], diagnostic: Mapping[str, Any],
                        count: int = DEFAULT_VARIANTS
                        ) -> list[tuple[dict, str]]:
    """First batch of the bounded one-factor neighborhood."""
    if not 2 <= int(count) <= MAX_VARIANTS:
        raise FactoryError(f"variants must be between 2 and {MAX_VARIANTS}")
    variants = coordinate_mutation_pool(spec, diagnostic)[:int(count)]
    if len(variants) != int(count):
        raise FactoryError("could not form the requested number of unique variants")
    return variants


# Deterministic discovery ladders.  These are the offline fallback for seeding
# a free slot, and they deliberately reach into the conditional grammar:
# equity traverses v3 breakeven coordinates while options stay on executable
# v2.  Without these axes the only structure a slot could ever explore is
# "another family at template defaults", which made the search terminate.
_DISCOVERY_WINDOWS: tuple[tuple[int, int], ...] = (
    (0, SESSION_MINUTES), (0, 120), (30, 210), (120, 330), (240, SESSION_MINUTES))
_DISCOVERY_CONFIRMATIONS: tuple[tuple[str, ...], ...] = (
    (), ("trend",), ("volume",), ("volatility",), ("trend", "volume"))
_DISCOVERY_BANDS: tuple[tuple[float, float], ...] = (
    (0.0, 5_000.0), (0.0, 60.0), (25.0, 120.0), (60.0, 5_000.0))
_DISCOVERY_BREAKEVEN_FRACTIONS: tuple[float, ...] = (0.0, .25, .5, .75)
# (side, target_r, stop_atr, max_hold_bars) — the payoff shape the conditional
# entry is expressed with.
_DISCOVERY_SHAPES: tuple[tuple[str, float, float, int], ...] = (
    # Cover the complete audited payoff span, including low-risk-unit roots
    # whose default stop/target pair otherwise leaves no economic signal.
    ("both", .25, .2, 1), ("both", .5, .5, 10),
    ("both", 1.0, .75, 30), ("both", 1.25, .75, 45),
    ("both", 2.0, 1.0, 90), ("both", 3.0, 1.5, 180),
    ("both", 5.0, 2.0, 240), ("both", 10.0, 4.0, 390),
    ("both", 10.0, 10.0, 390),
    ("long", 2.0, 1.0, 60), ("short", 2.0, 1.0, 60),
    ("long", 10.0, 10.0, 390), ("short", 10.0, 10.0, 390),
)
# One complete Cartesian traversal is the bounded search contract.  Derive the
# cap from the declared dimensions so adding an axis cannot silently make the
# tail unreachable.
MAX_DISCOVERY_ATTEMPTS = (
    len(_DISCOVERY_WINDOWS) * len(_DISCOVERY_CONFIRMATIONS) *
    len(_DISCOVERY_BANDS) * len(_DISCOVERY_SHAPES) *
    len(_DISCOVERY_BREAKEVEN_FRACTIONS))


def discovery_attempt_limit(vehicle: str = "equity") -> int:
    """Return one duplicate-free Cartesian traversal for a vehicle."""

    normalized = str(vehicle).lower()
    if normalized not in {"equity", "option"}:
        raise FactoryError("vehicle must be equity or option")
    base = (len(_DISCOVERY_WINDOWS) * len(_DISCOVERY_CONFIRMATIONS) *
            len(_DISCOVERY_BANDS) * len(_DISCOVERY_SHAPES))
    return (base * len(_DISCOVERY_BREAKEVEN_FRACTIONS)
            if normalized == "equity" else base)


def _target_hold_geometry_pair(
        root: Mapping[str, Any], diagnostic: Mapping[str, Any] | None,
        lessons: Sequence[Mapping[str, Any]] = (), *,
        coordinate_exhausted: bool = True,
        ) -> tuple[dict, str] | None:
    """Return at most one fit-only target/hold interaction.

    This selector is downstream of the ordinary coordinate neighborhood. It
    consumes only the compact reachability diagnostic and one-factor lesson
    coordinates; all values are checked against the finite discovery shapes
    and the rule validator before a new variant is emitted.
    """
    if not coordinate_exhausted or not isinstance(diagnostic, Mapping):
        return None
    section = diagnostic.get("target_hold_reachability")
    if not isinstance(section, Mapping):
        fit = diagnostic.get("fit_diagnostics")
        section = fit.get("target_hold_reachability") \
            if isinstance(fit, Mapping) else None
    if not isinstance(section, Mapping):
        return None
    if section.get("diagnostic_only") is not True or \
            section.get("authorizing") is True:
        return None
    if section.get("genuine_mismatch") is not True or \
            section.get("adequate") is not True:
        return None
    try:
        if int(section.get("usable")) < 30:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    recommendation = section.get("recommendation")
    if not isinstance(recommendation, Mapping):
        return None

    root = validate_rule_spec(root)
    allowed_targets = {round(float(shape[1]), 8)
                       for shape in _DISCOVERY_SHAPES}
    allowed_holds = {int(shape[3]) for shape in _DISCOVERY_SHAPES}
    # Lessons may add a value only when it is a validated one-factor change;
    # the candidate itself is validated again below.
    for lesson in lessons:
        if not isinstance(lesson, Mapping):
            continue
        changed = lesson.get("tried") or lesson.get("changed") or {}
        if not isinstance(changed, Mapping) or len(changed) != 1:
            continue
        field, change = next(iter(changed.items()))
        if field not in {"target_r", "max_hold_bars"} or \
                not isinstance(change, Mapping) or "to" not in change:
            continue
        try:
            value = (round(float(change["to"]), 8)
                     if field == "target_r" else int(change["to"]))
        except (TypeError, ValueError, OverflowError):
            continue
        try:
            validated = _safe_variant(root, **{field: value})
        except (TypeError, ValueError):
            continue
        if validated.get(field) == value:
            (allowed_targets if field == "target_r" else allowed_holds).add(value)

    try:
        target = round(float(recommendation.get("target_r")), 8)
        hold = int(recommendation.get("max_hold_bars"))
    except (TypeError, ValueError, OverflowError):
        return None
    if target not in allowed_targets or hold not in allowed_holds:
        return None
    try:
        candidate = _safe_variant(root, target_r=target, max_hold_bars=hold)
    except (TypeError, ValueError):
        return None
    delta = spec_delta(root, candidate)
    if not delta or len(delta) > 2 or set(delta) - {"target_r", "max_hold_bars"}:
        return None
    if rule_variant_id(candidate) == rule_variant_id(root):
        return None
    reason = (
        "Bounded target/hold geometry interaction after coordinate exhaustion: "
        "fit-only time-expiry/unreachable-target evidence selected "
        f"target_r={target:g}, max_hold_bars={hold}.")[:240]
    return candidate, reason


def discovery_spec(index: int, *, family: str,
                   vehicle: str = "equity") -> dict[str, Any]:
    """Return the deterministic *index*-th conditional variant of a family.

    The ladder dimensions have mixed lengths (5, 5, 4, 7 against a 12-family
    rotation). ``_DISCOVERY_SHAPES`` is the fastest-varying dimension so
    consecutive indices probe payoff geometry before repeating a window or
    confirmation predicate.
    """

    spec = family_template(family)
    if str(vehicle) == "equity":
        spec.update({"schema": RULE_SCHEMA_V4,
                     **V3_DEFAULT_EXTENSIONS,
                     **V4_DEFAULT_EXTENSIONS})
    if index <= 0:
        return validate_rule_spec(spec)
    windows, confirms = len(_DISCOVERY_WINDOWS), len(_DISCOVERY_CONFIRMATIONS)
    bands, shapes = len(_DISCOVERY_BANDS), len(_DISCOVERY_SHAPES)
    shape_index = index % shapes
    band_index = (index // shapes) % bands
    confirmation_index = (index // (shapes * bands)) % confirms
    window_index = (index // (shapes * bands * confirms)) % windows
    after, before = _DISCOVERY_WINDOWS[window_index]
    confirmations = _DISCOVERY_CONFIRMATIONS[confirmation_index]
    low, high = _DISCOVERY_BANDS[band_index]
    side, target_r, stop_atr, max_hold = _DISCOVERY_SHAPES[shape_index]
    breakeven_fraction = _DISCOVERY_BREAKEVEN_FRACTIONS[
        (index // (windows * confirms * bands * shapes)) %
        len(_DISCOVERY_BREAKEVEN_FRACTIONS)]
    spec.update({"schema": RULE_SCHEMA_V2, "entry_after_minutes": after,
                 "entry_before_minutes": before,
                 "confirmations": list(confirmations),
                 "min_atr_bps": low, "max_atr_bps": high,
                 "side": side, "target_r": target_r, "stop_atr": stop_atr,
                 "max_hold_bars": max_hold})
    if str(vehicle) == "equity":
        spec.update({
            "schema": RULE_SCHEMA_V4,
            "breakeven_r": round(target_r * breakeven_fraction, 8),
            **V4_DEFAULT_EXTENSIONS,
        })
    return validate_rule_spec(spec)


def discovery_hypothesis(previous: Mapping[str, Any], *, generation: int,
                         not_before: str | None,
                         existing_variant_ids: set[str],
                         tried_families: set[str]) -> StrategyHypothesis | None:
    """Seed a free slot with a new hypothesis the ledger has not tried.

    An untried family at its own template comes first, because that is the
    cheapest genuinely new shape.  Once a slot has seen every family, discovery
    continues into its executable conditional grammar instead of stopping: a
    slot that has run out of families has not run out of hypotheses.
    """

    vehicle = str(previous["vehicle"])
    slot = int(previous["slot"])
    current = str(previous["family"])
    start = RULE_FAMILIES.index(current) if current in RULE_FAMILIES else 0

    def build(spec: Mapping[str, Any]) -> StrategyHypothesis | None:
        if rule_variant_id(spec) in existing_variant_ids:
            return None
        return StrategyHypothesis(
            _hypothesis_id(vehicle, slot, generation, spec), slot, generation,
            vehicle, str(spec["family"]), _thesis(spec), _falsification(spec),
            dict(spec), str(previous["hypothesis_id"]), not_before)

    for offset in range(1, len(RULE_FAMILIES) + 1):
        family = RULE_FAMILIES[(start + offset) % len(RULE_FAMILIES)]
        if family in tried_families:
            continue
        seeded = build(discovery_spec(0, family=family, vehicle=vehicle))
        if seeded is not None:
            return seeded
    for index in range(1, discovery_attempt_limit(vehicle) + 1):
        family = RULE_FAMILIES[(start + index) % len(RULE_FAMILIES)]
        seeded = build(discovery_spec(index, family=family, vehicle=vehicle))
        if seeded is not None:
            return seeded
    return None


def replacement_hypothesis(previous: Mapping[str, Any], diagnostic: Mapping[str, Any],
                           *, max_generations: int,
                           not_before: str | None = None) -> StrategyHypothesis | None:
    generation = int(previous["generation"]) + 1
    if generation >= int(max_generations):
        return None
    slot = int(previous["slot"])
    current = str(previous["family"])
    offset = 2 if diagnostic.get("primary_failure") == "insufficient_signals" else 1
    family = RULE_FAMILIES[(RULE_FAMILIES.index(current) + generation + offset) % len(RULE_FAMILIES)]
    seed = dict(previous["rule_spec"])
    seed.update({
        "family": family,
        "confirmation": ("volume" if diagnostic.get("primary_failure") in
                         {"negative_expectancy", "low_win_rate"} else "trend"),
        "threshold_bps": min(500.0, max(0.0, float(seed["threshold_bps"]) + 3 * generation)),
        "lookback": min(120, max(3, int(seed["lookback"]) + generation)),
    })
    seed["slow_lookback"] = max(int(seed["slow_lookback"]), int(seed["lookback"]) + 5)
    spec = validate_rule_spec(seed)
    vehicle = str(previous["vehicle"])
    hid = _hypothesis_id(vehicle, slot, generation, spec)
    return StrategyHypothesis(
        hid, slot, generation, vehicle, family, _thesis(spec), _falsification(spec), spec,
        str(previous["hypothesis_id"]), not_before,
    )
