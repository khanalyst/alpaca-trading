"""B9.2: decisions get their own cadence; safety keeps the fast one.

The trade this batch must NOT make is slowing the margin guard. Simply
raising ``interval_seconds`` to 900 would cut LLM spend by the same factor
and take the drawdown breaker, the daily-loss stop, reconciliation and the
max-hold force close from a 5-minute reaction time to a 15-minute one. That
is not an acceptable price for a cost saving, so the two cadences are
separated rather than merged.

The other thing worth guarding is the off state. A feature that is off must
be off: a gate defaulting to the housekeeping cadence would do nothing in the
ordinary case and would silently drop a decision whenever a cycle finished
marginally early.
"""

import unittest
from unittest.mock import patch

from agent.config import ConfigError, validate_config
from agent.engine import Engine
from tests.helpers import valid_config


class ConfigTests(unittest.TestCase):
    def test_the_field_is_optional(self):
        cfg = validate_config(valid_config())
        self.assertIsNone(cfg["cycle"].get("decision_interval_seconds"))

    def test_a_valid_split_is_accepted(self):
        raw = valid_config()
        raw["cycle"]["decision_interval_seconds"] = 900

        cfg = validate_config(raw)

        self.assertEqual(cfg["cycle"]["decision_interval_seconds"], 900)
        self.assertEqual(cfg["cycle"]["interval_seconds"], 300)

    def test_a_decision_cadence_below_housekeeping_is_refused(self):
        """It is a multiple of the housekeeping cadence, never a fraction."""
        raw = valid_config()
        raw["cycle"]["interval_seconds"] = 300
        raw["cycle"]["decision_interval_seconds"] = 120

        with self.assertRaises(ConfigError):
            validate_config(raw)

    def test_an_unknown_cycle_key_is_still_refused(self):
        raw = valid_config()
        raw["cycle"]["decision_interval"] = 900       # near-miss spelling
        with self.assertRaises(ConfigError):
            validate_config(raw)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine.__new__(Engine)
        self.engine.cfg = valid_config()
        self.engine._audit_json = staticmethod(lambda payload: "{}")

    def test_unconfigured_never_gates(self):
        """Off means off, not 'gated at the housekeeping cadence'."""
        for offset in (0.0, 0.1, 1.0, 299.0):
            self.assertTrue(self.engine._decision_due(1_000_000.0 + offset))

    @patch("agent.engine.state.log_event")
    def test_the_first_cycle_always_decides(self, _log):
        self.engine.cfg["cycle"]["decision_interval_seconds"] = 900
        self.assertTrue(self.engine._decision_due(1_000_000.0))

    @patch("agent.engine.state.log_event")
    def test_a_cycle_inside_the_interval_is_skipped(self, _log):
        self.engine.cfg["cycle"]["decision_interval_seconds"] = 900

        self.assertTrue(self.engine._decision_due(1_000_000.0))
        self.assertFalse(self.engine._decision_due(1_000_300.0))
        self.assertFalse(self.engine._decision_due(1_000_600.0))

    @patch("agent.engine.state.log_event")
    def test_a_cycle_past_the_interval_decides(self, _log):
        self.engine.cfg["cycle"]["decision_interval_seconds"] = 900

        self.engine._decision_due(1_000_000.0)

        self.assertTrue(self.engine._decision_due(1_000_900.0))

    @patch("agent.engine.state.log_event")
    def test_jitter_does_not_defer_a_whole_period(self, _log):
        """A cycle landing a few seconds early must still decide."""
        self.engine.cfg["cycle"]["decision_interval_seconds"] = 900

        self.engine._decision_due(1_000_000.0)

        # 890s: inside the nominal interval, within the tolerance.
        self.assertTrue(self.engine._decision_due(1_000_890.0))

    @patch("agent.engine.state.log_event")
    def test_a_skip_is_journalled_with_its_reason(self, log_event):
        self.engine.cfg["cycle"]["decision_interval_seconds"] = 900

        self.engine._decision_due(1_000_000.0)
        self.engine._decision_due(1_000_300.0)

        kinds = [c.args[0] for c in log_event.call_args_list]
        self.assertIn("decision_skipped", kinds)

    @patch("agent.engine.state.log_event")
    def test_a_decision_cycle_is_not_journalled_as_a_skip(self, log_event):
        self.engine.cfg["cycle"]["decision_interval_seconds"] = 900

        self.engine._decision_due(1_000_000.0)

        log_event.assert_not_called()

    @patch("agent.engine.state.log_event")
    def test_the_expected_call_reduction(self, _log):
        """300s housekeeping against a 900s decision cadence is 3x."""
        self.engine.cfg["cycle"]["decision_interval_seconds"] = 900
        decisions = sum(
            1 for i in range(30)
            if self.engine._decision_due(1_000_000.0 + i * 300))

        self.assertEqual(decisions, 10)


class SafetyCadenceTests(unittest.TestCase):
    """Everything that protects capital runs before the gate."""

    def test_the_gate_sits_after_every_guard_in_the_cycle_source(self):
        import inspect
        from agent import engine as engine_mod

        source = inspect.getsource(engine_mod.Engine.cycle)
        gate = source.index("_decision_due")

        # Each of these must appear earlier in cycle() than the gate, or the
        # batch has traded safety reaction time for LLM spend.
        for guard in ("_reconcile_positions",
                      "max_drawdown_pct",
                      "daily_loss_limit_pct",
                      "_manage_positions"):
            self.assertLess(
                source.index(guard), gate,
                f"{guard} must run before the decision gate")

    def test_the_llm_call_sits_after_the_gate(self):
        import inspect
        from agent import engine as engine_mod

        source = inspect.getsource(engine_mod.Engine.cycle)

        self.assertLess(source.index("_decision_due"),
                        source.index("self.llm.decide"))
        self.assertLess(source.index("_decision_due"),
                        source.index("market.market_snapshot"))


if __name__ == "__main__":
    unittest.main()
