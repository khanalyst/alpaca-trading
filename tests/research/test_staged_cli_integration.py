"""CLI boundaries for bounded staging, retry semantics, and safe handoff."""

import gc
import importlib.util
import io
import json
import tempfile
import unittest
import warnings
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent import shadow
from agent.config import ConfigError, validate_config
from agent.staging import StagingStore
from research import authoring
from research.findings import FindingsStore
from research.staged_refinement import (RefinementPolicy,
                                        stage_initial_neighborhood)
from tests.helpers import valid_config
from tests.research.test_authoring import (FakeProposer, good_proposal,
                                           response)


REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "research_cli_staged_integration", REPO / "research.py")
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


def research_config(root: Path, *, max_active: int = 8) -> dict:
    return {
        "llm": {"provider": "openai", "model": "test"},
        "research": {
            "staging_store": str(root / "staging.db"),
            "findings_store": str(root / "findings.db"),
            "staging_handoff_dir": str(root / "handoffs"),
            "staging_max_active": max_active,
            "staging_lifecycle": {
                "never_fired_decisions": 17,
                "never_fired_max_age_hours": 11,
                "no_observation_max_age_hours": 7,
                "starved_max_age_hours": 5,
                "starved_fraction": 0.25,
            },
            "staging_refinement": {
                "max_attempts": 1,
                "max_variants_per_attempt": 2,
                "max_configurations_per_mechanism": 5,
                "relative_step": 0.10,
            },
        },
    }


class StagingConfigBoundaryTests(unittest.TestCase):
    def test_readonly_backup_probe_closes_its_sqlite_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "findings.db"
            import sqlite3

            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE marker (value INTEGER)")
            connection.commit()
            connection.close()
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                self.assertIsNone(
                    CLI._latest_verified_external_backup_readonly(path))
                gc.collect()
            self.assertFalse([
                item for item in caught
                if "sqlite3.Connection" in str(item.message)])

    def test_validator_supplies_safe_bounded_defaults(self):
        cfg = valid_config()
        cfg["research"] = {
            "shadow_enabled": False,
            "shadow_variants": [],
            "shadow_budget_ms": 0,
        }

        result = validate_config(cfg)["research"]

        self.assertEqual(result["staging_max_active"], 32)
        self.assertEqual(result["staging_refinement"]["max_attempts"], 1)
        self.assertEqual(
            result["staging_lifecycle"]["no_observation_max_age_hours"], 72)

    def test_validator_refuses_a_budget_smaller_than_one_neighborhood(self):
        cfg = valid_config()
        cfg["research"] = {
            "shadow_enabled": False,
            "shadow_variants": [],
            "shadow_budget_ms": 0,
            "staging_max_active": 2,
        }
        with self.assertRaisesRegex(
                ConfigError, "cover one initial configuration neighborhood"):
            validate_config(cfg)

    def test_cli_store_uses_the_configured_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = research_config(Path(directory), max_active=3)
            store = CLI._staging_store(cfg)
            self.assertEqual(store.capacity()["max_active"], 3)


class AuthorAndReviewExitSemanticsTests(unittest.TestCase):
    def test_authoring_stages_a_neighborhood_and_structures_non_novelty(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StagingStore(Path(directory) / "staging.db", max_active=8)
            cfg = {"llm": {"provider": "openai", "model": "test"}}
            first = authoring.author_generation(
                store, cfg,
                author=FakeProposer(response(good_proposal())), now=1)
            second = authoring.author_generation(
                store, cfg,
                author=FakeProposer(response(good_proposal("renamed"))), now=2)

            self.assertEqual(first["accepted"][0]["configuration_count"], 3)
            self.assertEqual(second["status"], "NO_NOVEL_CANDIDATE")
            self.assertEqual(second["rejected"][0]["code"],
                             "NO_NOVEL_CANDIDATE")

            empty = authoring.author_generation(
                store, cfg, author=FakeProposer(response()), now=3)
            self.assertEqual(empty["status"], "NO_NOVEL_CANDIDATE")

    def test_author_cli_is_nonzero_only_for_true_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = research_config(root)
            args = SimpleNamespace(
                staging=None, store=None, no_history=True,
                max_proposals=4, dry_run=False)
            cases = [
                ({"status": "FAILED", "accepted_count": 0,
                  "error": "provider down"}, 2),
                ({"status": "NO_NOVEL_CANDIDATE", "accepted_count": 0,
                  "rejected": [{"code": "NO_NOVEL_CANDIDATE"}]}, 0),
                ({"status": "CAPACITY", "accepted_count": 0,
                  "rejected": [{"code": "CAPACITY"}]}, 0),
                ({"status": "AUTHORED", "accepted_count": 1,
                  "rejected": [{"code": "VALIDATION_FAILED"}]}, 2),
            ]
            for result, expected in cases:
                with self.subTest(status=result["status"]), \
                        patch.object(CLI, "_load_config", return_value=cfg), \
                        patch.object(
                            authoring, "author_generation", return_value=result), \
                        redirect_stdout(io.StringIO()):
                    self.assertEqual(CLI.cmd_author(args), expected)

    def test_review_cli_reports_persisted_retry_pending_as_nonzero(self):
        args = SimpleNamespace(store=None, no_review=False, max_reviews=8)
        cfg = {"research": {"findings_store": "unused.db"}}
        result = {
            "status": "RETRY_PENDING", "retry_pending": 1, "failed": 0,
        }
        with patch.object(CLI, "_load_config", return_value=cfg), \
                patch.object(CLI.findings_mod, "FindingsStore", return_value=Mock()), \
                patch.object(CLI.review_mod, "process_pending_reviews",
                             return_value=result), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(CLI.cmd_research_loop(args), 2)


class RefinementAndHandoffCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = research_config(self.root)
        self.store = CLI._staging_store(self.cfg)
        self.family = stage_initial_neighborhood(
            self.store, good_proposal(), generation=0, now=1)

    def test_persisted_retirement_refines_once_across_reruns(self):
        root_id = self.family["root_contract_id"]
        self.store.retire(
            root_id, "NEGATIVE_EXPECTANCY: persisted failure", now=2)
        policy = RefinementPolicy(
            max_attempts=1, max_variants_per_attempt=2,
            max_configurations_per_mechanism=5)

        first = CLI._refine_persisted_retirements(self.store, policy)
        second = CLI._refine_persisted_retirements(self.store, policy)

        self.assertEqual(first[0]["status"], "REGISTERED")
        self.assertEqual(second[0]["status"], "ALREADY_REGISTERED")
        self.assertEqual(
            first[0]["registered_contract_ids"],
            second[0]["registered_contract_ids"])
        self.assertEqual(len(self.store.mechanism_records(
            self.family["mechanism_id"])), 5)

    def test_review_staged_constructs_the_configured_lifecycle_policy(self):
        args = SimpleNamespace(
            staging=None, store=None, scope=None, dry_run=True)
        observed = {}

        def capture(_staging, _findings, **kwargs):
            observed["policy"] = kwargs["policy"]
            return {
                "schema": "staged_review.v2", "reviewed_ts": 1,
                "reviewed": 0, "retired": [], "verdicts": [],
                "mechanisms": [],
            }

        with patch.object(CLI, "_load_config", return_value=self.cfg), \
                patch("research.staged_review.review", side_effect=capture), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(CLI.cmd_review_staged(args), 0)

        policy = observed["policy"]
        self.assertEqual(policy.never_fired_decisions, 17)
        self.assertEqual(policy.never_fired_max_age_seconds, 11 * 3600)
        self.assertEqual(policy.no_observation_max_age_seconds, 7 * 3600)
        self.assertEqual(policy.starved_max_age_seconds, 5 * 3600)
        self.assertEqual(policy.starved_fraction, 0.25)

    def test_prepare_handoff_is_content_addressed_idempotent_and_non_mutating(self):
        record = self.store.record(self.family["root_contract_id"])
        review = {
            "schema": "staged_review.v2", "reviewed_ts": 20.0,
            "retired": [],
            "mechanisms": [{
                "mechanism_id": self.family["mechanism_id"],
                "mechanism": record["proposal"]["mechanism"],
                "payer": record["proposal"]["payer"],
                "code": "SUPPORTED",
                "supported_configuration_ids": [record["configuration_id"]],
            }],
            "verdicts": [{
                "contract_id": record["contract_id"],
                "mechanism_id": self.family["mechanism_id"],
                "configuration_id": record["configuration_id"],
                "variant_id": "staged.funding_tail_unwind",
                "scope_key": "demo:test:staged", "stage": "CONFIRM",
                "code": "SUPPORTED", "trades": 150, "mean_r": 0.2,
                "firing_rate": 0.1, "coverage_pct": 99.0,
                "pair_coverage_pct": 98.0, "explanation": "supported",
            }],
        }
        args = SimpleNamespace(
            staging=None, store=None, scope=None, mechanism=None, out=None)
        # The production command reads a consistent temporary backup so its
        # validation cannot migrate or otherwise edit authoritative findings.
        FindingsStore(self.root / "findings.db").paper_scopes()
        findings_before = (self.root / "findings.db").read_bytes()
        config_before = (REPO / "config.yaml").read_bytes()
        registry_before = (REPO / "research" / "variants.yaml").read_bytes()

        outputs = []
        for _ in range(2):
            stream = io.StringIO()
            with patch.object(CLI, "_load_config", return_value=self.cfg), \
                    patch("research.staged_review.review", return_value=review), \
                    redirect_stdout(stream), redirect_stderr(io.StringIO()):
                self.assertEqual(CLI.cmd_prepare_handoff(args), 0)
            outputs.append(json.loads(stream.getvalue()))

        self.assertEqual(outputs[0]["prepared"][0]["status"], "CREATED")
        self.assertEqual(outputs[1]["prepared"][0]["status"], "EXISTING")
        self.assertEqual(outputs[0]["prepared"][0]["handoff_id"],
                         outputs[1]["prepared"][0]["handoff_id"])
        artifact = Path(outputs[0]["prepared"][0]["path"])
        self.assertTrue(artifact.is_file())
        self.assertEqual((self.root / "findings.db").read_bytes(),
                         findings_before)
        self.assertEqual((REPO / "config.yaml").read_bytes(), config_before)
        self.assertEqual(
            (REPO / "research" / "variants.yaml").read_bytes(),
            registry_before)


class RuntimeAndNightlyWiringTests(unittest.TestCase):
    def test_runtime_staging_reads_use_the_configured_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "staging.db"
            path.touch()
            cfg = valid_config()
            cfg["research"] = {
                "staging_store": str(path), "staging_max_active": 7,
            }
            staged_store = Mock()
            staged_store.active.return_value = []
            with patch("agent.staging.StagingStore",
                       return_value=staged_store) as constructor:
                self.assertEqual(
                    shadow._build_staged_lane(cfg, "demo:test", Mock()),
                    ({}, []))
            constructor.assert_called_once_with(path, max_active=7)

    def test_runtime_refresh_uses_the_same_configured_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "staging.db"
            path.touch()
            cfg = valid_config()
            cfg["research"] = {"staging_max_active": 9}
            evaluator = Mock()
            evaluator.registration_errors = {}
            evaluator.store = Mock()
            evaluator.scope_key = "demo:test"
            coordinator = shadow.StrategyShadowCoordinator(
                {"momentum": evaluator}, active_strategy_id="momentum",
                staged_store_path=path, staged_cfg=cfg)
            staged_store = Mock()
            staged_store.active.return_value = []

            with patch("agent.staging.StagingStore",
                       return_value=staged_store) as constructor:
                result = coordinator.refresh_staged_lanes(now=1)

            constructor.assert_called_once_with(path, max_active=9)
            self.assertEqual(result["status"], "unchanged")

    def test_nightly_checks_fidelity_before_lifecycle_and_does_not_mask_it(self):
        script = (REPO / "research" / "nightly.sh").read_text()
        corpus = script.index("research.py corpus stats")
        fidelity = script.index("--check-fidelity")
        lifecycle = script.index("research.py review-staged")
        qualification = script.index("research.py qualify-staged")
        review = script.index("research.py research-loop")
        author = script.index("research.py author")
        handoff = script.index("research.py prepare-handoff")
        self.assertLess(corpus, fidelity)
        for downstream in (
                lifecycle, qualification, review, author, handoff):
            self.assertLess(fidelity, downstream)
        self.assertNotIn("authoring unavailable this cycle", script)
        self.assertNotIn("research review deferred", script)
        self.assertIn(
            "staged lifecycle skipped until proposal fidelity passes", script)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
