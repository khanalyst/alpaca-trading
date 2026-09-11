"""Focused contracts for parallel diagnostic and single-paper status wiring."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from agent.config import ConfigError, load_config as load_runtime_config
from deploy import dashboard, health, shadow as shadow_service
from research.diagnostic_shadow import build_diagnostic_cohort


ROOT = Path(__file__).resolve().parents[1]


def _diagnostic_coverage(*, source_lag: float | None = 5.0,
                         active: bool = True, now: float = 100.0) -> dict:
    families = [f"family_{index}" for index in range(12)]
    arms = []
    candidate_ids = []
    code_identity = "diagnostic-code"
    cohort_identity = "diagnostic-cohort"
    for family in families:
        for role in ("baseline", "variant"):
            candidate_id = f"shadow:{family}:{role}"
            candidate_ids.append(candidate_id)
            arms.append({
                "candidate_id": candidate_id,
                "family": family,
                "role": role,
                "variant_id": f"rule.{family}.{role}",
                "code_identity": code_identity,
                "cohort_identity": cohort_identity,
            })
    activation_watermark = {
        "count": 48,
        "decision_event_count": 24,
        "last_inserted_at": now - 10.0,
        "last_event_key": "activation-event",
    }
    cursors = {
        candidate_id: {
            "last_inserted_at": now - 5.0,
            "last_event_key": "forward-event",
            "processed_events": 1,
        }
        for candidate_id in candidate_ids
    }
    return {
        "schema": "diagnostic-shadow-coverage.v1",
        "enabled": True,
        "diagnostic": True,
        "authorizing": False,
        "gate_eligible": False,
        "promotion_eligible": False,
        "online_fdr": False,
        "families_total": 12,
        "families_covered": 12,
        "families_missing": [],
        "families_observed": 12,
        "families_without_decisions": [],
        "baseline_count": 12,
        "variant_count": 12,
        "cohort_identity": cohort_identity,
        "code_identity": code_identity,
        "activation_identity": "forward-activation" if active else None,
        "activation_event_watermark": activation_watermark,
        "activation_status": "active" if active else "preregistered",
        "warmup_session": "2026-09-08",
        "candidate_identities": candidate_ids,
        "arms": arms,
        "decision_counts": {
            "total": 24,
            "this_poll": 2,
            "warmup": 0,
            "by_kind": {"reject": 22, "open_incomplete": 2},
        },
        "rejection_counts": {
            "reject": 22,
            "unpriced": 0,
            "preactivation": 0,
            "preactivation_this_poll": 0,
        },
        "quoteable_virtual_opens": 2,
        "unpriced_virtual_opens": 0,
        "replay_modeled_fills": 0,
        "warmup_replay_modeled_fills": 0,
        "processed_events": 24,
        "processed_event_cursors": cursors,
        "actual_fills": 0,
        "actual_fill_claims": False,
        "realized_pnl_authorizing": False,
        "observation_status": "quoteable_virtual_observations",
        "poll_duration_seconds": 0.25,
        "source_lag_seconds": source_lag,
        "unexpected_detail": {"must": "not escape"},
    }


def _paper_selection(state: str) -> dict:
    if state == "ready":
        resolved = {
            "candidate_id": "candidate-volume-breakout",
            "variant_id": "rule.volume-breakout.proved",
            "family": "volume_breakout",
            "proof": {
                "run_id": "shadow-proof-volume-breakout",
                "gate_hash": "verified-gate-hash",
                "config_hash": "config-hash-volume-breakout",
                "lane": "shadow",
                "unbounded": "discard",
            },
            "unbounded": "discard",
        }
        blocker = None
        armed = True
    elif state == "waiting_for_proof":
        resolved = None
        blocker = "validated_edge_required"
        armed = True
    else:
        resolved = None
        blocker = "operator_paused"
        armed = False
    return {
        "configured_strategy": "rule",
        "selection_mode": "specific",
        "requested_variant": "auto",
        "resolved": resolved,
        "blocker_code": blocker,
        "armed": armed,
        "state": state,
        "unbounded": {"secret": "discard"},
    }


def _write_config(root: Path) -> None:
    (root / "config.yaml").write_text(json.dumps({
        "mode": "paper",
        "broker": {
            "paper": True,
            "data_feed": "iex",
            "options_feed": "indicative",
        },
        "strategy": {
            "id": "rule",
            "version": "v1",
            "execution_mode": "shares",
            "variant_id": "auto",
            "selection_mode": "specific",
        },
        "research": {
            "enabled": True,
            "require_validated_variant": True,
        },
        "cycle": {"interval_seconds": 60},
        "universe": {"symbols": []},
    }), encoding="utf-8")


class ParallelRuntimeStatusTests(unittest.TestCase):
    def setUp(self):
        dashboard._CACHE.clear()

    def tearDown(self):
        dashboard._CACHE.clear()

    def test_compose_mounts_bounded_shadow_config_without_credentials(self):
        text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        shadow = text.split("  shadow:", 1)[1].split("  dashboard:", 1)[0]
        dashboard_service = text.split("  dashboard:", 1)[1].split(
            "\nconfigs:", 1)[0]

        self.assertIn("- --config\n      - /app/config.yaml", shadow)
        self.assertIn("- --diagnostic", shadow)
        self.assertIn(
            "- --max-workers\n      - ${ALPACA_SHADOW_MAX_WORKERS:-4}",
            shadow)
        self.assertIn("source: agent_config", shadow)
        self.assertIn("target: /app/config.yaml", shadow)
        self.assertIn("mode: 0444", shadow)
        self.assertIn("- runtime-data:/app/runtime:ro", shadow)
        self.assertIn('ALPACA_PAPER: "true"', shadow)
        self.assertIn('ALPACA_LIVE_ENABLE: "false"', shadow)
        self.assertIn(
            "ALPACA_DATA_FEED: ${ALPACA_DATA_FEED:-iex}", shadow)
        self.assertIn(
            "ALPACA_STOCK_FEED: ${ALPACA_STOCK_FEED:-iex}", shadow)
        self.assertIn(
            "ALPACA_OPTIONS_FEED: ${ALPACA_OPTIONS_FEED:-indicative}",
            shadow)
        self.assertNotIn("agent_credentials", shadow)
        self.assertNotIn("ALPACA_AGENT_SECRETS_FILE", shadow)
        self.assertNotIn("/run/secrets", shadow)
        self.assertIn("- shadow-data:/app/shadow:ro", dashboard_service)

    def test_compose_cpu_quotas_default_and_override_without_scope_changes(self):
        text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        recorder = text.split("  recorder:", 1)[1].split("  trader:", 1)[0]
        shadow = text.split("  shadow:", 1)[1].split("  dashboard:", 1)[0]
        self.assertIn('cpus: "${ALPACA_RECORDER_CPUS:-0.75}"', recorder)
        self.assertIn('cpus: "${ALPACA_SHADOW_CPUS:-0.50}"', shadow)

        try:
            version = subprocess.run(
                ["docker", "compose", "version"], cwd=ROOT,
                capture_output=True, text=True, check=False)
        except FileNotFoundError:
            self.skipTest("Docker Compose is unavailable")
        if version.returncode != 0:
            self.skipTest("Docker Compose plugin is unavailable")

        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "credentials.env"
            secret.write_text("", encoding="utf-8")
            environment = dict(os.environ)
            environment.update({
                "ALPACA_AGENT_SECRET_FILE": str(secret),
                "ALPACA_RESEARCH_LLM_SECRET_FILE": str(secret),
            })
            environment.pop("ALPACA_RECORDER_CPUS", None)
            environment.pop("ALPACA_SHADOW_CPUS", None)

            def render(extra: dict[str, str] | None = None) -> dict:
                resolved_environment = dict(environment)
                resolved_environment.update(extra or {})
                result = subprocess.run(
                    ["docker", "compose", "config", "--format", "json"],
                    cwd=ROOT, env=resolved_environment,
                    capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            default = render()
            overridden = render({
                "ALPACA_RECORDER_CPUS": "1.25",
                "ALPACA_SHADOW_CPUS": "1.25",
            })

        self.assertEqual(default["services"]["recorder"]["cpus"], 0.75)
        self.assertEqual(default["services"]["shadow"]["cpus"], 0.50)
        self.assertEqual(overridden["services"]["recorder"]["cpus"], 1.25)
        self.assertEqual(overridden["services"]["shadow"]["cpus"], 1.25)
        for service in ("recorder", "shadow"):
            baseline = deepcopy(default["services"][service])
            changed = deepcopy(overridden["services"][service])
            baseline.pop("cpus")
            changed.pop("cpus")
            self.assertEqual(changed, baseline)
        resolved_shadow = default["services"]["shadow"]
        self.assertFalse(resolved_shadow.get("secrets"))
        self.assertNotIn(
            "ALPACA_AGENT_SECRETS_FILE",
            resolved_shadow.get("environment", {}))
        self.assertFalse(any(
            "/run/secrets" in str(volume)
            for volume in resolved_shadow.get("volumes", [])))

    def test_compose_routes_one_current_corpus_without_relabeling_history(self):
        text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        recorder = text.split("  recorder:", 1)[1].split("  trader:", 1)[0]
        research = text.split("  research:", 1)[1].split(
            "  shadow-init:", 1)[0]
        shadow = text.split("  shadow:", 1)[1].split("  dashboard:", 1)[0]
        dashboard_service = text.split("  dashboard:", 1)[1].split(
            "\nconfigs:", 1)[0]
        corpus = (
            "${ALPACA_RECORDER_CORPUS_ROOT:-"
            "/app/runtime/research/recorded}")

        self.assertIn(f"- --out\n      - {corpus}", recorder)
        self.assertIn(f'"--path", "{corpus}"', recorder)
        self.assertIn(
            f"ALPACA_RECORDER_CORPUS_ROOT: {corpus}", recorder)
        self.assertIn(
            "ALPACA_RECORDER_CAPTURE_POLICY: "
            "${ALPACA_RECORDER_CAPTURE_POLICY:-forward_only}", recorder)
        self.assertIn(
            f"ALPACA_RECORDED_DATASET_ROOT: {corpus}", research)
        self.assertIn(
            "ALPACA_RESEARCH_DATASET: ${ALPACA_RESEARCH_DATASET:-}",
            research)
        self.assertIn(f"- {corpus}/market.csv", shadow)
        self.assertIn(
            f"ALPACA_RECORDER_CORPUS_ROOT: {corpus}", shadow)
        self.assertIn(
            f"ALPACA_RECORDER_CORPUS_ROOT: {corpus}", dashboard_service)
        self.assertNotIn("recorded-forward-2026-09-08", text)

        research_cycle = (ROOT / "deploy/research-cycle.sh").read_text(
            encoding="utf-8")
        explicit = research_cycle.index(
            'dataset="${ALPACA_RESEARCH_DATASET:-}"')
        # Readiness now resolves the recorder root before expensive work, but
        # only when no explicit dataset was supplied. Source selection still
        # starts with the caller's dataset, then enters the recorder fallback.
        fallback = research_cycle.index('if [ -z "$dataset" ]', explicit)
        self.assertLess(explicit, fallback)
        self.assertIn('[ -z "$preflight_dataset" ]', research_cycle)
        # Historical workbench evidence remains on its original durable path;
        # only the live recorder-health view follows the current corpus epoch.
        workbench = (ROOT / "deploy/dashboard_workbench.py").read_text(
            encoding="utf-8")
        self.assertIn('recorded = root / "runtime/research/recorded"',
                      workbench)

    def test_dashboard_live_recorder_view_uses_only_configured_corpus(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_config(root)
            historical = root / "runtime/research/recorded"
            forward = root / "runtime/research/recorded-forward"
            historical.mkdir(parents=True)
            forward.mkdir(parents=True)
            (historical / "historical-a.csv").write_text(
                "timestamp,close\n1,1\n", encoding="utf-8")
            (historical / "historical-b.csv").write_text(
                "timestamp,close\n2,2\n", encoding="utf-8")
            (forward / "market.csv").write_text(
                "timestamp,close\n3,3\n", encoding="utf-8")

            with patch.dict(os.environ, {
                    "ALPACA_RECORDER_CORPUS_ROOT": str(forward),
                    }, clear=False):
                result = dashboard.snapshot(root)

            self.assertEqual(result["recorder"]["corpus_root"],
                             str(forward.resolve()))
            self.assertEqual(result["recorder"]["series_files"], 1)
            self.assertTrue((historical / "historical-a.csv").is_file())
            self.assertTrue((historical / "historical-b.csv").is_file())
            self.assertIn("current corpus root", dashboard.HTML)
            self.assertIn(
                "Historical research reports retain their original source identities",
                dashboard.HTML)

            with patch.dict(os.environ, {
                    "ALPACA_RECORDER_CORPUS_ROOT": str(root / "outside"),
                    }, clear=False), self.assertRaisesRegex(
                        ValueError, "must remain inside runtime"):
                dashboard._recorder_corpus_path(root)

    def test_shadow_uses_runtime_resolved_feed_policy_and_rejects_mode_conflict(self):
        class CapturingRunner:
            config = None

            def __init__(self, config):
                type(self).config = config

            def run_once(self):
                return {}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_config(root)
            args = [
                "--config", str(root / "config.yaml"),
                "--corpus", str(root / "market.csv"),
                "--edge-db", str(root / "edge.sqlite3"),
                "--shadow-db", str(root / "shadow.sqlite3"),
                "--health-file", str(root / "health.json"),
                "--once",
            ]
            with patch.dict(os.environ, {
                    "ALPACA_PAPER": "true",
                    "ALPACA_LIVE_ENABLE": "false",
                }, clear=True):
                baseline_config = load_runtime_config(root / "config.yaml")
            baseline = build_diagnostic_cohort(
                baseline_config, code_identity="a" * 64)

            with patch.dict(os.environ, {
                    "ALPACA_PAPER": "true",
                    "ALPACA_LIVE_ENABLE": "false",
                    "ALPACA_DATA_FEED": "sip",
                    "ALPACA_STOCK_FEED": "sip",
                    "ALPACA_OPTIONS_FEED": "opra",
                    }, clear=True), patch.object(
                        shadow_service, "ShadowRunner", CapturingRunner), patch(
                            "builtins.print"):
                self.assertEqual(shadow_service.main(args), 0)

            resolved_config = CapturingRunner.config.runtime_config
            self.assertEqual(resolved_config["broker"]["data_feed"], "sip")
            self.assertEqual(resolved_config["broker"]["options_feed"], "opra")
            overridden = build_diagnostic_cohort(
                resolved_config, code_identity="a" * 64)
            self.assertNotEqual(
                baseline["policy_config_identity"],
                overridden["policy_config_identity"])
            self.assertTrue(all(
                arm["config"]["broker"]["data_feed"] == "sip" and
                arm["config"]["broker"]["options_feed"] == "opra"
                for arm in overridden["arms"]))

            with patch.dict(os.environ, {
                    "ALPACA_PAPER": "false",
                    "ALPACA_LIVE_ENABLE": "false",
                    }, clear=True), self.assertRaisesRegex(
                        ConfigError, "ALPACA_PAPER=true"):
                shadow_service.main(args)

    def test_shadow_health_exposes_complete_24_arm_diagnostic_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.json"
            path.write_text(json.dumps({
                "status": "running",
                "updated_ts": 100,
                "candidate_errors": {},
                "diagnostic_shadow": _diagnostic_coverage(now=100),
            }), encoding="utf-8")

            result = health.shadow(path, 60, now=100)

        self.assertTrue(result["ok"])
        self.assertTrue(result["coverage_ready"])
        self.assertEqual(result["coverage_status"], "ready")
        diagnostic = result["diagnostic_shadow"]
        self.assertTrue(diagnostic["diagnostic_only"])
        self.assertFalse(diagnostic["proof_authority"])
        self.assertEqual(diagnostic["families_covered"], 12)
        self.assertEqual(diagnostic["baseline_count"], 12)
        self.assertEqual(diagnostic["variant_count"], 12)
        self.assertEqual(diagnostic["candidate_count"], 24)
        self.assertEqual(diagnostic["arms_total"], 24)
        self.assertEqual(len(diagnostic["processed_event_cursors"]), 24)
        self.assertEqual(diagnostic["cursor_status"], "ready")
        self.assertEqual(diagnostic["actual_fills"], 0)
        self.assertFalse(diagnostic["authorizing"])
        self.assertFalse(diagnostic["proof_authority"])
        self.assertNotIn("arms", diagnostic)
        self.assertNotIn("candidate_identities", diagnostic)
        self.assertNotIn("unexpected_detail", diagnostic)
        self.assertNotIn("net_pnl", diagnostic)
        self.assertNotIn("profit", diagnostic)

    def test_shadow_liveness_is_separate_from_coverage_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.json"
            complete = _diagnostic_coverage()

            cases = []
            cases.append((
                {"status": "running", "updated_ts": 0,
                 "candidate_errors": {},
                 "diagnostic_shadow": complete},
                False, "stale_heartbeat"))
            cases.append((
                {"status": "running", "updated_ts": 100,
                 "candidate_errors": {},
                 "diagnostic_shadow": _diagnostic_coverage(
                     source_lag=None, now=100)},
                True, "fresh_data_unavailable"))
            incomplete = deepcopy(complete)
            incomplete["families_covered"] = 11
            incomplete["families_missing"] = ["family_11"]
            cases.append((
                {"status": "running", "updated_ts": 100,
                 "candidate_errors": {},
                 "diagnostic_shadow": incomplete},
                True, "family_coverage_incomplete"))
            cases.append((
                {"status": "running", "updated_ts": 100,
                 "candidate_errors": {},
                 "diagnostic_shadow": _diagnostic_coverage(
                     active=False, now=100)},
                True, "active_cohort_missing"))

            for heartbeat, service_alive, coverage_status in cases:
                with self.subTest(coverage_status=coverage_status):
                    path.write_text(json.dumps(heartbeat), encoding="utf-8")
                    result = health.shadow(path, 60, now=100)
                    self.assertEqual(result["ok"], service_alive)
                    self.assertFalse(result["coverage_ready"])
                    self.assertEqual(result["coverage_status"], coverage_status)

    def test_paper_selection_roundtrips_as_a_bounded_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "heartbeat.json"
            for state in ("waiting_for_proof", "ready", "blocked"):
                with self.subTest(state=state):
                    selection = _paper_selection(state)
                    path.write_text(json.dumps({
                        "status": "running",
                        "updated_ts": 100,
                        "paper_selection": selection,
                    }), encoding="utf-8")
                    trader = health.trader(path, 60, now=100)
                    safe = dashboard._safe_heartbeat(path)
                    self.assertEqual(trader["paper_selection"],
                                     safe["paper_selection"])
                    bounded = trader["paper_selection"]
                    self.assertNotIn("unbounded", bounded)
                    if bounded["resolved"] is not None:
                        self.assertNotIn("unbounded", bounded["resolved"])
                        self.assertNotIn(
                            "unbounded", bounded["resolved"]["proof"])
                    self.assertEqual(bounded["armed"], selection["armed"])
                    self.assertEqual(bounded["state"], state)

    def test_dashboard_surfaces_paper_identity_and_shadow_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_config(root)
            paper = root / "runtime" / "paper"
            paper.mkdir(parents=True)
            paper_selection = _paper_selection("ready")
            (paper / "heartbeat.json").write_text(json.dumps({
                "status": "running",
                "updated_ts": time.time(),
                "paper_selection": paper_selection,
            }), encoding="utf-8")
            shadow = root / "shadow"
            shadow.mkdir()
            now = time.time()
            (shadow / "health.json").write_text(json.dumps({
                "status": "running",
                "updated_ts": now,
                "candidate_errors": {},
                "diagnostic_shadow": _diagnostic_coverage(now=now),
            }), encoding="utf-8")

            result = dashboard.snapshot(root)

        self.assertEqual(result["strategy"]["selection_mode"], "specific")
        self.assertEqual(
            result["trader"]["health"]["paper_selection"]["resolved"]
            ["candidate_id"],
            "candidate-volume-breakout")
        self.assertEqual(
            result["trader"]["heartbeat"]["paper_selection"],
            result["trader"]["health"]["paper_selection"])
        self.assertTrue(result["shadow"]["ok"])
        self.assertTrue(result["shadow"]["coverage_ready"])
        self.assertEqual(
            result["shadow"]["diagnostic_shadow"]["arms_total"], 24)
        for marker in (
                "d.trader.health.paper_selection",
                "requested paper pair",
                "Diagnostic shadow",
                "coverage ready",
                "evaluations / no-trade decisions",
                "proof authority",
                "Zero actual fills is not a profit claim"):
            self.assertIn(marker, dashboard.HTML)
        self.assertNotIn("evaluations / no signal", dashboard.HTML)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
