"""Focused regressions for durable and gap-safe paper evidence."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import shadow, state
from research.findings import FindingsStore
from research.outcomes import SetupPlan, resolve, resolve_from_cache
from research.prices import Bar, MINUTE_MS, PriceCache, day_of
from tests.helpers import valid_config
from tests.research.test_research_selector import (
    TEN_DAYS,
    parsed_selection,
    selector_config,
    selector_registry,
)


DAY_START = 1_759_968_000_000
SYMBOL = "BTC/USDT:USDT"


def research_config(execution_mode: str) -> dict:
    cfg = valid_config()
    cfg["strategy"]["execution_mode"] = execution_mode
    cfg["research"] = {
        "shadow_enabled": True,
        "shadow_variants": ["*"],
        "shadow_budget_ms": 0,
        "shadow_workers": 1,
        "paper_initial_balance_usdt": 10_000,
        "paper_max_failures": 3,
        "paper_min_closed_trades": 100,
        "experiment_min_duration_days": 10,
        "experiment_min_observations": 1,
        "forward_feed_version": 2,
    }
    return cfg


class DurableSelectorIntegrityTests(unittest.TestCase):
    def test_pending_exact_selection_rehydrates_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "findings.db"
            scope = "demo:selector-restart"
            cfg = selector_config()
            store = FindingsStore(path)
            first = shadow.build(
                cfg, selector_registry(), scope_key=scope, store=store)
            resolved_scope = first.scope_key
            strategy_id = "momentum"
            selected_variant = "momentum.hyp.volume_thrust.registered"
            current = store.active_experiment_assignment(
                resolved_scope, strategy_id)
            selected = first.record_research_selection(
                parsed_selection(strategy_id, selected_variant), {
                    "run_id": "run-before-restart",
                    "cycle_id": "cycle-before-restart",
                    "model_id": "test-model",
                    "prompt_version": "test-prompt",
                })

            restarted = shadow.build(
                cfg, selector_registry(), scope_key=scope,
                store=FindingsStore(path))
            restarted.store.record_experiment_observations(
                current["assignment_id"],
                [{"observation_key": "finish-current"}],
                now=current["started_ts"] + 1)
            restarted.store.maybe_complete_experiment_assignment(
                current["assignment_id"],
                now=current["started_ts"] + TEN_DAYS)
            restarted.evaluators[strategy_id]._refresh_rotation(
                current["started_ts"] + TEN_DAYS + 1)

            assigned = restarted.store.active_experiment_assignment(
                resolved_scope, strategy_id)
            linked = restarted.store.research_selection(
                selected["selection_id"])
            self.assertEqual(assigned["candidate_variant_id"], selected_variant)
            self.assertEqual(linked["current_status"], "ASSIGNED")
            self.assertEqual(linked["assignment_id"], assigned["assignment_id"])


class EvidenceLaneIntegrityTests(unittest.TestCase):
    def test_only_analyst_execution_builds_a_recorded_llm_lane(self):
        with tempfile.TemporaryDirectory() as directory:
            deterministic = shadow.build(
                research_config("deterministic"), {},
                scope_key="demo:deterministic",
                store=FindingsStore(Path(directory) / "deterministic.db"))
            analyst = shadow.build(
                research_config("analyst"), {},
                scope_key="demo:analyst",
                store=FindingsStore(Path(directory) / "analyst.db"))

            self.assertIsNone(deterministic.llm_evaluator)
            deterministic.evaluate(
                {}, now=1_800_000_000, cycle_id="deterministic-only",
                proposals=[])
            self.assertIsNone(deterministic.last_coverage["llm_lane"])
            self.assertIsNotNone(analyst.llm_evaluator)
            self.assertTrue(analyst.llm_evaluator.scope_key.endswith(":llm"))

    def test_code_fingerprint_reads_nested_contract_modules(self):
        contract = (Path(state.__file__).resolve().parent
                    / "contracts" / "momentum_phase1v2.py")
        original_read = Path.read_bytes
        baseline = state.code_fingerprint()

        def changed_bytes(path):
            payload = original_read(path)
            return payload + b"\n# changed for fingerprint test\n" \
                if path == contract else payload

        with patch.object(Path, "read_bytes", autospec=True,
                          side_effect=changed_bytes):
            changed = state.code_fingerprint()
        self.assertNotEqual(changed, baseline)


class PriceEvidenceIntegrityTests(unittest.TestCase):
    def test_empty_and_partial_days_are_not_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = PriceCache(Path(directory) / "prices.db")
            day = day_of(DAY_START)
            cache.store(SYMBOL, day, [])
            self.assertFalse(cache.has_day(SYMBOL, day))

            partial = [
                [DAY_START + index * MINUTE_MS, 100, 101, 99, 100, 1]
                for index in range(60)]
            cache.store(SYMBOL, day, partial)
            self.assertFalse(cache.has_day(SYMBOL, day))
            self.assertTrue(cache.covers(
                SYMBOL, DAY_START, DAY_START + 60 * MINUTE_MS))
            self.assertFalse(cache.covers(
                SYMBOL, DAY_START, DAY_START + 61 * MINUTE_MS))

    def test_gap_keeps_outcome_unresolved_without_duration_or_funding(self):
        plan = SetupPlan(
            symbol=SYMBOL, direction="long", entry_price=100,
            stop_pct=20, take_pct=20, signal_ts=DAY_START,
            signal_timeframe="15m", funding_rate_pct=1,
            funding_interval_hours=0.01)
        start = plan.first_visible_ts
        bars = [
            Bar(start, 100, 101, 99, 100, 1),
            Bar(start + 2 * MINUTE_MS, 100, 101, 99, 100, 1),
        ]

        outcome = resolve(plan, bars, max_hold_hours=1)
        self.assertEqual(outcome.result, "no_data")
        self.assertEqual(outcome.bars_held, 0)
        self.assertEqual(outcome.costs, {})
        self.assertEqual(outcome.cost_pct, 0)
        self.assertEqual(outcome.risk_pct, 0)

    def test_partial_cache_cannot_manufacture_a_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = PriceCache(Path(directory) / "prices.db")
            plan = SetupPlan(
                symbol=SYMBOL, direction="long", entry_price=100,
                stop_pct=20, take_pct=20, signal_ts=DAY_START,
                signal_timeframe="15m", funding_rate_pct=1,
                funding_interval_hours=0.01)
            start = plan.first_visible_ts
            cache.store(SYMBOL, day_of(start), [
                [start + index * MINUTE_MS, 100, 101, 99, 100, 1]
                for index in range(10)])

            outcome = resolve_from_cache(plan, cache, max_hold_hours=1)
            self.assertEqual(outcome.result, "no_data")
            self.assertEqual(outcome.bars_held, 0)
            self.assertEqual(outcome.costs, {})


if __name__ == "__main__":
    unittest.main()
