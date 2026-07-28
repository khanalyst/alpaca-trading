"""B0: variant identity, and the fingerprint that must not drift.

Defect D4 in findings.md: the journal attributes results to a hash of the
whole configuration, so editing an alert timeout forks the attribution bucket
and splits a sample that is already too small to reject anything. The hash is
also opaque - sixteen hex characters that say nothing about what differed.

Both halves are tested here. A variant has a readable name and a stated
claim; the fingerprint that decides whether two runs may be pooled covers
only the blocks that can change a decision.
"""

import unittest

from agent import state, variants
from agent.config import ConfigError
from tests.helpers import valid_config


class StrategyFingerprintTests(unittest.TestCase):
    """What may be pooled, and what may not."""

    def test_irrelevant_edits_do_not_fork_attribution(self):
        base = valid_config()
        before = state.strategy_fingerprint(base)

        for path, value in (
            (("alerts", "timeout_seconds"), 99),
            (("llm", "max_tokens"), 4000),
            (("llm", "model"), "some-other-model"),
            (("universe", "refresh_minutes"), 30),
        ):
            cfg = valid_config()
            block, key = path
            if block not in cfg or key not in cfg[block]:
                continue                     # field absent in this schema
            cfg[block][key] = value
            self.assertEqual(
                state.strategy_fingerprint(cfg), before,
                f"{block}.{key} must not change the strategy fingerprint")

    def test_behaviour_changing_edits_do_fork_attribution(self):
        base = valid_config()
        before = state.strategy_fingerprint(base)

        for block, key, value in (
            ("strategy", "fixed_reward_risk", 2.5),
            ("strategy", "min_stop_atr_multiple", 1.5),
            ("risk", "min_confidence", 0.5),
            ("risk", "max_net_direction_pct", 60),
        ):
            cfg = valid_config()
            if block not in cfg or key not in cfg[block]:
                continue
            cfg[block][key] = value
            self.assertNotEqual(
                state.strategy_fingerprint(cfg), before,
                f"{block}.{key} must change the strategy fingerprint")

    def test_signal_timeframes_are_material(self):
        cfg = valid_config()
        before = state.strategy_fingerprint(cfg)
        cfg["cycle"]["timeframes"] = ["5m", "1h", "4h"]
        self.assertNotEqual(state.strategy_fingerprint(cfg), before)

    def test_fingerprint_is_stable_across_calls(self):
        self.assertEqual(
            state.strategy_fingerprint(valid_config()),
            state.strategy_fingerprint(valid_config()),
        )

    def test_whole_config_fingerprint_still_exists(self):
        """The new value is an addition; existing rows keep their meaning."""
        cfg = valid_config()
        self.assertNotEqual(
            state.stable_fingerprint(cfg), state.strategy_fingerprint(cfg))


class VariantRegistrationTests(unittest.TestCase):
    def test_a_variant_requires_a_stated_hypothesis(self):
        with self.assertRaises(ConfigError):
            variants.Variant(
                variant_id="momentum.rr.fixed_2_5", strategy_id="momentum",
                base_version="phase1-v2",
                overrides={"strategy.fixed_reward_risk": 2.5},
                hypothesis="")

    def test_variant_id_must_be_readable(self):
        for bad in ("Momentum.RR", "momentum rr", "", "momentum..rr"):
            with self.assertRaises(ConfigError, msg=bad):
                variants.Variant(
                    variant_id=bad, strategy_id="momentum",
                    base_version="phase1-v2",
                    hypothesis="a claim long enough to count as a sentence")

    def test_unknown_status_is_refused(self):
        with self.assertRaises(ConfigError):
            variants.Variant(
                variant_id="momentum.x", strategy_id="momentum",
                base_version="phase1-v2", status="maybe",
                hypothesis="a claim long enough to count as a sentence")


class VariantApplicationTests(unittest.TestCase):
    def test_overrides_are_applied_and_validated(self):
        variant = variants.Variant(
            variant_id="momentum.rr.fixed_2_5", strategy_id="momentum",
            base_version="phase1-v2",
            overrides={"strategy.fixed_reward_risk": 2.5},
            hypothesis="A 2.5R fixed target outperforms the default 2.0R.")

        cfg = variants.apply(variant, valid_config())

        self.assertEqual(cfg["strategy"]["fixed_reward_risk"], 2.5)

    def test_an_override_that_violates_bounds_raises_at_registration(self):
        variant = variants.Variant(
            variant_id="momentum.rr.absurd", strategy_id="momentum",
            base_version="phase1-v2",
            overrides={"strategy.fixed_reward_risk": 9999},
            hypothesis="An implausible target, to prove bounds are enforced.")

        with self.assertRaises(ConfigError):
            variants.apply(variant, valid_config())

    def test_a_typo_in_an_override_path_is_refused(self):
        """The failure this prevents is a week of replay reporting nothing."""
        variant = variants.Variant(
            variant_id="momentum.typo", strategy_id="momentum",
            base_version="phase1-v2",
            overrides={"strategy.fixed_reward_ratio": 2.5},   # not _risk
            hypothesis="A misspelled path must fail loudly, not silently.")

        with self.assertRaises(ConfigError) as caught:
            variants.apply(variant, valid_config())
        self.assertIn("does not exist", str(caught.exception))

    def test_apply_does_not_mutate_the_base_config(self):
        base = valid_config()
        original = base["strategy"]["fixed_reward_risk"]
        variant = variants.Variant(
            variant_id="momentum.rr.fixed_2_5", strategy_id="momentum",
            base_version="phase1-v2",
            overrides={"strategy.fixed_reward_risk": 2.5},
            hypothesis="A 2.5R fixed target outperforms the default 2.0R.")

        variants.apply(variant, base)

        self.assertEqual(base["strategy"]["fixed_reward_risk"], original)

    def test_baseline_variant_applies_cleanly(self):
        cfg = variants.apply(
            variants.baseline("momentum", "phase1-v2"), valid_config())
        self.assertEqual(cfg["strategy"]["id"], "momentum")

    def test_variants_differing_only_in_overrides_fingerprint_differently(self):
        base = valid_config()
        a = variants.apply(variants.baseline("momentum", "phase1-v2"), base)
        b = variants.apply(variants.Variant(
            variant_id="momentum.rr.fixed_2_5", strategy_id="momentum",
            base_version="phase1-v2",
            overrides={"strategy.fixed_reward_risk": 2.5},
            hypothesis="A 2.5R fixed target outperforms the default 2.0R."),
            base)

        self.assertNotEqual(state.strategy_fingerprint(a),
                            state.strategy_fingerprint(b))


class RegistryFileTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = f"{self.dir.name}/variants.yaml"

    def test_round_trip_is_lossless(self):
        registry = {
            "momentum.rr.fixed_2_5": variants.Variant(
                variant_id="momentum.rr.fixed_2_5", strategy_id="momentum",
                base_version="phase1-v2",
                overrides={"strategy.fixed_reward_risk": 2.5},
                hypothesis="A 2.5R fixed target outperforms the default 2.0R.",
                status="testing"),
        }
        variants.save_registry(self.path, registry)

        self.assertEqual(variants.load_registry(self.path), registry)

    def test_a_missing_registry_is_empty_not_an_error(self):
        self.assertEqual(
            variants.load_registry(f"{self.dir.name}/absent.yaml"), {})

    def test_duplicate_ids_are_refused(self):
        from pathlib import Path
        Path(self.path).write_text(
            "variants:\n"
            "  - variant_id: momentum.a\n"
            "    strategy_id: momentum\n"
            "    base_version: phase1-v2\n"
            "    hypothesis: a claim long enough to count as a sentence\n"
            "  - variant_id: momentum.a\n"
            "    strategy_id: momentum\n"
            "    base_version: phase1-v2\n"
            "    hypothesis: a claim long enough to count as a sentence\n",
            encoding="utf-8")

        with self.assertRaises(ConfigError):
            variants.load_registry(self.path)

    def test_unknown_field_is_refused(self):
        from pathlib import Path
        Path(self.path).write_text(
            "variants:\n"
            "  - variant_id: momentum.a\n"
            "    strategy_id: momentum\n"
            "    base_version: phase1-v2\n"
            "    hypothesis: a claim long enough to count as a sentence\n"
            "    notes: this field does not exist\n",
            encoding="utf-8")

        with self.assertRaises(ConfigError):
            variants.load_registry(self.path)


class JournalContextTests(unittest.TestCase):
    def test_variant_id_is_an_accepted_context_field(self):
        try:
            state.set_journal_context(variant_id=variants.LIVE_VARIANT_ID)
            self.assertEqual(
                state.journal_context()["variant_id"], "live")
        finally:
            state.set_journal_context(variant_id=None)

    def test_strategy_config_version_is_accepted(self):
        try:
            state.set_journal_context(strategy_config_version="abc123")
            self.assertEqual(
                state.journal_context()["strategy_config_version"], "abc123")
        finally:
            state.set_journal_context(strategy_config_version=None)

    def test_an_unknown_context_field_is_still_refused(self):
        with self.assertRaises(ValueError):
            state.set_journal_context(variant="oops")


if __name__ == "__main__":
    unittest.main()
