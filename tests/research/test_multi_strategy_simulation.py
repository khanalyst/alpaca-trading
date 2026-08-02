"""End-to-end same-feed simulation for every registered strategy."""

import tempfile
import time
import unittest

from agent import shadow, variants
from agent.forward_models import BY_STRATEGY
from research.findings import FindingsStore
from tests.helpers import valid_config
from tests.research.test_forward_models import market_row


SYMBOL = "BTC/USDT:USDT"


def config():
    cfg = valid_config()
    cfg["research"] = {
        "shadow_enabled": True,
        "shadow_variants": ["*"],
        "shadow_budget_ms": 0,
        "shadow_workers": 1,
        "paper_initial_balance_usdt": 10_000,
        "paper_max_failures": 3,
        "paper_min_closed_trades": 100,
        "experiment_min_duration_days": 3,
        "experiment_min_observations": 1,
        "forward_feed_version": 2,
    }
    return cfg


def momentum_proposal():
    return {
        "action": "open", "symbol": SYMBOL, "direction": "long",
        "setup_type": "trend_continuation", "confidence": 0.9,
        "invalidation_anchor": "structure", "exit_policy": "fixed_rr",
        "execution_choice": "normal",
    }


def firing_row(strategy_id):
    if strategy_id == "flush-fade":
        return market_row(mom_1h_pct=-2.0, trend_1h="flat",
                          trend_4h="flat")
    if strategy_id in {"funding-carry", "funding-unwind"}:
        return market_row(trend_1h="flat", trend_4h="flat")
    return market_row()


class SameFeedCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = FindingsStore(f"{self.tmp.name}/findings.db")
        self.cfg = config()
        self.scope = "demo:all-strategy-test"

    def build(self):
        return shadow.build(
            self.cfg, {}, scope_key=self.scope, store=self.store)

    def test_all_seven_process_the_exact_same_snapshot_and_timestamp(self):
        coordinator = self.build()
        snapshot = {SYMBOL: market_row()}
        now = time.time()

        coordinator.evaluate(
            snapshot, now=now, cycle_id="same-feed",
            proposals=[momentum_proposal()])

        self.assertEqual(coordinator.strategy_ids, sorted(BY_STRATEGY))
        self.assertEqual(len(coordinator.last_cycle_by_strategy), 7)
        for strategy_id, observed in coordinator.last_cycle_by_strategy.items():
            with self.subTest(strategy=strategy_id):
                self.assertEqual(observed["timestamp"], now)
                self.assertEqual(observed["snapshot_identity"], id(snapshot))
                baseline = variants.baseline_variant_id(strategy_id)
                self.assertTrue(self.store.paper_decisions_for(
                    coordinator.scope_key, baseline))

    def test_strategy_accounts_cannot_contaminate_one_another(self):
        coordinator = self.build()
        now = time.time()
        coordinator.evaluate(
            {SYMBOL: market_row()}, now=now, cycle_id="isolation",
            proposals=[momentum_proposal()])
        momentum_id = variants.baseline_variant_id("momentum")
        state, version = self.store.load_paper_portfolio(
            coordinator.scope_key, momentum_id, now=now)
        state["cash_usdt"] = 123.0
        self.store.save_paper_portfolio(
            coordinator.scope_key, momentum_id, state, version, now=now + 1)

        for strategy_id in coordinator.strategy_ids:
            if strategy_id == "momentum":
                continue
            other = self.store.paper_portfolio_state(
                coordinator.scope_key,
                variants.baseline_variant_id(strategy_id))
            with self.subTest(strategy=strategy_id):
                self.assertEqual(other["cash_usdt"], 10_000.0)

    def test_one_strategy_failure_does_not_stop_the_other_six(self):
        coordinator = self.build()
        broken = coordinator.evaluators["funding-carry"]

        def fail(*args, **kwargs):
            raise RuntimeError("synthetic strategy-local failure")

        broken.evaluate = fail
        coordinator.evaluate(
            {SYMBOL: market_row()}, now=time.time(), cycle_id="one-fails",
            proposals=[momentum_proposal()])

        self.assertIn(
            "synthetic strategy-local failure",
            coordinator.last_cycle_by_strategy["funding-carry"]["error"])
        for strategy_id in coordinator.strategy_ids:
            if strategy_id == "funding-carry":
                continue
            with self.subTest(strategy=strategy_id):
                baseline = variants.baseline_variant_id(strategy_id)
                self.assertTrue(self.store.paper_decisions_for(
                    coordinator.scope_key, baseline))

    def test_missing_book_persists_scalp_data_missing_veto(self):
        coordinator = self.build()
        row = market_row(_enrichment={})

        coordinator.evaluate(
            {SYMBOL: row}, now=time.time(), cycle_id="missing-book",
            proposals=[momentum_proposal()])

        scalp_id = variants.baseline_variant_id("scalp-maker")
        decisions = self.store.paper_decisions_for(
            coordinator.scope_key, scalp_id)
        self.assertTrue(decisions)
        self.assertTrue(all(row["decision_outcome"] == "VETOED"
                            for row in decisions))
        self.assertTrue(any("data missing" in str(row["reason"])
                            for row in decisions))
        self.assertEqual(self.store.paper_trades_for(
            coordinator.scope_key, scalp_id), [])

    def test_rotations_and_restart_are_strategy_local_and_durable(self):
        coordinator = self.build()
        now = time.time()
        coordinator.evaluate(
            {SYMBOL: market_row()}, now=now, cycle_id="before-restart",
            proposals=[momentum_proposal()])
        assignments = {
            strategy_id: self.store.active_experiment_assignment(
                coordinator.scope_key, strategy_id, now=now)
            for strategy_id in coordinator.strategy_ids
        }
        self.assertTrue(all(assignments.values()))
        scalp_assignment_id = assignments["scalp-maker"]["assignment_id"]
        momentum_state = self.store.paper_portfolio_state(
            coordinator.scope_key, variants.baseline_variant_id("momentum"))

        restarted = self.build()

        self.assertEqual(
            self.store.active_experiment_assignment(
                restarted.scope_key, "scalp-maker")["assignment_id"],
            scalp_assignment_id)
        restarted_state = self.store.paper_portfolio_state(
            restarted.scope_key, variants.baseline_variant_id("momentum"))
        self.assertEqual(restarted_state["positions"],
                         momentum_state["positions"])

        flush = assignments["flush-fade"]
        self.store.record_experiment_observations(
            flush["assignment_id"], [{
                "observation_key": "flush-only-completion",
                "observed_ts": now,
                "detail": {"strategy_id": "flush-fade"},
            }], now=now)
        draining = self.store.maybe_complete_experiment_assignment(
            flush["assignment_id"], now=now + 3 * 86_400)
        self.assertEqual(draining["status"], "DRAINING")
        self.assertGreater(draining["unresolved_actions"]["total"], 0)
        still_scalp = self.store.active_experiment_assignment(
            restarted.scope_key, "scalp-maker", now=now + 3 * 86_400)
        self.assertEqual(still_scalp["assignment_id"], scalp_assignment_id)


class ExecutableTradeLifecycleTests(unittest.TestCase):
    def test_each_strategy_can_open_and_resolve_a_synthetic_trade(self):
        for strategy_id, model in BY_STRATEGY.items():
            with self.subTest(strategy=strategy_id), tempfile.TemporaryDirectory() as d:
                cfg = shadow._research_cfg(valid_config(), strategy_id)
                baseline = variants.baseline(
                    strategy_id, cfg["strategy"]["version"])
                store = FindingsStore(f"{d}/findings.db")
                evaluator = shadow.ShadowEvaluator(
                    [baseline], cfg, store=store,
                    scope_key=f"demo:{strategy_id}")
                row = firing_row(strategy_id)
                snapshot = {SYMBOL: row}
                proposals = ([momentum_proposal()]
                             if strategy_id == "momentum"
                             else model.deterministic_proposals(snapshot, cfg))
                now = time.time()

                evaluator.evaluate(
                    snapshot, now=now, cycle_id=f"open-{strategy_id}",
                    proposals=proposals)
                trades = store.paper_trades_for(
                    evaluator.scope_key, baseline.variant_id)
                self.assertTrue(trades, f"{strategy_id} did not open")
                opened = next(row for row in trades if row["status"] == "OPEN")
                exit_price = (float(opened["take_price"]) * 1.01
                              if opened["direction"] == "long"
                              else float(opened["take_price"]) * 0.99)

                entry_bar = int(now // 60 * 60_000)
                if opened["direction"] == "long":
                    bar_low = max(
                        float(opened["stop_price"]) * 1.001,
                        float(opened["entry_price"]) * 0.9998)
                    bar_high = float(opened["take_price"]) * 1.01
                else:
                    bar_low = float(opened["take_price"]) * 0.99
                    bar_high = min(
                        float(opened["stop_price"]) * 0.999,
                        float(opened["entry_price"]) * 1.0002)
                fill_bar = {
                    "timestamp_ms": entry_bar + 60_000,
                    "open": float(opened["entry_price"]),
                    "high": bar_high, "low": bar_low, "close": exit_price,
                    "volume": 1_000.0, "complete": True,
                }
                enrichment = {
                    **row["_enrichment"],
                    "ticker_best_bid": exit_price,
                    "ticker_best_ask": exit_price,
                    "execution_bars": [fill_bar],
                }
                evaluator.advance(
                    {SYMBOL: {**row, "price": exit_price,
                              "_enrichment": enrichment}}, now=now + 60)
                if strategy_id == "scalp-maker":
                    exit_bar = {
                        **fill_bar, "timestamp_ms": entry_bar + 120_000,
                    }
                    evaluator.advance(
                        {SYMBOL: {**row, "price": exit_price,
                                  "_enrichment": {
                                      **enrichment,
                                      "execution_bars": [fill_bar, exit_bar],
                                  }}},
                        now=now + 120)

                closed = store.paper_trades_for(
                    evaluator.scope_key, baseline.variant_id)
                self.assertEqual(closed[0]["status"], "CLOSED")
                self.assertEqual(closed[0]["result"], "target")


if __name__ == "__main__":
    unittest.main()
