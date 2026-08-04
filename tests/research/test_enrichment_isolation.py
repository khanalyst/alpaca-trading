"""B0.5 gate G1: enrichment is recorded, and it changes nothing.

The batch that adds observation fields is the only batch in the research
programme whose value decays if it is deferred, because a snapshot without
open interest in it can never be made to have open interest in it. That makes
it tempting to ship quickly, and shipping it quickly is exactly how a field
leaks into the prompt.

A leaked field is not a small bug. Changing the prompt changes model
behaviour, which forks the comparability of every observation recorded before
the change against every observation recorded after it. The corpus would
split into a pre-enrichment and a post-enrichment population and every
cross-period comparison in the programme would be contaminated - silently,
with no error and no failing test.

So the design commitment is mechanical rather than aspirational: enrichment
lives under ``_enrichment`` keys, and the prompt builder strips those keys on
the way out. These tests hold that boundary.
"""

import json
import unittest

from agent import brain, market, strategy
from tests.helpers import valid_config


def symbol_snapshot(**overrides) -> dict:
    """A snapshot row that satisfies the momentum continuation contract."""
    base = {
        "price": 100.0,
        "trend_15m": "up", "trend_1h": "up", "trend_4h": "up",
        "mom_1h_pct": 0.6, "mom_15m_pct": 0.3,
        "atr_1h_pct": 0.8, "atr_1h_ratio": 1.0,
        "ema20_1h_dist_pct": 0.2,
        "range_pos_pct": 60.0, "relative_volume_1h": 1.4,
        "funding_rate_pct": 0.005, "funding_interval_hours": 8,
        "funding_percentile_30": 50, "funding_samples_30": 30,
        "funding_mean_30_pct": 0.004,
        "perp_index_basis_pct": 0.01, "open_interest_musd": 500.0,
        "oi_change_1h_pct": 1.2, "oi_change_4h_pct": 3.4,
        "swing_high_pct": 2.0, "swing_low_pct": 2.0,
        "spread_pct": 0.02, "signal_ts": 1_760_000_000_000,
    }
    base.update(overrides)
    return base


def enriched(snap: dict) -> dict:
    """The same row, carrying every field B0.5 records but withholds."""
    out = dict(snap)
    out["_enrichment"] = {
        "realised_vol_8": 0.91,
        "realised_vol_96": 0.62,
        "realised_vol_ratio_8_96": 1.47,
        "utc_hour": 14,
        "book_mid": 100.01,
        "book_spread_pct": 0.019,
        "book_bid_depth_usd": 412_000.0,
        "book_ask_depth_usd": 398_000.0,
    }
    return out


def execution_enriched(snap: dict) -> dict:
    """Add the executable fields required by current paper outcome models."""
    out = enriched(snap)
    out["_enrichment"].update({
        "ticker_ts": 1_760_000_000_000,
        "ticker_best_bid": 99.99,
        "ticker_best_ask": 100.01,
        "ticker_age_seconds": 0.1,
        "book_ts": 1_760_000_000_000,
        "book_best_bid": 99.99,
        "book_best_ask": 100.01,
        "book_top_bid_size": 5_000.0,
        "book_top_ask_size": 5_000.0,
        "book_bid_levels": [[99.99, 5_000.0], [99.98, 5_000.0]],
        "book_ask_levels": [[100.01, 5_000.0], [100.02, 5_000.0]],
        "book_contract_size": 1.0,
        "book_age_seconds": 0.1,
        "book_observation_error": None,
    })
    return out


class EnrichmentNeverReachesThePrompt(unittest.TestCase):
    """D1, asserted on the exact bytes the provider receives."""

    def test_user_message_omits_every_enrichment_value(self):
        snap = {"BTC/USDT:USDT": enriched(symbol_snapshot())}
        message = brain.LLM._user_message(snap, {"equity_usdt": 1000.0}, 3)

        self.assertNotIn("_enrichment", message)
        # The values themselves, not just the container key. A future
        # refactor that flattened enrichment into the row would still pass
        # a key-only check while forking the corpus exactly as before.
        for leaked in ("realised_vol_8", "realised_vol_ratio_8_96",
                       "utc_hour", "book_bid_depth_usd", "1.47", "412000"):
            self.assertNotIn(leaked, message)

    def test_user_message_is_byte_identical_with_and_without_enrichment(self):
        plain = {"BTC/USDT:USDT": symbol_snapshot()}
        rich = {"BTC/USDT:USDT": enriched(symbol_snapshot())}
        portfolio = {"equity_usdt": 1000.0}

        self.assertEqual(
            brain.LLM._user_message(plain, portfolio, 3),
            brain.LLM._user_message(rich, portfolio, 3),
        )

    def test_market_context_enrichment_is_stripped_too(self):
        """Portfolio-level enrichment (BTC reference) is withheld as well."""
        rich = {
            "BTC/USDT:USDT": symbol_snapshot(),
            "_market_context": {
                "benchmark": "BTC/USDT:USDT",
                "regime": "trending",
                "_enrichment": {"btc_ref_return_1_pct": -0.42},
            },
        }
        message = brain.LLM._user_message(rich, {}, 3)

        self.assertIn("_market_context", message)
        self.assertIn("regime", message)          # ordinary context survives
        self.assertNotIn("btc_ref_return_1_pct", message)
        self.assertNotIn("-0.42", message)

    def test_stripping_does_not_mutate_the_caller_snapshot(self):
        """The journal must still see enrichment after the prompt is built."""
        rich = {"BTC/USDT:USDT": enriched(symbol_snapshot())}
        brain.LLM._user_message(rich, {}, 3)

        self.assertIn("_enrichment", rich["BTC/USDT:USDT"])
        self.assertEqual(
            rich["BTC/USDT:USDT"]["_enrichment"]["utc_hour"], 14)


class DecisionsAreByteIdenticalUnderEnrichment(unittest.TestCase):
    """The acceptance criterion the batch is gated on."""

    def test_setup_evidence_is_unchanged(self):
        cfg = valid_config()
        plain = symbol_snapshot()
        rich = enriched(symbol_snapshot())

        self.assertEqual(
            json.dumps(strategy.setup_evidence(plain, cfg), sort_keys=True),
            json.dumps(strategy.setup_evidence(rich, cfg), sort_keys=True),
        )

    def test_build_setup_plan_is_unchanged(self):
        cfg = valid_config()
        decision = {
            "symbol": "BTC/USDT:USDT", "action": "open", "direction": "long",
            "setup_type": "trend_continuation", "confidence": 0.8,
            "invalidation_anchor": "structure", "exit_policy": "fixed_rr",
        }
        plain = strategy.enrich_snapshot(symbol_snapshot(), cfg)
        rich = strategy.enrich_snapshot(enriched(symbol_snapshot()), cfg)

        plan_plain, why_plain = strategy.build_setup_plan(
            decision, plain, cfg)
        plan_rich, why_rich = strategy.build_setup_plan(decision, rich, cfg)

        self.assertEqual(why_plain, why_rich)
        self.assertEqual(
            json.dumps(plan_plain, sort_keys=True, default=str),
            json.dumps(plan_rich, sort_keys=True, default=str),
        )

    def test_evidence_fingerprint_is_unchanged(self):
        """Setup memory keys off this; a fork here re-opens closed setups."""
        cfg = valid_config()
        plain = strategy.enrich_snapshot(symbol_snapshot(), cfg)
        rich = strategy.enrich_snapshot(enriched(symbol_snapshot()), cfg)

        self.assertEqual(
            strategy.evidence_fingerprint(plain),
            strategy.evidence_fingerprint(rich),
        )


class PromptVersionIsUnchanged(unittest.TestCase):
    """If this fails, a field reached the system prompt."""

    def test_prompt_version_matches_the_pinned_b05_value(self):
        cfg = valid_config()
        system = brain.build_system(cfg)

        # Pinned by hand. A value derived from build_system() would agree
        # with itself no matter what the prompt said, which is the one thing
        # this assertion must not do.
        #
        # b9a09a9dc3bc59ec -> 8d99182f0dcea1c4 in batch 6.1, which removed
        # structure_target from the model's option set and tightened the
        # invalidation-anchor rule.
        # 8d99182f0dcea1c4 -> 0f85dcf00f2acd36 in batch 6.3, which replaced
        # the unlabelled `other` escape hatch with a versioned list of
        # registered hypotheses injected into the prompt.
        # 0f85dcf00f2acd36 -> c61905480bfef239 in R4-02, which made the
        # hypothesis descriptions parameter-backed and explicitly stated
        # that the registered contract threshold is the tested point.
        #
        # Both are deliberate attribution forks: observations either side of
        # them describe different decision spaces and must never be pooled.
        # strategy.version moved phase1-v2 -> phase1-v3 with the second, so
        # the fork is visible in the journal without reading a prompt hash.
        # c61905480bfef239 -> 8790863bfadaf741 in R4-03, which added the
        # bounded registered numeric research-proposal schema, then
        # 8790863bfadaf741 -> 913e4bb0572a6e4e when proposal reasoning became
        # required and persisted with the adaptive setting history.
        # 913e4bb0572a6e4e -> 59f873815ffd3646 when each adaptive setting began
        # naming its exact target parameter and semantic bounds in the prompt.
        # 59f873815ffd3646 -> 2e6c312ae9d80316 when the bounded research
        # selector enumerated only registered single-axis strategy settings
        # and made its no-live-execution authority explicit.
        # That changes the model's proposal domain, so attribution must fork.
        # 2e6c312ae9d80316 -> ffbd0360d5f624c9 when the selectable research
        # catalog changed: feasible axes, stricter confidence floors, and
        # maker penetration.
        # ffbd0360d5f624c9 -> 608188bfb1314d2e when nine momentum arms were
        # retired on live demo evidence: the three observable confidence
        # floors (the 0.70-0.80 bucket went 0 for 13, so the axis runs the
        # wrong way) and both breakout discriminators (2 range_breakout
        # trades in 24 round trips, so neither is measurable). The selector's
        # menu is part of the prompt, so removing options changes the
        # model's decision space and attribution forks - which is the point:
        # evidence collected against the old menu must not pool with the new.
        # This constant moves only in a batch that intends it.
        self.assertEqual(
            brain.prompt_version(system),
            "608188bfb1314d2e",
            "the system prompt changed; enrichment must never touch it. "
            "If a later batch versions the prompt deliberately, update this "
            "constant in that batch and fork attribution on purpose.")

    def test_prompt_version_is_a_pure_function_of_the_system_text(self):
        cfg = valid_config()
        self.assertEqual(
            brain.prompt_version(brain.build_system(cfg)),
            brain.prompt_version(brain.build_system(valid_config())),
        )


class EnrichmentDegradesToNull(unittest.TestCase):
    """A statistics outage must not be able to stop trading."""

    def test_realised_vol_is_none_when_history_is_too_short(self):
        self.assertEqual(
            market.realised_vol_enrichment([100.0, 101.0], utc_hour=3),
            {"realised_vol_8": None, "realised_vol_96": None,
             "realised_vol_ratio_8_96": None, "utc_hour": 3},
        )

    def test_realised_vol_survives_non_finite_closes(self):
        closes = [100.0, float("nan"), 102.0, None] + [
            100.0 + i * 0.1 for i in range(120)]
        out = market.realised_vol_enrichment(closes, utc_hour=0)

        self.assertIsNotNone(out["realised_vol_8"])
        self.assertIsNotNone(out["realised_vol_96"])

    def test_ratio_is_none_rather_than_infinite_on_zero_baseline(self):
        out = market.realised_vol_enrichment([100.0] * 200, utc_hour=9)
        self.assertIsNone(out["realised_vol_ratio_8_96"])


if __name__ == "__main__":
    unittest.main()
