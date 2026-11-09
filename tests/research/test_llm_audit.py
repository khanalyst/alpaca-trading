"""The readable LLM audit log: what the model was given, returned, and what was kept."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.contracts.rule import DEFAULT_RULE_SPEC
from research import llm_audit
from research.llm_strategy import PROPOSAL_SCHEMA, ProposalResult, RuleProposalAdapter


def reply(spec):
    return json.dumps({"schema": PROPOSAL_SCHEMA, "rule_spec": spec}, separators=(",", ":"))


class LLMAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "audit.jsonl"
        patcher = patch.dict(os.environ, {"ALPACA_LLM_AUDIT_PATH": str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_proposal_records_inputs_outputs_and_the_changed_fields(self):
        proposed = {**DEFAULT_RULE_SPEC, "threshold_bps": 14.0}
        result = RuleProposalAdapter(caller=lambda **_: reply(proposed), max_attempts=1,
                                     model="model-x").propose(
            vehicle="equity", generation=2, prior_validated_rule_spec=DEFAULT_RULE_SPEC,
            diagnosis={"primary_failure": "negative_expectancy"})
        self.assertTrue(result.success, result.error)
        rows = llm_audit.read(self.path)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["event"], row["kind"], row["model"]), ("call", "proposal", "model-x"))
        self.assertEqual(row["request"]["generation"], 2)
        self.assertEqual(row["request"]["diagnosis"], {"primary_failure": "negative_expectancy"})
        self.assertEqual(row["response"]["changed"], {"threshold_bps": [12.0, 14.0]})
        self.assertEqual(row["response"]["variant_id"], result.variant_id)

    def test_a_failed_call_is_recorded_with_its_error(self):
        def broken(**_):
            raise RuntimeError("provider down")
        result = RuleProposalAdapter(caller=broken, max_attempts=1).propose(
            vehicle="equity", generation=0, prior_validated_rule_spec=DEFAULT_RULE_SPEC,
            diagnosis={})
        self.assertFalse(result.success)
        row = llm_audit.read(self.path)[0]
        self.assertFalse(row["response"]["success"])
        self.assertIn("provider down", row["response"]["error"])

    def test_decisions_record_what_was_kept(self):
        proposal = ProposalResult(True, evidence={"rejected": "near_structure",
                                                  "variant_id": "rule.x.1"})
        llm_audit.record_decision("discovery", accepted=False, provider_success=True,
                                  error=None, evidence=proposal.evidence)
        llm_audit.record_decision("tuning", accepted=True, provider_success=True, error=None,
                                  evidence={}, kept=[{"variant_id": "rule.y.2",
                                                      "source": "llm", "reason": "r"}])
        rows = llm_audit.read(self.path)
        self.assertEqual(rows[0]["rejected_because"], "near_structure")
        self.assertEqual(rows[1]["kept"][0]["variant_id"], "rule.y.2")
        text = llm_audit.render(rows)
        self.assertIn("near_structure", text)
        self.assertIn("`rule.y.2` (llm)", text)

    def test_the_factory_decision_hook_writes_a_decision(self):
        from research.strategy_factory import _record_llm_observation
        observations = []
        proposal = ProposalResult(True, evidence={"variant_id": "rule.z.3"})
        _record_llm_observation(observations, "replacement", proposal, accepted=True,
                                kept=[{"variant_id": "rule.z.3", "source": "llm"}])
        self.assertEqual(len(observations), 1)
        row = llm_audit.read(self.path)[0]
        self.assertEqual((row["event"], row["kind"], row["accepted"]),
                         ("decision", "replacement", True))

    def test_off_disables_the_log(self):
        with patch.dict(os.environ, {"ALPACA_LLM_AUDIT_PATH": "off"}):
            llm_audit.record_decision("tuning", accepted=True, provider_success=True,
                                      error=None, evidence={})
        self.assertFalse(self.path.exists())

    def test_oversized_inputs_are_bounded(self):
        big = {"telemetry": ["x" * 500 for _ in range(100)]}
        bounded = llm_audit._bounded(big, limit=1024)
        self.assertTrue(bounded["_truncated"])
        self.assertEqual(bounded["keys"], ["telemetry"])

    def test_the_cli_renders_a_report(self):
        llm_audit.record_decision("tuning", accepted=True, provider_success=True,
                                  error=None, evidence={})
        out = Path(self.tmp.name) / "report.md"
        self.assertEqual(llm_audit.main(["--log", str(self.path), "--out", str(out)]), 0)
        self.assertIn("# LLM research calls", out.read_text())


if __name__ == "__main__":
    unittest.main()
