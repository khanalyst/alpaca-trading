"""The exploratory engine, tested at last.

`research/edge_lab.py` produced every committed tier verdict in this
repository - three of them `T0_REJECTED` - and had no test coverage at all.
The authoritative journal path has `test_no_lookahead.py`,
`test_replay_determinism.py`, `test_replay_fidelity.py` and a hard G2 gate; the
path with two years of data behind it had `validate_features.py`, which needs a
downloaded dataset and therefore cannot run here. So the guarded path was the
one with no corpus and the unguarded one had already issued the verdicts.

The important test in this file is the lookahead test, and the way it asks the
question matters. It does not inspect the code for suspicious indexing: it
deletes the future and checks that nothing changed. A feature at bar *i* that
survives every bar after *i* being removed cannot have read one.

The second is reproduction. The numbers below are not predictions about
markets - the data is a seeded random walk - they are a fingerprint of the
engine's arithmetic, so a shifted index or a changed default shows up as a
failing assertion instead of as a slightly different report nobody diffs.
"""

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "research"))

from edge_lab import (COST_SCENARIOS, Contract,  # noqa: E402
                      FlushFadeContract, FundingCarryContract,
                      TrendMultidayContract, build_trades, load_dataset,
                      non_overlapping, summarize, universe_membership)
from tests.research.synthetic_market import truncated, write_dataset  # noqa: E402


BARS = 8000
KEEP = 5000


class DatasetFixture(unittest.TestCase):
    """One dataset per class: building it is the slow part, not using it."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.root = root / "full"
        write_dataset(cls.root, bars=BARS)
        cls.frames = load_dataset(cls.root, min_bars=BARS)
        cls.membership = universe_membership(cls.frames, top_n=10)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def trades(self, contract, cost="base", exit_policy="fixed_rr",
               frames=None, membership=None):
        return non_overlapping(build_trades(
            frames if frames is not None else self.frames,
            membership if membership is not None else self.membership,
            contract, COST_SCENARIOS[cost], exit_policy=exit_policy))


class LookaheadTests(DatasetFixture):
    """Delete the future; nothing about the past may move."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.short_root = Path(cls._tmp.name) / "truncated"
        cls.cutoff = truncated(cls.root, KEEP, cls.short_root)
        cls.short_frames = load_dataset(cls.short_root, min_bars=KEEP - 100)
        cls.short_membership = universe_membership(cls.short_frames, top_n=10)

    def test_the_fixture_actually_removed_something(self):
        """A truncation test on an untruncated dataset proves nothing."""
        self.assertEqual(sorted(self.short_frames), sorted(self.frames))
        for symbol, frame in self.short_frames.items():
            with self.subTest(symbol=symbol):
                self.assertLess(len(frame.df), len(self.frames[symbol].df))
                self.assertGreater(len(frame.df), KEEP - 200)

    def test_no_feature_reads_a_bar_that_had_not_happened(self):
        for symbol, short in self.short_frames.items():
            full = self.frames[symbol]
            rows = len(short.df)
            with self.subTest(symbol=symbol):
                pd.testing.assert_frame_equal(
                    short.df.reset_index(drop=True),
                    full.df.iloc[:rows].reset_index(drop=True),
                    check_dtype=True, check_exact=True)

    def test_the_universe_is_point_in_time(self):
        """Membership at bar i cannot depend on turnover after bar i."""
        for symbol, short in self.short_frames.items():
            rows = len(short.df)
            with self.subTest(symbol=symbol):
                self.assertEqual(
                    self.short_membership[symbol][:rows].tolist(),
                    self.membership[symbol][:rows].tolist())

    def test_a_trade_that_closed_before_the_cutoff_is_unchanged(self):
        """The simulator, not just the features."""
        for contract in (Contract(), FlushFadeContract()):
            name = contract.__class__.__name__
            full = self.trades(contract)
            short = self.trades(
                contract, frames=self.short_frames,
                membership=self.short_membership)
            settled = short[short["exit_ts"] <= self.cutoff]
            with self.subTest(contract=name):
                self.assertGreater(len(settled), 10,
                                   f"{name} closed too few trades to compare")
                merged = settled.merge(
                    full, on=["symbol", "entry_ts"], suffixes=("_short", "_full"))
                self.assertEqual(len(merged), len(settled),
                                 "a trade vanished when the future was removed")
                for column in ("entry_price", "stop_price", "take_price",
                               "exit_price", "net_pct", "r_multiple"):
                    self.assertTrue(
                        merged[f"{column}_short"].equals(
                            merged[f"{column}_full"]),
                        f"{name}: {column} changed when the future was removed")


class ReproductionTests(DatasetFixture):
    """A fingerprint of the engine's arithmetic on a fixed input.

    Update these numbers only when a change to the engine is intended, and say
    so in the commit. A test that is routinely re-baselined is a test that has
    stopped being one.
    """

    EXPECTED = {
        "momentum": (286, -0.11471614378295505),
        "flush-fade": (202, -0.09332887578440207),
        "trend-multiday": (107, 0.01011764084835285),
        "funding-carry": (48, 0.4980657493269698),
    }

    def contracts(self):
        return {
            "momentum": Contract(),
            "flush-fade": FlushFadeContract(),
            "trend-multiday": TrendMultidayContract(),
            "funding-carry": FundingCarryContract(),
        }

    def test_every_contract_reproduces_its_recorded_result(self):
        for name, contract in self.contracts().items():
            trades, expectancy = self.EXPECTED[name]
            scored = summarize(self.trades(contract))
            with self.subTest(contract=name):
                self.assertEqual(scored["trades"], trades)
                self.assertAlmostEqual(scored["expectancy_r"], expectancy,
                                       places=9)

    def test_costs_only_ever_reduce_expectancy(self):
        """Frictionless must be the optimistic bound, at every contract."""
        for name, contract in self.contracts().items():
            free = summarize(self.trades(contract, cost="frictionless"))
            charged = summarize(self.trades(contract, cost="stress"))
            with self.subTest(contract=name):
                self.assertGreater(free["expectancy_pct"],
                                   charged["expectancy_pct"])

    def test_the_run_is_deterministic(self):
        first = summarize(self.trades(Contract()))
        second = summarize(self.trades(Contract()))

        self.assertEqual(first, second)

    def test_non_overlapping_removes_concurrent_trades(self):
        raw = build_trades(self.frames, self.membership, Contract(),
                           COST_SCENARIOS["base"], exit_policy="fixed_rr")
        filtered = non_overlapping(raw)

        self.assertLessEqual(len(filtered), len(raw))
        for symbol, group in filtered.groupby("symbol"):
            ordered = group.sort_values("entry_ts")
            with self.subTest(symbol=symbol):
                self.assertTrue(
                    (ordered["entry_ts"].to_numpy()[1:]
                     >= ordered["exit_ts"].to_numpy()[:-1]).all())


class TournamentEndToEndTests(DatasetFixture):
    """The settings axis, driven the way the nightly run drives it."""

    def score(self, strategy_id):
        import tournament
        from agent.registry import spec_for

        return tournament.score_strategy(
            spec_for(strategy_id), self.frames, self.membership, None,
            "base", "fixed_rr")

    def test_a_hypothesis_is_scored_at_every_registered_setting(self):
        row = self.score("flush-fade")

        self.assertTrue(row["scored"])
        self.assertEqual(row["settings_tested"], 3)
        self.assertEqual(len(row["settings"]), 3)
        self.assertEqual(
            sum(1 for s in row["settings"] if s["registered"]), 1)

    def test_rejection_names_the_whole_set_rather_than_one_threshold(self):
        row = self.score("flush-fade")

        self.assertEqual(row["measured_tier"], "T0_REJECTED")
        self.assertIn("pre-registered parameterisations fail",
                      row["tier_reason"])

    def test_the_headline_stays_the_registered_parameterisation(self):
        """benchmark_check compares it; the best setting must not supply it."""
        row = self.score("flush-fade")
        registered = next(s for s in row["settings"] if s["registered"])

        self.assertEqual(row["headline"]["trades"], registered["trades"])

    def test_an_in_sample_hypothesis_is_capped_however_the_settings_score(self):
        row = self.score("funding-unwind")

        self.assertEqual(row["measured_tier"], "T1_HYPOTHESIS")
        self.assertIn("in-sample by construction", row["tier_reason"])


if __name__ == "__main__":
    unittest.main()
