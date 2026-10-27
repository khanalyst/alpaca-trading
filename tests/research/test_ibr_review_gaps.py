"""Regression tests for the IBR review findings A-06, A-07, A-10, A-16, A-17.

Each test fails on the revision before the fixes (commit 352fb70) and passes
after them.  New names are imported inside the tests so each one reports its
own failure on the old revision.  ``test_ibr_golden_equivalence`` separately
proves that the registered arms are unchanged.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import unittest
from zoneinfo import ZoneInfo

from agent.contracts.ibr import generate_ibr_signal
from research.costs import CostModel, ReplayPolicy
from research.edge_discovery_core import _effective_ibr_config, _opportunity_rows
from research.ibr import IBRConfig, replay_ibr
from research.market_data import normalize_quote, normalize_underlying_bar
from tests.research import ibr_golden_support as golden


NEW_YORK = ZoneInfo("America/New_York")
UTC = timezone.utc
FREE = CostModel(spread_bps=0, slippage_bps=0, fee_bps=0,
                 option_fee_per_contract_side=0)
BAR_FALLBACK = ReplayPolicy(strict_market_data=False)
SESSION = date(2026, 3, 10)
# Five prior sessions on either side of the 2026-03-08 daylight-saving change.
PRIOR_SESSIONS = (date(2026, 3, 3), date(2026, 3, 4), date(2026, 3, 5),
                  date(2026, 3, 6), date(2026, 3, 9))


def bar(day: date, minute: int, *, close: float, high: float | None = None,
        low: float | None = None, opened: float | None = None,
        volume: float = 1_000.0, symbol: str = "SPY") -> dict:
    """One completed IEX minute, ``minute`` minutes after the 09:30 open."""
    stamp = datetime.combine(day, time(9, 30), tzinfo=NEW_YORK) + timedelta(
        minutes=minute)
    ended = stamp + timedelta(minutes=1)
    opened = close if opened is None else opened
    return {
        "symbol": symbol, "timestamp": stamp.astimezone(UTC).isoformat(),
        "open": opened, "high": max(high if high is not None else close,
                                    opened, close),
        "low": min(low if low is not None else close, opened, close),
        "close": close, "volume": volume, "provider": "alpaca",
        "feed": "iex", "source_mode": "forward_observed",
        "as_of": ended.astimezone(UTC).isoformat(),
        "observed_at": ended.astimezone(UTC).isoformat(),
        "session_date": day.isoformat(),
    }


def opening_session(day: date, *, breakout_minute: int = 20,
                    breakout_volume: float = 5_000.0,
                    opening_volume: float = 1_000.0,
                    after: list[tuple[int, float]] | None = None,
                    last_minute: int = 385, skip: tuple[int, ...] = (),
                    breakout: bool = True) -> list[dict]:
    """A 15-minute 99.50-100.50 range, then a close-confirmed long break.

    The break closes at 100.80, so the range stop is 99.50 (1.30 away) and
    a 2R target is 103.40.  ``after`` sets ``(minute, price)`` levels from
    which the session continues flat.
    """
    rows = []
    level = 100.0
    levels = dict(after or ())
    for minute in range(last_minute + 1):
        if minute in skip:
            continue
        if minute < 15:
            high = 100.5 if minute == 0 else 100.2
            low = 99.5 if minute == 1 else 99.8
            rows.append(bar(day, minute, close=100.0, high=high, low=low,
                            volume=opening_volume))
            continue
        if minute < breakout_minute or not breakout:
            rows.append(bar(day, minute, close=100.0, high=100.2, low=99.8,
                            volume=200.0))
            continue
        if minute == breakout_minute:
            level = 100.8
            rows.append(bar(day, minute, opened=100.0, close=level,
                            high=100.85, low=99.95, volume=breakout_volume))
            continue
        level = levels.get(minute, level)
        rows.append(bar(day, minute, opened=level, close=level,
                        high=level + 0.05, low=level - 0.05, volume=200.0))
    return rows


def underlying(rows: list[dict]):
    """Replay bars without recorder observation stamps.

    Like the legacy bar-fallback fixtures in ``test_ibr``, these carry no
    ``as_of``/``observed_at``, so the entry bar's open is usable without a
    quote; the contract tests keep the stamps.
    """
    return [normalize_underlying_bar({
        key: value for key, value in row.items()
        if key not in {"as_of", "observed_at"}}) for row in rows]


def replay_config(**overrides) -> IBRConfig:
    values = dict(range_minutes=15, close_confirmed=True, range_stop=True,
                  target_r=2.0, breakout_buffer_bps=5.0, costs=FREE,
                  policy=BAR_FALLBACK)
    values.update(overrides)
    return IBRConfig(**values)


class A06DefaultsTests(unittest.TestCase):
    """research IBR defaults versus the runtime contract (A-06)."""

    def test_mapper_takes_contract_defaults_for_absent_admission_keys(self):
        # The strategy block omits every admission filter.  The old mapper
        # substituted permissive values (no spread cap, no relative-volume
        # floor, no staleness limit); the runtime contract uses 25 bps, 1.0
        # and 30 seconds.
        cfg, effective = _effective_ibr_config(
            {"strategy": {"range_minutes": 15}}, {}, close_confirmed=True)
        self.assertEqual(cfg.max_spread_bps, 25.0)
        self.assertEqual(cfg.min_relative_volume, 1.0)
        self.assertEqual(cfg.stale_minutes, 0.5)
        self.assertEqual(cfg.max_ibr_width_pct, float("inf"))
        self.assertEqual(cfg.relative_volume_mode, "opening_range_mean")
        # The persisted effective config (candidate identity) is unchanged.
        self.assertNotIn("max_spread_bps", effective["strategy"])

    def test_mapper_keeps_every_supplied_value(self):
        strategy = {"range_minutes": 15, "min_relative_volume": 0.0,
                    "max_spread_bps": 1_000, "stale_minutes": 5,
                    "atr_period": 7, "max_ibr_width_pct": 2}
        cfg, _ = _effective_ibr_config({"strategy": strategy}, {})
        self.assertEqual((cfg.min_relative_volume, cfg.max_spread_bps,
                          cfg.stale_minutes, cfg.atr_period,
                          cfg.max_ibr_width_pct),
                         (0.0, 1_000.0, 5.0, 7, 2.0))

    def test_partial_config_replay_now_applies_the_runtime_spread_cap(self):
        rows = opening_session(SESSION)
        quotes = []
        for row in rows:
            minute = int((datetime.fromisoformat(row["timestamp"]) -
                          datetime.fromisoformat(rows[0]["timestamp"])
                          ).total_seconds() // 60)
            half = 0.20 if minute == 21 else 0.01  # 40 bps at the break
            quotes.append(normalize_quote({
                "symbol": "SPY", "timestamp": row["timestamp"],
                "bid": round(row["open"] - half, 2),
                "ask": round(row["open"] + half, 2),
                "provider": "alpaca", "feed": "iex",
                "source_mode": "forward_observed"}))
        cfg, _ = _effective_ibr_config(
            {"strategy": {"range_minutes": 15}}, {}, close_confirmed=True,
            policy=BAR_FALLBACK)
        result = replay_ibr(underlying(rows), config=cfg, quotes=quotes)
        self.assertEqual(result.trades, [])
        self.assertEqual(result.refusals[0].detail.get("spread_above_limit"), 1)

    def test_registered_arms_supply_every_key_whose_default_changed(self):
        from research.ibr import runtime_admission_fields
        for arm in golden.registered_arms():
            strategy = arm["config"]["strategy"]
            with self.subTest(variant=arm["variant_id"]):
                for key in ("min_relative_volume", "stale_minutes",
                            "max_spread_bps", "min_ibr_width_atr",
                            "max_ibr_width_atr", "atr_period",
                            "max_entry_extension_r"):
                    self.assertIn(key, strategy)
                resolved = runtime_admission_fields(strategy)
                self.assertEqual(resolved["max_spread_bps"],
                                 float(strategy["max_spread_bps"]))
                self.assertEqual(resolved["max_ibr_width_pct"], float("inf"))
                self.assertEqual(resolved["relative_volume_mode"],
                                 "opening_range_mean")

    def test_runtime_contract_config_matches_the_contract(self):
        cfg = IBRConfig.from_runtime_contract()
        self.assertEqual(
            (cfg.range_minutes, cfg.breakout_buffer_bps, cfg.target_r,
             cfg.close_confirmed, cfg.range_stop, cfg.min_relative_volume,
             cfg.max_spread_bps, cfg.stale_minutes),
            (15, 5.0, 2.0, True, True, 1.0, 25.0, 0.5))
        shipped = IBRConfig.from_runtime_contract(golden.shipped_config())
        self.assertEqual((shipped.max_entry_extension_r,
                          shipped.min_ibr_width_atr, shipped.max_ibr_width_atr),
                         (1.0, 0.25, 3.0))

    def test_legacy_defaults_are_named_and_unchanged(self):
        from research.ibr import LEGACY_REPLAY_DEFAULTS
        legacy = IBRConfig.legacy()
        self.assertEqual(legacy, IBRConfig())
        for name, value in LEGACY_REPLAY_DEFAULTS.items():
            self.assertEqual(getattr(legacy, name), value, name)
        self.assertEqual((legacy.range_minutes, legacy.close_confirmed,
                          legacy.range_stop, legacy.max_spread_bps),
                         (30, False, False, float("inf")))


class A07HoldGapTests(unittest.TestCase):
    """A hold cut short by missing data (A-07)."""

    def test_internal_gap_exit_is_a_data_discontinuity(self):
        rows = opening_session(SESSION, skip=(26, 27))
        result = replay_ibr(underlying(rows), config=replay_config())
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        # The trade is still resolved on the last bar before the gap ...
        self.assertEqual(trade.exit_reason, "force_flat")
        self.assertEqual(trade.exit_timestamp.astimezone(NEW_YORK).time(),
                         time(9, 55))
        # ... but its durable cause is the gap, not the session flatten.
        self.assertEqual(trade.canonical_exit_reason, "data_discontinuity")
        self.assertEqual(result.summary()["canonical_exit_reasons"],
                         {"data_discontinuity": 1})
        row = _opportunity_rows(result, underlying(rows), "equity")[0]
        self.assertEqual(row["canonical_exit_reason"], "data_discontinuity")

    def test_observed_data_end_before_the_flatten_is_a_discontinuity(self):
        rows = opening_session(SESSION, last_minute=200)
        trade = replay_ibr(underlying(rows), config=replay_config()).trades[0]
        self.assertEqual(trade.exit_reason, "force_flat")
        self.assertEqual(trade.canonical_exit_reason, "data_discontinuity")

    def test_a_hold_that_reaches_the_flatten_keeps_session_force_flat(self):
        rows = opening_session(SESSION)
        trade = replay_ibr(underlying(rows), config=replay_config()).trades[0]
        self.assertEqual(trade.exit_timestamp.astimezone(NEW_YORK).time(),
                         time(15, 55))
        self.assertEqual(trade.canonical_exit_reason, "session_force_flat")

    def test_holding_across_gaps_is_opt_in(self):
        # Two missing minutes, then the next observed bar opens at 104.00,
        # through the 103.40 target.
        rows = opening_session(SESSION, skip=(26, 27), after=[(28, 104.0)])
        default = replay_ibr(underlying(rows), config=replay_config())
        self.assertEqual(default.trades[0].exit_reason, "force_flat")
        self.assertEqual(default.trades[0].exit_reference, 100.8)
        carried = replay_ibr(underlying(rows),
                             config=replay_config(hold_across_gaps=True))
        trade = carried.trades[0]
        self.assertEqual(trade.exit_reason, "target")
        self.assertTrue(trade.gap_fill)
        self.assertEqual(trade.exit_reference, 104.0)
        self.assertEqual(trade.exit_timestamp.astimezone(NEW_YORK).time(),
                         time(9, 58))
        # Without a gap through a level the carried hold reaches the flatten.
        flat = opening_session(SESSION, skip=(26, 27))
        carried_flat = replay_ibr(underlying(flat),
                                  config=replay_config(hold_across_gaps=True))
        self.assertEqual(carried_flat.trades[0].canonical_exit_reason,
                         "session_force_flat")


def same_clock_corpus(*, current_volume: float = 3_000.0,
                      prior_volume: float = 1_000.0,
                      missing_prior_minute: bool = False) -> tuple[list, list]:
    """Five prior sessions without a break and a current one with a heavy open.

    The current opening range trades 10,000 shares a minute and every prior
    post-range minute ``prior_volume``, so a 3,000-share breakout has an
    opening-range relative volume of 0.3 and a same-clock one of 3.0.
    """
    prior = []
    for index, day in enumerate(PRIOR_SESSIONS):
        rows = opening_session(day, breakout=False, last_minute=30)
        for minute, row in enumerate(rows):
            if missing_prior_minute and index == 2 and minute == 20:
                continue
            if minute >= 15:
                row = {**row, "volume": prior_volume}
            prior.append(row)
    current = opening_session(SESSION, breakout_volume=current_volume,
                              opening_volume=10_000.0, last_minute=60)
    return prior, current


SAME_CLOCK = {"range_minutes": 15, "breakout_buffer_bps": 5.0,
              "min_relative_volume": 1.0, "latest_entry_time": "15:00",
              "relative_volume_mode": "same_clock"}


class A10SameClockRelativeVolumeTests(unittest.TestCase):
    """A time-of-day adjusted relative-volume baseline (A-10)."""

    def test_same_clock_ratio_uses_the_exact_prior_sessions(self):
        from agent.contracts.ibr import same_clock_relative_volume
        prior, current = same_clock_corpus()
        candidate = current[20]
        self.assertEqual(
            same_clock_relative_volume(candidate, prior, sessions=5), 3.0)
        # One prior session without that minute fails closed.
        gappy, _ = same_clock_corpus(missing_prior_minute=True)
        self.assertIsNone(
            same_clock_relative_volume(candidate, gappy, sessions=5))
        # Fewer prior sessions than configured fails closed.
        self.assertIsNone(
            same_clock_relative_volume(candidate, prior, sessions=6))

    def test_contract_same_clock_mode_is_opt_in(self):
        prior, current = same_clock_corpus()
        default = generate_ibr_signal(
            "SPY", current, config={**SAME_CLOCK,
                                    "relative_volume_mode":
                                        "opening_range_mean"})
        self.assertIsNone(default, "opening-range RVOL is 0.3")
        signal = generate_ibr_signal("SPY", current, config=SAME_CLOCK,
                                     prior_session_bars=prior)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["relative_volume"], 3.0)
        self.assertEqual(signal["relative_volume_mode"], "same_clock")
        self.assertEqual(signal["relative_volume_sessions"], 5)
        # Without prior sessions the same-clock baseline is unavailable.
        self.assertIsNone(generate_ibr_signal("SPY", current,
                                              config=SAME_CLOCK))

    def test_contract_same_clock_rejects_a_thin_same_clock_bar(self):
        prior, current = same_clock_corpus(current_volume=800.0,
                                           prior_volume=1_000.0)
        self.assertIsNone(generate_ibr_signal(
            "SPY", current, config=SAME_CLOCK, prior_session_bars=prior))

    def test_unknown_relative_volume_mode_is_refused(self):
        _prior, current = same_clock_corpus()
        with self.assertRaises(ValueError):
            generate_ibr_signal("SPY", current, config={
                **SAME_CLOCK, "relative_volume_mode": "same-clock-ish"})

    def test_replay_same_clock_matches_the_contract(self):
        prior, current = same_clock_corpus()
        bars = underlying(prior + current)
        default = replay_ibr(bars, config=replay_config(
            min_relative_volume=1.0))
        self.assertEqual(default.trades, [])
        self.assertIn("relative_volume_below_limit",
                      [refusal.reason for refusal in default.refusals])
        same_clock = replay_ibr(bars, config=replay_config(
            min_relative_volume=1.0, relative_volume_mode="same_clock"))
        self.assertEqual(len(same_clock.trades), 1)
        trade = same_clock.trades[0]
        signal = generate_ibr_signal("SPY", current, config=SAME_CLOCK,
                                     prior_session_bars=prior)
        self.assertEqual(trade.session_date, SESSION)
        self.assertEqual(trade.signal_timestamp.timestamp(),
                         signal["signal_ts"])
        # The prior sessions themselves lack enough history: fail closed.
        self.assertTrue(all(
            refusal.reason in {"no_breakout",
                               "same_clock_relative_volume_unavailable"}
            for refusal in same_clock.refusals))


class A16SignalTimestampTests(unittest.TestCase):
    def test_ibr_signal_names_its_bar_open_and_close(self):
        rows = opening_session(SESSION, last_minute=40)
        signal = generate_ibr_signal("SPY", rows, config={
            "range_minutes": 15, "min_relative_volume": 1.0})
        breakout_open = datetime.fromisoformat(rows[20]["timestamp"])
        self.assertEqual(signal["signal_bar_open_ts"],
                         breakout_open.timestamp())
        self.assertEqual(signal["signal_bar_close_ts"],
                         breakout_open.timestamp() + 60.0)
        # ``signal_ts`` keeps its meaning: the IBR signal bar's close.
        self.assertEqual(signal["signal_ts"], signal["signal_bar_close_ts"])


# Rows with binary-exact prices, so every true range is exact:
# TR = 1.0, 1.5, 0.5, 1.5, 1.0.
ATR_ROWS = [
    {"high": 10.0, "low": 9.0, "close": 9.5},
    {"high": 11.0, "low": 9.75, "close": 10.5},
    {"high": 10.75, "low": 10.25, "close": 10.5},
    {"high": 12.0, "low": 10.5, "close": 11.75},
    {"high": 12.0, "low": 11.0, "close": 11.25},
]
WILDER_3 = [None, None, (1.0 + 1.5 + 0.5) / 3, (1.0 * 2 + 1.5) / 3,
            (((1.0 * 2 + 1.5) / 3) * 2 + 1.0) / 3]


class A17ATRDefinitionTests(unittest.TestCase):
    """Each ATR definition is named, documented and pinned (A-17)."""

    def test_ibr_contract_atr_is_named_wilder(self):
        from agent.contracts import ibr
        self.assertIs(ibr.atr_series, ibr.wilder_atr_series)
        self.assertEqual(ibr.wilder_atr_series(ATR_ROWS, period=3), WILDER_3)
        self.assertIn("generate_ibr_signal", ibr.wilder_atr_series.__doc__)

    def test_market_atr_is_the_same_wilder_definition_in_decimal(self):
        from agent import market
        from agent.contracts.ibr import wilder_atr_series
        self.assertIs(market.atr_series, market.wilder_atr_decimal_series)
        values = market.wilder_atr_decimal_series(ATR_ROWS, period=3)
        self.assertEqual(values, [None if value is None else
                                  Decimal(str(value)) for value in WILDER_3])
        # Bit-identical to the contract's floats on mapping input.
        self.assertEqual([None if value is None else float(value)
                          for value in values],
                         wilder_atr_series(ATR_ROWS, period=3))
        self.assertIn("attach_atr", market.wilder_atr_decimal_series.__doc__)

    def test_rule_atr_is_a_simple_mean_and_differs_from_wilder(self):
        from agent.contracts.rule import _atr
        # The mean of the last three true ranges (each against its prior
        # close): 0.5, 1.5 and 1.0.
        self.assertEqual(_atr(ATR_ROWS, 3), 1.0)
        self.assertNotEqual(_atr(ATR_ROWS, 3), WILDER_3[-1])
        self.assertIsNone(_atr(ATR_ROWS[:3], 3))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
