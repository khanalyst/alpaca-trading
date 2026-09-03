"""Differential proof that research and runtime share one exit contract.

The bounded rule grammar declares `max_hold_bars`.  Research enforces it in
`research.factory_core._simulate_trade`; the trader enforces it in
`_monitor_positions`.  These tests drive both over the same deterministic
one-minute series and require the same exit bar, so the two implementations
cannot silently diverge again.
"""

from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from agent import state
from agent.alpaca_domain import Account, Order, OrderRequest, Position, Quote
from agent.config import validate_config
from agent.contracts.risk_geometry import quantize_equity_bracket
from agent.contracts.rule import (BAR_SECONDS, CANONICAL_EXIT_REASONS,
                                  RuleSpecError,
                                  MIN_STOP_DISTANCE_FRACTION,
                                  RULE_SCHEMA_V3, RULE_SCHEMA_V4,
                                  canonical_exit_reason,
                                  completed_bar_exit_transition,
                                  evaluate_rule_signal_trace, exit_deadline,
                                  generate_rule_signal, hold_deadline,
                                  initialize_exit_state, thesis_exit_deadline,
                                  rule_variant_id, validate_rule_spec)
from agent.engine import Engine
from agent.risk import RiskEngine
from agent.strategy import build_setup_plan
from research.costs import ReplayPolicy
from research.edge_discovery_core import null_control_account
from research.factory_core import _simulate_trade, simulate_account
from research.market_data import normalize_quote, normalize_underlying_bar

BASE = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
SPEC = validate_rule_spec({
    "family": "momentum_continuation", "lookback": 3, "slow_lookback": 8,
    "atr_period": 3, "threshold_bps": 1.0, "stop_atr": 1.0, "target_r": 2.0,
    "max_hold_bars": 3, "confirmation": "none",
})
V3_SPEC = validate_rule_spec({
    **SPEC, "schema": RULE_SCHEMA_V3, "breakeven_r": 0.5,
})
FIXED_V4_SPEC = validate_rule_spec({
    **SPEC, "schema": RULE_SCHEMA_V4,
})
SESSION_VWAP_SPEC = validate_rule_spec({
    **SPEC, "schema": RULE_SCHEMA_V4, "family": "mean_reversion",
    "lookback": 3, "zscore": 1.0, "target_mode": "session_vwap",
    "target_lookback": 2, "trailing_stop_r": None,
})
ROLLING_MEAN_SPEC = validate_rule_spec({
    **SESSION_VWAP_SPEC, "target_mode": "rolling_mean",
    "target_lookback": 2,
})
# Four rising bars produce the signal at index 3, so the simulated entry bar
# is index 4 and the bounded hold expires at the end of index 7.
RISING = [100.2, 100.4, 100.6, 100.8]
FLAT = [100.8] * 8
# These differential fixtures deliberately isolate bar-level exit geometry.
# Production replay defaults to strict quote-backed fills; the test opts into
# bar pricing explicitly instead of weakening that safe default.
BAR_ONLY_POLICY = ReplayPolicy(strict_market_data=False)
BAR_ONLY_SIZING_POLICY = ReplayPolicy(
    strict_market_data=False, risk_per_trade_pct=.05)


def _expected_protective_levels():
    """Derive the stop/target from the current executable stop floor."""
    anchor = RISING[-1]
    distance = anchor * MIN_STOP_DISTANCE_FRACTION
    return quantize_equity_bracket(
        anchor, anchor - distance, anchor + distance * SPEC["target_r"],
        "long")


def _payloads(closes, opens=None, ranges=None):
    """``opens`` overrides an index's open, i.e. gaps that bar away from the
    previous close.  Without it every bar opens where the last one closed,
    which is exactly the case that hides an anchor divergence.  ``ranges``
    widens an index's ``(high, low)`` beyond its open/close, i.e. a wick that
    trades through a resting leg and comes back."""
    rows = []
    opened = 100.0
    for index, close in enumerate(closes):
        opened = float((opens or {}).get(index, opened))
        high, low = max(opened, close) + .05, min(opened, close) - .05
        override = (ranges or {}).get(index)
        if override is not None:
            high, low = max(high, float(override[0])), min(low, float(override[1]))
        timestamp = BASE + timedelta(minutes=index)
        rows.append({
            "kind": "bar", "provider": "test", "feed": "sip", "symbol": "SPY",
            "timestamp": timestamp.isoformat(),
            # Mechanics-only fixture: the source exposes the opening print at
            # the boundary, so bar fallback is intentionally admissible.
            "as_of": timestamp.isoformat(),
            "observed_at": timestamp.isoformat(),
            "open": opened,
            "high": high, "low": low,
            "close": close, "volume": 1000,
        })
        opened = close
    return rows


def _bars(closes, opens=None, ranges=None):
    return [normalize_underlying_bar(row) for row in _payloads(closes, opens, ranges)]


def _quote(index, bid, ask):
    timestamp = BASE + timedelta(minutes=index)
    return normalize_quote({
        "kind": "quote", "provider": "test", "feed": "sip",
        "symbol": "SPY", "timestamp": timestamp.isoformat(),
        "as_of": timestamp.isoformat(), "observed_at": timestamp.isoformat(),
        "bid": bid, "ask": ask,
    })


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

    def test_deadline_cause_uses_runtime_precedence(self):
        entry = BASE.timestamp()
        tied = exit_deadline(
            entry, {"max_hold_bars": 3},
            force_flat_ts=entry + 4 * BAR_SECONDS)
        self.assertEqual(tied, {
            "timestamp": entry + 4 * BAR_SECONDS,
            "reason": "session_force_flat",
        })
        thesis = exit_deadline(entry, {
            "max_hold_bars": 390, "exit_before_minutes": 385,
        })
        self.assertEqual(thesis["reason"], "thesis_deadline")

    def test_exit_aliases_share_one_canonical_vocabulary(self):
        self.assertEqual(canonical_exit_reason("time"), "max_hold")
        self.assertEqual(canonical_exit_reason("max_hold"), "max_hold")
        self.assertEqual(canonical_exit_reason("before_close"),
                         "session_force_flat")
        self.assertEqual(canonical_exit_reason("force_flat"),
                         "session_force_flat")
        self.assertEqual(canonical_exit_reason("exit_before"),
                         "thesis_deadline")

    def test_unknown_operational_reason_cannot_escape_canonical_vocabulary(self):
        """Persisted canonical causes must stay within the cross-lane enum."""
        canonical = canonical_exit_reason("protection_fill")
        self.assertEqual(canonical, "unknown")
        self.assertIn(canonical, CANONICAL_EXIT_REASONS)

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


class CompletedBarExitTransitionTests(unittest.TestCase):
    def test_completed_close_arms_breakeven_for_the_next_bar_only(self):
        state_row = initialize_exit_state(
            "long", 100.0, 99.0, 103.0, breakeven_r=1.0)
        arm_bar = {"timestamp": BASE, "open": 100.0, "high": 101.2,
                   "low": 99.5, "close": 101.1}
        armed = completed_bar_exit_transition(state_row, arm_bar)
        self.assertIsNone(armed["exit"])
        self.assertTrue(armed["stop_changed"])
        self.assertEqual(armed["state"]["initial_stop_price"], 99.0)
        self.assertEqual(armed["state"]["active_stop_price"], 100.0)
        self.assertEqual(armed["state"]["breakeven_armed_epoch"],
                         (BASE + timedelta(minutes=1)).timestamp())

        next_bar = {"timestamp": BASE + timedelta(minutes=1),
                    "open": 100.5, "high": 102.0,
                    "low": 99.8, "close": 101.5}
        stopped = completed_bar_exit_transition(armed["state"], next_bar)
        self.assertEqual(stopped["exit"]["reason"], "stop")
        self.assertEqual(stopped["exit"]["price"], 100.0)

    def test_trailing_close_ratchet_is_monotone_and_next_bar_only(self):
        state_row = initialize_exit_state(
            "long", 100.0, 99.0, 110.0, trailing_stop_r=1.0,
            target_mode="fixed_r")
        first = completed_bar_exit_transition(state_row, {
            "timestamp": BASE, "open": 100.0, "high": 103.0,
            "low": 99.5, "close": 102.0})
        self.assertIsNone(first["exit"])
        self.assertTrue(first["stop_changed"])
        self.assertEqual(first["state"]["active_stop_price"], 101.0)
        # A pullback on the next completed bar cannot loosen the ratchet.
        second = completed_bar_exit_transition(first["state"], {
            "timestamp": BASE + timedelta(minutes=1), "open": 101.5,
            "high": 101.8, "low": 101.2, "close": 101.3})
        self.assertIsNone(second["exit"])
        self.assertEqual(second["state"]["active_stop_price"], 101.0)

    def test_thesis_deadline_is_distinct_and_clamps_hold(self):
        spec = validate_rule_spec({"schema": RULE_SCHEMA_V4,
                                   "family": "momentum_continuation",
                                   "exit_before_minutes": 10,
                                   "max_hold_bars": 390})
        entry = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc).timestamp()
        thesis = thesis_exit_deadline(entry, spec)
        self.assertIsNotNone(thesis)
        self.assertEqual(hold_deadline(entry, spec), thesis)

    def test_gap_precedes_intrabar_and_stop_wins_a_tie(self):
        state_row = initialize_exit_state("long", 100, 99, 103)
        gap = completed_bar_exit_transition(state_row, {
            "timestamp": BASE, "open": 98.5, "high": 104,
            "low": 98, "close": 102})
        self.assertEqual(gap["exit"]["reason"], "stop")
        self.assertEqual(gap["exit"]["price"], 98.5)
        self.assertTrue(gap["exit"]["gapped"])

        tie = completed_bar_exit_transition(
            initialize_exit_state("long", 100, 99, 103), {
                "timestamp": BASE, "open": 100, "high": 104,
                "low": 98, "close": 101})
        self.assertEqual(tie["exit"]["reason"], "stop")
        self.assertTrue(tie["exit"]["tie_broken"])

    def test_entry_exactly_at_the_stop_resolves_at_the_fill_anchor(self):
        transition = completed_bar_exit_transition(
            initialize_exit_state("long", 99.0, 99.0, 103.0), {
                "timestamp": BASE, "open": 99.0, "high": 100.0,
                "low": 98.5, "close": 99.5})
        self.assertEqual(transition["exit"]["reason"], "stop")
        self.assertEqual(transition["exit"]["price"], 99.0)
        self.assertTrue(transition["exit"]["entry_gap"])

    def test_short_positions_use_the_same_next_bar_arm_contract(self):
        state_row = initialize_exit_state(
            "short", 100.0, 101.0, 97.0, breakeven_r=1.0)
        armed = completed_bar_exit_transition(state_row, {
            "timestamp": BASE, "open": 100.0, "high": 100.5,
            "low": 98.8, "close": 98.9})
        self.assertIsNone(armed["exit"])
        self.assertEqual(armed["state"]["active_stop_price"], 100.0)
        stopped = completed_bar_exit_transition(armed["state"], {
            "timestamp": BASE + timedelta(minutes=1), "open": 99.5,
            "high": 100.2, "low": 98.0, "close": 99.0})
        self.assertEqual(stopped["exit"]["reason"], "stop")
        self.assertEqual(stopped["exit"]["price"], 100.0)


class V3OptionRejectionTests(unittest.TestCase):
    def test_runtime_risk_rejects_v3_options_before_contract_selection(self):
        risk = RiskEngine({"strategy": {"execution_profile": "options"}})
        decision = {
            "symbol": "SPY", "direction": "long", "confidence": 1.0,
            "entry_price": 100.0, "stop_price": 99.0, "target_price": 102.0,
            "execution_profile": "options", "rule_schema": RULE_SCHEMA_V3,
            "breakeven_r": 0.5,
        }
        plan, reason = risk.vet_open(
            decision, 100_000, [], {"SPY": {"price": 100.0}}, {}, 0,
            now=BASE.timestamp())
        self.assertIsNone(plan)
        self.assertEqual(reason,
                         "rule-strategy.v3 is not executable for options")

    def test_research_marks_v3_options_unexecutable_not_unobserved(self):
        bars = _bars(RISING + FLAT)
        result = _simulate_trade(
            bars, V3_SPEC, [], "option",
            policy=BAR_ONLY_POLICY)
        self.assertIsNotNone(result)
        self.assertEqual(
            result["unpriced_reason"],
            "rule-strategy.v3 is not executable for options")
        null = null_control_account(
            bars, [], V3_SPEC, vehicle="option", account_id="unsupported-v3",
            reference_rows=[{
                "symbol": "SPY",
                "session_date": bars[0].session_date.isoformat(),
            }], policy=BAR_ONLY_POLICY)
        self.assertEqual(null["trades"], 0)
        self.assertEqual(
            null["rows"][0]["reject_reason"],
            "rule-strategy.v3 is not executable for options")


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
                    "stale": False, "quote_stale": False,
                    "force_flat_at": (BASE + timedelta(hours=6)).isoformat(),
                    "force_flat_ts": (BASE + timedelta(hours=6)).timestamp()}
        plan, why = build_setup_plan(signal, snapshot, self.cfg)
        self.assertIsNone(why)
        self.assertEqual(plan["max_hold_bars"], 3)
        self.assertEqual(plan["hold_deadline_ts"],
                         bars[7].end.timestamp())

    def test_rule_plan_requires_an_exact_calendar_boundary(self):
        bars = _bars(RISING + FLAT)
        signal = self._signal(bars[:4])
        snapshot = {"price": signal["entry_price"],
                    "signal_ts": signal["signal_ts"],
                    "session": signal["session"], "spread_bps": 1.0,
                    "stale": False, "quote_stale": False}
        plan, why = build_setup_plan(signal, snapshot, self.cfg)
        self.assertIsNone(plan)
        self.assertEqual(why,
                         "exact session force-flat timestamp is unavailable")

        fallback_cfg = validate_config({
            "session": {"require_exact_calendar": False},
            "strategy": {"id": "rule", "version": "v1",
                         "variant_id": self.variant_id,
                         "rule_spec": SPEC},
        })
        plan, why = build_setup_plan(signal, snapshot, fallback_cfg)
        self.assertIsNone(why)
        self.assertIsNotNone(plan["force_flat_ts"])

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
            # These fixtures exercise exit geometry after a position is
            # opened.  Keep the historical generous stressed-cost allowance
            # here so the global production default (0.30) cannot veto the
            # mechanics under test before monitoring begins.
            "risk": {"max_stressed_cost_to_risk_ratio": 1.0},
            "execution": {"client_order_id_prefix": "exit"},
            "llm": {"enabled": False},
            "research": {"enabled": True, "require_validated_variant": False,
                         "db_path": str(Path(self.tmp.name) / "edge.sqlite3")},
        }, light=True, provider=self.provider)
        state.ensure_ready()
        self.engine.market.should_force_flat = lambda now=None: False
        return self.engine

    def _open_runtime_trade(self, bars, name, *, spec=SPEC, filled_at=None):
        """Persist the same signal through plan, risk, and fill activation."""
        engine = self._engine(name)
        cfg = validate_config({"strategy": {
            "id": "rule", "version": "v1", "variant_id": rule_variant_id(spec),
            "rule_spec": spec}})
        signal = generate_rule_signal(
            "SPY", _payloads([bar.close for bar in bars[:4]]), config=cfg,
            now=datetime.now(timezone.utc))
        snapshot = {"price": signal["entry_price"], "signal_ts": signal["signal_ts"],
                    "session": signal["session"], "spread_bps": 1.0,
                    "stale": False, "quote_stale": False,
                    "force_flat_at": (BASE + timedelta(hours=6)).isoformat(),
                    "force_flat_ts": (BASE + timedelta(hours=6)).timestamp()}
        plan, why = build_setup_plan(signal, snapshot, cfg)
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
            filled_avg_price=Decimal(str(plan["entry_price"])),
            updated_at=(filled_at if filled_at is not None
                        else bars[4].timestamp))
        self.provider.positions_live = [self._position(plan["entry_price"])]
        engine.reconcile()
        return engine, risk_plan

    def _position(self, price):
        return Position("SPY", Decimal("10"), "long",
                        current_price=Decimal(str(price)),
                        market_value=Decimal(str(float(price) * 10)))

    def test_risk_order_keeps_authored_reference_but_sizes_at_quote_gap(self):
        engine = self._engine("runtime-quote-gap-sizing")
        signal = {"symbol": "SPY", "direction": "long", "entry_price": 100.8,
                  "stop_price": 100.5, "target_price": 101.4,
                  "confidence": 1.0, "setup_id": "quote-gap-sizing"}
        now = BASE + timedelta(minutes=4)
        row = {"symbol": "SPY", "quote": {
            "timestamp": now.isoformat(), "bid": 100.9, "ask": 101.0,
        }}
        result = engine._risk_order(
            "SPY", signal, row, self.provider.account(), [], now)
        self.assertIsNotNone(result)
        request, plan = result
        self.assertAlmostEqual(plan["authored_entry_reference"], 100.8)
        self.assertAlmostEqual(plan["executable_entry_reference"], 101.0)
        self.assertAlmostEqual(plan["entry_price"], 101.0)
        self.assertAlmostEqual(plan["stop_distance"], .5)
        self.assertEqual(request.stop_loss, Decimal("100.50"))
        self.assertEqual(request.take_profit, Decimal("101.40"))

        replay = _simulate_trade(
            _bars(RISING + FLAT, opens={4: 101.0}), SPEC, [], "equity",
            policy=BAR_ONLY_POLICY)
        self.assertIsNone(replay.get("unpriced_reason"))
        self.assertAlmostEqual(replay["plan_entry"], 100.8)
        self.assertAlmostEqual(replay["executable_entry_reference"], 101.0)
        self.assertAlmostEqual(replay["stop_distance"], .51)
        self.assertAlmostEqual(replay["risk_per_unit"], .51)

    def test_risk_order_refuses_missing_quote_instead_of_authored_fallback(self):
        engine = self._engine("runtime-missing-quote")
        events = []
        engine._event = lambda kind, payload: events.append((kind, payload))
        signal = {"symbol": "SPY", "direction": "long", "entry_price": 100.8,
                  "stop_price": 100.5, "target_price": 101.4,
                  "confidence": 1.0, "setup_id": "missing-quote"}
        self.assertIsNone(engine._risk_order(
            "SPY", signal, {"symbol": "SPY", "quote": {}},
            self.provider.account(), [], BASE + timedelta(minutes=4)))
        self.assertEqual(events[-1], ("execution_reject", {
            "symbol": "SPY", "reason": "executable entry quote is unavailable"}))

    def test_risk_order_refuses_quote_already_through_protective_stop(self):
        engine = self._engine("runtime-quote-through-stop")
        events = []
        engine._event = lambda kind, payload: events.append((kind, payload))
        signal = {"symbol": "SPY", "direction": "long", "entry_price": 100.8,
                  "stop_price": 100.5, "target_price": 101.4,
                  "confidence": 1.0, "setup_id": "quote-through-stop"}
        now = BASE + timedelta(minutes=4)
        self.assertIsNone(engine._risk_order(
            "SPY", signal, {"symbol": "SPY", "quote": {
                "timestamp": now.isoformat(), "bid": 100.2, "ask": 100.3,
            }}, self.provider.account(), [], now))
        self.assertEqual(events[-1][0], "execution_reject")
        self.assertIn("straddle", events[-1][1]["reason"])

    def _drive(self, engine, bars, *, start_index):
        """Return the first (reason, exit timestamp) the monitor produces."""
        self.last_runtime_exit = None
        for bar in bars[start_index:]:
            result = engine._monitor_positions(
                bar.end, [self._position(bar.close)],
                market_rows={"SPY": {"bars": [bar]}})
            if result["closed"]:
                self.last_runtime_exit = dict(result["closed"][0])
                persisted = state.load_state().get("active_trades", {}).get(
                    result["closed"][0]["symbol"], {})
                self.last_runtime_exit["exit_reason"] = persisted.get(
                    "closing_exit_reason")
                return result["closed"][0]["reason"], bar.end
        return None, None

    def _differential(self, closes, name, *, force_flat_from=None, opens=None,
                      ranges=None, start_index=5, spec=SPEC):
        bars = _bars(closes, opens, ranges)
        replay_policy = BAR_ONLY_POLICY
        if force_flat_from is not None:
            replay_policy = replace(
                BAR_ONLY_POLICY,
                force_flat_time=bars[force_flat_from].end.astimezone(
                    ZoneInfo("America/New_York")).time())
        simulated = _simulate_trade(
            bars, spec, [], "equity", policy=replay_policy)
        self.assertIsNotNone(simulated)
        engine, plan = self._open_runtime_trade(bars, name, spec=spec)
        if force_flat_from is not None:
            engine.market.should_force_flat = (
                lambda now=None: now is not None and
                now >= bars[force_flat_from].end)
        self.provider.positions_live = [self._position(bars[-1].close)]
        # The simulator evaluates protective exits from the entry bar onwards;
        # a caller testing the entry bar itself drives the runtime from it too.
        reason, exit_at = self._drive(engine, bars, start_index=start_index)
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
        self.assertEqual(simulated["canonical_exit_reason"],
                         "session_force_flat")
        self.assertEqual(self.last_runtime_exit["exit_reason"],
                         "session_force_flat")
        self.assertEqual(exit_at.isoformat(), simulated["exit_timestamp"])

    def test_delayed_poll_stops_replay_at_the_max_hold_deadline(self):
        bars = _bars(RISING + [100.8, 100.8, 100.8, 100.8, 101.5])
        engine, _ = self._open_runtime_trade(
            bars, "runtime-delayed-hold", spec=FIXED_V4_SPEC)
        result = engine._monitor_positions(
            bars[8].end, [self._position(bars[8].close)],
            market_rows={"SPY": {"bars": bars[4:9]}})
        self.assertEqual([row["reason"] for row in result["closed"]],
                         ["max_hold"])
        trade = state.load_state()["active_trades"]["SPY"]
        self.assertEqual(trade["canonical_exit_reason"], "max_hold")
        self.assertEqual(trade["last_completed_bar_epoch"],
                         bars[7].end.timestamp())

    def test_delayed_poll_keeps_protective_exit_before_force_flat(self):
        bars = _bars(RISING + [100.8, 101.5, 100.8, 100.8])
        engine, _ = self._open_runtime_trade(
            bars, "runtime-before-flat", spec=FIXED_V4_SPEC)
        deadline = bars[6].end
        state.update_state(lambda current: {
            **current,
            "active_trades": {"SPY": {
                **current["active_trades"]["SPY"],
                "force_flat_at": deadline.isoformat(),
                "force_flat_ts": deadline.timestamp(),
            }},
        })
        engine.market.should_force_flat = lambda now=None: True
        result = engine._monitor_positions(
            bars[7].end, [self._position(bars[7].close)],
            market_rows={"SPY": {"bars": bars[4:8]}})
        self.assertEqual([row["reason"] for row in result["closed"]],
                         ["target"])
        self.assertEqual(
            state.load_state()["active_trades"]["SPY"]["canonical_exit_reason"],
            "target")

    def test_delayed_poll_rejects_a_protective_bar_after_force_flat(self):
        bars = _bars(RISING + [100.8, 100.8, 100.8, 101.5])
        engine, _ = self._open_runtime_trade(
            bars, "runtime-after-flat", spec=FIXED_V4_SPEC)
        deadline = bars[6].end
        state.update_state(lambda current: {
            **current,
            "active_trades": {"SPY": {
                **current["active_trades"]["SPY"],
                "force_flat_at": deadline.isoformat(),
                "force_flat_ts": deadline.timestamp(),
            }},
        })
        result = engine._monitor_positions(
            bars[7].end, [self._position(bars[7].close)],
            market_rows={"SPY": {"bars": bars[4:8]}})
        self.assertEqual([row["reason"] for row in result["closed"]],
                         ["before_close"])
        self.assertEqual(
            state.load_state()["active_trades"]["SPY"]["canonical_exit_reason"],
            "session_force_flat")

    def test_runtime_deadline_tie_uses_session_then_thesis_then_hold(self):
        bars = _bars(RISING + [100.8, 100.8, 100.8, 100.8])
        engine, _ = self._open_runtime_trade(
            bars, "runtime-deadline-tie", spec=FIXED_V4_SPEC)
        deadline = bars[7].end
        state.update_state(lambda current: {
            **current,
            "active_trades": {"SPY": {
                **current["active_trades"]["SPY"],
                "force_flat_at": deadline.isoformat(),
                "force_flat_ts": deadline.timestamp(),
                "exit_before_ts": deadline.timestamp(),
                "hold_deadline_ts": deadline.timestamp(),
            }},
        })
        result = engine._monitor_positions(
            deadline, [self._position(bars[7].close)],
            market_rows={"SPY": {"bars": bars[4:8]}})
        self.assertEqual([row["reason"] for row in result["closed"]],
                         ["before_close"])
        self.assertEqual(
            state.load_state()["active_trades"]["SPY"]["canonical_exit_reason"],
            "session_force_flat")

    def test_delayed_poll_rejects_a_protective_bar_after_thesis_deadline(self):
        spec = validate_rule_spec({
            **SPEC, "schema": RULE_SCHEMA_V4, "max_hold_bars": 10,
            "exit_before_minutes": 385,
        })
        bars = _bars(RISING + [100.8, 101.5, 101.5])
        engine, _ = self._open_runtime_trade(
            bars, "runtime-after-thesis", spec=spec)
        result = engine._monitor_positions(
            bars[6].end, [self._position(bars[6].close)],
            market_rows={"SPY": {"bars": bars[4:7]}})
        self.assertEqual([row["reason"] for row in result["closed"]],
                         ["exit_before"])
        self.assertEqual(
            state.load_state()["active_trades"]["SPY"]["canonical_exit_reason"],
            "thesis_deadline")

    def test_runtime_hold_is_reanchored_to_the_actual_fill(self):
        bars = _bars(RISING + FLAT + [100.8])
        engine, plan = self._open_runtime_trade(
            bars, "runtime-fill-anchor", spec=FIXED_V4_SPEC,
            filled_at=bars[5].timestamp)
        trade = state.load_state()["active_trades"]["SPY"]
        self.assertEqual(plan["hold_deadline_ts"], bars[7].end.timestamp())
        self.assertEqual(trade["entry_filled_at_ts"],
                         bars[5].timestamp.timestamp())
        self.assertEqual(trade["exit_entry_bar_epoch"],
                         bars[5].timestamp.timestamp())
        self.assertEqual(trade["hold_deadline_ts"], bars[8].end.timestamp())

        first = engine._monitor_positions(
            bars[7].end, [self._position(bars[7].close)],
            market_rows={"SPY": {"bars": bars[5:8]}})
        self.assertEqual(first["closed"], [])
        second = engine._monitor_positions(
            bars[8].end, [self._position(bars[8].close)],
            market_rows={"SPY": {"bars": [bars[8]]}})
        self.assertEqual([row["reason"] for row in second["closed"]],
                         ["max_hold"])

    def test_minute_gap_exits_as_data_discontinuity(self):
        bars = _bars(RISING + [100.8, 100.8, 100.8, 100.8])
        engine, _ = self._open_runtime_trade(
            bars, "runtime-gap", spec=FIXED_V4_SPEC)
        result = engine._monitor_positions(
            bars[6].end, [self._position(bars[6].close)],
            market_rows={"SPY": {"bars": [bars[4], bars[6]]}})
        self.assertEqual([row["reason"] for row in result["closed"]],
                         ["data_discontinuity"])
        self.assertEqual(
            state.load_state()["active_trades"]["SPY"]["canonical_exit_reason"],
            "data_discontinuity")

    def test_session_vwap_target_is_frozen_across_replay_and_runtime(self):
        closes = [100.0, 100.0, 100.0, 95.0, 95.5, 100.0, 100.0]
        expected = evaluate_rule_signal_trace(
            _bars(closes[:4]), SESSION_VWAP_SPEC)["signal"]["target_reference"]
        simulated, plan, reason, exit_at = self._differential(
            closes, "runtime-v4-session-vwap", start_index=4,
            spec=SESSION_VWAP_SPEC)
        self.assertEqual(simulated["exit_reason"], "target")
        self.assertEqual(reason, "target")
        self.assertEqual(self.last_runtime_exit["exit_reason"], "target")
        self.assertEqual(exit_at.isoformat(), simulated["exit_timestamp"])
        self.assertAlmostEqual(simulated["target_reference"], expected, places=6)
        self.assertAlmostEqual(plan["target_reference"], expected, places=6)
        self.assertAlmostEqual(simulated["target_price"],
                               plan["target_price"], places=9)
        self.assertLessEqual(simulated["target_price"], expected)

    def test_v4_target_reference_survives_activation_and_reconcile(self):
        bars = _bars([100.0, 100.0, 100.0, 95.0, 95.5, 95.5, 95.5])
        engine, plan = self._open_runtime_trade(
            bars, "runtime-v4-target-state", spec=SESSION_VWAP_SPEC)
        first = state.load_state()["active_trades"]["SPY"]
        self.assertEqual(first["target_reference"], plan["target_reference"])
        self.assertEqual(
            state.load_state()["protection"]["SPY"]["target_reference"],
            plan["target_reference"])
        engine.reconcile()
        recovered = state.load_state()["active_trades"]["SPY"]
        self.assertEqual(recovered["target_reference"],
                         plan["target_reference"])

    def test_rolling_mean_target_is_frozen_across_replay_and_runtime(self):
        closes = [100.0, 100.0, 100.0, 95.0, 95.5, 99.0, 99.0]
        simulated, plan, reason, exit_at = self._differential(
            closes, "runtime-v4-rolling-mean", start_index=4,
            spec=ROLLING_MEAN_SPEC)
        self.assertEqual(simulated["exit_reason"], "target")
        self.assertEqual(reason, "target")
        self.assertEqual(exit_at.isoformat(), simulated["exit_timestamp"])
        self.assertAlmostEqual(simulated["target_reference"], 97.5, places=6)
        self.assertAlmostEqual(simulated["target_price"], 97.5, places=6)
        self.assertAlmostEqual(plan["target_reference"], 97.5, places=6)
        self.assertAlmostEqual(plan["target_price"], 97.5, places=6)

    def test_non_fixed_target_trailing_ratchet_still_honors_deadline(self):
        spec = validate_rule_spec({
            **ROLLING_MEAN_SPEC, "max_hold_bars": 3,
            "trailing_stop_r": 1.0,
        })
        closes = [100.0, 100.0, 100.0, 95.0,
                  95.5, 95.6, 95.6, 95.6, 95.6]
        simulated, plan, reason, exit_at = self._differential(
            closes, "runtime-v4-target-trailing-deadline", start_index=4,
            spec=spec)
        self.assertEqual(simulated["canonical_exit_reason"], "max_hold")
        self.assertEqual(reason, "max_hold")
        self.assertEqual(self.last_runtime_exit["exit_reason"], "max_hold")
        self.assertEqual(exit_at.isoformat(), simulated["exit_timestamp"])
        self.assertAlmostEqual(simulated["target_price"],
                               plan["target_price"], places=9)
        self.assertGreater(simulated["active_stop_price"],
                           simulated["initial_stop_price"])

    def test_non_fixed_target_rejections_are_explicit(self):
        underpowered = validate_rule_spec({
            **ROLLING_MEAN_SPEC, "target_lookback": 20,
        })
        trace = evaluate_rule_signal_trace(
            _bars([100.0, 100.0, 100.0, 95.0]), underpowered)
        self.assertIsNone(trace["signal"])
        self.assertEqual(trace["stages"][-1]["reason"],
                         "insufficient_prefix")

        wrong_side = validate_rule_spec({
            **SPEC, "schema": RULE_SCHEMA_V4,
            "target_mode": "session_vwap", "target_lookback": 2,
        })
        trace = evaluate_rule_signal_trace(_bars(RISING), wrong_side)
        self.assertIsNone(trace["signal"])
        self.assertEqual(trace["stages"][-1]["reason"],
                         "target_reference_unavailable_or_wrong_side")

    def test_strict_force_flat_requires_quote_and_prices_from_it(self):
        bars = _bars(RISING + FLAT[:4])
        policy = ReplayPolicy(strict_market_data=True,
                              force_flat_time=time(9, 36))
        entry_quote = _quote(4, 100.79, 100.80)
        refused = _simulate_trade(
            bars, SPEC, [], "equity", quotes={"SPY": [entry_quote]},
            policy=policy)
        self.assertEqual(refused["unpriced_reason"],
                         "no fresh equity quote at exit")

        exit_quote = _quote(6, 100.70, 100.71)
        account = simulate_account(
            bars, [], SPEC, vehicle="equity", account_id="strict-force-flat",
            quotes=[entry_quote, exit_quote], policy=policy)
        priced = account["rows"][0]
        self.assertEqual(priced["canonical_exit_reason"],
                         "session_force_flat")
        self.assertEqual(priced["exit_fill_source"], "quote")
        self.assertAlmostEqual(priced["exit_reference"], 100.70)
        expected_gross = ((priced["exit_price"] - priced["entry_price"]) *
                          priced["quantity"])
        self.assertAlmostEqual(priced["gross_pnl"], expected_gross, places=9)

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
        expected_stop, expected_target, expected_distance = _expected_protective_levels()
        self.assertEqual(simulated["underlying_entry"], 101.0)
        self.assertAlmostEqual(simulated["stop_price"], expected_stop, places=9)
        self.assertAlmostEqual(simulated["target_price"], expected_target, places=9)
        self.assertEqual(simulated["exit_reason"], "time")
        self.assertEqual(reason, "max_hold")
        self.assertEqual(exit_at.isoformat(), simulated["exit_timestamp"])
        # Sizing uses the executable quote-to-stop geometry.  The protective
        # levels remain authored, but the larger gap consumes more risk/unit.
        self.assertAlmostEqual(simulated["stop_distance"], 101.0 - expected_stop,
                               places=9)
        self.assertAlmostEqual(simulated["risk_per_unit"], 101.0 - expected_stop,
                               places=9)
        self.assertAlmostEqual(simulated["realized_risk_per_unit"],
                               101.0 - expected_stop, places=9)

    def test_a_gap_down_entry_does_not_move_the_protective_levels(self):
        simulated, plan, reason, exit_at = self._differential(
            RISING + FLAT, "runtime-gap-down", opens={4: 100.6})
        expected_stop, expected_target, expected_distance = _expected_protective_levels()
        self.assertEqual(simulated["underlying_entry"], 100.6)
        self.assertAlmostEqual(simulated["stop_price"], expected_stop, places=9)
        self.assertAlmostEqual(simulated["target_price"], expected_target, places=9)
        self.assertEqual(simulated["exit_reason"], "time")
        self.assertEqual(reason, "max_hold")
        self.assertEqual(exit_at.isoformat(), simulated["exit_timestamp"])
        self.assertAlmostEqual(simulated["stop_distance"], 100.6 - expected_stop,
                               places=9)
        self.assertAlmostEqual(simulated["risk_per_unit"], 100.6 - expected_stop,
                               places=9)
        self.assertAlmostEqual(simulated["realized_risk_per_unit"],
                               100.6 - expected_stop, places=9)

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

    def test_the_entry_bar_own_range_can_stop_the_trade_in_both_engines(self):
        # Entry is bar 4's open at 100.80, above the 100.50 stop, so this is
        # not a gap-through entry.  The rest of that same bar trades down to
        # 100.40: the broker's stop leg is live from the fill and triggers.
        closes = RISING + [100.45] + FLAT[:6]
        simulated, _, reason, exit_at = self._differential(
            closes, "runtime-entry-bar-stop", start_index=4)
        self.assertEqual(simulated["exit_reason"], "stop")
        self.assertIs(simulated["entry_gap_fill"], False)
        self.assertEqual(simulated["exit_reference"], simulated["stop_price"])
        self.assertEqual(reason, "stop")
        self.assertEqual(exit_at.isoformat(), simulated["exit_timestamp"])
        self.assertEqual(exit_at, _bars(closes)[4].end)

    def test_the_entry_bar_own_range_can_target_the_trade_in_both_engines(self):
        closes = RISING + [101.5] + FLAT[:6]
        simulated, _, reason, exit_at = self._differential(
            closes, "runtime-entry-bar-target", start_index=4)
        self.assertEqual(simulated["exit_reason"], "target")
        self.assertEqual(simulated["exit_reference"], simulated["target_price"])
        self.assertEqual(reason, "target")
        self.assertEqual(exit_at.isoformat(), simulated["exit_timestamp"])
        self.assertEqual(exit_at, _bars(closes)[4].end)

    def test_an_entry_bar_wick_alone_triggers_the_resting_leg(self):
        # The close comes back inside the bracket, so only the wick touches.
        # A resting broker leg does not wait for the close, so research must
        # not either; the local poller cannot observe this and is not driven.
        bars = _bars(RISING + FLAT, ranges={4: (100.85, 100.4)})
        simulated = _simulate_trade(
            bars, SPEC, [], "equity", policy=BAR_ONLY_POLICY)
        expected_stop, expected_target, _ = _expected_protective_levels()
        self.assertEqual(simulated["exit_reason"], "stop")
        self.assertEqual(simulated["exit_timestamp"], bars[4].end.isoformat())
        self.assertAlmostEqual(simulated["exit_reference"], expected_stop, places=9)
        bars = _bars(RISING + FLAT, ranges={4: (101.45, 100.75)})
        simulated = _simulate_trade(
            bars, SPEC, [], "equity", policy=BAR_ONLY_POLICY)
        self.assertEqual(simulated["exit_reason"], "target")
        self.assertEqual(simulated["exit_timestamp"], bars[4].end.isoformat())
        self.assertAlmostEqual(simulated["exit_reference"], expected_target, places=9)

    def test_the_entry_bar_stop_wins_a_two_sided_tie(self):
        # Both levels inside one bar's range: the intrabar path is unknowable,
        # so the established rule resolves it against the strategy.
        bars = _bars(RISING + FLAT, ranges={4: (101.45, 100.4)})
        simulated = _simulate_trade(
            bars, SPEC, [], "equity", policy=BAR_ONLY_POLICY)
        self.assertEqual(simulated["exit_reason"], "stop")
        self.assertIs(simulated["tie_broken"], True)

    def test_a_gapped_entry_still_fills_at_the_entry_not_the_entry_bar_low(self):
        # The open is already through the stop, so the gap branch owns this
        # trade: the fill is the entry price, never the better level the
        # widened range would otherwise offer.
        bars = _bars(RISING + FLAT, opens={4: 100.3}, ranges={4: (100.9, 100.0)})
        simulated = _simulate_trade(
            bars, SPEC, [], "equity", policy=BAR_ONLY_POLICY)
        self.assertEqual(simulated["unpriced_reason"],
                         "broker_tick_geometry_invalid")
        self.assertEqual(simulated["reject_stage"], "risk_geometry")

    def test_v3_replay_and_runtime_arm_then_stop_on_the_same_completed_bars(self):
        closes = RISING + [101.0, 100.9] + FLAT[:5]
        bars = _bars(closes, ranges={5: (101.05, 100.7)})
        simulated, plan, reason, exit_at = self._differential(
            closes, "runtime-v3-breakeven", ranges={5: (101.05, 100.7)},
            start_index=4, spec=V3_SPEC)
        self.assertEqual(simulated["exit_reason"], "stop")
        self.assertEqual(reason, "stop")
        self.assertEqual(exit_at.isoformat(), simulated["exit_timestamp"])
        self.assertEqual(exit_at, bars[5].end)
        self.assertAlmostEqual(simulated["initial_stop_price"],
                               plan["stop_price"], places=9)
        self.assertAlmostEqual(simulated["active_stop_price"],
                               plan["entry_price"], places=9)
        self.assertEqual(simulated["breakeven_armed_epoch"],
                         bars[4].end.timestamp())
        trade = state.load_state()["active_trades"]["SPY"]
        self.assertAlmostEqual(trade["initial_stop_price"],
                               plan["stop_price"], places=9)
        self.assertAlmostEqual(trade["active_stop_price"],
                               plan["entry_price"], places=9)
        self.assertEqual(trade["breakeven_armed_epoch"],
                         bars[4].end.timestamp())

    def test_v3_runtime_filters_the_signal_bar_before_the_expected_entry_bar(self):
        bars = _bars(RISING + [101.0] + FLAT[:6])
        engine, _plan = self._open_runtime_trade(
            bars, "runtime-v3-entry-filter", spec=V3_SPEC)
        trade = state.load_state()["active_trades"]["SPY"]
        self.assertEqual(trade["exit_entry_bar_epoch"],
                         bars[4].timestamp.timestamp())

        result = engine._monitor_positions(
            bars[3].end, [self._position(bars[3].close)],
            market_rows={"SPY": {"bars": [bars[3]]}})

        self.assertEqual(result["closed"], [])
        trade = state.load_state()["active_trades"]["SPY"]
        self.assertIsNone(trade["last_completed_bar_epoch"])
        self.assertIs(trade["entry_bar_pending"], True)

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
    """Size like the runtime sizes; account for the risk the fill committed."""

    # 100k at .05% is a $50 budget, small enough that the risk term rather than
    # the 25%-of-cash notional cap decides the share count on both sides.
    def _row(self, opens=None):
        book = simulate_account(_bars(RISING + FLAT, opens), [], SPEC,
                                vehicle="equity", account_id="sizing",
                                risk_pct=.05, policy=BAR_ONLY_SIZING_POLICY)
        row = book["rows"][0]
        self.assertIs(row["no_trade"], False)
        return row

    def _runtime_shares(self, entry, distance):
        risk = RiskEngine({"risk": {"max_position_notional_pct": 25}})
        return risk.size_shares(equity=100_000, entry_price=entry,
                                stop_distance=distance, risk_usd=50.0)["shares"]

    def test_research_and_runtime_agree_on_the_share_count(self):
        for opens in (None, {4: 101.0}, {4: 100.6}):
            with self.subTest(opens=opens):
                row = self._row(opens)
                # Both lanes size from the executable entry/stop geometry.
                self.assertEqual(row["quantity"], self._runtime_shares(
                    row["entry_reference"],
                    row["stop_distance"]))

    def test_a_gap_against_the_stop_is_sized_conservatively(self):
        flat, gapped = self._row(), self._row({4: 101.0})
        self.assertLess(gapped["quantity"], flat["quantity"])
        self.assertAlmostEqual(flat["risk_usd"],
                               flat["quantity"] * flat["stop_distance"], places=6)
        # The gap consumes more risk/unit, but never overspends the budget.
        self.assertAlmostEqual(gapped["risk_usd"],
                               gapped["quantity"] * gapped["realized_risk_per_unit"],
                               places=6)
        self.assertLessEqual(gapped["risk_usd"], gapped["risk_budget"])
        self.assertLess(flat["risk_usd"], flat["risk_budget"] + 1e-9)

    def test_a_gap_toward_the_stop_uses_the_tighter_executable_geometry(self):
        row = self._row({4: 100.6})
        self.assertAlmostEqual(row["risk_usd"],
                               row["quantity"] * row["realized_risk_per_unit"],
                               places=6)
        self.assertAlmostEqual(row["risk_per_unit"], row["stop_distance"], places=9)
        self.assertLessEqual(row["risk_usd"], row["risk_budget"])

    def test_an_entry_through_the_stop_is_refused(self):
        book = simulate_account(_bars(RISING + FLAT, {4: 100.3}), [], SPEC,
                                vehicle="equity", account_id="sizing",
                                risk_pct=.05, policy=BAR_ONLY_SIZING_POLICY)
        row = book["rows"][0]
        # The broker cannot accept a long bracket whose stop is already above
        # the executable entry.  Replay refuses the same geometry instead of
        # inventing an immediate gap-through-stop fill.
        self.assertIs(row["no_trade"], True)
        self.assertEqual(row["reject_reason"], "broker_tick_geometry_invalid")
        self.assertEqual(row["reject_stage"], "risk_geometry")
        self.assertEqual(book["trades"], 0)


if __name__ == "__main__":
    unittest.main()
