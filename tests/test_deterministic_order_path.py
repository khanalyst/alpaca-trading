"""A promoted contract must trade the thing that earned the promotion.

A strategy earns its evidence in a shadow lane, where a deterministic contract
decides and no analyst is involved. Running it live under an analyst trades
something other than what was measured, so the evidence stops describing what
the account is doing. The deterministic order path removes the analyst instead
of removing the evidence.

It also unblocks the practical problem: `momentum` was the only strategy with
`analyst_ready=True`, so no other registered strategy could reach the order
path at all no matter what its evidence said.
"""

import copy
import unittest
from unittest.mock import Mock

from agent.config import ConfigError, validate_config
from agent.engine import Engine
from tests.helpers import valid_config


def cfg_for(strategy_id="momentum", mode="deterministic", **strategy):
    cfg = copy.deepcopy(valid_config())
    cfg["strategy"]["execution_mode"] = mode
    cfg["strategy"]["id"] = strategy_id
    cfg["strategy"].update(strategy)
    return cfg


class ConfigurationGatesTheModeTests(unittest.TestCase):
    def test_the_default_is_the_analyst_path(self):
        cfg = copy.deepcopy(valid_config())
        cfg["strategy"].pop("execution_mode", None)
        validated = validate_config(cfg)
        self.assertEqual(validated["strategy"]["execution_mode"], "analyst")

    def test_an_unknown_mode_is_refused(self):
        for mode in ("auto", "llm", "", "DETERMINISTIC ", None, 7):
            with self.subTest(repr(mode)):
                with self.assertRaises(ConfigError):
                    validate_config(cfg_for(mode=mode))

    def test_a_shadow_only_strategy_may_drive_the_order_path_deterministically(self):
        # This is the point. Under an analyst it is refused for having no
        # analyst prompt; its contract is complete, so it can trade itself.
        cfg = cfg_for("ls-ratio-fade", "deterministic",
                      version="v1", signal_timeframe="1h")
        cfg["cycle"]["timeframes"] = ["1h", "4h"]
        validated = validate_config(cfg)
        self.assertEqual(validated["strategy"]["id"], "ls-ratio-fade")

        cfg["strategy"]["execution_mode"] = "analyst"
        with self.assertRaises(ConfigError) as caught:
            validate_config(cfg)
        self.assertIn("no live contract implementation", str(caught.exception))

    def test_a_strategy_without_a_complete_contract_cannot_be_deterministic(self):
        # Refusing here is the whole safety property: the deterministic path
        # trades the contract, so an absent contract must not start.
        cfg = cfg_for("momentum", "deterministic")
        with unittest.mock.patch(
                "agent.config.require_complete_contract",
                side_effect=KeyError("no forward model")):
            with self.assertRaises(ConfigError) as caught:
                validate_config(cfg)
        self.assertIn("no complete forward contract", str(caught.exception))


class DeterministicDecisionsComeFromTheContractTests(unittest.TestCase):
    def _engine(self, mode):
        engine = Engine.__new__(Engine)
        engine.cfg = validate_config(cfg_for("momentum", mode))
        engine.strategy_id = "momentum"
        return engine

    def test_the_mode_switch_reads_configuration(self):
        self.assertTrue(self._engine("deterministic")._deterministic_order_path())
        self.assertFalse(self._engine("analyst")._deterministic_order_path())

    def test_decisions_are_the_contract_output_and_never_an_llm_call(self):
        engine = self._engine("deterministic")
        engine.llm = Mock()
        snapshot = {"BTC/USDT:USDT": {}}
        with unittest.mock.patch(
                "agent.engine.require_complete_contract") as contract:
            contract.return_value.deterministic_proposals.return_value = [
                {"action": "open", "symbol": "BTC/USDT:USDT",
                 "direction": "long", "setup_type": "trend_continuation"},
            ]
            decisions = engine._deterministic_decisions(snapshot, max_new=3)
        engine.llm.decide.assert_not_called()
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["proposal_source"],
                         "deterministic_contract")

    def test_a_refused_proposal_never_becomes_an_order(self):
        engine = self._engine("deterministic")
        with unittest.mock.patch(
                "agent.engine.require_complete_contract") as contract:
            contract.return_value.deterministic_proposals.return_value = [
                {"action": "open", "symbol": "A", "direction": "long",
                 "research_refusal_reason": "data missing: book_bid_levels"},
                {"action": "open", "symbol": "B", "direction": "long"},
            ]
            decisions = engine._deterministic_decisions({}, max_new=5)
        self.assertEqual([d["symbol"] for d in decisions], ["B"])

    def test_the_new_position_budget_is_respected(self):
        engine = self._engine("deterministic")
        with unittest.mock.patch(
                "agent.engine.require_complete_contract") as contract:
            contract.return_value.deterministic_proposals.return_value = [
                {"action": "open", "symbol": f"S{i}", "direction": "long"}
                for i in range(10)
            ]
            self.assertEqual(
                len(engine._deterministic_decisions({}, max_new=2)), 2)
            self.assertEqual(
                len(engine._deterministic_decisions({}, max_new=0)), 0)

    def test_confidence_is_not_fabricated_into_a_score(self):
        # The contract fired or it did not. A synthesised score would let the
        # min_confidence gate look like it was filtering something.
        engine = self._engine("deterministic")
        with unittest.mock.patch(
                "agent.engine.require_complete_contract") as contract:
            contract.return_value.deterministic_proposals.return_value = [
                {"action": "open", "symbol": "A", "direction": "long"}]
            decisions = engine._deterministic_decisions({}, max_new=1)
        self.assertEqual(decisions[0]["confidence"], 1.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
