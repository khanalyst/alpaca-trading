"""Deterministic risk engine.

The LLM never touches the exchange directly. Every "open" it proposes passes
through vet_open(), which either rejects the trade or returns a clamped plan:

- leverage capped at config max_leverage and a HARD ceiling of 10
- notional sized so the all-in bounded loss costs risk_per_trade_pct of equity
- per-position and gross exposure caps in % of equity
- net directional exposure cap (long minus short notional) so several
  same-direction positions in correlated coins can't act as one macro bet
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

    def vet_open(self, decision: dict, equity: float, positions: list[dict],
                 snapshot: dict, cooldowns: dict,
                 gross_notional: float,
                 entry_feedback: dict | None = None,
                 entry_failures: dict | None = None,
                 ) -> tuple[dict | None, str | None]:
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
        confidence = float(decision.get("confidence", 0))
        if not math.isfinite(confidence):
            return None, "confidence is not finite"
        if confidence < float(self.r["min_confidence"]):
            return None, "confidence below floor"
        if float(cooldowns.get(symbol, 0)) > time.time():
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
            if (expires_at > time.time()
                    and blocked_until > time.time()):
                return None, "symbol in execution failure backoff"

        intent_pct = float(decision.get("size_pct_equity") or 0)
        if not math.isfinite(intent_pct) or intent_pct < 0:
            return None, "intended size is not finite"

        # A first depth rejection is execution feedback, not an immediate
        # ban. The model may reconsider the setup and explicitly request a
        # smaller retry. A repeated failure acquires a persisted backoff.
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
            if same_direction and expires_at > time.time():
                if blocked_until > time.time():
                    return None, "symbol in liquidity backoff"
                if max_retry_pct <= 0:
                    return None, "liquidity retry has no safe executable size"
                if intent_pct <= 0:
                    return None, (
                        "liquidity retry requires an explicit smaller size")
                if intent_pct > max_retry_pct + 1e-12:
                    return None, (
                        "liquidity retry size exceeds "
                        f"{max_retry_pct:.2f}% equity limit")

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

        leverage = float(decision.get("leverage") or 1)
        if not math.isfinite(leverage):
            return None, "leverage is not finite"
        leverage = int(math.floor(max(
            1.0, min(leverage, float(self.r["max_leverage"]),
                     HARD_MAX_LEVERAGE))))

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
        fee_pct = 2 * float(self.costs["taker_fee_pct_per_side"])
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
        risk_usd = equity * float(self.r["risk_per_trade_pct"]) / 100.0
        notional = risk_usd / (estimated_loss_pct / 100.0)

        # Per-position cap.
        notional = min(notional,
                       equity * float(self.r["max_position_notional_pct"]) / 100.0)

        # Respect the model's own (smaller) intent if it gave one.
        if intent_pct > 0:
            notional = min(notional, equity * intent_pct / 100.0 * leverage)

        # Gross exposure cap across the whole book.
        room = equity * float(self.r["max_gross_exposure_pct"]) / 100.0
        room -= gross_notional
        if room <= 0:
            return None, "gross exposure cap reached"
        notional = min(notional, room)

        if notional < MIN_NOTIONAL_USD:
            return None, "resulting size too small"

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
            "risk_budget_usd": risk_usd,
            "risk_usd": notional * estimated_loss_pct / 100.0,
            "confidence": confidence,
            "reason": decision.get("reasoning", ""),
        }, None
