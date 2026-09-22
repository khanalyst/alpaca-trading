"""The preregistered hypothesis is frozen, sealed, and decided by fixed rules."""

from datetime import date, datetime, timedelta, timezone
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.contracts.rule import rule_variant_id
from research import preregistered as prereg
from research.diagnostic_shadow import _logical_arms
from research.market_data import normalize_underlying_bar


# Pinned deliberately.  If this fails, the registered hypothesis was edited:
# revert the edit, or register a new version (``...v2``) with its own hash.
# Never update this literal to make the suite pass.
FROZEN_MANIFEST_HASH = (
    "d2e56d80bdb0e367bcceebbe4792bf4a00aee815eb442e62f043ee2e40b7c48e")


def _bars(day: date, *, count: int = 40, symbol: str = "SPY") -> list:
    # 09:30 New York is 13:30 UTC during daylight saving.
    start = datetime(day.year, day.month, day.day, 13, 30, tzinfo=timezone.utc)
    return [normalize_underlying_bar({
        "symbol": symbol, "timestamp": (start + timedelta(minutes=i)).isoformat(),
        "open": 100.0, "high": 100.05, "low": 99.95, "close": 100.0,
        "volume": 1000, "provider": "alpaca", "feed": "iex",
    }) for i in range(count)]


def _metric(**over):
    base = {"candidate_count": 400, "matched_count": 380, "session_clusters": 30,
            "candidate_minus_control_bps": 9.0,
            "candidate_minus_control_cluster_stderr_bps": 2.0,
            "candidate_minus_control_cluster_df": 29,
            "candidate_minus_control_cluster_sign_flip_p_value": 0.001}
    return {**base, **over}


class FrozenManifestTests(unittest.TestCase):
    def test_manifest_hash_is_pinned(self):
        self.assertEqual(prereg.preregistration()["manifest_hash"],
                         FROZEN_MANIFEST_HASH)

    def test_subject_and_mirror_resolve_to_their_frozen_ids(self):
        manifest = prereg.preregistration()
        subject = manifest["subject"]
        self.assertEqual(rule_variant_id(subject["rule_spec"]),
                         subject["variant_id"])
        mirror = manifest["secondary_descriptive"][0]
        self.assertEqual(rule_variant_id(mirror["rule_spec"]),
                         mirror["variant_id"])

    def test_subject_is_already_collected_by_the_deployed_shadow_cohort(self):
        # The forward data this hypothesis needs is being recorded only if the
        # live diagnostic cohort still carries the exact frozen arm.
        cohort = {arm["variant_id"] for arm in _logical_arms()}
        manifest = prereg.preregistration()
        self.assertIn(manifest["subject"]["variant_id"], cohort)
        self.assertIn(manifest["secondary_descriptive"][0]["variant_id"], cohort)

    def test_looks_follow_the_repository_cluster_floor_and_split_alpha(self):
        from research.gates import PROTOCOL_QUALIFICATION_MIN_CLUSTERS
        looks = prereg.preregistration()["looks"]
        self.assertEqual([look["sessions"] for look in looks],
                         [PROTOCOL_QUALIFICATION_MIN_CLUSTERS,
                          2 * PROTOCOL_QUALIFICATION_MIN_CLUSTERS])
        self.assertAlmostEqual(sum(look["alpha_one_sided"] for look in looks), 0.05)

    def test_unknown_hypothesis_is_refused(self):
        with self.assertRaises(prereg.PreregistrationError):
            prereg.preregistration("not-registered.v1")


class DecisionRuleTests(unittest.TestCase):
    def outcome(self, **over):
        return prereg.look_outcome(_metric(**over), alpha=0.025, hurdle_bps=3.0)

    def test_a_large_clustered_effect_above_the_hurdle_passes(self):
        self.assertEqual(self.outcome()["outcome"], "pass")

    def test_a_significant_effect_below_the_hurdle_is_futile_not_a_pass(self):
        # Real but too small to trade: significance alone never passes.
        result = self.outcome(candidate_minus_control_bps=2.0,
                              candidate_minus_control_cluster_stderr_bps=0.4)
        self.assertEqual(result["outcome"], "futility")

    def test_the_sign_flip_test_must_agree_with_the_t_test(self):
        result = self.outcome(
            candidate_minus_control_cluster_sign_flip_p_value=0.2)
        self.assertNotEqual(result["outcome"], "pass")

    def test_noise_is_inconclusive(self):
        result = self.outcome(candidate_minus_control_bps=4.0,
                              candidate_minus_control_cluster_stderr_bps=4.0)
        self.assertEqual(result["outcome"], "inconclusive")

    def test_thin_or_poorly_covered_controls_are_inconclusive(self):
        self.assertEqual(self.outcome(matched_count=20)["reason"],
                         "underpowered_control")
        self.assertEqual(self.outcome(matched_count=300)["reason"],
                         "underpowered_control")

    def test_a_fallback_control_tier_cannot_decide_a_look(self):
        # With too few sessions the instrument falls back to a same-session
        # control; the sealed manifest registered a cross-session one.
        result = self.outcome(control_matching_counts={
            "cross_session_same_session_minute": 370,
            "same_session_minute_band": 10})
        self.assertEqual(result["outcome"], "inconclusive")
        self.assertEqual(result["reason"], "control_tier_not_registered")
        registered = self.outcome(control_matching_counts={
            "cross_session_same_session_minute": 380})
        self.assertEqual(registered["outcome"], "pass")

    def test_missing_clustered_inference_is_inconclusive(self):
        result = self.outcome(candidate_minus_control_cluster_stderr_bps=None)
        self.assertEqual(result["reason"], "clustered_inference_unavailable")


class SealedEvaluationTests(unittest.TestCase):
    def test_examined_sessions_are_excluded_and_nothing_is_decided_early(self):
        examined = _bars(date(2026, 9, 21))
        sealed = _bars(date(2026, 9, 22))
        report = prereg.evaluate(examined + sealed, decision_eligible=True)
        self.assertEqual(report["sealed_sessions"], ["2026-09-22"])
        self.assertEqual(report["excluded_examined_bars"], len(examined))
        self.assertEqual(report["decision"], "accruing")
        self.assertEqual({look["status"] for look in report["looks"]},
                         {"not_reached"})
        self.assertFalse(report["authorizing"])
        self.assertFalse(report["trading_authorized"])

    def test_ineligible_data_can_never_be_decision_bearing(self):
        report = prereg.evaluate(_bars(date(2026, 9, 22)),
                                 decision_eligible=False,
                                 eligibility_reasons=["feed delayed_sip"])
        self.assertEqual(report["decision"], "diagnostic_only")
        self.assertEqual(report["eligibility_reasons"], ["feed delayed_sip"])
        self.assertFalse(report["authorizing"])

    def _sessions(self, count):
        days, day = [], date(2026, 9, 22)
        while len(days) < count:
            if day.weekday() < 5:
                days.append(day)
            day += timedelta(days=1)
        return [bar for day in days for bar in _bars(day, count=2)]

    def _run(self, sessions, metrics):
        calls = iter(metrics)

        def fake(rows, rule, **_kwargs):
            if rule["family"] == "vwap_trend":
                return {"horizon_metrics": {}}
            return {"horizon_metrics": {"60m": next(calls, _metric())}}
        with patch.object(prereg, "measure_signal_quality", side_effect=fake):
            return prereg.evaluate(self._sessions(sessions),
                                   decision_eligible=True)

    def test_look_one_uses_exactly_the_first_thirty_sealed_sessions(self):
        # interim, then look 1
        report = self._run(35, [_metric(), _metric()])
        look = report["looks"][0]
        sealed = report["sealed_sessions"]
        self.assertEqual(look["window_first"], sealed[0])
        self.assertEqual(look["window_last"], sealed[29])
        self.assertEqual(report["decision"], "pass")

    def test_futility_at_look_one_retires_the_hypothesis(self):
        report = self._run(30, [_metric(), _metric(
            candidate_minus_control_bps=-1.0,
            candidate_minus_control_cluster_stderr_bps=1.0)])
        self.assertEqual(report["decision"], "futility")

    def test_inconclusive_at_both_looks_is_not_established(self):
        noisy = _metric(candidate_minus_control_bps=4.0,
                        candidate_minus_control_cluster_stderr_bps=4.0)
        report = self._run(60, [noisy, noisy, noisy])
        self.assertEqual([look["outcome"] for look in report["looks"]],
                         ["inconclusive", "inconclusive"])
        self.assertEqual(report["decision"], "not_established")

    def test_inconclusive_at_look_one_keeps_accruing(self):
        noisy = _metric(candidate_minus_control_bps=4.0,
                        candidate_minus_control_cluster_stderr_bps=4.0)
        report = self._run(45, [noisy, noisy])
        self.assertEqual(report["decision"], "accruing")
        self.assertEqual(report["looks"][1]["status"], "not_reached")


class CommandLineTests(unittest.TestCase):
    def test_an_existing_report_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.json"
            out.write_text("{}", encoding="utf-8")
            data = Path(tmp) / "bars.jsonl"
            data.write_text("", encoding="utf-8")
            with self.assertRaises(SystemExit):
                prereg.main(["--data", str(data), "--out", str(out)])
            self.assertEqual(json.loads(out.read_text()), {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
