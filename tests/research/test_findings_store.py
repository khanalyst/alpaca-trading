"""B5: the store, and the two things it must never do.

It must never lose the reasoning behind a rejection, and it must never
produce a different file from the same data. The first because a deleted
rejection means the same idea returns and consumes the same calendar time
again. The second because scorecards are committed, and a generator that
diffs on every run produces a diff nobody reads.
"""

import tempfile
import unittest
from pathlib import Path

from agent import variants as variant_mod
from research import findings as findings_mod
from research.replay import ReplayResult


def variant(variant_id="momentum.rr.fixed_2_5", status="testing",
            overrides=None):
    return variant_mod.Variant(
        variant_id=variant_id, strategy_id="momentum",
        base_version="phase1-v3",
        overrides=overrides if overrides is not None
        else {"strategy.fixed_reward_risk": 2.5},
        hypothesis="A 2.5R fixed target outperforms the default 2.0R.",
        status=status)


def result(cycles=1200):
    r = ReplayResult(variant_id="momentum.rr.fixed_2_5",
                     mode="recorded_llm", cycles=cycles)
    r.corpus_from_ts = 1_760_000_000.0
    r.corpus_to_ts = 1_760_400_000.0
    return r


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = findings_mod.FindingsStore(
            Path(self.dir.name) / "findings.db")


class RegistrationTests(StoreFixture):
    def test_a_variant_round_trips(self):
        self.store.register(variant())

        stored = self.store.variant("momentum.rr.fixed_2_5")

        self.assertEqual(stored["status"], "testing")
        self.assertIn("2.5R", stored["hypothesis"])

    def test_re_registering_preserves_the_creation_time(self):
        self.store.register(variant())
        created = self.store.variant("momentum.rr.fixed_2_5")["created_ts"]

        self.store.register(variant(status="rejected"))

        self.assertEqual(
            self.store.variant("momentum.rr.fixed_2_5")["created_ts"],
            created)

    def test_status_can_change_without_losing_the_row(self):
        self.store.register(variant())
        self.store.set_status("momentum.rr.fixed_2_5", "rejected")

        self.assertEqual(
            self.store.variant("momentum.rr.fixed_2_5")["status"],
            "rejected")


class AppendOnlyTests(StoreFixture):
    def test_a_rejected_variant_keeps_its_full_findings_log(self):
        """The question later is why it was rejected, and on what sample."""
        self.store.register(variant())
        self.store.add_finding(
            "momentum.rr.fixed_2_5", "observation",
            "Lower win rate, higher expectancy - consistent with a wider "
            "target.", ts=1.0)
        self.store.add_finding(
            "momentum.rr.fixed_2_5", "recommendation",
            "Do not promote: the delta is inside the MDE.", ts=2.0)

        self.store.set_status("momentum.rr.fixed_2_5", "rejected")

        log = self.store.findings_for("momentum.rr.fixed_2_5")
        self.assertEqual(len(log), 2)
        self.assertEqual([e["kind"] for e in log],
                         ["observation", "recommendation"])

    def test_the_store_exposes_no_way_to_delete_a_finding(self):
        public = [name for name in dir(findings_mod.FindingsStore)
                  if not name.startswith("_")]
        for name in public:
            self.assertNotIn("delete", name.lower())
            self.assertNotIn("remove", name.lower())

    def test_findings_are_ordered_oldest_first(self):
        self.store.register(variant())
        for i, text in enumerate(["first", "second", "third"]):
            self.store.add_finding("momentum.rr.fixed_2_5", "observation",
                                   text, ts=float(i))

        log = self.store.findings_for("momentum.rr.fixed_2_5")

        self.assertEqual([e["text"] for e in log],
                         ["first", "second", "third"])

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.add_finding("v", "vibes", "text")


class NullResultTests(StoreFixture):
    def test_an_insufficient_sample_is_recorded_as_a_finding(self):
        """Null results are rows. A programme recording only positives
        records only noise."""
        self.store.register(variant())
        self.store.add_finding(
            "momentum.rr.fixed_2_5", "decision",
            "INSUFFICIENT_SAMPLE at n=38. The question is open, not "
            "answered in the negative.", ts=1.0)

        card = findings_mod.scorecard(self.store, "momentum.rr.fixed_2_5")

        self.assertIn("INSUFFICIENT_SAMPLE", card)

    def test_a_variant_with_no_run_still_renders(self):
        self.store.register(variant())

        card = findings_mod.scorecard(self.store, "momentum.rr.fixed_2_5")

        self.assertIn("Registered but never run", card)
        self.assertIn("no result to report", card)


class ScorecardTests(StoreFixture):
    def _populate(self):
        self.store.register(variant())
        self.store.record_run("run-1", "momentum.rr.fixed_2_5", result())
        self.store.record_metrics("run-1", {
            "label": "momentum.rr.fixed_2_5", "n": 47,
            "expectancy_r": 0.24, "ci_low": -0.12, "ci_high": 0.61,
            "win_rate": 44.7, "profit_factor": 1.31, "total_r": 11.3,
            "max_drawdown_r": 4.2, "mde_r": 0.34, "verdict": "SCORED"})
        self.store.add_finding(
            "momentum.rr.fixed_2_5", "recommendation",
            "Continue to 100 round trips. Do not promote: delta is inside "
            "the MDE.", ts=1_760_000_000.0)

    def test_regeneration_is_idempotent(self):
        """Committed files must not diff when nothing changed."""
        self._populate()

        first = findings_mod.scorecard(self.store, "momentum.rr.fixed_2_5")
        second = findings_mod.scorecard(self.store, "momentum.rr.fixed_2_5")

        self.assertEqual(first, second)

    def test_the_scorecard_states_the_mde(self):
        self._populate()

        card = findings_mod.scorecard(self.store, "momentum.rr.fixed_2_5")

        self.assertIn("MDE at n=47", card)
        self.assertIn("undetectable", card)

    def test_the_scorecard_shows_the_interval_not_just_the_point(self):
        self._populate()

        card = findings_mod.scorecard(self.store, "momentum.rr.fixed_2_5")

        self.assertIn("-0.1200", card)
        self.assertIn("+0.6100", card)

    def test_the_scorecard_carries_the_hypothesis_and_overrides(self):
        self._populate()

        card = findings_mod.scorecard(self.store, "momentum.rr.fixed_2_5")

        self.assertIn("2.5R fixed target", card)
        self.assertIn("strategy.fixed_reward_risk = 2.5", card)

    def test_a_baseline_says_it_is_the_floor(self):
        self.store.register(variant("momentum.baseline", overrides={}))

        card = findings_mod.scorecard(self.store, "momentum.baseline")

        self.assertIn("comparison floor", card)

    def test_an_unregistered_variant_renders_rather_than_raising(self):
        card = findings_mod.scorecard(self.store, "momentum.nope")
        self.assertIn("Not registered", card)


class IndexTests(StoreFixture):
    def test_rejected_variants_appear_in_the_index(self):
        self.store.register(variant())
        self.store.register(variant("momentum.dead", status="rejected"))

        text = findings_mod.index(self.store)

        self.assertIn("momentum.dead", text)
        self.assertIn("rejected", text)

    def test_writing_scorecards_twice_produces_identical_files(self):
        self.store.register(variant())
        self.store.record_run("run-1", "momentum.rr.fixed_2_5", result())

        with tempfile.TemporaryDirectory() as out:
            findings_mod.write_scorecards(self.store, out)
            before = {p: p.read_text() for p in Path(out).rglob("*.md")}
            findings_mod.write_scorecards(self.store, out)
            after = {p: p.read_text() for p in Path(out).rglob("*.md")}

        self.assertEqual(before, after)

    def test_scorecards_land_under_their_strategy(self):
        self.store.register(variant())

        with tempfile.TemporaryDirectory() as out:
            findings_mod.write_scorecards(self.store, out)
            self.assertTrue(
                (Path(out) / "momentum" /
                 "momentum.rr.fixed_2_5.md").exists())
            self.assertTrue((Path(out) / "README.md").exists())


if __name__ == "__main__":
    unittest.main()


class IdempotentRegistrationTests(StoreFixture):
    """updated_ts reaches the committed index, so it must not churn."""

    def test_re_registering_identical_content_does_not_touch_updated_ts(self):
        self.store.register(variant())
        first = self.store.variant("momentum.rr.fixed_2_5")["updated_ts"]

        self.store.register(variant())

        self.assertEqual(
            self.store.variant("momentum.rr.fixed_2_5")["updated_ts"], first)

    def test_a_real_change_does_update_the_timestamp(self):
        self.store.register(variant())
        first = self.store.variant("momentum.rr.fixed_2_5")["updated_ts"]

        self.store.register(variant(status="rejected"))

        self.assertGreaterEqual(
            self.store.variant("momentum.rr.fixed_2_5")["updated_ts"], first)
        self.assertEqual(
            self.store.variant("momentum.rr.fixed_2_5")["status"], "rejected")

    def test_the_generated_index_is_stable_across_regenerations(self):
        from research import findings as fm
        self.store.register(variant())
        self.store.register(variant("momentum.baseline", overrides={}))

        first = fm.index(self.store)
        self.store.register(variant())          # a no-op re-registration
        second = fm.index(self.store)

        self.assertEqual(first, second)
