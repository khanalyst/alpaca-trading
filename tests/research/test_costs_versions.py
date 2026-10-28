"""Cost-model versions and the cost-review fixes E1-01 to E1-11.

``docs/review-2026-10-15/reviews/handoff-E1_costs.md`` found the research cost
model badly miscalibrated.  The fix keeps the original flat model available
as ``static_v1`` (still the behaviour of ``CostModel``) and adds the
per-symbol, order-type-aware ``per_symbol_v2``.  The first class here proves
that ``static_v1`` and the legacy schedule and calibration paths reproduce the
original code exactly: the fingerprints were captured by running the same
payloads against the code before these changes (commit 352fb70).  Every
other test fails on that original code.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

from agent.config import load_config
from agent.contracts.rule import validate_rule_spec
from research.costs import (COST_MODEL_PER_SYMBOL_V2, COST_MODEL_STATIC_V1,
                            COST_STRESS_RUNG_BASIS,
                            DEFAULT_RESEARCH_COST_MODEL_VERSION,
                            FINRA_TAF_CAP, MARKET, MARKETABLE_LIMIT,
                            REFERENCE_PRICES, REFERENCE_SPREADS_BPS,
                            RESTING_LIMIT, STOP, CostError, CostModel,
                            PerSymbolCostModel, ReplayPolicy,
                            check_stressed_cost_plan,
                            recompute_row_cost_economics,
                            replay_policy_for_session, risk_unit_report,
                            row_cost_model_binding, static_cost_config,
                            stress_coherence, stress_relative_to_expected,
                            stressed_cost_usd)
from research.cost_rerun import run_cost_calibration
from research.factory_core import simulate_account
from research.market_data import normalize_quote, normalize_underlying_bar
from research.quote_costs import (QuoteCostError, cost_model_from_schedule,
                                  measure_quote_costs, measured_cost_resolver)
from research.stressed_cost_calibration import (
    calibrate_stressed_cost, verify_stress_calibration_artifact)
from studies.liquid_reversion import costs as study_costs
from tests.research.test_calibration_publication import _quote_rows
from tests.research.test_stressed_cost_calibration import _schedule


ROOT = Path(__file__).resolve().parents[2]
NEW_YORK = ZoneInfo("America/New_York")
BASE = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
SPEC = {
    "family": "momentum_continuation", "lookback": 3, "slow_lookback": 8,
    "atr_period": 3, "threshold_bps": 1.0, "stop_atr": 1.0, "target_r": 2.0,
    "max_hold_bars": 3, "confirmation": "none",
}
RISING = [100.2, 100.4, 100.6, 100.8]
TIME_EXIT = RISING + [100.8] * 8
STOP_EXIT = RISING + [100.8, 100.4] + [100.8] * 5
PERMISSIVE = ReplayPolicy(strict_market_data=False)
SHIPPED_COSTS = {
    "costs": {"spread_bps": 4.0, "slippage_bps": 6.0, "fee_bps": 0.5,
              "option_fee_per_contract_side": 0.65,
              "provenance": "shipped_conservative_v1_plus_25bps_stress"},
    "execution": {"max_slippage_bps": 50, "max_spread_bps": 100},
}
# SHA-256 of the canonical JSON payloads below, computed on the original code.
STATIC_V1_FINGERPRINT = (
    "b4919e5001a8299128f4d97d0cdaf25e5e88f4c5d2b3f2c3f35a9d4ab4395cc9")
LEGACY_SCHEDULE_HASH = (
    "b7a5f1af3d869dce74df48149522020e11d9a6abab92fa54ee4ba239df666b44")
LEGACY_CALIBRATION_HASH = (
    "2d32bedf3c25a048cb7729a75a1ddd84ce12a585659ea751019d439d9285d016")
LEGACY_DEPTH_CALIBRATION_HASH = (
    "5de844ae9e45061a66a1185b9b66ae1e6f4644107d1882056c951979ab499ae7")


def _digest(payload) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bars(closes):
    rows = []
    opened = 100.0
    for index, close in enumerate(closes):
        timestamp = BASE + timedelta(minutes=index)
        rows.append(normalize_underlying_bar({
            "kind": "bar", "provider": "test", "feed": "sip", "symbol": "SPY",
            "timestamp": timestamp.isoformat(), "as_of": timestamp.isoformat(),
            "observed_at": timestamp.isoformat(), "open": opened,
            "high": max(opened, close) + .05, "low": min(opened, close) - .05,
            "close": close, "volume": 1000,
        }))
        opened = close
    return rows


def _quote(minute, bid, ask):
    timestamp = BASE + timedelta(minutes=minute)
    return normalize_quote({
        "symbol": "SPY", "timestamp": timestamp.isoformat(),
        "as_of": timestamp.isoformat(), "observed_at": timestamp.isoformat(),
        "bid": bid, "ask": ask, "provider": "test", "feed": "sip",
    })


def _static_v1_payload() -> dict:
    """Every static_v1 output a stored result can depend on."""
    models = {
        "default": CostModel(),
        "shipped": CostModel.from_config(static_cost_config(SHIPPED_COSTS),
                                         vehicle="equity"),
        "custom": CostModel(spread_bps=2.5, slippage_bps=1.25, fee_bps=0.2,
                            provenance="custom"),
    }
    out: dict = {}
    for name, model in models.items():
        item: dict = {"as_dict": model.as_dict(),
                      "entry_cost_bps": model.entry_cost_bps,
                      "per_side": [model.per_side_bps(executable_quote=flag)
                                   for flag in (False, True)]}
        prices = []
        for reference in (43.76, 79.14, 100.6, 765.15):
            for direction in ("long", "short"):
                for entry in (True, False):
                    for quote in (False, True):
                        prices.append(model.execution_price(
                            reference, direction, entry=entry,
                            executable_quote=quote))
        item["prices"] = prices
        item["fees"] = [model.fees(100.0, 100.6, 250.0),
                        model.fees(765.15, 760.0, 32.0),
                        model.fees(2.5, 3.1, 4.0, 100.0, vehicle="option")]
        trips = []
        for entry_price, exit_price, qty, mult, vehicle in (
                (1.0, 1.0, 1.0, 1.0, "equity"),
                (100.0, 100.6, 250.0, 1.0, "equity"),
                (765.15, 760.0, 32.0, 1.0, "equity"),
                (2.5, 3.1, 4.0, 100.0, "option")):
            for flags in ({}, {"executable_quotes": True},
                          {"entry_executable_quote": True,
                           "exit_executable_quote": False}):
                trips.append(model.round_trip_cost(
                    entry_price, exit_price, qty, mult, vehicle=vehicle,
                    **flags))
        item["round_trips"] = trips
        item["stressed"] = [
            stressed_cost_usd(entry_notional=25_000.0, scenario_bps=scenario,
                              vehicle="equity", costs=model)
            for scenario in (9.0, 15.0, 25.0, 50.0)] + [
            stressed_cost_usd(entry_notional=1_000.0, scenario_bps=25.0,
                              vehicle="option", quantity=4.0, costs=model)]
        item["stress_plan"] = check_stressed_cost_plan(
            {"execution_profile": "shares", "shares": 100, "notional": 10_000.0,
             "risk_usd": 30.0}, scenario_bps=9.0, max_ratio=.30, costs=model)
        out[name] = item
    rows = [
        {"opportunity_id": "bar", "entry_price": 100.08, "exit_price": 100.52,
         "quantity": 250, "risk_usd": 75.0, "entry_fill_source": "bar",
         "exit_fill_source": "bar"},
        {"opportunity_id": "quote", "entry_price": 100.06, "exit_price": 99.7,
         "quantity": 250, "risk_usd": 90.0, "entry_fill_source": "quote",
         "exit_fill_source": "quote", "entry_feed": "iex", "exit_feed": "iex",
         "entry_provider": "alpaca", "exit_provider": "alpaca"},
        {"opportunity_id": "thin", "entry_price": 50.0, "exit_price": 50.1,
         "quantity": 10, "risk_usd": 0.01},
    ]
    out["risk_unit_report"] = risk_unit_report(rows, vehicle="equity",
                                               costs=models["shipped"])
    spec = validate_rule_spec(SPEC)
    out["account_bar"] = simulate_account(
        _bars(TIME_EXIT), [], spec, vehicle="equity",
        account_id="fingerprint-bar", risk_pct=.05, policy=PERMISSIVE)["rows"]
    out["account_quote"] = simulate_account(
        _bars(TIME_EXIT), [], spec, vehicle="equity",
        account_id="fingerprint-quote", risk_pct=.05, policy=PERMISSIVE,
        quotes=[_quote(4, 100.79, 100.81), _quote(8, 100.79, 100.81)],
        costs=models["shipped"])["rows"]
    stopped = RISING + [100.8, 100.4] + [100.8] * 5
    out["account_stop"] = simulate_account(
        _bars(stopped), [], spec, vehicle="equity",
        account_id="fingerprint-stop", risk_pct=.05, policy=PERMISSIVE)["rows"]
    normal = replay_policy_for_session(
        ReplayPolicy.from_config({
            "strategy": {"latest_entry_time": "15:00"},
            "session": {"require_exact_calendar": True,
                        "force_flat_minutes_before_close": 10,
                        "reject_new_entries_minutes_before_close": 5}}),
        session_open=datetime(2026, 10, 14, 9, 30, tzinfo=NEW_YORK),
        session_close=datetime(2026, 10, 14, 16, 0, tzinfo=NEW_YORK))
    out["normal_session_policy"] = normal.as_dict()
    return out


def _extended_hours_quotes() -> list[dict]:
    """Three sessions of quotes, six times wider outside regular hours."""
    rows = []
    for day in range(3):
        opening = datetime(2026, 1, 5, 9, 30, tzinfo=NEW_YORK) + timedelta(days=day)
        for minute in range(-30, 420, 2):
            stamp = (opening + timedelta(minutes=minute)).astimezone(timezone.utc)
            extended = minute < 0 or minute >= 390
            half = 100.0 * (6.0 if extended else 1.0) / 10_000.0 / 2.0
            rows.append({"kind": "quote", "symbol": "SPY", "feed": "iex",
                         "provider": "alpaca", "timestamp": stamp.isoformat(),
                         "bid": 100.0 - half, "ask": 100.0 + half,
                         "bid_size": 500.0, "ask_size": 700.0})
    return rows


def _session_quotes(*, sessions: int, start_minute: int = 0, minutes: int = 30,
                    feed: str = "iex", provider: str = "alpaca",
                    spread_bps: float = 1.0) -> list[dict]:
    rows = []
    for day in range(sessions):
        opening = datetime(2026, 1, 5, 9, 30, tzinfo=NEW_YORK) + timedelta(days=day)
        for minute in range(start_minute, start_minute + minutes):
            stamp = (opening + timedelta(minutes=minute)).astimezone(timezone.utc)
            half = 100.0 * spread_bps / 10_000.0 / 2.0
            rows.append({"kind": "quote", "symbol": "SPY", "feed": feed,
                         "provider": provider, "timestamp": stamp.isoformat(),
                         "bid": 100.0 - half, "ask": 100.0 + half,
                         "bid_size": 1_000.0, "ask_size": 1_000.0})
    return rows


class StaticV1EquivalenceTests(unittest.TestCase):
    """Stored evidence and sealed tests must recompute exactly."""

    def test_static_v1_outputs_match_the_original_code(self):
        self.assertEqual(_digest(_static_v1_payload()), STATIC_V1_FINGERPRINT)

    def test_static_v1_is_still_the_cost_model_default_and_unlabelled(self):
        model = CostModel()
        self.assertEqual(model.cost_model_version, COST_MODEL_STATIC_V1)
        # as_dict feeds identity hashes; static_v1 adds no version field to it.
        self.assertNotIn("cost_model_version", model.as_dict())
        self.assertEqual(model.expected_round_trip_bps(), 17.0)
        self.assertEqual(DEFAULT_RESEARCH_COST_MODEL_VERSION,
                         COST_MODEL_PER_SYMBOL_V2)

    def test_legacy_quote_schedule_reproduces_the_original_hash(self):
        schedule = measure_quote_costs(
            _extended_hours_quotes(), min_quotes_per_cell=10,
            min_sessions_per_cell=None, regular_session_aggregates=False)
        self.assertEqual(schedule["schedule_hash"], LEGACY_SCHEDULE_HASH)

    def test_legacy_calibration_reproduces_the_original_artifacts(self):
        artifact = calibrate_stressed_cost(
            _schedule(spread=16.0, digest="fit"),
            validation_schedule=_schedule(spread=20.0, digest="validation",
                                           session_start=6),
            expected_provider="alpaca", expected_feed="iex",
            cost_basis="one_way_v1")
        self.assertEqual(artifact["content_hash"], LEGACY_CALIBRATION_HASH)
        depth = calibrate_stressed_cost(
            _schedule(spread=4.0, digest="fit"),
            validation_schedule=_schedule(spread=8.0, digest="validation",
                                           session_start=6),
            order_shares=50_000.0, expected_provider="alpaca",
            expected_feed="iex", cost_basis="one_way_v1")
        self.assertEqual(depth["content_hash"], LEGACY_DEPTH_CALIBRATION_HASH)


class PerSymbolModelTests(unittest.TestCase):
    """E1-01 (per-symbol model), E1-03 (order types), E1-07 (sell-leg fees)."""

    def setUp(self):
        self.model = PerSymbolCostModel()

    def test_reference_table_is_the_audit_file(self):
        reference = json.loads((ROOT / "docs/audit-2026-09-21/spread-reference.json")
                               .read_text(encoding="utf-8"))["symbols"]
        self.assertEqual(set(reference), set(REFERENCE_SPREADS_BPS))
        for symbol, row in reference.items():
            self.assertEqual(REFERENCE_SPREADS_BPS[symbol],
                             row["corwin_schultz_median_bps"])
            self.assertEqual(REFERENCE_PRICES[symbol], row["median_price"])

    def test_round_trip_is_the_reference_study_formula(self):
        for symbol, price in REFERENCE_PRICES.items():
            with self.subTest(symbol=symbol):
                self.assertAlmostEqual(
                    self.model.round_trip_bps(symbol, price),
                    study_costs.round_trip_bps(
                        price, spread_bps=REFERENCE_SPREADS_BPS[symbol]),
                    places=12)

    def test_round_trips_at_reference_prices_are_realistic(self):
        expected = {"SPY": 2.1795, "QQQ": 2.5027, "IWM": 2.4937, "HYG": 2.4942}
        for symbol, total in expected.items():
            with self.subTest(symbol=symbol):
                breakdown = self.model.round_trip(symbol, REFERENCE_PRICES[symbol])
                self.assertAlmostEqual(breakdown["total_bps"], total, places=4)
                self.assertEqual(breakdown["cost_model_version"],
                                 COST_MODEL_PER_SYMBOL_V2)
        # VB is the cheapest (1.75 bps) and SLV the dearest (4.07 bps).
        trips = {symbol: self.model.round_trip_bps(symbol, price)
                 for symbol, price in REFERENCE_PRICES.items()}
        self.assertAlmostEqual(min(trips.values()), trips["VB"])
        self.assertAlmostEqual(max(trips.values()), trips["SLV"])
        self.assertTrue(1.7 < trips["VB"] and trips["SLV"] < 4.1)
        self.assertEqual(CostModel().expected_round_trip_bps(), 17.0)
        # HYG's reference spread is below one tick at $79.14: the tick binds.
        self.assertAlmostEqual(self.model.spread_bps("HYG", 79.14),
                               1e4 * .01 / 79.14)

    def test_a_symbol_without_a_spread_fails_closed(self):
        with self.assertRaisesRegex(CostError, "no measured or reference spread"):
            self.model.round_trip("ZZZ", 50.0)
        measured = PerSymbolCostModel.from_config(spreads={"ZZZ": 3.0})
        self.assertAlmostEqual(measured.spread_bps("ZZZ", 50.0), 3.0)
        self.assertEqual(measured.spread_entry("ZZZ")[1], "measured")

    def test_limit_legs_do_not_slip_and_carry_a_non_fill_flag(self):
        context = {"symbol": "SPY", "direction": "long", "quantity": 100,
                   "entry_reference": 100.0, "exit_reference": 100.60}
        target = self.model.leg_for_context(
            {**context, "cost_leg": "exit", "exit_reason": "target"})
        # The static model books this take-profit at 100.51952.
        self.assertAlmostEqual(
            CostModel().execution_price(100.60, "long", entry=False), 100.51952)
        self.assertEqual(target.execution_price(100.60, "long", entry=False), 100.60)
        self.assertIn(":resting_limit:", target.provenance)
        self.assertIn("target-fill-probability-1.0000", target.provenance)
        entry = self.model.leg_for_context({**context, "cost_leg": "entry"})
        self.assertIn(":marketable_limit:", entry.provenance)
        self.assertIn("nonfill-unmodelled", entry.provenance)
        # A marketable limit at the executable quote cannot fill worse.
        self.assertEqual(entry.execution_price(
            100.01, "long", entry=True, executable_quote=True), 100.01)
        spread = self.model.spread_bps("SPY", 100.0)
        self.assertAlmostEqual(entry.execution_price(100.0, "long", entry=True),
                               100.0 * (1 + spread / 2 / 1e4))
        stop = self.model.leg_for_context(
            {**context, "cost_leg": "exit", "exit_reason": "stop",
             "exit_reference": 99.70})
        stop_spread = self.model.spread_bps("SPY", 99.70)
        tick = 1e4 * .01 / 99.70
        self.assertAlmostEqual(
            stop.execution_price(99.70, "long", entry=False),
            99.70 * (1 - (stop_spread / 2 + 0.5 + tick) / 1e4))
        market = self.model.leg_for_context(
            {**context, "cost_leg": "exit", "exit_reason": "time"})
        self.assertIn(":market:", market.provenance)
        self.assertAlmostEqual(market.slippage_bps, 0.5)

    def test_target_fill_probability_charges_the_replacement_exit(self):
        partial = PerSymbolCostModel(target_fill_probability=.8)
        leg = partial.leg("SPY", 100.0, 100, side="sell", order_type=RESTING_LIMIT)
        spread = partial.spread_bps("SPY", 100.0)
        self.assertAlmostEqual(leg["non_fill_fallback_bps"],
                               .2 * (spread / 2 + .5))
        self.assertTrue(leg["non_fill_risk"])
        with self.assertRaises(CostError):
            PerSymbolCostModel(target_fill_probability=1.5)

    def test_fees_are_charged_on_the_sell_leg_only(self):
        buy = self.model.leg_model("SPY", 100.0, 100, side="buy", order_type=MARKET)
        sell = self.model.leg_model("SPY", 100.0, 100, side="sell", order_type=MARKET)
        self.assertEqual(buy.fees(100.0, 100.0, 100) / 2.0, 0.0)
        # SEC $20.60 per million on $10,000, plus TAF $0.000195 x 100 shares.
        self.assertAlmostEqual(sell.fees(100.0, 100.0, 100) / 2.0,
                               10_000 * 20.60 / 1e6 + 100 * 0.000195)
        # The static model charges 0.5 bps on the buy leg too.
        self.assertAlmostEqual(CostModel().fees(100.0, 100.0, 100) / 2.0, 0.5)
        self.assertAlmostEqual(self.model.sell_fee_usd(10.0, 100_000),
                               1_000_000 * 20.60 / 1e6 + FINRA_TAF_CAP)
        paused = PerSymbolCostModel(finra_taf_per_share=0.0)
        self.assertAlmostEqual(paused.sell_fee_usd(100.0, 100), 10_000 * 20.60 / 1e6)
        short = self.model.round_trip("SPY", 100.0, direction="short", shares=100)
        self.assertGreater(short["legs"]["entry"]["sec_fee_bps"], 0.0)
        self.assertEqual(short["legs"]["exit"]["sec_fee_bps"], 0.0)

    def test_account_rows_record_the_version_and_recompute_from_their_legs(self):
        spec = validate_rule_spec(SPEC)
        resolver = self.model.resolver()
        quotes = [_quote(4, 100.79, 100.81), _quote(8, 100.79, 100.81)]
        for closes, exit_order in ((TIME_EXIT, MARKET), (STOP_EXIT, STOP)):
            with self.subTest(exit=exit_order):
                row = simulate_account(
                    _bars(closes), [], spec, vehicle="equity",
                    account_id="per-symbol", risk_pct=.05, policy=PERMISSIVE,
                    quotes=quotes, cost_resolver=resolver)["rows"][0]
                # The marketable limit fills at the executable quote.
                self.assertEqual(row["entry_fill_source"], "quote")
                self.assertEqual(row["entry_price"], row["entry_reference"])
                self.assertTrue(row["entry_cost_model_provenance"].startswith(
                    f"{COST_MODEL_PER_SYMBOL_V2}:SPY:buy:{MARKETABLE_LIMIT}:"))
                self.assertTrue(row["exit_cost_model_provenance"].startswith(
                    f"{COST_MODEL_PER_SYMBOL_V2}:SPY:sell:{exit_order}:"))
                self.assertEqual(row["entry_cost_model_fee_bps"], 0.0)
                economics = recompute_row_cost_economics(row, vehicle="equity")
                legs_cost = (
                    abs(row["entry_price"] - row["entry_reference"]) * row["quantity"] +
                    abs(row["exit_price"] - row["exit_reference"]) * row["quantity"] +
                    row["costs"])
                self.assertAlmostEqual(economics["round_trip_cost"], legs_cost)
                report = risk_unit_report([row], vehicle="equity")
                self.assertAlmostEqual(
                    report["observations"][0]["round_trip_cost"], legs_cost)
                static = CostModel().round_trip_cost(
                    row["entry_price"], row["exit_price"], row["quantity"])
                self.assertLess(legs_cost, static / 4.0)


class StressTests(unittest.TestCase):
    """E1-02: stress is a round trip, defined relative to the expected model."""

    def test_rungs_are_round_trips_and_the_static_rung_is_not_a_stress(self):
        self.assertEqual(COST_STRESS_RUNG_BASIS,
                         "round_trip_all_in_bps_of_entry_notional")
        static = stress_coherence(9.0, CostModel().expected_round_trip_bps())
        self.assertFalse(static["stress_above_expected"])
        self.assertAlmostEqual(static["multiple_of_expected"], 9.0 / 17.0)
        quoted = stress_coherence(9.0, CostModel().expected_round_trip_bps(
            entry_executable_quote=True, exit_executable_quote=True))
        self.assertFalse(quoted["stress_above_expected"])
        model = PerSymbolCostModel()
        for symbol, price in REFERENCE_PRICES.items():
            with self.subTest(symbol=symbol):
                coherent = stress_coherence(9.0, model.round_trip_bps(symbol, price))
                self.assertTrue(coherent["stress_above_expected"])

    def test_stress_defined_from_the_expected_model(self):
        self.assertEqual(stress_relative_to_expected(2.0, multiple=3.0), 6.0)
        self.assertEqual(stress_relative_to_expected(2.0, add_bps=5.0), 7.0)
        with self.assertRaises(CostError):
            stress_relative_to_expected(2.0, multiple=.5)
        model = PerSymbolCostModel()
        expected = model.round_trip_bps("SPY", REFERENCE_PRICES["SPY"])
        self.assertAlmostEqual(
            model.stressed_round_trip_bps("SPY", REFERENCE_PRICES["SPY"],
                                          multiple=2.0), 2 * expected)

    def test_runtime_veto_arithmetic_is_unchanged(self):
        checked, reason = check_stressed_cost_plan(
            {"execution_profile": "shares", "shares": 100,
             "notional": 10_000.0, "risk_usd": 30.0},
            scenario_bps=9.0, max_ratio=.30)
        self.assertIsNone(reason)
        self.assertAlmostEqual(checked["stressed_cost_usd"], 9.0)


class CalibrationTests(unittest.TestCase):
    """E1-04 (materiality on rungs), E1-06 (round trip, depth), E1-08."""

    def test_a_change_inside_one_rung_keeps_the_cell_usable(self):
        # The review's XLRE case: one-way 1.7 -> 2.9 bps, both deep inside
        # the 9 bps rung, used to mark the cell unusable and void the artifact.
        fit = _schedule(spread=2.4, digest="fit")
        validation = _schedule(spread=4.8, digest="validation", session_start=6)
        artifact = calibrate_stressed_cost(
            fit, validation_schedule=validation, expected_provider="alpaca",
            expected_feed="iex", fallback_scenario_bps=9.0)
        cell = artifact["cells"][0]
        self.assertEqual((cell["status"], cell["selected_scenario_bps"]),
                         ("selected", 9.0))
        self.assertEqual(verify_stress_calibration_artifact(
            artifact, expected_provider="alpaca", expected_feed="iex"),
            (True, None))
        self.assertEqual(artifact["materiality_basis"], "ladder_rung")
        legacy = calibrate_stressed_cost(
            fit, validation_schedule=validation, expected_provider="alpaca",
            expected_feed="iex", cost_basis="one_way_v1")
        self.assertEqual(legacy["cells"][0]["fallback_reason"],
                         "validation_materially_exceeds_fit")
        # The lowest rung equals the shipped runtime rung, so the artifact can
        # never narrow it; the artifact says so.
        self.assertFalse(artifact["narrowing_possible"])
        self.assertTrue(calibrate_stressed_cost(
            fit, expected_feed="iex", fallback_scenario_bps=25.0)[
                "narrowing_possible"])

    def test_a_validation_rung_jump_is_material(self):
        artifact = calibrate_stressed_cost(
            _schedule(spread=4.0, digest="fit"),
            validation_schedule=_schedule(spread=12.0, digest="validation",
                                           session_start=6),
            expected_provider="alpaca", expected_feed="iex",
            fallback_scenario_bps=9.0)
        cell = artifact["cells"][0]
        self.assertEqual(cell["fallback_reason"], "validation_materially_exceeds_fit")
        self.assertFalse(cell["usable"])

    def test_the_round_trip_is_laddered(self):
        # A 16 bps p95 spread is a round trip of at least 16 bps; the one-way
        # mapping (8.5 bps) put it on the 9 bps rung.
        artifact = calibrate_stressed_cost(_schedule(spread=16.0), expected_feed="iex")
        cell = artifact["cells"][0]
        self.assertEqual(cell["selected_scenario_bps"], 25.0)
        self.assertAlmostEqual(cell["round_trip_cost_bps"], 16.0 + 0.206)
        self.assertEqual(artifact["cost_basis"], "round_trip")
        self.assertEqual(artifact["fee_basis"], "sell_leg_only")
        legacy = calibrate_stressed_cost(
            _schedule(spread=16.0), expected_feed="iex", cost_basis="one_way_v1")
        self.assertEqual(legacy["cells"][0]["selected_scenario_bps"], 9.0)

    def test_missing_depth_with_an_order_size_fails_closed(self):
        cell = calibrate_stressed_cost(
            _schedule(depth=None), order_shares=10_000.0,
            expected_feed="iex")["cells"][0]
        self.assertEqual(cell["fallback_reason"], "fit_measurement_missing")
        self.assertFalse(cell["usable"])
        legacy = calibrate_stressed_cost(
            _schedule(depth=None), order_shares=10_000.0, expected_feed="iex",
            cost_basis="one_way_v1")
        self.assertEqual(legacy["cells"][0]["status"], "selected")

    def test_the_rerun_falls_back_to_the_configured_runtime_rung(self):
        config = load_config(ROOT / "config.yaml")
        self.assertEqual(config["risk"]["stressed_cost_scenario_bps"], 9.0)
        report = run_cost_calibration(_quote_rows(), runtime_config=config,
                                      min_quotes_per_cell=1)
        self.assertEqual(report["stress_calibration"]["fallback_scenario_bps"], 9.0)


class MeasuredModeTests(unittest.TestCase):
    """E1-05 (stop-trigger slippage) and E1-10 (sessions, regular hours)."""

    def test_measured_v1_stop_legs_are_unchanged_by_default(self):
        schedule = measure_quote_costs(_session_quotes(sessions=6),
                                       min_quotes_per_cell=50)
        stop = measured_cost_resolver(schedule)(
            {"symbol": "SPY", "cost_leg": "exit", "quantity": 10,
             "cost_timestamp": "2026-01-05T14:40:00+00:00",
             "exit_reference": 100.0, "exit_reason": "stop"})
        self.assertEqual(stop.slippage_bps, 0.0)
        self.assertNotIn("stop-trigger", stop.provenance)

    def test_an_opted_in_stop_trigger_binds_at_the_gate(self):
        from research.gates import _cost_model_binding_report
        schedule = measure_quote_costs(_session_quotes(sessions=6),
                                       min_quotes_per_cell=50)
        authority = {"percentile": "p75", "depth_percentile": "p25",
                     "max_impact_half_spreads": 4.0, "feed": "iex",
                     "provider": "alpaca", "coverage_policy": "strict"}
        context = {"symbol": "SPY", "quantity": 10, "exit_reference": 100.37,
                   "cost_timestamp": "2026-01-05T14:40:00+00:00"}

        def report(ticks, reason, tamper=None):
            resolver = measured_cost_resolver(
                schedule, expected_feed="iex", expected_provider="alpaca",
                stop_trigger_slippage_ticks=ticks)
            entry = resolver({**context, "cost_leg": "entry",
                              "cost_timestamp": "2026-01-05T14:35:00+00:00"})
            exit_ = resolver({**context, "cost_leg": "exit", "exit_reason": reason})
            row = {"symbol": "SPY", "quantity": 10, "opportunity_id": "x",
                   "entry_timestamp": "2026-01-05T14:35:00+00:00",
                   "exit_timestamp": "2026-01-05T14:40:00+00:00"}
            for leg, model in (("entry", entry), ("exit", exit_)):
                row.update({f"{leg}_cost_model_provenance": model.provenance,
                            f"{leg}_cost_model_spread_bps": model.spread_bps,
                            f"{leg}_cost_model_slippage_bps": model.slippage_bps,
                            f"{leg}_cost_model_fee_bps": model.fee_bps,
                            f"{leg}_feed": "iex", f"{leg}_provider": "alpaca"})
            if tamper:
                row.update(tamper(row))
            return _cost_model_binding_report(
                {"heldout": [row]}, vehicle="equity",
                authority={**authority, "schedule": schedule,
                           "fee_bps": entry.fee_bps})["valid"]

        for ticks in (0, 1, 2.5):
            for reason in ("time", "stop", "target"):
                self.assertTrue(report(ticks, reason), (ticks, reason))
        # A suffix that disagrees with the charged slippage does not bind.
        self.assertFalse(report(1, "stop", lambda row: {
            "exit_cost_model_slippage_bps":
                row["exit_cost_model_slippage_bps"] + 0.5}))

    def test_a_measured_stop_leg_pays_trigger_slippage_when_opted_in(self):
        schedule = measure_quote_costs(_session_quotes(sessions=6),
                                       min_quotes_per_cell=50)
        resolver = measured_cost_resolver(schedule, stop_trigger_slippage_ticks=1)
        context = {"symbol": "SPY", "cost_leg": "exit", "quantity": 10,
                   "cost_timestamp": "2026-01-05T14:40:00+00:00",
                   "exit_reference": 100.0}
        stop = resolver({**context, "exit_reason": "stop"})
        timed = resolver({**context, "exit_reason": "time"})
        self.assertEqual(timed.slippage_bps, 0.0)
        self.assertAlmostEqual(stop.slippage_bps, 1.0)   # one tick at $100
        self.assertTrue(stop.provenance.endswith(":stop-trigger-1.0000"))
        self.assertEqual(measured_cost_resolver(
            schedule, stop_trigger_slippage_ticks=0)(
                {**context, "exit_reason": "stop"}).slippage_bps, 0.0)
        with self.assertRaisesRegex(QuoteCostError, "reference price"):
            resolver({**context, "exit_reason": "stop", "exit_reference": None})
        # The suffixed provenance still binds to a persisted row.
        row = {"symbol": "SPY", "quantity": 10,
               "entry_timestamp": "2026-01-05T14:35:00+00:00",
               "exit_timestamp": "2026-01-05T14:40:00+00:00"}
        for leg, model in (("entry", timed), ("exit", stop)):
            row.update({f"{leg}_cost_model_provenance": model.provenance,
                        f"{leg}_cost_model_spread_bps": model.spread_bps,
                        f"{leg}_cost_model_slippage_bps": model.slippage_bps,
                        f"{leg}_cost_model_fee_bps": model.fee_bps,
                        f"{leg}_feed": "iex", f"{leg}_provider": "alpaca"})
        binding = row_cost_model_binding(row, vehicle="equity")
        self.assertAlmostEqual(binding["legs"]["exit"]["slippage_bps"], 1.0)

    def test_cells_need_sessions_not_just_snapshots(self):
        # 600 snapshots in one half-hour cell, but from only two sessions.
        rows = _session_quotes(sessions=2, minutes=30) * 10
        # cost_rerun (new research runs) fits with this floor.
        schedule = measure_quote_costs(rows, min_quotes_per_cell=500,
                                       min_sessions_per_cell=5)
        self.assertNotIn("m000_030", schedule["symbols"]["SPY"]["buckets"])
        self.assertEqual(schedule["measured"]["min_sessions_per_cell"], 5)
        with self.assertRaisesRegex(QuoteCostError, "under-covered"):
            cost_model_from_schedule(schedule, symbol="SPY", bucket="m000_030")
        legacy = measure_quote_costs(rows, min_quotes_per_cell=500,
                                     min_sessions_per_cell=None,
                                     regular_session_aggregates=False)
        self.assertIn("m000_030", legacy["symbols"]["SPY"]["buckets"])

    def test_the_resolver_enforces_the_schedules_session_floor(self):
        schedule = measure_quote_costs(_session_quotes(sessions=3),
                                       min_quotes_per_cell=50,
                                       min_sessions_per_cell=3)
        cost_model_from_schedule(schedule, symbol="SPY", bucket="m000_030")
        schedule["measured"]["min_sessions_per_cell"] = 4
        schedule.pop("schedule_hash")
        from research.edge_ledger import content_hash
        schedule["schedule_hash"] = content_hash(schedule)
        with self.assertRaisesRegex(QuoteCostError, "under-covered"):
            cost_model_from_schedule(schedule, symbol="SPY", bucket="m000_030")

    def test_extended_hours_stay_out_of_the_aggregates(self):
        # Regular-hours quotes are 1 bps wide, extended-hours ones 6 bps.
        schedule = measure_quote_costs(_extended_hours_quotes(),
                                       min_quotes_per_cell=10)
        self.assertAlmostEqual(schedule["symbols"]["SPY"]["spread_bps"]["mean"],
                               1.0, places=6)
        self.assertAlmostEqual(schedule["universe"]["spread_bps"]["mean"],
                               1.0, places=6)
        self.assertEqual(schedule["measured"]["aggregate_scope"], "regular_session")
        legacy = measure_quote_costs(_extended_hours_quotes(), min_quotes_per_cell=10,
                                     min_sessions_per_cell=None,
                                     regular_session_aggregates=False)
        self.assertGreater(legacy["symbols"]["SPY"]["spread_bps"]["mean"], 1.5)
        # The extended-hours cells themselves are still measured.
        self.assertIn("post_close", measure_quote_costs(
            _extended_hours_quotes(), min_quotes_per_cell=10,
            min_sessions_per_cell=3)["symbols"]["SPY"]["buckets"])


class ReplayTests(unittest.TestCase):
    """E1-09 (coverage refusal) and E1-11 (early-close entry cut-off)."""

    def test_an_uncovered_measured_cell_refuses_one_opportunity(self):
        # The schedule covers only the second half hour; the entry is at 09:34.
        schedule = measure_quote_costs(
            _session_quotes(sessions=6, start_minute=30, feed="sip",
                            provider="test"),
            min_quotes_per_cell=10)
        rows = simulate_account(
            _bars(TIME_EXIT), [], validate_rule_spec(SPEC), vehicle="equity",
            account_id="coverage", risk_pct=.05, policy=PERMISSIVE,
            cost_resolver=measured_cost_resolver(schedule))["rows"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["no_trade"])
        self.assertEqual(rows[0]["reject_stage"], "cost_coverage")
        self.assertEqual(rows[0]["reject_reason"], "measured_cost_unavailable")
        self.assertEqual(rows[0]["cost_coverage_leg"], "entry")
        self.assertIn("under-covered", rows[0]["cost_coverage_detail"])

    def test_an_uncovered_exit_cell_stops_the_variant(self):
        # Review of E1-09: the exit leg's cell depends on how the trade played
        # out, so refusing that one trade would keep or drop it by outcome.
        schedule = measure_quote_costs(_session_quotes(sessions=6),
                                       min_quotes_per_cell=50)
        measured = measured_cost_resolver(schedule)

        def exit_uncovered(context):
            if context.get("cost_leg") == "exit":
                raise QuoteCostError("requested measured cost bucket is under-covered")
            return measured(context)

        with self.assertRaisesRegex(QuoteCostError, "select on its outcome"):
            simulate_account(
                _bars(TIME_EXIT), [], validate_rule_spec(SPEC), vehicle="equity",
                account_id="coverage", risk_pct=.05, policy=PERMISSIVE,
                cost_resolver=exit_uncovered)

    def test_early_close_policy_keeps_both_cutoffs(self):
        # E1-11 is closed where an entry is admitted (account simulation, live
        # shadow and now the IBR replay refuse an entry after force-flat), not
        # by rewriting the policy: every lane's refusal reason reads both
        # values (tests/research/test_shadow_deadlines.py).
        policy = ReplayPolicy.from_config({
            "strategy": {"latest_entry_time": "15:00"},
            "session": {"require_exact_calendar": True,
                        "force_flat_minutes_before_close": 10,
                        "reject_new_entries_minutes_before_close": 5}})
        early = replay_policy_for_session(
            policy, session_open=datetime(2026, 11, 27, 9, 30, tzinfo=NEW_YORK),
            session_close=datetime(2026, 11, 27, 13, 0, tzinfo=NEW_YORK))
        self.assertEqual(early.force_flat_time, time(12, 50))
        self.assertEqual(early.latest_entry_time, time(12, 55))
        normal = replay_policy_for_session(
            policy, session_open=datetime(2026, 10, 14, 9, 30, tzinfo=NEW_YORK),
            session_close=datetime(2026, 10, 14, 16, 0, tzinfo=NEW_YORK))
        self.assertEqual(normal.latest_entry_time, time(15, 0))


if __name__ == "__main__":
    unittest.main()
