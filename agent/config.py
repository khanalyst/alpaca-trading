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
    _keys(cfg, {"mode", "llm", "strategy", "universe", "cycle", "risk",
                "execution", "trading_costs", "alerts"}, "config")

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

    strategy = _mapping(cfg.get("strategy"), "strategy")
    _keys(strategy, {
        "id", "version", "signal_timeframe",
        "allow_experimental_setups_in_demo", "setup_cooldown_minutes",
        "setup_memory_hours", "loss_reentry_min_minutes",
        "min_stop_atr_multiple",
        "structure_buffer_atr_multiple", "hard_max_entry_extension_atr",
        "breakout_range_threshold_pct", "breakout_min_relative_volume",
        "funding_extreme_pct_per_8h", "fixed_reward_risk",
        "extended_reward_risk",
    }, "strategy")
    for key in ("id", "version", "signal_timeframe"):
        if (not isinstance(strategy.get(key), str)
                or not strategy[key].strip()):
            raise ConfigError(f"strategy.{key} must be a non-empty string")
    if strategy["id"] != "momentum":
        raise ConfigError(
            "strategy.id must be 'momentum' until another isolated strategy "
            "implementation exists")
    _boolean(
        strategy, "allow_experimental_setups_in_demo", "strategy")
    _number(strategy, "setup_cooldown_minutes", 0, 1440, "strategy")
    _number(strategy, "setup_memory_hours", 1, 720, "strategy")
    _number(strategy, "loss_reentry_min_minutes", 0, 10080, "strategy")
    _number(strategy, "min_stop_atr_multiple", 0.5, 5, "strategy")
    _number(strategy, "structure_buffer_atr_multiple", 0, 2, "strategy")
    _number(strategy, "hard_max_entry_extension_atr", 0.5, 10, "strategy")
    _number(strategy, "breakout_range_threshold_pct", 50, 99, "strategy")
    _number(strategy, "breakout_min_relative_volume", 0.5, 10, "strategy")
    # Compared against a funding rate normalized to an 8h equivalent, so an
    # instrument settling every 4h is not held to a bar twice as strict in
    # economic terms as one settling every 8h.
    _number(strategy, "funding_extreme_pct_per_8h", 0, 1, "strategy")
    fixed_rr = _number(
        strategy, "fixed_reward_risk", 1, 10, "strategy")
    extended_rr = _number(
        strategy, "extended_reward_risk", 1, 15, "strategy")
    if extended_rr < fixed_rr:
        raise ConfigError(
            "strategy.extended_reward_risk cannot be below "
            "fixed_reward_risk")
    if (float(strategy["setup_memory_hours"]) * 60
            < float(strategy["setup_cooldown_minutes"])):
        raise ConfigError(
            "strategy.setup_memory_hours must cover setup_cooldown_minutes")

    universe = _mapping(cfg.get("universe"), "universe")
    _keys(universe, {"top_n", "min_24h_quote_volume_usd",
                     "min_history_candles", "denylist",
                     "refresh_minutes"}, "universe")
    _integer(universe, "top_n", 1, 100, "universe")
    _number(universe, "min_24h_quote_volume_usd", 0, 1e15, "universe")
    _integer(universe, "min_history_candles", 60, 1000, "universe")
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
    if int(universe["min_history_candles"]) > int(cycle["candles"]):
        raise ConfigError(
            "universe.min_history_candles cannot exceed cycle.candles")
    if strategy["signal_timeframe"] != "15m":
        raise ConfigError(
            "strategy.signal_timeframe must be exactly '15m' for the "
            "current momentum strategy version")
    if strategy["signal_timeframe"] not in timeframes:
        raise ConfigError(
            "strategy.signal_timeframe must appear in cycle.timeframes")

    risk = _mapping(cfg.get("risk"), "risk")
    _keys(risk, {"max_leverage", "entry_leverage", "risk_per_trade_pct",
                 "experimental_risk_per_trade_pct",
                 "max_total_open_risk_pct",
                 "max_position_notional_pct", "max_gross_exposure_pct",
                 "max_net_direction_pct", "max_btc_beta_exposure_pct",
                 "min_btc_beta_samples", "max_concurrent_positions",
                 "min_confidence", "max_hold_hours", "daily_loss_limit_pct",
                 "flatten_on_daily_stop", "max_drawdown_pct",
                 "max_margin_usage_pct", "min_maintenance_margin_ratio",
                 "min_stop_liquidation_buffer_pct",
                 "cooldown_minutes_after_loss"},
          "risk")
    _integer(risk, "max_leverage", 1, 10, "risk")
    _integer(risk, "entry_leverage", 1, 10, "risk")
    if int(risk["entry_leverage"]) > int(risk["max_leverage"]):
        raise ConfigError(
            "risk.entry_leverage cannot exceed risk.max_leverage")
    _number(risk, "risk_per_trade_pct", 0.01, 5, "risk")
    _number(risk, "experimental_risk_per_trade_pct", 0.01, 5, "risk")
    if (float(risk["experimental_risk_per_trade_pct"])
            > float(risk["risk_per_trade_pct"])):
        raise ConfigError(
            "risk.experimental_risk_per_trade_pct cannot exceed "
            "risk_per_trade_pct")
    _number(risk, "max_total_open_risk_pct", 0.1, 20, "risk")
    _number(risk, "max_position_notional_pct", 1, 100, "risk")
    _number(risk, "max_gross_exposure_pct", 1, 300, "risk")
    _number(risk, "max_net_direction_pct", 1, 300, "risk")
    _number(risk, "max_btc_beta_exposure_pct", 1, 300, "risk")
    _integer(risk, "min_btc_beta_samples", 0, 200, "risk")
    _integer(risk, "max_concurrent_positions", 1, 20, "risk")
    _number(risk, "min_confidence", 0, 1, "risk")
    # This is a day-trading strategy. A position may run into a second
    # session, but a multi-day hold is a different strategy with different
    # risk (weekend gaps, funding accumulation, overnight news) and must not
    # be reachable by nudging one number.
    _number(risk, "max_hold_hours", 0.25, 48, "risk")
    _number(risk, "daily_loss_limit_pct", 0.1, 20, "risk")
    _boolean(risk, "flatten_on_daily_stop", "risk")
    _number(risk, "max_drawdown_pct", 1, 50, "risk")
    _number(risk, "max_margin_usage_pct", 1, 95, "risk")
    _number(risk, "min_maintenance_margin_ratio", 1.01, 100, "risk")
    _number(risk, "min_stop_liquidation_buffer_pct", 0.1, 50, "risk")
    _number(risk, "cooldown_minutes_after_loss", 0, 10080, "risk")
    if (float(risk["max_total_open_risk_pct"])
            > float(risk["daily_loss_limit_pct"])):
        raise ConfigError(
            "risk.max_total_open_risk_pct cannot exceed "
            "daily_loss_limit_pct")
    if float(risk["max_net_direction_pct"]) > float(risk["max_gross_exposure_pct"]):
        raise ConfigError("risk.max_net_direction_pct cannot exceed max_gross_exposure_pct")

    # A fully loaded book must not sit on top of the margin guard.
    #
    # Initial margin per position is max_position_notional_pct / entry_leverage
    # of equity, and the guard compares total initial margin against
    # mark-to-market equity. If a full book already uses the whole allowance,
    # any unrealized loss pushes usage past the threshold and the engine
    # force-closes its largest position for margin reasons rather than
    # strategy ones - a realized loss plus a taker round trip caused purely by
    # configuration arithmetic.
    #
    # Requiring 20% headroom means the book can lose about a fifth of its
    # value before the guard engages, because usage grows as M / (1 - loss).
    full_book_margin_pct = (
        int(risk["max_concurrent_positions"])
        * float(risk["max_position_notional_pct"])
        / int(risk["entry_leverage"])
    )
    margin_ceiling_pct = float(risk["max_margin_usage_pct"]) * 0.8
    if full_book_margin_pct > margin_ceiling_pct:
        raise ConfigError(
            "a full book would use "
            f"{full_book_margin_pct:.1f}% initial margin "
            f"({risk['max_concurrent_positions']} positions x "
            f"{float(risk['max_position_notional_pct']):g}% notional / "
            f"{risk['entry_leverage']}x leverage), which leaves no safe "
            f"headroom under risk.max_margin_usage_pct="
            f"{float(risk['max_margin_usage_pct']):g}%. Keep it at or below "
            f"{margin_ceiling_pct:.1f}% by lowering "
            "risk.max_position_notional_pct or risk.max_concurrent_positions, "
            "or by raising risk.max_margin_usage_pct")

    execution = _mapping(cfg.get("execution"), "execution")
    _keys(execution, {"slippage_guard_pct", "max_spread_pct",
                      "max_order_book_slippage_pct",
                      "max_market_data_age_seconds", "fill_timeout_seconds",
                      "liquidity_feedback_ttl_minutes",
                      "liquidity_retries_before_backoff",
                      "liquidity_backoff_minutes",
                      "liquidity_depth_buffer_pct",
                      "entry_failure_backoff_minutes",
                      "entry_failure_backoff_max_minutes",
                      "entry_failure_ttl_minutes"},
          "execution")
    _number(execution, "slippage_guard_pct", 0, 5, "execution")
    _number(execution, "max_spread_pct", 0.001, 2, "execution")
    _number(execution, "max_order_book_slippage_pct", 0.001, 5, "execution")
    _number(execution, "max_market_data_age_seconds", 1, 60, "execution")
    _number(execution, "fill_timeout_seconds", 1, 60, "execution")
    _number(execution, "liquidity_feedback_ttl_minutes", 5, 1440,
            "execution")
    _integer(execution, "liquidity_retries_before_backoff", 0, 10,
             "execution")
    _number(execution, "liquidity_backoff_minutes", 1, 1440, "execution")
    _number(execution, "liquidity_depth_buffer_pct", 10, 100, "execution")
    _number(execution, "entry_failure_backoff_minutes", 1, 1440,
            "execution")
    _number(execution, "entry_failure_backoff_max_minutes", 1, 10080,
            "execution")
    _number(execution, "entry_failure_ttl_minutes", 1, 10080,
            "execution")
    if (float(execution["entry_failure_backoff_max_minutes"])
            < float(execution["entry_failure_backoff_minutes"])):
        raise ConfigError(
            "execution.entry_failure_backoff_max_minutes cannot be below "
            "entry_failure_backoff_minutes")
    if (float(execution["entry_failure_ttl_minutes"])
            < float(execution["entry_failure_backoff_max_minutes"])):
        raise ConfigError(
            "execution.entry_failure_ttl_minutes cannot be below "
            "entry_failure_backoff_max_minutes")
    if (float(execution["max_spread_pct"])
            > float(execution["max_order_book_slippage_pct"]) * 2):
        raise ConfigError(
            "execution.max_spread_pct cannot exceed twice "
            "max_order_book_slippage_pct")

    costs = _mapping(cfg.get("trading_costs"), "trading_costs")
    _keys(costs, {"taker_fee_pct_per_side", "expected_stop_slippage_pct",
                  "expected_funding_intervals_held", "expected_hold_hours"},
          "trading_costs")
    _number(costs, "taker_fee_pct_per_side", 0, 1, "trading_costs")
    _number(costs, "expected_stop_slippage_pct", 0, 5, "trading_costs")
    _number(costs, "expected_funding_intervals_held", 0, 24, "trading_costs")
    _number(costs, "expected_hold_hours", 0, 168, "trading_costs")

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
