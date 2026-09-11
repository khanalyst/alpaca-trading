"""Regression coverage for deterministic cross-family replacements."""

from dataclasses import asdict
import unittest

from agent.contracts.rule import RULE_FAMILIES
from research.factory_core import (
    _hypothesis_id, discovery_spec, replacement_hypothesis,
    template_hypothesis,
)


class FactoryReplacementTests(unittest.TestCase):
    def test_replacement_uses_the_rotated_familys_canonical_vehicle_template(self):
        not_before = "2026-09-12"
        for vehicle in ("equity", "option"):
            for failure, offset in (("negative_expectancy", 1),
                                    ("insufficient_signals", 2)):
                with self.subTest(vehicle=vehicle, failure=failure):
                    previous = template_hypothesis(0, vehicle=vehicle)
                    generation = previous.generation + 1
                    family = RULE_FAMILIES[
                        (RULE_FAMILIES.index(previous.family) + generation +
                         offset) % len(RULE_FAMILIES)]
                    expected = discovery_spec(
                        0, family=family, vehicle=vehicle)

                    replacement = replacement_hypothesis(
                        asdict(previous), {"primary_failure": failure},
                        max_generations=2, not_before=not_before)

                    self.assertIsNotNone(replacement)
                    assert replacement is not None
                    self.assertEqual(replacement.rule_spec, expected)
                    self.assertEqual(replacement.family, family)
                    self.assertEqual(replacement.slot, previous.slot)
                    self.assertEqual(replacement.generation, generation)
                    self.assertEqual(replacement.vehicle, vehicle)
                    self.assertEqual(
                        replacement.parent_hypothesis_id,
                        previous.hypothesis_id)
                    self.assertEqual(replacement.not_before, not_before)
                    self.assertEqual(
                        replacement.hypothesis_id,
                        _hypothesis_id(
                            vehicle, previous.slot, generation, expected))

    def test_replacement_still_honors_the_generation_cap(self):
        previous = template_hypothesis(0)

        self.assertIsNone(replacement_hypothesis(
            asdict(previous), {"primary_failure": "negative_expectancy"},
            max_generations=1))


if __name__ == "__main__":
    unittest.main()
