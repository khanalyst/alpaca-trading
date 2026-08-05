"""B2: the outcome resolver, and the three ways it could invent an edge.

Every test in this file guards against a failure that produces *better*
numbers rather than an error. That is what makes them worth writing: a
resolver that crashes gets fixed on the first run, and a resolver that
quietly peeks one bar into the future gets published.
"""

import unittest

from research.outcomes import (CostModel, LookaheadError, SetupPlan, Outcome,
                               resolve)
from research.prices import Bar


SIGNAL_TS = 1_760_000_000_000          # a 15m bar open
BAR_MS = 900_000
FIRST_VISIBLE = SIGNAL_TS + BAR_MS


def bar(offset_minutes: int, high: float, low: float,
        close: float | None = None, open_: float = 100.0) -> Bar:
    ts = FIRST_VISIBLE + offset_minutes * 60_000
    return Bar(ts, open_, high, low, close if close is not None else close_of(
        high, low), 1000.0)


def close_of(high: float, low: float) -> float:
    return (high + low) / 2


def plan(direction: str = "long", **overrides) -> SetupPlan:
    base = dict(
        symbol="BTC/USDT:USDT", direction=direction, entry_price=100.0,
        stop_pct=2.0, take_pct=4.0, signal_ts=SIGNAL_TS,
        signal_timeframe="15m", spread_pct=0.02, funding_rate_pct=0.0,
        funding_interval_hours=8.0, taker_fee_pct_per_side=0.05)
    base.update(overrides)
    return SetupPlan(**base)


class NoLookaheadTests(unittest.TestCase):
    """The guarantee is enforced by raising, not by filtering."""

    def test_a_bar_at_the_signal_bar_open_raises(self):
        with self.assertRaises(LookaheadError):
            resolve(plan(), [Bar(SIGNAL_TS, 100.0, 100.5, 99.0, 99.5, 1000.0)])

    def test_a_bar_inside_the_signal_bar_raises(self):
        inside = Bar(SIGNAL_TS + BAR_MS - 60_000,
                     100.0, 100.5, 99.0, 99.5, 1000.0)
        with self.assertRaises(LookaheadError):
            resolve(plan(), [inside])

    def test_the_first_bar_after_the_signal_bar_is_allowed(self):
        first = Bar(FIRST_VISIBLE, 100.0, 100.5, 99.5, 100.0, 1000.0)
        outcome = resolve(plan(), [first])
        self.assertNotEqual(outcome.result, "no_data")

    def test_one_early_bar_among_valid_ones_still_raises(self):
        """Filtering it out silently would hide the bug that produced it."""
        bars = [Bar(SIGNAL_TS, 100.0, 100.5, 99.0, 99.5, 1000.0),
                bar(0, 101, 99)]
        with self.assertRaises(LookaheadError):
            resolve(plan(), bars)

    def test_the_floor_tracks_the_signal_timeframe(self):
        hourly = plan(signal_timeframe="1h")
        just_inside = Bar(SIGNAL_TS + 3_600_000 - 60_000,
                          100.0, 101.0, 99.0, 100.0, 1000.0)
        with self.assertRaises(LookaheadError):
            resolve(hourly, [just_inside])

    def test_pre_entry_minutes_after_signal_close_are_not_visible(self):
        entry_ts = FIRST_VISIBLE + 5 * 60_000 + 30_000
        before_entry = Bar(FIRST_VISIBLE + 5 * 60_000,
                           100.0, 105.0, 97.0, 100.0, 1000.0)

        with self.assertRaises(LookaheadError):
            resolve(plan(entry_ts=entry_ts), [before_entry])

        first_post_entry = Bar(FIRST_VISIBLE + 6 * 60_000,
                               100.0, 101.0, 99.0, 100.0, 1000.0)
        self.assertNotEqual(
            resolve(plan(entry_ts=entry_ts), [first_post_entry]).result,
            "no_data")

    def test_reversed_bars_are_rejected_instead_of_silently_sorted(self):
        bars = [bar(1, 101.0, 99.0), bar(0, 101.0, 99.0)]
        with self.assertRaises(LookaheadError):
            resolve(plan(), bars)


class GoldenPathTests(unittest.TestCase):
    """A hand-built price path with three known outcomes."""

    def test_stop_first_resolves_as_stop(self):
        # Long from 100, stop 98, target 104. Price goes down first.
        bars = [bar(0, 100.5, 99.0), bar(1, 99.5, 97.5), bar(2, 105.0, 97.0)]

        outcome = resolve(plan(), bars)

        self.assertEqual(outcome.result, "stop")
        self.assertEqual(outcome.exit_price, 98.0)
        self.assertEqual(outcome.bars_held, 2)
        self.assertFalse(outcome.tie_broken)

    def test_target_first_resolves_as_target(self):
        bars = [bar(0, 101.0, 99.5), bar(1, 104.5, 100.0),
                bar(2, 90.0, 89.0)]

        outcome = resolve(plan(), bars)

        self.assertEqual(outcome.result, "target")
        self.assertEqual(outcome.exit_price, 104.0)
        self.assertEqual(outcome.bars_held, 2)

    def test_a_same_bar_tie_resolves_as_stop(self):
        """The rule that stops a backtest inventing an edge."""
        bars = [bar(0, 104.5, 97.5)]        # spans both 98 and 104

        outcome = resolve(plan(), bars)

        self.assertEqual(outcome.result, "stop")
        self.assertTrue(outcome.tie_broken)
        self.assertEqual(outcome.exit_price, 98.0)

    def test_short_side_mirrors_exactly(self):
        # Short from 100, stop 102, target 96.
        stopped = resolve(plan("short"), [bar(0, 102.5, 99.0)])
        self.assertEqual(stopped.result, "stop")
        self.assertEqual(stopped.exit_price, 102.0)

        hit = resolve(plan("short"), [bar(0, 100.5, 95.5)])
        self.assertEqual(hit.result, "target")
        self.assertEqual(hit.exit_price, 96.0)

    def test_a_short_same_bar_tie_also_resolves_as_stop(self):
        outcome = resolve(plan("short"), [bar(0, 102.5, 95.5)])
        self.assertEqual(outcome.result, "stop")
        self.assertTrue(outcome.tie_broken)

    def test_no_bars_is_no_data_not_a_guess(self):
        self.assertEqual(resolve(plan(), []).result, "no_data")


class TimeoutTests(unittest.TestCase):
    def test_timeout_exits_at_that_bars_close(self):
        # Never reaches 98 or 104 within the hold window.
        bars = [bar(i, 101.0, 99.0, close=100.25) for i in range(90)]

        outcome = resolve(plan(), bars, max_hold_hours=1.0)

        self.assertEqual(outcome.result, "timeout")
        self.assertEqual(outcome.exit_price, 100.25)
        self.assertEqual(outcome.bars_held, 60)

    def test_bars_beyond_the_deadline_are_not_read(self):
        """A target hit after max_hold must not be credited."""
        bars = [bar(i, 101.0, 99.0, close=100.0) for i in range(60)]
        bars.append(bar(70, 110.0, 99.0))          # target, but too late

        outcome = resolve(plan(), bars, max_hold_hours=1.0)

        self.assertEqual(outcome.result, "timeout")


class ExcursionTests(unittest.TestCase):
    def test_mae_and_mfe_are_tracked_before_the_exit(self):
        bars = [bar(0, 101.0, 99.0), bar(1, 103.0, 98.5), bar(2, 104.5, 99.0)]

        outcome = resolve(plan(), bars)

        self.assertEqual(outcome.result, "target")
        self.assertAlmostEqual(outcome.mae_pct, 1.5, 6)     # low of 98.5
        self.assertAlmostEqual(outcome.mfe_pct, 4.5, 6)     # high of 104.5

    def test_mae_is_positive_for_a_short(self):
        bars = [bar(0, 101.5, 99.0), bar(1, 100.0, 95.5)]

        outcome = resolve(plan("short"), bars)

        self.assertAlmostEqual(outcome.mae_pct, 1.5, 6)     # high of 101.5


class CostModelTests(unittest.TestCase):
    """The R denominator must match what the live engine sized from."""

    def test_risk_pct_reproduces_estimated_loss_pct(self):
        # stop 2.0 + fees 2x0.05 + spread 0.02 + stop slip 0.15
        # + entry slip 0.35 + funding 0 = 2.62
        self.assertAlmostEqual(
            CostModel().risk_pct(plan(), funding_intervals=0), 2.62, 6)

    def test_funding_is_charged_to_the_paying_side_only(self):
        costs = CostModel()
        longer = plan("long", funding_rate_pct=0.01)
        shorter = plan("short", funding_rate_pct=0.01)

        self.assertAlmostEqual(
            costs.adverse_funding_pct(longer, 3), 0.03, 6)
        self.assertAlmostEqual(
            costs.adverse_funding_pct(shorter, 3), 0.0, 6)

    def test_a_negative_rate_is_a_cost_to_the_short(self):
        costs = CostModel()
        self.assertAlmostEqual(
            costs.adverse_funding_pct(
                plan("short", funding_rate_pct=-0.01), 2), 0.02, 6)

    def test_stop_slippage_applies_only_to_stop_exits(self):
        stopped = resolve(plan(), [bar(0, 100.5, 97.5)])
        won = resolve(plan(), [bar(0, 104.5, 99.5)])

        self.assertAlmostEqual(stopped.costs["slippage_pct"], 0.15, 6)
        self.assertAlmostEqual(won.costs["slippage_pct"], 0.0, 6)

    def test_r_multiple_uses_the_all_in_denominator(self):
        """Dividing by the bare stop would inflate every replayed variant."""
        outcome = resolve(plan(), [bar(0, 104.5, 99.5)])

        # gross +4.00, costs = fees 0.10 + spread 0.02 = 0.12, net 3.88
        self.assertAlmostEqual(outcome.gross_pct, 4.0, 6)
        self.assertAlmostEqual(outcome.net_pct, 3.88, 6)
        self.assertAlmostEqual(outcome.risk_pct, 2.62, 6)
        self.assertAlmostEqual(outcome.r_multiple, 3.88 / 2.62, 5)
        self.assertLess(outcome.r_multiple, 3.88 / 2.0,
                        "R must not be computed against the bare stop")

    def test_funding_accrues_only_on_completed_intervals(self):
        bars = [bar(i, 101.0, 99.0, close=100.0) for i in range(600)]
        outcome = resolve(
            plan(funding_rate_pct=0.01, funding_interval_hours=8.0),
            bars, max_hold_hours=9.0)

        self.assertEqual(outcome.result, "timeout")
        self.assertEqual(outcome.costs["funding_intervals"], 1)
        self.assertAlmostEqual(outcome.costs["funding_pct"], 0.01, 6)


class ValidationTests(unittest.TestCase):
    def test_an_unknown_direction_raises(self):
        with self.assertRaises(ValueError):
            resolve(plan(direction="sideways"), [bar(0, 101, 99)])

    def test_a_non_positive_stop_raises(self):
        with self.assertRaises(ValueError):
            resolve(plan(stop_pct=0.0), [bar(0, 101, 99)])


if __name__ == "__main__":
    unittest.main()
