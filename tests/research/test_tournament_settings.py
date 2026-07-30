"""Every hypothesis is tested at more than one point, and says why.

A hypothesis used to be scored at exactly one parameterisation - whatever the
research contract's dataclass defaults happened to be - and the battery turned
that single point into a tier. `flush-fade` was rejected at `min_move_atr=1.5`
and nothing else was ever tried, while `agent/registry.py` recorded that the
mechanism remained plausible and only that parameterisation had failed.

These tests hold the fix in both directions: the committed pre-registrations
must declare a real set of settings, and the loader must refuse a set that
would score the same point twice or silently ignore a typo.
"""

import sys
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "research"))

import tournament  # noqa: E402
from agent.registry import REGISTRY  # noqa: E402


HYPOTHESES = REPO / "research" / "hypotheses"


def load(strategy_id):
    return yaml.safe_load(
        (HYPOTHESES / f"{strategy_id}.yaml").read_text(encoding="utf-8"))


def config():
    from agent.config import validate_config
    return validate_config(
        yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8")))


class CommittedPreRegistrationTests(unittest.TestCase):
    """The scored hypotheses must carry enough settings to be rejectable."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = config()
        cls.scored = sorted(tournament.CONTRACTS)

    def test_every_backtestable_hypothesis_declares_enough_settings(self):
        import gates

        for strategy_id in self.scored:
            with self.subTest(strategy=strategy_id):
                settings = tournament.axis_settings(
                    strategy_id, load(strategy_id), self.cfg)
                self.assertGreaterEqual(
                    len(settings), gates.MIN_SETTINGS_TO_REJECT,
                    f"{strategy_id} cannot be rejected as a hypothesis: it "
                    f"declares {len(settings)} parameterisation(s) and the "
                    f"battery needs {gates.MIN_SETTINGS_TO_REJECT} before it "
                    "may blame the mechanism rather than the threshold")

    def test_every_setting_changes_something(self):
        for strategy_id in self.scored:
            settings = tournament.axis_settings(
                strategy_id, load(strategy_id), self.cfg)
            registered = next(s for s in settings if s["registered"])
            for candidate in (s for s in settings if not s["registered"]):
                with self.subTest(strategy=strategy_id,
                                  setting=candidate["setting_id"]):
                    self.assertNotEqual(candidate["contract"],
                                        registered["contract"])

    def test_every_setting_states_a_claim(self):
        for strategy_id in self.scored:
            for candidate in tournament.axis_settings(
                    strategy_id, load(strategy_id), self.cfg):
                with self.subTest(strategy=strategy_id,
                                  setting=candidate["setting_id"]):
                    self.assertGreater(len(candidate["claim"]), 20)

    def test_the_declared_hypothesis_count_covers_the_settings(self):
        """Multiplicity is pre-registered, not discovered afterwards."""
        for strategy_id in self.scored:
            prereg = load(strategy_id)
            with self.subTest(strategy=strategy_id):
                self.assertGreaterEqual(
                    int(prereg["hypotheses_tested"]),
                    len(tournament.axis_settings(
                        strategy_id, prereg, self.cfg)),
                    f"{strategy_id}: hypotheses_tested must count at least "
                    "every parameterisation that will be scored")

    def test_unscored_hypotheses_need_no_settings(self):
        """A hypothesis blocked on data cannot be parameterised into evidence."""
        blocked = [s for s in REGISTRY if s not in tournament.CONTRACTS]
        self.assertTrue(blocked)
        for strategy_id in blocked:
            with self.subTest(strategy=strategy_id):
                self.assertFalse(load(strategy_id).get("backtestable", False))


class SettingsValidationTests(unittest.TestCase):
    """Fail closed: a typo must not register cleanly and score nothing."""

    def setUp(self):
        self.cfg = config()

    def settings(self, declared):
        return tournament.axis_settings(
            "flush-fade", {"settings": declared}, self.cfg)

    def valid(self, **params):
        return {"id": "candidate", "params": params,
                "claim": "a sufficiently long stated claim"}

    def test_an_absent_settings_block_scores_the_registered_point(self):
        settings = tournament.axis_settings("flush-fade", {}, self.cfg)

        self.assertEqual(len(settings), 1)
        self.assertTrue(settings[0]["registered"])

    def test_an_unknown_parameter_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.settings([{"id": "registered", "params": {},
                            "claim": "the registered point"},
                           self.valid(min_move_atrr=2.5)])

        self.assertIn("min_move_atrr", str(caught.exception))
        self.assertIn("Available:", str(caught.exception))

    def test_a_setting_without_a_claim_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.settings([{"id": "registered", "params": {},
                            "claim": "the registered point"},
                           {"id": "bare", "params": {"min_move_atr": 2.5}}])

        self.assertIn("grid point", str(caught.exception))

    def test_duplicate_parameters_are_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.settings([
                {"id": "registered", "params": {},
                 "claim": "the registered point"},
                {"id": "a", "params": {"min_move_atr": 2.5},
                 "claim": "a sufficiently long stated claim"},
                {"id": "b", "params": {"min_move_atr": 2.5},
                 "claim": "another sufficiently long stated claim"}])

        self.assertIn("duplicates", str(caught.exception))

    def test_duplicate_ids_are_refused(self):
        with self.assertRaises(ValueError):
            self.settings([
                {"id": "registered", "params": {},
                 "claim": "the registered point"},
                {"id": "same", "params": {"min_move_atr": 2.5},
                 "claim": "a sufficiently long stated claim"},
                {"id": "same", "params": {"min_move_atr": 3.5},
                 "claim": "a sufficiently long stated claim"}])

    def test_the_registered_point_is_mandatory(self):
        """Every set is compared against the parameterisation on record."""
        with self.assertRaises(ValueError) as caught:
            self.settings([
                {"id": "a", "params": {"min_move_atr": 2.5},
                 "claim": "a sufficiently long stated claim"},
                {"id": "b", "params": {"min_move_atr": 3.5},
                 "claim": "a sufficiently long stated claim"}])

        self.assertIn("exactly one setting must have empty params",
                      str(caught.exception))

    def test_two_registered_points_are_refused(self):
        with self.assertRaises(ValueError):
            self.settings([
                {"id": "a", "params": {}, "claim": "the registered point"},
                {"id": "b", "params": {}, "claim": "also the registered point"}])

    def test_an_unknown_field_in_the_yaml_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.settings([{"id": "registered", "params": {},
                            "claim": "the registered point",
                            "hypothesis": "wrong key name"}])

        self.assertIn("hypothesis", str(caught.exception))

    def test_a_setting_leaves_the_registered_contract_untouched(self):
        settings = self.settings([
            {"id": "registered", "params": {},
             "claim": "the registered point"},
            self.valid(min_move_atr=2.5)])
        registered, candidate = settings

        self.assertEqual(registered["contract"].min_move_atr, 1.5)
        self.assertEqual(candidate["contract"].min_move_atr, 2.5)
        # Everything the setting did not name must be identical, or the
        # comparison measures more than the parameter it claims to.
        self.assertEqual(registered["contract"].min_oi_drop_pct,
                         candidate["contract"].min_oi_drop_pct)


if __name__ == "__main__":
    unittest.main()
