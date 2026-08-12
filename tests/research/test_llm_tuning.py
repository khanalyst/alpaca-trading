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

import json
from pathlib import Path
import tempfile
import unittest

from agent.contracts.rule import (rule_variant_id, validate_rule_spec)
from research.edge_lab import EdgeLedger
from research.factory_core import (
    MAX_VARIANTS, _REASON_LIMIT, family_template, initial_hypotheses,
    mutate_from_diagnosis, mutate_with_reasons, mutation_reason, spec_delta,
)
from research.factory_ledger import FactoryError, FactoryLedger
from research.llm_strategy import (
    MAX_REASON_CHARS, MAX_TUNED_VARIANTS, TUNING_SCHEMA, ProposalResult,
    RuleProposalAdapter,
)
import research.strategy_factory as factory_module
from research.strategy_factory import _lesson_brief, _tuned_variants, run_factory

from .test_strategy_factory import losing_breakouts


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
        adapter = _adapter(_reply(_tuned({"threshold_bps": 40.0}),
                                  _tuned({"threshold_bps": 55.0}, "Widened it "
                                         "further to test the same idea harder.")))
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

    def test_tuning_may_not_change_the_family(self):
        """Changing the idea is discovery's job; tuning changes its numbers."""
        adapter = _adapter(_reply(({**ROOT, "family": "mean_reversion"},
                                   "Switched to a different idea entirely.")))
        result = adapter.tune("equity", 0, ROOT, DIAGNOSIS, count=1)
        self.assertFalse(result.success)
        self.assertIn("family", result.error)

    def test_tuning_may_not_widen_the_grammar(self):
        """v2 unlocks whole predicate categories the root never expressed.

        Reaching them is inventing structure, not tuning values, so tuning is
        pinned to the root's own grammar version.
        """
        wider = {**ROOT, "schema": "rule-strategy.v2",
                 "confirmations": ["trend"], "entry_after_minutes": 45}
        adapter = _adapter(_reply((wider, "Added a time window and a filter.")))
        result = adapter.tune("equity", 0, ROOT, DIAGNOSIS, count=1)
        self.assertFalse(result.success)
        self.assertIn("may not widen the grammar", result.error)

    def test_a_v2_root_is_tuned_in_v2_and_still_cannot_change_family(self):
        """Pinning is to the root's own version, not to v1 forever."""
        root = validate_rule_spec({**ROOT, "schema": "rule-strategy.v2",
                                   "entry_after_minutes": 30})
        adapter = _adapter(_reply(({**root, "entry_after_minutes": 60},
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
        adapter = _adapter(_reply(_tuned({"threshold_bps": 40.0}),
                                  _tuned({"threshold_bps": 40.0}),
                                  _tuned({"threshold_bps": 60.0})))
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


class CitedLearningTests(unittest.TestCase):
    """A proposal has to say what it learned from, or it is a guess."""

    LESSONS = [
        {"id": "a1b2c3d4e5f6", "reason": "Raising the threshold cut every "
                                         "signal.", "verdict": "failed",
         "tried": {"threshold_bps": {"from": 5.0, "to": 90.0}}},
        {"id": "0f1e2d3c4b5a", "reason": "Shorter holds helped the payoff.",
         "verdict": "passed", "tried": {"max_hold_bars": {"from": 90, "to": 45}}},
    ]

    def _reply_with(self, builds_on, threshold=40.0):
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
            {"rule_spec": {**ROOT, "threshold_bps": 40.0},
             "reason": "Nothing has been tried yet; start wider."}]})
        result = _adapter(reply).tune("equity", 0, ROOT, DIAGNOSIS, count=1)
        self.assertTrue(result.success, result.error)
        self.assertIsNone(result.variants[0]["builds_on"])

    def test_a_repeat_of_a_graded_failure_is_dropped(self):
        """The ledger already answered that experiment."""
        repeat = validate_rule_spec({**ROOT, "threshold_bps": 40.0})
        adapter = _adapter(self._reply_with("a1b2c3d4e5f6", threshold=40.0))
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

    def test_failed_variant_ids_excludes_underpowered_and_passing(self):
        """Underpowered is not an answer, so it is not a closed door."""
        with tempfile.TemporaryDirectory() as directory:
            factory, _edge = _ledgers(directory)
            hypothesis = initial_hypotheses(1)[0]
            factory.register(hypothesis)
            outcomes = {
                "failed": {"passed": False, "underpowered": False},
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
        adapter = _adapter(_reply(_tuned({"threshold_bps": 41.0}),
                                  _tuned({"threshold_bps": 42.0})))
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
        self.assertEqual(origins.count("llm"), 2)
        self.assertEqual(
            len({rule_variant_id(item.rule_spec) for item in chosen}), 4)

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

    def test_a_sweep_fill_says_it_had_no_diagnosis_behind_it(self):
        reasons = [reason for _s, reason in
                   mutate_with_reasons(ROOT, DIAGNOSIS, MAX_VARIANTS)]
        self.assertTrue(any("no diagnosis behind it" in item for item in reasons))

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
            with sqlite3.connect(factory.path) as db:
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
                # A model that learns proposes something it has not already
                # been told the answer to, so the values move with the history.
                base = 40.0 + 3.0 * len(lessons)
                specs = [validate_rule_spec(
                    {**rule_spec, "threshold_bps": base + 7 * index})
                    for index in range(count)]
                return ProposalResult(
                    True, schema=TUNING_SCHEMA, rule_spec=rule_spec,
                    evidence={"kind": "tuning"},
                    variants=tuple(
                        {"rule_spec": spec, "variant_id": rule_variant_id(spec),
                         "reason": f"Threshold to {spec['threshold_bps']} against "
                                   f"{diagnosis.get('primary_failure')}.",
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
            self.assertIn(entry["verdict"], {"passed", "failed", "underpowered"})
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
            with sqlite3.connect(factory.path) as db:
                db.execute("DROP TABLE factory_lesson_outcomes")
                db.execute("DROP TABLE factory_lessons")
            self.assertEqual(_lesson_brief(factory, vehicle="equity"), [])


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
