"""Gate protocol 2 (review D2, October 2026) and protocol-1 compatibility.

Each finding of the D2 review has a test here that fails on the code the
review read and passes on the versioned protocol.  The stored fixture
``fixtures/gate_envelope_protocol_v1.json.gz`` is a passing envelope produced
by that original code; it must keep verifying under protocol 1.
"""

import copy
import gzip
import json
import math
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from research import gates, strategy_factory
from research.gates import (
    FALSIFICATION_INDEPENDENT_METHOD, GATE_PROTOCOL_VERSION,
    LEGACY_GATE_PROTOCOL_VERSION, _content_hash, envelope_protocol_version,
    expectancy_rejection_report, falsification_gate, fdr_batch_evidence,
    gate_check_classification, heldout_separation, matched_cluster_test,
    max_drawdown_of, opportunity_comparison_id, paired_control_adequacy,
    performance_floor, placebo_null_distribution, protocol_power_report,
    protocol_walk_forward_minimums, qualification_report,
    recompute_gate_statistics, structural_floor, verified_gate_envelope,
    verify_gate_envelope, walk_forward_report,
)
from research.stats import benjamini_yekutieli, stable_seed

FIXTURE = Path(__file__).parent / "fixtures" / "gate_envelope_protocol_v1.json.gz"
# Content hash of the fixture as written by the original (pre-versioning)
# gates code at commit 352fb70.
V1_FIXTURE_HASH = "7ae5a703bd95e95a6158bdddfcc3f09ac40aca0e5c3336019f8826447147d783"
SYMBOLS = ("SPY", "QQQ", "IWM", "DIA")
CANDIDATE = "fixture-v1-candidate"


def _rehash(envelope):
    envelope["content_hash"] = _content_hash({
        key: value for key, value in envelope.items() if key != "content_hash"})
    return envelope


def _row(symbol, session, opportunity, net_pnl, *, minute=35, **extra):
    return {
        "vehicle": "equity", "symbol": symbol, "session_date": session,
        "opportunity_id": opportunity, "net_pnl": float(net_pnl),
        "entry_timestamp": f"{session}T14:{minute:02d}:00+00:00",
        "exit_timestamp": f"{session}T15:{minute:02d}:00+00:00",
        "entry_price": 100.0, "exit_price": 101.0, "quantity": 1.0,
        "risk_usd": 1.0, "no_trade": False,
        "entry_fill_source": "quote", "exit_fill_source": "quote",
        "entry_feed": "iex", "exit_feed": "iex",
        "entry_provider": "alpaca", "exit_provider": "alpaca",
        "entry_quote_age_seconds": 0.0, "exit_quote_age_seconds": 0.0,
        **extra,
    }


def _sessions(start, count):
    return [(start + timedelta(days=offset)).isoformat() for offset in range(count)]


def _arm(days, prefix, value, **extra):
    return [_row(symbol, day, f"{prefix}-{day}-{symbol}",
                 value(index, symbol), **extra)
            for index, day in enumerate(days) for symbol in SYMBOLS]


def _evidence(*, protocol_version, held_value=None, fit_value=None,
              qual_value=None, protocol_walk=False, null_duplicate=True,
              heldout_extra=(), row_extra=None, arm_extra=None):
    """Inputs of the stored fixture, rebuilt for either protocol.

    With the defaults and ``protocol_version=1`` this reproduces exactly the
    arguments the original code was given when it wrote the fixture.
    ``row_extra`` adds fields to every row and ``arm_extra`` to one arm
    (``held``, ``qual`` and so on).
    """
    fit_days = _sessions(date(2025, 1, 2), 30)
    held_days = _sessions(date(2025, 3, 1), 30)
    qual_days = _sessions(date(2025, 5, 1), 30)
    if held_value is None:
        held_value = (lambda index, symbol: -2.0 if index in (10, 11) else 2.0)
    zero = (lambda index, symbol: 0.0)

    def arm(days, prefix, value):
        extra = {**(row_extra or {}), **((arm_extra or {}).get(prefix) or {})}
        return _arm(days, prefix, value, **extra)

    fit = arm(fit_days, "fit", fit_value or (lambda index, symbol: 1.5))
    heldout = arm(held_days, "held", held_value) + list(heldout_extra)
    fit_baseline = arm(fit_days, "fit-base", zero)
    baseline = arm(held_days, "held-base", zero)
    null = arm(held_days, "null", zero)
    if null_duplicate:
        null.append(_row("SPY", held_days[0], "null-duplicate", 0.0, minute=50))
    qual = arm(qual_days, "qual", qual_value or (lambda index, symbol: 2.0))
    qual_base = arm(qual_days, "qual-base", zero)
    version = {"protocol_version": protocol_version}
    fit_floor = structural_floor(fit, vehicle="equity", min_trades=100,
                                 min_sessions=30, min_clusters=30)
    held_floor = structural_floor(heldout, vehicle="equity", min_trades=100,
                                  min_sessions=30, min_clusters=30)
    control = matched_cluster_test(heldout, baseline, vehicle="equity", **version)
    fit_control = matched_cluster_test(fit, fit_baseline, vehicle="equity", **version)
    null_test = matched_cluster_test(heldout, null, vehicle="equity", **version)
    placebo = placebo_null_distribution(heldout, baseline, vehicle="equity")
    independent_seed = stable_seed({
        "purpose": "independent_placebo_null_tail.v1",
        "primary_assignments_hash": placebo["assignments_hash"],
        "draws": int(placebo["draws"]),
    })
    independent = placebo_null_distribution(
        heldout, baseline, vehicle="equity", draws=int(placebo["draws"]),
        seed=independent_seed)
    independent_result = falsification_gate(
        independent["observed"], independent["placebo"], alpha=.05)
    falsification = {
        **falsification_gate(
            placebo["observed"], placebo["placebo"], alpha=.05,
            preregistered_p_value=control["p_value"],
            independent_p_value=independent_result["p_value"],
            independent_method=FALSIFICATION_INDEPENDENT_METHOD,
            independent_result_hash=independent["assignments_hash"],
            require_independent=True),
        "method": placebo["method"],
        "assignments_hash": placebo["assignments_hash"],
        "observations": len(placebo["observed"]),
        "draws": int(placebo["draws"]), "seed": int(placebo["seed"]),
        "clusters": int(placebo["cluster_count"]),
        "primary_p_value": float(control["p_value"]),
        "independent_method": FALSIFICATION_INDEPENDENT_METHOD,
        "independent_result_hash": independent["assignments_hash"],
        "independent_assignments_hash": independent["assignments_hash"],
        "independent_draws": int(independent["draws"]),
        "independent_seed": int(independent["seed"]),
    }
    absolute = performance_floor(heldout, vehicle="equity")
    if protocol_walk:
        minimums = protocol_walk_forward_minimums("backtest")
        walk = walk_forward_report(
            heldout, baseline, vehicle="equity", folds=minimums["folds"],
            min_test_sessions=minimums["test_sessions_required"],
            min_test_trades=minimums["test_trades_required"],
            requested_min_sessions=minimums["requested_min_sessions"])
    else:
        walk = walk_forward_report(heldout, baseline, vehicle="equity", folds=3)
    retirement = expectancy_rejection_report(heldout, vehicle="equity")
    negative = [item for item in walk["results"]
                if item.get("adequate") and float(item.get("net_pnl", 0.0)) <= 0.0]
    retirement.update({
        "negative_forward_folds": len(negative),
        "independent_negative_windows": [
            list(item.get("test_sessions") or ()) for item in negative],
        "multi_window_negative": len(negative) >= 2,
    })
    qualification = qualification_report(
        qual, qual_base, vehicle="equity", sessions=qual_days,
        candidate_id=CANDIDATE, preselected=True, max_drawdown=5_000.0)
    batch = fdr_batch_evidence(
        candidate_id=CANDIDATE, family_name="fixture",
        family_candidate_key=CANDIDATE, global_candidate_key=CANDIDATE,
        family_values={"fixture": {CANDIDATE: control["p_value"],
                                   "sibling": .5}},
        global_values={CANDIDATE: control["p_value"], "sibling": .5},
        alpha=.05, p_value_source="gate")
    family_q = batch["family_results"]["fixture"][CANDIDATE]["p_adjusted"]
    global_q = batch["global_results"][CANDIDATE]["p_adjusted"]
    null_control = {**null_test, "kind": "randomized_entry_null",
                    "available": bool(null_test["available"] and
                                      null_test.get("adequate")),
                    "p_value": float(null_test["p_value"])}
    lcb = control["mean_delta_lcb"]
    checks = {
        "family_fdr_significant": family_q <= .05,
        "global_fdr_significant": global_q <= .05,
        "cumulative_fdr_significant": True,
        "falsification": bool(falsification["passes"]),
        "heldout_net_pnl_positive": bool(absolute["net_pnl_positive"]),
        "heldout_expectancy_positive": bool(absolute["expectancy_positive"]),
        "heldout_delta_lcb_positive": bool(lcb is not None and lcb > 0),
    }
    return dict(
        lane="backtest", vehicle="equity", fit=fit, heldout=heldout,
        fit_baseline=fit_baseline, heldout_baseline=baseline,
        null_source=null,
        fit_floor=fit_floor, heldout_floor=held_floor,
        fit_control=fit_control,
        control={**control, "kind": "matched_root_baseline"},
        p_value=control["p_value"], q_value=global_q,
        family_q_value=family_q, alpha=.05, fdr_batch=batch,
        falsification=falsification,
        separation=heldout_separation(fit, heldout),
        checks=checks, passes=True, walk_forward=walk,
        retirement=retirement, qualification=qualification,
        null_control=null_control,
        online_fdr={"required": False, "status": "deferred_to_live_shadow",
                    "tested": False, "decision": False},
        provenance={"dataset_hash": "d" * 64, "config_hash": "c" * 64,
                    "code_hash": "e" * 64, "provenance_hash": "f" * 64},
        candidate_id=CANDIDATE,
        performance={"heldout_delta": control["mean_delta"],
                     "heldout_r_delta": control.get("mean_r_delta"),
                     "heldout_delta_lcb": lcb,
                     "heldout_net_pnl": absolute["net_pnl"],
                     "heldout_expectancy": absolute["expectancy"],
                     "max_drawdown": max_drawdown_of([*fit, *heldout])},
        **version)


def _passing_v2_inputs(**changes):
    """Evidence that meets every protocol-2 requirement."""
    options = {"held_value": (lambda index, symbol: 2.0),
               "protocol_walk": True, "null_duplicate": False}
    options.update(changes)
    return _evidence(protocol_version=GATE_PROTOCOL_VERSION, **options)


def _failed(envelope):
    return sorted(name for name, ok in envelope["checks"].items() if not ok)


class GateProtocolVersioningTests(unittest.TestCase):
    """Stored envelopes re-verify under the protocol that produced them."""

    @classmethod
    def setUpClass(cls):
        with gzip.open(FIXTURE, "rt", encoding="utf-8") as handle:
            cls.fixture = json.load(handle)

    def test_protocol_1_fixture_from_the_original_code_still_verifies(self):
        envelope = copy.deepcopy(self.fixture)
        self.assertEqual(envelope["content_hash"], V1_FIXTURE_HASH)
        self.assertNotIn("gate_protocol_version", envelope)
        self.assertEqual(envelope_protocol_version(envelope),
                         LEGACY_GATE_PROTOCOL_VERSION)
        self.assertTrue(envelope["passes"])
        self.assertTrue(verify_gate_envelope(envelope))
        recomputed = recompute_gate_statistics(envelope)
        self.assertEqual(recomputed["p_value"], envelope["statistics"]["p_value"])

    def test_protocol_1_builder_reproduces_the_original_envelope_exactly(self):
        rebuilt = verified_gate_envelope(
            **_evidence(protocol_version=LEGACY_GATE_PROTOCOL_VERSION))
        self.assertEqual(rebuilt["content_hash"], V1_FIXTURE_HASH)
        self.assertEqual(rebuilt, self.fixture)

    def test_protocol_1_evidence_relabelled_as_protocol_2_is_rejected(self):
        # The fixture deliberately carries a 16R held-out drawdown, a
        # walk-forward below the protocol, a duplicated null key and a Monte
        # Carlo p at its floor.  Protocol 1 accepts all of them; a verifier
        # that ignored the recorded version would have to reject them.
        relabelled = _rehash({**copy.deepcopy(self.fixture),
                              "gate_protocol_version": GATE_PROTOCOL_VERSION})
        self.assertFalse(verify_gate_envelope(relabelled))
        explicit_v1 = _rehash({**copy.deepcopy(self.fixture),
                               "gate_protocol_version": 1})
        self.assertFalse(verify_gate_envelope(explicit_v1))
        unknown = _rehash({**copy.deepcopy(self.fixture),
                           "gate_protocol_version": 99})
        self.assertFalse(verify_gate_envelope(unknown))

        current = verified_gate_envelope(
            **_evidence(protocol_version=GATE_PROTOCOL_VERSION))
        self.assertFalse(current["passes"])
        self.assertEqual(_failed(current), [
            "comparison_keys_unique", "max_drawdown_supported",
            "protocol_parameters_bound"])
        self.assertTrue(verify_gate_envelope(current))

    def test_a_fully_compliant_protocol_2_envelope_passes_and_verifies(self):
        envelope = verified_gate_envelope(**_passing_v2_inputs())
        self.assertEqual(_failed(envelope), [])
        self.assertTrue(envelope["passes"])
        self.assertEqual(envelope["gate_protocol_version"], GATE_PROTOCOL_VERSION)
        self.assertTrue(verify_gate_envelope(envelope))


class CostStressTests(unittest.TestCase):
    """D2-01: a relative, lower-bound cost stress on unseen rows only."""

    @staticmethod
    def _realistic(*, net=lambda index, symbol: 2.0 + .1 * ((index % 3) - 1),
                   qual_net=None, fit_net=None, arm_extra=None):
        # 10,000 USD entries: +2 bps net per trade, a 30 bps stop.
        return _passing_v2_inputs(
            held_value=net, qual_value=qual_net or net,
            fit_value=fit_net or (lambda index, symbol: 2.0),
            row_extra={"quantity": 100.0, "risk_usd": 30.0},
            arm_extra=arm_extra)

    def test_relative_veto_admits_a_realistic_edge_the_25bps_veto_rejects(self):
        envelope = verified_gate_envelope(
            **self._realistic(), expected_round_trip_bps={"SPY": 3.0, "QQQ": 3.0,
                                                          "IWM": 4.0, "DIA": 4.0})
        stress = envelope["cost_stress"]
        self.assertEqual(stress["veto"], "relative_expected_round_trip")
        self.assertTrue(envelope["checks"]["cost_stress_adequate"])
        relative = stress["relative_veto"]
        self.assertEqual(relative["multiple"], 2.0)
        self.assertGreater(relative["lower_bound"], 0.0)
        self.assertEqual(relative["partitions"],
                         {"heldout": 120, "qualification": 120})
        # 25 and 50 bps remain persisted diagnostics, and they fail.
        by_bps = {item["entry_notional_bps"]: item for item in stress["scenarios"]}
        self.assertFalse(by_bps[25.0]["positive"])
        self.assertFalse(by_bps[50.0]["positive"])
        self.assertFalse(stress["entry_notional_bps_adequate"])
        self.assertTrue(verify_gate_envelope(envelope))
        # The declared round trip is part of the verified evidence.
        tampered = copy.deepcopy(envelope)
        tampered["cost_stress"]["relative_veto"]["expected_round_trip_bps"]["SPY"] = 40.0
        self.assertFalse(verify_gate_envelope(_rehash(tampered)))

    def test_a_declared_round_trip_cannot_fall_below_the_code_owned_floor(self):
        # Review of D2-01: a producer (row or caller) declaring a near-zero
        # round trip used to reduce the veto to "lower bound of net P&L > 0".
        # Every row is floored at the per_symbol_v2 round trip at its price.
        from research.costs import PerSymbolCostModel
        tiny = verified_gate_envelope(
            **self._realistic(), expected_round_trip_bps=1e-9)
        relative = tiny["cost_stress"]["relative_veto"]
        self.assertEqual(relative["expected_round_trip_sources"]["floor"], 240)
        floor = PerSymbolCostModel().round_trip("SPY", 100.0)["total_bps"]
        self.assertGreater(relative["mean_expected_round_trip_bps"], floor * .9)
        self.assertTrue(verify_gate_envelope(tiny))
        # A row cannot override a larger caller value either.
        overridden = verified_gate_envelope(
            **self._realistic(arm_extra={"held": {"expected_round_trip_bps": 1e-9},
                                         "qual": {"expected_round_trip_bps": 1e-9}}),
            expected_round_trip_bps=50.0)
        relative = overridden["cost_stress"]["relative_veto"]
        self.assertEqual(relative["expected_round_trip_sources"]["scalar"], 240)
        self.assertFalse(overridden["checks"]["cost_stress_adequate"])

    def test_relative_veto_charges_a_multiple_of_the_round_trip_above_cost(self):
        # A 10 bps expected round trip stressed twice (20 bps, 20 USD) is
        # above the recorded static cost, so the increment bites and a +2 bps
        # edge fails.
        envelope = verified_gate_envelope(
            **self._realistic(), expected_round_trip_bps=10.0)
        relative = envelope["cost_stress"]["relative_veto"]
        self.assertFalse(envelope["checks"]["cost_stress_adequate"])
        self.assertLess(relative["expectancy"], 0.0)
        self.assertTrue(verify_gate_envelope(envelope))

    def test_relative_veto_reads_heldout_and_qualification_rows_only(self):
        # A losing fit window is irrelevant; a losing sealed window is not.
        inputs = self._realistic(fit_net=lambda index, symbol: -50.0)
        envelope = verified_gate_envelope(**inputs, expected_round_trip_bps=1.0)
        self.assertTrue(envelope["checks"]["cost_stress_adequate"])
        losing = verified_gate_envelope(
            **self._realistic(qual_net=lambda index, symbol: -1.0),
            expected_round_trip_bps=1.0)
        self.assertFalse(losing["checks"]["cost_stress_adequate"])

    def test_relative_veto_needs_a_positive_lower_bound_not_a_positive_sum(self):
        # Mean +0.5 USD per trade, but one large loss per four sessions keeps
        # the session-clustered lower bound below zero.
        noisy = (lambda index, symbol: -30.0 if index % 4 == 0 else 11.0)
        envelope = verified_gate_envelope(
            **self._realistic(net=noisy), expected_round_trip_bps=1.0)
        relative = envelope["cost_stress"]["relative_veto"]
        self.assertGreater(relative["net_pnl"], 0.0)
        self.assertLessEqual(relative["lower_bound"], 0.0)
        self.assertFalse(envelope["checks"]["cost_stress_adequate"])

    def test_row_level_round_trip_from_the_cost_model_activates_the_veto(self):
        round_trip = {"expected_round_trip_bps": 3.0}
        envelope = verified_gate_envelope(
            **self._realistic(arm_extra={"held": round_trip}))
        relative = envelope["cost_stress"]["relative_veto"]
        # Qualification rows carry no round trip, so they are missing and
        # the veto fails closed rather than silently passing on half the rows.
        self.assertEqual(relative["expected_round_trip_sources"]["row"], 120)
        self.assertEqual(len(relative["missing_opportunities"]), 120)
        self.assertFalse(envelope["checks"]["cost_stress_adequate"])
        self.assertTrue(verify_gate_envelope(envelope))
        complete = verified_gate_envelope(**self._realistic(
            arm_extra={"held": round_trip, "qual": round_trip}))
        self.assertTrue(complete["checks"]["cost_stress_adequate"])
        self.assertTrue(verify_gate_envelope(complete))

    def test_without_a_round_trip_protocol_2_falls_back_to_the_25bps_veto(self):
        inputs = self._realistic()
        envelope = verified_gate_envelope(**inputs)
        stress = envelope["cost_stress"]
        self.assertEqual(stress["veto"], "entry_notional_bps")
        self.assertIsNone(stress["relative_veto"])
        legacy = verified_gate_envelope(
            **{**inputs, "protocol_version": LEGACY_GATE_PROTOCOL_VERSION,
               "control": {**inputs["control"]}})
        self.assertEqual(stress["adequate"], legacy["cost_stress"]["adequate"])
        self.assertEqual(stress["scenarios"], legacy["cost_stress"]["scenarios"])
        self.assertFalse(stress["adequate"])


class FactoryLaneFloorTests(unittest.TestCase):
    """D2-02: each factory lane carries its own protocol floor."""

    def test_forward_lane_floors_are_derived_from_the_shadow_protocol(self):
        floors = strategy_factory._lane_protocol_floors(100, 30)
        self.assertEqual(floors["backtest"], {"trades": 100, "sessions": 30})
        self.assertEqual(floors["shadow"], {"trades": 150, "sessions": 30})
        self.assertEqual(strategy_factory._lane_protocol_floors(200, 40)["shadow"],
                         {"trades": 200, "sessions": 40})
        # The derived floor is what the shadow-lane envelope declares, and
        # only that floor meets the protocol the envelope is verified against.
        held = _arm(_sessions(date(2025, 3, 1), 40), "held",
                    lambda index, symbol: 1.0)
        for trades, meets in ((100, False), (floors["shadow"]["trades"], True)):
            report = structural_floor(held, vehicle="equity", min_trades=trades,
                                      min_sessions=30, min_clusters=30)
            self.assertEqual(gates._floor_minimums_meet(report, lane="shadow"), meets)

    def test_factory_start_up_fails_fast_when_a_lane_cannot_meet_its_protocol(self):
        with mock.patch.object(strategy_factory, "MIN_PROMOTION_CLUSTERS", 10):
            with self.assertRaisesRegex(strategy_factory.FactoryError,
                                        "cannot meet its protocol"):
                strategy_factory._run_factory(
                    [], db_path=":memory:", min_trades=100, min_sessions=30,
                    _resources=[])

    def test_factory_walk_forward_parameters_meet_the_protocol(self):
        # The factory derives its fold floors from the lane floor; at the
        # shipped floors they meet the protocol checked by D2-09.
        for lane, trades in (("backtest", 100), ("shadow", 150)):
            # A 30% held-out partition of 120 sessions holds 36 sessions.
            days = _sessions(date(2025, 3, 1), 120 if lane == "backtest" else 40)
            rows = [_row(symbol, day, f"c-{day}-{symbol}", 1.0)
                    for day in days for symbol in (*SYMBOLS, "XLF")]
            base = [_row(symbol, day, f"b-{day}-{symbol}", 0.0)
                    for day in days for symbol in (*SYMBOLS, "XLF")]
            gate = strategy_factory._gate(
                rows, base, vehicle="equity", mode=lane, min_trades=trades,
                min_sessions=30, alpha=.05, null_rows=base)
            report = gates.protocol_parameter_report(
                lane=lane, floors={"fit": gate["fit_floor"],
                                   "heldout": gate["heldout_floor"]},
                walk_forward=gate["walk_forward"], control=gate["control"])
            self.assertEqual(report["failures"], [], lane)


class ReverificationDrawCountTests(unittest.TestCase):
    """D2-03: re-verification repeats the persisted number of draws."""

    def test_envelope_tested_with_more_than_20000_draws_reverifies(self):
        days = _sessions(date(2025, 3, 1), 30)
        # Mixed-sign sessions, so the Monte Carlo p depends on the draw count.
        heldout = [_row(symbol, day, f"held-{day}-{symbol}",
                        (1.0 if (index * 7 + offset) % 5 else -2.5))
                   for index, day in enumerate(days)
                   for offset, symbol in enumerate(SYMBOLS)]
        baseline = [{**row, "opportunity_id": f"base-{row['opportunity_id']}",
                     "net_pnl": 0.0} for row in heldout]
        for iterations in (40_000, 20_000):
            control = matched_cluster_test(
                heldout, baseline, vehicle="equity", iterations=iterations)
            self.assertEqual(control["resamples"], iterations)
            envelope = verified_gate_envelope(
                lane="shadow", vehicle="equity", fit=[], heldout=heldout,
                heldout_baseline=baseline,
                fit_floor=structural_floor([], vehicle="equity", min_trades=0,
                                           min_sessions=0, required=False),
                heldout_floor=structural_floor(heldout, vehicle="equity",
                                               min_trades=1, min_sessions=1),
                control=control, p_value=control["p_value"], q_value=1.0,
                alpha=.05, falsification={}, separation={}, checks={},
                passes=False)
            recomputed = recompute_gate_statistics(envelope)
            self.assertEqual(recomputed["p_value"], control["p_value"], iterations)
            self.assertTrue(verify_gate_envelope(envelope), iterations)
        default = matched_cluster_test(heldout, baseline, vehicle="equity")
        self.assertNotEqual(default["p_value"], matched_cluster_test(
            heldout, baseline, vehicle="equity", iterations=40_000)["p_value"])


class PowerReportTests(unittest.TestCase):
    """D2-04: the protocol publishes what its own floors can detect."""

    def test_mde_for_the_shipped_floors(self):
        report = protocol_power_report(sd_per_trade=40.0, trades_per_session=5,
                                       batch_size=50, effect_unit="bps")
        self.assertFalse(report["floors_sized_to_target_mde"])
        heldout = report["windows"]["heldout"]
        self.assertEqual(heldout["sessions"], 30)
        single = heldout["tests"]["single_test"]
        expected = ((1.6448536269514722 + 0.8416212335729143) *
                    40.0 / math.sqrt(150))
        self.assertAlmostEqual(single["mde"], expected)
        self.assertAlmostEqual(single["mde"], 8.12, places=2)
        self.assertAlmostEqual(
            report["windows"]["qualification"]["tests"]["single_test"]["mde"],
            8.12, places=2)
        self.assertAlmostEqual(heldout["tests"]["by_lone_discovery"]["mde"],
                               14.22, places=2)
        # The review's 36-session held-out figure.
        self.assertAlmostEqual(gates.minimum_detectable_effect(
            sd_per_trade=40.0, trades_per_session=5, sessions=36)["mde"],
            7.41, places=2)
        # Protocol 1 cannot reach a lone discovery below its Monte Carlo floor.
        legacy = protocol_power_report(
            sd_per_trade=40.0, trades_per_session=5, batch_size=240,
            protocol_version=LEGACY_GATE_PROTOCOL_VERSION)
        self.assertFalse(legacy["windows"]["heldout"]["tests"]
                         ["by_lone_discovery"]["attainable"])

    def test_opt_in_target_mde_sizing_only_raises_the_protocol_floors(self):
        sized = gates.lane_minimums_for_target_mde(
            "backtest", target_mde=5.0, sd_per_trade=40.0, trades_per_session=5)
        self.assertEqual(sized["sessions"], gates.sessions_for_target_mde(
            5.0, sd_per_trade=40.0, trades_per_session=5))
        self.assertGreater(sized["sessions"], 30)
        self.assertGreaterEqual(sized["trades"], 100)
        gates.validate_protocol_floor(lane="backtest", min_trades=sized["trades"],
                                      min_sessions=sized["sessions"])
        check = gates.minimum_detectable_effect(
            sd_per_trade=40.0, trades_per_session=5, sessions=sized["sessions"])
        self.assertLessEqual(check["mde"], 5.0)
        # A generous target never lowers the protocol.
        loose = gates.lane_minimums_for_target_mde(
            "shadow", target_mde=100.0, sd_per_trade=40.0, trades_per_session=5)
        self.assertEqual((loose["trades"], loose["sessions"]), (150, 30))

    def test_envelope_carries_a_verified_power_diagnostic(self):
        envelope = verified_gate_envelope(**_passing_v2_inputs())
        power = envelope["power"]
        self.assertTrue(power["available"])
        self.assertFalse(power["authorizing"])
        self.assertFalse(power["floors_sized_to_target_mde"])
        self.assertEqual(power["inputs"]["batch_size"], 2)
        self.assertTrue(verify_gate_envelope(envelope))
        tampered = copy.deepcopy(envelope)
        tampered["power"]["windows"]["heldout"]["tests"]["single_test"]["mde"] = 123.0
        self.assertFalse(verify_gate_envelope(_rehash(tampered)))


class MonteCarloResolutionTests(unittest.TestCase):
    """D2-05: a lone BY discovery stays attainable in a 175+ candidate batch."""

    def test_lone_discovery_in_large_batches(self):
        days = _sessions(date(2025, 3, 1), 30)
        heldout = [_row(symbol, day, f"held-{day}-{symbol}", 1.0 + .01 * index)
                   for index, day in enumerate(days) for symbol in SYMBOLS]
        baseline = [{**row, "opportunity_id": f"base-{row['opportunity_id']}",
                     "net_pnl": 0.0} for row in heldout]
        current = matched_cluster_test(heldout, baseline, vehicle="equity")
        legacy = matched_cluster_test(heldout, baseline, vehicle="equity",
                                      protocol_version=LEGACY_GATE_PROTOCOL_VERSION)
        self.assertEqual(legacy["p_value"], 1 / 20_001)
        self.assertTrue(current["resolution_tail"]["applied"])
        self.assertEqual(current["monte_carlo_p_value"], 1 / 20_001)
        self.assertLess(current["p_value"], 1 / 20_001)
        for batch in (175, 240):
            values = {"candidate": current["p_value"],
                      **{f"null-{index}": 1.0 for index in range(batch - 1)}}
            self.assertTrue(benjamini_yekutieli(values)["candidate"]["significant"])
            values["candidate"] = legacy["p_value"]
            self.assertFalse(benjamini_yekutieli(values)["candidate"]["significant"])

    def test_tail_is_used_only_when_no_null_draw_reached_the_statistic(self):
        days = _sessions(date(2025, 3, 1), 30)
        heldout = [_row(symbol, day, f"held-{day}-{symbol}",
                        (1.0 if (index * 7 + offset) % 5 else -2.5))
                   for index, day in enumerate(days)
                   for offset, symbol in enumerate(SYMBOLS)]
        baseline = [{**row, "opportunity_id": f"base-{row['opportunity_id']}",
                     "net_pnl": 0.0} for row in heldout]
        current = matched_cluster_test(heldout, baseline, vehicle="equity")
        legacy = matched_cluster_test(heldout, baseline, vehicle="equity",
                                      protocol_version=LEGACY_GATE_PROTOCOL_VERSION)
        self.assertFalse(current["resolution_tail"]["applied"])
        self.assertEqual(current["p_value"], legacy["p_value"])


class ComparisonKeyTests(unittest.TestCase):
    """D2-06: ambiguous pairing keys fail loudly instead of vanishing."""

    def test_duplicate_symbol_session_fails_the_named_check(self):
        repeat = _row("SPY", "2025-03-05", "held-repeat", 2.0, minute=55)
        envelope = verified_gate_envelope(
            **_passing_v2_inputs(heldout_extra=[repeat]))
        self.assertFalse(envelope["checks"]["comparison_keys_unique"])
        self.assertFalse(envelope["passes"])
        heldout = envelope["pairing_integrity"]["arms"]["heldout"]
        self.assertEqual(heldout["duplicate_keys"], ["equity:SPY:2025-03-05"])
        self.assertEqual(heldout["dropped_rows"], 2)
        self.assertTrue(verify_gate_envelope(envelope))
        # Protocol 1 silently dropped both rows from every paired statistic.
        legacy = verified_gate_envelope(**_evidence(
            protocol_version=LEGACY_GATE_PROTOCOL_VERSION,
            held_value=lambda index, symbol: 2.0, heldout_extra=[repeat]))
        self.assertNotIn("comparison_keys_unique", legacy["checks"])

    def test_explicit_comparison_ids_pair_repeat_entries(self):
        first = _row("SPY", "2025-03-05", "c-1", 2.0, minute=31,
                     signal_timestamp="2025-03-05T14:30:00+00:00")
        second = _row("SPY", "2025-03-05", "c-2", 3.0, minute=51,
                      signal_timestamp="2025-03-05T14:50:00+00:00")
        candidate = [{**row, "comparison_id": opportunity_comparison_id(
            row, vehicle="equity")} for row in (first, second)]
        baseline = [{**row, "opportunity_id": f"b-{row['opportunity_id']}",
                     "net_pnl": 1.0} for row in candidate]
        adequacy = paired_control_adequacy(candidate, baseline, vehicle="equity",
                                           min_matched=2, min_coverage=1.0)
        self.assertEqual(adequacy["matched"], 2)
        report = gates.comparison_key_report(
            {"heldout": candidate, "heldout_baseline": baseline}, vehicle="equity")
        self.assertTrue(report["unique"])
        unkeyed = gates.comparison_key_report(
            {"heldout": [first, second]}, vehicle="equity")
        self.assertFalse(unkeyed["unique"])


class HeldoutDrawdownTests(unittest.TestCase):
    """D2-07: a preregistered held-out drawdown limit in R."""

    def test_drawdown_beyond_ten_r_fails_under_protocol_2(self):
        losing_streak = (lambda index, symbol: -2.0 if index in (10, 11) else 2.0)
        envelope = verified_gate_envelope(
            **_passing_v2_inputs(held_value=losing_streak))
        drawdown = envelope["heldout_drawdown"]
        self.assertEqual(drawdown["max_drawdown"], 16.0)
        self.assertEqual(drawdown["max_drawdown_limit"], 10.0)
        self.assertFalse(drawdown["within_limit"])
        self.assertFalse(envelope["checks"]["max_drawdown_supported"])
        self.assertTrue(verify_gate_envelope(envelope))
        within = verified_gate_envelope(**_passing_v2_inputs(
            held_value=lambda index, symbol: -2.0 if index == 10 else 2.0))
        self.assertTrue(within["heldout_drawdown"]["within_limit"])
        self.assertTrue(within["checks"]["max_drawdown_supported"])
        legacy = verified_gate_envelope(**_evidence(
            protocol_version=LEGACY_GATE_PROTOCOL_VERSION,
            held_value=losing_streak))
        self.assertTrue(legacy["checks"]["max_drawdown_supported"])


class CheckClassificationTests(unittest.TestCase):
    """D2-08: the envelope states its independent content."""

    def test_every_required_check_is_classified(self):
        legacy = gate_check_classification(LEGACY_GATE_PROTOCOL_VERSION)
        self.assertEqual(legacy["summary"], {
            "required": 31, "independent": 16, "implied": 11, "diagnostic": 4})
        current = gate_check_classification()
        self.assertEqual(current["summary"], {
            "required": 33, "independent": 19, "implied": 11, "diagnostic": 3})
        for report in (legacy, current):
            self.assertNotIn("unclassified", {
                report["classes"][name] for name in report["required_checks"]})
        self.assertEqual(current["classes"]["heldout_p_significant"], "implied")
        self.assertEqual(current["classes"]["fill_quality_adequate"], "diagnostic")
        self.assertEqual(legacy["classes"]["max_drawdown_supported"], "diagnostic")
        self.assertEqual(current["classes"]["max_drawdown_supported"], "independent")
        self.assertIn("notes", gate_check_classification(include_notes=True))

    def test_envelope_classification_is_verified(self):
        envelope = verified_gate_envelope(**_passing_v2_inputs())
        classification = envelope["check_classification"]
        self.assertEqual(classification["summary"]["independent_passed"], 19)
        self.assertEqual(classification["summary"]["independent_failed"], [])
        self.assertTrue(verify_gate_envelope(envelope))
        tampered = copy.deepcopy(envelope)
        tampered["check_classification"]["classes"]["falsification"] = "independent"
        self.assertFalse(verify_gate_envelope(_rehash(tampered)))


class ProtocolParameterTests(unittest.TestCase):
    """D2-09: verification bounds the parameters it recomputes from."""

    @classmethod
    def setUpClass(cls):
        cls.envelope = verified_gate_envelope(**_passing_v2_inputs())

    def test_baseline_is_a_verified_pass(self):
        self.assertTrue(self.envelope["passes"])
        self.assertTrue(verify_gate_envelope(self.envelope))

    def test_optional_heldout_floor_cannot_authorize(self):
        tampered = copy.deepcopy(self.envelope)
        tampered["floors"]["heldout"]["required"] = False
        self.assertFalse(verify_gate_envelope(_rehash(tampered)))

    def test_walk_forward_below_the_protocol_cannot_authorize(self):
        tampered = copy.deepcopy(self.envelope)
        inputs = _passing_v2_inputs()
        tampered["walk_forward"] = walk_forward_report(
            inputs["heldout"], inputs["heldout_baseline"], vehicle="equity",
            folds=3)
        self.assertTrue(tampered["walk_forward"]["adequate"])
        self.assertFalse(verify_gate_envelope(_rehash(tampered)))

    def test_control_without_deltas_cannot_authorize(self):
        tampered = copy.deepcopy(self.envelope)
        for key in ("deltas", "delta_clusters"):
            tampered["control"].pop(key)
        self.assertFalse(verify_gate_envelope(_rehash(tampered)))

    def test_control_seed_and_draws_are_preregistered(self):
        report = gates.protocol_parameter_report(
            lane="backtest", floors=self.envelope["floors"],
            walk_forward=self.envelope["walk_forward"],
            control={**self.envelope["control"], "seed": 1, "resamples": 10})
        self.assertEqual(report["failures"], [
            "control_iterations_below_protocol",
            "control_seed_not_preregistered"])


class FactoryReportDisplayTests(unittest.TestCase):
    """The factory report shows the new diagnostics and check names."""

    def test_report_shows_power_classification_and_new_failures(self):
        from research import factory_report
        envelope = verified_gate_envelope(**_passing_v2_inputs(
            held_value=lambda index, symbol: 1.0 + index % 2))
        summary = factory_report._gate_power_summary(envelope)
        self.assertGreater(summary["heldout_mde"], 0.0)
        self.assertGreater(summary["lone_discovery_mde"], summary["heldout_mde"])
        self.assertEqual((summary["independent_passed"],
                          summary["independent_required"]), (19, 19))
        self.assertIsNone(factory_report._gate_power_summary({}))
        repeat = _row("SPY", "2025-03-05", "held-repeat", 2.0, minute=55)
        failing = verified_gate_envelope(
            **_passing_v2_inputs(heldout_extra=[repeat]))
        self.assertIn("every trade pairs unambiguously with its control",
                      factory_report._failed_checks({"verified_gate": failing}))


if __name__ == "__main__":
    unittest.main()
