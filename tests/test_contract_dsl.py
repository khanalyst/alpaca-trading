"""An authored mechanism must be executable, bounded, and never live-eligible.

Before this, a new mechanism meant a new hand-written Python function, which
capped the registry at three hypotheses all attached to one strategy. These
tests hold the properties that make opening that door safe: a proposal can
only name inputs the validated forward models already declare, can only
compare them four ways, must state a cause and a falsifier, and enters at a
tier the promotion path refuses to trade.
"""

import tempfile
import unittest
from pathlib import Path

from agent import registry as strategy_registry
from agent.contract_dsl import (MAX_CONDITIONS, OBSERVABLE_FIELDS,
                                ContractProposalError, build, validate)
from agent.staging import STAGING_TIER, StagingError, StagingStore


def proposal(**overrides) -> dict:
    base = {
        "contract_id": "funding-tail-unwind",
        "mechanism": (
            "Extreme funding marks crowded leverage that must keep paying to "
            "hold, and crowded leverage is forced out when continuation stalls."),
        "payer": (
            "the leveraged trader squeezed out of a position they could no "
            "longer finance"),
        "falsifier": (
            "Expectancy in the crowded cell does not exceed the unconditional "
            "case by more than the MDE at n>=100 per cell."),
        "direction": "both",
        "conditions": [
            {"field": "funding_percentile_30", "op": ">=", "value": 80.0,
             "when_direction": "short"},
            {"field": "funding_percentile_30", "op": "<=", "value": 20.0,
             "when_direction": "long"},
            {"field": "funding_samples_30", "op": ">=", "value": 20},
        ],
    }
    base.update(overrides)
    return base


class AuthoredContractsExecuteTests(unittest.TestCase):
    def test_a_valid_proposal_compiles_and_fires_on_its_own_terms(self):
        contract, fire = build(proposal())
        crowded = {"funding_percentile_30": 92.0, "funding_samples_30": 30}
        self.assertEqual(fire(crowded, "short", {}, {}), (True, None))
        fired, reason = fire(crowded, "long", {}, {})
        self.assertFalse(fired)
        self.assertIn("funding_percentile_30", reason)
        self.assertEqual(contract.direction, "both")

    def test_a_missing_or_non_numeric_field_vetoes_rather_than_raises(self):
        _, fire = build(proposal())
        for snapshot in ({}, {"funding_percentile_30": None},
                         {"funding_percentile_30": "high"},
                         {"funding_percentile_30": float("nan")}):
            with self.subTest(repr(snapshot)):
                fired, reason = fire(snapshot, "short", {}, {})
                self.assertFalse(fired)
                self.assertIn("unavailable", reason)

    def test_malformed_condition_lists_fail_as_proposals_not_runtime_errors(self):
        with self.assertRaises(ContractProposalError):
            validate(proposal(conditions=None))

    def test_a_threshold_can_be_swept_like_a_registered_axis(self):
        _, fire = build(proposal())
        crowded = {"funding_percentile_30": 92.0, "funding_samples_30": 30}
        self.assertTrue(fire(crowded, "short", {}, {})[0])
        tightened = fire(crowded, "short", {}, {"funding_percentile_30": 95.0})
        self.assertFalse(tightened[0])

    def test_a_direction_scoped_contract_refuses_the_other_side(self):
        _, fire = build(proposal(
            direction="long",
            conditions=[{"field": "mom_1h_pct", "op": ">", "value": 0.0}]))
        self.assertTrue(fire({"mom_1h_pct": 1.0}, "long", {}, {})[0])
        fired, reason = fire({"mom_1h_pct": 1.0}, "short", {}, {})
        self.assertFalse(fired)
        self.assertIn("long-only", reason)

    def test_bounded_primitives_use_only_persisted_snapshot_sources(self):
        bars = [
            {"complete": True, "timestamp_ms": 1, "close": 100.0},
            {"complete": True, "timestamp_ms": 2, "close": 110.0},
        ]
        primitive = proposal(
            conditions=[],
            primitives=[{
                "primitive": "rolling_change", "field": "close",
                "window": 1, "mode": "pct", "op": ">", "value": 5.0,
            }],
        )
        _, fire = build(primitive)
        row = {
            "regime": "trend_up",
            "_enrichment": {
                "execution_bars": bars,
                "book_bid_depth_usd": 1200.0,
                "book_ask_depth_usd": 800.0,
                "book_top_bid_size": 12.0,
                "book_top_ask_size": 8.0,
                "book_spread_pct": 0.02,
                "realized_funding_events": [
                    {"rate_pct": 0.1}, {"rate_pct": 0.2},
                ],
            },
        }
        self.assertEqual(fire(row, "short", {}, {}), (True, None))

        _, regime_fire = build(proposal(
            conditions=[],
            primitives=[{"primitive": "regime_filter", "state": "trend_up"}],
        ))
        self.assertEqual(regime_fire(row, "short", {}, {}), (True, None))

        _, book_fire = build(proposal(
            conditions=[],
            primitives=[{"primitive": "order_book_imbalance", "metric": "depth",
                         "op": ">", "value": 0.1}],
        ))
        self.assertEqual(book_fire(row, "short", {}, {}), (True, None))

        _, event_fire = build(proposal(
            conditions=[],
            primitives=[{"primitive": "event_sequence",
                         "events": ["funding_positive", "funding_positive"]}],
        ))
        self.assertEqual(event_fire(row, "short", {}, {}), (True, None))


class AuthoredContractsAreBoundedTests(unittest.TestCase):
    def test_only_fields_a_validated_contract_declares_are_proposable(self):
        with self.assertRaises(ContractProposalError) as caught:
            validate(proposal(conditions=[
                {"field": "moon_phase", "op": ">", "value": 1}]))
        self.assertIn("moon_phase", str(caught.exception))
        # The allowlist is derived, not hardcoded, so it must be non-trivial
        # and must contain fields the forward models actually require.
        self.assertGreater(len(OBSERVABLE_FIELDS), 15)
        self.assertIn("funding_percentile_30", OBSERVABLE_FIELDS)
        self.assertNotIn("_enrichment.book_bid_levels", OBSERVABLE_FIELDS)

    def test_arbitrary_operators_are_refused(self):
        for op in ("~=", "==", "exec", "in", None, 7):
            with self.subTest(repr(op)):
                with self.assertRaises(ContractProposalError):
                    validate(proposal(conditions=[
                        {"field": "mom_1h_pct", "op": op, "value": 1.0}]))

    def test_a_threshold_outside_the_observed_range_is_refused(self):
        # It would fire always or never, which measures nothing while still
        # consuming a research lane for weeks.
        for value in (-5.0, 900.0):
            with self.subTest(value):
                with self.assertRaises(ContractProposalError) as caught:
                    validate(proposal(conditions=[
                        {"field": "funding_percentile_30", "op": ">",
                         "value": value}]))
                self.assertIn("outside the observed range",
                              str(caught.exception))

    def test_a_claim_must_state_a_cause_and_a_test(self):
        for field in ("mechanism", "payer", "falsifier"):
            for thin in ("", "   ", "it works", "try it and see"):
                with self.subTest(f"{field}={thin!r}"):
                    with self.assertRaises(ContractProposalError):
                        validate(proposal(**{field: thin}))

    def test_an_unboundedly_complex_contract_is_refused(self):
        with self.assertRaises(ContractProposalError) as caught:
            validate(proposal(conditions=[
                {"field": "mom_1h_pct", "op": ">", "value": float(i)}
                for i in range(MAX_CONDITIONS + 1)]))
        self.assertIn("fitting the sample", str(caught.exception))

    def test_a_declared_direction_must_match_what_the_contract_can_do(self):
        with self.assertRaises(ContractProposalError):
            validate(proposal(direction="both", conditions=[
                {"field": "mom_1h_pct", "op": ">", "value": 0.0,
                 "when_direction": "long"}]))
        with self.assertRaises(ContractProposalError):
            validate(proposal(direction="short", conditions=[
                {"field": "mom_1h_pct", "op": ">", "value": 0.0,
                 "when_direction": "long"}]))

    def test_a_non_finite_threshold_is_refused(self):
        for value in (float("inf"), float("nan"), True, "1.0", None):
            with self.subTest(repr(value)):
                with self.assertRaises(ContractProposalError):
                    validate(proposal(conditions=[
                        {"field": "mom_1h_pct", "op": ">", "value": value}]))

    def test_cross_sectional_rank_fails_closed_without_universe_context(self):
        with self.assertRaises(ContractProposalError) as caught:
            validate(proposal(conditions=[], primitives=[{
                "primitive": "cross_sectional_rank", "field": "atr_1h_ratio",
                "op": ">", "value": 80.0,
            }]))
        self.assertIn("full-universe runtime context", str(caught.exception))


class StagedContractsCannotReachCapitalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StagingStore(Path(self.tmp.name) / "staging.db")

    def test_a_staged_contract_enters_below_the_live_floor(self):
        self.store.register(proposal(), generation=0)
        staged = self.store.active()
        self.assertEqual(len(staged), 1)
        ladder = strategy_registry.TIERS
        self.assertLess(ladder.index(STAGING_TIER),
                        ladder.index(strategy_registry.LIVE_MIN_TIER),
                        "a machine-authored contract must be born below the "
                        "tier that authorises capital")

    def test_a_registered_claim_cannot_be_rewritten(self):
        self.store.register(proposal(), generation=0)
        import sqlite3
        with sqlite3.connect(self.store.path) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE staged_contracts SET mechanism='something else' "
                    "WHERE contract_id='funding-tail-unwind'")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM staged_contracts")

    def test_a_reused_id_is_refused_so_results_stay_attributable(self):
        self.store.register(proposal(), generation=0)
        with self.assertRaises(StagingError):
            self.store.register(proposal(mechanism=(
                "A different claim entirely, stated at sufficient length to "
                "pass the substance floor for a registered mechanism.")),
                generation=1)

    def test_an_uncompilable_proposal_never_takes_an_identity(self):
        with self.assertRaises(ContractProposalError):
            self.store.register(
                proposal(conditions=[{"field": "nope", "op": ">", "value": 1}]),
                generation=0)
        self.assertEqual(self.store.active(), [])

    def test_retiring_stops_scheduling_without_erasing_the_claim(self):
        self.store.register(proposal(), generation=0)
        self.store.retire("funding-tail-unwind", "placebo reached 60% of it")
        self.assertEqual(self.store.active(), [])
        self.assertIsNotNone(self.store.contract("funding-tail-unwind"))
        self.assertEqual(self.store.summary()["retired"], 1)
        with self.assertRaises(StagingError):
            self.store.retire("funding-tail-unwind", "again")

    def test_active_contracts_compile_into_shadow_ready_evaluators(self):
        self.store.register(proposal(), generation=0)
        evaluators = self.store.evaluators()
        self.assertEqual(sorted(evaluators), ["funding-tail-unwind"])
        fire = evaluators["funding-tail-unwind"]
        self.assertEqual(
            fire({"funding_percentile_30": 92.0, "funding_samples_30": 30},
                 "short", {}, {}), (True, None))

    def test_generations_advance_so_a_later_batch_is_attributable(self):
        self.assertEqual(self.store.generation(), -1)
        self.store.register(proposal(), generation=0)
        self.store.register(proposal(contract_id="basis-crowding"), generation=1)
        self.assertEqual(self.store.generation(), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
