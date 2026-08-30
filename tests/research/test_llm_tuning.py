"""Tuned parameters, the reasons given for them, and grading those reasons.

Before this existed the model could choose *which* hypothesis a slot held but
never *what numbers to try inside it*: parameter search was a fixed table of
three hand-written responses per diagnosed failure mode with an arithmetic
sweep behind it.  These tests pin the three properties that make the addition
safe rather than merely capable — a tuned variant is bounded by exactly the
same grammar and gates as a mutated one, every proposal states a reason before
the gate that will judge it exists, and that reason is graded afterwards and
handed back to the next proposal.
"""

from contextlib import closing, contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.contracts.rule import (rule_variant_id, validate_rule_spec)
from research.edge_lab import EdgeLedger
from research.factory_core import (
    MAX_VARIANTS, _REASON_LIMIT, coordinate_mutation_pool, family_template,
    initial_hypotheses, mutate_from_diagnosis, mutate_with_reasons,
    interaction_mutation_pool, mutation_reason, spec_delta,
)
from research.factory_ledger import FactoryError, FactoryLedger
from research.llm_strategy import (
    MAX_REASON_CHARS, MAX_TUNED_VARIANTS, PROPOSAL_SCHEMA, TUNING_SCHEMA,
    ProposalResult,
    RuleProposalAdapter,
)
import research.gates as gates
import research.strategy_factory as factory_module
from research.strategy_factory import (_interaction_lessons, _lesson_brief,
                                        _llm_replacement, _sanitize_fit_selection,
                                        _tuned_variants,
                                        run_factory)

from .test_strategy_factory import losing_breakouts


@contextmanager
def _compact_factory_protocol():
    """Keep compact LLM-cycle fixtures below production evidence floors."""
    with patch.multiple(
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
        yield


ROOT = validate_rule_spec(family_template("opening_range_breakout"))
DIAGNOSIS = {"primary_failure": "negative_expectancy", "trades": 40,
             "win_rate": .3, "net_pnl": -900.0}


def _reply(*specs_and_reasons):
    return json.dumps({"schema": TUNING_SCHEMA, "variants": [
        {"rule_spec": spec, "reason": reason}
        for spec, reason in specs_and_reasons]})


def _tuned(spec_changes, reason="Raised the entry threshold to drop the "
                                "marginal signals the diagnosis blamed."):
    return ({**ROOT, **spec_changes}, reason)


def _adapter(reply, **kwargs):
    return RuleProposalAdapter(model="test", caller=lambda *_a, **_k: reply,
                               max_attempts=1, **kwargs)


def _ledgers(directory):
    path = Path(directory) / "edge_lab.sqlite3"
    return FactoryLedger(path), EdgeLedger(path)


class TuningContractTests(unittest.TestCase):
    """The output boundary, which is the only thing standing between a model
    and the evaluator."""

    def test_a_valid_reply_normalizes_every_spec_and_keeps_its_reason(self):
        adapter = _adapter(_reply(_tuned({"threshold_bps": 6.0}),
                                  _tuned({"threshold_bps": 4.0}, "Lowered the "
                                         "threshold within the local neighborhood.")))
        result = adapter.tune("equity", 0, ROOT, DIAGNOSIS, count=2)
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.schema, TUNING_SCHEMA)
        self.assertEqual(len(result.variants), 2)
        for entry in result.variants:
            self.assertEqual(entry["variant_id"],
                             rule_variant_id(entry["rule_spec"]))
            self.assertTrue(entry["reason"])
        self.assertEqual(result.evidence["kind"], "tuning")
        self.assertEqual(result.evidence["family"], ROOT["family"])
        # Evidence is hashed, never the raw text of the exchange.
        for key in ("request_hash", "raw_response_hash", "system_prompt_hash"):
            self.assertRegex(result.evidence[key], r"^[0-9a-f]{64}$")

    def test_fit_execution_rejection_counts_reach_tuning_but_rows_do_not(self):
        seen = {}

        def caller(*, system_prompt, request, timeout):
            seen.update(request)
            return _reply(_tuned({"threshold_bps": 6.0}))

        adapter = RuleProposalAdapter(
            model="test", caller=caller, max_attempts=1)
        diagnosis = {
            **DIAGNOSIS,
            "fit_diagnostics": {
                "execution_rejections": {
                    "rows": 126,
                    "executed_rows": 0,
                    "no_trade_rows": 126,
                    "execution_blocked": True,
                },
            },
        }
        result = adapter.tune("equity", 0, ROOT, diagnosis, count=1)
        self.assertTrue(result.success, result.error)
        self.assertEqual(
            seen["diagnosis"]["fit_diagnostics"]["execution_rejections"]
                ["rows"], 126)

        unsafe = {
            **diagnosis,
            "fit_diagnostics": {
                "execution_rejections": {
                    "rows": [{"close": 101.0}],
                },
            },
        }
        refused = adapter.tune("equity", 0, ROOT, unsafe, count=1)
        self.assertFalse(refused.success)
        self.assertIn("non-negative integer count", refused.error or "")

    def test_tuning_may_not_change_the_family(self):
        """Changing the idea is discovery's job; tuning changes its numbers."""
        adapter = _adapter(_reply(({**ROOT, "family": "mean_reversion"},
                                   "Switched to a different idea entirely.")))
        result = adapter.tune("equity", 0, ROOT, DIAGNOSIS, count=1)
        self.assertFalse(result.success)
        self.assertIn("family", result.error)

    def test_tuning_rejects_categorical_confirmation_changes(self):
        changed = {**ROOT, "confirmation": "trend"}
        adapter = _adapter(_reply((
            changed, "Changed confirmation after the diagnosed trend failure.")))
        result = adapter.tune("equity", 0, ROOT, DIAGNOSIS, count=1)
        self.assertFalse(result.success)
        self.assertIn("numeric values only", result.error)

    def test_coordinate_tuning_rejects_a_bundled_change(self):
        bundled = {**ROOT, "threshold_bps": 40.0, "target_r": 1.5}
        adapter = _adapter(_reply((
            bundled, "Raised threshold and lowered target R together.")))
        result = adapter.tune(
            "equity", 0, ROOT,
            {**DIAGNOSIS, "refinement_phase": "coordinate"}, count=1)
        self.assertFalse(result.success)
        self.assertIn("exactly 1 field", result.error)

    def test_coordinate_tuning_rejects_an_unbounded_numeric_jump(self):
        jumped = {**ROOT, "threshold_bps": 40.0}
        adapter = _adapter(_reply((
            jumped, "Raised threshold_bps after the negative expectancy diagnosis.")))
        result = adapter.tune(
            "equity", 0, ROOT,
            {**DIAGNOSIS, "refinement_phase": "coordinate"}, count=1)
        self.assertFalse(result.success)
        self.assertIn("bounded local step", result.error)

    def test_interaction_tuning_accepts_exactly_two_named_fields(self):
        interaction = {**ROOT, "threshold_bps": 6.0, "target_r": 1.6}
        adapter = _adapter(_reply((
            interaction,
            "Combined the measured threshold and target R coordinate gains.")))
        result = adapter.tune(
            "equity", 0, ROOT,
            {**DIAGNOSIS, "refinement_phase": "interaction"}, count=1)
        self.assertTrue(result.success, result.error)

    def test_tuning_may_not_widen_the_grammar(self):
        """v2 unlocks whole predicate categories the root never expressed.

        Reaching them is inventing structure, not tuning values, so tuning is
        pinned to the root's own grammar version.
        """
        # Keep this regression anchored to a v1 root even when the shared
        # family template defaults evolve to v2.
        root = validate_rule_spec({
            key: value for key, value in ROOT.items()
            if key not in {"confirmations", "entry_after_minutes",
                           "entry_before_minutes", "min_atr_bps",
                           "max_atr_bps"}
        } | {"schema": "rule-strategy.v1"})
        wider = {**root, "schema": "rule-strategy.v2",
                 "confirmations": ["trend"], "entry_after_minutes": 45}
        adapter = _adapter(_reply((wider, "Added a time window and a filter.")))
        result = adapter.tune("equity", 0, root, DIAGNOSIS, count=1)
        self.assertFalse(result.success)
        self.assertIn("may not widen the grammar", result.error)

    def test_a_v2_root_is_tuned_in_v2_and_still_cannot_change_family(self):
        """Pinning is to the root's own version, not to v1 forever."""
        root = validate_rule_spec({**ROOT, "schema": "rule-strategy.v2",
                                   "entry_after_minutes": 30})
        adapter = _adapter(_reply(({**root, "entry_after_minutes": 36},
                                   "Pushed the entry window later.")))
        result = adapter.tune("equity", 0, root, DIAGNOSIS, count=1)
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.variants[0]["rule_spec"]["schema"],
                         "rule-strategy.v2")
        self.assertEqual(result.variants[0]["rule_spec"]["family"],
                         root["family"])

    def test_the_model_cannot_invent_a_signal(self):
        """The signal primitives are fixed code, not a namespace to extend."""
        cases = {
            # A family that does not exist.
            "unsupported rule family": {**ROOT, "family": "order_book_imbalance"},
            # A confirmation filter that does not exist.
            "confirmation must be": {**ROOT, "confirmation": "rsi_divergence"},
            # A brand-new indicator field.
            "unknown field(s)": {**ROOT, "rsi_period": 14},
            # A new data source.
            "unknown field(s)": {**ROOT, "sentiment_feed": "twitter"},
        }
        for expected, spec in cases.items():
            with self.subTest(spec=sorted(set(spec) - set(ROOT)) or spec["family"]):
                result = _adapter(_reply((spec, "A new idea I had."))).tune(
                    "equity", 0, ROOT, DIAGNOSIS, count=1)
                self.assertFalse(result.success)
                self.assertIn(expected, result.error)

    def test_discovery_cannot_invent_a_signal_either(self):
        """Discovery picks from the fixed catalog; it does not extend it."""
        for spec in ({"family": "order_book_imbalance"},
                     {"family": "momentum_continuation", "rsi_period": 14},
                     {"family": "momentum_continuation",
                      "confirmation": "news_sentiment"}):
            with self.subTest(spec=spec):
                reply = json.dumps({"schema": "llm-edge-discovery.v1",
                                    "rule_spec": spec,
                                    "thesis": "A brand new kind of signal."})
                result = _adapter(reply).discover("equity", 0, {})
                self.assertFalse(result.success)

    def test_a_reason_is_required_bounded_and_plain(self):
        for reason, expected in ((None, "must be a string"),
                                 ("", "must not be empty"),
                                 ("x" * (MAX_REASON_CHARS + 1), "exceeds"),
                                 ("```py```", "markdown")):
            with self.subTest(reason=reason):
                adapter = _adapter(_reply(({**ROOT, "threshold_bps": 40.0},
                                           reason)))
                result = adapter.tune("equity", 0, ROOT, DIAGNOSIS, count=1)
                self.assertFalse(result.success)
                self.assertIn(expected, result.error)

    def test_source_and_credential_keys_are_refused(self):
        for spec in ({**ROOT, "code": "import os"},
                     {**ROOT, "api_key": "sk-live"},
                     {**ROOT, "market_rows": [1, 2]}):
            with self.subTest(spec=sorted(set(spec) - set(ROOT))):
                adapter = _adapter(_reply((spec, "A perfectly ordinary reason.")))
                result = adapter.tune("equity", 0, ROOT, DIAGNOSIS, count=1)
                self.assertFalse(result.success)
                self.assertIn("not permitted", result.error)

    def test_fences_unknown_fields_and_oversize_replies_are_refused(self):
        cases = {
            "markdown": "```json\n{}\n```",
            "unknown field(s)": json.dumps(
                {"schema": TUNING_SCHEMA, "variants": [], "notes": "hi"}),
            "not strict JSON": "here you go: {}",
            "schema must be 'llm-variant-tuning.v1'": json.dumps(
                {"schema": "llm-edge-discovery.v1", "variants": []}),
        }
        for expected, reply in cases.items():
            with self.subTest(expected=expected):
                result = _adapter(reply).tune("equity", 0, ROOT, DIAGNOSIS, count=1)
                self.assertFalse(result.success)
                self.assertIn(expected, result.error)

    def test_a_duplicate_spec_is_dropped_rather_than_failing_the_reply(self):
        adapter = _adapter(_reply(_tuned({"threshold_bps": 6.0}),
                                  _tuned({"threshold_bps": 6.0}),
                                  _tuned({"threshold_bps": 4.0})))
        result = adapter.tune("equity", 0, ROOT, DIAGNOSIS, count=3)
        self.assertTrue(result.success, result.error)
        self.assertEqual(len(result.variants), 2)

    def test_bounds_are_validated_before_any_provider_call(self):
        def explode(*_a, **_k):
            raise AssertionError("the provider must not be reached")

        adapter = RuleProposalAdapter(model="test", caller=explode, max_attempts=1)
        for kwargs in ({"vehicle": "crypto"}, {"slot": -1},
                       {"count": 0}, {"count": MAX_TUNED_VARIANTS + 1}):
            with self.subTest(**kwargs):
                call = {"vehicle": "equity", "slot": 0, "count": 2, **kwargs}
                result = adapter.tune(call["vehicle"], call["slot"], ROOT,
                                      DIAGNOSIS, count=call["count"])
                self.assertFalse(result.success)

    def test_the_lesson_brief_is_bounded(self):
        huge = [{"reason": "x" * 500, "tried": {"threshold_bps": i}}
                for i in range(100)]
        result = _adapter(_reply(_tuned({"threshold_bps": 40.0}))).tune(
            "equity", 0, ROOT, DIAGNOSIS, count=1, lessons=huge)
        self.assertFalse(result.success)
        self.assertIn("8192-byte", result.error)

    def test_missing_provider_deployment_stops_retries_and_is_recorded(self):
        class DeploymentMissing(RuntimeError):
            status_code = 404

        calls = []

        def missing(*_args, **_kwargs):
            calls.append(1)
            raise DeploymentMissing("DeploymentNotFound: gpt-missing")

        adapter = RuleProposalAdapter(model="gpt-missing", caller=missing,
                                      max_attempts=3)
        result = adapter.tune("equity", 0, ROOT, DIAGNOSIS, count=1)

        self.assertFalse(result.success)
        self.assertEqual(len(calls), 1)
        self.assertTrue(result.evidence["provider_circuit_open"])
        self.assertIn("DeploymentNotFound", result.error)

    def test_preflight_success_is_one_non_authorizing_call(self):
        calls = []

        def probe(*_args, **_kwargs):
            calls.append(1)
            return object()

        result = RuleProposalAdapter(model="gpt-test", caller=probe).preflight()
        self.assertEqual(result.status, "ready")
        self.assertTrue(result.ok)
        self.assertEqual(len(calls), 1)
        self.assertFalse(result.evidence["response_parsed"])
        self.assertEqual(result.evidence["attempts"], 1)

    def test_preflight_uses_a_tiny_dedicated_provider_schema(self):
        calls = []

        class Responses:
            @staticmethod
            def create(**kwargs):
                calls.append(kwargs)
                return object()

        client = type("Client", (), {"responses": Responses()})()
        result = RuleProposalAdapter(
            provider="openai", model="gpt-test", client=client).preflight()
        self.assertEqual(result.status, "ready")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["max_output_tokens"], 32)
        output_format = calls[0]["text"]["format"]
        self.assertEqual(output_format["name"], "llm_provider_preflight")
        self.assertEqual(output_format["schema"]["required"], ["status"])
        self.assertNotIn("rule_spec", json.dumps(output_format["schema"]))

    def test_preflight_fatal_deployment_is_one_call_and_safe(self):
        class DeploymentMissing(RuntimeError):
            status_code = 404

        calls = []

        def probe(*_args, **_kwargs):
            calls.append(1)
            raise DeploymentMissing(
                "Incorrect API key provided: sk-SECRET; DeploymentNotFound: gpt-missing")

        result = RuleProposalAdapter(model="gpt-missing", caller=probe).preflight()
        self.assertEqual(result.status, "fatal")
        self.assertEqual(len(calls), 1)
        self.assertTrue(result.evidence["provider_circuit_open"])
        self.assertNotIn("SECRET", result.reason)
        self.assertNotIn("sk-SECRET", result.evidence["error"])

    def test_preflight_redacts_dict_shaped_credentials(self):
        def probe(*_args, **_kwargs):
            raise RuntimeError(
                "{'api_key': 'sk-live-secret', "
                "'authorization': 'Basic VERYSECRET', "
                "'url': 'https://host/v1?sig=SIGNEDSECRET'}")

        result = RuleProposalAdapter(model="gpt-test", caller=probe).preflight()
        self.assertNotIn("sk-live-secret", result.reason)
        self.assertNotIn("sk-live-secret", result.evidence["error"])
        self.assertNotIn("VERYSECRET", result.reason)
        self.assertNotIn("SIGNEDSECRET", result.reason)

    def test_preflight_network_failure_is_degraded(self):
        def probe(*_args, **_kwargs):
            raise ConnectionError("network unavailable")

        result = RuleProposalAdapter(model="gpt-test", caller=probe).preflight()
        self.assertEqual(result.status, "degraded")
        self.assertFalse(result.ok)


class CitedLearningTests(unittest.TestCase):
    """A proposal has to say what it learned from, or it is a guess."""

    LESSONS = [
        {"id": "a1b2c3d4e5f6", "reason": "Raising the threshold cut every "
                                         "signal.", "verdict": "failed",
         "tried": {"threshold_bps": {"from": 5.0, "to": 90.0}}},
        {"id": "0f1e2d3c4b5a", "reason": "Shorter holds helped the payoff.",
         "verdict": "passed", "tried": {"max_hold_bars": {"from": 90, "to": 45}}},
    ]

    def _reply_with(self, builds_on, threshold=6.0):
        return json.dumps({"schema": TUNING_SCHEMA, "variants": [{
            "rule_spec": {**ROOT, "threshold_bps": threshold},
            "reason": "Backing off the threshold the cited lesson overshot.",
            "builds_on": builds_on}]})

    def test_a_cited_lesson_is_accepted_and_carried(self):
        result = _adapter(self._reply_with("a1b2c3d4e5f6")).tune(
            "equity", 0, ROOT, DIAGNOSIS, count=1, lessons=self.LESSONS)
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.variants[0]["builds_on"], "a1b2c3d4e5f6")
        self.assertEqual(result.evidence["lessons_supplied"], 2)
        self.assertEqual(result.evidence["lessons_cited"], ["a1b2c3d4e5f6"])

    def test_an_uncited_proposal_is_refused_when_lessons_exist(self):
        """This is the whole point: no proposing into the void."""
        reply = json.dumps({"schema": TUNING_SCHEMA, "variants": [
            {"rule_spec": {**ROOT, "threshold_bps": 40.0},
             "reason": "Felt right."}]})
        result = _adapter(reply).tune("equity", 0, ROOT, DIAGNOSIS, count=1,
                                      lessons=self.LESSONS)
        self.assertFalse(result.success)
        self.assertIn("must cite one of the 2 lesson id(s)", result.error)

    def test_a_fabricated_citation_is_refused(self):
        for bogus in ("ffffffffffff", "not-a-lesson", ""):
            with self.subTest(bogus=bogus):
                result = _adapter(self._reply_with(bogus)).tune(
                    "equity", 0, ROOT, DIAGNOSIS, count=1, lessons=self.LESSONS)
                self.assertFalse(result.success)

    def test_a_citation_without_any_lesson_supplied_is_refused(self):
        result = _adapter(self._reply_with("a1b2c3d4e5f6")).tune(
            "equity", 0, ROOT, DIAGNOSIS, count=1)
        self.assertFalse(result.success)
        self.assertIn("none were supplied", result.error)

    def test_the_first_cycle_may_propose_without_a_citation(self):
        reply = json.dumps({"schema": TUNING_SCHEMA, "variants": [
            {"rule_spec": {**ROOT, "threshold_bps": 6.0},
             "reason": "Nothing has been tried yet; widen threshold."}]})
        result = _adapter(reply).tune("equity", 0, ROOT, DIAGNOSIS, count=1)
        self.assertTrue(result.success, result.error)
        self.assertIsNone(result.variants[0]["builds_on"])

    def test_a_repeat_of_a_graded_failure_is_dropped(self):
        """The ledger already answered that experiment."""
        repeat = validate_rule_spec({**ROOT, "threshold_bps": 6.0})
        adapter = _adapter(self._reply_with("a1b2c3d4e5f6", threshold=6.0))
        chosen, proposal = _tuned_variants(
            {"rule_spec": ROOT, "slot": 0}, DIAGNOSIS, count=3,
            vehicle="equity", llm_enabled=True, config={"model": "test"},
            adapter=adapter, lessons=self.LESSONS,
            already_failed=frozenset({rule_variant_id(repeat)}))
        self.assertTrue(proposal.success)
        self.assertEqual(len(chosen), 3)
        self.assertNotIn(rule_variant_id(repeat),
                         {rule_variant_id(item.rule_spec) for item in chosen})
        self.assertEqual({item.source for item in chosen}, {"deterministic"})

    def test_the_citation_becomes_a_durable_link(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            hypothesis = initial_hypotheses(1)[0]
            factory.register(hypothesis)
            first = factory.record_lesson(
                hypothesis.hypothesis_id, vehicle="equity",
                family=hypothesis.family,
                variant_id=rule_variant_id(hypothesis.rule_spec),
                kind="tuning", source="deterministic",
                reason="The root, unchanged.")
            factory.grade_lesson(hypothesis.hypothesis_id,
                                 rule_variant_id(hypothesis.rule_spec),
                                 kind="tuning",
                                 outcome={"passed": False,
                                          "failed_checks": ["made money"]})
            brief = _lesson_brief(factory, vehicle="equity")
            self.assertEqual(brief[0]["id"], first[:12])

            # A proposal citing that brief entry resolves back to the row.
            self.assertEqual(factory.resolve_lesson_ref(brief[0]["id"]), first)
            self.assertIsNone(factory.resolve_lesson_ref("ffffffffffff"))

            child_spec = validate_rule_spec(
                {**hypothesis.rule_spec, "threshold_bps": 33.0})
            second = factory.record_lesson(
                hypothesis.hypothesis_id, vehicle="equity",
                family=hypothesis.family,
                variant_id=rule_variant_id(child_spec), kind="tuning",
                source="llm", reason="The cited attempt made no money; widen.",
                parent_lesson_id=first)
            chain = {row["lesson_id"]: row["parent_lesson_id"]
                     for row in factory.lessons(vehicle="equity")}
            self.assertEqual(chain[second], first)
            self.assertIsNone(chain[first])

    def test_novel_tuning_marker_survives_durable_lesson_history(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            hypothesis = initial_hypotheses(1)[0]
            factory.register(hypothesis)
            variant = rule_variant_id(
                validate_rule_spec({**hypothesis.rule_spec, "stop_atr": 1.2}))
            factory.record_lesson(
                hypothesis.hypothesis_id, vehicle="equity",
                family=hypothesis.family, variant_id=variant, kind="tuning",
                source="llm", reason="Raised stop_atr after a prior attempt.",
                evidence={"novel_tuning": True})
            factory.grade_lesson(
                hypothesis.hypothesis_id, variant, kind="tuning",
                outcome={"passed": False, "underpowered": False})
            brief = _lesson_brief(factory, vehicle="equity")
        self.assertTrue(brief[0]["novel_tuning"])

    def test_failed_variant_ids_only_closes_explicit_powered_rejections(self):
        """Uncertainty is not an answer, so it is not a closed door."""
        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            hypothesis = initial_hypotheses(1)[0]
            factory.register(hypothesis)
            outcomes = {
                "failed": {"passed": False, "underpowered": False,
                           "classification": "adequate_negative_rejection"},
                "inconclusive": {"passed": False, "underpowered": False,
                                 "classification": "adequate_inconclusive"},
                "thin": {"passed": False, "underpowered": True},
                "won": {"passed": True, "underpowered": False},
            }
            ids = {}
            for index, (name, outcome) in enumerate(outcomes.items()):
                spec = validate_rule_spec(
                    {**hypothesis.rule_spec, "threshold_bps": 20.0 + index})
                ids[name] = rule_variant_id(spec)
                factory.record_lesson(
                    hypothesis.hypothesis_id, vehicle="equity",
                    family=hypothesis.family, variant_id=ids[name],
                    kind="tuning", source="llm", reason=f"The {name} attempt.")
                factory.grade_lesson(hypothesis.hypothesis_id, ids[name],
                                     kind="tuning", outcome=outcome)
            closed = factory.failed_variant_ids(vehicle="equity")
        self.assertEqual(closed, {ids["failed"]})


class VariantSelectionTests(unittest.TestCase):
    def test_malformed_success_is_recorded_as_rejected_llm_call(self):
        class Adapter:
            def propose(self, **_kwargs):
                return ProposalResult(
                    True, schema=PROPOSAL_SCHEMA,
                    rule_spec={"family": "not-a-rule-family"},
                    variant_id="rule.invalid")

        observations = []
        replacement, proposal, reason = _llm_replacement(
            {"vehicle": "equity", "slot": 0, "generation": 0,
             "hypothesis_id": "hypothesis", "rule_spec": ROOT},
            DIAGNOSIS, config={"model": "test"}, max_generations=3,
            not_before=None, existing_variant_ids=set(), adapter=Adapter(),
            llm_observations=observations)
        self.assertIsNone(replacement)
        self.assertFalse(proposal.success)
        self.assertEqual(reason, "llm_proposal_failed")
        self.assertEqual(len(observations), 1)
        self.assertFalse(observations[0]["success"])
        self.assertTrue(observations[0]["provider_success"])

    def test_interaction_lessons_ignore_injected_heldout_score(self):
        lessons = _interaction_lessons([
            {"fit_delta": "0.25", "heldout_delta": 999.0,
             "changed": {"lookback": {"from": 10, "to": 11}}},
            {"fit_delta": float("nan"), "heldout_delta": -999.0,
             "changed": {"threshold_bps": {"from": 5.0, "to": 6.0}}},
            {"heldout_delta": 123.0,
             "changed": {"target_r": {"from": 2.0, "to": 2.2}}},
        ])
        self.assertEqual(lessons[0]["heldout_delta"], .25)
        self.assertEqual(lessons[1]["heldout_delta"], 0.0)
        self.assertEqual(lessons[2]["heldout_delta"], 0.0)

    """What actually reaches an isolated simulated account."""

    def test_with_the_llm_off_selection_is_the_previous_mutation_exactly(self):
        chosen, proposal = _tuned_variants(
            {"rule_spec": ROOT, "slot": 0}, DIAGNOSIS, count=4,
            vehicle="equity", llm_enabled=False, config={}, adapter=None)
        self.assertIsNone(proposal)
        self.assertEqual([item.rule_spec for item in chosen],
                         mutate_from_diagnosis(ROOT, DIAGNOSIS, 4))
        self.assertEqual({item.source for item in chosen}, {"deterministic"})
        self.assertEqual({item.builds_on for item in chosen}, {None})

    def test_tuned_variants_are_adopted_and_topped_up_to_count(self):
        adapter = _adapter(_reply(_tuned({"threshold_bps": 6.0}),
                                  _tuned({"threshold_bps": 4.0})))
        chosen, proposal = _tuned_variants(
            {"rule_spec": ROOT, "slot": 0}, DIAGNOSIS, count=4,
            vehicle="equity", llm_enabled=True, config={"model": "test"},
            adapter=adapter)
        self.assertTrue(proposal.success)
        self.assertEqual(len(chosen), 4)
        origins = [item.source for item in chosen]
        # Variant zero stays the unmutated root: its own matched control is
        # itself, so it is the null calibration, not a candidate.
        self.assertEqual(chosen[0].rule_spec, ROOT)
        self.assertEqual(origins[0], "deterministic")
        # Both provider choices are exact members of the deterministic local
        # neighborhood; the deterministic ladder tops up the remaining arms.
        self.assertEqual(origins.count("llm"), 2)
        self.assertEqual(
            len({rule_variant_id(item.rule_spec) for item in chosen}), 4)

    def test_only_one_novel_numeric_value_is_accepted_per_cycle(self):
        first = _tuned({"stop_atr": 1.2},
                       "Raised stop_atr after the diagnosis showed marginal entries.")
        second = _tuned({"stop_atr": 1.1},
                        "Lowered stop_atr after the diagnosis showed weak entries.")
        chosen, proposal = _tuned_variants(
            {"rule_spec": ROOT, "slot": 0}, DIAGNOSIS, count=4,
            vehicle="equity", llm_enabled=True, config={"model": "test"},
            adapter=_adapter(_reply(first, second)))
        self.assertTrue(proposal.success, proposal.error)
        self.assertEqual(sum(item.novel_tuning for item in chosen), 1)
        self.assertEqual(sum(item.source == "llm" for item in chosen), 1)
        self.assertEqual(len(chosen), 4)

    def test_grid_reordering_does_not_spend_novel_tuning_cap(self):
        lessons = [{"id": f"{index + 1:012x}", "proposed_by": "llm",
                    "novel_tuning": False,
                    "tried": {"threshold_bps": {"from": 5.0, "to": 4.0}},
                    "verdict": "failed"}
                   for index in range(8)]
        reply = json.dumps({"schema": TUNING_SCHEMA, "variants": [
            {"rule_spec": _tuned({"threshold_bps": 4.0})[0],
             "reason": "Lowered threshold_bps after a prior failed attempt.",
             "builds_on": lessons[0]["id"]},
            {"rule_spec": _tuned({"threshold_bps": 6.0})[0],
             "reason": "Raised threshold_bps after a prior failed attempt.",
             "builds_on": lessons[0]["id"]},
        ]})
        chosen, proposal = _tuned_variants(
            {"rule_spec": ROOT, "slot": 0}, DIAGNOSIS, count=4,
            vehicle="equity", llm_enabled=True, config={"model": "test"},
            adapter=_adapter(reply),
            lessons=lessons)
        self.assertTrue(proposal.success, proposal.error)
        self.assertEqual(sum(item.source == "llm" for item in chosen), 2)
        self.assertFalse(any(item.novel_tuning for item in chosen))

    def test_eight_durable_novel_lessons_block_the_ninth_value(self):
        lessons = [{"id": f"{index + 1:012x}", "proposed_by": "llm",
                    "novel_tuning": True,
                    "tried": {"stop_atr": {"from": 1.0, "to": 1.2}},
                    "verdict": "failed"}
                   for index in range(8)]
        novel = _tuned({"stop_atr": 1.2},
                       "Raised stop_atr after a prior failed attempt.")
        reply = json.dumps({"schema": TUNING_SCHEMA, "variants": [
            {"rule_spec": novel[0], "reason": novel[1],
             "builds_on": lessons[0]["id"]}]})
        chosen, proposal = _tuned_variants(
            {"rule_spec": ROOT, "slot": 0}, DIAGNOSIS, count=4,
            vehicle="equity", llm_enabled=True, config={"model": "test"},
            adapter=_adapter(reply), lessons=lessons)
        self.assertTrue(proposal.success, proposal.error)
        self.assertFalse(any(item.novel_tuning for item in chosen))
        self.assertEqual({item.source for item in chosen}, {"deterministic"})

    def test_a_failed_or_broken_adapter_still_fills_every_variant(self):
        class Exploding:
            def tune(self, **_):
                raise RuntimeError("provider exploded")

        class NoTune:
            def discover(self, **_):
                raise AssertionError("discover is not the tuning seam")

        for adapter in (Exploding(), NoTune(),
                        _adapter("not json at all")):
            with self.subTest(adapter=type(adapter).__name__):
                chosen, _proposal = _tuned_variants(
                    {"rule_spec": ROOT, "slot": 0}, DIAGNOSIS, count=4,
                    vehicle="equity", llm_enabled=True,
                    config={"model": "test"}, adapter=adapter)
                self.assertEqual(len(chosen), 4)
                self.assertEqual({item.source for item in chosen},
                                 {"deterministic"})

    def test_every_variant_carries_a_reason_whoever_proposed_it(self):
        adapter = _adapter(_reply(_tuned({"threshold_bps": 41.0})))
        chosen, _proposal = _tuned_variants(
            {"rule_spec": ROOT, "slot": 0}, DIAGNOSIS, count=4,
            vehicle="equity", llm_enabled=True, config={"model": "test"},
            adapter=adapter)
        for item in chosen:
            self.assertTrue(item.reason.strip())
            self.assertLessEqual(len(item.reason), MAX_REASON_CHARS)

    def test_the_deterministic_reason_names_the_change_and_the_diagnosis(self):
        variants = mutate_with_reasons(ROOT, DIAGNOSIS, 4)
        self.assertEqual(variants[0][1][:9], "Unmutated")
        for spec, reason in variants[1:]:
            delta = spec_delta(ROOT, spec)
            self.assertTrue(delta)
            for key in delta:
                self.assertIn(key, reason)
        self.assertEqual([spec for spec, _r in variants],
                         mutate_from_diagnosis(ROOT, DIAGNOSIS, 4))

    def test_coordinate_pool_is_complete_and_every_child_changes_one_field(self):
        pool = coordinate_mutation_pool(ROOT, DIAGNOSIS)
        self.assertGreater(len(pool), MAX_VARIANTS)
        self.assertEqual(pool[0][0], ROOT)
        for spec, reason in pool[1:]:
            delta = spec_delta(ROOT, spec)
            self.assertEqual(len(delta), 1)
            self.assertIn(next(iter(delta)), reason)

    def test_refinement_exhausts_coordinates_then_interactions_then_confirms(self):
        coordinate = coordinate_mutation_pool(ROOT, DIAGNOSIS)
        coordinate_ids = frozenset(rule_variant_id(spec) for spec, _ in coordinate)
        lessons = [
            {"id": "threshold", "tried": {
                "threshold_bps": {"from": ROOT["threshold_bps"], "to": 40.0}},
             "heldout_delta": -0.1},
            {"id": "target", "tried": {
                "target_r": {"from": ROOT["target_r"], "to": 1.5}},
             "heldout_delta": -0.2},
        ]
        interaction_state = {}
        interactions, _ = _tuned_variants(
            {"rule_spec": ROOT, "slot": 0}, DIAGNOSIS, count=4,
            vehicle="equity", llm_enabled=False, config={}, adapter=None,
            lessons=lessons, already_failed=coordinate_ids,
            refinement_state=interaction_state)
        self.assertEqual(interaction_state["phase"], "interaction")
        self.assertTrue(interactions)
        self.assertTrue(all(len(spec_delta(ROOT, item.rule_spec)) == 2
                            for item in interactions))

        confirm_state = {}
        confirmed, _ = _tuned_variants(
            {"rule_spec": ROOT, "slot": 0}, DIAGNOSIS, count=4,
            vehicle="equity", llm_enabled=False, config={}, adapter=None,
            lessons=lessons,
            already_failed=frozenset({
                *coordinate_ids,
                *(rule_variant_id(item.rule_spec) for item in interactions),
            }), refinement_state=confirm_state)
        self.assertEqual(confirm_state["phase"], "confirmatory")
        self.assertEqual([item.rule_spec for item in confirmed], [ROOT])

    def test_execution_blocked_atr_pair_is_deferred_bounded_and_measured(self):
        diagnosis = {"primary_failure": "execution_blocked"}
        coordinate = coordinate_mutation_pool(ROOT, diagnosis)
        lessons = [
            {"id": f"coordinate-{index}", "changed": spec_delta(ROOT, spec),
             "fit_delta": 0.0}
            for index, (spec, _reason) in enumerate(coordinate[1:], start=1)
        ]
        early = interaction_mutation_pool(
            ROOT, lessons, diagnostic=diagnosis, coordinate_exhausted=False)
        self.assertEqual([
            spec for spec, _reason in early
            if set(spec_delta(ROOT, spec)) == {"min_atr_bps", "stop_atr"}
        ], [])
        late = interaction_mutation_pool(
            ROOT, lessons, diagnostic=diagnosis, coordinate_exhausted=True)
        pairs = [(spec, reason) for spec, reason in late
                  if set(spec_delta(ROOT, spec)) == {"min_atr_bps", "stop_atr"}]
        self.assertEqual(len(pairs), 1)
        pair, reason = pairs[0]
        self.assertEqual((pair["min_atr_bps"], pair["stop_atr"]), (15.0, 6.0))
        self.assertIn("coordinate-", reason)
        self.assertLessEqual(len(late), 12)
        self.assertEqual(len({rule_variant_id(spec) for spec, _ in late}), len(late))

    def test_execution_blocked_pair_uses_first_measured_geometry_clear(self):
        diagnosis = {"primary_failure": "execution_blocked"}
        lessons = [
            {"id": "min-5", "changed": {"min_atr_bps": {
                "from": ROOT["min_atr_bps"], "to": 5.0}}, "fit_delta": 0.0},
            {"id": "min-15", "changed": {"min_atr_bps": {
                "from": ROOT["min_atr_bps"], "to": 15.0}}, "fit_delta": 0.0},
            {"id": "stop-2", "changed": {"stop_atr": {
                "from": ROOT["stop_atr"], "to": 2.0}}, "fit_delta": 0.0},
            {"id": "stop-6", "changed": {"stop_atr": {
                "from": ROOT["stop_atr"], "to": 6.0}}, "fit_delta": 0.0},
        ]
        pool = interaction_mutation_pool(
            ROOT, lessons, diagnostic=diagnosis, coordinate_exhausted=True,
            risk_config={"stressed_cost_scenario_bps": 9.0,
                         "max_stressed_cost_to_risk_ratio": .30})
        pair = [spec for spec, _reason in pool
                if set(spec_delta(ROOT, spec)) == {"min_atr_bps", "stop_atr"}]
        self.assertEqual(len(pair), 1)
        self.assertEqual((pair[0]["min_atr_bps"], pair[0]["stop_atr"]), (5.0, 6.0))

    def test_execution_blocked_pair_never_invents_unmeasured_fallback_values(self):
        diagnosis = {"primary_failure": "execution_blocked"}
        lessons = [
            {"id": "min-5", "changed": {"min_atr_bps": {
                "from": ROOT["min_atr_bps"], "to": 5.0}}, "fit_delta": 0.0},
            {"id": "stop-2", "changed": {"stop_atr": {
                "from": ROOT["stop_atr"], "to": 2.0}}, "fit_delta": 0.0},
        ]
        pool = interaction_mutation_pool(
            ROOT, lessons, diagnostic=diagnosis, coordinate_exhausted=True)
        pair = [spec for spec, _reason in pool
                if set(spec_delta(ROOT, spec)) == {"min_atr_bps", "stop_atr"}]
        self.assertEqual(len(pair), 1)
        self.assertEqual((pair[0]["min_atr_bps"], pair[0]["stop_atr"]), (5.0, 2.0))

    def test_target_hold_geometry_pair_is_bounded_and_coordinate_deferred(self):
        diagnostic = {
            "fit_diagnostics": {"target_hold_reachability": {
                "diagnostic_only": True, "authorizing": False,
                "genuine_mismatch": True, "adequate": True, "usable": 30,
                "recommendation": {"target_r": 1.0, "max_hold_bars": 90},
            }}}
        early = interaction_mutation_pool(
            ROOT, [], diagnostic=diagnostic, coordinate_exhausted=False)
        self.assertFalse(any(set(spec_delta(ROOT, spec)) <= {
            "target_r", "max_hold_bars"} for spec, _reason in early))
        late = interaction_mutation_pool(
            ROOT, [], diagnostic=diagnostic, coordinate_exhausted=True)
        pairs = [(spec, reason) for spec, reason in late
                 if set(spec_delta(ROOT, spec)) <= {
                     "target_r", "max_hold_bars"}]
        self.assertEqual(len(pairs), 1)
        spec, reason = pairs[0]
        self.assertEqual(spec["target_r"], 1.0)
        self.assertEqual(spec["max_hold_bars"], 90)
        self.assertNotEqual(rule_variant_id(spec), rule_variant_id(ROOT))
        self.assertIn("time-expiry", reason)

    def test_target_hold_geometry_pair_is_underpowered_or_ambiguous_safe(self):
        diagnostic = {
            "fit_diagnostics": {"target_hold_reachability": {
                "diagnostic_only": True, "authorizing": False,
                "genuine_mismatch": True, "adequate": False, "usable": 29,
                "recommendation": {"target_r": 1.0, "max_hold_bars": 90},
            }}}
        pool = interaction_mutation_pool(
            ROOT, [], diagnostic=diagnostic, coordinate_exhausted=True)
        self.assertFalse(any(set(spec_delta(ROOT, spec)) <= {
            "target_r", "max_hold_bars"} for spec, _reason in pool))

    def test_generic_interaction_ties_use_failure_priority_and_numeric_values(self):
        lessons = [
            {"id": "threshold-high", "changed": {"threshold_bps": {
                "from": ROOT["threshold_bps"], "to": 10.0}}, "heldout_delta": 0.1},
            {"id": "threshold-low", "changed": {"threshold_bps": {
                "from": ROOT["threshold_bps"], "to": 6.0}}, "heldout_delta": 0.1},
            {"id": "target", "changed": {"target_r": {
                "from": ROOT["target_r"], "to": 1.5}}, "heldout_delta": 0.1},
        ]
        pool = interaction_mutation_pool(
            ROOT, lessons, diagnostic={"primary_failure": "negative_expectancy"},
            limit=1)
        self.assertEqual(len(pool), 1)
        spec, _reason = pool[0]
        # ``threshold_bps`` is the first negative-expectancy axis, and 6.0 is
        # selected numerically before 10.0 even though string ordering differs.
        self.assertEqual(spec["threshold_bps"], 6.0)
        self.assertEqual(spec["target_r"], 1.5)

    def test_the_reason_limit_matches_the_adapters(self):
        """The core states the bound without importing the optional adapter."""
        self.assertEqual(_REASON_LIMIT, MAX_REASON_CHARS)
        long = mutation_reason(ROOT, {**ROOT, "threshold_bps": 1234.5678},
                               {"primary_failure": "x" * 400})
        self.assertLessEqual(len(long), MAX_REASON_CHARS)


class LessonLedgerTests(unittest.TestCase):
    """The reason is fixed before the gate exists, and graded against it after."""

    def _seeded(self, factory):
        hypothesis = initial_hypotheses(1)[0]
        factory.register(hypothesis)
        return hypothesis

    def test_a_reason_is_recorded_then_graded_once(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            hypothesis = self._seeded(factory)
            variant = rule_variant_id(hypothesis.rule_spec)
            lesson = factory.record_lesson(
                hypothesis.hypothesis_id, vehicle="equity",
                family=hypothesis.family, variant_id=variant, kind="tuning",
                source="llm", reason="Raised the threshold.",
                changed={"threshold_bps": {"from": 5.0, "to": 40.0}},
                diagnosis=DIAGNOSIS)
            self.assertEqual(factory.lessons(vehicle="equity")[0]["outcome"], None)
            graded = factory.grade_lesson(
                hypothesis.hypothesis_id, variant, kind="tuning",
                outcome={"passed": False, "underpowered": False,
                         "heldout_delta": -0.4, "q_value": .8,
                         "failed_checks": ["heldout_delta_positive"],
                         "gate_hash": "a" * 64})
            self.assertEqual(graded, lesson)
            row = factory.lessons(vehicle="equity")[0]
            self.assertEqual(row["reason"], "Raised the threshold.")
            self.assertEqual(row["outcome"]["passed"], False)
            self.assertEqual(row["outcome"]["failed_checks"],
                             ["heldout_delta_positive"])
            self.assertEqual(row["outcome"]["gate_hash"], "a" * 64)
            # Grading twice must not rewrite the first verdict.
            factory.grade_lesson(hypothesis.hypothesis_id, variant,
                                 kind="tuning",
                                 outcome={"passed": True, "failed_checks": []})
            self.assertIs(factory.lessons(vehicle="equity")[0]["outcome"]["passed"],
                          False)

    def test_recording_the_same_proposal_twice_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            hypothesis = self._seeded(factory)
            variant = rule_variant_id(hypothesis.rule_spec)
            ids = {factory.record_lesson(
                hypothesis.hypothesis_id, vehicle="equity",
                family=hypothesis.family, variant_id=variant, kind="tuning",
                source="llm", reason="Once.") for _ in range(3)}
            self.assertEqual(len(ids), 1)
            self.assertEqual(len(factory.lessons(vehicle="equity")), 1)

    def test_novel_tuning_cap_survives_restart_and_long_history(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            hypothesis = self._seeded(factory)
            for index in range(9):
                spec = validate_rule_spec({
                    **hypothesis.rule_spec, "stop_atr": 1.1 + index * .1})
                factory.record_lesson(
                    hypothesis.hypothesis_id, vehicle="equity",
                    family=hypothesis.family,
                    variant_id=rule_variant_id(spec) + f".{index}",
                    kind="tuning", source="llm", reason="Novel stop tuning.",
                    changed={"stop_atr": {"from": 1.0,
                                            "to": spec["stop_atr"]}},
                    evidence={"novel_tuning": True})
            restarted = FactoryLedger(factory.path)
            durable = restarted.novel_tuning_values(
                hypothesis_id=hypothesis.hypothesis_id, vehicle="equity",
                family=hypothesis.family)
            self.assertEqual(len(durable), 9)
            novel = ({**hypothesis.rule_spec, "stop_atr": 1.2},
                     "Raised stop_atr after durable prior attempts.")
            chosen, proposal = _tuned_variants(
                {"rule_spec": hypothesis.rule_spec, "slot": 0}, DIAGNOSIS,
                count=4, vehicle="equity", llm_enabled=True,
                config={"model": "test"},
                adapter=_adapter(_reply(novel)),
                durable_novel_tuning_values=frozenset(durable))
        self.assertTrue(proposal.success)
        self.assertFalse(any(item.novel_tuning for item in chosen))

    def test_lessons_are_immutable_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            hypothesis = self._seeded(factory)
            variant = rule_variant_id(hypothesis.rule_spec)
            factory.record_lesson(hypothesis.hypothesis_id, vehicle="equity",
                                  family=hypothesis.family, variant_id=variant,
                                  kind="tuning", source="llm", reason="Why.")
            with self.assertRaises(FactoryError):
                factory.record_lesson(hypothesis.hypothesis_id, vehicle="equity",
                                      family=hypothesis.family,
                                      variant_id=variant, kind="guessing",
                                      source="llm", reason="Why.")
            with self.assertRaises(FactoryError):
                factory.record_lesson(hypothesis.hypothesis_id, vehicle="equity",
                                      family=hypothesis.family,
                                      variant_id=variant, kind="tuning",
                                      source="llm", reason="   ")
            with self.assertRaises(KeyError):
                factory.record_lesson("hyp.unknown", vehicle="equity",
                                      family="x", variant_id=variant,
                                      kind="tuning", source="llm", reason="Why.")
            import sqlite3
            with closing(sqlite3.connect(factory.path)) as db, db:
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("UPDATE factory_lessons SET reason='rewritten'")
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("DELETE FROM factory_lessons")

    def test_grading_a_variant_nobody_proposed_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            hypothesis = self._seeded(factory)
            self.assertIsNone(factory.grade_lesson(
                hypothesis.hypothesis_id, "rule.absent.0", kind="tuning",
                outcome={"passed": True}))


class FeedbackLoopTests(unittest.TestCase):
    """A cycle records reasons; the next cycle is told how they turned out."""

    def _model(self, briefs):
        class Model:
            def discover(inner, **_):
                return ProposalResult(False, error="stay deterministic")

            def tune(inner, *, vehicle, slot, rule_spec, diagnosis, count,
                     lessons):
                briefs.append(list(lessons))
                # Cite the most recent lesson, exactly as the contract makes a
                # real model do once any history exists.
                cite = lessons[0]["id"] if lessons else None
                tried: set[str] = set()
                for lesson in lessons:
                    changes = lesson.get("tried") or {}
                    if not isinstance(changes, dict):
                        continue
                    candidate = dict(rule_spec)
                    usable = True
                    for field, change in changes.items():
                        if not isinstance(change, dict) or "to" not in change:
                            usable = False
                            break
                        candidate[field] = change["to"]
                    if usable:
                        try:
                            tried.add(rule_variant_id(validate_rule_spec(candidate)))
                        except (TypeError, ValueError):
                            pass
                specs = [spec for spec, _reason in coordinate_mutation_pool(
                    rule_spec, diagnosis)
                    if rule_variant_id(spec) != rule_variant_id(rule_spec)
                    and rule_variant_id(spec) not in tried][:count]
                # V3 tuning is numeric-only.  If the prior lesson is a
                # categorical confirmation change from an older fixture,
                # continue with the next unused numeric coordinate so this
                # feedback-loop test still exercises durable citation.
                specs = [spec for spec in specs
                         if all(isinstance(rule_spec.get(field), (int, float))
                                and not isinstance(rule_spec.get(field), bool)
                                for field in spec_delta(rule_spec, spec))]
                if not specs:
                    specs = [spec for spec, _reason in coordinate_mutation_pool(
                        rule_spec, diagnosis)
                             if rule_variant_id(spec) != rule_variant_id(rule_spec)
                             and rule_variant_id(spec) not in tried
                             and all(isinstance(rule_spec.get(field), (int, float))
                                     and not isinstance(rule_spec.get(field), bool)
                                     for field in spec_delta(rule_spec, spec))][:count]
                return ProposalResult(
                    True, schema=TUNING_SCHEMA, rule_spec=rule_spec,
                    evidence={"kind": "tuning"},
                    variants=tuple(
                        {"rule_spec": spec, "variant_id": rule_variant_id(spec),
                         "reason": f"Changed {' and '.join(spec_delta(rule_spec, spec))} "
                                   f"against {diagnosis.get('primary_failure')}.",
                         "builds_on": cite}
                        for spec in specs))
        return Model()

    def test_a_second_cycle_is_told_how_the_first_cycle_reasons_turned_out(self):
        briefs: list[list] = []
        rows = losing_breakouts()
        with tempfile.TemporaryDirectory() as directory:
            options = dict(
                db_path=Path(directory) / "edge_lab.sqlite3", strategies=1,
                variants_per_strategy=3, workers=1, min_trades=1,
                min_sessions=1, alpha=1.0,
                strategy_llm={"enabled": True, "model": "test"},
                proposal_adapter=self._model(briefs))
            with _compact_factory_protocol():
                first = run_factory(rows, **options)
                # A different corpus, so this is a real second cycle rather than
                # the duplicate-dataset short circuit.
                second = run_factory(rows[:-60], **options)
            factory = FactoryLedger(options["db_path"])
            recorded = factory.lessons(vehicle="equity", limit=100)

        self.assertEqual(first["status"], "complete")
        self.assertEqual(len(briefs), 2)
        self.assertEqual(briefs[0], [], "the first cycle has nothing to learn from")
        self.assertTrue(briefs[1], "the second cycle must receive graded history")
        for entry in briefs[1]:
            self.assertIn(entry["verdict"], {
                "fit_positive", "fit_negative", "fit_inconclusive",
                "underpowered", "execution_blocked", "fit_ungraded",
            })
            self.assertTrue(entry["reason"])
            self.assertIn("proposed_by", entry)
            self.assertRegex(entry["id"], r"^[0-9a-f]{12}$")
        self.assertTrue(any(item["source"] == "llm" for item in recorded))
        self.assertTrue(all(item["outcome"] is not None for item in recorded
                            if item["kind"] == "tuning"))
        self.assertTrue(second["tuning"])

        # The citation survives as a durable edge: at least one second-cycle
        # lesson points back at a first-cycle one, and every parent resolves.
        by_id = {item["lesson_id"]: item for item in recorded}
        children = [item for item in recorded if item["parent_lesson_id"]]
        self.assertTrue(children, "the second cycle must record what it built on")
        for child in children:
            self.assertIn(child["parent_lesson_id"], by_id)
            self.assertLess(by_id[child["parent_lesson_id"]]["created_at"],
                            child["created_at"])

    def test_the_brief_trims_oldest_history_rather_than_failing(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            hypothesis = initial_hypotheses(1)[0]
            factory.register(hypothesis)
            for index in range(40):
                spec = validate_rule_spec(
                    {**hypothesis.rule_spec, "threshold_bps": 10.0 + index})
                variant = rule_variant_id(spec)
                factory.record_lesson(
                    hypothesis.hypothesis_id, vehicle="equity",
                    family=hypothesis.family, variant_id=variant, kind="tuning",
                    source="llm", reason="A padded reason. " * 12,
                    changed=spec_delta(hypothesis.rule_spec, spec))
                factory.grade_lesson(hypothesis.hypothesis_id, variant,
                                     kind="tuning",
                                     outcome={"passed": False,
                                              "failed_checks": ["a", "b"]})
            brief = _lesson_brief(factory, vehicle="equity")
            self.assertTrue(brief)
            self.assertLessEqual(len(json.dumps(brief).encode("utf-8")),
                                 factory_module.LESSON_BRIEF_BYTES)

    def test_a_ledger_without_lessons_degrades_to_no_history(self):
        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            import sqlite3
            with closing(sqlite3.connect(factory.path)) as db, db:
                db.execute("DROP TABLE factory_lesson_outcomes")
                db.execute("DROP TABLE factory_lessons")
            self.assertEqual(_lesson_brief(factory, vehicle="equity"), [])


class FitSelectionSanitizerTests(unittest.TestCase):
    """Only fit aggregates may cross from the factory into a model seam."""

    def test_recursive_projection_drops_post_selection_and_raw_fields(self):
        unsafe = {
            **DIAGNOSIS,
            "p_value": .001,
            "q_value": .002,
            "heldout": [{"net_pnl": 9999}],
            "fit_diagnostics": {
                "scope": "fit_only",
                "eligible_prefix": {"eligible": 4, "heldout": 99},
                "risk": {
                    "configured": {"median": 500.0},
                    "planned": {"median": 117.5},
                    "capped_delivered": {"median": 117.5},
                    "delivered_to_configured": {"median": .235},
                    "raw_rows": [{"risk": 9999}],
                },
                "gate": {"passes": True},
                "raw_rows": [{"close": 1}],
            },
        }
        projected = _sanitize_fit_selection(
            unsafe, context="diagnostic", label="diagnosis")
        encoded = json.dumps(projected, sort_keys=True)
        for token in ("p_value", "q_value", "heldout", "gate", "passes",
                      "raw_rows", "close"):
            self.assertNotIn(token, encoded)
        self.assertEqual(projected["fit_diagnostics"]["eligible_prefix"]["eligible"], 4)
        risk = projected["fit_diagnostics"]["risk"]
        self.assertEqual(risk["configured"]["median"], 500.0)
        self.assertEqual(risk["planned"]["median"], 117.5)
        self.assertEqual(risk["capped_delivered"]["median"], 117.5)
        self.assertEqual(risk["delivered_to_configured"]["median"], .235)

    def test_unsafe_lesson_fields_do_not_reach_tuner_or_change_choices(self):
        captured = []

        class Recorder:
            def tune(inner, *, diagnosis, lessons, **_kwargs):
                captured.append((diagnosis, lessons))
                return ProposalResult(False, error="recording only")

        coordinate = coordinate_mutation_pool(ROOT, DIAGNOSIS)
        failed = frozenset(rule_variant_id(spec) for spec, _ in coordinate)
        lessons = [{
            "id": "a1b2c3d4e5f6",
            "tried": {"threshold_bps": {"from": 5.0, "to": 6.0}},
            "heldout_delta": 999.0,
            "q_value": .001,
            "failed_checks": ["heldout_delta_positive"],
            "raw_rows": [{"close": 1}],
            "verdict": "passed",
        }, {
            "id": "0f1e2d3c4b5a",
            "tried": {"target_r": {"from": 2.0, "to": 2.2}},
            "heldout_delta": -999.0,
            "q_value": .99,
            "failed_checks": ["heldout_delta_positive"],
            "raw_rows": [{"close": 2}],
            "verdict": "failed",
        }]
        baseline, _ = _tuned_variants(
            {"rule_spec": ROOT, "slot": 0}, DIAGNOSIS, count=3,
            vehicle="equity", llm_enabled=True, config={"model": "test"},
            adapter=Recorder(), lessons=lessons, already_failed=failed)
        # A second call is deliberately given adversarial values.  The same
        # bounded interaction coordinate must be selected because held-out
        # fields are not part of the model-selection projection.
        adversarial = [{**lessons[0], "heldout_delta": -999.0,
                        "p_value": .99, "passes": False},
                       {**lessons[1], "heldout_delta": 999.0,
                        "p_value": .001, "passes": True}]
        changed, _ = _tuned_variants(
            {"rule_spec": ROOT, "slot": 0}, DIAGNOSIS, count=3,
            vehicle="equity", llm_enabled=True, config={"model": "test"},
            adapter=Recorder(), lessons=adversarial, already_failed=failed)
        self.assertEqual([item.rule_spec for item in baseline],
                         [item.rule_spec for item in changed])
        diagnosis, seen_lessons = captured[-1]
        encoded = json.dumps({"diagnosis": diagnosis, "lessons": seen_lessons},
                             sort_keys=True)
        for token in ("heldout", "q_value", "p_value", "failed_checks",
                      "raw_rows", "passes"):
            self.assertNotIn(token, encoded)
        self.assertEqual(seen_lessons[0]["verdict"], "passed")


class SharedLearningTests(unittest.TestCase):
    """One slot's search should benefit from the other ten.

    A per-family brief can only say what happened to one idea. Some of what
    research learns is not about one idea at all, and sharing that is what
    stops eleven slots rediscovering the same thing eleven times.
    """

    def _graded(self, factory, hypothesis, *, family, changes, passed,
                kind="tuning", source="llm"):
        for index, change in enumerate(changes):
            spec = validate_rule_spec({**hypothesis.rule_spec, **change})
            variant = rule_variant_id(spec) + f".{family}.{index}"
            factory.record_lesson(
                hypothesis.hypothesis_id, vehicle="equity", family=family,
                variant_id=variant, kind=kind, source=source,
                reason=f"Attempt {index} on {family}.",
                changed=spec_delta(hypothesis.rule_spec, spec))
            factory.grade_lesson(hypothesis.hypothesis_id, variant, kind=kind,
                                 outcome={"passed": passed,
                                          "underpowered": False,
                                          "failed_checks": [],
                                          # Shared proposal learning is
                                          # intentionally fit-only.  Keep this
                                          # helper explicit so these tests do
                                          # not rely on the retired full-gate
                                          # ``passed`` bit.
                                          "fit_classification": (
                                              "fit_positive" if passed else
                                              "fit_negative"),
                                          "classification": (
                                              "fit_positive" if passed else
                                              "fit_negative"),
                                          "fit_passed": bool(passed),
                                          "fit_delta": 1.0 if passed else -1.0})

    def test_it_aggregates_parameter_directions_across_families(self):
        from research.strategy_factory import shared_learning

        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            hypothesis = initial_hypotheses(1)[0]
            factory.register(hypothesis)
            raised = [{"threshold_bps": 20.0 + index} for index in range(4)]
            self._graded(factory, hypothesis, family="alpha",
                         changes=raised, passed=True)
            self._graded(factory, hypothesis, family="beta",
                         changes=[{"threshold_bps": 40.0 + index}
                                  for index in range(3)], passed=False)
            digest = shared_learning(factory, vehicle="equity")

        self.assertEqual(digest["graded_attempts"], 7)
        by_parameter = {(item["parameter"], item["direction"]): item
                        for item in digest["parameters"]}
        entry = by_parameter[("threshold_bps", "raised")]
        self.assertEqual(entry["attempts"], 7)
        self.assertEqual(entry["passed"], 4)
        families = {item["family"] for item in digest["families"]}
        self.assertEqual(families, {"alpha", "beta"})

    def test_underpowered_attempts_do_not_manufacture_a_trend(self):
        from research.strategy_factory import shared_learning

        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            hypothesis = initial_hypotheses(1)[0]
            factory.register(hypothesis)
            for index in range(5):
                spec = validate_rule_spec(
                    {**hypothesis.rule_spec, "threshold_bps": 30.0 + index})
                variant = rule_variant_id(spec)
                factory.record_lesson(
                    hypothesis.hypothesis_id, vehicle="equity",
                    family="alpha", variant_id=variant, kind="tuning",
                    source="llm", reason="Thin sample.",
                    changed=spec_delta(hypothesis.rule_spec, spec))
                factory.grade_lesson(hypothesis.hypothesis_id, variant,
                                     kind="tuning",
                                     outcome={"passed": False,
                                              "underpowered": True})
        digest = shared_learning(factory, vehicle="equity")
        self.assertEqual(digest["parameters"], [])
        self.assertEqual(digest["families"], [])
        self.assertEqual(digest["live_trials"], {"run": 0, "failed": 0})

    def test_legacy_full_gate_lesson_is_not_shared_as_fit_learning(self):
        from research.strategy_factory import shared_learning

        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            hypothesis = initial_hypotheses(1)[0]
            factory.register(hypothesis)
            variant = rule_variant_id(validate_rule_spec(
                {**hypothesis.rule_spec, "threshold_bps": 31.0}))
            factory.record_lesson(
                hypothesis.hypothesis_id, vehicle="equity",
                family=hypothesis.family, variant_id=variant, kind="tuning",
                source="llm", reason="Legacy gate result.",
                changed={"threshold_bps": {"from": 5.0, "to": 31.0}})
            factory.grade_lesson(
                hypothesis.hypothesis_id, variant, kind="tuning",
                outcome={"passed": True, "underpowered": False,
                         "classification": "proved", "heldout_delta": 99.0})
            digest = shared_learning(factory, vehicle="equity")
        self.assertEqual(digest["graded_attempts"], 0)
        self.assertEqual(digest["parameters"], [])

    def test_a_single_attempt_is_not_reported_as_a_pattern(self):
        from research.strategy_factory import (SHARED_LEARNING_MIN_ATTEMPTS,
                                               shared_learning)

        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            hypothesis = initial_hypotheses(1)[0]
            factory.register(hypothesis)
            self._graded(factory, hypothesis, family="alpha",
                         changes=[{"threshold_bps": 42.0}], passed=True)
            digest = shared_learning(factory, vehicle="equity")
        self.assertGreater(SHARED_LEARNING_MIN_ATTEMPTS, 1)
        self.assertEqual(digest["parameters"], [])
        self.assertEqual(digest["graded_attempts"], 1)

    def test_live_trials_are_counted_apart_from_replays(self):
        from research.strategy_factory import shared_learning

        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            hypothesis = initial_hypotheses(1)[0]
            factory.register(hypothesis)
            self._graded(factory, hypothesis, family="alpha",
                         changes=[{"threshold_bps": 20.0}], passed=False,
                         kind="trial", source="live_paper")
            digest = shared_learning(factory, vehicle="equity")
        self.assertEqual(digest["live_trials"], {"run": 1, "failed": 1})

    def test_an_empty_or_missing_ledger_degrades_to_no_digest(self):
        from research.strategy_factory import shared_learning

        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            self.assertEqual(shared_learning(factory, vehicle="equity"),
                             {"graded_attempts": 0, "parameters": [],
                              "families": [],
                              "live_trials": {"run": 0, "failed": 0}})
            import sqlite3

            with closing(sqlite3.connect(factory.path)) as db, db:
                db.execute("DROP TABLE factory_lesson_outcomes")
                db.execute("DROP TABLE factory_lessons")
            self.assertEqual(
                shared_learning(factory, vehicle="equity")["graded_attempts"], 0)

    def test_the_digest_reaches_a_tuning_request(self):
        """Aggregated learning is only useful if a proposal actually sees it."""
        seen = []

        class Recorder:
            def tune(inner, *, diagnosis, **_):
                seen.append(diagnosis)
                return ProposalResult(False, error="recording only")

        digest = {"graded_attempts": 9,
                  "parameters": [{"parameter": "target_r", "direction": "lowered",
                                  "attempts": 5, "passed": 4}],
                  "families": [], "live_trials": {"run": 0, "failed": 0}}
        _tuned_variants({"rule_spec": ROOT, "slot": 0}, DIAGNOSIS, count=3,
                        vehicle="equity", llm_enabled=True,
                        config={"model": "test"}, adapter=Recorder(),
                        shared=digest)
        self.assertEqual(seen[0]["shared_learning"], digest)

    def test_an_empty_digest_is_not_attached(self):
        seen = []

        class Recorder:
            def tune(inner, *, diagnosis, **_):
                seen.append(diagnosis)
                return ProposalResult(False, error="recording only")

        _tuned_variants({"rule_spec": ROOT, "slot": 0}, DIAGNOSIS, count=3,
                        vehicle="equity", llm_enabled=True,
                        config={"model": "test"}, adapter=Recorder(),
                        shared={"graded_attempts": 0, "parameters": []})
        self.assertNotIn("shared_learning", seen[0])


class FrozenEdgeTests(unittest.TestCase):
    """Tuning is a pre-promotion activity, and only a pre-promotion activity."""

    def test_forward_validation_carries_variants_forward_untuned(self):
        """The seam that guarantees a proved variant is never re-tuned."""
        calls: list = []

        class Model:
            def tune(inner, **kwargs):
                calls.append(kwargs)
                return ProposalResult(False, error="unused")

            def discover(inner, **_):
                return ProposalResult(False, error="unused")

        rows = losing_breakouts()
        with tempfile.TemporaryDirectory() as directory:
            with _compact_factory_protocol():
                run_factory(rows, db_path=Path(directory) / "edge_lab.sqlite3",
                            strategies=1, variants_per_strategy=2, workers=1,
                            min_trades=1, min_sessions=1, alpha=1.0,
                            strategy_llm={"enabled": True, "model": "test"},
                            proposal_adapter=Model())
        # Every tuning request in a backtest cycle names a backtest diagnosis;
        # ``forward_validation`` never reaches the tuner at all.
        self.assertTrue(calls)
        for call in calls:
            self.assertNotEqual(call["diagnosis"].get("primary_failure"),
                                "forward_validation")


if __name__ == "__main__":
    unittest.main()
