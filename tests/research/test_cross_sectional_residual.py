"""Focused parity checks for the fixed-SPY residual rule family."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from agent.contracts.rule import (
    CROSS_SECTIONAL_BENCHMARK, evaluate_rule_signal,
    evaluate_rule_signal_trace, generate_rule_signal, rule_behavior_identity,
    rule_variant_id, rule_vehicle_executable, validate_rule_spec,
)
from agent.engine_cycle import _rule_runtime_bars
from research.costs import ReplayPolicy
from research.factory_core import _simulate_trade, simulate_account
from research.fit_diagnostics import measure_fit_diagnostics
from research.live_shadow import ShadowConfig, ShadowRunner
from research.market_data import normalize_underlying_bar
from research.signal_quality import measure_signal_quality
from research.strategy_factory import _signal_quality_screen_worker


SPEC = validate_rule_spec({
    "family": "cross_sectional_residual",
    "lookback": 3,
    "slow_lookback": 5,
    "atr_period": 3,
    "threshold_bps": 5.0,
    "max_hold_bars": 1,
})
BAR_FALLBACK = ReplayPolicy(strict_market_data=False)


def mapping_bars(symbol: str, closes: list[float], *, day: int = 28) -> list[dict]:
    start = datetime(2026, 8, day, 13, 30, tzinfo=timezone.utc)
    result = []
    previous = closes[0]
    for index, close in enumerate(closes):
        stamp = start + timedelta(minutes=index)
        result.append({
            "symbol": symbol,
            "timestamp": stamp.isoformat(),
            "as_of": (stamp + timedelta(minutes=1)).isoformat(),
            "observed_at": (stamp + timedelta(minutes=1)).isoformat(),
            "provider": "test",
            "feed": "iex",
            "open": previous,
            "high": max(previous, close) + 0.1,
            "low": min(previous, close) - 0.1,
            "close": close,
            "volume": 1_000,
        })
        previous = close
    return result


def normalized_bars(symbol: str, closes: list[float], *, day: int = 28):
    rows = mapping_bars(symbol, closes, day=day)
    # Bar-open visibility is an explicit fixture concession for the replay
    # mechanics under test; the signal itself still uses completed prefixes.
    return [normalize_underlying_bar({**row,
                                      "as_of": row["timestamp"],
                                      "observed_at": row["timestamp"]})
            for row in rows]


class CrossSectionalRuleTests(unittest.TestCase):
    def setUp(self):
        self.spy = mapping_bars("SPY", [100, 100.05, 100.10, 100.20])

    def test_positive_and_negative_residuals_are_directional(self):
        long_rows = mapping_bars("QQQ", [100, 100.2, 100.4, 101.0])
        short_rows = mapping_bars("QQQ", [100, 99.9, 99.7, 99.4])
        context = {CROSS_SECTIONAL_BENCHMARK: tuple(self.spy)}

        long_signal = evaluate_rule_signal(
            long_rows, SPEC, bars_by_symbol=context, symbol="QQQ")
        short_signal = evaluate_rule_signal(
            short_rows, SPEC, bars_by_symbol=context, symbol="QQQ")

        self.assertEqual(long_signal["direction"], "long")
        self.assertEqual(short_signal["direction"], "short")
        self.assertEqual(long_signal["benchmark_symbol"], "SPY")
        self.assertGreater(long_signal["residual_return"], 0)
        self.assertLess(short_signal["residual_return"], 0)
        self.assertEqual(len(long_signal["market_context_digest"]), 64)
        self.assertEqual(
            long_signal["candidate_behavior_identity"],
            rule_behavior_identity(
                SPEC,
                market_context_digest=long_signal["market_context_digest"]),
        )

    def test_missing_stale_and_misaligned_spy_fail_closed_with_trace_reasons(self):
        subject = mapping_bars("QQQ", [100, 100.2, 100.4, 101.0])
        cases = {
            "benchmark_context_missing": {},
            "benchmark_context_stale": {"SPY": tuple(self.spy[:-1])},
            "benchmark_context_misaligned": {
                "SPY": tuple([self.spy[0], self.spy[2], self.spy[3]])},
        }
        for expected, context in cases.items():
            with self.subTest(expected=expected):
                trace = evaluate_rule_signal_trace(
                    subject, SPEC, bars_by_symbol=context, symbol="QQQ")
                self.assertIsNone(trace["signal"])
                self.assertEqual(trace["terminal_stage"], "family_predicate")
                self.assertEqual(trace["stages"][-1]["reason"], expected)

    def test_missing_subject_or_benchmark_close_fails_closed(self):
        subject = mapping_bars("QQQ", [100, 100.2, 100.4, 101.0])
        cases = (
            ([{**subject[0], "close": None}, *subject[1:]], self.spy),
            (subject, [{**self.spy[0], "close": None}, *self.spy[1:]]),
        )
        for subject_rows, benchmark_rows in cases:
            with self.subTest(subject_missing=subject_rows[0]["close"] is None):
                trace = evaluate_rule_signal_trace(
                    subject_rows, SPEC,
                    bars_by_symbol={"SPY": tuple(benchmark_rows)},
                    symbol="QQQ")
                self.assertIsNone(trace["signal"])
                self.assertEqual(trace["stages"][-1]["reason"],
                                 "cross_sectional_price_unavailable")

    def test_spy_against_itself_is_valid_context_but_never_directional(self):
        trace = evaluate_rule_signal_trace(
            self.spy, SPEC,
            bars_by_symbol={"SPY": tuple(self.spy)}, symbol="SPY")
        self.assertIsNone(trace["signal"])
        self.assertEqual(trace["stages"][-1]["reason"],
                         "residual_threshold_not_met")
        self.assertEqual(trace["market_context"]["residual_return"], 0.0)

    def test_existing_family_output_and_identity_ignore_market_context(self):
        spec = validate_rule_spec({
            "family": "momentum_continuation", "lookback": 3,
            "slow_lookback": 5, "atr_period": 3, "threshold_bps": 5.0,
        })
        rows = mapping_bars("QQQ", [100, 100.2, 100.4, 101.0, 101.2])
        baseline = evaluate_rule_signal(rows, spec)
        contextual = evaluate_rule_signal(
            rows, spec, bars_by_symbol={"SPY": tuple(reversed(self.spy))},
            symbol="QQQ")
        self.assertEqual(contextual, baseline)
        self.assertEqual(rule_behavior_identity(
            spec, market_context_digest="different"), rule_variant_id(spec))

    def test_cross_sectional_family_is_shares_only(self):
        self.assertTrue(rule_vehicle_executable(SPEC, "equity"))
        self.assertTrue(rule_vehicle_executable(SPEC, "shares"))
        self.assertFalse(rule_vehicle_executable(SPEC, "option"))
        config = {"strategy": {"id": "rule", "rule_spec": SPEC,
                               "execution_mode": "options"}}
        self.assertIsNone(generate_rule_signal(
            "QQQ", mapping_bars("QQQ", [100, 100.2, 100.4, 101.0]),
            config=config, bars_by_symbol={"SPY": tuple(self.spy)}))


class CrossSectionalIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.qqq = normalized_bars(
            "QQQ", [100, 100.2, 100.4, 101.0, 101.1, 101.2, 101.3])
        self.spy = normalized_bars(
            "SPY", [100, 100.05, 100.10, 100.20, 100.25, 100.30, 100.35])
        self.context = {"SPY": tuple(self.spy)}

    def test_runtime_and_research_use_the_same_signal_context(self):
        now = self.qqq[3].end
        prepared = _rule_runtime_bars(self.qqq[:4], SPEC, now)
        benchmark = _rule_runtime_bars(self.spy[:4], SPEC, now)
        self.assertIsNotNone(prepared)
        self.assertIsNotNone(benchmark)
        runtime = generate_rule_signal(
            "QQQ", prepared[0], now=now,
            config={"strategy": {"id": "rule", "rule_spec": SPEC,
                                 "execution_mode": "shares"}},
            bars_by_symbol={"SPY": tuple(benchmark[0])})
        research = _simulate_trade(
            self.qqq, SPEC, [], "equity", policy=BAR_FALLBACK,
            bars_by_symbol=self.context)
        self.assertEqual(research["execution_disposition"], "candidate")
        self.assertEqual(research["direction"], runtime["direction"])
        self.assertEqual(research["benchmark_symbol"], runtime["benchmark_symbol"])
        self.assertEqual(research["market_context_digest"],
                         runtime["market_context_digest"])
        self.assertEqual(research["candidate_behavior_identity"],
                         runtime["candidate_behavior_identity"])

    def test_factory_records_explicit_missing_benchmark_no_signal(self):
        account = simulate_account(
            self.qqq, [], SPEC, vehicle="equity", account_id="missing-spy",
            policy=BAR_FALLBACK, bars_by_symbol={})
        self.assertEqual(len(account["rows"]), 1)
        row = account["rows"][0]
        self.assertEqual(row["execution_disposition"], "no_signal")
        self.assertEqual(row["no_signal_reason"], "benchmark_context_missing")
        self.assertEqual(row["benchmark_symbol"], "SPY")
        self.assertIn(row["candidate_behavior_identity"], row["opportunity_id"])

    def test_fit_diagnostics_and_factory_screen_use_synchronized_context(self):
        corpus = [*self.qqq, *self.spy]
        quality = measure_signal_quality(
            corpus, SPEC, policy=BAR_FALLBACK, horizons=(1,))
        diagnostic = measure_fit_diagnostics(
            corpus, SPEC, policy=BAR_FALLBACK)
        self.assertGreater(quality["event_count"], 0)
        self.assertEqual(quality["market_context"]["status"], "complete")
        self.assertGreater(diagnostic["first_signal"]["signals"], 0)
        self.assertEqual(diagnostic["market_context"]["status"], "complete")

        second_day = normalized_bars(
            "QQQ", [100, 100.1, 100.2, 100.3], day=29)
        result = _signal_quality_screen_worker({
            "bars": [*self.qqq, *self.spy, *second_day],
            "snapshots": [], "quotes": [], "specs": [SPEC],
            "policy": BAR_FALLBACK,
            "hypothesis": {"hypothesis_id": "cross-sectional"},
        })
        record = result["screens"][rule_variant_id(SPEC)]
        self.assertEqual(record["status"], "complete_actionable_signal")
        self.assertEqual(record["reason"], "actionable_signal_present")

    def test_fit_diagnostics_report_missing_context_as_unknown(self):
        quality = measure_signal_quality(
            self.qqq, SPEC, policy=BAR_FALLBACK, horizons=(1,))
        diagnostic = measure_fit_diagnostics(
            self.qqq, SPEC, policy=BAR_FALLBACK)
        self.assertEqual(quality["event_count"], 0)
        self.assertEqual(quality["market_context"]["status"], "unknown")
        self.assertEqual(quality["market_context"]["reason"],
                         "benchmark_context_missing")
        self.assertEqual(diagnostic["first_signal"]["signals"], 0)
        self.assertEqual(diagnostic["market_context"]["status"], "unknown")
        self.assertEqual(diagnostic["market_context"]["reason"],
                         "benchmark_context_missing")
        screened = _signal_quality_screen_worker({
            "bars": self.qqq, "snapshots": [], "quotes": [],
            "specs": [SPEC], "policy": BAR_FALLBACK,
            "hypothesis": {"hypothesis_id": "missing-cross-context"},
        })["screens"][rule_variant_id(SPEC)]
        self.assertEqual(screened["status"], "unknown")
        self.assertEqual(screened["reason"], "benchmark_context_missing")

    def test_shadow_consumes_synchronized_spy_and_refuses_missing_context(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        runner = ShadowRunner(ShadowConfig(
            root / "corpus.csv", root / "edge.sqlite3", root / "shadow.sqlite3"))
        variant = rule_variant_id(SPEC)
        candidate = {
            "candidate_id": "cross-sectional",
            "strategy_id": "rule",
            "variant_id": variant,
            "vehicle": "equity",
            "config": {
                "strategy": {"id": "rule", "version": "v1",
                             "variant_id": variant, "rule_spec": SPEC,
                             "execution_mode": "shares"},
                "broker": {"data_feed": "iex"},
                "risk": {"risk_per_trade_pct": 1.0},
                "execution": {}, "session": {},
            },
        }
        subject = mapping_bars(
            "QQQ", [100, 100.2, 100.4, 101.0])
        benchmark = mapping_bars(
            "SPY", [100, 100.05, 100.10, 100.20])
        event = subject[-1]
        quote_at = event["as_of"]
        quotes = {"QQQ": [{
            "symbol": "QQQ", "timestamp": quote_at, "as_of": quote_at,
            "observed_at": quote_at, "provider": "test", "feed": "iex",
            "bid": 100.9, "ask": 101.1,
        }]}
        plan = {"symbol": "QQQ", "direction": "long", "entry_price": 101.0,
                "stop_price": 100.0, "target_price": 103.0,
                "stop_distance": 1.0, "risk_usd": 100.0,
                "notional": 1_000.0, "shares": 10,
                "execution_profile": "shares"}
        risk = SimpleNamespace(vet_open=lambda *args, **kwargs: (plan, None))
        with patch("research.live_shadow.build_setup_plan",
                   return_value=(plan, None)), patch(
                       "research.live_shadow.RiskEngine", return_value=risk):
            accepted = runner._evaluate(
                candidate, event,
                {"QQQ": subject, "SPY": benchmark}, quotes, {})
            missing = runner._evaluate(
                candidate, event, {"QQQ": subject}, quotes, {})
        self.assertEqual(accepted[0], "open_incomplete")
        self.assertEqual(accepted[2]["signal"]["benchmark_symbol"], "SPY")
        self.assertEqual((missing[0], missing[1]),
                         ("no_data", "benchmark_context_missing"))


if __name__ == "__main__":
    unittest.main()
