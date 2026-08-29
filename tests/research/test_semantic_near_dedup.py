"""Semantic duplicate policy for model-authored research proposals."""

from dataclasses import asdict
import unittest

from agent.config import ConfigError, validate_config
from agent.contracts.rule import (RULE_SCHEMA_V2, RULE_SCHEMA_V3,
                                  rule_semantic_distance,
                                  rule_semantic_signature, rule_variant_id,
                                  rule_vehicle_executable, validate_rule_spec)
from research.factory_core import family_template, template_hypothesis, mutate_with_reasons
from research.llm_strategy import (DISCOVERY_SCHEMA, PROPOSAL_SCHEMA,
                                    TUNING_SCHEMA, ProposalResult)
from research.strategy_factory import (
    NEAR_DUPLICATE_DISTANCE, _behavior_distance, _behavior_signature, _llm_replacement,
    _seed_slot, _semantic_duplicate, _structurally_distinct,
    _tuned_variants, structure_signature,
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
    def test_all_eleven_equity_roots_have_stable_semantic_signatures(self):
        from research.factory_core import initial_hypotheses

        roots = initial_hypotheses(11, vehicle="equity")
        self.assertEqual(len(roots), 11)
        signatures = [rule_semantic_signature(item.rule_spec) for item in roots]
        self.assertTrue(all(signatures))
        self.assertEqual(len(set(signatures)), 11)

    def test_v1_and_noop_v2_are_semantic_equivalents(self):
        v1 = validate_rule_spec({"family": "momentum_continuation",
                                 "threshold_bps": 18.0})
        v2 = validate_rule_spec({**v1, "schema": RULE_SCHEMA_V2})
        self.assertEqual(rule_semantic_signature(v1), rule_semantic_signature(v2))
        self.assertEqual(rule_semantic_distance(v1, v2), 0.0)

    def test_v3_breakeven_is_nullable_noop_until_activated(self):
        v2 = validate_rule_spec({"schema": RULE_SCHEMA_V2,
                                 "family": "momentum_continuation"})
        neutral = validate_rule_spec({"schema": RULE_SCHEMA_V3,
                                      "family": "momentum_continuation"})
        active = validate_rule_spec({**neutral, "breakeven_r": 0.5})
        self.assertEqual(rule_semantic_signature(v2),
                         rule_semantic_signature(neutral))
        self.assertNotEqual(rule_semantic_signature(neutral),
                            rule_semantic_signature(active))
        self.assertGreater(rule_semantic_distance(neutral, active), 0.0)

    def test_v3_breakeven_numeric_motion_is_local_but_activation_is_distinct(self):
        neutral = validate_rule_spec({"schema": RULE_SCHEMA_V3,
                                      "family": "momentum_continuation"})
        half = validate_rule_spec({**neutral, "breakeven_r": 0.5})
        slightly_higher = validate_rule_spec({**neutral, "breakeven_r": 0.6})
        local = _behavior_distance(half, slightly_higher)
        activation = _behavior_distance(neutral, half)
        self.assertGreater(local, 0.0)
        self.assertLess(local, activation)

    def test_relative_numeric_change_is_visible_despite_broad_threshold_bound(self):
        root = validate_rule_spec({"family": "momentum_continuation",
                                   "threshold_bps": 5.0})
        changed = validate_rule_spec({**root, "threshold_bps": 6.0})
        self.assertGreater(rule_semantic_distance(root, changed), 0.001)

    def test_one_step_integer_lookback_remains_a_near_duplicate(self):
        root = validate_rule_spec({"family": "momentum_continuation",
                                   "lookback": 12})
        changed = validate_rule_spec({**root, "lookback": 13})
        self.assertLessEqual(rule_semantic_distance(root, changed), 0.001)

    def test_v3_is_not_executable_for_options(self):
        v3 = validate_rule_spec({"schema": RULE_SCHEMA_V3,
                                 "family": "momentum_continuation"})
        v2 = validate_rule_spec({"schema": RULE_SCHEMA_V2,
                                 "family": "momentum_continuation"})
        self.assertFalse(rule_vehicle_executable(v3, "option"))
        self.assertTrue(rule_vehicle_executable(v2, "option"))

    def test_legacy_confirmation_and_v2_confirmation_list_share_behavior_key(self):
        legacy = validate_rule_spec({"family": "momentum_continuation",
                                     "confirmation": "volume"})
        modern = validate_rule_spec({"schema": "rule-strategy.v2",
                                     "family": "momentum_continuation",
                                     "confirmation": "none",
                                     "confirmations": ["volume"]})
        self.assertEqual(_behavior_signature(legacy), _behavior_signature(modern))
        self.assertTrue(_semantic_duplicate(modern, [legacy], near_distance=0.0))

    def test_topology_alias_does_not_count_an_active_axis_against_same_prior(self):
        prior = validate_rule_spec({"family": "momentum_continuation",
                                    "threshold_bps": 23.5})
        alias = validate_rule_spec({"family": "momentum_continuation",
                                    "threshold_bps": 23.50001})
        self.assertEqual(structure_signature(alias, prior)["active_axes"], [])
        self.assertFalse(_structurally_distinct(alias, [prior]))

    def test_mixed_family_history_cannot_hide_same_family_alias(self):
        prior = validate_rule_spec({"family": "momentum_continuation",
                                    "threshold_bps": 23.5})
        alias = validate_rule_spec({"family": "momentum_continuation",
                                    "threshold_bps": 23.50001})
        other = validate_rule_spec({"family": "vwap_reversion"})
        self.assertFalse(_structurally_distinct(alias, [other, prior]))

    def test_deterministic_pool_near_duplicate_falls_back_without_rewriting_ids(self):
        root = validate_rule_spec({"family": "momentum_continuation",
                                   "max_hold_bars": 120})
        prior = validate_rule_spec({**root, "max_hold_bars": 121})
        chosen, _proposal = _tuned_variants(
            {"rule_spec": root, "slot": 0},
            {"primary_failure": "negative_expectancy"}, count=2,
            vehicle="equity", llm_enabled=False,
            config={"near_duplicate_distance": 0.01},
            adapter=None,
            existing_specs=[prior])
        self.assertTrue(chosen)
        self.assertNotEqual(chosen[0].rule_spec, prior)
        self.assertEqual(chosen[0].source, "deterministic")

    def test_broad_model_threshold_cannot_delete_deterministic_coordinates(self):
        root = validate_rule_spec(family_template("momentum_continuation"))
        expected = [spec for spec, _reason in mutate_with_reasons(
            root, {"primary_failure": "negative_expectancy"}, 4)]
        chosen, _proposal = _tuned_variants(
            {"rule_spec": root, "slot": 0},
            {"primary_failure": "negative_expectancy"}, count=4,
            vehicle="equity", llm_enabled=True,
            config={"model": "test", "near_duplicate_distance": 1.0},
            adapter=_TuningAdapter([]),
            existing_specs=[root])
        self.assertEqual([item.rule_spec for item in chosen], expected)
        self.assertEqual({item.source for item in chosen}, {"deterministic"})

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
