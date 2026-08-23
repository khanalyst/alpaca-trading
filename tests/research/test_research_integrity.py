"""Regression tests for scheduled research boundaries and portability."""

from argparse import Namespace
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.costs import CostModel

_CLI_PATH = Path(__file__).parents[2] / "research.py"
_CLI_SPEC = importlib.util.spec_from_file_location("research_cli_test", _CLI_PATH)
research_cli = importlib.util.module_from_spec(_CLI_SPEC)
assert _CLI_SPEC.loader is not None
_CLI_SPEC.loader.exec_module(research_cli)


class ScheduledResearchTests(unittest.TestCase):
    @staticmethod
    def _factory_args(data: Path, *, diagnostic_only: bool = False) -> Namespace:
        return Namespace(
            agent_config="config.yaml", config=None, data=str(data),
            db="edge.sqlite3", vehicle="equity", strategies=1,
            variants=2, workers=1, starting_cash=100_000.0,
            min_trades=1, min_sessions=1, alpha=.05, max_generations=1,
            max_confirmatory_attempts=3, worker_data=None,
            diagnostic_only=diagnostic_only)

    @staticmethod
    def _factory_config() -> dict:
        return {
            "broker": {"data_feed": "iex"},
            "costs": {"spread_bps": 7.0, "slippage_bps": 4.0,
                      "fee_bps": 0.8},
            "execution": {"max_spread_bps": 20.0,
                          "max_slippage_bps": 15.0},
            "research": {"strategy_llm": {"enabled": False}},
        }

    def test_factory_command_uses_validated_agent_cost_and_execution_config(self):
        agent_config = self._factory_config()
        args = Namespace(
            agent_config="config.yaml", config=None, data="market.jsonl",
            db="edge.sqlite3", vehicle="equity", strategies=1,
            variants=2, workers=1, starting_cash=100_000.0,
            min_trades=1, min_sessions=1, alpha=.05, max_generations=1,
            max_confirmatory_attempts=7)
        captured = {}

        def fake_run_factory(*_args, **kwargs):
            captured.update(kwargs)
            return {"results": []}

        with patch.object(research_cli, "_agent_config", return_value=agent_config), \
             patch.object(research_cli, "_factory_dataset_preflight",
                          return_value={"authorizing": True,
                                        "diagnostic_only": False,
                                        "source": {}}), \
             patch.object(research_cli, "run_factory", side_effect=fake_run_factory), \
             patch.object(research_cli, "_emit_proofs", return_value=False), \
             patch.object(research_cli, "_write_factory_report", return_value=None):
            research_cli.cmd_factory_run(args)
        model = captured["costs"]
        self.assertIsInstance(model, CostModel)
        self.assertEqual(model.spread_bps, 7.0)
        self.assertEqual(model.slippage_bps, 4.0)
        self.assertEqual(model.max_spread_bps, 20.0)
        self.assertEqual(model.max_slippage_bps, 15.0)
        self.assertEqual(captured["max_confirmatory_attempts"], 7)

    def test_factory_status_exit_codes_are_distinct_and_proof_first(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "market.jsonl"
            source.write_text(json.dumps({
                "kind": "bar", "provider": "alpaca", "feed": "iex",
            }) + "\n", encoding="utf-8")
            base = self._factory_args(source)
            for status, expected in (
                    ("complete", 2),
                    ("bounded_space_exhausted",
                     research_cli.FACTORY_BOUNDED_SPACE_EXHAUSTED_EXIT),
                    ("llm_all_calls_failed",
                     research_cli.FACTORY_LLM_ALL_CALLS_FAILED_EXIT)):
                with self.subTest(status=status), \
                        patch.object(research_cli, "_agent_config",
                                     return_value=self._factory_config()), \
                        patch.object(research_cli, "_factory_dataset_preflight",
                                     return_value={"authorizing": True,
                                                   "diagnostic_only": False,
                                                   "source": {}}), \
                        patch.object(research_cli, "run_factory",
                                     return_value={"status": status,
                                                    "results": []}), \
                        patch.object(research_cli, "_emit_proofs",
                                     return_value=[]), \
                        patch.object(research_cli, "_write_factory_report",
                                     return_value=None), \
                        patch.object(research_cli, "print"):
                    self.assertEqual(research_cli.cmd_factory_run(base), expected)

            # A proof is the authorizing outcome even if a status also records
            # a provider/search diagnosis for another slot.
            with patch.object(research_cli, "_agent_config",
                              return_value=self._factory_config()), \
                 patch.object(research_cli, "_factory_dataset_preflight",
                              return_value={"authorizing": True,
                                            "diagnostic_only": False,
                                            "source": {}}), \
                 patch.object(research_cli, "run_factory",
                              return_value={"status": "llm_all_calls_failed",
                                             "results": []}), \
                 patch.object(research_cli, "_emit_proofs",
                              return_value=[{"candidate_id": "edge-1"}]), \
                 patch.object(research_cli, "_write_factory_report",
                              return_value=None), \
                 patch.object(research_cli, "print"):
                self.assertEqual(research_cli.cmd_factory_run(base), 0)

            # Provider exhaustion remains a distinct terminal diagnosis even
            # when every candidate gate also says the corpus was unevaluable.
            stalled_gate = {"fill_quality": {
                "fit": {"opportunities": 1, "executed": 0,
                         "dominant_reject_reason": "no quote"},
                "heldout": {"opportunities": 1, "executed": 0,
                             "dominant_reject_reason": "no quote"},
            }}
            with patch.object(research_cli, "_agent_config",
                              return_value=self._factory_config()), \
                 patch.object(research_cli, "_factory_dataset_preflight",
                              return_value={"authorizing": True,
                                            "diagnostic_only": False,
                                            "source": {}}), \
                 patch.object(research_cli, "run_factory",
                              return_value={"status": "llm_all_calls_failed",
                                            "results": [{"gate": stalled_gate}]}), \
                 patch.object(research_cli, "_emit_proofs", return_value=[]), \
                 patch.object(research_cli, "_write_factory_report",
                              return_value=None), \
                 patch.object(research_cli, "print"):
                self.assertEqual(research_cli.cmd_factory_run(base),
                                 research_cli.FACTORY_LLM_ALL_CALLS_FAILED_EXIT)

            with patch.object(research_cli, "_agent_config",
                              return_value=self._factory_config()), \
                 patch.object(research_cli, "_factory_dataset_preflight",
                              return_value={"authorizing": True,
                                            "diagnostic_only": False,
                                            "source": {}}), \
                 patch.object(research_cli, "run_factory",
                              return_value={"status": "llm_provider_failure",
                                            "results": []}), \
                 patch.object(research_cli, "_emit_proofs", return_value=[]), \
                 patch.object(research_cli, "_write_factory_report",
                              return_value=None), \
                 patch.object(research_cli, "print"):
                self.assertEqual(research_cli.cmd_factory_run(base),
                                 research_cli.FACTORY_LLM_ALL_CALLS_FAILED_EXIT)

    def test_llm_preflight_cli_status_and_exit_contract(self):
        args = Namespace(agent_config="config.yaml")
        disabled = {"research": {"strategy_llm": {"enabled": False}}}
        output = []
        with patch.object(research_cli, "_agent_config", return_value=disabled), \
             patch.object(research_cli, "print",
                          side_effect=lambda value, **_kwargs: output.append(value)):
            self.assertEqual(research_cli.cmd_llm_preflight(args), 0)
        self.assertEqual(json.loads(output[-1])["schema"],
                         "research-llm-preflight.v1")
        self.assertEqual(json.loads(output[-1])["status"], "disabled")

        class FakeOutcome:
            def __init__(self, status):
                self.status = status

            def as_dict(self):
                return {"schema": "research-llm-preflight.v1",
                        "status": self.status, "evidence": {}}

        enabled = {"research": {"strategy_llm": {
            "enabled": True, "provider": "openai", "model": "gpt-test",
            "max_attempts": 1, "timeout_seconds": 1,
            "max_response_bytes": 1024, "max_total_calls": 1}}}
        for status, expected in (("ready", 0), ("degraded", 4), ("fatal", 3)):
            with self.subTest(status=status):
                output = []
                fake = type("FakeAdapter", (), {
                    "__init__": lambda self, **_kwargs: None,
                    "preflight": lambda self, _status=status: FakeOutcome(_status),
                })
                with patch.object(research_cli, "_agent_config", return_value=enabled), \
                     patch.object(research_cli, "RuleProposalAdapter", fake), \
                     patch.object(research_cli, "print",
                                  side_effect=lambda value, **_kwargs: output.append(value)):
                    self.assertEqual(research_cli.cmd_llm_preflight(args), expected)
                self.assertEqual(json.loads(output[-1])["status"], status)

    def test_llm_preflight_cli_redacts_configuration_exception_and_signed_url(self):
        args = Namespace(agent_config="config.yaml")
        output = []
        secret = "sig=super-secret&X-Amz-Security-Token=another-secret"
        with patch.object(
                research_cli, "_agent_config",
                side_effect=RuntimeError(
                    f"bad endpoint https://example.test/probe?{secret}")), \
             patch.object(research_cli, "print",
                          side_effect=lambda value, **_kwargs: output.append(value)):
            self.assertEqual(research_cli.cmd_llm_preflight(args), 3)
        payload = json.loads(output[-1])
        self.assertEqual(payload["status"], "fatal")
        self.assertNotIn("super-secret", payload["reason"])
        self.assertNotIn("another-secret", payload["reason"])
        self.assertLessEqual(len(payload["reason"]), 300)

    def test_factory_parser_exposes_confirmatory_attempt_budget(self):
        parser = research_cli.build_parser()
        for argv in (
                ["factory", "run", "--data", "market.jsonl",
                 "--max-confirmatory-attempts", "9"],
                ["factory-run", "--data", "market.jsonl",
                 "--max-confirmatory-attempts", "9"]):
            with self.subTest(argv=argv):
                self.assertEqual(
                    parser.parse_args(argv).max_confirmatory_attempts, 9)

    def test_factory_default_rejects_non_iex_or_missing_provenance_before_runner(self):
        for row in (
                {"kind": "bar", "provider": "alpaca", "feed": "sip"},
                {"kind": "bar", "feed": "iex"}):
            with self.subTest(row=row), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "market.jsonl"
                source.write_text(json.dumps(row) + "\n", encoding="utf-8")
                args = self._factory_args(source)
                with patch.object(research_cli, "_agent_config",
                                  return_value=self._factory_config()), \
                     patch.object(research_cli, "run_factory") as factory, \
                     patch.object(research_cli, "_emit_proofs"):
                    with self.assertRaisesRegex(ValueError,
                                                "provenance preflight"):
                        research_cli.cmd_factory_run(args)
                factory.assert_not_called()

    def test_factory_diagnostic_only_marks_result_and_emits_no_proof(self):
        row = {"kind": "bar", "provider": "alpaca", "feed": "sip"}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "market.jsonl"
            source.write_text(json.dumps(row) + "\n", encoding="utf-8")
            args = self._factory_args(source, diagnostic_only=True)
            output = []

            def fake_run_factory(*_args, **_kwargs):
                return {"results": []}

            with patch.object(research_cli, "_agent_config",
                              return_value=self._factory_config()), \
                 patch.object(research_cli, "run_factory",
                              side_effect=fake_run_factory) as factory, \
                 patch.object(research_cli, "_emit_proofs") as emit, \
                 patch.object(research_cli, "_write_factory_report",
                              return_value=None), \
                 patch.object(research_cli, "print",
                              side_effect=lambda value, **_kwargs: output.append(value)):
                self.assertEqual(research_cli.cmd_factory_run(args), 2)
            factory.assert_called_once()
            self.assertTrue(factory.call_args.kwargs["diagnostic_only"])
            emit.assert_not_called()
            result = json.loads(output[-1])
            self.assertTrue(result["diagnostic_only"])
            self.assertFalse(result["authorizing"])
            self.assertEqual(result["source"]["provider"], "alpaca")
            self.assertEqual(result["source"]["feed"], "sip")
            self.assertEqual(result["source_provider"], "alpaca")
            self.assertEqual(result["source_feed"], "sip")
            self.assertEqual(result["source_provenance"],
                             {"provider": "alpaca", "feed": "sip"})
            self.assertEqual(result["provenance"],
                             {"provider": "alpaca", "feed": "sip"})
            self.assertEqual(result["proofs"], [])

    def test_factory_iex_default_keeps_proof_emission_path(self):
        row = {"kind": "bar", "provider": "alpaca", "feed": "iex"}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "market.jsonl"
            source.write_text(json.dumps(row) + "\n", encoding="utf-8")
            args = self._factory_args(source)
            with patch.object(research_cli, "_agent_config",
                              return_value=self._factory_config()), \
                 patch.object(research_cli, "run_factory",
                              return_value={"results": []}), \
                 patch.object(research_cli, "_emit_proofs",
                              return_value=[]) as emit, \
                 patch.object(research_cli, "_write_factory_report",
                              return_value=None), \
                 patch.object(research_cli, "print"):
                self.assertEqual(research_cli.cmd_factory_run(args), 2)
            emit.assert_called_once()

    def test_factory_parser_exposes_diagnostic_only_for_both_command_forms(self):
        parser = research_cli.build_parser()
        for argv in (
                ["factory", "run", "--data", "market.jsonl", "--diagnostic-only"],
                ["factory-run", "--data", "market.jsonl", "--diagnostic-only"]):
            with self.subTest(argv=argv):
                self.assertTrue(parser.parse_args(argv).diagnostic_only)

    def test_research_cycle_script_has_no_bash4_mapfile_or_empty_quote_array(self):
        script = (Path(__file__).parents[2] / "deploy" / "research-cycle.sh").read_text()
        self.assertNotRegex(script, r"(?m)^\s*mapfile\b")
        self.assertNotIn('"${quote_flags[@]}"', script)
        # Bash 3.2 still supports the rest of this script's syntax.
        import subprocess
        result = subprocess.run(["bash", "-n", str(Path(__file__).parents[2] /
                                                     "deploy" / "research-cycle.sh")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_research_cycle_preflights_llm_before_vehicle_resolution(self):
        script = (Path(__file__).parents[2] / "deploy" / "research-cycle.sh").read_text()
        probe = script.index("research.py\" llm-preflight")
        vehicles = script.index('research.py" vehicles')
        self.assertLess(probe, vehicles)
        self.assertIn("research-llm-preflight-warning.v1", script)
        self.assertNotIn('research-llm.v1","status":"ready"', script)


if __name__ == "__main__":
    unittest.main()
