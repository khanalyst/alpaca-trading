"""Deterministic sizing and execution-profile risk checks for IBR trades."""

from __future__ import annotations

import math
import time
from datetime import date, datetime, timezone
from typing import Mapping


def _num(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _timestamp(value):
    """Normalize quote timestamps expressed as epoch or ISO text."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return None
        if abs(number) > 100_000_000_000:
            number /= 1000.0
        return number
    else:
        raw = str(value).strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _option_kind(value) -> str:
    return str(getattr(value, "value", value or "")).lower().split(".")[-1].strip()


class RiskEngine:
    """Risk boundary with shares and single-leg long-option profiles.

    The constructor accepts the whole config mapping or just a ``risk``
    mapping. No exchange/client imports are needed, which keeps replay and
    unit tests deterministic.
    """

    def __init__(self, cfg: Mapping):
        self.cfg = cfg
        self.r = cfg.get("risk", cfg) if isinstance(cfg, Mapping) else {}
        if isinstance(self.r, Mapping) and isinstance(self.r.get("risk"), Mapping):
            self.r = self.r["risk"]
        self.execution = cfg.get("execution", {}) if isinstance(cfg, Mapping) else {}
        strategy = cfg.get("strategy", {}) if isinstance(cfg, Mapping) else {}
        self.strategy_id = str(strategy.get("id") or "ibr")

    def _risk_usd(self, equity: float, decision: Mapping) -> float:
        explicit = _num(decision.get("risk_usd"))
        if explicit is not None and explicit > 0:
            return explicit
        pct = _num(self.r.get("risk_per_trade_pct"), 1.0) or 1.0
        return equity * pct / 100.0

    @staticmethod
    def _entry_stop(decision: Mapping, market: Mapping) -> tuple[float | None, float | None, float | None]:
        entry = _num(decision.get("entry_price"), _num(market.get("price")))
        stop = _num(decision.get("stop_price"))
        distance = _num(decision.get("stop_distance"))
        if distance is None:
            if entry is not None and stop is not None:
                distance = abs(entry - stop)
            else:
                pct = _num(decision.get("stop_loss_pct"))
                if entry is not None and pct is not None:
                    distance = entry * abs(pct) / 100.0
        if stop is None and entry is not None and distance is not None:
            stop = entry - distance if decision.get("direction") == "long" else entry + distance
        return entry, stop, distance

    def size_shares(self, equity: float, entry_price: float,
                    stop_distance: float, symbol_data: Mapping | None = None,
                    risk_usd: float | None = None) -> dict:
        """Return floor(risk / stop distance), capped by notional/liquidity."""
        equity = _num(equity); entry = _num(entry_price); distance = _num(stop_distance)
        if equity is None or equity <= 0 or entry is None or entry <= 0 or distance is None or distance <= 0:
            raise ValueError("equity, entry price, and stop distance must be positive")
        budget = _num(risk_usd, self._risk_usd(equity, {})) or 0.0
        raw = math.floor(budget / distance)
        cap_pct = _num(self.r.get("max_position_notional_pct"), 100.0) or 100.0
        notional_cap = equity * cap_pct / 100.0
        cap_shares = math.floor(notional_cap / entry)
        liquidity_cap = None
        data = symbol_data if isinstance(symbol_data, Mapping) else {}
        for key in ("liquidity_cap_shares", "max_liquidity_shares", "available_shares"):
            candidate = _num(data.get(key))
            if candidate is not None:
                liquidity_cap = math.floor(candidate) if liquidity_cap is None else min(liquidity_cap, math.floor(candidate))
        for key in ("liquidity_cap_notional", "max_liquidity_notional", "safe_depth_usd"):
            candidate = _num(data.get(key))
            if candidate is not None and candidate >= 0:
                value = math.floor(candidate / entry)
                liquidity_cap = value if liquidity_cap is None else min(liquidity_cap, value)
        shares = max(0, min(raw, cap_shares, liquidity_cap if liquidity_cap is not None else raw))
        return {"shares": shares, "risk_usd": shares * distance,
                "notional": shares * entry, "stop_distance": distance,
                "notional_cap": notional_cap,
                "liquidity_cap_shares": liquidity_cap}

    def _option_limits(self):
        minimum = int(_num(self.r.get("options_min_dte"), 7) or 7)
        maximum = int(_num(self.r.get("options_max_dte"), 45) or 45)
        spread_pct = _num(self.r.get("options_max_spread_pct"), 10.0) or 10.0
        return minimum, maximum, spread_pct

    def select_option_contract(self, candidates, direction: str,
                               now: float | None = None) -> dict:
        """Select one liquid long call/put contract.

        Multi-leg debit spreads and short/naked structures remain explicitly
        unsupported until an order-leg model and reconciliation path exist.
        """
        if direction not in {"long", "short"}:
            raise ValueError("option direction must be long or short")
        minimum, maximum, max_spread = self._option_limits()
        now_value = time.time() if now is None else float(now)
        wanted = "call" if direction == "long" else "put"
        accepted = []
        rejected = []
        for raw in candidates or ():
            if not isinstance(raw, Mapping):
                rejected.append("not a mapping"); continue
            option = dict(raw)
            raw_kind = option.get("type", option.get("right", option.get("option_type", "")))
            kind = _option_kind(raw_kind)
            strategy = str(option.get("strategy", option.get("structure", "single"))).lower()
            side = str(option.get("side", "buy")).lower()
            intent = str(option.get("position_intent", "buy_to_open")).lower()
            if intent not in {"buy_to_open", "buy_to_close"}:
                rejected.append("short option intent unsupported"); continue
            if strategy not in {"single", "long", "call", "put"}:
                rejected.append("multi-leg option structure unsupported"); continue
            if strategy in {"naked_short", "short", "sell", "credit_spread"} or side in {"sell", "short"}:
                rejected.append("naked short"); continue
            if kind in {"c", "p"}:
                kind = {"c": "call", "p": "put"}[kind]
            if kind != wanted:
                rejected.append("wrong right"); continue
            dte = _num(option.get("dte"))
            if dte is None:
                expiry = option.get("expiration", option.get("expiry"))
                if expiry is not None:
                    try:
                        if isinstance(expiry, date) and not isinstance(expiry, datetime):
                            dte = (expiry - datetime.fromtimestamp(now_value, timezone.utc).date()).days
                        else:
                            exp = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
                            if exp.tzinfo is None: exp = exp.replace(tzinfo=timezone.utc)
                            dte = (exp.timestamp() - now_value) / 86400.0
                    except (TypeError, ValueError, OverflowError):
                        dte = None
            if dte is None or dte < minimum or dte > maximum:
                rejected.append("dte out of bounds"); continue
            if dte <= 0:
                rejected.append("0DTE"); continue
            stale = option.get("stale") is True or option.get("quote_stale") is True
            max_age = _num(self.execution.get("max_market_data_age_seconds"), 30) or 30
            quote_age = _num(option.get("quote_age_seconds"))
            quote_ts = _timestamp(option.get("quote_ts", option.get("quote_timestamp", option.get("timestamp"))))
            if quote_age is None and quote_ts is not None:
                quote_age = now_value - quote_ts
            # A caller that supplies an evaluation clock opts into strict
            # point-in-time validation.  The no-clock form remains compatible
            # with offline fixtures, while provider candidates always include
            # quote_age_seconds and therefore still fail closed.
            require_fresh = now is not None or any(key in option for key in (
                "quote_ts", "quote_timestamp", "quote_age_seconds", "timestamp"))
            if require_fresh and quote_age is None:
                rejected.append("quote freshness unavailable"); continue
            if quote_age is not None and (quote_age < 0 or quote_age > max_age):
                stale = True
            if stale:
                rejected.append("stale quote"); continue
            # A long option must be executable at a real two-sided market;
            # accepting ask-only/debit-only rows hides untradeable chains.
            bid = _num(option.get("bid"))
            ask = _num(option.get("ask"))
            if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
                rejected.append("invalid bid/ask"); continue
            bid_size = _num(option.get("bid_size"))
            ask_size = _num(option.get("ask_size"))
            if (bid_size is not None and bid_size <= 0) or (
                    ask_size is not None and ask_size <= 0):
                rejected.append("empty displayed option market"); continue
            debit = _num(option.get("debit", option.get("net_debit")), ask)
            if debit is None or debit <= 0:
                rejected.append("invalid debit"); continue
            spread_pct = (ask - bid) / ((ask + bid) / 2) * 100.0
            if spread_pct > max_spread:
                rejected.append("wide option spread"); continue
            volume = _num(option.get("volume"), 0.0) or 0.0
            open_interest = _num(option.get("open_interest", option.get("oi")), 0.0) or 0.0
            displayed_size = min(
                value for value in (bid_size, ask_size)
                if value is not None
            ) if bid_size is not None and ask_size is not None else 0.0
            if volume <= 0 and open_interest <= 0 and displayed_size <= 0:
                rejected.append("illiquid option"); continue
            multiplier = _num(option.get("multiplier", option.get("contract_multiplier", option.get("size"))))
            if multiplier is None or multiplier <= 0:
                rejected.append("invalid contract multiplier"); continue
            if multiplier != math.floor(multiplier):
                rejected.append("invalid contract multiplier"); continue
            # Derive a stable moneyness distance when the provider did not
            # precompute one.  ATM proximity is the primary tie-breaker; a
            # narrow, liquid quote then beats a merely cheap deep-OTM row.
            strike = _num(option.get("strike", option.get("strike_price")))
            spot = _num(option.get("underlying_price", option.get("underlying_last", option.get("spot"))))
            moneyness_distance = _num(option.get("moneyness_distance"))
            if moneyness_distance is None and strike is not None and spot is not None and spot > 0:
                moneyness_distance = abs(strike - spot) / spot
            option["moneyness_distance"] = moneyness_distance if moneyness_distance is not None else float("inf")
            option["spread_pct"] = spread_pct
            option["displayed_size"] = displayed_size
            option["debit"] = debit; option["multiplier"] = multiplier; option["dte"] = dte
            option["max_loss_per_contract"] = debit * multiplier
            option["max_loss"] = option["max_loss_per_contract"]
            accepted.append(option)
        if not accepted:
            detail = rejected[0] if rejected else "no candidates"
            raise ValueError(f"no eligible single-leg long option: {detail}")
        # Deterministic and execution-aware: choose near-ATM first, then a
        # tighter spread and deeper displayed liquidity.  Debit is only a
        # late tie-breaker, preventing the cheapest illiquid far-OTM contract
        # from winning by construction.
        return min(accepted, key=lambda row: (
            float(row.get("moneyness_distance", float("inf"))),
            float(row.get("spread_pct", float("inf"))),
            -(_num(row.get("volume"), 0.0) or 0.0),
            -(_num(row.get("open_interest", row.get("oi")), 0.0) or 0.0),
            -(_num(row.get("displayed_size"), 0.0) or 0.0),
            float(row["debit"]), str(row.get("symbol", ""))))

    def size_options(self, equity: float, risk_usd: float,
                     candidates, direction: str, now: float | None = None) -> dict:
        rows = list(candidates or ())
        # Provider-generated candidates carry quote_age_seconds/quote_ts.  A
        # bare offline fixture predating those fields remains usable without
        # an artificial clock, while any timestamped candidate is checked
        # strictly against ``now``.
        has_freshness = any(isinstance(row, Mapping) and any(key in row for key in (
            "quote_ts", "quote_timestamp", "quote_age_seconds", "timestamp")) for row in rows)
        contract = self.select_option_contract(rows, direction=direction,
                                               now=now if has_freshness else None)
        max_loss = float(contract["max_loss_per_contract"])
        contracts = math.floor(float(risk_usd) / max_loss)
        liquidity_cap = _num(contract.get("liquidity_cap_contracts", contract.get("max_contracts")))
        if liquidity_cap is not None:
            contracts = min(contracts, math.floor(liquidity_cap))
        if contracts <= 0:
            raise ValueError("option debit exceeds risk budget")
        contract = dict(contract)
        contract["contracts"] = int(contracts)
        if contract["contracts"] <= 0:
            raise ValueError("option debit exceeds risk budget")
        contract["max_loss"] = contracts * max_loss
        contract["risk_usd"] = contract["max_loss"]
        return contract

    def vet_open(self, decision: Mapping, equity: float, positions: list[Mapping],
                 snapshot: Mapping, cooldowns: Mapping, gross_notional: float,
                 entry_feedback: Mapping | None = None,
                 entry_failures: Mapping | None = None,
                 active_trades: Mapping | None = None,
                 now: float | None = None):
        now_value = time.time() if now is None else float(now)
        equity = _num(equity); gross = _num(gross_notional)
        if equity is None or equity <= 0: return None, "account equity measurement is invalid"
        if gross is None or gross < 0: return None, "gross exposure measurement is invalid"
        symbol = decision.get("symbol")
        if not isinstance(symbol, str) or symbol.startswith("_"): return None, "not a tradable symbol"
        if symbol not in snapshot: return None, "symbol not in current snapshot"
        if any(p.get("symbol") == symbol for p in positions): return None, "already holding this symbol"
        if len(positions) >= int(_num(self.r.get("max_concurrent_positions"), 1) or 1): return None, "max concurrent positions reached"
        direction = decision.get("direction")
        if direction not in {"long", "short"}: return None, "direction is invalid"
        confidence = _num(decision.get("confidence"), 1.0)
        if confidence is None: return None, "confidence is not finite"
        if confidence < (_num(self.r.get("min_confidence"), 0.0) or 0.0): return None, "confidence below floor"
        if _num((cooldowns or {}).get(symbol), 0.0) > now_value: return None, "symbol in post-loss cooldown"
        market = snapshot[symbol] if isinstance(snapshot[symbol], Mapping) else {}
        entry, stop, distance = self._entry_stop(decision, market)
        if entry is None or entry <= 0: return None, "invalid price"
        if distance is None or distance <= 0: return None, "stop distance is invalid"
        target = _num(decision.get("target_price"))
        if target is None:
            target_r = _num(decision.get("target_r"), 2.0) or 2.0
            target = entry + target_r * distance if direction == "long" else entry - target_r * distance
        if (direction == "long" and (stop is None or stop >= entry or target <= entry)) or (direction == "short" and (stop is None or stop <= entry or target >= entry)):
            return None, "stop/target side validation failed"
        spread = _num(market.get("spread_bps"))
        max_spread = _num(self.execution.get("max_spread_bps"), 100.0) or 100.0
        if spread is not None and spread > max_spread: return None, "spread is too wide"
        if market.get("stale") is True or market.get("quote_stale") is True:
            return None, "market data is stale"
        # Existing planned stop risk is durable state. Never let a malformed
        # or over-cap book be bypassed by the new profile's sizing path.
        active = active_trades if isinstance(active_trades, Mapping) else {}
        open_risk = 0.0
        for held in positions:
            held_symbol = held.get("symbol")
            row = active.get(held_symbol)
            if not isinstance(row, Mapping):
                return None, "held position planned risk is unavailable"
            held_risk = _num(row.get("risk_usd"))
            if held_risk is None or held_risk < 0:
                return None, "held position planned risk is unavailable"
            open_risk += held_risk
        strategy_cfg = self.cfg.get("strategy", {}) if isinstance(self.cfg, Mapping) else {}
        profile = str(decision.get("execution_profile", decision.get(
            "profile", strategy_cfg.get("execution_profile", "shares")))).lower()
        budget = self._risk_usd(equity, decision)
        try:
            if profile in {"shares", "stock", "etf", "stock_etf", "stock_etf_shares"}:
                sized = self.size_shares(equity=equity, entry_price=entry, stop_distance=distance, symbol_data=market, risk_usd=budget)
                if sized["shares"] <= 0: return None, "risk budget cannot buy one share"
                contracts = sized["shares"]
                plan = {"execution_profile": "shares", "shares": contracts, "contracts": contracts,
                        "notional": sized["notional"], "risk_usd": sized["risk_usd"]}
            elif profile in {"options", "option", "defined_risk_options", "options_defined_risk"}:
                candidates = decision.get("option_chain") or market.get("option_chain") or market.get("options") or []
                option = self.size_options(equity=equity, risk_usd=budget, candidates=candidates, direction=direction, now=now_value)
                plan = {"execution_profile": "options", "contracts": option["contracts"],
                        "option": option, "contract_multiplier": option["multiplier"],
                        "max_loss": option["max_loss"], "risk_usd": option["risk_usd"],
                        "notional": option["debit"] * option["multiplier"] * option["contracts"]}
            else:
                return None, "unknown execution profile"
        except ValueError as exc:
            return None, str(exc)
        open_cap_pct = _num(self.r.get("max_open_risk_pct"))
        if open_cap_pct is None:
            open_cap_pct = _num(self.r.get("max_total_open_risk_pct"))
        if open_cap_pct is not None and open_risk + float(plan["risk_usd"]) > equity * open_cap_pct / 100.0 + 1e-9:
            return None, "max open risk cap reached"
        gross_cap_pct = _num(self.r.get("max_gross_exposure_pct"))
        if gross_cap_pct is not None and gross + float(plan["notional"]) > equity * gross_cap_pct / 100.0 + 1e-9:
            return None, "max gross exposure cap reached"
        # Account snapshots may provide realized/unrealized daily P&L.  A
        # negative loss at or beyond the configured stop can only close risk.
        daily_pnl = _num(decision.get("daily_pnl", decision.get("day_pnl")))
        daily_limit = _num(self.r.get("daily_loss_limit_pct"))
        if daily_pnl is not None and daily_limit is not None and daily_pnl <= -equity * daily_limit / 100.0:
            return None, "daily loss limit reached"
        stop_pct = distance / entry * 100.0
        tp_pct = abs(target - entry) / entry * 100.0
        plan.update({"symbol": symbol, "direction": direction, "entry_price": entry,
                     "stop_price": stop, "target_price": target, "sl_pct": stop_pct,
                     "tp_pct": tp_pct, "stop_loss_pct": stop_pct,
                     "take_profit_pct": tp_pct, "estimated_loss_pct": stop_pct,
                     "margin_pct_equity": plan["notional"] / equity * 100.0,
                     "force_flat": bool(decision.get("force_flat", True)),
                     "force_flat_at": decision.get("force_flat_at"),
                     "underlying_stop_price": stop, "underlying_target_price": target})
        return plan, None


def size_shares(equity: float, entry_price: float, stop_distance: float,
                risk_usd: float, risk_config: Mapping | None = None,
                symbol_data: Mapping | None = None) -> dict:
    """Functional wrapper for callers that do not need a stateful engine."""
    return RiskEngine({"risk": dict(risk_config or {})}).size_shares(
        equity=equity, entry_price=entry_price, stop_distance=stop_distance,
        risk_usd=risk_usd, symbol_data=symbol_data)


def select_option_contract(candidates, direction: str,
                           risk_config: Mapping | None = None,
                           now: float | None = None) -> dict:
    return RiskEngine({"risk": dict(risk_config or {})}).select_option_contract(
        candidates, direction=direction, now=now)


def size_options(equity: float, risk_usd: float, candidates,
                 direction: str, risk_config: Mapping | None = None,
                 now: float | None = None) -> dict:
    return RiskEngine({"risk": dict(risk_config or {})}).size_options(
        equity=equity, risk_usd=risk_usd, candidates=candidates,
        direction=direction, now=now)
