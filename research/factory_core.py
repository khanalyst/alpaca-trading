"""Deterministic, behavior-preserving strategy-factory primitives.

This module contains the bounded hypothesis catalog, deterministic simulation,
diagnostics, and mutation/replacement logic used by the strategy-factory
orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import math
from statistics import mean
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from agent.contracts.rule import (
    RULE_FAMILIES, RULE_SCHEMA_V2, SESSION_MINUTES, evaluate_rule_signal,
    hold_deadline, rule_variant_id, validate_rule_spec,
)
from .costs import BAR, QUOTE, CostModel, index_quotes, quote_fill
from .edge_ledger import content_hash
from .factory_ledger import FactoryError
from .gates import max_drawdown_of
from .market_data import OptionSnapshot, QuoteSnapshot, UnderlyingBar


# `risk.max_position_notional_pct` in the checked runtime config.  Research
# caps on the same percentage of the account, anchored to the same price.
NOTIONAL_CAP_PCT = 25.0

# The recorder samples on a 60s cycle (`deploy/recorder.py --interval`), so a
# snapshot within one cycle of the fill instant is the best quote that existed
# and is priced as executable.  Past that the quote is an approximation: it is
# still used, but charged the modelled half-spread instead of being trusted as
# a fill.  Beyond five cycles it is not a fill price at all and the observation
# is rejected explicitly rather than priced off a quote from another regime.
FRESH_OPTION_QUOTE_SECONDS = 60.0
MAX_OPTION_QUOTE_STALENESS_SECONDS = 300.0

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
               direction: str, cutoff: datetime, contract_symbol: str | None = None) -> OptionSnapshot | None:
    right = "call" if direction == "long" else "put"
    # A pinned exit lookup always has at least the entry snapshot available, so
    # without this bound the "no quote" branch is unreachable and a contract
    # that stopped being quoted at 10:05 silently prices a 15:30 exit off its
    # last morning bid.  Bounding staleness turns that fabrication back into a
    # visible rejection.
    floor = cutoff.timestamp() - MAX_OPTION_QUOTE_STALENESS_SECONDS
    eligible = [snap for snap in snapshots
                if snap.contract.underlying.upper() == symbol.upper()
                and snap.session_date == day and snap.timestamp <= cutoff
                and snap.timestamp.timestamp() >= floor
                and _visible(snap, cutoff) and snap.bid > 0 and snap.ask > 0
                and (contract_symbol is None
                     or snap.contract.symbol == contract_symbol)
                and (contract_symbol is not None or snap.contract.right.lower() == right)]
    if not eligible:
        return None
    if contract_symbol is not None:
        return max(eligible, key=lambda item: item.timestamp)
    spot = eligible[-1].underlying_price
    return min(eligible, key=lambda item: (
        abs(item.contract.strike - (spot or item.contract.strike)),
        (item.ask - item.bid) / item.ask, -item.timestamp.timestamp(),
        item.contract.symbol))


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


def _simulate_trade(session_bars: Sequence[UnderlyingBar], spec: Mapping[str, Any],
                    snapshots: Sequence[OptionSnapshot], vehicle: str,
                    quotes: Mapping[str, Sequence[QuoteSnapshot]] | None = None) -> dict | None:
    for index in range(1, len(session_bars) - 1):
        signal_bar = session_bars[index]
        if not _visible(signal_bar, signal_bar.end):
            continue
        signal = evaluate_rule_signal(session_bars[:index + 1], spec)
        if signal is None:
            continue
        entry_bar = session_bars[index + 1]
        if not _visible(entry_bar, entry_bar.end):
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
            if session_bars[probe].end.timestamp() > deadline:
                break
            last_index = probe
        exit_bar = session_bars[last_index]
        exit_ref = float(exit_bar.close)
        exit_at = exit_bar.end
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
            exit_at = entry_bar.timestamp
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
                    exit_at, exit_gapped = bar.timestamp, True
                    break
                if hit_stop or hit_target:
                    tie = hit_stop and hit_target
                    reason = "stop" if hit_stop else "target"
                    exit_ref = stop if hit_stop else target
                    exit_bar = bar
                    exit_at = bar.end
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
            quoted = quote_fill(quotes, symbol=signal_bar.symbol,
                                at=entry_bar.timestamp, side=side)
            if quoted is not None:
                entry_ref, entry_source = quoted, QUOTE
            if reason == "time" or gapped or exit_gapped:
                quoted_exit = quote_fill(
                    quotes, symbol=signal_bar.symbol, at=exit_at,
                    side="sell" if direction == "long" else "buy")
                if quoted_exit is not None:
                    exit_ref, exit_source = quoted_exit, QUOTE
        entry_age = exit_age = 0.0
        if vehicle == "option":
            entry_snap = _option_at(snapshots, symbol=signal_bar.symbol, day=day,
                                    direction=direction, cutoff=entry_bar.end)
            if entry_snap is None:
                return _unpriced(signal_bar, entry_bar, day, direction,
                                 "no option quote within staleness bound at entry")
            exit_snap = _option_at(snapshots, symbol=signal_bar.symbol, day=day,
                                   direction=direction, cutoff=exit_bar.end,
                                   contract_symbol=entry_snap.contract.symbol)
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
            entry_age = max(0.0, (entry_bar.end - entry_snap.timestamp).total_seconds())
            exit_age = max(0.0, (exit_at - exit_snap.timestamp).total_seconds())
        return {
            "vehicle": vehicle, "symbol": signal_bar.symbol,
            "session_date": day.isoformat(), "direction": direction,
            "signal_timestamp": signal_bar.end.isoformat(),
            "entry_timestamp": entry_bar.timestamp.isoformat(),
            "exit_timestamp": exit_bar.end.isoformat(),
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


def _fresh(raw: Mapping[str, Any], leg: str) -> bool:
    return float(raw.get(f"{leg}_quote_age_seconds") or 0.0) <= FRESH_OPTION_QUOTE_SECONDS


def simulate_account(bars: Sequence[UnderlyingBar], snapshots: Sequence[OptionSnapshot],
                     spec: Mapping[str, Any], *, vehicle: str, account_id: str,
                     starting_cash: float = 100_000.0, risk_pct: float = .5,
                     costs: CostModel | None = None,
                     quotes: Sequence[QuoteSnapshot] | None = None) -> dict:
    """Replay one variant in a completely isolated cash/equity book."""
    spec = validate_rule_spec(spec)
    model = costs or CostModel()
    quote_index = index_quotes(quotes)
    grouped: dict[tuple[str, date], list[UnderlyingBar]] = {}
    for bar in sorted(bars, key=lambda item: (item.timestamp, item.symbol)):
        grouped.setdefault((bar.symbol, bar.session_date), []).append(bar)
    cash = float(starting_cash)
    peak = cash
    drawdown = 0.0
    rows = []
    for (symbol, day), session_bars in sorted(grouped.items(), key=lambda item: (item[0][1], item[0][0])):
        opportunity = f"{rule_variant_id(spec)}:{vehicle}:{symbol}:{day.isoformat()}"
        raw = _simulate_trade(session_bars, spec, snapshots, vehicle,
                              quotes=quote_index)
        if raw is None or raw.get("unpriced_reason"):
            row = {"vehicle": vehicle, "symbol": symbol,
                   "session_date": day.isoformat(), "opportunity_id": opportunity,
                   "net_pnl": 0.0, "return_value": 0.0, "no_trade": True}
            if raw is not None:
                # A no-trade row keeps the session in the structural sample and
                # out of expectancy/win-rate, which is exactly right: this was a
                # signal that could not be filled, not a flat trade.
                row.update({key: value for key, value in raw.items()
                            if key != "unpriced_reason"})
                row["reject_reason"] = str(raw["unpriced_reason"])
            rows.append(row)
            continue
        risk_budget = max(0.0, cash * float(risk_pct) / 100.0)
        per_unit = max(float(raw["risk_per_unit"]), 1e-9)
        quantity = math.floor(risk_budget / per_unit)
        if vehicle == "equity":
            # `RiskEngine.size_shares` caps notional at plan time against the
            # same signal-close price it prices the bracket from.  Capping on
            # the gapped fill instead would silently size a different position
            # than the runtime, exactly as the stop anchor once did.
            notional_cap = max(0.0, cash * NOTIONAL_CAP_PCT / 100.0)
            quantity = min(quantity, math.floor(notional_cap / max(float(raw["plan_entry"]), 1e-9)))
        if quantity <= 0:
            rows.append({"vehicle": vehicle, "symbol": symbol,
                         "session_date": day.isoformat(), "opportunity_id": opportunity,
                         "net_pnl": 0.0, "return_value": 0.0, "no_trade": True,
                         "reject_reason": "isolated account risk budget cannot fund one unit"})
            continue
        execution_direction = "long" if vehicle == "option" else raw["direction"]
        # An option bid/ask is executable only while it is current.  A quote
        # older than one recorder cycle is an approximation of the fill, so it
        # is charged the modelled half-spread rather than trusted at face value.
        fresh = vehicle == "option" and _fresh(raw, "entry")
        entry = model.execution_price(
            raw["entry_reference"], execution_direction, entry=True,
            executable_quote=fresh or raw.get("entry_fill_source") == QUOTE)
        fresh = vehicle == "option" and _fresh(raw, "exit")
        exit_price = model.execution_price(
            raw["exit_reference"], execution_direction, entry=False,
            executable_quote=fresh or raw.get("exit_fill_source") == QUOTE)
        multiplier = int(raw["contract_multiplier"])
        gross = ((exit_price - entry) if execution_direction == "long" else
                 (entry - exit_price)) * quantity * multiplier
        fees = model.fees(entry, exit_price, quantity, multiplier)
        net = gross - fees
        before = cash
        cash += net
        peak = max(peak, cash)
        drawdown = max(drawdown, peak - cash)
        # Size on the nominal distance because the runtime does; report the risk
        # the fill actually committed, which a gapped entry can push past the
        # budget the sizing believed it was spending.
        risk_usd = quantity * float(raw["realized_risk_per_unit"])
        rows.append({**raw, "opportunity_id": opportunity, "quantity": quantity,
                     "entry_price": entry, "exit_price": exit_price,
                     "gross_pnl": gross, "costs": fees, "net_pnl": net,
                     "risk_budget": risk_budget, "risk_usd": risk_usd,
                     "r_multiple": net / risk_usd if risk_usd > 0 else None,
                     "return_value": net / before if before > 0 else 0.0,
                     "no_trade": False})
    executed = [row for row in rows if row.get("no_trade") is not True]
    return {
        "account_id": account_id, "starting_cash": float(starting_cash),
        "ending_equity": cash, "realized_pnl": cash - float(starting_cash),
        "max_drawdown": drawdown, "trades": len(executed), "rows": rows,
    }


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


def mutate_from_diagnosis(spec: Mapping[str, Any], diagnostic: Mapping[str, Any],
                          count: int = DEFAULT_VARIANTS) -> list[dict]:
    """Create bounded variants from an explicit failure diagnosis."""
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
    variants = [root]
    for change in changes:
        if len(variants) >= int(count):
            break
        try:
            candidate = _safe_variant(root, **change)
        except ValueError:
            continue
        if rule_variant_id(candidate) not in {rule_variant_id(item) for item in variants}:
            variants.append(candidate)
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
        if rule_variant_id(candidate) not in {
                rule_variant_id(item) for item in variants}:
            variants.append(candidate)
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
MAX_DISCOVERY_ATTEMPTS = 512


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
