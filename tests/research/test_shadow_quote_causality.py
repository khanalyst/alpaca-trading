"""Point-in-time quote visibility for IBR candidate spread admission."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from research.costs import ReplayPolicy
from research.ibr import IBRConfig, _candidate_spread_bps, replay_ibr
from research.market_data import normalize_quote

from .test_ibr import FREE, bars_for_day


def _quote(timestamp: datetime, bid: float, ask: float, *,
           observed_at: datetime | None = None):
    """Build a quote with explicit provider and availability provenance."""
    return normalize_quote({
        "symbol": "SPY",
        "timestamp": timestamp.isoformat(),
        "as_of": timestamp.isoformat(),
        "observed_at": (observed_at or timestamp).isoformat(),
        "provider": "alpaca",
        "feed": "sip",
        "bid": bid,
        "ask": ask,
    })


class ShadowQuoteCausalityTests(unittest.TestCase):
    def test_delayed_wide_quote_is_ignored_until_available(self):
        bars = bars_for_day()
        signal = bars[30]
        low_spread = _quote(signal.timestamp, 100.00, 100.10)
        wide_spread = _quote(
            signal.timestamp + timedelta(seconds=5), 99.00, 101.00,
            observed_at=signal.end + timedelta(minutes=4))

        # The wide quote's market timestamp is before the signal-bar close,
        # but its availability is after it.  The low quote is therefore the
        # only causal spread observation at the candidate boundary.
        self.assertAlmostEqual(
            _candidate_spread_bps(
                {"SPY": [low_spread, wide_spread]}, "SPY", signal,
                policy=ReplayPolicy(strict_market_data=False)),
            9.995,
            places=3,
        )
        later = replace(signal, timestamp=signal.timestamp + timedelta(minutes=4))
        self.assertAlmostEqual(
            _candidate_spread_bps(
                {"SPY": [low_spread, wide_spread]}, "SPY", later,
                policy=ReplayPolicy(strict_market_data=False)),
            200.0,
            places=6,
        )

        result = replay_ibr(
            bars,
            config=IBRConfig(
                costs=FREE,
                max_spread_bps=25.0,
                policy=ReplayPolicy(strict_market_data=False),
            ),
            quotes=[low_spread, wide_spread],
        )
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].signal_timestamp, signal.end)

    def test_current_wide_quote_rejects_breakout(self):
        bars = bars_for_day()
        signal = bars[30]
        quotes = [
            _quote(signal.timestamp, 100.00, 100.10),
            _quote(signal.timestamp + timedelta(seconds=5), 99.00, 101.00),
        ]
        result = replay_ibr(
            bars,
            config=IBRConfig(
                costs=FREE,
                max_spread_bps=25.0,
                policy=ReplayPolicy(strict_market_data=False),
            ),
            quotes=quotes,
        )
        self.assertEqual(result.trades, [])
        self.assertEqual(result.refusals[0].reason, "spread_above_limit")

    def test_delayed_candidate_bar_uses_its_observation_boundary(self):
        bars = bars_for_day()
        signal = bars[30]
        delayed_identity = replace(
            signal.identity,
            as_of=signal.end + timedelta(minutes=4),
            observed_at=signal.end + timedelta(minutes=4),
        )
        delayed_signal = replace(signal, identity=delayed_identity)
        wide = _quote(
            signal.timestamp + timedelta(seconds=5), 99.00, 101.00,
            observed_at=signal.end + timedelta(minutes=1))

        self.assertAlmostEqual(
            _candidate_spread_bps(
                {"SPY": [wide]}, "SPY", delayed_signal,
                policy=ReplayPolicy(strict_market_data=False)),
            200.0,
            places=6,
        )

    def test_quote_without_availability_is_silence(self):
        bars = bars_for_day()
        signal = bars[30]
        malformed = type("Quote", (), {
            "timestamp": signal.timestamp,
            "bid": 99.0,
            "ask": 101.0,
        })()
        self.assertIsNone(
            _candidate_spread_bps(
                {"SPY": [malformed]}, "SPY", signal,
                policy=ReplayPolicy(strict_market_data=False)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
