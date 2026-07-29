"""B7: the shadow tier cannot trade, and cannot break the loop that can.

Both properties are asserted rather than trusted. "The evaluator has no
exchange" is a claim about the code as it is today; a test that walks the
object graph is a claim about the code as it will be after the next edit.

The second property matters more than it looks. Shadow evaluation exists to
observe the trading loop, so a shadow failure that stopped a cycle would be a
safety regression introduced by a research feature - strictly worse than not
having the feature at all.
"""

import json
import unittest
from unittest.mock import Mock, patch

from agent import shadow, state, variants
from agent.engine import Engine
from agent.exchange import Exchange
from tests.helpers import valid_config
from tests.research.test_enrichment_isolation import symbol_snapshot


def variant(variant_id="momentum.rr.fixed_2_5", overrides=None):
    return variants.Variant(
        variant_id=variant_id, strategy_id="momentum",
        base_version="phase1-v3",
        overrides=overrides if overrides is not None
        else {"strategy.fixed_reward_risk": 2.5},
        hypothesis="A 2.5R fixed target outperforms the default 2.0R.",
        status="candidate")


def snapshot():
    return {"BTC/USDT:USDT": symbol_snapshot(),
            "_market_context": {"benchmark": "BTC/USDT:USDT"}}


def portfolio():
    return {"equity_usdt": 10_000.0, "positions": [], "cooldowns": {},
            "active_trades": {}, "gross_notional": 0.0}


class TypeBoundaryTests(unittest.TestCase):
    """Isolation by construction, not by discipline."""

    def test_no_exchange_is_reachable_from_the_evaluator(self):
        evaluator = shadow.ShadowEvaluator([variant()], valid_config())

        seen, stack = set(), [evaluator]
        while stack:
            obj = stack.pop()
            if id(obj) in seen:
                continue
            seen.add(id(obj))
            self.assertNotIsInstance(
                obj, Exchange,
                "an Exchange is reachable from the shadow evaluator")
            values = (obj.values() if isinstance(obj, dict)
                      else obj if isinstance(obj, (list, tuple, set))
                      else getattr(obj, "__dict__", {}).values())
            for value in values:
                if isinstance(value, (dict, list, tuple, set)) or hasattr(
                        value, "__dict__"):
                    stack.append(value)

    def test_the_constructor_takes_no_exchange_parameter(self):
        import inspect
        parameters = set(
            inspect.signature(shadow.ShadowEvaluator.__init__).parameters)
        self.assertEqual(parameters,
                         {"self", "variants", "base_cfg", "budget_ms"})

    def test_evaluate_returns_records_and_touches_nothing(self):
        evaluator = shadow.ShadowEvaluator([variant()], valid_config())

        records = evaluator.evaluate(snapshot(), portfolio(), 1_760_000_000.0)

        self.assertTrue(records)
        for record in records:
            self.assertIsInstance(record, shadow.ShadowRecord)

    def test_variant_recomputes_cached_live_evidence(self):
        snap = snapshot()
        snap["BTC/USDT:USDT"]["setup_evidence"] = {
            "trend_continuation": {"long": False, "short": False}}
        evaluator = shadow.ShadowEvaluator([variant()], valid_config())

        with patch("agent.shadow.strategy.setup_evidence",
                   wraps=shadow.strategy.setup_evidence) as evidence:
            evaluator.evaluate(snap, portfolio(), 1_760_000_000.0)

        self.assertTrue(evidence.called)


class NoOpTests(unittest.TestCase):
    def test_an_absent_research_block_builds_nothing(self):
        cfg = valid_config()
        cfg.pop("research", None)
        self.assertIsNone(shadow.build(cfg, {}))

    def test_a_disabled_flag_builds_nothing(self):
        cfg = valid_config()
        cfg["research"] = {"shadow_enabled": False,
                           "shadow_variants": ["momentum.rr.fixed_2_5"]}
        self.assertIsNone(
            shadow.build(cfg, {"momentum.rr.fixed_2_5": variant()}))

    def test_an_empty_variant_list_builds_nothing(self):
        cfg = valid_config()
        cfg["research"] = {"shadow_enabled": True, "shadow_variants": []}
        self.assertIsNone(shadow.build(cfg, {}))

    def test_enabling_it_with_a_known_variant_builds_an_evaluator(self):
        cfg = valid_config()
        cfg["research"] = {"shadow_enabled": True,
                           "shadow_variants": ["momentum.rr.fixed_2_5"],
                           "shadow_budget_ms": 250}
        evaluator = shadow.build(
            cfg, {"momentum.rr.fixed_2_5": variant()})

        self.assertIsNotNone(evaluator)
        self.assertEqual(evaluator.variant_ids, ["momentum.rr.fixed_2_5"])


class ResilienceTests(unittest.TestCase):
    def test_an_invalid_variant_is_skipped_not_fatal(self):
        """One bad registry entry must not disable every other variant."""
        broken = variant("momentum.broken",
                         overrides={"strategy.does_not_exist": 1})

        evaluator = shadow.ShadowEvaluator(
            [broken, variant()], valid_config())

        self.assertEqual(evaluator.variant_ids, ["momentum.rr.fixed_2_5"])
        self.assertIn("momentum.broken", evaluator.registration_errors)

    def test_a_raising_variant_does_not_propagate(self):
        evaluator = shadow.ShadowEvaluator([variant()], valid_config())

        with patch("agent.shadow.strategy.build_setup_plan",
                   side_effect=RuntimeError("boom")):
            records = evaluator.evaluate(
                snapshot(), portfolio(), 1_760_000_000.0)

        self.assertTrue(records)
        self.assertIn("shadow error", records[0].reason)

    def test_a_non_variant_entry_is_refused_loudly(self):
        with self.assertRaises(TypeError):
            shadow.ShadowEvaluator([{"variant_id": "nope"}], valid_config())


class BudgetTests(unittest.TestCase):
    def test_an_exhausted_budget_stops_evaluation(self):
        evaluator = shadow.ShadowEvaluator(
            [variant(f"momentum.v{i}",
                     overrides={"strategy.fixed_reward_risk": 2.0})
             for i in range(8)],
            valid_config(), budget_ms=0.0001)

        records = evaluator.evaluate(snapshot(), portfolio(), 1.0)

        self.assertTrue(evaluator.last_budget.overran)
        self.assertLess(len(records), 8)

    def test_a_generous_budget_evaluates_everything(self):
        evaluator = shadow.ShadowEvaluator(
            [variant(), variant("momentum.rr.fixed_3_0",
                                overrides={"strategy.fixed_reward_risk": 3.0})],
            valid_config(), budget_ms=10_000)

        records = evaluator.evaluate(snapshot(), portfolio(), 1.0)

        self.assertFalse(evaluator.last_budget.overran)
        self.assertEqual({r.variant_id for r in records},
                         {"momentum.rr.fixed_2_5", "momentum.rr.fixed_3_0"})


class EngineHookTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine.__new__(Engine)
        self.engine.cfg = valid_config()
        self.engine._audit_json = staticmethod(json.dumps)
        self.engine.shadow = shadow.ShadowEvaluator(
            [variant()], valid_config())

    @patch("agent.engine.state.log_event")
    def test_records_are_journalled_as_variant_shadow_decisions(
            self, log_event):
        self.engine._run_shadow_variants(
            snapshot(), 10_000.0, [], {}, 0.0)

        kinds = [c.args[0] for c in log_event.call_args_list]
        self.assertTrue(kinds)
        self.assertEqual(set(kinds), {"variant_shadow_decision"})

    @patch("agent.engine.state.log_event")
    def test_every_record_carries_its_own_variant_attribution(self, log_event):
        self.engine._run_shadow_variants(
            snapshot(), 10_000.0, [], {}, 0.0)

        for call in log_event.call_args_list:
            self.assertEqual(call.kwargs.get("variant_id"),
                             "momentum.rr.fixed_2_5")

    @patch("agent.engine.state.log_event")
    def test_shadow_never_writes_a_loop_key(self, log_event):
        """It cannot corrupt trading state even when it is wrong."""
        self.engine._run_shadow_variants(
            snapshot(), 10_000.0, [], {}, 0.0)

        for call in log_event.call_args_list:
            payload = call.args[1]
            for key in state.LOOP_KEYS:
                self.assertNotIn(f'"{key}"', payload)

    @patch("agent.engine.state.log_event")
    def test_a_failing_evaluator_does_not_raise(self, log_event):
        self.engine.shadow = Mock()
        self.engine.shadow.evaluate.side_effect = RuntimeError("boom")

        # Must not raise: the cycle has already committed its decisions.
        self.engine._run_shadow_variants(snapshot(), 10_000.0, [], {}, 0.0)

        kinds = [c.args[0] for c in log_event.call_args_list]
        self.assertIn("shadow_failed", kinds)

    @patch("agent.engine.state.log_event")
    def test_no_evaluator_is_a_silent_no_op(self, log_event):
        self.engine.shadow = None

        self.engine._run_shadow_variants(snapshot(), 10_000.0, [], {}, 0.0)

        log_event.assert_not_called()

    @patch("agent.engine.state.log_event")
    def test_a_budget_overrun_is_journalled(self, log_event):
        self.engine.shadow = shadow.ShadowEvaluator(
            [variant(f"momentum.v{i}") for i in range(6)],
            valid_config(), budget_ms=0.0001)

        self.engine._run_shadow_variants(snapshot(), 10_000.0, [], {}, 0.0)

        kinds = [c.args[0] for c in log_event.call_args_list]
        self.assertIn("shadow_budget_overrun", kinds)


class ConfigValidationTests(unittest.TestCase):
    def test_an_unknown_research_key_is_refused(self):
        from agent.config import ConfigError, validate_config
        cfg = valid_config()
        cfg["research"] = {"shadow_enabled": False, "shadow_budget_ms": 500,
                           "surprise": True}
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_llm_variants_are_capped(self):
        """Each entry costs a full extra model call every cycle."""
        from agent.config import ConfigError, validate_config
        cfg = valid_config()
        cfg["research"] = {
            "shadow_enabled": True, "shadow_budget_ms": 500,
            "shadow_variants": [],
            "shadow_llm_variants": ["a", "b", "c"]}
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_variant_names_must_be_strings(self):
        from agent.config import ConfigError, validate_config
        cfg = valid_config()
        cfg["research"] = {"shadow_enabled": True, "shadow_budget_ms": 500,
                           "shadow_variants": [123]}
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_the_shipped_config_is_a_no_op(self):
        import yaml
        from agent.config import validate_config
        from pathlib import Path

        cfg = validate_config(
            yaml.safe_load(Path("config.yaml").read_text()))

        self.assertFalse(cfg["research"]["shadow_enabled"])
        self.assertEqual(cfg["research"]["shadow_llm_variants"], [])


if __name__ == "__main__":
    unittest.main()


class NonInterferenceTests(unittest.TestCase):
    """Trading decisions must be byte-identical with and without shadow."""

    def test_evaluation_does_not_mutate_the_snapshot(self):
        evaluator = shadow.ShadowEvaluator(
            [variant(), variant("momentum.rr.fixed_3_0",
                                overrides={"strategy.fixed_reward_risk": 3.0})],
            valid_config())
        snap = snapshot()
        before = json.dumps(snap, sort_keys=True, default=str)

        evaluator.evaluate(snap, portfolio(), 1_760_000_000.0)

        self.assertEqual(
            json.dumps(snap, sort_keys=True, default=str), before)

    def test_evaluation_does_not_mutate_the_portfolio(self):
        evaluator = shadow.ShadowEvaluator([variant()], valid_config())
        book = portfolio()
        before = json.dumps(book, sort_keys=True, default=str)

        evaluator.evaluate(snapshot(), book, 1_760_000_000.0)

        self.assertEqual(
            json.dumps(book, sort_keys=True, default=str), before)

    def test_the_trading_decision_is_identical_with_and_without_shadow(self):
        """The decision path is run twice; only shadow differs between them."""
        from agent import strategy
        from agent.risk import RiskEngine

        cfg = valid_config()
        snap = snapshot()
        decision = {
            "symbol": "BTC/USDT:USDT", "action": "open", "direction": "long",
            "setup_type": "trend_continuation", "confidence": 0.8,
            "invalidation_anchor": "structure", "exit_policy": "fixed_rr",
        }

        def decide():
            row = snap["BTC/USDT:USDT"]
            plan, why = strategy.build_setup_plan(decision, row, cfg)
            merged = dict(decision, **{
                "stop_loss_pct": (plan or {}).get("stop_loss_pct"),
                "take_profit_pct": (plan or {}).get("take_profit_pct")})
            sized, veto = RiskEngine(cfg).vet_open(
                merged, 10_000.0, [], snap, {}, 0.0, now=1_760_000_000.0)
            return json.dumps([plan, why, sized, veto],
                              sort_keys=True, default=str)

        without = decide()
        shadow.ShadowEvaluator(
            [variant()], valid_config()).evaluate(
                snap, portfolio(), 1_760_000_000.0)
        with_shadow = decide()

        self.assertEqual(without, with_shadow)

    def test_a_variant_config_does_not_leak_into_the_base(self):
        cfg = valid_config()
        original = cfg["strategy"]["fixed_reward_risk"]

        shadow.ShadowEvaluator([variant()], cfg).evaluate(
            snapshot(), portfolio(), 1.0)

        self.assertEqual(cfg["strategy"]["fixed_reward_risk"], original)
