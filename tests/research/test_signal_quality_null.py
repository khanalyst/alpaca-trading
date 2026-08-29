"""The conditional-return null must not manufacture edges, or erase them.

The screen exists to say whether a predicate carries information.  That makes
its null control load-bearing: an unmatched null turns the session's clock into
an apparent edge, and an over-matched null hides a real one.  These tests pin
both directions on corpora whose answer is known by construction.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import unittest
from zoneinfo import ZoneInfo

from agent.contracts.rule import entry_window_bounds, validate_rule_spec
from research.costs import diagnostic_backfill_policy
from research.edge_lab import _read_discovery_rows
from research.signal_quality import measure_signal_quality

NEW_YORK = ZoneInfo("America/New_York")
SYMBOLS = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")
SESSIONS = 24
BARS = 120
# The drift lives only here, and the rule may only enter here, so a null drawn
# from elsewhere in the session is comparing two different clocks.
OPENING_BARS = 25
DRIFT_BPS = 6.0


def _uniform(*parts: object) -> float:
    """Deterministic white noise in [-1, 1); no seeded RNG, so it is stable."""
    key = ":".join(str(part) for part in parts).encode("utf-8")
    return (int(hashlib.sha256(key).hexdigest()[:8], 16) % 2001 - 1000) / 1000.0


def _rows(symbol: str, day: int, opening: datetime,
          closes: list[float]) -> list[dict]:
    rows, opened = [], closes[0]
    for index, close in enumerate(closes):
        stamp = opening + timedelta(minutes=index)
        rows.append({
            "kind": "bar", "provider": "test", "feed": "iex", "symbol": symbol,
            "timestamp": stamp.isoformat(),
            "as_of": (stamp + timedelta(minutes=1)).isoformat(),
            "observed_at": (stamp + timedelta(minutes=1)).isoformat(),
            "open": round(opened, 4),
            "high": round(max(opened, close) + .02, 4),
            "low": round(min(opened, close) - .02, 4),
            "close": round(close, 4), "volume": 1000 + (index % 7) * 100,
        })
        opened = close
    return rows


def _corpus(builder) -> list[dict]:
    rows, day, stamp = [], 0, datetime(2026, 1, 5, 9, 30, tzinfo=NEW_YORK)
    while day < SESSIONS:
        if stamp.weekday() < 5:
            for symbol in SYMBOLS:
                rows.extend(_rows(symbol, day, stamp.astimezone(timezone.utc),
                                  builder(symbol, day)))
            day += 1
        stamp += timedelta(days=1)
    return rows


def _drift_only(symbol: str, day: int) -> list[float]:
    """White noise plus a time-of-day drift confined to the entry window.

    Nothing here is predictable from the bars: any long entry inside the
    opening window earns the drift and the signal adds nothing to it.
    """
    price, closes = 100.0, []
    for index in range(BARS):
        drift = DRIFT_BPS if index < OPENING_BARS else 0.0
        price *= 1.0 + (drift + _uniform(symbol, day, index) * 12.0) / 10_000.0
        closes.append(price)
    return closes


def _conditional_edge(symbol: str, day: int) -> list[float]:
    """A real, session-varying edge: the open's direction keeps running.

    Each session picks a sign, moves that way through the entry window, and
    keeps going afterwards.  A same-minute draw from a *different* session has
    an independent sign, so the matched null cannot absorb this.
    """
    sign = 1.0 if _uniform("side", symbol, day) >= 0 else -1.0
    price, closes = 100.0, []
    for index in range(BARS):
        step = 8.0 * sign + _uniform(symbol, day, index) * 3.0
        price *= 1.0 + step / 10_000.0
        closes.append(price)
    return closes


def _spec(**changes) -> dict:
    return validate_rule_spec({
        "schema": "rule-strategy.v2", "family": "momentum_continuation",
        "side": "both", "lookback": 5, "slow_lookback": 40,
        "range_minutes": 15, "threshold_bps": 1.0, "compression_bps": 45.0,
        "zscore": 1.25, "volume_multiplier": 1.25, "atr_period": 14,
        "stop_atr": 1.0, "target_r": 1.5, "max_hold_bars": 90,
        "confirmation": "none", "confirmations": [],
        "entry_after_minutes": 0, "entry_before_minutes": OPENING_BARS,
        "min_atr_bps": 0.0, "max_atr_bps": 5000.0, **changes})


class MatchedNullControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = diagnostic_backfill_policy()
        cls.spec = _spec(side="long")
        cls.drift = _read_discovery_rows(_corpus(_drift_only))[1]
        cls.edge = _read_discovery_rows(_corpus(_conditional_edge))[1]
        cls.drift_quality = measure_signal_quality(
            cls.drift, cls.spec, policy=cls.policy, cost_hurdle_bps=17.0,
            horizons=(15, 30, 60))
        cls.edge_quality = measure_signal_quality(
            cls.edge, _spec(), policy=cls.policy, cost_hurdle_bps=17.0,
            horizons=(15, 30, 60))

    def test_time_of_day_drift_alone_is_not_reported_as_an_edge(self):
        """The regression: an unmatched null read +45 bps on this corpus."""
        for label, metrics in self.drift_quality["horizon_metrics"].items():
            with self.subTest(horizon=label):
                self.assertGreater(metrics["matched_count"], 0)
                # The raw conditional return is large: that is the drift.
                self.assertGreater(metrics["mean_forward_return_bps"], 20.0)
                # Against a clock-matched null, almost none of it survives.
                self.assertLess(abs(metrics["candidate_minus_control_bps"]),
                                .2 * metrics["mean_forward_return_bps"])
                self.assertLess(abs(metrics["candidate_minus_control_t_stat"]), 2.0)

    def test_a_real_session_varying_edge_still_clears_the_null(self):
        for label, metrics in self.edge_quality["horizon_metrics"].items():
            with self.subTest(horizon=label):
                self.assertGreater(metrics["candidate_minus_control_bps"], 0.0)
                self.assertGreater(metrics["candidate_minus_control_t_stat"], 3.0)

    def test_the_null_is_drawn_at_the_candidates_own_session_minute(self):
        after, before = entry_window_bounds(self.spec)
        for label, metrics in self.drift_quality["horizon_metrics"].items():
            with self.subTest(horizon=label):
                self.assertEqual(metrics["control_matching_counts"],
                                 {"cross_session_same_session_minute":
                                  metrics["matched_count"]})
                self.assertAlmostEqual(metrics["control_mean_session_minute"],
                                       metrics["candidate_mean_session_minute"],
                                       places=6)
                self.assertLessEqual(after, metrics["control_mean_session_minute"])
                self.assertLess(metrics["control_mean_session_minute"], before)

    def test_the_null_averages_a_pool_instead_of_one_draw(self):
        for label, metrics in self.drift_quality["horizon_metrics"].items():
            with self.subTest(horizon=label):
                self.assertGreater(metrics["control_pool_mean"], 1.0)

    def test_every_reported_mean_carries_its_own_error(self):
        for label, metrics in self.edge_quality["horizon_metrics"].items():
            with self.subTest(horizon=label):
                for name in ("forward_return_stdev_bps",
                             "forward_return_stderr_bps",
                             "forward_return_t_stat",
                             "candidate_minus_control_stdev_bps",
                             "candidate_minus_control_stderr_bps",
                             "candidate_minus_control_t_stat"):
                    self.assertIsNotNone(metrics[name], name)
                self.assertAlmostEqual(
                    metrics["forward_return_t_stat"],
                    metrics["mean_forward_return_bps"] /
                    metrics["forward_return_stderr_bps"])
                self.assertAlmostEqual(
                    metrics["after_hurdle_t_stat"],
                    metrics["mean_after_hurdle_bps"] /
                    metrics["forward_return_stderr_bps"])

    def test_an_empty_screen_reports_no_error_terms_rather_than_zeroes(self):
        empty = measure_signal_quality([], _spec(), cost_hurdle_bps=17.0)
        metrics = empty["horizon_metrics"]["5m"]
        for name in ("forward_return_stdev_bps", "forward_return_stderr_bps",
                     "forward_return_t_stat", "candidate_minus_control_t_stat",
                     "control_pool_mean", "control_mean_session_minute"):
            self.assertIsNone(metrics[name], name)


if __name__ == "__main__":
    unittest.main()
