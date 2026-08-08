"""Content-addressed research evidence and truthful golden replay coverage."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        # Parent references are real content-addressed packages.  Keeping a
        # concrete parent in the fixture exercises ancestry resolution instead
        # of allowing a placeholder digest to bypass it.
        self.parent = evidence.create_evidence_package(
            self.store, self.spec(parent_evidence_ids=()))
        self.parent_id = self.parent.name

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
            "parent_evidence_ids": (
                getattr(self, "parent_id", None),
            ) if getattr(self, "parent_id", None) else (),
        }
        values.update(overrides)
        return evidence.EvidenceSpec(**values)

    def parent_lookup(self, evidence_id):
        return self.store / str(evidence_id)

    def create(self, store=None, spec=None):
        return evidence.create_evidence_package(
            store or self.store, spec or self.spec(),
            parent_lookup=self.parent_lookup)

    def verify(self, package, **kwargs):
        return evidence.verify_evidence_package(
            package, parent_lookup=self.parent_lookup, **kwargs)


class ContentAddressedEvidenceTests(AuthoritativePackageFixture):
    def test_manifest_and_package_path_are_deterministic_and_research_only(self):
        first = self.create()
        second = self.create()

        self.assertEqual(first, second)
        manifest = self.verify(
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
            manifest["parent_evidence_ids"], [self.parent_id])

    def test_tampered_or_missing_material_is_rejected(self):
        for defect in ("tamper", "missing"):
            with self.subTest(defect=defect):
                package = evidence.create_evidence_package(
                    self.store / defect, self.spec(),
                    parent_lookup=self.parent_lookup)
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
        package = self.create()
        self.strategy.write_text("STRATEGY = 'drift'\n", encoding="utf-8")
        with self.assertRaisesRegex(
                evidence.EvidenceValidationError, "code tree"):
            self.verify(package, code_root=self.root)

        self.strategy.write_text("STRATEGY = 'exact-v1'\n", encoding="utf-8")
        self.config.write_text('{"changed":true}\n', encoding="utf-8")
        with self.assertRaisesRegex(
                evidence.EvidenceValidationError, "config"):
            self.verify(
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
                    self.store / str(index), self.spec(dataset=dataset),
                    parent_lookup=self.parent_lookup)

    def test_prompt_identity_is_exact_when_a_model_was_applicable(self):
        prompt = self.root / "prompt.txt"
        inputs = self.root / "prompt-inputs.json"
        prompt.write_text("select no trades", encoding="utf-8")
        inputs.write_text('{"snapshot":"synthetic"}\n', encoding="utf-8")
        package = self.create(spec=self.spec(
            prompt=evidence.PromptEvidence(
                applicable=True, provider="openai", model="test-model",
                prompt_path=prompt, inputs_path=inputs)))
        manifest = self.verify(package)
        self.assertTrue(manifest["prompt"]["applicable"])
        self.assertEqual(manifest["prompt"]["provider"], "openai")
        self.assertEqual(len(manifest["prompt"]["prompt"]["sha256"]), 64)

    def test_missing_or_tampered_parent_is_rejected(self):
        child = self.create()
        for defect in ("missing", "tampered"):
            with self.subTest(defect=defect):
                parent = self.parent
                if defect == "missing":
                    shutil.rmtree(parent)
                else:
                    manifest = json.loads(
                        (parent / "manifest.json").read_text(encoding="utf-8"))
                    digest = manifest["dataset"]["artifact"]["sha256"]
                    (parent / "blobs" / digest).write_text(
                        "tampered parent\n", encoding="utf-8")
                with self.assertRaises(evidence.EvidenceValidationError):
                    self.verify(child)

                # Restore the parent for the next subtest when necessary.
                if defect == "missing":
                    self.parent = evidence.create_evidence_package(
                        self.store, self.spec(parent_evidence_ids=()))

    def test_parent_cycle_is_rejected_by_lookup(self):
        child = self.create()
        real_verify = evidence._verify_evidence_package
        calls = 0

        def cyclic_verify(package, **kwargs):
            nonlocal calls
            calls += 1
            if calls <= 2:
                return real_verify(package, **kwargs)
            if calls == 3:
                return {
                    "evidence_id": self.parent_id,
                    "parent_evidence_ids": [child.name],
                }
            return {"evidence_id": child.name, "parent_evidence_ids": []}

        with patch.object(evidence, "_verify_evidence_package",
                          side_effect=cyclic_verify):
            with self.assertRaisesRegex(
                evidence.EvidenceValidationError, "cycle"):
                evidence.verify_evidence_package(
                    child, parent_lookup={
                        self.parent_id: child, child.name: child})

    def test_parent_lookup_must_return_the_declared_identity(self):
        other_output = self.root / "other-result.json"
        other_output.write_text('{"expectancy_r":0.2}\n', encoding="utf-8")
        other = self.create(spec=self.spec(
            parent_evidence_ids=(), outputs=(other_output,)))
        child = self.create()
        with self.assertRaisesRegex(
                evidence.EvidenceValidationError, "does not match declared ID"):
            evidence.verify_evidence_package(
                child, parent_lookup={self.parent_id: other})

    def test_parent_depth_limit_is_enforced(self):
        middle = self.create(
            spec=self.spec(parent_evidence_ids=(self.parent_id,)))
        child = self.create(spec=self.spec(
            parent_evidence_ids=(middle.name,)))
        with self.assertRaisesRegex(
                evidence.EvidenceValidationError, "maximum depth"):
            self.verify(child, max_parent_depth=1)

    def test_fresh_copy_corruption_is_rejected_before_publish(self):
        def corrupt_copy(source, target, length=0):
            del source, length
            target.write(b"corrupt copy")

        with patch.object(evidence.shutil, "copyfileobj", corrupt_copy):
            with self.assertRaisesRegex(
                    evidence.EvidenceValidationError, "freshly copied"):
                evidence.create_evidence_package(
                    self.store / "corrupt", self.spec(),
                    parent_lookup=self.parent_lookup)
        self.assertFalse(any(self.store.glob(".evidence-*")))


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
