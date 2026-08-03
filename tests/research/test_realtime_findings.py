"""Persistence, fairness, qualification, and review-gated evidence."""

import argparse
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent import shadow, state, variants
from research import findings, protocol, replay
from tests.helpers import valid_config
from tests.research.test_enrichment_isolation import (execution_enriched,
                                                      symbol_snapshot)


_CLI_SPEC = importlib.util.spec_from_file_location(
    "research_cli_module", Path(__file__).resolve().parents[2] / "research.py")
research_cli = importlib.util.module_from_spec(_CLI_SPEC)
assert _CLI_SPEC.loader is not None
_CLI_SPEC.loader.exec_module(research_cli)


def variant(variant_id="momentum.rr.fixed_2_5", overrides=None):
    return variants.Variant(
        variant_id=variant_id,
        strategy_id="momentum",
        base_version="phase1-v3",
        overrides=(overrides if overrides is not None else
                   {"strategy.fixed_reward_risk": 2.5}),
        hypothesis="A registered parameter setting is tested in real time.",
        status="candidate",
    )


def trade(scope, variant_id, proposal_id, entry_ts):
    return {
        "proposal_id": proposal_id,
        "scope_key": scope,
        "variant_id": variant_id,
        "cycle_id": f"cycle-{proposal_id}",
        "symbol": "BTC/USDT:USDT",
        "direction": "long",
        "setup_type": "trend_continuation",
        "signal_ts": entry_ts * 1000,
        "model_id": "momentum.fixed_rr.15m.v2",
        "assumptions": {"horizon_hours": 48},
        "entry_ts": entry_ts,
        "entry_price": 100.0,
        "notional": 1000.0,
        "risk_usd": 100.0,
        "stop_price": 90.0,
        "take_price": 120.0,
    }


def promotion_analysis_payload(scope, variant_id):
    criteria = {
        "min_round_trips": True,
        "min_axis_settings": True,
        "paired_sample": True,
        "paired_coverage": True,
        "no_duplicate_proposals": True,
        "fit_interval_positive": True,
        "drawdown_no_worse": True,
        "out_of_sample_survives": True,
        "confirmation_interval_positive": True,
    }
    return {
        "source": "real_time_shadow_portfolios",
        "scope_key": scope,
        "strategy_id": "momentum",
        "verdict": protocol.PROMOTE,
        "corpus_from_ts": 1,
        "corpus_to_ts": 200,
        "resolved_outcomes": 200,
        "strategy_config_version": "cfg",
        "code_version": "code",
        "forward_model_id": "momentum.fixed_rr.15m.v2",
        "evidence": {
            "best": variant_id,
            "n": 120,
            "axis_settings": 3,
            "selection_window": "fit",
            "split": {"survives": True},
            "fit_interval": {"point": 0.3, "low": 0.1, "high": 0.5,
                             "n": 84},
            "confirmation_interval": {
                "point": 0.2, "low": 0.05, "high": 0.4, "n": 36},
            "confirm_delta": "+0.2000 [+0.0500,+0.4000] n=36",
            "criteria": criteria,
            "paired": {
                "paired_n": 120,
                "pair_coverage_pct": 100.0,
                "left_duplicates": 0,
                "right_duplicates": 0,
                "bootstrap": {"kind": "paired_cluster_block",
                              "cluster_seconds": 21600},
            },
        },
    }


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "findings.db"
        self.store = findings.FindingsStore(self.path)


class MigrationTests(StoreFixture):
    def test_legacy_v1_rows_migrate_in_place(self):
        legacy = Path(self.tmp.name) / "legacy.db"
        with sqlite3.connect(legacy) as conn:
            conn.executescript("""
                CREATE TABLE variants (
                    variant_id TEXT PRIMARY KEY, strategy_id TEXT,
                    base_version TEXT, overrides_json TEXT, hypothesis TEXT,
                    status TEXT, created_ts REAL, updated_ts REAL);
                CREATE TABLE variant_runs (
                    run_id TEXT PRIMARY KEY, variant_id TEXT,
                    corpus_from_ts REAL, corpus_to_ts REAL,
                    corpus_cycles INTEGER, mode TEXT, code_version TEXT,
                    scorer_version TEXT, ts REAL);
                CREATE TABLE variant_results (
                    run_id TEXT, metric TEXT, value REAL, ci_low REAL,
                    ci_high REAL, n INTEGER);
                CREATE TABLE findings (
                    finding_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    variant_id TEXT, ts REAL, author TEXT, kind TEXT,
                    text TEXT, run_id TEXT);
            """)
            conn.execute(
                "INSERT INTO variants VALUES (?,?,?,?,?,?,?,?)",
                ("momentum.legacy", "momentum", "phase1-v2", "{}",
                 "Legacy hypothesis remains available after migration.",
                 "testing", 1.0, 1.0))
            conn.execute(
                "INSERT INTO findings "
                "(variant_id, ts, author, kind, text, run_id) "
                "VALUES (?,?,?,?,?,NULL)",
                ("momentum.legacy", 2.0, "legacy", "observation",
                 "preserve me"))

        migrated = findings.FindingsStore(legacy)

        self.assertEqual(migrated.schema_version(), findings.SCHEMA_VERSION)
        self.assertEqual(
            [row["version"] for row in migrated.migration_history()],
            list(range(1, findings.SCHEMA_VERSION + 1)))
        self.assertEqual(migrated.variant("momentum.legacy")["status"],
                         "testing")
        self.assertEqual(
            migrated.findings_for("momentum.legacy")[0]["text"],
            "preserve me")

    def test_future_schema_is_refused(self):
        future = Path(self.tmp.name) / "future.db"
        with sqlite3.connect(future) as conn:
            conn.execute(
                "CREATE TABLE schema_meta "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute(
                "INSERT INTO schema_meta VALUES ('schema_version', '999')")

        with self.assertRaises(findings.MigrationError):
            findings.FindingsStore(future).schema_version()

    def test_a_failed_migration_closes_its_connection(self):
        connection = Mock()
        with patch.object(findings.sqlite3, "connect", return_value=connection), \
                patch.object(findings, "_migrate",
                             side_effect=findings.MigrationError("future")):
            with self.assertRaises(findings.MigrationError):
                findings._connect(Path(self.tmp.name) / "broken.db")

        connection.close.assert_called_once_with()

    def test_v6_deduplicates_analyses_and_remaps_every_reference(self):
        legacy = Path(self.tmp.name) / "v5-duplicates.db"
        registered = variant()
        with patch.object(findings, "SCHEMA_VERSION", 5):
            v5 = findings.FindingsStore(legacy)
            self.assertEqual(v5.schema_version(), 5)
            v5.register(registered)

        packet_rows = []
        for packet_id, analysis_id in (
                ("packet-a", "analysis-a"),
                ("packet-b", "analysis-b")):
            payload = {
                "forward_analysis": {"analysis_id": analysis_id},
                "qualification": {"source_analysis_id": analysis_id},
            }
            canonical, payload_hash = findings._t3_canonical(
                registered.variant_id, payload, "DRAFT_REVIEW_REQUIRED",
                None, None)
            packet_rows.append((
                packet_id, registered.variant_id, 1.0,
                "DRAFT_REVIEW_REQUIRED", None, None, canonical, payload_hash))

        with sqlite3.connect(legacy) as conn:
            conn.executemany(
                "INSERT INTO analysis_runs "
                "(analysis_id, kind, subject_id, ts, payload_json) "
                "VALUES (?,?,?,?,?)", [
                    ("analysis-a", "observation", "momentum", 1.0,
                     '{"answer":42}'),
                    ("analysis-b", "observation", "momentum", 2.0,
                     '{"answer":42}'),
                    ("analysis-c", "observation", "reference", 3.0,
                     '{"source_analysis_id":"analysis-b"}'),
                ])
            conn.executemany(
                "INSERT INTO edge_qualification_events "
                "(event_id, variant_id, scope_key, status, ts, "
                "source_analysis_id, detail_json) VALUES (?,?,?,?,?,?,?)", [
                    ("event-a", registered.variant_id, "demo:a", "QUALIFIED",
                     1.0, "analysis-a", '{"analysis_id":"analysis-a"}'),
                    ("event-b", registered.variant_id, "demo:b", "QUALIFIED",
                     2.0, "analysis-b", '{"analysis_id":"analysis-b"}'),
                ])
            conn.executemany(
                "INSERT INTO t3_evidence_packets "
                "(packet_id, variant_id, created_ts, review_status, "
                "reviewed_by, registry_change_ref, payload_json, payload_hash) "
                "VALUES (?,?,?,?,?,?,?,?)", packet_rows)

        migrated = findings.FindingsStore(legacy)

        self.assertEqual(migrated.schema_version(), findings.SCHEMA_VERSION)
        with sqlite3.connect(legacy) as conn:
            conn.row_factory = sqlite3.Row
            analyses = conn.execute(
                "SELECT * FROM analysis_runs ORDER BY analysis_id").fetchall()
            self.assertEqual(
                [row["analysis_id"] for row in analyses],
                ["analysis-a", "analysis-c"])
            self.assertEqual(
                json.loads(analyses[1]["payload_json"])["source_analysis_id"],
                "analysis-a")
            events = conn.execute(
                "SELECT * FROM edge_qualification_events ORDER BY event_id"
            ).fetchall()
            original_events = [row for row in events
                               if row["event_id"] in {"event-a", "event-b"}]
            self.assertEqual(
                {row["source_analysis_id"] for row in original_events},
                {"analysis-a"})
            self.assertEqual(
                {json.loads(row["detail_json"])["analysis_id"]
                 for row in original_events}, {"analysis-a"})
            self.assertEqual(
                {row["status"] for row in events}, {"QUALIFIED", "REVOKED"})
            self.assertTrue(all(
                migrated.qualification_status(
                    registered.variant_id, scope)["status"] == "REVOKED"
                for scope in ("demo:a", "demo:b")))
            self.assertIsNotNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='paper_decisions'").fetchone())
            packets = conn.execute(
                "SELECT * FROM t3_evidence_packets").fetchall()
            self.assertEqual(len(packets), 1)
            packet_payload = json.loads(packets[0]["payload_json"])
            self.assertEqual(
                packet_payload["forward_analysis"]["analysis_id"],
                "analysis-a")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE edge_qualification_events SET status='REVOKED' "
                    "WHERE event_id='event-a'")

    def test_v7_backfills_legacy_accepts_but_starts_a_fresh_ledger(self):
        legacy = Path(self.tmp.name) / "v6-paper.db"
        registered = variant()
        with patch.object(findings, "SCHEMA_VERSION", 6):
            v6 = findings.FindingsStore(legacy)
            v6.register(registered)
            state, version = v6.load_paper_portfolio(
                "demo:legacy", registered.variant_id, now=10)
            state.pop("decision_ledger_started_ts", None)
            v6.save_paper_portfolio(
                "demo:legacy", registered.variant_id, state, version, now=10)
            trade_id = v6.record_paper_trade_open(
                trade("demo:legacy", registered.variant_id, "old", 11))
            v6.close_paper_trade(
                trade_id, exit_ts=12, exit_price=120, result="target",
                net_pnl_usd=100, r_multiple=1.0)

        migrated = findings.FindingsStore(legacy)
        decision = migrated.paper_decisions_for(
            "demo:legacy", registered.variant_id)[0]
        portfolio = migrated.paper_portfolio_state(
            "demo:legacy", registered.variant_id)

        self.assertEqual(migrated.schema_version(), findings.SCHEMA_VERSION)
        self.assertEqual(decision["decision_outcome"], "PROPOSED")
        self.assertEqual(decision["paper_trade_id"], trade_id)
        self.assertIn("veto ledger unavailable", decision["reason"])
        self.assertGreater(
            portfolio["decision_ledger_started_ts"], decision["decision_ts"])


class SchedulerAndPortfolioTests(StoreFixture):
    def setUp(self):
        super().setUp()
        self.a = variant("momentum.rr.fixed_2_5")
        self.b = variant(
            "momentum.stop.atr_1_5",
            {"strategy.min_stop_atr_multiple": 1.5})
        self.store.register(self.a)
        self.store.register(self.b)

    def test_least_observed_variant_moves_to_the_front_and_persists(self):
        scope = "demo:account-a"
        ids = [self.a.variant_id, self.b.variant_id]
        self.assertEqual(self.store.scheduler_order(scope, ids), ids)
        self.store.record_scheduler_cycle(
            scope, [self.a.variant_id], [self.b.variant_id], now=10)

        reopened = findings.FindingsStore(self.path)

        self.assertEqual(
            reopened.scheduler_order(scope, ids)[0], self.b.variant_id)
        coverage = {row["variant_id"]: row
                    for row in reopened.scheduler_coverage(scope)}
        self.assertEqual(coverage[self.a.variant_id]["evaluations"], 1)
        self.assertEqual(coverage[self.b.variant_id]["skips"], 1)

    def test_variant_accounts_are_independent_and_persistent(self):
        scope = "demo:account-a"
        first, version = self.store.load_paper_portfolio(
            scope, self.a.variant_id, now=1)
        self.store.load_paper_portfolio(scope, self.b.variant_id, now=1)
        first["cash_usdt"] = 9000.0
        self.store.save_paper_portfolio(
            scope, self.a.variant_id, first, version, now=2)

        reopened = findings.FindingsStore(self.path)
        changed, _ = reopened.load_paper_portfolio(scope, self.a.variant_id)
        untouched, _ = reopened.load_paper_portfolio(scope, self.b.variant_id)

        self.assertEqual(changed["cash_usdt"], 9000.0)
        self.assertEqual(untouched["cash_usdt"], 10000.0)

    def test_registered_experiment_identity_is_immutable(self):
        changed = variant(
            self.a.variant_id,
            {"strategy.fixed_reward_risk": 3.0})

        with self.assertRaises(ValueError):
            self.store.register(changed)

        persisted = self.store.variant(self.a.variant_id)
        self.assertEqual(
            json.loads(persisted["overrides_json"]), self.a.overrides)

    def test_existing_evidence_refuses_a_new_code_identity(self):
        scope = "demo:identity"
        evaluator = shadow.ShadowEvaluator(
            [self.a], valid_config(), store=self.store, scope_key=scope)
        evaluator.evaluate(
            {"BTC/USDT:USDT": execution_enriched(symbol_snapshot())},
            now=1_760_000_000.0,
            proposals=[{
                "symbol": "BTC/USDT:USDT", "action": "open",
                "direction": "long", "setup_type": "trend_continuation",
                "confidence": 0.8, "invalidation_anchor": "structure",
                "exit_policy": "fixed_rr"}])
        self.assertTrue(
            self.store.paper_trades_for(scope, self.a.variant_id))

        with patch("agent.shadow.runtime_state.code_fingerprint",
                   return_value="different-code"):
            reopened = shadow.ShadowEvaluator(
                [self.a], valid_config(), store=self.store, scope_key=scope)

        self.assertEqual(reopened.variant_ids, [])
        self.assertIn("different or unprovenanced experiment",
                      reopened.registration_errors[self.a.variant_id])

    def test_existing_evidence_refuses_llm_universe_and_cadence_drift(self):
        scope = "demo:executable-config"
        evaluator = shadow.ShadowEvaluator(
            [self.a], valid_config(), store=self.store, scope_key=scope)
        evaluator.evaluate(
            {"BTC/USDT:USDT": execution_enriched(symbol_snapshot())},
            now=1_760_000_000.0,
            proposals=[{
                "symbol": "BTC/USDT:USDT", "action": "open",
                "direction": "long", "setup_type": "trend_continuation",
                "confidence": 0.8, "invalidation_anchor": "structure",
                "exit_policy": "fixed_rr"}])
        self.assertTrue(
            self.store.paper_trades_for(scope, self.a.variant_id))

        changed_configs = []
        llm_changed = valid_config()
        llm_changed["llm"]["model"] = "different-model"
        changed_configs.append(llm_changed)
        universe_changed = valid_config()
        universe_changed["universe"]["top_n"] = 8
        changed_configs.append(universe_changed)
        cadence_changed = valid_config()
        cadence_changed["cycle"]["decision_interval_seconds"] = 900
        changed_configs.append(cadence_changed)

        for changed in changed_configs:
            with self.subTest(changed=changed):
                reopened = shadow.ShadowEvaluator(
                    [self.a], changed, store=self.store, scope_key=scope)
                self.assertEqual(reopened.variant_ids, [])
                self.assertIn(
                    "different or unprovenanced experiment",
                    reopened.registration_errors[self.a.variant_id])

    def test_persistence_boundary_rejects_secret_bearing_provenance(self):
        evaluator = shadow.ShadowEvaluator(
            [self.a], valid_config(), store=self.store,
            scope_key="demo:safe-provenance")
        provenance = json.loads(json.dumps(
            evaluator._provenance[self.a.variant_id]))
        provenance["experiment_config"]["llm"]["api_key"] = "must-not-persist"
        provenance["strategy_config_version"] = findings._content_hash(
            provenance["experiment_config"])[:16]

        with self.assertRaisesRegex(ValueError, "contains secrets"):
            self.store.bind_paper_experiment(
                "demo:secret-provenance", self.a.variant_id, provenance)

        self.assertIsNone(self.store.paper_portfolio_state(
            "demo:secret-provenance", self.a.variant_id))

    def test_analyses_are_content_addressed_and_immutable(self):
        first = self.store.record_analysis(
            "observation", "momentum", {"answer": 42})
        duplicate = self.store.record_analysis(
            "observation", "momentum", {"answer": 42})
        analysis = self.store.analysis(first)

        self.assertEqual(first, duplicate)
        self.assertEqual(len(analysis["payload_hash"]), 64)
        with sqlite3.connect(self.path) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE analysis_runs SET payload_json='{}' "
                    "WHERE analysis_id=?", (first,))

    def test_trade_and_portfolio_mutations_commit_or_roll_back_together(self):
        scope = "demo:account-a"
        state, version = self.store.load_paper_portfolio(
            scope, self.a.variant_id, now=1)
        opened = trade(scope, self.a.variant_id, "atomic", 2)
        opened["trade_id"] = "atomic-trade"
        decision = {
            "decision_id": "atomic-decision",
            "proposal_id": opened["proposal_id"], "scope_key": scope,
            "variant_id": self.a.variant_id, "cycle_id": "atomic-cycle",
            "symbol": opened["symbol"], "direction": opened["direction"],
            "setup_type": opened["setup_type"],
            "signal_ts": opened["signal_ts"], "confidence": 0.9,
            "decision_outcome": "PROPOSED", "paper_trade_id": "atomic-trade",
            "model_id": opened["model_id"],
            "assumptions": opened["assumptions"], "proposal": {},
            "decision_ts": 2,
        }
        state["positions"] = [{"trade_id": "atomic-trade"}]

        with self.assertRaises(RuntimeError):
            self.store.commit_paper_portfolio(
                scope, self.a.variant_id, state, version + 1,
                opened_trades=[opened], decisions=[decision], now=2)
        self.assertEqual(
            self.store.paper_trades_for(scope, self.a.variant_id), [])
        self.assertEqual(
            self.store.paper_decisions_for(scope, self.a.variant_id), [])

        version = self.store.commit_paper_portfolio(
            scope, self.a.variant_id, state, version,
            opened_trades=[opened], decisions=[decision], now=2)
        self.assertEqual(
            len(self.store.paper_decisions_for(scope, self.a.variant_id)), 1)
        closed_state = dict(state, positions=[])
        close = {
            "trade_id": "atomic-trade", "exit_ts": 3, "exit_price": 120,
            "result": "target", "net_pnl_usd": 100, "r_multiple": 1.0,
        }
        with self.assertRaises(RuntimeError):
            self.store.commit_paper_portfolio(
                scope, self.a.variant_id, closed_state, version - 1,
                closed_trades=[close], now=3)

        persisted = self.store.paper_trades_for(scope, self.a.variant_id)[0]
        self.assertEqual(persisted["status"], "OPEN")
        self.assertEqual(
            self.store.paper_portfolio_state(
                scope, self.a.variant_id)["positions"],
            [{"trade_id": "atomic-trade"}])

    def test_zero_work_is_a_skip_not_an_evaluation(self):
        evaluator = shadow.ShadowEvaluator(
            [self.a, self.b], valid_config(), store=self.store,
            scope_key="demo:budget")
        with patch.object(
                shadow.ShadowBudget, "exhausted",
                side_effect=[False, True, True]):
            evaluator.evaluate({"BTC/USDT:USDT": {"price": 100}}, now=10)

        self.assertEqual(evaluator.last_coverage["evaluated"], [])
        self.assertEqual(
            {row["evaluations"] for row in
             self.store.scheduler_coverage("demo:budget")},
            {0})

    def test_paper_summary_excludes_prequalification_shadow_trades(self):
        scope = "demo:account-a"
        state, version = self.store.load_paper_portfolio(
            scope, self.a.variant_id, now=1)
        shadow_trade = self.store.record_paper_trade_open(
            trade(scope, self.a.variant_id, "shadow", 2))
        self.store.close_paper_trade(
            shadow_trade, exit_ts=3, exit_price=120, result="target",
            net_pnl_usd=100, r_multiple=1.0)
        state["status"] = "PAPER"
        state["paper_started_ts"] = 10
        self.store.save_paper_portfolio(
            scope, self.a.variant_id, state, version, now=10)
        paper_trade = self.store.record_paper_trade_open(
            trade(scope, self.a.variant_id, "paper", 11))
        self.store.close_paper_trade(
            paper_trade, exit_ts=12, exit_price=120, result="target",
            net_pnl_usd=200, r_multiple=2.0)

        self.assertEqual(
            self.store.paper_summary(
                scope, self.a.variant_id, paper_only=False)["closed_trades"],
            2)
        paper = self.store.paper_summary(scope, self.a.variant_id)
        self.assertEqual(paper["closed_trades"], 1)
        self.assertEqual(paper["expectancy_r"], 2.0)

    def test_paper_summary_separates_terminal_unfilled_opportunities(self):
        scope = "demo:unfilled-summary"
        self.store.load_paper_portfolio(scope, self.a.variant_id, now=1)
        unfilled = self.store.record_paper_trade_open(
            trade(scope, self.a.variant_id, "unfilled", 2))
        # Legacy rows could have been stored inference-valid before expiry was
        # classified correctly. Result semantics still keep them out of PnL/R.
        self.store.close_paper_trade(
            unfilled, exit_ts=3, exit_price=100, result="unfilled",
            net_pnl_usd=0, r_multiple=0.0)

        summary = self.store.paper_summary(
            scope, self.a.variant_id, paper_only=False)

        self.assertEqual(summary["fill_opportunities"], 1)
        self.assertEqual(summary["unfilled_trades"], 1)
        self.assertEqual(summary["closed_trades"], 0)
        self.assertEqual(summary["invalid_closed_trades"], 0)
        self.assertEqual(summary["net_pnl_usdt"], 0)
        self.assertIsNone(summary["expectancy_r"])

    def test_scorecard_reports_real_time_coverage_and_stage(self):
        scope = "demo:account-a"
        self.store.load_paper_portfolio(scope, self.a.variant_id, now=1)
        self.store.scheduler_order(scope, [self.a.variant_id])
        self.store.record_scheduler_cycle(
            scope, [self.a.variant_id], [], now=2)

        card = findings.scorecard(self.store, self.a.variant_id)

        self.assertIn("Real-time learning and paper", card)
        self.assertIn("demo:account-a", card)
        self.assertIn("SHADOW", card)


class QualificationAndPacketTests(StoreFixture):
    def setUp(self):
        super().setUp()
        self.v = variant()
        self.baseline = variant("momentum.baseline", {})
        self.control = variant(
            "momentum.rr.fixed_1_5",
            {"strategy.fixed_reward_risk": 1.5})
        for item in (self.v, self.baseline, self.control):
            self.store.register(item)

    @staticmethod
    def complete_checklist():
        return {
            "current_g2_pass": True,
            "held_out_confirmation": True,
            "corpus_provenance": True,
            "config_provenance": True,
            "code_provenance": True,
            "forward_sample": True,
            "axis_family_corrected": True,
            "saved_edge_candidate": True,
            "paper_sample": True,
            "paper_positive": True,
            "manual_registry_review": True,
        }

    def assignment_fixture(self, scope, candidates):
        evaluator = shadow.ShadowEvaluator(
            [self.baseline, *candidates], valid_config(),
            store=self.store, scope_key=scope)
        descriptors = {}
        for order, candidate in enumerate(candidates):
            declared = variants.declared_research_setting(candidate)
            code_identity, config_identity = shadow._identity_bundle(
                evaluator._provenance[self.baseline.variant_id],
                evaluator._provenance[candidate.variant_id])
            descriptor = {
                **declared,
                "variant_id": candidate.variant_id,
                "source": "static", "priority": order,
                "order_key": f"{order:04d}:{candidate.variant_id}",
                "code_identity": code_identity,
                "config_identity": config_identity,
            }
            descriptor["candidate_key"] = findings._content_hash({
                key: descriptor[key] for key in (
                    "variant_id", "axis", "setting_id", "setting", "source",
                    "code_identity", "config_identity")})
            descriptors[candidate.variant_id] = descriptor
        return evaluator, descriptors

    def start_assignment(
            self, scope, evaluator, descriptor, now,
            retry_of_assignment_id=None):
        del evaluator
        return self.store.ensure_experiment_assignment(
            scope, "momentum", self.baseline.variant_id, [descriptor],
            minimum_duration_seconds=0, minimum_observations=1,
            retry_of_assignment_id=retry_of_assignment_id, now=now)

    def seed_assignment(
            self, assignment, evaluator, baseline_returns, candidate_returns,
            *, baseline_veto=False, candidate_veto=False):
        self.assertEqual(len(baseline_returns), len(candidate_returns))
        decisions = []
        trades = []
        for lane, variant_id, outcomes, vetoed in (
                ("baseline", assignment["baseline_variant_id"],
                 baseline_returns, baseline_veto),
                ("candidate", assignment["candidate_variant_id"],
                 candidate_returns, candidate_veto)):
            assumptions = json.dumps({
                "forward_model": evaluator._models[variant_id].as_dict(),
                "experiment_provenance": evaluator._provenance[variant_id],
            }, sort_keys=True)
            for index, outcome in enumerate(outcomes):
                decision_ts = (float(assignment["started_ts"]) + 1
                               + index * protocol.PAIR_CLUSTER_SECONDS)
                proposal_id = f"{assignment['assignment_id']}:paired:{index}"
                trade_id = (None if vetoed else
                            f"{assignment['assignment_id']}:{lane}:{index}")
                decisions.append({
                    "decision_id": f"{assignment['assignment_id']}:{lane}:d:{index}",
                    "proposal_id": proposal_id,
                    "scope_key": assignment["scope_key"],
                    "variant_id": variant_id,
                    "cycle_id": f"{assignment['assignment_id']}:cycle:{index}",
                    "symbol": "BTC/USDT:USDT", "direction": "long",
                    "setup_type": "trend_continuation", "signal_ts": index,
                    "confidence": 0.9,
                    "decision_outcome": "VETOED" if vetoed else "PROPOSED",
                    "reason": "fixture veto" if vetoed else None,
                    "paper_trade_id": trade_id,
                    "model_id": "momentum.fixed_rr.15m.v2",
                    "assumptions_json": assumptions,
                    "proposal_json": "{}", "decision_ts": decision_ts,
                })
                if not vetoed:
                    trades.append({
                        **decisions[-1], "trade_id": trade_id,
                        "entry_ts": decision_ts, "entry_price": 100.0,
                        "notional": 1000.0, "risk_usd": 100.0,
                        "stop_price": 90.0, "take_price": 120.0,
                        "exit_ts": decision_ts + 1, "exit_price": 120.0,
                        "result": "target" if outcome >= 0 else "stop",
                        "net_pnl_usd": outcome * 100.0,
                        "r_multiple": outcome,
                    })
        with sqlite3.connect(self.path) as conn:
            conn.executemany("""
                INSERT INTO paper_trades (
                    trade_id, proposal_id, scope_key, variant_id, cycle_id,
                    symbol, direction, setup_type, signal_ts, model_id,
                    assumptions_json, entry_ts, entry_price, notional,
                    risk_usd, stop_price, take_price, exit_ts, exit_price,
                    result, net_pnl_usd, r_multiple, status)
                VALUES (
                    :trade_id, :proposal_id, :scope_key, :variant_id, :cycle_id,
                    :symbol, :direction, :setup_type, :signal_ts, :model_id,
                    :assumptions_json, :entry_ts, :entry_price, :notional,
                    :risk_usd, :stop_price, :take_price, :exit_ts, :exit_price,
                    :result, :net_pnl_usd, :r_multiple, 'CLOSED')
            """, trades)
            conn.executemany("""
                INSERT INTO paper_decisions (
                    decision_id, proposal_id, scope_key, variant_id, cycle_id,
                    symbol, direction, setup_type, signal_ts, confidence,
                    decision_outcome, reason, paper_trade_id, model_id,
                    assumptions_json, proposal_json, decision_ts)
                VALUES (
                    :decision_id, :proposal_id, :scope_key, :variant_id,
                    :cycle_id, :symbol, :direction, :setup_type, :signal_ts,
                    :confidence, :decision_outcome, :reason, :paper_trade_id,
                    :model_id, :assumptions_json, :proposal_json, :decision_ts)
            """, decisions)
        return (float(assignment["started_ts"]) + 2
                + len(baseline_returns) * protocol.PAIR_CLUSTER_SECONDS)

    def complete_assignment(self, assignment, now):
        self.store.record_experiment_observations(
            assignment["assignment_id"],
            [{"observation_key": f"{assignment['assignment_id']}:complete"}],
            now=now - 1)
        completed = self.store.maybe_complete_experiment_assignment(
            assignment["assignment_id"], now=now)
        self.assertEqual(completed["status"], "COMPLETED")
        return completed

    def empty_assignment_analysis(self, scope):
        evaluator, descriptors = self.assignment_fixture(
            scope, [self.v, self.control])
        first = self.start_assignment(
            scope, evaluator, descriptors[self.v.variant_id], now=1_000.0)
        self.complete_assignment(first, 1_010.0)
        second = self.start_assignment(
            scope, evaluator, descriptors[self.control.variant_id],
            now=1_100.0)
        self.complete_assignment(second, 1_110.0)
        analysis_id, _ = self.store.record_forward_analysis(
            scope, "momentum", ["strategy.fixed_reward_risk"],
            self.baseline.variant_id,
            [self.v.variant_id, self.control.variant_id])
        return (evaluator, descriptors, first,
                self.store.analysis(analysis_id)["payload"])

    def qualifying_analysis(self, scope):
        evaluator, descriptors = self.assignment_fixture(
            scope, [self.v, self.control])
        baseline_returns = [
            -0.2 if index % 2 else 0.1
            for index in range(protocol.MIN_ROUND_TRIPS)]
        assignment = self.start_assignment(
            scope, evaluator, descriptors[self.v.variant_id], now=1_000.0)
        ended = self.seed_assignment(
            assignment, evaluator, baseline_returns,
            [1.0 if index % 3 else 0.8
             for index in range(protocol.MIN_ROUND_TRIPS)])
        self.complete_assignment(assignment, ended)
        control_assignment = self.start_assignment(
            scope, evaluator, descriptors[self.control.variant_id],
            now=ended + 100.0)
        control_ended = self.seed_assignment(
            control_assignment, evaluator, baseline_returns,
            [0.2 if index % 2 else 0.1
             for index in range(protocol.MIN_ROUND_TRIPS)])
        self.complete_assignment(control_assignment, control_ended)
        analysis_id, verdict = self.store.record_forward_analysis(
            scope, "momentum", ["strategy.fixed_reward_risk"],
            self.baseline.variant_id,
            [self.v.variant_id, self.control.variant_id])
        self.assertEqual(verdict.verdict, protocol.PROMOTE)
        self.assertEqual(verdict.evidence["best"], self.v.variant_id)
        family = protocol.apply_axis_family_correction({
            "strategy.fixed_reward_risk": verdict
        })["strategy.fixed_reward_risk"]
        self.family_analysis_id = self.store.record_analysis(
            "forward_axis_family", f"momentum:{scope}", {
                "strategy_id": "momentum", "scope_key": scope,
                "axes": {"strategy.fixed_reward_risk": {
                    "analysis_id": analysis_id,
                    "verdict_before_correction": verdict.verdict,
                    "verdict": family.verdict,
                    "governing_criterion": family.governing_criterion,
                    "family": family.evidence["family"],
                }},
            })
        return analysis_id

    def test_policy_axis_can_promote_from_explicit_accept_vs_veto_actions(self):
        scope = "demo:confidence-edge"
        low = variant(
            "momentum.conf.floor_0_50", {"risk.min_confidence": 0.50})
        middle = variant(
            "momentum.conf.floor_0_60", {"risk.min_confidence": 0.60})
        for item in (low, middle):
            self.store.register(item)
        evaluator, descriptors = self.assignment_fixture(scope, [low, middle])
        zero = [0.0] * protocol.MIN_ROUND_TRIPS
        assignment = self.start_assignment(
            scope, evaluator, descriptors[low.variant_id], now=1_000.0)
        ended = self.seed_assignment(
            assignment, evaluator, zero,
            [1.0] * protocol.MIN_ROUND_TRIPS, baseline_veto=True)
        self.complete_assignment(assignment, ended)
        middle_assignment = self.start_assignment(
            scope, evaluator, descriptors[middle.variant_id], now=ended + 100.0)
        middle_ended = self.seed_assignment(
            middle_assignment, evaluator, zero,
            [0.25] * protocol.MIN_ROUND_TRIPS, baseline_veto=True)
        self.complete_assignment(middle_assignment, middle_ended)

        _, verdict = self.store.record_forward_analysis(
            scope, "momentum", ["risk.min_confidence"],
            self.baseline.variant_id, [low.variant_id, middle.variant_id])

        self.assertEqual(verdict.verdict, protocol.PROMOTE)
        self.assertEqual(verdict.evidence["best"], low.variant_id)
        self.assertEqual(verdict.evidence["paired"]["paired_n"], 100)
        self.assertEqual(
            verdict.evidence["paired"]["pair_coverage_pct"], 100.0)

    def test_serial_assignment_evidence_keeps_every_attempt_and_own_baseline(self):
        scope = "demo:serial-assignment-evidence"
        evaluator, descriptors = self.assignment_fixture(
            scope, [self.v, self.control])

        first = self.start_assignment(
            scope, evaluator, descriptors[self.v.variant_id], now=1_000.0)
        self.complete_assignment(first, 1_010.0)
        retry = self.start_assignment(
            scope, evaluator, descriptors[self.v.variant_id], now=1_100.0,
            retry_of_assignment_id=first["assignment_id"])
        retry_ended = self.seed_assignment(
            retry, evaluator, [0.0] * protocol.MIN_ROUND_TRIPS,
            [1.0] * protocol.MIN_ROUND_TRIPS)
        self.complete_assignment(retry, retry_ended)

        later = self.start_assignment(
            scope, evaluator, descriptors[self.control.variant_id],
            now=retry_ended + 100.0)
        later_ended = self.seed_assignment(
            later, evaluator, [2.0] * protocol.MIN_ROUND_TRIPS,
            [2.2] * protocol.MIN_ROUND_TRIPS)
        self.complete_assignment(later, later_ended)

        analysis_id, verdict = self.store.record_forward_analysis(
            scope, "momentum", ["strategy.fixed_reward_risk"],
            self.baseline.variant_id,
            [self.v.variant_id, self.control.variant_id])
        analysis_payload = self.store.analysis(analysis_id)["payload"]
        source = analysis_payload["source_evidence"]
        first_setting = source["settings"][self.v.variant_id]["assignments"]
        second_setting = source["settings"][self.control.variant_id]["assignments"]

        self.assertEqual(source["schema"], "paper_decision_evidence.v3")
        self.assertEqual(
            [row["assignment_id"] for row in first_setting],
            [first["assignment_id"], retry["assignment_id"]])
        self.assertEqual(first_setting[1]["attempt"], 2)
        self.assertEqual(
            first_setting[1]["retry_of_assignment_id"], first["assignment_id"])
        self.assertEqual(
            [row["assignment_id"] for row in second_setting],
            [later["assignment_id"]])
        self.assertEqual(verdict.evidence["best"], self.v.variant_id)

        proposal_sets = []
        for setting in (first_setting, second_setting):
            for assignment in setting:
                baseline_ids = {
                    row["proposal_id"]
                    for row in assignment["baseline"]["decisions"]}
                candidate_ids = {
                    row["proposal_id"]
                    for row in assignment["candidate"]["decisions"]}
                self.assertEqual(baseline_ids, candidate_ids)
                if baseline_ids:
                    proposal_sets.append(baseline_ids)
        self.assertTrue(proposal_sets[0].isdisjoint(proposal_sets[1]))

        tampered = json.loads(json.dumps(analysis_payload))
        tampered_source = tampered["source_evidence"]
        tampered_source["settings"][self.v.variant_id]["assignments"] = [
            tampered_source["settings"][self.v.variant_id]["assignments"][1]]
        tampered_source["sha256"] = (
            findings.FindingsStore._forward_v3_source_hash(tampered_source))
        with findings._connect(self.path) as conn:
            with self.assertRaisesRegex(ValueError, "every eligible assignment"):
                findings.FindingsStore._forward_verdict_from_payload(
                    conn, tampered)

    def test_forward_analysis_rejects_matching_unfinished_attempts(self):
        scope = "demo:unfinished-assignment-evidence"
        evaluator, descriptors, first, payload = (
            self.empty_assignment_analysis(scope))
        cutoff = payload["source_evidence"]["completed_through_ts"]
        active = self.start_assignment(
            scope, evaluator, descriptors[self.v.variant_id], now=cutoff,
            retry_of_assignment_id=first["assignment_id"])
        self.assertEqual(active["status"], "OBSERVING")
        message = (
            rf"{active['assignment_id']}.*unfinished at status OBSERVING")

        with self.assertRaisesRegex(ValueError, message):
            self.store.record_forward_analysis(
                scope, "momentum", ["strategy.fixed_reward_risk"],
                self.baseline.variant_id,
                [self.v.variant_id, self.control.variant_id])
        with findings._connect(self.path) as conn:
            with self.assertRaisesRegex(ValueError, message):
                findings.FindingsStore._forward_verdict_from_payload(
                    conn, payload)

    def test_forward_analysis_rejects_matching_rejected_attempts(self):
        scope = "demo:rejected-assignment-evidence"
        evaluator, descriptors, first, payload = (
            self.empty_assignment_analysis(scope))
        cutoff = payload["source_evidence"]["completed_through_ts"]
        rejected = self.start_assignment(
            scope, evaluator, descriptors[self.v.variant_id], now=cutoff,
            retry_of_assignment_id=first["assignment_id"])
        rejected = self.store.reject_experiment_assignment(
            rejected["assignment_id"], "market data gap", now=cutoff)
        self.assertEqual(rejected["status"], "REJECTED")
        message = rf"{rejected['assignment_id']}.*REJECTED: market data gap"

        with self.assertRaisesRegex(ValueError, message):
            self.store.record_forward_analysis(
                scope, "momentum", ["strategy.fixed_reward_risk"],
                self.baseline.variant_id,
                [self.v.variant_id, self.control.variant_id])
        with findings._connect(self.path) as conn:
            with self.assertRaisesRegex(ValueError, message):
                findings.FindingsStore._forward_verdict_from_payload(
                    conn, payload)

    def test_legacy_v2_forward_evidence_remains_recomputable(self):
        analysis_id = self.qualifying_analysis("demo:v2-read-compatibility")
        payload = self.store.analysis(analysis_id)["payload"]
        source = payload["source_evidence"]
        arms = {
            self.baseline.variant_id: {
                "experiment_provenance":
                    source["eligibility"]["baseline_provenance"],
                "decisions": [],
            },
        }
        for variant_id, setting in source["settings"].items():
            arms[variant_id] = {
                "experiment_provenance": source["eligibility"]
                    ["setting_provenances"][variant_id],
                "decisions": [],
            }
            for assignment in setting["assignments"]:
                arms[self.baseline.variant_id]["decisions"].extend(
                    assignment["baseline"]["decisions"])
                arms[variant_id]["decisions"].extend(
                    assignment["candidate"]["decisions"])
        legacy_payload = {
            **payload,
            "source_evidence": {
                "schema": "paper_decision_evidence.v2",
                "baseline_id": self.baseline.variant_id,
                "setting_ids": [self.v.variant_id, self.control.variant_id],
                "arms": arms, "sha256": findings._content_hash(arms),
            },
        }

        with findings._connect(self.path) as conn:
            verdict = findings.FindingsStore._forward_verdict_from_payload(
                conn, legacy_payload)

        self.assertIn(verdict.verdict, {
            protocol.PROMOTE, protocol.CONTINUE, protocol.REJECT,
            protocol.INSUFFICIENT_SAMPLE,
        })

    def test_forward_analysis_rejects_operational_failures_in_evidence_window(self):
        scope = "demo:operational-failure"
        evaluator, descriptors = self.assignment_fixture(
            scope, [self.v, self.control])
        assignment = self.start_assignment(
            scope, evaluator, descriptors[self.v.variant_id], now=1_000.0)
        self.store.record_paper_failure(
            scope, self.v.variant_id, "variant_evaluation_failed", {},
            now=1_001.0)
        self.complete_assignment(assignment, 1_010.0)

        with self.assertRaisesRegex(ValueError, "operational failure"):
            self.store.record_forward_analysis(
                scope, "momentum", ["strategy.fixed_reward_risk"],
                self.baseline.variant_id,
                [self.v.variant_id, self.control.variant_id])

    def test_forward_analysis_excludes_postqualification_decisions(self):
        scope = "demo:prequalification-boundary"
        analysis_id = self.qualifying_analysis(scope)
        self.store.qualify_variant(
            self.v.variant_id, {"reason": "paired edge",
                                "family_analysis_id": self.family_analysis_id,
                                "axis": ["strategy.fixed_reward_risk"]},
            source_analysis_id=analysis_id, scope_key=scope)
        qualification = self.store.qualification_status(
            self.v.variant_id, scope)
        source = self.store.analysis(analysis_id)["payload"]["source_evidence"]
        postqualification = []
        templates = {}
        for setting in source["settings"].values():
            for assignment in setting["assignments"]:
                for lane in ("baseline", "candidate"):
                    arm = assignment[lane]
                    templates.setdefault(
                        arm["variant_id"], arm["decisions"][0])
        for variant_id, decision in templates.items():
            postqualification.append({
                "decision_id": f"postqualification-{variant_id}",
                "proposal_id": "postqualification-proposal",
                "scope_key": scope,
                "variant_id": variant_id,
                "cycle_id": "postqualification-cycle",
                "symbol": "BTC/USDT:USDT",
                "direction": "long",
                "setup_type": "trend_continuation",
                "signal_ts": qualification["ts"] * 1000,
                "confidence": 0.1,
                "decision_outcome": "VETOED",
                "reason": "postqualification action",
                "model_id": decision["model_id"],
                "assumptions_json": json.dumps(
                    decision["assumptions"], sort_keys=True),
                "proposal_json": '{"confidence":0.1}',
                "decision_ts": qualification["ts"] + 1,
            })
        with sqlite3.connect(self.path) as conn:
            conn.executemany("""
                INSERT INTO paper_decisions (
                    decision_id, proposal_id, scope_key, variant_id, cycle_id,
                    symbol, direction, setup_type, signal_ts, confidence,
                    decision_outcome, reason, paper_trade_id, model_id,
                    assumptions_json, proposal_json, decision_ts)
                VALUES (
                    :decision_id, :proposal_id, :scope_key, :variant_id,
                    :cycle_id, :symbol, :direction, :setup_type, :signal_ts,
                    :confidence, :decision_outcome, :reason, NULL, :model_id,
                    :assumptions_json, :proposal_json, :decision_ts)
            """, postqualification)

        replay_id, verdict = self.store.record_forward_analysis(
            scope, "momentum", ["strategy.fixed_reward_risk"],
            self.baseline.variant_id,
            [self.v.variant_id, self.control.variant_id])
        replay = self.store.analysis(replay_id)["payload"]

        self.assertEqual(verdict.verdict, protocol.PROMOTE)
        self.assertEqual(
            replay["source_evidence"]["settings"], source["settings"])
        for setting in replay["source_evidence"]["settings"].values():
            for assignment in setting["assignments"]:
                for lane in ("baseline", "candidate"):
                    self.assertFalse(any(
                        row["decision_id"].startswith("postqualification-")
                        for row in assignment[lane]["decisions"]))

    def test_forward_analysis_rejects_a_mislabeled_axis(self):
        scope = "demo:mislabeled-axis"
        shadow.ShadowEvaluator(
            [self.v, self.baseline, self.control], valid_config(),
            store=self.store, scope_key=scope)

        with self.assertRaisesRegex(ValueError, "declared axis"):
            self.store.record_forward_analysis(
                scope, "momentum", ["risk.min_confidence"],
                self.baseline.variant_id,
                [self.v.variant_id, self.control.variant_id])

    def test_forward_analysis_rejects_mixed_axes(self):
        scope = "demo:mixed-axis"
        stop = variant(
            "momentum.stop.atr_1_5",
            {"strategy.min_stop_atr_multiple": 1.5})
        shadow.ShadowEvaluator(
            [self.baseline, self.v, stop], valid_config(),
            store=self.store, scope_key=scope)

        with self.assertRaisesRegex(ValueError, "declared axis"):
            self.store.record_forward_analysis(
                scope, "momentum", ["strategy.fixed_reward_risk"],
                self.baseline.variant_id, [self.v.variant_id, stop.variant_id])

    def test_forward_analysis_rejects_a_fake_baseline(self):
        scope = "demo:fake-baseline"
        fake = variant("momentum.fake_baseline", {})
        shadow.ShadowEvaluator(
            [fake, self.v, self.control], valid_config(),
            store=self.store, scope_key=scope)

        with self.assertRaisesRegex(ValueError, "forward baseline must be"):
            self.store.record_forward_analysis(
                scope, "momentum", ["strategy.fixed_reward_risk"],
                fake.variant_id, [self.v.variant_id, self.control.variant_id])

    def test_forward_analysis_rejects_non_axis_override_differences(self):
        scope = "demo:extra-override"
        mixed = variant(
            "momentum.rr.mixed",
            {"strategy.fixed_reward_risk": 2.7,
             "risk.min_confidence": 0.55})
        shadow.ShadowEvaluator(
            [self.baseline, mixed, self.control], valid_config(),
            store=self.store, scope_key=scope)

        with self.assertRaisesRegex(ValueError, "declared axis"):
            self.store.record_forward_analysis(
                scope, "momentum", ["strategy.fixed_reward_risk"],
                self.baseline.variant_id,
                [mixed.variant_id, self.control.variant_id])

    def test_forward_analysis_rejects_non_axis_runtime_config_drift(self):
        scope = "demo:runtime-drift"
        shadow.ShadowEvaluator(
            [self.baseline, self.control], valid_config(),
            store=self.store, scope_key=scope)
        changed = valid_config()
        changed["llm"]["model"] = "different-model"
        shadow.ShadowEvaluator(
            [self.v], changed, store=self.store, scope_key=scope)

        with self.assertRaisesRegex(ValueError, "non-axis executable"):
            self.store.record_forward_analysis(
                scope, "momentum", ["strategy.fixed_reward_risk"],
                self.baseline.variant_id,
                [self.v.variant_id, self.control.variant_id])

    def test_forward_analysis_rejects_mixed_strategy_versions(self):
        scope = "demo:mixed-version"
        old = variants.Variant(
            variant_id="momentum.rr.old_version", strategy_id="momentum",
            base_version="phase1-v2",
            overrides={"strategy.fixed_reward_risk": 2.8},
            hypothesis="An old strategy version cannot join a current axis.")
        shadow.ShadowEvaluator(
            [self.baseline, old, self.control], valid_config(),
            store=self.store, scope_key=scope)

        with self.assertRaisesRegex(ValueError, "strategy versions"):
            self.store.record_forward_analysis(
                scope, "momentum", ["strategy.fixed_reward_risk"],
                self.baseline.variant_id,
                [old.variant_id, self.control.variant_id])

    def complete_payload(self):
        scope = "demo:a"
        analysis_id = self.qualifying_analysis(scope)
        self.store.qualify_variant(
            self.v.variant_id, {"reason": "paired edge",
                                "family_analysis_id": self.family_analysis_id,
                                "axis": ["strategy.fixed_reward_risk"]},
            source_analysis_id=analysis_id, scope_key=scope)
        state, version = self.store.load_paper_portfolio(
            scope, self.v.variant_id, now=9)
        state["status"] = "PAPER"
        state["resume_status"] = "PAPER"
        state["paper_started_ts"] = 10
        self.store.save_paper_portfolio(
            scope, self.v.variant_id, state, version, now=10)
        rows = []
        for index in range(protocol.MIN_ROUND_TRIPS):
            rows.append({
                "trade_id": f"paper-{index}",
                "proposal_id": f"proposal-{index}",
                "scope_key": scope,
                "variant_id": self.v.variant_id,
                "cycle_id": f"cycle-{index}",
                "symbol": "BTC/USDT:USDT",
                "direction": "long",
                "setup_type": "trend_continuation",
                "signal_ts": index,
                "model_id": "momentum.fixed_rr.15m.v2",
                "assumptions_json": "{}",
                "entry_ts": 11 + index,
                "entry_price": 100.0,
                "notional": 1000.0,
                "risk_usd": 100.0,
                "stop_price": 90.0,
                "take_price": 120.0,
                "exit_ts": 12 + index,
                "exit_price": 120.0,
                "result": "target",
                "net_pnl_usd": 100.0,
                "r_multiple": 1.0,
            })
        with sqlite3.connect(self.path) as conn:
            conn.executemany("""
                INSERT INTO paper_trades (
                    trade_id, proposal_id, scope_key, variant_id, cycle_id,
                    symbol, direction, setup_type, signal_ts, model_id,
                    assumptions_json, entry_ts, entry_price, notional,
                    risk_usd, stop_price, take_price, exit_ts, exit_price,
                    result, net_pnl_usd, r_multiple, status)
                VALUES (
                    :trade_id, :proposal_id, :scope_key, :variant_id, :cycle_id,
                    :symbol, :direction, :setup_type, :signal_ts, :model_id,
                    :assumptions_json, :entry_ts, :entry_price, :notional,
                    :risk_usd, :stop_price, :take_price, :exit_ts, :exit_price,
                    :result, :net_pnl_usd, :r_multiple, 'CLOSED')
            """, rows)
        analysis = self.store.analysis(analysis_id)
        assignment_ids = research_cli._forward_assignment_ids(
            analysis, self.v.variant_id)
        edge_candidates = [
            item for item in self.store.edge_candidates_for(
                variant_id=self.v.variant_id, scope_key=scope)
            if item["assignment_id"] in assignment_ids]
        return {
            "variant_id": self.v.variant_id,
            "scope_key": scope,
            "checklist": self.complete_checklist(),
            "g2": {
                "status": "PASS", "strategy_config_version": "cfg",
                "fidelity_code_version": "fidelity"},
            "qualification": self.store.qualification_status(
                self.v.variant_id, scope),
            "forward_analysis": analysis,
            "edge_candidates": edge_candidates,
            "paper_result": self.store.paper_summary(
                scope, self.v.variant_id),
            "current_provenance": {
                "strategy_config_version": "cfg",
                "code_version": "code",
                "fidelity_code_version": "fidelity"},
        }

    def test_qualification_is_scope_specific_and_append_only(self):
        analysis_id = self.qualifying_analysis("demo:a")
        event_id = self.store.qualify_variant(
            self.v.variant_id, {"reason": "paired edge",
                                "family_analysis_id": self.family_analysis_id,
                                "axis": ["strategy.fixed_reward_risk"]},
            source_analysis_id=analysis_id, scope_key="demo:a")

        self.assertEqual(
            self.store.qualification_status(
                self.v.variant_id, "demo:a")["event_id"], event_id)
        self.assertIsNone(
            self.store.qualification_status(self.v.variant_id, "demo:b"))

    def test_qualified_edge_starts_a_clean_isolated_paper_account(self):
        scope = "demo:a"
        evaluator = shadow.ShadowEvaluator(
            [self.v], valid_config(), store=self.store, scope_key=scope)
        analysis_id = self.qualifying_analysis(scope)
        self.store.qualify_variant(
            self.v.variant_id, {"reason": "paired edge",
                                "family_analysis_id": self.family_analysis_id,
                                "axis": ["strategy.fixed_reward_risk"]},
            source_analysis_id=analysis_id, scope_key=scope)

        evaluator.evaluate({}, {}, now=100)
        state = self.store.paper_portfolio_state(scope, self.v.variant_id)

        self.assertEqual(state["status"], "PAPER")
        self.assertEqual(state["paper_started_ts"], 100)
        self.assertEqual(state["cash_usdt"], 10000.0)
        self.assertEqual(state["positions"], [])

    def test_qualification_cannot_bypass_the_promotion_protocol(self):
        with self.assertRaises(ValueError):
            self.store.qualify_variant(
                self.v.variant_id, {"reason": "manual bypass"},
                scope_key="demo:a")
        with self.assertRaises(ValueError):
            self.store.record_analysis(
                "forward_parameter_axis", "demo:a:axis",
                {"verdict": "PROMOTE"})

    def test_qualification_cannot_bypass_family_correction_with_a_boolean(self):
        scope = "demo:family-bypass"
        analysis_id = self.qualifying_analysis(scope)
        with self.assertRaisesRegex(ValueError, "forward_axis_family"):
            self.store.qualify_variant(
                self.v.variant_id,
                {"axis": ["strategy.fixed_reward_risk"],
                 "family": {"significant": True}},
                source_analysis_id=analysis_id, scope_key=scope)

    def test_family_correction_must_cover_every_active_axis(self):
        self.store.register(variant(
            "momentum.conf.floor_0_60",
            {"risk.min_confidence": 0.60}))
        scope = "demo:incomplete-family"
        analysis_id = self.qualifying_analysis(scope)
        with self.assertRaisesRegex(ValueError, "complete active axis set"):
            self.store.qualify_variant(
                self.v.variant_id,
                {"axis": ["strategy.fixed_reward_risk"],
                 "family_analysis_id": self.family_analysis_id},
                source_analysis_id=analysis_id, scope_key=scope)

    def test_t3_packets_are_deterministic_review_gated_and_immutable(self):
        payload = self.complete_payload()
        packet_id = self.store.create_t3_packet(
            self.v.variant_id, payload, reviewed_by="reviewer",
            registry_change_ref="change-123")
        duplicate_id = self.store.create_t3_packet(
            self.v.variant_id, payload, reviewed_by="reviewer",
            registry_change_ref="change-123")
        packet = self.store.t3_packets_for(self.v.variant_id)[0]

        self.assertEqual(packet_id, duplicate_id)
        self.assertEqual(packet["review_status"], "REVIEWED")
        self.assertEqual(len(packet["payload_hash"]), 64)
        self.assertEqual(packet_id, packet["payload_hash"][:32])
        other_reviewer = self.store.create_t3_packet(
            self.v.variant_id, payload, reviewed_by="second-reviewer",
            registry_change_ref="change-123")
        self.assertNotEqual(packet_id, other_reviewer)
        with sqlite3.connect(self.path) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE t3_evidence_packets SET review_status='DRAFT' "
                    "WHERE packet_id=?", (packet_id,))

    def test_incomplete_packet_remains_a_draft_even_with_a_reviewer(self):
        packet_id = self.store.create_t3_packet(
            self.v.variant_id,
            {"checklist": {"current_g2_pass": True}},
            reviewed_by="reviewer", registry_change_ref="change-123")
        packet = self.store.t3_packets_for(self.v.variant_id)[0]

        self.assertEqual(packet["packet_id"], packet_id)
        self.assertEqual(packet["review_status"], "DRAFT_REVIEW_REQUIRED")

    def test_prepare_review_artifacts_is_restart_safe_and_manual_only(self):
        payload = self.complete_payload()
        scope = payload["scope_key"]
        cfg = valid_config()
        journal = Path(self.tmp.name) / "journal.db"
        args = argparse.Namespace(
            store=str(self.path), scope=scope, db=str(journal), mode="demo")
        g2 = {
            "gate": "G2",
            "status": "PASS",
            "proposal_count": 0,
            "max_proposal_ts": 0.0,
            "strategy_config_version": state.strategy_fingerprint(cfg),
            "fidelity_code_version": replay.fidelity_code_fingerprint(),
        }
        protected = [
            research_cli.REPO / "research" / "variants.yaml",
            research_cli.REPO / "config.yaml",
        ]
        before = {path: path.read_bytes() for path in protected}

        with patch.object(research_cli, "_load_config", return_value=cfg), \
                patch.object(research_cli, "_latest_g2_payload",
                             return_value=None):
            self.assertEqual(research_cli.cmd_prepare_review_artifacts(args), 0)
        self.assertEqual(self.store.t3_packets_for(self.v.variant_id), [])

        with patch.object(research_cli, "_load_config", return_value=cfg), \
                patch.object(research_cli, "_latest_g2_payload",
                             return_value=g2):
            self.assertEqual(research_cli.cmd_prepare_review_artifacts(args), 0)

        reopened = findings.FindingsStore(self.path)
        packets = reopened.t3_packets_for(self.v.variant_id)
        self.assertEqual(len(packets), 1)
        packet = packets[0]
        self.assertEqual(packet["review_status"], "DRAFT_REVIEW_REQUIRED")
        self.assertIsNone(packet["reviewed_by"])
        self.assertIsNone(packet["registry_change_ref"])
        self.assertFalse(
            packet["payload"]["checklist"][findings.T3_MANUAL_CHECK])
        self.assertTrue(all(
            packet["payload"]["checklist"][name]
            for name in findings.T3_REQUIRED_CHECKS
            if name != findings.T3_MANUAL_CHECK))

        with patch.object(research_cli, "_load_config", return_value=cfg), \
                patch.object(research_cli, "_latest_g2_payload",
                             return_value=g2):
            self.assertEqual(research_cli.cmd_prepare_review_artifacts(args), 0)
        self.assertEqual(
            len(findings.FindingsStore(self.path).t3_packets_for(
                self.v.variant_id)), 1)
        self.assertEqual(
            findings.FindingsStore(self.path).variant(
                self.v.variant_id)["status"], "candidate")
        self.assertEqual(
            {path: path.read_bytes() for path in protected}, before)

    def test_checklist_booleans_cannot_fake_a_reviewed_packet(self):
        payload = {
            "variant_id": self.v.variant_id,
            "scope_key": "demo:a",
            "checklist": self.complete_checklist(),
            "g2": {"status": "PASS"},
        }
        packet_id = self.store.create_t3_packet(
            self.v.variant_id, payload, reviewed_by="reviewer",
            registry_change_ref="change-123")
        packet = next(
            row for row in self.store.t3_packets_for(self.v.variant_id)
            if row["packet_id"] == packet_id)

        self.assertEqual(packet["review_status"], "DRAFT_REVIEW_REQUIRED")

    def test_t3_packet_family_evidence_cannot_be_faked_by_checklist(self):
        payload = self.complete_payload()
        payload["checklist"]["axis_family_corrected"] = True
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT INTO edge_qualification_events "
                "(event_id, variant_id, scope_key, status, ts, "
                "source_analysis_id, detail_json) VALUES (?,?,?,?,?,?,?)",
                ("missing-family", self.v.variant_id, "demo:a", "QUALIFIED",
                 payload["qualification"]["ts"] + 1.0,
                 payload["qualification"]["source_analysis_id"],
                 json.dumps({"axis": ["strategy.fixed_reward_risk"]})))
        payload["qualification"] = self.store.qualification_status(
            self.v.variant_id, "demo:a")
        packet_id = self.store.create_t3_packet(
            self.v.variant_id, payload, reviewed_by="reviewer",
            registry_change_ref="change-123")
        packet = next(
            row for row in self.store.t3_packets_for(self.v.variant_id)
            if row["packet_id"] == packet_id)
        self.assertEqual(packet["review_status"], "DRAFT_REVIEW_REQUIRED")


class ForwardQualificationCommandTests(StoreFixture):
    def test_report_uses_configured_store_when_cli_store_is_omitted(self):
        configured = Path(self.tmp.name) / "configured" / "findings.db"
        out = Path(self.tmp.name) / "scorecards"
        cfg = valid_config()
        cfg["research"] = {"findings_store": str(configured)}
        args = argparse.Namespace(store=None, out=str(out))

        with patch.object(research_cli, "_load_config", return_value=cfg):
            result = research_cli.cmd_report(args)

        self.assertEqual(result, 0)
        self.assertTrue(configured.exists())
        snapshots = configured.with_name("findings.db.snapshots")
        self.assertTrue(any(snapshots.glob("*.db")))

    def test_relative_configured_store_is_repo_relative(self):
        self.assertEqual(
            findings.resolve_store_path("research/cache/custom.db"),
            Path(__file__).resolve().parents[2]
            / "research" / "cache" / "custom.db")

    def test_unrelated_parameter_axes_are_never_pooled(self):
        scope = "demo:account-a"
        registry = variants.load_registry("research/variants.yaml")
        active = [item for item in registry.values()
                  if item.status in {"candidate", "testing"}]
        shadow.ShadowEvaluator(
            active, valid_config(), store=self.store, scope_key=scope)
        calls = []

        def record_axis(
                _store, _scope, _strategy, axis, _baseline_id, setting_ids):
            calls.append((list(axis), list(setting_ids)))
            return (f"analysis-{len(calls)}", protocol.Verdict(
                protocol.CONTINUE, "test", "collecting", {}))

        args = argparse.Namespace(
            store=str(self.path), scope=scope, strategy="momentum")
        with patch.object(
                findings.FindingsStore, "record_forward_analysis",
                autospec=True, side_effect=record_axis), patch.object(
                findings.FindingsStore, "analysis",
                return_value={"payload": {"portfolio_statuses": {}}}):
            result = research_cli.cmd_forward_qualify(args)

        self.assertEqual(result, 0)
        self.assertGreaterEqual(len(calls), 4)
        for axis, ids in calls:
            registered_ids = [variant_id for variant_id in ids
                              if variant_id in registry]
            paths = {
                tuple(sorted(registry[variant_id].overrides))
                for variant_id in registered_ids
            }
            if paths:
                self.assertEqual(paths, {tuple(sorted(axis))}, ids)
            else:
                hypothesis_ids = {
                    variant_id.split(".")[2] for variant_id in ids}
                self.assertEqual(len(hypothesis_ids), 1, ids)
        self.assertFalse(any(
            "momentum.rr.fixed_3_0" in ids for _, ids in calls))


if __name__ == "__main__":
    unittest.main()
