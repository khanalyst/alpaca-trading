"""Fail-closed configuration for the Alpaca paper-trading runtime."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping
import os

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
    raw = str(value or ("indicative" if options else "iex")).strip().lower().replace("-", "_")
    if raw == "delayed":
        raw = "delayed_sip"
    allowed = {"indicative", "opra"} if options else {"iex", "sip", "delayed_sip"}
    if raw not in allowed:
        raise ConfigError(f"unsupported {'option' if options else 'equity'} data feed: {value!r}")
    return raw


DEFAULT_CONFIG: dict[str, Any] = {
    "mode": "paper",
    "broker": {"paper": True, "data_feed": "iex", "options_feed": "indicative"},
    "session": {"timezone": "America/New_York", "entries_regular_session_only": True, "allow_exits_outside_session": True, "force_flat_minutes_before_close": 10, "reject_new_entries_minutes_before_close": 5},
    "universe": {"symbols": ["SPY", "QQQ"], "asset_classes": ["us_equity", "us_option"], "min_price": 1.0, "max_symbols": 50, "denylist": []},
    "strategy": {"id": "ibr", "range_minutes": 15, "breakout_buffer_bps": 5, "min_relative_volume": 1.0, "target_r": 2.0, "max_entry_extension_r": 1.0, "min_ibr_width_atr": 0.25, "max_ibr_width_atr": 3.0, "latest_entry_time": "15:00", "force_flat_minutes_before_close": 10},
    "risk": {"risk_per_trade_pct": 0.5, "daily_loss_limit_pct": 2.0, "max_open_risk_pct": 2.0, "max_concurrent_positions": 3, "max_position_notional_pct": 25.0, "options_min_dte": 7, "options_max_dte": 60, "options_max_spread_pct": 10.0},
    "execution": {"order_type": "market", "time_in_force": "day", "client_order_id_prefix": "ibr", "max_slippage_bps": 50},
    "llm": {"provider": "openai", "model": "gpt-5.6-sol-coding", "temperature": 0.2, "max_tokens": 2000},
}


def validate_config(raw: Mapping[str, Any], *, allow_shadow_strategy: bool = False) -> dict:
    del allow_shadow_strategy
    cfg = deepcopy(_map(raw, "config"))
    allowed = {"mode", "broker", "data", "session", "universe", "strategy", "risk", "execution", "llm", "alerts", "research", "cycle"}
    _unknown(cfg, allowed, "config")
    mode = cfg.get("mode", "paper")
    if mode != "paper":
        raise ConfigError("only paper mode is supported; live trading is disabled")
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
    _unknown(broker, {"paper", "data_feed", "options_feed", "api_key", "secret_key", "endpoint"}, "broker")
    paper = _bool(broker, "paper", "broker", True)
    if not paper:
        raise ConfigError("paper mode requires broker.paper=true")
    paper_env = os.getenv("ALPACA_PAPER")
    if paper_env is not None and paper_env.strip().lower() not in {"1", "true", "yes", "on"}:
        raise ConfigError("ALPACA_PAPER must be true; live trading is disabled")
    if broker.get("endpoint"):
        raise ConfigError("broker.endpoint overrides are disabled")
    broker.update(paper=True)
    broker["data_feed"] = _feed(os.getenv("ALPACA_DATA_FEED") or os.getenv("ALPACA_STOCK_FEED") or broker.get("data_feed"))
    broker["options_feed"] = _feed(os.getenv("ALPACA_OPTIONS_FEED") or broker.get("options_feed"), options=True)
    out["broker"] = broker

    data = out.get("data")
    if data is not None:
        data = _map(data, "data")
        _unknown(data, {"feed", "options_feed", "adjustment", "delayed"}, "data")
        if "feed" in data:
            data["feed"] = _feed(data["feed"])
        if "options_feed" in data:
            data["options_feed"] = _feed(data["options_feed"], options=True)
        out["data"] = data
    session = _map(out.get("session"), "session")
    _unknown(session, {"timezone", "entries_regular_session_only", "allow_exits_outside_session", "force_flat_minutes_before_close", "reject_new_entries_minutes_before_close"}, "session")
    if session.get("timezone") != "America/New_York":
        raise ConfigError("session.timezone must be America/New_York")
    session["entries_regular_session_only"] = _bool(session, "entries_regular_session_only", "session", True)
    session["allow_exits_outside_session"] = _bool(session, "allow_exits_outside_session", "session", True)
    session["force_flat_minutes_before_close"] = _int(session, "force_flat_minutes_before_close", "session", 0, 240, 10)
    session["reject_new_entries_minutes_before_close"] = _int(session, "reject_new_entries_minutes_before_close", "session", 0, 240, 5)
    out["session"] = session

    universe = _map(out.get("universe"), "universe")
    _unknown(universe, {"symbols", "asset_classes", "min_price", "max_symbols", "denylist", "top_n", "refresh_minutes"}, "universe")
    symbols = universe.get("symbols", [])
    if not isinstance(symbols, list) or any(not isinstance(x, str) or not x.strip() for x in symbols):
        raise ConfigError("universe.symbols must be a list of non-empty symbols")
    universe["symbols"] = [x.strip().upper() for x in symbols]
    classes = universe.get("asset_classes", ["us_equity"])
    if not isinstance(classes, list) or any(x not in {"us_equity", "us_option", "option"} for x in classes):
        raise ConfigError("universe.asset_classes must contain us_equity and/or us_option")
    universe["asset_classes"] = classes
    universe["min_price"] = _num(universe, "min_price", "universe", 0, 1_000_000, 1.0)
    universe["max_symbols"] = _int(universe, "max_symbols", "universe", 1, 10_000, 50)
    if not isinstance(universe.get("denylist", []), list):
        raise ConfigError("universe.denylist must be a list")
    out["universe"] = universe

    strategy = _map(out.get("strategy"), "strategy")
    _unknown(strategy, {"id", "version", "variant_id", "execution_mode", "range_minutes", "breakout_buffer_bps", "min_relative_volume", "target_r", "max_entry_extension_r", "min_ibr_width_atr", "max_ibr_width_atr", "latest_entry_time", "force_flat_minutes_before_close", "signal_timeframe"}, "strategy")
    if not isinstance(strategy.get("id"), str) or not strategy["id"].strip():
        raise ConfigError("strategy.id must be a non-empty string")
    strategy["range_minutes"] = _int(strategy, "range_minutes", "strategy", 1, 240, 15)
    strategy["breakout_buffer_bps"] = _num(strategy, "breakout_buffer_bps", "strategy", 0, 500, 5)
    strategy["min_relative_volume"] = _num(strategy, "min_relative_volume", "strategy", 0, 100, 1)
    strategy["target_r"] = _num(strategy, "target_r", "strategy", 0.1, 100, 2)
    strategy["max_entry_extension_r"] = _num(strategy, "max_entry_extension_r", "strategy", 0, 100, 1)
    strategy["min_ibr_width_atr"] = _num(strategy, "min_ibr_width_atr", "strategy", 0, 100, .25)
    strategy["max_ibr_width_atr"] = _num(strategy, "max_ibr_width_atr", "strategy", 0, 100, 3)
    if strategy["min_ibr_width_atr"] > strategy["max_ibr_width_atr"]:
        raise ConfigError("strategy.min_ibr_width_atr cannot exceed max_ibr_width_atr")
    if not isinstance(strategy.get("latest_entry_time"), str):
        raise ConfigError("strategy.latest_entry_time must be HH:MM")
    out["strategy"] = strategy

    risk = _map(out.get("risk"), "risk")
    _unknown(risk, {"risk_per_trade_pct", "daily_loss_limit_pct", "max_open_risk_pct", "max_total_open_risk_pct", "max_concurrent_positions", "max_position_notional_pct", "options_min_dte", "options_max_dte", "options_max_spread_pct", "max_drawdown_pct", "max_gross_exposure_pct", "max_hold_hours"}, "risk")
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
    out["risk"] = risk

    execution = _map(out.get("execution"), "execution")
    _unknown(execution, {"order_type", "time_in_force", "client_order_id_prefix", "max_slippage_bps", "maker_first_enabled"}, "execution")
    if execution.get("order_type", "market") not in {"market", "limit"}:
        raise ConfigError("execution.order_type must be market or limit")
    if execution.get("time_in_force", "day") not in {"day", "gtc", "ioc", "fok"}:
        raise ConfigError("execution.time_in_force is unsupported")
    if not isinstance(execution.get("client_order_id_prefix", "ibr"), str) or not execution["client_order_id_prefix"]:
        raise ConfigError("execution.client_order_id_prefix must be non-empty")
    execution["max_slippage_bps"] = _num(execution, "max_slippage_bps", "execution", 0, 10_000, 50)
    out["execution"] = execution

    llm = _map(out.get("llm"), "llm")
    _unknown(llm, {"provider", "model", "temperature", "max_tokens", "base_url"}, "llm")
    if llm.get("provider") not in {"openai", "anthropic"}:
        raise ConfigError("llm.provider must be openai or anthropic")
    if not isinstance(llm.get("model"), str) or not llm["model"].strip():
        raise ConfigError("llm.model must be a non-empty string")
    llm["temperature"] = _num(llm, "temperature", "llm", 0, 2, .2)
    llm["max_tokens"] = _int(llm, "max_tokens", "llm", 128, 32_000, 2_000)
    out["llm"] = llm
    return out


def load_config(path: str | Path) -> dict:
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError("PyYAML is required to load YAML configuration files") from exc
    with open(path, encoding="utf-8") as handle:
        return validate_config(yaml.safe_load(handle) or {})
