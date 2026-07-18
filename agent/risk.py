"""Deterministic risk engine.

The LLM never touches the exchange directly. Every "open" it proposes passes
through vet_open(), which either rejects the trade or returns a clamped plan:

- leverage capped at config max_leverage and a HARD ceiling of 10
- notional sized so that hitting the stop costs risk_per_trade_pct of equity
- per-position and gross exposure caps in % of equity
- net directional exposure cap (long minus short notional) so several
  same-direction positions in correlated coins can't act as one macro bet
- confidence floor, per-symbol cooldowns, stop-distance sanity bounds

Circuit breakers (daily loss, max drawdown, margin usage) live in the engine
loop because they act on the whole account, not one trade.
"""

import logging
import time

log = logging.getLogger("risk")

HARD_MAX_LEVERAGE = 10
MIN_STOP_PCT = 0.2
MAX_STOP_PCT = 15.0
MIN_NOTIONAL_USD = 10.0


class RiskEngine:
    def __init__(self, cfg: dict):
        self.r = cfg["risk"]

    def vet_open(self, decision: dict, equity: float, positions: list[dict],
                 snapshot: dict, cooldowns: dict,
                 gross_notional: float) -> tuple[dict | None, str | None]:
        symbol = decision["symbol"]

        if symbol not in snapshot:
            return None, "symbol not in current snapshot"
        if any(p.get("symbol") == symbol for p in positions):
            return None, "already holding this symbol"
        if len(positions) >= int(self.r["max_concurrent_positions"]):
            return None, "max concurrent positions reached"
        if float(decision.get("confidence", 0)) < float(self.r["min_confidence"]):
            return None, "confidence below floor"
        if float(cooldowns.get(symbol, 0)) > time.time():
            return None, "symbol in post-loss cooldown"

        stop_pct = float(decision.get("stop_loss_pct") or 0)
        if not (MIN_STOP_PCT <= stop_pct <= MAX_STOP_PCT):
            return None, f"stop distance {stop_pct}% out of bounds"
        take_pct = float(decision.get("take_profit_pct") or 0)
        if take_pct <= 0:
            take_pct = stop_pct * 2

        leverage = float(decision.get("leverage") or 1)
        leverage = max(1.0, min(leverage, float(self.r["max_leverage"]),
                                HARD_MAX_LEVERAGE))

        price = float(snapshot[symbol]["price"])
        if price <= 0:
            return None, "invalid price"

        # Size from risk: losing the stop costs risk_per_trade_pct of equity.
        risk_usd = equity * float(self.r["risk_per_trade_pct"]) / 100.0
        notional = risk_usd / (stop_pct / 100.0)

        # Per-position cap.
        notional = min(notional,
                       equity * float(self.r["max_position_notional_pct"]) / 100.0)

        # Respect the model's own (smaller) intent if it gave one.
        intent_pct = float(decision.get("size_pct_equity") or 0)
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
        for p in positions:
            pn = abs(float(p.get("notional") or 0))
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
            "price": price,
            "sl_pct": stop_pct,
            "tp_pct": take_pct,
            "confidence": float(decision.get("confidence", 0)),
            "reason": decision.get("reasoning", ""),
        }, None
