"""Tests for the liquid-ETF VWAP reversion study in ``studies/liquid_reversion``.

Fast, offline and deterministic: every bar is synthetic, and the SIP fetcher
talks to a fake HTTP session. Run from the repository root with::

    python -I -m unittest discover -s tests/research -t . -p test_liquid_reversion_study.py -v

(``-I`` drops the working directory from ``sys.path``; ``discover -t .`` puts
the repository root back, which ``-m unittest tests.research...`` cannot.)
"""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import logging
import math
import random
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import NormalDist

from studies.liquid_reversion import bars as bar_io
from studies.liquid_reversion import costs, daytype, fetch_sip, power, run, strategy, walkforward
from studies.liquid_reversion.bars import Bar, ny_instant

DAY = date(2026, 9, 15)  # a Tuesday, New York daylight time
SYMBOL = "SPY"
KEY = (1.5, 1.5, 1.5)  # (k_entry, cost_multiple, stop_mult)
PRIOR = [1e-4] * 200  # prior sessions' absolute log returns: 1 bp per minute


def start_of(day: date, index: int, interval: int = 60) -> datetime:
    return ny_instant(day, time(9, 30)) + timedelta(seconds=interval * index)


def make_bar(day: date, index: int, o: float, h: float, l: float, c: float, v: float = 1000.0, *,
             symbol: str = SYMBOL, interval: int = 60) -> Bar:
    return Bar(symbol, start_of(day, index, interval), o, h, l, c, v, interval)


def session(day: date = DAY, events: dict[int, tuple[float, float, float, float]] | None = None,
            levels: dict[int, float] | None = None, *, count: int = 390, symbol: str = SYMBOL,
            first_open: float | None = None) -> list[Bar]:
    """Quiet one-minute bars whose closes alternate between ``level + 0.01`` and ``level``.

    ``events`` replaces a bar by an explicit (open, high, low, close); ``levels``
    changes the quiet level from a bar index onwards. Each quiet bar opens at
    the previous close.
    """
    events = events or {}
    levels = {0: 100.0, **(levels or {})}
    out: list[Bar] = []
    level = levels[0]
    previous = first_open
    for index in range(count):
        level = levels.get(index, level)
        if index in events:
            o, h, l, c = events[index]
        else:
            c = level + (0.01 if index % 2 == 0 else 0.0)
            o = level if previous is None else previous
            h, l = max(o, c), min(o, c)
        out.append(make_bar(day, index, o, h, l, c, symbol=symbol))
        previous = c
    return out


def long_setup(signal: int = 40, **extra_events) -> tuple[dict, dict]:
    """A drop to 99.50 on bar ``signal`` and an entry bar opening at 99.52, then quiet near 99.60."""
    events = {signal: (100.00, 100.00, 99.50, 99.50), signal + 1: (99.52, 99.61, 99.50, 99.60)}
    events.update({int(key): value for key, value in extra_events.items()})
    return events, {signal + 2: 99.60}


def one_trade(bars: list[Bar], key=KEY, day: date = DAY):
    found = strategy.session_variants(bars, symbol=SYMBOL, day=day, prior_returns=PRIOR, keys=[key])
    return found[key]


def weekdays(first: date, count: int) -> list[date]:
    out, day = [], first
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def walk_session(day: date, rng: random.Random, start: float, *, count: int = 390,
                 symbol: str = SYMBOL) -> list[Bar]:
    """A seeded random walk of one-minute bars (about 2 bps per bar)."""
    out, price = [], start
    for index in range(count):
        o = price
        c = o * (1 + rng.gauss(0, 2.0) / 1e4)
        h = max(o, c) * (1 + abs(rng.gauss(0, 1.0)) / 1e4)
        l = min(o, c) * (1 - abs(rng.gauss(0, 1.0)) / 1e4)
        out.append(make_bar(day, index, o, h, l, c, float(rng.randint(500, 5000)), symbol=symbol))
        price = c
    return out


def walk_sessions(count: int, seed: int = 7, symbol: str = SYMBOL) -> dict[date, list[Bar]]:
    rng = random.Random(seed)
    out: dict[date, list[Bar]] = {}
    price = 100.0
    for day in weekdays(date(2026, 6, 1), count):
        price *= 1 + rng.gauss(0, 15.0) / 1e4  # overnight gap
        out[day] = walk_session(day, rng, price, symbol=symbol)
        price = out[day][-1].close
    return out


def scrambled(bar: Bar, rng: random.Random, *, keep_open: bool = False) -> Bar:
    """The same bar slot with random prices and volume (the open kept if asked)."""
    o = bar.open if keep_open else bar.open * (1 + rng.uniform(-0.03, 0.03))
    c = o * (1 + rng.uniform(-0.03, 0.03))
    h = max(o, c) * (1 + rng.uniform(0, 0.02))
    l = min(o, c) * (1 - rng.uniform(0, 0.02))
    return Bar(bar.symbol, bar.ts, o, h, l, c, float(rng.randint(1, 10**7)), bar.interval_seconds)


# --------------------------------------------------------------------------- power


class PowerTests(unittest.TestCase):
    def test_known_value_619_sessions(self):
        # (z_0.95 + z_0.80) x sd / effect, squared, rounded up:
        # (1.644854 + 0.841621) x 20 / 2 = 24.86475; 24.86475 ** 2 = 618.26 -> 619.
        z = NormalDist().inv_cdf(0.95) + NormalDist().inv_cdf(0.80)
        self.assertAlmostEqual(z, 2.486475, places=6)
        self.assertAlmostEqual((z * 20 / 2) ** 2, 618.2557, places=3)
        self.assertEqual(power.required_sessions(2.0, 20.0, alpha=0.05, power=0.80, one_sided=True), 619)
        # The minimum detectable effect is the inverse: 619 sessions suffice, 618 do not.
        self.assertLessEqual(power.minimum_detectable_effect(619, 20.0), 2.0)
        self.assertGreater(power.minimum_detectable_effect(618, 20.0), 2.0)
        # Two-sided needs more.
        self.assertGreater(power.required_sessions(2.0, 20.0, one_sided=False), 619)

    def test_power_table_translates_per_trade_effects_through_trades_per_session(self):
        table = power.power_table(sd_bps=20.0, trades_per_session=2.0, traded_share=0.5,
                                  effects_bps=(1.0,), sessions_for_mde=2500)
        row = table["rows"][0]
        self.assertEqual(row["traded_sessions"], 619)  # 1 bp per trade x 2 trades = 2 bps per session
        self.assertEqual(row["calendar_sessions"], 1238)  # half the sessions trade
        mde = table["mde"]
        self.assertEqual(mde["traded_sessions"], 1250)
        self.assertAlmostEqual(mde["session_bps"], 2.486475 * 20 / math.sqrt(1250), places=5)
        self.assertAlmostEqual(mde["per_trade_bps"], mde["session_bps"] / 2.0)

    def test_rounding_the_inputs_moves_the_table(self):
        # The 1-minute smoke run's measured inputs, unrounded and rounded to 3 places.
        raw = power.power_table(sd_bps=42.94074750162889, trades_per_session=15 / 7, traded_share=7 / 11)
        rounded = power.power_table(sd_bps=42.941, trades_per_session=2.143, traded_share=0.636)
        self.assertEqual(raw["rows"][0]["calendar_sessions"], 15606)
        self.assertEqual(rounded["rows"][0]["calendar_sessions"], 15614)

    def test_evaluate_builds_the_power_table_from_unrounded_measurements(self):
        days = weekdays(date(2026, 3, 2), 11)
        trades = []
        for index, net in enumerate((3.1, -7.3, 12.9, 0.7, -2.2, 5.5, 9.4)):  # 7 of 11 sessions trade
            trades += [fake_trade(days[index], net), fake_trade(days[index], net / 3, symbol="QQQ")]
        trades.append(fake_trade(days[0], 1.234, symbol="IWM"))  # 15 trades
        table = walkforward.evaluate(trades, sessions=days, draws=50)["power_table"]
        summary = power.summarise_sessions(trades)
        expected = power.power_table(sd_bps=summary.sd, trades_per_session=summary.trades_per_session,
                                     traded_share=summary.count / len(days))
        self.assertEqual([(r["traded_sessions"], r["calendar_sessions"]) for r in table["rows"]],
                         [(r["traded_sessions"], r["calendar_sessions"]) for r in expected["rows"]])
        self.assertAlmostEqual(table["mde"]["per_trade_bps"], expected["mde"]["per_trade_bps"], places=4)


    def test_whole_rule_needs_the_net_effect_to_exceed_the_cost(self):
        # The 2x-cost check is "net per trade above the mean cost per trade".
        self.assertIsNone(power.rule_required_sessions(1.0, 1.43, 20.0, 1.0))
        self.assertIsNone(power.rule_required_sessions(1.43, 1.43, 20.0, 1.0))
        # 2 bps against a 1.43 bps cost: the cost margin, not the test, binds.
        # (z_0.80 x 20 / 0.57) ** 2 = 29.5306 ** 2 = 872.06 -> 873 > 619.
        self.assertEqual(power.rule_required_sessions(2.0, 1.43, 20.0, 1.0), 873)
        # With no cost the rule is the significance test alone.
        self.assertEqual(power.rule_required_sessions(2.0, 0.0, 20.0, 1.0), 619)

    def test_whole_rule_mde_never_falls_below_the_cost(self):
        for n in (100, 1000, 10_000, 1_000_000):
            mde = power.rule_minimum_detectable_effect(n, 20.0, 2.0, 1.43)
            self.assertGreater(mde, 1.43)
            self.assertGreaterEqual(mde, power.minimum_detectable_effect(n, 20.0) / 2.0)
        self.assertAlmostEqual(power.rule_minimum_detectable_effect(10**12, 20.0, 2.0, 1.43), 1.43, places=4)

    def test_power_table_reports_the_whole_rule_beside_the_test(self):
        table = power.power_table(sd_bps=20.0, trades_per_session=1.0, traded_share=0.5,
                                  effects_bps=(1.0, 2.0), cost_bps_per_trade=1.43)
        below, above = table["rows"]
        self.assertIsNone(below["rule_traded_sessions"])
        self.assertIsNone(below["rule_calendar_sessions"])
        self.assertEqual((above["traded_sessions"], above["rule_traded_sessions"]), (619, 873))
        self.assertEqual(above["rule_calendar_sessions"], 1746)
        self.assertGreater(table["mde"]["rule_per_trade_bps"], table["mde"]["per_trade_bps"])


# --------------------------------------------------------------------------- costs


class CostTests(unittest.TestCase):
    def test_one_tick_on_spy_at_778(self):
        self.assertAlmostEqual(costs.tick_bps(778.0), 0.1285, places=4)
        self.assertAlmostEqual(costs.tick_bps(778.0), 1e4 * 0.01 / 778.0, places=12)

    def test_spread_is_the_larger_of_one_tick_and_the_measured_spread(self):
        tick = costs.tick_bps(778.0)
        self.assertAlmostEqual(costs.cost_breakdown(778.0).spread_bps, tick)
        self.assertAlmostEqual(costs.cost_breakdown(778.0, spread_bps=0.05).spread_bps, tick)
        self.assertAlmostEqual(costs.cost_breakdown(778.0, spread_bps=0.5).spread_bps, 0.5)

    def test_sec_fee_and_taf_on_sells_only_and_no_commission(self):
        self.assertEqual(costs.leg_fees_usd("buy", 778.0, 100), 0.0)
        self.assertAlmostEqual(costs.leg_fees_usd("sell", 778.0, 100),
                               20.60 * 77_800 / 1e6 + 0.000195 * 100, places=12)
        self.assertEqual(costs.taf_usd(10**6), 9.79)  # per-trade cap
        parts = costs.cost_breakdown(778.0, notional=25_000.0, slippage_bps_per_side=0.5)
        shares = 25_000.0 / 778.0
        self.assertAlmostEqual(parts.sec_fee_bps, 0.206, places=12)  # one sell leg, not two
        self.assertAlmostEqual(parts.taf_bps, 1e4 * 0.000195 * shares / 25_000.0, places=12)
        self.assertAlmostEqual(parts.slippage_bps, 1.0)  # 2 x 0.5 bps
        # Nothing else (no commission): the total is exactly the four components.
        self.assertAlmostEqual(parts.total_bps, costs.tick_bps(778.0) + 1.0 + 0.206 + parts.taf_bps, places=12)
        self.assertAlmostEqual(costs.round_trip_bps(778.0, stress=2.0), 2 * parts.total_bps, places=12)


# --------------------------------------------------------------------------- VWAP and sessions


class VwapTests(unittest.TestCase):
    def test_vwap_on_a_tiny_session(self):
        rows = [make_bar(DAY, 0, 10.0, 10.2, 9.8, 10.0, 100), make_bar(DAY, 1, 10.0, 10.6, 10.0, 10.5, 300),
                make_bar(DAY, 2, 10.5, 10.5, 10.1, 10.1, 0)]
        series = strategy.vwap_series(rows)
        self.assertAlmostEqual(series[0], 10.0)  # typical price (10.2 + 9.8 + 10.0) / 3
        self.assertAlmostEqual(series[1], (10.0 * 100 + (10.6 + 10.0 + 10.5) / 3 * 300) / 400)
        self.assertAlmostEqual(series[1], 10.275)
        self.assertAlmostEqual(series[2], 10.275)  # zero volume adds nothing

    def test_no_vwap_before_any_volume(self):
        rows = [make_bar(DAY, 0, 10.0, 10.0, 10.0, 10.0, 0), make_bar(DAY, 1, 10.0, 11.0, 9.0, 10.0, 5)]
        self.assertEqual(strategy.vwap_series(rows), [None, 10.0])

    def test_only_regular_session_bars_feed_the_vwap(self):
        regular = [make_bar(DAY, 0, 10.0, 10.2, 9.8, 10.0, 100), make_bar(DAY, 1, 10.0, 10.6, 10.0, 10.5, 300)]
        pre = make_bar(DAY, -1, 50.0, 50.0, 50.0, 50.0, 10**6)  # 09:29
        post = make_bar(DAY, 390, 50.0, 50.0, 50.0, 50.0, 10**6)  # 16:00
        grouped = bar_io.session_bars([post, *regular, pre])
        self.assertEqual(grouped[SYMBOL][DAY], regular)
        self.assertAlmostEqual(strategy.vwap_series(grouped[SYMBOL][DAY])[-1], 10.275)

    def test_a_bar_is_usable_only_from_start_plus_interval(self):
        bar = make_bar(DAY, 0, 10, 10, 10, 10, interval=300)
        self.assertEqual(bar.end, ny_instant(DAY, time(9, 35)))
        for candidate in strategy.signal_candidates(session(), day=DAY, prior_returns=PRIOR):
            self.assertEqual(candidate.signal_ts, session()[candidate.index].end)


# --------------------------------------------------------------------------- day type


class DayTypeTests(unittest.TestCase):
    def setUp(self):
        self.sessions = walk_sessions(14)
        self.days = sorted(self.sessions)
        self.last = self.days[-1]

    def test_known_day_type_is_unchanged_when_bars_after_10_00_are_corrupted(self):
        baseline = daytype.classify_sessions(self.sessions)
        self.assertTrue(baseline[self.last].known)
        rng = random.Random(1)
        cutoff = ny_instant(self.last, time(10, 0))
        corrupted = dict(self.sessions)
        corrupted[self.last] = [bar if bar.end <= cutoff else scrambled(bar, rng)
                                for bar in self.sessions[self.last]]
        self.assertNotEqual(corrupted[self.last], self.sessions[self.last])
        self.assertEqual(daytype.classify_sessions(corrupted), baseline)

    def test_the_bar_ending_at_10_00_is_read(self):
        # Sensitivity check: the 09:59 bar ends at 10:00 and so is part of the classification.
        baseline = daytype.classify_sessions(self.sessions)
        rows = list(self.sessions[self.last])
        self.assertEqual(rows[29].end, ny_instant(self.last, time(10, 0)))
        rows[29] = scrambled(rows[29], random.Random(2), keep_open=True)
        changed = daytype.classify_sessions({**self.sessions, self.last: rows})
        self.assertNotEqual(changed[self.last].drive, baseline[self.last].drive)

    def test_later_sessions_never_change_earlier_day_types(self):
        baseline = daytype.classify_sessions(self.sessions)
        extra = self.days[-1] + timedelta(days=1)
        wild = walk_session(extra, random.Random(3), 250.0)
        extended = daytype.classify_sessions({**self.sessions, extra: wild})
        self.assertEqual({day: extended[day] for day in self.days}, baseline)

    def test_run_symbol_gates_on_the_same_10_00_day_type(self):
        rng = random.Random(4)
        cutoff = ny_instant(self.last, time(10, 0))
        corrupted = {**self.sessions, self.last: [bar if bar.end <= cutoff else scrambled(bar, rng)
                                                  for bar in self.sessions[self.last]]}

        def day_types(sessions):
            return [outcome.day_type for outcome in strategy.run_symbol(
                SYMBOL, sorted(sessions.items()), keys=[KEY], day_filter=lambda _: False)]

        self.assertEqual(day_types(corrupted), day_types(self.sessions))
        self.assertEqual(day_types(self.sessions)[-1], daytype.classify_sessions(self.sessions)[self.last])


# --------------------------------------------------------------------------- signals and entries


class SignalWindowTests(unittest.TestCase):
    def test_candidates_are_completed_bars_ending_in_10_00_to_15_00(self):
        candidates = strategy.signal_candidates(session(), day=DAY, prior_returns=PRIOR)
        self.assertEqual(candidates[0].index, 29)
        self.assertEqual(candidates[0].signal_ts, ny_instant(DAY, time(10, 0)))
        self.assertEqual(candidates[-1].index, 328)
        self.assertEqual(candidates[-1].signal_ts, ny_instant(DAY, time(14, 59)))

    def test_a_deviation_before_10_00_is_not_a_signal(self):
        events = {28: (100.00, 100.00, 99.50, 99.50), 29: (99.50, 100.01, 99.50, 100.00)}
        self.assertIsNone(one_trade(session(events=events)))


class NoLookAheadTests(unittest.TestCase):
    def test_corrupting_bars_after_the_signal_changes_neither_signal_nor_entry(self):
        events, levels = long_setup()
        bars = session(events=events, levels=levels)
        trade = one_trade(bars)
        self.assertIsInstance(trade, strategy.Trade)
        self.assertEqual(trade.signal_ts, bars[40].end)
        self.assertEqual(trade.entry_ts, bars[41].ts)
        self.assertEqual(trade.entry, 99.52)
        rng = random.Random(5)
        # The entry bar keeps only its open (the fill); everything later is noise.
        corrupted = bars[:41] + [scrambled(bars[41], rng, keep_open=True)] + [scrambled(b, rng) for b in bars[42:]]
        other = one_trade(corrupted)
        self.assertIsInstance(other, strategy.Trade)
        for name in ("signal_ts", "z", "side", "entry_ts", "entry", "target", "stop"):
            self.assertEqual(getattr(other, name), getattr(trade, name), name)

    def test_each_candidate_is_the_same_on_every_prefix_of_the_session(self):
        events, levels = long_setup()
        bars = session(events=events, levels=levels)
        full = strategy.signal_candidates(bars, day=DAY, prior_returns=PRIOR)
        for cut in (30, 41, 42, 97, 200, 329, 390):
            prefix = strategy.signal_candidates(bars[:cut], day=DAY, prior_returns=PRIOR)
            self.assertEqual(prefix, [c for c in full if c.index < cut], cut)


class OneTradePerSessionTests(unittest.TestCase):
    def test_only_the_first_signal_trades(self):
        events, levels = long_setup()
        events.update({200: (100.00, 100.00, 99.40, 99.40), 201: (99.42, 99.50, 99.40, 99.45)})
        levels.update({150: 100.0, 202: 99.6})
        bars = session(events=events, levels=levels)
        outcomes = list(strategy.run_symbol(SYMBOL, [(DAY, bars)], keys=[KEY], day_filter=lambda _: True))
        trade = outcomes[0].variants[KEY]
        self.assertIsInstance(trade, strategy.Trade)
        self.assertEqual(trade.signal_ts, bars[40].end)

    def test_a_refused_first_signal_still_consumes_the_session(self):
        events = {40: (100.00, 100.00, 99.50, 99.50),
                  41: (100.10, 100.10, 99.95, 100.00),  # opens through the frozen target
                  60: (100.00, 100.00, 99.40, 99.40), 61: (99.42, 99.50, 99.40, 99.45)}
        bars = session(events=events, levels={42: 100.0, 62: 99.6})
        candidates = strategy.signal_candidates(bars, day=DAY, prior_returns=PRIOR)
        first = strategy.first_signal(candidates, 1.5, 1.5)
        self.assertEqual(first.index, 40)
        later = strategy.first_signal([c for c in candidates if c.index > 41], 1.5, 1.5)
        self.assertEqual(later.index, 60)  # a later signal would qualify and enter cleanly
        self.assertEqual(strategy.plan_entry(bars, later, day=DAY), 61)
        outcome = one_trade(bars)
        self.assertIsInstance(outcome, strategy.Refusal)
        self.assertEqual(outcome.reason, "through_target")
        self.assertEqual(outcome.signal_ts, bars[40].end)

    def test_refusal_consumes_the_session_through_the_full_simulation(self):
        refused = session(events={40: (100.00, 100.00, 99.50, 99.50), 41: (100.10, 100.10, 99.95, 100.00),
                                  60: (100.00, 100.00, 99.40, 99.40), 61: (99.42, 99.50, 99.40, 99.45)},
                          levels={42: 100.0, 62: 99.6}, symbol="QQQ")
        events, levels = long_setup()
        taken = session(events=events, levels=levels)
        rules = strategy.Rules(min_history=1, lookback=5)
        params = strategy.Params(k_drive=math.inf, k_gap=math.inf, k_entry=1.5, cost_multiple=1.5, stop_mult=1.5)
        days = weekdays(DAY - timedelta(days=7), 3)
        # Two quiet history sessions with a small opening drive and gap make the day type known.
        streams = {"SPY": {days[0]: session(days[0], first_open=99.98),
                           days[1]: session(days[1], levels={0: 100.05}, first_open=100.02)},
                   "QQQ": {days[0]: session(days[0], first_open=99.98, symbol="QQQ"),
                           days[1]: session(days[1], levels={0: 100.05}, first_open=100.02, symbol="QQQ")}}
        streams["SPY"][DAY] = taken
        streams["QQQ"][DAY] = refused
        result = strategy.simulate(streams, params, rules=rules)
        self.assertTrue(result.day_types[("SPY", DAY)].known)
        self.assertEqual([(t.symbol, t.session) for t in result.trades], [("SPY", DAY)])
        self.assertEqual([(r.symbol, r.session, r.reason) for r in result.refusals],
                         [("QQQ", DAY, "through_target")])

    def test_entry_bar_must_start_before_15_00(self):
        events = {328: (100.00, 100.00, 99.50, 99.50), 329: (99.52, 99.61, 99.50, 99.60)}
        bars = session(events=events, levels={330: 99.6})
        accepted = one_trade(bars)
        self.assertIsInstance(accepted, strategy.Trade)
        self.assertEqual(accepted.entry_ts, ny_instant(DAY, time(14, 59)))
        missing = [bar for index, bar in enumerate(bars) if index != 329]  # next bar starts at 15:00
        refused = one_trade(missing)
        self.assertIsInstance(refused, strategy.Refusal)
        self.assertEqual(refused.reason, "entry_after_cutoff")


# --------------------------------------------------------------------------- exits


class ExitTests(unittest.TestCase):
    def test_levels_follow_the_frozen_vwap(self):
        events, levels = long_setup()
        bars = session(events=events, levels=levels)
        trade = one_trade(bars)
        target = strategy.vwap_series(bars)[40]
        self.assertEqual(trade.target, target)
        self.assertEqual(trade.side, strategy.LONG)
        self.assertAlmostEqual(trade.stop, 99.52 - 1.5 * abs(99.52 - target), places=12)

    def test_time_stop_after_60_minutes(self):
        events, levels = long_setup()
        bars = session(events=events, levels=levels)
        trade = one_trade(bars)
        self.assertEqual(trade.reason, "time")
        self.assertEqual(trade.exit_ts - trade.entry_ts, timedelta(minutes=60))
        self.assertEqual(trade.exit, bars[100].close)  # the bar ending 11:11

    def test_flat_by_the_bar_ending_15_50(self):
        events, levels = long_setup(320)
        bars = session(events=events, levels=levels)
        trade = one_trade(bars)
        self.assertEqual(trade.entry_ts, ny_instant(DAY, time(14, 51)))
        self.assertEqual(trade.reason, "flat")
        self.assertEqual(trade.exit_ts, ny_instant(DAY, time(15, 50)))
        self.assertEqual(trade.exit, bars[379].close)

    def test_stop_wins_a_same_bar_tie(self):
        events, levels = long_setup(**{"45": (99.60, 100.10, 98.70, 99.60)})
        trade = one_trade(session(events=events, levels=levels))
        self.assertLess(98.70, trade.stop)
        self.assertGreater(100.10, trade.target)
        self.assertEqual(trade.reason, "stop")
        self.assertEqual(trade.exit, trade.stop)
        self.assertLess(trade.gross_bps, 0)

    def test_target_hit_inside_a_bar_fills_at_the_target(self):
        events, levels = long_setup(**{"45": (99.60, 100.05, 99.55, 99.70)})
        trade = one_trade(session(events=events, levels=levels))
        self.assertEqual((trade.reason, trade.exit), ("target", trade.target))

    def test_a_bar_opening_beyond_a_level_fills_at_the_open(self):
        events, levels = long_setup(**{"45": (98.50, 98.60, 98.40, 98.55)})
        bars = session(events=events, levels=levels)
        trade = one_trade(bars)
        self.assertEqual((trade.reason, trade.exit, trade.exit_ts), ("stop_gap", 98.50, bars[45].ts))
        events, levels = long_setup(**{"45": (100.20, 100.30, 100.10, 100.25)})
        trade = one_trade(session(events=events, levels=levels))
        self.assertEqual((trade.reason, trade.exit), ("target_gap", 100.20))

    def test_short_side_mirror(self):
        events = {40: (100.00, 100.50, 100.00, 100.50), 41: (100.48, 100.50, 100.39, 100.40),
                  45: (101.50, 101.60, 101.40, 101.45)}
        bars = session(events=events, levels={42: 100.40})
        trade = one_trade(bars)
        self.assertEqual(trade.side, strategy.SHORT)
        self.assertAlmostEqual(trade.stop, 100.48 + 1.5 * abs(100.48 - trade.target), places=12)
        self.assertEqual((trade.reason, trade.exit), ("stop_gap", 101.50))
        self.assertAlmostEqual(trade.gross_bps, -(101.50 / 100.48 - 1) * 1e4)


# --------------------------------------------------------------------------- walk-forward


def fake_trade(day: date, net: float, symbol: str = SYMBOL) -> strategy.Trade:
    at = ny_instant(day, time(10, 30))
    return strategy.Trade(symbol, day, strategy.LONG, at, at, 100.0, at + timedelta(minutes=60), 100.0,
                          "time", net + 1.0, 1.0, net, 100.1, 99.9, -2.0)


class WalkForwardTests(unittest.TestCase):
    def setUp(self):
        self.sessions = weekdays(date(2026, 1, 5), 30)
        self.a, self.b = strategy.GRID[0], strategy.GRID[1]

    def test_windows_are_chronological_and_test_blocks_disjoint(self):
        windows = walkforward.rolling_windows(self.sessions, train=10, test=5)
        self.assertEqual(len(windows), 4)
        tested = [day for window in windows for day in window.test]
        self.assertEqual(tested, self.sessions[10:])  # every later session tested exactly once
        self.assertEqual(len(tested), len(set(tested)))
        for window in windows:
            self.assertLess(window.train[-1], window.test[0])
            self.assertEqual(list(window.train), sorted(window.train))
            self.assertEqual(list(window.test), sorted(window.test))
            self.assertFalse(set(window.train) & set(window.test))
        for earlier, later in zip(windows, windows[1:]):
            self.assertLess(earlier.test[-1], later.test[0])
        with self.assertRaises(ValueError):
            walkforward.rolling_windows(list(reversed(self.sessions)), train=10, test=5)
        overlapping = [walkforward.Window(0, tuple(self.sessions[:5]), tuple(self.sessions[5:10])),
                       walkforward.Window(1, tuple(self.sessions[3:8]), tuple(self.sessions[8:12]))]
        with self.assertRaises(ValueError):
            walkforward.check_windows(overlapping)

    def _trades(self, test_days=frozenset(), poison=False):
        out = {self.a: [], self.b: []}
        for index, day in enumerate(self.sessions):
            wobble = 1.0 if index % 2 else -1.0
            out[self.a].append(fake_trade(day, 2.0 + wobble))
            out[self.b].append(fake_trade(day, -0.5 + wobble))
            if poison and day in test_days:  # make the loser spectacular and the winner awful
                out[self.a][-1] = fake_trade(day, -1000.0 + wobble)
                out[self.b][-1] = fake_trade(day, 1000.0 + wobble)
        return out

    def test_selection_never_reads_test_sessions(self):
        windows = walkforward.rolling_windows(self.sessions, train=10, test=5)
        clean = self._trades()
        for window in windows:
            chosen = walkforward.select_params(clean, window, grid=(self.a, self.b), min_trades=3)
            self.assertEqual(chosen.params, self.a)
            poisoned = self._trades(frozenset(window.test), poison=True)
            again = walkforward.select_params(poisoned, window, grid=(self.a, self.b), min_trades=3)
            self.assertEqual((again.params, again.train_t, again.train_trades),
                             (chosen.params, chosen.train_t, chosen.train_trades))

    def test_out_of_sample_record_uses_only_test_sessions_of_the_chosen_params(self):
        trades = self._trades()
        wf = walkforward.walk_forward(trades, {}, self.sessions, grid=(self.a, self.b), train=10, test=5,
                                      min_trades=3)
        self.assertEqual(list(wf.test_sessions), self.sessions[10:])
        self.assertEqual([t.session for t in wf.trades], self.sessions[10:])
        chosen = {t.session: t for t in trades[self.a]}
        for trade in wf.trades:
            self.assertIs(trade, chosen[trade.session])

    def test_decision_publishes_the_unrounded_inputs_it_used(self):
        rule = walkforward.decision(0.0004, 500 / 10001, 0.0001)
        self.assertTrue(rule["checks"]["sign_flip_p_below_0_05"])
        self.assertEqual(rule["inputs"]["sign_flip_p_one_sided"], 500 / 10001)
        self.assertEqual(rule["inputs"]["session_mean"], 0.0004)

    def test_decision_rule_needs_all_three_checks(self):
        self.assertTrue(walkforward.decision(1.0, 0.01, 0.5)["deploy_to_paper"])
        self.assertFalse(walkforward.decision(1.0, 0.05, 0.5)["deploy_to_paper"])
        self.assertFalse(walkforward.decision(1.0, 0.01, -0.1)["deploy_to_paper"])
        self.assertFalse(walkforward.decision(-1.0, 0.01, 0.5)["deploy_to_paper"])
        self.assertFalse(walkforward.decision(None, None, None)["deploy_to_paper"])

    def test_sign_flip_p_is_small_for_consistently_positive_sessions(self):
        self.assertLess(walkforward.sign_flip_p([1.0] * 12, draws=2000), 0.01)
        self.assertGreater(walkforward.sign_flip_p([1.0, -1.0] * 6, draws=2000), 0.4)


# --------------------------------------------------------------------------- smoke report


class SmokeReportTests(unittest.TestCase):
    def test_too_few_sessions_gives_a_smoke_report_that_says_it_is_not_evidence(self):
        rng = random.Random(9)
        rows: dict[str, list[dict]] = {}
        for symbol in ("SPY", "QQQ"):
            price = 100.0
            for day in weekdays(date(2026, 6, 1), 3):
                for bar in walk_session(day, rng, price, symbol=symbol):
                    rows.setdefault(symbol, []).append({
                        "timestamp": bar.ts.astimezone(bar_io.NEW_YORK).isoformat(), "open": bar.open,
                        "high": bar.high, "low": bar.low, "close": bar.close, "volume": bar.volume})
                    price = bar.close
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "bars.json"
            data.write_text(json.dumps({"interval": "1m", "bars": rows}), encoding="utf-8")
            out = Path(folder) / "out"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run.main(["--data", str(data), "--format", "public", "--out", str(out),
                                           "--symbols", "SPY,QQQ", "--permutations", "50"]), 0)
            results = json.loads((out / "results.json").read_text(encoding="utf-8"))
            report = (out / "report.md").read_text(encoding="utf-8")
        self.assertEqual(results["mode"], "smoke")
        self.assertFalse(results["out_of_sample"])
        self.assertIn("NOT EVIDENCE", report.splitlines()[0] + report.splitlines()[2])
        self.assertEqual(results["hypothesis"], run.HYPOTHESIS)
        # A smoke run never carries a deploy verdict, whatever its checks read.
        self.assertFalse(results["decision"]["applicable"])
        self.assertFalse(results["decision"]["deploy_to_paper"])
        self.assertIn("mode", results["decision"]["design_deviations"])

    def test_alpaca_data_needs_a_calendar(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(SystemExit, "needs --calendar"):
                run.main(["--data", folder, "--format", "alpaca", "--out", str(Path(folder) / "out")])

    def test_a_calendar_that_misses_data_sessions_stops_the_run(self):
        days = weekdays(date(2026, 9, 14), 2)
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            with gzip.open(data / "SPY_1Min_2026-09.csv.gz", "wt", encoding="utf-8", newline="") as handle:
                handle.write(",".join(bar_io.ALPACA_CSV_COLUMNS) + "\n")
                for day in days:
                    for index in range(390):
                        stamp = start_of(day, index).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                        handle.write(f"SPY,{stamp},100,100.1,99.9,100,1000,10,100\n")
            (data / "calendar.json").write_text(json.dumps(
                [{"date": days[0].isoformat(), "open": "09:30", "close": "16:00"}]), encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "misses 1 data sessions"):
                run.main(["--data", str(data), "--format", "alpaca", "--symbols", "SPY",
                          "--calendar", str(data / "calendar.json"), "--out", str(data / "out")])

    def test_post_close_bars_of_an_early_close_are_not_regular_hours(self):
        day = date(2026, 11, 27)  # the day after Thanksgiving closes at 13:00
        bars = [make_bar(day, index, 100, 100.1, 99.9, 100) for index in range(390)]
        kept = bar_io.session_bars(bars, early_closes={day: time(13, 0)})[SYMBOL][day]
        self.assertEqual(len(kept), 210)
        self.assertLess(kept[-1].ts.astimezone(bar_io.NEW_YORK).time(), time(13, 0))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "calendar.json"
            path.write_text(json.dumps([{"date": "2026-11-27", "open": "09:30", "close": "13:00"},
                                        {"date": "2026-11-30", "open": "09:30", "close": "16:00"}]),
                            encoding="utf-8")
            early, days = bar_io.load_calendar(path)
        self.assertEqual(early, {day: time(13, 0)})
        self.assertEqual(days, frozenset({day, date(2026, 11, 30)}))

    def test_results_json_stays_under_200_kb(self):
        trade = fake_trade(DAY, 1.0).as_dict()
        text = run.serialise_results({"schema": run.SCHEMA, "trades": [trade] * 2000})
        self.assertLess(len(text.encode("utf-8")), 200_000)
        self.assertIn("omitted: 2000 trades", text)
        small = run.serialise_results({"schema": run.SCHEMA, "trades": [trade] * 3})
        self.assertEqual(json.loads(small)["trades"], [trade] * 3)


# --------------------------------------------------------------------------- SIP fetcher

API_KEY = "PKTESTKEYID0123456789"
API_SECRET = "sEcReT-value-that-must-never-leak-0123"


class FakeResponse:
    def __init__(self, status: int, payload=None, headers=None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class FakeSession:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, *, params, headers, timeout, allow_redirects=True):
        self.calls.append({"url": url, "params": dict(params), "headers": dict(headers), "timeout": timeout,
                           "allow_redirects": allow_redirects})
        return self.responses.pop(0)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def alpaca_bar(stamp: str, price: float = 100.0) -> dict:
    return {"t": stamp, "o": price, "h": price + 0.05, "l": price - 0.05, "c": price + 0.01, "v": 1200,
            "n": 31, "vw": price + 0.004}


def client_for(session: FakeSession, clock: FakeClock, **kwargs) -> fetch_sip.AlpacaClient:
    return fetch_sip.AlpacaClient(session, key_id=API_KEY, secret_key=API_SECRET,
                                  pacer=fetch_sip.Pacer(clock=clock, sleep=clock.sleep), sleep=clock.sleep,
                                  **kwargs)


class FetcherTests(unittest.TestCase):
    START = datetime(2026, 9, 1, 4, 0, tzinfo=timezone.utc)
    END = datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc)

    def assert_keys_absent(self, text: str) -> None:
        self.assertNotIn(API_KEY, text)
        self.assertNotIn(API_SECRET, text)

    def test_pagination_and_429_retry_with_keys_only_in_headers(self):
        pages = [FakeResponse(429, headers={"Retry-After": "3"}),
                 FakeResponse(200, {"bars": {"SPY": [alpaca_bar("2026-09-15T13:30:00Z")],
                                             "QQQ": [alpaca_bar("2026-09-15T13:30:00Z", 600.0)]},
                                    "next_page_token": "page-2"}),
                 FakeResponse(200, {"bars": {"SPY": [alpaca_bar("2026-09-15T13:31:00Z")]},
                                    "next_page_token": None})]
        session, clock = FakeSession(pages), FakeClock()
        client = client_for(session, clock)
        out = fetch_sip.fetch_bars(client, ["SPY", "QQQ"], timeframe="1Min", start=self.START, end=self.END)
        self.assertEqual([bar["t"] for bar in out["SPY"]], ["2026-09-15T13:30:00Z", "2026-09-15T13:31:00Z"])
        self.assertEqual(len(out["QQQ"]), 1)
        self.assertEqual(len(session.calls), 3)
        first, retry, second = (call["params"] for call in session.calls)
        self.assertEqual(first, retry)  # the 429 is retried with the same request
        self.assertNotIn("page_token", first)
        self.assertEqual(second["page_token"], "page-2")
        self.assertEqual({k: first[k] for k in ("feed", "adjustment", "limit", "timeframe")},
                         {"feed": "sip", "adjustment": "raw", "limit": 10_000, "timeframe": "1Min"})
        self.assertIn(3.0, clock.sleeps)  # Retry-After honoured
        for call in session.calls:
            self.assertEqual(call["headers"], {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": API_SECRET})
            self.assert_keys_absent(call["url"] + json.dumps(call["params"]))
        self.assert_keys_absent(repr(client))

    def test_429_backs_off_exponentially_then_gives_up_without_leaking_keys(self):
        session, clock = FakeSession([FakeResponse(429) for _ in range(4)]), FakeClock()
        client = client_for(session, clock, max_retries=3)
        with self.assertRaises(fetch_sip.FetchError) as caught:
            client.get_json(fetch_sip.BARS_URL, {"symbols": "SPY"})
        self.assertEqual(len(session.calls), 4)
        self.assertEqual([s for s in clock.sleeps if s >= 1.0], [1.0, 2.0, 4.0])
        self.assert_keys_absent(str(caught.exception))

    def test_error_bodies_that_echo_a_key_are_redacted(self):
        body = f'{{"message": "forbidden", "echo": "{API_KEY} {API_SECRET}"}}'
        session, clock = FakeSession([FakeResponse(403, text=body)]), FakeClock()
        with self.assertRaises(fetch_sip.FetchError) as caught:
            client_for(session, clock).get_json(fetch_sip.BARS_URL, {"symbols": "SPY"})
        self.assertIn("403", str(caught.exception))
        self.assert_keys_absent(str(caught.exception))

    def test_keys_go_only_to_https_alpaca_hosts_and_never_follow_a_redirect(self):
        for url in ("http://data.alpaca.markets/v2/stocks/bars", "https://example.com/v2/calendar",
                    "https://data.alpaca.markets.evil.example/v2/stocks/bars"):
            session = FakeSession([])
            with self.assertRaisesRegex(fetch_sip.FetchError, "only HTTPS Alpaca hosts"):
                client_for(session, FakeClock()).get_json(url, {})
            self.assertEqual(session.calls, [])
        session = FakeSession([FakeResponse(302, headers={"Location": "https://example.com/"})])
        with self.assertRaisesRegex(fetch_sip.FetchError, "redirect .* refused") as caught:
            client_for(session, FakeClock()).get_json(fetch_sip.CALENDAR_URL, {})
        self.assertFalse(session.calls[0]["allow_redirects"])
        self.assertNotIn(API_KEY, str(caught.exception))
        self.assertNotIn(API_SECRET, str(caught.exception))

    def test_repeated_page_token_is_an_error(self):
        page = {"bars": {"SPY": []}, "next_page_token": "same"}
        session, clock = FakeSession([FakeResponse(200, page), FakeResponse(200, page)]), FakeClock()
        with self.assertRaises(fetch_sip.FetchError):
            fetch_sip.fetch_bars(client_for(session, clock), ["SPY"], timeframe="1Min", start=self.START,
                                 end=self.END)

    def test_main_writes_month_files_and_never_writes_or_logs_the_keys(self):
        responses = [FakeResponse(429),
                     FakeResponse(200, {"bars": {"SPY": [alpaca_bar("2026-09-15T13:30:00Z")]},
                                        "next_page_token": "p2"}),
                     FakeResponse(200, {"bars": {"SPY": [alpaca_bar("2026-09-15T13:31:00Z")]},
                                        "next_page_token": None}),
                     FakeResponse(200, [{"date": "2026-09-15", "open": "09:30", "close": "16:00"}])]
        session, clock = FakeSession(responses), FakeClock()
        records: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler, root = Capture(level=logging.DEBUG), logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                out = Path(folder) / "sip"
                code = fetch_sip.main(["--out", str(out), "--start", "2026-09-01", "--end", "2026-09-30",
                                       "--symbols", "SPY", "--calendar"], session=session,
                                      environ={"ALPACA_API_KEY": API_KEY, "ALPACA_SECRET_KEY": API_SECRET},
                                      now=datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc),
                                      sleep=clock.sleep, clock=clock)
                written = sorted(path.name for path in out.iterdir())
                blobs = [path.read_bytes() for path in out.rglob("*") if path.is_file()]
                blobs += [gzip.decompress(path.read_bytes()) for path in out.glob("*.gz")]
                loaded = bar_io.load_alpaca_csv(out / "SPY_1Min_2026-09.csv.gz")
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        self.assertEqual(code, 0)
        self.assertEqual(written, ["SPY_1Min_2026-09.csv.gz", "calendar.json"])
        self.assertEqual([bar.ts for bar in loaded], [datetime(2026, 9, 15, 13, 30, tzinfo=timezone.utc),
                                                     datetime(2026, 9, 15, 13, 31, tzinfo=timezone.utc)])
        for blob in blobs:
            self.assertNotIn(API_KEY.encode(), blob)
            self.assertNotIn(API_SECRET.encode(), blob)
        self.assert_keys_absent(stdout.getvalue() + stderr.getvalue() + "\n".join(records))
        self.assertIn("1 final", stderr.getvalue())
        # The calendar call carries the keys in headers too, never in the query.
        self.assertEqual(session.calls[-1]["url"], fetch_sip.CALENDAR_URL)
        for call in session.calls:
            self.assert_keys_absent(call["url"] + json.dumps(call["params"]))

    def test_missing_keys_stop_before_any_request(self):
        session = FakeSession([])
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stderr(stderr):
            code = fetch_sip.main(["--out", str(Path(folder) / "sip"), "--start", "2026-09-01",
                                   "--end", "2026-09-30"], session=session, environ={"ALPACA_API_KEY": API_KEY})
        self.assertEqual(code, 2)
        self.assertEqual(session.calls, [])
        self.assert_keys_absent(stderr.getvalue())

    def test_months_end_16_minutes_before_now_and_the_current_month_is_partial(self):
        now = datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc)
        plans = fetch_sip.plan_months(date(2026, 9, 1), date(2026, 10, 31), now=now)
        self.assertEqual([(p.label, p.final) for p in plans], [("2026-09", True), ("2026-10", False)])
        self.assertEqual(plans[-1].fetch_end, now - timedelta(minutes=16))


if __name__ == "__main__":
    unittest.main()
