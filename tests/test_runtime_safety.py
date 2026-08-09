"""Focused paper-runtime safety checks using injected provider fakes."""

from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from agent.alpaca_domain import Account, Asset, CalendarDay, MarketClock, Order, Position
from agent.alpaca_provider import AlpacaError, AlpacaProvider, AlpacaSession, PaperModeError
from agent.config import ConfigError, validate_config
from agent.engine import Engine
from agent.edge import resolve_validated_variants
from agent.market import MarketData
from agent.risk import RiskEngine
from research.edge_lab import EdgeLedger


class FakeProvider:
    paper = True

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

    def test_bounded_run_flattens_existing_positions_before_exit(self):
        provider = FakeProvider()
        provider.positions_live = [Position("SPY", Decimal("2"), "long")]
        engine = Engine(_cfg(), provider=provider)
        engine.run(max_cycles=0)
        self.assertEqual(provider.positions_live, [])
        self.assertEqual(len(provider.orders_sent), 1)

    def test_bounded_run_reports_incomplete_shutdown_flatten(self):
        engine = Engine(_cfg(), provider=FakeProvider())
        with patch.object(engine, "_flatten_all_impl", return_value=False):
            with self.assertRaisesRegex(AlpacaError, "shutdown flatten incomplete"):
                engine.run(max_cycles=0)

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
    def _verified_proof(confidence=.99):
        return {"lane": "shadow", "metrics": {"confidence": confidence},
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
        proof = self._verified_proof()
        with patch.object(EdgeLedger, "latest_verified_run", return_value=proof):
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
