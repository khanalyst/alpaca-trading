"""Opt-in tolerance of IEX gaps for session-anchored rules (review A-08).

Under the frozen grammar a session-anchored rule (opening range, opening
drive, session VWAP) needs every minute from 09:30 to its signal bar, so one
missing IEX minute removes every later signal of the session.  The V7
``session_gap_mode: zero_volume`` correction treats such a minute as a minute
without IEX volume: replay then needs adjacency only over the trailing causal
window, while the session must still be observed from the bell.  Nothing
changes for a spec that does not opt in.
"""

from datetime import datetime, timedelta, timezone
import unittest
from zoneinfo import ZoneInfo

from agent.contracts.rule import (RULE_SCHEMA_V6, RULE_SCHEMA_V7, RuleSpecError,
                                  continuity_window_bars, feature_window_bars,
                                  rule_variant_id, validate_rule_spec)
from research.costs import ReplayPolicy
from research.edge_lab import _read_discovery_rows
from research.factory_core import _simulate_trade
from research.session_coverage import prefix_gap_report

BAR_FALLBACK = ReplayPolicy(strict_market_data=False)
NEW_YORK = ZoneInfo("America/New_York")

# Forty quiet minutes around 100, a 40 bps flush below session VWAP on bar 40,
# then a reversion that reaches the 1R target and settles inside the band, so
# the session holds exactly one signal.
_CLOSES = ([100.0 + (.03 if step % 2 else -.03) for step in range(40)] +
           [99.60] + [99.75, 99.95] + [100.05] * 17)
_VOLUMES = [1000] * len(_CLOSES)
_BASE = {"schema": RULE_SCHEMA_V7, "family": "vwap_reversion", "lookback": 5,
         "atr_period": 5, "threshold_bps": 20.0, "confirmation": "none",
         "stop_atr": 1.0, "target_r": 1.0, "max_hold_bars": 15,
         "min_stop_distance_bps": 30.0}
STRICT = validate_rule_spec(_BASE)
TOLERANT = validate_rule_spec({**_BASE, "session_gap_mode": "zero_volume"})


def _rows(drop: tuple[int, ...] = ()) -> list[dict]:
    opening = datetime(2026, 1, 6, 9, 30, tzinfo=NEW_YORK).astimezone(timezone.utc)
    rows, previous = [], _CLOSES[0]
    for index, (close, volume) in enumerate(zip(_CLOSES, _VOLUMES)):
        if index in drop:
            previous = close
            continue
        stamp = opening + timedelta(minutes=index)
        rows.append({
            "kind": "bar", "provider": "test", "feed": "iex", "symbol": "AAA",
            "timestamp": stamp.isoformat(), "as_of": stamp.isoformat(),
            "observed_at": stamp.isoformat(), "open": round(previous, 4),
            "high": round(max(previous, close) + .02, 4),
            "low": round(min(previous, close) - .02, 4),
            "close": round(close, 4), "volume": volume,
        })
        previous = close
    return rows


def _outcome(spec, drop: tuple[int, ...] = ()):
    bars = _read_discovery_rows(_rows(drop))[1]
    return _simulate_trade(bars, spec, [], "equity", quotes=None,
                           policy=BAR_FALLBACK)


def _traded(outcome) -> bool:
    return (outcome is not None and outcome.get("execution_disposition") not in
            {"refused", "no_signal"} and not outcome.get("unpriced_reason"))


class SessionGapModeGrammarTests(unittest.TestCase):
    def test_neutral_value_is_normalized_away(self):
        neutral = validate_rule_spec({**_BASE, "session_gap_mode": "strict_prefix"})
        self.assertNotIn("session_gap_mode", neutral)
        self.assertEqual(rule_variant_id(neutral), rule_variant_id(STRICT))

    def test_opt_in_is_a_new_registration(self):
        self.assertEqual(TOLERANT["session_gap_mode"], "zero_volume")
        self.assertNotEqual(rule_variant_id(TOLERANT), rule_variant_id(STRICT))

    def test_only_session_anchored_rules_accept_it(self):
        with self.assertRaises(RuleSpecError):
            validate_rule_spec({**_BASE, "family": "mean_reversion",
                                "session_gap_mode": "zero_volume"})
        validate_rule_spec({**_BASE, "family": "mean_reversion",
                            "target_mode": "session_vwap",
                            "session_gap_mode": "zero_volume"})
        validate_rule_spec({"schema": RULE_SCHEMA_V7,
                            "family": "opening_range_breakout",
                            "range_minutes": 15,
                            "session_gap_mode": "zero_volume"})

    def test_it_needs_the_v7_grammar(self):
        with self.assertRaises(RuleSpecError):
            validate_rule_spec({**_BASE, "schema": RULE_SCHEMA_V6,
                                "session_gap_mode": "zero_volume"})

    def test_feature_window_is_unchanged_and_continuity_is_trailing(self):
        self.assertIsNone(feature_window_bars(TOLERANT))
        self.assertIsNone(continuity_window_bars(STRICT))
        self.assertEqual(continuity_window_bars(TOLERANT), 7)

    def test_every_registered_arm_keeps_its_continuity_window(self):
        from research.diagnostic_shadow import _logical_arms
        from research.mechanism_cohort import mechanism_cohort as m1
        from research.mechanism_cohort_v2 import mechanism_cohort as m2
        from research.mechanism_cohort_v3 import mechanism_cohort as m3
        from research.mechanism_cost_cohort import mechanism_cost_cohort as mc
        from research.mechanism_deadline_cohort import mechanism_deadline_cohort as md
        from research.mechanism_refresh_cohort import mechanism_refresh_cohort as mr
        specs = [arm["rule_spec"] for arm in _logical_arms()]
        for build in (m1, m2, m3, mc, md, mr):
            specs += [arm["rule_spec"] for family in build()["families"]
                      for arm in family["arms"]]
        self.assertGreater(len(specs), 70)
        for spec in specs:
            self.assertEqual(continuity_window_bars(spec), feature_window_bars(spec))


class SessionGapReplayTests(unittest.TestCase):
    # The flush bar (index 40) closes at 10:11 New York.
    SIGNAL = "2026-01-06T15:11:00+00:00"

    def test_both_modes_trade_a_complete_session(self):
        for spec in (STRICT, TOLERANT):
            outcome = _outcome(spec)
            self.assertTrue(_traded(outcome))
            self.assertEqual(outcome["signal_timestamp"], self.SIGNAL)

    def test_an_early_gap_voids_the_strict_rule_but_not_the_tolerant_one(self):
        strict = _outcome(STRICT, drop=(5,))
        self.assertFalse(_traded(strict))
        self.assertEqual(strict["unpriced_reason"], "no_contiguous_feature_window")
        tolerant = _outcome(TOLERANT, drop=(5,))
        self.assertTrue(_traded(tolerant))
        self.assertEqual(tolerant["signal_timestamp"], self.SIGNAL)

    def test_a_gap_inside_the_trailing_window_still_refuses(self):
        self.assertFalse(_traded(_outcome(TOLERANT, drop=(37,))))

    def test_a_session_first_seen_after_the_bell_is_refused(self):
        self.assertFalse(_traded(_outcome(TOLERANT, drop=(0,))))


class SignalQualityParityTests(unittest.TestCase):
    """Review of A-08: the opt-in must reach every sampler, not only the factory."""

    def test_signal_quality_and_controls_honour_the_opt_in(self):
        from research.signal_quality import _ControlPolicy, _first_event
        bars = _read_discovery_rows(_rows(drop=(5,)))[1]
        strict, strict_reason = _first_event(bars, STRICT, allow_backfill=False)
        tolerant, _reason = _first_event(bars, TOLERANT, allow_backfill=False)
        self.assertIsNone(strict, strict_reason)
        self.assertIsNotNone(tolerant)
        # Bar 40 (the flush) is list index 39 once bar 5 is missing.
        self.assertIsNone(_ControlPolicy(STRICT).admissible(bars, 39))
        self.assertIsNotNone(_ControlPolicy(TOLERANT).admissible(bars, 39))


class PrefixGapReportTests(unittest.TestCase):
    def test_counts_the_minutes_a_strict_prefix_rule_loses(self):
        report = prefix_gap_report({"AAA": _rows(drop=(5,)), "BBB": _rows(),
                                    "CCC": _rows(drop=(0,))})
        aaa, bbb, ccc = (report["symbols"][name] for name in ("AAA", "BBB", "CCC"))
        observed = len(_CLOSES) - 1
        self.assertEqual((aaa["sessions"], aaa["sessions_with_gap"],
                          aaa["median_first_gap_minute"]), (1, 1, 5))
        lost = (len(_CLOSES) - 6) / observed
        self.assertAlmostEqual(aaa["strict_prefix_minutes_lost_share"], lost)
        self.assertAlmostEqual(aaa["opening_anchored_minutes_lost_share"], lost)
        self.assertEqual(bbb["sessions_complete"], 1)
        self.assertEqual(bbb["strict_prefix_minutes_lost_share"], 0.0)
        # A missing 09:30 bar costs the opening families the whole session;
        # a session VWAP rule still has an unbroken prefix from its first bar.
        self.assertEqual(ccc["sessions_missing_open"], 1)
        self.assertEqual(ccc["sessions_with_gap"], 0)
        self.assertEqual(ccc["strict_prefix_minutes_lost_share"], 0.0)
        self.assertEqual(ccc["opening_anchored_minutes_lost_share"], 1.0)
        self.assertEqual(report["universe"]["sessions"], 3)


if __name__ == "__main__":
    unittest.main()
