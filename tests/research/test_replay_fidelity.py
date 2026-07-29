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
from pathlib import Path

from research import replay
from tests.helpers import valid_config
from tests.research.test_replay_determinism import (cycle, model_output,
                                                    open_decision)


class FidelityFixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db = Path(self.dir.name) / "journal.db"
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE events (ts REAL, kind TEXT, payload TEXT, "
            "run_id TEXT, cycle_id TEXT, setup_id TEXT)")
        conn.commit()
        conn.close()

    def record(self, cycle_id: str, symbol: str, direction: str,
               ts: float = 1.0) -> None:
        """Write a setup_proposed event as the live engine would have."""
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?)",
            (ts, "setup_proposed",
             json.dumps({"symbol": symbol, "direction": direction}),
             "run-a", cycle_id, f"s-{cycle_id}-{symbol}"))
        conn.commit()
        conn.close()

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
                signal_ts=None, stage="executed", direction="long"))
        for i in range(missing):
            self.record(f"ghost{i}", "BTC/USDT:USDT", "long")
        return result



class ReproductionTests(FidelityFixture):
    def test_a_perfect_reproduction_passes_g2(self):
        cycles = [cycle(i) for i in range(4)]
        outputs = [model_output(i, [open_decision()]) for i in range(4)]
        result = self.replay_baseline(cycles, outputs)

        for decision in result.executed():
            self.record(decision.cycle_id, decision.symbol,
                        decision.direction)

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

    def test_ninety_nine_percent_passes_the_gate(self):
        report = replay.fidelity(self._synthetic(99, 1), self.db)

        self.assertAlmostEqual(report["reproduction_rate"], 0.99, 6)
        self.assertTrue(report["passes_g2"])

    def test_ninety_eight_percent_fails_the_gate(self):
        report = replay.fidelity(self._synthetic(98, 2), self.db)

        self.assertAlmostEqual(report["reproduction_rate"], 0.98, 6)
        self.assertFalse(report["passes_g2"],
                         "G2 is a full stop, not a rounding decision")

    def test_an_empty_corpus_does_not_claim_failure(self):
        """Nothing recorded is not the same as nothing reproduced."""
        result = self.replay_baseline([], [])

        report = replay.fidelity(result, self.db)

        self.assertTrue(report["passes_g2"])
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

        self.assertTrue(empty["passes_g2"])
        self.assertTrue(empty["vacuous"],
                        "the gate must expose that it checked nothing")


if __name__ == "__main__":
    unittest.main()
