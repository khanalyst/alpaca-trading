"""A corpus the replay cannot price must not look like an edgeless one.

This is the failure that motivated these checks.  A 180-session backfill of
bars, taken without ``--quotes``, met every dataset guard: the files existed,
were non-empty, and held real rows.  Strict replay then refused to price a
single fill, so all 800 opportunities came back ``no_trade``, the gates saw
zero trades, and the cycle reported ``completed_no_edge`` -- the same status a
healthy run produces when nothing beats its control.

The operator had been told to expect exactly that status for weeks.  Nothing in
the proof recorded whether a fill came from a quote or a bar, so nothing
anywhere said the corpus had never been evaluated at all.
"""

import importlib.util
from pathlib import Path
import unittest

from research.costs import BAR, QUOTE, CostError, ReplayPolicy
from research.gates import fill_source_summary, unevaluable_reason

# ``research.py`` is the CLI script; a bare ``import research`` resolves to the
# package of the same name instead.  Load it by path, as the other CLI tests do.
_CLI_PATH = Path(__file__).parents[2] / "research.py"
_CLI_SPEC = importlib.util.spec_from_file_location("research_cli_unpriceable", _CLI_PATH)
research_cli = importlib.util.module_from_spec(_CLI_SPEC)
assert _CLI_SPEC.loader is not None
_CLI_SPEC.loader.exec_module(research_cli)


def _priced(count: int, source: str = QUOTE) -> list[dict]:
    return [{"vehicle": "equity", "symbol": "SPY", "session_date": f"2026-01-{i:02d}",
             "opportunity_id": f"p{i}", "net_pnl": 1.0, "no_trade": False,
             "entry_fill_source": source} for i in range(1, count + 1)]


def _refused(count: int, reason: str) -> list[dict]:
    return [{"vehicle": "equity", "symbol": "SPY", "session_date": f"2026-02-{i:02d}",
             "opportunity_id": f"r{i}", "net_pnl": 0.0, "no_trade": True,
             "reject_reason": reason} for i in range(1, count + 1)]


def _gate(fit: list[dict], heldout: list[dict]) -> dict:
    return {"fill_quality": {
        "fit": fill_source_summary(fit, vehicle="equity"),
        "heldout": fill_source_summary(heldout, vehicle="equity")}}


class StrictMarketDataConfigTests(unittest.TestCase):
    """The policy field every sibling reads, which this one did not."""

    def test_defaults_to_strict(self):
        self.assertTrue(ReplayPolicy.from_config({}).strict_market_data)

    def test_config_can_relax_it(self):
        policy = ReplayPolicy.from_config(
            {"execution": {"strict_market_data": False}})
        self.assertFalse(policy.strict_market_data)

    def test_accepts_the_string_forms_a_config_file_produces(self):
        for raw, expected in (("false", False), ("true", True), ("False", False)):
            policy = ReplayPolicy.from_config(
                {"execution": {"strict_market_data": raw}})
            self.assertIs(policy.strict_market_data, expected, raw)

    def test_a_non_boolean_is_a_configuration_error_not_a_coercion(self):
        for raw in (1, 0, "yes", None, []):
            with self.assertRaises(CostError):
                ReplayPolicy.from_config({"execution": {"strict_market_data": raw}})


class FillQualityTests(unittest.TestCase):
    """A proof has to state what actually priced it."""

    def test_quoted_and_bar_fills_are_distinguished(self):
        summary = fill_source_summary(
            _priced(3, QUOTE) + _priced(1, BAR), vehicle="equity")
        self.assertEqual(summary["executed"], 4)
        self.assertEqual(summary["sources"], {BAR: 1, QUOTE: 3})
        self.assertAlmostEqual(summary["quoted_fraction"], 0.75)
        self.assertFalse(summary["priced_nothing"])

    def test_a_corpus_that_priced_nothing_says_so_and_names_the_cause(self):
        summary = fill_source_summary(
            _refused(40, "no fresh equity quote at entry"), vehicle="equity")
        self.assertEqual(summary["opportunities"], 40)
        self.assertEqual(summary["executed"], 0)
        self.assertTrue(summary["priced_nothing"])
        self.assertEqual(summary["dominant_reject_reason"],
                         "no fresh equity quote at entry")
        self.assertIsNone(summary["quoted_fraction"])

    def test_the_dominant_reason_is_the_most_common_one(self):
        rows = _refused(2, "risk budget cannot fund one unit") + \
            _refused(9, "no fresh equity quote at entry")
        # Distinct session dates keep the two groups from colliding on id.
        for index, row in enumerate(rows):
            row["opportunity_id"] = f"mixed-{index}"
        summary = fill_source_summary(rows, vehicle="equity")
        self.assertEqual(summary["dominant_reject_reason"],
                         "no fresh equity quote at entry")

    def test_an_empty_lane_is_not_a_priced_nothing_lane(self):
        summary = fill_source_summary([], vehicle="equity")
        self.assertFalse(summary["priced_nothing"])
        self.assertEqual(summary["opportunities"], 0)

    def test_explicit_no_signal_rows_are_not_unpriced_opportunities(self):
        rows = [{
            "vehicle": "equity", "symbol": "SPY",
            "session_date": "2026-02-02", "opportunity_id": "flat",
            "net_pnl": 0.0, "no_trade": True,
            "execution_disposition": "no_signal",
            "signal_opportunity": False,
            "no_signal_reason": "rule_not_triggered",
        }]
        summary = fill_source_summary(rows, vehicle="equity")
        self.assertEqual(summary["opportunities"], 1)
        self.assertEqual(summary["execution_opportunities"], 0)
        self.assertEqual(summary["no_signal"], 1)
        self.assertFalse(summary["priced_nothing"])
        self.assertIsNone(unevaluable_reason(
            [{"fill_quality": {"fit": summary, "heldout": summary}}]))


class UnevaluableCorpusTests(unittest.TestCase):
    """The distinction the cycle status was collapsing."""

    def test_a_bars_only_corpus_is_reported_as_unevaluable(self):
        reason = "no fresh equity quote at entry"
        gates = [_gate(_refused(80, reason), _refused(40, reason))
                 for _ in range(4)]
        self.assertEqual(unevaluable_reason(gates), reason)

    def test_a_corpus_that_priced_anything_is_a_research_verdict(self):
        reason = "no fresh equity quote at entry"
        gates = [_gate(_refused(80, reason), _refused(40, reason)),
                 # One lane priced four trades: the corpus is evaluable, and
                 # every remaining failure is the gates doing their job.
                 _gate(_priced(4), _priced(2))]
        self.assertIsNone(unevaluable_reason(gates))

    def test_a_run_with_no_opportunities_at_all_is_not_this_failure(self):
        # An empty corpus is already caught by the dataset guards upstream;
        # claiming it here would mask a different problem.
        self.assertIsNone(unevaluable_reason([_gate([], [])]))

    def test_it_reads_a_factory_gate_nested_under_verified_gate(self):
        reason = "no fresh equity quote at entry"
        nested = {"verified_gate": _gate(_refused(9, reason), _refused(4, reason))}
        self.assertEqual(unevaluable_reason([nested]), reason)

    def test_refusals_without_a_recorded_reason_still_surface(self):
        rows = [{"vehicle": "equity", "opportunity_id": f"x{i}",
                 "session_date": "2026-03-02", "net_pnl": 0.0, "no_trade": True}
                for i in range(5)]
        self.assertIn("without a recorded reason",
                      unevaluable_reason([_gate(rows, rows)]) or "")

    def test_malformed_gates_are_ignored_rather_than_crashing(self):
        self.assertIsNone(unevaluable_reason([None, {}, {"fill_quality": 7}]))


class CycleExitContractTests(unittest.TestCase):
    """The exit code the scheduled cycle maps to ``no_data``."""

    def test_unevaluable_exit_is_distinct_from_success_and_no_edge(self):
        self.assertNotIn(research_cli.UNEVALUABLE_EXIT, (0, 2))

    def test_reporting_marks_the_result_and_returns_true(self):
        reason = "no fresh equity quote at entry"
        result: dict = {}
        stalled = research_cli._report_unevaluable(
            result, [_gate(_refused(9, reason), _refused(4, reason))])
        self.assertTrue(stalled)
        self.assertEqual(result["unevaluable"]["reason"], reason)

    def test_an_evaluable_run_is_left_untouched(self):
        result: dict = {}
        self.assertFalse(
            research_cli._report_unevaluable(result, [_gate(_priced(3), _priced(2))]))
        self.assertNotIn("unevaluable", result)


if __name__ == "__main__":
    unittest.main()


class RealReplayReproductionTests(unittest.TestCase):
    """The end-to-end path, driven by the real replay rather than fixtures.

    Bars in, no quotes, strict policy: every opportunity is materialised and
    refused, which is precisely what a ``--quotes``-less backfill produces.
    """

    @staticmethod
    def _corpus() -> list[dict]:
        from datetime import datetime, timedelta, timezone
        from zoneinfo import ZoneInfo

        # A mechanical opening-range breakout, so a signal certainly fires and
        # the only thing standing between it and a fill is the missing quote.
        closes = ([100.60 - .05 * step for step in range(1, 16)] +
                  [100.85, 101.05, 101.25, 101.45] +
                  [101.45 - .10 * step for step in range(1, 16)])
        volumes = [1000] * 15 + [6000] + [3000] * 3 + [1200] * 15
        rows: list[dict] = []
        stamp = datetime(2026, 1, 5, 9, 30, tzinfo=ZoneInfo("America/New_York"))
        day = 0
        while day < 12:
            if stamp.weekday() < 5:
                opening = stamp.astimezone(timezone.utc)
                for symbol in ("SPY", "QQQ"):
                    previous = 100.60
                    for index, (close, volume) in enumerate(zip(closes, volumes)):
                        start = opening + timedelta(minutes=index)
                        end = start + timedelta(minutes=1)
                        rows.append({
                            "kind": "bar", "provider": "test", "feed": "iex",
                            "symbol": symbol, "timestamp": start.isoformat(),
                            # Mechanics corpus: opening prints are visible at
                            # their boundaries; delayed recorder bars have
                            # separate temporal acceptance coverage.
                            "as_of": start.isoformat(), "observed_at": start.isoformat(),
                            "open": round(previous, 4),
                            "high": round(max(previous, close) + .02, 4),
                            "low": round(min(previous, close) - .02, 4),
                            "close": round(close, 4), "volume": volume})
                        previous = close
                day += 1
            stamp += timedelta(days=1)
        return rows

    def _account(self, policy, *, corpus=None):
        from agent.contracts.rule import validate_rule_spec
        from research.edge_lab import _read_discovery_rows
        from research.factory_core import simulate_account

        spec = validate_rule_spec({"family": "opening_range_breakout",
                                   "range_minutes": 15, "threshold_bps": 5.0,
                                   "confirmation": "volume"})
        _raw, bars, _snapshots, quotes = _read_discovery_rows(
            self._corpus() if corpus is None else corpus)
        return simulate_account(bars, [], spec, vehicle="equity",
                                account_id="unpriceable", quotes=quotes,
                                policy=policy)

    def test_strict_replay_on_a_bars_only_corpus_prices_nothing(self):
        rows = self._account(ReplayPolicy())["rows"]
        self.assertTrue(rows, "the fixture must materialise opportunities")
        self.assertEqual([row for row in rows if row.get("no_trade") is not True], [])
        self.assertTrue(all(row.get("execution_disposition") == "refused"
                            for row in rows))
        self.assertTrue(all(row.get("reject_stage") == "entry_pricing"
                            for row in rows))

        summary = fill_source_summary(rows, vehicle="equity")
        self.assertTrue(summary["priced_nothing"])
        self.assertEqual(summary["dominant_reject_reason"],
                         "no fresh equity quote at entry")

        # The whole point: this reaches the cycle as a named data problem.
        gate = {"fill_quality": {"fit": summary, "heldout": summary}}
        self.assertEqual(unevaluable_reason([gate]),
                         "no fresh equity quote at entry")
        result: dict = {}
        self.assertTrue(research_cli._report_unevaluable(result, [gate]))

    def test_the_same_corpus_trades_once_strict_pricing_is_relaxed(self):
        # Identical bars, identical rule; only the documented policy changes.
        # Fills come from bar prints marked up by the modelled half-spread.
        rows = self._account(ReplayPolicy(strict_market_data=False))["rows"]
        executed = [row for row in rows if row.get("no_trade") is not True]
        self.assertTrue(executed, "relaxed pricing must be able to fill")

        summary = fill_source_summary(rows, vehicle="equity")
        self.assertFalse(summary["priced_nothing"])
        self.assertEqual(summary["sources"], {BAR: len(executed)})
        self.assertEqual(summary["quoted_fraction"], 0.0)
        self.assertIsNone(unevaluable_reason(
            [{"fill_quality": {"fit": summary, "heldout": summary}}]))

    def test_a_valid_flat_corpus_is_no_signal_not_unpriceable(self):
        corpus = []
        for source in self._corpus():
            row = dict(source)
            row.update({"open": 100.0, "high": 100.01, "low": 99.99,
                        "close": 100.0, "volume": 1000})
            corpus.append(row)
        rows = self._account(ReplayPolicy(), corpus=corpus)["rows"]
        self.assertTrue(rows)
        self.assertTrue(all(row.get("execution_disposition") == "no_signal"
                            for row in rows))
        self.assertTrue(all(row.get("signal_opportunity") is False
                            for row in rows))
        self.assertTrue(all(not row.get("reject_reason") for row in rows))
        summary = fill_source_summary(rows, vehicle="equity")
        self.assertEqual(summary["execution_opportunities"], 0)
        self.assertFalse(summary["priced_nothing"])
        self.assertIsNone(unevaluable_reason(
            [{"fill_quality": {"fit": summary, "heldout": summary}}]))


class CycleScopeTests(unittest.TestCase):
    """One unevaluable vehicle must not abort the others.

    The first version of this check called ``finish`` inline, which exits the
    whole script: an unpriceable equity corpus silently stopped the option lane
    from running at all.  The cycle records the condition per vehicle and only
    reports ``no_data`` when no vehicle reached a verdict.
    """

    @staticmethod
    def _script() -> str:
        return (Path(__file__).parents[2] / "deploy" / "research-cycle.sh").read_text()

    def test_unevaluable_is_recorded_not_fatal(self):
        script = self._script()
        for lane in ("discover", "factory"):
            marker = f'cycle_outcomes+=("$vehicle:{lane}:unevaluable")'
            self.assertIn(marker, script, lane)
        # The exit-4 branches must not terminate the run.
        for block in script.split('elif [ "$status" -eq 4 ]; then')[1:]:
            branch = block.split("else")[0]
            self.assertNotIn("finish ", branch)
            self.assertIn("cycle_unevaluable=1", branch)

    def test_no_data_only_when_nothing_reached_a_verdict(self):
        script = self._script()
        self.assertIn(
            'if [ "$cycle_unevaluable" -eq 1 ] && [ "$cycle_no_edge" -eq 0 ]; then',
            script)
        # And a proof still outranks it.
        self.assertLess(script.index('if [ "$cycle_success" -eq 1 ]; then'),
                        script.index('if [ "$cycle_unevaluable" -eq 1 ]'))
