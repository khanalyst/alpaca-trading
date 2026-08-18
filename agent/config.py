"""Fail-closed configuration for the Alpaca trading runtime."""

from __future__ import annotations

from copy import deepcopy
from datetime import time
import json
from pathlib import Path
import re
from typing import Any, Mapping
import os
from urllib.parse import urlparse

from .governance import GovernanceError, validate_promotions
from .instruments import validate_asset_class, validate_equity_symbol

class ConfigError(ValueError):
    pass


def _map(value: Any, path: str) -> dict:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{path} must be a mapping")
    return dict(value)


def _unknown(block: Mapping[str, Any], allowed: set[str], path: str) -> None:
    extra = sorted(set(block) - allowed)
    if extra:
        raise ConfigError(f"{path} has unknown field(s): {', '.join(extra)}")


def _bool(block: Mapping[str, Any], key: str, path: str, default=None) -> bool:
    value = block.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{path}.{key} must be true or false")
    return value


def _num(block: Mapping[str, Any], key: str, path: str, lo: float, hi: float, default=None) -> float:
    value = block.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= float(value) <= hi:
        raise ConfigError(f"{path}.{key} must be a number between {lo:g} and {hi:g}")
    return float(value)


def _int(block: Mapping[str, Any], key: str, path: str, lo: int, hi: int, default=None) -> int:
    value = block.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ConfigError(f"{path}.{key} must be an integer between {lo} and {hi}")
    return value


def _feed(value: Any, *, options: bool = False) -> str:
    # SIP/OPRA are the only feeds with the complete executable market view.
    # Keep the parser's fallback fail-closed as well as the shipped config:
    # an omitted feed must never silently select a partial/indicative stream.
    raw = str(value or ("opra" if options else "sip")).strip().lower().replace("-", "_")
    if raw == "delayed":
        raw = "delayed_sip"
    allowed = {"indicative", "opra"} if options else {"iex", "sip", "delayed_sip"}
    if raw not in allowed:
        raise ConfigError(f"unsupported {'option' if options else 'equity'} data feed: {value!r}")
    return raw


DEFAULT_CONFIG: dict[str, Any] = {
    "mode": "paper",
    "broker": {"paper": True, "allow_live": False, "data_feed": "sip", "options_feed": "opra"},
    "session": {"timezone": "America/New_York", "entries_regular_session_only": True, "allow_exits_outside_session": True, "require_exact_calendar": True, "force_flat_minutes_before_close": 10, "reject_new_entries_minutes_before_close": 5},
    "universe": {"symbols": ["SPY", "QQQ", "IWM", "DIA", "XLF", "XLK", "XLE", "XLV"], "asset_classes": ["us_equity", "us_option"], "min_price": 1.0, "max_symbols": 50, "denylist": []},
    "strategy": {"id": "rule", "version": "v1", "variant_id": "auto", "selection_mode": "all_proved", "pinned": [], "execution_mode": "shares", "range_minutes": 15, "breakout_buffer_bps": 5, "min_relative_volume": 1.0, "target_r": 2.0, "max_entry_extension_r": 1.0, "min_ibr_width_atr": 0.25, "max_ibr_width_atr": 3.0, "atr_period": 14, "max_spread_bps": 25.0, "stale_minutes": 60, "latest_entry_time": "15:00", "force_flat_minutes_before_close": 10},
    "risk": {"risk_per_trade_pct": 0.5, "daily_loss_limit_pct": 2.0, "max_open_risk_pct": 2.0, "max_concurrent_positions": 3, "max_position_notional_pct": 25.0, "options_min_dte": 7, "options_max_dte": 60, "options_max_spread_pct": 10.0, "min_confidence": 0.0},
    "execution": {"order_type": "market", "time_in_force": "day", "client_order_id_prefix": "edge", "max_slippage_bps": 50, "max_market_data_age_seconds": 30, "max_spread_bps": 100},
    "costs": {"spread_bps": 4.0, "slippage_bps": 6.0, "fee_bps": 0.5,
              "option_fee_per_contract_side": 0.65,
              "provenance": "shipped_conservative_v1_plus_25bps_stress"},
    # Runtime rule execution stays deterministic.  The separate decision-LLM
    # boundary remains disabled unless explicitly enabled.
    "llm": {"enabled": False, "provider": "openai", "model": "", "temperature": 0.2, "max_tokens": 2000, "timeout_seconds": 10.0},
    "research": {
        "enabled": True, "require_validated_variant": True,
        "backtest_bar_fallback": True,
        "champion_min_confidence": 0.95,
        "strategy_llm": {"enabled": True, "provider": "openai", "model": "gpt-5",
                         "max_attempts": 1, "timeout_seconds": 30,
                         "max_response_bytes": 16_384,
                         "max_total_calls": 64,
                         "near_duplicate_distance": 0.001},
        "proof": {"directory": "research/results/edges", "webhook_url": "",
                  "webhook_timeout_seconds": 10},
        # The paper-account trial window. An auto-lane edge trades the same
        # Alpaca paper account for this long before its live record is judged;
        # a pinned edge is never judged here at all.
        "trial": {"enabled": True, "min_sessions": 20, "min_trades": 20,
                  "min_mean_r": 0.0, "min_total_r": 0.0},
    },
    "cycle": {"interval_seconds": 60},
}


def validate_config(raw: Mapping[str, Any]) -> dict:
    cfg = deepcopy(_map(raw, "config"))
    allowed = {"mode", "broker", "data", "session", "universe", "strategy", "risk", "execution", "costs", "llm", "research", "cycle"}
    _unknown(cfg, allowed, "config")
    mode = str(cfg.get("mode", "paper")).strip().lower()
    if mode not in {"paper", "live"}:
        raise ConfigError("mode must be paper or live")
    out = deepcopy(DEFAULT_CONFIG)
    for key, value in cfg.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            merged = deepcopy(out[key])
            merged.update(value)
            out[key] = merged
        else:
            out[key] = value
    out["mode"] = mode

    broker = _map(out.get("broker"), "broker")
    _unknown(broker, {"paper", "allow_live", "data_feed", "options_feed", "api_key", "secret_key", "endpoint"}, "broker")
    paper = _bool(broker, "paper", "broker", mode == "paper")
    allow_live = _bool(broker, "allow_live", "broker", False)
    paper_env = os.getenv("ALPACA_PAPER")
    truthy = {"1", "true", "yes", "on"}
    if mode == "paper":
        if paper is not True or allow_live is not False:
            raise ConfigError("paper mode requires broker.paper=true and broker.allow_live=false")
        if paper_env is not None and paper_env.strip().lower() not in truthy:
            raise ConfigError("paper mode requires ALPACA_PAPER=true when set")
    else:
        if paper is not False or allow_live is not True:
            raise ConfigError("live mode requires broker.paper=false and broker.allow_live=true")
        if os.getenv("ALPACA_LIVE_ENABLE", "").strip().lower() not in truthy:
            raise ConfigError("live mode requires ALPACA_LIVE_ENABLE=true")
        if paper_env is not None and paper_env.strip().lower() in truthy:
            raise ConfigError("live mode conflicts with ALPACA_PAPER=true")
    if broker.get("endpoint"):
        raise ConfigError("broker.endpoint overrides are disabled")
    broker.update(paper=paper, allow_live=allow_live)
    broker["data_feed"] = _feed(os.getenv("ALPACA_DATA_FEED") or os.getenv("ALPACA_STOCK_FEED") or broker.get("data_feed"))
    broker["options_feed"] = _feed(os.getenv("ALPACA_OPTIONS_FEED") or broker.get("options_feed"), options=True)
    out["broker"] = broker

    data = out.get("data")
    if data is not None:
        data = _map(data, "data")
        _unknown(data, {"feed", "options_feed"}, "data")
        if "feed" in data:
            data["feed"] = _feed(data["feed"])
        if "options_feed" in data:
            data["options_feed"] = _feed(data["options_feed"], options=True)
        out["data"] = data
        # ``data`` is the legacy alias accepted by provider callers. Respect
        # it when broker did not explicitly choose a feed; otherwise the
        # merged broker defaults would silently mask an indicative override.
        raw_broker = cfg.get("broker")
        raw_broker = raw_broker if isinstance(raw_broker, Mapping) else {}
        if ("feed" in data and "data_feed" not in raw_broker and
                not (os.getenv("ALPACA_DATA_FEED") or os.getenv("ALPACA_STOCK_FEED"))):
            broker["data_feed"] = data["feed"]
        if ("options_feed" in data and "options_feed" not in raw_broker and
                not os.getenv("ALPACA_OPTIONS_FEED")):
            broker["options_feed"] = data["options_feed"]
    session = _map(out.get("session"), "session")
    _unknown(session, {"timezone", "entries_regular_session_only", "allow_exits_outside_session", "require_exact_calendar", "force_flat_minutes_before_close", "reject_new_entries_minutes_before_close"}, "session")
    if session.get("timezone") != "America/New_York":
        raise ConfigError("session.timezone must be America/New_York")
    session["entries_regular_session_only"] = _bool(session, "entries_regular_session_only", "session", True)
    session["allow_exits_outside_session"] = _bool(session, "allow_exits_outside_session", "session", True)
    if session["entries_regular_session_only"] is not True:
        raise ConfigError("session.entries_regular_session_only must be true")
    if session["allow_exits_outside_session"] is not True:
        raise ConfigError("session.allow_exits_outside_session must be true")
    session["require_exact_calendar"] = _bool(
        session, "require_exact_calendar", "session", True)
    session["force_flat_minutes_before_close"] = _int(session, "force_flat_minutes_before_close", "session", 1, 240, 10)
    session["reject_new_entries_minutes_before_close"] = _int(session, "reject_new_entries_minutes_before_close", "session", 0, 240, 5)
    out["session"] = session

    universe = _map(out.get("universe"), "universe")
    _unknown(universe, {"symbols", "asset_classes", "min_price", "max_symbols", "denylist"}, "universe")
    symbols = universe.get("symbols", [])
    if not isinstance(symbols, list) or any(not isinstance(x, str) or not x.strip() for x in symbols):
        raise ConfigError("universe.symbols must be a list of non-empty symbols")
    try:
        universe["symbols"] = [validate_equity_symbol(x) for x in symbols]
    except ValueError as exc:
        raise ConfigError(f"universe.symbols: {exc}") from exc
    classes = universe.get("asset_classes", ["us_equity"])
    if not isinstance(classes, list):
        raise ConfigError("universe.asset_classes must be a list")
    try:
        universe["asset_classes"] = [validate_asset_class(x) for x in classes]
    except ValueError as exc:
        raise ConfigError(f"universe.asset_classes: {exc}") from exc
    if not universe["asset_classes"]:
        raise ConfigError("universe.asset_classes must not be empty")
    universe["min_price"] = _num(universe, "min_price", "universe", 0, 1_000_000, 1.0)
    universe["max_symbols"] = _int(universe, "max_symbols", "universe", 1, 10_000, 50)
    if not isinstance(universe.get("denylist", []), list):
        raise ConfigError("universe.denylist must be a list")
    try:
        universe["denylist"] = [validate_equity_symbol(x) for x in universe.get("denylist", [])]
    except ValueError as exc:
        raise ConfigError(f"universe.denylist: {exc}") from exc
    out["universe"] = universe

    # Autonomous research and execution require the complete entitled feeds.
    # IEX/indicative remain valid only for explicitly non-research, equity-only
    # diagnostics; they are never allowed to become runtime evidence for a
    # configured research or option lane.
    research_requested = bool(
        isinstance(out.get("research"), Mapping)
        and out.get("research", {}).get("enabled", True))
    option_lane = any(str(item).lower() in {"us_option", "option", "options"}
                      for item in universe["asset_classes"])
    if research_requested and broker["data_feed"] != "sip":
        raise ConfigError(
            "research.enabled requires the SIP equity feed entitlement; "
            "set broker.data_feed=\"sip\" or ALPACA_DATA_FEED=sip")
    if (option_lane and research_requested and
            broker["options_feed"] != "opra"):
        raise ConfigError(
            "option research requires the OPRA feed entitlement; "
            "set broker.options_feed=\"opra\" or ALPACA_OPTIONS_FEED=opra")

    strategy = _map(out.get("strategy"), "strategy")
    _unknown(strategy, {"id", "version", "variant_id", "selection_mode", "execution_mode", "rule_spec", "pinned", "range_minutes", "breakout_buffer_bps", "min_relative_volume", "target_r", "max_entry_extension_r", "min_ibr_width_atr", "max_ibr_width_atr", "atr_period", "max_ibr_width_pct", "max_spread_bps", "stale_minutes", "latest_entry_time", "force_flat_minutes_before_close"}, "strategy")
    if not isinstance(strategy.get("id"), str) or not strategy["id"].strip():
        raise ConfigError("strategy.id must be a non-empty string")
    if strategy.get("execution_mode", "shares") not in {"shares", "options"}:
        raise ConfigError("strategy.execution_mode must be shares or options")
    if strategy.get("selection_mode", "all_proved") not in {
            "specific", "all_proved", "pinned"}:
        raise ConfigError(
            "strategy.selection_mode must be specific, all_proved, or pinned")
    # Operator-declared promotions.  Validated here so a malformed promotion
    # fails the preflight rather than silently trading nothing.
    try:
        strategy["pinned"] = validate_promotions(strategy.get("pinned"))
    except GovernanceError as exc:
        raise ConfigError(str(exc)) from exc
    if strategy.get("selection_mode") == "pinned" and not strategy["pinned"]:
        raise ConfigError(
            "strategy.selection_mode=pinned requires at least one "
            "strategy.pinned entry naming a variant_id")
    if strategy["pinned"] and strategy.get("selection_mode") != "pinned":
        raise ConfigError(
            "strategy.pinned entries are only used by "
            "strategy.selection_mode=pinned; set it or remove them")
    strategy["range_minutes"] = _int(strategy, "range_minutes", "strategy", 1, 240, 15)
    strategy["breakout_buffer_bps"] = _num(strategy, "breakout_buffer_bps", "strategy", 0, 500, 5)
    strategy["min_relative_volume"] = _num(strategy, "min_relative_volume", "strategy", 0, 100, 1)
    strategy["target_r"] = _num(strategy, "target_r", "strategy", 0.1, 100, 2)
    strategy["max_entry_extension_r"] = _num(strategy, "max_entry_extension_r", "strategy", 0, 100, 1)
    strategy["min_ibr_width_atr"] = _num(strategy, "min_ibr_width_atr", "strategy", 0, 100, .25)
    strategy["max_ibr_width_atr"] = _num(strategy, "max_ibr_width_atr", "strategy", 0, 100, 3)
    strategy["atr_period"] = _int(strategy, "atr_period", "strategy", 1, 500, 14)
    strategy["max_spread_bps"] = _num(strategy, "max_spread_bps", "strategy", 0, 10_000, 25)
    strategy["stale_minutes"] = _int(strategy, "stale_minutes", "strategy", 1, 1440, 60)
    strategy["force_flat_minutes_before_close"] = _int(
        strategy, "force_flat_minutes_before_close", "strategy", 1, 240,
        session["force_flat_minutes_before_close"])
    if strategy["min_ibr_width_atr"] > strategy["max_ibr_width_atr"]:
        raise ConfigError("strategy.min_ibr_width_atr cannot exceed max_ibr_width_atr")
    if (str(strategy.get("execution_mode", "shares")).lower() in {"options", "option"}
            and broker["options_feed"] != "opra"):
        raise ConfigError(
            "option execution requires the OPRA feed entitlement; "
            "set broker.options_feed=\"opra\" or ALPACA_OPTIONS_FEED=opra")
    latest_entry_time = strategy.get("latest_entry_time")
    if (not isinstance(latest_entry_time, str) or
            re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", latest_entry_time) is None):
        raise ConfigError("strategy.latest_entry_time must be HH:MM")
    # Parse as a second strict guard against accepting platform-specific time
    # spellings while preserving the canonical HH:MM string in config.
    try:
        time.fromisoformat(latest_entry_time)
    except ValueError as exc:
        raise ConfigError("strategy.latest_entry_time must be HH:MM") from exc
    out["strategy"] = strategy

    risk = _map(out.get("risk"), "risk")
    _unknown(risk, {"risk_per_trade_pct", "daily_loss_limit_pct", "max_open_risk_pct", "max_total_open_risk_pct", "max_concurrent_positions", "max_position_notional_pct", "options_min_dte", "options_max_dte", "options_max_spread_pct", "max_gross_exposure_pct", "min_confidence"}, "risk")
    risk["risk_per_trade_pct"] = _num(risk, "risk_per_trade_pct", "risk", .001, 100, .5)
    risk["daily_loss_limit_pct"] = _num(risk, "daily_loss_limit_pct", "risk", .01, 100, 2)
    risk["max_open_risk_pct"] = _num(risk, "max_open_risk_pct", "risk", .01, 100, 2)
    risk["max_concurrent_positions"] = _int(risk, "max_concurrent_positions", "risk", 1, 100, 3)
    risk["max_position_notional_pct"] = _num(risk, "max_position_notional_pct", "risk", .1, 1000, 25)
    risk["options_min_dte"] = _int(risk, "options_min_dte", "risk", 0, 3650, 7)
    risk["options_max_dte"] = _int(risk, "options_max_dte", "risk", 1, 3650, 60)
    if risk["options_min_dte"] > risk["options_max_dte"]:
        raise ConfigError("risk.options_min_dte cannot exceed options_max_dte")
    risk["options_max_spread_pct"] = _num(risk, "options_max_spread_pct", "risk", 0, 100, 10)
    risk["min_confidence"] = _num(risk, "min_confidence", "risk", 0, 1, 0)
    out["risk"] = risk

    execution = _map(out.get("execution"), "execution")
    _unknown(execution, {"order_type", "time_in_force", "client_order_id_prefix", "max_slippage_bps", "max_market_data_age_seconds", "max_spread_bps"}, "execution")
    if execution.get("order_type", "market") not in {"market", "limit"}:
        raise ConfigError("execution.order_type must be market or limit")
    if execution.get("time_in_force", "day") != "day":
        raise ConfigError("execution.time_in_force must be day")
    if not isinstance(execution.get("client_order_id_prefix", "ibr"), str) or not execution["client_order_id_prefix"]:
        raise ConfigError("execution.client_order_id_prefix must be non-empty")
    execution["max_slippage_bps"] = _num(execution, "max_slippage_bps", "execution", 0, 10_000, 50)
    # A quote older than one recorder cycle is not an executable authorization
    # boundary.  Lower values are valid for stricter deployments; relaxing
    # beyond thirty seconds would let research and runtime disagree.
    execution["max_market_data_age_seconds"] = _num(
        execution, "max_market_data_age_seconds", "execution", 1, 30, 30)
    execution["max_spread_bps"] = _num(execution, "max_spread_bps", "execution", 0, 10_000, 100)
    out["execution"] = execution

    costs = out.get("costs")
    if costs is not None:
        # The shared research cost model owns the field set and the caps; this
        # only makes the block reachable from the checked-in configuration
        # instead of a --config JSON override, and fails closed on its errors.
        from research.costs import CostError, CostModel
        raw_costs = _map(costs, "costs")
        explicit_costs = (_map(cfg.get("costs"), "costs")
                          if "costs" in cfg else None)
        model_costs = dict(raw_costs)
        # Once an operator changes any cost input, the shipped schedule's
        # provenance label is no longer truthful unless they explicitly name
        # their replacement calibration/source.
        if explicit_costs is not None and "provenance" not in explicit_costs:
            model_costs["provenance"] = "config"
        try:
            model = CostModel.from_config({"costs": model_costs,
                                           "execution": execution})
        except CostError as exc:
            raise ConfigError(f"costs: {exc}") from exc
        normalized_costs = {"spread_bps": model.spread_bps,
                            "slippage_bps": model.slippage_bps,
                            "fee_bps": model.fee_bps,
                            "option_fee_per_contract_side":
                                model.option_fee_per_contract_side,
                            "provenance": model.provenance}
        out["costs"] = normalized_costs

    llm = _map(out.get("llm"), "llm")
    _unknown(llm, {"enabled", "provider", "model", "temperature", "max_tokens", "timeout_seconds", "base_url"}, "llm")
    llm["enabled"] = _bool(llm, "enabled", "llm", False)
    if llm.get("provider") not in {"openai", "anthropic"}:
        raise ConfigError("llm.provider must be openai or anthropic")
    if not isinstance(llm.get("model"), str):
        raise ConfigError("llm.model must be a string")
    if llm["enabled"] and not llm["model"].strip():
        raise ConfigError("llm.model is required when llm.enabled=true")
    llm["temperature"] = _num(llm, "temperature", "llm", 0, 2, .2)
    llm["max_tokens"] = _int(llm, "max_tokens", "llm", 128, 32_000, 2_000)
    llm["timeout_seconds"] = _num(llm, "timeout_seconds", "llm", .1, 120, 10)
    out["llm"] = llm
    research = _map(out.get("research"), "research")
    _unknown(research, {"enabled", "require_validated_variant", "backtest_bar_fallback", "champion_min_confidence", "db_path", "strategy_llm", "proof", "trial"}, "research")
    research["enabled"] = _bool(research, "enabled", "research", True)
    research["require_validated_variant"] = _bool(
        research, "require_validated_variant", "research", True)
    research["backtest_bar_fallback"] = _bool(
        research, "backtest_bar_fallback", "research", True)
    research["champion_min_confidence"] = _num(
        research, "champion_min_confidence", "research", 0, 1, .95)
    if research.get("db_path") is not None and not isinstance(research["db_path"], str):
        raise ConfigError("research.db_path must be a path string")
    strategy_llm_defaults = DEFAULT_CONFIG["research"]["strategy_llm"]
    strategy_llm = dict(strategy_llm_defaults)
    strategy_llm.update(_map(research.get("strategy_llm", {}), "research.strategy_llm"))
    _unknown(strategy_llm, {"enabled", "provider", "model", "max_attempts",
                            "timeout_seconds", "max_response_bytes",
                            "max_total_calls", "near_duplicate_distance"},
             "research.strategy_llm")
    strategy_llm["enabled"] = _bool(
        strategy_llm, "enabled", "research.strategy_llm", False)
    if strategy_llm.get("provider") not in {"openai", "anthropic"}:
        raise ConfigError("research.strategy_llm.provider must be openai or anthropic")
    if not isinstance(strategy_llm.get("model"), str):
        raise ConfigError("research.strategy_llm.model must be a string")
    if strategy_llm["enabled"] and not strategy_llm["model"].strip():
        raise ConfigError("research.strategy_llm.model is required when enabled=true")
    strategy_llm["max_attempts"] = _int(
        strategy_llm, "max_attempts", "research.strategy_llm", 1, 3, 1)
    strategy_llm["timeout_seconds"] = _num(
        strategy_llm, "timeout_seconds", "research.strategy_llm", 1, 120, 30)
    strategy_llm["max_response_bytes"] = _int(
        strategy_llm, "max_response_bytes", "research.strategy_llm", 1024, 65_536, 16_384)
    strategy_llm["max_total_calls"] = _int(
        strategy_llm, "max_total_calls", "research.strategy_llm", 1, 256, 64)
    strategy_llm["near_duplicate_distance"] = _num(
        strategy_llm, "near_duplicate_distance", "research.strategy_llm", 0, 1,
        0.001)
    proof_defaults = DEFAULT_CONFIG["research"]["proof"]
    proof = dict(proof_defaults)
    proof.update(_map(research.get("proof", {}), "research.proof"))
    _unknown(proof, {"directory", "webhook_url", "webhook_timeout_seconds"},
             "research.proof")
    if not isinstance(proof.get("directory"), str) or not proof["directory"].strip():
        raise ConfigError("research.proof.directory must be a non-empty string")
    if not isinstance(proof.get("webhook_url"), str):
        raise ConfigError("research.proof.webhook_url must be a string")
    webhook = proof["webhook_url"].strip()
    if webhook:
        parsed = urlparse(webhook)
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            raise ConfigError("research.proof.webhook_url must use HTTPS")
    proof["webhook_url"] = webhook
    trial_defaults = DEFAULT_CONFIG["research"]["trial"]
    trial = dict(trial_defaults)
    trial.update(_map(research.get("trial", {}), "research.trial"))
    _unknown(trial, {"enabled", "min_sessions", "min_trades", "min_mean_r",
                     "min_total_r"}, "research.trial")
    trial["enabled"] = _bool(trial, "enabled", "research.trial", True)
    # A trial that concludes from a handful of fills is measuring noise, and
    # parking an edge on noise costs more search than it saves.
    trial["min_sessions"] = _int(trial, "min_sessions", "research.trial", 5, 500, 20)
    trial["min_trades"] = _int(trial, "min_trades", "research.trial", 5, 5_000, 20)
    trial["min_mean_r"] = _num(trial, "min_mean_r", "research.trial", -10, 10, 0.0)
    trial["min_total_r"] = _num(trial, "min_total_r", "research.trial", -1_000, 1_000, 0.0)
    research["trial"] = trial
    proof["webhook_timeout_seconds"] = _num(
        proof, "webhook_timeout_seconds", "research.proof", 1, 30, 10)
    research["strategy_llm"] = strategy_llm
    research["proof"] = proof
    out["research"] = research
    if mode == "live":
        selection = strategy.get("selection_mode")
        # Live pins exactly one edge, by either route.  ``pinned`` carries an
        # operator-assigned promotion id, so the audit trail can answer "who
        # put this in production and when"; ``specific`` remains supported
        # unchanged for configurations written before promotions existed.
        if selection == "pinned":
            if len(strategy["pinned"]) != 1:
                raise ConfigError(
                    "live mode requires exactly one strategy.pinned entry")
        elif selection == "specific":
            variant_id = str(strategy.get("variant_id") or "").strip()
            if not variant_id or variant_id.lower() == "auto":
                raise ConfigError("live mode requires a named strategy.variant_id")
        else:
            raise ConfigError(
                "live mode requires strategy.selection_mode=specific or pinned")
        if not research["enabled"] or not research["require_validated_variant"]:
            raise ConfigError("live mode requires the validated research edge gate")
        if strategy.get("execution_mode") == "options":
            raise ConfigError(
                "live mode rejects strategy.execution_mode=options: Alpaca offers no bracket, "
                "OCO, or stop order of any kind on options, so an option position's protective "
                "stop can only be the local 60-second poller, which dies with the process. The "
                "live canary is equities-only until the deploy/watchdog.py flatten path has soak "
                "evidence; run the options profile on paper")
        if llm["enabled"]:
            raise ConfigError(
                "live mode requires llm.enabled=false: the validated edge was proven with the "
                "deterministic rule and no LLM in the loop, so a runtime LLM veto would deploy a "
                "strategy that differs from the one that passed the gates")
    cycle = _map(out.get("cycle"), "cycle")
    _unknown(cycle, {"interval_seconds"}, "cycle")
    cycle["interval_seconds"] = _num(cycle, "interval_seconds", "cycle", .1, 86_400, 60)
    out["cycle"] = cycle
    from .registry import validate_contract_config
    validate_contract_config(out)
    return out


def load_config(path: str | Path) -> dict:
    source = Path(path).read_text(encoding="utf-8")
    try:
        raw = json.loads(source)
    except json.JSONDecodeError as json_error:
        try:
            import yaml
        except ImportError as exc:
            raise ConfigError(
                f"{path}: PyYAML is unavailable and the configuration is not JSON"
            ) from exc
        try:
            raw = yaml.safe_load(source) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML: {exc}") from exc
    return validate_config(raw or {})
