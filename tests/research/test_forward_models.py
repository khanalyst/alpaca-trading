"""Executable evidence for every real-time forward outcome model."""

import unittest

from agent import shadow, strategy
from agent.forward_models import BY_STRATEGY
from tests.helpers import valid_config


def market_row(**overrides):
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
            "book_ts": 1_760_000_010_000,
            "book_mid": 100.0,
            "book_best_bid": 99.99,
            "book_best_ask": 100.01,
            "book_spread_pct": 0.02,
            "book_bid_depth_usd": 80_000.0,
            "book_ask_depth_usd": 20_000.0,
            "book_top_bid_size": 12.0,
            "book_top_ask_size": 4.0,
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
                self.assertTrue(model.validated)
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

    def test_favourable_funding_is_credited_on_the_observed_schedule(self):
        position = {
            "entry_price": 100.0, "direction": "short", "notional": 1000.0,
            "risk_usd": 10.0, "entry_ts": 0.0,
            "cost_components": {
                "entry_fee_pct": 0.0, "exit_fee_pct": 0.0,
                "spread_pct": 0.0, "entry_slippage_pct": 0.0,
                "stop_slippage_pct": 0.0, "funding_rate_pct": 0.02,
                "funding_interval_hours": 8.0, "next_funding_minutes": 60.0,
            },
        }

        pnl, _ = shadow.ShadowEvaluator._paper_pnl(
            position, 100.0, "timeout", 9 * 3600.0)

        # Settlements occur at +1h and +9h, both at or before the exit.
        self.assertAlmostEqual(pnl, 0.4, 8)


if __name__ == "__main__":
    unittest.main()
