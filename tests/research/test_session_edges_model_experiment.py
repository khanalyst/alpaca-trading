"""Evidence boundary tests for proposal receipts and paired model experiments."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from research import session_model_experiment as experiment
from research.llm_strategy import content_hash
from studies.session_edges import grammar, propose


SPEC = {"family": "close_weakness_reversion_daily", "symbol": "SPY", "params": {"enter_below": 0.18}}
THESIS = "Forced late selling supplies liquidity at a discount and reverses later."
REPLY = json.dumps({"hypotheses": [{**SPEC, "thesis": THESIS}]})
SESSIONS = ["2026-01-05", "2026-01-06", "2026-01-07"]
DATA_HASH, CODE_HASH = "a" * 64, "b" * 40


def envelope(reply=REPLY, **kwargs):
    attempt_id = kwargs.pop("attempt_id", "provider-attempt-1")
    return propose.proposal_envelope(reply, provider="test-provider", model="test-model",
                                     prompt=propose.build_prompt(None, ["SPY"]), symbols=["SPY"],
                                     attempt_id=attempt_id, **kwargs)


def ledger(budget=1):
    return experiment.new_ledger(experiment.create_plan(
        lanes=[{"name": "grid", "kind": "deterministic"},
               {"name": "model-a", "kind": "model", "provider": "test-provider", "model": "test-model"}],
        candidate_budget=budget, design_end="2026-01-01", oos_sessions=SESSIONS,
        data_hash=DATA_HASH, code_hash=CODE_HASH))


def admitted_ledger(*, distinct=False):
    result = experiment.record_attempt(ledger(), lane_name="grid", attempt_id="grid-1", status="proposed", spec=SPEC)
    reply = json.dumps({"hypotheses": [{**SPEC, "params": {"enter_below": 0.19}, "thesis": THESIS}]}) if distinct else REPLY
    return experiment.record_attempt(result, lane_name="model-a", attempt_id="model-1", status="proposed", envelope=envelope(reply))


class ProposalReceiptTests(unittest.TestCase):
    def test_exact_call_response_and_thesis_survive_receipt(self):
        observed = {}
        raw = {"id": "response-123", "model": "pinned-model-version", "usage": {"total_tokens": 19},
               "choices": [{"message": {"content": REPLY}, "finish_reason": "stop"}]}

        class Response:
            id = raw["id"]
            model = raw["model"]
            system_fingerprint = "fingerprint-456"
            choices = [SimpleNamespace(message=SimpleNamespace(content=REPLY))]

            def model_dump(self, *, mode):
                assert mode == "json"
                return raw

        def create(**kwargs):
            observed.update(kwargs)
            return Response()

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
                                 base_url="https://user:secret@example.com/v1?api_key=hidden")
        prompt = propose.build_prompt(None, ["SPY"])
        receipt = propose.ask_envelope("test-model", prompt, provider="test-provider", symbols=["SPY"],
                                        config={"temperature": 0, "seed": 17}, client=client, attempt_id="call-1")
        evidence = receipt["provenance"]
        self.assertEqual(evidence["request"], observed)
        self.assertEqual(evidence["provider_response"], raw)
        self.assertEqual(evidence["prompt"], prompt)
        self.assertEqual(evidence["response_hash"], content_hash({"text": REPLY, "payload": raw}))
        self.assertTrue(evidence["provider_call_observed"])
        self.assertEqual(evidence["endpoint"]["origin"], "https://example.com")
        self.assertNotIn("secret", json.dumps(receipt))
        self.assertNotIn("hidden", json.dumps(receipt))
        row = propose.proposal_rows(receipt)[0]
        self.assertEqual(row["thesis"], THESIS)
        self.assertEqual(row["proposal_attribution"]["reported_model"], "pinned-model-version")
        self.assertEqual(row["proposal_attribution"]["attempt_id"], "call-1")

    def test_receipt_rejects_payload_mutation_and_consistent_hash_splicing(self):
        receipt = envelope()
        mutated = copy.deepcopy(receipt)
        mutated["hypotheses"][0]["params"]["enter_below"] = 0.4
        with self.assertRaisesRegex(ValueError, "integrity"):
            propose.proposal_rows(mutated)
        # Recomputing an outer digest cannot silently splice an unrelated
        # request into otherwise internally inconsistent captured evidence.
        mutated = copy.deepcopy(receipt)
        mutated["provenance"]["request"]["model"] = "different-model"
        mutated["provenance"]["request_hash"] = content_hash(mutated["provenance"]["request"])
        mutated.pop("envelope_hash")
        mutated["envelope_hash"] = content_hash(mutated)
        with self.assertRaisesRegex(ValueError, "request differs"):
            propose.proposal_rows(mutated)

    def test_provider_failure_retains_attempt_without_secrets_or_fabricated_response(self):
        def fail(**_kwargs):
            raise RuntimeError("authorization failed api_key=do-not-retain")

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fail)))
        receipt = propose.ask_envelope("test-model", propose.build_prompt(None, ["SPY"]), symbols=["SPY"],
                                        provider="test-provider", client=client, attempt_id="failed-1")
        self.assertEqual(receipt["attempt_id"], "failed-1")
        self.assertEqual(receipt["status"], "provider_failed")
        self.assertEqual(receipt["error_type"], "RuntimeError")
        self.assertNotIn("do-not-retain", json.dumps(receipt))
        self.assertIsNone(receipt["provenance"]["response_text"])
        self.assertEqual(propose.proposal_rows(receipt), [])

    def test_manual_and_legacy_paths_explicitly_lack_call_evidence(self):
        manual = propose.proposal_envelope(REPLY, provider="manual", model="unknown")
        evidence = propose.proposal_rows(manual)[0]["proposal_attribution"]
        self.assertFalse(evidence["attribution_complete"])
        self.assertFalse(evidence["provider_call_observed"])
        legacy = propose.proposal_rows([{**SPEC, "thesis": THESIS}])[0]
        self.assertEqual(legacy["proposal_attribution"]["status"], "legacy_unattributed")

    def test_nested_leaderboard_cannot_leak_holdout_fields(self):
        record = {"design_leaderboard": [{"spec": {**SPEC, "thesis": "heldout secret"},
                                           "design_t": 3.2, "holdout": {"t": 987.654},
                                           "screen_failures": ["sealed-pvalue=0.001"]}]}
        prompt = propose.build_prompt(record, ["SPY"])
        for blocked in ("heldout secret", "987.654", "sealed-pvalue", "holdout"):
            self.assertNotIn(blocked, prompt["user"])
        self.assertIn("3.2", prompt["user"])

    def test_check_cli_writes_reusable_envelope(self):
        with tempfile.TemporaryDirectory() as folder:
            reply_path, out = Path(folder) / "reply.json", Path(folder) / "proposals.json"
            reply_path.write_text(REPLY)
            self.assertEqual(propose.main(["check", str(reply_path), "--symbols", "SPY", "--out", str(out)]), 0)
            rows = propose.proposal_rows(json.loads(out.read_text()))
            self.assertEqual(grammar.spec_id(
                {key: rows[0][key] for key in ("family", "symbol", "params")}), grammar.spec_id(SPEC))


class ModelExperimentTests(unittest.TestCase):
    def test_unused_response_rows_cannot_hide_search_outside_equal_budget(self):
        response = json.dumps({"hypotheses": [{**SPEC, "thesis": THESIS},
                                               {**SPEC, "params": {"enter_below": 0.99}, "thesis": "Invalid value."}]})
        receipt = envelope(response)
        result = experiment.record_attempt(ledger(), lane_name="grid", attempt_id="grid-1", status="proposed", spec=SPEC)
        result = experiment.record_attempt(result, lane_name="model-a", attempt_id="model-1", status="proposed", envelope=receipt)
        with self.assertRaisesRegex(ValueError, "every hypothesis and rejection"):
            experiment.freeze_selection(result, {"grid": "grid-1", "model-a": "model-1"})
        result = experiment.record_attempt(ledger(2), lane_name="grid", attempt_id="grid-1", status="proposed", spec=SPEC)
        result = experiment.record_attempt(result, lane_name="grid", attempt_id="grid-2", status="invalid")
        result = experiment.record_attempt(result, lane_name="model-a", attempt_id="model-1", status="proposed", envelope=receipt)
        with self.assertRaisesRegex(ValueError, "already spent"):
            experiment.record_attempt(result, lane_name="model-a", attempt_id="model-reused", status="proposed", envelope=receipt)
        result = experiment.record_attempt(result, lane_name="model-a", attempt_id="model-invalid", status="invalid", envelope=receipt)
        self.assertIsNotNone(experiment.freeze_selection(result, {"grid": "grid-1", "model-a": "model-1"})["selection"])

    def test_equal_budget_includes_rejections_and_failed_calls(self):
        result = experiment.record_attempt(ledger(2), lane_name="grid", attempt_id="grid-1", status="proposed", spec=SPEC)
        result = experiment.record_attempt(result, lane_name="model-a", attempt_id="model-1", status="proposed", envelope=envelope())
        result = experiment.record_attempt(result, lane_name="grid", attempt_id="grid-2", status="invalid")
        with self.assertRaisesRegex(ValueError, "same candidate budget"):
            experiment.freeze_selection(result, {"grid": "grid-1", "model-a": "model-1"})
        failure = envelope(None, error_type="TimeoutError", attempt_id="provider-failed-2")
        result = experiment.record_attempt(result, lane_name="model-a", attempt_id="model-failed", status="provider_failed",
                                             envelope=failure)
        frozen = experiment.freeze_selection(result, {"grid": "grid-1", "model-a": "model-1"})
        self.assertEqual(len(frozen["attempts"]), 4)
        with self.assertRaisesRegex(ValueError, "frozen"):
            experiment.record_attempt(frozen, lane_name="model-a", attempt_id="model-retry", status="invalid", envelope=envelope("bad"))
        self.assertEqual(experiment.report(result)["lanes"][1]["attempt_ids"], ["model-1", "model-failed"])

    def test_duplicates_preserve_both_claims_without_becoming_independent_specs(self):
        result = admitted_ledger()
        self.assertEqual(result["attempts"][1]["status"], "duplicate")
        self.assertEqual(result["attempts"][1]["duplicate_of"], "grid-1")
        self.assertEqual(result["attempts"][1]["proposal_attribution"]["thesis"], THESIS)
        self.assertEqual(result["attempts"][0]["spec_id"], result["attempts"][1]["spec_id"])

    def test_freeze_and_exact_paired_oos_grid_are_mandatory(self):
        result = admitted_ledger(distinct=True)
        outcomes = {session: float(index) for index, session in enumerate(SESSIONS)}
        with self.assertRaisesRegex(ValueError, "freeze every lane"):
            experiment.record_outcomes(result, lane_name="grid", net_bps_by_session=outcomes,
                                       data_hash=DATA_HASH, code_hash=CODE_HASH)
        result = experiment.freeze_selection(result, {"grid": "grid-1", "model-a": "model-1"})
        for invalid in ({SESSIONS[0]: 1.0}, {**outcomes, SESSIONS[-1]: float("nan")}):
            with self.assertRaises(ValueError):
                experiment.record_outcomes(result, lane_name="grid", net_bps_by_session=invalid,
                                           data_hash=DATA_HASH, code_hash=CODE_HASH)
        with self.assertRaisesRegex(ValueError, "source/code"):
            experiment.record_outcomes(result, lane_name="grid", net_bps_by_session=outcomes,
                                       data_hash="c" * 64, code_hash=CODE_HASH)
        result = experiment.record_outcomes(result, lane_name="grid", net_bps_by_session=outcomes,
                                           data_hash=DATA_HASH, code_hash=CODE_HASH)
        self.assertFalse(experiment.report(result)["complete_paired_oos"])
        result = experiment.record_outcomes(result, lane_name="model-a", net_bps_by_session={
            session: value + 0.5 for session, value in outcomes.items()}, data_hash=DATA_HASH, code_hash=CODE_HASH)
        report = experiment.report(result)
        self.assertTrue(report["complete_paired_oos"])
        self.assertEqual(report["lanes"][1]["paired_oos"]["mean_delta_net_bps"], 0.5)
        self.assertIsNone(report["lanes"][1]["paired_oos"]["hac_t_diagnostic"])
        self.assertFalse(report["authorizing"])
        self.assertFalse(report["model_performance_claim"])
        self.assertEqual(report["lanes"][1]["observed_successful_provider_calls"], 0)
        with self.assertRaisesRegex(ValueError, "immutable"):
            experiment.record_outcomes(result, lane_name="grid", net_bps_by_session=outcomes,
                                       data_hash=DATA_HASH, code_hash=CODE_HASH)

    def test_same_frozen_rule_cannot_claim_different_shared_oos_returns(self):
        result = experiment.freeze_selection(admitted_ledger(), {"grid": "grid-1", "model-a": "model-1"})
        outcomes = dict.fromkeys(SESSIONS, 1.0)
        result = experiment.record_outcomes(result, lane_name="grid", net_bps_by_session=outcomes,
                                           data_hash=DATA_HASH, code_hash=CODE_HASH)
        with self.assertRaisesRegex(ValueError, "identical OOS"):
            experiment.record_outcomes(result, lane_name="model-a", net_bps_by_session=dict.fromkeys(SESSIONS, 2.0),
                                       data_hash=DATA_HASH, code_hash=CODE_HASH)
        result = experiment.record_outcomes(result, lane_name="model-a", net_bps_by_session=outcomes,
                                           data_hash=DATA_HASH, code_hash=CODE_HASH)
        self.assertEqual(experiment.report(result)["lanes"][1]["paired_oos"]["mean_delta_net_bps"], 0)

    def test_provider_identity_and_exact_response_candidate_are_bound(self):
        wrong = propose.proposal_envelope(REPLY, provider="wrong-provider", model="test-model")
        with self.assertRaisesRegex(ValueError, "provider/model"):
            experiment.record_attempt(ledger(), lane_name="model-a", attempt_id="model-1", status="proposed", envelope=wrong)
        with self.assertRaisesRegex(ValueError, "differs from the retained"):
            experiment.record_attempt(ledger(), lane_name="model-a", attempt_id="model-1", status="proposed", envelope=envelope(),
                                      spec={**SPEC, "params": {"enter_below": 0.4}})
        tampered = ledger()
        tampered["plan"]["candidate_budget"] = 99
        with self.assertRaisesRegex(ValueError, "integrity"):
            experiment.report(tampered)


if __name__ == "__main__":
    unittest.main()
