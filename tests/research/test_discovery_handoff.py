import copy
import contextlib
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from research.discovery import SCHEMA, run_discovery, seed_forced_flow_pressure
from research.discovery_handoff import (
    HandoffError, build_discovery_handoff, load_persisted_discovery,
    validate_discovery_handoff, write_discovery_handoff)
from research.findings import FindingsStore


CONTRACT = {
    "strategy_id": "test",
    "strategy_version": "v1",
    "variant_id": "test.baseline",
    "semantic_hash": "a" * 64,
}


def complete_rows(count=12):
    rows = []
    for index in range(count):
        ts = (index + 1) * 3_600_000
        rows.append({
            "event_id": f"event-{index}",
            "snapshot_id": f"snapshot-{index}",
            "feed": "synthetic",
            "schema": "synthetic.v1",
            "payload_hash": f"{index + 1:064x}",
            "event_ts_ms": ts,
            "available_at_ms": ts,
            "oi_change_4h_pct": 1.0,
            "relative_volume_1h": 2.0,
            "imbalance_5bps": 0.2,
            "direction": "long",
            "entry_price": 100.0,
            "stop_pct": 2.0,
            "signal_ts": ts - 60_000,
            "entry_ts": ts,
            "funding_complete": True,
            "funding_provenance": {"complete": True, "event_ids": []},
            "bars": [{
                "event_id": f"bar-{index}-0",
                "event_ts_ms": ts,
                "ts": ts,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 101.0,
                "oi_change_4h_pct": 0.0,
                "relative_volume_1h": 2.0,
                "imbalance_5bps": 0.2,
            }, {
                "event_id": f"bar-{index}-1",
                "event_ts_ms": ts + 60_000,
                "ts": ts + 60_000,
                "open": 100.0,
                "high": 100.0,
                "low": 99.0,
                "close": 99.0,
                "oi_change_4h_pct": 0.0,
                "relative_volume_1h": 2.0,
                "imbalance_5bps": 0.2,
            }],
        })
    return rows


class DiscoveryHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = FindingsStore(self.root / "findings.db")
        self.artifacts = self.root / "artifacts"
        self.results = self.root / "results"
        self.out = self.root / "handoffs"
        cli_path = Path(__file__).parents[2] / "research.py"
        spec = importlib.util.spec_from_file_location(
            "research_cli_discovery_handoff", cli_path)
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)

    def run_cli(self, store, *, analysis_id=None):
        output, errors = io.StringIO(), io.StringIO()
        argv = [
            "prepare-discovery-handoff", "--store", str(store.path),
            "--results", str(self.results),
            "--artifacts", str(self.artifacts), "--out", str(self.out)]
        if analysis_id is not None:
            argv.extend(("--analysis-id", str(analysis_id)))
        args = self.cli.build_parser().parse_args(argv)
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = args.func(args)
        payload = json.loads(output.getvalue()) if output.getvalue() else None
        return status, payload, errors.getvalue()

    @staticmethod
    def insert_analysis(store, payload, *, subject_id="cli-fixture"):
        # Keep this helper independent of module globals: it is also used by
        # the CLI-only cases, where a dynamically loaded research module may
        # otherwise leave ``findings`` unresolved.
        from research import findings as findings_mod
        canonical, digest = findings_mod._analysis_canonical(
            "discovery", subject_id, payload)
        with sqlite3.connect(store.path) as conn:
            conn.execute(
                "INSERT INTO analysis_runs (analysis_id, kind, subject_id, ts, "
                "payload_json, payload_hash) VALUES (?,?,?,?,?,?)",
                (digest[:32], "discovery", subject_id, 1.0, canonical, digest))
        return digest[:32]

    def complete(self):
        return run_discovery(
            complete_rows(), result_root=self.artifacts,
            analysis_root=self.results, findings_store=self.store,
            program=seed_forced_flow_pressure(), contract_identity=CONTRACT)

    def test_complete_result_builds_verified_non_authorizing_handoff(self):
        result = self.complete()
        self.assertEqual(result["status"], "COMPLETE")
        evidence = load_persisted_discovery(
            self.store, result_root=self.results,
            artifact_root=self.artifacts)
        handoff = build_discovery_handoff(
            evidence, analysis_id=evidence["analysis_id"],
            analysis_payload_hash=evidence["analysis_payload_hash"])
        self.assertEqual(handoff["authority"]["future_registration_identity"],
                         "HUMAN_DECISION_REQUIRED")
        self.assertFalse(handoff["authority"]["live_authority"])
        self.assertFalse(handoff["requested_action"]["registry_mutation_performed"])
        self.assertEqual(validate_discovery_handoff(handoff), handoff)

        first = write_discovery_handoff(handoff, self.out)
        second = write_discovery_handoff(handoff, self.out)
        self.assertEqual(first["status"], "CREATED")
        self.assertEqual(second["status"], "EXISTING")
        self.assertEqual(first["path"], second["path"])
        self.assertTrue(Path(first["path"]).is_file())

    def test_incomplete_and_falsified_results_are_rejected(self):
        result = self.complete()
        # Persisted analysis rows are immutable; a copied result is enough to
        # prove the gate without mutating the authoritative store.
        incomplete = copy.deepcopy(result)
        incomplete["status"] = "NO_STATE_DATA"
        with self.assertRaises(HandoffError):
            from research.discovery_handoff import _validate_result
            _validate_result(incomplete, result_root=self.results,
                             artifact_root=self.artifacts)
        falsified = copy.deepcopy(result)
        falsified["verdict"] = "FALSIFIED"
        with self.assertRaises(HandoffError):
            from research.discovery_handoff import _validate_result
            _validate_result(falsified, result_root=self.results,
                             artifact_root=self.artifacts)

    def test_tampered_event_evidence_and_result_hash_fail_closed(self):
        result = self.complete()
        evidence = load_persisted_discovery(
            self.store, result_root=self.results,
            artifact_root=self.artifacts)
        tampered = copy.deepcopy(evidence["result"])
        tampered["event_join_digest"] = "0" * 64
        with self.assertRaises(HandoffError):
            from research.discovery_handoff import _validate_result
            _validate_result(tampered, result_root=self.results,
                             artifact_root=self.artifacts)
        path = evidence["result_path"]
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["rows"] = int(payload["rows"]) + 1
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(HandoffError):
            load_persisted_discovery(
                self.store, result_root=self.results,
                artifact_root=self.artifacts)

    def test_cli_automatic_mode_fails_closed_for_missing_incomplete_or_falsified(self):
        cases = (
            ("no-analysis", lambda store: None,
             "NO_COMPLETE_DISCOVERY", None),
            ("incomplete", lambda store: run_discovery(
                [], result_root=self.artifacts, analysis_root=self.results,
                findings_store=store, program=seed_forced_flow_pressure(),
                contract_identity=CONTRACT),
             "NOT_HANDOFF_ELIGIBLE", None),
            ("falsified", lambda store: self.insert_analysis(store, {
                "schema": SCHEMA, "status": "COMPLETE",
                "verdict": "FALSIFIED"}),
             "NOT_HANDOFF_ELIGIBLE", "explicit-analysis"),
        )
        for name, create, expected, explicit in cases:
            with self.subTest(name=name):
                store = FindingsStore(self.root / f"{name}.db")
                store.schema_version()  # create the empty store for CLI discovery
                analysis_id = create(store)
                status, payload, errors = self.run_cli(
                    store, analysis_id=(analysis_id if explicit else None))
                if explicit:
                    self.assertEqual(status, 2)
                    self.assertIsNone(payload)
                    self.assertIn("VALIDATION_FAILED", errors)
                else:
                    self.assertEqual(status, 0)
                    self.assertEqual(payload["status"], expected)
                    self.assertEqual(payload["prepared"], [])

    def test_cli_reports_missing_store_separately_from_empty_store(self):
        missing = self.root / "missing-findings.db"
        output, errors = io.StringIO(), io.StringIO()
        argv = [
            "prepare-discovery-handoff", "--store", str(missing),
            "--results", str(self.results), "--artifacts", str(self.artifacts),
            "--out", str(self.out)]
        args = self.cli.build_parser().parse_args(argv)
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = args.func(args)
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["status"],
                         "NO_DISCOVERY_STORE")


if __name__ == "__main__":
    unittest.main()
