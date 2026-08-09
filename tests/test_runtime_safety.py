"""Focused paper-runtime safety checks using injected provider fakes."""

from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from inspect import getattr_static
from unittest.mock import patch

from agent.alpaca_domain import Account, Asset, CalendarDay, MarketClock, Order, Position, Quote
from agent.alpaca_provider import AlpacaError, AlpacaProvider, AlpacaSession, PaperModeError
from agent.config import ConfigError, validate_config
from agent import engine as engine_module
from agent.engine import Engine
from agent.edge import resolve_validated_variant, resolve_validated_variants
from agent.market import MarketData
from agent.risk import RiskEngine
from agent.runtime_control import RuntimeControlMixin
from agent.startup_edge_policy import StartupEdgePolicyMixin
from agent.market_entry_risk import MarketEntryRiskMixin
from research.edge_lab import EdgeLedger
from research.edge_ledger_store import hash_config


class FakeProvider:
    paper = True
    endpoint = "https://paper-api.alpaca.markets"

    class Session:
        api_key = "key"
        secret_key = "secret"

    session = Session()

    def __init__(self, *, early_close=False):
        self.orders_sent = []
        self.positions_live = []
        self.cancel_calls = 0
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
        self.cancel_calls += 1
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


class RuntimeControlFacadeTests(unittest.TestCase):
    def test_engine_reexports_runtime_control_facade_by_identity(self):
        self.assertIs(engine_module.RuntimeControlMixin, RuntimeControlMixin)
        self.assertTrue(issubclass(Engine, RuntimeControlMixin))
        self.assertLess(Engine.__mro__.index(engine_module.ExecutionLifecycleMixin),
                        Engine.__mro__.index(RuntimeControlMixin))
        for name in ("flatten_all", "_flatten_all_impl", "request_shutdown",
                     "close", "run"):
            self.assertIs(getattr(Engine, name), getattr(RuntimeControlMixin, name))


class StartupEdgePolicyFacadeTests(unittest.TestCase):
    def test_engine_reexports_startup_edge_policy_facade_by_identity(self):
        self.assertIs(engine_module.StartupEdgePolicyMixin, StartupEdgePolicyMixin)
        self.assertTrue(issubclass(Engine, StartupEdgePolicyMixin))
        self.assertEqual(Engine.__mro__[1:4], (
            engine_module.ExecutionLifecycleMixin,
            RuntimeControlMixin,
            StartupEdgePolicyMixin,
        ))
        for name in ("preflight", "_ensure_order_ready",
                     "_inside_regular_session", "_enforce_intraday_cleanup",
                     "_latest_entry_allowed", "_refresh_edge"):
            self.assertIs(getattr(Engine, name), getattr(StartupEdgePolicyMixin, name))


class MarketEntryRiskFacadeTests(unittest.TestCase):
    def test_engine_reexports_market_entry_risk_facade_by_identity(self):
        self.assertIs(engine_module.MarketEntryRiskMixin, MarketEntryRiskMixin)
        self.assertTrue(issubclass(Engine, MarketEntryRiskMixin))
        self.assertEqual(Engine.__mro__[1:5], (
            engine_module.ExecutionLifecycleMixin,
            RuntimeControlMixin,
            StartupEdgePolicyMixin,
            MarketEntryRiskMixin,
        ))
        for name in (
            "_event", "_bar_mapping", "_quote_mapping", "_universe", "_collect",
            "_number", "_required_number", "_timestamp", "_wall_clock",
            "_validated_clock_timestamp", "_completed", "_llm_allows",
            "_entry_execution", "_update_daily_risk", "_fail_closed",
            "_risk_order", "_client_id",
        ):
            self.assertIs(getattr_static(Engine, name),
                          getattr_static(MarketEntryRiskMixin, name))


class RuntimeSafetyTests(unittest.TestCase):
    def setUp(self):
        self.runtime_tmp = tempfile.TemporaryDirectory(prefix="alpaca-runtime-safety-")
        from agent import state
        self.original_runtime_base = state.RUNTIME_BASE
        state.RUNTIME_BASE = Path(self.runtime_tmp.name)
        state.configure_runtime("paper")
        state.ensure_ready()
        self.addCleanup(self._cleanup_runtime)

    def _cleanup_runtime(self):
        from agent import state
        state.RUNTIME_BASE = self.original_runtime_base
        state.configure_runtime("paper")
        self.runtime_tmp.cleanup()

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
                    "asset_class": "us_equity",
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

    def test_engine_scope_requires_actual_boolean_provider_paper(self):
        class StringPaperProvider(FakeProvider):
            paper = "false"

        with self.assertRaisesRegex(AlpacaError, "provider is not paper scoped"):
            Engine(_cfg(), light=True, provider=StringPaperProvider())

    def test_string_clock_open_flag_cannot_submit_through_run_once(self):
        class StringOpenProvider(FakeProvider):
            def clock(self):
                return {"timestamp": self.now.isoformat(),
                        "is_open": "false", "next_close": self.day.close}

        provider = StringOpenProvider()
        engine = Engine(_cfg(), provider=provider, brain=BrainFake())
        self.addCleanup(engine.close)
        result = engine.run_once({})
        self.assertEqual(provider.orders_sent, [])
        self.assertIn(result["action"], {"force_flat", "hold"})

    def test_operator_pause_requires_exact_false(self):
        from agent import state
        engine = Engine(_cfg(), light=True, provider=FakeProvider())
        self.addCleanup(engine.close)
        for value in ("yes", None, 0, 1):
            with self.subTest(value=value):
                state.update_state(lambda current, value=value: {
                    **current, "operator_pause": value})
                self.assertFalse(engine._ensure_order_ready())
        state.update_state(lambda current: {**current, "operator_pause": False})

    def test_run_does_not_clear_malformed_durable_operator_pause(self):
        from agent import state
        engine = Engine(_cfg(), light=True, provider=FakeProvider())
        state.update_state(lambda current: {**current, "operator_pause": "yes"})
        with self.assertRaisesRegex(AlpacaError, "operator_pause"):
            engine.run(max_cycles=0)
        self.assertEqual(state.load_state()["operator_pause"], "yes")

    def test_preflight_rejects_missing_endpoint_metadata(self):
        class MissingEndpointProvider(FakeProvider):
            endpoint = ""

        engine = Engine(_cfg(), light=True, provider=MissingEndpointProvider())
        self.addCleanup(engine.close)
        with self.assertRaisesRegex(AlpacaError, "endpoint validation failed"):
            engine.preflight()

    def test_preflight_requires_asset_tradable_to_be_boolean_true(self):
        class StringTradableProvider(FakeProvider):
            def assets(self):
                return [Asset("SPY", tradable="true")]

        engine = Engine(_cfg(), light=True, provider=StringTradableProvider())
        self.addCleanup(engine.close)
        with self.assertRaisesRegex(AlpacaError, "inactive or not tradable"):
            engine.preflight()

    def test_preflight_validates_injected_clock_boundary(self):
        malformed_clocks = (
            {"timestamp": "2026-08-07T14:00:00+00:00"},
            {"timestamp": "2026-08-07T14:00:00+00:00", "is_open": "false"},
            {"timestamp": None, "is_open": True},
        )

        for clock in malformed_clocks:
            class MalformedClockProvider(FakeProvider):
                def clock(self):
                    return clock

            with self.subTest(clock=clock):
                engine = Engine(_cfg(), light=True,
                                provider=MalformedClockProvider())
                self.addCleanup(engine.close)
                with self.assertRaisesRegex(
                        AlpacaError, "authenticated paper preflight failed"):
                    engine.preflight()

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
        engine = Engine(_cfg(), provider=provider, brain=BrainFake())
        engine._wall_clock = lambda: provider.now
        result = engine.run_once(snapshot)
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
        engine = Engine(_cfg(), provider=provider, brain=BrainFake())
        engine._wall_clock = lambda: provider.now
        result = engine.run_once(snapshot)
        self.assertEqual(result["orders"], [])
        self.assertEqual(provider.orders_sent, [])

    def test_collect_uses_latest_valid_quote_not_provider_order(self):
        provider = FakeProvider()
        engine = Engine(_cfg(), light=True, provider=provider)
        self.addCleanup(engine.close)
        now = provider.now
        older = {"symbol": "SPY", "timestamp": now - timedelta(seconds=20),
                 "bid_price": 100, "ask_price": 100.1}
        latest = Quote("SPY", timestamp=now - timedelta(seconds=2),
                       bid=Decimal("101"), ask=Decimal("101.1"))
        rows = engine._collect(["SPY"], now,
                               {"SPY": {"bars": [], "quotes": [latest, older]}})
        self.assertEqual(rows["SPY"]["quote"]["bid"], Decimal("101"))

    def test_collect_rejects_naive_timestamp_and_non_boolean_freshness(self):
        provider = FakeProvider()
        engine = Engine(_cfg(), light=True, provider=provider)
        self.addCleanup(engine.close)
        now = provider.now
        malformed = {
            "SPY": {"quote_stale": "false", "quote": {
                "symbol": "SPY", "timestamp": now,
                "bid": 100, "ask": 100.1}},
            "AAPL": {"quote": {
                "symbol": "AAPL", "timestamp": datetime(2026, 8, 7, 14),
                "bid": 100, "ask": 100.1}},
            "MSFT": {"stale": None, "quote": {
                "symbol": "MSFT", "timestamp": now,
                "bid": 100, "ask": 100.1}},
        }
        self.assertEqual(engine._collect(["SPY", "AAPL", "MSFT"], now, malformed), {})

    def test_plan_snapshot_uses_signal_entry_for_slippage_reference(self):
        provider = FakeProvider()
        engine = Engine(_cfg(), provider=provider, brain=BrainFake())
        self.addCleanup(engine.close)
        engine._wall_clock = lambda: provider.now
        seen = {}
        signal = {"symbol": "SPY", "direction": "long", "setup_type": "ibr_breakout",
                  "entry_price": 100.0, "signal_ts": provider.now.timestamp(),
                  "range_high": 99.0, "range_low": 98.0, "range_width": 1.0,
                  "relative_volume": 2.0, "session": "2026-08-07"}
        snapshot = {"SPY": {"bars": [], "quote": {
            "symbol": "SPY", "timestamp": provider.now,
            "bid": 100.48, "ask": 100.51}}}

        def capture_plan(_signal, plan_snapshot, _cfg):
            seen.update(plan_snapshot)
            return None, "test rejection"

        with patch.object(engine_module, "generate_ibr_signal", return_value=signal), \
             patch.object(engine_module, "build_setup_plan", side_effect=capture_plan):
            result = engine.run_once(snapshot)
        self.assertEqual(result["orders"], [])
        self.assertEqual(seen["entry_price"], 100.0)
        self.assertEqual(seen["price"], 100.0)

    def test_entry_slippage_strictly_above_fifty_bps_is_rejected(self):
        engine = Engine(_cfg(), light=True, provider=FakeProvider())
        self.addCleanup(engine.close)
        with self.assertRaisesRegex(ValueError, r"51\.00 bps"):
            engine._entry_execution("buy", {"bid": 100, "ask": 100.51}, 100)

    def test_client_id_is_deterministic_and_retry_suffix_survives_prefix_truncation(self):
        cfg = _cfg()
        cfg["execution"]["client_order_id_prefix"] = "x" * 100
        engine = Engine(cfg, light=True, provider=FakeProvider())
        self.addCleanup(engine.close)
        signal = {"symbol": "SPY", "direction": "long", "setup_id": "setup"}
        first = engine._client_id("flatten", signal, attempt=0)
        retry = engine._client_id("flatten", signal, attempt=1)
        other = engine._client_id("flatten", {**signal, "symbol": "AAPL"}, attempt=0)
        self.assertEqual(first, engine._client_id("flatten", signal, attempt=0))
        self.assertNotEqual(first, retry)
        self.assertNotEqual(first, other)
        self.assertTrue(retry.endswith("-1"))
        self.assertLessEqual(len(retry), 48)

    def test_account_failure_cancels_and_pauses_via_fail_closed(self):
        class BrokenAccountProvider(FakeProvider):
            def account(self):
                raise RuntimeError("account unavailable")

        provider = BrokenAccountProvider()
        engine = Engine(_cfg(), light=True, provider=provider)
        self.addCleanup(engine.close)
        engine._ensure_order_ready = lambda: True
        engine._wall_clock = lambda: provider.now
        result = engine.run_once({})
        self.assertEqual(result["reason"], "account_unavailable")
        self.assertGreaterEqual(provider.cancel_calls, 1)
        self.assertTrue(__import__("agent.state", fromlist=["load_state"]).load_state()["operator_pause"])

    def test_post_risk_positions_failure_cancels_and_pauses_via_fail_closed(self):
        class BrokenPostRiskPositionsProvider(FakeProvider):
            def __init__(self):
                super().__init__()
                self.position_calls = 0

            def positions(self):
                self.position_calls += 1
                if self.position_calls >= 2:
                    raise RuntimeError("positions unavailable")
                return []

        provider = BrokenPostRiskPositionsProvider()
        engine = Engine(_cfg(), light=True, provider=provider)
        self.addCleanup(engine.close)
        engine._ensure_order_ready = lambda: True
        engine._wall_clock = lambda: provider.now
        result = engine.run_once({})
        self.assertEqual(result["reason"], "post_risk_positions_unavailable")
        self.assertGreaterEqual(provider.cancel_calls, 1)

    def test_malformed_risk_day_date_does_not_reset_baseline(self):
        from agent import state
        provider = FakeProvider()
        engine = Engine(_cfg(), light=True, provider=provider)
        self.addCleanup(engine.close)
        state.update_state(lambda current: {
            **current,
            "risk_day": {"date": True, "start_equity": 1},
        })
        with self.assertRaisesRegex(AlpacaError, "risk date is malformed"):
            engine._update_daily_risk(provider.account(), provider.now)

    def test_malformed_active_risk_fails_closed_without_tuple_unpack_error(self):
        from agent import state
        provider = FakeProvider()
        engine = Engine(_cfg(), light=True, provider=provider)
        self.addCleanup(engine.close)
        engine._ensure_order_ready = lambda: True
        engine._wall_clock = lambda: provider.now
        state.update_state(lambda current: {
            **current,
            "active_trades": {"SPY": {"risk_usd": "nan", "notional": 100}},
        })
        with patch.object(engine, "reconcile", return_value={"positions": []}):
            result = engine.run_once({})
        self.assertEqual(result["action"], "fail_closed")
        self.assertEqual(result["reason"], "durable_risk_state_invalid")
        self.assertGreaterEqual(provider.cancel_calls, 1)

    def test_required_edge_blocks_the_real_order_path_until_champion_exists(self):
        provider = FakeProvider()
        cfg = _cfg()
        cfg["research"] = {
            "enabled": True,
            "require_validated_variant": True,
            "champion_min_confidence": .95,
            "db_path": str(Path(self.runtime_tmp.name) / "empty-edge.sqlite3"),
        }
        engine = Engine(cfg, provider=provider, brain=BrainFake())
        self.addCleanup(engine.close)
        engine._wall_clock = lambda: provider.now
        result = engine.run_once({})
        self.assertEqual(result["action"], "hold")
        self.assertIn("no latest-passing validated edge", result["reason"])
        self.assertEqual(provider.orders_sent, [])
        self.assertFalse(engine.check()["edge_ready"])

    def test_authenticated_preflight_rejects_inactive_or_untradable_symbols(self):
        class InvalidAssetProvider(FakeProvider):
            def assets(self):
                return [Asset("SPY", status="inactive", tradable=False)]

        engine = Engine(_cfg(), light=True, provider=InvalidAssetProvider())
        self.addCleanup(engine.close)
        with self.assertRaisesRegex(Exception, "inactive or not tradable"):
            engine.preflight()

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
        engine = Engine(_cfg(), provider=provider, brain=BrainFake())
        engine._wall_clock = lambda: provider.now
        result = engine.run_once(snapshot)
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

    def test_flatten_requires_working_order_cancellation_confirmation(self):
        class PersistentOrderProvider(FakeProvider):
            def orders(self, **_):
                return [Order("accepted", "SPY", Decimal("1"), "buy",
                               "accepted", "market", "day")]

        provider = PersistentOrderProvider()
        engine = Engine(_cfg(), light=True, provider=provider)
        with patch("agent.runtime_control.time.sleep") as sleep, \
                patch.object(engine, "_event") as event:
            self.assertFalse(engine.flatten_all("persistent-order"))
        self.assertEqual(sleep.call_count, 5)
        flatten = [call for call in event.call_args_list
                   if call.args and call.args[0] == "flatten_incomplete"]
        self.assertEqual(len(flatten), 1)
        payload = flatten[0].args[1]
        self.assertEqual(payload["residual_positions"], [])
        self.assertEqual(len(payload["residual_working_orders"]), 1)

    def test_flatten_ignores_terminal_orders_after_cancellation(self):
        class TerminalOrderProvider(FakeProvider):
            def orders(self, **_):
                return [Order("filled", "SPY", Decimal("1"), "buy",
                               "filled", "market", "day")]

        engine = Engine(_cfg(), light=True, provider=TerminalOrderProvider())
        self.assertTrue(engine.flatten_all("terminal-order"))

    def test_flatten_fails_closed_when_order_poll_errors(self):
        class BrokenOrderProvider(FakeProvider):
            def orders(self, **_):
                raise RuntimeError("orders unavailable")

        engine = Engine(_cfg(), light=True, provider=BrokenOrderProvider())
        with patch.object(engine, "_event") as event:
            self.assertFalse(engine.flatten_all("orders-error"))
        self.assertTrue(any(call.args and call.args[0] == "flatten_orders_error"
                            for call in event.call_args_list))

    def test_flatten_fails_closed_when_final_position_poll_errors(self):
        class FinalPositionErrorProvider(FakeProvider):
            def __init__(self):
                super().__init__()
                self.position_calls = 0
                self.order_calls = 0

            def positions(self):
                self.position_calls += 1
                if self.position_calls > 12:
                    raise RuntimeError("positions unavailable")
                return []

            def orders(self, **_):
                self.order_calls += 1
                if self.order_calls > 12:
                    return []
                return [Order("accepted", "SPY", Decimal("1"), "buy",
                               "accepted", "market", "day")]

        engine = Engine(_cfg(), light=True,
                        provider=FinalPositionErrorProvider())
        with patch("agent.runtime_control.time.sleep"), \
                patch.object(engine, "_event") as event:
            self.assertFalse(engine.flatten_all("final-positions-error"))
        flatten = [call for call in event.call_args_list
                   if call.args and call.args[0] == "flatten_incomplete"]
        self.assertEqual(len(flatten), 1)
        self.assertEqual(flatten[0].args[1]["positions_error"],
                         "positions unavailable")

    def test_flatten_close_position_retries_rejected_with_distinct_attempt_ids(self):
        class CloseProvider(FakeProvider):
            def __init__(self):
                super().__init__()
                self.close_requests = []
                self.close_orders = []

            def close_position(self, symbol, qty=None, *, client_order_id=None,
                               order_type="market", time_in_force="day"):
                quantity = qty or abs(self.positions_live[0].qty)
                attempt = len(self.close_requests)
                status = "rejected" if attempt == 0 else "filled"
                order = Order(
                    f"close-{attempt}", symbol, quantity, "sell", status,
                    order_type, time_in_force, client_order_id=client_order_id)
                self.close_requests.append(order)
                self.close_orders.append(order)
                if status == "filled":
                    self.positions_live = []
                return order

            def orders(self, **_):
                return list(self.close_orders)

        provider = CloseProvider()
        provider.positions_live = [Position("SPY", Decimal("2"), "long")]
        engine = Engine(_cfg(), light=True, provider=provider)
        with patch("agent.engine.time.sleep"):
            self.assertTrue(engine.flatten_all("retry"))
        self.assertEqual(len(provider.close_requests), 2)
        first, second = provider.close_requests
        self.assertNotEqual(first.client_order_id, second.client_order_id)
        runtime = __import__("agent.state", fromlist=["load_state"]).load_state()
        rows = [row for row in runtime["orders"].values()
                if row.get("action") == "flatten"]
        self.assertEqual([row["attempt"] for row in rows], [0, 1])
        self.assertEqual({row["status"] for row in rows}, {"rejected", "filled"})

    def test_flatten_dedupes_live_close_position_order(self):
        class LiveCloseProvider(FakeProvider):
            def __init__(self):
                super().__init__()
                self.close_requests = []
                self.close_orders = []

            def close_position(self, symbol, qty=None, *, client_order_id=None,
                               order_type="market", time_in_force="day"):
                quantity = qty or abs(self.positions_live[0].qty)
                order = Order(
                    "live-close", symbol, quantity, "sell", "accepted",
                    order_type, time_in_force, client_order_id=client_order_id)
                self.close_requests.append(order)
                self.close_orders.append(order)
                return order

            def orders(self, **_):
                return list(self.close_orders)

        provider = LiveCloseProvider()
        provider.positions_live = [Position("SPY", Decimal("2"), "long")]
        engine = Engine(_cfg(), light=True, provider=provider)
        with patch("agent.engine.time.sleep"):
            self.assertFalse(engine.flatten_all("live"))
        self.assertEqual(len(provider.close_requests), 1)
        runtime = __import__("agent.state", fromlist=["load_state"]).load_state()
        rows = [row for row in runtime["orders"].values()
                if row.get("action") == "flatten"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attempt"], 0)
        self.assertEqual(rows[0]["status"], "accepted")

    def test_external_lock_blocks_flatten_and_cycle_without_mutation(self):
        holder = Engine(_cfg(), light=True, provider=FakeProvider())
        contender_provider = FakeProvider()
        contender = Engine(_cfg(), light=True, provider=contender_provider)
        self.assertTrue(holder._acquire_lock(persistent=True))
        try:
            self.assertFalse(contender.flatten_all("held"))
            result = contender.run_once({})
            self.assertEqual(result, {"action": "hold", "reason": "runtime_lock_held"})
            self.assertEqual(contender_provider.cancel_calls, 0)
            self.assertEqual(contender_provider.orders_sent, [])
        finally:
            holder.close()

    def test_close_pauses_state_before_stopped_heartbeat_and_releases_lock(self):
        from agent import state
        provider = FakeProvider()
        engine = Engine(_cfg(), light=True, provider=provider)
        state.update_state(lambda current: {**current, "state": state.RUNNING})
        self.assertTrue(engine._acquire_lock(persistent=True))
        engine.close()
        self.assertEqual(state.load_state()["state"], state.PAUSED)
        heartbeat = json.loads(state.HEARTBEAT_FILE.read_text())
        self.assertEqual(heartbeat["status"], "stopped")
        self.assertIsNone(engine._lock_handle)
        handle = state.acquire_run_lock()
        self.assertIsNotNone(handle)
        state.release_run_lock(handle)

    def test_close_preserves_killed_and_day_stopped_states(self):
        from agent import state
        engine = Engine(_cfg(), light=True, provider=FakeProvider())
        for terminal in (state.KILLED, state.DAY_STOPPED):
            state.update_state(lambda current, terminal=terminal: {
                **current, "state": terminal})
            self.assertTrue(engine._acquire_lock(persistent=True))
            engine.close()
            self.assertEqual(state.load_state()["state"], terminal)

    def test_close_releases_lock_when_heartbeat_write_fails(self):
        from agent import state
        engine = Engine(_cfg(), light=True, provider=FakeProvider())
        state.update_state(lambda current: {**current, "state": state.RUNNING})
        self.assertTrue(engine._acquire_lock(persistent=True))
        with patch.object(state, "write_heartbeat", side_effect=OSError("disk full")):
            engine.close()
        self.assertIsNone(engine._lock_handle)

    def test_request_shutdown_preserves_operator_pause_through_close(self):
        from agent import state
        engine = Engine(_cfg(), light=True, provider=FakeProvider())
        state.update_state(lambda current: {**current, "state": state.RUNNING,
                                            "operator_pause": False})
        self.assertTrue(engine._acquire_lock(persistent=True))
        engine.running = True
        engine.request_shutdown("operator request")
        runtime = state.load_state()
        self.assertFalse(engine.running)
        self.assertEqual(engine.shutdown_reason, "operator request")
        self.assertEqual(runtime["state"], state.PAUSED)
        self.assertTrue(runtime["operator_pause"])
        heartbeat = json.loads(state.HEARTBEAT_FILE.read_text())
        self.assertEqual(heartbeat["status"], "pausing")
        engine.close()
        runtime = state.load_state()
        self.assertEqual(runtime["state"], state.PAUSED)
        self.assertTrue(runtime["operator_pause"])
        heartbeat = json.loads(state.HEARTBEAT_FILE.read_text())
        self.assertEqual(heartbeat["status"], "stopped")
        self.assertEqual(heartbeat["reason"], "operator request")

    def test_readiness_false_rolls_back_running_state_and_releases_lock(self):
        from agent import state
        engine = Engine(_cfg(), light=True, provider=FakeProvider())
        with patch.object(engine, "_ensure_order_ready", return_value=False):
            with self.assertRaisesRegex(AlpacaError, "runtime readiness"):
                engine.run()
        self.assertEqual(state.load_state()["state"], state.PAUSED)
        self.assertFalse(state.load_state()["operator_pause"])
        self.assertIsNone(engine._lock_handle)
        heartbeat = json.loads(state.HEARTBEAT_FILE.read_text())
        self.assertEqual(heartbeat["status"], "stopped")

    def test_readiness_exception_rolls_back_and_propagates_original(self):
        from agent import state
        engine = Engine(_cfg(), light=True, provider=FakeProvider())
        with patch.object(engine, "_ensure_order_ready",
                          side_effect=RuntimeError("readiness exploded")):
            with self.assertRaisesRegex(RuntimeError, "readiness exploded"):
                engine.run()
        self.assertEqual(state.load_state()["state"], state.PAUSED)
        self.assertIsNone(engine._lock_handle)
        heartbeat = json.loads(state.HEARTBEAT_FILE.read_text())
        self.assertEqual(heartbeat["status"], "stopped")

    def test_direct_cycle_alpaca_error_stops_and_releases_temporary_lock(self):
        from agent import state
        engine = Engine(_cfg(), light=True, provider=FakeProvider())
        with patch.object(engine, "_run_once_impl",
                          side_effect=AlpacaError("cycle failed")):
            with self.assertRaisesRegex(AlpacaError, "cycle failed"):
                engine.run_once({})
        self.assertEqual(state.load_state()["state"], state.PAUSED)
        self.assertIsNone(engine._lock_handle)
        heartbeat = json.loads(state.HEARTBEAT_FILE.read_text())
        self.assertEqual(heartbeat["status"], "stopped")

    def test_bounded_run_flattens_existing_positions_before_exit(self):
        provider = FakeProvider()
        provider.positions_live = [Position("SPY", Decimal("2"), "long")]
        engine = Engine(_cfg(), provider=provider)
        engine.run(max_cycles=0)
        self.assertEqual(provider.positions_live, [])
        self.assertEqual(len(provider.orders_sent), 1)
        from agent import state
        self.assertEqual(state.load_state()["state"], state.PAUSED)
        self.assertIsNone(engine._lock_handle)
        heartbeat = json.loads(state.HEARTBEAT_FILE.read_text())
        self.assertEqual(heartbeat["status"], "stopped")

    def test_bounded_run_reports_incomplete_shutdown_flatten(self):
        engine = Engine(_cfg(), provider=FakeProvider())
        with patch.object(engine, "_flatten_all_impl", return_value=False):
            with self.assertRaisesRegex(AlpacaError, "shutdown flatten incomplete"):
                engine.run(max_cycles=0)
        from agent import state
        self.assertEqual(state.load_state()["state"], state.PAUSED)
        self.assertTrue(state.load_state()["operator_pause"])
        self.assertIsNone(engine._lock_handle)
        heartbeat = json.loads(state.HEARTBEAT_FILE.read_text())
        self.assertEqual(heartbeat["status"], "degraded")
        self.assertEqual(heartbeat["reason"], "shutdown_flatten_incomplete")
        self.assertFalse(engine._ensure_order_ready())

    def test_bounded_run_reports_shutdown_flatten_exception_as_degraded(self):
        engine = Engine(_cfg(), provider=FakeProvider())
        with patch.object(engine, "_flatten_all_impl",
                          side_effect=RuntimeError("flatten exploded")):
            with self.assertRaisesRegex(AlpacaError, "shutdown flatten failed"):
                engine.run(max_cycles=0)
        from agent import state
        self.assertEqual(state.load_state()["state"], state.PAUSED)
        self.assertTrue(state.load_state()["operator_pause"])
        self.assertIsNone(engine._lock_handle)
        heartbeat = json.loads(state.HEARTBEAT_FILE.read_text())
        self.assertEqual(heartbeat["status"], "degraded")
        self.assertEqual(heartbeat["reason"], "shutdown_flatten_failed")
        self.assertFalse(engine._ensure_order_ready())

    def test_run_cycle_alpaca_error_preserves_propagation_and_safe_terminal_state(self):
        from agent import state
        engine = Engine(_cfg(), provider=FakeProvider())
        with patch.object(engine, "_ensure_order_ready", return_value=True), \
             patch.object(engine, "run_once",
                          side_effect=AlpacaError("cycle exploded")), \
             patch.object(engine, "_flatten_all_impl", return_value=True):
            with self.assertRaisesRegex(AlpacaError, "cycle exploded"):
                engine.run()
        self.assertEqual(state.load_state()["state"], state.PAUSED)
        self.assertIsNone(engine._lock_handle)
        heartbeat = json.loads(state.HEARTBEAT_FILE.read_text())
        self.assertEqual(heartbeat["status"], "stopped")

    def test_preopen_startup_cancels_orders_and_flattens_residual_position(self):
        class PreopenProvider(FakeProvider):
            def clock(self):
                now = self.day.open - timedelta(minutes=30)
                return MarketClock(now, False, next_open=self.day.open,
                                   next_close=self.day.close)

        provider = PreopenProvider()
        provider.positions_live = [Position("SPY", Decimal("2"), "long")]
        engine = Engine(_cfg(), provider=provider)
        self.addCleanup(engine.close)
        self.assertGreaterEqual(provider.cancel_calls, 1)
        self.assertEqual(provider.positions_live, [])

    def test_intraday_restart_also_cleans_unknown_residual_exposure(self):
        provider = FakeProvider()
        provider.positions_live = [Position("SPY", Decimal("2"), "long")]
        engine = Engine(_cfg(), provider=provider)
        self.addCleanup(engine.close)
        self.assertGreaterEqual(provider.cancel_calls, 1)
        self.assertEqual(provider.positions_live, [])

    def test_outside_session_cleanup_failure_pauses_and_blocks(self):
        class BrokenPreopenProvider(FakeProvider):
            def clock(self):
                now = self.day.open - timedelta(minutes=30)
                return MarketClock(now, False, next_open=self.day.open,
                                   next_close=self.day.close)

            def cancel_all_orders(self):
                raise RuntimeError("cancel unavailable")

        provider = BrokenPreopenProvider()
        provider.positions_live = [Position("SPY", Decimal("2"), "long")]
        engine = Engine(_cfg(), provider=provider)
        self.addCleanup(engine.close)
        runtime = __import__("agent.state", fromlist=["load_state"]).load_state()
        self.assertEqual(runtime["state"], "PAUSED")
        self.assertTrue(runtime["operator_pause"])
        result = engine.run_once({})
        self.assertEqual(result["action"], "hold")
        self.assertEqual(provider.positions_live[0].symbol, "SPY")

    def test_latest_entry_time_blocks_rule_independent_entry_path(self):
        provider = FakeProvider()
        cfg = _cfg()
        cfg["strategy"]["latest_entry_time"] = "09:00"
        engine = Engine(cfg, provider=provider, brain=BrainFake())
        self.addCleanup(engine.close)
        engine._wall_clock = lambda: provider.now
        result = engine.run_once({})
        self.assertEqual(result["reason"], "latest_entry_time_passed")
        self.assertEqual(provider.orders_sent, [])

    @staticmethod
    def _prove(ledger, variant_id, *, confidence=.99):
        record = ledger.register_candidate(
            variant_id, strategy_id="ibr", vehicle="equity",
            hypothesis=f"proof for {variant_id}", config={}, axes={})
        passing = {"gate": {"passes": True, "heldout_delta": .1,
                            "heldout_trades": 20},
                   "confidence": confidence}
        ledger.append_run(record["candidate_id"], lane="shadow",
                          vehicle="equity", metrics=passing)
        with closing(sqlite3.connect(ledger.path)) as db, db:
            db.execute("UPDATE candidate_state SET status='validated' WHERE candidate_id=?",
                       (record["candidate_id"],))
            db.commit()
        return ledger.candidate(record["candidate_id"])

    @staticmethod
    def _verified_proof(confidence=.99,
                        config_hash=None):
        config_hash = hash_config({}) if config_hash is None else config_hash
        return {"lane": "shadow", "metrics": {"confidence": confidence},
                "config_hash": config_hash,
                "verified_gate": {"passes": True, "heldout_delta": .1,
                                  "heldout_trades": 20}}

    def test_live_edge_is_pinned_and_never_switches_after_demotion(self):
        db = Path(self.runtime_tmp.name) / "live-edge.sqlite3"
        ledger = EdgeLedger(db)
        pinned = self._prove(ledger, "ibr.baseline", confidence=.99)
        other = self._prove(ledger, "ibr.range.30", confidence=1.0)

        class LiveProvider(FakeProvider):
            paper = False
            endpoint = "https://api.alpaca.markets"

        raw = {
            "mode": "live", "broker": {"paper": False, "allow_live": True},
            "strategy": {"id": "ibr", "variant_id": "ibr.baseline",
                         "selection_mode": "specific", "execution_mode": "shares"},
            "research": {"enabled": True, "require_validated_variant": True,
                         "db_path": str(db)},
        }
        with patch.dict(os.environ, {"ALPACA_LIVE_ENABLE": "true"}, clear=False), \
                patch.object(EdgeLedger, "latest_verified_run",
                             return_value=self._verified_proof()):
            cfg = validate_config(raw)
            engine = Engine(cfg, light=True, provider=LiveProvider())
        self.addCleanup(engine.close)
        self.assertEqual(engine._edge_record["candidate_id"], pinned["candidate_id"])
        ledger.transition(pinned["candidate_id"], "demoted",
                          reason="proof no longer eligible")
        self.assertFalse(engine._refresh_edge())
        self.assertIsNone(engine._edge_record)
        self.assertNotEqual(engine._edge_pinned_candidate_id,
                            other["candidate_id"])

    def test_live_edge_blocks_when_latest_proof_fails(self):
        db = Path(self.runtime_tmp.name) / "live-proof.sqlite3"
        ledger = EdgeLedger(db)
        pinned = self._prove(ledger, "ibr.baseline")

        class LiveProvider(FakeProvider):
            paper = False
            endpoint = "https://api.alpaca.markets"

        with patch.dict(os.environ, {"ALPACA_LIVE_ENABLE": "true"}, clear=False), \
                patch.object(EdgeLedger, "latest_verified_run",
                             return_value=self._verified_proof()):
            cfg = validate_config({
                "mode": "live", "broker": {"paper": False, "allow_live": True},
                "strategy": {"id": "ibr", "variant_id": "ibr.baseline",
                             "selection_mode": "specific"},
                "research": {"enabled": True, "require_validated_variant": True,
                             "db_path": str(db)},
            })
            engine = Engine(cfg, light=True, provider=LiveProvider())
        self.addCleanup(engine.close)
        self.assertFalse(engine._refresh_edge())
        self.assertIn("no latest-passing", engine._edge_error)

    def test_edge_rejects_shadow_proof_without_matching_candidate_config_hash(self):
        db = Path(self.runtime_tmp.name) / "edge-config-hash.sqlite3"
        ledger = EdgeLedger(db)
        record = self._prove(ledger, "ibr.baseline")
        cfg = {"strategy": {"id": "ibr", "variant_id": "ibr.baseline",
                             "selection_mode": "specific",
                             "execution_mode": "shares"}}
        for proof_hash in (None, "different-config"):
            with self.subTest(proof_hash=proof_hash), \
                    patch.object(
                        EdgeLedger, "latest_verified_run",
                        return_value=self._verified_proof(
                            config_hash=proof_hash or "")):
                self.assertIsNone(resolve_validated_variant(cfg, db_path=db))
        with patch.object(EdgeLedger, "latest_verified_run",
                          return_value=self._verified_proof(
                              config_hash=record["config_hash"])):
            self.assertIsNotNone(resolve_validated_variant(cfg, db_path=db))

    def test_live_preflight_requires_explicit_pdt_eligibility(self):
        db = Path(self.runtime_tmp.name) / "live-pdt.sqlite3"
        ledger = EdgeLedger(db)
        self._prove(ledger, "ibr.baseline")

        class LiveProvider(FakeProvider):
            paper = False
            endpoint = "https://api.alpaca.markets"

        with patch.dict(os.environ, {"ALPACA_LIVE_ENABLE": "true"}, clear=False), \
                patch.object(EdgeLedger, "latest_verified_run",
                             return_value=self._verified_proof()):
            cfg = validate_config({
                "mode": "live", "broker": {"paper": False, "allow_live": True},
                "strategy": {"id": "ibr", "variant_id": "ibr.baseline",
                             "selection_mode": "specific"},
                "research": {"enabled": True, "require_validated_variant": True,
                             "db_path": str(db)},
            })
            engine = Engine(cfg, light=True, provider=LiveProvider())
        self.addCleanup(engine.close)
        with self.assertRaisesRegex(AlpacaError, "pattern_day_trader=true"):
            engine.preflight()

    def test_live_engine_cannot_bypass_validated_edge_gate(self):
        class LiveProvider(FakeProvider):
            paper = False
            endpoint = "https://api.alpaca.markets"

        with patch.dict(os.environ, {"ALPACA_LIVE_ENABLE": "true"}, clear=False):
            with self.assertRaisesRegex(AlpacaError, "validated research edge gate"):
                Engine({
                    "mode": "live",
                    "broker": {"paper": False, "allow_live": True},
                    "strategy": {"id": "ibr", "variant_id": "ibr.baseline",
                                 "selection_mode": "specific"},
                }, light=True, provider=LiveProvider())

    def test_preflight_rejects_endpoint_hostname_confusion(self):
        class ConfusedEndpointProvider(FakeProvider):
            endpoint = "https://paper-api.alpaca.markets.attacker.invalid"

        engine = Engine(_cfg(), light=True, provider=ConfusedEndpointProvider())
        self.addCleanup(engine.close)
        with self.assertRaisesRegex(AlpacaError, "endpoint validation failed"):
            engine.preflight()

    def test_all_proved_resolver_keeps_one_deterministic_edge_per_family(self):
        db = Path(self.runtime_tmp.name) / "all-proved.sqlite3"
        ledger = EdgeLedger(db)
        rows = [
            ("rule.alpha.0000000000000001", "mean_reversion"),
            ("rule.alpha.0000000000000002", "mean_reversion"),
            ("rule.beta.0000000000000001", "volume_breakout"),
        ]
        for variant_id, family in rows:
            record = ledger.register_candidate(
                variant_id, strategy_id="rule", vehicle="equity",
                hypothesis=family,
                config={"strategy": {"rule_spec": {"family": family}}})
            with closing(sqlite3.connect(ledger.path)) as db_handle, db_handle:
                db_handle.execute(
                    "UPDATE candidate_state SET status='validated' WHERE candidate_id=?",
                    (record["candidate_id"],))
                db_handle.commit()
        def proof_for(candidate_id, *, lane=None):
            return self._verified_proof(
                config_hash=ledger.candidate(candidate_id)["config_hash"])
        with patch.object(EdgeLedger, "latest_verified_run",
                          side_effect=proof_for):
            selected = resolve_validated_variants({
                "strategy": {"id": "rule", "execution_mode": "shares",
                             "selection_mode": "all_proved"}}, db_path=db)
        self.assertEqual(len(selected), 2)
        self.assertEqual({row["variant_id"] for row in selected}, {
            "rule.alpha.0000000000000002",
            "rule.beta.0000000000000001",
        })

    def test_stale_broker_clock_blocks_entry_but_runs_cleanup_path(self):
        class StaleClockProvider(FakeProvider):
            def clock(self):
                return MarketClock(
                    datetime.now(timezone.utc) - timedelta(minutes=5), True,
                    next_close=self.day.close)

        provider = StaleClockProvider()
        engine = Engine(_cfg(), light=True, provider=provider)
        self.addCleanup(engine.close)
        result = engine.run_once({})
        self.assertEqual(result["action"], "hold")
        self.assertEqual(result["reason"], "broker_clock_invalid")
        self.assertIn("stale", result["error"])
        self.assertGreaterEqual(provider.cancel_calls, 1)

    def test_post_submit_journal_failure_pauses_for_reconciliation(self):
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
        snapshot = {"SPY": {"bars": bars, "quote": {
            "timestamp": now, "bid": 101.4, "ask": 101.5}}}
        engine = Engine(_cfg(), provider=provider, brain=BrainFake())
        self.addCleanup(engine.close)
        engine._wall_clock = lambda: provider.now
        with patch.object(engine, "_record_open_order",
                          side_effect=RuntimeError("journal unavailable")):
            with self.assertRaisesRegex(AlpacaError, "reconciliation required"):
                engine.run_once(snapshot)
        self.assertEqual(len(provider.orders_sent), 1)
        self.assertFalse(engine._reconciled)
        runtime = __import__("agent.state", fromlist=["load_state"]).load_state()
        self.assertTrue(runtime["operator_pause"])


if __name__ == "__main__":
    unittest.main()
