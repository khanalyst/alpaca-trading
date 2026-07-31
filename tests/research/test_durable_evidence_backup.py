"""Tournament history and backups are durable evidence, not latest-only files."""

from __future__ import annotations

import copy
import contextlib
import hashlib
import importlib.util
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from research import backup, tournament
from research import findings as findings_mod
from research.findings import FindingsStore, SCHEMA_VERSION


REPO = Path(__file__).resolve().parents[2]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tournament_row() -> dict:
    return {
        "strategy_id": "momentum",
        "version": "phase1-v3",
        "scored": True,
        "registered_tier": "T0_REJECTED",
        "measured_tier": "T0_REJECTED",
        "tier_reason": "negative expectancy reproduces the benchmark",
        "exploratory_ceiling": "T2_CANDIDATE",
        "beyond_exploratory_authority": False,
        "tier_changed": False,
        "hypotheses_tested": 1,
        "mechanism": "Price continuation should persist after confirmation.",
        "falsification": "Negative expectancy falsifies the mechanism.",
        "headline": {
            "trades": 100,
            "expectancy_pct": -0.10,
            "expectancy_r": -0.096,
        },
        "gates": [],
        "settings_tested": 1,
        "best_setting": "registered",
        "settings": [{
            "setting_id": "registered",
            "params": {},
            "claim": "the pre-registered parameterisation",
            "registered": True,
            "tier": "T0_REJECTED",
            "why": "negative expectancy",
            "trades": 100,
            "expectancy_pct": -0.10,
            "expectancy_r": -0.096,
            "p_adjusted": None,
            "significant_corrected": False,
            "gates": [],
        }],
    }


class TournamentHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        (self.data / "manifest.json").write_text(
            json.dumps({
                "schema": tournament.DATA_MANIFEST_SCHEMA,
                "files": [],
                "fixture": True,
            }) + "\n", encoding="utf-8")
        self.out = self.root / "tournament"
        self.store_path = self.root / "findings.db"
        self.frame = SimpleNamespace(
            ts=[1_700_000_000_000, 1_700_086_400_000],
            df=[{}, {}])

    def run_success(self) -> int:
        with patch.object(
                tournament, "load_dataset",
                return_value={"BTC/USDT:USDT": self.frame}), \
                patch.object(tournament, "universe_membership", return_value={}), \
                patch.object(
                    tournament, "score_strategy",
                    side_effect=lambda *args, **kwargs: copy.deepcopy(
                        tournament_row())):
            return tournament.main([
                "--data", str(self.data),
                "--out", str(self.out),
                "--store", str(self.store_path),
                "--only", "momentum",
                "--workers", "1",
                "--min-bars", "1",
            ])

    def test_two_runs_preserve_history_and_structured_results(self):
        self.assertEqual(self.run_success(), 0)
        first_directory = next((self.out / "runs").iterdir())
        first_report = (first_directory / "REPORT.md").read_bytes()

        self.assertEqual(self.run_success(), 0)

        directories = sorted((self.out / "runs").iterdir())
        self.assertEqual(len(directories), 2)
        self.assertNotEqual(directories[0].name, directories[1].name)
        self.assertEqual((first_directory / "REPORT.md").read_bytes(),
                         first_report)
        runs = FindingsStore(self.store_path).tournament_runs()
        self.assertEqual([row["status"] for row in runs],
                         ["SUCCEEDED", "SUCCEEDED"])
        self.assertEqual(len({row["run_id"] for row in runs}), 2)
        self.assertEqual(len({row["content_id"] for row in runs}), 1)
        for row in runs:
            results = FindingsStore(self.store_path).tournament_results(
                row["run_id"])
            self.assertEqual(len(results["strategies"]), 1)
            self.assertEqual(len(results["settings"]), 1)
            self.assertEqual(results["strategies"][0]["verdict"],
                             "T0_REJECTED")
            self.assertEqual(results["settings"][0]["setting_id"],
                             "registered")
        latest = json.loads((self.out / "leaderboard.json").read_text())
        self.assertEqual(latest["run_id"], runs[-1]["run_id"])
        self.assertTrue((first_directory / "INVOCATION.json").exists())
        self.assertTrue((first_directory / "INPUTS.json").exists())

        with sqlite3.connect(self.store_path) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE tournament_runs SET content_id='changed' "
                    "WHERE run_id=?", (runs[0]["run_id"],))

    def test_failed_run_is_persisted_with_artifacts_and_error(self):
        with patch.object(
                tournament, "load_dataset",
                side_effect=RuntimeError("fixture scoring interrupted")):
            status = tournament.main([
                "--data", str(self.data),
                "--out", str(self.out),
                "--store", str(self.store_path),
                "--only", "momentum",
                "--min-bars", "1",
            ])

        self.assertEqual(status, 1)
        run = FindingsStore(self.store_path).tournament_runs()[0]
        self.assertEqual(run["status"], "FAILED")
        directory = Path(run["run_directory"])
        completion = json.loads((directory / "COMPLETION.json").read_text())
        errors = json.loads((directory / "ERRORS.json").read_text())
        self.assertEqual(completion["status"], "FAILED")
        self.assertIn("interrupted", errors[0]["message"])
        self.assertTrue((directory / "REPORT.md").exists())
        self.assertTrue((directory / "leaderboard.json").exists())


class SchemaMigrationTests(unittest.TestCase):
    def test_schema_12_migrates_without_rewriting_accepted_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "findings.db"
            with patch.object(findings_mod, "SCHEMA_VERSION", 12):
                schema12 = FindingsStore(path)
                self.assertEqual(schema12.schema_version(), 12)
            with sqlite3.connect(path) as conn:
                before = conn.execute(
                    "SELECT version, name FROM schema_migrations "
                    "WHERE version BETWEEN 9 AND 12 ORDER BY version").fetchall()

            migrated = FindingsStore(path)

            self.assertEqual(migrated.schema_version(), SCHEMA_VERSION)
            with sqlite3.connect(path) as conn:
                after = conn.execute(
                    "SELECT version, name FROM schema_migrations "
                    "WHERE version BETWEEN 9 AND 12 ORDER BY version").fetchall()
            self.assertEqual(after, before)

    def test_schema_13_unproven_external_history_becomes_configured_local(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "findings.db"
            with patch.object(findings_mod, "SCHEMA_VERSION", 13):
                self.assertEqual(FindingsStore(path).schema_version(), 13)
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "INSERT INTO backup_runs (backup_id, request_content_id, "
                    "started_ts, target_root, target_kind, require_external, "
                    "command_json, options_json, source_paths_json) "
                    "VALUES ('legacy', 'request', 1, '/configured', "
                    "'external_configured', 0, '[]', '{}', '[]')")
                conn.execute(
                    "INSERT INTO backup_run_events (event_id, backup_id, "
                    "status, event_ts, detail_json) VALUES "
                    "('started', 'legacy', 'STARTED', 1, '{}')")

            migrated = FindingsStore(path)
            run = migrated.backup_runs()[0]

            self.assertEqual(migrated.schema_version(), SCHEMA_VERSION)
            self.assertEqual(run["target_kind"], "configured_local")
            self.assertFalse(run["target_evidence"]["external_proven"])
            self.assertIsNone(migrated.latest_verified_external_backup())


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store_path = self.root / "findings.db"
        self.store = FindingsStore(self.store_path)
        self.store.schema_version()

        self.journal = self.root / "journal.db"
        self.writer = sqlite3.connect(self.journal)
        self.addCleanup(self.writer.close)
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.execute("PRAGMA wal_autocheckpoint=0")
        self.writer.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, value TEXT)")
        self.writer.commit()
        self.writer.execute("INSERT INTO events (value) VALUES ('in-wal')")
        self.writer.commit()

        self.runtime_research = self.root / "runtime-research"
        recorded = self.runtime_research / "recorded" / "order_book"
        recorded.mkdir(parents=True)
        self.recorder = recorded / "2026-07-31.csv"
        self.recorder.write_text("ts,bid,ask\n1,10,11\n", encoding="utf-8")
        data = self.runtime_research / "data"
        data.mkdir()
        self.data_manifest = data / "manifest.json"
        self.data_manifest.write_text('{"window":"test"}\n', encoding="utf-8")

        self.results = self.root / "results"
        result_run = self.results / "tournament" / "runs" / "run-a"
        result_run.mkdir(parents=True)
        self.result_report = result_run / "REPORT.md"
        self.result_report.write_text("# durable result\n", encoding="utf-8")
        self.target = self.root / "configured-target"
        self.target.mkdir()

    def create(self, *, require_external: bool = False) -> dict:
        return backup.create_backup(
            store_path=self.store_path,
            target=self.target,
            target_configured=True,
            journal_path=self.journal,
            runtime_research_root=self.runtime_research,
            results_root=self.results,
            require_external=require_external,
            command=["research.py", "backup"],
            options={"fixture": True})

    def readiness(self) -> tuple[int, str]:
        spec = importlib.util.spec_from_file_location(
            "research_cli_durability_test", REPO / "research.py")
        cli = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cli)
        output = io.StringIO()
        with patch.object(cli, "_load_config", return_value={
                "research": {"findings_store": str(self.store_path)}}), \
                patch.object(cli.readiness_mod, "report", return_value=([], {})), \
                patch.object(cli.readiness_mod, "format_report",
                             return_value="base readiness"), \
                contextlib.redirect_stdout(output):
            status = cli.cmd_readiness(SimpleNamespace(
                db=str(self.root / "absent-journal.db"), mode="demo"))
        return status, output.getvalue()

    def test_wal_consistent_verified_backup_and_two_histories(self):
        source_hashes = {
            path: sha256(path) for path in (
                self.recorder, self.data_manifest, self.result_report)
        }
        first = self.create()
        second = self.create()

        self.assertNotEqual(first["backup_id"], second["backup_id"])
        self.assertNotEqual(first["backup_path"], second["backup_path"])
        self.assertTrue(Path(first["backup_path"]).is_dir())
        self.assertTrue(Path(second["backup_path"]).is_dir())
        self.assertTrue(backup.verify_backup(first["backup_path"])["ok"])
        journal_snapshot = (
            Path(first["backup_path"]) / "sqlite" / "journal.db")
        restored_journal = self.root / "restored-journal.db"
        with sqlite3.connect(journal_snapshot) as source, \
                sqlite3.connect(restored_journal) as destination:
            source.backup(destination)
        with sqlite3.connect(restored_journal) as conn:
            self.assertEqual(conn.execute(
                "SELECT value FROM events").fetchall(), [("in-wal",)])
        self.assertEqual(
            [(row["target_kind"], row["status"])
             for row in self.store.backup_runs()],
            [("configured_local", "VERIFIED"),
             ("configured_local", "VERIFIED")])
        self.assertIsNone(self.store.latest_verified_external_backup())
        self.assertFalse(first["target_evidence"]["external_proven"])
        readiness_status, readiness_output = self.readiness()
        self.assertEqual(readiness_status, 2)
        self.assertIn("external backup  BLOCKED", readiness_output)
        for path, before in source_hashes.items():
            self.assertEqual(sha256(path), before)
        self.assertEqual(self.writer.execute(
            "SELECT COUNT(*) FROM events").fetchone()[0], 1)

        with sqlite3.connect(self.store_path) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE backup_runs SET target_kind='local_default' "
                    "WHERE backup_id=?", (first["backup_id"],))

    def test_tamper_and_manifest_checksum_failures_are_recorded(self):
        first = self.create()
        first_root = Path(first["backup_path"])
        copied_report = first_root / (
            "files/research/results/tournament/runs/run-a/REPORT.md")
        copied_report.write_text("tampered\n", encoding="utf-8")
        payload_failure = backup.verify_and_record(
            first_root, store_path=self.store_path)
        self.assertFalse(payload_failure["ok"])
        self.assertTrue(any("checksum mismatch" in error
                            for error in payload_failure["errors"]))

        second = self.create()
        second_root = Path(second["backup_path"])
        manifest = second_root / backup.MANIFEST_NAME
        manifest.write_text(manifest.read_text() + "\n", encoding="utf-8")
        manifest_failure = backup.verify_and_record(
            second_root, store_path=self.store_path)
        self.assertFalse(manifest_failure["ok"])
        self.assertIn("manifest checksum mismatch", manifest_failure["errors"])
        self.assertEqual(
            [row["status"] for row in self.store.backup_verifications()],
            ["VERIFIED", "FAILED", "VERIFIED", "FAILED"])

    def test_same_device_and_missing_targets_fail_external_gate_closed(self):
        with self.assertRaises(backup.BackupError):
            backup.create_backup(
                store_path=self.store_path,
                target=None,
                target_configured=False,
                journal_path=self.journal,
                runtime_research_root=self.runtime_research,
                results_root=self.results,
                require_external=True)
        with self.assertRaises(backup.BackupError) as same_device:
            self.create(require_external=True)
        self.assertEqual(
            same_device.exception.result["target_kind"], "configured_local")
        self.assertFalse(
            same_device.exception.result["target_evidence"]["external_proven"])
        missing = self.root / "not-mounted"
        with self.assertRaises(backup.BackupError):
            backup.create_backup(
                store_path=self.store_path,
                target=missing,
                target_configured=True,
                journal_path=self.journal,
                runtime_research_root=self.runtime_research,
                results_root=self.results)

        self.assertFalse(missing.exists())
        self.assertEqual([row["status"] for row in self.store.backup_runs()],
                         ["FAILED", "FAILED", "FAILED"])
        self.assertIsNone(self.store.latest_verified_external_backup())

    def test_mocked_different_device_is_external_and_satisfies_gate(self):
        actual_device_id = backup._device_id
        target = self.target.resolve()
        source_device = actual_device_id(REPO)

        def device_id(path: Path) -> int:
            if Path(path).resolve() == target:
                return source_device + 1
            return actual_device_id(Path(path))

        with patch.object(backup, "_device_id", side_effect=device_id):
            result = self.create(require_external=True)

        self.assertEqual(result["target_kind"], "external_mounted")
        self.assertTrue(result["target_evidence"]["external_proven"])
        latest = self.store.latest_verified_external_backup()
        self.assertIsNotNone(latest)
        self.assertEqual(latest["backup_id"], result["backup_id"])
        readiness_status, readiness_output = self.readiness()
        self.assertEqual(readiness_status, 0)
        self.assertIn("external backup  PASS", readiness_output)
        manifest = json.loads((
            Path(result["backup_path"]) / backup.MANIFEST_NAME).read_text())
        self.assertEqual(manifest["target_kind"], "external_mounted")
        self.assertTrue(manifest["target_evidence"]["external_proven"])

    def test_local_default_backup_is_clearly_local(self):
        local_target = self.root / "local-default"
        with patch.object(backup, "DEFAULT_TARGET", local_target):
            result = backup.create_backup(
                store_path=self.store_path,
                target=None,
                target_configured=False,
                journal_path=self.journal,
                runtime_research_root=self.runtime_research,
                results_root=self.results)

        self.assertTrue(result["ok"])
        self.assertEqual(result["target_kind"], "local_default")
        self.assertTrue(local_target.is_dir())
        self.assertIsNone(self.store.latest_verified_external_backup())


class VmFixtureTests(unittest.TestCase):
    def test_copied_vm_export_migrates_and_backs_up_without_touching_source(self):
        fixture = REPO / "vm-import" / "2026-07-30"
        findings_source = fixture / "okx-findings-2026-07-30.db"
        journal_source = fixture / "okx-demo-journal-2026-07-30.db"
        if not findings_source.exists() or not journal_source.exists():
            self.skipTest("supplied VM export fixture is absent")
        source_hashes = {
            findings_source: sha256(findings_source),
            journal_source: sha256(journal_source),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for source in (
                    findings_source,
                    findings_source.with_name(findings_source.name + "-wal"),
                    findings_source.with_name(findings_source.name + "-shm"),
                    journal_source,
                    journal_source.with_name(journal_source.name + "-wal"),
                    journal_source.with_name(journal_source.name + "-shm")):
                if source.exists():
                    shutil.copy2(source, root / source.name)
            copied_findings = root / findings_source.name
            copied_journal = root / journal_source.name
            self.assertEqual(
                FindingsStore(copied_findings).schema_version(), SCHEMA_VERSION)
            target = root / "mounted-target"
            target.mkdir()
            runtime_research = root / "runtime-research"
            results = root / "results"
            runtime_research.mkdir()
            results.mkdir()
            result = backup.create_backup(
                store_path=copied_findings,
                target=target,
                target_configured=True,
                journal_path=copied_journal,
                runtime_research_root=runtime_research,
                results_root=results)
            self.assertTrue(result["ok"])
            restored = Path(result["backup_path"]) / "sqlite" / "findings.db"
            self.assertEqual(
                FindingsStore(restored).schema_version(), SCHEMA_VERSION)

        for path, before in source_hashes.items():
            self.assertEqual(sha256(path), before)


class NightlyWiringTests(unittest.TestCase):
    def test_nightly_passes_store_and_surfaces_backup_failure(self):
        script = (REPO / "research" / "nightly.sh").read_text()
        self.assertIn(
            'research/tournament.py --data "$DATA" --store "$STORE"', script)
        self.assertIn('backup --store "$STORE" --journal "$JOURNAL"', script)
        self.assertIn("backup_status=$?", script)
        self.assertIn("status=5", script)
        self.assertIn("okx-trader.service", script)


if __name__ == "__main__":
    unittest.main()
