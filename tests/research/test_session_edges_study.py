"""Tests for the session-edge study and its forward shadow in ``studies/session_edges``.

Fast, offline and deterministic: every bar is synthetic and the forward
runner talks to a fake provider. Run from the repository root with::

    python -I -m unittest discover -s tests/research -t . -p test_session_edges_study.py -v
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import io
import json
import tempfile
import unittest
import zipfile
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from studies.liquid_reversion.bars import NEW_YORK
from studies.session_edges import discover, grammar, live, propose, run, stats
from studies.session_edges import data as session_data
from studies.session_edges.data import MINUTES, DailyBar, Session, build_session
from studies.session_edges.strategies import (CLOSE_REVERSION, ENTER, EXIT, FLAT, HOLD, LONG, NOISE_BAND,
                                              SHORT, STAND_ASIDE, DayResult, NoiseBandRules,
                                              close_reversion_action, internal_bar_strength,
                                              noise_band_bounds, noise_band_sigma, noise_band_target,
                                              simulate_close_reversion, simulate_close_reversion_daily,
                                              simulate_down_streak_daily, simulate_first_last,
                                              simulate_gap, simulate_last_hour, simulate_noise_band,
                                              simulate_opening_range, simulate_overnight,
                                              simulate_overnight_hold, simulate_rsi_reversion_daily,
                                              simulate_turn_of_month_daily)

BASE = date(2026, 3, 2)  # a Monday


def trading_days(count: int, start: date = BASE) -> list[date]:
    out: list[date] = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def flat_session(day: date, level: float = 100.0, moves: dict[int, float] | None = None) -> Session:
    """A session that sits at ``level`` and steps to a new level from each minute in ``moves``."""
    moves = moves or {}
    rows = []
    price = level
    for minute in range(MINUTES):
        opened = price
        if minute in moves:
            price = moves[minute]
        rows.append((minute, opened, max(opened, price), min(opened, price), price, 1000.0))
    session = build_session(day, rows)
    assert session is not None
    return session


def wiggle_session(day: date, level: float = 100.0, swing: float = 0.002) -> Session:
    """A session whose close at every minute is ``level * (1 + swing)``: a constant noise band."""
    return flat_session(day, level, {1: level * (1 + swing)})


class DataTests(unittest.TestCase):
    def test_missing_minutes_repeat_the_last_close(self):
        rows = [(m, 10.0, 10.5, 9.5, 10.2, 5.0) for m in range(MINUTES) if m not in (3, 4)]
        session = build_session(BASE, rows)
        self.assertEqual(session.observed, MINUTES - 2)
        self.assertEqual((session.open[3], session.high[3], session.low[3], session.close[4]),
                         (10.2, 10.2, 10.2, 10.2))
        self.assertEqual(session.volume[3], 0.0)

    def test_thin_sessions_are_dropped(self):
        rows = [(m, 10.0, 10.0, 10.0, 10.0, 1.0) for m in range(210)]  # a half day
        self.assertIsNone(build_session(BASE, rows))
        self.assertIsNotNone(build_session(BASE, rows, min_observed=200))

    def test_previous_close_needs_adjacent_sessions(self):
        days = trading_days(2)
        near = [flat_session(days[0], 100.0), flat_session(days[1], 101.0)]
        self.assertEqual(session_data.previous_close(near, 1), 100.0)
        far = [flat_session(days[0], 100.0), flat_session(days[0] + timedelta(days=9), 101.0)]
        self.assertIsNone(session_data.previous_close(far, 1))
        self.assertIsNone(session_data.previous_close(near, 0))

    def test_index_archive_stamps_are_new_york_wall_clock(self):
        lines = []
        for minute in range(MINUTES):
            total = 570 + minute
            lines.append(f"20260715 {total // 60:02d}{total % 60:02d}00;100.0;100.5;99.5;100.2;0")
        lines.append("20260715 080000;90;90;90;90;0")      # before the open: ignored
        lines.append("20260715 160000;95;95;95;95;0")      # at the close: ignored
        with tempfile.TemporaryDirectory() as folder:
            with zipfile.ZipFile(Path(folder) / "nsxusd_2026.zip", "w") as archive:
                archive.writestr("DAT_ASCII_NSXUSD_M1_2026.csv", "\n".join(lines))
            sessions = session_data.load_index_minute_archives(folder, "nsxusd")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].day, date(2026, 7, 15))
        self.assertEqual(sessions[0].observed, MINUTES)
        self.assertEqual(sessions[0].open[0], 100.0)

    def test_sip_month_files_load_as_sessions_and_run_end_to_end(self):
        import csv
        from studies.liquid_reversion.bars import ALPACA_CSV_COLUMNS, month_file_name
        days = [date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1)]
        with tempfile.TemporaryDirectory() as folder:
            for month in ("2026-09", "2026-10"):
                path = Path(folder) / month_file_name("QQQ", "1Min", month)
                with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(ALPACA_CSV_COLUMNS)
                    for day in days:
                        if f"{day:%Y-%m}" != month:
                            continue
                        opened = datetime.combine(day, time(9, 30), NEW_YORK)
                        for minute in range(-5, MINUTES + 5):       # pre- and post-market rows are ignored
                            stamp = (opened + timedelta(minutes=minute)).astimezone(timezone.utc)
                            price = 400.0 + minute * 0.01
                            writer.writerow(["QQQ", stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), price, price + 0.05,
                                             price - 0.05, price + 0.01, 1000, 10, price])
            sessions = session_data.load_alpaca_sessions(folder, "QQQ")
            self.assertEqual([session.day for session in sessions], days)
            self.assertEqual(sessions[0].observed, MINUTES)
            self.assertAlmostEqual(sessions[0].open[0], 400.0)
            self.assertAlmostEqual(sessions[0].close[-1], 400.0 + 389 * 0.01 + 0.01)
            out = Path(folder) / "out"
            with contextlib.redirect_stdout(io.StringIO()):
                code = run.main(["--alpaca-dir", folder, "--symbols", "QQQ", "--out", str(out), "--draws", "10"])
            self.assertEqual(code, 0)
            results = json.loads((out / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(results["schema"], run.SCHEMA)
            self.assertEqual({block["verdict"] for block in results["minute"]["QQQ"].values()}, {run.REJECT})
            self.assertIn("Session-edge study", (out / "report.md").read_text(encoding="utf-8"))
            with self.assertRaises(FileNotFoundError):
                session_data.load_alpaca_sessions(folder, "ZZZZ")

    def test_daily_chart_is_adjusted(self):
        stamps = [int(datetime(2026, 3, d, 14, 30, tzinfo=timezone.utc).timestamp()) for d in (2, 3)]
        payload = {"t": stamps, "o": [100.0, 102.0], "h": [101.0, 103.0], "l": [99.0, 101.0],
                   "c": [100.0, 102.0], "adj": [50.0, 102.0]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "SPY.json.gz"
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                json.dump(payload, handle)
            bars = session_data.load_daily_chart(path)
        self.assertEqual([bar.day for bar in bars], [date(2026, 3, 2), date(2026, 3, 3)])
        self.assertEqual((bars[0].open, bars[0].close, bars[1].close), (50.0, 50.0, 102.0))


class NoiseBandTests(unittest.TestCase):
    def test_sigma_is_the_mean_absolute_move_from_the_open(self):
        history = [wiggle_session(day, swing=s) for day, s in zip(trading_days(2), (0.002, 0.004))]
        self.assertAlmostEqual(noise_band_sigma(history, 30, 2), 0.003)
        self.assertIsNone(noise_band_sigma(history, 30, 3))

    def test_a_gap_widens_the_band_on_its_own_side(self):
        upper, lower = noise_band_bounds(100.0, 101.0, 0.01)
        self.assertAlmostEqual(upper, 101.0 * 1.01)
        self.assertAlmostEqual(lower, 100.0 * 0.99)
        self.assertEqual(noise_band_bounds(100.0, None, 0.01), (101.0, 99.0))

    def test_target_follows_a_break_and_leaves_on_a_return(self):
        self.assertEqual(noise_band_target(FLAT, 102.0, 101.0, 99.0), LONG)
        self.assertEqual(noise_band_target(FLAT, 98.0, 101.0, 99.0), SHORT)
        self.assertEqual(noise_band_target(FLAT, 100.0, 101.0, 99.0), FLAT)
        self.assertEqual(noise_band_target(LONG, 100.5, 101.0, 99.0), FLAT)
        self.assertEqual(noise_band_target(SHORT, 99.5, 101.0, 99.0), FLAT)
        self.assertEqual(noise_band_target(LONG, 98.0, 101.0, 99.0), SHORT)
        self.assertEqual(noise_band_target(FLAT, 102.0, 101.0, 99.0, sides=("short",)), FLAT)

    def test_simulator_enters_at_the_next_open_and_exits_at_the_close(self):
        days = trading_days(NOISE_BAND.lookback_sessions + 1)
        history = [wiggle_session(day) for day in days[:-1]]           # band is 0.2% wide
        today = flat_session(days[-1], 100.2, {45: 101.0, 200: 102.0})  # breaks out before 10:30
        results = simulate_noise_band([*history, today])
        self.assertTrue(all(result.round_trips == 0 for result in results[:-1]))
        last = results[-1]
        self.assertEqual(last.round_trips, 1)
        fill = last.fills[0]
        self.assertEqual((fill.side, fill.entry_minute, fill.entry, fill.exit_minute, fill.exit),
                         (LONG, 60, 101.0, MINUTES, 102.0))
        self.assertAlmostEqual(last.gross, 102.0 / 101.0 - 1.0)

    def test_a_return_inside_the_band_closes_the_position(self):
        days = trading_days(NOISE_BAND.lookback_sessions + 1)
        history = [wiggle_session(day) for day in days[:-1]]
        today = flat_session(days[-1], 100.2, {45: 101.0, 100: 100.2})
        fills = simulate_noise_band([*history, today])[-1].fills
        self.assertEqual([(fill.entry_minute, fill.exit_minute) for fill in fills], [(60, 120)])
        self.assertLess(fills[0].gross, 0)

    def test_no_decision_without_the_full_lookback(self):
        days = trading_days(5)
        sessions = [flat_session(day, 100.0, {45: 105.0}) for day in days]
        self.assertTrue(all(result.round_trips == 0 for result in simulate_noise_band(sessions)))

    def test_early_flat_minute_exits_at_that_open(self):
        days = trading_days(NOISE_BAND.lookback_sessions + 1)
        history = [wiggle_session(day) for day in days[:-1]]
        today = flat_session(days[-1], 100.2, {45: 101.0, 389: 110.0})
        rules = NoiseBandRules(flat_minute=388)
        fill = simulate_noise_band([*history, today], rules)[-1].fills[0]
        self.assertEqual((fill.exit_minute, fill.exit), (388, 101.0))


class CloseReversionTests(unittest.TestCase):
    def test_internal_bar_strength(self):
        self.assertEqual(internal_bar_strength(110.0, 100.0, 100.0), 0.0)
        self.assertEqual(internal_bar_strength(110.0, 100.0, 110.0), 1.0)
        self.assertAlmostEqual(internal_bar_strength(110.0, 100.0, 102.0), 0.2)
        self.assertEqual(internal_bar_strength(100.0, 100.0, 100.0), 0.5)

    def test_action_table(self):
        self.assertEqual(close_reversion_action(False, 0, 0.19), ENTER)
        self.assertEqual(close_reversion_action(False, 0, 0.20), STAND_ASIDE)
        self.assertEqual(close_reversion_action(True, 1, 0.50), HOLD)
        self.assertEqual(close_reversion_action(True, 1, 0.51), EXIT)
        self.assertEqual(close_reversion_action(True, CLOSE_REVERSION.max_sessions, 0.10), EXIT)

    def test_minute_simulator_buys_a_weak_close_and_sells_the_recovery(self):
        days = trading_days(3)
        weak = flat_session(days[0], 100.0, {300: 98.0})            # closes at the low of the day
        strong = flat_session(days[1], 98.0, {300: 100.0})          # recovers to the high of the day
        quiet = flat_session(days[2], 100.0)
        results = simulate_close_reversion([weak, strong, quiet])
        self.assertAlmostEqual(results[0].gross, 0.0)               # bought at 98, closed at 98
        self.assertEqual(results[0].round_trips, 0)
        self.assertAlmostEqual(results[1].gross, 100.0 / 98.0 - 1.0)
        self.assertEqual(results[1].round_trips, 1)
        self.assertEqual((results[2].gross, results[2].round_trips), (0.0, 0))

    def test_position_is_dropped_across_missing_sessions(self):
        days = trading_days(1)
        weak = flat_session(days[0], 100.0, {300: 98.0})
        later = flat_session(days[0] + timedelta(days=20), 120.0, {300: 121.0})
        results = simulate_close_reversion([weak, later])
        self.assertEqual(results[1].gross, 0.0)

    def test_daily_simulator_holds_at_most_the_registered_sessions(self):
        days = trading_days(8)
        bars = [DailyBar(days[0], 100.0, 101.0, 99.0, 99.1)]         # weak close: enter
        closes = (98.9, 98.8, 98.7, 98.6, 98.5, 98.45, 98.42)        # never recovers, never weak enough
        bars += [DailyBar(day, 99.0, 100.0, 98.0, close) for day, close in zip(days[1:], closes)]
        results = simulate_close_reversion_daily(bars)
        self.assertEqual(sum(result.round_trips for result in results), 1)
        held = [result.gross != 0.0 for result in results]
        self.assertEqual(held[:7], [False, True, True, True, True, True, False])

    def test_overnight_is_prior_close_to_open(self):
        days = trading_days(2)
        results = simulate_overnight([DailyBar(days[0], 100, 101, 99, 100.0),
                                      DailyBar(days[1], 101.0, 102, 100, 100.5)])
        self.assertEqual(results[0].round_trips, 0)
        self.assertAlmostEqual(results[1].gross, 0.01)


class OpeningRangeTests(unittest.TestCase):
    def test_long_is_stopped_at_the_range_low(self):
        session = flat_session(BASE, 100.0, {2: 100.5, 60: 99.0})
        result = simulate_opening_range([session])[0]
        fill = result.fills[0]
        self.assertEqual((fill.side, fill.entry_minute, fill.entry), (LONG, 5, 100.5))
        self.assertEqual((fill.exit_minute, fill.exit), (60, 100.0))

    def test_short_runs_to_the_close(self):
        session = flat_session(BASE, 100.0, {2: 99.5, 200: 98.0})
        fill = simulate_opening_range([session])[0].fills[0]
        self.assertEqual((fill.side, fill.exit_minute, fill.exit), (SHORT, MINUTES, 98.0))
        self.assertAlmostEqual(fill.gross, -(98.0 / 99.5 - 1.0))

    def test_flat_first_candle_is_no_trade(self):
        self.assertEqual(simulate_opening_range([flat_session(BASE)])[0].round_trips, 0)

    def test_first_last_trades_the_sign_of_the_first_half_hour(self):
        days = trading_days(2)
        sessions = [flat_session(days[0]), flat_session(days[1], 100.0, {10: 101.0, 370: 102.0})]
        result = simulate_first_last(sessions)[1]
        self.assertEqual(result.fills[0].side, LONG)
        self.assertAlmostEqual(result.gross, 102.0 / 101.0 - 1.0)


class StatsTests(unittest.TestCase):
    def test_costs_are_charged_per_round_trip(self):
        results = [DayResult(BASE, 0.001, 2), DayResult(BASE + timedelta(days=1), 0.0, 0)]
        self.assertEqual(stats.net_series(results, 1.0), [0.001 - 0.0002, 0.0])

    def test_max_drawdown_is_additive(self):
        self.assertAlmostEqual(stats.max_drawdown([0.01, -0.02, -0.01, 0.05]), -0.03)
        self.assertEqual(stats.max_drawdown([0.01, 0.02]), 0.0)

    def test_newey_west_matches_the_plain_t_for_independent_noise(self):
        series = [0.001 if i % 2 else -0.0005 for i in range(400)]
        value = stats.newey_west_t(series, lags=0)
        mean = sum(series) / 400
        deviation = (sum((x - mean) ** 2 for x in series) / 400) ** 0.5
        self.assertAlmostEqual(value, mean / (deviation / 400 ** 0.5))
        self.assertIsNone(stats.newey_west_t([0.0] * 100))

    def test_sessions_for_power(self):
        self.assertEqual(stats.sessions_for_power(1.0, 10.0), 619)
        self.assertIsNone(stats.sessions_for_power(-1.0, 10.0))

    def test_rolling_sum_quantiles_are_window_totals_in_percent(self):
        series = [0.01, -0.01] * 20
        bands = stats.rolling_sum_quantiles(series, 3)
        self.assertEqual({key: round(value, 6) for key, value in bands.items()},
                         {"p01": -1.0, "p05": -1.0, "p50": 1.0})
        self.assertIsNone(stats.rolling_sum_quantiles(series, 30))

    def test_combine_uses_shared_sessions(self):
        days = trading_days(3)
        merged_days, merged = stats.combine([(days, [0.01, 0.02, 0.03], 1.0), (days[1:], [0.10, 0.20], 0.5)])
        self.assertEqual(merged_days, days[1:])
        self.assertEqual([round(value, 6) for value in merged], [0.07, 0.13])

    def test_evaluate_splits_design_and_holdout(self):
        days = trading_days(60)
        results = [DayResult(day, 0.001 if i % 3 else -0.001, 1) for i, day in enumerate(days)]
        block = stats.evaluate(results, cost_bps=0.0, split=days[30], draws=200)
        self.assertEqual(block["design"]["sessions"], 30)
        self.assertEqual(block["holdout"]["sessions"], 30)
        self.assertEqual(block["all"]["round_trips"], 60)
        self.assertLess(block["sign_flip_p"], 0.05)


class VerdictTests(unittest.TestCase):
    @staticmethod
    def block(t_all, t_holdout, mean=1.0, deflated=0.99):
        return {"all": {"t_newey_west": t_all, "net_bps_per_session": mean},
                "holdout": {"t_newey_west": t_holdout, "net_bps_per_session": mean},
                "deflated_sharpe": deflated}

    def test_every_check_must_pass_for_a_paper_candidate(self):
        good = self.block(3.5, 2.5)
        self.assertEqual(run.verdict(good, good, 1.0), (run.PAPER, []))
        tier, failed = run.verdict(self.block(2.5, 2.5), good, 1.0)
        self.assertEqual(tier, run.SHADOW)
        self.assertEqual(len(failed), 1)

    def test_cost_stress_and_neighbours_are_vetoes(self):
        good = self.block(3.5, 2.5)
        stressed_out = self.block(3.5, 2.5, mean=-0.1)
        self.assertEqual(run.verdict(good, stressed_out, 1.0)[0], run.SHADOW)
        self.assertEqual(run.verdict(good, good, 0.5)[0], run.SHADOW)
        self.assertEqual(run.verdict(good, good, None)[0], run.SHADOW)

    def test_a_negative_holdout_is_rejected(self):
        bad = self.block(3.5, -1.0, mean=-1.0)
        self.assertEqual(run.verdict(bad, bad, 1.0)[0], run.REJECT)

    def test_study_runs_end_to_end_on_synthetic_sessions(self):
        days = trading_days(60)
        sessions = [wiggle_session(day) if i % 4 else flat_session(day, 100.2, {45: 101.0, 300: 102.0})
                    for i, day in enumerate(days)]
        results = run.run(minute={"TEST": sessions}, daily={}, source="synthetic", draws=50)
        self.assertEqual(set(results["minute"]["TEST"]), set(run.RULES))
        text = run.render_report(results)
        self.assertIn("noise_band_momentum", text)
        json.dumps(run._strip(results))          # the record must be serialisable


# ---------------------------------------------------------------------------
# Forward shadow
# ---------------------------------------------------------------------------

@dataclass
class FakeBar:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal("1000")


class FakeProvider:
    """A broker stub whose clock the test moves; bars are visible once complete.

    A submitted market order fills at once at the current quote unless the
    test says otherwise, and the stub keeps the positions that result.
    """

    mode = "paper"
    paper = True
    session = None

    def __init__(self, sessions: dict[str, list[Session]], *, early_close: bool = False) -> None:
        self.sessions = sessions
        self.now = datetime(2000, 1, 1, tzinfo=timezone.utc)
        self.early_close = early_close
        self.sent: list = []
        self.records: dict[str, SimpleNamespace] = {}
        self.held: dict[str, int] = {}
        self.cancelled: list[str] = []
        self.equity = Decimal("100000")
        self.fail_orders = False
        self.fill_orders = True
        self.status = "active"

    @staticmethod
    def open_at(day: date) -> datetime:
        return datetime.combine(day, time(9, 30), NEW_YORK)

    def set(self, day: date, minute: float) -> None:
        self.now = (self.open_at(day) + timedelta(minutes=minute)).astimezone(timezone.utc)

    def clock(self):
        local = self.now.astimezone(NEW_YORK)
        days = {session.day for rows in self.sessions.values() for session in rows}
        minute = local.hour * 60 + local.minute - 570
        is_open = local.date() in days and 0 <= minute < MINUTES
        close = datetime.combine(local.date(), time(13, 0) if self.early_close else time(16, 0), NEW_YORK)
        return SimpleNamespace(timestamp=self.now, is_open=is_open, next_open=None, next_close=close)

    def bars(self, symbols, timeframe="1Min", start=None, end=None, feed=None):
        out = {}
        for symbol in symbols:
            rows = []
            for session in self.sessions.get(symbol, []):
                opened = self.open_at(session.day)
                if opened > end or opened + timedelta(minutes=MINUTES) < start:
                    continue
                for minute in range(MINUTES):
                    stamp = opened + timedelta(minutes=minute)
                    if stamp + timedelta(minutes=1) > min(end, self.now) or stamp < start:
                        continue
                    rows.append(FakeBar(stamp, *(Decimal(str(value)) for value in (
                        session.open[minute], session.high[minute], session.low[minute],
                        session.close[minute]))))
            out[symbol] = rows
        return out

    def _price(self, symbol: str) -> Decimal | None:
        local = self.now.astimezone(NEW_YORK)
        minute = local.hour * 60 + local.minute - 570
        for session in self.sessions.get(symbol, []):
            if session.day == local.date() and 0 <= minute < MINUTES:
                return Decimal(str(session.open[minute]))
        return None

    def quotes(self, symbols, start=None, end=None, feed=None):
        """The quote is the open of the minute the clock is in, with no spread."""
        out = {}
        for symbol in symbols:
            price = self._price(symbol)
            out[symbol] = [] if price is None else [SimpleNamespace(bid=price, ask=price)]
        return out

    def account(self):
        return SimpleNamespace(equity=self.equity, status=self.status)

    def positions(self):
        return [SimpleNamespace(symbol=symbol, qty=Decimal(abs(quantity)), side="long" if quantity > 0 else "short")
                for symbol, quantity in self.held.items() if quantity]

    def submit_order(self, request):
        if self.fail_orders:
            raise RuntimeError("rejected")
        self.sent.append(request)
        signed = int(request.qty) * (1 if request.side == "buy" else -1)
        record = SimpleNamespace(id=f"order-{len(self.sent)}", symbol=request.symbol, status="accepted",
                                 filled_qty=Decimal(0), filled_avg_price=None,
                                 client_order_id=request.client_order_id, signed=signed)
        if self.fill_orders:
            record.status, record.filled_qty = "filled", Decimal(abs(signed))
            record.filled_avg_price = self._price(request.symbol)
            self.held[request.symbol] = self.held.get(request.symbol, 0) + signed
        self.records[request.client_order_id] = record
        return record

    def orders(self, *, status=None, client_order_id=None):
        if client_order_id is not None:
            record = self.records.get(client_order_id)
            return [] if record is None else [record]
        return [record for record in self.records.values()
                if status != "open" or record.status in ("accepted", "new", "partially_filled")]

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        for record in self.records.values():
            if record.id == order_id and record.status != "filled":
                record.status = "canceled"


def run_session(runner: live.ShadowRunner, provider: FakeProvider, day: date, *, step: float = 0.5,
                until: float = MINUTES, each=None) -> None:
    minute = 0.1
    while minute < until:
        provider.set(day, minute)
        if each is not None:
            each(minute)
        runner.step()
        minute += step
    if until >= MINUTES:
        provider.set(day, MINUTES + 5)
        runner.step()


class ShadowRunnerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.days = trading_days(NOISE_BAND.lookback_sessions + 2)
        self.history = [wiggle_session(day) for day in self.days[:-2]]
        self.breakout = flat_session(self.days[-2], 100.2, {45: 101.0, 200: 102.0, 370: 100.3})
        self.quiet = flat_session(self.days[-1], 100.3, {300: 101.5})
        self.sessions = [*self.history, self.breakout, self.quiet]

    def runner(self, legs, provider, **kwargs) -> live.ShadowRunner:
        return live.ShadowRunner(provider, directory=self.folder.name, legs=legs, **kwargs)

    def events(self, kind: str) -> list[dict]:
        return [event for event in live.read_journal(Path(self.folder.name) / "journal.jsonl")
                if event["kind"] == kind]

    def sent(self, provider: FakeProvider) -> list[tuple[str, int]]:
        return [(order.side, int(order.qty)) for order in provider.sent]

    def control(self, command: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return live.main([command, "--dir", self.folder.name])

    # ---- shadow ----------------------------------------------------------
    def test_noise_band_shadow_matches_the_simulator(self):
        provider = FakeProvider({"QQQ": self.sessions})
        runner = self.runner([live.Leg(live.NOISE, "QQQ")], provider)
        run_session(runner, provider, self.days[-2])
        summary = self.events("session_summary")[-1]["legs"]["noise_band_momentum:QQQ"]
        expected = simulate_noise_band(self.sessions[:-1], replace(NOISE_BAND, flat_minute=live.FLAT_MINUTE))[-1]
        self.assertEqual(summary["shadow_round_trips"], expected.round_trips)
        self.assertAlmostEqual(summary["shadow_gross"], expected.gross, places=12)
        self.assertAlmostEqual(summary["replay_gross"], expected.gross, places=12)
        self.assertFalse(provider.sent)                            # shadow mode never sends an order
        decisions = [event for event in self.events("decision") if event["target"] != FLAT]
        self.assertEqual((decisions[0]["minute"], decisions[0]["target"], decisions[0]["price_source"]),
                         (60, LONG, "quote"))

    def test_close_reversion_enters_then_exits_across_sessions(self):
        provider = FakeProvider({"SPY": self.sessions})
        runner = self.runner([live.Leg(live.REVERSION, "SPY")], provider)
        run_session(runner, provider, self.days[-2])               # closes near its low: enter
        self.assertTrue(runner.state["carry"]["close_weakness_reversion:SPY"]["in_position"])
        run_session(runner, provider, self.days[-1])               # closes at its high: exit
        trips = self.events("round_trip")
        self.assertEqual(len(trips), 1)
        self.assertAlmostEqual(trips[0]["gross"], 101.5 / 100.3 - 1.0)
        self.assertEqual(trips[0]["sessions_held"], 1)
        report = live.forward_report(live.read_journal(Path(self.folder.name) / "journal.jsonl"))
        self.assertEqual(report["legs"]["close_weakness_reversion:SPY"]["closed_round_trips"], 1)

    def test_book_parameters_drive_the_leg(self):
        book = {"schema": live.BOOK_SCHEMA, "legs": [
            {"rule": live.NOISE, "symbol": "qqq", "weight": 0.25, "trade": True, "tier": "confirmed",
             "params": {"band_multiple": 20.0, "sides": ["long"], "lookback_sessions": 7,
                        "decision_step_minutes": 60, "first_decision_minute": 60, "last_decision_minute": 360}},
            {"rule": live.REVERSION, "symbol": "SPY", "weight": 0.0, "trade": True, "tier": "provisional",
             "params": {"enter_below": 0.1, "exit_above": 0.6, "max_sessions": 3, "decision_minute": 380}}]}
        path = Path(self.folder.name) / "edge_book.json"
        path.write_text(json.dumps(book), encoding="utf-8")
        legs = live.load_book(path)
        self.assertEqual([(leg.symbol, leg.trade) for leg in legs], [("QQQ", True), ("SPY", False)])
        rules = legs[0].noise_rules()
        self.assertEqual((rules.band_multiple, rules.sides, rules.decision_minutes()[0], rules.flat_minute),
                         (20.0, ("long",), 60, live.FLAT_MINUTE))
        self.assertEqual(legs[1].reversion_rules().max_sessions, 3)
        provider = FakeProvider({"QQQ": self.sessions, "SPY": self.sessions})
        runner = self.runner(legs, provider)
        run_session(runner, provider, self.days[-2])               # a band twenty times wider is never broken
        self.assertEqual(self.events("session_summary")[-1]["legs"]["noise_band_momentum:QQQ"]
                         ["shadow_round_trips"], 0)
        path.write_text(json.dumps({"schema": live.BOOK_SCHEMA, "legs": [{"rule": "opening_range_breakout",
                                                                         "symbol": "QQQ"}]}), encoding="utf-8")
        with self.assertRaises(ValueError):
            live.load_book(path)

    def test_late_start_skips_stale_decisions(self):
        provider = FakeProvider({"QQQ": self.sessions})
        runner = self.runner([live.Leg(live.NOISE, "QQQ")], provider)
        provider.set(self.days[-2], 122)                           # started after the 10:30 breakout
        runner.step()
        skipped = [event["minute"] for event in self.events("decision_skipped")]
        self.assertEqual(skipped, [30, 60, 90])
        self.assertEqual([event["minute"] for event in self.events("decision")], [120])

    def test_half_day_is_not_traded(self):
        provider = FakeProvider({"QQQ": self.sessions}, early_close=True)
        runner = self.runner([live.Leg(live.NOISE, "QQQ")], provider)
        provider.set(self.days[-2], 70)
        self.assertEqual(runner.step(), "skipped")
        self.assertFalse(self.events("decision"))

    def test_opening_range_shadow_enters_and_stops(self):
        session = flat_session(self.days[-1], 100.0, {2: 100.5, 60: 99.0})
        provider = FakeProvider({"QQQ": [session]})
        runner = self.runner([live.Leg(live.OPENING, "QQQ")], provider)
        run_session(runner, provider, self.days[-1])
        summary = self.events("session_summary")[-1]["legs"]["opening_range_breakout:QQQ"]
        self.assertEqual(summary["shadow_round_trips"], 1)
        self.assertLess(summary["shadow_gross"], 0)
        self.assertAlmostEqual(summary["replay_gross"], 100.0 / 100.5 - 1.0)

    # ---- orders ----------------------------------------------------------
    def test_orders_follow_the_shadow_and_are_confirmed_by_the_broker(self):
        provider = FakeProvider({"QQQ": self.sessions})
        runner = self.runner([live.Leg(live.NOISE, "QQQ", 0.5, True)], provider, orders="paper")
        run_session(runner, provider, self.days[-2])
        self.assertEqual(self.sent(provider), [("buy", 495), ("sell", 495)])   # 50,000 / 101.0, out at 15:58
        self.assertTrue(all(order.time_in_force == "day" and order.type == "market" for order in provider.sent))
        self.assertTrue(provider.sent[0].client_order_id.startswith("sedge-noise-QQQ-"))
        fills = self.events("fill")
        self.assertEqual([(fill["shares"], fill["holding"]) for fill in fills], [(495, 495), (-495, 0)])
        self.assertEqual(fills[0]["slippage_bps"], 0.0)
        self.assertEqual(provider.held["QQQ"], 0)
        report = live.forward_report(live.read_journal(Path(self.folder.name) / "journal.jsonl"))
        self.assertEqual(report["legs"]["noise_band_momentum:QQQ"]["broker_fills"], 2)
        self.assertEqual(report["problem_events"], {})

    def test_max_notional_caps_the_position(self):
        provider = FakeProvider({"QQQ": self.sessions})
        runner = self.runner([live.Leg(live.NOISE, "QQQ", 0.5, True)], provider, orders="paper",
                             max_notional=2000.0)
        run_session(runner, provider, self.days[-2])
        self.assertEqual(self.sent(provider), [("buy", 19), ("sell", 19)])     # 2,000 / 101.0

    def test_failed_orders_stop_after_a_few_attempts_and_the_shadow_runs_on(self):
        provider = FakeProvider({"QQQ": self.sessions})
        provider.fail_orders = True
        runner = self.runner([live.Leg(live.NOISE, "QQQ", 0.5, True)], provider, orders="paper")
        run_session(runner, provider, self.days[-2])
        self.assertEqual(len(self.events("order_failed")), live.MAX_ENTRY_ATTEMPTS)
        self.assertEqual(len(self.events("order_gave_up")), 1)
        self.assertEqual(self.events("session_summary")[-1]["legs"]["noise_band_momentum:QQQ"]
                         ["shadow_round_trips"], 1)
        self.assertFalse(provider.held)

    def test_end_of_day_retries_an_exit_that_failed_earlier(self):
        round_trip = flat_session(self.days[-2], 100.2, {45: 101.0, 100: 100.2})   # in at 10:30, out at 11:30
        provider = FakeProvider({"QQQ": [*self.history, round_trip]})
        runner = self.runner([live.Leg(live.NOISE, "QQQ", 0.5, True)], provider, orders="paper")

        def reject_midday(minute: float) -> None:
            provider.fail_orders = 100 <= minute < 300

        run_session(runner, provider, self.days[-2], each=reject_midday)
        self.assertEqual(self.sent(provider), [("buy", 495), ("sell", 495)])
        self.assertTrue(self.events("order_gave_up"))
        self.assertEqual(provider.held["QQQ"], 0)
        self.assertEqual(runner.state["orders"]["noise_band_momentum:QQQ"]["shares"], 0)

    def test_an_order_that_never_fills_is_cancelled_and_never_counted(self):
        provider = FakeProvider({"QQQ": self.sessions})
        provider.fill_orders = False
        runner = self.runner([live.Leg(live.NOISE, "QQQ", 0.5, True)], provider, orders="paper")
        run_session(runner, provider, self.days[-2])
        self.assertTrue(self.events("order_timeout"))
        self.assertTrue(self.events("order_dead"))
        self.assertEqual(len(provider.sent), live.MAX_ENTRY_ATTEMPTS)
        self.assertEqual(len(provider.cancelled), live.MAX_ENTRY_ATTEMPTS)
        self.assertFalse(self.events("fill"))
        self.assertEqual(runner.state["orders"]["noise_band_momentum:QQQ"]["shares"], 0)

    def test_a_flip_is_two_orders_and_never_crosses_zero_in_one(self):
        flip = flat_session(self.days[-2], 100.2, {45: 101.0, 100: 99.0})          # long at 10:30, short at 11:30
        provider = FakeProvider({"QQQ": [*self.history, flip]})
        runner = self.runner([live.Leg(live.NOISE, "QQQ", 0.5, True)], provider, orders="paper")
        run_session(runner, provider, self.days[-2])
        self.assertEqual(self.sent(provider), [("buy", 495), ("sell", 495), ("sell", 505), ("buy", 505)])
        self.assertEqual(provider.held["QQQ"], 0)

    def test_short_selling_off_keeps_the_traded_leg_long_only(self):
        down = flat_session(self.days[-2], 100.2, {45: 99.0})
        provider = FakeProvider({"QQQ": [*self.history, down]})
        runner = self.runner([live.Leg(live.NOISE, "QQQ", 0.5, True)], provider, orders="paper",
                             allow_short=False)
        run_session(runner, provider, self.days[-2])
        self.assertFalse(provider.sent)
        self.assertEqual(self.events("session_summary")[-1]["legs"]["noise_band_momentum:QQQ"]
                         ["shadow_round_trips"], 0)

    def test_shares_the_runner_did_not_buy_block_entries_and_are_never_sold(self):
        provider = FakeProvider({"QQQ": self.sessions})
        provider.held["QQQ"] = 40                                   # the account already owns QQQ
        runner = self.runner([live.Leg(live.NOISE, "QQQ", 0.5, True)], provider, orders="paper")
        run_session(runner, provider, self.days[-2])
        self.assertFalse(provider.sent)
        self.assertEqual(provider.held["QQQ"], 40)
        self.assertTrue(self.events("position_mismatch"))
        self.assertEqual(self.events("entry_blocked")[0]["reason"], "foreign_position")

    def test_shares_that_vanish_are_adopted_not_sold_again(self):
        provider = FakeProvider({"QQQ": self.sessions})
        runner = self.runner([live.Leg(live.NOISE, "QQQ", 0.5, True)], provider, orders="paper")

        def close_by_hand(minute: float) -> None:
            if 150 <= minute < 150.5:
                provider.held["QQQ"] = 0                            # someone closed the position elsewhere

        run_session(runner, provider, self.days[-2], each=close_by_hand)
        self.assertEqual(self.sent(provider), [("buy", 495)])
        self.assertEqual(self.events("position_mismatch")[0]["broker"], 0)
        self.assertEqual(provider.held["QQQ"], 0)

    def test_daily_loss_limit_halts_new_entries(self):
        provider = FakeProvider({"QQQ": self.sessions})
        runner = self.runner([live.Leg(live.NOISE, "QQQ", 0.5, True)], provider, orders="paper")
        provider.set(self.days[-2], 1)
        runner.step()
        self.assertEqual(runner.state["equity_at_open"], 100000.0)
        provider.equity = Decimal("97000")
        run_session(runner, provider, self.days[-2])
        self.assertEqual(self.events("entries_halted")[0]["reason"], "daily loss limit")
        self.assertFalse(provider.sent)

    def test_halt_file_blocks_entries_and_flatten_closes_a_swing_position(self):
        provider = FakeProvider({"SPY": self.sessions})
        runner = self.runner([live.Leg(live.REVERSION, "SPY", 0.25, True)], provider, orders="paper")
        run_session(runner, provider, self.days[-2])               # buys the weak close
        self.assertEqual(self.sent(provider), [("buy", 249)])       # 25,000 / 100.3
        self.assertEqual(provider.held["SPY"], 249)
        self.assertEqual(self.control("flatten"), 0)
        run_session(runner, provider, self.days[-1], until=20)
        self.assertEqual(self.sent(provider), [("buy", 249), ("sell", 249)])
        self.assertTrue(self.events("flatten_complete"))
        self.assertTrue((Path(self.folder.name) / live.HALT_FILE).exists())
        self.assertFalse((Path(self.folder.name) / live.FLATTEN_FILE).exists())
        self.assertEqual(self.control("resume"), 0)
        self.assertFalse((Path(self.folder.name) / live.HALT_FILE).exists())

    def test_halt_command_stops_a_new_position(self):
        provider = FakeProvider({"QQQ": self.sessions})
        runner = self.runner([live.Leg(live.NOISE, "QQQ", 0.5, True)], provider, orders="paper")
        self.assertEqual(self.control("halt"), 0)
        run_session(runner, provider, self.days[-2])
        self.assertFalse(provider.sent)
        self.assertEqual(self.events("entry_blocked")[0]["reason"], "entries halted")

    def test_intraday_shares_left_overnight_are_closed_at_the_next_open(self):
        provider = FakeProvider({"QQQ": self.sessions})
        runner = self.runner([live.Leg(live.NOISE, "QQQ", 0.5, True)], provider, orders="paper")
        run_session(runner, provider, self.days[-2], until=200)     # the runner dies while long
        self.assertEqual(provider.held["QQQ"], 495)
        again = self.runner([live.Leg(live.NOISE, "QQQ", 0.5, True)], provider, orders="paper")
        run_session(again, provider, self.days[-1], until=3)
        self.assertEqual(self.sent(provider), [("buy", 495), ("sell", 495)])
        self.assertEqual(provider.held["QQQ"], 0)

    def test_a_book_that_drops_a_leg_holding_shares_is_refused(self):
        provider = FakeProvider({"SPY": self.sessions, "QQQ": self.sessions})
        swing = live.Leg(live.REVERSION, "SPY", 0.25, True)
        runner = self.runner([swing], provider, orders="paper")
        run_session(runner, provider, self.days[-2])               # ends the day long SPY
        self.assertEqual(provider.held["SPY"], 249)
        other = [live.Leg(live.NOISE, "QQQ", 0.25, True), replace(swing, trade=False)]
        with self.assertRaises(RuntimeError):
            self.runner(other, provider, orders="paper")
        provider.set(self.days[-1], 5)
        result = live.preflight(provider, other, orders="paper", directory=self.folder.name, allow_short=False)
        self.assertIn("no_shares_in_untraded_legs", {item["name"] for item in result["checks"] if not item["ok"]})
        self.assertEqual(self.runner(other, provider).orders, "none")   # shadow mode is unaffected
        self.assertEqual(self.runner([swing], provider, orders="paper").orders, "paper")

    def test_restart_resumes_without_repeating_a_decision(self):
        provider = FakeProvider({"QQQ": self.sessions})
        legs = [live.Leg(live.NOISE, "QQQ", 0.5, True)]
        runner = self.runner(legs, provider, orders="paper")
        for minute in (30.2, 60.2):
            provider.set(self.days[-2], minute)
            runner.step()
        again = self.runner(legs, provider, orders="paper")
        provider.set(self.days[-2], 61.0)
        again.step()
        self.assertEqual(len(provider.sent), 1)
        self.assertEqual(again.state["legs"]["noise_band_momentum:QQQ"]["position"], LONG)
        self.assertEqual(again.state["orders"]["noise_band_momentum:QQQ"]["shares"], 495)

    # ---- guards ----------------------------------------------------------
    def test_configuration_guards(self):
        provider = FakeProvider({})
        momentum = live.Leg(live.NOISE, "QQQ", 0.5, True)
        with self.assertRaises(ValueError):
            self.runner([momentum, live.Leg(live.REVERSION, "QQQ", 0.2, True)], provider)
        with self.assertRaises(ValueError):
            self.runner([live.Leg(live.NOISE, "QQQ", 0.8, True), live.Leg(live.REVERSION, "SPY", 0.5, True)],
                        provider, orders="paper")
        with self.assertRaises(ValueError):
            self.runner([live.Leg(live.OPENING, "QQQ", 0.1, True)], provider)
        with self.assertRaises(RuntimeError):
            self.runner([momentum], provider, orders="live", max_notional=1000.0)   # paper endpoint
        provider.mode, provider.paper = "live", False
        with self.assertRaises(RuntimeError):
            self.runner([momentum], provider, orders="paper")
        with self.assertRaises(ValueError):
            self.runner([momentum], provider, orders="live")                         # no notional cap
        self.assertEqual(self.runner([momentum], provider, orders="live", max_notional=1000.0).orders, "live")

    def test_live_orders_need_every_flag(self):
        def mode(*flags: str) -> str:
            parser = argparse.ArgumentParser()
            live._account_arguments(parser, Path(self.folder.name))
            return live.order_mode(parser.parse_args(list(flags)))

        self.assertEqual(mode(), "none")
        self.assertEqual(mode("--paper-orders"), "paper")
        self.assertEqual(mode("--mode", "live", "--live-orders", "--confirm", "LIVE", "--max-notional", "500"),
                         "live")
        for flags in (("--live-orders",), ("--mode", "live", "--live-orders"),
                      ("--mode", "live", "--live-orders", "--confirm", "LIVE"),
                      ("--mode", "live", "--live-orders", "--confirm", "yes", "--max-notional", "500"),
                      ("--mode", "live", "--paper-orders"),
                      ("--paper-orders", "--live-orders")):
            with self.assertRaises(ValueError):
                mode(*flags)

    def test_preflight_passes_clean_and_names_what_is_wrong(self):
        provider = FakeProvider({"QQQ": self.sessions, "SPY": self.sessions})
        provider.set(self.days[-1], 100)
        legs = [live.Leg(live.NOISE, "QQQ", 0.25, True), live.Leg(live.REVERSION, "SPY", 0.25, True)]
        clean = live.preflight(provider, legs, orders="paper", directory=self.folder.name, allow_short=False)
        self.assertTrue(clean["ok"], [item for item in clean["checks"] if not item["ok"]])
        provider.held["SPY"] = 10
        provider.status = "account_updated"
        (Path(self.folder.name) / live.HALT_FILE).write_text("x", encoding="utf-8")
        result = live.preflight(provider, legs, orders="live", directory=self.folder.name,
                                max_notional=50.0, allow_short=True)
        failed = {item["name"] for item in result["checks"] if not item["ok"]}
        self.assertFalse(result["ok"])
        self.assertEqual(failed, {"endpoint_matches_order_mode", "account_active",
                                  "position_is_the_runners:SPY", "size:QQQ", "size:SPY",
                                  "short_selling_available:QQQ", "no_halt_file"})
        self.assertFalse(provider.sent)                             # preflight never sends an order

    def test_trade_only_keeps_orders_for_the_named_legs(self):
        legs = live.restrict_trading(live.DEFAULT_LEGS, "close_weakness_reversion")
        self.assertEqual({leg.key for leg in legs if leg.trade}, {"close_weakness_reversion:SPY"})
        legs = live.restrict_trading(live.DEFAULT_LEGS, "noise_band_momentum:QQQ")
        self.assertEqual({leg.key for leg in legs if leg.trade}, {"noise_band_momentum:QQQ"})
        self.assertEqual(live.restrict_trading(live.DEFAULT_LEGS, ""), live.DEFAULT_LEGS)
        with self.assertRaises(ValueError):
            live.restrict_trading(live.DEFAULT_LEGS, "momentum")

    def test_default_legs_are_the_study_candidates(self):
        traded = {leg.key for leg in live.DEFAULT_LEGS if leg.trade}
        self.assertEqual(traded, {"noise_band_momentum:QQQ", "close_weakness_reversion:SPY"})
        self.assertLessEqual(sum(leg.weight for leg in live.DEFAULT_LEGS if leg.trade), 1.0)


def daily_bars(count: int, start: date, pattern, *, level: float = 100.0) -> list[DailyBar]:
    """Daily bars built by ``pattern(index, previous_close) -> (open, high, low, close)``."""
    bars: list[DailyBar] = []
    close = level
    for index, day in enumerate(trading_days(count, start)):
        o, h, l, close = pattern(index, close)
        bars.append(DailyBar(day, o, h, l, close))
    return bars


def reverting(index: int, previous: float) -> tuple[float, float, float, float]:
    """Every third day closes on its low, down 1%; the next day recovers it and a little more."""
    phase = index % 3
    if phase == 0:
        close = previous * 0.99
        return previous, previous, close, close
    if phase == 1:
        close = previous * 1.0125
        return previous, close, previous, close
    close = previous * 0.9995
    return previous, previous * 1.001, previous * 0.998, close


def drifting(index: int, previous: float) -> tuple[float, float, float, float]:
    """No pattern: alternating small moves that always close mid-range."""
    close = previous * (1.001 if index % 2 else 0.999)
    return previous, max(previous, close) * 1.002, min(previous, close) * 0.998, close


class GrammarTests(unittest.TestCase):
    def test_specs_are_validated_typed_and_content_addressed(self):
        spec = {"family": "noise_band_momentum", "symbol": "QQQ", "params": {"band_multiple": 1.1}}
        canonical = grammar.normalise(spec)
        self.assertEqual(canonical["params"], {"band_multiple": 1.1, "decision_step_minutes": 30,
                                               "lookback_sessions": 14, "sides": "both"})
        self.assertEqual(grammar.spec_id(spec), grammar.spec_id(canonical))
        ten = {"family": "opening_range_breakout", "symbol": "QQQ", "params": {"target_r": 10}}
        self.assertEqual(grammar.spec_id(ten), grammar.spec_id({**ten, "params": {"target_r": 10.0}}))
        for bad in ({"family": "martingale", "symbol": "QQQ"},
                    {"family": "noise_band_momentum", "symbol": "QQQ", "params": {"band_multiple": 9}},
                    {"family": "noise_band_momentum", "symbol": "QQQ", "params": {"leverage": 4}},
                    {"family": "noise_band_momentum", "symbol": "QQQ", "params": {"sides": "short"}},
                    {"family": "close_weakness_reversion", "symbol": "SPY", "params": {"max_sessions": 2.5}},
                    {"family": "close_weakness_reversion", "symbol": "", "params": {}},
                    {"family": "close_weakness_reversion", "symbol": "SPY", "code": "import os"}):
            with self.assertRaises(grammar.SpecError):
                grammar.normalise(bad)

    def test_grid_and_neighbours(self):
        family = grammar.FAMILIES["close_weakness_reversion"]
        specs = list(grammar.enumerate_family(family, "SPY"))
        self.assertEqual(len(specs), 30)
        self.assertEqual(len({grammar.spec_id(spec) for spec in specs}), 30)
        near = grammar.neighbours({"family": "close_weakness_reversion", "symbol": "SPY",
                                   "params": {"enter_below": 0.2, "exit_above": 0.5, "max_sessions": 3}})
        changed = sorted((name, spec["params"][name]) for spec in near
                         for name in spec["params"] if spec["params"][name] != {"enter_below": 0.2,
                                                                               "exit_above": 0.5,
                                                                               "max_sessions": 3}[name])
        self.assertEqual(changed, [("enter_below", 0.15), ("enter_below", 0.25), ("exit_above", 0.6),
                                   ("max_sessions", 1), ("max_sessions", 5)])
        off_grid = grammar.neighbours({"family": "close_weakness_reversion", "symbol": "SPY",
                                       "params": {"enter_below": 0.22}})
        self.assertIn(0.20, [spec["params"]["enter_below"] for spec in off_grid])
        self.assertIn(0.25, [spec["params"]["enter_below"] for spec in off_grid])

    def test_every_family_runs_and_says_whether_the_runner_can_trade_it(self):
        days = trading_days(40)
        sessions = [wiggle_session(day) if i % 3 else flat_session(day, 100.2, {45: 101.0, 300: 99.0})
                    for i, day in enumerate(days)]
        bars = daily_bars(300, BASE, reverting)
        for family in grammar.FAMILIES.values():
            data = sessions if family.data == grammar.MINUTE else bars
            results = grammar.simulator(next(iter(grammar.enumerate_family(family, "X"))))(data)
            self.assertEqual(len(results), len(data), family.name)
            self.assertEqual(family.executable, family.runner_rule is not None, family.name)
        described = grammar.describe()
        self.assertEqual(set(described), set(grammar.FAMILIES))
        json.dumps(described)


class FamilyTests(unittest.TestCase):
    def test_last_hour_follows_or_fades_the_days_move(self):
        days = trading_days(15)
        history = [wiggle_session(day) for day in days[:-1]]
        up = flat_session(days[-1], 100.0, {100: 101.0, 380: 102.0})
        follow = simulate_last_hour([*history, up], mode="follow")[-1]
        fade = simulate_last_hour([*history, up], mode="fade")[-1]
        self.assertEqual((follow.fills[0].side, follow.fills[0].entry_minute), (LONG, 330))
        self.assertAlmostEqual(follow.gross, 102.0 / 101.0 - 1.0)
        self.assertAlmostEqual(fade.gross, -(102.0 / 101.0 - 1.0))
        self.assertEqual(simulate_last_hour([*history, up], threshold=50.0)[-1].round_trips, 0)

    def test_gap_is_faded_only_when_it_is_unusual(self):
        days = trading_days(23)
        closes = [100.0 + 0.1 * (i % 2) for i in range(22)]
        quiet = [flat_session(day, level) for day, level in zip(days[:-1], closes)]
        gapped = flat_session(days[-1], 103.0, {30: 102.0})         # opens 3% up, slips back
        result = simulate_gap([*quiet, gapped], mode="fade", threshold=1.0, exit_minute=60)[-1]
        self.assertEqual(result.fills[0].side, SHORT)
        self.assertAlmostEqual(result.gross, -(102.0 / 103.0 - 1.0))
        self.assertTrue(all(row.round_trips == 0 for row in simulate_gap(quiet, threshold=3.0)))

    def test_overnight_hold_earns_close_to_next_open(self):
        days = trading_days(2)
        sessions = [flat_session(days[0], 100.0), flat_session(days[1], 101.0)]
        results = simulate_overnight_hold(sessions, condition="any")
        self.assertEqual((results[0].gross, results[0].round_trips), (0.0, 0))
        self.assertAlmostEqual(results[1].gross, 0.01)
        self.assertEqual(results[1].round_trips, 1)
        weak_only = simulate_overnight_hold(sessions, condition="weak_close")
        self.assertEqual(sum(row.round_trips for row in weak_only), 0)   # a flat day is not a weak close

    def test_daily_families(self):
        bars = daily_bars(60, BASE, reverting)
        streak = simulate_down_streak_daily(bars, streak=2, max_sessions=1)   # two lower closes, then the bounce
        self.assertAlmostEqual(sum(row.gross for row in streak), 19 * 0.0125)
        self.assertEqual(sum(row.round_trips for row in streak), 19)
        rsi = simulate_rsi_reversion_daily(bars, period=2, enter_below=40.0, exit_above=50.0)
        self.assertGreater(sum(row.round_trips for row in rsi), 5)
        month = simulate_turn_of_month_daily(daily_bars(64, date(2026, 3, 2), drifting),
                                             enter_days_before=1, hold_days_after=3)
        self.assertEqual(sum(row.round_trips for row in month), 3)   # March, April and May month ends
        # Three sessions held at the start of March, April and May; the very first bar has no prior close.
        self.assertEqual(sum(1 for row in month if row.gross != 0.0), 8)


class DiscoveryTests(unittest.TestCase):
    START = date(2012, 1, 2)

    def test_corrections(self):
        p_values = {"a": 0.001, "b": 0.012, "c": 0.04, "d": 0.5}
        self.assertEqual(discover.holm(p_values), {"a", "b"})
        self.assertEqual(discover.holm({"a": 0.03, "b": 0.04}), set())
        self.assertEqual(discover.benjamini_yekutieli({"a": 0.0001, "b": 0.5}), {"a"})
        self.assertEqual(discover.benjamini_yekutieli({}), set())

    def test_a_real_effect_is_confirmed_and_noise_is_not(self):
        daily = {"REAL": daily_bars(3600, self.START, reverting), "NOISE": daily_bars(3600, self.START, drifting)}
        record = discover.discover(minute={}, daily=daily,
                                   families=["close_weakness_reversion_daily", "down_streak_daily"])
        by_symbol: dict[str, set[str]] = {}
        for row in record["promoted"].values():
            by_symbol.setdefault(row["spec"]["symbol"], set()).add(row["tier"])
        self.assertIn(discover.CONFIRMED, by_symbol["REAL"])
        self.assertNotIn("NOISE", by_symbol)                        # never passed the design screen
        self.assertEqual(record["funnel"]["close_weakness_reversion_daily"]["tested"], 60)
        self.assertNotIn("holdout", json.dumps(record["design_leaderboard"]))
        book = discover.build_book(record)
        traded = [leg for leg in book["legs"] if leg["trade"]]
        self.assertEqual([(leg["rule"], leg["symbol"], leg["tier"]) for leg in traded],
                         [("close_weakness_reversion", "REAL", discover.CONFIRMED)])
        self.assertEqual(traded[0]["weight"], discover.MAX_LEG_WEIGHT)
        self.assertEqual(set(traded[0]["params"]), {"decision_minute", "enter_below", "exit_above", "max_sessions"})
        text = discover.render_report(record, book)
        self.assertIn("confirmed", text)

    def test_the_ledger_only_grows_and_proposals_are_bounded(self):
        daily = {"REAL": daily_bars(3600, self.START, reverting)}
        ledger = discover.load_ledger(None)
        first = discover.discover(minute={}, daily=daily, ledger=ledger,
                                  families=["close_weakness_reversion_daily"])
        self.assertEqual(first["promoted_ever"], 1)
        proposals = [{"family": "close_weakness_reversion_daily", "symbol": "REAL",
                      "params": {"enter_below": 0.22}},
                     {"family": "close_weakness_reversion_daily", "symbol": "REAL", "params": {"enter_below": 5}},
                     {"family": "close_weakness_reversion_daily", "symbol": "ABSENT", "params": {}},
                     {"family": "print_money", "symbol": "REAL"}]
        second = discover.discover(minute={}, daily=daily, ledger=ledger, proposals=proposals,
                                   families=["down_streak_daily"])
        self.assertGreaterEqual(second["promoted_ever"], 2)        # the first champion is still counted
        self.assertTrue(set(first["promoted"]) <= set(second["promoted"]))
        self.assertEqual(len(second["rejected_proposals"]), 3)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ledger.json"
            path.write_text(json.dumps(ledger), encoding="utf-8")
            self.assertEqual(set(discover.load_ledger(path)["promoted"]), set(second["promoted"]))
            path.write_text(json.dumps({"schema": "other"}), encoding="utf-8")
            self.assertEqual(discover.load_ledger(path)["promoted"], {})

    def test_book_trades_one_leg_per_underlying_and_never_a_leveraged_fund(self):
        def promoted(symbol: str, t_value: float, tier: str) -> dict:
            return {"spec": {"family": "close_weakness_reversion_daily", "symbol": symbol,
                             "params": {"enter_below": 0.2, "exit_above": 0.5, "max_sessions": 5}},
                    "tier": tier, "holdout": {"t": t_value, "sharpe": 1.0}}

        record = {"holdout_start": "2020-01-01", "promoted_ever": 6, "promoted": {
            "a": promoted("SOXL", 3.9, discover.CONFIRMED), "b": promoted("SPY", 3.8, discover.CONFIRMED),
            "c": promoted("VOO", 3.7, discover.CONFIRMED), "d": promoted("XLK", 3.6, discover.CONFIRMED),
            "e": promoted("DIA", 2.9, discover.PROVISIONAL), "f": promoted("TLT", 0.2, discover.UNCONFIRMED)}}
        registered = {"minute": {"QQQ (index proxy)": {
            "noise_band_momentum": {"verdict": "paper_candidate", "evidence": {"holdout": {"t_newey_west": 2.4}}},
            "close_weakness_reversion": {"verdict": "paper_candidate",
                                         "evidence": {"holdout": {"t_newey_west": 2.2}}},
            "opening_range_breakout": {"verdict": "shadow_only", "evidence": {}}}}}
        book = discover.build_book(record, registered)
        rows = {(leg["rule"], leg["symbol"]): leg for leg in book["legs"]}
        self.assertEqual({key for key, leg in rows.items() if leg["trade"]},
                         {("close_weakness_reversion", "SPY"), ("close_weakness_reversion", "XLK"),
                          ("noise_band_momentum", "QQQ")})
        self.assertIn("leveraged", rows[("close_weakness_reversion", "SOXL")]["note"])
        self.assertIn("underlying", rows[("close_weakness_reversion", "VOO")]["note"])
        self.assertIn("symbol already owned", rows[("close_weakness_reversion", "QQQ")]["note"])
        self.assertIn("tier", rows[("close_weakness_reversion", "DIA")]["note"])
        self.assertNotIn(("close_weakness_reversion", "TLT"), rows)
        self.assertEqual(rows[("close_weakness_reversion", "SPY")]["weight"], 0.25)
        self.assertEqual(rows[("noise_band_momentum", "QQQ")]["weight"], 0.25)
        self.assertLessEqual(sum(leg["weight"] for leg in book["legs"]), 1.0)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "edge_book.json"
            path.write_text(json.dumps(book), encoding="utf-8")
            legs = live.load_book(path)                              # the runner reads what the engine writes
        self.assertEqual(sum(1 for leg in legs if leg.trade), 3)

    def test_command_line_writes_the_record_the_book_and_the_report(self):
        with tempfile.TemporaryDirectory() as folder:
            stamps = []
            columns: dict[str, list[float]] = {"o": [], "h": [], "l": [], "c": []}
            for bar in daily_bars(3600, self.START, reverting):
                stamps.append(int(datetime.combine(bar.day, time(14, 30), timezone.utc).timestamp()))
                for key, value in zip("ohlc", (bar.open, bar.high, bar.low, bar.close)):
                    columns[key].append(value)
            with gzip.open(Path(folder) / "REAL.json.gz", "wt", encoding="utf-8") as handle:
                json.dump({"t": stamps, **columns, "adj": columns["c"]}, handle)
            out = Path(folder) / "out"
            with contextlib.redirect_stdout(io.StringIO()):
                code = discover.main(["--daily-dir", folder, "--daily", "REAL", "--out", str(out),
                                      "--families", "close_weakness_reversion_daily"])
            self.assertEqual(code, 0)
            self.assertEqual({path.name for path in out.iterdir()},
                             {"discovery.json", "discovery.md", "edge_book.json", "ledger.json"})
            book = json.loads((out / "edge_book.json").read_text(encoding="utf-8"))
            self.assertEqual(book["schema"], live.BOOK_SCHEMA)
            self.assertTrue(any(leg["trade"] for leg in book["legs"]))


class ProposalTests(unittest.TestCase):
    RECORD = {"design_leaderboard": [{"spec": {"family": "close_weakness_reversion_daily", "symbol": "SPY",
                                               "params": {}}, "design_t": 4.2}],
              "promoted": {"abc": {"spec": {"family": "down_streak_daily", "symbol": "QQQ", "params": {}},
                                   "design": {"t": 3.1}, "holdout": {"t": 9.9, "sharpe": 7.7},
                                   "tier": "confirmed"}}}

    def test_prompt_carries_the_grammar_and_no_holdout_result(self):
        prompt = propose.build_prompt(self.RECORD, ["SPY", "QQQ"], count=500)
        payload = json.loads(prompt["user"])
        self.assertEqual(set(payload["grammar"]), set(grammar.FAMILIES))
        self.assertEqual(payload["wanted"], propose.MAX_PROPOSALS)
        self.assertEqual(payload["already_promoted"],
                         [{"spec": self.RECORD["promoted"]["abc"]["spec"], "design_t": 3.1}])
        for leak in ("holdout", "9.9", "7.7", "confirmed"):
            self.assertNotIn(leak, prompt["user"])

    def test_reply_is_bounded_by_the_grammar(self):
        reply = "```json\n" + json.dumps({"hypotheses": [
            {"family": "close_weakness_reversion", "symbol": "SPY", "params": {"enter_below": 0.18},
             "thesis": "Late-day forced selling by volatility-targeting funds overshoots and is bought back."},
            {"family": "close_weakness_reversion", "symbol": "SPY", "params": {"enter_below": 0.18},
             "thesis": "The same idea again."},
            {"family": "close_weakness_reversion", "symbol": "TSLA", "params": {}, "thesis": "Not allowed."},
            {"family": "close_weakness_reversion", "symbol": "SPY", "params": {"enter_below": 0.9},
             "thesis": "Out of range."},
            {"family": "noise_band_momentum", "symbol": "QQQ", "params": {}},
            {"family": "noise_band_momentum", "symbol": "QQQ", "params": {}, "thesis": "x", "code": "rm -rf /"},
        ]}) + "\n```"
        valid, rejected = propose.parse_reply(reply, symbols=["SPY", "QQQ"])
        self.assertEqual([(row["family"], row["params"]["enter_below"]) for row in valid],
                         [("close_weakness_reversion", 0.18)])
        self.assertEqual(len(rejected), 5)
        self.assertEqual(propose.parse_reply("I think you should buy more.")[0], [])
        self.assertEqual(propose.parse_reply(json.dumps({"ideas": 1}))[1][0]["reason"],
                         "reply has no list of hypotheses")
        grammar.normalise(valid[0])                                 # what is written can be read back

    def test_ask_sends_both_messages_and_returns_the_text(self):
        seen = {}

        class Completions:
            def create(self, **kwargs):
                seen.update(kwargs)
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="[]"))])

        client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        prompt = propose.build_prompt(None, ["SPY"])
        self.assertEqual(propose.ask("some-model", prompt, client=client), "[]")
        self.assertEqual(seen["model"], "some-model")
        self.assertEqual([message["role"] for message in seen["messages"]], ["system", "user"])


class DeploymentOverrideTests(unittest.TestCase):
    def test_override_runs_the_shadow_without_orders(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not installed")
        root = Path(__file__).resolve().parents[2]
        override = yaml.safe_load((root / "deploy" / "compose.session-edges.yaml").read_text(encoding="utf-8"))
        service = override["services"]["session-edges"]
        self.assertEqual(service["command"][:4], ["python", "-m", "studies.session_edges.live", "run"])
        self.assertNotIn("--paper-orders", service["command"])
        self.assertEqual(service["environment"]["ALPACA_PAPER"], "true")
        self.assertEqual(service["environment"]["ALPACA_LIVE_ENABLE"], "false")
        self.assertIs(service["read_only"], True)
        self.assertEqual(service["user"], "10001:10001")
        base = yaml.safe_load((root / "compose.yaml").read_text(encoding="utf-8"))
        self.assertIn(service["secrets"][0]["source"], base["secrets"])
        self.assertIn(service["configs"][0]["source"], base["configs"])
        self.assertNotIn("session-edges", base["services"])


if __name__ == "__main__":
    unittest.main()
