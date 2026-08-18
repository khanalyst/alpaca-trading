"""Deterministic, behavior-preserving strategy-factory primitives.

This module contains the bounded hypothesis catalog, deterministic simulation,
diagnostics, and mutation/replacement logic used by the strategy-factory
orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import math
from statistics import mean
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from agent.contracts.rule import (
    RULE_FAMILIES, RULE_SCHEMA_V2, SESSION_MINUTES, evaluate_rule_signal,
    feature_window_bars, hold_deadline, rule_semantic_signature,
    rule_variant_id, validate_rule_spec,
)
from .costs import (BAR, QUOTE, CostError, CostModel, ReplayPolicy,
                    index_quotes, quote_fill, quote_fill_record,
                    replay_policy_for_bars)
from .edge_ledger import content_hash
from .factory_ledger import FactoryError
from .gates import max_drawdown_of
from .market_data import (OptionSnapshot, QuoteSnapshot, UnderlyingBar,
                          option_has_liquidity, record_available_at,
                          record_is_available)


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

DEFAULT_STRATEGIES = len(RULE_FAMILIES)
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
    return f"A completed-bar {family}{suffix} has positive expectancy after executable costs."


def _falsification(spec: Mapping[str, Any]) -> str:
    return (
        f"The {str(spec['family']).replace('_', ' ')} rule has no positive "
        "held-out and forward expectancy after costs and multiple-test correction."
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
)


def family_template(family: str) -> dict[str, Any]:
    for template in FAMILY_TEMPLATES:
        if template["family"] == family:
            return dict(template)
    raise FactoryError(f"unknown rule family: {family!r}")


def template_hypothesis(slot: int, *, vehicle: str = "equity",
                        generation: int = 0) -> StrategyHypothesis:
    """Return the generation-zero hypothesis a slot starts from."""
    if not 0 <= int(slot) < MAX_STRATEGIES:
        raise FactoryError(f"slot must be between 0 and {MAX_STRATEGIES - 1}")
    spec = validate_rule_spec(dict(FAMILY_TEMPLATES[int(slot)]))
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


def _visible(row: Any, cutoff: datetime) -> bool:
    return record_is_available(row, cutoff)


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
                and _visible(snap, cutoff) and snap.bid > 0 and snap.ask > 0
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


def _unpriced(signal_bar: UnderlyingBar, entry_bar: UnderlyingBar, day: date,
              direction: str, reason: str, *, contract: str | None = None,
              decision_timestamp: datetime | None = None,
              entry_timestamp: datetime | None = None) -> dict:
    """Mark a real signal that has no honest fill price.

    Returning ``None`` here would make the observation indistinguishable from a
    session that never signalled, which deletes exactly the trades whose
    contract stopped being quoted — the least random subset there is.
    """
    return {"unpriced_reason": reason, "direction": direction, "contract": contract,
            "session_date": day.isoformat(),
            "signal_bar_feed": signal_bar.feed,
            "signal_bar_provider": signal_bar.provider,
            "entry_bar_feed": entry_bar.feed,
            "entry_bar_provider": entry_bar.provider,
            "signal_timestamp": signal_bar.end.isoformat(),
            "decision_timestamp": ((decision_timestamp or signal_bar.end).isoformat()),
            "entry_timestamp": ((entry_timestamp or entry_bar.timestamp).isoformat())}


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


def _at_or_before_force_flat(timestamp: datetime, policy: ReplayPolicy) -> bool:
    if policy.force_flat_time is None:
        return True
    local = timestamp.astimezone(ZoneInfo("America/New_York"))
    return local.time() < policy.force_flat_time


def _simulate_trade(session_bars: Sequence[UnderlyingBar], spec: Mapping[str, Any],
                    snapshots: Sequence[OptionSnapshot], vehicle: str,
                    quotes: Mapping[str, Sequence[QuoteSnapshot]] | None = None,
                    policy: ReplayPolicy | Mapping[str, Any] | None = None) -> dict | None:
    resolved_policy = _coerce_policy(policy)
    if not _session_bars_valid(session_bars):
        return None
    try:
        resolved_policy = replay_policy_for_bars(
            resolved_policy, session_bars,
            session_date=(session_bars[0].session_date if session_bars else None))
    except CostError as exc:
        return _unpriced(session_bars[0], session_bars[0],
                         session_bars[0].session_date,
                         "unknown", str(exc))
    metadata = (session_bars[0].session_open, session_bars[0].session_close)
    if metadata[0] is not None and metadata[1] is not None:
        if any(bar.timestamp < metadata[0] or bar.end > metadata[1]
               for bar in session_bars):
            return _unpriced(session_bars[0], session_bars[0],
                             session_bars[0].session_date, "unknown",
                             "bar_outside_exact_session")
    # ``None`` means the family accumulates from the session open, so its
    # window starts at the session's first bar rather than a trailing offset.
    window = feature_window_bars(spec)
    for index in range(1, len(session_bars) - 1):
        signal_bar = session_bars[index]
        # The bars a signal is computed from must be consecutive: a gap inside
        # the feature window stretches a fixed lookback across an outage and
        # silently evaluates a different statistic than the spec names.
        feature_start = (0 if window is None
                         else max(0, index + 1 - int(window)))
        if not _contiguous(session_bars, feature_start, index + 1):
            continue
        signal = evaluate_rule_signal(session_bars[:index + 1], spec)
        if signal is None:
            continue
        entry_bar = session_bars[index + 1]
        available = [record_available_at(item)
                     for item in session_bars[feature_start:index + 1]
                     if record_available_at(item) is not None]
        signal_ready = max([signal_bar.end, *available], default=None)
        if signal_ready is None:
            continue
        boundary = signal_bar.end
        entry_at = boundary if signal_ready <= boundary else signal_ready
        # Keep the immediate next-bar contract when the signal was actionable
        # at the boundary.  A delayed signal can legitimately enter on the
        # first full bar after an outage because its decision did not exist at
        # the missing next-bar boundary.
        if entry_bar.timestamp != boundary and signal_ready <= boundary:
            continue
        entry_index = next((probe for probe in range(index + 1, len(session_bars))
                            if session_bars[probe].timestamp >= entry_at), None)
        if entry_index is None:
            continue
        entry_bar = session_bars[entry_index]
        # The completed bar record is not visible until its end, but its open
        # is the boundary observation used for next-bar entry.  Never consume
        # the entry bar's high/low/close before that bar ends; executable entry
        # pricing below still requires a point-in-time quote at this boundary.
        local_entry = entry_at.astimezone(ZoneInfo("America/New_York"))
        if (resolved_policy.latest_entry_time is not None and
                local_entry.time() > resolved_policy.latest_entry_time):
            continue
        if not _at_or_before_force_flat(entry_at, resolved_policy):
            continue
        direction = signal["direction"]
        # A completed recorder bar is normally observed at its end, so it
        # cannot authorize the opening print at the entry boundary.  A fresh
        # executable quote is an independent boundary observation and is used
        # as the fill/underlying anchor when available.  Bar fallback remains
        # explicit and therefore requires the bar itself to be visible at its
        # timestamp; delayed OHLC never becomes an entry by hindsight.
        entry_bar_visible = _visible(entry_bar, entry_bar.timestamp)
        entry_underlying: float | None = (
            float(entry_bar.open) if entry_bar_visible else None)
        entry_ref: float | None = entry_underlying
        entry_source = BAR
        entry_feed = entry_provider = None
        entry_age = 0.0
        entry_snap: OptionSnapshot | None = None
        if vehicle == "equity":
            quoted = quote_fill_record(
                quotes, symbol=signal_bar.symbol, at=entry_at,
                side="buy" if direction == "long" else "sell",
                max_age_seconds=resolved_policy.max_market_data_age_seconds,
                session_date=signal_bar.session_date)
            if quoted is not None:
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
                                 entry_timestamp=entry_at)
            elif not entry_bar_visible:
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
                                 entry_timestamp=entry_at)
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
                                 entry_timestamp=entry_at)
            entry_ref = entry_snap.ask
            entry_source = QUOTE
            entry_feed = str(entry_snap.identity.feed)
            entry_provider = str(entry_snap.identity.provider)
            entry_age = max(
                0.0, (entry_at - entry_snap.timestamp).total_seconds())
        if entry_underlying is None or entry_ref is None:
            continue
        distance = float(signal["stop_distance"])
        # The runtime submits the bracket legs with the entry order, before any
        # fill exists, so the only anchor it can use is the signal bar's close.
        # Research must use the same levels and let the entry gap show up as
        # real sizing/R error rather than silently re-anchoring them.
        stop = float(signal["stop_price"])
        target = float(signal["target_price"])
        # Sizing reproduces `RiskEngine.size_shares`, which divides the budget by
        # this same nominal distance at plan time.  Accounting uses the distance
        # from the real fill to that stop, which is what the account actually
        # risked once the entry gapped.
        real_risk = max(0.0, entry_underlying - stop if direction == "long"
                        else stop - entry_underlying)
        deadline = hold_deadline(entry_at, spec)
        last_index = entry_index
        for probe in range(entry_index + 1, len(session_bars)):
            # The hold never crosses an outage.  Treating the next recorded
            # minute as adjacent would let a stop or target "trigger" on a bar
            # the position could not have been carried into; the position is
            # resolved on the last observed bar instead.
            if (session_bars[probe].timestamp -
                    session_bars[probe - 1].timestamp != timedelta(minutes=1)):
                break
            if session_bars[probe].end.timestamp() > deadline:
                break
            if not _at_or_before_force_flat(session_bars[probe].timestamp,
                                            resolved_policy):
                break
            last_index = probe
        exit_bar = session_bars[last_index]
        exit_ref = float(exit_bar.close)
        exit_at = exit_bar.end
        pricing_cutoff = exit_at
        reason = "time"
        tie = False
        gapped = False
        exit_gapped = False
        if direction == "long":
            through_stop = entry_underlying <= stop
            through_target = entry_underlying >= target
        else:
            through_stop = entry_underlying >= stop
            through_target = entry_underlying <= target
        if through_stop or through_target:
            # The entry gapped past one of its own levels.  The runtime cannot
            # see that gap when it sizes or prices the bracket, so it takes the
            # trade anyway and the resting leg triggers on arrival: a real fill
            # at the entry, never at the impossible better level.
            gapped = True
            reason = "stop" if through_stop else "target"
            exit_ref = entry_underlying
            exit_bar = entry_bar
            # The resting leg can execute at the entry boundary, but the
            # completed-bar replay only observes and records that outcome at
            # the bar close.  Pricing stays pinned to the earlier executable
            # instant so no later quote leaks into the fill.
            exit_at = entry_bar.end
            pricing_cutoff = entry_bar.timestamp
        else:
            # The scan starts at the entry bar, not the one after it.  Since
            # commit 11e87c8 the broker's bracket legs are live the moment the
            # entry fills, so a level touched later inside the entry bar does
            # execute.  Entry is the bar's open — its first instant — so the
            # whole of that bar's remaining range is after the entry and none
            # of it is lookahead; nothing before index+1 is ever examined, and
            # the signal itself still only saw bars[:index+1].  The intrabar
            # path is unknowable either way, so the established stop-wins-ties
            # rule resolves it against the strategy here too.
            for bar in session_bars[entry_index:last_index + 1]:
                if record_available_at(bar) is None:
                    # Resting exits consume completed OHLC once the record is
                    # observed, even when observation trails market end.
                    continue
                if direction == "long":
                    gap_stop, gap_target = bar.open <= stop, bar.open >= target
                    hit_stop, hit_target = bar.low <= stop, bar.high >= target
                else:
                    gap_stop, gap_target = bar.open >= stop, bar.open <= target
                    hit_stop, hit_target = bar.high >= stop, bar.low <= target
                if gap_stop or gap_target:
                    # A bar that opens beyond a resting leg fills at that open,
                    # not at the level the market never traded again.  Stop
                    # still wins the tie; the stop side makes results worse and
                    # that is exactly the point of modelling it.
                    reason = "stop" if gap_stop else "target"
                    exit_ref, exit_bar = float(bar.open), bar
                    exit_at, pricing_cutoff, exit_gapped = (
                        bar.end, bar.timestamp, True)
                    break
                if hit_stop or hit_target:
                    tie = hit_stop and hit_target
                    reason = "stop" if hit_stop else "target"
                    exit_ref = stop if hit_stop else target
                    exit_bar = bar
                    # Minute bars do not reveal the intrabar trigger instant.
                    # Price option exits only from information available at
                    # the bar open; a later quote from the same bar would be
                    # lookahead relative to an unknown trigger.
                    exit_at = bar.end
                    pricing_cutoff = bar.timestamp
                    break
        day = signal_bar.session_date
        multiplier = 1
        contract = None
        exit_source = BAR
        exit_feed = exit_provider = None
        exit_age = 0.0
        if vehicle == "equity":
            if reason == "time" or gapped or exit_gapped:
                quoted_exit = quote_fill_record(
                    quotes, symbol=signal_bar.symbol, at=pricing_cutoff,
                    side="sell" if direction == "long" else "buy",
                    max_age_seconds=resolved_policy.max_market_data_age_seconds,
                    session_date=day)
                if quoted_exit is not None:
                    exit_ref, exit_source = quoted_exit.price, QUOTE
                    exit_feed, exit_provider = quoted_exit.feed, quoted_exit.provider
                    exit_age = max(
                        0.0, (pricing_cutoff - quoted_exit.timestamp).total_seconds())
                elif resolved_policy.strict_market_data:
                    return _unpriced(signal_bar, entry_bar, day, direction,
                                     "no fresh equity quote at exit",
                                     decision_timestamp=signal_ready,
                                     entry_timestamp=entry_at)
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
                                 entry_timestamp=entry_at)
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
        return {
            "vehicle": vehicle, "symbol": signal_bar.symbol,
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
            "underlying_entry": entry_underlying, "stop_price": stop,
            "target_price": target, "exit_reason": reason, "tie_broken": tie,
            "contract": contract, "contract_multiplier": multiplier,
            "stop_distance": distance, "entry_gap_fill": gapped,
            "exit_gap_fill": exit_gapped,
            "entry_fill_source": entry_source, "exit_fill_source": exit_source,
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
            "plan_entry": float(signal["entry_price"]),
            "risk_per_unit": (entry_ref * multiplier if vehicle == "option"
                              else distance),
            # A long option's maximum loss is the premium actually paid, so its
            # nominal and realized risk are the same number.
            "realized_risk_per_unit": (entry_ref * multiplier
                                       if vehicle == "option" else real_risk),
        }
    return None


def _fresh(raw: Mapping[str, Any], leg: str,
           max_age_seconds: float | None = None) -> bool:
    limit = FRESH_OPTION_QUOTE_SECONDS if max_age_seconds is None else float(max_age_seconds)
    return float(raw.get(f"{leg}_quote_age_seconds") or 0.0) <= limit


def _visible_bar_mark(rows: Sequence[UnderlyingBar], cutoff: datetime) -> float | None:
    """Return the last bar price that was observable at ``cutoff``.

    A bar's open is the boundary observation used for an entry at its
    timestamp (the same convention used by :func:`_simulate_trade`).  A
    partial bar otherwise contributes nothing until its completed record is
    visible, so its close/high/low can never leak into an earlier mark.
    """
    for row in reversed(rows):
        if row.timestamp == cutoff and _visible(row, cutoff):
            return float(row.open)
        if row.end <= cutoff and _visible(row, row.end):
            return float(row.close)
    return None


def simulate_account(bars: Sequence[UnderlyingBar], snapshots: Sequence[OptionSnapshot],
                     spec: Mapping[str, Any], *, vehicle: str, account_id: str,
                     starting_cash: float = 100_000.0, risk_pct: float = .5,
                     costs: CostModel | None = None,
                     quotes: Sequence[QuoteSnapshot] | None = None,
                     policy: ReplayPolicy | Mapping[str, Any] | None = None) -> dict:
    """Replay one variant in an event-ordered isolated cash/equity book."""
    spec = validate_rule_spec(spec)
    model = costs or CostModel()
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
    bars_by_symbol: dict[str, list[UnderlyingBar]] = {}
    for bar in bars:
        bars_by_symbol.setdefault(str(bar.symbol).upper(), []).append(bar)
    for symbol_rows in bars_by_symbol.values():
        symbol_rows.sort(key=lambda item: item.timestamp)
    for (symbol, day), session_bars in sorted(
            grouped.items(), key=lambda item: (item[0][1], item[0][0])):
        opportunity = f"{rule_variant_id(spec)}:{vehicle}:{symbol}:{day.isoformat()}"
        raw = _simulate_trade(session_bars, spec, snapshots, vehicle,
                              quotes=quote_index, policy=resolved_policy)
        if raw is None or raw.get("unpriced_reason"):
            row = {"vehicle": vehicle, "symbol": symbol,
                   "session_date": day.isoformat(), "opportunity_id": opportunity,
                   "net_pnl": 0.0, "return_value": 0.0, "no_trade": True}
            if raw is not None:
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

    def realize_until(timestamp: datetime) -> None:
        nonlocal cash, peak, drawdown
        closing = [item for item in active
                   if datetime.fromisoformat(item["exit_timestamp"]) <= timestamp]
        for item in sorted(closing, key=lambda value: (
                value["exit_timestamp"], value["symbol"])):
            cash += float(item["net_pnl"])
            day_key = item["session_date"]
            realized_by_day[day_key] = realized_by_day.get(day_key, 0.0) + float(item["net_pnl"])
            peak = max(peak, cash)
            drawdown = max(drawdown, peak - cash)
            active.remove(item)

    def mark_active(timestamp: datetime) -> float:
        """Mark open positions from information visible at ``timestamp``.

        Equity marks prefer the executable side of a fresh recorded quote and
        fall back to the exact bar open/last completed close.  Long options
        use the visible bid for liquidation.  Closed rows are removed by
        ``realize_until`` before this function runs, so realized P&L and
        unrealized P&L cannot be counted twice.
        """
        unrealized = 0.0
        for item in active:
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
            else:
                side = "sell" if direction == "long" else "buy"
                mark = quote_fill(
                    quote_index, symbol=symbol, at=timestamp, side=side,
                    max_age_seconds=resolved_policy.max_market_data_age_seconds,
                    session_date=date.fromisoformat(str(item["session_date"])))
                if mark is None:
                    mark = _visible_bar_mark(bars_by_symbol.get(symbol, ()), timestamp)
            if mark is None:
                # No visible mark is a zero-move assumption, not permission to
                # inspect a future exit price.  Entry-side fees still reduce
                # equity immediately, as they do in the runtime account.
                mark = float(item["entry_price"])
            quantity = float(item.get("quantity", 0.0))
            multiplier = float(item.get("contract_multiplier", 1))
            entry = float(item["entry_price"])
            gross = ((mark - entry) if direction == "long" else
                     (entry - mark)) * quantity * multiplier
            entry_fees = model.fees(
                entry, entry, quantity, multiplier, vehicle=vehicle) / 2.0
            unrealized += gross - entry_fees
        return unrealized

    for raw in sorted(candidates, key=lambda item: (
            item["entry_timestamp"], item["_symbol"], item["_day"])):
        entry_at = datetime.fromisoformat(raw["entry_timestamp"])
        realize_until(entry_at)
        symbol, day, opportunity = raw["_symbol"], raw["_day"], raw["opportunity_id"]
        day_key = day.isoformat()
        day_start_equity.setdefault(day_key, cash)
        current_equity = cash + mark_active(entry_at)
        effective_risk_pct = float(resolved_policy.risk_per_trade_pct)
        risk_budget = max(0.0, current_equity * effective_risk_pct / 100.0)
        per_unit = max(float(raw["risk_per_unit"]), 1e-9)
        quantity = math.floor(risk_budget / per_unit)
        if vehicle == "equity":
            notional_pct = (NOTIONAL_CAP_PCT if
                            resolved_policy.max_position_notional_pct is None else
                            resolved_policy.max_position_notional_pct)
            quantity = min(quantity, math.floor(
                max(0.0, current_equity * float(notional_pct) / 100.0) /
                max(float(raw["plan_entry"]), 1e-9)))
        elif resolved_policy.max_position_notional_pct is not None:
            quantity = min(quantity, math.floor(
                max(0.0, current_equity * float(resolved_policy.max_position_notional_pct) / 100.0) /
                max(float(raw["entry_reference"]) * int(raw["contract_multiplier"]), 1e-9)))
        multiplier = int(raw["contract_multiplier"])
        risk_usd = quantity * float(raw["realized_risk_per_unit"])
        entry_notional = float(raw["entry_reference"]) * quantity * multiplier
        reject_reason = None
        if (resolved_policy.max_concurrent_positions is not None and
                len(active) >= resolved_policy.max_concurrent_positions):
            reject_reason = "max concurrent positions reached"
        elif (resolved_policy.max_gross_exposure_pct is not None and
              sum(float(item.get("entry_notional", 0.0)) for item in active) + entry_notional >
              current_equity * float(resolved_policy.max_gross_exposure_pct) / 100.0):
            reject_reason = "buying power/notional limit reached"
        elif (resolved_policy.max_open_risk_pct is not None and
              sum(float(item.get("risk_usd", 0.0)) for item in active) + risk_usd >
              current_equity * float(resolved_policy.max_open_risk_pct) / 100.0):
            reject_reason = "max open risk reached"
        elif (resolved_policy.daily_loss_limit_pct is not None and
              current_equity - day_start_equity[day_key] <=
              -day_start_equity[day_key] *
              float(resolved_policy.daily_loss_limit_pct) / 100.0):
            reject_reason = "daily loss limit reached"
        if quantity <= 0:
            reject_reason = reject_reason or "isolated account risk budget cannot fund one unit"
        if reject_reason:
            rows.append({"vehicle": vehicle, "symbol": symbol,
                         "session_date": day.isoformat(), "opportunity_id": opportunity,
                         "net_pnl": 0.0, "return_value": 0.0, "no_trade": True,
                         "reject_reason": reject_reason})
            continue
        execution_direction = "long" if vehicle == "option" else raw["direction"]
        entry = model.execution_price(
            raw["entry_reference"], execution_direction, entry=True,
            executable_quote=(vehicle == "option" and _fresh(
                raw, "entry", resolved_policy.max_market_data_age_seconds)) or
            raw.get("entry_fill_source") == QUOTE)
        exit_price = model.execution_price(
            raw["exit_reference"], execution_direction, entry=False,
            executable_quote=(vehicle == "option" and _fresh(
                raw, "exit", resolved_policy.max_market_data_age_seconds)) or
            raw.get("exit_fill_source") == QUOTE)
        gross = ((exit_price - entry) if execution_direction == "long" else
                 (entry - exit_price)) * quantity * multiplier
        fees = model.fees(entry, exit_price, quantity, multiplier, vehicle=vehicle)
        net = gross - fees
        row = {key: value for key, value in raw.items() if not key.startswith("_")}
        row.update({"quantity": quantity, "entry_price": entry, "exit_price": exit_price,
                    "gross_pnl": gross, "costs": fees, "net_pnl": net,
                    "risk_budget": risk_budget, "risk_usd": risk_usd,
                    "r_multiple": net / risk_usd if risk_usd > 0 else None,
                    "return_value": net / cash if cash > 0 else 0.0,
                    "no_trade": False, "entry_notional": entry_notional})
        rows.append(row)
        active.append(row)
    if active:
        realize_until(max(datetime.fromisoformat(item["exit_timestamp"]) for item in active))
    rows.sort(key=lambda row: (str(row.get("session_date", "")),
                               str(row.get("symbol", "")),
                               str(row.get("entry_timestamp", ""))))
    executed = [row for row in rows if row.get("no_trade") is not True]
    return {"account_id": account_id, "starting_cash": float(starting_cash),
            "ending_equity": cash, "realized_pnl": cash - float(starting_cash),
            "max_drawdown": drawdown, "trades": len(executed), "rows": rows}


def diagnose(rows: Sequence[Mapping], *, starting_cash: float = 100_000.0) -> dict:
    trades = [row for row in rows if row.get("no_trade") is not True]
    pnl = [float(row.get("net_pnl", 0.0)) for row in trades]
    wins = [value for value in pnl if value > 0]
    losses = [value for value in pnl if value < 0]
    sessions = {row.get("session_date") for row in rows}
    expectancy = mean(pnl) if pnl else 0.0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses else (float("inf") if wins else 0.0)
    drawdown = max_drawdown_of(rows)
    if len(trades) < max(3, len(sessions) // 3):
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
    return {
        "primary_failure": failure, "trades": len(trades),
        "sessions": len(sessions), "net_pnl": sum(pnl), "expectancy": expectancy,
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "profit_factor": profit_factor if math.isfinite(profit_factor) else 999.0,
        "max_drawdown": drawdown,
        "stop_rate": (sum(row.get("exit_reason") == "stop" for row in trades) / len(trades)
                      if trades else 0.0),
        "target_rate": (sum(row.get("exit_reason") == "target" for row in trades) / len(trades)
                        if trades else 0.0),
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
    "threshold_bps", "target_r", "stop_atr", "max_hold_bars",
    "lookback", "slow_lookback", "range_minutes", "zscore",
    "volume_multiplier", "compression_bps", "atr_period", "side",
    "confirmation", "entry_after_minutes", "entry_before_minutes",
    "min_atr_bps", "max_atr_bps", "confirmations",
)
_FAILURE_FIELD_PRIORITY = {
    "insufficient_signals": (
        "threshold_bps", "confirmation", "lookback", "range_minutes",
        "zscore", "volume_multiplier", "entry_before_minutes"),
    "negative_expectancy": (
        "threshold_bps", "target_r", "stop_atr", "max_hold_bars",
        "confirmation", "side"),
    "poor_payoff": (
        "target_r", "stop_atr", "max_hold_bars", "threshold_bps"),
    "low_win_rate": (
        "target_r", "threshold_bps", "max_hold_bars", "confirmation"),
    "excess_drawdown": (
        "stop_atr", "max_hold_bars", "side", "threshold_bps",
        "confirmation"),
}
_ZERO_AXIS_STEPS = {
    "threshold_bps": (5.0, 10.0),
    "min_atr_bps": (5.0, 15.0),
    "entry_after_minutes": (30, 60),
}


def _coordinate_values(root: Mapping[str, Any], field: str) -> list[Any]:
    """Return two bounded directions for one executable field.

    Validation remains the source of truth for bounds.  This helper merely
    proposes neighboring values; invalid boundary points are discarded by the
    same rule validator used for every other authored strategy.
    """
    value = root.get(field)
    if field == "side":
        return [item for item in ("both", "long", "short") if item != value]
    if field == "confirmation":
        return [item for item in ("none", "trend", "volume", "volatility")
                if item != value]
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
        for value in _coordinate_values(root, field):
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
    return variants


def interaction_mutation_pool(
        spec: Mapping[str, Any], lessons: Sequence[Mapping[str, Any]], *,
        limit: int = 12) -> list[tuple[dict, str]]:
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
            score = float(lesson.get("heldout_delta"))
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(score):
            continue
        ranked.append((score, str(field), change["to"],
                       str(lesson.get("id") or lesson.get("lesson_id") or "")))
    # Retain one best measured value per field.  Positive relative improvements
    # rank first, while the closest negative near-misses remain usable when no
    # coordinate helped enough on its own.
    best: dict[str, tuple[float, Any, str]] = {}
    for score, field, value, lesson_id in sorted(
            ranked, key=lambda item: (-item[0], item[1], str(item[2]))):
        best.setdefault(field, (score, value, lesson_id))
    selected = sorted(best.items(), key=lambda item: (-item[1][0], item[0]))[:6]
    root_signature = rule_semantic_signature(root)
    seen = {rule_variant_id(root)}
    variants: list[tuple[dict, str]] = []
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
            seen.add(variant_id)
            reason = (
                f"Bounded interaction after coordinate evidence: {left} from "
                f"lesson {left_lesson or 'recorded'}; {right} from lesson "
                f"{right_lesson or 'recorded'}.")[:_REASON_LIMIT]
            variants.append((candidate, reason))
            if len(variants) >= max(0, int(limit)):
                return variants
    return variants


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
# a free slot, and they deliberately reach into the v2 grammar: without them
# the only structure a slot could ever explore is "another family at template
# defaults", which is what made the search terminate in the first place.
_DISCOVERY_WINDOWS: tuple[tuple[int, int], ...] = (
    (0, SESSION_MINUTES), (0, 120), (30, 210), (120, 330), (240, SESSION_MINUTES))
_DISCOVERY_CONFIRMATIONS: tuple[tuple[str, ...], ...] = (
    (), ("trend",), ("volume",), ("volatility",), ("trend", "volume"))
_DISCOVERY_BANDS: tuple[tuple[float, float], ...] = (
    (0.0, 5_000.0), (0.0, 60.0), (25.0, 120.0), (60.0, 5_000.0))
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
    len(_DISCOVERY_BANDS) * len(_DISCOVERY_SHAPES))


def discovery_spec(index: int, *, family: str) -> dict[str, Any]:
    """Return the deterministic *index*-th conditional variant of a family.

    The ladder dimensions have mixed lengths (5, 5, 4, 7 against an 11-family
    rotation) so consecutive indices vary several axes at once rather
    than sweeping one and repeating.
    """

    spec = family_template(family)
    if index <= 0:
        return validate_rule_spec(spec)
    windows, confirms = len(_DISCOVERY_WINDOWS), len(_DISCOVERY_CONFIRMATIONS)
    bands, shapes = len(_DISCOVERY_BANDS), len(_DISCOVERY_SHAPES)
    after, before = _DISCOVERY_WINDOWS[index % windows]
    confirmations = _DISCOVERY_CONFIRMATIONS[(index // windows) % confirms]
    low, high = _DISCOVERY_BANDS[(index // (windows * confirms)) % bands]
    side, target_r, stop_atr, max_hold = _DISCOVERY_SHAPES[
        (index // (windows * confirms * bands)) % shapes]
    spec.update({"schema": RULE_SCHEMA_V2, "entry_after_minutes": after,
                 "entry_before_minutes": before,
                 "confirmations": list(confirmations),
                 "min_atr_bps": low, "max_atr_bps": high,
                 "side": side, "target_r": target_r, "stop_atr": stop_atr,
                 "max_hold_bars": max_hold})
    return validate_rule_spec(spec)


def discovery_hypothesis(previous: Mapping[str, Any], *, generation: int,
                         not_before: str | None,
                         existing_variant_ids: set[str],
                         tried_families: set[str]) -> StrategyHypothesis | None:
    """Seed a free slot with a new hypothesis the ledger has not tried.

    An untried family at its own template comes first, because that is the
    cheapest genuinely new shape.  Once a slot has seen every family, discovery
    continues into the conditional v2 grammar instead of stopping: a slot that
    has run out of families has not run out of hypotheses.
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
        seeded = build(validate_rule_spec(family_template(family)))
        if seeded is not None:
            return seeded
    for index in range(1, MAX_DISCOVERY_ATTEMPTS + 1):
        family = RULE_FAMILIES[(start + index) % len(RULE_FAMILIES)]
        seeded = build(discovery_spec(index, family=family))
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
