"""A successful fidelity check becomes durable readiness evidence."""

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

from research import readiness
from tests.helpers import valid_config


REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "research_cli_for_test", REPO / "research.py")
RESEARCH_CLI = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(RESEARCH_CLI)


class FakeResult:
    variant_id = "momentum.baseline"
    mode = "recorded_llm"

    @staticmethod
    def digest():
        return "deterministic-test-digest"


class G2PersistenceTests(unittest.TestCase):
    def test_writer_and_readiness_agree_on_the_exact_corpus(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "journal.db"
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "CREATE TABLE events (ts REAL, kind TEXT, payload TEXT)")
                conn.executemany(
                    "INSERT INTO events VALUES (?,?,?)",
                    [(float(i), "setup_proposed", "{}")
                     for i in range(1, 101)])

            cfg = valid_config()
            stored = RESEARCH_CLI._record_g2_result(
                db, "PASS", {
                    "matched": 100, "recorded": 100,
                    "reproduction_rate": 1.0,
                }, FakeResult(), cfg)
            gate = readiness.gate_g2(db, {}, cfg)

        self.assertTrue(stored)
        self.assertEqual(gate.status, readiness.PASS)
        self.assertIn("persisted", gate.detail)


if __name__ == "__main__":
    unittest.main()
