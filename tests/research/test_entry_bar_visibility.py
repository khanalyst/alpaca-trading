"""Adversarial point-in-time checks for next-bar entry opens."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from agent.config import ConfigError, validate_config
from research.costs import (ReplayPolicy, SQLiteQuoteIndex,
                             diagnostic_backfill_policy)
from research.costs import index_quotes
from research.edge_discovery_core import _null_reference_rows
from research.edge_lab import _read_discovery_rows
from research.factory_core import _simulate_trade, diagnose, simulate_account
from research.fit_diagnostics import _fit_prefixes, measure_fit_diagnostics
from research.ibr import IBRConfig, IBRResult, replay_ibr
from research.gates import authorization_projection
from research.market_data import normalize_quote, normalize_underlying_bar
from research.strategy_factory import null_control_account

from tests.research.test_bar_gap_scope import BAR_FALLBACK, SPEC, _bars
from tests.research.test_costs import (
    FLAT as COST_FLAT, RISING as COST_RISING, SPEC as COST_SPEC,
    _bars as cost_bars,
)
from tests.research.test_factory_end_to_end import ROOT_SPEC, edge_corpus
from tests.research.test_ibr import (FREE, bars_for_day, equity_quote,
                                     option_quote, permissive_config)
from tests.research.test_option_exit_tolerance import (
    CONTRACT as OPTION_CONTRACT, SPEC as OPTION_SPEC, _rising_session, _snap)


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


def _historical(record):
    return replace(
        record,
        identity=replace(record.identity, source_mode="historical_backfill"),
    )


def _shifted_symbol_bars(bars, symbol, minutes=0):
    shift = timedelta(minutes=minutes)
    return [replace(
        bar, symbol=symbol, timestamp=bar.timestamp + shift,
        identity=replace(
            bar.identity,
            as_of=bar.identity.as_of + shift,
            observed_at=bar.identity.observed_at + shift,
        ),
    ) for bar in bars]


def _equity_mark(symbol, timestamp, bid):
    return normalize_quote({
        "symbol": symbol, "timestamp": timestamp.isoformat(),
        "as_of": timestamp.isoformat(), "observed_at": timestamp.isoformat(),
        "bid": bid, "ask": bid + .01, "provider": "alpaca", "feed": "iex",
    })


class EntryBarVisibilityTests(unittest.TestCase):
    def test_ibr_historical_equity_quote_marks_forward_bars_diagnostic(self):
        bars = bars_for_day()
        quotes = [_historical(equity_quote(31, 100.9, 101.1)),
                  _historical(equity_quote(32, 102.9, 103.1))]
        policy = diagnostic_backfill_policy(ReplayPolicy())
        result = replay_ibr(
            bars, config=IBRConfig(policy=policy), quotes=quotes)
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].entry_fill_source, "quote")
        self.assertEqual(result.trades[0].evidence_mode,
                         "diagnostic_historical_backfill")

    def test_ibr_historical_option_snapshots_mark_forward_bars_diagnostic(self):
        bars = bars_for_day()
        snapshots = {
            bar.timestamp: _historical(option_quote(
                bar.timestamp, bid=2.0, ask=2.1))
            for bar in bars[31:]
        }
        result = replay_ibr(
            bars,
            config=IBRConfig(
                policy=diagnostic_backfill_policy(ReplayPolicy())),
            vehicle="option", option_snapshots=snapshots)
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].exit_fill_source, "quote")
        self.assertEqual(result.trades[0].evidence_mode,
                         "diagnostic_historical_backfill")

    def test_factory_historical_equity_fills_mark_forward_bars_diagnostic(self):
        _raw, bars, snapshots, quotes = _read_discovery_rows(edge_corpus(1))
        symbol_bars = [bar for bar in bars if bar.symbol == "AAA"]
        historical_quotes = index_quotes(
            _historical(quote) for quote in quotes if quote.symbol == "AAA")
        trade = _simulate_trade(
            symbol_bars, ROOT_SPEC, snapshots, "equity",
            quotes=historical_quotes,
            policy=diagnostic_backfill_policy(ReplayPolicy()),
        )
        self.assertIsNotNone(trade)
        self.assertEqual(trade["entry_fill_source"], "quote")
        self.assertEqual(trade["exit_fill_source"], "quote")
        self.assertEqual(trade["evidence_mode"],
                         "diagnostic_historical_backfill")

    def test_factory_non_strict_bar_fallback_is_non_authorizing(self):
        book = simulate_account(
            cost_bars(COST_RISING + COST_FLAT), [], COST_SPEC,
            vehicle="equity", account_id="non-strict-bar-fallback",
            policy=ReplayPolicy(strict_market_data=False),
        )
        row = book["rows"][0]
        self.assertFalse(row["no_trade"])
        self.assertEqual(row["evidence_mode"],
                         "diagnostic_bar_fallback")
        self.assertFalse(row["directional_authorizing"])
        self.assertFalse(row["authorizing"])
        self.assertEqual(book["authorizing_trades"], 0)
        self.assertEqual(diagnose(book["rows"])["trades"], 0)
        self.assertEqual(
            diagnose(book["rows"], diagnostic_only=True)["trades"], 1)

    def test_factory_explicit_diagnostic_policy_never_authorizes_forward_quotes(self):
        _raw, bars, snapshots, quotes = _read_discovery_rows(edge_corpus(1))
        symbol_bars = [bar for bar in bars if bar.symbol == "AAA"]
        symbol_quotes = [quote for quote in quotes if quote.symbol == "AAA"]
        book = simulate_account(
            symbol_bars, snapshots, ROOT_SPEC, vehicle="equity",
            account_id="forward-diagnostic-policy", quotes=symbol_quotes,
            policy=diagnostic_backfill_policy(ReplayPolicy()),
        )
        row = next(item for item in book["rows"] if not item["no_trade"])
        self.assertEqual("forward_observed", row["evidence_mode"])
        self.assertTrue(row["diagnostic_only"])
        self.assertFalse(row["authorizing"])
        self.assertFalse(row["directional_authorizing"])
        self.assertEqual("diagnostic_backfill_policy",
                         row["diagnostic_reason"])
        self.assertEqual(0, book["authorizing_trades"])
        self.assertEqual(0, diagnose(book["rows"])["trades"])
        self.assertEqual(1, diagnose(
            book["rows"], diagnostic_only=True)["trades"])

    def test_null_historical_entry_exit_quote_taints_row(self):
        bars = bars_for_day()
        references = [{
            "symbol": "SPY", "session_date": bars[0].session_date.isoformat(),
            "underlying_entry": 101.0, "stop_price": 100.0,
            "stop_distance": 1.0, "direction": "long", "no_trade": False,
        }]
        historical_quotes = [
            _historical(equity_quote(
                index, bar.open - .01, bar.open + .01))
            for index, bar in enumerate(bars)
        ]
        book = null_control_account(
            bars, [], {"target_r": 2, "max_hold_bars": 2},
            vehicle="equity", reference_rows=references,
            account_id="null-historical-equity-quotes", fixed_quantity=1,
            quotes=historical_quotes,
            policy=diagnostic_backfill_policy(ReplayPolicy()),
        )
        row = book["rows"][0]
        self.assertFalse(row["no_trade"])
        self.assertEqual(row["evidence_mode"],
                         "diagnostic_historical_backfill")
        self.assertFalse(row["directional_authorizing"])
        self.assertFalse(row["authorizing"])

    def test_null_historical_option_snapshots_taint_row(self):
        bars = bars_for_day()
        references = [{
            "symbol": "SPY", "session_date": bars[0].session_date.isoformat(),
            "underlying_entry": 101.0, "stop_price": 100.0,
            "stop_distance": 1.0, "direction": "long", "no_trade": False,
        }]
        historical_snapshots = [
            _historical(option_quote(bar.timestamp, bid=2.0, ask=2.1))
            for bar in bars
        ]
        book = null_control_account(
            bars, historical_snapshots,
            {"target_r": 2, "max_hold_bars": 2},
            vehicle="option", reference_rows=references,
            account_id="null-historical-option-snapshots", fixed_quantity=1,
            policy=diagnostic_backfill_policy(ReplayPolicy()),
        )
        row = book["rows"][0]
        self.assertFalse(row["no_trade"])
        self.assertEqual(row["evidence_mode"],
                         "diagnostic_historical_backfill")
        self.assertFalse(row["directional_authorizing"])
        self.assertFalse(row["authorizing"])

    def test_ibr_resolver_historical_quote_mode_is_non_authorizing(self):
        bars = bars_for_day()
        quotes = SQLiteQuoteIndex()
        try:
            for index in (31, 32):
                quotes.add(_historical(equity_quote(
                    index, bars[index].open - .01, bars[index].open + .01)))
            quotes.finalize()
            rejected = replay_ibr(
                bars,
                config=IBRConfig(stop_pct=.01, target_pct=.02, costs=FREE),
                quotes=quotes,
            )
            self.assertEqual([], rejected.trades)
            self.assertFalse(rejected.authorizing)
            self.assertEqual("source_preflight_failed",
                             rejected.refusals[0].reason)
            self.assertFalse(rejected.source_report["authorizing"])
            self.assertEqual({"forward_observed": 33,
                              "historical_backfill": 2},
                             rejected.source_report["source_mode_counts"])

            diagnostic = replay_ibr(
                bars,
                config=IBRConfig(
                    stop_pct=.01, target_pct=.02, costs=FREE,
                    policy=diagnostic_backfill_policy(ReplayPolicy()),
                ),
                quotes=quotes,
            )
            self.assertEqual(1, len(diagnostic.trades))
            self.assertFalse(diagnostic.authorizing)
            self.assertTrue(diagnostic.diagnostic_only)
            self.assertFalse(diagnostic.source_report["authorizing"])
            self.assertTrue(diagnostic.source_report["diagnostic_only"])
        finally:
            quotes.close()

    def test_ibr_malformed_resolver_provenance_fails_closed_in_diagnostic_mode(self):
        class MalformedResolver:
            def quote_fill(self, **_kwargs):
                return None

            def quote_fill_record(self, **_kwargs):
                return None

            def source_mode_counts(self):
                return {"future_mode": 1}

        result = replay_ibr(
            bars_for_day(),
            config=IBRConfig(
                stop_pct=.01, target_pct=.02, costs=FREE,
                policy=diagnostic_backfill_policy(ReplayPolicy()),
            ),
            quotes=MalformedResolver(),
        )
        self.assertEqual([], result.trades)
        self.assertFalse(result.authorizing)
        self.assertTrue(result.source_report["preflight_failed"])
        self.assertFalse(result.source_report["authorizing"])

    def test_factory_historical_option_fills_mark_forward_bars_diagnostic(self):
        snapshots = []
        for minute in range(1, 40):
            snapshots.append(_historical(_snap(minute)))
        book = simulate_account(
            _rising_session(), snapshots, OPTION_SPEC, vehicle="option",
            account_id="historical-option-fills",
            policy=diagnostic_backfill_policy(ReplayPolicy()),
        )
        self.assertEqual(len(book["rows"]), 1)
        self.assertFalse(book["rows"][0]["no_trade"])
        self.assertEqual(book["rows"][0]["evidence_mode"],
                         "diagnostic_historical_backfill")
        self.assertFalse(book["rows"][0]["directional_authorizing"])
        self.assertFalse(book["rows"][0]["authorizing"])
        self.assertEqual(book["authorizing_trades"], 0)
        self.assertEqual(book["authorizing_realized_pnl"], 0.0)
        self.assertEqual(diagnose(book["rows"])["trades"], 0)
        diagnostic = diagnose(book["rows"], diagnostic_only=True)
        self.assertEqual(diagnostic["trades"], 1)
        self.assertFalse(diagnostic["directional_authorizing"])

    def test_factory_historical_equity_mark_taints_later_sizing_only(self):
        base = cost_bars(COST_RISING + COST_FLAT)
        bars = (_shifted_symbol_bars(base, "AAA") +
                _shifted_symbol_bars(base, "BBB", minutes=1))
        policy = diagnostic_backfill_policy(
            ReplayPolicy(strict_market_data=False))
        baseline = simulate_account(
            bars, [], COST_SPEC, vehicle="equity", account_id="mark-baseline",
            policy=policy)
        baseline_rows = {row["symbol"]: row for row in baseline["rows"]}
        mark_at = datetime.fromisoformat(
            baseline_rows["BBB"]["entry_timestamp"])
        historical_mark = _historical(_equity_mark("AAA", mark_at, 150.0))

        marked = simulate_account(
            bars, [], COST_SPEC, vehicle="equity", account_id="mark-historical",
            quotes=[historical_mark], policy=policy)
        marked_rows = {row["symbol"]: row for row in marked["rows"]}
        self.assertEqual(marked_rows["AAA"]["evidence_mode"],
                         "diagnostic_bar_fallback")
        self.assertFalse(marked_rows["AAA"]["directional_authorizing"])
        self.assertEqual(marked_rows["BBB"]["evidence_mode"],
                         "diagnostic_historical_backfill")
        self.assertGreater(marked_rows["BBB"]["quantity"],
                           baseline_rows["BBB"]["quantity"])

    def test_factory_historical_option_mark_taints_later_sizing_only(self):
        base = _rising_session()
        bars = (list(base) +
                _shifted_symbol_bars(base, "QQQ", minutes=1))
        qqq_contract = replace(
            OPTION_CONTRACT, symbol="QQQ240119C00500000", underlying="QQQ")
        spy_snapshots = [_snap(minute) for minute in range(1, 40)]
        qqq_snapshots = []
        for minute in range(1, 40):
            snap = _snap(minute)
            shift = timedelta(minutes=1)
            qqq_snapshots.append(replace(
                snap, contract=qqq_contract,
                timestamp=snap.timestamp + shift,
                identity=replace(
                    snap.identity,
                    as_of=snap.identity.as_of + shift,
                    observed_at=snap.identity.observed_at + shift,
                ),
            ))
        policy = diagnostic_backfill_policy(ReplayPolicy())
        baseline = simulate_account(
            bars, spy_snapshots + qqq_snapshots, OPTION_SPEC,
            vehicle="option", account_id="option-mark-baseline",
            policy=policy)
        baseline_rows = {row["symbol"]: row for row in baseline["rows"]}
        mark_at = datetime.fromisoformat(
            baseline_rows["QQQ"]["entry_timestamp"])
        marked_spy_snapshots = [
            _historical(replace(snap, bid=200.0, ask=200.1, last=200.0))
            if snap.timestamp == mark_at else snap
            for snap in spy_snapshots
        ]

        marked = simulate_account(
            bars, marked_spy_snapshots + qqq_snapshots, OPTION_SPEC,
            vehicle="option", account_id="option-mark-historical",
            policy=policy)
        marked_rows = {row["symbol"]: row for row in marked["rows"]}
        self.assertEqual(marked_rows["SPY"]["evidence_mode"],
                         "forward_observed")
        self.assertEqual(marked_rows["QQQ"]["evidence_mode"],
                         "diagnostic_historical_backfill")
        self.assertGreater(marked_rows["QQQ"]["quantity"],
                           baseline_rows["QQQ"]["quantity"])

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

    def test_fit_rejects_entry_row_observed_after_entry_boundary(self):
        _raw, bars, _snapshots, _quotes = _read_discovery_rows(edge_corpus(1))
        delayed = [replace(
            bar,
            identity=replace(bar.identity,
                             observed_at=bar.end + timedelta(seconds=5)),
        ) for bar in bars]
        diagnostic = measure_fit_diagnostics(delayed, ROOT_SPEC)
        self.assertEqual(diagnostic["first_signal"]["signals"], 0)
        prefixes = _fit_prefixes(delayed, ROOT_SPEC)
        self.assertFalse(prefixes["first_signals"])
        self.assertEqual(prefixes["eligibility_provenance"]["status"],
                         "data_incomplete")

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
