"""Deterministic sizing and execution-profile risk checks for IBR trades."""

from __future__ import annotations

import math
from numbers import Number
import re
import time
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timezone
from collections.abc import Mapping

from .instruments import validate_equity_symbol, validate_option_symbol

from .risk_inputs import (
    _OCC_OPTION_RE,
    _object_mapping,
    _normalize_option_candidate,
    _candidate_sequence,
    _num,
    _timestamp,
    _evaluation_timestamp,
    _expiration_date,
    _normalized_option_kind,
    _option_kind,
    _identity_alias_conflict,
)


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
        if "risk_usd" in decision and decision.get("risk_usd") is not None:
            explicit = _num(decision.get("risk_usd"))
            if explicit is None:
                raise ValueError("risk_usd measurement is invalid")
            return explicit
        pct = _num(self.r.get("risk_per_trade_pct"), 1.0) or 1.0
        return equity * pct / 100.0

    @staticmethod
    def _entry_stop(decision: Mapping, market: Mapping) -> tuple[float | None, float | None, float | None]:
        if any(isinstance(decision.get(name), bool)
               for name in ("entry_price", "stop_price", "stop_distance", "stop_loss_pct")):
            return None, None, None
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
        if isinstance(risk_usd, bool):
            raise ValueError("risk_usd measurement is invalid")
        if risk_usd is None:
            budget = self._risk_usd(equity, {})
        else:
            budget = _num(risk_usd)
            if budget is None:
                raise ValueError("risk_usd measurement is invalid")
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
                               now: float | None = None,
                               underlying: str | None = None) -> dict:
        """Select one liquid long call/put contract.

        Multi-leg debit spreads and short/naked structures remain explicitly
        unsupported until an order-leg model and reconciliation path exist.
        """
        if direction not in {"long", "short"}:
            raise ValueError("option direction must be long or short")
        requested_underlying = None
        if underlying is not None:
            try:
                requested_underlying = validate_equity_symbol(underlying)
            except ValueError as exc:
                raise ValueError("option underlying is invalid") from exc
        minimum, maximum, max_spread = self._option_limits()
        now_value = _evaluation_timestamp(now)
        wanted = "call" if direction == "long" else "put"
        accepted = []
        rejected = []
        for raw in _candidate_sequence(candidates):
            option = _normalize_option_candidate(raw)
            if option is None:
                rejected.append("not a mapping"); continue
            if option.get("_nested_identity_conflict"):
                rejected.append("option identity metadata mismatch"); continue
            # Indicative quotes are explicitly non-executable.  Provider
            # candidates carry their feed, while the no-clock fixture path is
            # retained for deterministic legacy sizing tests; a live/runtime
            # evaluation (``now`` supplied) must prove OPRA provenance.
            feed_value = option.get("feed")
            feed = str(getattr(feed_value, "value", feed_value) or "").strip().lower()
            if feed and feed != "opra":
                rejected.append("non-executable option feed"); continue
            if now is not None and feed != "opra":
                rejected.append("option feed entitlement unavailable"); continue
            # Freshness and identity aliases must agree rather than relying
            # on whichever provider field happens to win a precedence chain.
            flag_invalid = False
            for flag_name in ("stale", "quote_stale"):
                if flag_name in option and not isinstance(option[flag_name], bool):
                    flag_invalid = True
                    break
            if flag_invalid:
                rejected.append("malformed stale flag"); continue

            underlying_values = [option[name] for name in ("underlying_symbol", "underlying")
                                 if name in option and option[name] is not None and
                                 str(option[name]).strip()]
            candidate_underlyings = []
            try:
                candidate_underlyings = [validate_equity_symbol(value)
                                         for value in underlying_values]
            except ValueError:
                rejected.append("invalid option underlying"); continue
            if candidate_underlyings and any(
                    value != candidate_underlyings[0] for value in candidate_underlyings[1:]):
                rejected.append("option underlying metadata mismatch"); continue
            candidate_underlying = candidate_underlyings[0] if candidate_underlyings else None
            if (requested_underlying is not None and
                    candidate_underlying is not None and
                    candidate_underlying != requested_underlying):
                rejected.append("wrong underlying"); continue

            symbol = option.get("symbol")
            symbol_text = str(getattr(symbol, "value", symbol)).strip() if symbol is not None else ""
            occ = None
            if symbol_text:
                try:
                    normalized_symbol = validate_option_symbol(
                        symbol_text, requested_underlying or candidate_underlying)
                except ValueError:
                    rejected.append("invalid OCC option symbol"); continue
                option["symbol"] = normalized_symbol
                occ = _OCC_OPTION_RE.fullmatch(normalized_symbol)
                if occ is None:  # defensive: validate_option_symbol is the authority
                    rejected.append("invalid OCC option symbol"); continue
                occ_root = occ.group("root")
                if (candidate_underlying is not None and
                        candidate_underlying != occ_root):
                    rejected.append("wrong underlying"); continue
                candidate_underlying = candidate_underlying or occ_root

            raw_kinds = [option[name] for name in ("type", "right", "option_type")
                         if name in option and option[name] is not None]
            kinds = [_normalized_option_kind(value) for value in raw_kinds]
            if raw_kinds and any(kind is None for kind in kinds):
                rejected.append("invalid option right"); continue
            if kinds and any(kind != kinds[0] for kind in kinds[1:]):
                rejected.append("option right metadata mismatch"); continue
            kind = kinds[0] if kinds else None
            if occ is not None:
                occ_kind = "call" if occ.group("right") == "C" else "put"
                if kind is not None and kind != occ_kind:
                    rejected.append("option right metadata mismatch"); continue
                kind = occ_kind
            if kind is not None:
                option.setdefault("type", kind)
                option.setdefault("right", kind)
                option.setdefault("option_type", kind)
            strategy = str(option.get("strategy", option.get("structure", "single"))).lower()
            side = str(option.get("side", "buy")).lower()
            intent = str(option.get("position_intent", "buy_to_open")).lower()
            if intent not in {"buy_to_open", "buy_to_close"}:
                rejected.append("short option intent unsupported"); continue
            if strategy not in {"single", "long", "call", "put"}:
                rejected.append("multi-leg option structure unsupported"); continue
            if strategy in {"naked_short", "short", "sell", "credit_spread"} or side in {"sell", "short"}:
                rejected.append("naked short"); continue
            if kind != wanted:
                rejected.append("wrong right"); continue

            # Every supplied expiry alias must identify one calendar day, and
            # OCC identity (when present) is authoritative for that day.
            expiry_values = [option[name] for name in ("expiration", "expiration_date", "expiry")
                             if name in option and option[name] is not None]
            parsed_expiries = [_expiration_date(value) for value in expiry_values]
            if expiry_values and any(value is None for value in parsed_expiries):
                rejected.append("invalid option expiration"); continue
            if parsed_expiries and any(value != parsed_expiries[0] for value in parsed_expiries[1:]):
                rejected.append("option expiration metadata mismatch"); continue
            expiry = parsed_expiries[0] if parsed_expiries else None
            if occ is not None:
                try:
                    occ_expiry = date(2000 + int(occ.group("expiry")[:2]),
                                      int(occ.group("expiry")[2:4]),
                                      int(occ.group("expiry")[4:]))
                except ValueError:
                    rejected.append("invalid option expiration"); continue
                if expiry is not None and expiry != occ_expiry:
                    rejected.append("option expiration metadata mismatch"); continue
                expiry = occ_expiry
                strike_values = [option[name] for name in ("strike", "strike_price")
                                 if name in option and option[name] is not None]
                occ_strike = int(occ.group("strike")) / 1000.0
                if any(isinstance(value, bool) or _num(value) is None or
                       abs(_num(value) - occ_strike) > 0.0005
                       for value in strike_values):
                    rejected.append("option strike metadata mismatch"); continue

            supplied_dte = _num(option.get("dte")) if "dte" in option else None
            if ("dte" in option and
                    (option.get("dte") is None or isinstance(option.get("dte"), bool) or
                     supplied_dte is None)):
                rejected.append("invalid dte"); continue
            dte = supplied_dte
            if expiry is not None:
                dte = float((expiry - datetime.fromtimestamp(now_value, timezone.utc).date()).days)
                if supplied_dte is not None and abs(supplied_dte - dte) > 1e-9:
                    rejected.append("dte does not match expiration"); continue
            if dte is None or dte < minimum or dte > maximum:
                rejected.append("dte out of bounds"); continue
            if dte <= 0:
                rejected.append("0DTE"); continue

            stale = option.get("stale", False) or option.get("quote_stale", False)
            max_age = _num(self.execution.get("max_market_data_age_seconds"), 30) or 30
            quote_age_raw = option.get("quote_age_seconds") if "quote_age_seconds" in option else None
            if isinstance(quote_age_raw, bool):
                rejected.append("invalid quote age"); continue
            quote_age = _num(quote_age_raw)
            if "quote_age_seconds" in option and quote_age is None:
                rejected.append("invalid quote age"); continue
            quote_time_keys = [name for name in ("quote_ts", "quote_timestamp", "timestamp")
                               if name in option]
            quote_times = []
            timestamp_invalid = False
            for quote_time_key in quote_time_keys:
                quote_raw = option[quote_time_key]
                quote_ts = _timestamp(quote_raw)
                if quote_raw is not None and quote_ts is None:
                    rejected.append("invalid quote timestamp")
                    timestamp_invalid = True
                    break
                if quote_ts is not None:
                    quote_times.append(quote_ts)
            if timestamp_invalid:
                continue
            if quote_time_keys:
                if quote_times:
                    derived_ages = [now_value - quote_ts for quote_ts in quote_times]
                    if any(age < 0 for age in derived_ages):
                        rejected.append("future quote timestamp"); continue
                    # Every timestamp alias describes the same quote. A
                    # future or materially older alias cannot be hidden by a
                    # fresh value in the first alias position.
                    if max(derived_ages) - min(derived_ages) > 2.0:
                        rejected.append("quote timestamp metadata mismatch"); continue
                    if (quote_age is not None and
                            any(abs(quote_age - age) > 2.0 for age in derived_ages)):
                        rejected.append("quote age mismatch"); continue
                    quote_age = max(derived_ages)
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
            explicit_debits = []
            debit_invalid = False
            for debit_key in ("debit", "net_debit"):
                if debit_key not in option:
                    continue
                explicit_debit = _num(option[debit_key])
                if explicit_debit is None or explicit_debit <= 0:
                    rejected.append("invalid debit")
                    debit_invalid = True
                    break
                explicit_debits.append(explicit_debit)
            if debit_invalid:
                continue
            if explicit_debits and any(
                    abs(value - explicit_debits[0]) > 1e-9
                    for value in explicit_debits[1:]):
                rejected.append("debit metadata mismatch")
                continue
            debit = explicit_debits[0] if explicit_debits else ask
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
            multiplier = _num(option.get("multiplier", option.get(
                "contract_multiplier", option.get("contract_size", option.get("size")))))
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
                     candidates, direction: str, now: float | None = None,
                     underlying: str | None = None) -> dict:
        equity_value = _num(equity)
        if equity_value is None or equity_value <= 0:
            raise ValueError("equity measurement is invalid")
        risk_value = _num(risk_usd)
        if risk_value is None or risk_value <= 0:
            raise ValueError("risk_usd measurement is invalid")
        if isinstance(equity, bool):
            raise ValueError("equity measurement is invalid")
        if isinstance(risk_usd, bool):
            raise ValueError("risk_usd measurement is invalid")
        rows = list(_candidate_sequence(candidates))
        # Provider-generated candidates carry quote_age_seconds/quote_ts.  A
        # bare offline fixture predating those fields remains usable without
        # an artificial clock, while any timestamped candidate is checked
        # strictly against ``now``.
        has_freshness = False
        for row in rows:
            normalized = _normalize_option_candidate(row)
            if normalized and any(key in normalized for key in (
                    "quote_ts", "quote_timestamp", "quote_age_seconds", "timestamp")):
                has_freshness = True
                break
        evaluation_now = None if now is None else _evaluation_timestamp(now)
        contract = self.select_option_contract(rows, direction=direction,
                                               now=evaluation_now if has_freshness else None,
                                               underlying=underlying)
        max_loss = float(contract["max_loss_per_contract"])
        contracts = math.floor(risk_value / max_loss)
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
        try:
            now_value = _evaluation_timestamp(now)
        except ValueError as exc:
            return None, str(exc)
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
        if any(flag in market and not isinstance(market.get(flag), bool)
               for flag in ("stale", "quote_stale")):
            return None, "market freshness flag is invalid"
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
        try:
            budget = self._risk_usd(equity, decision)
        except ValueError as exc:
            return None, str(exc)
        try:
            if profile in {"shares", "stock", "etf", "stock_etf", "stock_etf_shares"}:
                sized = self.size_shares(equity=equity, entry_price=entry, stop_distance=distance, symbol_data=market, risk_usd=budget)
                if sized["shares"] <= 0: return None, "risk budget cannot buy one share"
                contracts = sized["shares"]
                plan = {"execution_profile": "shares", "shares": contracts, "contracts": contracts,
                        "notional": sized["notional"], "risk_usd": sized["risk_usd"]}
            elif profile in {"options", "option", "defined_risk_options", "options_defined_risk"}:
                candidates = decision.get("option_chain") or market.get("option_chain") or market.get("options") or []
                option = self.size_options(equity=equity, risk_usd=budget, candidates=candidates,
                                            direction=direction, now=now_value,
                                            underlying=symbol)
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
        pnl_key = "daily_pnl" if "daily_pnl" in decision else (
            "day_pnl" if "day_pnl" in decision else None)
        daily_pnl = None
        if pnl_key is not None and decision.get(pnl_key) is not None:
            daily_pnl = _num(decision.get(pnl_key))
            if daily_pnl is None:
                return None, "daily P&L measurement is invalid"
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
                     "max_hold_bars": decision.get("max_hold_bars"),
                     "hold_deadline_ts": decision.get("hold_deadline_ts"),
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
                           now: float | None = None,
                           underlying: str | None = None) -> dict:
    return RiskEngine({"risk": dict(risk_config or {})}).select_option_contract(
        candidates, direction=direction, now=now, underlying=underlying)


def size_options(equity: float, risk_usd: float, candidates,
                 direction: str, risk_config: Mapping | None = None,
                 now: float | None = None,
                 underlying: str | None = None) -> dict:
    return RiskEngine({"risk": dict(risk_config or {})}).size_options(
        equity=equity, risk_usd=risk_usd, candidates=candidates,
        direction=direction, now=now, underlying=underlying)
