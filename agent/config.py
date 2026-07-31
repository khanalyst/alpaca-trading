"""Configuration loading and fail-closed validation.

Trading configuration is executable risk policy.  Accepting a typo such as
``mode: demos`` as live trading is therefore unsafe; every supported field is
validated before an exchange client or model is created.
"""

from __future__ import annotations

from copy import deepcopy

from .registry import (LIVE_MIN_TIER, UnknownStrategy, live_eligible_ids,
                       runnable_ids, spec_for)


# Batch 6.4. "none" is the shipped behaviour: the two contracts overlap and
# nothing separates them. The other two are competing answers to which
# variable should - see agent/contracts/momentum_phase1v2.py.
BREAKOUT_DISCRIMINATORS = {"none", "trend_alignment", "volatility_regime"}


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


def validate_config(raw: dict, *, allow_shadow_strategy: bool = False) -> dict:
    """Return a validated defensive copy of *raw* or raise ``ConfigError``."""
    cfg = deepcopy(_mapping(raw, "config"))
    _keys(cfg, {"mode", "llm", "strategy", "universe", "cycle", "risk",
                "execution", "trading_costs", "alerts", "research"}, "config")

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
        "breakout_discriminator", "breakout_compression_max_atr_ratio",
        "allow_experimental_setups_in_demo", "setup_cooldown_minutes",
        "setup_memory_hours", "loss_reentry_min_minutes",
        "min_stop_atr_multiple", "min_hold_minutes",
        "structure_buffer_atr_multiple", "hard_max_entry_extension_atr",
        "breakout_range_threshold_pct", "breakout_min_relative_volume",
        "funding_extreme_pct_per_8h", "fixed_reward_risk",
        "extended_reward_risk", "forward_horizon_hours",
        "flush_min_move_atr", "flush_min_oi_drop_pct",
        "flush_min_relative_volume", "carry_percentile",
        "carry_min_samples", "unwind_percentile", "unwind_min_samples",
        "trend_min_range_pos_pct", "trend_max_atr_ratio",
        "ls_high_percentile", "ls_low_percentile",
        "scalp_max_spread_pct", "scalp_min_abs_imbalance",
        "scalp_min_depth_usd",
    }, "strategy")
    for key in ("id", "version", "signal_timeframe"):
        if (not isinstance(strategy.get(key), str)
                or not strategy[key].strip()):
            raise ConfigError(f"strategy.{key} must be a non-empty string")
    # The register is the authority on what may run. It replaces the former
    # hard-coded 'momentum' check: adding a strategy is now a registry entry
    # plus a contract, not an edit to this validator.
    try:
        spec = spec_for(strategy["id"])
    except UnknownStrategy as exc:
        raise ConfigError(str(exc)) from None
    if strategy["version"] != spec.version:
        raise ConfigError(
            f"strategy.version must be {spec.version!r} for strategy.id "
            f"{spec.id!r}; a different version is a different strategy and "
            "needs its own registry entry")
    if not spec.implemented:
        raise ConfigError(
            f"strategy.id {spec.id!r} is registered for research but has no "
            f"live contract implementation. Runnable strategies: "
            f"{', '.join(runnable_ids())}")
    if not spec.analyst_ready and not allow_shadow_strategy:
        raise ConfigError(
            f"strategy.id {spec.id!r} has no live contract implementation; "
            "its deterministic shadow contract is research-only because the "
            "analyst prompt/schema is not implemented. Runnable "
            f"strategies: {', '.join(runnable_ids())}")
    # Demo is an operations rehearsal, but it still needs a complete analyst
    # contract. Live capital additionally requires a strategy that has
    # cleared the evidence gates.
    if cfg["mode"] == "live" and not spec.meets(LIVE_MIN_TIER):
        eligible = live_eligible_ids()
        raise ConfigError(
            f"strategy.id {spec.id!r} is tier {spec.tier} and mode is live, "
            f"which requires {LIVE_MIN_TIER} or better. "
            + (f"Live-eligible strategies: {', '.join(eligible)}"
               if eligible else
               "No registered strategy currently meets that bar.")
            + f" Reason: {spec.falsification}")
    _boolean(
        strategy, "allow_experimental_setups_in_demo", "strategy")
    # Batch 6.4. Which variable separates range_breakout from
    # trend_continuation. Defaults to "none" - the shipped behaviour - so
    # the choice is made deliberately and its attribution fork is visible.
    discriminator = strategy.get("breakout_discriminator")
    if discriminator is None:
        strategy["breakout_discriminator"] = "none"
    elif discriminator not in BREAKOUT_DISCRIMINATORS:
        raise ConfigError(
            "strategy.breakout_discriminator must be one of: "
            + ", ".join(sorted(BREAKOUT_DISCRIMINATORS)))
    if strategy.get("breakout_compression_max_atr_ratio") is None:
        strategy["breakout_compression_max_atr_ratio"] = 1.0
    else:
        _number(strategy, "breakout_compression_max_atr_ratio",
                0.1, 5.0, "strategy")
    _number(strategy, "setup_cooldown_minutes", 0, 1440, "strategy")
    _number(strategy, "setup_memory_hours", 1, 720, "strategy")
    _number(strategy, "loss_reentry_min_minutes", 0, 10080, "strategy")
    _number(strategy, "min_stop_atr_multiple", 0.5, 5, "strategy")
    _number(strategy, "min_hold_minutes", 0, 1440, "strategy")
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
    if "forward_horizon_hours" in strategy:
        horizon = _number(
            strategy, "forward_horizon_hours", 0.01,
            spec.max_hold_hours_ceiling, "strategy")
        if horizon > spec.max_hold_hours_ceiling:
            raise ConfigError(
                "strategy.forward_horizon_hours exceeds the registry ceiling")
    for key in ("flush_min_move_atr", "flush_min_relative_volume"):
        if key in strategy:
            _number(strategy, key, 0.01, 20, "strategy")
    if "flush_min_oi_drop_pct" in strategy:
        _number(strategy, "flush_min_oi_drop_pct", 0, 100, "strategy")
    for key in ("carry_percentile", "unwind_percentile"):
        if key in strategy:
            _number(strategy, key, 50, 100, "strategy")
    for key in ("carry_min_samples", "unwind_min_samples"):
        if key in strategy:
            _integer(strategy, key, 1, 10_000, "strategy")
    if "trend_min_range_pos_pct" in strategy:
        _number(strategy, "trend_min_range_pos_pct", 50, 100, "strategy")
    if "trend_max_atr_ratio" in strategy:
        _number(strategy, "trend_max_atr_ratio", 0.01, 20, "strategy")
    if "ls_high_percentile" in strategy:
        _number(strategy, "ls_high_percentile", 50, 100, "strategy")
    if "ls_low_percentile" in strategy:
        _number(strategy, "ls_low_percentile", 0, 50, "strategy")
    if ("ls_high_percentile" in strategy and "ls_low_percentile" in strategy
            and float(strategy["ls_low_percentile"])
            >= float(strategy["ls_high_percentile"])):
        raise ConfigError(
            "strategy.ls_low_percentile must be below ls_high_percentile")
    if "scalp_max_spread_pct" in strategy:
        _number(strategy, "scalp_max_spread_pct", 0.000001, 2, "strategy")
    if "scalp_min_abs_imbalance" in strategy:
        _number(strategy, "scalp_min_abs_imbalance", 0, 1, "strategy")
    if "scalp_min_depth_usd" in strategy:
        _number(strategy, "scalp_min_depth_usd", 0, 1_000_000_000,
                "strategy")
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
    _keys(cycle, {"interval_seconds", "decision_interval_seconds",
                  "candles", "timeframes"}, "cycle")
    _integer(cycle, "interval_seconds", 30, 86400, "cycle")
    # Optional. Absent means decisions run at the housekeeping cadence,
    # which is the pre-B9.2 behaviour, so an existing config is unaffected.
    if cycle.get("decision_interval_seconds") is not None:
        _integer(cycle, "decision_interval_seconds", 30, 86400, "cycle")
        if (cycle["decision_interval_seconds"]
                < cycle["interval_seconds"]):
            raise ConfigError(
                "cycle.decision_interval_seconds cannot be below "
                "cycle.interval_seconds: the decision cadence is a multiple "
                "of the housekeeping cadence, never a fraction of it")
        if (int(cycle["decision_interval_seconds"])
                % int(cycle["interval_seconds"]) != 0):
            raise ConfigError(
                "cycle.decision_interval_seconds must be an exact multiple "
                "of cycle.interval_seconds")
    _integer(cycle, "candles", 60, 1000, "cycle")
    timeframes = cycle.get("timeframes")
    if not isinstance(timeframes, list) or not all(
            isinstance(x, str) and x for x in timeframes):
        raise ConfigError("cycle.timeframes must be a non-empty list of strings")
    missing = [tf for tf in spec.required_timeframes if tf not in timeframes]
    if missing and not allow_shadow_strategy:
        raise ConfigError(
            f"cycle.timeframes must include {', '.join(spec.required_timeframes)} "
            f"for strategy.id {spec.id!r} (missing: {', '.join(missing)})")
    if int(universe["min_history_candles"]) > int(cycle["candles"]):
        raise ConfigError(
            "universe.min_history_candles cannot exceed cycle.candles")
    if strategy["signal_timeframe"] != spec.signal_timeframe:
        raise ConfigError(
            f"strategy.signal_timeframe must be exactly "
            f"{spec.signal_timeframe!r} for strategy.id {spec.id!r}")
    if (strategy["signal_timeframe"] not in timeframes
            and not allow_shadow_strategy):
        raise ConfigError(
            "strategy.signal_timeframe must appear in cycle.timeframes")

    risk = _mapping(cfg.get("risk"), "risk")
    _keys(risk, {"max_leverage", "entry_leverage", "risk_per_trade_pct",
                 "experimental_risk_per_trade_pct",
                 "max_total_open_risk_pct",
                 "max_position_notional_pct", "max_gross_exposure_pct",
                 "max_net_direction_pct", "max_btc_beta_exposure_pct",
                 "min_btc_beta_samples", "max_concurrent_positions",
                 "max_same_direction_positions",
                 "max_setups_firing_for_entry",
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
    _integer(risk, "max_same_direction_positions", 1, 20, "risk")
    if (int(risk["max_same_direction_positions"])
            > int(risk["max_concurrent_positions"])):
        raise ConfigError(
            "risk.max_same_direction_positions cannot exceed "
            "max_concurrent_positions")
    # Simultaneous setups are one market-wide move expressed many ways. This
    # is the count of instruments whose contract fires in a cycle, above
    # which no new entry is allowed at all.
    _integer(risk, "max_setups_firing_for_entry", 1, 100, "risk")
    _number(risk, "min_confidence", 0, 1, "risk")
    # Holding time is a property of the strategy, not a free parameter. The
    # ceiling comes from the registered spec, so a day-trading contract still
    # cannot be turned into a multi-day one by nudging a number - but a
    # strategy that is genuinely multi-day (carry, multi-week trend) declares
    # its own ceiling instead of being blocked by the momentum-era 48h limit.
    _number(risk, "max_hold_hours", 0.25, spec.max_hold_hours_ceiling, "risk")
    # A discretionary-close floor above the force-close ceiling would trap
    # every position until the clock closed it at whatever price was
    # available, which is the opposite of what the floor is for.
    if (float(strategy["min_hold_minutes"])
            >= float(risk["max_hold_hours"]) * 60):
        raise ConfigError(
            "strategy.min_hold_minutes must be below risk.max_hold_hours "
            "expressed in minutes, otherwise no model close is ever "
            "permitted before the max-hold timer fires")
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
    _keys(execution, {"maker_first_enabled", "maker_first_wait_seconds",
                      "slippage_guard_pct", "max_spread_pct",
                      "max_order_book_slippage_pct",
                      "max_market_data_age_seconds", "fill_timeout_seconds",
                      "paper_maker_fill_penetration_bps",
                      "paper_maker_order_ttl_seconds",
                      "liquidity_feedback_ttl_minutes",
                      "liquidity_retries_before_backoff",
                      "liquidity_backoff_minutes",
                      "liquidity_depth_buffer_pct",
                      "entry_failure_backoff_minutes",
                      "entry_failure_backoff_max_minutes",
                      "entry_failure_ttl_minutes"},
          "execution")
    # B7.5 maker-first path. Off unless set: this is the only research feature that
    # modifies the entry path, so it must be turned on deliberately.
    if execution.get("maker_first_enabled") is not None:
        _boolean(execution, "maker_first_enabled", "execution")
    else:
        execution["maker_first_enabled"] = False
    if execution.get("maker_first_wait_seconds") is not None:
        # Bounded well inside a 15m signal bar. A passive order must resolve
        # within the bar it was signalled on, or the setup it was based on is
        # no longer the setup being traded.
        _number(execution, "maker_first_wait_seconds", 1, 120, "execution")
    else:
        execution["maker_first_wait_seconds"] = 20
    _number(execution, "slippage_guard_pct", 0, 5, "execution")
    _number(execution, "max_spread_pct", 0.001, 2, "execution")
    _number(execution, "max_order_book_slippage_pct", 0.001, 5, "execution")
    _number(execution, "max_market_data_age_seconds", 1, 60, "execution")
    if execution.get("paper_maker_fill_penetration_bps") is None:
        execution["paper_maker_fill_penetration_bps"] = 1.0
    if execution.get("paper_maker_order_ttl_seconds") is None:
        execution["paper_maker_order_ttl_seconds"] = 60.0
    _number(execution, "paper_maker_fill_penetration_bps", 0.01, 100,
            "execution")
    _number(execution, "paper_maker_order_ttl_seconds", 10, 300,
            "execution")
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

    # Optional. Absent means shadow evaluation is off. The shipped config
    # explicitly enables isolated variants; custom deployments can omit
    # the block for a complete no-op.
    research = cfg.get("research")
    if research is not None:
        research = _mapping(research, "research")
        _keys(research, {"shadow_enabled", "shadow_variants",
                         "shadow_budget_ms",
                         "shadow_workers",
                         "findings_store", "backup_target",
                         "paper_initial_balance_usdt",
                         "paper_max_failures", "paper_min_closed_trades",
                         "experiment_min_duration_days",
                         "experiment_min_observations",
                         "forward_feed_version"},
              "research")
        _boolean(research, "shadow_enabled", "research")
        _number(research, "shadow_budget_ms", 0, 60_000, "research")
        if "shadow_workers" in research:
            _integer(research, "shadow_workers", 1, 32, "research")
        if "paper_initial_balance_usdt" in research:
            _number(research, "paper_initial_balance_usdt", 100,
                    1_000_000_000, "research")
        if "paper_max_failures" in research:
            _integer(research, "paper_max_failures", 1, 1_000, "research")
        if "paper_min_closed_trades" in research:
            _integer(research, "paper_min_closed_trades", 1, 100_000,
                     "research")
        if "experiment_min_duration_days" in research:
            _integer(research, "experiment_min_duration_days", 1, 365,
                     "research")
        if "experiment_min_observations" in research:
            _integer(research, "experiment_min_observations", 1, 100_000,
                     "research")
        if "forward_feed_version" in research:
            _integer(research, "forward_feed_version", 1, 1_000,
                     "research")
        findings_store = research.get("findings_store")
        if findings_store is not None and (
                not isinstance(findings_store, str)
                or not findings_store.strip()):
            raise ConfigError("research.findings_store must be a path string")
        backup_target = research.get("backup_target")
        if backup_target is not None and (
                not isinstance(backup_target, str)
                or not backup_target.strip()):
            raise ConfigError("research.backup_target must be a path string")
        for key in ("shadow_variants",):
            names = research.get(key)
            if names is None:
                research[key] = []
                continue
            if not isinstance(names, list) or not all(
                    isinstance(n, str) and n.strip() for n in names):
                raise ConfigError(
                    f"research.{key} must be a list of variant id strings")
        cfg["research"] = research

    return cfg
