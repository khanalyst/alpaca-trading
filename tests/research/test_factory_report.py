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
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent.contracts.rule import (rule_spec_hash, rule_variant_id,
                                  validate_rule_spec)
import research.gates as gates
from research.factory_report import (DEFAULT_REPORT_ROOT, REPORT_SCHEMA,
                                     build_report, render_markdown,
                                     render_text, write_report)
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
            patch.object(factory_module, "_worker", side_effect=fake_adequate_worker), \
            patch.multiple(
                gates,
                PROTOCOL_BACKTEST_MIN_TRADES=1,
                PROTOCOL_BACKTEST_MIN_SESSIONS=1,
                PROTOCOL_BACKTEST_MIN_CLUSTERS=1,
                PROTOCOL_SHADOW_MIN_TRADES=1,
                PROTOCOL_SHADOW_MIN_SESSIONS=1,
                PROTOCOL_SHADOW_MIN_CLUSTERS=1,
                PROTOCOL_QUALIFICATION_MIN_TRADES=1,
                PROTOCOL_QUALIFICATION_MIN_SESSIONS=1,
                PROTOCOL_QUALIFICATION_MIN_CLUSTERS=1), \
            patch.object(factory_module, "MIN_PROMOTION_CLUSTERS", 1):
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
                # The hypothesis was retired after all development variants
                # failed.  Nonselected diagnostic candidates have no sealed
                # proof run, so their EdgeLedger lifecycle correctly remains
                # at candidate rather than inventing a retirement proof.
                self.assertEqual(variant["ledger_status"], "candidate")

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
            self.assertEqual(summary["llm_proposals_rejected"], 0)
            self.assertEqual(summary["retired_hypotheses"], 1)
            self.assertEqual(summary["variants_tested"], 2)

    def test_a_refused_proposal_is_not_counted_as_an_llm_hypothesis(self):
        """A provider that was asked and said no leaves a deterministic run."""
        class Refusing:
            def discover(self, **_):
                return ProposalResult(False, schema=DISCOVERY_SCHEMA,
                                      error="provider unavailable",
                                      evidence={"provider": "openai",
                                                "model": "gpt-5",
                                                "kind": "discovery"})

            def propose(self, **_):
                return ProposalResult(False, error="provider unavailable",
                                      evidence={"provider": "openai",
                                                "model": "gpt-5"})

        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "edge.sqlite3"
            with patch.object(factory_module, "ProcessPoolExecutor",
                              side_effect=OSError), \
                    patch.object(factory_module, "_worker",
                                 side_effect=fake_adequate_worker), \
                    patch.multiple(
                        gates,
                        PROTOCOL_BACKTEST_MIN_TRADES=1,
                        PROTOCOL_BACKTEST_MIN_SESSIONS=1,
                        PROTOCOL_BACKTEST_MIN_CLUSTERS=1,
                        PROTOCOL_SHADOW_MIN_TRADES=1,
                        PROTOCOL_SHADOW_MIN_SESSIONS=1,
                        PROTOCOL_SHADOW_MIN_CLUSTERS=1,
                        PROTOCOL_QUALIFICATION_MIN_TRADES=1,
                        PROTOCOL_QUALIFICATION_MIN_SESSIONS=1,
                        PROTOCOL_QUALIFICATION_MIN_CLUSTERS=1), \
                    patch.object(factory_module, "MIN_PROMOTION_CLUSTERS", 1):
                run_factory(losing_breakouts(), db_path=db, strategies=1,
                            variants_per_strategy=2, workers=1, min_trades=1,
                            min_sessions=1, alpha=1.0, max_generations=2,
                            strategy_llm={"enabled": True, "provider": "openai",
                                          "model": "gpt-5"},
                            proposal_adapter=Refusing())
            summary = build_report(db)["vehicles"][0]["summary"]
        self.assertEqual(summary["llm_seeded_hypotheses"], 0)
        self.assertGreaterEqual(summary["llm_proposals_rejected"], 1)

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
    def test_persisted_classifications_survive_counts_and_rendering(self):
        with tempfile.TemporaryDirectory() as directory:
            db = _run(directory)
            with sqlite3.connect(db) as connection:
                rows = connection.execute(
                    "SELECT cycle_id, hypothesis_id, vehicle, starting_cash, "
                    "ending_equity, realized_pnl, max_drawdown, trades, "
                    "worker_pid, result_json FROM factory_accounts "
                    "ORDER BY account_id").fetchall()
                self.assertGreaterEqual(len(rows), 2)
                for index, (row, classification) in enumerate(zip(
                        rows, ("execution_blocked", "qualification_unavailable"))):
                    (cycle_id, hypothesis_id, vehicle, starting_cash,
                     ending_equity, realized_pnl, max_drawdown, trades,
                     worker_pid, raw) = row
                    payload = json.loads(raw)
                    payload["classification"] = classification
                    payload["variant_id"] = f"report-fixture-{index}"
                    connection.execute(
                        "INSERT INTO factory_accounts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (f"report-fixture-account-{index}", cycle_id,
                         hypothesis_id, payload["variant_id"], vehicle,
                         starting_cash, ending_equity, realized_pnl,
                         max_drawdown, trades, worker_pid,
                         json.dumps(payload, sort_keys=True),
                         0.0))

            report = build_report(db)
            summary = report["vehicles"][0]["summary"]["classifications"]
            self.assertEqual(summary["execution_blocked"], 1)
            self.assertEqual(summary["qualification_unavailable"], 1)

            text = render_text(report)
            self.assertIn("[execution_blocked]", text)
            self.assertIn("[qualification_unavailable]", text)

            markdown = render_markdown(report)
            self.assertIn("| execution_blocked |", markdown)
            self.assertIn("| qualification_unavailable |", markdown)

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


class ArchivedReportTests(unittest.TestCase):
    """The narrative is only useful where somebody will actually see it.

    ``factory report`` printed to stdout and was never invoked by the
    scheduled cycle, so on the documented headless topology the one artifact
    explaining what research had been doing was written and never read.  It is
    now archived under ``research/results``, which is exactly the tree the
    read-only dashboard already lists.
    """

    def test_it_writes_markdown_the_dashboard_will_list(self):
        from deploy.dashboard import _reports, report_file

        with tempfile.TemporaryDirectory() as directory:
            db = _run(directory)
            root = Path(directory) / "site"
            target = write_report(db, vehicle="equity",
                                  output_root=root / "research" / "results" / "factory")
            self.assertIsNotNone(target)
            self.assertTrue(target.is_file())
            body = target.read_text(encoding="utf-8")
            self.assertIn("Autonomous research report", body)
            self.assertIn("vwap_reversion", body)

            listed = [row["path"] for row in _reports(root)]
            self.assertIn("research/results/factory/equity.md", listed)
            text, _kind = report_file(root, "research/results/factory/equity.md")
            self.assertEqual(text, body)
            # No stray staging file is left behind for the dashboard to serve.
            self.assertEqual(
                sorted(item.name for item in target.parent.iterdir()),
                ["equity.md"])

    def test_rewriting_replaces_rather_than_accumulates(self):
        with tempfile.TemporaryDirectory() as directory:
            db = _run(directory)
            root = Path(directory) / "out"
            first = write_report(db, vehicle="equity", output_root=root)
            second = write_report(db, vehicle="equity", output_root=root)
            self.assertEqual(first, second)
            self.assertEqual(len(list(root.iterdir())), 1)

    def test_an_empty_ledger_archives_nothing_instead_of_an_empty_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "out"
            self.assertIsNone(write_report(Path(directory) / "absent.sqlite3",
                                           output_root=root))
            self.assertFalse(root.exists())

    def test_the_default_root_is_inside_the_dashboards_report_tree(self):
        self.assertEqual(DEFAULT_REPORT_ROOT.parts[:2],
                         ("research", "results"))

    def test_a_cycle_archives_the_narrative_even_when_nothing_proved(self):
        """The cycle that found nothing is the one an operator needs to read."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = _run(directory)
            completed = subprocess.run(
                [sys.executable, str(REPO / "research.py"), "factory", "report",
                 "--db", str(db), "--vehicle", "equity", "--write",
                 "--format", "json"],
                capture_output=True, text=True,
                env={"PATH": "/usr/bin:/bin", "HOME": str(root),
                     "ALPACA_RESEARCH_REPORT_DIR": str(root / "archive")})
            self.assertEqual(completed.returncode, 0, completed.stderr)
            artifact = json.loads(completed.stderr.strip().splitlines()[-1])
            self.assertTrue(Path(artifact["artifact"]).is_file())
            self.assertEqual(Path(artifact["artifact"]).parent,
                             root / "archive")


if __name__ == "__main__":
    unittest.main()
