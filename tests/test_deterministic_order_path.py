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
from pathlib import Path
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
        for mode in ("auto", "llm", "", "DETERMINISTIC ", "shadow-only",
                     "SHADOW_ONLY", None, 7):
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


class ShadowOnlyEmptiesTheOrderPathTests(unittest.TestCase):
    """No mechanism has earned the account, so none occupies it.

    Leaving a falsified strategy trading is not neutral: a sustained drawdown
    trips risk.max_drawdown_pct, which flattens and self-kills the process,
    ending the research collection every other lane depends on.
    """

    def test_a_strategy_with_no_analyst_prompt_may_still_be_configured(self):
        # Nothing is analysed and nothing is traded, so neither an analyst
        # prompt nor a complete forward contract is required of strategy.id.
        cfg = cfg_for("ls-ratio-fade", "shadow_only",
                      version="v1", signal_timeframe="1h")
        cfg["cycle"]["timeframes"] = ["1h", "4h"]
        validated = validate_config(cfg)
        self.assertEqual(validated["strategy"]["execution_mode"], "shadow_only")

    def test_the_mode_switch_reads_configuration(self):
        for mode, deterministic, shadow_only in (
                ("analyst", False, False),
                ("deterministic", True, False),
                ("shadow_only", False, True)):
            with self.subTest(mode):
                engine = Engine.__new__(Engine)
                engine.cfg = validate_config(cfg_for("momentum", mode))
                self.assertIs(engine._deterministic_order_path(), deterministic)
                self.assertIs(engine._shadow_only_order_path(), shadow_only)

    def test_the_shipped_configuration_never_runs_the_rejected_strategy(self):
        # Whatever occupies the order path, it must not be the one the
        # evidence rejected. momentum is T0_REJECTED and is the only strategy
        # the recorded corpus says something significant about.
        import yaml

        cfg = validate_config(yaml.safe_load(
            Path(__file__).resolve().parents[1].joinpath(
                "config.yaml").read_text(encoding="utf-8")))
        mode = cfg["strategy"]["execution_mode"]
        self.assertIn(mode, ("analyst", "deterministic", "shadow_only"))
        if mode != "shadow_only":
            self.assertNotEqual(cfg["strategy"]["id"], "momentum")


class DeterministicDecisionsComeFromTheContractTests(unittest.TestCase):
    def _engine(self, mode):
        engine = Engine.__new__(Engine)
        engine.cfg = validate_config(cfg_for("momentum", mode))
        engine.strategy_id = "momentum"
        return engine

    @staticmethod
    def _contract_fires():
        """Let every probe through the contract gate.

        These tests are about the budget and the refusal filter, not about
        what any particular contract decides; the contract's own firing is
        covered where the contract lives.
        """
        return unittest.mock.patch(
            "agent.engine.strategy.build_setup_plan",
            side_effect=lambda proposal, row, cfg, **kw: (dict(proposal), None))

    def test_the_mode_switch_reads_configuration(self):
        self.assertTrue(self._engine("deterministic")._deterministic_order_path())
        self.assertFalse(self._engine("analyst")._deterministic_order_path())

    def test_decisions_are_the_contract_output_and_never_an_llm_call(self):
        engine = self._engine("deterministic")
        engine.llm = Mock()
        snapshot = {"BTC/USDT:USDT": {}}
        with self._contract_fires(), unittest.mock.patch(
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
        with self._contract_fires(), unittest.mock.patch(
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
        with self._contract_fires(), unittest.mock.patch(
                "agent.engine.require_complete_contract") as contract:
            contract.return_value.deterministic_proposals.return_value = [
                {"action": "open", "symbol": f"S{i}", "direction": "long"}
                for i in range(10)
            ]
            self.assertEqual(
                len(engine._deterministic_decisions({}, max_new=2)), 2)
            self.assertEqual(
                len(engine._deterministic_decisions({}, max_new=0)), 0)

    def test_the_budget_counts_firing_setups_and_not_probes(self):
        """The contract decides before the budget does.

        ``deterministic_proposals`` emits a probe for every symbol and both
        directions and applies no contract at all - 50 of them on a
        25-symbol universe. Spending the new-position budget on the first
        few probes in symbol order would leave a strategy whose setups are
        anywhere else in the universe unable to open at all.
        """
        engine = self._engine("deterministic")
        probes = [{"action": "open", "symbol": f"S{i}", "direction": "long"}
                  for i in range(10)]

        def only_s7(proposal, row, cfg, **kw):
            if proposal.get("symbol") == "S7":
                return dict(proposal), None
            return None, "evidence contract is not met"

        with unittest.mock.patch(
                "agent.engine.strategy.build_setup_plan", side_effect=only_s7), \
                unittest.mock.patch(
                    "agent.engine.require_complete_contract") as contract:
            contract.return_value.deterministic_proposals.return_value = probes
            decisions = engine._deterministic_decisions({}, max_new=2)
        self.assertEqual([d["symbol"] for d in decisions], ["S7"])

    def test_a_setup_the_contract_refuses_never_becomes_an_order(self):
        engine = self._engine("deterministic")
        with unittest.mock.patch(
                "agent.engine.strategy.build_setup_plan",
                return_value=(None, "positioning_fade evidence contract is "
                                    "not met")), \
                unittest.mock.patch(
                    "agent.engine.require_complete_contract") as contract:
            contract.return_value.deterministic_proposals.return_value = [
                {"action": "open", "symbol": "A", "direction": "long"}]
            self.assertEqual(engine._deterministic_decisions({}, max_new=3), [])

    def test_confidence_is_not_fabricated_into_a_score(self):
        # The contract fired or it did not. A synthesised score would let the
        # min_confidence gate look like it was filtering something.
        engine = self._engine("deterministic")
        with self._contract_fires(), unittest.mock.patch(
                "agent.engine.require_complete_contract") as contract:
            contract.return_value.deterministic_proposals.return_value = [
                {"action": "open", "symbol": "A", "direction": "long"}]
            decisions = engine._deterministic_decisions({}, max_new=1)
        self.assertEqual(decisions[0]["confidence"], 1.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
