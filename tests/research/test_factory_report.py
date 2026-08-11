"""The discovery report: everything the ledgers know, made readable.

The factory and edge ledgers already recorded each hypothesis, every variant's
full gate, the diagnosis behind each mutation, why a family was retired and
after how many variants, and the provider/prompt hashes behind an LLM proposal.
None of it could be read — ``factory status`` returned rows and three counts.
These tests pin the reader: that it answers each of those questions, that it is
strictly derived and never writes, and that it degrades rather than crashing on
a partial or malformed ledger.
"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent.contracts.rule import (rule_spec_hash, rule_variant_id,
                                  validate_rule_spec)
from research.factory_report import (REPORT_SCHEMA, build_report,
                                     render_markdown, render_text)
import research.strategy_factory as factory_module
from research.llm_strategy import (DISCOVERY_SCHEMA, PROPOSAL_SCHEMA,
                                   ProposalResult)
from research.strategy_factory import run_factory
from tests.research.test_strategy_factory import (fake_adequate_worker,
                                                  losing_breakouts)

REPO = Path(__file__).resolve().parents[2]

REPLACEMENT = validate_rule_spec({
    "family": "volume_breakout", "lookback": 9, "slow_lookback": 25,
    "threshold_bps": 12, "confirmation": "trend"})
DISCOVERED = validate_rule_spec({
    "schema": "rule-strategy.v2", "family": "vwap_reversion",
    "entry_after_minutes": 45, "entry_before_minutes": 300,
    "confirmations": ["volume"]})
THESIS = "Late-morning stretch from session VWAP reverts when volume confirms."


class Adapter:
    """Answers both bounded requests with fixed, inspectable evidence."""

    def propose(self, **_):
        return ProposalResult(
            True, schema=PROPOSAL_SCHEMA, rule_spec=REPLACEMENT,
            variant_id=rule_variant_id(REPLACEMENT),
            spec_id=rule_spec_hash(REPLACEMENT),
            evidence={"provider": "openai", "model": "gpt-5", "attempts": 1,
                      "request_hash": "r" * 64, "raw_response_hash": "x" * 64,
                      "system_prompt_hash": "s" * 64,
                      "normalized_spec_hash": rule_spec_hash(REPLACEMENT)})

    def discover(self, **_):
        return ProposalResult(
            True, schema=DISCOVERY_SCHEMA, rule_spec=DISCOVERED,
            variant_id=rule_variant_id(DISCOVERED),
            spec_id=rule_spec_hash(DISCOVERED), thesis=THESIS,
            evidence={"provider": "openai", "model": "gpt-5",
                      "kind": "discovery", "attempts": 1,
                      "request_hash": "q" * 64, "raw_response_hash": "y" * 64,
                      "system_prompt_hash": "p" * 64})


def _run(directory, *, llm=True):
    """One real cycle whose single family fails and is replaced."""
    db = Path(directory) / "edge.sqlite3"
    with patch.object(factory_module, "ProcessPoolExecutor", side_effect=OSError), \
            patch.object(factory_module, "_worker", side_effect=fake_adequate_worker):
        run_factory(
            losing_breakouts(), db_path=db, strategies=1,
            variants_per_strategy=2, workers=1, min_trades=1, min_sessions=1,
            alpha=1.0, max_generations=2,
            strategy_llm=({"enabled": True, "provider": "openai",
                           "model": "gpt-5"} if llm else None),
            proposal_adapter=Adapter() if llm else None)
    return db


class ReportContentTests(unittest.TestCase):
    def test_it_answers_every_question_the_ledgers_can_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            report = build_report(_run(directory))
            self.assertEqual(report["schema"], REPORT_SCHEMA)
            self.assertTrue(report["available"])
            vehicle = report["vehicles"][0]
            generations = vehicle["slots"][0]["generations"]
            self.assertEqual(len(generations), 2)
            first, second = generations

            # Which edges are being discovered, and on whose suggestion.
            self.assertEqual(first["family"], "vwap_reversion")
            self.assertEqual(first["origin"]["kind"], "llm_discovery")
            self.assertEqual(first["thesis"], THESIS)
            self.assertEqual(first["origin"]["llm"]["model"], "gpt-5")
            self.assertEqual(first["origin"]["llm"]["request_hash"], "q" * 64)
            self.assertEqual(first["rule_schema"], "rule-strategy.v2")

            # Each variant and how it did.
            self.assertEqual(first["variants_tested"], 2)
            for variant in first["variants"]:
                self.assertFalse(variant["passes"])
                self.assertEqual(variant["lane"], "backtest")
                self.assertIsNotNone(variant["trades"])
                self.assertIsNotNone(variant["heldout_delta"])
                self.assertEqual(variant["primary_failure"], "negative_expectancy")
                self.assertIn("made money held-out", variant["failed_checks"])
                self.assertEqual(variant["ledger_status"], "retired")

            # What was retired, why, and after how many variants.
            outcome = first["outcome"]
            self.assertEqual(outcome["kind"], "retired")
            self.assertEqual(outcome["variants_tested"], 2)
            self.assertEqual(outcome["variants_intended"], 2)
            self.assertEqual(outcome["primary_failure"], "negative_expectancy")
            self.assertEqual(outcome["replacement_hypothesis_id"],
                             second["hypothesis_id"])
            self.assertEqual(len(outcome["failed_gate_hashes"]), 2)

            # And what replaced it, on whose suggestion.
            self.assertEqual(second["origin"]["kind"], "llm_replacement")
            self.assertEqual(second["origin"]["llm"]["response_hash"], "x" * 64)
            self.assertEqual(second["family"], "volume_breakout")

            summary = vehicle["summary"]
            self.assertEqual(summary["llm_seeded_hypotheses"], 2)
            self.assertEqual(summary["retired_hypotheses"], 1)
            self.assertEqual(summary["variants_tested"], 2)

    def test_a_deterministic_run_reports_no_llm_involvement(self):
        with tempfile.TemporaryDirectory() as directory:
            report = build_report(_run(directory, llm=False))
            vehicle = report["vehicles"][0]
            self.assertEqual(vehicle["summary"]["llm_seeded_hypotheses"], 0)
            for entry in vehicle["slots"]:
                for item in entry["generations"]:
                    self.assertNotIn("llm", item["origin"])

    def test_trade_rows_never_leak_into_the_report(self):
        """Accounts store every simulated row; a summary must not carry them."""
        with tempfile.TemporaryDirectory() as directory:
            report = build_report(_run(directory))
            text = json.dumps(report)
            self.assertNotIn("opportunity_id", text)
            self.assertNotIn("entry_timestamp", text)

    def test_the_report_never_writes_to_the_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            db = _run(directory)
            before = db.read_bytes()
            build_report(db)
            build_report(db, vehicle="equity")
            build_report(db, slot=0)
            self.assertEqual(db.read_bytes(), before)

    def test_filters_narrow_without_changing_content(self):
        with tempfile.TemporaryDirectory() as directory:
            db = _run(directory)
            self.assertEqual(build_report(db, vehicle="option")["vehicles"], [])
            self.assertEqual(build_report(db, slot=3)["vehicles"], [])
            scoped = build_report(db, vehicle="equity", slot=0)
            self.assertEqual(scoped["vehicles"][0]["slots"][0]["slot"], 0)
            with self.assertRaises(ValueError):
                build_report(db, vehicle="crypto")


class ReportDegradationTests(unittest.TestCase):
    def test_a_missing_ledger_reports_absence_rather_than_raising(self):
        report = build_report(Path("/nonexistent/edge.sqlite3"))
        self.assertFalse(report["available"])
        self.assertEqual(report["vehicles"], [])
        self.assertIn("not created", report["reason"])
        self.assertIn("No research lineage yet", render_text(report))

    def test_an_edge_ledger_without_factory_lineage_is_handled(self):
        from research.edge_lab import init_ledger
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "edge.sqlite3"
            init_ledger(db)
            report = build_report(db)
            self.assertFalse(report["available"])
            self.assertIn("no factory lineage", report["reason"])


class ReportRenderingTests(unittest.TestCase):
    def test_text_renders_the_narrative_a_reader_needs(self):
        with tempfile.TemporaryDirectory() as directory:
            text = render_text(build_report(_run(directory)))
            for fragment in (
                    "VEHICLE: equity", "SLOT 0", "via llm discovery", THESIS,
                    "proposed by openai/gpt-5", "variants tested: 2",
                    "outcome: retired",
                    "after 2 of 2 intended variants failed",
                    "dominant failure mode: negative_expectancy",
                    "via llm replacement", "failed:"):
                self.assertIn(fragment, text, fragment)

    def test_markdown_renders_a_variant_table(self):
        with tempfile.TemporaryDirectory() as directory:
            markdown = render_markdown(build_report(_run(directory)))
            self.assertIn("# Autonomous research report", markdown)
            self.assertIn("| variant | lane | trades |", markdown)
            self.assertIn(f"> {THESIS}", markdown)


class ReportCommandTests(unittest.TestCase):
    def _run_cli(self, db, *arguments):
        return subprocess.run(
            [sys.executable, "research.py", "factory", "report",
             "--db", str(db), *arguments],
            cwd=REPO, capture_output=True, text=True, check=False)

    def test_every_format_is_available_from_the_command_line(self):
        with tempfile.TemporaryDirectory() as directory:
            db = _run(directory)
            text = self._run_cli(db)
            self.assertEqual(text.returncode, 0, text.stderr)
            self.assertIn("VEHICLE: equity", text.stdout)

            payload = self._run_cli(db, "--format", "json")
            self.assertEqual(payload.returncode, 0, payload.stderr)
            self.assertEqual(json.loads(payload.stdout)["schema"], REPORT_SCHEMA)

            markdown = self._run_cli(db, "--format", "markdown")
            self.assertEqual(markdown.returncode, 0, markdown.stderr)
            self.assertIn("# Autonomous research report", markdown.stdout)

    def test_a_missing_ledger_exits_non_zero_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._run_cli(Path(directory) / "absent.sqlite3")
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
