"""Integration coverage for protection cycles while operator pause is active."""

from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import types
import unittest

from agent import state
from agent.alpaca_domain import Account, CalendarDay, MarketClock, Order, Position
from agent.engine import Engine


def _cfg() -> dict:
    return {
        "mode": "paper",
        "broker": {"paper": True},
        "universe": {"symbols": ["SPY"]},
        "session": {
            "timezone": "America/New_York",
            "entries_regular_session_only": True,
            "allow_exits_outside_session": True,
            "force_flat_minutes_before_close": 10,
            "reject_new_entries_minutes_before_close": 5,
        },
        "strategy": {
            "id": "ibr", "version": "v1", "range_minutes": 15,
            "breakout_buffer_bps": 5, "min_relative_volume": 1,
            "target_r": 2, "max_entry_extension_r": 1,
            "min_ibr_width_atr": 0, "max_ibr_width_atr": 99,
            "latest_entry_time": "15:00",
            "force_flat_minutes_before_close": 10,
        },
        "risk": {
            "risk_per_trade_pct": .5, "max_open_risk_pct": 2,
            "max_concurrent_positions": 3,
            "max_position_notional_pct": 25,
        },
        "execution": {
            "client_order_id_prefix": "ibr",
            "max_market_data_age_seconds": 30,
        },
        "llm": {},
    }


class ProtectionProvider:
    paper = True
    endpoint = "https://paper-api.alpaca.markets"
    data_feed = "iex"
    options_feed = "indicative"

    def __init__(self, now: datetime, positions: list[Position] | None = None):
        self.session = types.SimpleNamespace(api_key="key", secret_key="secret")
        self.now = now
        self.day = CalendarDay(
            date(2026, 8, 7),
            datetime(2026, 8, 7, 13, 30, tzinfo=timezone.utc),
            datetime(2026, 8, 7, 20, 0, tzinfo=timezone.utc),
        )
        self.positions_live = list(positions or [])
        self.orders_sent: list[object] = []
        self.order_history: list[Order] = []
        self.cancel_calls = 0

    def calendar(self, **_):
        return [self.day]

    def clock(self):
        return MarketClock(self.now, True, next_close=self.day.close)

    def account(self):
        return Account("account", "active", Decimal("100000"),
                       Decimal("100000"), Decimal("100000"))

    def positions(self):
        return list(self.positions_live)

    def orders(self, **_):
        return list(self.order_history)

    def submit_order(self, request):
        self.orders_sent.append(request)
        self.positions_live = []
        order = Order("close-order", request.symbol, request.qty, request.side,
                      "filled", request.type, request.time_in_force,
                      filled_qty=request.qty, filled_avg_price=Decimal("100"),
                      client_order_id=request.client_order_id)
        self.order_history.append(order)
        return order

    def cancel_all_orders(self):
        self.cancel_calls += 1


class PausedProtectionTests(unittest.TestCase):
    def setUp(self):
        self.runtime_tmp = tempfile.TemporaryDirectory(
            prefix="alpaca-paused-protection-")
        self.original_runtime_base = state.RUNTIME_BASE
        state.RUNTIME_BASE = Path(self.runtime_tmp.name)
        state.configure_runtime("paper")
        state.ensure_ready()
        self.addCleanup(self._cleanup_runtime)

    def _cleanup_runtime(self):
        state.RUNTIME_BASE = self.original_runtime_base
        state.configure_runtime("paper")
        self.runtime_tmp.cleanup()

    @staticmethod
    def _pause():
        state.update_state(lambda current: {
            **current, "state": state.PAUSED, "operator_pause": True,
        })

    def _engine(self, provider):
        engine = Engine(_cfg(), light=True, provider=provider)
        engine._wall_clock = lambda: provider.now
        self.addCleanup(engine.close)
        return engine

    def test_paused_1550_cycle_flattens_before_close(self):
        provider = ProtectionProvider(
            datetime(2026, 8, 7, 19, 50, tzinfo=timezone.utc),
            [Position("SPY", Decimal("2"), "long",
                      market_value=Decimal("200"), current_price=Decimal("100"))],
        )
        self._pause()
        engine = self._engine(provider)

        result = engine.run_once({"SPY": {}})

        self.assertEqual(result["action"], "force_flat")
        self.assertEqual(provider.positions_live, [])
        self.assertEqual([order.side for order in provider.orders_sent], ["sell"])
        self.assertTrue(state.load_state()["operator_pause"])
        self.assertEqual(
            json.loads(state.HEARTBEAT_FILE.read_text())["status"], "paused")

    def test_paused_force_flat_failure_publishes_degraded_health(self):
        provider = ProtectionProvider(
            datetime(2026, 8, 7, 19, 50, tzinfo=timezone.utc),
            [Position("SPY", Decimal("2"), "long",
                      market_value=Decimal("200"), current_price=Decimal("100"))],
        )
        self._pause()
        engine = self._engine(provider)
        engine.flatten_all = types.MethodType(
            lambda _self, _reason: False, engine)

        result = engine.run_once({"SPY": {}})

        self.assertEqual(result["action"], "force_flat")
        self.assertFalse(result["closed"])
        heartbeat = json.loads(state.HEARTBEAT_FILE.read_text())
        self.assertEqual(heartbeat["status"], "degraded")
        self.assertNotEqual(heartbeat.get("reason"), "operator_pause")

    def test_paused_before_cutoff_runs_position_monitor_without_entries(self):
        provider = ProtectionProvider(
            datetime(2026, 8, 7, 18, 0, tzinfo=timezone.utc),
            [Position("SPY", Decimal("1"), "long",
                      market_value=Decimal("100"), current_price=Decimal("100"))],
        )
        self._pause()
        engine = self._engine(provider)

        result = engine.run_once({"SPY": {}})

        self.assertEqual(result["action"], "close")
        self.assertEqual(provider.positions_live, [])
        self.assertEqual([order.side for order in provider.orders_sent], ["sell"])
        self.assertTrue(state.load_state()["operator_pause"])
        self.assertEqual(
            json.loads(state.HEARTBEAT_FILE.read_text())["status"], "paused")

    def test_paused_position_close_failure_publishes_degraded_health(self):
        provider = ProtectionProvider(
            datetime(2026, 8, 7, 18, 0, tzinfo=timezone.utc),
            [Position("SPY", Decimal("1"), "long",
                      market_value=Decimal("100"), current_price=Decimal("100"))],
        )
        self._pause()
        engine = self._engine(provider)
        engine._monitor_positions = types.MethodType(
            lambda _self, *_args, **_kwargs: {
                "closed": [],
                "failed": [{"symbol": "SPY", "reason": "close_failed"}],
            }, engine)

        result = engine.run_once({"SPY": {}})

        self.assertEqual(result["action"], "hold")
        self.assertEqual(result["reason"], "position_close_failed")
        heartbeat = json.loads(state.HEARTBEAT_FILE.read_text())
        self.assertEqual(heartbeat["status"], "degraded")
        self.assertNotEqual(heartbeat.get("reason"), "operator_pause")

    def test_paused_account_identity_mismatch_stays_closed(self):
        provider = ProtectionProvider(
            datetime(2026, 8, 7, 18, 0, tzinfo=timezone.utc))
        foreign = state.account_fingerprint("paper", "different-key")
        state.update_state(lambda current: {
            **current,
            "account_fingerprint": foreign,
            "state": state.PAUSED,
            "operator_pause": True,
        })
        engine = self._engine(provider)

        result = engine.run_once({"SPY": {}})

        self.assertEqual(result["action"], "hold")
        self.assertEqual(provider.orders_sent, [])
        self.assertEqual(state.load_state()["account_fingerprint"], foreign)
        self.assertEqual(
            json.loads(state.HEARTBEAT_FILE.read_text())["status"], "degraded")

    def test_unpaused_force_flat_failure_publishes_degraded_health(self):
        provider = ProtectionProvider(
            datetime(2026, 8, 7, 19, 50, tzinfo=timezone.utc),
            [Position("SPY", Decimal("2"), "long",
                      market_value=Decimal("200"), current_price=Decimal("100"))],
        )
        engine = self._engine(provider)
        engine._ensure_order_ready = lambda: True
        engine.reconcile = lambda: {"positions": []}
        engine._reconciled = True
        engine.flatten_all = types.MethodType(
            lambda _self, _reason: False, engine)

        result = engine.run_once({"SPY": {}})

        self.assertEqual(result["action"], "force_flat")
        self.assertFalse(result["closed"])
        heartbeat = json.loads(state.HEARTBEAT_FILE.read_text())
        self.assertEqual(heartbeat["status"], "degraded")
        self.assertNotEqual(heartbeat.get("reason"), "operator_pause")

    def test_paused_daily_stop_failure_publishes_degraded_health(self):
        provider = ProtectionProvider(
            datetime(2026, 8, 7, 18, 0, tzinfo=timezone.utc))
        self._pause()
        engine = self._engine(provider)
        engine._update_daily_risk = types.MethodType(
            lambda _self, *_args, **_kwargs: (-1000.0, True), engine)
        engine.flatten_all = types.MethodType(
            lambda _self, _reason: False, engine)

        result = engine.run_once({"SPY": {}})

        self.assertEqual(result["action"], "day_stopped")
        self.assertFalse(result["closed"])
        heartbeat = json.loads(state.HEARTBEAT_FILE.read_text())
        self.assertEqual(heartbeat["status"], "degraded")
        self.assertNotEqual(heartbeat.get("reason"), "daily_loss_limit")

    def test_paused_cycle_never_opens_new_position(self):
        provider = ProtectionProvider(datetime(2026, 8, 7, 18, 0,
                                              tzinfo=timezone.utc))
        self._pause()
        engine = self._engine(provider)

        result = engine.run_once({"SPY": {}})

        self.assertEqual(result, {"action": "hold", "reason": "operator_pause",
                                  "closed": False, "residual": []})
        self.assertEqual(provider.orders_sent, [])

    def test_untrusted_pause_state_and_preflight_failure_fail_degraded_closed(self):
        for value in ("true", None, 0, 1):
            with self.subTest(operator_pause=value):
                provider = ProtectionProvider(
                    datetime(2026, 8, 7, 18, 0, tzinfo=timezone.utc))
                state.update_state(lambda current, value=value: {
                    **current, "state": state.PAUSED,
                    "operator_pause": value,
                })
                engine = self._engine(provider)
                result = engine.run_once({"SPY": {}})
                self.assertEqual(result["action"], "hold")
                self.assertEqual(provider.orders_sent, [])
                heartbeat = json.loads(state.HEARTBEAT_FILE.read_text())
                self.assertEqual(heartbeat["status"], "degraded")

        class MissingCredentialsProvider(ProtectionProvider):
            def __init__(self, now):
                super().__init__(now)
                self.session = types.SimpleNamespace(api_key="",
                                                     secret_key="secret")

        state.update_state(lambda current: {
            **current, "state": state.PAUSED, "operator_pause": True,
        })
        provider = MissingCredentialsProvider(
            datetime(2026, 8, 7, 18, 0, tzinfo=timezone.utc))
        engine = self._engine(provider)
        result = engine.run_once({"SPY": {}})
        self.assertEqual(result["action"], "hold")
        self.assertIn("credentials", result["reason"])
        self.assertEqual(provider.orders_sent, [])
        heartbeat = json.loads(state.HEARTBEAT_FILE.read_text())
        self.assertEqual(heartbeat["status"], "degraded")

    def test_malformed_durable_state_cannot_enter_protection_lane(self):
        provider = ProtectionProvider(datetime(2026, 8, 7, 18, 0,
                                              tzinfo=timezone.utc))
        engine = self._engine(provider)
        malformed = dict(state.DEFAULT)
        malformed.update({"state": "UNKNOWN", "operator_pause": True})
        state.STATE_FILE.write_text(json.dumps(malformed))

        result = engine.run_once({"SPY": {}})

        self.assertEqual(result["action"], "hold")
        self.assertEqual(provider.orders_sent, [])
        heartbeat = json.loads(state.HEARTBEAT_FILE.read_text())
        self.assertEqual(heartbeat["status"], "degraded")


if __name__ == "__main__":
    unittest.main()
