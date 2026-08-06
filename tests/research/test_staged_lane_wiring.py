"""A staged mechanism must reach a paper account, and must stay out of the
registered strategies' comparison.

Authoring, validation and the harness were each complete before this and the
loop was still open: nothing evaluated a staged contract, so proposals
accumulated without being measured. These tests hold the wiring, and the
isolation that makes it safe to leave running unattended.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from agent import shadow
from agent.contract_dsl import validate
from agent.forward_models import STAGED_HARNESS
from agent.staging import StagingStore
from tests.helpers import valid_config


def contract(contract_id="liq-absorption"):
    return validate({
        "contract_id": contract_id,
        "mechanism": (
            "Filled liquidations are price-insensitive sells that must "
            "complete regardless of price, so the book reprices afterwards."),
        "payer": ("the over-leveraged trader whose margin ran out and who "
                  "has no discretion about the exit price"),
        "falsifier": ("Top-decile liquidation bars show no more reversion "
                      "than bars where none printed, at n>=100 per cell."),
        "direction": "both",
        "conditions": [
            {"field": "liq_notional_1h_usd", "op": ">=", "value": 250_000.0}],
    })


def snapshot(liq=900_000.0):
    return {"BTC/USDT:USDT": {
        "price": 100.0, "spread_pct": 0.01, "funding_rate_pct": 0.01,
        "funding_interval_hours": 8.0, "next_funding_minutes": 60.0,
        "taker_fee_pct_per_side": 0.05, "atr_1h_pct": 1.0,
        "ema20_1h_dist_pct": 0.2, "signal_ts": 1_800_000_000,
        "liq_notional_1h_usd": liq,
        "_enrichment": {
            "book_ts": 1_800_000_000_000, "book_best_bid": 99.99,
            "book_best_ask": 100.01, "book_spread_pct": 0.02,
            "book_bid_levels": [[99.99, 500.0]],
            "book_ask_levels": [[100.01, 500.0]],
            "book_contract_size": 0.01, "book_observation_error": None,
        },
    }}


class StagedLaneIsWiredTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.staging = Path(self.tmp.name) / "staging.db"

    def _cfg(self, staged: bool):
        cfg = valid_config()
        research = dict(cfg.get("research") or {})
        research.update({
            "shadow_enabled": True, "shadow_variants": ["*"],
            "shadow_budget_ms": 0, "shadow_workers": 1,
            "findings_store": str(Path(self.tmp.name) / "findings.db"),
            "staging_store": str(self.staging) if staged else "",
        })
        cfg["research"] = research
        return cfg

    def test_the_lane_is_absent_until_something_is_staged(self):
        evaluators, contracts = shadow._build_staged_lane(
            self._cfg(staged=True), "demo:test", None)
        self.assertEqual(evaluators, {})
        self.assertEqual(contracts, [])

    def test_a_staged_mechanism_becomes_an_enrolled_variant(self):
        store = StagingStore(self.staging)
        store.register({
            "contract_id": contract().contract_id,
            "mechanism": contract().mechanism, "payer": contract().payer,
            "falsifier": contract().falsifier, "direction": "both",
            "conditions": [{"field": "liq_notional_1h_usd", "op": ">=",
                            "value": 250_000.0}],
        }, generation=0)
        from research.findings import FindingsStore

        findings = FindingsStore(Path(self.tmp.name) / "findings.db")
        evaluators, contracts = shadow._build_staged_lane(
            self._cfg(staged=True), "demo:test", findings)
        self.assertEqual([c.contract_id for c in contracts],
                         ["liq-absorption"])
        evaluator = evaluators["liq-absorption"]
        # Its own scope: a staged claim must never be pooled with the
        # registered strategies' comparison arms.
        self.assertTrue(evaluator.scope_key.endswith(":staged"))
        variant_id = shadow.staged_variant_id("liq-absorption")
        self.assertEqual(variant_id, "staged.liq_absorption")
        # Measured on the shared harness, not on momentum's.
        self.assertEqual(evaluator._models[variant_id].model_id,
                         STAGED_HARNESS.model_id)

    def test_each_mechanism_gets_its_own_evaluator(self):
        store = StagingStore(self.staging)
        for name in ("alpha-claim", "beta-claim"):
            base = contract(name)
            store.register({
                "contract_id": name, "mechanism": base.mechanism,
                "payer": base.payer, "falsifier": base.falsifier,
                "direction": "both",
                "conditions": [{"field": "liq_notional_1h_usd", "op": ">=",
                                "value": 250_000.0}],
            }, generation=0)
        from research.findings import FindingsStore

        findings = FindingsStore(Path(self.tmp.name) / "findings.db")
        evaluators, contracts = shadow._build_staged_lane(
            self._cfg(staged=True), "demo:test", findings)
        # Sharing one evaluator would apply every contract's proposals to
        # every variant, so each claim would trade on the others' signals.
        self.assertEqual(sorted(evaluators), ["alpha-claim", "beta-claim"])
        self.assertIsNot(evaluators["alpha-claim"], evaluators["beta-claim"])
        self.assertEqual(len(contracts), 2)


class StagedLaneCannotCostTheCycleTests(unittest.TestCase):
    """A research lane that can interrupt trading is a safety regression."""

    def _coordinator(self, staged_evaluator, contracts):
        inner = Mock()
        inner.scope_key = "demo:x"
        inner.store = Mock()
        inner.registration_errors = {}
        inner.last_coverage = {}
        inner.last_budget = None
        inner.evaluate.return_value = []
        staged = ({} if staged_evaluator is None
                  else {c.contract_id: staged_evaluator for c in contracts})
        return shadow.StrategyShadowCoordinator(
            {"momentum": inner}, active_strategy_id="momentum",
            staged_evaluators=staged, staged_contracts=contracts)

    def test_a_failing_staged_lane_is_journalled_and_swallowed(self):
        broken = Mock()
        broken.scope_key = "demo:x:staged"
        broken.base_cfg = {}
        broken.evaluate.side_effect = RuntimeError("staged lane exploded")
        coordinator = self._coordinator(broken, [contract()])
        records = coordinator.evaluate(snapshot(), now=1000.0)
        self.assertEqual(records, [])
        lane = coordinator.last_coverage["staged_lane"]
        self.assertIn("staged lane exploded", lane["errors"]["liq-absorption"])

    def test_coverage_separates_fired_from_declined(self):
        staged = Mock()
        staged.scope_key = "demo:x:staged"
        staged.base_cfg = {}
        staged.evaluate.return_value = []
        staged.last_coverage = {}
        coordinator = self._coordinator(staged, [contract()])

        coordinator.evaluate(snapshot(liq=900_000.0), now=1000.0)
        fired = coordinator.last_coverage["staged_lane"]["per_contract"]
        self.assertEqual(fired["liq-absorption"]["fired"], 2)

        coordinator.evaluate(snapshot(liq=1_000.0), now=2000.0)
        declined = coordinator.last_coverage["staged_lane"]["per_contract"]
        self.assertEqual(declined["liq-absorption"]["declined"], 2)
        self.assertEqual(declined["liq-absorption"]["fired"], 0)

    def test_no_staged_lane_leaves_the_registered_lanes_untouched(self):
        coordinator = self._coordinator(None, [])
        records = coordinator.evaluate(snapshot(), now=1000.0)
        self.assertEqual(records, [])
        self.assertIsNone(coordinator.last_coverage["staged_lane"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
