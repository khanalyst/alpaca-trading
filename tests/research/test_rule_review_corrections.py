"""Opt-in corrections to the rule signal contracts (October 2026 review).

Each class covers one finding of docs/review-2026-10-15/reviews/
handoff-A_signals.md.  Every test here fails on the code before the fixes and
passes after them; tests.research.test_rule_golden_equivalence separately
proves that no registered arm changed.
"""

from datetime import date, datetime, time, timedelta, timezone
import random
import unittest
from zoneinfo import ZoneInfo

from agent.contracts.rule import (
    RULE_SCHEMA_V4, RULE_SCHEMA_V6, RULE_SCHEMA_V7, RuleSpecError,
    THESIS_DEADLINE_BEFORE_ENTRY_WINDOW, THESIS_DEADLINE_INSIDE_ENTRY_HORIZON,
    evaluate_rule_signal, evaluate_rule_signal_metadata,
    evaluate_rule_signal_trace, exit_deadline, feature_window_bars,
    hold_deadline, rule_semantic_distance, rule_semantic_signature,
    rule_spec_hash, rule_variant_id, rule_vehicle_executable,
    thesis_deadline_conflict, thesis_exit_deadline, validate_rule_spec,
)
from tests.research import rule_golden_harness as harness


NY = ZoneInfo("America/New_York")


def _ny(day: date, hour: int, minute: int) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=NY)


def _session(closes, *, day=date(2026, 8, 10), symbol="SPY", wick=.01,
             volumes=None, start=0):
    """Completed one-minute bars from 09:30 (+ *start* minutes)."""
    rows = []
    for index, close in enumerate(closes):
        opened = closes[index - 1] if index else close
        rows.append({
            "timestamp": _ny(day, 9, 30) + timedelta(minutes=start + index),
            "symbol": symbol, "feed": "iex",
            "open": opened, "high": max(opened, close) + wick,
            "low": min(opened, close) - wick, "close": close,
            "volume": 1_000.0 if volumes is None else float(volumes[index]),
        })
    return rows


def _fired(rows, spec, **kwargs):
    """Indices of every prefix on which *spec* emits a signal."""
    return [index for index in range(len(rows))
            if evaluate_rule_signal(rows[:index + 1], spec, **kwargs)]


class CorrectionGrammarIdentityTests(unittest.TestCase):
    """Corrections are opt-in: absent or neutral leaves identity unchanged."""

    BASE = {"schema": RULE_SCHEMA_V7, "family": "opening_range_breakout",
            "range_minutes": 15, "threshold_bps": 5.0}

    def test_absent_explicit_neutral_and_null_corrections_share_identity(self):
        frozen = validate_rule_spec(self.BASE)
        for neutral in ({"signal_event": "state"}, {"stop_mode": "floor_atr"},
                        {"signal_event": None, "stop_mode": None}):
            with self.subTest(neutral=neutral):
                spec = validate_rule_spec({**self.BASE, **neutral})
                self.assertEqual(spec, frozen)
                self.assertEqual(rule_spec_hash(spec), rule_spec_hash(frozen))
                self.assertEqual(rule_variant_id(spec), rule_variant_id(frozen))

    def test_a_correction_is_a_new_non_executable_identity(self):
        frozen = validate_rule_spec(self.BASE)
        corrected = validate_rule_spec({**self.BASE, "signal_event": "first_cross"})
        self.assertNotEqual(rule_variant_id(corrected), rule_variant_id(frozen))
        self.assertNotEqual(rule_semantic_signature(corrected),
                            rule_semantic_signature(frozen))
        self.assertGreater(rule_semantic_distance(frozen, corrected), 0.0)
        self.assertFalse(rule_vehicle_executable(corrected, "equity"))

    def test_corrections_are_v7_only_and_bounded(self):
        with self.assertRaisesRegex(RuleSpecError, "review correction fields"):
            validate_rule_spec({**self.BASE, "schema": RULE_SCHEMA_V6,
                                "signal_event": "first_cross"})
        for bad in ({"signal_event": "sometimes"}, {"stop_mode": "structure"},
                    {"residual_mode": "beta"}, {"range_multiplier": 2.0},
                    {"residual_beta_sessions": 10},
                    {"volume_confirmation_mode": "same_clock"}):
            with self.subTest(bad=bad), self.assertRaises(RuleSpecError):
                validate_rule_spec({**self.BASE, **bad})


class FirstCrossEventTests(unittest.TestCase):
    """A-04: breakout predicates can fire once per crossing, not per bar."""

    SPEC = {"schema": RULE_SCHEMA_V7, "family": "opening_range_breakout",
            "range_minutes": 5, "threshold_bps": 5.0, "atr_period": 3}

    def setUp(self):
        # Five range minutes, five closes above it, a return inside, a recross.
        self.rows = _session([100.0, 100.05, 99.98, 100.02, 100.0,
                              100.2, 100.25, 100.3, 100.28, 100.32,
                              100.05, 100.02, 100.25, 100.3])

    def test_state_predicate_fires_on_every_bar_after_the_crossing(self):
        self.assertEqual(_fired(self.rows, validate_rule_spec(self.SPEC)),
                         [5, 6, 7, 8, 9, 12, 13])

    def test_first_cross_fires_only_when_the_predicate_turns_true(self):
        spec = validate_rule_spec({**self.SPEC, "signal_event": "first_cross"})
        self.assertEqual(_fired(self.rows, spec), [5, 12])
        trace = evaluate_rule_signal_trace(self.rows[:7], spec)
        self.assertEqual(trace["stages"][-1],
                         {"stage": "signal_event", "tested": True,
                          "passed": False, "reason": "predicate_already_held"})
        self.assertEqual(evaluate_rule_signal(self.rows[:6], spec)["signal_event"],
                         "first_cross")

    def test_bounded_window_contract_includes_the_previous_bar(self):
        state = validate_rule_spec({
            "schema": RULE_SCHEMA_V7, "family": "momentum_continuation",
            "lookback": 3, "slow_lookback": 5, "atr_period": 3,
            "threshold_bps": 0.0})
        event = validate_rule_spec({**state, "signal_event": "first_cross"})
        self.assertEqual(feature_window_bars(event),
                         feature_window_bars(state) + 1)
        rows = _session([100, 100.1, 100.05, 100.2, 100.3, 100.25, 100.4,
                         100.5, 100.6, 100.55, 100.7])
        window = feature_window_bars(event)
        for index in range(window, len(rows)):
            with self.subTest(index=index):
                self.assertEqual(
                    evaluate_rule_signal(rows[:index + 1], event),
                    evaluate_rule_signal(rows[index + 1 - window:index + 1], event))


class HorizonVolatilityStopTests(unittest.TestCase):
    """A-02: a per-symbol, horizon-scaled ATR stop instead of an inert multiple."""

    SPEC = {"schema": RULE_SCHEMA_V7, "family": "momentum_continuation",
            "lookback": 3, "slow_lookback": 5, "atr_period": 5,
            "threshold_bps": 0.0, "max_hold_bars": 25}

    def setUp(self):
        closes = [100 + index * .01 for index in range(12)]
        self.quiet = _session(closes, wick=.01)
        self.volatile = _session(closes, wick=.10, symbol="SMH")

    def _stop_bps(self, rows, spec):
        signal = evaluate_rule_signal(rows, validate_rule_spec(spec))
        return signal["stop_distance"] / signal["entry_price"] * 10_000

    def test_frozen_floor_makes_the_atr_multiple_inert(self):
        for rows in (self.quiet, self.volatile):
            for multiple in (1.0, 1.4):
                with self.subTest(symbol=rows[0]["symbol"], multiple=multiple):
                    self.assertAlmostEqual(self._stop_bps(
                        rows, {**self.SPEC, "stop_atr": multiple}), 30.0)

    def test_horizon_atr_scales_by_symbol_and_multiple_above_the_floor(self):
        spec = {**self.SPEC, "stop_mode": "horizon_atr"}
        # The quiet symbol still sits on the cost floor ...
        self.assertAlmostEqual(self._stop_bps(self.quiet, spec), 30.0)
        quiet = evaluate_rule_signal_metadata(
            self.quiet, validate_rule_spec(spec))
        self.assertTrue(quiet["stop_floor_binding"])
        self.assertTrue(quiet["floor_binding"])
        # ... while the volatile one carries ATR x sqrt(25) and a live multiple.
        signal = evaluate_rule_signal(self.volatile, validate_rule_spec(spec))
        atr = evaluate_rule_signal_metadata(
            self.volatile, validate_rule_spec(spec))["atr"]
        self.assertAlmostEqual(signal["stop_distance"], atr * 5.0)
        self.assertFalse(signal["stop_floor_binding"])
        self.assertEqual(signal["stop_mode"], "horizon_atr")
        one = self._stop_bps(self.volatile, spec)
        two = self._stop_bps(self.volatile, {**spec, "stop_atr": 2.0})
        self.assertGreater(one, 30.0)
        self.assertAlmostEqual(two, 2 * one)


class StructuralFadeStopTests(unittest.TestCase):
    """A-05: an opening-range fade's stop sits beyond the failed extreme."""

    SPEC = {"schema": RULE_SCHEMA_V7, "family": "opening_range_fade",
            "range_minutes": 20, "threshold_bps": 8.0, "target_r": 1.5,
            "atr_period": 14}

    def _rows(self, high, close):
        rows = _session([100.05, 100.0, 100.08, 100.03] * 5 +
                        [100.04, 100.06, 100.02, 100.05, 100.05],
                        wick=.02)
        rows[-1].update(high=high, close=close, low=min(close, 100.05) - .02)
        return rows

    def test_frozen_stop_can_sit_inside_the_rejection_wick(self):
        signal = evaluate_rule_signal(self._rows(100.40, 99.99),
                                      validate_rule_spec(self.SPEC))
        self.assertEqual(signal["direction"], "short")
        self.assertLess(signal["stop_price"], 100.40)

    def test_structural_stop_clears_the_wick_by_one_atr(self):
        spec = validate_rule_spec({**self.SPEC, "stop_mode": "structure"})
        rows = self._rows(100.40, 99.99)
        signal = evaluate_rule_signal(rows, spec)
        atr = evaluate_rule_signal_metadata(rows, spec)["atr"]
        self.assertEqual(signal["structural_stop_level"], 100.40)
        self.assertAlmostEqual(signal["stop_price"], 100.40 + atr)
        self.assertFalse(signal["stop_floor_binding"])

    def test_a_small_wick_falls_back_to_the_authored_floor(self):
        spec = validate_rule_spec({**self.SPEC, "stop_mode": "structure"})
        signal = evaluate_rule_signal(self._rows(100.20, 100.09), spec)
        self.assertEqual(signal["structural_stop_level"], 100.20)
        self.assertTrue(signal["stop_floor_binding"])
        self.assertAlmostEqual(signal["stop_distance"], 100.09 * .003)
        self.assertGreater(signal["stop_price"], 100.20)

    def test_structure_is_fade_only(self):
        with self.assertRaisesRegex(RuleSpecError, "opening_range_fade"):
            validate_rule_spec({**self.SPEC, "family": "opening_range_breakout",
                                "stop_mode": "structure"})


class BetaAdjustedResidualTests(unittest.TestCase):
    """A-09: the residual can remove a causal prior-session beta x SPY move."""

    DAY = harness.SESSION_DATES[-1]
    SPEC = {"schema": RULE_SCHEMA_V7, "family": "cross_sectional_residual",
            "lookback": 15, "threshold_bps": 10.0, "atr_period": 5}
    BETAS = {"SMH": 1.6, "XLU": 0.4}

    @classmethod
    def setUpClass(cls):
        # Six prior sessions of a one-factor market: SPY is the factor and
        # each subject is beta x SPY plus independent noise.
        rng = random.Random(20261015)
        cls.prior = {symbol: [] for symbol in ("SPY", *cls.BETAS)}
        for day in harness.SESSION_DATES[:-1]:
            paths = {symbol: [base] for symbol, base in
                     (("SPY", 500.0), ("SMH", 250.0), ("XLU", 70.0))}
            for _minute in range(1, 120):
                market = rng.gauss(0.0, 3.0) / 10_000
                paths["SPY"].append(paths["SPY"][-1] * (1 + market))
                for symbol, beta in cls.BETAS.items():
                    move = beta * market + rng.gauss(0.0, 2.0) / 10_000
                    paths[symbol].append(paths[symbol][-1] * (1 + move))
            for symbol, closes in paths.items():
                cls.prior[symbol] += _session(closes, day=day, symbol=symbol,
                                              wick=.005)

    def _ols(self, subject, sessions=5):
        keep = set(harness.SESSION_DATES[-1 - sessions:-1])
        def returns(symbol):
            rows = [row for row in self.prior[symbol]
                    if row["timestamp"].astimezone(NY).date() in keep]
            return [right["close"] / left["close"] - 1
                    for left, right in zip(rows, rows[1:])
                    if right["timestamp"] - left["timestamp"] == timedelta(minutes=1)]
        xs, ys = returns("SPY"), returns(subject)
        x_mean, y_mean = sum(xs) / len(xs), sum(ys) / len(ys)
        return (sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) /
                sum((x - x_mean) ** 2 for x in xs))

    def _current(self, symbol, total_bps, *, base):
        # Flat, then the whole move inside the final 15-minute lookback.
        closes = [base * (1 + total_bps / 10_000 * max(index - 4, 0) / 15)
                  for index in range(20)]
        return _session(closes, day=self.DAY, symbol=symbol, wick=.005)

    def _context(self, subject, rows, spy_rows, sessions=None):
        prior = {name: self.prior[name] for name in (subject, "SPY")}
        if sessions is not None:
            keep = set(harness.SESSION_DATES[-1 - sessions:-1])
            prior = {name: [row for row in values
                            if row["timestamp"].astimezone(NY).date() in keep]
                     for name, values in prior.items()}
        return {subject: prior[subject] + rows, "SPY": prior["SPY"] + spy_rows}

    def _beta(self, subject):
        spec = validate_rule_spec({**self.SPEC, "residual_mode": "beta",
                                   "residual_beta_sessions": 5})
        rows = self._current(subject, 0.0, base=100.0)
        spy = self._current("SPY", 0.0, base=500.0)
        trace = evaluate_rule_signal_trace(
            rows, spec, bars_by_symbol=self._context(subject, rows, spy),
            symbol=subject)
        return trace["market_context"]["residual_beta"]

    def test_prior_session_beta_is_the_ols_beta_of_the_last_sessions(self):
        for subject, generating in self.BETAS.items():
            with self.subTest(subject=subject):
                self.assertAlmostEqual(self._beta(subject), self._ols(subject),
                                       places=9)
                self.assertAlmostEqual(self._beta(subject), generating,
                                       delta=0.15)

    def test_a_pure_beta_move_is_a_raw_signal_but_not_a_residual_one(self):
        raw = validate_rule_spec(self.SPEC)
        adjusted = validate_rule_spec({**self.SPEC, "residual_mode": "beta",
                                       "residual_beta_sessions": 5})
        spy = self._current("SPY", 20.0, base=500.0)
        for subject, expected_raw in (("SMH", "long"), ("XLU", "short")):
            with self.subTest(subject=subject):
                beta = self._beta(subject)
                rows = self._current(subject, beta * 20.0, base=100.0)
                context = self._context(subject, rows, spy)
                self.assertEqual(evaluate_rule_signal(
                    rows, raw, bars_by_symbol=context, symbol=subject)["direction"],
                    expected_raw)
                trace = evaluate_rule_signal_trace(
                    rows, adjusted, bars_by_symbol=context, symbol=subject)
                self.assertIsNone(trace["signal"])
                self.assertEqual(trace["stages"][-1]["reason"],
                                 "residual_threshold_not_met")
                self.assertAlmostEqual(
                    trace["market_context"]["residual_return"], 0.0, places=6)
                # Idiosyncratic strength beyond the beta move still signals.
                rows = self._current(subject, beta * 20.0 + 15.0, base=100.0)
                signal = evaluate_rule_signal(
                    rows, adjusted, bars_by_symbol=self._context(subject, rows, spy),
                    symbol=subject)
                self.assertEqual(signal["direction"], "long")
                self.assertEqual(signal["residual_mode"], "beta")

    def test_beta_is_causal_and_fails_closed_without_enough_sessions(self):
        spec = validate_rule_spec({**self.SPEC, "residual_mode": "beta",
                                   "residual_beta_sessions": 5})
        rows = self._current("SMH", 0.0, base=100.0)
        spy = self._current("SPY", 0.0, base=500.0)
        short = self._context("SMH", rows, spy, sessions=4)
        trace = evaluate_rule_signal_trace(rows, spec, bars_by_symbol=short,
                                           symbol="SMH")
        self.assertEqual(trace["stages"][-1]["reason"], "residual_beta_unavailable")
        # Bars from the signal's own or a later session never enter the beta.
        context = self._context("SMH", rows, spy)
        later = _session([100 + 5 * (index % 2) for index in range(200)],
                         day=date(2026, 8, 12), symbol="SMH")
        context["SMH"] = context["SMH"] + later
        self.assertEqual(
            evaluate_rule_signal_trace(rows, spec, bars_by_symbol=context,
                                       symbol="SMH")["market_context"]["residual_beta"],
            self._beta("SMH"))


class SameClockVolumeConfirmationTests(unittest.TestCase):
    """A-10 (rule side): the volume confirmation can use same-clock RVOL."""

    SPEC = {"schema": RULE_SCHEMA_V7, "family": "momentum_continuation",
            "lookback": 3, "slow_lookback": 5, "atr_period": 3,
            "threshold_bps": 0.0, "confirmation": "volume",
            "volume_multiplier": 1.5}
    PRIOR_DAYS = harness.SESSION_DATES[:5]
    DAY = harness.SESSION_DATES[5]

    def _case(self, preceding, current):
        closes = [100 + index * .01 for index in range(30)]
        rows = _session(closes, day=self.DAY,
                        volumes=[preceding] * 29 + [current])
        prior = [row for day in self.PRIOR_DAYS
                 for row in _session(closes, day=day, volumes=[1_000] * 30)]
        return rows, {"SPY": prior + rows}

    def test_current_feed_and_same_clock_disagree_on_a_quiet_minute(self):
        legacy = validate_rule_spec(self.SPEC)
        same_clock = validate_rule_spec(
            {**self.SPEC, "volume_confirmation_mode": "same_clock"})
        # 300 shares after 100-share minutes, where 1,000 is normal for the
        # clock: a burst against the preceding minutes, quiet for the time.
        rows, context = self._case(100, 300)
        self.assertIsNotNone(evaluate_rule_signal(
            rows, legacy, bars_by_symbol=context, symbol="SPY"))
        trace = evaluate_rule_signal_trace(
            rows, same_clock, bars_by_symbol=context, symbol="SPY")
        self.assertIsNone(trace["signal"])
        self.assertEqual(trace["stages"][-1]["stage"], "confirmation:volume")
        # 2,000 shares after 3,000-share minutes: quiet against the preceding
        # minutes, twice the normal participation for the clock.
        rows, context = self._case(3_000, 2_000)
        self.assertIsNone(evaluate_rule_signal(
            rows, legacy, bars_by_symbol=context, symbol="SPY"))
        signal = evaluate_rule_signal(
            rows, same_clock, bars_by_symbol=context, symbol="SPY")
        self.assertEqual(signal["confirmation_relative_volume"], 2.0)

    def test_missing_prior_samples_fail_closed_and_mode_needs_a_filter(self):
        same_clock = validate_rule_spec(
            {**self.SPEC, "volume_confirmation_mode": "same_clock",
             "relative_volume_sessions": 6})
        rows, context = self._case(3_000, 2_000)
        self.assertIsNone(evaluate_rule_signal(
            rows, same_clock, bars_by_symbol=context, symbol="SPY"))
        with self.assertRaisesRegex(RuleSpecError, "volume confirmation"):
            validate_rule_spec({**self.SPEC, "confirmation": "none",
                                "volume_confirmation_mode": "same_clock"})


class RangeMultiplierTests(unittest.TestCase):
    """A-15: range_expansion has its own range multiple."""

    SPEC = {"schema": RULE_SCHEMA_V7, "family": "range_expansion",
            "lookback": 5, "threshold_bps": 0.0, "atr_period": 3,
            "confirmation": "volume", "volume_multiplier": 2.0}

    def _rows(self, volume):
        rows = _session([100.0] * 11 + [100.06], wick=.05,
                        volumes=[1_000] * 11 + [volume])
        # The final bar's range is 1.6x the prior bars' 0.10.
        rows[-1].update(open=100.0, high=100.1, low=99.94, close=100.06)
        return rows

    def test_one_field_cannot_gate_range_and_volume_separately(self):
        self.assertIsNone(evaluate_rule_signal(
            self._rows(2_500), validate_rule_spec(self.SPEC)))

    def test_range_multiplier_gates_the_range_independently_of_volume(self):
        spec = validate_rule_spec({**self.SPEC, "range_multiplier": 1.5})
        signal = evaluate_rule_signal(self._rows(2_500), spec)
        self.assertEqual(signal["direction"], "long")
        self.assertEqual(signal["range_multiplier"], 1.5)
        trace = evaluate_rule_signal_trace(self._rows(1_500), spec)
        self.assertEqual(trace["stages"][-1]["stage"], "confirmation:volume")

    def test_range_multiplier_is_family_scoped_and_semantic(self):
        with self.assertRaisesRegex(RuleSpecError, "range_expansion"):
            validate_rule_spec({**self.SPEC, "family": "momentum_continuation",
                                "range_multiplier": 1.5})
        unconfirmed = {**self.SPEC, "confirmation": "none",
                       "range_multiplier": 1.5}
        # Without a volume filter volume_multiplier no longer means anything.
        self.assertEqual(
            rule_semantic_signature(unconfirmed),
            rule_semantic_signature({**unconfirmed, "volume_multiplier": 3.0}))


class ThesisDeadlineGuardTests(unittest.TestCase):
    """A-01: a thesis deadline must leave the entry window room to trade."""

    def test_the_frozen_v2_reversion_deadlines_are_reported_not_rejected(self):
        from research.mechanism_cohort_v2 import mechanism_cohort
        conflicts = {}
        for family in mechanism_cohort()["families"]:
            spec = family["arms"][0]["rule_spec"]
            validate_rule_spec(spec)  # still loadable
            conflicts[family["name"]] = thesis_deadline_conflict(
                spec, require_full_window=True)
        self.assertEqual(
            {name: reason for name, reason in conflicts.items() if reason}, {
                "opening_range_fade": THESIS_DEADLINE_INSIDE_ENTRY_HORIZON,
                "mean_reversion": THESIS_DEADLINE_INSIDE_ENTRY_HORIZON,
                "vwap_reversion": THESIS_DEADLINE_BEFORE_ENTRY_WINDOW,
            })

    def test_a_corrected_spec_cannot_register_an_unreachable_deadline(self):
        spec = {"schema": RULE_SCHEMA_V7, "family": "vwap_reversion",
                "entry_after_minutes": 30, "entry_before_minutes": 300,
                "exit_before_minutes": 360, "max_hold_bars": 15,
                "signal_event": "first_cross"}
        with self.assertRaisesRegex(RuleSpecError, "minutes before the 16:00"):
            validate_rule_spec(spec)
        accepted = validate_rule_spec({**spec, "exit_before_minutes": 30})
        self.assertIsNone(thesis_deadline_conflict(
            accepted, require_full_window=True))

    def test_a_deadline_at_the_first_possible_entry_is_unreachable(self):
        # Review: a signal on minute 30 enters at minute 31, so a deadline at
        # minute 31 (exit_before 359) can never hold a position either.
        spec = {"schema": RULE_SCHEMA_V7, "family": "momentum_continuation",
                "entry_after_minutes": 30, "entry_before_minutes": 300,
                "exit_before_minutes": 359, "max_hold_bars": 15,
                "signal_event": "first_cross"}
        with self.assertRaisesRegex(RuleSpecError, "minutes before the 16:00"):
            validate_rule_spec(spec)
        validate_rule_spec({**spec, "exit_before_minutes": 358,
                            "entry_before_minutes": 31, "max_hold_bars": 1})


class TrendPullbackConfirmationTests(unittest.TestCase):
    """A-14: the trend confirmation repeats the trend_pullback predicate."""

    def test_the_diagnostic_trend_confirmation_never_filters_a_bar(self):
        from research.diagnostic_shadow import _logical_arms
        specs = [arm["rule_spec"] for arm in _logical_arms()
                 if arm["family"] == "trend_pullback"]
        self.assertEqual([spec["confirmation"] for spec in specs],
                         ["trend", "trend"])
        market = harness.synthetic_market()
        reached = 0
        for spec in specs:
            for symbol in harness.SYMBOLS:
                rows = harness._session_rows(market[symbol],
                                             harness.SESSION_DATES[-1])
                for index in range(60, len(rows)):
                    stages = evaluate_rule_signal_trace(
                        rows[index - 40:index + 1], spec)["stages"]
                    if any(item["stage"] == "confirmation:trend"
                           for item in stages):
                        reached += 1
                        self.assertNotEqual(stages[-1]["stage"],
                                            "confirmation:trend")
        self.assertGreater(reached, 100)

    def test_a_corrected_trend_pullback_cannot_repeat_its_predicate(self):
        base = {"schema": RULE_SCHEMA_V7, "family": "trend_pullback",
                "lookback": 10, "slow_lookback": 35, "threshold_bps": 3.0,
                "signal_event": "first_cross"}
        for confirmation in ({"confirmation": "trend"},
                             {"confirmations": ["trend"]}):
            with self.subTest(confirmation=confirmation), \
                    self.assertRaisesRegex(RuleSpecError, "repeats"):
                validate_rule_spec({**base, **confirmation})
        validate_rule_spec({**base, "confirmation": "volume"})


class ThesisDeadlineCalendarCloseTests(unittest.TestCase):
    """A-19: the thesis deadline follows the caller's calendar close."""

    SPEC = validate_rule_spec({
        "schema": RULE_SCHEMA_V4, "family": "vwap_reversion",
        "max_hold_bars": 390, "exit_before_minutes": 30,
    })
    EARLY_CLOSE = date(2026, 11, 27)  # the day after Thanksgiving, 13:00 close

    def test_default_remains_the_regular_1600_close(self):
        entry = _ny(self.EARLY_CLOSE, 10, 0).timestamp()
        self.assertEqual(thesis_exit_deadline(entry, self.SPEC),
                         _ny(self.EARLY_CLOSE, 15, 30).timestamp())
        self.assertEqual(
            thesis_exit_deadline(entry, self.SPEC, session_close=None),
            thesis_exit_deadline(entry, self.SPEC))

    def test_early_close_anchors_the_deadline_in_every_accepted_form(self):
        entry = _ny(self.EARLY_CLOSE, 10, 0).timestamp()
        expected = _ny(self.EARLY_CLOSE, 12, 30).timestamp()
        close = _ny(self.EARLY_CLOSE, 13, 0)
        for session_close in (time(13, 0), close,
                              close.astimezone(timezone.utc), close.timestamp()):
            with self.subTest(session_close=session_close):
                self.assertEqual(thesis_exit_deadline(
                    entry, self.SPEC, session_close=session_close), expected)

    def test_exit_and_hold_deadlines_pass_the_close_through(self):
        entry = _ny(self.EARLY_CLOSE, 10, 0).timestamp()
        resolved = exit_deadline(entry, self.SPEC, session_close=time(13, 0))
        self.assertEqual(resolved, {
            "timestamp": _ny(self.EARLY_CLOSE, 12, 30).timestamp(),
            "reason": "thesis_deadline"})
        self.assertEqual(
            hold_deadline(entry, self.SPEC, session_close=time(13, 0)),
            _ny(self.EARLY_CLOSE, 12, 30).timestamp())
        # The unanchored deadline is after a 13:00 close, as the review found.
        self.assertGreater(hold_deadline(entry, self.SPEC),
                           _ny(self.EARLY_CLOSE, 13, 0).timestamp())

    def test_close_must_be_unambiguous_and_on_the_entry_date(self):
        entry = _ny(self.EARLY_CLOSE, 10, 0).timestamp()
        for session_close in (
                datetime(2026, 11, 27, 13, 0),  # naive
                _ny(date(2026, 11, 28), 13, 0),  # another session
                time(13, 0, tzinfo=timezone.utc)):
            with self.subTest(session_close=session_close), \
                    self.assertRaises(RuleSpecError):
                thesis_exit_deadline(entry, self.SPEC,
                                     session_close=session_close)


class DeadCodeTests(unittest.TestCase):
    """A-18: unused proof helper and a contradictory refresh branch."""

    def test_unused_edge_proof_helper_is_gone(self):
        import agent.edge
        self.assertFalse(hasattr(agent.edge, "_latest_passing_proof"))

    def test_refresh_parent_check_has_no_360_minute_branch(self):
        import inspect
        import research.mechanism_refresh_cohort as refresh
        self.assertNotIn("360", inspect.getsource(refresh._verified_parent_spec))
        # The refresh manifest is unchanged (hash computed before the edit).
        self.assertEqual(
            refresh.mechanism_refresh_cohort()["manifest_hash"],
            "67525c166888c5d10c2294ec1223200245d8ea89335cbaed1a75cd2418cbef76")


if __name__ == "__main__":
    unittest.main()
