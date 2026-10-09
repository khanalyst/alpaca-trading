"""Entry admission must leave a complete bounded exit path."""

from dataclasses import replace
from datetime import time, timedelta
import unittest

from agent.contracts.rule import (
    RULE_SCHEMA_V4, RULE_SCHEMA_V6, RULE_SCHEMA_V7, validate_rule_spec,
)
from research.costs import ReplayPolicy
from research.factory_core import _simulate_trade

from tests.test_exit_contract import BAR_ONLY_POLICY, FLAT, RISING, SPEC, _bars


def _spec(schema=RULE_SCHEMA_V4, **overrides):
    versioned = ({"min_stop_distance_bps": 100.0}
                 if schema == RULE_SCHEMA_V7 else {})
    return validate_rule_spec({
        **SPEC,
        **versioned,
        "schema": schema,
        **overrides,
    })


def _delayed_bars(seconds: int):
    bars = _bars(RISING + FLAT)
    return [replace(
        bar,
        identity=replace(
            bar.identity,
            observed_at=bar.timestamp + timedelta(seconds=seconds),
        ),
    ) for bar in bars]


def _simulate(spec, *, bars=None, policy=BAR_ONLY_POLICY):
    return _simulate_trade(
        _bars(RISING + FLAT) if bars is None else bars,
        spec,
        [],
        "equity",
        policy=policy,
    )


class EntryDeadlineAdmissionTests(unittest.TestCase):
    def test_late_thesis_deadline_refuses_before_pricing(self):
        # Keep strict quote admission enabled: the deadline refusal must win
        # before the missing executable quote can be consulted.
        result = _simulate(
            _spec(exit_before_minutes=389), policy=ReplayPolicy())

        self.assertEqual(result["execution_disposition"], "refused")
        self.assertEqual(result["unpriced_reason"],
                         "entry_after_exit_deadline")
        self.assertEqual(result["reject_stage"], "entry_policy")
        detail = result["reject_detail"]
        self.assertEqual(detail["exit_deadline_reason"], "thesis_deadline")
        self.assertLessEqual(detail["exit_deadline_timestamp"],
                             detail["entry_timestamp"])
        self.assertNotIn("exit_timestamp", result)
        self.assertNotIn("net_pnl", result)

    def test_entry_exactly_at_thesis_deadline_is_refused(self):
        result = _simulate(_spec(exit_before_minutes=386))

        self.assertEqual(result["execution_disposition"], "refused")
        self.assertEqual(result["unpriced_reason"],
                         "entry_after_exit_deadline")
        detail = result["reject_detail"]
        self.assertEqual(detail["entry_timestamp"],
                         detail["exit_deadline_timestamp"])
        self.assertEqual(detail["exit_deadline_reason"], "thesis_deadline")

    def test_late_thesis_deadline_refuses_for_v6_and_v7(self):
        bars = _bars([100.0 + index * 0.01 for index in range(20)])
        for schema in (RULE_SCHEMA_V6, RULE_SCHEMA_V7):
            with self.subTest(schema=schema):
                result = _simulate(
                    _spec(schema=schema, exit_before_minutes=389),
                    bars=bars,
                    policy=ReplayPolicy(),
                )
                self.assertEqual(result["execution_disposition"], "refused")
                self.assertEqual(result["unpriced_reason"],
                                 "entry_after_exit_deadline")
                self.assertEqual(result["reject_stage"], "entry_policy")
                self.assertEqual(
                    result["reject_detail"]["exit_deadline_reason"],
                    "thesis_deadline")

    def test_no_full_post_entry_bar_fits_before_thesis_deadline(self):
        # A 70-second publication delay makes the decision/entry boundary
        # 14:34:10.  The first full bar ends at 14:36 while the thesis closes
        # at 14:35, so this is before pricing and cannot be scored.
        result = _simulate(
            _spec(exit_before_minutes=385), bars=_delayed_bars(70))

        self.assertEqual(result["execution_disposition"], "refused")
        self.assertEqual(result["unpriced_reason"],
                         "entry_bar_after_exit_deadline")
        self.assertEqual(result["reject_stage"], "entry_policy")
        detail = result["reject_detail"]
        self.assertLess(detail["entry_timestamp"],
                        detail["exit_deadline_timestamp"])
        self.assertGreater(detail["entry_bar_end_timestamp"],
                           detail["exit_deadline_timestamp"])

    def test_exact_first_bar_deadline_remains_admissible(self):
        result = _simulate(_spec(exit_before_minutes=385))

        self.assertEqual(result["execution_disposition"], "candidate")
        self.assertEqual(result["exit_reason"], "exit_before")
        self.assertEqual(result["canonical_exit_reason"], "thesis_deadline")

    def test_normal_max_hold_entry_remains_admissible(self):
        result = _simulate(SPEC)

        self.assertEqual(result["execution_disposition"], "candidate")
        self.assertEqual(result["canonical_exit_reason"], "max_hold")
        self.assertEqual(result["hold_exit_reason"], "time_expiry")

    def test_force_flat_at_first_bar_boundary_remains_admissible(self):
        # BASE is 09:30 New York; the entry is 09:34 and the first full bar
        # ends at the exact 09:35 force-flat boundary.
        result = _simulate(
            SPEC,
            policy=ReplayPolicy(
                strict_market_data=False, force_flat_time=time(9, 35)),
        )

        self.assertEqual(result["execution_disposition"], "candidate")
        self.assertEqual(result["canonical_exit_reason"],
                         "session_force_flat")
        self.assertEqual(result["hold_exit_reason"], "session_force_flat")


if __name__ == "__main__":
    unittest.main()
