"""A small, offline golden path through the research lifecycle.

This deliberately uses one recorded event and one proposal.  It proves the
identity boundaries and refusal behavior without manufacturing a large happy
path sample that could accidentally look like qualification evidence.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from agent import registry, shadow, strategy, variants
from agent.staging import StagingStore
from research import authoring, findings
from research.recorded_data import EventPlane
from tests.helpers import valid_config
from tests.research.test_enrichment_isolation import (
    execution_enriched,
    symbol_snapshot,
)


EVENT_TS_MS = 1_760_000_000_000
OBSERVED_TS_MS = EVENT_TS_MS + 100
AVAILABLE_TS_MS = EVENT_TS_MS + 200
DECISION_TS_MS = EVENT_TS_MS + 250
START_TS = EVENT_TS_MS / 1000.0


class GoldenLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.recorded = self.root / "recorded" / "order_book"
        self.recorded.mkdir(parents=True)
        self.archive = self.root / "archive"
        self.event_db = self.root / "market-events.db"
        self.findings_db = self.root / "findings.db"

    def _write_recorder_csv(self):
        (self.recorded / "2026-08-07.csv").write_text(
            "ts,inst_id,mid,observed_at,available_at,source,feed,schema,status\n"
            f"{EVENT_TS_MS},BTC/USDT:USDT,100.0,{OBSERVED_TS_MS},"
            f"{AVAILABLE_TS_MS},fixture,book,v1,observed\n",
            encoding="utf-8",
        )

    @staticmethod
    def _descriptor(evaluator, baseline, candidate):
        declared = variants.declared_research_setting(candidate)
        code_identity, config_identity = shadow._identity_bundle(
            evaluator._provenance[baseline.variant_id],
            evaluator._provenance[candidate.variant_id],
        )
        descriptor = {
            **declared,
            "variant_id": candidate.variant_id,
            "source": "static",
            "priority": 0,
            "order_key": candidate.variant_id,
            "code_identity": code_identity,
            "config_identity": config_identity,
        }
        descriptor["candidate_key"] = findings._content_hash({
            key: descriptor[key] for key in (
                "variant_id", "axis", "setting_id", "setting", "source",
                "code_identity", "config_identity",
            )
        })
        return descriptor

    def test_bounded_offline_lifecycle_is_linked_and_refuses_qualification(self):
        self._write_recorder_csv()
        plane = EventPlane(self.event_db)
        with patch("research.recorded_data._now_ms", return_value=AVAILABLE_TS_MS):
            first_ingest = plane.ingest(
                self.recorded.parent, archive_root=self.archive)
            second_ingest = plane.ingest(
                self.recorded.parent, archive_root=self.archive)
        self.assertEqual(first_ingest["rows_valid"], 1)
        self.assertEqual(first_ingest["rows_inserted"], 1)
        self.assertEqual(second_ingest["rows_inserted"], 0)
        self.assertIsNone(second_ingest["snapshot_id"])

        asof = plane.query_asof(
            series="order_book", instrument="BTC/USDT:USDT", feed="book",
            decision_time_ms=DECISION_TS_MS,
            cutoff_event_ts_ms=EVENT_TS_MS,
        )
        repeated_asof = plane.query_asof(
            series="order_book", instrument="BTC/USDT:USDT", feed="book",
            decision_time_ms=DECISION_TS_MS,
            cutoff_event_ts_ms=EVENT_TS_MS,
        )
        self.assertEqual(asof["event_join_digest"], repeated_asof["event_join_digest"])
        self.assertEqual(len(asof["events"]), 1)
        event = asof["events"][0]
        event_digest = asof["event_join_digest"]
        payload = json.loads(event["payload_json"])

        cfg = valid_config()
        original_cfg = deepcopy(cfg)
        baseline = variants.baseline("momentum", "phase1-v3")
        candidate = variants.Variant(
            variant_id="momentum.golden.lifecycle_2_5",
            strategy_id="momentum",
            base_version="phase1-v3",
            overrides={"strategy.fixed_reward_risk": 2.5},
            hypothesis="A bounded lifecycle fixture exercises one exact setting.",
            status="candidate",
        )
        store = findings.FindingsStore(self.findings_db)
        scope = "golden:lifecycle:feed-v1"
        evaluator = shadow.ShadowEvaluator(
            [baseline, candidate], cfg, store=store, scope_key=scope,
            budget_ms=0,
        )
        descriptor = self._descriptor(evaluator, baseline, candidate)
        assignment = store.ensure_experiment_assignment(
            scope, "momentum", baseline.variant_id, [descriptor],
            minimum_duration_seconds=0, minimum_observations=1, now=START_TS,
        )

        symbol = "BTC/USDT:USDT"
        snapshot_row = execution_enriched(
            symbol_snapshot(price=float(payload["mid"]), signal_ts=EVENT_TS_MS)
        )
        snapshot = {symbol: snapshot_row,
                    "_market_context": {"benchmark": symbol}}
        proposal = {
            "symbol": symbol,
            "action": "open",
            "direction": "long",
            "setup_type": "trend_continuation",
            "confidence": 0.8,
            "invalidation_anchor": "structure",
            "exit_policy": "fixed_rr",
            "proposal_source": "deterministic_contract",
            "signal_ts": EVENT_TS_MS,
            "event_join_digest": event_digest,
        }
        plan, refusal = strategy.build_setup_plan(
            proposal, snapshot_row, evaluator._configs[candidate.variant_id]
        )
        self.assertIsNotNone(plan, refusal)

        with patch("ccxt.okx") as exchange, \
                patch("agent.brain.LLM") as llm, \
                patch("agent.exchange.Exchange") as live_exchange:
            with patch.object(
                    shadow.uuid, "uuid4",
                    side_effect=[uuid.UUID(int=1), uuid.UUID(int=2),
                                 uuid.UUID(int=3), uuid.UUID(int=4)]):
                first_records = evaluator.evaluate(
                    snapshot, now=START_TS + 1.0, cycle_id="golden-cycle-1",
                    proposals=[proposal],
                )
                second_records = evaluator.evaluate(
                    snapshot, now=START_TS + 2.0, cycle_id="golden-cycle-1",
                    proposals=[proposal],
                )
            exchange.assert_not_called()
            llm.assert_not_called()
            live_exchange.assert_not_called()
        self.assertEqual(len(first_records), 2)
        self.assertEqual(len(second_records), 2)
        self.assertEqual(
            {record.outcome for record in first_records}, {"proposed"})
        self.assertEqual(
            {len(store.paper_trades_for(scope, variant_id))
             for variant_id in (baseline.variant_id, candidate.variant_id)},
            {1},
        )
        self.assertEqual(cfg, original_cfg)

        candidate_decision = store.paper_decisions_for(
            scope, candidate.variant_id)[0]
        self.assertEqual(
            json.loads(candidate_decision["proposal_json"])["event_join_digest"],
            event_digest,
        )
        provenance = json.loads(candidate_decision["assumptions_json"])[
            "experiment_provenance"]
        canonical = registry.contract_for("momentum")
        self.assertEqual(provenance["strategy_contract_hash"], canonical.semantic_hash)
        self.assertEqual(provenance["strategy_contract_variant_id"], canonical.variant_id)

        # Close one bounded observation per arm.  The candidate deliberately
        # has missing funding coverage: it must remain audit-visible but can
        # never enter inference or qualify.
        for variant_id in (baseline.variant_id, candidate.variant_id):
            trade = store.paper_trades_for(scope, variant_id)[0]
            verified = variant_id == baseline.variant_id
            with findings._connect(self.findings_db) as conn:
                findings.FindingsStore._close_paper_trade(conn, {
                    "trade_id": trade["trade_id"],
                    "exit_ts": START_TS + 3.0,
                    "exit_price": 101.0,
                    "result": "target",
                    "net_pnl_usd": 10.0,
                    "r_multiple": 1.0,
                    "valid_for_inference": verified,
                    "funding_status": (
                        "verified_no_settlement_due" if verified else "missing"),
                    "inference_exclusion_reason": (
                        None if verified else "fixture missing funding"),
                    "execution": {
                        "funding_events": [],
                        "exit": {"funding_status": (
                            "verified_no_settlement_due"
                            if verified else "missing")},
                    },
                })

        observation_key = candidate_decision["proposal_id"]
        store.record_experiment_observations(
            assignment["assignment_id"],
            [{"observation_key": observation_key,
              "observed_ts": START_TS + 3.0}],
            now=START_TS + 3.0,
        )
        terminal = store.maybe_complete_experiment_assignment(
            assignment["assignment_id"], now=START_TS + 4.0)
        self.assertEqual(terminal["status"], "COMPLETED")
        outcome = store.experiment_outcome(assignment["assignment_id"])
        self.assertEqual(outcome["terminal_status"], "COMPLETED")
        self.assertEqual(outcome["verdict"], "INCONCLUSIVE")
        candidate_evidence = outcome["payload"]["candidate"]
        self.assertEqual(candidate_evidence["scored_closes"], 0)
        self.assertEqual(candidate_evidence["inference_exclusion_count"], 1)
        self.assertIn("funding", candidate_evidence["inference_exclusions"][0]["reason"])
        self.assertEqual(store.edge_evidence(scope), [])
        self.assertIsNone(store.qualification_status(candidate.variant_id, scope))
        self.assertTrue(outcome["outcome_id"])
        self.assertEqual(outcome["payload_hash"], findings._content_hash(
            outcome["payload"]))
        self.assertEqual(candidate_evidence["proposal_count"], 1)
        # Outcome payloads intentionally omit the full decision ledger; the
        # immutable paper decision row is the linked source for this digest.
        self.assertEqual(
            json.loads(store.paper_decisions_for(
                scope, candidate.variant_id)[0]["proposal_json"])
            ["event_join_digest"], event_digest)

        # Terminal persistence is immutable and reruns do not create a second
        # outcome.  The authoring request sees this exact outcome as evidence.
        rerun = store.ensure_terminal_experiment_outcomes()
        self.assertEqual(rerun["created"], [])
        reopened = findings.FindingsStore(self.findings_db)
        reopened_outcome = reopened.experiment_outcome(
            outcome_id=outcome["outcome_id"])
        self.assertEqual(reopened_outcome["payload_hash"], outcome["payload_hash"])
        request = authoring.build_request(
            StagingStore(self.root / "staging.db"),
            findings_store=reopened, max_proposals=1,
        )
        self.assertIn(
            outcome["outcome_id"], json.dumps(request["evidence"], sort_keys=True)
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
