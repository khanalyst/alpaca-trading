"""B3.3, gate G2: the keystone test.

Replaying the baseline variant over the historical corpus must reproduce the
live agent's own recorded decisions. If it does not, the replay is wrong and
every number downstream of it is worthless.

What makes this the keystone rather than merely an important test is the
shape of the failure. A broken replay does not raise. It produces a clean
table of precise, plausible, internally consistent numbers describing a
system nobody runs, and there is nothing in the output to suggest anything
is wrong. Every sweep, every conditional, every promotion decision would then
be made on that table.

So the tests below care most about the direction that is easy to get wrong:
not "does fidelity report 100% when everything matches", but "does it
actually notice when something does not".
"""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from copy import deepcopy
from pathlib import Path

from agent.risk import RiskEngine
from research import replay
from tests.helpers import valid_config
from tests.research.test_replay_determinism import (cycle, model_output,
                                                    open_decision)


class FidelityFixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db = Path(self.dir.name) / "journal.db"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "CREATE TABLE events (ts REAL, kind TEXT, payload TEXT, "
                "run_id TEXT, cycle_id TEXT, setup_id TEXT)")

    def record(self, cycle_id: str, symbol: str, direction: str,
               ts: float = 1.0) -> None:
        """Write a setup_proposed event as the live engine would have."""
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?)",
                (ts, "setup_proposed",
                 json.dumps({"symbol": symbol, "direction": direction}),
                 "run-a", cycle_id, f"s-{cycle_id}-{symbol}"))

    def record_modern(self, decision, *, ts: float = 1.0) -> None:
        """Record the full proposal-stage identity used by current G2."""
        payload = {
            "symbol": decision.symbol,
            "direction": decision.direction,
            "setup_type": decision.setup_type,
            "setup_id": decision.proposal_id,
            "setup_key": decision.setup_key,
            "signal_ts": decision.signal_ts,
            "strategy_id": decision.strategy_id or "momentum",
            "strategy_version": decision.strategy_version or "phase1-v3",
            "variant_id": "live",
            "cycle_id": decision.cycle_id,
        }
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?)",
                (ts, "setup_proposed", json.dumps(payload), "run-a",
                 decision.cycle_id, decision.proposal_id))

    def replay_baseline(self, cycles, outputs):
        return replay.Replay(
            valid_config(), variant_id="momentum.baseline",
            mode="recorded_llm").run(cycles, outputs)

    def _synthetic(self, matched: int, missing: int):
        """Record ``matched + missing`` events, reproduce only ``matched``.

        Built directly rather than through a replay run so the boundary is
        exercised at an exact rate, without depending on how portfolio
        dynamics happen to distribute executions across cycles.
        """
        result = replay.ReplayResult(
            variant_id="momentum.baseline", mode="recorded_llm")
        for i in range(matched):
            self.record(f"c{i}", "BTC/USDT:USDT", "long")
            result.decisions.append(replay.ReplayDecision(
                cycle_id=f"c{i}", ts=float(i), symbol="BTC/USDT:USDT",
                signal_ts=None, stage="executed", direction="long",
                contract_passed=True))
        for i in range(missing):
            self.record(f"ghost{i}", "BTC/USDT:USDT", "long")
        return result



class ReproductionTests(FidelityFixture):
    def test_a_perfect_reproduction_passes_g2(self):
        cycles = [cycle(i) for i in range(4)]
        outputs = [model_output(i, [open_decision()]) for i in range(4)]
        result = self.replay_baseline(cycles, outputs)

        # G2 compares the contract boundary, which includes risk-vetoed
        # proposals.  Record every contract-passed decision, not executions
        # alone; the latter would manufacture a false extra mismatch.
        for decision in result.decisions:
            if decision.contract_passed:
                self.record_modern(decision)

        report = replay.fidelity(result, self.db)

        self.assertEqual(report["reproduction_rate"], 1.0)
        self.assertTrue(report["passes_g2"])
        self.assertEqual(report["missing_count"], 0)

    def test_a_missing_decision_is_detected(self):
        """The replay refused something the live agent took."""
        cycles = [cycle(i) for i in range(4)]
        outputs = [model_output(i, [open_decision()]) for i in range(4)]
        result = self.replay_baseline(cycles, outputs)

        for decision in result.executed():
            self.record(decision.cycle_id, decision.symbol,
                        decision.direction)
        # One more decision the live agent made and the replay did not.
        self.record("c99", "GHOST/USDT:USDT", "long")

        report = replay.fidelity(result, self.db)

        self.assertLess(report["reproduction_rate"], 1.0)
        self.assertEqual(report["missing_count"], 1)
        self.assertIn(("c99", "GHOST/USDT:USDT", "long"), report["missing"])

    def test_an_extra_decision_is_reported(self):
        """The replay took something the live agent never did."""
        cycles = [cycle(i) for i in range(3)]
        outputs = [model_output(i, [open_decision()]) for i in range(3)]
        result = self.replay_baseline(cycles, outputs)

        executed = result.executed()
        self.assertTrue(executed, "fixture must execute something")
        for decision in executed[:-1]:            # deliberately omit one
            self.record(decision.cycle_id, decision.symbol,
                        decision.direction)

        report = replay.fidelity(result, self.db)

        self.assertGreater(report["extra_count"], 0)

    def test_a_wholesale_mismatch_fails_g2(self):
        """The failure mode this gate exists to catch."""
        cycles = [cycle(i) for i in range(5)]
        outputs = [model_output(i, [open_decision()]) for i in range(5)]
        result = self.replay_baseline(cycles, outputs)

        for i in range(20):
            self.record(f"other-{i}", "NOTHING/USDT:USDT", "short")

        report = replay.fidelity(result, self.db)

        self.assertFalse(report["passes_g2"])
        self.assertLess(report["reproduction_rate"], 0.99)

    def test_replay_corpus_appended_after_run_cannot_certify_pass(self):
        """A replay is bound to the corpus fingerprint captured before it ran."""
        result = self._synthetic(1, 0)
        captured = replay.g2_evidence_metadata(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?)",
                (2.0, "llm_input", json.dumps({"decision": "hold"}),
                 "run-a", "hold-cycle", None))

        report = replay.fidelity(
            result, self.db, captured_metadata=captured)

        self.assertFalse(report["passes_g2"])
        self.assertTrue(report["capture_stale"])
        self.assertIn("replay corpus", report["capture_stale_reason"])

    def test_ninety_nine_percent_fails_exact_gate(self):
        report = replay.fidelity(self._synthetic(99, 1), self.db)

        self.assertAlmostEqual(report["reproduction_rate"], 0.99, 6)
        self.assertFalse(report["passes_g2"])

    def test_ninety_eight_percent_fails_the_gate(self):
        report = replay.fidelity(self._synthetic(98, 2), self.db)

        self.assertAlmostEqual(report["reproduction_rate"], 0.98, 6)
        self.assertFalse(report["passes_g2"],
                         "G2 is a full stop, not a rounding decision")

    def test_an_empty_corpus_does_not_claim_failure(self):
        """Nothing recorded is not the same as nothing reproduced."""
        result = self.replay_baseline([], [])

        report = replay.fidelity(result, self.db)

        self.assertFalse(report["passes_g2"])
        self.assertEqual(report["recorded"], 0)

    def test_the_report_lists_specific_mismatches_for_explanation(self):
        """Every mismatch must be individually explainable, so name them."""
        cycles = [cycle(0)]
        outputs = [model_output(0, [open_decision()])]
        result = self.replay_baseline(cycles, outputs)
        self.record("cX", "AAA/USDT:USDT", "long")
        self.record("cY", "BBB/USDT:USDT", "short")

        report = replay.fidelity(result, self.db)

        self.assertEqual(len(report["missing"]), report["missing_count"])
        self.assertIn(("cX", "AAA/USDT:USDT", "long"), report["missing"])


class SetupBreadthReplayParityTests(unittest.TestCase):
    @staticmethod
    def _breadth(firing):
        return {
            "momentum": {
                "instruments_scanned": 10,
                "instruments_with_a_valid_setup": firing,
                "setup_breadth_pct": (
                    firing * 10.0 if isinstance(firing, (int, float))
                    else None),
            },
        }

    @staticmethod
    def _live_risk(cfg, observed, breadth=None):
        snapshot = deepcopy(observed.snapshot)
        if breadth is not None:
            snapshot["_market_context"]["_enrichment"] = {
                "setup_breadth_by_strategy": breadth,
            }
        decision = dict(
            open_decision(), stop_loss_pct=2.0, take_profit_pct=4.0)
        return RiskEngine(cfg).vet_open(
            decision, 10_000.0, [], snapshot, {}, 0.0, now=observed.ts)

    def _replay(self, cfg, observed):
        return replay.Replay(cfg, mode="recorded_llm").run(
            [observed], [model_output(0, [open_decision()])])

    def test_crowded_recorded_lane_matches_the_live_veto(self):
        cfg = valid_config()
        observed = cycle(0)
        breadth = self._breadth(5)
        observed.enrichment = {
            "market": {"setup_breadth_by_strategy": breadth},
            "symbols": {},
        }
        original_snapshot = deepcopy(observed.snapshot)

        live_plan, live_veto = self._live_risk(cfg, observed, breadth)
        result = self._replay(cfg, observed)

        self.assertIsNone(live_plan)
        self.assertEqual(
            live_veto,
            "setup breadth 5 instruments exceeds 4: correlated market-wide "
            "move")
        self.assertEqual(result.decisions[0].stage, "vetoed")
        self.assertEqual(result.decisions[0].reason, live_veto)
        self.assertEqual(observed.snapshot, original_snapshot)
        self.assertNotIn("_enrichment",
                         observed.snapshot["_market_context"])

    def test_quiet_and_missing_legacy_enrichment_remain_openable(self):
        cfg = valid_config()
        for label, breadth in (("quiet", self._breadth(4)),
                               ("legacy_missing", None)):
            with self.subTest(label=label):
                observed = cycle(0)
                if breadth is not None:
                    observed.enrichment = {
                        "market": {"setup_breadth_by_strategy": breadth},
                        "symbols": {},
                    }

                live_plan, live_veto = self._live_risk(
                    cfg, observed, breadth)
                result = self._replay(cfg, observed)

                self.assertIsNotNone(live_plan)
                self.assertIsNone(live_veto)
                self.assertEqual(len(result.executed()), 1)

    def test_malformed_recorded_lane_fails_closed_in_live_and_replay(self):
        cfg = valid_config()
        observed = cycle(0)
        breadth = self._breadth("many")
        observed.enrichment = {
            "market": {"setup_breadth_by_strategy": breadth},
            "symbols": {},
        }

        live_plan, live_veto = self._live_risk(cfg, observed, breadth)
        result = self._replay(cfg, observed)

        self.assertIsNone(live_plan)
        self.assertEqual(live_veto, "setup breadth measurement is invalid")
        self.assertEqual(result.decisions[0].stage, "vetoed")
        self.assertEqual(result.decisions[0].reason, live_veto)


class VacuousGateTests(FidelityFixture):
    """100% of nothing is not evidence of fidelity."""

    def test_an_empty_corpus_is_marked_vacuous(self):
        report = replay.fidelity(self.replay_baseline([], []), self.db)

        self.assertTrue(report["vacuous"])
        self.assertEqual(report["recorded"], 0)

    def test_a_real_corpus_is_not_vacuous(self):
        result = self._synthetic(matched=10, missing=0)

        report = replay.fidelity(result, self.db)

        self.assertFalse(report["vacuous"])
        self.assertTrue(report["passes_g2"])

    def test_vacuous_and_passing_are_distinguishable(self):
        """A caller reading passes_g2 alone would be misled, so both exist."""
        empty = replay.fidelity(self.replay_baseline([], []), self.db)

        self.assertFalse(empty["passes_g2"])
        self.assertTrue(empty["vacuous"],
                        "the gate must expose that it checked nothing")


if __name__ == "__main__":
    unittest.main()


class ComparisonStageTests(FidelityFixture):
    """G2 must compare at the stage the engine actually journals.

    `setup_proposed` is written inside _prepare_setup_decision, which runs
    BEFORE RiskEngine.vet_open. Comparing it against replayed *executions*
    would count every risk-vetoed setup as a reproduction failure - roughly
    four fifths of them on the historical corpus - and G2 would hard-fail at
    ~20% while the replay was entirely correct.
    """

    def test_a_risk_vetoed_setup_still_counts_as_reproduced(self):
        result = replay.ReplayResult(
            variant_id="momentum.baseline", mode="recorded_llm")
        self.record("c1", "BTC/USDT:USDT", "long")
        result.decisions.append(replay.ReplayDecision(
            cycle_id="c1", ts=1.0, symbol="BTC/USDT:USDT", signal_ts=None,
            stage="vetoed", direction="long",
            reason="max concurrent positions reached",
            contract_passed=True))

        report = replay.fidelity(result, self.db)

        self.assertEqual(report["reproduction_rate"], 1.0)
        self.assertTrue(report["passes_g2"])

    def test_a_contract_refusal_does_not_count_as_reproduced(self):
        """The contract refused it, so the live engine never proposed it."""
        result = replay.ReplayResult(
            variant_id="momentum.baseline", mode="recorded_llm")
        self.record("c1", "BTC/USDT:USDT", "long")
        result.decisions.append(replay.ReplayDecision(
            cycle_id="c1", ts=1.0, symbol="BTC/USDT:USDT", signal_ts=None,
            stage="vetoed", direction="long",
            reason="evidence contract is not met",
            contract_passed=False))

        report = replay.fidelity(result, self.db)

        self.assertEqual(report["reproduction_rate"], 0.0)
        self.assertFalse(report["passes_g2"])

    def test_the_historical_ratio_would_have_failed_the_old_comparison(self):
        """206 proposals, 41 executions: the shape that broke the gate."""
        result = replay.ReplayResult(
            variant_id="momentum.baseline", mode="recorded_llm")
        for i in range(206):
            self.record(f"c{i}", "BTC/USDT:USDT", "long")
            result.decisions.append(replay.ReplayDecision(
                cycle_id=f"c{i}", ts=float(i), symbol="BTC/USDT:USDT",
                signal_ts=None,
                stage="executed" if i < 41 else "vetoed",
                direction="long", contract_passed=True))

        report = replay.fidelity(result, self.db)

        self.assertEqual(report["reproduction_rate"], 1.0)
        executions_only = len([d for d in result.decisions
                               if d.stage == "executed"])
        self.assertLess(executions_only / 206, 0.99,
                        "comparing against executions alone would fail G2")


class _StrictIdentityFixture(FidelityFixture):
    """Current journals use the full proposal identity at the G2 boundary."""

    def record_modern(self, cycle_id="c1", *, setup_type="trend_continuation",
                      setup_id="setup-1", setup_key="key-1",
                      variant_id="live", signal_ts=100, ts=1.0):
        payload = {
            "symbol": "BTC/USDT:USDT", "direction": "long",
            "setup_type": setup_type, "setup_id": setup_id,
            "setup_key": setup_key, "signal_ts": signal_ts,
            "strategy_id": "momentum", "strategy_version": "v1",
            "variant_id": variant_id,
        }
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?)",
                (ts, "setup_proposed", json.dumps(payload), "run-a",
                 cycle_id, setup_id))
            # The fixture's legacy schema does not expose context columns;
            # payload identity remains authoritative for this regression.
            conn.commit()

    def decision(self, *, cycle_id="c1", setup_type="trend_continuation",
                 setup_id="setup-1", setup_key="key-1", signal_ts=100):
        return replay.ReplayDecision(
            cycle_id=cycle_id, ts=1.0, symbol="BTC/USDT:USDT",
            signal_ts=signal_ts, stage="vetoed", direction="long",
            setup_type=setup_type, proposal_id=setup_id,
            strategy_id="momentum", strategy_version="v1",
            setup_key=setup_key, contract_passed=True)


class StrictIdentityTests(_StrictIdentityFixture):
    """Distinct modern proposal identities must never collapse together."""

    def test_setup_type_change_does_not_collapse_to_same_key(self):
        self.record_modern()
        result = replay.ReplayResult(
            variant_id="momentum.baseline", mode="recorded_llm",
            decisions=[self.decision(setup_type="range_breakout")],
            strategy_id="momentum", strategy_version="v1")
        report = replay.fidelity(result, self.db)
        self.assertEqual(report["missing_count"], 1)
        self.assertEqual(report["extra_count"], 1)
        self.assertFalse(report["passes_g2"])

    def test_live_and_baseline_variant_aliases_match(self):
        self.record_modern()
        result = replay.ReplayResult(
            variant_id="momentum.baseline", mode="recorded_llm",
            decisions=[self.decision()], strategy_id="momentum",
            strategy_version="v1")
        report = replay.fidelity(result, self.db)
        self.assertTrue(report["passes_g2"])

    def test_hyphenated_strategy_uses_registry_safe_baseline_alias(self):
        common = {
            "cycle_id": "c1", "symbol": "BTC/USDT:USDT",
            "direction": "long", "strategy_id": "flush-fade",
            "strategy_version": "v1", "setup_id": "setup-1",
            "setup_key": "key-1", "setup_type": "trend",
            "signal_ts": 100,
        }
        safe = replay.canonical_proposal_identity(
            {**common, "variant_id": "flush_fade.baseline"})
        candidate = replay.canonical_proposal_identity(
            {**common, "variant_id": "flush_fade.other"})
        self.assertEqual(safe[3], "baseline")
        self.assertNotEqual(candidate[3], "baseline")

    def test_duplicate_and_malformed_rows_fail_closed(self):
        self.record_modern()
        self.record_modern()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?)",
                (2.0, "setup_proposed", "not-json", "run-a", "c2",
                 "setup-2"))
        result = replay.ReplayResult(
            variant_id="momentum.baseline", mode="recorded_llm",
            decisions=[self.decision()], strategy_id="momentum",
            strategy_version="v1")
        report = replay.fidelity(result, self.db)
        self.assertEqual(report["duplicate_count"], 1)
        self.assertGreaterEqual(report["malformed_count"], 1)
        self.assertFalse(report["passes_g2"])


class ModernBoundaryTests(_StrictIdentityFixture):
    """Legacy rows are readable, but never dilute modern strict evidence."""

    def _result(self, *decisions):
        return replay.ReplayResult(
            variant_id="momentum.baseline", mode="recorded_llm",
            decisions=list(decisions), strategy_id="momentum",
            strategy_version="v1")

    def test_legacy_only_corpus_remains_compatible(self):
        self.record("legacy-1", "BTC/USDT:USDT", "long")
        result = self._result(replay.ReplayDecision(
            cycle_id="legacy-1", ts=1.0, symbol="BTC/USDT:USDT",
            signal_ts=None, stage="vetoed", direction="long",
            contract_passed=True))

        report = replay.fidelity(result, self.db)

        self.assertTrue(report["passes_g2"])
        self.assertTrue(report["legacy_identity"])
        self.assertEqual(report["legacy_excluded_count"], 0)
        self.assertIsNone(report["modern_boundary_ts"])

    def test_legacy_before_modern_is_excluded_from_strict_evidence(self):
        self.record("legacy-1", "BTC/USDT:USDT", "long", ts=1.0)
        self.record_modern(cycle_id="c1", ts=2.0)

        report = replay.fidelity(self._result(self.decision()), self.db)

        self.assertTrue(report["passes_g2"])
        self.assertFalse(report["legacy_identity"])
        self.assertEqual(report["legacy_excluded_count"], 1)
        self.assertEqual(report["modern_evidence_count"], 1)
        self.assertEqual(report["modern_boundary_ts"], 2.0)

    def test_legacy_after_modern_fails_closed(self):
        self.record_modern(cycle_id="c1", ts=1.0)
        self.record("legacy-2", "BTC/USDT:USDT", "long", ts=2.0)

        report = replay.fidelity(self._result(self.decision()), self.db)

        self.assertFalse(report["passes_g2"])
        self.assertEqual(report["legacy_after_boundary_count"], 1)
        self.assertEqual(report["malformed_reasons"]
                         ["legacy_after_modern_boundary"], 1)

    def test_late_appended_legacy_row_fails_even_with_older_timestamp(self):
        self.record_modern(cycle_id="c1", ts=10.0)
        self.record("legacy-2", "BTC/USDT:USDT", "long", ts=1.0)

        report = replay.fidelity(self._result(self.decision()), self.db)

        self.assertEqual(report["modern_boundary_rowid"], 1)
        self.assertEqual(report["legacy_after_boundary_count"], 1)
        self.assertFalse(report["passes_g2"])

    def test_post_boundary_replay_extra_still_fails(self):
        self.record("legacy-1", "BTC/USDT:USDT", "long", ts=1.0)
        self.record_modern(cycle_id="c1", ts=2.0)
        extra = self.decision(cycle_id="c2", setup_id="setup-2",
                              setup_key="key-2", signal_ts=200)

        report = replay.fidelity(
            self._result(self.decision(), extra), self.db)

        self.assertEqual(report["legacy_excluded_count"], 1)
        self.assertEqual(report["extra_count"], 1)
        self.assertFalse(report["passes_g2"])

    def test_pre_boundary_replay_is_ignored_but_post_boundary_hold_is_extra(self):
        # The pre-boundary model input was appended before the legacy proposal;
        # the later HOLD cycle has no setup proposal and must remain visible.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(
                "INSERT INTO events (ts, kind, payload, run_id, cycle_id, setup_id) "
                "VALUES (?,?,?,?,?,?)",
                (1.0, "llm_input", "{}", "run-a", "legacy-cycle", None))
        self.record("legacy-cycle", "BTC/USDT:USDT", "long", ts=2.0)
        self.record_modern(cycle_id="modern-cycle", ts=3.0)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executemany(
                "INSERT INTO events (ts, kind, payload, run_id, cycle_id, setup_id) "
                "VALUES (?,?,?,?,?,?)", [
                    (4.0, "llm_input", "{}", "run-a", "modern-cycle", None),
                    (5.0, "llm_input", "{}", "run-a", "hold-cycle", None),
                ])
        modern = self.decision(cycle_id="modern-cycle")
        pre_boundary = self.decision(cycle_id="legacy-cycle",
                                     setup_id="legacy-setup",
                                     setup_key="legacy-key", signal_ts=99)
        hold = self.decision(cycle_id="hold-cycle", setup_id="hold-setup",
                             setup_key="hold-key", signal_ts=200)

        matched = replay.fidelity(self._result(modern, pre_boundary), self.db)
        self.assertTrue(matched["passes_g2"])

        extra = replay.fidelity(self._result(modern, hold), self.db)
        self.assertEqual(extra["extra_count"], 1)
        self.assertFalse(extra["passes_g2"])
