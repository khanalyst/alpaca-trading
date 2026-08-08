"""Dependency-free checks for the Alpaca runtime boundary."""

from datetime import datetime
from decimal import Decimal
import unittest

from agent.alpaca_domain import Asset, OrderRequest
from agent.alpaca_provider import AlpacaProvider, AlpacaSession, PaperModeError
from agent.alpaca_session import NEW_YORK, SessionPolicy, normalize_calendar_day
from agent.config import ConfigError, validate_config
from agent.market import MarketData


class TradingFake:
    def get_clock(self):
        return {"timestamp": "2026-08-07T14:00:00+00:00", "is_open": True}

    def get_calendar(self, **kwargs):
        return [{"date": "2026-08-07", "open": "09:30", "close": "16:00"}]

    def get_all_assets(self, *args, **kwargs):
        return [{"symbol": "spy", "class": "us_equity", "status": "active"}]

    def get_orders(self, **kwargs):
        return []

    def submit_order(self, request):
        return {"id": "order-1", "symbol": request.symbol, "qty": str(request.qty), "side": request.side, "status": "accepted", "type": request.type, "time_in_force": request.time_in_force, "client_order_id": request.client_order_id}


class AlpacaRuntimeTests(unittest.TestCase):
    def test_live_requires_explicit_guard(self):
        with self.assertRaises(PaperModeError):
            AlpacaSession(paper=False)

    def test_early_close_and_session_policy(self):
        day = normalize_calendar_day({"date": "2026-07-03", "open": "09:30", "close": "13:00"})
        policy = SessionPolicy(force_flat_minutes_before_close=10)
        self.assertTrue(policy.entry_allowed(datetime(2026, 7, 3, 12, 40, tzinfo=NEW_YORK), day))
        self.assertTrue(policy.should_force_flat(datetime(2026, 7, 3, 12, 55, tzinfo=NEW_YORK), day))

    def test_normalized_assets_and_idempotent_order(self):
        provider = AlpacaProvider({"mode": "paper"}, session=AlpacaSession(paper=True, trading_client=TradingFake()))
        self.assertEqual(provider.assets()[0], Asset(symbol="SPY"))
        request = OrderRequest("SPY", Decimal("2"), "buy", client_order_id="test-1")
        first = provider.submit_order(request)
        second = provider.submit_order(request)
        self.assertEqual(first.client_order_id, "test-1")
        self.assertEqual(second.client_order_id, "test-1")

    def test_market_status_does_not_construct_network_client(self):
        provider = AlpacaProvider({"mode": "paper"})
        market = MarketData(provider)
        # Entries remain closed until the broker calendar has been loaded.
        self.assertFalse(market.can_enter(datetime(2026, 8, 7, 12, tzinfo=NEW_YORK)))

    def test_config_is_paper_by_default_and_live_is_fail_closed(self):
        config = validate_config({})
        self.assertEqual(config["mode"], "paper")
        self.assertTrue(config["broker"]["paper"])
        with self.assertRaises(ConfigError):
            validate_config({"mode": "live", "broker": {"paper": False}})


if __name__ == "__main__":
    unittest.main()
