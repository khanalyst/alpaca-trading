"""The executable depth ladder must never be discarded without a reason.

On the 2026-07-29..08-05 demo corpus the order-book fetch succeeded on every
attempt, and 6,184 of 8,727 ladders still reached the research lanes as
``null`` with ``book_observation_error`` unset. Six of seven strategies were
blocked on missing depth for 63-100% of their decisions as a result, and
nothing logged, alerted, or recorded why. These tests hold the two properties
that failure violated: a rejected level is always explained, and a partial
observation is never presented as a whole one.
"""

import unittest

from agent.engine import Engine


def ladder(n: int = 5, start: float = 100.0) -> list[list[float]]:
    """A well-formed descending bid ladder in [price, amount] pairs."""
    return [[start - i, 1.0 + i] for i in range(n)]


class LadderIsNeverSilentlyDiscardedTests(unittest.TestCase):
    def test_a_clean_ladder_survives_whole_and_reports_nothing(self):
        levels, reason = Engine._plain_levels(ladder(50))
        self.assertEqual(len(levels), 50)
        self.assertIsNone(reason)

    def test_every_rejection_names_the_level_and_the_value(self):
        cases = {
            "non-numeric price": [[100.0, 1.0], ["abc", 1.0]],
            "zero price": [[100.0, 1.0], [0.0, 1.0]],
            "negative price": [[100.0, 1.0], [-1.0, 1.0]],
            "negative amount": [[100.0, 1.0], [99.0, -1.0]],
            "infinite price": [[100.0, 1.0], [float("inf"), 1.0]],
            "short pair": [[100.0, 1.0], [99.0]],
            "not a pair": [[100.0, 1.0], "99"],
        }
        for label, raw in cases.items():
            with self.subTest(label):
                levels, reason = Engine._plain_levels(raw)
                self.assertIsNotNone(
                    reason, f"{label} was discarded with no reason recorded")
                self.assertIn("level 1", reason)
                self.assertIn("kept 1 of 2", reason)
                # The valid prefix is retained rather than thrown away.
                self.assertEqual(levels, [[100.0, 1.0]])

    def test_no_surviving_level_is_absent_depth_not_empty_depth(self):
        # ``missing_fields`` treats only None as absent. An empty list would
        # therefore satisfy a required-field check while carrying no depth at
        # all, so a ladder whose very first level is bad must read as absent.
        levels, reason = Engine._plain_levels([[0.0, 1.0], [99.0, 1.0]])
        self.assertIsNone(levels)
        self.assertIsNotNone(reason)
        self.assertIn("kept 0 of 2", reason)

    def test_absent_depth_still_fails_the_contract_required_field_check(self):
        from agent.forward_models import require_complete_contract

        model = require_complete_contract("scalp-maker")
        levels, _ = Engine._plain_levels([[0.0, 1.0]])
        row = {"_enrichment": {"book_bid_levels": levels}}
        self.assertIn("_enrichment.book_bid_levels", model.missing_fields(row))

    def test_a_non_list_is_reported_rather_than_returned_bare(self):
        for value in (None, 42, "levels", {"bid": 1}):
            with self.subTest(repr(value)):
                levels, reason = Engine._plain_levels(value)
                self.assertIsNone(levels)
                self.assertIsNotNone(reason)
                self.assertIn("not a list", reason)

    def test_zero_amount_levels_are_legitimate_and_kept(self):
        # A level can be quoted with no size; that is a real book state and
        # rejecting it would discard depth below it for no reason.
        levels, reason = Engine._plain_levels([[100.0, 0.0], [99.0, 2.0]])
        self.assertEqual(levels, [[100.0, 0.0], [99.0, 2.0]])
        self.assertIsNone(reason)

    def test_the_okx_ladder_shape_is_accepted_in_full(self):
        # exchange.book_state hands over [[float(price), float(amount)], ...]
        # built from OKX's [price, sz, liqSz, numOrders] strings.
        raw = [["64864.2", "2239.05", "0", "84"], ["64864.1", "12.0", "0", "3"]]
        pairs = [[float(level[0]), float(level[1])] for level in raw]
        levels, reason = Engine._plain_levels(pairs)
        self.assertEqual(levels, [[64864.2, 2239.05], [64864.1, 12.0]])
        self.assertIsNone(reason)

    def test_truncation_is_conservative_about_depth(self):
        # A truncated ladder must never claim more depth than was observed.
        full = ladder(20)
        full[10] = [-1.0, 1.0]
        levels, reason = Engine._plain_levels(full)
        self.assertEqual(len(levels), 10)
        self.assertLess(
            sum(amount for _, amount in levels),
            sum(1.0 + i for i in range(20)),
            "a shortened ladder must understate, never overstate, depth")
        self.assertIn("kept 10 of 20", reason)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
