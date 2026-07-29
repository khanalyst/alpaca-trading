"""Shadow evaluation: what every registered contract would have done.

Two properties matter and are tested here. Shadow records must be attributed
to the strategy that produced them, not to whichever strategy happens to be
trading - otherwise the forward evidence is worthless. And shadow bookkeeping
must never be able to interrupt trading, because it exists to observe the
loop, not to participate in it.
"""

import json
import unittest
from unittest.mock import Mock, patch

from agent import registry
from agent.engine import Engine
from tests.helpers import valid_config


def symbol_snapshot(**overrides):
    """A snapshot row that satisfies the momentum continuation contract."""
    base = {
        "price": 100.0,
        "trend_15m": "up", "trend_1h": "up", "trend_4h": "up",
        "mom_1h_pct": 0.6, "mom_15m_pct": 0.3,
        "atr_1h_pct": 0.8, "atr_1h_ratio": 1.0,
        "ema20_1h_dist_pct": 0.2,
        "range_pos_pct": 60.0, "relative_volume_1h": 1.4,
        "funding_rate_pct": 0.005, "funding_interval_hours": 8,
        "funding_percentile_30": 50, "funding_samples_30": 30,
        "perp_index_basis_pct": 0.01, "open_interest_musd": 500.0,
        "fresh_breakout_long": False, "fresh_breakout_short": False,
        "price_stabilized_long": False, "price_stabilized_short": False,
        "swing_low_pct": 0.9, "swing_high_pct": 1.1,
        "signal_ts": 1_700_000_000_000,
    }
    base.update(overrides)
    return base


class ShadowConfigTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine.__new__(Engine)
        self.engine.cfg = valid_config()

    def test_active_strategy_is_evaluated_with_the_live_config(self):
        # Its shadow record must be exactly what the live contract saw, or
        # the comparison against what the model took is meaningless.
        spec = registry.spec_for("momentum")
        self.assertIs(self.engine._shadow_cfg(spec), self.engine.cfg)

    def test_inactive_strategy_uses_its_registered_parameters(self):
        spec = registry.spec_for("flush-fade")
        shadow = self.engine._shadow_cfg(spec)
        self.assertEqual(shadow["strategy"]["id"], "flush-fade")
        self.assertEqual(shadow["strategy"]["version"], "v1")
        # The live config is not mutated by building a shadow view of it.
        self.assertEqual(self.engine.cfg["strategy"]["id"], "momentum")

    def test_registered_contract_params_override_the_live_block(self):
        spec = registry.spec_for("momentum")
        params = spec.contract_params
        self.assertTrue(params, "momentum should declare shadow parameters")
        # Momentum is active here, so build a shadow view for a spec that is
        # not, and confirm the declared parameters win.
        other = registry.StrategySpec(
            id="example", version="v1",
            mechanism="a stated payer who cannot stop paying, at length",
            falsification="the effect vanishes under a placebo control test",
            tier="T1_HYPOTHESIS", signal_timeframe="15m",
            required_timeframes=("15m",), max_hold_hours_ceiling=24.0,
            execution_style="taker", setup_types=("thing",),
            contract_params={"hard_max_entry_extension_atr": 9.5})
        shadow = self.engine._shadow_cfg(other)
        self.assertEqual(
            shadow["strategy"]["hard_max_entry_extension_atr"], 9.5)


class ShadowRecordingTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine.__new__(Engine)
        self.engine.cfg = valid_config()
        self.engine._audit_json = staticmethod(json.dumps)

    @staticmethod
    def _events(log_event):
        return [(call.args[0], json.loads(call.args[1]), call.kwargs)
                for call in log_event.call_args_list]

    @patch("agent.engine.state.log_event")
    def test_a_firing_contract_is_recorded(self, log_event):
        snapshot = {"BTC/USDT:USDT": symbol_snapshot()}
        self.engine._record_shadow_decisions(snapshot)
        kinds = [kind for kind, _, _ in self._events(log_event)]
        self.assertIn("strategy_shadow_decision", kinds)
        self.assertIn("strategy_shadow_summary", kinds)

    @patch("agent.engine.state.log_event")
    def test_records_carry_their_own_strategy_attribution(self, log_event):
        # Every registered contract is evaluated, and each record must be
        # attributed to the strategy that produced it rather than to the one
        # that happens to be trading. Without this the forward evidence for
        # every strategy collapses into one indistinguishable pile.
        self.engine._record_shadow_decisions(
            {"BTC/USDT:USDT": symbol_snapshot()})
        seen = set()
        for _, _, kwargs in self._events(log_event):
            self.assertIsNotNone(kwargs.get("strategy_id"))
            self.assertIsNotNone(kwargs.get("strategy_version"))
            seen.add(kwargs["strategy_id"])
        self.assertIn("momentum", seen)
        self.assertGreater(len(seen), 1,
                           "more than one contract should be shadowed")
        for strategy_id in seen:
            self.assertIn(strategy_id, registry.REGISTRY)

    @patch("agent.engine.state.log_event")
    def test_summary_is_written_even_when_nothing_fires(self, log_event):
        # Without the denominator, a count of firings is not a rate.
        quiet = symbol_snapshot(trend_1h="down", trend_4h="down",
                                mom_1h_pct=0.0, mom_15m_pct=0.0)
        self.engine._record_shadow_decisions({"BTC/USDT:USDT": quiet})
        events = self._events(log_event)
        summaries = [(k, p) for k, p, _ in events
                     if k == "strategy_shadow_summary"]
        # One per implemented contract, always - the denominator is what
        # turns a count of firings into a rate.
        self.assertEqual(len(summaries),
                         len(registry.implemented_ids()))
        for _, payload in summaries:
            self.assertEqual(payload["instruments_scanned"], 1)

    @patch("agent.engine.state.log_event")
    def test_context_blocks_are_not_treated_as_instruments(self, log_event):
        snapshot = {
            "_market_context": {"regime": "trend_up"},
            "BTC/USDT:USDT": symbol_snapshot(),
        }
        self.engine._record_shadow_decisions(snapshot)
        summaries = [p for kind, p, _ in self._events(log_event)
                     if kind == "strategy_shadow_summary"]
        self.assertEqual(summaries[0]["instruments_scanned"], 1)

    @patch("agent.engine.state.log_event")
    def test_exactly_one_strategy_is_flagged_active(self, log_event):
        self.engine._record_shadow_decisions(
            {"BTC/USDT:USDT": symbol_snapshot()})
        active = [kwargs["strategy_id"]
                  for kind, payload, kwargs in self._events(log_event)
                  if (kind == "strategy_shadow_summary"
                      and payload["is_active"])]
        self.assertEqual(active, ["momentum"])

    @patch("agent.engine.log")
    @patch("agent.engine.state.log_event")
    def test_a_broken_contract_cannot_interrupt_trading(self, log_event,
                                                        logger):
        # Shadow bookkeeping observes the loop; it must never stop it.
        with patch.dict("agent.contracts.EVIDENCE_BUILDERS",
                        {"momentum": Mock(side_effect=RuntimeError("boom"))}):
            self.engine._record_shadow_decisions(
                {"BTC/USDT:USDT": symbol_snapshot()})
        logger.warning.assert_called()

    @patch("agent.engine.state.log_event")
    def test_a_nan_field_does_not_cost_the_whole_cycle(self, log_event):
        # The journal serializer runs with allow_nan=False. A missing
        # measurement recorded as null is recoverable; losing the cycle is
        # not, so NaN must degrade rather than raise.
        self.engine._audit_json = staticmethod(
            lambda payload: json.dumps(payload, allow_nan=False))
        snapshot = {"BTC/USDT:USDT": symbol_snapshot(
            swing_low_pct=float("nan"), price=float("inf"))}
        self.engine._record_shadow_decisions(snapshot)
        decisions = [p for kind, p, _ in self._events(log_event)
                     if kind == "strategy_shadow_decision"]
        self.assertTrue(decisions)
        self.assertIsNone(decisions[0]["swing_low_pct"])
        self.assertIsNone(decisions[0]["price"])

    @patch("agent.engine.state.log_event")
    def test_a_malformed_snapshot_row_is_skipped(self, log_event):
        snapshot = {"BTC/USDT:USDT": "not a dict"}
        self.engine._record_shadow_decisions(snapshot)
        summaries = [p for kind, p, _ in self._events(log_event)
                     if kind == "strategy_shadow_summary"]
        self.assertTrue(summaries)
        for payload in summaries:
            self.assertEqual(payload["instruments_scanned"], 0)


if __name__ == "__main__":
    unittest.main()
