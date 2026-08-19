"""Regression coverage for the explicit bars-only backtest lane.

The recorder's historical corpus is not guaranteed to contain an equity quote
at every bar boundary.  A backtest may opt into the documented bar fallback,
but a forward shadow run must remain strict: it is an executable-parity check,
not another opportunity to manufacture a fill.  These tests use the earned
edge corpus so the distinction is observable in the same gate/account path
used by the factory rather than in a test-only pricing stub.
"""

from pathlib import Path
import tempfile
import unittest
from argparse import Namespace
import importlib.util
from unittest.mock import patch

from agent.config import ConfigError, validate_config
from research.costs import (ReplayPolicy, diagnostic_backfill_policy,
                            replay_policy_for_mode)
from research.edge_lab import discover
import research.gates as gates
from research.ibr import IBRConfig
from research.live_shadow import _policy as shadow_policy
import research.strategy_factory as factory_module
from research.strategy_factory import run_factory

from tests.research.test_factory_end_to_end import SESSIONS, edge_corpus


_CLI_PATH = Path(__file__).parents[2] / "research.py"
_CLI_SPEC = importlib.util.spec_from_file_location("research_lane_cli_test", _CLI_PATH)
research_cli = importlib.util.module_from_spec(_CLI_SPEC)
assert _CLI_SPEC.loader is not None
_CLI_SPEC.loader.exec_module(research_cli)


def bars_only(rows):
    """Return a boundary-visible bars-only corpus for this lane suite.

    ``edge_corpus`` intentionally models recorder output: its bars become
    observable at the bar end.  The tests below exercise the documented,
    mechanics-only backtest bar fallback instead, so copy each retained bar
    and expose it at its own timestamp.  Recorder-style delayed bars remain
    covered by the separate entry-visibility tests and are not changed here.
    """
    visible = []
    for row in rows:
        if str(row.get("kind", "bar")).lower() in {
                "quote", "equity_quote", "underlying_quote"}:
            continue
        boundary = dict(row)
        timestamp = boundary.get("timestamp")
        if timestamp is not None:
            boundary["as_of"] = timestamp
            boundary["observed_at"] = timestamp
        visible.append(boundary)
    return visible


def floor_trades(result):
    """Return the largest fit-floor trade count in a factory result."""
    return max(
        (int((row.get("gate") or {}).get("fit_floor", {}).get("trades", 0))
         for row in result.get("results", ())),
        default=0,
    )


class BacktestFallbackConfigTests(unittest.TestCase):
    def test_validated_config_defaults_and_rejects_non_boolean_flag(self):
        self.assertTrue(validate_config({})["research"]["backtest_bar_fallback"])
        for value in (None, 0, 1, "true", []):
            with self.subTest(value=value), self.assertRaisesRegex(
                    ConfigError, r"research\.backtest_bar_fallback"):
                validate_config({"research": {"backtest_bar_fallback": value}})

    def test_cli_backtest_defaults_to_bar_fallback_but_strict_flag_overrides(self):
        parser = research_cli.build_parser()
        permissive_args = parser.parse_args(["backtest-ibr", "bars.jsonl"])
        strict_args = parser.parse_args([
            "backtest-ibr", "bars.jsonl", "--strict-market-data"])
        self.assertFalse(research_cli._config(permissive_args)
                         .policy.strict_market_data)
        self.assertTrue(research_cli._config(strict_args)
                        .policy.strict_market_data)
        self.assertTrue(IBRConfig().policy.strict_market_data)
        self.assertTrue(ReplayPolicy().strict_market_data)


class ResearchCommandLaneConfigTests(unittest.TestCase):
    """CLI callers must forward the validated lane policy explicitly."""

    @staticmethod
    def _agent_config(enabled=False):
        return {
            "costs": {"spread_bps": 2.0, "slippage_bps": 3.0, "fee_bps": .5},
            "execution": {"max_spread_bps": 100.0, "max_slippage_bps": 50.0},
            "research": {"backtest_bar_fallback": enabled,
                         "strategy_llm": {"enabled": False}},
        }

    def test_edge_discover_forwards_validated_research_flag(self):
        args = Namespace(
            agent_config="config.yaml", config=None, data="market.jsonl",
            db="edge.sqlite3", vehicle="equity", lane="backtest",
            variants=None, min_trades=1, min_sessions=1, alpha=.05)
        captured = {}
        with patch.object(research_cli, "_agent_config",
                          return_value=self._agent_config(False)), \
             patch.object(research_cli, "discover",
                          side_effect=lambda *_args, **kwargs:
                          (captured.update(kwargs) or {"variants": []})), \
             patch.object(research_cli, "_emit_proofs", return_value=False), \
             patch.object(research_cli, "print"):
            self.assertEqual(research_cli.cmd_edge_discover(args), 2)
        self.assertFalse(captured["backtest_bar_fallback"])
        self.assertEqual(captured["config"]["costs"]["spread_bps"], 2.0)
        self.assertEqual(
            captured["config"]["execution"]["max_slippage_bps"], 50.0)

    def test_factory_run_forwards_validated_research_flag(self):
        args = Namespace(
            agent_config="config.yaml", config=None, data="market.jsonl",
            db="edge.sqlite3", vehicle="equity", strategies=1, variants=2,
            workers=1, starting_cash=100_000.0, min_trades=1,
            min_sessions=1, alpha=.05, max_generations=1,
            worker_data=None)
        captured = {}

        def fake_factory(*_args, **kwargs):
            captured.update(kwargs)
            return {"results": []}

        with patch.object(research_cli, "_agent_config",
                          return_value=self._agent_config(False)), \
             patch.object(research_cli, "_factory_dataset_preflight",
                          return_value={"authorizing": True,
                                        "diagnostic_only": False,
                                        "source": {}}), \
             patch.object(research_cli, "run_factory", side_effect=fake_factory), \
             patch.object(research_cli, "_emit_proofs", return_value=False), \
             patch.object(research_cli, "_write_factory_report", return_value=None), \
             patch.object(research_cli, "print"):
            self.assertEqual(research_cli.cmd_factory_run(args), 2)
        self.assertFalse(captured["backtest_bar_fallback"])

    def test_live_shadow_forces_strict_market_data(self):
        self.assertTrue(shadow_policy(
            {"execution": {"strict_market_data": False}}
        ).strict_market_data)


class MarketDataLaneBoundaryTests(unittest.TestCase):
    """Backtest fallback is opt-in and cannot bleed into the shadow lane."""

    @classmethod
    def setUpClass(cls):
        # The fixture has a real opening-range edge and enough independent
        # sessions for the factory to earn a backtest pass.  Removing only
        # quote rows leaves the bar corpus otherwise identical to the
        # quote-backed end-to-end proof.
        cls.corpus = bars_only(edge_corpus())
        cls.forward = bars_only(edge_corpus(30, skip=SESSIONS))
        cls.directory = tempfile.TemporaryDirectory(prefix="alpaca-lane-")
        root = Path(cls.directory.name)
        common = {
            "strategies": 1,
            "variants_per_strategy": 2,
            "workers": 1,
            "min_trades": 100,
            "min_sessions": 10,
        }
        # This suite isolates fill-source lane semantics.  Its compact corpus
        # intentionally does not meet production authorization floors, so
        # lower only the imported protocol constants for these calls; the
        # production defaults and rejection boundary remain unchanged.
        with patch.multiple(
                gates,
                PROTOCOL_BACKTEST_MIN_TRADES=1,
                PROTOCOL_BACKTEST_MIN_SESSIONS=1,
                PROTOCOL_BACKTEST_MIN_CLUSTERS=1,
                PROTOCOL_SHADOW_MIN_TRADES=1,
                PROTOCOL_SHADOW_MIN_SESSIONS=1,
                PROTOCOL_SHADOW_MIN_CLUSTERS=1,
                PROTOCOL_QUALIFICATION_MIN_TRADES=1,
                PROTOCOL_QUALIFICATION_MIN_SESSIONS=1,
                PROTOCOL_QUALIFICATION_MIN_CLUSTERS=1), \
                patch.object(factory_module, "MIN_PROMOTION_CLUSTERS", 1):
            cls.strict = run_factory(
                cls.corpus, db_path=root / "strict.sqlite3",
                **common)
            cls.backtest = run_factory(
                cls.corpus, db_path=root / "backtest.sqlite3",
                backtest_bar_fallback=True, **common)
            # Reusing the fallback cycle's ledger makes the next cycle a genuine
            # forward-shadow task.  The flag is deliberately still true here;
            # mode selection, rather than caller forgetfulness, must keep
            # shadow replay strict.
            cls.shadow = run_factory(
                cls.forward, db_path=root / "backtest.sqlite3",
                backtest_bar_fallback=True, **common)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_mode_policy_cannot_make_shadow_permissive(self):
        permissive = replay_policy_for_mode(
            ReplayPolicy(), "backtest", backtest_bar_fallback=True)
        shadow = replay_policy_for_mode(
            ReplayPolicy(), "shadow", backtest_bar_fallback=True)
        self.assertFalse(permissive.strict_market_data)
        self.assertTrue(shadow.strict_market_data)
        diagnostic = diagnostic_backfill_policy()
        self.assertTrue(diagnostic.allow_historical_backfill_diagnostics)
        self.assertFalse(replay_policy_for_mode(
            diagnostic, "backtest", backtest_bar_fallback=True,
        ).allow_historical_backfill_diagnostics)
        self.assertFalse(replay_policy_for_mode(
            diagnostic, "shadow", backtest_bar_fallback=True,
        ).allow_historical_backfill_diagnostics)

    def test_strict_default_refuses_bars_only_but_explicit_backtest_fallback_prices(self):
        self.assertEqual(floor_trades(self.strict), 0)
        # Bar fallback remains visible as replay diagnostics, but contributes
        # zero authorizing trades to structural floors and every downstream
        # inference.
        self.assertEqual(floor_trades(self.backtest), 0)
        fallback_gates = [row.get("gate") or {}
                          for row in self.backtest.get("results", ())]
        self.assertTrue(any(
            int(((gate.get("verified_gate") or {}).get("fill_quality") or {}
                 ).get("fit", {}).get("executed", 0)) > 0
            for gate in fallback_gates))
        self.assertTrue(any(
            int((((gate.get("verified_gate") or {}).get(
                "authorization_projection") or {}).get("fit", {})
                .get("counts", {}).get("eligible", 0))) == 0
            for gate in fallback_gates))
        self.assertTrue(all(row["mode"] == "backtest"
                            for row in self.backtest["results"]))

    def test_edge_discovery_uses_the_same_explicit_backtest_boundary(self):
        # One session is enough to expose the fill-source contract; this test
        # intentionally inspects the replay floor, not a statistical pass.
        corpus = bars_only(edge_corpus(1))
        with tempfile.TemporaryDirectory(prefix="alpaca-discovery-lane-") as directory:
            with patch.multiple(
                    gates,
                    PROTOCOL_BACKTEST_MIN_TRADES=1,
                    PROTOCOL_BACKTEST_MIN_SESSIONS=1,
                    PROTOCOL_BACKTEST_MIN_CLUSTERS=1,
                    PROTOCOL_SHADOW_MIN_TRADES=1,
                    PROTOCOL_SHADOW_MIN_SESSIONS=1,
                    PROTOCOL_SHADOW_MIN_CLUSTERS=1,
                    PROTOCOL_QUALIFICATION_MIN_TRADES=1,
                    PROTOCOL_QUALIFICATION_MIN_SESSIONS=1,
                    PROTOCOL_QUALIFICATION_MIN_CLUSTERS=1):
                strict = discover(
                    corpus, db_path=Path(directory) / "strict.sqlite3",
                    lane="backtest", min_trades=1, min_sessions=1, alpha=1.0)
                fallback = discover(
                    corpus, db_path=Path(directory) / "fallback.sqlite3",
                    lane="backtest", min_trades=1, min_sessions=1, alpha=1.0,
                    backtest_bar_fallback=True)
        self.assertEqual(max(
            int((row.get("gate") or {}).get("floor", {}).get("trades", 0))
            for row in strict["variants"]), 0)
        # Explicit bar fallback remains available as a diagnostic replay, but
        # the authorization-quality projection excludes those bar-priced rows
        # from every structural/performance floor.
        self.assertEqual(max(
            int((row.get("gate") or {}).get("floor", {}).get("trades", 0))
            for row in fallback["variants"]), 0)
        fallback_gates = [row.get("gate") or {}
                          for row in fallback["variants"]]
        self.assertTrue(any(
            int(((gate.get("verified_gate") or {}).get("fill_quality") or {}
                 ).get("fit", {}).get("executed", 0)) > 0
            for gate in fallback_gates))
        self.assertTrue(any(
            int((((gate.get("verified_gate") or {}).get(
                "authorization_projection") or {}).get("fit", {})
                .get("counts", {}).get("eligible", 0))) == 0
            for gate in fallback_gates))

    def test_backtest_fallback_does_not_relax_a_forward_shadow(self):
        shadow_rows = [row for row in self.shadow["results"]
                       if row.get("mode") == "shadow"]
        # A bar-only fallback is diagnostic evidence, not executable proof;
        # it must not seed a forward-shadow authorization task at all.
        self.assertFalse(shadow_rows)

    def test_lane_policy_is_bound_into_experiment_identity_and_provenance(self):
        strict_identity = self.strict["experiment_identity"]
        backtest_identity = self.backtest["experiment_identity"]
        self.assertNotEqual(strict_identity["identity_hash"],
                            backtest_identity["identity_hash"])
        # The exact hash slot is an implementation detail of the provenance
        # envelope; the contract is that the complete recorded provenance is
        # not byte-identical when the lane policy changes.
        self.assertNotEqual(self.strict["experiment_provenance"],
                            self.backtest["experiment_provenance"])


if __name__ == "__main__":
    unittest.main()
