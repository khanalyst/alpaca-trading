from copy import deepcopy


VALID_CONFIG = {
    "mode": "demo",
    "llm": {
        "provider": "anthropic",
        "model": "test-model",
        "temperature": 0.2,
        "max_tokens": 2000,
    },
    "universe": {
        "top_n": 10,
        "min_24h_quote_volume_usd": 50_000_000,
        "denylist": [],
        "refresh_minutes": 60,
    },
    "cycle": {
        "interval_seconds": 300,
        "timeframes": ["15m", "1h", "4h"],
        "candles": 120,
    },
    "risk": {
        "max_leverage": 3,
        "risk_per_trade_pct": 1.5,
        "max_position_notional_pct": 40,
        "max_gross_exposure_pct": 150,
        "max_net_direction_pct": 100,
        "max_concurrent_positions": 3,
        "min_confidence": 0.65,
        "max_hold_hours": 24,
        "daily_loss_limit_pct": 5,
        "flatten_on_daily_stop": False,
        "max_drawdown_pct": 15,
        "max_margin_usage_pct": 60,
        "cooldown_minutes_after_loss": 45,
    },
    "execution": {
        "slippage_guard_pct": 0.5,
        "max_spread_pct": 0.15,
        "max_order_book_slippage_pct": 0.35,
        "max_market_data_age_seconds": 10,
        "fill_timeout_seconds": 1,
    },
    "trading_costs": {
        "taker_fee_pct_per_side": 0.05,
        "expected_stop_slippage_pct": 0.15,
        "expected_funding_intervals_held": 1,
        "expected_hold_hours": 8,
    },
    "alerts": {
        "enabled": False,
        "webhook_url_env": "ALERT_WEBHOOK_URL",
        "format": "generic",
        "minimum_level": "error",
        "timeout_seconds": 5,
    },
}


def valid_config() -> dict:
    return deepcopy(VALID_CONFIG)
