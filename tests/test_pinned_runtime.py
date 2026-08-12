"""Pinning selects an edge; it never authorizes one.

Writing an id into a configuration file must not be able to put an unproved
variant on the book, and it must be the only thing that can take a proved one
off. These tests pin both directions.
"""

from contextlib import closing
import copy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent.config import DEFAULT_CONFIG, validate_config
from agent.alpaca_provider import AlpacaError
from agent.contracts.rule import rule_variant_id
from agent.engine import Engine
from agent.edge import (resolve_pinned_variants, resolve_validated_variants,
                        unresolved_promotions)
from research.edge_lab import EdgeLedger


VARIANT = "rule.orb.aaaabbbbccccdddd"


def _config(entries, **strategy):
    base = copy.deepcopy(DEFAULT_CONFIG)
    base["strategy"] = {**base["strategy"], "selection_mode": "pinned",
                        "pinned": entries, **strategy}
    return validate_config(base)


def _deployed(ledger, variant=VARIANT, *, status="validated", proof=True):
    """A candidate at ``status``, with or without the proof deployment needs."""
    record = ledger.register_candidate(
        variant, strategy_id="rule", vehicle="equity", hypothesis="pinned",
        config={"strategy": {"rule_spec": {"family": "opening_range_breakout"}}})
    with closing(sqlite3.connect(ledger.path)) as db, db:
        db.execute("UPDATE candidate_state SET status=? WHERE candidate_id=?",
                   (status, record["candidate_id"]))
    if proof:
        _stub_proof(ledger, record)
    return record["candidate_id"]


def _stub_proof(ledger, record):
    """Stand in for a re-verified passing shadow run.

    The proof machinery is exercised end-to-end elsewhere; here the subject is
    the pinning boundary, so the proof is supplied rather than earned.
    """
    return {"lane": "shadow", "config_hash": record["config_hash"],
            "verified_gate": {"passes": True,
                              "statistics": {"q_value": 0.01}}}


class PinnedResolutionTests(unittest.TestCase):
    def test_a_pinned_entry_resolves_and_carries_its_promotion(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            record = ledger.register_candidate(
                VARIANT, strategy_id="rule", vehicle="equity",
                hypothesis="pinned",
                config={"strategy": {"rule_spec": {"family": "opening_range_breakout"}}})
            with closing(sqlite3.connect(ledger.path)) as db, db:
                db.execute("UPDATE candidate_state SET status='champion' "
                           "WHERE candidate_id=?", (record["candidate_id"],))
            config = _config([{"id": "pin-orb-may", "variant_id": VARIANT,
                               "vehicle": "equity",
                               "note": "cleared its live trial"}])
            with patch("agent.edge._latest_passing_proof",
                       return_value=_stub_proof(ledger, record)):
                resolved = resolve_pinned_variants(config, db_path=ledger.path)
            self.assertEqual(len(resolved), 1)
            self.assertEqual(resolved[0]["variant_id"], VARIANT)
            self.assertTrue(resolved[0]["pinned"])
            self.assertEqual(resolved[0]["promotion"]["id"], "pin-orb-may")
            self.assertEqual(resolved[0]["promotion"]["note"],
                             "cleared its live trial")

    def test_selection_mode_pinned_routes_through_the_pinned_resolver(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            _deployed(ledger, proof=False)
            config = _config([{"id": "pin-orb", "variant_id": VARIANT,
                               "vehicle": "equity"}])
            with patch("agent.edge.resolve_pinned_variants",
                       return_value=[{"variant_id": VARIANT}]) as resolver:
                result = resolve_validated_variants(config, db_path=ledger.path)
            resolver.assert_called_once()
            self.assertEqual(result, [{"variant_id": VARIANT}])

    def test_pinning_an_unproved_variant_trades_nothing(self):
        """The evidence gate is not a thing configuration can turn off."""
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            for status in ("candidate", "backtest_passed", "shadow",
                           "demoted", "retired"):
                with self.subTest(status=status):
                    variant = f"rule.orb.{status}"
                    _deployed(ledger, variant, status=status, proof=False)
                    config = _config([{"id": f"pin-{status}",
                                       "variant_id": variant,
                                       "vehicle": "equity"}])
                    self.assertEqual(
                        resolve_pinned_variants(config, db_path=ledger.path), [])

    def test_pinning_an_unknown_variant_trades_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            config = _config([{"id": "pin-ghost",
                               "variant_id": "rule.does.not.exist",
                               "vehicle": "equity"}])
            self.assertEqual(
                resolve_pinned_variants(config, db_path=ledger.path), [])

    def test_a_validated_record_without_a_passing_proof_trades_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            _deployed(ledger, proof=False)
            config = _config([{"id": "pin-orb", "variant_id": VARIANT,
                               "vehicle": "equity"}])
            self.assertEqual(
                resolve_pinned_variants(config, db_path=ledger.path), [])

    def test_a_promotion_for_the_other_vehicle_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            config = _config(
                [{"id": "pin-opt", "variant_id": VARIANT, "vehicle": "option"}],
                execution_mode="shares")
            self.assertEqual(
                resolve_pinned_variants(config, db_path=ledger.path), [])


class UnresolvedPromotionTests(unittest.TestCase):
    """A promotion that quietly resolves to nothing is the failure to surface."""

    def test_it_explains_why_each_pin_cannot_trade(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            _deployed(ledger, "rule.orb.shadowed", status="shadow", proof=False)
            _deployed(ledger, "rule.orb.novalid", status="validated", proof=False)
            config = _config([
                {"id": "pin-missing", "variant_id": "rule.orb.ghost",
                 "vehicle": "equity"},
                {"id": "pin-shadow", "variant_id": "rule.orb.shadowed",
                 "vehicle": "equity"},
                {"id": "pin-noproof", "variant_id": "rule.orb.novalid",
                 "vehicle": "equity"},
                {"id": "pin-option", "variant_id": "rule.orb.other",
                 "vehicle": "option"},
            ])
            problems = {item["id"]: item["reason"] for item in
                        unresolved_promotions(config, db_path=ledger.path)}
            self.assertIn("no candidate", problems["pin-missing"])
            self.assertIn("'shadow'", problems["pin-shadow"])
            self.assertIn("shadow proof", problems["pin-noproof"])
            self.assertIn("this trader runs equity", problems["pin-option"])

    def test_a_resolvable_pin_is_not_reported_as_a_problem(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            record = ledger.register_candidate(
                VARIANT, strategy_id="rule", vehicle="equity",
                hypothesis="pinned",
                config={"strategy": {"rule_spec": {"family": "opening_range_breakout"}}})
            with closing(sqlite3.connect(ledger.path)) as db, db:
                db.execute("UPDATE candidate_state SET status='validated' "
                           "WHERE candidate_id=?", (record["candidate_id"],))
            config = _config([{"id": "pin-ok", "variant_id": VARIANT,
                               "vehicle": "equity"}])
            with patch("agent.edge._latest_passing_proof",
                       return_value=_stub_proof(ledger, record)):
                self.assertEqual(
                    unresolved_promotions(config, db_path=ledger.path), [])

    def test_no_promotions_means_nothing_to_report(self):
        base = copy.deepcopy(DEFAULT_CONFIG)
        self.assertEqual(unresolved_promotions(validate_config(base)), [])


class LivePinnedRuntimeTests(unittest.TestCase):
    """A validated promotion stays exact across live startup refreshes."""

    class _LiveProvider:
        paper = False
        endpoint = "https://api.alpaca.markets"

        class Session:
            api_key = "key"
            secret_key = "secret"

        session = Session()

    def test_live_pinned_startup_ignores_competing_champion_and_fails_closed(self):
        from agent import state

        with tempfile.TemporaryDirectory() as directory:
            runtime_base = state.RUNTIME_BASE
            state.RUNTIME_BASE = Path(directory) / "runtime"
            state.configure_runtime("live")
            state.ensure_ready()
            self.addCleanup(lambda: self._restore_runtime(state, runtime_base))

            db_path = Path(directory) / "edge.sqlite3"
            ledger = EdgeLedger(db_path)

            def deploy(variant_id, status, family):
                record = ledger.register_candidate(
                    variant_id, strategy_id="rule", vehicle="equity",
                    hypothesis=variant_id,
                    config={"strategy": {"rule_spec": {"family": family}}})
                with closing(sqlite3.connect(ledger.path)) as db, db:
                    db.execute("UPDATE candidate_state SET status=? WHERE candidate_id=?",
                               (status, record["candidate_id"]))
                return ledger.candidate(record["candidate_id"])

            pinned_variant = rule_variant_id({"family": "opening_range_breakout"})
            competing_variant = rule_variant_id({"family": "mean_reversion"})
            pinned = deploy(pinned_variant, "validated", "opening_range_breakout")
            competing = deploy(competing_variant, "champion", "mean_reversion")

            def proof_for(candidate_id, *, lane=None):
                record = ledger.candidate(candidate_id)
                if record is None:
                    return None
                return {"lane": lane or "shadow",
                        "config_hash": record["config_hash"],
                        "metrics": {"confidence": 1.0},
                        "verified_gate": {"passes": True}}

            with patch.dict("os.environ", {"ALPACA_LIVE_ENABLE": "true"}, clear=False):
                cfg = validate_config({
                    "mode": "live",
                    "broker": {"paper": False, "allow_live": True},
                    "strategy": {
                        "selection_mode": "pinned", "execution_mode": "shares",
                        "pinned": [{"id": "promote-baseline",
                                    "variant_id": pinned_variant,
                                    "vehicle": "equity"}],
                    },
                    "research": {"enabled": True,
                                 "require_validated_variant": True,
                                 "db_path": str(db_path)},
                })
            with patch.dict("os.environ", {"ALPACA_LIVE_ENABLE": "true"}, clear=False), \
                    patch.object(EdgeLedger, "latest_verified_run",
                                 side_effect=proof_for):
                engine = Engine(cfg, light=True, provider=self._LiveProvider())
            self.addCleanup(engine.close)

            self.assertEqual(engine._edge_record["candidate_id"],
                             pinned["candidate_id"])
            self.assertNotEqual(engine._edge_record["candidate_id"],
                                competing["candidate_id"])
            # Startup refresh re-resolves the pin, rather than selecting the
            # stronger champion that an automatic resolver would prefer.
            with patch.object(EdgeLedger, "latest_verified_run",
                              side_effect=proof_for):
                self.assertTrue(engine._refresh_edge(), engine._edge_error)
            self.assertEqual(engine._edge_record["candidate_id"],
                             pinned["candidate_id"])

            # A missing proof blocks the exact pin and cannot fall through to
            # the competing champion.
            with patch.object(EdgeLedger, "latest_verified_run",
                              return_value=None):
                self.assertFalse(engine._refresh_edge())
            self.assertIsNone(engine._edge_record)

    def test_engine_rejects_direct_live_llm_but_preserves_paper_injection(self):
        live_cfg = {
            "mode": "live",
            "broker": {"paper": False, "allow_live": True},
            "strategy": {"id": "ibr", "variant_id": "ibr.baseline",
                         "selection_mode": "specific"},
            "research": {"enabled": True,
                         "require_validated_variant": True},
        }
        with patch.dict("os.environ", {"ALPACA_LIVE_ENABLE": "true"}, clear=False):
            with self.assertRaisesRegex(AlpacaError, "runtime LLM"):
                Engine({**live_cfg, "llm": {"enabled": True}},
                       light=True, provider=self._LiveProvider())
            with self.assertRaisesRegex(AlpacaError, "runtime LLM"):
                Engine(live_cfg, light=True, provider=self._LiveProvider(),
                       brain=object())

        class PaperProvider:
            paper = True
            endpoint = "https://paper-api.alpaca.markets"

            class Session:
                api_key = "key"
                secret_key = "secret"

            session = Session()

        brain = object()
        engine = Engine({
            "mode": "paper", "broker": {"paper": True, "allow_live": False},
            "strategy": {"id": "ibr", "variant_id": "ibr.baseline",
                         "selection_mode": "specific"},
            "llm": {"enabled": False},
        }, light=True, provider=PaperProvider(), brain=brain)
        self.addCleanup(engine.close)
        self.assertIs(engine.brain, brain)

    @staticmethod
    def _restore_runtime(state, runtime_base):
        state.RUNTIME_BASE = runtime_base
        state.configure_runtime("paper")


if __name__ == "__main__":
    unittest.main()
