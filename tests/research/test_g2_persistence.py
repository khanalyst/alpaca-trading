"""A successful fidelity check becomes durable readiness evidence."""

import contextlib
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from research import corpus, readiness, replay
from agent import state
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
    @staticmethod
    def _proposal(symbol="BTC/USDT:USDT", *, cycle_id="c1",
                  setup_id="s1", setup_key="k1", setup_type="trend",
                  signal_ts=1):
        return {
            "cycle_id": cycle_id, "symbol": symbol, "direction": "long",
            "setup_type": setup_type, "setup_id": setup_id,
            "setup_key": setup_key, "signal_ts": signal_ts,
            "strategy_id": "momentum", "strategy_version": "v1",
            "variant_id": "live",
        }

    def _assert_current_failed(self, rows, report):
        cfg = valid_config()
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "journal.db"
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "CREATE TABLE events (ts REAL, kind TEXT, payload TEXT, "
                    "cycle_id TEXT, setup_id TEXT)")
                for ts, raw in rows:
                    payload = raw if isinstance(raw, str) else json.dumps(raw)
                    row = None if isinstance(raw, str) else raw
                    conn.execute(
                        "INSERT INTO events VALUES (?,?,?,?,?)",
                        (float(ts), "setup_proposed", payload,
                         row.get("cycle_id") if row else None,
                         row.get("setup_id") if row else None))
            result = SimpleNamespace(
                variant_id="momentum.baseline", mode="recorded_llm",
                digest=lambda: "digest")
            self.assertTrue(RESEARCH_CLI._record_g2_result(
                db, "FAILED", report, result, cfg))
            current, detail = replay.validate_persisted_g2(
                readiness._latest_g2_result(db), db, cfg)
            self.assertTrue(current, detail)
            self.assertEqual(readiness.gate_g2(db, {}, cfg).status,
                             readiness.FAILED)

    def test_current_failed_result_remains_failed_with_diagnostics(self):
        row = {
            "cycle_id": "c1", "symbol": "BTC/USDT:USDT",
            "direction": "long", "setup_type": "trend",
            "setup_id": "s1", "setup_key": "k1", "signal_ts": 1,
            "strategy_id": "momentum", "strategy_version": "v1",
        }
        cfg = valid_config()
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "journal.db"
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "CREATE TABLE events (ts REAL, kind TEXT, payload TEXT, "
                    "cycle_id TEXT, setup_id TEXT)")
                conn.execute(
                    "INSERT INTO events VALUES (?,?,?,?,?)",
                    (1.0, "setup_proposed", json.dumps(row), "c1", "s1"))
            report = {
                "matched": 0, "recorded": 1, "reproduction_rate": 0.0,
                "missing_count": 1, "extra_count": 0,
                "malformed_count": 0, "duplicate_count": 0,
                "unresolved_count": 0,
            }
            result = SimpleNamespace(
                variant_id="momentum.baseline", mode="recorded_llm",
                digest=lambda: "digest")
            # Writer computes the canonical digest; provide a report with the
            # failure diagnostics and assert readiness keeps FAILED current.
            RESEARCH_CLI._record_g2_result(db, "FAILED", report, result, cfg)
            self.assertEqual(readiness.gate_g2(db, {}, cfg).status,
                             readiness.FAILED)

    def test_forged_pass_with_zero_matches_is_not_current(self):
        row = {
            "cycle_id": "c1", "symbol": "BTC/USDT:USDT",
            "direction": "long", "setup_type": "trend",
            "setup_id": "s1", "setup_key": "k1", "signal_ts": 1,
            "strategy_id": "momentum", "strategy_version": "v1",
        }
        cfg = valid_config()
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "journal.db"
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "CREATE TABLE events (ts REAL, kind TEXT, payload TEXT, "
                    "cycle_id TEXT, setup_id TEXT)")
                conn.execute(
                    "INSERT INTO events VALUES (?,?,?,?,?)",
                    (1.0, "setup_proposed", json.dumps(row), "c1", "s1"))
            digest = replay.g2_corpus_digest(db)
            payload = {
                "gate": "G2", "status": "PASS", "proposal_count": 1,
                "max_proposal_ts": 1.0, "matched": 0, "recorded": 1,
                "reproduction_rate": 0.0,
                "missing_count": 0, "extra_count": 0,
                "malformed_count": 0, "duplicate_count": 0,
                "unresolved_count": 0, "malformed_reasons": {},
                "modern_evidence_count": 1,
                "legacy_excluded_count": 0,
                "legacy_after_boundary_count": 0,
                "modern_boundary_ts": 1.0,
                "modern_boundary_rowid": 1,
                "modern_boundary_cycle_id": "c1",
                "vacuous": False, "legacy_identity": False,
                "replay_digest": "replay-digest",
                "variant_id": "momentum.baseline",
                "replay_mode": "recorded_llm", "corpus_digest": digest,
                **corpus.g2_replay_corpus_metadata(db),
                "strategy_config_version": state.strategy_fingerprint(cfg),
                "fidelity_code_version": replay.fidelity_code_fingerprint(),
            }
            current, _ = replay.validate_persisted_g2(payload, db, cfg)
            self.assertFalse(current)

    def test_current_failed_diagnostics_do_not_become_ready(self):
        cases = {
            "duplicate": (
                [(1.0, self._proposal()), (2.0, self._proposal())],
                {"matched": 0, "recorded": 1, "reproduction_rate": 0.0,
                 "duplicate_count": 1}),
            "malformed_json": (
                [(1.0, self._proposal()), (2.0, "not-json")],
                {"matched": 0, "recorded": 1, "reproduction_rate": 0.0,
                 "malformed_count": 1}),
            "malformed_identity": (
                [(1.0, {**self._proposal(), "setup_type": None})],
                {"matched": 0, "recorded": 0, "reproduction_rate": 0.0,
                 "malformed_count": 1, "vacuous": True}),
            "extra": (
                [(1.0, self._proposal())],
                {"matched": 0, "recorded": 1, "reproduction_rate": 0.0,
                 "extra_count": 1}),
            "unresolved": (
                [(1.0, self._proposal())],
                {"matched": 1, "recorded": 1, "reproduction_rate": 1.0,
                 "unresolved_count": 1}),
        }
        for label, (rows, report) in cases.items():
            with self.subTest(label=label):
                self._assert_current_failed(rows, report)

    def test_unresolved_outcomes_remain_diagnostic_for_proposal_pass(self):
        """Nightly proposal fidelity runs without an authoritative price lane."""
        cfg = valid_config()
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "journal.db"
            proposal = self._proposal()
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "CREATE TABLE events (ts REAL, kind TEXT, payload TEXT, "
                    "cycle_id TEXT, setup_id TEXT)")
                conn.execute(
                    "INSERT INTO events VALUES (?,?,?,?,?)",
                    (1.0, "setup_proposed", json.dumps(proposal), "c1", "s1"))
            result = SimpleNamespace(
                variant_id="momentum.baseline", mode="recorded_llm",
                digest=lambda: "digest")
            self.assertTrue(RESEARCH_CLI._record_g2_result(
                db, "PASS", {
                    "matched": 1, "recorded": 1,
                    "reproduction_rate": 1.0,
                    "unresolved_count": 1,
                }, result, cfg))
            self.assertEqual(readiness.gate_g2(db, {}, cfg).status,
                             readiness.PASS)

    def test_writer_and_readiness_agree_on_the_exact_corpus(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "journal.db"
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "CREATE TABLE events (ts REAL, kind TEXT, payload TEXT)")
                conn.executemany(
                    "INSERT INTO events VALUES (?,?,?)",
                    [(float(i), "setup_proposed", json.dumps({
                        "cycle_id": f"c{i}",
                        "symbol": "BTC/USDT:USDT", "direction": "long",
                        "setup_type": "trend_continuation",
                        "setup_id": f"s{i}", "setup_key": f"k{i}",
                        "signal_ts": i, "strategy_id": "momentum",
                        "strategy_version": "phase1-v3", "variant_id": "live",
                    })) for i in range(1, 101)])

            cfg = valid_config()
            stored = RESEARCH_CLI._record_g2_result(
                db, "PASS", {
                    "matched": 100, "recorded": 100,
                    "reproduction_rate": 1.0,
                    "vacuous": False, "legacy_identity": False,
                    "missing_count": 0, "extra_count": 0,
                    "malformed_count": 0, "duplicate_count": 0,
                    "unresolved_count": 0, "malformed_reasons": {},
                }, FakeResult(), cfg)
            gate = readiness.gate_g2(db, {}, cfg)

        self.assertTrue(stored)
        self.assertEqual(gate.status, readiness.PASS)
        self.assertIn("persisted", gate.detail)

    def _persist_pass_with_replay_corpus(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        db = Path(directory.name) / "journal.db"
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE events (ts REAL, kind TEXT, payload TEXT, "
                "cycle_id TEXT, setup_id TEXT)")
            for i in range(100):
                proposal = self._proposal(
                    cycle_id=f"c{i}", setup_id=f"s{i}", setup_key=f"k{i}",
                    signal_ts=i + 1)
                conn.execute(
                    "INSERT INTO events VALUES (?,?,?,?,?)",
                    (float(i + 1), "setup_proposed", json.dumps(proposal),
                     proposal["cycle_id"], proposal["setup_id"]))
            conn.execute(
                "INSERT INTO events VALUES (?,?,?,?,?)",
                (101.0, "llm_input", json.dumps({"decision": "hold"}),
                 "hold-cycle", None))
            conn.execute(
                "INSERT INTO events VALUES (?,?,?,?,?)",
                (102.0, "llm_output", json.dumps({"decision": "hold"}),
                 "hold-cycle", None))
        cfg = valid_config()
        result = FakeResult()
        report = {
            "matched": 100, "recorded": 100,
            "reproduction_rate": 1.0,
            "vacuous": False, "legacy_identity": False,
            "missing_count": 0, "extra_count": 0,
            "malformed_count": 0, "duplicate_count": 0,
            "unresolved_count": 0, "malformed_reasons": {},
        }
        self.assertTrue(RESEARCH_CLI._record_g2_result(
            db, "PASS", report, result, cfg))
        self.assertEqual(readiness.gate_g2(db, {}, cfg).status,
                         readiness.PASS)
        return db, cfg

    def test_persisted_pass_is_stale_when_hold_replay_corpus_is_appended(self):
        db, cfg = self._persist_pass_with_replay_corpus()
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO events VALUES (?,?,?,?,?)",
                (103.0, "llm_input", json.dumps({"decision": "hold",
                                                   "new": True}),
                 "hold-cycle-2", None))
        gate = readiness.gate_g2(db, {}, cfg)
        self.assertEqual(gate.status, readiness.READY)
        self.assertIn("replay corpus digest is stale", gate.detail)

    def test_persisted_pass_is_stale_when_hold_replay_corpus_is_mutated(self):
        db, cfg = self._persist_pass_with_replay_corpus()
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE events SET payload=? WHERE kind='llm_output'",
                (json.dumps({"decision": "hold", "mutated": True}),))
        gate = readiness.gate_g2(db, {}, cfg)
        self.assertEqual(gate.status, readiness.READY)
        self.assertIn("replay corpus digest is stale", gate.detail)

    def test_race_capture_cannot_persist_a_current_pass(self):
        db, cfg = self._persist_pass_with_replay_corpus()
        captured = replay.g2_evidence_metadata(db)
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO events VALUES (?,?,?,?,?)",
                (103.0, "llm_input", json.dumps({"decision": "hold",
                                                   "race": True}),
                 "hold-race", None))
        report = {
            "matched": 100, "recorded": 100,
            "reproduction_rate": 1.0,
            "missing_count": 0, "extra_count": 0,
            "malformed_count": 0, "duplicate_count": 0,
            "unresolved_count": 0, "malformed_reasons": {},
            **captured,
        }
        self.assertFalse(RESEARCH_CLI._record_g2_result(
            db, "PASS", report, FakeResult(), cfg, captured))
        with sqlite3.connect(db) as conn:
            raw = conn.execute(
                "SELECT payload FROM events "
                "WHERE kind='research_gate_result' ORDER BY rowid DESC "
                "LIMIT 1").fetchone()[0]
        self.assertEqual(json.loads(raw)["status"], "FAILED")
        gate = readiness.gate_g2(db, {}, cfg)
        self.assertEqual(gate.status, readiness.READY)
        self.assertIn("replay corpus", gate.detail)

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

    def test_capture_load_race_blocks_before_replay_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "journal.db"
            proposal = self._proposal()
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "CREATE TABLE events (ts REAL, kind TEXT, payload TEXT, "
                    "cycle_id TEXT, setup_id TEXT)")
                conn.execute(
                    "INSERT INTO events VALUES (?,?,?,?,?)",
                    (1.0, "setup_proposed", json.dumps(proposal), "c1", "s1"))

            def load_after_capture(_db):
                with sqlite3.connect(db) as conn:
                    conn.execute(
                        "INSERT INTO events VALUES (?,?,?,?,?)",
                        (2.0, "llm_input", json.dumps({"hold": True}),
                         "hold-race", None))
                return [], []

            args = SimpleNamespace(
                db=str(db), mode="demo", variant="momentum.baseline",
                replay_mode="recorded_llm", prices=None,
                check_fidelity=True, no_persist=True)
            with patch.object(RESEARCH_CLI, "_resolve_cfg",
                              return_value=valid_config()), \
                    patch.object(RESEARCH_CLI, "_corpus_for",
                                 side_effect=load_after_capture), \
                    patch.object(RESEARCH_CLI.replay_mod, "Replay") as replay_cls:
                status = RESEARCH_CLI.cmd_replay(args)

        self.assertEqual(status, 2)
        replay_cls.assert_not_called()

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
