"""Semantic duplicate policy for model-authored research proposals."""

from dataclasses import asdict
import unittest

from agent.config import ConfigError, validate_config
from agent.contracts.rule import rule_variant_id, validate_rule_spec
from research.factory_core import family_template, template_hypothesis, mutate_with_reasons
from research.llm_strategy import (DISCOVERY_SCHEMA, PROPOSAL_SCHEMA,
                                    TUNING_SCHEMA, ProposalResult)
from research.strategy_factory import (
    NEAR_DUPLICATE_DISTANCE, _llm_replacement, _seed_slot, _tuned_variants,
)


class _DiscoveryAdapter:
    def __init__(self, spec):
        self.spec = validate_rule_spec(spec)

    def discover(self, **_kwargs):
        return ProposalResult(
            True, schema=DISCOVERY_SCHEMA, rule_spec=self.spec,
            variant_id=rule_variant_id(self.spec), thesis="A bounded test edge.")


class _TuningAdapter:
    def __init__(self, specs):
        self.specs = [validate_rule_spec(spec) for spec in specs]

    def tune(self, **_kwargs):
        return ProposalResult(
            True, schema=TUNING_SCHEMA,
            variants=tuple({
                "rule_spec": spec,
                "variant_id": rule_variant_id(spec),
                "reason": "Raised lookback to test the diagnosed history change.",
            } for spec in self.specs))


class _ReplacementAdapter:
    def __init__(self, spec):
        self.spec = validate_rule_spec(spec)

    def propose(self, **_kwargs):
        return ProposalResult(
            True, schema=PROPOSAL_SCHEMA, rule_spec=self.spec,
            variant_id=rule_variant_id(self.spec))


class SemanticNearDuplicateTests(unittest.TestCase):
    def test_config_exposes_bounded_default(self):
        cfg = validate_config({})
        self.assertEqual(NEAR_DUPLICATE_DISTANCE, 0.001)
        self.assertEqual(
            cfg["research"]["strategy_llm"]["near_duplicate_distance"], 0.001)
        for value in (-0.01, 1.01, "near"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                validate_config({"research": {"strategy_llm": {
                    "near_duplicate_distance": value}}})

    def test_genesis_template_alias_falls_back_to_deterministic_discovery(self):
        template = template_hypothesis(0)
        proposal = _DiscoveryAdapter(template.rule_spec)
        seeded, result, source = _seed_slot(
            asdict(template), generation=0, not_before=None,
            existing_variant_ids=set(), existing_specs=(),
            tried_families=set(), context={}, llm_enabled=True,
            config={"model": "test", "near_duplicate_distance": 0.001},
            adapter=proposal)
        self.assertTrue(result.success)
        self.assertEqual(source, "deterministic_discovery")
        self.assertNotEqual(seeded.rule_spec, template.rule_spec)

    def test_persisted_one_step_lookback_alias_is_rejected(self):
        prior = validate_rule_spec(family_template("momentum_continuation"))
        near = dict(prior, lookback=prior["lookback"] + 1)
        previous = asdict(template_hypothesis(0))
        seeded, _result, source = _seed_slot(
            previous, generation=1, not_before=None,
            existing_variant_ids={rule_variant_id(prior)},
            existing_specs=[prior], tried_families=set(), context={},
            llm_enabled=True, config={"model": "test"},
            adapter=_DiscoveryAdapter(near))
        self.assertEqual(source, "deterministic_discovery")
        self.assertNotEqual(seeded.rule_spec, near)

    def test_replacement_alias_is_rejected_against_previous_root(self):
        previous = asdict(template_hypothesis(0))
        near = dict(previous["rule_spec"], range_minutes=16)
        replacement, proposal, reason = _llm_replacement(
            previous, {"primary_failure": "negative_expectancy"},
            config={"model": "test", "near_duplicate_distance": 0.001},
            max_generations=5, not_before=None, existing_variant_ids=set(),
            existing_specs=[previous["rule_spec"]],
            adapter=_ReplacementAdapter(near))
        self.assertTrue(proposal.success)
        self.assertIsNone(replacement)
        self.assertEqual(reason, "duplicate_llm_variant")

    def test_current_cycle_llm_alias_is_rejected(self):
        root = validate_rule_spec(family_template("momentum_continuation"))
        # These broad-grammar values are deliberately outside the current
        # deterministic coordinate neighborhood.  Model tuning is now a
        # search-order aid over that finite preregistered pool, not a second
        # uncounted hypothesis generator.
        first = dict(root, lookback=30)
        second = dict(root, lookback=31)
        chosen, proposal = _tuned_variants(
            {"rule_spec": root, "slot": 0},
            {"primary_failure": "negative_expectancy"},
            count=4, vehicle="equity", llm_enabled=True,
            config={"model": "test", "near_duplicate_distance": 0.001},
            adapter=_TuningAdapter([first, second]), existing_specs=[])
        self.assertTrue(proposal.success)
        self.assertEqual(len(chosen), 4)
        self.assertEqual([item.source for item in chosen].count("llm"), 0)
        self.assertNotIn(first, [item.rule_spec for item in chosen])
        self.assertNotIn(second, [item.rule_spec for item in chosen])

    def test_near_duplicate_llm_falls_back_to_complete_deterministic_ladder(self):
        root = validate_rule_spec(family_template("momentum_continuation"))
        near_root = dict(root, lookback=root["lookback"] + 1)
        chosen, proposal = _tuned_variants(
            {"rule_spec": root, "slot": 0},
            {"primary_failure": "negative_expectancy"},
            count=4, vehicle="equity", llm_enabled=True,
            config={"model": "test", "near_duplicate_distance": 0.001},
            adapter=_TuningAdapter([near_root]), existing_specs=[])
        self.assertTrue(proposal.success)
        self.assertEqual(len(chosen), 4)
        self.assertEqual({item.source for item in chosen}, {"deterministic"})
        # The deterministic table remains the complete bounded search, even
        # though the model's one-step lookback alias was rejected.
        self.assertEqual([item for item, _reason in
                          mutate_with_reasons(root, {"primary_failure":
                          "negative_expectancy"}, 4)],
                         [item.rule_spec for item in chosen])


if __name__ == "__main__":
    unittest.main()
