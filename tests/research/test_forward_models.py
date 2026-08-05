"""Executable evidence for every real-time forward outcome model."""

import time
import unittest

from agent import shadow, strategy
from agent.forward_models import BY_STRATEGY
from tests.helpers import valid_config


def market_row(**overrides):
    observed_ms = int(time.time() * 1000)
    row = {
        "price": 100.0,
        "spread_pct": 0.02,
        "taker_fee_pct_per_side": 0.05,
        "fee_rate_source": "test",
        "funding_rate_pct": 0.02,
        "funding_interval_hours": 8.0,
        "next_funding_minutes": 60.0,
        "funding_percentile_30": 90.0,
        "funding_samples_30": 30,
        "perp_index_basis_pct": 0.10,
        "open_interest_musd": 500.0,
        "long_short_ratio": 1.5,
        "long_short_percentile_30": 90.0,
        "oi_change_4h_pct": -2.0,
        "liq_notional_1h_usd": 250_000.0,
        "relative_volume_1h": 2.0,
        "mom_1h_pct": 2.0,
        "mom_15m_pct": 0.4,
        "atr_1h_pct": 1.0,
        "atr_1h_ratio": 1.0,
        "ema20_1h_dist_pct": 0.0,
        "range_pos_pct": 75.0,
        "trend_15m": "up",
        "trend_1h": "up",
        "trend_4h": "up",
        "swing_low_pct": 1.0,
        "swing_high_pct": 1.0,
        "fresh_breakout_long": False,
        "fresh_breakout_short": False,
        "price_stabilized_long": False,
        "price_stabilized_short": False,
        "signal_ts": 1_760_000_000_000,
        "signal_1h_ts": 1_759_999_200_000,
        "signal_4h_ts": 1_759_996_800_000,
        "corr_btc_samples": 0,
        "beta_btc_1h_72": None,
        "_enrichment": {
            "ticker_ts": observed_ms,
            "ticker_best_bid": 99.99,
            "ticker_best_ask": 100.01,
            "ticker_age_seconds": 0.0,
            "book_ts": observed_ms,
            "book_mid": 100.0,
            "book_best_bid": 99.99,
            "book_best_ask": 100.01,
            "book_spread_pct": 0.02,
            "book_bid_depth_usd": 80_000.0,
            "book_ask_depth_usd": 20_000.0,
            "book_top_bid_size": 100.0,
            "book_top_ask_size": 50.0,
            "book_bid_levels": [[99.99, 100.0], [99.98, 100.0]],
            "book_ask_levels": [[100.01, 100.0], [100.02, 100.0]],
            "book_contract_size": 1.0,
            "book_age_seconds": 0.1,
            "book_observation_error": None,
        },
    }
    row.update(overrides)
    return row


class ForwardModelDeclarationTests(unittest.TestCase):
    def test_all_seven_models_are_executable_and_implementation_validated(self):
        self.assertEqual(set(BY_STRATEGY), {
            "momentum", "flush-fade", "funding-carry", "funding-unwind",
            "trend-multiday", "ls-ratio-fade", "scalp-maker",
        })
        for model in BY_STRATEGY.values():
            with self.subTest(strategy=model.strategy_id):
                self.assertTrue(model.contract_complete)
                self.assertTrue(model.required_fields)
                self.assertGreater(model.stop_atr_multiple, 0)
                self.assertGreater(model.reward_risk, 0)
                self.assertIn("funding", model.cost_assumption.lower())

    def test_non_active_models_generate_comparable_two_direction_probes(self):
        snap = {"BTC/USDT:USDT": market_row()}
        for strategy_id, model in BY_STRATEGY.items():
            cfg = shadow._research_cfg(valid_config(), strategy_id)
            proposals = model.deterministic_proposals(snap, cfg)
            with self.subTest(strategy=strategy_id):
                self.assertTrue(proposals)
                directions = {proposal["direction"] for proposal in proposals}
                self.assertEqual(directions, {"long", "short"})
                self.assertTrue(all(
                    proposal["proposal_source"] == "deterministic_contract"
                    for proposal in proposals))

    def test_missing_book_is_an_explicit_refusal_not_a_synthetic_fill(self):
        model = BY_STRATEGY["scalp-maker"]
        row = market_row(_enrichment={})
        proposal = model.deterministic_proposals(
            {"BTC/USDT:USDT": row},
            shadow._research_cfg(valid_config(), "scalp-maker"))[0]

        self.assertIn("data missing", proposal["research_refusal_reason"])
        self.assertIsNone(model.entry_price(row, "long"))

    def test_maker_entry_joins_the_observed_quote(self):
        model = BY_STRATEGY["scalp-maker"]
        row = market_row()

        self.assertEqual(model.entry_price(row, "long"), 99.99)
        self.assertEqual(model.entry_price(row, "short"), 100.01)

    def test_funding_models_share_execution_but_keep_distinct_provenance(self):
        carry = BY_STRATEGY["funding-carry"]
        unwind = BY_STRATEGY["funding-unwind"]

        for model in (carry, unwind):
            with self.subTest(strategy=model.strategy_id):
                self.assertEqual(
                    (model.signal_timeframe, model.horizon_hours,
                     model.stop_atr_multiple, model.reward_risk,
                     model.funding_treatment),
                    ("1h", 240.0, 3.0, 2.0,
                     "observed_realized_settlements"))

    def test_the_two_funding_models_exit_on_different_things(self):
        """They share a signal cadence and a horizon, not an exit.

        funding-carry earns from the carry, so it closes when the carry is
        spent. funding-unwind is a directional bet on the crowd being forced
        out, so a price exit is the right one for it. Collapsing the two onto
        one exit policy is what made carry scoreable on a move it never
        claimed to forecast.
        """
        carry = BY_STRATEGY["funding-carry"]
        unwind = BY_STRATEGY["funding-unwind"]

        self.assertEqual(carry.exit_policy, "carry_until_normalised")
        self.assertEqual(carry.carry_exit_funding_percentile, 50.0)
        self.assertIn("funding_percentile_30", carry.required_fields)
        self.assertEqual(unwind.exit_policy, "fixed_rr")
        self.assertIsNone(unwind.carry_exit_funding_percentile)

        self.assertEqual(carry.model_id, "funding_carry.interval.1h.v2")
        self.assertEqual(unwind.model_id, "funding_unwind.reversion.1h.v2")
        self.assertNotEqual(carry.strategy_id, unwind.strategy_id)
        self.assertIn(
            "research/hypotheses/funding-carry.yaml",
            carry.contract_evidence)
        self.assertIn(
            "research/hypotheses/funding-unwind.yaml",
            unwind.contract_evidence)
        self.assertNotEqual(carry.contract_evidence, unwind.contract_evidence)

        self.assertIn("perp_index_basis_pct", carry.required_fields)
        self.assertNotIn("perp_index_basis_pct", unwind.required_fields)


class ContractExecutionTests(unittest.TestCase):
    def assert_fires(self, strategy_id, setup_type, direction, row):
        cfg = shadow._research_cfg(valid_config(), strategy_id)
        evidence = strategy.setup_evidence(row, cfg)
        self.assertTrue(
            evidence[setup_type][direction],
            f"{strategy_id} did not fire: {evidence}")

    def test_each_contract_has_an_executable_synthetic_signal(self):
        cases = {
            "momentum": ("trend_continuation", "long", market_row()),
            "flush-fade": (
                "flush_reversion", "long",
                market_row(mom_1h_pct=-2.0, trend_1h="flat",
                           trend_4h="flat")),
            "funding-carry": (
                "carry", "short",
                market_row(trend_1h="flat", trend_4h="flat")),
            "funding-unwind": (
                "positioning_unwind", "short",
                market_row(trend_1h="flat", trend_4h="flat")),
            "trend-multiday": ("trend_follow", "long", market_row()),
            "ls-ratio-fade": ("positioning_fade", "long", market_row()),
            "scalp-maker": ("spread_capture", "long", market_row()),
        }
        for strategy_id, (setup, direction, row) in cases.items():
            with self.subTest(strategy=strategy_id):
                self.assert_fires(strategy_id, setup, direction, row)

    def test_funding_contracts_refuse_an_unknown_interval(self):
        for strategy_id, setup in (
                ("funding-carry", "carry"),
                ("funding-unwind", "positioning_unwind")):
            cfg = shadow._research_cfg(valid_config(), strategy_id)
            evidence = strategy.setup_evidence(
                market_row(funding_interval_hours=None), cfg)
            with self.subTest(strategy=strategy_id):
                self.assertFalse(evidence[setup]["long"])
                self.assertFalse(evidence[setup]["short"])

    def test_funding_contracts_share_direction_but_not_basis_eligibility(self):
        cases = (
            (market_row(
                funding_rate_pct=0.02, funding_percentile_30=90.0,
                perp_index_basis_pct=0.10, trend_1h="flat",
                trend_4h="flat"), "short"),
            (market_row(
                funding_rate_pct=-0.02, funding_percentile_30=10.0,
                perp_index_basis_pct=-0.10, trend_1h="flat",
                trend_4h="flat"), "long"),
        )
        for row, expected_direction in cases:
            evidence = {}
            for strategy_id, setup in (
                    ("funding-carry", "carry"),
                    ("funding-unwind", "positioning_unwind")):
                cfg = shadow._research_cfg(valid_config(), strategy_id)
                evidence[strategy_id] = strategy.setup_evidence(
                    row, cfg)[setup]
            with self.subTest(funding_rate=row["funding_rate_pct"]):
                self.assertEqual(
                    evidence["funding-carry"], evidence["funding-unwind"])
                self.assertTrue(
                    evidence["funding-carry"][expected_direction])

        row = market_row(
            perp_index_basis_pct=None, trend_1h="flat", trend_4h="flat")
        carry = BY_STRATEGY["funding-carry"]
        unwind = BY_STRATEGY["funding-unwind"]
        carry_evidence = strategy.setup_evidence(
            row, shadow._research_cfg(valid_config(), "funding-carry"))
        unwind_evidence = strategy.setup_evidence(
            row, shadow._research_cfg(valid_config(), "funding-unwind"))

        self.assertEqual(
            carry.missing_fields(row), ("perp_index_basis_pct",))
        self.assertEqual(unwind.missing_fields(row), ())
        self.assertEqual(
            carry_evidence["carry"], {"long": False, "short": False})
        self.assertTrue(unwind_evidence["positioning_unwind"]["short"])

    def test_only_realized_funding_events_are_credited(self):
        position = {
            "entry_price": 100.0, "direction": "short", "notional": 1000.0,
            "risk_usd": 10.0, "entry_ts": 0.0,
            "cost_components": {
                "entry_fee_pct": 0.0, "exit_fee_pct": 0.0,
                "spread_pct": 0.0, "entry_slippage_pct": 0.0,
                "stop_slippage_pct": 0.0, "funding_rate_pct": 0.02,
                "funding_interval_hours": 8.0, "next_funding_minutes": 60.0,
            },
            "funding_events": [
                {"timestamp_ms": 3_600_000, "rate_pct": 0.02,
                 "status": "realized"},
                {"timestamp_ms": 9 * 3_600_000, "rate_pct": 0.02,
                 "status": "realized"},
            ],
        }

        pnl, _ = shadow.ShadowEvaluator._paper_pnl(
            position, 100.0, "timeout", 9 * 3600.0)

        # Both events came from funding-rate-history, not a repeated forecast.
        self.assertAlmostEqual(pnl, 0.4, 8)


if __name__ == "__main__":
    unittest.main()
