"""Regression coverage for the randomized null's causal entry clock."""

from datetime import date, datetime, time, timedelta, timezone
import unittest

from research.costs import QUOTE, ReplayPolicy, index_quotes
from research.edge_discovery_core import (
    _null_admissible_entry_indices,
    null_control_account,
)
from research.market_data import (
    EventIdentity,
    OptionContract,
    OptionSnapshot,
    UnderlyingBar,
    normalize_quote,
)


SESSION = date(2026, 1, 5)
OPEN = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
ENTRY = OPEN + timedelta(minutes=1)
SPEC = {"target_r": 2, "max_hold_bars": 1}
REFERENCE = [{
    "symbol": "SPY",
    "session_date": SESSION.isoformat(),
    "underlying_entry": 100.0,
    "stop_price": 90.0,
    "stop_distance": 10.0,
    "direction": "long",
    "no_trade": False,
}]
CONTRACT = OptionContract(
    symbol="SPY260116C00100000",
    underlying="SPY",
    expiration=date(2026, 1, 16),
    strike=100.0,
    right="call",
    multiplier=100,
    currency="USD",
    provider="alpaca",
    feed="opra",
)


def _identity(timestamp, *, feed="iex", observed_at=None):
    observed = timestamp if observed_at is None else observed_at
    return EventIdentity(
        provider="alpaca",
        feed=feed,
        as_of=timestamp,
        observed_at=observed,
        session_date=SESSION,
    )


def _bar(minute, *, observed_delay_seconds=60):
    timestamp = OPEN + timedelta(minutes=minute)
    end = timestamp + timedelta(minutes=1)
    return UnderlyingBar(
        symbol="SPY",
        timestamp=timestamp,
        open=100.0,
        high=100.1,
        low=99.9,
        close=100.0,
        volume=1_000,
        identity=EventIdentity(
            provider="alpaca",
            feed="iex",
            as_of=end,
            observed_at=timestamp + timedelta(seconds=observed_delay_seconds),
            session_date=SESSION,
        ),
    )


def _bars(count=3):
    return [_bar(minute) for minute in range(count)]


def _quote(timestamp, bid, ask, *, observed_at=None):
    observed = timestamp if observed_at is None else observed_at
    return normalize_quote({
        "symbol": "SPY",
        "timestamp": timestamp.isoformat(),
        "as_of": timestamp.isoformat(),
        "observed_at": observed.isoformat(),
        "bid": bid,
        "ask": ask,
        "provider": "alpaca",
        "feed": "iex",
    })


def _option(timestamp, bid, ask, *, underlying_price):
    return OptionSnapshot(
        contract=CONTRACT,
        timestamp=timestamp,
        bid=bid,
        ask=ask,
        last=(bid + ask) / 2.0,
        underlying_price=underlying_price,
        identity=_identity(timestamp, feed="opra"),
        bid_size=5,
        ask_size=5,
        volume=20,
        open_interest=100,
    )


def _account(bars, *, vehicle="equity", quotes=(), snapshots=(), policy=None,
             spec=SPEC):
    return null_control_account(
        bars,
        snapshots,
        spec,
        vehicle=vehicle,
        reference_rows=REFERENCE,
        account_id=f"null-entry-clock-{vehicle}",
        fixed_quantity=1,
        quotes=quotes,
        policy=ReplayPolicy() if policy is None else policy,
    )


class NullEntryClockTests(unittest.TestCase):
    def test_sampled_boundary_and_quote_are_the_executed_entry(self):
        bars = _bars()
        quotes = [
            _quote(ENTRY, 100.9, 101.0),
            _quote(ENTRY + timedelta(minutes=1), 108.9, 109.0),
            _quote(ENTRY + timedelta(minutes=2), 99.9, 100.0),
        ]
        admissible = _null_admissible_entry_indices(
            bars,
            SPEC,
            direction="long",
            policy=ReplayPolicy(),
            vehicle="equity",
            snapshots=(),
            quote_index=index_quotes(quotes),
        )
        self.assertEqual(admissible, [(1, ENTRY)])

        row = _account(bars, quotes=quotes)["rows"][0]
        self.assertFalse(row["no_trade"])
        self.assertEqual(row["entry_timestamp"], ENTRY.isoformat())
        self.assertEqual(row["entry_reference"], 101.0)
        self.assertEqual(row["entry_fill_source"], QUOTE)

    def test_boundary_quote_can_fill_before_entry_bar_completion(self):
        bars = _bars()
        row = _account(bars, quotes=[
            _quote(ENTRY, 100.9, 101.0),
            _quote(ENTRY + timedelta(minutes=2), 99.9, 100.0),
        ])["rows"][0]
        self.assertFalse(row["no_trade"])
        self.assertEqual(row["entry_timestamp"], ENTRY.isoformat())
        self.assertEqual(row["entry_reference"], 101.0)

    def test_invisible_entry_open_cannot_supply_bar_fallback(self):
        row = _account(
            _bars(),
            policy=ReplayPolicy(strict_market_data=False),
        )["rows"][0]
        self.assertTrue(row["no_trade"])
        self.assertEqual(row["reject_reason"], "no_admissible_null_entry")

    def test_missing_stale_future_or_delayed_quote_fails_closed(self):
        exit_quote = _quote(ENTRY + timedelta(minutes=2), 99.9, 100.0)
        cases = {
            "missing": [],
            "stale": [_quote(ENTRY - timedelta(seconds=31), 100.9, 101.0)],
            "future": [_quote(ENTRY + timedelta(seconds=1), 100.9, 101.0)],
            "not_yet_observed": [_quote(
                ENTRY,
                100.9,
                101.0,
                observed_at=ENTRY + timedelta(seconds=1),
            )],
        }
        for label, entry_quotes in cases.items():
            with self.subTest(label=label):
                row = _account(
                    _bars(), quotes=[*entry_quotes, exit_quote],
                )["rows"][0]
                self.assertTrue(row["no_trade"])
                self.assertEqual(row["reject_reason"],
                                 "no_admissible_null_entry")

    def test_partial_minute_signal_readiness_keeps_its_clock_and_deadline(self):
        bars = [_bar(0, observed_delay_seconds=90),
                _bar(1), _bar(2), _bar(3)]
        entry_at = OPEN + timedelta(minutes=1, seconds=30)
        exit_at = OPEN + timedelta(minutes=4)
        policy = ReplayPolicy(
            max_market_data_age_seconds=0,
            force_flat_time=time(9, 34),
        )
        spec = {"target_r": 2, "max_hold_bars": 2}
        quotes = [_quote(entry_at, 100.9, 101.0),
                  _quote(exit_at, 99.9, 100.0)]
        admissible = _null_admissible_entry_indices(
            bars,
            spec,
            direction="long",
            policy=policy,
            vehicle="equity",
            snapshots=(),
            quote_index=index_quotes(quotes),
        )
        self.assertEqual(admissible, [(2, entry_at)])

        row = _account(
            bars, quotes=quotes, policy=policy, spec=spec,
        )["rows"][0]
        self.assertFalse(row["no_trade"])
        self.assertEqual(row["entry_timestamp"], entry_at.isoformat())
        self.assertEqual(row["exit_timestamp"], exit_at.isoformat())
        self.assertEqual(row["canonical_exit_reason"], "session_force_flat")

    def test_option_entry_uses_the_sampled_snapshot_without_an_extra_shift(self):
        bars = _bars()
        snapshots = [
            _option(ENTRY, 2.0, 2.1, underlying_price=101.0),
            _option(ENTRY + timedelta(minutes=1), 9.0, 9.1,
                    underlying_price=109.0),
            _option(ENTRY + timedelta(minutes=2), 2.4, 2.5,
                    underlying_price=100.0),
        ]
        admissible = _null_admissible_entry_indices(
            bars,
            SPEC,
            direction="long",
            policy=ReplayPolicy(),
            vehicle="option",
            snapshots=snapshots,
            quote_index=None,
        )
        self.assertEqual(admissible, [(1, ENTRY)])

        row = _account(
            bars, vehicle="option", snapshots=snapshots,
        )["rows"][0]
        self.assertFalse(row["no_trade"])
        self.assertEqual(row["entry_timestamp"], ENTRY.isoformat())
        self.assertEqual(row["entry_reference"], 2.1)
        self.assertEqual(row["entry_fill_source"], QUOTE)


if __name__ == "__main__":
    unittest.main()
