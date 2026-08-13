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
    feature_window_bars, hold_deadline, rule_variant_id, validate_rule_spec,
)
from .costs import BAR, QUOTE, CostModel, ReplayPolicy, index_quotes, quote_fill
from .edge_ledger import content_hash
from .factory_ledger import FactoryError
from .gates import max_drawdown_of
from .market_data import (OptionSnapshot, QuoteSnapshot, UnderlyingBar,
                          option_has_liquidity)


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

DEFAULT_STRATEGIES = 7
DEFAULT_VARIANTS = 4
MAX_STRATEGIES = 7
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
    identity = getattr(row, "identity", None)
    return bool(identity is not None and identity.as_of <= cutoff)


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
              direction: str, reason: str, *, contract: str | None = None) -> dict:
    """Mark a real signal that has no honest fill price.

    Returning ``None`` here would make the observation indistinguishable from a
    session that never signalled, which deletes exactly the trades whose
    contract stopped being quoted — the least random subset there is.
    """
    return {"unpriced_reason": reason, "direction": direction, "contract": contract,
            "session_date": day.isoformat(),
            "signal_timestamp": signal_bar.end.isoformat(),
            "entry_timestamp": entry_bar.timestamp.isoformat()}


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
    # ``None`` means the family accumulates from the session open, so its
    # window starts at the session's first bar rather than a trailing offset.
    window = feature_window_bars(spec)
    for index in range(1, len(session_bars) - 1):
        signal_bar = session_bars[index]
        # Every feature prefix is point-in-time visible at the signal cutoff;
        # checking only the latest candle lets a corrected historical bar leak
        # into an otherwise valid-looking signal.
        if not _visible(signal_bar, signal_bar.end) or not all(
                _visible(item, signal_bar.end)
                for item in session_bars[:index + 1]):
            continue
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
        # "Next bar" means the immediate following one-minute bar.  Carrying a
        # signal across an outage would turn a stale breakout into an entry.
        if entry_bar.timestamp != signal_bar.end:
            continue
        # The completed bar record is not visible until its end, but its open
        # is the boundary observation used for next-bar entry.  Never consume
        # the entry bar's high/low/close before that bar ends; executable entry
        # pricing below still requires a point-in-time quote at this boundary.
        local_entry = entry_bar.timestamp.astimezone(ZoneInfo("America/New_York"))
        if (resolved_policy.latest_entry_time is not None and
                local_entry.time() > resolved_policy.latest_entry_time):
            continue
        if not _at_or_before_force_flat(entry_bar.timestamp, resolved_policy):
            continue
        direction = signal["direction"]
        entry_underlying = float(entry_bar.open)
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
        deadline = hold_deadline(entry_bar.timestamp, spec)
        last_index = index + 1
        for probe in range(index + 2, len(session_bars)):
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
            for bar in session_bars[index + 1:last_index + 1]:
                if not _visible(bar, bar.end):
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
        entry_ref = entry_underlying
        entry_source = exit_source = BAR
        if vehicle == "equity":
            # A fill that lands on a bar boundary has a real instant, so a
            # recorded quote is its executable price.  A level-triggered exit
            # inside a bar has no such instant and keeps the bar's level.
            side = "buy" if direction == "long" else "sell"
            quoted = quote_fill(
                quotes, symbol=signal_bar.symbol, at=entry_bar.timestamp,
                side=side,
                max_age_seconds=resolved_policy.max_market_data_age_seconds,
                session_date=day)
            if quoted is not None:
                entry_ref, entry_source = quoted, QUOTE
            elif resolved_policy.strict_market_data:
                return _unpriced(signal_bar, entry_bar, day, direction,
                                 "no fresh equity quote at entry")
            if reason == "time" or gapped or exit_gapped:
                quoted_exit = quote_fill(
                    quotes, symbol=signal_bar.symbol, at=pricing_cutoff,
                    side="sell" if direction == "long" else "buy",
                    max_age_seconds=resolved_policy.max_market_data_age_seconds,
                    session_date=day)
                if quoted_exit is not None:
                    exit_ref, exit_source = quoted_exit, QUOTE
                elif resolved_policy.strict_market_data:
                    return _unpriced(signal_bar, entry_bar, day, direction,
                                     "no fresh equity quote at exit")
        entry_age = exit_age = 0.0
        if vehicle == "option":
            entry_snap = _option_at(snapshots, symbol=signal_bar.symbol, day=day,
                                    direction=direction, cutoff=entry_bar.timestamp,
                                    policy=resolved_policy)
            if entry_snap is None:
                return _unpriced(signal_bar, entry_bar, day, direction,
                                 "no option quote within staleness bound at entry")
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
                                 contract=entry_snap.contract.symbol)
            contract = entry_snap.contract.symbol
            entry_ref = entry_snap.ask
            exit_ref = exit_snap.bid
            multiplier = entry_snap.contract.multiplier
            # A snapshot inside the exit bar but after a level-triggered instant
            # is not stale, it is simply the bar's quote; only genuinely older
            # quotes carry an age.
            entry_age = max(0.0, (entry_bar.timestamp - entry_snap.timestamp).total_seconds())
            exit_age = max(0.0, (pricing_cutoff - exit_snap.timestamp).total_seconds())
        return {
            "vehicle": vehicle, "symbol": signal_bar.symbol,
            "session_date": day.isoformat(), "direction": direction,
            "signal_timestamp": signal_bar.end.isoformat(),
            "entry_timestamp": entry_bar.timestamp.isoformat(),
            # A gap exit happens at the bar open; an intrabar level exit is
            # conservatively represented at the completed bar cutoff used for
            # its bar-level price.  Never imply a quote after the trigger.
            "exit_timestamp": exit_at.isoformat(),
            "entry_reference": entry_ref, "exit_reference": exit_ref,
            "underlying_entry": entry_underlying, "stop_price": stop,
            "target_price": target, "exit_reason": reason, "tie_broken": tie,
            "contract": contract, "contract_multiplier": multiplier,
            "stop_distance": distance, "entry_gap_fill": gapped,
            "exit_gap_fill": exit_gapped,
            "entry_fill_source": entry_source, "exit_fill_source": exit_source,
            "entry_quote_age_seconds": entry_age,
            "exit_quote_age_seconds": exit_age,
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
        if row.timestamp == cutoff:
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


def mutate_with_reasons(spec: Mapping[str, Any], diagnostic: Mapping[str, Any],
                        count: int = DEFAULT_VARIANTS
                        ) -> list[tuple[dict, str]]:
    """Bounded variants from an explicit failure diagnosis, each with its reason."""
    if not 2 <= int(count) <= MAX_VARIANTS:
        raise FactoryError(f"variants must be between 2 and {MAX_VARIANTS}")
    root = validate_rule_spec(spec)
    failure = str(diagnostic.get("primary_failure") or "none")
    changes: list[dict] = []
    if failure == "insufficient_signals":
        changes = [
            {"threshold_bps": max(0.0, root["threshold_bps"] * .65),
             "zscore": max(.25, root["zscore"] * .8), "confirmation": "none"},
            {"lookback": max(3, root["lookback"] - 3),
             "slow_lookback": max(root["lookback"] + 2, root["slow_lookback"] - 5)},
            {"volume_multiplier": max(.25, root["volume_multiplier"] * .8),
             "range_minutes": max(3, root["range_minutes"] - 5)},
        ]
    elif failure in {"negative_expectancy", "poor_payoff"}:
        changes = [
            {"threshold_bps": min(500.0, root["threshold_bps"] * 1.35 + 1),
             "confirmation": "trend"},
            {"target_r": max(.25, root["target_r"] * .8),
             "stop_atr": min(10.0, root["stop_atr"] * 1.15)},
            {"volume_multiplier": min(10.0, root["volume_multiplier"] * 1.25),
             "confirmation": "volume"},
        ]
    elif failure == "low_win_rate":
        changes = [
            {"target_r": max(.25, root["target_r"] * .7)},
            {"threshold_bps": min(500.0, root["threshold_bps"] * 1.5 + 1),
             "confirmation": "trend"},
            {"max_hold_bars": max(1, int(root["max_hold_bars"] * .65)),
             "confirmation": "volume"},
        ]
    elif failure == "excess_drawdown":
        changes = [
            {"threshold_bps": min(500.0, root["threshold_bps"] * 1.5 + 1),
             "confirmation": "trend"},
            {"stop_atr": max(.2, root["stop_atr"] * .8),
             "max_hold_bars": max(1, int(root["max_hold_bars"] * .7))},
            {"side": "long", "confirmation": "volume"},
        ]
    else:
        changes = [
            {"target_r": min(10.0, root["target_r"] * 1.25)},
            {"threshold_bps": min(500.0, root["threshold_bps"] + 5),
             "confirmation": "trend"},
            {"lookback": min(120, root["lookback"] + 5),
             "slow_lookback": min(240, max(root["slow_lookback"] + 8,
                                             root["lookback"] + 6))},
        ]
    variants: list[tuple[dict, str]] = [
        (root, mutation_reason(root, root, diagnostic))]
    seen = {rule_variant_id(root)}
    for change in changes:
        if len(variants) >= int(count):
            break
        try:
            candidate = _safe_variant(root, **change)
        except ValueError:
            continue
        if rule_variant_id(candidate) not in seen:
            seen.add(rule_variant_id(candidate))
            variants.append((candidate, mutation_reason(root, candidate, diagnostic)))
    attempt = 0
    while len(variants) < int(count) and attempt < 64:
        attempt += 1
        lookback = 3 + ((int(root["lookback"]) - 3 + attempt) % 118)
        candidate = _safe_variant(
            root,
            lookback=lookback,
            slow_lookback=max(lookback + 2,
                              min(240, int(root["slow_lookback"]) + attempt)),
            threshold_bps=(float(root["threshold_bps"]) + attempt * 7.0) % 500.0,
            target_r=.25 + ((float(root["target_r"]) - .25 + attempt * .25) % 9.75),
        )
        if rule_variant_id(candidate) not in seen:
            seen.add(rule_variant_id(candidate))
            variants.append((candidate,
                             mutation_reason(root, candidate, diagnostic,
                                             swept=True)))
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
    ("both", 2.0, 1.0, 90), ("both", 1.25, 0.75, 30), ("both", 3.0, 1.5, 180),
    ("long", 2.0, 1.0, 60), ("short", 2.0, 1.0, 60), ("both", 1.5, 2.0, 45),
    ("both", 4.0, 1.0, 240),
)
# One complete Cartesian traversal is the bounded search contract.  Derive the
# cap from the declared dimensions so adding an axis cannot silently make the
# tail unreachable.
MAX_DISCOVERY_ATTEMPTS = (
    len(_DISCOVERY_WINDOWS) * len(_DISCOVERY_CONFIRMATIONS) *
    len(_DISCOVERY_BANDS) * len(_DISCOVERY_SHAPES))


def discovery_spec(index: int, *, family: str) -> dict[str, Any]:
    """Return the deterministic *index*-th conditional variant of a family.

    The ladder dimensions have pairwise-coprime lengths (5, 5, 4, 7 against a
    7-family rotation) so consecutive indices vary several axes at once rather
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
