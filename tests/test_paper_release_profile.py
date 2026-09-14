"""The opt-in demo profile selects one frozen arm without relaxing safety."""

from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import yaml

from agent.config import ConfigError, load_config, validate_config
from agent.paper_trial import build_descriptor, effective_config, resolve_catalog_arm
from research.diagnostic_shadow import build_diagnostic_cohort


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "deploy" / "paper-orb.config.json"
VARIANT = "rule.opening-range-breakout.0eb200d3136d80ee"


class PaperReleaseProfileTests(unittest.TestCase):
    def test_opt_in_profile_changes_only_named_trial_selection(self):
        base = json.loads((ROOT / "config.yaml").read_text())
        profile = json.loads(PROFILE.read_text())
        self.assertIs(base["research"]["paper_trial"]["enabled"], False)
        expected = deepcopy(base)
        expected["research"]["paper_trial"].update(
            enabled=True,
            trial_id="paper-orb-baseline-20260914-v1",
            variant_id=VARIANT,
        )
        self.assertEqual(profile, expected)

    def test_profile_resolves_one_existing_baseline_and_exact_rule(self):
        config = load_config(PROFILE)
        with patch.dict("os.environ", {"ALPACA_SHADOW_INCLUDE_IBR": "1"}):
            descriptor = build_descriptor(config)
        arm = resolve_catalog_arm(VARIANT)
        resolved = effective_config(config, descriptor)
        self.assertEqual(descriptor["variant_id"], VARIANT)
        self.assertEqual(descriptor["role"], "baseline")
        self.assertEqual(resolved["strategy"]["variant_id"], VARIANT)
        self.assertEqual(resolved["strategy"]["rule_spec"], arm["rule_spec"])
        for key in ("broker", "universe", "risk", "execution", "costs", "session"):
            self.assertEqual(resolved[key], config[key])
        self.assertIs(resolved["research"]["require_validated_variant"], True)
        self.assertIs(resolved["llm"]["enabled"], False)

    def test_profile_binds_full_31_arm_shadow_catalog(self):
        config = load_config(PROFILE)
        with patch.dict("os.environ", {"ALPACA_SHADOW_INCLUDE_IBR": "1"}):
            descriptor = build_descriptor(config)
        cohort = build_diagnostic_cohort(
            config, code_identity=descriptor["code_identity"], include_ibr=True)
        self.assertEqual(len(descriptor["diagnostic_candidate_ids"]), 31)
        self.assertEqual(descriptor["cohort_identity"], cohort["cohort_identity"])
        self.assertEqual(set(descriptor["diagnostic_candidate_ids"]),
                         {row["candidate_id"] for row in cohort["arms"]})

    def test_profile_remains_paper_only_with_independent_safety_gates(self):
        config = load_config(PROFILE)
        self.assertEqual(config["mode"], "paper")
        self.assertIs(config["broker"]["paper"], True)
        self.assertIs(config["broker"]["allow_live"], False)
        changes = (
            ("live",),
            ("research", "require_validated_variant", False),
            ("execution", "strict_market_data", False),
            ("llm", "enabled", True),
        )
        for change in changes:
            with self.subTest(change=change):
                invalid = deepcopy(config)
                if change == ("live",):
                    invalid["mode"] = "live"
                    invalid["broker"].update(paper=False, allow_live=True)
                else:
                    section, key, value = change
                    invalid[section][key] = value
                with self.assertRaises(ConfigError):
                    validate_config(invalid)

    def test_compose_keeps_default_disabled_config_and_one_shared_mode(self):
        compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
        self.assertEqual(compose["configs"]["agent_config"]["file"],
                         "${ALPACA_AGENT_CONFIG_FILE:-./config.yaml}")
        for service in ("trader", "watchdog", "research", "shadow"):
            with self.subTest(service=service):
                self.assertEqual(
                    compose["services"][service]["environment"]["ALPACA_SHADOW_INCLUDE_IBR"],
                    "${ALPACA_SHADOW_INCLUDE_IBR:-1}",
                )


if __name__ == "__main__":
    unittest.main()
