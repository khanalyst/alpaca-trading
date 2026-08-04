"""Deterministic risk engine.

The LLM never touches the exchange directly. Every "open" it proposes passes
through vet_open(), which either rejects the trade or returns a clamped plan:

- leverage capped at config max_leverage and a HARD ceiling of 10
- notional sized so the all-in bounded loss costs risk_per_trade_pct of equity
- per-position and gross exposure caps in % of equity
- total planned stop-risk cap across all open positions
- net directional exposure cap (long minus short notional) so several
  same-direction positions in correlated coins can't act as one macro bet
- signed BTC-beta-weighted exposure cap using measured beta or a conservative
  beta of one when history is insufficient
- confidence floor, per-symbol cooldowns, stop-distance sanity bounds

Circuit breakers (daily loss, max drawdown, margin usage) live in the engine
loop because they act on the whole account, not one trade.
"""

import logging
import math
import time

log = logging.getLogger("risk")

HARD_MAX_LEVERAGE = 10
MIN_STOP_PCT = 0.2
MAX_STOP_PCT = 15.0
# Generous ceiling (>3R on the widest stop). Beyond it the number is noise,
# and a short's TP trigger price entry*(1 - tp/100) would go non-positive at
# 100%, which OKX rejects together with the whole attached-SL/TP entry.
MAX_TP_PCT = 50.0
MIN_NOTIONAL_USD = 10.0


class RiskEngine:
    def __init__(self, cfg: dict):
        self.r = cfg["risk"]
        self.costs = cfg["trading_costs"]
        self.execution = cfg["execution"]
        self.strategy_id = str(cfg["strategy"]["id"])

    def vet_open(self, decision: dict, equity: float, positions: list[dict],
                 snapshot: dict, cooldowns: dict,
                 gross_notional: float,
                 entry_feedback: dict | None = None,
                 entry_failures: dict | None = None,
                 active_trades: dict | None = None,
                 now: float | None = None,
                 ) -> tuple[dict | None, str | None]:
        # Injectable clock, defaulting to the real one. Replay must be able
        # to evaluate a cooldown as of the moment the decision was actually
        # made; using wall-clock time there would expire every cooldown in
        # the corpus and silently admit trades the live agent refused.
        now = time.time() if now is None else float(now)
        symbol = decision["symbol"]

        try:
            equity = float(equity)
            gross_notional = float(gross_notional)
        except (TypeError, ValueError):
            return None, "account exposure measurement is invalid"
        if not math.isfinite(equity) or equity <= 0:
            return None, "account equity measurement is invalid"
        if not math.isfinite(gross_notional) or gross_notional < 0:
            return None, "gross exposure measurement is invalid"

        # Validate every held position independently of the engine's normalizer.
        # In particular, NaN must never reach arithmetic below: comparisons
        # with NaN are false and would silently bypass both exposure caps.
        position_notionals = []
        for position in positions:
            try:
                position_notional = abs(float(position.get("notional")))
            except (TypeError, ValueError):
                return None, "held position notional is invalid"
            if not math.isfinite(position_notional) or position_notional <= 0:
                return None, "held position notional is invalid"
            if position.get("side") not in {"long", "short"}:
                return None, "held position direction is invalid"
            position_notionals.append(position_notional)

        active_trades = active_trades or {}
        current_open_risk = 0.0
        for position in positions:
            symbol_key = position.get("symbol")
            trade = active_trades.get(symbol_key)
            if not isinstance(trade, dict):
                return None, "held position planned risk is unavailable"
            try:
                held_risk = float(trade.get("risk_usd"))
            except (TypeError, ValueError):
                return None, "held position planned risk is unavailable"
            if not math.isfinite(held_risk) or held_risk < 0:
                return None, "held position planned risk is unavailable"
            current_open_risk += held_risk

        # Snapshot keys starting with "_" are context blocks (_market_context),
        # not tradable instruments; never let one pass the membership check.
        if not isinstance(symbol, str) or symbol.startswith("_"):
            return None, "not a tradable symbol"
        if symbol not in snapshot:
            return None, "symbol not in current snapshot"
        if any(p.get("symbol") == symbol for p in positions):
            return None, "already holding this symbol"
        if len(positions) >= int(self.r["max_concurrent_positions"]):
            return None, "max concurrent positions reached"

        # Count correlated concentration separately from notional exposure.
        direction = decision.get("direction")
        if direction not in {"long", "short"}:
            return None, "direction is invalid"
        same_side = "long" if direction == "long" else "short"
        held_same_side = sum(
            1 for p in positions if p.get("side") == same_side)
        if held_same_side >= int(self.r["max_same_direction_positions"]):
            return None, (
                f"max same-direction positions reached "
                f"({held_same_side} {same_side})")

        # Breadth guard. When many instruments qualify at once they are not
        # independent setups; they are one market-wide move, and those bars
        # were measured materially worse than quiet ones. The count is
        # supplied by the engine's per-strategy contract pass under the
        # market context's hidden enrichment block.
        breadth_limit = self.r.get("max_setups_firing_for_entry")
        if breadth_limit is not None:
            context = snapshot.get("_market_context")
            missing = object()
            breadth_map = missing
            if isinstance(context, dict):
                enrichment = context.get("_enrichment")
                if isinstance(enrichment, dict):
                    breadth_map = enrichment.get(
                        "setup_breadth_by_strategy", missing)
            if breadth_map is missing:
                firing = (
                    context.get("instruments_with_a_valid_setup")
                    if self.strategy_id == "momentum"
                    and isinstance(context, dict) else None)
            elif not isinstance(breadth_map, dict):
                return None, "setup breadth measurement is invalid"
            else:
                selected = breadth_map.get(self.strategy_id, missing)
                if (not isinstance(selected, dict)
                        or "instruments_with_a_valid_setup" not in selected):
                    return None, "setup breadth measurement is invalid"
                firing = selected["instruments_with_a_valid_setup"]
                if firing is None:
                    return None, "setup breadth measurement is invalid"
            if firing is not None:
                try:
                    firing = int(firing)
                except (TypeError, ValueError, OverflowError):
                    return None, "setup breadth measurement is invalid"
                if firing > int(breadth_limit):
                    return None, (
                        f"setup breadth {firing} instruments exceeds "
                        f"{int(breadth_limit)}: correlated market-wide move")

        confidence = float(decision.get("confidence", 0))
        if not math.isfinite(confidence):
            return None, "confidence is not finite"
        if confidence < float(self.r["min_confidence"]):
            return None, "confidence below floor"
        if float(cooldowns.get(symbol, 0)) > now:
            return None, "symbol in post-loss cooldown"
        failure = (entry_failures or {}).get(symbol)
        if failure is not None:
            if not isinstance(failure, dict):
                return None, "execution failure feedback is invalid"
            try:
                blocked_until = float(failure.get("blocked_until") or 0)
                expires_at = float(failure.get("expires_at") or 0)
            except (TypeError, ValueError):
                return None, "execution failure feedback is invalid"
            if (not math.isfinite(blocked_until)
                    or not math.isfinite(expires_at)):
                return None, "execution failure feedback is invalid"
            if (expires_at > now
                    and blocked_until > now):
                return None, "symbol in execution failure backoff"

        execution_choice = str(
            decision.get("execution_choice") or "normal")
        if execution_choice not in {"normal", "retry_smaller"}:
            return None, "execution choice is invalid"
        retry_margin_pct = None

        # A first depth rejection is execution feedback, not an immediate ban.
        # The model chooses whether the setup still deserves a smaller retry;
        # deterministic code chooses the safe retry size.
        feedback = (entry_feedback or {}).get(symbol)
        if feedback is not None:
            if not isinstance(feedback, dict):
                return None, "liquidity retry feedback is invalid"
            try:
                expires_at = float(feedback.get("expires_at") or 0)
                blocked_until = float(feedback.get("blocked_until") or 0)
                max_retry_pct = float(
                    feedback.get("max_retry_size_pct_equity") or 0)
            except (TypeError, ValueError):
                return None, "liquidity retry feedback is invalid"
            if not all(math.isfinite(value) for value in (
                    expires_at, blocked_until, max_retry_pct)):
                return None, "liquidity retry feedback is invalid"
            same_direction = feedback.get("direction") == decision["direction"]
            if same_direction and expires_at > now:
                if blocked_until > now:
                    return None, "symbol in liquidity backoff"
                if max_retry_pct <= 0:
                    return None, "liquidity retry has no safe executable size"
                if execution_choice != "retry_smaller":
                    return None, (
                        "liquidity retry requires retry_smaller approval")
                retry_margin_pct = max_retry_pct
        elif execution_choice == "retry_smaller":
            return None, "no current liquidity rejection supports a retry"

        stop_pct = float(decision.get("stop_loss_pct") or 0)
        if not math.isfinite(stop_pct):
            return None, "stop distance is not finite"
        if not (MIN_STOP_PCT <= stop_pct <= MAX_STOP_PCT):
            return None, f"stop distance {stop_pct}% out of bounds"
        take_pct = float(decision.get("take_profit_pct") or 0)
        if not math.isfinite(take_pct):
            return None, "take-profit distance is not finite"
        if take_pct > MAX_TP_PCT:
            return None, f"take-profit distance {take_pct}% out of bounds"
        if take_pct <= 0:
            take_pct = stop_pct * 2

        # Leverage is configured policy, never a model-selected number.
        leverage = int(min(
            int(self.r["entry_leverage"]), int(self.r["max_leverage"]),
            HARD_MAX_LEVERAGE))

        price = float(snapshot[symbol]["price"])
        if not math.isfinite(price) or price <= 0:
            return None, "invalid price"

        symbol_data = snapshot[symbol]
        spread_raw = symbol_data.get("spread_pct")
        if spread_raw is None:
            return None, "live spread unavailable"
        funding_raw = symbol_data.get("funding_rate_pct")
        if funding_raw is None:
            return None, "live funding rate unavailable"
        spread_pct = max(0.0, float(spread_raw))
        funding_pct = float(funding_raw)
        if not math.isfinite(spread_pct) or not math.isfinite(funding_pct):
            return None, "live cost data is not finite"
        funding_intervals = float(
            self.costs["expected_funding_intervals_held"])
        interval_raw = symbol_data.get("funding_interval_hours")
        hold_hours = float(self.costs["expected_hold_hours"])
        if interval_raw not in (None, ""):
            interval_hours = float(interval_raw)
            if not math.isfinite(interval_hours):
                return None, "funding schedule is not finite"
        else:
            interval_hours = 0.0
        if interval_hours > 0:
            next_minutes = symbol_data.get("next_funding_minutes")
            if next_minutes in (None, ""):
                dynamic_intervals = math.ceil(hold_hours / interval_hours)
            else:
                next_minutes = float(next_minutes)
                if not math.isfinite(next_minutes):
                    return None, "funding schedule is not finite"
                next_hours = max(0.0, next_minutes / 60)
                dynamic_intervals = (0 if next_hours > hold_hours else
                                     1 + math.floor(max(
                                         0.0, hold_hours - next_hours)
                                         / interval_hours))
            funding_intervals = max(funding_intervals, dynamic_intervals)
        adverse_funding_pct = (
            max(0.0, funding_pct)
            if decision["direction"] == "long"
            else max(0.0, -funding_pct)
        ) * funding_intervals
        fee_rate_raw = symbol_data.get("taker_fee_pct_per_side")
        fee_rate = (
            float(fee_rate_raw) if fee_rate_raw is not None
            else float(self.costs["taker_fee_pct_per_side"])
        )
        if not math.isfinite(fee_rate) or fee_rate < 0 or fee_rate > 1:
            return None, "account fee rate is invalid"
        fee_pct = 2 * fee_rate
        stop_slippage_pct = float(self.costs["expected_stop_slippage_pct"])
        # A fill can occur anywhere inside the exchange-side IOC boundary
        # after the depth snapshot changes. Reserve the whole bounded entry
        # move so it cannot sit outside risk_per_trade_pct.
        entry_slippage_pct = float(
            self.execution["max_order_book_slippage_pct"])
        estimated_loss_pct = (stop_pct + fee_pct + spread_pct
                              + stop_slippage_pct + entry_slippage_pct
                              + adverse_funding_pct)
        if estimated_loss_pct <= 0:
            return None, "invalid estimated stop cost"

        # Size from all-in adverse risk, not price distance alone. This keeps
        # expected fees, spread, stop slippage and adverse funding inside the
        # configured risk-per-trade budget.
        risk_pct = (
            float(self.r["experimental_risk_per_trade_pct"])
            if decision.get("setup_type") == "other"
            else float(self.r["risk_per_trade_pct"])
        )
        risk_usd = equity * risk_pct / 100.0
        notional = risk_usd / (estimated_loss_pct / 100.0)

        # Track which limit actually decides the size. risk_per_trade_pct is a
        # ceiling, not a promise: for any stop narrower than
        # risk_per_trade_pct / max_position_notional_pct the notional cap binds
        # first and the real loss at the stop is smaller than the configured
        # budget. Recording the binding constraint keeps that visible in the
        # journal instead of leaving operators to infer it from the numbers.
        sizing_constraint = "risk_per_trade_budget"

        notional_cap = (
            equity * float(self.r["max_position_notional_pct"]) / 100.0)
        if notional_cap < notional:
            sizing_constraint = "max_position_notional_pct"
            notional = notional_cap

        if retry_margin_pct is not None:
            retry_cap = equity * retry_margin_pct / 100.0 * leverage
            if retry_cap < notional:
                sizing_constraint = "liquidity_retry_cap"
                notional = retry_cap

        room = equity * float(self.r["max_gross_exposure_pct"]) / 100.0
        room -= gross_notional
        if room <= 0:
            return None, "gross exposure cap reached"
        if room < notional:
            sizing_constraint = "max_gross_exposure_pct"
            notional = room

        if notional < MIN_NOTIONAL_USD:
            return None, "resulting size too small"

        # The daily stop is an account outcome, while per-trade risk is only a
        # plan. Cap the sum of those plans so three simultaneous stops cannot
        # consume essentially the whole daily loss allowance.
        total_risk_cap = (
            equity * float(self.r["max_total_open_risk_pct"]) / 100.0)
        risk_room = total_risk_cap - current_open_risk
        if risk_room <= 0:
            return None, "total planned open-risk cap reached"
        candidate_risk = notional * estimated_loss_pct / 100.0
        if candidate_risk > risk_room:
            sizing_constraint = "max_total_open_risk_pct"
            notional = risk_room / (estimated_loss_pct / 100.0)
            candidate_risk = notional * estimated_loss_pct / 100.0
        if notional < MIN_NOTIONAL_USD:
            return None, "total planned open-risk room is too small"

        # Correlation guard: most alts move with BTC, so N same-direction
        # positions behave like one position N times the size. Cap the net
        # directional book (long minus short notional). Opens that REDUCE
        # the net always pass this check.
        net = 0.0
        for p, pn in zip(positions, position_notionals):
            if p.get("side") == "long":
                net += pn
            elif p.get("side") == "short":
                net -= pn
        candidate = notional if decision["direction"] == "long" else -notional
        net_cap = equity * float(self.r.get("max_net_direction_pct", 100)) / 100.0
        if abs(net + candidate) > net_cap:
            return None, "net directional exposure cap reached"

        def beta_for(position_symbol: str) -> float:
            if position_symbol == "BTC/USDT:USDT":
                return 1.0
            data = snapshot.get(position_symbol)
            if not isinstance(data, dict):
                return 1.0
            try:
                samples = int(data.get("corr_btc_samples") or 0)
                beta = float(data.get("beta_btc_1h_72"))
            except (TypeError, ValueError):
                return 1.0
            if (samples < int(self.r["min_btc_beta_samples"])
                    or not math.isfinite(beta)):
                return 1.0
            return max(-3.0, min(3.0, beta))

        beta_exposure = 0.0
        for position, held_notional in zip(positions, position_notionals):
            sign = 1.0 if position["side"] == "long" else -1.0
            beta_exposure += (
                sign * held_notional * beta_for(position["symbol"]))
        candidate_beta = beta_for(symbol)
        candidate_beta_exposure = (
            notional * candidate_beta
            * (1.0 if decision["direction"] == "long" else -1.0))
        beta_cap = (
            equity * float(self.r["max_btc_beta_exposure_pct"]) / 100.0)
        beta_after = beta_exposure + candidate_beta_exposure
        if (abs(beta_after) > beta_cap
                and abs(beta_after) > abs(beta_exposure)):
            return None, "BTC-beta-weighted exposure cap reached"

        return {
            "symbol": symbol,
            "direction": decision["direction"],
            "leverage": leverage,
            "notional": notional,
            "margin": notional / leverage,
            "margin_pct_equity": notional / leverage / equity * 100.0,
            "price": price,
            "sl_pct": stop_pct,
            "tp_pct": take_pct,
            "estimated_loss_pct": estimated_loss_pct,
            "estimated_cost_pct": estimated_loss_pct - stop_pct,
            "spread_pct": spread_pct,
            "entry_slippage_budget_pct": entry_slippage_pct,
            "adverse_funding_pct": adverse_funding_pct,
            "estimated_funding_intervals": funding_intervals,
            "taker_fee_pct_per_side": fee_rate,
            "fee_rate_source": symbol_data.get(
                "fee_rate_source", "configured_fallback"),
            "risk_budget_usd": candidate_risk,
            "per_trade_risk_budget_usd": risk_usd,
            "risk_usd": candidate_risk,
            "risk_budget_pct": risk_pct,
            # Which cap decided the size, and what the trade actually risks as
            # a percentage of equity. When sizing_constraint is not
            # "risk_per_trade_budget", effective_risk_pct_equity is below the
            # configured risk_budget_pct and raising that number alone will
            # not change the size.
            "sizing_constraint": sizing_constraint,
            "effective_risk_pct_equity": candidate_risk / equity * 100.0,
            "portfolio_open_risk_usd_before": current_open_risk,
            "portfolio_open_risk_usd_after": (
                current_open_risk + candidate_risk),
            "btc_beta": candidate_beta,
            "btc_beta_exposure_usd_before": beta_exposure,
            "btc_beta_exposure_usd_after": beta_after,
            "confidence": confidence,
            "reason": decision.get("reasoning", ""),
            "strategy_id": decision.get("strategy_id"),
            "strategy_version": decision.get("strategy_version"),
            "setup_id": decision.get("setup_id"),
            "setup_key": decision.get("setup_key"),
            "setup_type": decision.get("setup_type"),
            "signal_ts": decision.get("signal_ts"),
            "exit_policy": decision.get("exit_policy"),
            "invalidation_anchor": decision.get("invalidation_anchor"),
            "execution_choice": execution_choice,
        }, None
