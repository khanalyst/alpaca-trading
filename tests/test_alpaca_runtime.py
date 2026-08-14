"""Dependency-free checks for the Alpaca runtime boundary."""

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import inspect
import os
import sys
import types
import pickle
import unittest
from unittest.mock import patch

from agent.alpaca_domain import Asset, OptionContract, OrderRequest
from agent.alpaca_provider import (AlpacaError, AlpacaProvider, AlpacaSession,
                                   IdempotencyConflict, PaperModeError)
from agent.alpaca_session import NEW_YORK, SessionPolicy, normalize_calendar_day
from agent.config import ConfigError, validate_config
from agent.instruments import (validate_equity_symbol,
                               validate_option_symbol)
from agent.market import MarketData


class TradingFake:
    def get_clock(self):
        return {"timestamp": "2026-08-07T14:00:00+00:00", "is_open": True}

    def get_calendar(self, **kwargs):
        return [{"date": "2026-08-07", "open": "09:30", "close": "16:00"}]

    def get_all_assets(self, *args, **kwargs):
        return [{"symbol": "spy", "class": "us_equity", "status": "active",
                 "tradable": True}]

    def get_orders(self, **kwargs):
        return []

    def submit_order(self, request):
        asset_class = "us_option" if request.symbol[-9:-8] in {"C", "P"} else "us_equity"
        return {"id": "order-1", "symbol": request.symbol,
                "asset_class": asset_class, "qty": str(request.qty),
                "side": request.side, "status": "accepted",
                "type": request.type, "time_in_force": request.time_in_force,
                "client_order_id": request.client_order_id}


class AlpacaRuntimeTests(unittest.TestCase):
    def test_live_requires_explicit_guard(self):
        with self.assertRaises(PaperModeError):
            AlpacaSession(paper=False)
        with patch.dict(os.environ, {"ALPACA_LIVE_ENABLE": "true"}, clear=False):
            session = AlpacaSession(paper=False, allow_live=True,
                                    trading_client=TradingFake())
        self.assertFalse(session.paper)
        self.assertEqual(session.endpoint, "https://api.alpaca.markets")

    def test_legacy_facade_aliases_and_policy_session_are_sdk_lazy(self):
        from agent import alpaca_provider as provider_module
        from agent import alpaca_sdk as sdk_module
        from agent import alpaca_session as session_module

        for name in ("EQUITY_FEEDS", "OPTION_FEEDS", "_canonical_feed",
                     "_sdk_feed", "_value", "_text", "_dt", "_mapping",
                     "_decimal_or_none", "_first", "normalize_quote",
                     "normalize_bar"):
            with self.subTest(name=name):
                self.assertIs(getattr(provider_module, name),
                              getattr(sdk_module, name))
        for name in ("AlpacaError", "CredentialsError", "PaperModeError",
                     "AlpacaSession"):
            with self.subTest(name=name):
                self.assertIs(getattr(provider_module, name),
                              getattr(session_module, name))
        self.assertTrue(issubclass(provider_module.IdempotencyConflict,
                                   session_module.AlpacaError))

        # Constructing policy/session objects must not import or instantiate SDK
        # clients.  Client imports remain inside the lazy properties.
        with patch("builtins.__import__",
                   side_effect=AssertionError("unexpected SDK import")):
            session = session_module.AlpacaSession(
                api_key="key", secret_key="secret", paper=True)
            session_module.SessionPolicy()
        self.assertIsNone(session._trading)
        self.assertIsNone(session._stock_data)
        self.assertIsNone(session._option_data)

    def test_market_data_mixin_facade_identity_lazy_seam_and_pickle(self):
        from agent import alpaca_provider as provider_module
        from agent.alpaca_market_data import AlpacaMarketDataMixin

        methods = ("option_contracts", "bars", "quotes", "option_chain",
                   "option_snapshots", "option_candidates")
        self.assertIs(provider_module.AlpacaMarketDataMixin, AlpacaMarketDataMixin)
        self.assertTrue(issubclass(provider_module.AlpacaProvider,
                                    AlpacaMarketDataMixin))
        for name in methods:
            with self.subTest(name=name):
                self.assertIs(inspect.getattr_static(provider_module.AlpacaProvider, name),
                              inspect.getattr_static(AlpacaMarketDataMixin, name))

        provider = AlpacaProvider({"mode": "paper"})
        bound = provider.option_chain
        self.assertIs(bound.__self__, provider)
        restored = pickle.loads(pickle.dumps(provider))
        self.assertEqual(type(restored), provider_module.AlpacaProvider)
        self.assertEqual(type(restored).__module__, "agent.alpaca_provider")

        class OptionData:
            def get_option_chain(self, request):
                return {}

        provider = AlpacaProvider(
            {"mode": "paper"},
            session=AlpacaSession(paper=True, option_data_client=OptionData()))
        original = provider_module._canonical_feed
        with patch.object(provider_module, "_canonical_feed",
                          wraps=original) as feed:
            provider.option_chain("SPY")
        self.assertTrue(feed.called)

        class ChainOptionData:
            def get_option_chain(self, request):
                return {"SPY260821C00600000": {
                    "symbol": "SPY260821C00600000",
                    "latest_quote": {
                        "bid_price": "1", "ask_price": "2",
                        "timestamp": "2026-08-09T12:00:00+00:00"}}}

        provider = AlpacaProvider(
            {"mode": "paper"},
            session=AlpacaSession(paper=True, trading_client=object(),
                                  option_data_client=ChainOptionData()))
        with (patch.object(provider_module, "parse_occ_symbol",
                           wraps=provider_module.parse_occ_symbol) as parse_occ,
              patch.object(provider_module, "validate_equity_symbol",
                           wraps=provider_module.validate_equity_symbol) as equity,
              patch.object(provider_module, "validate_option_symbol",
                           wraps=provider_module.validate_option_symbol) as option):
            provider.option_snapshots("SPY")
            provider.option_candidates(
                "SPY", now=datetime(2026, 8, 9, 13, tzinfo=NEW_YORK),
                underlying_price=Decimal("600"))
        self.assertTrue(parse_occ.called)
        self.assertTrue(equity.called)
        self.assertTrue(option.called)

    def test_a_trading_client_without_option_contracts_is_a_capability_gap(self):
        # option_candidates tolerates exactly one error text when it falls back
        # to the chain alone.  Whether alpaca-py is importable decides which
        # branch of option_contracts runs, so both have to spell the missing
        # endpoint the same way or the fallback silently stops working.
        provider = AlpacaProvider(
            {"mode": "paper"},
            session=AlpacaSession(paper=True, trading_client=object()))
        with self.assertRaises(AlpacaError) as raised:
            provider.option_contracts("SPY")
        self.assertIn("request support is unavailable", str(raised.exception).lower())

    def test_early_close_and_session_policy(self):
        day = normalize_calendar_day({"date": "2026-07-03", "open": "09:30", "close": "13:00"})
        policy = SessionPolicy(force_flat_minutes_before_close=10)
        self.assertTrue(policy.entry_allowed(datetime(2026, 7, 3, 12, 40, tzinfo=NEW_YORK), day))
        self.assertTrue(policy.should_force_flat(datetime(2026, 7, 3, 12, 55, tzinfo=NEW_YORK), day))
        with self.assertRaisesRegex(ValueError, "calendar open is required"):
            normalize_calendar_day({"date": "2026-07-03", "close": "13:00"})

    def test_normalized_assets_and_idempotent_order(self):
        provider = AlpacaProvider({"mode": "paper"}, session=AlpacaSession(paper=True, trading_client=TradingFake()))
        self.assertEqual(provider.assets()[0], Asset(symbol="SPY"))
        request = OrderRequest("SPY", Decimal("2"), "buy", client_order_id="test-1")
        first = provider.submit_order(request)
        second = provider.submit_order(request)
        self.assertEqual(first.client_order_id, "test-1")
        self.assertEqual(second.client_order_id, "test-1")
        contract = OptionContract.from_sdk({"symbol": "SPY260821C00600000"})
        self.assertTrue(contract.tradable)
        self.assertEqual(contract.underlying_symbol, "SPY")
        with self.assertRaisesRegex(ValueError, "does not match OCC"):
            OptionContract.from_sdk({
                "symbol": "SPY260821C00600000", "option_type": "put"})

    def test_order_request_rejects_non_boolean_or_enabled_extended_hours(self):
        for value in ("false", 1, None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "extended_hours"):
                OrderRequest("SPY", Decimal("1"), "buy", extended_hours=value)
        with self.assertRaisesRegex(ValueError, "extended_hours"):
            OrderRequest("SPY", Decimal("1"), "buy", extended_hours=True)

    def test_order_request_rejects_unsupported_stop_price(self):
        with self.assertRaisesRegex(ValueError, "stop orders are not supported"):
            OrderRequest("SPY", Decimal("1"), "buy", stop_price=Decimal("99"))

    def test_positions_require_finite_positive_qty_and_exact_side(self):
        class BadPositions(TradingFake):
            def get_all_positions(self):
                return [{"symbol": "SPY", "asset_class": "us_equity",
                         "qty": "NaN", "side": "long"}]

        provider = AlpacaProvider(
            {"mode": "paper"}, session=AlpacaSession(
                paper=True, trading_client=BadPositions()))
        with self.assertRaises(AlpacaError):
            provider.positions()

    def test_orders_apply_local_status_filters_and_all_is_unfiltered(self):
        class MisfilteredOrders(TradingFake):
            def get_orders(self, **kwargs):
                return [
                    {"id": "open", "symbol": "SPY", "asset_class": "us_equity",
                     "qty": "1", "side": "buy", "status": "accepted",
                     "type": "market", "time_in_force": "day"},
                    {"id": "filled", "symbol": "SPY", "asset_class": "us_equity",
                     "qty": "1", "side": "buy", "status": "filled",
                     "type": "market", "time_in_force": "day"},
                    {"id": "stopped", "symbol": "SPY", "asset_class": "us_equity",
                     "qty": "1", "side": "buy", "status": "stopped",
                     "type": "market", "time_in_force": "day"},
                ]

        provider = AlpacaProvider(
            {"mode": "paper"}, session=AlpacaSession(
                paper=True, trading_client=MisfilteredOrders()))
        self.assertEqual([order.id for order in provider.orders(status="open")], ["open"])
        self.assertEqual({order.id for order in provider.orders(status="all")},
                         {"open", "filled", "stopped"})

    def test_idempotency_exact_lookup_rejects_unrelated_order(self):
        class UnrelatedLookup(TradingFake):
            def get_order_by_client_id(self, client_order_id):
                return {"id": "other", "symbol": "SPY", "asset_class": "us_equity",
                        "qty": "1", "side": "buy", "status": "accepted",
                        "type": "market", "time_in_force": "day",
                        "client_order_id": "different"}

        provider = AlpacaProvider(
            {"mode": "paper"}, session=AlpacaSession(
                paper=True, trading_client=UnrelatedLookup()))
        request = OrderRequest("SPY", Decimal("1"), "buy", client_order_id="wanted")
        with self.assertRaises(IdempotencyConflict):
            provider.submit_order(request)

    def test_exact_order_lookup_treats_alpaca_missing_code_as_absent(self):
        class OrderNotFound(Exception):
            def __str__(self):
                return ('{"code":40410000,"message":"order not found for '
                        'smoke-open-test"}')

        class MissingLookup(TradingFake):
            def get_order_by_client_id(self, client_order_id):
                raise OrderNotFound()

        provider = AlpacaProvider(
            {"mode": "paper"}, session=AlpacaSession(
                paper=True, trading_client=MissingLookup()))
        self.assertEqual(provider.orders(client_order_id="smoke-open-test"), [])
        order = provider.submit_order(OrderRequest(
            "SPY", Decimal("1"), "buy",
            client_order_id="smoke-open-test"))
        self.assertEqual(order.client_order_id, "smoke-open-test")

    def test_exact_order_lookup_does_not_hide_unrelated_404(self):
        class RouteNotFound(Exception):
            status_code = 404

            def __str__(self):
                return '{"code":40400000,"message":"route not found"}'

        class BrokenLookup(TradingFake):
            def get_order_by_client_id(self, client_order_id):
                raise RouteNotFound()

        provider = AlpacaProvider(
            {"mode": "paper"}, session=AlpacaSession(
                paper=True, trading_client=BrokenLookup()))
        with self.assertRaisesRegex(AlpacaError, "route not found"):
            provider.orders(client_order_id="smoke-open-test")

    def test_idempotency_full_bounded_history_does_not_prove_absence(self):
        class FullHistory(TradingFake):
            def get_orders(self, **kwargs):
                return [{"id": str(index), "client_order_id": f"other-{index}"}
                        for index in range(500)]

        provider = AlpacaProvider(
            {"mode": "paper"}, session=AlpacaSession(
                paper=True, trading_client=FullHistory()))
        request = OrderRequest("SPY", Decimal("1"), "buy", client_order_id="wanted")
        with self.assertRaises(AlpacaError):
            provider.submit_order(request)

    def test_close_position_uses_held_qty_and_rejects_reversal(self):
        class HeldPosition(TradingFake):
            def get_all_positions(self):
                return [{"symbol": "SPY", "asset_class": "us_equity",
                         "qty": "2", "side": "long"}]

        provider = AlpacaProvider(
            {"mode": "paper"}, session=AlpacaSession(
                paper=True, trading_client=HeldPosition()))
        self.assertEqual(provider.close_position("SPY").qty, Decimal("2"))
        with self.assertRaises(AlpacaError):
            provider.close_position("SPY", qty=Decimal("3"))

    def test_provider_rejects_non_boolean_clock_open_flag(self):
        class StringClockTrading(TradingFake):
            def get_clock(self):
                return {"timestamp": "2026-08-07T14:00:00+00:00",
                        "is_open": "false"}

        provider = AlpacaProvider(
            {"mode": "paper"},
            session=AlpacaSession(paper=True,
                                  trading_client=StringClockTrading()))
        with self.assertRaisesRegex(AlpacaError,
                                     "clock is_open must be true or false"):
            provider.clock()

    def test_provider_rejects_non_boolean_pattern_day_trader(self):
        class StringPdtTrading(TradingFake):
            def get_account(self):
                return {"id": "account", "status": "active",
                        "equity": "100000", "cash": "100000",
                        "buying_power": "100000", "currency": "USD",
                        "pattern_day_trader": "false"}

        provider = AlpacaProvider(
            {"mode": "paper"},
            session=AlpacaSession(paper=True,
                                  trading_client=StringPdtTrading()))
        with self.assertRaisesRegex(AlpacaError,
                                     "pattern_day_trader must be true or false"):
            provider.account()

    def test_provider_defaults_missing_or_null_pattern_day_trader_to_false(self):
        missing = object()

        class AccountTrading(TradingFake):
            def __init__(self, pattern_day_trader=missing):
                self._pattern_day_trader = pattern_day_trader

            def get_account(self):
                account = {"id": "account", "status": "active",
                           "equity": "100000", "cash": "100000",
                           "buying_power": "100000", "currency": "USD"}
                if self._pattern_day_trader is not missing:
                    account["pattern_day_trader"] = self._pattern_day_trader
                return account

        for value in (missing, None):
            with self.subTest(value=value):
                provider = AlpacaProvider(
                    {"mode": "paper"},
                    session=AlpacaSession(
                        paper=True, trading_client=AccountTrading(value)))
                self.assertFalse(provider.account().pattern_day_trader)

    def test_provider_accepts_boolean_pattern_day_trader(self):
        class BooleanPdtTrading(TradingFake):
            def __init__(self, pattern_day_trader):
                self._pattern_day_trader = pattern_day_trader

            def get_account(self):
                return {"id": "account", "status": "active",
                        "equity": "100000", "cash": "100000",
                        "buying_power": "100000", "currency": "USD",
                        "pattern_day_trader": self._pattern_day_trader}

        for value in (True, False):
            with self.subTest(value=value):
                provider = AlpacaProvider(
                    {"mode": "paper"},
                    session=AlpacaSession(
                        paper=True, trading_client=BooleanPdtTrading(value)))
                self.assertIs(provider.account().pattern_day_trader, value)

    def test_market_status_does_not_construct_network_client(self):
        provider = AlpacaProvider({"mode": "paper"})
        market = MarketData(provider)
        # Entries remain closed until the broker calendar has been loaded.
        self.assertFalse(market.can_enter(datetime(2026, 8, 7, 12, tzinfo=NEW_YORK)))

    def test_config_is_paper_by_default_and_live_is_fail_closed(self):
        config = validate_config({})
        self.assertEqual(config["mode"], "paper")
        self.assertTrue(config["broker"]["paper"])
        self.assertFalse(config["llm"]["enabled"])
        with self.assertRaises(ConfigError):
            validate_config({"mode": "live", "broker": {"paper": False}})
        with self.assertRaisesRegex(ConfigError, "model is required"):
            validate_config({"llm": {"enabled": True}})

    def test_live_config_requires_all_three_guards_and_specific_variant(self):
        base = {"mode": "live", "broker": {"paper": False, "allow_live": True},
                "strategy": {"id": "ibr", "variant_id": "ibr.baseline",
                             "selection_mode": "specific"}}
        with self.assertRaisesRegex(ConfigError, "ALPACA_LIVE_ENABLE"):
            validate_config(base)
        with patch.dict(os.environ, {"ALPACA_LIVE_ENABLE": "true"}, clear=False):
            live = validate_config(base)
            self.assertFalse(live["broker"]["paper"])
            with self.assertRaisesRegex(ConfigError, "named strategy.variant_id"):
                validate_config({**base, "strategy": {
                    "id": "ibr", "variant_id": "auto", "selection_mode": "specific"}})
            with self.assertRaisesRegex(ConfigError, "selection_mode=specific"):
                validate_config({**base, "strategy": {
                    "id": "ibr", "variant_id": "ibr.baseline",
                    "selection_mode": "all_proved"}})
            with self.assertRaisesRegex(ConfigError, "live mode requires llm.enabled=false"):
                validate_config({**base, "llm": {"enabled": True, "model": "gpt-4o"}})
            self.assertFalse(validate_config({**base, "llm": {"enabled": False}})["llm"]["enabled"])
        self.assertTrue(validate_config({"llm": {"enabled": True, "model": "gpt-4o"}})["llm"]["enabled"])

    def test_bracket_order_requests_are_validated_and_translated(self):
        bracket = OrderRequest("SPY", Decimal("10"), "buy", order_class="bracket",
                               take_profit=Decimal("105"), stop_loss=Decimal("95"))
        self.assertEqual(bracket.order_class, "bracket")
        self.assertEqual((bracket.take_profit, bracket.stop_loss),
                         (Decimal("105"), Decimal("95")))
        plain = OrderRequest("SPY", Decimal("10"), "buy")
        self.assertIsNone(plain.order_class)
        self.assertIsNone(plain.take_profit)
        OrderRequest("SPY", Decimal("10"), "sell", order_class="bracket",
                     take_profit=Decimal("95"), stop_loss=Decimal("105"))
        OrderRequest("SPY", Decimal("10"), "buy", type="limit", limit_price=Decimal("100"),
                     order_class="bracket", take_profit=Decimal("105"), stop_loss=Decimal("95"))
        cases = [
            ({"order_class": "bracket", "take_profit": Decimal("105")}, "require both"),
            ({"order_class": "bracket", "stop_loss": Decimal("95")}, "require both"),
            ({"take_profit": Decimal("105"), "stop_loss": Decimal("95")}, "order_class=bracket"),
            ({"order_class": "oco", "take_profit": Decimal("105"),
              "stop_loss": Decimal("95")}, "unsupported order class"),
            ({"order_class": "bracket", "take_profit": Decimal("95"),
              "stop_loss": Decimal("105")}, "take_profit must be above stop_loss"),
            ({"order_class": "bracket", "take_profit": Decimal("0"),
              "stop_loss": Decimal("95")}, "finite and positive"),
            ({"order_class": "bracket", "take_profit": Decimal("nan"),
              "stop_loss": Decimal("95")}, "finite and positive"),
            ({"type": "limit", "limit_price": Decimal("110"), "order_class": "bracket",
              "take_profit": Decimal("105"), "stop_loss": Decimal("95")}, "straddle"),
        ]
        for kwargs, message in cases:
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, message):
                OrderRequest("SPY", Decimal("10"), "buy", **kwargs)
        with self.assertRaisesRegex(ValueError, "take_profit must be below stop_loss"):
            OrderRequest("SPY", Decimal("10"), "sell", order_class="bracket",
                         take_profit=Decimal("105"), stop_loss=Decimal("95"))
        with self.assertRaisesRegex(ValueError, "not supported for option symbols"):
            OrderRequest("SPY260821C00600000", Decimal("1"), "buy", order_class="bracket",
                         take_profit=Decimal("5"), stop_loss=Decimal("1"))
        with self.assertRaisesRegex(ValueError, "stop orders are not supported"):
            OrderRequest("SPY", Decimal("10"), "buy", stop_price=Decimal("95"),
                         order_class="bracket", take_profit=Decimal("105"),
                         stop_loss=Decimal("95"))
        with self.assertRaisesRegex(ValueError, "must be day"):
            OrderRequest("SPY", Decimal("10"), "buy", time_in_force="gtc",
                         order_class="bracket", take_profit=Decimal("105"),
                         stop_loss=Decimal("95"))
        with self.assertRaisesRegex(ValueError, "extended_hours must be false"):
            OrderRequest("SPY", Decimal("10"), "buy", extended_hours=True,
                         order_class="bracket", take_profit=Decimal("105"),
                         stop_loss=Decimal("95"))

        seen = {}

        class MarketOrderRequest:
            type = "market"

            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)
                seen.update(kwargs)

        class TakeProfitRequest:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class StopLossRequest(TakeProfitRequest):
            pass

        modules = {
            name: types.ModuleType(name) for name in (
                "alpaca", "alpaca.trading", "alpaca.trading.requests",
                "alpaca.trading.enums")}
        modules["alpaca.trading.requests"].MarketOrderRequest = MarketOrderRequest
        modules["alpaca.trading.requests"].LimitOrderRequest = MarketOrderRequest
        modules["alpaca.trading.requests"].TakeProfitRequest = TakeProfitRequest
        modules["alpaca.trading.requests"].StopLossRequest = StopLossRequest
        modules["alpaca.trading.enums"].OrderSide = types.SimpleNamespace(BUY="buy", SELL="sell")
        modules["alpaca.trading.enums"].TimeInForce = types.SimpleNamespace(DAY="day")
        modules["alpaca.trading.enums"].OrderClass = types.SimpleNamespace(BRACKET="bracket")

        session = AlpacaSession(paper=True, trading_client=TradingFake())
        with patch.dict(sys.modules, modules):
            provider = AlpacaProvider({"mode": "paper", "broker": {"paper": True}},
                                      session=session)
            provider.submit_order(replace(bracket, client_order_id="bracket-1"))
            self.assertEqual(seen["order_class"], "bracket")
            self.assertEqual(seen["take_profit"].limit_price, 105.0)
            self.assertEqual(seen["stop_loss"].stop_price, 95.0)
            seen.clear()
            provider.submit_order(replace(plain, client_order_id="plain-1"))
        self.assertNotIn("order_class", seen)
        self.assertNotIn("take_profit", seen)

    def test_crypto_gtc_and_malformed_entry_cutoff_are_rejected(self):
        for symbol in ("BTC/USD", "BTCUSD", "ETHUSD"):
            with self.subTest(symbol=symbol), self.assertRaises(ConfigError):
                validate_config({"universe": {"symbols": [symbol]}})
            with self.subTest(order_symbol=symbol), self.assertRaises(ValueError):
                OrderRequest(symbol, Decimal("1"), "buy")
        with self.assertRaisesRegex(ConfigError, "must be day"):
            validate_config({"execution": {"time_in_force": "gtc"}})
        with self.assertRaisesRegex(ValueError, "must be day"):
            OrderRequest("SPY", Decimal("1"), "buy", time_in_force="gtc")
        for cutoff in ("9:30", "24:00", "15:60", "15:00:00"):
            with self.subTest(cutoff=cutoff), self.assertRaises(ConfigError):
                validate_config({"strategy": {"latest_entry_time": cutoff}})
        self.assertEqual(validate_equity_symbol("ETH"), "ETH")
        with self.assertRaisesRegex(ValueError, "invalid expiration"):
            validate_option_symbol("SPY269931C00600000")
        with self.assertRaisesRegex(ValueError, "strike must be positive"):
            validate_option_symbol("SPY260821C00000000")
        self.assertEqual(validate_option_symbol(
            "SPY260821C00600000", underlying="SPY",
            expiration="2026-08-21", strike="600"),
            "SPY260821C00600000")

    def test_intraday_session_policy_is_not_configurable_away(self):
        with self.assertRaisesRegex(ConfigError, "entries_regular_session_only"):
            validate_config({"session": {"entries_regular_session_only": False}})
        with self.assertRaisesRegex(ConfigError, "allow_exits_outside_session"):
            validate_config({"session": {"allow_exits_outside_session": False}})
        with self.assertRaises(ConfigError):
            validate_config({"session": {"force_flat_minutes_before_close": 0}})
        with self.assertRaisesRegex(ConfigError, "model is required"):
            validate_config({"research": {"strategy_llm": {
                "enabled": True, "model": ""}}})

    def test_research_strategy_llm_and_proof_are_bounded(self):
        cfg = validate_config({"research": {
            "strategy_llm": {"enabled": True, "provider": "anthropic",
                             "model": "model", "max_attempts": 3,
                             "timeout_seconds": 120,
                             "max_response_bytes": 65536},
            "proof": {"directory": "proofs",
                      "webhook_url": "https://example.test/hook",
                      "webhook_timeout_seconds": 30},
        }})
        self.assertEqual(cfg["research"]["strategy_llm"]["max_attempts"], 3)
        self.assertEqual(cfg["research"]["proof"]["directory"], "proofs")
        with self.assertRaisesRegex(ConfigError, "HTTPS"):
            validate_config({"research": {"proof": {
                "webhook_url": "http://example.test/hook"}}})

    def test_provider_rejects_crypto_and_non_day_broker_rows(self):
        class CryptoTrading(TradingFake):
            def get_all_positions(self):
                return [{"symbol": "BTCUSD", "asset_class": "crypto",
                         "qty": "1", "side": "long"}]

            def get_orders(self, **kwargs):
                return [{"id": "bad", "symbol": "SPY", "qty": "1",
                         "asset_class": "us_equity",
                         "side": "buy", "status": "open", "type": "market",
                         "time_in_force": "gtc"}]

        provider = AlpacaProvider(
            {"mode": "paper"},
            session=AlpacaSession(paper=True, trading_client=CryptoTrading()))
        with self.assertRaises(AlpacaError):
            provider.positions()
        with self.assertRaisesRegex(AlpacaError, "must be day"):
            provider.orders()

        class MissingTifTrading(TradingFake):
            def get_orders(self, **kwargs):
                return [{"id": "bad", "symbol": "SPY", "qty": "1",
                         "asset_class": "us_equity", "side": "buy",
                         "status": "open", "type": "market"}]

        missing_tif = AlpacaProvider(
            {"mode": "paper"}, session=AlpacaSession(
                paper=True, trading_client=MissingTifTrading()))
        with self.assertRaisesRegex(AlpacaError, "time_in_force is required"):
            missing_tif.orders()

    def test_provider_rejects_unknown_asset_class(self):
        class UnknownAssetTrading(TradingFake):
            def get_all_assets(self, *args, **kwargs):
                return [{"symbol": "SPY", "class": "mystery",
                         "status": "active", "tradable": True}]

        provider = AlpacaProvider(
            {"mode": "paper"}, session=AlpacaSession(
                paper=True, trading_client=UnknownAssetTrading()))
        with self.assertRaisesRegex(ValueError, "unsupported asset class"):
            provider.assets()

        class StringBooleanTrading(TradingFake):
            def get_all_assets(self, *args, **kwargs):
                return [{"symbol": "SPY", "class": "us_equity",
                         "status": "active", "tradable": "false"}]

        string_boolean = AlpacaProvider(
            {"mode": "paper"}, session=AlpacaSession(
                paper=True, trading_client=StringBooleanTrading()))
        with self.assertRaisesRegex(ValueError, "tradable must be true or false"):
            string_boolean.assets()

        class MissingClassTrading(TradingFake):
            def get_all_positions(self):
                return [{"symbol": "SPY", "qty": "1", "side": "long"}]

            def get_orders(self, **kwargs):
                return [{"id": "bad", "symbol": "SPY", "qty": "1",
                         "side": "buy", "status": "open", "type": "market",
                         "time_in_force": "day"}]

        provider = AlpacaProvider(
            {"mode": "paper"}, session=AlpacaSession(
                paper=True, trading_client=MissingClassTrading()))
        with self.assertRaisesRegex(AlpacaError, "asset_class is required"):
            provider.positions()
        with self.assertRaisesRegex(ValueError, "asset_class is required"):
            provider.orders()


if __name__ == "__main__":
    unittest.main()
