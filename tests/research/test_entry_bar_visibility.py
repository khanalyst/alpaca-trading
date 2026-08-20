"""Adversarial point-in-time checks for next-bar entry opens."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from agent.config import ConfigError, validate_config
from research.costs import ReplayPolicy, diagnostic_backfill_policy
from research.costs import index_quotes
from research.edge_discovery_core import _null_reference_rows
from research.edge_lab import _read_discovery_rows
from research.factory_core import _simulate_trade
from research.fit_diagnostics import _fit_prefixes, measure_fit_diagnostics
from research.ibr import IBRConfig, IBRResult, replay_ibr
from research.gates import authorization_projection
from research.market_data import normalize_underlying_bar
from research.strategy_factory import null_control_account

from tests.research.test_bar_gap_scope import BAR_FALLBACK, SPEC, _bars
from tests.research.test_factory_end_to_end import ROOT_SPEC, edge_corpus
from tests.research.test_ibr import (FREE, bars_for_day, equity_quote,
                                     option_quote, permissive_config)


def _available_at(bar, timestamp):
    return replace(
        bar,
        identity=replace(bar.identity, as_of=timestamp,
                         observed_at=timestamp),
    )


def _delayed(bar, minutes=5):
    return replace(
        bar,
        identity=replace(bar.identity,
                         observed_at=bar.timestamp + timedelta(minutes=minutes)),
    )


class EntryBarVisibilityTests(unittest.TestCase):
    def test_backfill_label_comes_from_provenance_and_null_visibility_is_policy_bound(self):
        historical = [replace(
            bar, identity=replace(bar.identity,
                                   source_mode="historical_backfill"))
                      for bar in bars_for_day()]
        policy_off = ReplayPolicy(strict_market_data=False)
        result = replay_ibr(
            historical,
            config=permissive_config(stop_pct=.01, target_pct=.02,
                                     costs=FREE, policy=policy_off),
        )
        self.assertEqual(len(result.trades), 1)
        # The source label is truthful even though the diagnostic policy was
        # off; policy controls only whether the backfill is visible to replay.
        self.assertEqual(result.trades[0].evidence_mode,
                         "diagnostic_historical_backfill")

        # Remove the persisted anchor so this check exercises opening-bar
        # visibility rather than a separately recorded executable quote.
        trade = replace(result.trades[0], underlying_entry=None)
        result = IBRResult(vehicle="equity", trades=[trade])
        delayed = [replace(
            bar,
            identity=replace(
                bar.identity,
                source_mode="historical_backfill",
                observed_at=bar.timestamp + timedelta(minutes=5),
            ),
        ) if index == 31 else bar for index, bar in enumerate(historical)]
        hidden = _null_reference_rows(
            result, delayed, "equity", policy=policy_off)[0]
        visible = _null_reference_rows(
            result, delayed, "equity",
            policy=diagnostic_backfill_policy(ReplayPolicy()))[0]
        self.assertTrue(hidden["no_trade"])
        self.assertFalse(visible["no_trade"])
        self.assertEqual(hidden["evidence_mode"],
                         "diagnostic_historical_backfill")

    def test_backfill_requires_explicit_diagnostic_policy_and_cannot_authorize(self):
        observed = datetime(2026, 8, 19, tzinfo=timezone.utc)
        bars = [replace(
            bar,
            identity=replace(
                bar.identity, as_of=bar.end, observed_at=observed,
                source_mode="historical_backfill",
            ),
        ) for bar in bars_for_day()]
        base = permissive_config(stop_pct=.01, target_pct=.02, costs=FREE)

        refused = replay_ibr(bars, config=base)
        self.assertEqual(refused.trades, [])

        diagnostic = replay_ibr(
            bars,
            config=replace(
                base, policy=diagnostic_backfill_policy(base.policy)),
        )
        self.assertEqual(len(diagnostic.trades), 1)
        trade = diagnostic.trades[0]
        self.assertEqual(trade.evidence_mode,
                         "diagnostic_historical_backfill")
        row = vars(trade)
        projection = authorization_projection([row], vehicle="equity")
        self.assertEqual(projection["eligible"], [])
        self.assertEqual(projection["reasons"], {
            "diagnostic_historical_backfill": 1,
        })

    def test_ibr_rejects_delayed_open_but_accepts_exact_boundary(self):
        baseline = bars_for_day()
        config = permissive_config(stop_pct=.01, target_pct=.02, costs=FREE)
        self.assertEqual(len(replay_ibr(baseline, config=config).trades), 1)
        delayed = list(baseline)
        delayed[31] = _delayed(delayed[31])
        result = replay_ibr(delayed, config=config)
        self.assertEqual(result.trades, [])
        self.assertEqual(result.refusals[0].reason, "entry_bar_not_visible")

        exact = list(baseline)
        exact[31] = _available_at(exact[31], exact[31].timestamp)
        self.assertEqual(len(replay_ibr(exact, config=config).trades), 1)

    def test_strict_quote_backed_entry_does_not_need_delayed_bar_ohlc(self):
        bars = bars_for_day()
        signal = bars[30]
        bars[30] = replace(
            signal,
            identity=replace(signal.identity, as_of=signal.end,
                             observed_at=signal.end),
        )
        entry = bars[31]
        bars[31] = replace(
            entry,
            identity=replace(
                entry.identity,
                as_of=entry.end,
                observed_at=entry.end + timedelta(minutes=5),
            ),
        )
        # The delayed completed bar cannot provide its opening print, but a
        # fresh boundary quote can authorize the strict entry and the next bar
        # supplies a second boundary quote for the gap exit.
        result = replay_ibr(
            bars,
            config=IBRConfig(),
            quotes=[equity_quote(31, 100.9, 101.1),
                    equity_quote(32, 102.9, 103.1)],
        )
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].entry_fill_source, "quote")

    def test_delayed_signal_uses_observation_quote_and_ignores_preentry_ohlc(self):
        bars = bars_for_day()
        signal = bars[30]
        decision = signal.end + timedelta(seconds=5)
        bars[30] = replace(
            signal,
            identity=replace(signal.identity, as_of=signal.end,
                             observed_at=decision),
        )
        quotes = []
        for index, bar in enumerate(bars[31:], 31):
            quote_at = decision if index == 31 else bar.timestamp
            quote = equity_quote(index, 100.9, 101.1)
            quotes.append(replace(
                quote, timestamp=quote_at,
                identity=replace(quote.identity, as_of=quote_at,
                                 observed_at=quote_at),
            ))
        result = replay_ibr(bars, config=IBRConfig(policy=ReplayPolicy()),
                            quotes=quotes)
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.decision_timestamp, decision)
        self.assertEqual(trade.entry_timestamp, decision)
        # The next bar opened before the delayed decision and its high/low are
        # not allowed to trigger a resting leg before the entry instant.
        self.assertGreaterEqual(trade.exit_timestamp, bars[31].end)
        self.assertEqual(trade.entry_fill_source, "quote")

        delayed_quote = [replace(
            quote, identity=replace(quote.identity,
                                    observed_at=quote.timestamp + timedelta(seconds=5)),
        ) if quote.timestamp == decision else quote for quote in quotes]
        refused = replay_ibr(
            bars, config=IBRConfig(policy=ReplayPolicy()), quotes=delayed_quote)
        self.assertEqual(refused.trades, [])
        self.assertEqual(refused.refusals[0].reason, "no_quote_at_entry")

    def test_delayed_option_signal_uses_snapshot_spot_at_observation_entry(self):
        bars = bars_for_day()
        signal = bars[30]
        decision = signal.end + timedelta(seconds=5)
        bars[30] = replace(
            signal,
            identity=replace(signal.identity, as_of=signal.end,
                             observed_at=decision),
        )
        snapshots = {}
        for index, bar in enumerate(bars[31:], 31):
            quote_at = decision if index == 31 else bar.timestamp
            snapshots[quote_at] = option_quote(quote_at, bid=2.0, ask=2.1)
        result = replay_ibr(
            bars, config=IBRConfig(policy=ReplayPolicy()), vehicle="option",
            option_snapshots=snapshots)
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.decision_timestamp, decision)
        self.assertEqual(trade.entry_timestamp, decision)
        self.assertEqual(trade.entry_reference, 2.1)
        null_row = _null_reference_rows(result, bars, "option")[0]
        self.assertFalse(null_row["no_trade"])
        self.assertEqual(null_row["underlying_entry"],
                         trade.underlying_entry)

    def test_strict_option_underlying_quote_requires_policy_equity_feed(self):
        bars = bars_for_day()
        signal = bars[30]
        decision = signal.end + timedelta(seconds=5)
        bars[30] = replace(
            signal,
            identity=replace(signal.identity, as_of=signal.end,
                             observed_at=decision),
        )
        entry_bar = bars[32]
        bars[32] = replace(
            entry_bar,
            identity=replace(
                entry_bar.identity,
                as_of=entry_bar.timestamp + timedelta(seconds=1),
                observed_at=entry_bar.timestamp + timedelta(seconds=1),
            ),
        )
        snapshots = {}
        for index, bar in enumerate(bars[31:], 31):
            quote_at = decision if index == 31 else bar.timestamp
            snapshots[quote_at] = replace(
                option_quote(quote_at, bid=2.0, ask=2.1),
                underlying_price=None,
            )

        def underlying_quote(feed):
            quote = equity_quote(31, 100.9, 101.1)
            return replace(
                quote, timestamp=decision,
                identity=replace(quote.identity, feed=feed, as_of=decision,
                                 observed_at=decision),
            )

        mismatched = replay_ibr(
            bars, config=IBRConfig(policy=ReplayPolicy()), vehicle="option",
            option_snapshots=snapshots, quotes=[underlying_quote("sip")])
        self.assertEqual(mismatched.trades, [])
        self.assertEqual(mismatched.refusals[0].reason,
                         "underlying_quote_feed_mismatch")
        self.assertEqual(mismatched.refusals[0].detail,
                         {"expected": "iex", "observed": "sip"})

        matched = replay_ibr(
            bars, config=IBRConfig(policy=ReplayPolicy()), vehicle="option",
            option_snapshots=snapshots, quotes=[underlying_quote("iex")])
        self.assertEqual(len(matched.trades), 1)
        self.assertEqual(matched.trades[0].underlying_quote_feed, "iex")

    def test_factory_and_fit_diagnostic_share_the_entry_boundary(self):
        baseline = [
            _available_at(bar, bar.timestamp) for bar in _bars()
        ]
        trade = _simulate_trade(baseline, SPEC, [], "equity",
                                policy=BAR_FALLBACK)
        self.assertIsNotNone(trade)
        fit = measure_fit_diagnostics(baseline, SPEC)
        self.assertEqual(fit["first_signal"]["signals"], 1)

        entry_timestamp = datetime.fromisoformat(trade["entry_timestamp"])
        delayed = [
            _delayed(bar) if bar.timestamp == entry_timestamp else bar
            for bar in baseline
        ]
        delayed_trade = _simulate_trade(delayed, SPEC, [], "equity",
                                         policy=BAR_FALLBACK)
        self.assertIsNotNone(delayed_trade)
        delayed_observation = next(
            bar.observed_at for bar in delayed
            if bar.timestamp == entry_timestamp)
        self.assertEqual(delayed_trade["decision_timestamp"],
                         delayed_observation.isoformat())
        self.assertEqual(measure_fit_diagnostics(delayed, SPEC)
                         ["first_signal"]["signals"], 1)

        historical_trade = _simulate_trade(
            [replace(bar, identity=replace(
                bar.identity, source_mode="historical_backfill"))
             for bar in baseline],
            SPEC, [], "equity", policy=BAR_FALLBACK)
        self.assertIsNotNone(historical_trade)
        self.assertEqual(historical_trade["evidence_mode"],
                         "diagnostic_historical_backfill")

    def test_factory_accepts_recorder_style_delayed_bar_with_boundary_quote(self):
        _raw, bars, snapshots, quotes = _read_discovery_rows(edge_corpus(1))
        symbol_bars = [bar for bar in bars if bar.symbol == "AAA"]
        symbol_quotes = index_quotes(
            quote for quote in quotes if quote.symbol == "AAA")
        trade = _simulate_trade(
            symbol_bars, ROOT_SPEC, snapshots, "equity",
            quotes=symbol_quotes, policy=ReplayPolicy(),
        )
        self.assertIsNotNone(trade)
        self.assertEqual(trade["entry_fill_source"], "quote")

    def test_fit_counts_recorder_signal_at_observed_end_plus_five_seconds(self):
        _raw, bars, _snapshots, _quotes = _read_discovery_rows(edge_corpus(1))
        delayed = [replace(
            bar,
            identity=replace(bar.identity,
                             observed_at=bar.end + timedelta(seconds=5)),
        ) for bar in bars]
        diagnostic = measure_fit_diagnostics(delayed, ROOT_SPEC)
        self.assertGreater(diagnostic["first_signal"]["signals"], 0)
        prefixes = _fit_prefixes(delayed, ROOT_SPEC)
        self.assertEqual(prefixes["first_signals"][0]["entry_pricing"],
                         "quote_required")

    def test_randomized_null_and_reference_use_availability_time(self):
        start = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
        bars = []
        for index in range(3):
            timestamp = start + timedelta(minutes=index)
            bars.append(normalize_underlying_bar({
                "symbol": "SPY", "timestamp": timestamp.isoformat(),
                "open": 100, "high": 101, "low": 99, "close": 100,
                "volume": 1, "provider": "test", "feed": "sip",
            }))
        reference = [{
            "symbol": "SPY", "session_date": bars[0].session_date.isoformat(),
            "underlying_entry": 100, "stop_price": 99, "direction": "long",
        }]
        spec = {"target_r": 2, "max_hold_bars": 1}
        policy = ReplayPolicy(strict_market_data=False)
        self.assertFalse(null_control_account(
            bars, [], spec, vehicle="equity", reference_rows=reference,
            account_id="visibility", policy=policy)["rows"][0]["no_trade"])

        delayed = list(bars)
        delayed[1] = _delayed(delayed[1])
        self.assertTrue(null_control_account(
            delayed, [], spec, vehicle="equity", reference_rows=reference,
            account_id="visibility", policy=policy)["rows"][0]["no_trade"])

        ibr_bars = bars_for_day()
        result = replay_ibr(
            ibr_bars,
            config=permissive_config(stop_pct=.01, target_pct=.02, costs=FREE),
        )
        self.assertEqual(len(result.trades), 1)
        delayed_ibr = list(ibr_bars)
        delayed_ibr[31] = _delayed(delayed_ibr[31])
        row = _null_reference_rows(result, delayed_ibr, "equity")[0]
        self.assertFalse(row["no_trade"])

    def test_execution_strict_market_data_is_a_strict_boolean_config_field(self):
        self.assertTrue(validate_config({})["execution"]["strict_market_data"])
        self.assertFalse(validate_config({
            "execution": {"strict_market_data": False},
        })["execution"]["strict_market_data"])
        for value in (None, 0, 1, "false", []):
            with self.subTest(value=value), self.assertRaisesRegex(
                    ConfigError, r"execution\.strict_market_data"):
                validate_config({"execution": {"strict_market_data": value}})


if __name__ == "__main__":
    unittest.main()
