"""Tests for the session-edge study and its forward shadow in ``studies/session_edges``.

Fast, offline and deterministic: every bar is synthetic and the forward
runner talks to a fake provider. Run from the repository root with::

    python -I -m unittest discover -s tests/research -t . -p test_session_edges_study.py -v
"""

from __future__ import annotations

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
from studies.session_edges import live, run, stats
from studies.session_edges import data as session_data
from studies.session_edges.data import MINUTES, DailyBar, Session, build_session
from studies.session_edges.strategies import (CLOSE_REVERSION, ENTER, EXIT, FLAT, HOLD, LONG, NOISE_BAND,
                                              SHORT, STAND_ASIDE, DayResult, NoiseBandRules,
                                              close_reversion_action, internal_bar_strength,
                                              noise_band_bounds, noise_band_sigma, noise_band_target,
                                              simulate_close_reversion, simulate_close_reversion_daily,
                                              simulate_first_last, simulate_noise_band,
                                              simulate_opening_range, simulate_overnight)

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
    """A broker stub whose clock the test moves; bars are visible once complete."""

    mode = "paper"
    paper = True

    def __init__(self, sessions: dict[str, list[Session]], *, early_close: bool = False) -> None:
        self.sessions = sessions
        self.now = datetime(2000, 1, 1, tzinfo=timezone.utc)
        self.early_close = early_close
        self.orders: list = []
        self.equity = Decimal("100000")
        self.fail_orders = False

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

    def quotes(self, symbols, start=None, end=None, feed=None):
        """The quote is the open of the minute the clock is in, with no spread."""
        out = {}
        local = self.now.astimezone(NEW_YORK)
        minute = local.hour * 60 + local.minute - 570
        for symbol in symbols:
            price = None
            for session in self.sessions.get(symbol, []):
                if session.day == local.date() and 0 <= minute < MINUTES:
                    price = Decimal(str(session.open[minute]))
            out[symbol] = [] if price is None else [SimpleNamespace(bid=price, ask=price)]
        return out

    def account(self):
        return SimpleNamespace(equity=self.equity)

    def submit_order(self, request):
        if self.fail_orders:
            raise RuntimeError("rejected")
        self.orders.append(request)
        return request


def run_session(runner: live.ShadowRunner, provider: FakeProvider, day: date, *, step: float = 0.5) -> None:
    minute = 0.1
    while minute < MINUTES:
        provider.set(day, minute)
        runner.step()
        minute += step
    provider.set(day, MINUTES + 5)
    runner.step()


class ShadowRunnerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.days = trading_days(NOISE_BAND.lookback_sessions + 2)
        history = [wiggle_session(day) for day in self.days[:-2]]
        self.breakout = flat_session(self.days[-2], 100.2, {45: 101.0, 200: 102.0, 370: 100.3})
        self.quiet = flat_session(self.days[-1], 100.3, {300: 101.5})
        self.sessions = [*history, self.breakout, self.quiet]

    def runner(self, legs, provider, **kwargs) -> live.ShadowRunner:
        return live.ShadowRunner(provider, directory=self.folder.name, legs=legs, **kwargs)

    def events(self, kind: str) -> list[dict]:
        return [event for event in live.read_journal(Path(self.folder.name) / "journal.jsonl")
                if event["kind"] == kind]

    def test_noise_band_shadow_matches_the_simulator(self):
        provider = FakeProvider({"QQQ": self.sessions})
        runner = self.runner([live.Leg(live.NOISE, "QQQ")], provider)
        run_session(runner, provider, self.days[-2])
        summary = self.events("session_summary")[-1]["legs"]["noise_band_momentum:QQQ"]
        expected = simulate_noise_band(self.sessions[:-1], replace(NOISE_BAND, flat_minute=live.FLAT_MINUTE))[-1]
        self.assertEqual(summary["shadow_round_trips"], expected.round_trips)
        self.assertAlmostEqual(summary["shadow_gross"], expected.gross, places=12)
        self.assertAlmostEqual(summary["replay_gross"], expected.gross, places=12)
        self.assertFalse(provider.orders)                          # shadow mode never sends an order
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

    def test_paper_orders_follow_the_shadow_and_flatten_before_the_close(self):
        provider = FakeProvider({"QQQ": self.sessions})
        runner = self.runner([live.Leg(live.NOISE, "QQQ", 0.5, True)], provider, paper_orders=True)
        run_session(runner, provider, self.days[-2])
        self.assertEqual([(order.side, int(order.qty)) for order in provider.orders],
                         [("buy", 495), ("sell", 495)])           # 50,000 / 101.0, then out at 15:58
        self.assertTrue(all(order.time_in_force == "day" and order.type == "market"
                            for order in provider.orders))
        self.assertTrue(provider.orders[0].client_order_id.startswith("sedge-noise-QQQ-"))

    def test_failed_order_keeps_the_shadow_running(self):
        provider = FakeProvider({"QQQ": self.sessions})
        provider.fail_orders = True
        runner = self.runner([live.Leg(live.NOISE, "QQQ", 0.5, True)], provider, paper_orders=True)
        run_session(runner, provider, self.days[-2])
        self.assertTrue(self.events("order_failed"))
        self.assertEqual(self.events("session_summary")[-1]["legs"]["noise_band_momentum:QQQ"]
                         ["shadow_round_trips"], 1)

    def test_end_of_day_closes_shares_an_earlier_exit_left_behind(self):
        days = self.days
        history = [wiggle_session(day) for day in days[:-2]]
        round_trip = flat_session(days[-2], 100.2, {45: 101.0, 100: 100.2})   # in at 10:30, out at 11:30
        provider = FakeProvider({"QQQ": [*history, round_trip]})
        runner = self.runner([live.Leg(live.NOISE, "QQQ", 0.5, True)], provider, paper_orders=True)
        minute = 0.1
        while minute < MINUTES:
            provider.fail_orders = 100 <= minute < 300                         # the 11:30 exit is rejected
            provider.set(days[-2], minute)
            runner.step()
            minute += 0.5
        self.assertEqual([(order.side, int(order.qty)) for order in provider.orders],
                         [("buy", 495), ("sell", 495)])
        self.assertIn("-eod0", provider.orders[-1].client_order_id)
        self.assertEqual(runner.state["legs"]["noise_band_momentum:QQQ"]["shares"], 0)

    def test_daily_loss_limit_halts_new_entries(self):
        provider = FakeProvider({"QQQ": self.sessions})
        runner = self.runner([live.Leg(live.NOISE, "QQQ", 0.5, True)], provider, paper_orders=True)
        provider.set(self.days[-2], 1)
        runner.step()
        runner.state["equity_at_open"] = 100000.0
        provider.equity = Decimal("97000")
        run_session(runner, provider, self.days[-2])
        self.assertTrue(self.events("entries_halted"))
        self.assertFalse(provider.orders)

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

    def test_restart_resumes_without_repeating_a_decision(self):
        provider = FakeProvider({"QQQ": self.sessions})
        runner = self.runner([live.Leg(live.NOISE, "QQQ", 0.5, True)], provider, paper_orders=True)
        for minute in (30.2, 60.2):
            provider.set(self.days[-2], minute)
            runner.step()
        again = self.runner([live.Leg(live.NOISE, "QQQ", 0.5, True)], provider, paper_orders=True)
        provider.set(self.days[-2], 61.0)
        again.step()
        self.assertEqual(len(provider.orders), 1)
        self.assertEqual(again.state["legs"]["noise_band_momentum:QQQ"]["position"], LONG)

    def test_opening_range_shadow_enters_and_stops(self):
        session = flat_session(self.days[-1], 100.0, {2: 100.5, 60: 99.0})
        provider = FakeProvider({"QQQ": [session]})
        runner = self.runner([live.Leg(live.OPENING, "QQQ")], provider)
        run_session(runner, provider, self.days[-1])
        summary = self.events("session_summary")[-1]["legs"]["opening_range_breakout:QQQ"]
        self.assertEqual(summary["shadow_round_trips"], 1)
        self.assertLess(summary["shadow_gross"], 0)
        self.assertAlmostEqual(summary["replay_gross"], 100.0 / 100.5 - 1.0)

    def test_configuration_guards(self):
        provider = FakeProvider({})
        with self.assertRaises(ValueError):
            self.runner([live.Leg(live.NOISE, "QQQ", 0.5, True), live.Leg(live.REVERSION, "QQQ", 0.2, True)],
                        provider)
        with self.assertRaises(ValueError):
            self.runner([live.Leg(live.NOISE, "QQQ", 0.8, True), live.Leg(live.REVERSION, "SPY", 0.5, True)],
                        provider, paper_orders=True)
        provider.mode = "live"
        with self.assertRaises(RuntimeError):
            self.runner([live.Leg(live.NOISE, "QQQ", 0.5, True)], provider, paper_orders=True)

    def test_default_legs_are_the_study_candidates(self):
        traded = {leg.key for leg in live.DEFAULT_LEGS if leg.trade}
        self.assertEqual(traded, {"noise_band_momentum:QQQ", "close_weakness_reversion:SPY"})
        self.assertLessEqual(sum(leg.weight for leg in live.DEFAULT_LEGS if leg.trade), 1.0)


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
