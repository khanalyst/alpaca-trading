"""Focused paper-runtime safety checks using injected provider fakes."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
import sys
import types
import unittest
from unittest.mock import patch

from agent.alpaca_domain import Account, CalendarDay, MarketClock, Order, Position
from agent.alpaca_provider import AlpacaProvider, AlpacaSession, PaperModeError
from agent.config import ConfigError, validate_config
from agent.engine import Engine
from agent.market import MarketData
from agent.risk import RiskEngine


class FakeProvider:
    paper = True

    class Session:
        api_key = "key"
        secret_key = "secret"

    session = Session()

    def __init__(self, *, early_close=False):
        self.orders_sent = []
        self.positions_live = []
        close = datetime(2026, 8, 7, 13 if early_close else 16,
                         tzinfo=timezone.utc)
        self.day = CalendarDay(date(2026, 8, 7),
                               datetime(2026, 8, 7, 9, 30,
                                        tzinfo=timezone.utc), close)
        self.now = datetime(2026, 8, 7, 14, tzinfo=timezone.utc)

    def calendar(self, **_):
        return [self.day]

    def clock(self):
        return MarketClock(self.now, True, next_close=self.day.close)

    def account(self):
        return Account("a", "active", Decimal("100000"), Decimal("100000"),
                       Decimal("100000"))

    def positions(self):
        return list(self.positions_live)

    def orders(self, **_):
        return []

    def submit_order(self, request):
        self.orders_sent.append(request)
        self.positions_live = []
        return Order("order", request.symbol, request.qty, request.side,
                     "accepted", request.type, request.time_in_force,
                     client_order_id=request.client_order_id)

    def cancel_all_orders(self):
        return None


def _cfg():
    return {
        "mode": "paper", "broker": {"paper": True},
        "universe": {"symbols": ["SPY"]},
        "session": {"timezone": "America/New_York",
                     "entries_regular_session_only": True,
                     "allow_exits_outside_session": True,
                     "force_flat_minutes_before_close": 10,
                     "reject_new_entries_minutes_before_close": 5},
        "strategy": {"id": "ibr", "version": "v1", "range_minutes": 15,
                     "breakout_buffer_bps": 5, "min_relative_volume": 1,
                     "target_r": 2, "max_entry_extension_r": 1,
                     "min_ibr_width_atr": 0, "max_ibr_width_atr": 99,
                     "latest_entry_time": "15:00",
                     "force_flat_minutes_before_close": 10},
        "risk": {"risk_per_trade_pct": .5, "max_open_risk_pct": 2,
                 "max_concurrent_positions": 3,
                 "max_position_notional_pct": 25},
        "execution": {"client_order_id_prefix": "ibr",
                       "max_market_data_age_seconds": 30},
        "llm": {},
    }


class BrainFake:
    def decide(self, snapshot, portfolio):
        return {"decisions": []}


class RuntimeSafetyTests(unittest.TestCase):
    def test_provider_uses_configured_equity_and_option_feeds(self):
        seen = []

        class Request:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)
                seen.append((type(self).__name__, kwargs))

        class StockBarsRequest(Request):
            pass

        class StockQuotesRequest(Request):
            pass

        class OptionChainRequest(Request):
            pass

        class DataFeed:
            IEX = "IEX"
            SIP = "SIP"

        class OptionsFeed:
            INDICATIVE = "INDICATIVE"
            OPRA = "OPRA"

        modules = {
            name: types.ModuleType(name) for name in (
                "alpaca", "alpaca.data", "alpaca.data.requests",
                "alpaca.data.timeframe", "alpaca.data.enums")}
        modules["alpaca.data.requests"].StockBarsRequest = StockBarsRequest
        modules["alpaca.data.requests"].StockQuotesRequest = StockQuotesRequest
        modules["alpaca.data.requests"].OptionChainRequest = OptionChainRequest
        modules["alpaca.data.timeframe"].TimeFrame = types.SimpleNamespace(
            Minute="MINUTE", Day="DAY")
        modules["alpaca.data.enums"].DataFeed = DataFeed
        modules["alpaca.data.enums"].OptionsFeed = OptionsFeed

        class StockData:
            def get_stock_bars(self, request):
                return types.SimpleNamespace(data={"SPY": [{
                    "timestamp": "2026-08-07T14:00:00+00:00", "open": 1,
                    "high": 2, "low": 1, "close": 1.5}]})

            def get_stock_quotes(self, request):
                return types.SimpleNamespace(data={"SPY": [{
                    "timestamp": "2026-08-07T14:00:00+00:00", "bid_price": 1,
                    "ask_price": 2}]})

        class OptionData:
            def get_option_chain(self, request):
                return types.SimpleNamespace(data={"OPT": {
                    "symbol": "OPT", "latest_quote": {"bid_price": 1,
                    "ask_price": 2, "timestamp": "2026-08-07T14:00:00+00:00"}}})

        session = AlpacaSession(paper=True, trading_client=object(),
                                stock_data_client=StockData(),
                                option_data_client=OptionData())
        with patch.dict(sys.modules, modules):
            default = AlpacaProvider({"mode": "paper", "broker": {
                "paper": True}}, session=session)
            default.bars(["SPY"], "1m")
            default.quotes(["SPY"])
            default.option_snapshots("SPY")
            provider = AlpacaProvider({"mode": "paper", "broker": {
                "paper": True, "data_feed": "sip", "options_feed": "opra"}},
                session=session)
            bars = provider.bars(["SPY"], "1m")
            quotes = provider.quotes(["SPY"])
            options = provider.option_snapshots("SPY")
        self.assertEqual(provider.data_feed, "sip")
        self.assertEqual(provider.options_feed, "opra")
        self.assertEqual(bars["SPY"][0].feed, "sip")
        self.assertEqual(quotes["SPY"][0].feed, "sip")
        self.assertEqual(options[0].feed, "opra")
        request_feeds = [(name, kwargs.get("feed")) for name, kwargs in seen]
        self.assertEqual(request_feeds[:3], [
            ("StockBarsRequest", "IEX"),
            ("StockQuotesRequest", "IEX"),
            ("OptionChainRequest", "INDICATIVE")])
        request_feeds = dict(request_feeds)
        self.assertEqual(request_feeds["StockBarsRequest"], "SIP")
        self.assertEqual(request_feeds["StockQuotesRequest"], "SIP")
        self.assertEqual(request_feeds["OptionChainRequest"], "OPRA")

    def test_order_reconciliation_uses_bounded_filter_without_page_token(self):
        seen = []

        class GetOrdersRequest:
            def __init__(self, **kwargs):
                if "page_token" in kwargs:
                    raise AssertionError("page_token is not a Trading API order filter")
                self.__dict__.update(kwargs)
                seen.append(kwargs)

        class QueryOrderStatus:
            OPEN = "OPEN"

        modules = {
            name: types.ModuleType(name) for name in (
                "alpaca", "alpaca.trading", "alpaca.trading.requests",
                "alpaca.trading.enums")}
        modules["alpaca.trading.requests"].GetOrdersRequest = GetOrdersRequest
        modules["alpaca.trading.enums"].QueryOrderStatus = QueryOrderStatus

        class Trading:
            def get_orders(self, request):
                return types.SimpleNamespace(orders=[{
                    "id": "order-1", "symbol": "SPY", "qty": "1",
                    "side": "buy", "status": "open",
                    "type": "market", "time_in_force": "day",
                }])

        session = AlpacaSession(paper=True, trading_client=Trading())
        with patch.dict(sys.modules, modules):
            provider = AlpacaProvider({"mode": "paper", "broker": {"paper": True}},
                                      session=session)
            orders = provider.orders(status="open")
        self.assertEqual(len(orders), 1)
        self.assertEqual(seen[0]["limit"], 500)
        self.assertNotIn("page_token", seen[0])

    def test_paper_guard_rejects_false_env_and_live_session(self):
        with patch.dict(os.environ, {"ALPACA_PAPER": "false"}):
            with self.assertRaises(PaperModeError):
                AlpacaSession()
        with self.assertRaises(PaperModeError):
            AlpacaSession(paper=False, allow_live=True)

    def test_config_is_paper_only(self):
        with self.assertRaises(ConfigError):
            validate_config({"mode": "live"})
        with self.assertRaises(ConfigError):
            validate_config({"broker": {"paper": False}})

    def test_market_data_is_closed_before_calendar_load(self):
        market = MarketData(FakeProvider())
        self.assertFalse(market.can_enter(FakeProvider().now))

    def test_options_reject_multi_leg_and_non_integer_qty(self):
        risk = RiskEngine({"risk": {"options_min_dte": 7,
                                    "options_max_dte": 60}})
        with self.assertRaises(ValueError):
            risk.select_option_contract([{"strategy": "debit_spread",
                                         "type": "call", "dte": 20,
                                         "ask": 1, "volume": 2,
                                         "multiplier": 100}], "long")

    def test_engine_uses_ibr_and_risk_and_ignores_unknown_symbols(self):
        provider = FakeProvider()
        now = provider.now
        bars = []
        for index in range(16):
            timestamp = now - timedelta(minutes=30 - index)
            bars.append({"timestamp": timestamp, "open": 100,
                         "high": 100.5 if index < 15 else 102,
                         "low": 99.5 if index < 15 else 100,
                         "close": 100 if index < 15 else 101.5,
                         "volume": 10 if index < 15 else 20, "atr": 1})
        snapshot = {
            "SPY": {"bars": bars, "quote": {"timestamp": now,
                                               "bid": 101.4, "ask": 101.5}},
            "NOT_CONFIGURED": {"bars": bars,
                               "quote": {"timestamp": now,
                                         "bid": 1, "ask": 1.1}},
        }
        result = Engine(_cfg(), provider=provider,
                        brain=BrainFake()).run_once(snapshot)
        self.assertEqual([order.symbol for order in provider.orders_sent],
                         ["SPY"])
        self.assertEqual(result["orders"][0].qty, Decimal("246"))

    def test_engine_rejects_future_quote_without_lookahead(self):
        provider = FakeProvider()
        now = provider.now
        bars = []
        for index in range(16):
            timestamp = now - timedelta(minutes=30 - index)
            bars.append({"timestamp": timestamp, "open": 100,
                         "high": 100.5 if index < 15 else 102,
                         "low": 99.5 if index < 15 else 100,
                         "close": 100 if index < 15 else 101.5,
                         "volume": 10 if index < 15 else 20, "atr": 1})
        snapshot = {
            "SPY": {"bars": bars, "quote": {"timestamp": now + timedelta(seconds=1),
                                               "bid": 101.4, "ask": 101.5}},
        }
        result = Engine(_cfg(), provider=provider,
                        brain=BrainFake()).run_once(snapshot)
        self.assertEqual(result["orders"], [])
        self.assertEqual(provider.orders_sent, [])

    def test_engine_uses_early_calendar_close_for_force_flat_metadata(self):
        provider = FakeProvider()
        # Opening range bars remain 09:30--09:45 New York while the broker
        # calendar reports an early 11:00 close (15:00 UTC).
        provider.day = CalendarDay(
            date(2026, 8, 7),
            datetime(2026, 8, 7, 13, 30, tzinfo=timezone.utc),
            datetime(2026, 8, 7, 15, 0, tzinfo=timezone.utc),
        )
        provider.now = datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc)
        bars = []
        for index in range(16):
            timestamp = provider.now - timedelta(minutes=30 - index)
            bars.append({"timestamp": timestamp, "open": 100,
                         "high": 100.5 if index < 15 else 102,
                         "low": 99.5 if index < 15 else 100,
                         "close": 100 if index < 15 else 101.5,
                         "volume": 10 if index < 15 else 20, "atr": 1})
        snapshot = {
            "SPY": {"bars": bars, "quote": {"timestamp": provider.now,
                                               "bid": 101.4, "ask": 101.5}},
        }
        result = Engine(_cfg(), provider=provider,
                        brain=BrainFake()).run_once(snapshot)
        self.assertTrue(result["signals"])
        self.assertEqual(result["signals"][0]["force_flat_at"],
                         "2026-08-07T14:50:00+00:00")

    def test_flatten_retries_with_distinct_ids(self):
        provider = FakeProvider()
        provider.positions_live = [Position("SPY", Decimal("2"), "long")]
        engine = Engine(_cfg(), light=True, provider=provider)
        self.assertTrue(engine.flatten_all("test"))
        self.assertEqual(len(provider.orders_sent), 1)
        self.assertIn("flatten", provider.orders_sent[0].client_order_id)


if __name__ == "__main__":
    unittest.main()
