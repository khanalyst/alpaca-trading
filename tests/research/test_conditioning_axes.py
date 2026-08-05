"""A conditioning axis has to stay a conditioning axis.

``research/sweep.py`` prefers conditioning axes to parameter axes because they
partition trades that already exist instead of dividing a small sample further.
Two things make that true, and both are silent when they break.

The first is that the variant carries no config override. Give it one and it
becomes an ordinary parameter arm: the rotation schedules it, it occupies the
strategy's single candidate slot for ten days, and the cheap axis has quietly
bought the expensive one.

The second is that its buckets were fixed before any query. The registry states
the claim; ``research/sweeps/*.yaml`` states the boundaries. If the two drift
apart, the buckets that get scored are no longer the buckets that were
pre-registered, and nothing errors - the table still prints.
"""

import unittest
from pathlib import Path

import yaml

from agent import variants
from agent.config import validate_config
from agent.market import realised_vol_enrichment
from research import sweep
from research.protocol import MIN_AXIS_SETTINGS


REPO = Path(__file__).resolve().parents[2]
REGISTRY = variants.load_registry(REPO / "research" / "variants.yaml")
SWEEPS = REPO / "research" / "sweeps"


def conditioning_variants() -> list:
    """Registered variants that change no setting: the partition-only arms."""
    return [
        variant for variant in REGISTRY.values()
        if not variant.overrides and not variant.hypothesis_id
        and variant.variant_id != variants.baseline_variant_id(
            variant.strategy_id)
    ]


def condition_axes() -> list:
    """Every pre-registered conditioning spec, by its partition variable."""
    return [spec for spec in
            (sweep.load_spec(path) for path in sorted(SWEEPS.glob("*.yaml")))
            if spec.is_conditioning()]


class ConditioningAxesAreRegisteredTests(unittest.TestCase):
    def test_the_registry_carries_conditioning_axes_at_all(self):
        """Registering only parameter axes contradicts the file's own header."""
        self.assertTrue(
            conditioning_variants(),
            "research/variants.yaml registers no conditioning axis, so every "
            "live arm divides the sample")

    def test_each_names_a_preregistered_partition_and_its_buckets(self):
        specs = {spec.condition_axis.variable: spec for spec in condition_axes()}
        self.assertTrue(specs, "research/sweeps/ has no conditioning spec")

        for variant in conditioning_variants():
            with self.subTest(variant=variant.variant_id):
                named = [variable for variable in specs
                         if variable in variant.hypothesis]
                self.assertEqual(
                    len(named), 1,
                    f"{variant.variant_id} must name exactly one "
                    f"pre-registered partition variable; named {named}")
                axis = specs[named[0]].condition_axis
                for bucket in axis.buckets:
                    self.assertIn(
                        str(bucket.get("name")), variant.hypothesis,
                        f"{variant.variant_id} omits a pre-registered bucket")

    def test_each_conditions_on_a_field_that_reaches_a_decision(self):
        """``sweep.partition`` reads enrichment, not the recorded snapshot.

        A variable that only exists on the snapshot buckets nothing, and the
        arm reports an empty partition rather than an error.
        """
        recorded = set(realised_vol_enrichment([], utc_hour=0))
        for variant in conditioning_variants():
            with self.subTest(variant=variant.variant_id):
                self.assertTrue(
                    [field for field in recorded if field in variant.hypothesis],
                    f"{variant.variant_id} conditions on no recorded "
                    "enrichment field")


class ConditioningAxesCostNoArmTests(unittest.TestCase):
    def test_they_declare_no_setting_so_the_rotation_skips_them(self):
        """The whole point: one replay produces every bucket."""
        for variant in conditioning_variants():
            with self.subTest(variant=variant.variant_id):
                self.assertIsNone(
                    variants.declared_research_setting(variant),
                    f"{variant.variant_id} declares an executable setting, so "
                    "the shadow rotation would schedule it as a parameter arm")

    def test_the_selector_catalog_does_not_offer_them(self):
        catalog = variants.research_selection_catalog()
        offered = {item["variant_id"]
                   for items in catalog.values() for item in items}
        for variant in conditioning_variants():
            with self.subTest(variant=variant.variant_id):
                self.assertNotIn(variant.variant_id, offered)


class UniverseBreadthIsAnAxisTests(unittest.TestCase):
    """``universe.top_n`` was fixed at 10 and never asked a question."""

    def setUp(self):
        self.base = validate_config(
            yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8")))

    def test_the_override_machinery_accepts_a_universe_path(self):
        registered = [variant for variant in REGISTRY.values()
                      if "universe.top_n" in variant.overrides]
        self.assertTrue(registered, "universe.top_n is not registered as an axis")
        for variant in registered:
            with self.subTest(variant=variant.variant_id):
                applied = variants.apply(variant, self.base)
                self.assertEqual(applied["universe"]["top_n"],
                                 variant.overrides["universe.top_n"])

    def test_the_axis_has_enough_settings_to_conclude(self):
        settings = {self.base["universe"]["top_n"]}
        settings.update(
            variant.overrides["universe.top_n"] for variant in REGISTRY.values()
            if "universe.top_n" in variant.overrides)
        self.assertGreaterEqual(len(settings), MIN_AXIS_SETTINGS)


if __name__ == "__main__":
    unittest.main()
