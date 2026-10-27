"""v2 preregistrations: a fresh window, a frozen instrument, each look decided once.

Review findings D1-01 to D1-05 against v1: the manifest froze text only, a
single fallback control voided a look and hid futility, the hurdle was a
pre-cost point estimate, any date with one bar counted as a session and
nothing recorded that a look was spent, and the outcome was anchored where it
could not be traded.  v1 stays exactly as sealed; these tests pin v2.
"""

from contextlib import redirect_stderr, redirect_stdout
import copy
from datetime import date, datetime, time, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from agent.contracts.rule import rule_variant_id
from research import preregistered as prereg
from research.diagnostic_shadow import _logical_arms

NEW_YORK = ZoneInfo("America/New_York")
UTC = timezone.utc
# Pinned deliberately, like the v1 hashes.  If one fails, a registered v2
# hypothesis (or the instrument digest inside it) was edited: revert the
# edit, or register v3 with its own window.  Never update these to pass.
# ``python -B -c "from research.preregistered import v2_registration_literals
# as f; print(f())"`` prints these three for the running tree.
V2_VWAP_MANIFEST_HASH = (
    "c18b38c580f256ea60e860eda700841e73c2e622b4a628cb76d6856d36b4f0f5")
V2_ORF_MANIFEST_HASH = (
    "20a86ca6c05c7f26c56343b015960f2463fab8ffd18c3df185cee0e8122851bd")
V2_INSTRUMENT_DIGEST = (
    "4b7075f72e6c9097e784b7248edfc3210028cb824b9f675d19931bdd8b0df36d")


# Every fixture date is relative to the registered window, so moving the
# registration literals does not break the fixtures.
SEALED = date.fromisoformat(prereg.V2_SEALED_FROM)


def _on(offset: int) -> date:
    return SEALED + timedelta(days=offset)


def _weekdays(first: date, count: int) -> list[date]:
    days, day = [], first
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _session(day: date, symbols=("SPY",), *, minutes: int = 390,
             close: time = time(16, 0), calendar: bool = True,
             keep=None) -> list:
    """Light calendar-carrying bars: enough for session classification."""
    opened = datetime.combine(day, time(9, 30), NEW_YORK).astimezone(UTC)
    closed = datetime.combine(day, close, NEW_YORK).astimezone(UTC)
    rows = []
    for symbol in symbols:
        for minute in range(minutes):
            if keep is not None and not keep(minute):
                continue
            rows.append(SimpleNamespace(
                symbol=symbol, timestamp=opened + timedelta(minutes=minute),
                interval_seconds=60,
                session_open=opened if calendar else None,
                session_close=closed if calendar else None,
                identity=SimpleNamespace(session_date=day)))
    return rows


def _metric(**over):
    base = {"candidate_count": 400, "matched_count": 380, "session_clusters": 30,
            "candidate_minus_control_bps": 30.0,
            "candidate_minus_control_cluster_stderr_bps": 4.0,
            "candidate_minus_control_cluster_df": 29,
            "candidate_minus_control_cluster_sign_flip_p_value": 0.0001,
            "candidate_minus_control_after_hurdle_sign_flip_p_value": 0.001,
            "control_matching_counts": {"cross_session_same_session_minute": 380}}
    return {**base, **over}


def _small_manifest(hypothesis=prereg.VWAP_REVERSION_HYPOTHESIS_V2, **over):
    """The registered manifest with a one-symbol universe, for light fixtures."""
    manifest = copy.deepcopy(prereg.preregistration(hypothesis))
    manifest["session_definition"].update(universe=["SPY"],
                                          min_covered_symbols=1)
    manifest.update(over)
    return manifest


class _FakeInstrument:
    """Stand-in for measure_signal_quality that records how it was called."""

    def __init__(self, metric=None):
        self.metric = metric or (lambda sessions, kwargs: _metric())
        self.calls = []

    def __call__(self, rows, rule, **kwargs):
        sessions = {row.identity.session_date for row in rows}
        self.calls.append({"family": rule["family"], "sessions": len(sessions),
                           **kwargs})
        value = self.metric(len(sessions), kwargs)
        return {"horizon_metrics": {f"{h}m": value for h in kwargs["horizons"]}}


def _run(bars, *, manifest=None, records=(), eligible=True, metric=None,
         as_of=datetime.combine(_on(250), time(12, 0), UTC)):
    fake = _FakeInstrument(metric)
    with patch.object(prereg, "measure_signal_quality", side_effect=fake):
        report = prereg._evaluate_v2(
            bars, manifest or _small_manifest(), decision_eligible=eligible,
            eligibility_reasons=(), provenance=None,
            look_records=None if records is None else list(records),
            as_of=as_of)
    return report, fake


class RegistrationTests(unittest.TestCase):
    def test_v2_manifests_are_pinned_and_v1_is_untouched(self):
        self.assertEqual(prereg.preregistration(
            prereg.VWAP_REVERSION_HYPOTHESIS_V2)["manifest_hash"],
            V2_VWAP_MANIFEST_HASH)
        self.assertEqual(prereg.preregistration(
            prereg.OPENING_RANGE_FADE_HYPOTHESIS_V2)["manifest_hash"],
            V2_ORF_MANIFEST_HASH)
        self.assertEqual(
            prereg.preregistration(prereg.VWAP_REVERSION_HYPOTHESIS)["manifest_hash"],
            "d2e56d80bdb0e367bcceebbe4792bf4a00aee815eb442e62f043ee2e40b7c48e")
        self.assertEqual(
            prereg.preregistration(prereg.OPENING_RANGE_FADE_HYPOTHESIS)["manifest_hash"],
            "a0ae21f5ffd765142f2f4b92c47e33f652e0fb3213f1fd8d5a6d82c3915c2b9e")

    def test_fresh_window_starts_after_every_examined_session(self):
        self.assertGreater(prereg.V2_SEALED_FROM, prereg.V2_LAST_EXAMINED_SESSION)
        self.assertGreaterEqual(prereg.V2_SEALED_FROM, prereg.V2_REGISTERED_ON)
        self.assertGreater(prereg.V2_REGISTERED_ON, "2026-10-23")
        for hypothesis in prereg.V2_HYPOTHESES:
            manifest = prereg.preregistration(hypothesis)
            self.assertEqual(manifest["sealed_window"]["sessions_on_or_after"],
                             prereg.V2_SEALED_FROM)
            self.assertEqual(manifest["registered_on"], prereg.V2_REGISTERED_ON)
            self.assertEqual(manifest["supersedes"]["hypothesis_id"],
                             hypothesis.replace(".v2", ".v1"))
            self.assertIn(manifest["supersedes"]["hypothesis_id"],
                          prereg.COMPROMISED_HYPOTHESES)

    def test_subjects_are_the_frozen_v1_subjects_and_recorded_forward(self):
        cohort = {arm["variant_id"] for arm in _logical_arms()}
        for hypothesis in prereg.V2_HYPOTHESES:
            manifest = prereg.preregistration(hypothesis)
            v1 = prereg.preregistration(hypothesis.replace(".v2", ".v1"))
            self.assertEqual(manifest["subject"]["rule_spec"],
                             v1["subject"]["rule_spec"])
            for item in (manifest["subject"], manifest["secondary_descriptive"][0]):
                self.assertEqual(rule_variant_id(item["rule_spec"]),
                                 item["variant_id"])
                self.assertIn(item["variant_id"], cohort)


class InstrumentIdentityTests(unittest.TestCase):
    """D1-01: the registered instrument is the code that runs."""

    def test_registered_digest_is_the_running_instrument(self):
        current = prereg.instrument_identity()
        for hypothesis in prereg.V2_HYPOTHESES:
            registered = prereg.preregistration(hypothesis)["instrument_identity"]
            self.assertEqual(registered["digest"], V2_INSTRUMENT_DIGEST)
            # If this fails, code the v2 instrument reaches was edited.  Do
            # not re-pin after a sealed session's data have been examined: register
            # v3 with a new window instead.
            self.assertEqual(current["components"], registered["components"])
            self.assertEqual(current["digest"], registered["digest"])
            self.assertEqual(registered["roots"], current["roots"])

    def test_identity_covers_the_instrument_modules(self):
        components = prereg.instrument_identity()["components"]
        for module in ("research.signal_quality", "research.stats",
                       "research.preregistered", "research.market_data",
                       "agent.contracts.rule"):
            self.assertIn(module, components)
        self.assertNotIn("research.costs", components)

    def test_identity_follows_reached_code_and_ignores_the_rest(self):
        base = ("from .helpers import shared\n"
                "LIMIT = 3\n"
                "def root(x):\n    return helper(x) + LIMIT + shared\n"
                "def helper(x):\n    return x * 2\n"
                "def unrelated():\n    return 1\n")
        helpers = "shared = 5\nother = 6\n"

        def index_for(root_source, helper_source):
            sources = {"research.fake": root_source,
                       "research.helpers": helper_source}
            return lambda name: prereg._index_source(name, sources[name], "research")

        def digest(root_source, helper_source=helpers):
            entries = prereg._code_closure(
                [("research.fake", "root")], packages=("research",),
                index=index_for(root_source, helper_source))
            return json.dumps(sorted([list(key), value]
                                     for key, value in entries.items()))

        original = digest(base)
        self.assertEqual(original, digest(base.replace("return 1", "return 2")))
        self.assertEqual(original, digest(base, helpers.replace("6", "7")))
        self.assertNotEqual(original, digest(base.replace("x * 2", "x * 3")))
        self.assertNotEqual(original, digest(base.replace("LIMIT = 3", "LIMIT = 4")))
        self.assertNotEqual(original, digest(base, helpers.replace("5", "9")))

    def test_a_changed_instrument_refuses_the_decision_but_reports(self):
        manifest = _small_manifest()
        manifest["instrument_identity"] = {
            **manifest["instrument_identity"], "digest": "0" * 64}
        bars = [bar for day in _weekdays(SEALED, 31) for bar in _session(day)]
        report, _fake = _run(bars, manifest=manifest)
        self.assertEqual(report["decision"], "refused")
        self.assertEqual(report["decision_refusals"],
                         ["instrument_digest_mismatch"])
        self.assertFalse(report["instrument_identity"]["matches"])
        # Diagnostic output is still produced, and nothing is recorded.
        self.assertEqual(report["rule_outcome_if_eligible"], "pass")
        self.assertEqual(report["looks"][0]["status"], "evaluated")
        self.assertEqual(report["looks"][0]["look_record_status"], "not_recorded")
        self.assertEqual(report["look_records_to_append"], [])

    def test_public_evaluate_dispatches_v2_and_refuses_without_a_record_store(self):
        days = _weekdays(SEALED, 2)
        bars = [bar for day in days
                for bar in _session(day, prereg.V2_UNIVERSE[:18])]
        fake = _FakeInstrument()
        with patch.object(prereg, "measure_signal_quality", side_effect=fake):
            report = prereg.evaluate(
                bars, hypothesis_id=prereg.VWAP_REVERSION_HYPOTHESIS_V2,
                decision_eligible=True,
                as_of=datetime.combine(days[-1] + timedelta(days=5),
                                       time(12, 0), NEW_YORK))
        self.assertEqual(report["schema"], prereg.EVALUATION_SCHEMA_V2)
        self.assertEqual(report["sealed_sessions"],
                         [day.isoformat() for day in days])
        self.assertIn("look_record_store_missing", report["decision_refusals"])
        self.assertEqual(report["decision"], "refused")
        self.assertFalse(report["trading_authorized"])


class SessionDefinitionTests(unittest.TestCase):
    """D1-04: only complete regular sessions count, never the current one."""

    def test_incomplete_dates_are_excluded_with_a_reason(self):
        definition = prereg.preregistration(
            prereg.VWAP_REVERSION_HYPOTHESIS_V2)["session_definition"]
        universe = prereg.V2_UNIVERSE[:18]
        bars = (
            _session(_on(-4), universe) +                       # examined
            _session(_on(0), universe) +                        # complete
            _session(_on(1), universe, calendar=False) +
            _session(_on(2), universe, minutes=210,
                     close=time(13, 0)) +                       # early close
            _session(_on(3), universe,
                     keep=lambda minute: minute < 300) +        # outage
            _session(_on(6), universe[:10]) +                   # thin
            _session(_on(7), universe[:1]) +
            _session(_on(7), universe[1:],
                     keep=lambda minute: minute % 3) +          # 2/3 coverage
            _session(_on(8), universe))                         # today
        as_of = datetime.combine(_on(8), time(17, 30), NEW_YORK)  # after close
        quality = prereg.v2_session_quality(
            bars, definition, first=SEALED, as_of=as_of)
        self.assertEqual(quality["complete_sessions"],
                         [_on(0).isoformat(), _on(7).isoformat()])
        self.assertEqual(quality["excluded_counts"], {
            "before_sealed_window": 1,
            "calendar_metadata_missing_or_conflicting": 1,
            "current_or_future_date": 1,
            "early_or_irregular_close": 1,
            "recorder_coverage_below_minimum": 1,
            "symbol_coverage_below_minimum": 1,
        })

    def test_a_session_still_trading_is_not_counted(self):
        definition = _small_manifest()["session_definition"]
        bars = _session(SEALED)
        during = datetime.combine(SEALED, time(11, 0), NEW_YORK)
        self.assertEqual(prereg.v2_session_quality(
            bars, definition, first=SEALED,
            as_of=during)["complete_sessions"], [])
        later = datetime.combine(_on(1), time(5, 0), NEW_YORK)
        self.assertEqual(prereg.v2_session_quality(
            bars, definition, first=SEALED,
            as_of=later)["complete_sessions"], [SEALED.isoformat()])
        with self.assertRaises(ValueError):
            prereg.v2_session_quality(bars, definition, first=SEALED,
                                      as_of=datetime.combine(_on(1), time(5, 0)))

    def test_excluded_dates_never_reach_the_measurement(self):
        days = _weekdays(SEALED, 31)
        bars = [bar for day in days for bar in _session(day)]
        bars += _session(_on(-1))                               # before window
        bars += _session(days[3] + timedelta(days=0), ("QQQ",), calendar=False)
        report, fake = _run(bars)
        self.assertEqual(len(report["sealed_sessions"]), 30)
        self.assertNotIn(days[3].isoformat(), report["sealed_sessions"])
        for call in fake.calls:
            self.assertLessEqual(call["sessions"], 30)


class LookRecordTests(unittest.TestCase):
    """D1-04: a look is decided once and later runs reproduce it."""

    def setUp(self):
        self.days = _weekdays(SEALED, 61)
        self.bars = [bar for day in self.days for bar in _session(day)]

    def bars_for(self, count):
        keep = set(self.days[:count])
        return [bar for bar in self.bars if bar.identity.session_date in keep]

    def test_first_decision_is_recorded_and_later_runs_reproduce_it(self):
        first, _fake = _run(self.bars_for(31), records=[])
        self.assertEqual(first["decision"], "pass")
        self.assertEqual(len(first["look_records_to_append"]), 1)
        record = first["look_records_to_append"][0]
        self.assertEqual(record["schema"], prereg.LOOK_RECORD_SCHEMA)
        self.assertEqual(record["look"], 1)
        self.assertEqual(len(record["session_list"]), 30)
        self.assertEqual(record["instrument_digest"],
                         prereg.instrument_identity()["digest"])
        self.assertEqual(first["looks"][1]["status"],
                         "not_needed_after_terminal_decision")
        # A later run on more data reproduces look 1 and appends nothing.
        again, _fake = _run(self.bars_for(45), records=[record])
        self.assertEqual(again["decision"], "pass")
        self.assertEqual(again["looks"][0]["look_record_status"], "reproduced")
        self.assertEqual(again["look_records_to_append"], [])

    def test_a_look_is_never_decided_again(self):
        noisy = _metric(candidate_minus_control_bps=17.0,
                        candidate_minus_control_cluster_stderr_bps=10.0)
        first, _fake = _run(self.bars_for(31), records=[],
                            metric=lambda sessions, kwargs: noisy)
        self.assertEqual(first["decision"], "accruing")
        record = first["look_records_to_append"][0]
        self.assertEqual(record["result"]["outcome"], "inconclusive")
        # The same look-1 sessions now measure differently (a revised
        # corpus, or a changed instrument): the record stands, nothing is
        # re-decided, and the run is a conflict rather than a pass.
        changed, _fake = _run(self.bars_for(31), records=[record])
        self.assertEqual(changed["looks"][0]["look_record_status"], "conflict")
        self.assertEqual(changed["looks"][0]["look_record_mismatch"],
                         ["result_digest"])
        self.assertEqual(changed["looks"][0]["recorded_outcome"], "inconclusive")
        self.assertEqual(changed["decision"], "look_record_conflict")
        self.assertEqual(changed["look_records_to_append"], [])

    def test_look_two_uses_the_record_of_look_one(self):
        noisy = _metric(candidate_minus_control_bps=17.0,
                        candidate_minus_control_cluster_stderr_bps=10.0)
        first, _fake = _run(self.bars_for(30), records=[],
                            metric=lambda sessions, kwargs: noisy)
        records = first["look_records_to_append"]
        second, _fake = _run(self.bars_for(61), records=records,
                             metric=lambda sessions, kwargs: noisy)
        self.assertEqual(second["looks"][0]["look_record_status"], "reproduced")
        self.assertEqual(second["looks"][1]["look_record_status"], "recorded_now")
        self.assertEqual(second["decision"], "not_established")
        self.assertEqual([item["look"] for item in
                          second["look_records_to_append"]], [2])

    def test_a_diagnostic_run_records_nothing(self):
        report, _fake = _run(self.bars_for(31), records=[], eligible=False)
        self.assertEqual(report["decision"], "diagnostic_only")
        self.assertEqual(report["look_records_to_append"], [])

    def test_the_command_line_appends_each_look_once(self):
        bars = self.bars_for(31)
        with tempfile.TemporaryDirectory() as tmp:
            records = Path(tmp) / "looks.jsonl"
            fake = _FakeInstrument()
            outputs = []
            for run in range(2):
                out = Path(tmp) / f"report-{run}.json"
                with patch.object(prereg, "_load", return_value=(
                        bars, True, [], {})), \
                        patch.object(prereg, "measure_signal_quality",
                                     side_effect=fake), \
                        patch.object(prereg, "preregistration",
                                     return_value=_small_manifest()), \
                        redirect_stdout(io.StringIO()):
                    prereg.main(["--data", tmp, "--out", str(out),
                                 "--hypothesis", prereg.VWAP_REVERSION_HYPOTHESIS_V2,
                                 "--look-records", str(records),
                                 "--as-of", datetime.combine(
                                     _on(120), time(12, 0), UTC).isoformat()])
                outputs.append(json.loads(out.read_text()))
            lines = [line for line in records.read_text().splitlines() if line]
            self.assertEqual(len(lines), 1)
            self.assertEqual(outputs[0]["looks"][0]["look_record_status"],
                             "recorded_now")
            self.assertEqual(outputs[1]["looks"][0]["look_record_status"],
                             "reproduced")

    def test_the_command_line_requires_a_look_record_for_v2(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                prereg.main(["--data", tmp, "--out", str(Path(tmp) / "r.json"),
                             "--hypothesis", prereg.VWAP_REVERSION_HYPOTHESIS_V2])

    def test_a_malformed_record_line_is_refused(self):
        with self.assertRaises(prereg.PreregistrationError):
            prereg.read_look_records(io.StringIO('{"schema": "other"}\n'))
        with self.assertRaises(prereg.PreregistrationError):
            prereg.read_look_records(io.StringIO("not json\n"))


class DecisionRuleTests(unittest.TestCase):
    def v2(self, **over):
        return prereg.look_outcome_v2(_metric(**over), alpha=0.025, cost_bps=17.0)

    def test_fallback_controls_neither_void_a_look_nor_hide_futility(self):
        # D1-02.  The reviewer's example: 120 candidates, mean -20 bps, SE 4,
        # df 29, one control from a same-session fallback tier.  v1 calls it
        # inconclusive; under v2 the instrument leaves that candidate
        # unmatched, coverage is still adequate, and futility stands.
        base = dict(candidate_count=120, matched_count=119,
                    candidate_minus_control_bps=-20.0,
                    candidate_minus_control_cluster_stderr_bps=4.0)
        v1 = prereg.look_outcome(_metric(**base, control_matching_counts={
            "cross_session_same_session_minute": 119,
            "same_session_minute_band": 1}), alpha=0.025, hurdle_bps=3.0)
        self.assertEqual(v1["reason"], "control_tier_not_registered")
        result = self.v2(**base, control_matching_counts={
            "cross_session_same_session_minute": 119,
            "excluded_same_session_minute_band": 1})
        self.assertEqual(result["outcome"], "futility")
        self.assertEqual(result["reason"], "upper_bound_below_round_trip_cost")

    def test_unmatched_candidates_count_against_coverage(self):
        self.assertEqual(self.v2(matched_count=300)["reason"],
                         "underpowered_control")
        self.assertEqual(self.v2(matched_count=29, candidate_count=30)["reason"],
                         "underpowered_control")

    def test_the_hurdle_is_the_round_trip_cost_on_a_lower_bound(self):
        # D1-03.  9 bps with SE 2 passes v1 (significant, above 3 bps) but
        # cannot pay a 17 bps round trip: its upper bound is about 13 bps.
        v1 = prereg.look_outcome(_metric(
            candidate_minus_control_bps=9.0,
            candidate_minus_control_cluster_stderr_bps=2.0),
            alpha=0.025, hurdle_bps=3.0)
        self.assertEqual(v1["outcome"], "pass")
        small = self.v2(candidate_minus_control_bps=9.0,
                        candidate_minus_control_cluster_stderr_bps=2.0)
        self.assertEqual(small["outcome"], "futility")
        self.assertLess(small["delta_upper_bound_bps"], 17.0)
        # A point estimate above the cost is not enough: the bound must be.
        above = self.v2(candidate_minus_control_bps=22.0)
        self.assertEqual(above["outcome"], "inconclusive")
        self.assertLess(above["delta_lower_bound_bps"], 17.0)
        clear = self.v2(candidate_minus_control_bps=30.0)
        self.assertEqual(clear["outcome"], "pass")
        self.assertGreater(clear["delta_lower_bound_bps"], 17.0)
        self.assertAlmostEqual(clear["cr1_critical_t"], 2.0452296, places=5)

    def test_the_after_cost_sign_flip_must_agree(self):
        result = self.v2(candidate_minus_control_after_hurdle_sign_flip_p_value=0.2)
        self.assertNotEqual(result["outcome"], "pass")
        result = self.v2(candidate_minus_control_after_hurdle_sign_flip_p_value=None)
        self.assertEqual(result["reason"], "clustered_inference_unavailable")

    def test_the_decision_measures_the_executable_registered_endpoint(self):
        days = _weekdays(SEALED, 31)
        bars = [bar for day in days for bar in _session(day)]
        _report, fake = _run(bars, records=[])
        anchors = {call["outcome_anchor"] for call in fake.calls}
        self.assertEqual(anchors, {"next_bar_open", "skip_one_bar_open",
                                   "signal_close"})
        for call in fake.calls:
            self.assertEqual(call["horizon_clock"], "wall_clock")
            self.assertEqual(call["control_tiers"],
                             ["cross_session_same_session_minute"])
            self.assertEqual(call["delta_hurdle_bps"], 17.0)
        decision_calls = [call for call in fake.calls if call["sessions"] == 30
                          and call["outcome_anchor"] == "next_bar_open"]
        self.assertEqual(len(decision_calls), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
