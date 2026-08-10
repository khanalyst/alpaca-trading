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
    RULE_FAMILIES, evaluate_rule_signal, hold_deadline, rule_variant_id,
    validate_rule_spec,
)
from .costs import BAR, QUOTE, CostModel, index_quotes, quote_fill
from .edge_ledger import content_hash
from .factory_ledger import FactoryError
from .gates import max_drawdown_of
from .market_data import OptionSnapshot, QuoteSnapshot, UnderlyingBar


# `risk.max_position_notional_pct` in the checked runtime config.  Research
# caps on the same percentage of the account, anchored to the same price.
NOTIONAL_CAP_PCT = 25.0

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


def initial_hypotheses(count: int = DEFAULT_STRATEGIES, *,
                       vehicle: str = "equity") -> list[StrategyHypothesis]:
    if not 1 <= int(count) <= MAX_STRATEGIES:
        raise FactoryError(f"strategies must be between 1 and {MAX_STRATEGIES}")
    templates = [
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
    ]
    output = []
    for slot in range(int(count)):
        raw = templates[slot]
        spec = validate_rule_spec(raw)
        output.append(StrategyHypothesis(
            _hypothesis_id(vehicle, slot, 0, spec), slot, 0, vehicle, spec["family"],
            _thesis(spec), _falsification(spec), spec, None, None,
        ))
    return output


def _session(row: UnderlyingBar) -> str:
    return row.timestamp.astimezone(ZoneInfo("America/New_York")).date().isoformat()


def _visible(row: Any, cutoff: datetime) -> bool:
    identity = getattr(row, "identity", None)
    return bool(identity is not None and identity.as_of <= cutoff)


def _option_at(snapshots: Sequence[OptionSnapshot], *, symbol: str, day: date,
               direction: str, cutoff: datetime, contract_symbol: str | None = None) -> OptionSnapshot | None:
    right = "call" if direction == "long" else "put"
    eligible = [snap for snap in snapshots
                if snap.contract.underlying.upper() == symbol.upper()
                and snap.session_date == day and snap.timestamp <= cutoff
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
        if vehicle == "option":
            entry_snap = _option_at(snapshots, symbol=signal_bar.symbol, day=day,
                                    direction=direction, cutoff=entry_bar.end)
            if entry_snap is None:
                return None
            exit_snap = _option_at(snapshots, symbol=signal_bar.symbol, day=day,
                                   direction=direction, cutoff=exit_bar.end,
                                   contract_symbol=entry_snap.contract.symbol)
            if exit_snap is None:
                return None
            contract = entry_snap.contract.symbol
            entry_ref = entry_snap.ask
            exit_ref = exit_snap.bid
            multiplier = entry_snap.contract.multiplier
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
        if raw is None:
            rows.append({"vehicle": vehicle, "symbol": symbol,
                         "session_date": day.isoformat(), "opportunity_id": opportunity,
                         "net_pnl": 0.0, "return_value": 0.0, "no_trade": True})
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
        executable = vehicle == "option"
        entry = model.execution_price(
            raw["entry_reference"], execution_direction, entry=True,
            executable_quote=executable or raw.get("entry_fill_source") == QUOTE)
        exit_price = model.execution_price(
            raw["exit_reference"], execution_direction, entry=False,
            executable_quote=executable or raw.get("exit_fill_source") == QUOTE)
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
