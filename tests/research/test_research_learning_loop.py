import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from agent import variants
from research import findings, review
from tests.helpers import valid_config


START = 1_800_000_000.0
STEP = 12 * 3_600.0
MODEL_ID = "test.forward.model.v1"


class FakeReviewer:
    provider = "openai"
    model = "test-review-model"
    prompt_version = "test-review-prompt"

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


class ResearchLearningFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "findings.db"
        self.store = findings.FindingsStore(self.path)
        self.registry = variants.load_registry(
            Path(__file__).resolve().parents[2] / "research" / "variants.yaml")
        self.baseline = variants.baseline("momentum", "phase1-v3")
        self.store.register(self.baseline)

    def _assignment(
            self, scope="demo:learning:feed-v1",
            candidate_id="momentum.rr.fixed_2_5", observations=100):
        candidate = self.registry[candidate_id]
        self.store.register(candidate)
        descriptor = {
            "variant_id": candidate.variant_id,
            "candidate_key": f"{scope}:{candidate.variant_id}",
            "axis": next(iter(candidate.overrides)),
            "setting_id": candidate.variant_id.rsplit(".", 1)[-1],
            "setting": dict(candidate.overrides),
            "source": "static", "priority": 10,
            "order_key": candidate.variant_id,
            "code_identity": {
                "baseline": {"code_version": "test-code",
                             "forward_model_id": MODEL_ID},
                "candidate": {"code_version": "test-code",
                              "forward_model_id": MODEL_ID},
            },
            "config_identity": {
                "baseline": {"strategy_config_version": "base-config"},
                "candidate": {"strategy_config_version": "candidate-config"},
            },
        }
        assignment = self.store.ensure_experiment_assignment(
            scope, "momentum", self.baseline.variant_id, [descriptor],
            minimum_duration_seconds=0,
            minimum_observations=observations, now=START)
        self.store.load_paper_portfolio(
            scope, self.baseline.variant_id, now=START)
        self.store.load_paper_portfolio(
            scope, candidate.variant_id, now=START)
        return assignment, candidate

    @staticmethod
    def _returns(mode, index, baseline_r):
        if mode == "worked":
            return baseline_r + 0.20 + (index % 5) * 0.02
        if mode == "failed":
            return baseline_r - 0.25 - (index % 5) * 0.01
        if mode == "inconsistent":
            return (baseline_r + 0.35 + (index % 5) * 0.01
                    if index < 70
                    else baseline_r - 0.45 - (index % 5) * 0.01)
        return baseline_r + 0.20

    def _seed(self, assignment, count, mode):
        scope = assignment["scope_key"]
        baseline_id = assignment["baseline_variant_id"]
        candidate_id = assignment["candidate_variant_id"]
        pattern = (-0.30, 0.10, 0.20, 0.40, 0.0)
        with sqlite3.connect(self.path) as conn:
            for index in range(count):
                ts = START + (index + 1) * STEP
                proposal_id = f"{assignment['assignment_id']}-{index:04d}"
                baseline_r = pattern[index % len(pattern)]
                candidate_r = self._returns(mode, index, baseline_r)
                for variant_id, r_multiple, lane in (
                        (baseline_id, baseline_r, "baseline"),
                        (candidate_id, candidate_r, "candidate")):
                    trade_id = f"{lane}-{assignment['assignment_id']}-{index}"
                    risk = 100.0
                    notional = 10_000.0
                    net_pnl = r_multiple * risk
                    modeled_cost = 3.0
                    entry_price = 100.0
                    exit_price = entry_price * (
                        1.0 + (net_pnl + modeled_cost) / notional)
                    provenance = {
                        "variant_definition_hash": variant_id,
                        "strategy_config_version": f"{lane}-config",
                        "experiment_config": {"lane": lane},
                        "code_version": "test-code",
                        "forward_model_id": MODEL_ID,
                        "forward_model_assumptions_hash": "test-model-hash",
                    }
                    assumptions = json.dumps({
                        "forward_model": {"model_id": MODEL_ID},
                        "experiment_provenance": provenance,
                        "cost_components": {
                            "entry_fee_pct": 0.01,
                            "exit_fee_pct": 0.01,
                            "spread_pct": 0.01,
                            "entry_slippage_pct": 0.0,
                            "stop_slippage_pct": 0.0,
                            "funding_rate_pct": 0.0,
                            "funding_interval_hours": 8.0,
                            "next_funding_minutes": 480.0,
                        },
                    }, sort_keys=True)
                    conn.execute("""
                        INSERT INTO paper_trades (
                            trade_id, proposal_id, scope_key, variant_id,
                            cycle_id, symbol, direction, setup_type, signal_ts,
                            model_id, assumptions_json, entry_ts, entry_price,
                            notional, risk_usd, stop_price, take_price, exit_ts,
                            exit_price, result, net_pnl_usd, r_multiple, status,
                            failure)
                        VALUES (
                            :trade_id, :proposal_id, :scope_key, :variant_id,
                            :cycle_id, :symbol, :direction, :setup_type,
                            :signal_ts, :model_id, :assumptions_json, :entry_ts,
                            :entry_price, :notional, :risk_usd, :stop_price,
                            :take_price, :exit_ts, :exit_price, :result,
                            :net_pnl_usd, :r_multiple, 'CLOSED', NULL)
                    """, {
                        "trade_id": trade_id, "proposal_id": proposal_id,
                        "scope_key": scope, "variant_id": variant_id,
                        "cycle_id": f"cycle-{index}",
                        "symbol": "BTC/USDT:USDT", "direction": "long",
                        "setup_type": "trend_continuation",
                        "signal_ts": int(ts * 1000), "model_id": MODEL_ID,
                        "assumptions_json": assumptions, "entry_ts": ts,
                        "entry_price": entry_price, "notional": notional,
                        "risk_usd": risk, "stop_price": 95.0,
                        "take_price": 110.0, "exit_ts": ts + 60.0,
                        "exit_price": exit_price,
                        "result": "target" if net_pnl >= 0 else "stop",
                        "net_pnl_usd": net_pnl,
                        "r_multiple": r_multiple,
                    })
                    conn.execute("""
                        INSERT INTO paper_decisions (
                            decision_id, proposal_id, scope_key, variant_id,
                            cycle_id, symbol, direction, setup_type, signal_ts,
                            confidence, decision_outcome, reason, paper_trade_id,
                            model_id, assumptions_json, proposal_json,
                            decision_ts)
                        VALUES (
                            :decision_id, :proposal_id, :scope_key, :variant_id,
                            :cycle_id, :symbol, :direction, :setup_type,
                            :signal_ts, 0.8, 'PROPOSED', NULL, :paper_trade_id,
                            :model_id, :assumptions_json, '{}', :decision_ts)
                    """, {
                        "decision_id": f"decision-{trade_id}",
                        "proposal_id": proposal_id, "scope_key": scope,
                        "variant_id": variant_id,
                        "cycle_id": f"cycle-{index}",
                        "symbol": "BTC/USDT:USDT", "direction": "long",
                        "setup_type": "trend_continuation",
                        "signal_ts": int(ts * 1000),
                        "paper_trade_id": trade_id, "model_id": MODEL_ID,
                        "assumptions_json": assumptions, "decision_ts": ts,
                    })
        self.store.record_experiment_observations(
            assignment["assignment_id"], [{
                "observation_key": (
                    f"{assignment['assignment_id']}-{index:04d}"),
                "observed_ts": START + (index + 1) * STEP,
            } for index in range(count)], now=START + count * STEP)

    def _complete(self, assignment, count):
        return self.store.maybe_complete_experiment_assignment(
            assignment["assignment_id"], now=START + (count + 1) * STEP)


class DeterministicOutcomeTests(ResearchLearningFixture):
    def test_schema_11_terminal_history_backfills_without_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schema11.db"
            baseline = variants.baseline("momentum", "phase1-v3")
            candidate = self.registry["momentum.rr.fixed_2_5"]
            descriptor = {
                "variant_id": candidate.variant_id,
                "candidate_key": "schema11-exact-candidate",
                "axis": "strategy.fixed_reward_risk",
                "setting_id": "fixed_2_5",
                "setting": {"strategy.fixed_reward_risk": 2.5},
                "source": "static", "priority": 10,
                "order_key": candidate.variant_id,
                "code_identity": {"schema": 11},
                "config_identity": {"schema": 11},
            }
            with patch.object(findings, "SCHEMA_VERSION", 11):
                old = findings.FindingsStore(path)
                old.register(baseline)
                old.register(candidate)
                assignment = old.ensure_experiment_assignment(
                    "demo:schema11", "momentum", baseline.variant_id,
                    [descriptor], minimum_duration_seconds=0,
                    minimum_observations=1, now=START)
                old.reject_experiment_assignment(
                    assignment["assignment_id"], "schema 11 rejection",
                    now=START + 1)
                self.assertEqual(old.schema_version(), 11)

            migrated = findings.FindingsStore(path)
            outcome = migrated.experiment_outcome(assignment["assignment_id"])

            self.assertEqual(migrated.schema_version(), findings.SCHEMA_VERSION)
            self.assertEqual(
                migrated.migration_history()[-1]["name"],
                "paper_execution_evidence_and_validity")
            # Backfilled history uses the same rule as a live rejection: an
            # operational abort is INCONCLUSIVE, never FAILED.
            self.assertEqual(outcome["verdict"], "INCONCLUSIVE")
            self.assertEqual(
                migrated.experiment_assignment(
                    assignment["assignment_id"])["status"], "REJECTED")

    def test_positive_assignment_saves_research_only_edge_before_qualification(self):
        assignment, candidate = self._assignment()
        self._seed(assignment, 100, "worked")

        terminal = self._complete(assignment, 100)
        outcome = self.store.experiment_outcome(assignment["assignment_id"])

        self.assertEqual(terminal["status"], "COMPLETED")
        self.assertEqual(outcome["verdict"], "WORKED")
        self.assertIn(
            "ALL_CONSERVATIVE_GATES_PASSED",
            {item["code"] for item in outcome["payload"]["reasons"]})
        self.assertEqual(outcome["payload"]["candidate"]["closes"], 100)
        self.assertGreater(
            outcome["payload"]["candidate"]["costs"]["fees_usdt"], 0)
        self.assertIn(
            "funding_cost_usdt",
            outcome["payload"]["candidate"]["costs"])
        limitations = outcome["payload"]["data_limitations"]
        self.assertTrue(any(
            item["code"] == "FORWARD_AXIS_QUALIFICATION_REQUIRED"
            and "current v5 forward-axis analysis" in item["detail"]
            for item in limitations))
        self.assertEqual(
            outcome["payload"]["feed_identity"]["forward_feed_version"], 1)
        edges = self.store.edge_evidence(assignment["scope_key"])
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["current_status"], "EDGE_CANDIDATE")
        self.assertFalse(edges[0]["protocol_satisfied"])
        self.assertEqual(edges[0]["evidence"]["authority"], "RESEARCH_ONLY")
        self.assertFalse(edges[0]["evidence"]["promotion_allowed"])
        self.assertEqual(
            edges[0]["evidence"]["variant_identity"]["variant_id"],
            candidate.variant_id)
        context = self.store.research_history_context(assignment["scope_key"])
        self.assertEqual(len(context["edge_candidates"]), 1)
        self.assertEqual(context["retryable_inconclusive_assignments"], [])
        self.assertIn(candidate.variant_id, context["terminal_variant_ids"])
        self.assertEqual(self.store.variant(candidate.variant_id)["status"],
                         candidate.status)
        finding = self.store.findings_for(candidate.variant_id)[-1]
        self.assertEqual(
            json.loads(finding["text"])["verdict"], "WORKED")
        self.assertIsNotNone(self.store.analysis(outcome["analysis_id"]))

    def test_failed_and_inconclusive_results_are_persisted_without_edges(self):
        failed_assignment, failed_candidate = self._assignment(
            scope="demo:failed:feed-v1")
        self._seed(failed_assignment, 100, "failed")
        self._complete(failed_assignment, 100)
        failed = self.store.experiment_outcome(
            failed_assignment["assignment_id"])

        thin_assignment, thin_candidate = self._assignment(
            scope="demo:thin:feed-v1",
            candidate_id="momentum.stop.atr_1_5", observations=10)
        self._seed(thin_assignment, 10, "worked")
        self._complete(thin_assignment, 10)
        thin = self.store.experiment_outcome(thin_assignment["assignment_id"])

        self.assertEqual(failed["verdict"], "FAILED")
        self.assertIn(
            "NONPOSITIVE_AFTER_COST_EXPECTANCY",
            {item["code"] for item in failed["payload"]["reasons"]})
        self.assertEqual(thin["verdict"], "INCONCLUSIVE")
        self.assertIn(
            "CLOSED_SAMPLE_BELOW_FLOOR",
            {item["code"] for item in thin["payload"]["reasons"]})
        self.assertEqual(self.store.edge_evidence(), [])
        failed_context = self.store.research_history_context(
            failed_assignment["scope_key"])
        self.assertEqual(
            failed_context["failed_exact_settings"][0]["variant_id"],
            failed_candidate.variant_id)
        self.assertEqual(
            json.loads(self.store.findings_for(failed_candidate.variant_id)[-1]
                       ["text"])["verdict"], "FAILED")
        self.assertEqual(
            json.loads(self.store.findings_for(thin_candidate.variant_id)[-1]
                       ["text"])["verdict"], "INCONCLUSIVE")

    def test_forward_segment_inconsistency_refuses_edge(self):
        assignment, _ = self._assignment(scope="demo:inconsistent:feed-v1")
        self._seed(assignment, 100, "inconsistent")

        self._complete(assignment, 100)
        outcome = self.store.experiment_outcome(assignment["assignment_id"])

        self.assertEqual(outcome["verdict"], "FAILED")
        self.assertIn(
            "SEGMENT_INCONSISTENT",
            {item["code"] for item in outcome["payload"]["reasons"]})
        self.assertEqual(self.store.edge_evidence(assignment["scope_key"]), [])

    def test_rejected_assignment_is_voided_not_failed(self):
        """An operational abort produced no evidence, so it decides nothing.

        Every reason an assignment can be rejected is operational: the
        variant went missing after a restart, code/config identity changed
        mid-window, or the paper portfolio was revoked. Recording those as
        FAILED wrote a permanent, immutable verdict about a strategy that was
        never measured - and because a duplicate-key IntegrityError revokes
        the portfolio after three occurrences, a SQLite constraint violation
        could author a research finding.
        """
        assignment, _ = self._assignment(
            scope="demo:rejected:feed-v1", observations=1)

        self.store.reject_experiment_assignment(
            assignment["assignment_id"], "identity changed", now=START + 1)
        outcome = self.store.experiment_outcome(assignment["assignment_id"])

        self.assertEqual(outcome["terminal_status"], "REJECTED")
        self.assertEqual(outcome["verdict"], "INCONCLUSIVE")
        self.assertEqual(
            [item["code"] for item in outcome["payload"]["reasons"]],
            ["ASSIGNMENT_VOIDED"])

    def test_restart_and_retry_are_idempotent_and_immutable(self):
        assignment, candidate = self._assignment(scope="demo:idempotent:feed-v1")
        self._seed(assignment, 100, "worked")
        self._complete(assignment, 100)

        self._complete(assignment, 100)
        summary = self.store.ensure_terminal_experiment_outcomes()
        reopened = findings.FindingsStore(self.path)
        outcome = reopened.experiment_outcome(assignment["assignment_id"])

        self.assertEqual(summary["created"], [])
        self.assertEqual(len(reopened.experiment_outcomes()), 1)
        self.assertEqual(len(reopened.edge_evidence()), 1)
        outcome_findings = [
            row for row in reopened.findings_for(candidate.variant_id)
            if json.loads(row["text"]).get("type") == "experiment_outcome"]
        self.assertEqual(len(outcome_findings), 1)
        with sqlite3.connect(self.path) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE experiment_outcomes SET verdict='FAILED' "
                    "WHERE outcome_id=?", (outcome["outcome_id"],))


class ResearchReviewTests(ResearchLearningFixture):
    def _pending_outcome(self, scope="demo:review:feed-v1"):
        assignment, _ = self._assignment(scope=scope, observations=10)
        self._seed(assignment, 10, "worked")
        self._complete(assignment, 10)
        return self.store.experiment_outcome(assignment["assignment_id"])

    @staticmethod
    def _success_response(next_selection=True):
        payload = {
            "explanation": (
                "The deterministic result is inconclusive because the closed "
                "sample and paired windows remain below the stored gates."),
            "limitations": [
                "Paper costs are modeled rather than exchange fills.",
                "The result cannot authorize a live strategy change.",
            ],
            "next_selection": None,
        }
        if next_selection:
            payload["next_selection"] = {
                "strategy_id": "momentum",
                "variant_id": "momentum.stop.atr_1_5",
                "reasoning": (
                    "Test the registered stop-width setting after the current "
                    "exact variant finished without sufficient evidence."),
            }
        return json.dumps(payload)

    @staticmethod
    def _hypothesis_draft():
        return {
            "title": "Volume-confirmed momentum continuation",
            "strategy_id": "momentum",
            "mechanism": (
                "High relative volume may distinguish persistent demand from "
                "a low-liquidity price move that quickly mean reverts."),
            "payer": "Late traders crossing the spread after the impulse",
            "falsifier": (
                "Net paired expectancy is no better when the recorded volume "
                "predicate holds than when it does not."),
            "predicate": {
                "field": "relative_volume_1h",
                "operator": "gte",
                "value": 1.5,
                "direction": "both",
            },
            "horizon_hours": 12,
            "cost_treatment": "taker_round_trip",
            "evidence_needed": (
                "At least 100 paired forward observations with costs and "
                "separate fit and confirmation windows."),
        }

    def test_review_failure_is_nonfatal_pending_and_retries_to_success(self):
        outcome = self._pending_outcome()
        cfg = valid_config()
        failed_reviewer = FakeReviewer(error=RuntimeError("provider down"))

        failed = review.process_pending_review(
            self.store, cfg, reviewer=failed_reviewer, now=START + 999)

        self.assertEqual(failed["status"], "RETRY_PENDING")
        self.assertIsNone(self.store.experiment_review(outcome["outcome_id"]))
        self.assertEqual(
            len(self.store.experiment_review_attempts(outcome["outcome_id"])), 1)

        successful_reviewer = FakeReviewer(self._success_response())
        succeeded = review.process_pending_review(
            self.store, cfg, reviewer=successful_reviewer, now=START + 1000)
        stored = self.store.experiment_review(outcome["outcome_id"])

        self.assertEqual(succeeded["status"], "REVIEWED")
        self.assertIsNotNone(stored)
        self.assertEqual(
            len(self.store.experiment_review_attempts(outcome["outcome_id"])), 2)
        self.assertEqual(stored["next_selection"]["validation_status"],
                         "ACCEPTED")
        self.assertIsNone(succeeded["hypothesis_draft"])
        self.assertIsNone(
            self.store.experiment_review_attempts(outcome["outcome_id"])[-1]
            ["response"]["hypothesis_draft"])
        selection = self.store.research_selection(stored["selection_id"])
        self.assertEqual(selection["current_status"], "ACCEPTED")
        self.assertEqual(selection["model_id"], "test-review-model")

    def test_invalid_or_verdict_overriding_response_is_retried(self):
        outcome = self._pending_outcome(scope="demo:bounded:feed-v1")
        raw = json.dumps({
            "verdict": "WORKED",
            "explanation": "The model tries to override the fixed verdict.",
            "limitations": [], "next_selection": None,
        })

        result = review.process_pending_review(
            self.store, valid_config(), reviewer=FakeReviewer(raw),
            now=START + 1001)

        self.assertEqual(result["status"], "RETRY_PENDING")
        self.assertEqual(
            self.store.experiment_outcome(
                outcome_id=outcome["outcome_id"])["verdict"],
            "INCONCLUSIVE")
        self.assertIn(
            "forbidden field", self.store.experiment_review_attempts(
                outcome["outcome_id"])[0]["parse_error"])

    def test_omitted_and_null_hypothesis_drafts_are_backward_compatible(self):
        omitted = json.loads(self._success_response(next_selection=False))
        explicit_null = {**omitted, "hypothesis_draft": None}

        for payload in (omitted, explicit_null):
            with self.subTest(hypothesis_draft=payload.get(
                    "hypothesis_draft", "omitted")):
                parsed = review.parse_review_response(json.dumps(payload))
                self.assertIsNone(parsed["hypothesis_draft"])

    def test_hypothesis_draft_is_normalized_persisted_and_not_registered(self):
        outcome = self._pending_outcome(scope="demo:draft:feed-v1")
        cfg = valid_config()
        cfg_before = deepcopy(cfg)
        variants_before = self.store.variants()
        baseline_before = self.store.paper_portfolio_state(
            outcome["scope_key"], outcome["baseline_variant_id"])
        candidate_before = self.store.paper_portfolio_state(
            outcome["scope_key"], outcome["candidate_variant_id"])
        draft = self._hypothesis_draft()
        draft["title"] = f"  {draft['title']}  "
        draft["predicate"]["value"] = 2
        draft["horizon_hours"] = 24
        payload = json.loads(self._success_response(next_selection=False))
        payload["hypothesis_draft"] = draft

        result = review.process_pending_review(
            self.store, cfg, reviewer=FakeReviewer(json.dumps(payload)),
            now=START + 1001.5)
        persisted = self.store.experiment_review_attempts(
            outcome["outcome_id"])[-1]["response"]["hypothesis_draft"]

        self.assertEqual(result["status"], "REVIEWED")
        self.assertEqual(result["hypothesis_draft"], persisted)
        self.assertEqual(
            persisted["title"], "Volume-confirmed momentum continuation")
        self.assertEqual(persisted["predicate"]["value"], 2.0)
        self.assertEqual(persisted["horizon_hours"], 24.0)
        self.assertIsNone(result["review"]["selection_id"])
        self.assertEqual(self.store.variants(), variants_before)
        self.assertEqual(cfg, cfg_before)
        self.assertEqual(
            self.store.paper_portfolio_state(
                outcome["scope_key"], outcome["baseline_variant_id"]),
            baseline_before)
        self.assertEqual(
            self.store.paper_portfolio_state(
                outcome["scope_key"], outcome["candidate_variant_id"]),
            candidate_before)
        self.assertEqual(
            self.store.research_history_context(outcome["scope_key"])
            ["pending_selections"], [])

    def test_hypothesis_draft_contract_rejects_every_unbounded_form(self):
        base = json.loads(self._success_response(next_selection=False))

        def response_with(mutator):
            draft = deepcopy(self._hypothesis_draft())
            mutator(draft)
            return json.dumps({**base, "hypothesis_draft": draft})

        cases = {
            "extra draft key": (
                lambda item: item.update({"sql": "SELECT 1"}),
                "exactly the declared fields"),
            "missing draft key": (
                lambda item: item.pop("payer"),
                "exactly the declared fields"),
            "extra predicate key": (
                lambda item: item["predicate"].update({"window": 4}),
                "predicate must have exactly"),
            "missing predicate key": (
                lambda item: item["predicate"].pop("direction"),
                "predicate must have exactly"),
            "boolean predicate value": (
                lambda item: item["predicate"].update({"value": True}),
                "predicate value must be a number"),
            "boolean horizon": (
                lambda item: item.update({"horizon_hours": False}),
                "horizon_hours must be a number"),
            "nan": (
                lambda item: item["predicate"].update({"value": float("nan")}),
                "predicate value must be finite"),
            "infinity": (
                lambda item: item.update({"horizon_hours": float("inf")}),
                "horizon_hours must be finite"),
            "overflowing integer": (
                lambda item: item["predicate"].update({"value": 10 ** 400}),
                "predicate value must be finite"),
            "unregistered strategy": (
                lambda item: item.update({"strategy_id": "not-registered"}),
                "strategy_id is not registered"),
            "unknown field": (
                lambda item: item["predicate"].update({"field": "price"}),
                "predicate field is not allowlisted"),
            "unknown operator": (
                lambda item: item["predicate"].update({"operator": "eq"}),
                "predicate operator is not allowlisted"),
            "unknown direction": (
                lambda item: item["predicate"].update({"direction": "buy"}),
                "predicate direction is not allowlisted"),
            "out-of-bounds value": (
                lambda item: item["predicate"].update({"value": -0.1}),
                "outside the declared bounds"),
            "out-of-bounds horizon": (
                lambda item: item.update({"horizon_hours": 337}),
                "horizon_hours must be 1 to 336"),
            "unknown cost": (
                lambda item: item.update({"cost_treatment": "ignore_costs"}),
                "cost_treatment is not allowlisted"),
            "short title": (
                lambda item: item.update({"title": "no"}),
                "title must be 5 to 120"),
            "short mechanism": (
                lambda item: item.update({"mechanism": "too short"}),
                "mechanism must be 20 to 1000"),
            "short payer": (
                lambda item: item.update({"payer": "x"}),
                "payer must be 5 to 500"),
            "short falsifier": (
                lambda item: item.update({"falsifier": "too short"}),
                "falsifier must be 20 to 1000"),
            "short evidence": (
                lambda item: item.update({"evidence_needed": "too short"}),
                "evidence_needed must be 20 to 1000"),
        }
        for name, (mutator, message) in cases.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, message):
                    review.parse_review_response(response_with(mutator))

        for forbidden in ("verdict", "execution_instruction"):
            with self.subTest(forbidden=forbidden):
                payload = {**base, forbidden: "unauthorized"}
                with self.assertRaisesRegex(ValueError, "forbidden field"):
                    review.parse_review_response(json.dumps(payload))

        with self.assertRaisesRegex(ValueError, "must be an object or null"):
            review.parse_review_response(json.dumps({
                **base, "hypothesis_draft": ["not", "declarative"],
            }))

    def test_prompt_and_request_expose_non_executable_draft_contract(self):
        outcome = self._pending_outcome(scope="demo:draft-contract:feed-v1")

        request = review.build_review_request(self.store, outcome)
        contract = request["hypothesis_draft_contract"]

        self.assertIn("NON-EXECUTABLE", review.RESEARCH_REVIEW_SYSTEM)
        self.assertIn("manually reviewed and registered",
                      review.RESEARCH_REVIEW_SYSTEM)
        self.assertIn("no live, demo, order", review.RESEARCH_REVIEW_SYSTEM)
        self.assertEqual(contract["status"], "NON_EXECUTABLE_DRAFT")
        self.assertEqual(
            contract["evidence_source"],
            "immutable_llm_input_snapshot_corpus")
        self.assertIn("immutable llm_input corpus",
                      review.RESEARCH_REVIEW_SYSTEM)
        self.assertIn("terminal outcome aggregates",
                      review.RESEARCH_REVIEW_SYSTEM)
        self.assertTrue(contract["manual_registration_required"])
        self.assertFalse(contract["creates_variant_or_selection"])
        self.assertFalse(contract["execution_authority"])
        self.assertEqual(
            contract["predicate"]["field_bounds"],
            review.HYPOTHESIS_PREDICATE_FIELD_BOUNDS)
        self.assertEqual(
            contract["predicate"]["operators"],
            list(review.HYPOTHESIS_PREDICATE_OPERATORS))
        self.assertEqual(
            contract["cost_treatments"],
            list(review.HYPOTHESIS_COST_TREATMENTS))

    def test_one_pending_outcome_per_invocation(self):
        first = self._pending_outcome(scope="demo:one:feed-v1")
        second = self._pending_outcome(scope="demo:two:feed-v1")
        reviewer = FakeReviewer(self._success_response(next_selection=False))

        result = review.process_pending_review(
            self.store, valid_config(), reviewer=reviewer, now=START + 1002)

        self.assertEqual(result["processed"], 1)
        self.assertEqual(reviewer.calls, 1)
        reviews = [self.store.experiment_review(item["outcome_id"])
                   for item in (first, second)]
        self.assertEqual(sum(item is not None for item in reviews), 1)
        self.assertIsNotNone(self.store.pending_experiment_outcome())

    def test_history_context_and_review_do_not_change_portfolio_or_strategy(self):
        outcome = self._pending_outcome(scope="demo:safety:feed-v1")
        cfg = valid_config()
        cfg_before = deepcopy(cfg)
        baseline_before = self.store.paper_portfolio_state(
            outcome["scope_key"], outcome["baseline_variant_id"])
        candidate_before = self.store.paper_portfolio_state(
            outcome["scope_key"], outcome["candidate_variant_id"])

        review.process_pending_review(
            self.store, cfg,
            reviewer=FakeReviewer(self._success_response()),
            now=START + 1003)
        context = self.store.research_history_context(outcome["scope_key"])

        self.assertEqual(cfg, cfg_before)
        self.assertEqual(
            self.store.paper_portfolio_state(
                outcome["scope_key"], outcome["baseline_variant_id"]),
            baseline_before)
        self.assertEqual(
            self.store.paper_portfolio_state(
                outcome["scope_key"], outcome["candidate_variant_id"]),
            candidate_before)
        self.assertEqual(
            context["completed_outcomes"][0]["outcome_id"],
            outcome["outcome_id"])
        self.assertNotIn(outcome["candidate_variant_id"],
                         context["terminal_variant_ids"])
        self.assertEqual(
            context["retryable_inconclusive_assignments"][0]
            ["assignment_id"], outcome["assignment_id"])
        self.assertEqual(len(context["pending_selections"]), 1)
        self.assertEqual(cfg["strategy"]["id"], "momentum")


if __name__ == "__main__":
    unittest.main()
