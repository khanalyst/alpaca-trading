"""Is it ready yet, answered from the journal rather than from memory.

Two items in this programme wait on data rather than on engineering, and both
fail the same quiet way: someone eventually decides they have waited long
enough. G2 in particular reproduces 100% of nothing on an empty corpus, which
looks like a pass.

So the tests here care most about the states that are easy to misread - a
corpus too small for a ratio to mean anything, and a passive order that could
not be cancelled.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from research import readiness
from tests.helpers import valid_config


class ReadinessFixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db = Path(self.dir.name) / "journal.db"
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE events (ts REAL, kind TEXT, payload TEXT, "
            "run_id TEXT, cycle_id TEXT, setup_id TEXT)")
        conn.execute(
            "CREATE TABLE trades (ts REAL, symbol TEXT, action TEXT, "
            "trade_id TEXT, realized_pnl_usd REAL)")
        conn.commit()
        conn.close()
        self.cfg = valid_config()

    def event(self, kind, payload=None, ts=1.0, cycle_id="c1"):
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?)",
            (ts, kind, json.dumps(payload or {}), "run-a", cycle_id, None))
        conn.commit()
        conn.close()

    def gates(self, db=None):
        gates, _ = readiness.report(
            db if db is not None else self.db, self.cfg)
        return {g.name: g for g in gates}


class NoJournalTests(ReadinessFixture):
    def test_a_fresh_install_says_so_rather_than_erroring(self):
        gates = self.gates(db=None)

        self.assertEqual(gates["G2"].status, readiness.COLLECTING)
        self.assertIn("run the agent", gates["G2"].next_step)

    def test_b75_is_blocked_before_g2(self):
        gates = self.gates(db=None)

        self.assertEqual(gates["B7.5"].status, readiness.BLOCKED)
        self.assertIn("G2", gates["B7.5"].detail)


class G2Tests(ReadinessFixture):
    def test_an_empty_corpus_is_collecting_not_passing(self):
        """100% of nothing is the failure mode this whole check exists for."""
        gates = self.gates()

        self.assertEqual(gates["G2"].status, readiness.COLLECTING)
        self.assertIn("100% of nothing", gates["G2"].detail)

    def test_a_small_sample_quotes_what_one_mismatch_would_read_as(self):
        for i in range(10):
            self.event("setup_proposed", cycle_id=f"c{i}")

        gate = self.gates()["G2"]

        self.assertEqual(gate.status, readiness.COLLECTING)
        self.assertIn("10 recorded proposals", gate.detail)
        self.assertIn("90%", gate.detail)

    def test_a_sufficient_sample_is_ready_not_passed(self):
        """READY means the command has not been run, not that it succeeded."""
        for i in range(readiness.MIN_PROPOSALS_FOR_G2):
            self.event("setup_proposed", cycle_id=f"c{i}")

        gate = self.gates()["G2"]

        self.assertEqual(gate.status, readiness.READY)
        self.assertIn("check-fidelity", gate.next_step)

    def test_g2_blocks_b75_until_it_is_reachable(self):
        for i in range(5):
            self.event("setup_proposed", cycle_id=f"c{i}")

        gates = self.gates()

        self.assertEqual(gates["G2"].status, readiness.COLLECTING)
        self.assertEqual(gates["B7.5"].status, readiness.BLOCKED)


class B75Tests(ReadinessFixture):
    def _reachable_g2(self):
        for i in range(readiness.MIN_PROPOSALS_FOR_G2):
            self.event("setup_proposed", cycle_id=f"c{i}")

    def test_a_resting_order_fails_regardless_of_everything_else(self):
        """The one failure mode the design forbids."""
        self._reachable_g2()
        self.event("maker_attempt", {"resting": True, "order_id": "o1"})

        gate = self.gates()["B7.5"]

        self.assertEqual(gate.status, readiness.FAILED)
        self.assertIn("could not be cancelled", gate.detail)

    def test_a_resting_order_fails_even_before_g2(self):
        self.event("maker_attempt", {"resting": True, "order_id": "o1"})

        self.assertEqual(self.gates()["B7.5"].status, readiness.FAILED)

    def test_the_flag_being_off_is_ready_not_failed(self):
        self._reachable_g2()

        gate = self.gates()["B7.5"]

        self.assertEqual(gate.status, readiness.READY)
        self.assertIn("maker_first_enabled", gate.next_step)

    def test_enabled_with_too_few_attempts_is_collecting(self):
        self._reachable_g2()
        self.cfg["execution"]["maker_first_enabled"] = True
        for i in range(3):
            self.event("maker_attempt",
                       {"resting": False, "filled_contracts": 1},
                       cycle_id=f"m{i}")

        gate = self.gates()["B7.5"]

        self.assertEqual(gate.status, readiness.COLLECTING)
        self.assertIn(f"of {readiness.MIN_MAKER_ATTEMPTS}", gate.detail)

    def test_enough_clean_attempts_passes(self):
        self._reachable_g2()
        self.cfg["execution"]["maker_first_enabled"] = True
        for i in range(readiness.MIN_MAKER_ATTEMPTS):
            self.event("maker_attempt",
                       {"resting": False, "filled_contracts": 1},
                       cycle_id=f"m{i}")

        gate = self.gates()["B7.5"]

        self.assertEqual(gate.status, readiness.PASS)
        self.assertIn("none left resting", gate.detail)


class G5Tests(ReadinessFixture):
    def test_it_states_that_nothing_is_baked_in_while_it_waits(self):
        """6.4's default is `none`, which is why the wait is safe."""
        gate = self.gates()["G5"]

        self.assertEqual(gate.status, readiness.COLLECTING)
        self.assertIn("none", gate.detail)


class G6Tests(ReadinessFixture):
    def test_it_says_the_clock_cannot_be_shortened(self):
        gate = self.gates()["G6"]

        self.assertEqual(gate.status, readiness.COLLECTING)
        self.assertIn("cannot be shortened", gate.detail)

    def test_once_observations_exist_it_states_the_per_cell_bar(self):
        self.event("book_state", {"symbol": "BTC/USDT:USDT"})
        self.event("snapshot_enrichment", {"symbols": {}})

        gate = self.gates()["G6"]

        self.assertEqual(gate.status, readiness.COLLECTING)
        self.assertIn(str(readiness.MIN_OBSERVATIONS_PER_CELL), gate.detail)
        self.assertIn("three months", gate.detail)


class ReportFormattingTests(ReadinessFixture):
    def test_the_report_renders_with_no_journal(self):
        gates, stats = readiness.report(None, self.cfg)

        text = readiness.format_report(gates, stats, "absent")

        self.assertIn("has not run yet", text)
        self.assertIn("G2", text)

    def test_a_failure_is_called_a_stop_not_a_delay(self):
        self.event("maker_attempt", {"resting": True})
        gates, stats = readiness.report(self.db, self.cfg)

        text = readiness.format_report(gates, stats, self.db)

        self.assertIn("FAILED", text)
        self.assertIn("stop, not a delay", text)

    def test_waiting_says_there_is_nothing_to_build(self):
        gates, stats = readiness.report(self.db, self.cfg)

        text = readiness.format_report(gates, stats, self.db)

        self.assertIn("Waiting on data", text)
        self.assertIn("calendar time", text)

    def test_every_gate_carries_a_next_step(self):
        gates, _ = readiness.report(self.db, self.cfg)

        for gate in gates:
            self.assertTrue(gate.next_step, f"{gate.name} has no next step")
            self.assertTrue(gate.detail, f"{gate.name} has no detail")


if __name__ == "__main__":
    unittest.main()
