"""Content-addressed research evidence and truthful golden replay coverage."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from research import evidence_package as evidence
from research import evidence_cli


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "evidence"
GOLDEN = FIXTURES / "golden_replay_synthetic.json"
EXPECTED = FIXTURES / "golden_replay_expected.json"


class AuthoritativePackageFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "agent").mkdir()
        self.strategy = self.root / "agent" / "contract.py"
        self.forward = self.root / "agent" / "forward.py"
        self.strategy.write_text("STRATEGY = 'exact-v1'\n", encoding="utf-8")
        self.forward.write_text("FORWARD = 'fixed-rr-v1'\n", encoding="utf-8")
        self.dataset = self.root / "dataset.jsonl"
        self.dataset.write_text('{"cycle_id":"one"}\n', encoding="utf-8")
        self.config = self.root / "config.json"
        self.config.write_text(
            '{"strategy":{"id":"momentum","version":"phase1-v3"}}\n',
            encoding="utf-8")
        self.output = self.root / "result.json"
        self.output.write_text('{"expectancy_r":-0.1}\n', encoding="utf-8")
        self.store = self.root / "evidence"

    def tearDown(self):
        self.temporary.cleanup()

    def spec(self, **overrides):
        values = {
            "dataset": evidence.DatasetEvidence(
                path=self.dataset,
                format="jsonl",
                provenance={
                    "classification": "synthetic",
                    "generator": "unit-test vector",
                    "disclaimer": "Synthetic evidence; not observed market data.",
                },
                expected_records=1,
                observed_records=1,
                gaps=(),
            ),
            "code_root": self.root,
            "code_files": ("agent/contract.py", "agent/forward.py"),
            "strategy_contract": evidence.ContractEvidence(
                "momentum.phase1-v3", ("agent/contract.py",)),
            "forward_contract": evidence.ContractEvidence(
                "momentum.fixed-rr-v1", ("agent/forward.py",)),
            "config_path": self.config,
            "config_identity": {
                "strategy_id": "momentum",
                "strategy_version": "phase1-v3",
                "config_id": "fixture-config-v1",
            },
            "economics": {
                "fees": {"taker_pct_per_side": 0.05},
                "slippage": {"stop_pct": 0.15},
                "funding": {"intervals_held": 1},
            },
            "prompt": evidence.PromptEvidence(
                applicable=False,
                reason="deterministic contract replay; no model call"),
            "runtime": {
                "python": "test-runtime",
                "environment": "offline",
            },
            "command": ("python", "-m", "research.replay", "fixture"),
            "outputs": (self.output,),
            "timestamps": {
                "started_at": "2026-08-07T00:00:00+00:00",
                "completed_at": "2026-08-07T00:00:01+00:00",
            },
            "parent_evidence_ids": ("a" * 64,),
        }
        values.update(overrides)
        return evidence.EvidenceSpec(**values)


class ContentAddressedEvidenceTests(AuthoritativePackageFixture):
    def test_manifest_and_package_path_are_deterministic_and_research_only(self):
        first = evidence.create_evidence_package(self.store, self.spec())
        second = evidence.create_evidence_package(self.store, self.spec())

        self.assertEqual(first, second)
        manifest = evidence.verify_evidence_package(
            first, code_root=self.root, config_path=self.config)
        self.assertEqual(first.name, manifest["evidence_id"])
        self.assertEqual(len(first.name), 64)
        self.assertEqual(manifest["purpose"], "research_evidence_only")
        self.assertEqual(manifest["capabilities"], {
            "promotion": False, "live_trading": False})
        self.assertEqual(
            manifest["dataset"]["provenance"]["classification"],
            "synthetic")
        self.assertEqual(
            manifest["parent_evidence_ids"], ["a" * 64])

    def test_tampered_or_missing_material_is_rejected(self):
        for defect in ("tamper", "missing"):
            with self.subTest(defect=defect):
                package = evidence.create_evidence_package(
                    self.store / defect, self.spec())
                manifest = json.loads(
                    (package / "manifest.json").read_text(encoding="utf-8"))
                digest = manifest["dataset"]["artifact"]["sha256"]
                blob = package / "blobs" / digest
                if defect == "tamper":
                    blob.write_text("changed\n", encoding="utf-8")
                else:
                    blob.unlink()
                with self.assertRaises(evidence.EvidenceValidationError):
                    evidence.verify_evidence_package(package)

    def test_current_code_and_config_drift_are_rejected(self):
        package = evidence.create_evidence_package(self.store, self.spec())
        self.strategy.write_text("STRATEGY = 'drift'\n", encoding="utf-8")
        with self.assertRaisesRegex(
                evidence.EvidenceValidationError, "code tree"):
            evidence.verify_evidence_package(package, code_root=self.root)

        self.strategy.write_text("STRATEGY = 'exact-v1'\n", encoding="utf-8")
        self.config.write_text('{"changed":true}\n', encoding="utf-8")
        with self.assertRaisesRegex(
                evidence.EvidenceValidationError, "config"):
            evidence.verify_evidence_package(
                package, code_root=self.root, config_path=self.config)

    def test_gappy_incomplete_and_unlabelled_datasets_fail_closed(self):
        defects = (
            evidence.DatasetEvidence(
                self.dataset, "jsonl", {
                    "classification": "synthetic",
                    "generator": "test",
                    "disclaimer": "Synthetic only."},
                expected_records=2, observed_records=1),
            evidence.DatasetEvidence(
                self.dataset, "jsonl", {
                    "classification": "synthetic",
                    "generator": "test",
                    "disclaimer": "Synthetic only."},
                expected_records=1, observed_records=1,
                gaps=({"from": 1, "to": 2},)),
            evidence.DatasetEvidence(
                self.dataset, "jsonl", {
                    "generator": "test",
                    "disclaimer": "Synthetic only."},
                expected_records=1, observed_records=1),
        )
        for index, dataset in enumerate(defects):
            with self.subTest(index=index), self.assertRaises(
                    evidence.EvidenceValidationError):
                evidence.create_evidence_package(
                    self.store / str(index), self.spec(dataset=dataset))

    def test_prompt_identity_is_exact_when_a_model_was_applicable(self):
        prompt = self.root / "prompt.txt"
        inputs = self.root / "prompt-inputs.json"
        prompt.write_text("select no trades", encoding="utf-8")
        inputs.write_text('{"snapshot":"synthetic"}\n', encoding="utf-8")
        package = evidence.create_evidence_package(
            self.store,
            self.spec(prompt=evidence.PromptEvidence(
                applicable=True, provider="openai", model="test-model",
                prompt_path=prompt, inputs_path=inputs)))
        manifest = evidence.verify_evidence_package(package)
        self.assertTrue(manifest["prompt"]["applicable"])
        self.assertEqual(manifest["prompt"]["provider"], "openai")
        self.assertEqual(len(manifest["prompt"]["prompt"]["sha256"]), 64)


class TruthfulGoldenReplayTests(unittest.TestCase):
    @staticmethod
    def _write_replay_scope(root: Path, scope: tuple[str, ...]) -> None:
        for relative in scope:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if relative == "agent/registry.py":
                source = (
                    "REGISTRY_ENTRY = ('momentum', 'phase1-v3')\n"
                    "class Spec:\n"
                    "    tier = 'T0_REJECTED'\n"
                    "def lookup():\n"
                    "    return REGISTRY_ENTRY\n")
            elif relative == "agent/risk.py":
                source = (
                    "MAX_LEVERAGE = 3\n"
                    "def vet(value):\n"
                    "    return value <= MAX_LEVERAGE\n")
            else:
                source = f"REPLAY_SOURCE = {relative!r}\n"
            path.write_text(source, encoding="utf-8")

    def test_checked_in_synthetic_replay_matches_expected_output_twice(self):
        first = evidence.verify_golden_replay(GOLDEN, EXPECTED)
        second = evidence.verify_golden_replay(GOLDEN, EXPECTED)

        self.assertEqual(first, second)
        self.assertEqual(first["provenance_classification"], "synthetic")
        self.assertEqual(first["funnel"]["executed"], 1)
        self.assertEqual(
            first["replay_code"]["normalization"],
            evidence.GOLDEN_REPLAY_NORMALIZATION)
        self.assertTrue(first["decisions"])

    def test_unrelated_source_does_not_invalidate_golden_replay_code(self):
        fixture = evidence.load_replay_fixture(GOLDEN)
        scope = evidence.golden_replay_code_scope(fixture["config"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_replay_scope(root, scope)
            first = evidence.golden_replay_code_fingerprint(
                fixture["config"], root=root)

            unrelated = root / "agent" / "staging.py"
            unrelated.write_text("UNRELATED = 1\n", encoding="utf-8")
            second = evidence.golden_replay_code_fingerprint(
                fixture["config"], root=root)

            self.assertEqual(first, second)

    def test_comments_and_docstrings_do_not_change_replay_code_identity(self):
        fixture = evidence.load_replay_fixture(GOLDEN)
        scope = evidence.golden_replay_code_scope(fixture["config"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_replay_scope(root, scope)
            first = evidence.golden_replay_code_fingerprint(
                fixture["config"], root=root)
            registry = root / "agent" / "registry.py"
            registry.write_text(
                '"""Registry documentation only."""\n'
                "# A non-executable registry comment.\n"
                "REGISTRY_ENTRY = ('momentum', 'phase1-v3')\n"
                "class Spec:\n"
                '    """Specification documentation."""\n'
                "    tier = 'T0_REJECTED'\n"
                "def lookup():\n"
                '    """Return the registered entry."""\n'
                "    return REGISTRY_ENTRY  # same executable expression\n",
                encoding="utf-8")
            risk = root / "agent" / "risk.py"
            risk.write_text(
                "# Risk comments do not execute.\n"
                "MAX_LEVERAGE = 3\n"
                "def vet(value):\n"
                '    """Risk helper documentation."""\n'
                "    return value <= MAX_LEVERAGE\n",
                encoding="utf-8")
            second = evidence.golden_replay_code_fingerprint(
                fixture["config"], root=root)
            self.assertEqual(first, second)

    def test_executable_registry_and_risk_changes_invalidate_replay_code(self):
        fixture = evidence.load_replay_fixture(GOLDEN)
        scope = evidence.golden_replay_code_scope(fixture["config"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_replay_scope(root, scope)
            baseline = evidence.golden_replay_code_fingerprint(
                fixture["config"], root=root)

            registry = root / "agent" / "registry.py"
            registry.write_text(
                registry.read_text(encoding="utf-8").replace(
                    "phase1-v3", "phase1-v4"), encoding="utf-8")
            changed_registry = evidence.golden_replay_code_fingerprint(
                fixture["config"], root=root)
            self.assertNotEqual(
                baseline["sha256"], changed_registry["sha256"])

            self._write_replay_scope(root, scope)
            risk = root / "agent" / "risk.py"
            risk.write_text(
                risk.read_text(encoding="utf-8").replace("<=", "<"),
                encoding="utf-8")
            changed_risk = evidence.golden_replay_code_fingerprint(
                fixture["config"], root=root)
            self.assertNotEqual(baseline["sha256"], changed_risk["sha256"])

    def test_synthetic_fixture_is_rejected_when_called_real_market(self):
        with self.assertRaisesRegex(
                evidence.EvidenceValidationError, "not real_market"):
            evidence.load_replay_fixture(
                GOLDEN, require_classification="real_market")

    def test_import_command_cannot_upgrade_synthetic_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "imported.json"
            with self.assertRaisesRegex(
                    evidence.EvidenceValidationError,
                    "cannot be relabelled real_market"):
                evidence.import_replay_fixture(
                    GOLDEN, destination, classification="real_market",
                    provenance={
                        "classification": "real_market",
                        "venue": "OKX",
                        "source": "unsupported assertion",
                        "collection_started_at":
                            "2026-08-07T00:00:00+00:00",
                        "collection_completed_at":
                            "2026-08-07T00:01:00+00:00",
                        "source_sha256": evidence.sha256_file(GOLDEN),
                    })
            self.assertFalse(destination.exists())

    def test_cli_reports_rejected_synthetic_to_real_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "imported.json"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                status = evidence_cli.main([
                    "import-replay", str(GOLDEN), str(destination),
                    "--classification", "real_market",
                ])
            self.assertEqual(status, 2)
            self.assertIn("cannot be relabelled real_market", stderr.getvalue())
            self.assertFalse(destination.exists())

    def test_synthetic_import_is_atomic_sanitized_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "imported.json"
            evidence.import_replay_fixture(
                GOLDEN, destination, classification="synthetic")
            imported = evidence.load_replay_fixture(destination)
            self.assertEqual(
                imported["provenance"]["classification"], "synthetic")
            self.assertNotIn("webhook_url_env", imported["config"]["alerts"])
            with self.assertRaisesRegex(
                    evidence.EvidenceValidationError, "overwrite"):
                evidence.import_replay_fixture(
                    GOLDEN, destination, classification="synthetic")


if __name__ == "__main__":
    unittest.main()
