"""Configuration loading and fail-closed validation.

Trading configuration is executable risk policy.  Accepting a typo such as
``mode: demos`` as live trading is therefore unsafe; every supported field is
validated before an exchange client or model is created.
"""

from __future__ import annotations

from copy import deepcopy


class ConfigError(ValueError):
    """Raised when configuration is missing, malformed, or outside safe bounds."""


def _mapping(value, path: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be a mapping")
    return value


def _keys(block: dict, allowed: set[str], path: str) -> None:
    unknown = sorted(set(block) - allowed)
    if unknown:
        raise ConfigError(f"{path} has unknown field(s): {', '.join(unknown)}")


def _number(block: dict, key: str, lo: float, hi: float, path: str) -> float:
    value = block.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path}.{key} must be a number")
    value = float(value)
    if not lo <= value <= hi:
        raise ConfigError(f"{path}.{key} must be between {lo:g} and {hi:g}")
    return value


def _integer(block: dict, key: str, lo: int, hi: int, path: str) -> int:
    value = block.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{path}.{key} must be an integer")
    if not lo <= value <= hi:
        raise ConfigError(f"{path}.{key} must be between {lo} and {hi}")
    return value


def _boolean(block: dict, key: str, path: str) -> bool:
    value = block.get(key)
    if not isinstance(value, bool):
        raise ConfigError(f"{path}.{key} must be true or false")
    return value


def validate_config(raw: dict) -> dict:
    """Return a validated defensive copy of *raw* or raise ``ConfigError``."""
    cfg = deepcopy(_mapping(raw, "config"))
    _keys(cfg, {"mode", "llm", "universe", "cycle", "risk", "execution",
                "trading_costs", "alerts"}, "config")

    mode = cfg.get("mode")
    if mode not in {"demo", "live"}:
        raise ConfigError("mode must be exactly 'demo' or 'live'")

    llm = _mapping(cfg.get("llm"), "llm")
    _keys(llm, {"provider", "model", "temperature", "max_tokens"}, "llm")
    if llm.get("provider") not in {"anthropic", "openai"}:
        raise ConfigError("llm.provider must be 'anthropic' or 'openai'")
    if not isinstance(llm.get("model"), str) or not llm["model"].strip():
        raise ConfigError("llm.model must be a non-empty string")
    _number(llm, "temperature", 0, 2, "llm")
    _integer(llm, "max_tokens", 128, 32000, "llm")

    universe = _mapping(cfg.get("universe"), "universe")
    _keys(universe, {"top_n", "min_24h_quote_volume_usd", "denylist",
                     "refresh_minutes"}, "universe")
    _integer(universe, "top_n", 1, 100, "universe")
    _number(universe, "min_24h_quote_volume_usd", 0, 1e15, "universe")
    _number(universe, "refresh_minutes", 1, 1440, "universe")
    denylist = universe.get("denylist")
    if not isinstance(denylist, list) or not all(isinstance(x, str) for x in denylist):
        raise ConfigError("universe.denylist must be a list of symbols")

    cycle = _mapping(cfg.get("cycle"), "cycle")
    _keys(cycle, {"interval_seconds", "candles", "timeframes"}, "cycle")
    _integer(cycle, "interval_seconds", 30, 86400, "cycle")
    _integer(cycle, "candles", 60, 1000, "cycle")
    timeframes = cycle.get("timeframes")
    if not isinstance(timeframes, list) or not all(
            isinstance(x, str) and x for x in timeframes):
        raise ConfigError("cycle.timeframes must be a non-empty list of strings")
    required = {"15m", "1h", "4h"}
    if not required.issubset(set(timeframes)):
        raise ConfigError("cycle.timeframes must include 15m, 1h, and 4h")

    risk = _mapping(cfg.get("risk"), "risk")
    _keys(risk, {"max_leverage", "risk_per_trade_pct",
                 "max_position_notional_pct", "max_gross_exposure_pct",
                 "max_net_direction_pct", "max_concurrent_positions",
                 "min_confidence", "max_hold_hours", "daily_loss_limit_pct",
                 "flatten_on_daily_stop", "max_drawdown_pct",
                 "max_margin_usage_pct", "cooldown_minutes_after_loss"},
          "risk")
    _number(risk, "max_leverage", 1, 10, "risk")
    _number(risk, "risk_per_trade_pct", 0.01, 5, "risk")
    _number(risk, "max_position_notional_pct", 1, 100, "risk")
    _number(risk, "max_gross_exposure_pct", 1, 300, "risk")
    _number(risk, "max_net_direction_pct", 1, 300, "risk")
    _integer(risk, "max_concurrent_positions", 1, 20, "risk")
    _number(risk, "min_confidence", 0, 1, "risk")
    _number(risk, "max_hold_hours", 0.25, 168, "risk")
    _number(risk, "daily_loss_limit_pct", 0.1, 20, "risk")
    _boolean(risk, "flatten_on_daily_stop", "risk")
    _number(risk, "max_drawdown_pct", 1, 50, "risk")
    _number(risk, "max_margin_usage_pct", 1, 95, "risk")
    _number(risk, "cooldown_minutes_after_loss", 0, 10080, "risk")
    if float(risk["max_net_direction_pct"]) > float(risk["max_gross_exposure_pct"]):
        raise ConfigError("risk.max_net_direction_pct cannot exceed max_gross_exposure_pct")

    execution = _mapping(cfg.get("execution"), "execution")
    _keys(execution, {"slippage_guard_pct", "fill_timeout_seconds"},
          "execution")
    _number(execution, "slippage_guard_pct", 0, 5, "execution")
    _number(execution, "fill_timeout_seconds", 1, 60, "execution")

    costs = _mapping(cfg.get("trading_costs"), "trading_costs")
    _keys(costs, {"taker_fee_pct_per_side", "expected_stop_slippage_pct",
                  "expected_funding_intervals_held"}, "trading_costs")
    _number(costs, "taker_fee_pct_per_side", 0, 1, "trading_costs")
    _number(costs, "expected_stop_slippage_pct", 0, 5, "trading_costs")
    _number(costs, "expected_funding_intervals_held", 0, 24, "trading_costs")

    alerts = _mapping(cfg.get("alerts"), "alerts")
    _keys(alerts, {"enabled", "webhook_url_env", "format", "minimum_level",
                   "timeout_seconds"}, "alerts")
    _boolean(alerts, "enabled", "alerts")
    env_name = alerts.get("webhook_url_env")
    if not isinstance(env_name, str) or not env_name.strip():
        raise ConfigError("alerts.webhook_url_env must be a non-empty string")
    if alerts.get("format") not in {"generic", "slack", "discord"}:
        raise ConfigError("alerts.format must be generic, slack, or discord")
    if alerts.get("minimum_level") not in {"warning", "error", "critical"}:
        raise ConfigError("alerts.minimum_level must be warning, error, or critical")
    _number(alerts, "timeout_seconds", 1, 30, "alerts")

    return cfg
