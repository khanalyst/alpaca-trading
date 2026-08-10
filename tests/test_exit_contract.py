"""Differential proof that research and runtime share one exit contract.

The bounded rule grammar declares `max_hold_bars`.  Research enforces it in
`research.factory_core._simulate_trade`; the trader enforces it in
`_monitor_positions`.  These tests drive both over the same deterministic
one-minute series and require the same exit bar, so the two implementations
cannot silently diverge again.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from agent import state
from agent.alpaca_domain import Account, Order, OrderRequest, Position, Quote
from agent.config import validate_config
from agent.contracts.rule import (BAR_SECONDS, RuleSpecError,
                                  generate_rule_signal, hold_deadline,
                                  rule_variant_id, validate_rule_spec)
from agent.engine import Engine
from agent.risk import RiskEngine
from agent.strategy import build_setup_plan
from research.factory_core import _simulate_trade, simulate_account
from research.market_data import normalize_underlying_bar

BASE = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
SPEC = validate_rule_spec({
    "family": "momentum_continuation", "lookback": 3, "slow_lookback": 8,
    "atr_period": 3, "threshold_bps": 1.0, "stop_atr": 1.0, "target_r": 2.0,
    "max_hold_bars": 3, "confirmation": "none",
})
# Four rising bars produce the signal at index 3, so the simulated entry bar
# is index 4 and the bounded hold expires at the end of index 7.
RISING = [100.2, 100.4, 100.6, 100.8]
FLAT = [100.8] * 8


def _payloads(closes, opens=None):
    """``opens`` overrides an index's open, i.e. gaps that bar away from the
    previous close.  Without it every bar opens where the last one closed,
    which is exactly the case that hides an anchor divergence."""
    rows = []
    opened = 100.0
    for index, close in enumerate(closes):
        opened = float((opens or {}).get(index, opened))
        timestamp = BASE + timedelta(minutes=index)
        end = timestamp + timedelta(minutes=1)
        rows.append({
            "kind": "bar", "provider": "test", "feed": "sip", "symbol": "SPY",
            "timestamp": timestamp.isoformat(), "as_of": end.isoformat(),
            "observed_at": end.isoformat(), "open": opened,
            "high": max(opened, close) + .05, "low": min(opened, close) - .05,
            "close": close, "volume": 1000,
        })
        opened = close
    return rows


def _bars(closes, opens=None):
    return [normalize_underlying_bar(row) for row in _payloads(closes, opens)]


class ExitBroker:
    """Minimal broker snapshot; the monitor only needs a held position."""

    paper = True
    data_feed = "iex"
    options_feed = "indicative"

    class Session:
        api_key = "paper-key"
        secret_key = "paper-secret"

    session = Session()
    endpoint = "https://paper-api.alpaca.markets"

    def __init__(self):
        self.orders_by_id = {}
        self.positions_live = []
        self._next_id = 1

    def submit_order(self, request: OrderRequest) -> Order:
        order_id = f"order-{self._next_id}"
        self._next_id += 1
        order = Order(order_id, request.symbol, request.qty, request.side,
                      "accepted", request.type, request.time_in_force,
                      client_order_id=request.client_order_id)
        self.orders_by_id[order_id] = order
        return order

    def close_position(self, symbol, qty=None, *, client_order_id=None,
                       order_type="market", time_in_force="day"):
        request = OrderRequest(str(symbol).upper(), qty or Decimal("1"), "sell",
                               type=order_type, time_in_force=time_in_force,
                               client_order_id=client_order_id)
        return self.submit_order(request)

    def reconcile(self):
        return {"positions": list(self.positions_live),
                "orders": list(self.orders_by_id.values())}

    def positions(self):
        return list(self.positions_live)

    def quotes(self, symbols, **_):
        return {str(symbol).upper(): [
            Quote(str(symbol).upper(), BASE, bid=Decimal("100"),
                  ask=Decimal("101"))] for symbol in symbols}

    def account(self):
        return Account("paper-account", "active", Decimal("100000"),
                       Decimal("100000"), Decimal("100000"))


class HoldDeadlineTests(unittest.TestCase):
    def test_deadline_is_the_close_of_the_last_permitted_bar(self):
        entry = BASE.timestamp()
        self.assertEqual(hold_deadline(entry, {"max_hold_bars": 1}),
                         entry + 2 * BAR_SECONDS)
        self.assertEqual(hold_deadline(entry, {"max_hold_bars": 3}),
                         entry + 4 * BAR_SECONDS)
        self.assertEqual(hold_deadline(BASE, {"max_hold_bars": 3}),
                         entry + 4 * BAR_SECONDS)

    def test_absent_max_hold_bars_means_no_time_exit(self):
        self.assertIsNone(hold_deadline(BASE.timestamp(), {}))
        self.assertIsNone(hold_deadline(BASE.timestamp(),
                                        {"max_hold_bars": None}))

    def test_session_force_flat_clamps_the_deadline(self):
        entry = BASE.timestamp()
        limit = entry + 90.0
        self.assertEqual(hold_deadline(entry, {"max_hold_bars": 390},
                                       force_flat_ts=limit), limit)
        self.assertEqual(hold_deadline(entry, {"max_hold_bars": 1},
                                       force_flat_ts=entry + 10_000.0),
                         entry + 2 * BAR_SECONDS)

    def test_malformed_inputs_are_rejected_rather_than_ignored(self):
        entry = BASE.timestamp()
        for spec in ({"max_hold_bars": True}, {"max_hold_bars": 3.5},
                     {"max_hold_bars": 0}, {"max_hold_bars": 391}):
            with self.assertRaises(RuleSpecError):
                hold_deadline(entry, spec)
        with self.assertRaises(RuleSpecError):
            hold_deadline("not-a-time", {"max_hold_bars": 3})
        with self.assertRaises(RuleSpecError):
            hold_deadline(float("inf"), {"max_hold_bars": 3})
        with self.assertRaises(RuleSpecError):
            hold_deadline(entry, {"max_hold_bars": 3}, force_flat_ts="soon")


class PlanCarriesTheHoldTests(unittest.TestCase):
    def setUp(self):
        self.variant_id = rule_variant_id(SPEC)
        self.cfg = validate_config({"strategy": {
            "id": "rule", "version": "v1", "variant_id": self.variant_id,
            "rule_spec": SPEC}})

    def _signal(self, bars):
        return generate_rule_signal("SPY", _payloads([bar.close for bar in bars]),
                                    config=self.cfg,
                                    now=datetime.now(timezone.utc))

    def test_rule_plan_carries_the_bounded_hold(self):
        bars = _bars(RISING + FLAT)
        signal = self._signal(bars[:4])
        self.assertIsNotNone(signal)
        snapshot = {"price": signal["entry_price"], "signal_ts": signal["signal_ts"],
                    "session": signal["session"], "spread_bps": 1.0,
                    "stale": False, "quote_stale": False}
        plan, why = build_setup_plan(signal, snapshot, self.cfg)
        self.assertIsNone(why)
        self.assertEqual(plan["max_hold_bars"], 3)
        self.assertEqual(plan["hold_deadline_ts"],
                         bars[7].end.timestamp())

    def test_ibr_plan_has_no_time_exit(self):
        cfg = {"strategy": {"id": "ibr", "version": "v1", "target_r": 2.0,
                            "breakout_buffer_bps": 5, "min_relative_volume": 1}}
        snapshot = {"price": 101, "close": 101, "signal_ts": 1710164760,
                    "ibr_range": {"high": 100.5, "low": 99.5, "width": 1,
                                  "range_end_ts": 1710164700, "complete": True},
                    "relative_volume": 2, "spread_bps": 10, "stale": False,
                    "quote_stale": False, "session": "2024-03-11"}
        plan, why = build_setup_plan(
            {"symbol": "SPY", "direction": "long", "setup_type": "ibr_breakout"},
            snapshot, cfg)
        self.assertIsNone(why)
        self.assertIsNone(plan.get("max_hold_bars"))
        self.assertIsNone(plan.get("hold_deadline_ts"))

    def test_risk_plan_forwards_the_hold_fields(self):
        risk = RiskEngine({"risk": {"risk_per_trade_pct": 1}})
        decision = {"symbol": "SPY", "direction": "long", "entry_price": 101,
                    "stop_price": 99, "target_price": 105,
                    "max_hold_bars": 3, "hold_deadline_ts": 1767622200.0}
        plan, why = risk.vet_open(decision, 10_000, [], {"SPY": {"price": 101}},
                                  {}, 0, now=0)
        self.assertIsNone(why)
        self.assertEqual(plan["max_hold_bars"], 3)
        self.assertEqual(plan["hold_deadline_ts"], 1767622200.0)
        plan, why = risk.vet_open({key: value for key, value in decision.items()
                                   if not key.startswith(("max_hold", "hold_"))},
                                  10_000, [], {"SPY": {"price": 101}}, {}, 0, now=0)
        self.assertIsNone(why)
        self.assertIsNone(plan["max_hold_bars"])
        self.assertIsNone(plan["hold_deadline_ts"])


class ExitContractDifferentialTests(unittest.TestCase):
    """Both engines are driven over one series and must agree on the exit."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="alpaca-exit-")
        self.variant_id = rule_variant_id(SPEC)
        self.cfg = validate_config({"strategy": {
            "id": "rule", "version": "v1", "variant_id": self.variant_id,
            "rule_spec": SPEC}})
        self.provider = ExitBroker()
        self.engine = None
        self.original_runtime_base = state.RUNTIME_BASE
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            if self.engine is not None:
                self.engine.close()
        finally:
            state.RUNTIME_BASE = self.original_runtime_base
            state.configure_runtime("paper")
            self.tmp.cleanup()

    def _engine(self, name):
        state.RUNTIME_BASE = Path(self.tmp.name) / name
        self.engine = Engine({
            "mode": "paper", "broker": {"paper": True},
            "universe": {"symbols": ["SPY"]},
            "session": {"timezone": "America/New_York",
                        "allow_exits_outside_session": True,
                        "force_flat_minutes_before_close": 10},
            "strategy": {"id": "rule", "version": "v1",
                         "variant_id": self.variant_id, "rule_spec": SPEC,
                         "execution_mode": "shares"},
            "risk": {}, "execution": {"client_order_id_prefix": "exit"},
            "llm": {"enabled": False},
            "research": {"enabled": True, "require_validated_variant": False,
                         "db_path": str(Path(self.tmp.name) / "edge.sqlite3")},
        }, light=True, provider=self.provider)
        state.ensure_ready()
        self.engine.market.should_force_flat = lambda now=None: False
        return self.engine

    def _open_runtime_trade(self, bars, name):
        """Persist the same signal through plan, risk, and fill activation."""
        engine = self._engine(name)
        signal = generate_rule_signal(
            "SPY", _payloads([bar.close for bar in bars[:4]]), config=self.cfg,
            now=datetime.now(timezone.utc))
        snapshot = {"price": signal["entry_price"], "signal_ts": signal["signal_ts"],
                    "session": signal["session"], "spread_bps": 1.0,
                    "stale": False, "quote_stale": False}
        plan, why = build_setup_plan(signal, snapshot, self.cfg)
        self.assertIsNone(why)
        risk_plan, why = engine.risk.vet_open(
            dict(plan), 100_000, [], {"SPY": {"price": plan["entry_price"]}},
            {}, 0, now=signal["signal_ts"])
        self.assertIsNone(why)
        risk_plan.update({"underlying_symbol": "SPY", "setup_id": plan["setup_id"],
                          "setup_type": plan["setup_type"]})
        request = OrderRequest("SPY", Decimal("10"), "buy",
                               client_order_id=f"entry-{name}")
        order = self.provider.submit_order(request)
        engine._record_open_order(request, order, risk_plan)
        self.provider.orders_by_id[order.id] = replace(
            order, status="filled", filled_qty=Decimal("10"),
            filled_avg_price=Decimal(str(plan["entry_price"])))
        self.provider.positions_live = [self._position(plan["entry_price"])]
        engine.reconcile()
        return engine, plan

    def _position(self, price):
        return Position("SPY", Decimal("10"), "long",
                        current_price=Decimal(str(price)),
                        market_value=Decimal(str(float(price) * 10)))

    def _drive(self, engine, bars, *, start_index):
        """Return the first (reason, exit timestamp) the monitor produces."""
        for bar in bars[start_index:]:
            result = engine._monitor_positions(bar.end, [self._position(bar.close)])
            if result["closed"]:
                return result["closed"][0]["reason"], bar.end
        return None, None

    def _differential(self, closes, name, *, force_flat_from=None, opens=None):
        bars = _bars(closes, opens)
        simulated = _simulate_trade(bars, SPEC, [], "equity")
        self.assertIsNotNone(simulated)
        engine, plan = self._open_runtime_trade(bars, name)
        if force_flat_from is not None:
            engine.market.should_force_flat = (
                lambda now=None: now is not None and
                now >= bars[force_flat_from].end)
        self.provider.positions_live = [self._position(bars[-1].close)]
        # The simulator evaluates protective exits from the bar after entry.
        reason, exit_at = self._drive(engine, bars, start_index=5)
        # The protective levels themselves, not only their timing, are part of
        # the shared contract: both engines anchor them to the signal close.
        self.assertAlmostEqual(simulated["stop_price"], plan["stop_price"], places=9)
        self.assertAlmostEqual(simulated["target_price"], plan["target_price"], places=9)
        return simulated, plan, reason, exit_at

    def test_time_exit_fires_at_the_simulated_exit_bar(self):
        simulated, plan, reason, exit_at = self._differential(
            RISING + FLAT, "runtime-time")
        self.assertEqual(simulated["exit_reason"], "time")
        self.assertEqual(reason, "max_hold")
        self.assertEqual(exit_at.isoformat(), simulated["exit_timestamp"])
        self.assertEqual(plan["hold_deadline_ts"], exit_at.timestamp())

    def test_stop_preempts_the_time_exit_in_both_engines(self):
        closes = RISING + [100.8, 100.4] + FLAT[:5]
        simulated, _, reason, exit_at = self._differential(closes, "runtime-stop")
        self.assertEqual(simulated["exit_reason"], "stop")
        self.assertEqual(reason, "stop")
        self.assertEqual(exit_at.isoformat(), simulated["exit_timestamp"])

    def test_target_preempts_the_time_exit_in_both_engines(self):
        closes = RISING + [100.8, 101.5] + FLAT[:5]
        simulated, _, reason, exit_at = self._differential(closes, "runtime-target")
        self.assertEqual(simulated["exit_reason"], "target")
        self.assertEqual(reason, "target")
        self.assertEqual(exit_at.isoformat(), simulated["exit_timestamp"])

    def test_force_flat_preempts_the_time_exit_at_the_session_clamp(self):
        # The simulator clamps the hold to the final session bar; the runtime
        # reaches the same bar through its hard session constraint.
        closes = RISING + FLAT[:2]
        simulated, _, reason, exit_at = self._differential(
            closes, "runtime-flat", force_flat_from=5)
        self.assertEqual(simulated["exit_reason"], "time")
        self.assertEqual(reason, "before_close")
        self.assertEqual(exit_at.isoformat(), simulated["exit_timestamp"])

    def test_time_exit_does_not_need_a_tradable_price(self):
        bars = _bars(RISING + FLAT)
        engine, _ = self._open_runtime_trade(bars, "runtime-blind")
        blind = Position("SPY", Decimal("10"), "long",
                         market_value=Decimal("1008"))
        self.provider.positions_live = [blind]
        engine.market.stock_quotes = lambda *args, **kwargs: {}
        result = engine._monitor_positions(bars[6].end, [blind])
        self.assertEqual([row["reason"] for row in result["closed"]],
                         ["protection_data_unavailable"])
        state.update_state(lambda current: {
            **current,
            "active_trades": {"SPY": {
                key: value for key, value
                in current["active_trades"]["SPY"].items()
                if not key.startswith("closing_")}}})
        result = engine._monitor_positions(bars[7].end, [blind])
        self.assertEqual([row["reason"] for row in result["closed"]],
                         ["max_hold"])

    def test_a_malformed_deadline_closes_the_position(self):
        bars = _bars(RISING + FLAT)
        engine, _ = self._open_runtime_trade(bars, "runtime-malformed")
        state.update_state(lambda current: {
            **current,
            "active_trades": {"SPY": {**current["active_trades"]["SPY"],
                                      "hold_deadline_ts": "not-a-time"}}})
        result = engine._monitor_positions(bars[5].end,
                                           [self._position(bars[5].close)])
        self.assertEqual([row["reason"] for row in result["closed"]], ["max_hold"])

    def test_a_gap_up_entry_does_not_move_the_protective_levels(self):
        simulated, plan, reason, exit_at = self._differential(
            RISING + FLAT, "runtime-gap-up", opens={4: 101.0})
        self.assertEqual(simulated["underlying_entry"], 101.0)
        self.assertAlmostEqual(simulated["stop_price"], 100.5, places=9)
        self.assertAlmostEqual(simulated["target_price"], 101.4, places=9)
        self.assertEqual(simulated["exit_reason"], "time")
        self.assertEqual(reason, "max_hold")
        self.assertEqual(exit_at.isoformat(), simulated["exit_timestamp"])
        # Nominal R is unchanged; the gap shows up as real risk per share.
        self.assertAlmostEqual(simulated["stop_distance"], .3, places=9)
        self.assertAlmostEqual(simulated["risk_per_unit"], .5, places=9)

    def test_a_gap_down_entry_does_not_move_the_protective_levels(self):
        simulated, plan, reason, exit_at = self._differential(
            RISING + FLAT, "runtime-gap-down", opens={4: 100.6})
        self.assertEqual(simulated["underlying_entry"], 100.6)
        self.assertAlmostEqual(simulated["stop_price"], 100.5, places=9)
        self.assertAlmostEqual(simulated["target_price"], 101.4, places=9)
        self.assertEqual(simulated["exit_reason"], "time")
        self.assertEqual(reason, "max_hold")
        self.assertEqual(exit_at.isoformat(), simulated["exit_timestamp"])
        self.assertAlmostEqual(simulated["risk_per_unit"], .1, places=9)

    def test_a_gapped_entry_still_agrees_on_a_stop_exit(self):
        closes = RISING + [100.8, 100.4] + FLAT[:5]
        simulated, _, reason, exit_at = self._differential(
            closes, "runtime-gap-stop", opens={4: 101.0})
        self.assertEqual(simulated["exit_reason"], "stop")
        self.assertEqual(reason, "stop")
        self.assertEqual(exit_at.isoformat(), simulated["exit_timestamp"])

    def test_a_gapped_entry_still_agrees_on_a_target_exit(self):
        closes = RISING + [100.8, 101.5] + FLAT[:5]
        simulated, _, reason, exit_at = self._differential(
            closes, "runtime-gap-target", opens={4: 100.6})
        self.assertEqual(simulated["exit_reason"], "target")
        self.assertEqual(reason, "target")
        self.assertEqual(exit_at.isoformat(), simulated["exit_timestamp"])

    def test_a_legacy_trade_without_the_field_keeps_its_behaviour(self):
        bars = _bars(RISING + FLAT)
        engine, _ = self._open_runtime_trade(bars, "runtime-legacy")
        state.update_state(lambda current: {
            **current,
            "active_trades": {"SPY": {
                key: value for key, value
                in current["active_trades"]["SPY"].items()
                if key not in {"hold_deadline_ts", "max_hold_bars"}}}})
        for bar in bars[5:]:
            result = engine._monitor_positions(bar.end, [self._position(bar.close)])
            self.assertEqual(result["closed"], [])


class GappedEntrySizingTests(unittest.TestCase):
    """Sizing must charge the real fill-to-stop distance, not the nominal one."""

    def _row(self, opens=None):
        book = simulate_account(_bars(RISING + FLAT, opens), [], SPEC,
                                vehicle="equity", account_id="sizing",
                                risk_pct=.05)
        row = book["rows"][0]
        self.assertIs(row.get("no_trade"), False)
        return row

    def test_a_gap_against_the_stop_shrinks_the_position(self):
        flat, gapped = self._row(), self._row({4: 101.0})
        # 100k * .05% = $50 of risk. Ungapped risk per share is the nominal
        # .30 ATR stop; the gap widens it to 101.00 - 100.50 = .50.
        self.assertEqual(flat["quantity"], 166)
        self.assertEqual(gapped["quantity"], 100)
        self.assertLess(gapped["quantity"], flat["quantity"])

    def test_a_gap_toward_the_stop_is_a_smaller_risk_per_share(self):
        row = self._row({4: 100.6})
        # Real risk is .10/share, so $50 funds 500 shares; the 25%-of-cash
        # notional cap binds first at floor(25_000 / 100.60) = 248.
        self.assertEqual(row["quantity"], 248)
        self.assertGreater(row["quantity"], self._row()["quantity"])

    def test_an_entry_through_the_stop_is_not_booked(self):
        book = simulate_account(_bars(RISING + FLAT, {4: 100.3}), [], SPEC,
                                vehicle="equity", account_id="sizing",
                                risk_pct=.05)
        row = book["rows"][0]
        self.assertIs(row["no_trade"], True)
        self.assertEqual(row["reject_reason"],
                         "entry gapped through the protective stop")
        self.assertEqual(book["trades"], 0)


if __name__ == "__main__":
    unittest.main()
