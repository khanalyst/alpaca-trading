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
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent import shadow, state, variants
from agent.engine import Engine
from agent.exchange import Exchange
from research import findings
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


def proposal(confidence=0.8):
    return {
        "symbol": "BTC/USDT:USDT", "action": "open", "direction": "long",
        "setup_type": "trend_continuation", "confidence": confidence,
        "invalidation_anchor": "structure", "exit_policy": "fixed_rr",
    }


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
        self.assertNotIn("exchange", parameters)

    def test_evaluate_returns_records_and_touches_nothing(self):
        evaluator = shadow.ShadowEvaluator([variant()], valid_config())

        records = evaluator.evaluate(
            snapshot(), portfolio(), 1_760_000_000.0,
            proposals=[proposal()])

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
            evaluator.evaluate(
                snap, portfolio(), 1_760_000_000.0,
                proposals=[proposal()])

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
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = valid_config()
        cfg["research"] = {"shadow_enabled": True, "shadow_variants": []}
        with patch.object(
                findings, "DEFAULT_STORE", Path(tmp.name) / "findings.db"):
            self.assertIsNone(shadow.build(cfg, {}))

    def test_enabling_it_with_a_known_variant_builds_an_evaluator(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = valid_config()
        cfg["research"] = {"shadow_enabled": True,
                           "shadow_variants": ["momentum.rr.fixed_2_5"],
                           "shadow_budget_ms": 250}
        evaluator = shadow.build(
            cfg, {"momentum.rr.fixed_2_5": variant()},
            store=findings.FindingsStore(Path(tmp.name) / "findings.db"))

        self.assertIsNotNone(evaluator)
        self.assertEqual(
            evaluator.variant_ids,
            ["momentum.baseline", "momentum.rr.fixed_2_5"])

    def test_enabled_runtime_without_a_store_uses_the_durable_default(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        durable = Path(tmp.name) / "persistent" / "findings.db"
        cfg = valid_config()
        cfg["research"] = {
            "shadow_enabled": True,
            "shadow_variants": ["momentum.rr.fixed_2_5"],
            "shadow_budget_ms": 0,
        }

        with patch.object(findings, "DEFAULT_STORE", durable):
            evaluator = shadow.build(
                cfg, {"momentum.rr.fixed_2_5": variant()})

        self.assertIsNotNone(evaluator)
        self.assertEqual(evaluator.store.path, durable)
        self.assertIsNone(evaluator._temporary_store)
        self.assertTrue(durable.exists())


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
                snapshot(), portfolio(), 1_760_000_000.0,
                proposals=[proposal()])

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

        records = evaluator.evaluate(
            snapshot(), portfolio(), 1.0, proposals=[proposal()])

        self.assertTrue(evaluator.last_budget.overran)
        self.assertLess(len(records), 8)

    def test_positive_budget_never_counts_a_partial_variant(self):
        evaluator = shadow.ShadowEvaluator(
            [variant(), variant("momentum.rr.fixed_3_0",
                                overrides={"strategy.fixed_reward_risk": 3.0})],
            valid_config(), budget_ms=1)

        with patch.object(
                shadow.ShadowBudget, "exhausted",
                side_effect=[False, True]):
            records = evaluator.evaluate(
                snapshot(), portfolio(), 1.0,
                proposals=[proposal(), proposal(confidence=0.9)])

        self.assertEqual(len(records), 2)
        self.assertEqual(len(evaluator.last_coverage["evaluated"]), 1)
        self.assertEqual(len(evaluator.last_coverage["skipped"]), 1)

    def test_a_generous_budget_evaluates_everything(self):
        evaluator = shadow.ShadowEvaluator(
            [variant(), variant("momentum.rr.fixed_3_0",
                                overrides={"strategy.fixed_reward_risk": 3.0})],
            valid_config(), budget_ms=10_000)

        records = evaluator.evaluate(
            snapshot(), portfolio(), 1.0, proposals=[proposal()])

        self.assertFalse(evaluator.last_budget.overran)
        self.assertEqual({r.variant_id for r in records},
                         {"momentum.rr.fixed_2_5", "momentum.rr.fixed_3_0"})


class RecordedProposalTests(unittest.TestCase):
    def test_confidence_variants_use_the_recorded_model_confidence(self):
        lower = variant(
            "momentum.conf.floor_0_55", {"risk.min_confidence": 0.55})
        baseline = variant("momentum.baseline", {})
        evaluator = shadow.ShadowEvaluator(
            [lower, baseline], valid_config(), budget_ms=0)

        records = evaluator.evaluate(
            snapshot(), portfolio(), 1_760_000_000.0,
            proposals=[proposal(confidence=0.60)])
        by_variant = {record.variant_id: record for record in records}

        self.assertEqual(by_variant[lower.variant_id].outcome, "proposed")
        self.assertEqual(by_variant[baseline.variant_id].outcome, "vetoed")
        self.assertEqual(
            by_variant[baseline.variant_id].reason,
            "confidence below floor")
        lower_decision = evaluator.store.paper_decisions_for(
            evaluator.scope_key, lower.variant_id)[0]
        baseline_decision = evaluator.store.paper_decisions_for(
            evaluator.scope_key, baseline.variant_id)[0]
        self.assertEqual(
            lower_decision["proposal_id"], baseline_decision["proposal_id"])
        self.assertEqual(lower_decision["decision_outcome"], "PROPOSED")
        self.assertEqual(baseline_decision["decision_outcome"], "VETOED")
        self.assertIsNotNone(lower_decision["paper_trade_id"])
        self.assertIsNone(baseline_decision["paper_trade_id"])

    def test_unlimited_budget_evaluates_every_variant(self):
        evaluator = shadow.ShadowEvaluator(
            [variant(f"momentum.v{i}",
                     overrides={"strategy.fixed_reward_risk": 2.0})
             for i in range(8)], valid_config(), budget_ms=0)

        evaluator.evaluate(
            snapshot(), portfolio(), 1.0, proposals=[proposal()])

        self.assertEqual(len(evaluator.last_coverage["evaluated"]), 8)

    def test_overdue_position_closes_at_last_mark_when_symbol_disappears(self):
        evaluator = shadow.ShadowEvaluator(
            [variant()], valid_config(), budget_ms=0)
        opened_at = 1_760_000_000.0
        evaluator.evaluate(
            snapshot(), portfolio(), opened_at, proposals=[proposal()])

        self.assertEqual(evaluator.held_symbols(), ["BTC/USDT:USDT"])
        evaluator.advance({}, now=opened_at + 48 * 3600 + 1)
        state = evaluator.store.paper_portfolio_state(
            evaluator.scope_key, variant().variant_id)
        trades = evaluator.store.paper_trades_for(
            evaluator.scope_key, variant().variant_id)

        self.assertEqual(state["positions"], [])
        self.assertEqual(trades[0]["status"], "CLOSED")
        self.assertEqual(trades[0]["result"], "timeout")
        self.assertEqual(trades[0]["exit_price"], trades[0]["entry_price"])


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
            snapshot(), 10_000.0, [], {}, 0.0,
            decisions=[proposal()])

        kinds = [c.args[0] for c in log_event.call_args_list]
        self.assertTrue(kinds)
        self.assertIn("variant_shadow_decision", kinds)
        self.assertIn("shadow_coverage", kinds)

    @patch("agent.engine.state.log_event")
    def test_every_record_carries_its_own_variant_attribution(self, log_event):
        self.engine._run_shadow_variants(
            snapshot(), 10_000.0, [], {}, 0.0,
            decisions=[proposal()])

        for call in log_event.call_args_list:
            if call.args[0] == "shadow_coverage":
                continue
            self.assertEqual(call.kwargs.get("variant_id"),
                             "momentum.rr.fixed_2_5")

    @patch("agent.engine.state.log_event")
    def test_shadow_never_writes_a_loop_key(self, log_event):
        """It cannot corrupt trading state even when it is wrong."""
        self.engine._run_shadow_variants(
            snapshot(), 10_000.0, [], {}, 0.0,
            decisions=[proposal()])

        for call in log_event.call_args_list:
            payload = call.args[1]
            for key in state.LOOP_KEYS:
                self.assertNotIn(f'"{key}"', payload)

    @patch("agent.engine.state.log_event")
    def test_a_failing_evaluator_does_not_raise(self, log_event):
        self.engine.shadow = Mock()
        self.engine.shadow.evaluate.side_effect = RuntimeError("boom")

        # Must not raise: the cycle has already committed its decisions.
        self.engine._run_shadow_variants(
            snapshot(), 10_000.0, [], {}, 0.0,
            decisions=[proposal()])

        kinds = [c.args[0] for c in log_event.call_args_list]
        self.assertIn("shadow_failed", kinds)

    @patch("agent.engine.state.log_event")
    def test_no_evaluator_is_a_silent_no_op(self, log_event):
        self.engine.shadow = None

        self.engine._run_shadow_variants(
            snapshot(), 10_000.0, [], {}, 0.0,
            decisions=[proposal()])

        log_event.assert_not_called()

    @patch("agent.engine.state.log_event")
    def test_a_budget_overrun_is_journalled(self, log_event):
        self.engine.shadow = shadow.ShadowEvaluator(
            [variant(f"momentum.v{i}") for i in range(6)],
            valid_config(), budget_ms=0.0001)

        self.engine._run_shadow_variants(
            snapshot(), 10_000.0, [], {}, 0.0,
            decisions=[proposal()])

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

    def test_removed_shadow_llm_variants_key_is_refused(self):
        from agent.config import ConfigError, validate_config
        cfg = valid_config()
        cfg["research"] = {
            "shadow_enabled": True, "shadow_budget_ms": 500,
            "shadow_variants": [],
            "shadow_llm_variants": []}
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_variant_names_must_be_strings(self):
        from agent.config import ConfigError, validate_config
        cfg = valid_config()
        cfg["research"] = {"shadow_enabled": True, "shadow_budget_ms": 500,
                           "shadow_variants": [123]}
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_the_shipped_config_rotates_deterministic_variants(self):
        import yaml
        from agent.config import validate_config
        from pathlib import Path

        cfg = validate_config(
            yaml.safe_load(Path("config.yaml").read_text()))

        self.assertTrue(cfg["research"]["shadow_enabled"])
        self.assertEqual(cfg["research"]["shadow_variants"], ["*"])
        self.assertEqual(cfg["research"]["shadow_budget_ms"], 0)
        self.assertEqual(cfg["research"]["experiment_min_duration_days"], 3)
        self.assertEqual(cfg["research"]["experiment_min_observations"], 100)
        self.assertNotIn("shadow_llm_variants", cfg["research"])


if __name__ == "__main__":
    unittest.main()


class SymbolVisibilityTests(unittest.TestCase):
    """A simulated holding must not become a live candidate.

    Variant accounts hold positions the exchange account does not, and those
    symbols are fetched so each account can mark its own book. The channel
    that opens is not the mark - it is everything downstream that reads the
    symbol map: the model gets an extra candidate, and ``_market_context``
    counts an extra firing instrument into the breadth veto. Both would let a
    position that exists only in a database change one that exists on OKX.
    """

    @staticmethod
    def _wide():
        """A snapshot holding one live symbol and one shadow-only symbol.

        Both rows carry a firing contract, so breadth counts two while only
        one is live-eligible - which is the situation the veto reads wrong.
        """
        from agent import market
        firing = {"trend_continuation": {"long": True, "short": False}}
        snap = {
            "BTC/USDT:USDT": symbol_snapshot(setup_evidence=firing),
            "DOGE/USDT:USDT": symbol_snapshot(setup_evidence=firing),
            "_market_context": {"benchmark": "BTC/USDT:USDT"},
        }
        snap["_market_context"].update(market._setup_crowding(snap))
        return snap

    def test_a_shadow_only_symbol_is_withheld_from_the_live_view(self):
        from agent import market

        view = market.restrict_snapshot(self._wide(), ["BTC/USDT:USDT"])

        self.assertIn("BTC/USDT:USDT", view)
        self.assertNotIn("DOGE/USDT:USDT", view)

    def test_breadth_is_recomputed_so_it_describes_only_what_is_visible(self):
        from agent import market

        wide = self._wide()
        view = market.restrict_snapshot(wide, ["BTC/USDT:USDT"])

        # Both rows satisfy the continuation contract, so the wide snapshot
        # counts two. Slicing the map without recomputing would leave the
        # live path reading 2 while able to see 1.
        self.assertEqual(
            wide["_market_context"]["instruments_with_a_valid_setup"], 2)
        self.assertEqual(
            view["_market_context"]["instruments_with_a_valid_setup"], 1)
        self.assertEqual(view["_market_context"]["instruments_scanned"], 1)
        self.assertEqual(view["_market_context"]["benchmark"],
                         "BTC/USDT:USDT")

    def test_the_wide_snapshot_is_left_untouched(self):
        from agent import market

        wide = self._wide()
        before = json.dumps(wide, sort_keys=True, default=str)

        market.restrict_snapshot(wide, ["BTC/USDT:USDT"])

        self.assertEqual(
            json.dumps(wide, sort_keys=True, default=str), before)

    def test_a_snapshot_with_nothing_to_withhold_is_returned_as_is(self):
        from agent import market

        wide = self._wide()

        view = market.restrict_snapshot(
            wide, ["BTC/USDT:USDT", "DOGE/USDT:USDT"])

        self.assertIs(view, wide)

    def test_an_empty_snapshot_survives_restriction(self):
        from agent import market

        self.assertEqual(market.restrict_snapshot({}, ["BTC/USDT:USDT"]), {})

    def test_the_cycle_asks_the_model_only_about_live_symbols(self):
        """End to end: a shadow holding never reaches the LLM or the veto."""
        from agent import market

        engine = Engine.__new__(Engine)
        engine.cfg = valid_config()
        engine.ex = Mock()
        engine.universe = ["BTC/USDT:USDT"]
        engine.shadow = Mock()
        engine.shadow.held_symbols.return_value = ["DOGE/USDT:USDT"]

        # What the fetch layer returns for the union of both symbol sets.
        wide = self._wide()
        seen = {}

        def fake_snapshot(ex, symbols, cfg):
            seen["fetched"] = list(symbols)
            return wide

        with patch.object(market, "market_snapshot", fake_snapshot):
            fetched = market.market_snapshot(
                engine.ex,
                list(dict.fromkeys(engine.universe
                                   + engine.shadow.held_symbols())),
                engine.cfg)
        live_view = market.restrict_snapshot(fetched, engine.universe)

        # The shadow symbol is fetched, so its account can mark its position.
        self.assertIn("DOGE/USDT:USDT", seen["fetched"])
        self.assertIn("DOGE/USDT:USDT", fetched)
        # And it is invisible to everything the live path reads.
        self.assertNotIn("DOGE/USDT:USDT", live_view)
        self.assertEqual(
            live_view["_market_context"]["instruments_with_a_valid_setup"], 1)

    def test_the_engine_routes_the_restricted_view_to_the_live_path(self):
        """Guards the wiring: a future edit must not pass the wide map on."""
        import inspect

        from agent import engine as engine_mod

        source = inspect.getsource(engine_mod.Engine.cycle)
        for call in ("self.llm.decide(live_snapshot",
                     "self._journal_llm_input(live_snapshot",
                     "self._record_shadow_decisions(live_snapshot",
                     "self._record_observations(live_snapshot"):
            self.assertIn(call, source, f"{call} is no longer restricted")
        # The variant accounts still need the wide map to mark their books.
        self.assertIn("self._advance_shadow_variants(snapshot", source)


class NonInterferenceTests(unittest.TestCase):
    """Trading decisions must be byte-identical with and without shadow."""

    def test_evaluation_does_not_mutate_the_snapshot(self):
        evaluator = shadow.ShadowEvaluator(
            [variant(), variant("momentum.rr.fixed_3_0",
                                overrides={"strategy.fixed_reward_risk": 3.0})],
            valid_config())
        snap = snapshot()
        before = json.dumps(snap, sort_keys=True, default=str)

        evaluator.evaluate(
            snap, portfolio(), 1_760_000_000.0, proposals=[proposal()])

        self.assertEqual(
            json.dumps(snap, sort_keys=True, default=str), before)

    def test_evaluation_does_not_mutate_the_portfolio(self):
        evaluator = shadow.ShadowEvaluator([variant()], valid_config())
        book = portfolio()
        before = json.dumps(book, sort_keys=True, default=str)

        evaluator.evaluate(
            snapshot(), book, 1_760_000_000.0, proposals=[proposal()])

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
                snap, portfolio(), 1_760_000_000.0,
                proposals=[proposal()])
        with_shadow = decide()

        self.assertEqual(without, with_shadow)

    def test_a_variant_config_does_not_leak_into_the_base(self):
        cfg = valid_config()
        original = cfg["strategy"]["fixed_reward_risk"]

        shadow.ShadowEvaluator([variant()], cfg).evaluate(
            snapshot(), portfolio(), 1.0, proposals=[proposal()])

        self.assertEqual(cfg["strategy"]["fixed_reward_risk"], original)
