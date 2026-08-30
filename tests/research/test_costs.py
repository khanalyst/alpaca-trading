"""One cost and fill model, shared by every research lane.

These tests pin the three things that let the lanes drift apart before: each
lane carried its own spread/slippage/fee numbers, each re-implemented the
arithmetic, and one of them sourced an expected slippage from the runtime's
*rejection cap*.  They also pin the fill realism the model is worth nothing
without: exits that gap through a resting leg, and fills priced from a
recorded quote when one exists at the fill instant.
"""

from datetime import datetime, timedelta, timezone
from dataclasses import replace
import sqlite3
import unittest

from agent.contracts.rule import validate_rule_spec
from agent.config import ConfigError, validate_config
from agent.risk import RiskEngine
from research import calibration
from research.costs import (BAR, CostError, CostModel, DEFAULT_FEE_BPS,
                            DEFAULT_OPTION_FEE_PER_CONTRACT_SIDE,
                            DEFAULT_SLIPPAGE_BPS, DEFAULT_SPREAD_BPS, QUOTE,
                            ENTRY_SLIPPAGE_INVALID_REASON,
                            ENTRY_SLIPPAGE_REJECT_REASON,
                            RUNTIME_MAX_SLIPPAGE_BPS, ReplayPolicy,
                            STRESSED_COST_BASIS, STRESSED_COST_SCHEMA,
                            SQLiteQuoteIndex, check_stressed_cost_plan,
                            check_entry_slippage,
                            index_quotes, quote_fill, quote_fill_record,
                            cost_model_for_vehicle, risk_unit_report,
                            stressed_cost_usd)
from research.edge_discovery_core import (DiscoveryError,
                                          _effective_ibr_config,
                                          _read_discovery_rows)
from research.factory_core import (NOTIONAL_CAP_PCT, _simulate_trade,
                                   simulate_account)
from research.strategy_factory import null_control_account
from research.ibr import IBRConfig
from research.market_data import normalize_quote, normalize_underlying_bar

BASE = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
SPEC = validate_rule_spec({
    "family": "momentum_continuation", "lookback": 3, "slow_lookback": 8,
    "atr_period": 3, "threshold_bps": 1.0, "stop_atr": 1.0, "target_r": 2.0,
    "max_hold_bars": 3, "confirmation": "none",
})
PERMISSIVE_POLICY = ReplayPolicy(strict_market_data=False)
# Four rising bars signal at index 3, so entry is index 4 and the bounded hold
# expires at the end of index 7.  Stop 100.50, target 101.40.
RISING = [100.2, 100.4, 100.6, 100.8]
FLAT = [100.8] * 8


def _bars(closes, opens=None):
    rows = []
    opened = 100.0
    for index, close in enumerate(closes):
        opened = float((opens or {}).get(index, opened))
        timestamp = BASE + timedelta(minutes=index)
        end = timestamp + timedelta(minutes=1)
        rows.append(normalize_underlying_bar({
            "kind": "bar", "provider": "test", "feed": "sip", "symbol": "SPY",
            # Mechanics-only fixture: model a source whose opening print is
            # visible at the boundary; recorder-completed OHLC is tested
            # separately with delayed observations.
            "timestamp": timestamp.isoformat(), "as_of": timestamp.isoformat(),
            "observed_at": timestamp.isoformat(), "open": opened,
            "high": max(opened, close) + .05, "low": min(opened, close) - .05,
            "close": close, "volume": 1000,
        }))
        opened = close
    return rows


def _quote(minute, bid, ask, *, as_of_minute=None):
    timestamp = BASE + timedelta(minutes=minute)
    as_of = BASE + timedelta(minutes=as_of_minute if as_of_minute is not None else minute)
    return normalize_quote({
        "symbol": "SPY", "timestamp": timestamp.isoformat(),
        "as_of": as_of.isoformat(), "observed_at": as_of.isoformat(),
        "bid": bid, "ask": ask, "provider": "test", "feed": "sip",
    })


class CostModelTests(unittest.TestCase):
    def test_entry_slippage_helper_is_adverse_once_and_stable(self):
        telemetry, reason = check_entry_slippage("buy", 101.0, 101.06, 5.0)
        self.assertEqual(reason, ENTRY_SLIPPAGE_REJECT_REASON)
        self.assertFalse(telemetry["accepted"])
        self.assertAlmostEqual(telemetry["adverse_bps"],
                               (101.06 - 101.0) / 101.0 * 10_000.0)
        self.assertEqual(telemetry["slippage_bps"], telemetry["adverse_bps"])
        self.assertEqual(telemetry["reason"], ENTRY_SLIPPAGE_REJECT_REASON)

        accepted, accepted_reason = check_entry_slippage(
            "sell", 101.0, 101.01, 5.0)
        self.assertIsNone(accepted_reason)
        self.assertTrue(accepted["accepted"])
        self.assertEqual(accepted["adverse_bps"], 0.0)

    def test_entry_slippage_helper_fails_closed_for_malformed_inputs(self):
        for values in (
                ("hold", 100.0, 100.0, 50.0),
                ("buy", "100", 100.0, 50.0),
                ("buy", 100.0, float("nan"), 50.0),
                ("buy", 100.0, 100.0, -1.0),
                ("buy", 0.0, 100.0, 50.0),
        ):
            with self.subTest(values=values):
                telemetry, reason = check_entry_slippage(*values)
                self.assertEqual(reason, ENTRY_SLIPPAGE_INVALID_REASON)
                self.assertFalse(telemetry["accepted"])

    def test_replay_policy_stress_controls_are_runtime_only_and_identity_bound(self):
        direct = ReplayPolicy()
        self.assertEqual(direct.equity_feed, "iex")
        self.assertIsNone(direct.stressed_cost_scenario_bps)
        self.assertIsNone(direct.max_stressed_cost_to_risk_ratio)
        runtime = ReplayPolicy.from_config(validate_config({}))
        self.assertEqual(runtime.equity_feed, "iex")
        self.assertEqual(runtime.as_dict()["equity_feed"], "iex")
        self.assertEqual(runtime.stressed_cost_scenario_bps, 25.0)
        self.assertEqual(runtime.max_stressed_cost_to_risk_ratio, .30)
        self.assertEqual(runtime.as_dict()["stressed_cost_scenario_bps"], 25.0)
        self.assertEqual(runtime.as_dict()["max_stressed_cost_to_risk_ratio"], .30)

    def test_research_stress_veto_matches_runtime_at_boundary(self):
        config = validate_config({"risk": {
            "stressed_cost_scenario_bps": 25.0,
            "max_stressed_cost_to_risk_ratio": .30,
        }})
        engine = RiskEngine(config)
        for distance_bps, expected in ((30.0, "stressed_cost_risk_limit"),
                                       (84.0, None)):
            with self.subTest(distance_bps=distance_bps):
                plan = {"execution_profile": "shares", "shares": 10,
                        "notional": 1_000.0,
                        "risk_usd": 10 * distance_bps / 100.0}
                runtime, runtime_reason = engine.check_stressed_cost(
                    plan, cfg=config)
                research, research_reason = check_stressed_cost_plan(
                    plan, scenario_bps=25.0, max_ratio=.30,
                    config=config)
                self.assertEqual(research_reason, runtime_reason)
                self.assertEqual(research_reason, expected)
                self.assertEqual(research is None, runtime is None)
                if research is not None:
                    self.assertAlmostEqual(
                        research["stressed_cost_to_risk_ratio"],
                        runtime["stressed_cost_to_risk_ratio"])

    def test_partial_null_stress_controls_fail_closed_like_runtime(self):
        plan = {"execution_profile": "shares", "shares": 1,
                "notional": 100.0, "risk_usd": 1.0}
        _, reason = check_stressed_cost_plan(
            plan, scenario_bps=None, max_ratio=.30)
        self.assertEqual(reason, "stressed_cost_invalid")

    def test_stress_names_entry_notional_and_basis_without_changing_formula(self):
        self.assertAlmostEqual(
            stressed_cost_usd(entry_notional=1_000.0, scenario_bps=25.0,
                              vehicle="equity"), 2.5)
        with self.assertRaisesRegex(CostError, "not both"):
            stressed_cost_usd(1_000.0, 25.0, entry_notional=1_000.0,
                              vehicle="equity")
        self.assertEqual(STRESSED_COST_SCHEMA, "stressed-entry-cost.v1")
        self.assertEqual(STRESSED_COST_BASIS["notional"], "entry_notional")

    def test_option_default_has_a_conservative_two_side_contract_fee(self):
        option = CostModel().fees(2.0, 2.0, 1, 100, vehicle="option")
        equity = CostModel().fees(2.0, 2.0, 1, 100, vehicle="equity")
        self.assertGreaterEqual(
            option - equity, 2 * DEFAULT_OPTION_FEE_PER_CONTRACT_SIDE)

    def test_entry_cost_is_half_the_spread_plus_slippage(self):
        model = CostModel(spread_bps=2.0, slippage_bps=3.0)
        self.assertAlmostEqual(model.entry_cost_bps, 4.0, places=12)
        self.assertAlmostEqual(model.per_side_bps(), 4.0, places=12)
        # An executable quote already contains the spread; only slippage is
        # charged on top, or the same spread is billed twice.
        self.assertAlmostEqual(model.per_side_bps(executable_quote=True), 3.0,
                               places=12)

    def test_execution_price_is_adverse_on_both_sides_and_both_directions(self):
        model = CostModel(spread_bps=2.0, slippage_bps=3.0)
        rate = 4.0 / 10_000.0
        self.assertAlmostEqual(model.execution_price(100.0, "long", entry=True),
                               100.0 * (1 + rate), places=12)
        self.assertAlmostEqual(model.execution_price(100.0, "long", entry=False),
                               100.0 * (1 - rate), places=12)
        self.assertAlmostEqual(model.execution_price(100.0, "short", entry=True),
                               100.0 * (1 - rate), places=12)
        self.assertAlmostEqual(model.execution_price(100.0, "short", entry=False),
                               100.0 * (1 + rate), places=12)

    def test_fees_are_charged_on_both_sides_of_the_traded_notional(self):
        model = CostModel(fee_bps=0.5)
        self.assertAlmostEqual(model.fees(100.0, 110.0, 10, 1),
                               (100.0 + 110.0) * 10 * 0.5 / 10_000.0, places=12)
        self.assertAlmostEqual(model.fees(2.0, 3.0, 4, 100),
                               (2.0 + 3.0) * 4 * 100 * 0.5 / 10_000.0, places=12)

    def test_the_rejection_cap_bounds_the_model_and_never_supplies_it(self):
        # A cap is the worst fill the runtime will accept, not the fill it
        # expects.  Expecting the cap is as wrong as ignoring it.
        self.assertLess(CostModel().entry_cost_bps, RUNTIME_MAX_SLIPPAGE_BPS)
        with self.assertRaisesRegex(CostError, "slippage cap"):
            CostModel(slippage_bps=RUNTIME_MAX_SLIPPAGE_BPS, max_slippage_bps=10.0)
        with self.assertRaisesRegex(CostError, "rejection cap"):
            CostModel(spread_bps=200.0, max_spread_bps=100.0)

    def test_malformed_parameters_fail_closed(self):
        for bad in (float("nan"), float("inf"), -1.0, True, "2", None):
            with self.subTest(bad=bad):
                with self.assertRaises(CostError):
                    CostModel(spread_bps=bad)

    def test_from_config_reads_one_block_and_the_runtime_caps(self):
        model = CostModel.from_config({
            "costs": {"spread_bps": 4.0, "slippage_bps": 6.0},
            "execution": {"max_slippage_bps": 40, "max_spread_bps": 80}})
        self.assertEqual((model.spread_bps, model.slippage_bps, model.fee_bps),
                         (4.0, 6.0, DEFAULT_FEE_BPS))
        self.assertEqual((model.max_spread_bps, model.max_slippage_bps), (80.0, 40.0))
        self.assertEqual(CostModel.from_config(None), CostModel())
        with self.assertRaisesRegex(CostError, "unknown field"):
            CostModel.from_config({"costs": {"max_slippage_bps": 50}})
        # Tightening the runtime's tolerance is immediately a research
        # constraint rather than a number somebody remembers to copy.
        with self.assertRaises(CostError):
            CostModel.from_config({"execution": {"max_slippage_bps": 1}})

    def test_vehicle_schedule_inherits_flat_values_and_caps(self):
        config = validate_config({"costs": {
            "spread_bps": 4.0, "slippage_bps": 5.0,
            "vehicles": {"option": {"spread_bps": 8.0}},
        }})
        option = CostModel.from_config(config, vehicle="option")
        equity = CostModel.from_config(config, vehicle="equity")
        self.assertEqual(option.spread_bps, 8.0)
        self.assertEqual(option.slippage_bps, 5.0)
        self.assertEqual(option.provenance, "config")
        self.assertEqual(option.max_slippage_bps,
                         config["execution"]["max_slippage_bps"])
        self.assertEqual(cost_model_for_vehicle(config["costs"], "option"), option)
        self.assertEqual(equity.spread_bps, 4.0)

    def test_vehicle_schedule_rejects_unknown_or_malformed_entries(self):
        for costs in (
                {"vehicles": {"crypto": {"spread_bps": 1}}},
                {"vehicles": {"option": []}},
                {"vehicles": []},
                {"vehicles": {"option": {"bogus": 1}}}):
            with self.subTest(costs=costs), self.assertRaises(ConfigError):
                validate_config({"costs": costs})


class OptionProvenanceTests(unittest.TestCase):
    @staticmethod
    def _row(**changes):
        row = {
            "vehicle": "option", "opportunity_id": "option:1",
            "no_trade": False, "entry_price": 2.0, "exit_price": 2.2,
            "quantity": 1.0, "contract_multiplier": 100.0,
            "risk_usd": 200.0, "entry_fill_source": QUOTE,
            "exit_fill_source": QUOTE, "entry_feed": "opra",
            "exit_feed": "opra", "entry_provider": "alpaca",
            "exit_provider": "alpaca", "entry_quote_age_seconds": 0.0,
            "exit_quote_age_seconds": 0.0,
        }
        row.update(changes)
        return row

    def test_option_requires_fresh_opra_quote_fills_on_both_legs(self):
        free = CostModel(spread_bps=0, slippage_bps=0, fee_bps=0)
        self.assertTrue(risk_unit_report(
            [self._row()], vehicle="option", costs=free)["adequate"])
        for changes in (
                {"entry_fill_source": BAR},
                {"exit_fill_source": BAR},
                {"entry_feed": "indicative"},
                {"exit_feed": "indicative"},
                {"entry_provider": None},
                {"exit_provider": ""},
                {"entry_quote_age_seconds": 31.0},
                {"exit_quote_age_seconds": float("nan")},
                {"entry_quote_age_seconds": None}):
            with self.subTest(changes=changes):
                report = risk_unit_report(
                    [self._row(**changes)], vehicle="option", costs=free)
                self.assertFalse(report["adequate"])
                self.assertTrue(report["failure_reasons"])
                self.assertIn("option", report["adequacy_reason"])


class EquityProvenanceTests(unittest.TestCase):
    """Only IEX quote legs can authorize current equity economics evidence."""

    @staticmethod
    def _row(**changes):
        row = {
            "vehicle": "equity", "opportunity_id": "equity:1",
            "no_trade": False, "entry_price": 100.0, "exit_price": 101.0,
            "quantity": 1.0, "contract_multiplier": 1.0,
            "risk_usd": 10.0, "entry_fill_source": QUOTE,
            "exit_fill_source": QUOTE, "entry_feed": "iex",
            "exit_feed": "iex", "entry_provider": "alpaca",
            "exit_provider": "alpaca", "entry_quote_age_seconds": 0.0,
            "exit_quote_age_seconds": 0.0,
        }
        row.update(changes)
        return row

    @staticmethod
    def _report(row):
        return risk_unit_report(
            [row], vehicle="equity",
            costs=CostModel(spread_bps=0, slippage_bps=0, fee_bps=0))

    def test_iex_quote_sources_and_providers_are_adequate(self):
        self.assertTrue(self._report(self._row())["adequate"])

    def test_sip_quote_source_is_diagnostic_only(self):
        report = self._report(self._row(entry_feed="sip", exit_feed="sip"))
        self.assertFalse(report["adequate"])

    def test_missing_or_unknown_equity_feed_is_diagnostic_only(self):
        for changes in ({"entry_feed": None}, {"exit_feed": None},
                        {"entry_feed": "unknown"}, {"exit_feed": "unknown"}):
            with self.subTest(changes=changes):
                self.assertFalse(self._report(self._row(**changes))["adequate"])

    def test_factory_quote_priced_legs_retain_feed_and_provider(self):
        row = _simulate_trade(
            _bars(RISING + FLAT), SPEC, [], "equity",
            quotes=index_quotes([_quote(4, 99.0, 100.0),
                                 _quote(8, 99.0, 100.0)]),
            policy=PERMISSIVE_POLICY)
        self.assertEqual((row["entry_fill_source"], row["exit_fill_source"]),
                         (QUOTE, QUOTE))
        self.assertEqual((row["entry_feed"], row["exit_feed"]), ("sip", "sip"))
        self.assertEqual((row["entry_provider"], row["exit_provider"]),
                         ("test", "test"))


class SharedModelTests(unittest.TestCase):
    """No lane may carry its own numbers or its own arithmetic."""

    def test_every_lane_defaults_to_the_same_model(self):
        self.assertEqual(IBRConfig().costs, CostModel())
        priced = simulate_account(_bars(RISING + FLAT), [], SPEC, vehicle="equity",
                                  account_id="explicit", risk_pct=.05,
                                  costs=CostModel(), policy=PERMISSIVE_POLICY)
        implied = simulate_account(_bars(RISING + FLAT), [], SPEC, vehicle="equity",
                                   account_id="implied", risk_pct=.05,
                                   policy=PERMISSIVE_POLICY)
        self.assertEqual(priced["rows"][0]["net_pnl"], implied["rows"][0]["net_pnl"])

    def test_discovery_no_longer_sources_slippage_from_the_rejection_cap(self):
        cfg, effective = _effective_ibr_config(
            {"execution": {"max_slippage_bps": 50, "max_spread_bps": 100}}, {})
        self.assertEqual(cfg.costs.slippage_bps, DEFAULT_SLIPPAGE_BPS)
        self.assertEqual(cfg.costs.spread_bps, DEFAULT_SPREAD_BPS)
        self.assertEqual(cfg.costs.max_slippage_bps, 50.0)
        # The model is persisted with the effective config, so a proof records
        # what its results were priced at.
        self.assertEqual(effective["costs"], cfg.costs.as_dict())

    def test_discovery_takes_an_explicit_cost_override_from_the_one_block(self):
        cfg, _ = _effective_ibr_config({"costs": {"spread_bps": 6.0}}, {})
        self.assertEqual(cfg.costs.spread_bps, 6.0)

    def test_a_replay_config_cannot_carry_loose_cost_numbers(self):
        with self.assertRaises(TypeError):
            IBRConfig(spread_bps=1.0)


class DailyLossReplayTests(unittest.TestCase):
    """The replay gate sees the same intraday equity loss as runtime."""

    @staticmethod
    def _shifted(rows, *, symbol, minutes):
        return [replace(row, symbol=symbol,
                        timestamp=row.timestamp + timedelta(minutes=minutes))
                for row in rows]

    @classmethod
    def _book(cls, first_values, *, second_shift=1, limit=.01):
        first = _bars(first_values)
        # A second isolated symbol creates a later entry while the first
        # position remains open.  strict_market_data=False intentionally uses
        # the visible bar boundary marks for this focused MTM fixture.
        second = cls._shifted(_bars(RISING + FLAT), symbol="QQQ",
                              minutes=second_shift)
        return simulate_account(
            first + second, [], SPEC, vehicle="equity", account_id="mtm",
            policy=ReplayPolicy(daily_loss_limit_pct=limit,
                                strict_market_data=False))

    def test_open_unrealized_loss_halts_a_later_same_day_entry(self):
        book = self._book(RISING + [100.6, 100.6] + FLAT[:6])
        by_symbol = {row["symbol"]: row for row in book["rows"]}
        self.assertFalse(by_symbol["SPY"]["no_trade"])
        self.assertTrue(by_symbol["QQQ"]["no_trade"])
        self.assertEqual(by_symbol["QQQ"]["reject_reason"],
                         "daily loss limit reached")

    def test_mark_does_not_consume_a_future_price(self):
        baseline = self._book(RISING + [100.6, 100.6] + FLAT[:6])
        future_drop = self._book(
            RISING + [100.6, 100.6, 90.0] + FLAT[:5])
        baseline_qqq = next(row for row in baseline["rows"] if row["symbol"] == "QQQ")
        future_qqq = next(row for row in future_drop["rows"] if row["symbol"] == "QQQ")
        self.assertEqual((baseline_qqq["no_trade"], baseline_qqq["reject_reason"]),
                         (future_qqq["no_trade"], future_qqq["reject_reason"]))

    def test_realized_exit_is_counted_once_and_caps_can_be_disabled(self):
        # The first trade hits its stop before the second symbol's entry.  Its
        # realized loss is in cash exactly once when the later gate runs.
        realized = self._book(RISING + [100.8, 100.4] + FLAT[:6],
                              second_shift=2)
        realized_qqq = next(row for row in realized["rows"] if row["symbol"] == "QQQ")
        self.assertEqual(realized_qqq["reject_reason"], "daily loss limit reached")

        uncapped = self._book(RISING + [100.8, 100.6] + FLAT[:6], limit=None)
        loose = self._book(RISING + [100.8, 100.6] + FLAT[:6], limit=100.0)
        self.assertEqual(uncapped["trades"], loose["trades"])
        self.assertEqual(
            [row["no_trade"] for row in uncapped["rows"]],
            [row["no_trade"] for row in loose["rows"]],
        )

    def test_open_loss_reduces_the_next_entry_sizing_denominator(self):
        flat = self._book(RISING + [100.8, 100.8] + FLAT[:6], limit=None)
        losing = self._book(RISING + [100.6, 100.6] + FLAT[:6], limit=None)
        flat_entry = next(row for row in flat["rows"] if row["symbol"] == "QQQ")
        losing_entry = next(row for row in losing["rows"] if row["symbol"] == "QQQ")
        self.assertFalse(flat_entry["no_trade"])
        self.assertFalse(losing_entry["no_trade"])
        self.assertLess(losing_entry["risk_budget"], flat_entry["risk_budget"])


class RecordedQuotesReachTheReplayTests(unittest.TestCase):
    """The recorder captures quotes; the corpus loader must not drop them."""

    def _rows(self):
        bar = {"kind": "bar", "provider": "alpaca", "feed": "sip", "symbol": "SPY",
               "timestamp": BASE.isoformat(), "open": 100, "high": 101,
               "low": 99, "close": 100, "volume": 1}
        quote = {"kind": "quote", "provider": "alpaca", "feed": "sip",
                 "symbol": "SPY", "timestamp": BASE.isoformat(),
                 "bid": 99.98, "ask": 100.02}
        return [bar, quote]

    def test_quote_rows_are_normalized_rather_than_only_hashed(self):
        raw, bars, snapshots, quotes = _read_discovery_rows(self._rows())
        self.assertEqual(len(raw), 2)
        self.assertEqual(len(bars), 1)
        self.assertEqual(snapshots, {})
        self.assertEqual([(q.symbol, q.bid, q.ask) for q in quotes],
                         [("SPY", 99.98, 100.02)])

    def test_a_malformed_quote_fails_the_corpus_rather_than_being_skipped(self):
        rows = self._rows()
        rows[1]["ask"] = 0
        with self.assertRaises(DiscoveryError):
            _read_discovery_rows(rows)


class ExitGapThroughTests(unittest.TestCase):
    """A bar that opens beyond a resting leg fills at that open."""

    def test_a_gap_through_the_stop_fills_worse_than_the_stop(self):
        # Stop is 100.50; bar 6 opens at 100.20 and never trades back.
        row = _simulate_trade(_bars(RISING + FLAT, {6: 100.2}), SPEC, [], "equity",
                              policy=PERMISSIVE_POLICY)
        self.assertEqual(row["exit_reason"], "stop")
        self.assertIs(row["exit_gap_fill"], True)
        self.assertAlmostEqual(row["exit_reference"], 100.2, places=9)
        self.assertLess(row["exit_reference"], row["stop_price"])

    def test_a_gap_through_the_target_fills_at_the_open_not_the_target(self):
        row = _simulate_trade(_bars(RISING + FLAT, {6: 101.9}), SPEC, [], "equity",
                              policy=PERMISSIVE_POLICY)
        self.assertEqual(row["exit_reason"], "target")
        self.assertIs(row["exit_gap_fill"], True)
        self.assertAlmostEqual(row["exit_reference"], 101.9, places=9)
        self.assertGreater(row["exit_reference"], row["target_price"])

    def test_an_intrabar_touch_still_fills_at_the_level(self):
        # 100.4 closes below the 100.50 stop without opening through it, so the
        # resting leg triggers at its own price.
        row = _simulate_trade(_bars(RISING + [100.8, 100.4] + FLAT[:5]),
                              SPEC, [], "equity", policy=PERMISSIVE_POLICY)
        self.assertEqual(row["exit_reason"], "stop")
        self.assertIs(row["exit_gap_fill"], False)
        self.assertAlmostEqual(row["exit_reference"], row["stop_price"], places=9)

    def test_a_gap_spanning_both_levels_resolves_to_the_stop(self):
        # A bar opening at 100.20 with a 102.0 close spans stop and target.
        # The stop is encountered at the open, so ties stay stop-first.
        row = _simulate_trade(_bars(RISING + FLAT[:1] + [100.8, 102.0] + FLAT[:4],
                                    {6: 100.2}), SPEC, [], "equity",
                              policy=PERMISSIVE_POLICY)
        self.assertEqual(row["exit_reason"], "stop")
        self.assertAlmostEqual(row["exit_reference"], 100.2, places=9)

    def test_the_gap_makes_the_simulated_result_worse(self):
        # The same series held to its time exit, versus one whose sixth bar
        # opens through the stop.  Stop-side gaps must cost money.
        clean = simulate_account(_bars(RISING + FLAT), [], SPEC,
                                 vehicle="equity", account_id="clean",
                                 risk_pct=.05, policy=PERMISSIVE_POLICY)
        gapped = simulate_account(_bars(RISING + FLAT, {6: 100.2}), [], SPEC,
                                  vehicle="equity", account_id="gapped",
                                  risk_pct=.05, policy=PERMISSIVE_POLICY)
        self.assertLess(gapped["rows"][0]["net_pnl"], clean["rows"][0]["net_pnl"])


class QuoteDrivenFillTests(unittest.TestCase):
    def test_quote_fill_record_preserves_executable_provenance(self):
        indexed = index_quotes([_quote(4, 100.0, 100.1)])
        record = quote_fill_record(indexed, symbol="SPY",
                                   at=BASE + timedelta(minutes=4), side="buy")
        self.assertIsNotNone(record)
        self.assertAlmostEqual(record.price, 100.1)
        self.assertEqual((record.feed, record.provider), ("sip", "test"))

    def test_the_latest_visible_quote_at_the_instant_wins(self):
        indexed = index_quotes([_quote(3, 99.0, 99.1), _quote(4, 100.0, 100.1),
                                _quote(5, 200.0, 200.1)])
        at = BASE + timedelta(minutes=4)
        self.assertAlmostEqual(quote_fill(indexed, symbol="SPY", at=at, side="buy"),
                               100.1, places=9)
        self.assertAlmostEqual(quote_fill(indexed, symbol="SPY", at=at, side="sell"),
                               100.0, places=9)

    def test_a_quote_not_yet_available_at_the_instant_is_not_used(self):
        # Timestamped at the fill instant but only observable a minute later.
        indexed = index_quotes([_quote(4, 100.0, 100.1, as_of_minute=5)])
        self.assertIsNone(quote_fill(indexed, symbol="SPY",
                                     at=BASE + timedelta(minutes=4), side="buy"))

    def test_a_quote_observed_after_its_event_is_not_used_until_observed(self):
        delayed = normalize_quote({
            "kind": "quote", "provider": "test", "feed": "sip", "symbol": "SPY",
            "timestamp": BASE.isoformat(), "as_of": BASE.isoformat(),
            "observed_at": (BASE + timedelta(minutes=1)).isoformat(),
            "bid": 100.0, "ask": 100.1,
        })
        indexed = index_quotes([delayed])
        self.assertIsNone(quote_fill(indexed, symbol="SPY", at=BASE,
                                     side="buy", max_age_seconds=180))
        self.assertEqual(quote_fill(indexed, symbol="SPY",
                                    at=BASE + timedelta(minutes=1),
                                    side="buy", max_age_seconds=180), 100.1)

    def test_absence_is_explicit_rather_than_an_invented_price(self):
        self.assertIsNone(quote_fill(None, symbol="SPY", at=BASE, side="buy"))
        self.assertIsNone(quote_fill(index_quotes([_quote(4, 100.0, 100.1)]),
                                     symbol="QQQ", at=BASE, side="buy"))


class SQLiteQuoteIndexTests(unittest.TestCase):
    """The bounded quote store must preserve point-in-time fill semantics."""

    def test_latest_visible_quote_matches_the_in_memory_index(self):
        visible = _quote(4, 100.0, 100.1)
        delayed = _quote(5, 101.0, 101.1, as_of_minute=6)
        expected = index_quotes([visible, delayed])
        index = SQLiteQuoteIndex()
        try:
            index.add(visible)
            index.add(delayed)
            at = BASE + timedelta(minutes=5)
            self.assertEqual(
                quote_fill(index, symbol="SPY", at=at, side="buy",
                           max_age_seconds=90),
                quote_fill(expected, symbol="SPY", at=at, side="buy",
                           max_age_seconds=90),
            )
            self.assertAlmostEqual(
                quote_fill(index, symbol="SPY", at=at, side="buy",
                           max_age_seconds=90), 100.1, places=9)
        finally:
            index.close()

    def test_observed_at_is_stored_and_enforced_by_disk_index(self):
        delayed = normalize_quote({
            "kind": "quote", "provider": "test", "feed": "sip", "symbol": "SPY",
            "timestamp": BASE.isoformat(), "as_of": BASE.isoformat(),
            "observed_at": (BASE + timedelta(minutes=1)).isoformat(),
            "bid": 100.0, "ask": 100.1,
        })
        index = SQLiteQuoteIndex()
        try:
            index.add(delayed)
            self.assertIsNone(quote_fill(index, symbol="SPY", at=BASE,
                                         side="buy", max_age_seconds=180))
            self.assertEqual(quote_fill(
                index, symbol="SPY", at=BASE + timedelta(minutes=1),
                side="buy", max_age_seconds=180), 100.1)
        finally:
            index.close()

    def test_an_entry_uses_the_recorded_ask_and_records_the_source(self):
        bars = _bars(RISING + FLAT)
        quoted = _simulate_trade(bars, SPEC, [], "equity",
                                 quotes=index_quotes([_quote(4, 100.70, 100.90)]),
                                 policy=PERMISSIVE_POLICY)
        self.assertEqual(quoted["entry_fill_source"], QUOTE)
        self.assertAlmostEqual(quoted["entry_reference"], 100.90, places=9)
        # A short thesis would lift the bid instead; the long path is enough to
        # prove the executable side is chosen rather than a mid.
        self.assertNotEqual(quoted["entry_reference"], bars[4].open)

    def test_a_missing_quote_falls_back_to_the_bar_and_says_so(self):
        row = _simulate_trade(_bars(RISING + FLAT), SPEC, [], "equity",
                              policy=PERMISSIVE_POLICY)
        self.assertEqual(row["entry_fill_source"], BAR)
        self.assertEqual(row["exit_fill_source"], BAR)
        self.assertAlmostEqual(row["entry_reference"], 100.8, places=9)

    def test_omitted_policy_requires_a_fresh_equity_quote(self):
        row = _simulate_trade(_bars(RISING + FLAT), SPEC, [], "equity")
        self.assertEqual(row["unpriced_reason"],
                         "no fresh equity quote at entry")

    def test_an_intrabar_level_exit_keeps_the_bar_because_it_has_no_instant(self):
        # The stop triggers somewhere inside bar 5; no quote is at that instant,
        # so a quote recorded at the boundary must not be used as the fill.
        row = _simulate_trade(_bars(RISING + [100.8, 100.4] + FLAT[:5]), SPEC, [],
                              "equity", quotes=index_quotes([_quote(6, 90.0, 90.1)]),
                              policy=PERMISSIVE_POLICY)
        self.assertEqual(row["exit_reason"], "stop")
        self.assertEqual(row["exit_fill_source"], BAR)
        self.assertAlmostEqual(row["exit_reference"], row["stop_price"], places=9)

    def test_a_quoted_fill_is_not_charged_a_modelled_spread_twice(self):
        bars = _bars(RISING + FLAT)
        quotes = [_quote(4, 100.79, 100.81), _quote(8, 100.79, 100.81)]
        book = simulate_account(bars, [], SPEC, vehicle="equity", risk_pct=.05,
                                account_id="quoted", quotes=quotes,
                                policy=PERMISSIVE_POLICY)
        row = book["rows"][0]
        model = CostModel()
        self.assertEqual(row["entry_fill_source"], QUOTE)
        self.assertAlmostEqual(
            row["entry_price"],
            100.81 * (1 + model.slippage_bps / 10_000.0), places=9)

    def test_dynamic_cost_resolver_prices_entry_and_exit_at_their_own_times(self):
        calls = []

        def resolver(context):
            leg = str(context.get("cost_leg") or "mark")
            calls.append((leg, context.get("cost_timestamp")))
            return CostModel(
                spread_bps=2.0 if leg == "entry" else 8.0,
                slippage_bps=0.0, fee_bps=0.0,
                provenance=f"measured:{leg}")

        book = simulate_account(
            _bars(RISING + FLAT), [], SPEC, vehicle="equity",
            account_id="dynamic-cost", risk_pct=.05,
            policy=PERMISSIVE_POLICY, cost_resolver=resolver)
        row = book["rows"][0]
        self.assertEqual([leg for leg, _stamp in calls], ["entry", "exit"])
        self.assertEqual(row["entry_cost_model_provenance"], "measured:entry")
        self.assertEqual(row["exit_cost_model_provenance"], "measured:exit")
        self.assertEqual(calls[0][1], row["entry_timestamp"])
        self.assertEqual(calls[1][1], row["exit_timestamp"])


class NullControlSharesTheFillModelTests(unittest.TestCase):
    """The chance-entry null must not get exit realism the candidate lacks."""

    def _null(self, bars):
        reference = simulate_account(bars, [], SPEC, vehicle="equity",
                                     account_id="reference", risk_pct=.05,
                                     policy=PERMISSIVE_POLICY)["rows"]
        # The entry bar is drawn from a seed derived from account, spec and
        # session set, so two same-shaped series draw the same bar.
        return null_control_account(bars, [], SPEC, vehicle="equity",
                                    reference_rows=reference, account_id="null",
                                    risk_pct=.05, policy=PERMISSIVE_POLICY)

    # The null enters at bar 9 and its stop sits at 100.50.  Bar 10 reaches
    # that stop either way; only the fill differs.
    STOPPED = RISING + FLAT[:6] + [100.5, 100.8]

    def test_a_gap_through_the_null_stop_costs_it_money(self):
        touched = self._null(_bars(self.STOPPED))
        gapped = self._null(_bars(self.STOPPED, {10: 99.0}))
        self.assertLess(gapped["ending_equity"], touched["ending_equity"])

    def test_the_null_is_priced_by_the_shared_cost_model(self):
        bars = _bars(RISING + FLAT)
        reference = simulate_account(bars, [], SPEC, vehicle="equity",
                                     account_id="reference", risk_pct=.05,
                                     policy=PERMISSIVE_POLICY)["rows"]
        free = null_control_account(
            bars, [], SPEC, vehicle="equity", reference_rows=reference,
            account_id="null", risk_pct=.05,
            costs=CostModel(spread_bps=0, slippage_bps=0, fee_bps=0),
            policy=PERMISSIVE_POLICY)
        priced = null_control_account(
            bars, [], SPEC, vehicle="equity", reference_rows=reference,
            account_id="null", risk_pct=.05, policy=PERMISSIVE_POLICY)
        self.assertGreater(free["ending_equity"], priced["ending_equity"])

    def test_candidate_and_null_share_the_stressed_cost_veto(self):
        bars = _bars(RISING + FLAT)
        # Build the null reference without the runtime veto so it has the same
        # authored geometry the candidate would present to RiskEngine.
        reference = simulate_account(
            bars, [], SPEC, vehicle="equity", account_id="reference-stress",
            risk_pct=.05, policy=PERMISSIVE_POLICY)["rows"]
        stressed = ReplayPolicy(
            strict_market_data=False, risk_per_trade_pct=.05,
            stressed_cost_scenario_bps=25.0,
            max_stressed_cost_to_risk_ratio=.30)
        candidate = simulate_account(
            bars, [], SPEC, vehicle="equity", account_id="candidate-stress",
            risk_pct=.05, policy=stressed)
        null = null_control_account(
            bars, [], SPEC, vehicle="equity", reference_rows=reference,
            account_id="null-stress", risk_pct=.05, policy=stressed)
        self.assertEqual(candidate["rows"][0]["reject_reason"],
                         "stressed_cost_risk_limit")
        null_rows = [row for row in null["rows"]
                     if row.get("no_trade") is True]
        self.assertTrue(null_rows)
        self.assertTrue(any(row.get("reject_reason") ==
                            "stressed_cost_risk_limit" for row in null_rows))


class NotionalCapAnchorTests(unittest.TestCase):
    """Research and the runtime must cap on the same price."""

    def _runtime_cap_shares(self, equity, plan_entry):
        risk = RiskEngine({"risk": {"max_position_notional_pct": NOTIONAL_CAP_PCT}})
        # A budget large enough that the notional cap, not the risk term, binds.
        return risk.size_shares(equity=equity, entry_price=plan_entry,
                                stop_distance=.3, risk_usd=1e9)["shares"]

    def test_a_gapped_entry_caps_on_the_plan_price_like_the_runtime(self):
        book = simulate_account(_bars(RISING + FLAT, {4: 101.0}), [], SPEC,
                                vehicle="equity", account_id="cap", risk_pct=50.0,
                                policy=PERMISSIVE_POLICY)
        row = book["rows"][0]
        self.assertAlmostEqual(row["plan_entry"], 100.8, places=9)
        self.assertAlmostEqual(row["underlying_entry"], 101.0, places=9)
        # The runtime sizes at plan time from the signal close; it cannot see
        # the gap, so capping on the fill would size a different position.
        self.assertEqual(row["quantity"],
                         self._runtime_cap_shares(100_000.0, row["plan_entry"]))
        self.assertNotEqual(row["quantity"],
                            self._runtime_cap_shares(100_000.0, row["underlying_entry"]))

    def test_the_cap_percentage_matches_the_runtime_risk_block(self):
        book = simulate_account(_bars(RISING + FLAT), [], SPEC, vehicle="equity",
                                account_id="cap-pct", risk_pct=50.0,
                                policy=PERMISSIVE_POLICY)
        row = book["rows"][0]
        self.assertEqual(row["quantity"],
                         int(100_000.0 * NOTIONAL_CAP_PCT / 100.0 / row["plan_entry"]))


class CalibrationTests(unittest.TestCase):
    """Measure the model against what the broker actually filled."""

    def _journal(self, fills):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE trades (ts REAL, symbol TEXT, side TEXT, "
                   "action TEXT, qty REAL, price REAL, notional REAL, "
                   "order_id TEXT, runtime_mode TEXT, variant_id TEXT)")
        db.execute("CREATE TABLE orders (order_id TEXT, qty REAL)")
        for index, (fill, reference, qty) in enumerate(fills):
            order = f"order-{index}"
            db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (float(index), "SPY", "buy", "open", qty, fill,
                        reference * qty, order, "paper", "v1"))
            db.execute("INSERT INTO orders VALUES (?,?)", (order, qty))
        return db

    def test_a_conservative_model_is_reported_as_conservative(self):
        # Two bps paid against an eight bps expectation (4 bps spread / 6 bps
        # adverse slippage, with half the spread charged per side).
        db = self._journal([(100.02, 100.0, 10)] * calibration.MIN_FILLS)
        report = calibration.json_report(db)
        self.assertEqual(report["referenced_fills"], calibration.MIN_FILLS)
        self.assertAlmostEqual(report["observed_mean_bps"], 2.0, places=6)
        self.assertAlmostEqual(report["expected_entry_cost_bps"], 8.0, places=6)
        self.assertAlmostEqual(report["bias_bps"], 6.0, places=6)
        self.assertEqual(report["within_model_rate"], 1.0)
        self.assertEqual(report["over_runtime_cap"], 0)
        self.assertEqual(report["verdict"], "conservative")

    def test_an_optimistic_model_is_named_rather_than_absorbed(self):
        db = self._journal([(100.10, 100.0, 10)] * calibration.MIN_FILLS)
        report = calibration.json_report(db)
        self.assertAlmostEqual(report["observed_mean_bps"], 10.0, places=6)
        self.assertLess(report["bias_bps"], 0)
        self.assertEqual(report["within_model_rate"], 0.0)
        self.assertEqual(report["verdict"], "optimistic")

    def test_a_fill_past_the_runtime_cap_is_reported_separately(self):
        # The pre-trade slippage check accepted it; the fill disagreed.
        rows = [(100.02, 100.0, 10)] * calibration.MIN_FILLS + [(101.0, 100.0, 10)]
        report = calibration.json_report(self._journal(rows))
        self.assertEqual(report["over_runtime_cap"], 1)
        self.assertAlmostEqual(report["worst_bps"], 100.0, places=6)

    def test_the_reference_survives_a_partial_fill(self):
        # 10 shares planned, 4 filled: the plan price is notional/planned, and
        # dividing by the filled quantity would invent a 150 bps cost.
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE trades (ts REAL, symbol TEXT, side TEXT, "
                   "action TEXT, qty REAL, price REAL, notional REAL, "
                   "order_id TEXT, runtime_mode TEXT, variant_id TEXT)")
        db.execute("CREATE TABLE orders (order_id TEXT, qty REAL)")
        db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (1.0, "SPY", "buy", "open", 4.0, 100.02, 1000.0, "o1",
                    "paper", "v1"))
        db.execute("INSERT INTO orders VALUES ('o1', 10.0)")
        fills = calibration.load_entry_fills(db)
        report = calibration.calibrate(fills)
        self.assertAlmostEqual(report["observed_mean_bps"], 2.0, places=6)

    def test_a_sell_side_entry_measures_the_adverse_direction(self):
        db = self._journal([(100.02, 100.0, 10)])
        db.execute("UPDATE trades SET side = 'sell'")
        report = calibration.calibrate(calibration.load_entry_fills(db))
        # Selling above the reference is a favourable fill, not a cost.
        self.assertAlmostEqual(report["observed_mean_bps"], -2.0, places=6)

    def test_an_unreferenced_fill_is_counted_not_guessed(self):
        db = self._journal([(100.02, 100.0, 10)])
        db.execute("UPDATE trades SET notional = NULL")
        report = calibration.calibrate(calibration.load_entry_fills(db))
        self.assertEqual(report["referenced_fills"], 0)
        self.assertEqual(report["unreferenced_fills"], 1)
        self.assertIsNone(report["observed_mean_bps"])
        self.assertEqual(report["verdict"], "insufficient_data")

    def test_a_thin_sample_refuses_to_issue_a_verdict(self):
        report = calibration.json_report(self._journal([(100.10, 100.0, 10)] * 3))
        self.assertEqual(report["referenced_fills"], 3)
        self.assertEqual(report["verdict"], "insufficient_data")
        self.assertEqual(report["authorization_exit_code"], 2)

    def test_an_empty_journal_is_insufficient_data_not_a_pass(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        report = calibration.json_report(db)
        self.assertEqual(report["journal_fills"], 0)
        self.assertEqual(report["verdict"], "insufficient_data")
        self.assertEqual(report["authorization_exit_code"], 2)

    def test_the_report_names_the_model_it_scored(self):
        db = self._journal([(100.02, 100.0, 10)])
        model = CostModel(spread_bps=1.0, slippage_bps=1.0)
        self.assertEqual(calibration.json_report(db, model)["model"],
                         model.as_dict())

    def _evidence_journal(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute(
            "CREATE TABLE trades (ts REAL, symbol TEXT, side TEXT, action TEXT, "
            "qty REAL, price REAL, notional REAL, order_id TEXT, runtime_mode TEXT, "
            "variant_id TEXT, execution_profile TEXT, vehicle TEXT, "
            "reference_price REAL, entry_reference REAL, exit_reference REAL, "
            "market_price REAL, mid_price REAL, requested_qty REAL, planned_qty REAL, "
            "cumulative_filled_qty REAL, fill_fraction REAL)")
        db.execute(
            "CREATE TABLE orders (ts REAL, order_id TEXT, qty REAL, status TEXT, "
            "filled_qty REAL, requested_qty REAL, planned_qty REAL, "
            "execution_profile TEXT, vehicle TEXT, reference_price REAL, "
            "entry_reference REAL, exit_reference REAL)")
        return db

    def test_incremental_rows_are_one_referenced_order_and_partial_cancel_vetoes(self):
        db = self._evidence_journal()
        db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (3, "o1", 10, "canceled", 4, 10, 10, "shares", "equity", 100, 100, None))
        for ts, qty, price, cumulative in ((1, 2, 100.01, 2), (2, 2, 100.03, 4)):
            db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (ts, "SPY", "buy", "open", qty, price, qty * price, "o1", "paper",
                        "v1", "shares", "equity", 100, 100, None, None, None, 10, 10,
                        cumulative, cumulative / 10))
        db.commit()
        report = calibration.json_report(db)
        self.assertEqual(report["journal_fills"], 2)
        self.assertEqual(report["unique_orders"], 1)
        self.assertEqual(report["referenced_fills"], 1)
        self.assertEqual(report["cumulative_filled_qty"], 4)
        self.assertEqual(report["partial_cancel_orders"], 1)
        self.assertEqual(report["authorization_exit_code"], 2)
        self.assertEqual(report["authorization_verdict"], "veto_underfilled_execution")

    def test_migrated_rows_without_explicit_reference_are_insufficient(self):
        db = self._evidence_journal()
        db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (1, "legacy", 10, "filled", 10, 10, 10, "shares", "equity", None, None, None))
        db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (1, "SPY", "buy", "open", 10, 100.02, 1000.2, "legacy", "paper",
                    "v1", "shares", "equity", None, None, None, None, None, 10, 10, 10, 1))
        db.commit()
        report = calibration.json_report(db)
        self.assertEqual(report["referenced_fills"], 0)
        self.assertEqual(report["verdict"], "insufficient_data")
        self.assertEqual(report["authorization_verdict"], "insufficient_data")

    def test_inflight_partial_status_is_reported_not_penalized(self):
        db = self._evidence_journal()
        db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (1, "open", 10, "partially_filled", 4, 10, 10, "shares", "equity", 100, 100, None))
        db.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (1, "SPY", "buy", "open", 4, 100.02, 400.08, "open", "paper",
                    "v1", "shares", "equity", 100, 100, None, None, None, 10, 10, 4, .4))
        db.commit()
        report = calibration.json_report(db)
        self.assertEqual(report["inflight_orders"], 1)
        self.assertEqual(report["terminal_orders"], 0)
        self.assertFalse(report["partial_execution_veto"])
        # In-flight evidence is diagnostic, but cannot authorize promotion
        # until the sample reaches the calibration floor.
        self.assertEqual(report["authorization_exit_code"], 2)


if __name__ == "__main__":
    unittest.main()
