"""Every staged mechanism is measured on the same contract, and starvation
is never recorded as a decline.

Two authored mechanisms that differ in both their entry rule and their exit
policy cannot be compared: a winner could be a lucky stop distance. The
harness is therefore fixed for the whole staged population. Separately, a
mechanism evaluated on a snapshot it could not read has not been tested, and
counting that as "the contract declined" is what turned seven days of
depth-ladder starvation into apparent evidence against six strategies.
"""

import unittest

from agent import staged_lane
from agent.contract_dsl import validate
from agent.forward_models import (BY_STRATEGY, MODELS, STAGED_HARNESS,
                                  STAGED_HARNESS_ID,
                                  require_complete_contract)


def contract(contract_id="liq-absorption", direction="both", **overrides):
    base = {
        "contract_id": contract_id,
        "mechanism": (
            "Filled liquidations are price-insensitive sells that must "
            "complete regardless of price, so the book reprices afterwards."),
        "payer": (
            "the over-leveraged trader whose margin ran out and who has no "
            "discretion about the exit price"),
        "falsifier": (
            "Top-decile liquidation bars show no more reversion than bars "
            "where none printed, at n>=100 per cell."),
        "direction": direction,
        "conditions": [
            {"field": "liq_notional_1h_usd", "op": ">=", "value": 250_000.0}],
    }
    base.update(overrides)
    return validate(base)


def snapshot(**overrides):
    row = {
        "price": 100.0, "spread_pct": 0.01, "funding_rate_pct": 0.01,
        "funding_interval_hours": 8.0, "next_funding_minutes": 60.0,
        "taker_fee_pct_per_side": 0.05, "atr_1h_pct": 1.0,
        "ema20_1h_dist_pct": 0.2, "signal_ts": 1_800_000_000,
        "liq_notional_1h_usd": 900_000.0,
        "_enrichment": {
            "book_ts": 1_800_000_000_000, "book_best_bid": 99.99,
            "book_best_ask": 100.01, "book_spread_pct": 0.02,
            "book_bid_levels": [[99.99, 5.0]], "book_ask_levels": [[100.01, 5.0]],
            "book_contract_size": 0.01, "book_observation_error": None,
        },
    }
    enrichment = overrides.pop("_enrichment", None)
    row.update(overrides)
    if enrichment is not None:
        row["_enrichment"] = {**row["_enrichment"], **enrichment}
    return {"BTC/USDT:USDT": row}


class OneHarnessForEveryStagedMechanismTests(unittest.TestCase):
    def test_the_harness_is_complete_but_is_not_a_strategy(self):
        self.assertEqual(STAGED_HARNESS.model_id, STAGED_HARNESS_ID)
        self.assertTrue(STAGED_HARNESS.contract_complete)
        self.assertTrue(STAGED_HARNESS.contract_evidence)
        # It is a measurement instrument, not a registered strategy. Listing
        # it beside the seven strategy models would make every "one model per
        # registered strategy" invariant quietly false.
        self.assertNotIn(STAGED_HARNESS_ID, MODELS)
        self.assertNotIn("staged", BY_STRATEGY)
        with self.assertRaises(KeyError):
            require_complete_contract("staged")

    def test_two_mechanisms_share_identical_outcome_assumptions(self):
        proposals = staged_lane.proposals_for(
            [contract("a"), contract("b", direction="long")], snapshot(), {})
        shapes = {(p["model_id"], p["exit_policy"], p["invalidation_anchor"],
                   p["setup_type"]) for p in proposals}
        self.assertEqual(
            len(shapes), 1,
            "a difference between staged mechanisms must be the mechanism, "
            "not the exit policy")
        self.assertEqual(
            {p["contract_id"] for p in proposals}, {"a", "b"},
            "results must still be attributable per mechanism")

    def test_the_harness_is_not_a_favourable_one(self):
        # A mechanism that cannot clear ordinary assumptions has been measured
        # against the same bar as everything else, not treated unfairly.
        self.assertEqual(STAGED_HARNESS.stop_atr_multiple, 1.0)
        self.assertEqual(STAGED_HARNESS.reward_risk, 2.0)
        self.assertEqual(STAGED_HARNESS.exit_policy, "fixed_rr")
        self.assertIn("taker", STAGED_HARNESS.cost_assumption)
        self.assertIn("funding", STAGED_HARNESS.cost_assumption)


class StarvationIsNotADeclineTests(unittest.TestCase):
    def test_a_firing_mechanism_carries_no_refusal(self):
        proposals = staged_lane.proposals_for([contract()], snapshot(), {})
        fired = [p for p in proposals if not p.get("research_refusal_reason")]
        self.assertTrue(fired)
        self.assertEqual(staged_lane.coverage(proposals)["liq-absorption"],
                         {"fired": 2, "declined": 0, "starved": 0})

    def test_a_declining_mechanism_says_which_condition_failed(self):
        quiet = snapshot(liq_notional_1h_usd=1_000.0)
        proposals = staged_lane.proposals_for([contract()], quiet, {})
        reasons = {p["research_refusal_reason"] for p in proposals}
        self.assertTrue(all(r.startswith("contract declined") for r in reasons))
        self.assertTrue(any("liq_notional_1h_usd" in r for r in reasons))
        self.assertEqual(staged_lane.coverage(proposals)["liq-absorption"],
                         {"fired": 0, "declined": 2, "starved": 0})

    def test_missing_data_outranks_the_contracts_own_opinion(self):
        # The mechanism would decline here too, but it was never evaluated:
        # recording a decline would count starvation as evidence against it.
        starved = snapshot(liq_notional_1h_usd=1_000.0,
                           _enrichment={"book_bid_levels": None})
        proposals = staged_lane.proposals_for([contract()], starved, {})
        for proposal in proposals:
            self.assertTrue(
                proposal["research_refusal_reason"].startswith("data missing"),
                proposal["research_refusal_reason"])
        self.assertEqual(staged_lane.coverage(proposals)["liq-absorption"],
                         {"fired": 0, "declined": 0, "starved": 2})

    def test_an_observation_error_is_reported_over_a_missing_field(self):
        broken = snapshot(_enrichment={
            "book_observation_error": "bid depth: level 3 has invalid amount"})
        proposals = staged_lane.proposals_for([contract()], broken, {})
        for proposal in proposals:
            self.assertIn("market data invalid",
                          proposal["research_refusal_reason"])
            self.assertIn("level 3", proposal["research_refusal_reason"])

    def test_a_one_sided_mechanism_emits_only_its_own_side(self):
        proposals = staged_lane.proposals_for(
            [contract(direction="short")], snapshot(), {})
        self.assertEqual({p["direction"] for p in proposals}, {"short"})

    def test_private_snapshot_keys_are_skipped(self):
        feed = snapshot()
        feed["_market_context"] = {"btc_ref_return_1_pct": 0.5}
        proposals = staged_lane.proposals_for([contract()], feed, {})
        self.assertEqual({p["symbol"] for p in proposals}, {"BTC/USDT:USDT"})

    def test_no_staged_mechanism_can_claim_a_registered_strategy_identity(self):
        proposals = staged_lane.proposals_for([contract()], snapshot(), {})
        for proposal in proposals:
            self.assertEqual(proposal["strategy_id"], "staged")
            self.assertEqual(proposal["proposal_source"], "staged_contract")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
