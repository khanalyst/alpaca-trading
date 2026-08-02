"""A successful fidelity check becomes durable readiness evidence."""

import contextlib
import importlib.util
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
    cycles = 1
    funnel = {
        "fired": 0, "proposed": 0, "vetoed": 0, "executed": 0,
        "veto_reasons": {},
    }

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

    @staticmethod
    def _runner(*_args, **_kwargs):
        class Runner:
            @staticmethod
            def run(*_args, **_kwargs):
                return FakeResult()
        return Runner()

    def _run_cli_fidelity(self, db: Path, *, no_persist: bool):
        args = SimpleNamespace(
            db=str(db), mode="demo", variant="momentum.baseline",
            replay_mode="recorded_llm", prices=None,
            check_fidelity=True, no_persist=no_persist,
        )
        report = {
            "matched": 100, "recorded": 100,
            "reproduction_rate": 1.0,
            "vacuous": False, "passes_g2": True,
        }
        output = io.StringIO()
        with patch.object(RESEARCH_CLI, "_resolve_cfg",
                          return_value=valid_config()), \
                patch.object(RESEARCH_CLI, "_corpus_for",
                             return_value=([], [])), \
                patch.object(RESEARCH_CLI.replay_mod, "Replay",
                             side_effect=self._runner), \
                patch.object(RESEARCH_CLI.replay_mod, "fidelity",
                             return_value=report), \
                patch.object(RESEARCH_CLI, "_record_g2_result",
                             return_value=True) as persist, \
                contextlib.redirect_stdout(output):
            status = RESEARCH_CLI.cmd_replay(args)
        return status, output.getvalue(), persist

    def test_no_persist_fidelity_check_leaves_the_journal_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "journal.db"
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "CREATE TABLE events (ts REAL, kind TEXT, payload TEXT)")
            before = db.read_bytes()

            status, output, persist = self._run_cli_fidelity(
                db, no_persist=True)

            self.assertEqual(db.read_bytes(), before)
        self.assertEqual(status, 0)
        self.assertIn("persistence disabled", output)
        persist.assert_not_called()

    def test_fidelity_check_persists_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "journal.db"
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "CREATE TABLE events (ts REAL, kind TEXT, payload TEXT)")

            status, _, persist = self._run_cli_fidelity(
                db, no_persist=False)

        self.assertEqual(status, 0)
        persist.assert_called_once()


class ReadinessDiagnosticTests(unittest.TestCase):
    def test_absent_explicit_store_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "absent" / "findings.db"

            result = RESEARCH_CLI._latest_verified_external_backup_readonly(
                store)

            self.assertIsNone(result)
            self.assertFalse(store.exists())
            self.assertFalse(store.parent.exists())

    def test_legacy_store_is_not_migrated_by_diagnostic_read(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "findings.db"
            with sqlite3.connect(store) as conn:
                conn.execute("CREATE TABLE variants (variant_id TEXT)")
                conn.execute("INSERT INTO variants VALUES ('legacy')")
            before = store.read_bytes()

            result = RESEARCH_CLI._latest_verified_external_backup_readonly(
                store)

            self.assertIsNone(result)
            self.assertEqual(store.read_bytes(), before)
            with sqlite3.connect(store) as conn:
                tables = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, {"variants"})

    def test_readiness_honours_the_explicit_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            explicit = root / "copy.db"
            output = io.StringIO()
            with patch.object(RESEARCH_CLI, "_load_config", return_value={
                    "research": {"findings_store": str(root / "default.db")}
                    }), \
                    patch.object(RESEARCH_CLI.readiness_mod, "report",
                                 return_value=([], {})), \
                    patch.object(RESEARCH_CLI.readiness_mod, "format_report",
                                 return_value="readiness"), \
                    patch.object(
                        RESEARCH_CLI,
                        "_latest_verified_external_backup_readonly",
                        return_value=None) as inspect_store, \
                    contextlib.redirect_stdout(output):
                status = RESEARCH_CLI.cmd_readiness(SimpleNamespace(
                    db=str(root / "absent-journal.db"), mode="demo",
                    store=str(explicit)))

            inspect_store.assert_called_once_with(explicit)
        self.assertEqual(status, 2)


if __name__ == "__main__":
    unittest.main()
