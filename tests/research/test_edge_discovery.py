from datetime import date, datetime, timedelta, timezone
from contextlib import closing
import ast
import copy
import json
import os
import inspect
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace
from typing import get_args, get_type_hints

from research import edge_lab, edge_ledger, edge_ledger_proof, edge_ledger_store, gates
from research import edge_discovery_core
from research.edge_lab import DiscoveryError, EdgeLedger, discover
from research.edge_ledger import (
    SCHEMA_VERSION, canonical_json, content_hash, hash_config, hash_dataset,
    hash_provenance, init_db, init_ledger,
)
from research.gates import (
    FALSIFICATION_INDEPENDENT_METHOD, fdr_batch_evidence,
    falsification_gate, heldout_separation, matched_cluster_test,
    max_drawdown_of, performance_floor, placebo_null_distribution, qualification_report,
    structural_floor, verified_gate_envelope, walk_forward_report,
)
from research.stats import stable_seed
from research.costs import SQLiteQuoteIndex, quote_fill
from research.factory_ledger import CONFIRMATORY_SCOPE_VERSION, FactoryLedger
from tests.research.test_factory_end_to_end import ROOT_SPEC, edge_corpus


def _gate_evidence(heldout, *, alpha=.05, equity_feed="iex"):
    """Build the statistical evidence a persisted proof must now reproduce.

    The zero baseline makes the matched deltas equal to the held-out P&L, so
    every recorded statistic is recomputable from the envelope itself.
    """
    baseline = [{**row, "net_pnl": 0.0, "opportunity_id": f"base-{index}"}
                for index, row in enumerate(heldout)]
    control = matched_cluster_test(
        heldout, baseline, vehicle="equity", equity_feed=equity_feed)
    placebo = placebo_null_distribution(
        heldout, baseline, vehicle="equity", equity_feed=equity_feed)
    independent_seed = stable_seed({
        "purpose": "independent_placebo_null_tail.v1",
        "primary_assignments_hash": placebo["assignments_hash"],
        "draws": int(placebo["draws"]),
    })
    independent = placebo_null_distribution(
        heldout, baseline, vehicle="equity", draws=int(placebo["draws"]),
        seed=independent_seed, equity_feed=equity_feed)
    independent_result = falsification_gate(
        independent["observed"], independent["placebo"], alpha=alpha)
    falsification = {
        **falsification_gate(
            placebo["observed"], placebo["placebo"], alpha=alpha,
            preregistered_p_value=control["p_value"],
            independent_p_value=independent_result["p_value"],
            independent_method=FALSIFICATION_INDEPENDENT_METHOD,
            independent_result_hash=independent["assignments_hash"],
            require_independent=True),
        "method": placebo["method"], "assignments_hash": placebo["assignments_hash"],
        "observations": len(placebo["observed"]),
        "draws": int(placebo["draws"]), "seed": int(placebo["seed"]),
        "independent_method": FALSIFICATION_INDEPENDENT_METHOD,
        "independent_result_hash": independent["assignments_hash"],
        "independent_assignments_hash": independent["assignments_hash"],
        "independent_draws": int(independent["draws"]),
        "independent_seed": int(independent["seed"]),
    }
    absolute = performance_floor(
        heldout, vehicle="equity", equity_feed=equity_feed)
    walk = walk_forward_report(
        heldout, baseline, vehicle="equity", equity_feed=equity_feed)
    return control, falsification, absolute, walk


def _sessions(start: datetime, count: int, symbols=("SPY", "QQQ", "IWM", "DIA")) -> list[dict]:
    """Sessions with a real timing edge, not a session-long directional drift.

    The breakout runs far enough to pay 1.5R but not 2R, and then gives the
    whole move back over a long decline.  The give-back is what makes a
    randomly timed entry lose: without it the randomized-entry null control
    would score the same trade as the strategy, which is the point of having
    the control at all.
    """
    rows: list[dict] = []
    for offset in range(count):
        session = start + timedelta(days=offset)
        values = [
            (100, 101, 99, 100),        # one-minute opening range
            (100, 102, 99.5, 102),      # confirmed breakout
            (102, 103, 101.5, 103),     # next-bar entry at 102
            # Gap through the 1.5R target at the next-bar boundary.  The
            # candidate's exit is therefore priced by the executable IEX
            # quote at that boundary; the 2R baseline still holds to the
            # force-flat boundary because this bar's high remains below 2R.
            (106.5, 107, 102.5, 106.8),
        ]
        price = 106.8
        for _ in range(20):            # the whole move handed back
            values.append((price, price + .05, price - .4, price - .35))
            price -= .35
        for index, symbol in enumerate(symbols):
            # A per-symbol offset keeps the sample from being one repeated
            # observation while leaving every level on the same side of itself.
            shift = index * .01
            for minute, (open_, high, low, close) in enumerate(values):
                timestamp = session + timedelta(minutes=minute)
                rows.append({
                    "symbol": symbol,
                    "timestamp": timestamp.isoformat(),
                    "as_of": (timestamp + timedelta(minutes=1)).isoformat(),
                    "observed_at": (timestamp + timedelta(minutes=1)).isoformat(),
                    "open": open_ + shift, "high": high + shift,
                    "low": low + shift, "close": close + shift,
                    "volume": 1, "provider": "alpaca", "feed": "iex",
                    "source_mode": "forward_observed",
                })
                rows.append({
                    "kind": "quote", "symbol": symbol,
                    "timestamp": timestamp.isoformat(),
                    # Boundary quotes authorize entries even though the bar
                    # record itself arrives at its completed-bar timestamp.
                    "as_of": timestamp.isoformat(),
                    "observed_at": timestamp.isoformat(),
                    # The target bar needs an executable IEX bid for the
                    # long exit; opening-price quotes would force a bar
                    # fallback and make this lifecycle fixture fail the
                    # production fill-quality gate.
                    "bid": (high if minute == 3 else open_) + shift - .01,
                    "ask": (high if minute == 3 else open_) + shift + .01,
                    "provider": "alpaca", "feed": "iex",
                    "source_mode": "forward_observed",
                })
            # The 2R baseline can remain open after this compact four-minute
            # fixture.  Supply its exact force-flat IEX quote so the baseline
            # and randomized null stay in the authorizing-quality sample.
            final_quote = session + timedelta(minutes=len(values))
            rows.append({
                "kind": "quote", "symbol": symbol,
                "timestamp": final_quote.isoformat(),
                "as_of": final_quote.isoformat(),
                "observed_at": final_quote.isoformat(),
                "bid": values[-1][3] + shift - .01,
                "ask": values[-1][3] + shift + .01,
                "provider": "alpaca", "feed": "iex",
                "source_mode": "forward_observed",
            })
            # The baseline (2R) remains open through this compact session and
            # is force-flat on the final completed bar.  Supply a distinct
            # quote at that exact bar-end boundary so strict replay records a
            # genuine IEX exit rather than silently falling back to OHLC.
            final_timestamp = session + timedelta(minutes=len(values))
            final_close = values[-1][3] + shift
            rows.append({
                "kind": "quote", "symbol": symbol,
                "timestamp": final_timestamp.isoformat(),
                "as_of": final_timestamp.isoformat(),
                "observed_at": final_timestamp.isoformat(),
                "bid": final_close - .01, "ask": final_close + .01,
                "provider": "alpaca", "feed": "iex",
                "source_mode": "forward_observed",
            })
    return rows


def _persist_gate(ledger: EdgeLedger, candidate_id: str, lane: str, *,
                  passes: bool = True, record: bool = True,
                  score: float = 1.0,
                  scores: list[float] | None = None,
                  r_multiples: list[float] | None = None,
                  equity_feed: str = "iex",
                  legacy_feedless: bool = False,
                  legacy_gate_v2: bool = False) -> tuple[dict, dict]:
    def priced(row: dict) -> dict:
        return {
            **row,
            "no_trade": False,
            "entry_price": 100.0,
            "exit_price": 100.1,
            "quantity": 1.0,
            "multiplier": 1.0,
            "stop_distance": 1.0,
            "risk_usd": 10.0,
            "entry_fill_source": "quote",
            "exit_fill_source": "quote",
            "entry_feed": equity_feed, "exit_feed": equity_feed,
            "entry_provider": "alpaca", "exit_provider": "alpaca",
            "entry_quote_age_seconds": 0.0,
            "exit_quote_age_seconds": 0.0,
        }

    prefix = f"{lane}-{'pass' if passes else 'fail'}"
    # Five opportunities per each of thirty independent sessions satisfy the
    # immutable 100-trade/30-cluster offline floor and the 150-trade shadow
    # floor while keeping this shared proof fixture compact.
    symbols = ("SPY", "QQQ", "IWM", "DIA", "AAPL")
    # Shadow fixtures model the production split: the final authorizing run
    # carries 30 confirmatory sessions (150 trades), while 30 older sessions
    # supply the adaptive selection p-value. Backtest fixtures retain the
    # compact 30-session corpus.
    session_count = 60 if lane == "shadow" else 30
    fit = [] if lane == "shadow" else [
        priced({"vehicle": "equity", "symbol": symbol,
                "session_date": (datetime(2023, 12, 1, tzinfo=timezone.utc) +
                                  timedelta(days=index)).date().isoformat(),
                "opportunity_id": f"{prefix}-fit-{index}-{symbol}",
                "net_pnl": 1.0})
        for index in range(session_count) for symbol in symbols]
    # ``scores`` gives one P&L per held-out session, which lets a fixture hold
    # the mean delta fixed while moving its dispersion, and therefore its
    # lower confidence bound.  Short score sequences repeat over the 30
    # sessions but preserve their distribution for ranking tests.
    # ``scores`` gives one P&L per held-out session, which lets a fixture hold
    # the mean delta fixed while moving its dispersion, and therefore its
    # lower confidence bound.
    score_values = list(scores) if scores is not None else [score]
    values = [score_values[index % len(score_values)] if passes else -1.0
              for index in range(session_count)]
    heldout_start = datetime(2024, 1, 3, tzinfo=timezone.utc)
    all_heldout = [priced({
        "vehicle": "equity", "symbol": symbol,
        "session_date": (heldout_start + timedelta(days=index)).date().isoformat(),
        "opportunity_id": f"{prefix}-held-{index}-{symbol}",
        "net_pnl": values[index],
    }) for index in range(session_count) for symbol in symbols]
    heldout = list(all_heldout)
    selection_sessions: list[str] = []
    confirmatory_sessions: list[str] = []
    selection_rows: list[dict] = []
    if lane == "shadow":
        all_sessions = sorted({row["session_date"] for row in all_heldout})
        split = len(all_sessions) // 2
        selection_sessions = all_sessions[:split]
        confirmatory_sessions = all_sessions[split:]
        selection_rows = [row for row in all_heldout
                          if row["session_date"] in set(selection_sessions)]
        heldout = [row for row in all_heldout
                   if row["session_date"] in set(confirmatory_sessions)]
    selection_baseline_rows = [
        {**row, "net_pnl": 0.0,
         "opportunity_id": f"baseline-selection-{index}"}
        for index, row in enumerate(selection_rows)]
    selection_null_rows = [
        {**row, "net_pnl": 0.0,
         "opportunity_id": f"null-selection-{index}"}
        for index, row in enumerate(selection_rows)]
    selection_p_value = None
    if lane == "shadow":
        selection_control = matched_cluster_test(
            selection_rows, selection_baseline_rows, vehicle="equity",
            equity_feed=equity_feed)
        selection_p_value = float(selection_control["p_value"])
    baseline = [{**row, "net_pnl": 0.0,
                 "opportunity_id": f"baseline-{index}"}
                for index, row in enumerate(heldout)]
    fit_baseline = [{**row, "net_pnl": 0.0,
                     "opportunity_id": f"fit-baseline-{index}"}
                    for index, row in enumerate(fit)]
    floor_trades = 150 if lane == "shadow" else 100
    fit_floor = structural_floor(
        fit, vehicle="equity", min_trades=floor_trades, min_sessions=30,
        min_clusters=30,
        required=lane != "shadow", equity_feed=equity_feed)
    held_floor = structural_floor(
        heldout, vehicle="equity", min_trades=floor_trades, min_sessions=30,
        min_clusters=30, equity_feed=equity_feed)
    separation = (heldout_separation(fit, heldout) if lane == "backtest" else
                  {"fit": 0, "heldout": len(heldout), "overlap_sessions": [],
                   "passes": True, "mode": "new_data"})
    control, falsification, absolute, walk = _gate_evidence(
        heldout, equity_feed=equity_feed)
    fit_control = (matched_cluster_test(
                       fit, fit_baseline, vehicle="equity",
                       equity_feed=equity_feed)
                   if fit else {"available": True, "actual_control": True,
                                "matched": 0, "mean_delta": None,
                                "p_value": 1.0, "mode": "prior_backtest"})
    qualification_start = datetime(2024, 2, 1, tzinfo=timezone.utc)
    qualification_rows = [priced({
        "vehicle": "equity", "symbol": symbol,
        "session_date": (qualification_start + timedelta(days=index)).date().isoformat(),
        "opportunity_id": f"{prefix}-qualification-{index}-{symbol}",
        "net_pnl": 1.0,
    }) for index in range(session_count) for symbol in symbols]
    qualification_baseline = [
        {**row, "net_pnl": 0.0,
         "opportunity_id": f"qualification-baseline-{index}"}
        for index, row in enumerate(qualification_rows)
    ]
    qualification = qualification_report(
        qualification_rows, qualification_baseline, vehicle="equity",
        sessions=sorted({row["session_date"] for row in qualification_rows}),
        candidate_id=candidate_id, preselected=True,
        max_drawdown=0.0, equity_feed=equity_feed)
    candidate = ledger.candidate(candidate_id)
    candidate_config = json.loads(candidate["config_json"])
    hashes = edge_ledger.provenance_hash(config=candidate_config)
    if lane == "shadow":
        hashes.update({
            "independent_confirmatory": True,
            "disjoint_sessions": True,
            "session_disjoint": True,
            "selection_sessions": selection_sessions,
            "confirmatory_sessions": confirmatory_sessions,
            "selection_session_digest": edge_ledger.content_hash(selection_sessions),
            "confirmatory_session_digest": edge_ledger.content_hash(confirmatory_sessions),
            "p_value_source": "live_shadow_confirmatory_gate",
            "selection_raw_p_value": selection_p_value,
            "confirmatory_raw_p_value": control["p_value"],
        })
    checks = {"edge_positive": passes, "family_fdr_significant": passes,
              "global_fdr_significant": passes,
              "cumulative_fdr_significant": passes,
              "falsification": bool(falsification["passes"]),
              "heldout_net_pnl_positive": bool(absolute["net_pnl_positive"]),
              "heldout_expectancy_positive": bool(absolute["expectancy_positive"]),
              "heldout_delta_lcb_positive": bool(
                  control["mean_delta_lcb"] is not None and
                  control["mean_delta_lcb"] > 0),
              "walk_forward_majority_positive": bool(walk["majority_positive"])}
    fdr_record = None
    if lane == "shadow":
        factory = FactoryLedger(ledger.path)
        test_id = f"{candidate_id}:shadow"
        scope = f"{CONFIRMATORY_SCOPE_VERSION}:equity"
        state = factory.fdr_state(scope)
        fdr_record = next((
            {**item, "tests": index}
            for index, item in enumerate(state["decisions"], start=1)
            if item["test_id"] == test_id
        ), None)
        if fdr_record is None:
            fdr_record = (factory.record_fdr_decision(
                scope, test_id,
                control["p_value"], alpha=.05) if record else
                factory.next_fdr_allocation(
                    scope, alpha=.05))
    envelope = verified_gate_envelope(
        lane=lane, vehicle="equity", fit=fit, heldout=heldout,
        fit_baseline=fit_baseline, heldout_baseline=baseline,
        null_source=baseline,
        fit_floor=fit_floor, heldout_floor=held_floor,
        fit_control=fit_control,
        control={**control, "kind": "matched_actual_baseline"},
        p_value=control["p_value"],
        q_value=(selection_p_value if lane == "shadow" else
                 control["p_value"]), alpha=.05,
        family_q_value=(selection_p_value if lane == "shadow" else None),
        fdr_batch=fdr_batch_evidence(
            candidate_id=candidate_id,
            family_name="fixture",
            family_candidate_key=candidate_id,
            global_candidate_key=candidate_id,
            family_values={"fixture": {
                candidate_id: (selection_p_value if lane == "shadow" else
                               control["p_value"])}},
            global_values={
                candidate_id: (selection_p_value if lane == "shadow" else
                               control["p_value"])},
            alpha=.05,
            p_value_source=("selection_window_gate"
                            if lane == "shadow" else "gate")),
        falsification=falsification,
        separation=separation, checks=checks, passes=passes,
        walk_forward=walk,
        qualification=qualification,
        null_control={"kind": "randomized_entry_null",
                      "matched": control["matched"], "available": True,
                      "mean_delta": control["mean_delta"],
                      "mean_delta_lcb": control["mean_delta_lcb"],
                      "p_value": control["p_value"]},
        online_fdr={"scope": (f"{CONFIRMATORY_SCOPE_VERSION}:equity"
                              if lane == "shadow" else "test"),
                    "test_id": f"{candidate_id}:{lane}",
                    "p_value": control["p_value"],
                    "raw_p_value": control["p_value"],
                    "confirmatory_raw_p_value": control["p_value"],
                    "selection_raw_p_value": (
                        selection_p_value if lane == "shadow" else
                        control["p_value"]),
                    "family_q_value": (
                        selection_p_value if lane == "shadow" else
                        control["p_value"]),
                    "global_q_value": (
                        selection_p_value if lane == "shadow" else
                        control["p_value"]),
                    "allocated_alpha": (fdr_record["allocated_alpha"]
                                        if fdr_record is not None else .05),
                    "alpha": .05,
                    "tests": (fdr_record["tests"] if fdr_record is not None else 1),
                    "decision": passes,
                    "required": True, "tested": True,
                    "p_value_kind": "raw_confirmatory",
                    **({"method": fdr_record.get("method"),
                        "method_version": fdr_record.get("method_version")}
                       if lane == "shadow" and fdr_record is not None else {}),
                    "p_value_source": "live_shadow_confirmatory_gate",
                    "independent_confirmatory": lane == "shadow",
                    "disjoint_sessions": lane == "shadow",
                    "session_disjoint": lane == "shadow",
                    **({"selection_sessions": selection_sessions,
                        "confirmatory_sessions": confirmatory_sessions,
                        "selection_session_digest": edge_ledger.content_hash(selection_sessions),
                        "confirmatory_session_digest": edge_ledger.content_hash(confirmatory_sessions)}
                       if lane == "shadow" else {})},
        provenance=hashes, candidate_id=candidate_id,
        performance={"heldout_delta": control["mean_delta"],
                     "heldout_delta_lcb": control["mean_delta_lcb"],
                     "heldout_net_pnl": absolute["net_pnl"],
                     "heldout_expectancy": absolute["expectancy"],
                     "max_drawdown": max_drawdown_of(heldout)},
        equity_feed=equity_feed)
    if legacy_feedless:
        def strip_feed(value):
            if isinstance(value, dict):
                value.pop("equity_feed", None)
                for item in value.values():
                    strip_feed(item)
            elif isinstance(value, list):
                for item in value:
                    strip_feed(item)

        strip_feed(envelope)
        envelope["passes"] = bool(passes and all(envelope["checks"].values()))
        envelope["content_hash"] = gates._content_hash({
            key: value for key, value in envelope.items()
            if key != "content_hash"
        })
    if legacy_gate_v2:
        envelope["schema"] = gates.LEGACY_GATE_ENVELOPE_SCHEMA_V2
        envelope.pop("fdr_batch", None)
        envelope["checks"].pop("actual_control_adequate", None)
        envelope["checks"].pop("multiple_testing_batch_bound", None)
        envelope["passes"] = bool(
            passes and all(envelope["checks"].values()))
        envelope["content_hash"] = gates._content_hash({
            key: value for key, value in envelope.items()
            if key != "content_hash"
        })
    live_source = None
    if lane == "shadow":
        session_rows = {
            day: next(row for row in all_heldout if row["session_date"] == day)
            for day in [*selection_sessions, *confirmatory_sessions]
        }
        session_records = [{
            "session_date": day,
            "source_digest": f"source:{day}",
            "shadow_digest": f"shadow:{day}",
            "replay_digest": f"replay:{day}",
            "account_id": f"account:{day}",
            "trade_count": 5,
        } for day in [*selection_sessions, *confirmatory_sessions]]
        live_source = {
            "schema": "shadow-ingest.v1", "candidate_id": candidate_id,
            "vehicle": "equity",
            "independent_confirmatory": True,
            "disjoint_sessions": True,
            "session_disjoint": True,
            "selection_sessions": selection_sessions,
            "confirmatory_sessions": confirmatory_sessions,
            "selection_session_digest": edge_ledger.content_hash(selection_sessions),
            "confirmatory_session_digest": edge_ledger.content_hash(confirmatory_sessions),
            "p_value_source": "live_shadow_confirmatory_gate",
            "sessions": session_records,
            "selection": {
                "sessions": selection_sessions,
                "session_digest": edge_ledger.content_hash(selection_sessions),
                "rows_digest": edge_ledger.content_hash(selection_rows),
                "candidate_source": selection_rows,
                "baseline_source": selection_baseline_rows,
                "null_source": selection_null_rows,
                "minimums": {"trades": 150, "sessions": 30},
                "baseline_rows_digest": edge_ledger.content_hash(selection_baseline_rows),
                "null_rows_digest": edge_ledger.content_hash(selection_null_rows),
                "p_value_source": "selection_window_gate",
                "raw_p_value": selection_p_value,
                "alpha": .05, "test_iterations": 20_000,
                "bh": {
                    "family_values": {"fixture": {candidate_id: selection_p_value}},
                    "family_results": {candidate_id: {
                        "p": selection_p_value, "p_adjusted": selection_p_value,
                        "significant": selection_p_value <= .05, "family_size": 1}},
                    "global_values": {candidate_id: selection_p_value},
                    "global_results": {candidate_id: {
                        "p": selection_p_value, "p_adjusted": selection_p_value,
                        "significant": selection_p_value <= .05, "family_size": 1}},
                },
            },
            "confirmatory": {
                "sessions": confirmatory_sessions,
                "session_digest": edge_ledger.content_hash(confirmatory_sessions),
                "rows_digest": edge_ledger.content_hash(heldout),
                "baseline_rows_digest": edge_ledger.content_hash(baseline),
                "null_rows_digest": edge_ledger.content_hash(baseline),
                "test_id": f"{candidate_id}:shadow",
                "p_value_source": "live_shadow_confirmatory_gate",
                "raw_p_value": control["p_value"],
            },
            "baseline": {"candidate_id": "fixture:baseline",
                         "rows_digest": edge_ledger.content_hash(baseline),
                         "role": "paired_root_control"},
            "null": {"candidate_id": "fixture:null",
                      "rows_digest": edge_ledger.content_hash(baseline),
                      "role": "randomized_entry_null"},
        }
    run = ledger.append_run(
        candidate_id, lane=lane, vehicle="equity", fit=fit, heldout=heldout,
        config=candidate_config,
        metrics={"gate": {"passes": not passes,
                           "verified_gate": envelope,
                           "gate_hash": envelope["content_hash"]},
                 "confidence": 0.0,
                 **({"shadow_source": live_source,
                    "replay_digests": [item["replay_digest"]
                                       for item in live_source["sessions"]]}
                    if live_source is not None else {})})
    # ``r_multiples`` attaches the risk-normalized outcome the drift reference
    # is read from; the gate statistics themselves never consume it.
    per_trade = dict(zip((row["opportunity_id"] for row in heldout),
                         r_multiples or ()))
    for row in [*fit, *heldout]:
        value = per_trade.get(row["opportunity_id"])
        ledger.append_trade(run["run_id"],
                            row if value is None else {**row, "r_multiple": value})
    if record:
        ledger.record_verified_gate(run["run_id"], envelope)
        if live_source is not None:
            ledger.record_shadow_ingestion(
                candidate_id,
                {"schema": "shadow-ingest.v1", "candidate_id": candidate_id,
                 "vehicle": "equity", "source": live_source,
                 "replay_digests": [item["replay_digest"]
                                    for item in live_source["sessions"]],
                 # Keep the persisted marker's provenance identical to the
                 # production ingest contract.  The verifier binds these
                 # identity fields to the immutable candidate and run before
                 # accepting the shadow proof for promotion.
                 "run_provenance": {
                     **hashes,
                     "candidate_id": candidate_id,
                     "vehicle": "equity",
                     "candidate_proof": {key: candidate.get(key) for key in (
                         "candidate_id", "dataset_hash", "config_hash",
                         "code_hash", "provenance_hash")},
                 },
                 "candidate_proof": {key: candidate.get(key) for key in (
                     "candidate_id", "dataset_hash", "config_hash", "code_hash",
                     "provenance_hash")},
                 "gate_hash": envelope["content_hash"]},
                run_id=run["run_id"])
    return run, envelope


class EdgeLedgerStoreExtractionTests(unittest.TestCase):
    def test_ledger_accepts_legacy_and_configured_sip_proofs_but_not_delayed(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity",
                hypothesis="pre-feed-binding SIP proof", config={})
            run, legacy = _persist_gate(
                ledger, candidate["candidate_id"], "backtest",
                record=False, equity_feed="sip", legacy_feedless=True)
            self.assertTrue(legacy["passes"])
            self.assertNotIn("equity_feed", legacy)
            ledger.record_verified_gate(run["run_id"], legacy)
            proof = ledger.latest_verified_run(
                candidate["candidate_id"], lane="backtest")
            self.assertIsNotNone(proof)
            self.assertTrue(proof["verified_gate"]["passes"])

            explicit_run, explicit = _persist_gate(
                ledger, candidate["candidate_id"], "backtest",
                record=False, equity_feed="sip")
            self.assertEqual(explicit["equity_feed"], "sip")
            self.assertTrue(explicit["passes"])
            ledger.record_verified_gate(explicit_run["run_id"], explicit)

            delayed_run, delayed = _persist_gate(
                ledger, candidate["candidate_id"], "shadow",
                record=False, equity_feed="delayed_sip")
            self.assertEqual(delayed["equity_feed"], "delayed_sip")
            self.assertFalse(delayed["passes"])

    def test_store_symbols_are_identical_through_edge_facades(self):
        names = (
            "VEHICLES", "LANES", "LIFECYCLE", "CANDIDATE", "BACKTEST_PASSED",
            "SHADOW", "VALIDATED", "CHAMPION", "RETIRED", "DEMOTED",
            "SCHEMA_VERSION", "DEFAULT_DB_PATH", "PAPER_DEMOTION_MIN_OUTCOMES",
            "PAPER_DEMOTION_R_FLOOR",
            "canonical_json", "content_hash", "hash_dataset", "hash_config",
            "hash_provenance", "hash_file", "provenance_hash", "init_ledger", "init_db",
        )
        for name in names:
            self.assertIs(getattr(edge_ledger, name), getattr(edge_ledger_store, name))
            self.assertIs(getattr(edge_lab, name), getattr(edge_ledger_store, name))
        self.assertIs(edge_ledger.EdgeLedger, edge_lab.EdgeLedger)

    def test_proof_mixin_preserves_edge_ledger_facade_and_mro_identity(self):
        names = (
            "record_verified_gate", "_gate_envelope_error", "_latest_verified_gate",
            "latest_verified_run", "eligibility",
        )
        self.assertTrue(issubclass(edge_ledger.EdgeLedger,
                                   edge_ledger_proof.EdgeLedgerProofMixin))
        for name in names:
            self.assertNotIn(name, edge_ledger.EdgeLedger.__dict__)
            self.assertIs(getattr(edge_ledger.EdgeLedger, name),
                          getattr(edge_ledger_proof.EdgeLedgerProofMixin, name))
            self.assertIs(inspect.getattr_static(edge_ledger.EdgeLedger, name),
                          inspect.getattr_static(edge_ledger_proof.EdgeLedgerProofMixin, name))

    def test_proof_import_is_lazy_and_does_not_capture_ledger_defaults(self):
        root = Path(__file__).resolve().parents[2]
        script = (
            "import sys; import research.edge_ledger_proof as proof; "
            "assert 'research.edge_ledger' not in sys.modules; "
            "assert 'DEFAULT_DB_PATH' not in vars(proof)"
        )
        result = subprocess.run([sys.executable, "-c", script], cwd=root,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_legacy_edge_ledger_helpers_are_intercepted_by_proof_facade(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", hypothesis="facade helper patches", config={})
            run, envelope = _persist_gate(
                ledger, candidate["candidate_id"], "backtest", record=False)
            names = (
                "_connect", "_json", "_row", "_utc", "content_hash",
                "sample_counts", "verify_gate_envelope", "_finite_number",
                "_nonnegative_integer",
            )
            patches = [mock.patch.object(edge_ledger, name,
                                         wraps=getattr(edge_ledger, name))
                       for name in names]
            with patches[0] as connect, patches[1] as json_, patches[2] as row, \
                    patches[3] as utc, patches[4] as content, patches[5] as counts, \
                    patches[6] as verify, patches[7] as finite, patches[8] as integer:
                ledger.record_verified_gate(run["run_id"], envelope)
            for helper in (connect, json_, row, utc, content, counts, verify,
                           finite, integer):
                self.assertTrue(helper.called, helper)

    def test_hash_aliases_are_deterministic_and_reject_nonfinite_json(self):
        value = {"z": "café", "a": [1, True, None]}
        self.assertEqual(canonical_json(value), '{"a":[1,true,null],"z":"café"}')
        self.assertIs(hash_dataset, content_hash)
        self.assertIs(hash_config, content_hash)
        self.assertIs(hash_provenance, content_hash)
        expected = content_hash(value)
        self.assertEqual(hash_dataset(value), expected)
        self.assertEqual(hash_config(value), expected)
        self.assertEqual(hash_provenance(value), expected)
        with self.assertRaises(ValueError):
            canonical_json(float("nan"))
        with self.assertRaises(ValueError):
            canonical_json(float("inf"))

    def test_fresh_schema_initialization_is_idempotent_and_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "edge.sqlite3"
            self.assertEqual(init_ledger(path), {"db_path": str(path), "schema": SCHEMA_VERSION})
            self.assertEqual(init_db(path), {"db_path": str(path), "schema": SCHEMA_VERSION})
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(
                    db.execute("SELECT value FROM ledger_meta WHERE key='schema'").fetchone()[0],
                    str(SCHEMA_VERSION),
                )
            candidate = EdgeLedger(path).register_candidate(
                "ibr.target.1_5r", hypothesis="immutable", config={})
            with closing(sqlite3.connect(path)) as db:
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    db.execute(
                        "UPDATE candidates SET hypothesis='changed' WHERE candidate_id=?",
                        (candidate["candidate_id"],),
                    )


class EdgeDiscoveryCoreExtractionTests(unittest.TestCase):
    def test_core_helpers_are_facade_identity_aliases(self):
        names = (
            "DiscoveryError", "_read_discovery_rows", "_effective_ibr_config",
            "_opportunity_rows", "_discover_gate", "_finalize_gate",
        )
        for name in names:
            self.assertIs(getattr(edge_lab, name), getattr(edge_discovery_core, name))
        self.assertIs(
            __import__("research.strategy_factory", fromlist=["_read_discovery_rows"])._read_discovery_rows,
            edge_lab._read_discovery_rows,
        )

    def test_helpers_are_not_local_edge_lab_definitions(self):
        source = inspect.getsource(edge_lab)
        tree = ast.parse(source)
        local_names = {
            node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        for name in (
            "DiscoveryError", "_read_discovery_rows", "_effective_ibr_config",
            "_opportunity_rows", "_discover_gate", "_finalize_gate",
        ):
            self.assertNotIn(name, local_names)

    def test_core_import_is_lazy_and_does_not_load_edge_lab_or_ledger(self):
        root = Path(__file__).resolve().parents[2]
        script = (
            "import sys; import research.edge_discovery_core; "
            "assert 'research.edge_lab' not in sys.modules; "
            "assert 'research.edge_ledger' not in sys.modules; "
            "assert 'sqlite3' not in sys.modules"
        )
        result = subprocess.run([sys.executable, "-c", script], cwd=root,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_moved_helper_type_hints_resolve(self):
        result = get_type_hints(edge_discovery_core._read_discovery_rows)["return"]
        _, bars, snapshots, quotes = get_args(result)
        self.assertIs(get_args(bars)[0], edge_discovery_core.UnderlyingBar)
        self.assertIs(get_args(snapshots)[1], edge_discovery_core.OptionSnapshot)
        self.assertIs(get_args(quotes)[0], edge_discovery_core.QuoteSnapshot)

    def test_moved_read_helper_forwards_legacy_facade_normalizer_patch(self):
        row = {
            "symbol": "SPY", "timestamp": "2024-01-02T14:30:00+00:00",
            "open": 100, "high": 101, "low": 99, "close": 100,
            "volume": 1,
        }
        bar = mock.Mock()
        with mock.patch.object(edge_lab, "normalize_underlying_bar",
                               return_value=bar) as normalizer:
            raw, bars, snapshots, quotes = edge_discovery_core._read_discovery_rows([row])
        self.assertEqual(raw, [row])
        self.assertEqual(bars, [bar])
        self.assertEqual(snapshots, {})
        self.assertEqual(quotes, [])
        normalizer.assert_called_once_with(row, provider="alpaca", feed="iex")

    def test_equity_corpus_requires_exact_configured_realtime_feed_and_provider(self):
        row = {
            "kind": "bar", "symbol": "SPY",
            "timestamp": "2024-01-02T14:30:00+00:00",
            "as_of": "2024-01-02T14:31:00+00:00",
            "observed_at": "2024-01-02T14:31:00+00:00",
            "open": 100, "high": 101, "low": 99, "close": 100,
            "volume": 1, "provider": "alpaca", "feed": "sip",
        }
        raw, bars, snapshots, quotes = edge_discovery_core._read_discovery_rows(
            [row], require_provenance=True, expected_equity_feed="sip",
            expected_provider="alpaca")
        self.assertEqual(raw, [row])
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].feed, "sip")
        self.assertEqual(snapshots, {})
        self.assertEqual(quotes, [])

        with self.assertRaisesRegex(DiscoveryError, "does not match configured"):
            edge_discovery_core._read_discovery_rows(
                [row], require_provenance=True, expected_equity_feed="iex",
                expected_provider="alpaca")
        with self.assertRaisesRegex(DiscoveryError, "configured provider"):
            edge_discovery_core._read_discovery_rows(
                [row], require_provenance=True, expected_equity_feed="sip",
                expected_provider="other")
        delayed = {**row, "feed": "delayed_sip"}
        with self.assertRaisesRegex(DiscoveryError, "diagnostic-only"):
            edge_discovery_core._read_discovery_rows(
                [delayed], require_provenance=True, expected_equity_feed="sip",
                expected_provider="alpaca")

    def test_option_corpus_requires_explicit_opra_provenance(self):
        row = {
            "kind": "option", "symbol": "SPY240119C00100000",
            "underlying": "SPY", "expiration": "2024-01-19",
            "strike": 100, "right": "call", "multiplier": 100,
            "timestamp": "2024-01-02T14:31:00+00:00",
            "as_of": "2024-01-02T14:31:00+00:00",
            "observed_at": "2024-01-02T14:31:00+00:00",
            "bid": 1.0, "ask": 1.1, "bid_size": 5, "ask_size": 5,
            "provider": "alpaca",
        }
        for feed in (None, "indicative"):
            with self.subTest(feed=feed):
                candidate = dict(row)
                if feed is not None:
                    candidate["feed"] = feed
                with self.assertRaisesRegex(
                        DiscoveryError, "explicit OPRA feed provenance"):
                    edge_discovery_core._normalize_corpus([candidate])
        opra = {**row, "feed": "opra"}
        _bars, snapshots, _quotes = edge_discovery_core._normalize_corpus([opra])
        self.assertEqual(len(snapshots), 1)

    def test_discovery_promotion_requires_thirty_session_clusters(self):
        candidate = [{
            "vehicle": "equity", "symbol": "SPY",
            "session_date": f"2026-03-{index + 1:02d}",
            "opportunity_id": f"edge:{index}", "net_pnl": 1.0,
            "return_value": .001, "no_trade": False,
        } for index in range(29)]
        baseline = [{**row, "net_pnl": 0.0, "return_value": 0.0}
                    for row in candidate]
        gate = edge_discovery_core._discover_gate(
            candidate, baseline, vehicle="equity", min_trades=1,
            min_sessions=1, alpha=.05, test_iterations=100,
            null_rows=baseline,
            qualification={"available": True, "net_positive": True,
                           "delta_positive": True})
        self.assertEqual(gate["heldout_floor"]["minimums"]["clusters"], 30)
        self.assertFalse(gate["heldout_floor"]["checks"]["clusters"])

    def test_discovery_actual_baseline_requires_count_and_coverage(self):
        candidate = [{
            "vehicle": "equity", "symbol": "SPY",
            "session_date": f"2026-04-{index + 1:02d}",
            "opportunity_id": f"candidate:{index}", "net_pnl": 1.0,
            "return_value": .001, "no_trade": False,
            "entry_fill_source": "quote", "exit_fill_source": "quote",
            "entry_feed": "iex", "exit_feed": "iex",
            "entry_provider": "fixture", "exit_provider": "fixture",
            "entry_quote_age_seconds": 0.0, "exit_quote_age_seconds": 0.0,
        } for index in range(30)]
        baseline = [{**row, "net_pnl": 0.0}
                    for row in candidate[:5]]
        # Keep keys aligned for the five matched observations while leaving
        # 25 candidate opportunities without an actual baseline control.
        gate = edge_discovery_core._discover_gate(
            candidate, baseline, vehicle="equity", min_trades=1,
            min_sessions=1, alpha=.05, test_iterations=10,
            null_rows=baseline, shadow=True)
        adequacy = gate["heldout_paired_baseline"]["paired_adequacy"]
        self.assertEqual(adequacy["matched"], 5)
        self.assertLess(adequacy["coverage"], .8)
        self.assertFalse(gate["checks_without_family"]["actual_control_available"])
        self.assertFalse(gate["checks_without_family"]["actual_control_adequate"])

    def test_arm_diagnostics_keep_candidate_fixed_while_null_quotes_fill_gaps(self):
        """Sparse null pricing is visible without changing candidate evidence."""
        corpus = edge_corpus(36)
        keys = sorted({(str(row.get("symbol")), str(row.get("timestamp", ""))[:10])
                       for row in corpus
                       if str(row.get("kind", "bar")).lower() not in {
                           "quote", "equity_quote", "underlying_quote"}})
        self.assertEqual(len(keys), 288)

        def priced(prefix: str, index: int, key: tuple[str, str], net: float) -> dict:
            symbol, day = key
            return {
                "vehicle": "equity", "symbol": symbol, "session_date": day,
                "comparison_id": f"{symbol}:{day}",
                "opportunity_id": f"{prefix}:{index}", "no_trade": False,
                "net_pnl": net, "gross_pnl": net + .1, "costs": .1,
                "entry_fill_source": "quote", "exit_fill_source": "quote",
                "entry_feed": "iex", "exit_feed": "iex",
                "entry_provider": "fixture", "exit_provider": "fixture",
                "entry_quote_age_seconds": 0.0, "exit_quote_age_seconds": 0.0,
            }

        candidate = [priced("candidate", index, key, 1.0)
                     for index, key in enumerate(keys)]
        baseline = [priced("baseline", index, key, 0.0)
                    for index, key in enumerate(keys)]
        for added_quotes, expected_null in ((0, 265), (15, 280),
                                            (30, 288), (45, 288)):
            eligible = min(len(keys), 265 + added_quotes)
            null = [priced("null", index, key, 0.0)
                    if index < eligible else {
                        **priced("null", index, key, 0.0),
                        "no_trade": True,
                        "reject_reason": "no fresh equity quote at entry",
                    }
                    for index, key in enumerate(keys)]
            gate = edge_discovery_core._discover_gate(
                candidate, baseline, vehicle="equity", min_trades=1,
                min_sessions=1, alpha=1.0, test_iterations=100,
                null_rows=null)
            all_arms = gate["arm_diagnostics"]["all"]["arms"]
            self.assertEqual(
                all_arms["candidate"]["counts"]["eligible"], 288)
            self.assertEqual(
                all_arms["null"]["counts"]["eligible"], expected_null)
            pairing = gate["arm_diagnostics"]["all"]["pairing"][
                "candidate_vs_null"]
            self.assertEqual(pairing["matched"], expected_null)
            self.assertEqual(pairing["adequacy_reason"],
                             "full_pair_coverage" if expected_null == 288
                             else "partial_pair_coverage")

    def test_discover_uses_patchable_edge_lab_helper_alias(self):
        with mock.patch.object(edge_lab, "_read_discovery_rows",
                               side_effect=DiscoveryError("patched facade")):
            with self.assertRaisesRegex(DiscoveryError, "patched facade"):
                discover([], lane="backtest")


class DiscoveryCorpusStreamingTests(unittest.TestCase):
    """The corpus is fed row by row; what the replay computes is unchanged."""

    @staticmethod
    def _rows(sessions=3, per_session=4):
        rows = []
        for session in range(sessions):
            day = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc) + timedelta(days=session)
            for minute in range(per_session):
                stamp = (day + timedelta(minutes=minute)).isoformat()
                rows.append({"kind": "bar", "symbol": "SPY", "timestamp": stamp,
                             "as_of": stamp, "open": 100, "high": 101, "low": 99,
                             "close": 100.5, "volume": 10})
        return rows

    def _write(self, root: Path, rows, *, partitioned: bool):
        if not partitioned:
            target = root / "market.jsonl"
            target.write_text("".join(json.dumps(row, sort_keys=True) + "\n"
                                      for row in rows), encoding="utf-8")
            return target
        directory = root / "sessions"
        directory.mkdir()
        for row in rows:
            day = row["timestamp"][:10]
            with (directory / f"market-{day}.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
        return directory

    def test_partitioned_corpus_loads_exactly_like_one_file(self):
        rows = self._rows()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flat = edge_discovery_core._read_discovery_rows(
                self._write(root, rows, partitioned=False))
            split = edge_discovery_core._read_discovery_rows(
                self._write(root, rows, partitioned=True))
        self.assertEqual(flat[0], split[0])
        self.assertEqual(content_hash(flat[0]), content_hash(split[0]))
        self.assertEqual([vars(bar) for bar in flat[1]],
                         [vars(bar) for bar in split[1]])
        self.assertEqual((flat[2], flat[3]), (split[2], split[3]))

    def test_reader_streams_without_materializing_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self._write(Path(directory), self._rows(), partitioned=False)
            with mock.patch.object(
                    Path, "read_text",
                    side_effect=AssertionError("corpus must not be slurped")):
                raw, bars, _, _ = edge_discovery_core._read_discovery_rows(source)
        self.assertEqual(len(raw), 12)
        self.assertEqual(len(bars), 12)

    def test_session_window_bounds_work_by_window_not_corpus_size(self):
        loaded = {}
        with tempfile.TemporaryDirectory() as directory:
            for label, sessions in (("small", 4), ("large", 40)):
                root = Path(directory) / label
                root.mkdir()
                corpus = self._write(root, self._rows(sessions=sessions),
                                     partitioned=True)
                self.assertEqual(len(edge_discovery_core.corpus_partitions(
                    corpus, window=2)), 2)
                with mock.patch.dict(
                        os.environ,
                        {edge_discovery_core.SESSION_WINDOW_ENV: "2"}, clear=False):
                    windowed = edge_discovery_core._read_discovery_rows(corpus)
                whole = edge_discovery_core._read_discovery_rows(corpus)
                loaded[label] = (len(windowed[0]), len(whole[0]))
        self.assertEqual(loaded["small"][0], loaded["large"][0])
        self.assertEqual((loaded["small"][1], loaded["large"][1]), (16, 160))

    def test_invalid_partition_line_names_its_partition(self):
        with tempfile.TemporaryDirectory() as directory:
            corpus = self._write(Path(directory), self._rows(), partitioned=True)
            broken = sorted(corpus.glob("*.jsonl"))[-1]
            with broken.open("a", encoding="utf-8") as handle:
                handle.write("{not json\n")
            with self.assertRaisesRegex(DiscoveryError, broken.name):
                edge_discovery_core._read_discovery_rows(corpus)

    def test_large_file_spills_quotes_without_changing_dataset_hash(self):
        rows = self._rows(sessions=1)
        timestamp = rows[0]["timestamp"]
        rows.append({"kind": "quote", "provider": "alpaca", "feed": "sip",
                     "symbol": "SPY", "timestamp": timestamp,
                     "as_of": timestamp, "observed_at": timestamp,
                     "bid": 99.9, "ask": 100.1})
        with tempfile.TemporaryDirectory() as directory:
            source = self._write(Path(directory), rows, partitioned=False)
            with mock.patch.object(edge_discovery_core, "QUOTE_INDEX_MIN_BYTES", 1):
                raw, bars, _, quotes = edge_discovery_core._read_discovery_rows(source)
            self.assertIsInstance(raw, edge_discovery_core._StreamingRawRows)
            self.assertIsInstance(quotes, SQLiteQuoteIndex)
            self.assertEqual(content_hash(raw), content_hash(rows))
            self.assertAlmostEqual(
                quote_fill(quotes, symbol="SPY", at=datetime.fromisoformat(timestamp),
                           side="buy"), 100.1, places=9)
            quotes.close()


class EdgeDiscoveryLifecycleTests(unittest.TestCase):
    def test_trade_metrics_reject_nonfinite_values(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity",
                hypothesis="finite evidence", config={})
            run = ledger.append_run(candidate["candidate_id"], lane="backtest")
            base = {
                "session_date": "2024-01-02",
                "opportunity_id": "finite-check",
            }
            for value in ("inf", "-inf", "nan"):
                with self.subTest(net_pnl=value), self.assertRaisesRegex(
                        ValueError, "net_pnl must be finite"):
                    ledger.append_trade(run["run_id"], {**base, "net_pnl": value})
                with self.subTest(return_value=value), self.assertRaisesRegex(
                        ValueError, "return_value must be finite"):
                    ledger.append_trade(run["run_id"], {
                        **base, "opportunity_id": f"return-{value}",
                        "net_pnl": 1.0, "return_value": value,
                    })
            for field in ("net_pnl", "return_value"):
                for invalid in (True, b"1.0", 10 ** 10000):
                    with self.subTest(field=field, value=type(invalid).__name__), \
                            self.assertRaisesRegex(ValueError, f"{field} must be numeric"):
                        ledger.append_trade(run["run_id"], {
                            **base, "opportunity_id": f"invalid-{field}",
                            "net_pnl": 1.0, field: invalid,
                        })

    def test_verified_gate_rejects_malformed_scalars_without_evidence_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="scalar proof",
                config={})
            run, envelope = _persist_gate(
                ledger, candidate["candidate_id"], "backtest", record=False)

            def rehash(value):
                value["content_hash"] = content_hash({
                    key: item for key, item in value.items()
                    if key != "content_hash"})
                return value

            malformed = [
                ("floor count None", lambda value: value["floors"]["fit"].__setitem__(
                    "trades", None)),
                ("floor count bool", lambda value: value["floors"]["fit"].__setitem__(
                    "trades", True)),
                ("floor minimum huge", lambda value: value["floors"]["fit"]["minimums"].__setitem__(
                    "trades", 10 ** 4000)),
                ("floor structural string", lambda value: value["floors"]["fit"][
                    "structural_checks"].__setitem__("trades", "true")),
                ("floor required number", lambda value: value["floors"]["fit"].__setitem__(
                    "required", 1)),
                ("statistics None", lambda value: value["statistics"].__setitem__(
                    "p_value", None)),
                ("statistics bool", lambda value: value["statistics"].__setitem__(
                    "alpha", True)),
                ("statistics huge", lambda value: value["statistics"].__setitem__(
                    "q_value", 10 ** 4000)),
                ("control delta None", lambda value: value["control"].__setitem__(
                    "mean_delta", None)),
                ("control delta string", lambda value: value["control"].__setitem__(
                    "mean_delta", "bad")),
                ("matched bool", lambda value: value["control"].__setitem__(
                    "matched", True)),
                ("matched huge", lambda value: value["control"].__setitem__(
                    "matched", 10 ** 4000)),
                ("performance bool", lambda value: value["performance"].__setitem__(
                    "heldout_delta", True)),
                ("performance drawdown negative", lambda value: value["performance"].__setitem__(
                    "max_drawdown", -1.0)),
                ("falsification string", lambda value: value["falsification"].__setitem__(
                    "passes", "yes")),
            ]
            for name, mutate in malformed:
                with self.subTest(name=name):
                    candidate_gate = json.loads(json.dumps(envelope))
                    mutate(candidate_gate)
                    with self.assertRaises(ValueError):
                        ledger.record_verified_gate(run["run_id"], rehash(candidate_gate))
                    with closing(sqlite3.connect(ledger.path)) as db:
                        self.assertEqual(
                            db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 0)

            for invalid_gate in (None, True, b"gate", []):
                with self.subTest(gate=type(invalid_gate).__name__), self.assertRaises(
                        ValueError):
                    ledger.record_verified_gate(run["run_id"], invalid_gate)

    def test_select_champion_skips_corrupt_scoring_data(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="corrupt score",
                config={})
            candidate_id = candidate["candidate_id"]
            _persist_gate(ledger, candidate_id, "backtest")
            ledger.transition(candidate_id, "backtest_passed", reason="backtest proof")
            run, envelope = _persist_gate(ledger, candidate_id, "shadow")
            ledger.transition(candidate_id, "shadow", reason="shadow proof")
            ledger.transition(candidate_id, "validated", reason="validated proof")
            malformed = json.loads(json.dumps(envelope))
            malformed["performance"]["heldout_delta"] = True
            with mock.patch.object(ledger, "_latest_verified_gate",
                                   return_value=(run, malformed)):
                self.assertIsNone(ledger.select_champion(
                    vehicle="equity", min_confidence=.9))

    def test_failed_verified_gate_rejects_malformed_control_and_proof_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="failed scalar proof",
                config={})
            run, envelope = _persist_gate(
                ledger, candidate["candidate_id"], "backtest", passes=False,
                record=False)

            def rehash(value):
                value["content_hash"] = content_hash({
                    key: item for key, item in value.items()
                    if key != "content_hash"})
                return value

            def remove(mapping, key):
                mapping.pop(key, None)

            malformed = [
                ("matched None", lambda value: value["control"].__setitem__(
                    "matched", None)),
                ("matched bool", lambda value: value["control"].__setitem__(
                    "matched", True)),
                ("actual control missing", lambda value: remove(
                    value["control"], "actual_control")),
                ("available missing", lambda value: remove(
                    value["control"], "available")),
                ("falsification missing", lambda value: remove(
                    value, "falsification")),
                ("falsification scalar", lambda value: value.__setitem__(
                    "falsification", "bad")),
                ("falsification pass missing", lambda value: remove(
                    value["falsification"], "passes")),
                ("separation missing", lambda value: remove(value, "separation")),
                ("separation scalar", lambda value: value.__setitem__(
                    "separation", 1)),
                ("separation pass missing", lambda value: remove(
                    value["separation"], "passes")),
            ]
            for name, mutate in malformed:
                with self.subTest(name=name):
                    candidate_gate = json.loads(json.dumps(envelope))
                    mutate(candidate_gate)
                    with self.assertRaises(ValueError):
                        ledger.record_verified_gate(run["run_id"], rehash(candidate_gate))
                    with closing(sqlite3.connect(ledger.path)) as db:
                        self.assertEqual(
                            db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 0)

    def test_failed_zero_match_gate_with_none_delta_reverifies(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="no control proof",
                config={})
            run, envelope = _persist_gate(
                ledger, candidate["candidate_id"], "backtest", passes=False,
                record=False)
            # A genuinely unmatched control carries no deltas at all, so the
            # recomputed statistics must be empty rather than merely relabelled.
            envelope["control"].update({
                "matched": 0, "available": False, "mean_delta": None,
                "mean_delta_lcb": None, "deltas": [], "delta_clusters": [],
            })
            envelope["checks"]["heldout_delta_positive"] = False
            envelope["checks"]["heldout_delta_lcb_positive"] = False
            envelope["checks"]["actual_control_available"] = False
            envelope["statistics"]["p_value"] = 1.0
            envelope["performance"]["heldout_delta"] = None
            envelope["performance"]["heldout_delta_lcb"] = None
            envelope["content_hash"] = content_hash({
                key: item for key, item in envelope.items()
                if key != "content_hash"})
            # The current epoch seals a proof to the immutable run metrics.
            # Rebuild the diagnostic run after changing its zero-match report
            # so the fixture models a producer that sealed this envelope.
            run = ledger.append_run(
                candidate["candidate_id"], lane="backtest", vehicle="equity",
                fit=envelope["fit_source"], heldout=envelope["heldout_source"],
                metrics={"gate": {"verified_gate": envelope,
                                   "gate_hash": envelope["content_hash"]}})
            for row in [*envelope["fit_source"], *envelope["heldout_source"]]:
                ledger.append_trade(run["run_id"], row)
            ledger.record_verified_gate(run["run_id"], envelope)
            proof = ledger.latest_verified_run(candidate["candidate_id"], lane="backtest")
            self.assertIsNotNone(proof)
            self.assertIsNone(proof["verified_gate"]["control"]["mean_delta"])
            self.assertIsNone(proof["verified_gate"]["performance"]["heldout_delta"])

    def test_verified_gate_binds_exact_durable_trade_rows_not_just_counts(self):
        """Same-count rows from another replay cannot borrow a gate proof."""
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", hypothesis="durable source binding", config={})
            _, envelope = _persist_gate(
                ledger, candidate["candidate_id"], "backtest", record=False)
            source = [*envelope["fit_source"], *envelope["heldout_source"]]
            altered = []
            for index, row in enumerate(source):
                copy = dict(row)
                # Keep aggregate P&L and all floors unchanged while altering
                # the durable payload in both partitions.
                if index == 0:
                    copy["net_pnl"] = float(copy["net_pnl"]) + 1.0
                elif index == 1:
                    copy["net_pnl"] = float(copy["net_pnl"]) - 1.0
                elif index == len(envelope["fit_source"]):
                    copy["net_pnl"] = float(copy["net_pnl"]) + 1.0
                elif index == len(envelope["fit_source"]) + 1:
                    copy["net_pnl"] = float(copy["net_pnl"]) - 1.0
                elif index == len(envelope["fit_source"]) + 2:
                    copy["opportunity_id"] = str(copy["opportunity_id"]) + "-forged"
                altered.append(copy)
            fit_count = len(envelope["fit_source"])
            run = ledger.append_run(
                candidate["candidate_id"], lane="backtest", vehicle="equity",
                fit=altered[:fit_count], heldout=altered[fit_count:],
                metrics={"gate": {"verified_gate": envelope,
                                   "gate_hash": envelope["content_hash"]}})
            for row in altered:
                ledger.append_trade(run["run_id"], row)
            with self.assertRaisesRegex(ValueError, "source rows"):
                ledger.record_verified_gate(run["run_id"], envelope)
            with closing(sqlite3.connect(ledger.path)) as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 0)

    def test_verified_gate_binds_candidate_identity_and_shadow_source(self):
        """UUID/variant aliases work, while missing or transplanted ids fail."""
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", hypothesis="candidate source binding", config={})

            def rehash(value):
                value["content_hash"] = content_hash({
                    key: item for key, item in value.items()
                    if key != "content_hash"})
                return value

            def append_gate_run(owner, gate, *, metrics=None):
                fit = gate["fit_source"]
                heldout = gate["heldout_source"]
                run = ledger.append_run(
                    owner["candidate_id"], lane=gate["lane"], vehicle="equity",
                    fit=fit, heldout=heldout,
                    metrics=metrics or {"gate": {
                        "verified_gate": gate,
                        "gate_hash": gate["content_hash"]}})
                for row in [*fit, *heldout]:
                    ledger.append_trade(run["run_id"], row)
                return run

            # The UUID is the current durable identity and remains valid.
            run_uuid, envelope = _persist_gate(
                ledger, candidate["candidate_id"], "backtest", record=False)
            ledger.record_verified_gate(run_uuid["run_id"], envelope)

            # Historical producers used the immutable variant id; it is the
            # only compatibility alias accepted by the proof boundary.
            _, variant_gate = _persist_gate(
                ledger, candidate["candidate_id"], "backtest", record=False)
            variant_gate = copy.deepcopy(variant_gate)
            variant_gate["candidate_id"] = candidate["variant_id"]
            variant_gate["qualification"]["post_selection"]["candidate_id"] = \
                candidate["variant_id"]
            inconsistent_alias = rehash(copy.deepcopy(variant_gate))
            self.assertFalse(gates.verify_gate_envelope(inconsistent_alias))
            # v3 binds the complete multiple-testing batch to the same
            # candidate identity as the envelope.  Model a producer that used
            # the historical variant alias consistently throughout the proof,
            # rather than leaving a contradictory UUID inside the FDR batch.
            batch = variant_gate["fdr_batch"]
            original_id = candidate["candidate_id"]
            alias = candidate["variant_id"]
            batch["candidate_id"] = alias
            batch["family_candidate_key"] = alias
            batch["global_candidate_key"] = alias
            family = batch["family_name"]
            batch["family_values"][family][alias] = \
                batch["family_values"][family].pop(original_id)
            batch["family_results"][family][alias] = \
                batch["family_results"][family].pop(original_id)
            batch["global_values"][alias] = \
                batch["global_values"].pop(original_id)
            batch["global_results"][alias] = \
                batch["global_results"].pop(original_id)
            variant_gate = rehash(variant_gate)
            run_variant = append_gate_run(candidate, variant_gate)
            ledger.record_verified_gate(run_variant["run_id"], variant_gate)

            # A source-backed v3 envelope without an identity is internally
            # invalid before it can reach the ledger's candidate-alias check:
            # the complete FDR batch is bound to that same identity.
            _, missing_gate = _persist_gate(
                ledger, candidate["candidate_id"], "backtest", record=False)
            missing_gate = copy.deepcopy(missing_gate)
            missing_gate["candidate_id"] = None
            missing_gate = rehash(missing_gate)
            run_missing = append_gate_run(candidate, missing_gate)
            self.assertFalse(gates.verify_gate_envelope(missing_gate))
            with self.assertRaisesRegex(ValueError, "envelope/hash is invalid"):
                ledger.record_verified_gate(run_missing["run_id"], missing_gate)

            # A live-shadow run carries an independent source payload.  Its
            # candidate identity is bound separately from the gate alias.
            _, shadow_gate = _persist_gate(
                ledger, candidate["candidate_id"], "shadow", record=False)
            run_shadow = append_gate_run(
                candidate, shadow_gate,
                metrics={"gate": {"verified_gate": shadow_gate,
                                   "gate_hash": shadow_gate["content_hash"]},
                         "shadow_source": {"candidate_id": "forged-candidate"}})
            with self.assertRaisesRegex(ValueError, "shadow source"):
                ledger.record_verified_gate(run_shadow["run_id"], shadow_gate)

            # The same-count evidence from a different candidate cannot be
            # transplanted, even if its trade rows happen to be identical.
            other = ledger.register_candidate(
                "ibr.target.3r", hypothesis="other candidate", config={})
            run_other = append_gate_run(
                other, envelope,
                metrics={"gate": {"verified_gate": envelope,
                                   "gate_hash": envelope["content_hash"]}})
            with self.assertRaisesRegex(ValueError, "candidate identity"):
                ledger.record_verified_gate(run_other["run_id"], envelope)

    def test_verified_gate_rejects_durable_trade_scalar_column_tampering(self):
        """Indexed trade columns cannot diverge from their immutable payload."""
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", hypothesis="scalar source binding", config={})
            other = ledger.register_candidate(
                "ibr.target.3r", hypothesis="other scalar source", config={})
            mutations = {
                "net_pnl": 99.0,
                "session_date": "2099-01-01",
                "opportunity_id": "forged-opportunity",
                "candidate_id": other["candidate_id"],
            }
            for column, value in mutations.items():
                with self.subTest(column=column):
                    run, envelope = _persist_gate(
                        ledger, candidate["candidate_id"], "backtest", record=False)
                    with closing(sqlite3.connect(ledger.path)) as db, db:
                        # The production schema is append-only.  Temporarily
                        # disabling the guard models direct storage corruption
                        # without weakening the real trigger.
                        db.execute("DROP TRIGGER trades_no_update")
                        db.execute(
                            f"UPDATE trades SET {column}=? WHERE trade_id="
                            "(SELECT trade_id FROM trades WHERE run_id=? LIMIT 1)",
                            (value, run["run_id"]))
                        db.execute(
                            "CREATE TRIGGER trades_no_update BEFORE UPDATE ON trades "
                            "BEGIN SELECT RAISE(ABORT, 'trades are immutable'); END")
                    with self.assertRaisesRegex(ValueError, "scalar columns"):
                        ledger.record_verified_gate(run["run_id"], envelope)

    def test_none_delta_is_rejected_for_contradictory_gate_state(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="contradictory proof",
                config={})

            def rehash(value):
                value["content_hash"] = content_hash({
                    key: item for key, item in value.items()
                    if key != "content_hash"})
                return value

            malformed = [
                ("matched positive", lambda value: value["control"].__setitem__(
                    "matched", 1)),
                ("control available", lambda value: value["control"].__setitem__(
                    "available", True)),
                ("heldout check positive", lambda value: value["checks"].__setitem__(
                    "heldout_delta_positive", True)),
                ("control delta missing", lambda value: value["control"].pop(
                    "mean_delta", None)),
                ("performance delta missing", lambda value: value["performance"].pop(
                    "heldout_delta", None)),
                ("performance drawdown missing", lambda value: value["performance"].pop(
                    "max_drawdown", None)),
                ("performance string", lambda value: value["performance"].__setitem__(
                    "heldout_delta", "bad")),
            ]
            for name, mutate in malformed:
                with self.subTest(name=name):
                    run, envelope = _persist_gate(
                        ledger, candidate["candidate_id"], "backtest", passes=False,
                        record=False)
                    envelope["control"].update({
                        "matched": 0, "available": False, "mean_delta": None,
                    })
                    envelope["checks"]["heldout_delta_positive"] = False
                    envelope["performance"]["heldout_delta"] = None
                    mutate(envelope)
                    with self.assertRaises(ValueError):
                        ledger.record_verified_gate(run["run_id"], rehash(envelope))
                    with closing(sqlite3.connect(ledger.path)) as db:
                        self.assertEqual(
                            db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 0)

    def test_paper_outcomes_reject_malformed_input_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="paper scalar",
                config={})
            invalid = [
                None, True, b"outcome", bytearray(b"outcome"),
                {"net_pnl": True, "risk_usd": 1},
                {"net_pnl": b"1", "risk_usd": 1},
                {"net_pnl": 10 ** 10000, "risk_usd": 1},
                {"net_pnl": 1, "risk_usd": 10 ** 10000},
                {"net_pnl": 1, "risk_usd": 0},
                {"net_pnl": 1, "risk_usd": float("inf")},
            ]
            for value in invalid:
                with self.subTest(value=type(value).__name__), self.assertRaisesRegex(
                        ValueError, "paper outcome requires finite net_pnl and positive risk_usd"):
                    ledger.ingest_paper_outcome(candidate["candidate_id"], value)
            with closing(sqlite3.connect(ledger.path)) as db:
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM paper_outcomes").fetchone()[0], 0)
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_paper_replay_skips_malformed_persisted_r_multiple(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="paper replay",
                config={})
            candidate_id = candidate["candidate_id"]
            malformed = (True, [], 10 ** 4000)
            with closing(sqlite3.connect(ledger.path)) as db, db:
                for index, value in enumerate(malformed):
                    db.execute(
                        "INSERT INTO paper_outcomes "
                        "(outcome_id,candidate_id,vehicle,opportunity_id,session_date,net_pnl,outcome_json,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (f"legacy-{index}", candidate_id, "equity", f"legacy-{index}",
                         "2024-01-01", 0.0, json.dumps({"r_multiple": value}), index))
            result = ledger.ingest_paper_outcome(candidate_id, {
                "vehicle": "equity", "opportunity_id": "paper-valid",
                "net_pnl": 1, "risk_usd": 10,
            })
            self.assertEqual(result["rolling_outcomes"], 1)
            self.assertEqual(result["rolling_r"], .1)

    def test_paper_outcomes_keep_a_champion_deployed_on_rolling_guard_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity",
                hypothesis="earlier target", config={"strategy": {"target_r": 1.5}})
            candidate_id = candidate["candidate_id"]
            for lane in ("backtest", "shadow"):
                _persist_gate(ledger, candidate_id, lane)
                if lane == "backtest":
                    ledger.transition(candidate_id, "backtest_passed",
                                      reason="backtest gates passed")
                else:
                    ledger.transition(candidate_id, "shadow",
                                      reason="shadow evidence started")
                    ledger.transition(candidate_id, "validated",
                                      reason="shadow gates passed")
            ledger.transition(candidate_id, "champion",
                              reason="best validated evidence")
            for index in range(20):
                outcome = ledger.ingest_paper_outcome(candidate_id, {
                    "vehicle": "equity", "opportunity_id": f"paper-{index}",
                    "session_date": f"2024-02-{index + 1:02d}",
                    "net_pnl": -1, "risk_usd": 10, "r_multiple": 999,
                })
            self.assertTrue(outcome["rolling_guard_breached"])
            self.assertIsNone(outcome["guard_breach"])
            self.assertEqual(outcome["status"], "champion")
            self.assertEqual(ledger.candidate(candidate_id)["status"], "champion")

    def _deployed_candidate(self, ledger, *, r_multiples=None):
        candidate = ledger.register_candidate(
            "ibr.target.1_5r", vehicle="equity", hypothesis="deployed guard",
            config={"strategy": {"target_r": 1.5}})
        candidate_id = candidate["candidate_id"]
        _persist_gate(ledger, candidate_id, "backtest")
        ledger.transition(candidate_id, "backtest_passed", reason="backtest gates passed")
        _persist_gate(ledger, candidate_id, "shadow", scores=[1.0] * 20,
                      r_multiples=r_multiples)
        ledger.transition(candidate_id, "shadow", reason="shadow evidence started")
        ledger.transition(candidate_id, "validated", reason="shadow gates passed")
        return candidate_id

    def _ingest(self, ledger, candidate_id, values, *, prefix="paper",
                frozen=False, pin_context=None):
        result = {}
        for index, r_multiple in enumerate(values):
            result = ledger.ingest_paper_outcome(candidate_id, {
                "vehicle": "equity", "opportunity_id": f"{prefix}-{index}",
                "session_date": f"2024-02-{index + 1:02d}",
                "net_pnl": r_multiple * 10, "risk_usd": 10,
            }, frozen=frozen, pin_context=pin_context)
        return result

    def test_rolling_guard_warning_keeps_a_losing_validated_non_champion_deployed(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate_id = self._deployed_candidate(ledger)
            result = self._ingest(ledger, candidate_id, [-0.1] * 20)
            self.assertTrue(result["rolling_guard_breached"])
            self.assertIsNone(result["guard_breach"])
            self.assertEqual(result["status"], "validated")
            self.assertEqual(ledger.candidate(candidate_id)["status"], "validated")
            transitions = [(row["from_status"], row["to_status"])
                           for row in ledger.history(candidate_id)
                           if row["event_type"] == "safety_demotion"]
            self.assertEqual(transitions, [])

    def test_repeated_ingestion_of_one_opportunity_is_a_single_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate_id = self._deployed_candidate(ledger)
            self._ingest(ledger, candidate_id, [-0.1] * 20)
            outcome = {"vehicle": "equity", "opportunity_id": "paper-repeat",
                       "net_pnl": -1, "risk_usd": 10}
            first = ledger.ingest_paper_outcome(candidate_id, outcome)
            repeats = [ledger.ingest_paper_outcome(candidate_id, outcome)
                       for _ in range(3)]
            self.assertFalse(first["duplicate"])
            self.assertTrue(all(row["duplicate"] for row in repeats))
            self.assertEqual({row["outcome_id"] for row in repeats},
                             {first["outcome_id"]})
            self.assertTrue(first["rolling_guard_breached"])
            self.assertIsNone(first["guard_breach"])
            self.assertTrue(all(row["rolling_guard_breached"] for row in repeats))
            self.assertTrue(all(row["guard_breach"] is None for row in repeats))
            # The fixed-size window drops the oldest of the 21 observations.
            # ``rolling_r`` is a running float sum, so twenty -0.1R rows land
            # one unit in the last place away from -2.0. Compare on the
            # telemetry's own tolerance rather than on exact float equality.
            self.assertEqual(len({row["rolling_r"] for row in repeats}), 1)
            for row in repeats:
                self.assertAlmostEqual(row["rolling_r"], -2.0, places=9)
            self.assertEqual(ledger.candidate(candidate_id)["status"], "validated")
            with closing(sqlite3.connect(ledger.path)) as db:
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM paper_outcomes").fetchone()[0], 21)

    def test_drift_demotes_a_deployed_candidate_whose_paper_r_collapses(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate_id = self._deployed_candidate(
                ledger, r_multiples=[.5, 1.5] * 10)
            # The rolling floor cannot see this: twenty outcomes summing to
            # -1R stay above the -2R floor.  Only the sequential test against
            # the validated held-out distribution proves the edge is gone.
            result = self._ingest(ledger, candidate_id, [-0.05] * 20)
            self.assertGreater(result["rolling_r"], -2.0)
            self.assertTrue(result["drift"]["degraded"])
            self.assertGreaterEqual(result["drift"]["statistic"], 4.0)
            self.assertFalse(result["rolling_guard_breached"])
            self.assertEqual(result["guard_breach"], "heldout_drift")
            self.assertEqual(result["status"], "demoted")

    def test_pinned_heldout_drift_preserves_the_demote_and_pause_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate_id = self._deployed_candidate(
                ledger, r_multiples=[.5, 1.5] * 10)
            pin_context = {"variant_id": "ibr.target.1_5r",
                           "vehicle": "equity", "note": "operator pin"}
            result = self._ingest(ledger, candidate_id, [-0.05] * 20,
                                  frozen=True, pin_context=pin_context)
            self.assertEqual(result["guard_breach"], "heldout_drift")
            self.assertEqual(result["status"], "demoted")
            demotions = [json.loads(event["payload_json"])
                         for event in ledger.history(candidate_id)
                         if event["event_type"] == "safety_demotion"]
            self.assertTrue(demotions)
            self.assertTrue(demotions[-1]["pinned"])
            self.assertEqual(demotions[-1]["pin_context"], pin_context)
            self.assertEqual(demotions[-1]["action"], "demote_and_pause")

    def test_drift_stays_quiet_while_paper_r_wobbles_around_the_validated_mean(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate_id = self._deployed_candidate(
                ledger, r_multiples=[.5, 1.5] * 10)
            result = self._ingest(ledger, candidate_id, [.8, 1.2] * 15)
            self.assertTrue(result["drift"]["applicable"])
            self.assertFalse(result["drift"]["degraded"])
            self.assertLess(result["drift"]["statistic"], 0.0)
            self.assertEqual(result["status"], "validated")

    def test_drift_tolerates_a_single_severe_loss_inside_the_validated_spread(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate_id = self._deployed_candidate(
                ledger, r_multiples=[.5, 1.5] * 10)
            result = self._ingest(ledger, candidate_id, [1.0] * 19 + [-3.0])
            self.assertFalse(result["drift"]["degraded"])
            self.assertEqual(result["status"], "validated")

    def test_drift_is_inapplicable_without_a_risk_normalized_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate_id = self._deployed_candidate(ledger)
            self.assertIsNone(ledger.heldout_reference(candidate_id))
            result = self._ingest(ledger, candidate_id, [.5] * 20)
            self.assertFalse(result["drift"]["applicable"])
            self.assertEqual(result["status"], "validated")

    def test_paper_outcomes_recompute_r_and_reject_manual_demotion(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity",
                hypothesis="paper guard", config={"strategy": {"target_r": 1.5}})
            with self.assertRaisesRegex(ValueError, "positive risk_usd"):
                ledger.ingest_paper_outcome(candidate["candidate_id"], {
                    "vehicle": "equity", "net_pnl": -1, "r_multiple": -100,
                    "demote": True,
                })
            result = ledger.ingest_paper_outcome(candidate["candidate_id"], {
                "vehicle": "equity", "opportunity_id": "paper-safe",
                "net_pnl": -1, "risk_usd": 10, "r_multiple": -100,
                "demote": True,
            })
            self.assertEqual(result["rolling_r"], -0.1)
            self.assertEqual(result["status"], "candidate")

    def test_discovery_operational_error_is_distinct_from_insufficient_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            options_only = root / "options.jsonl"
            options_only.write_text(
                json.dumps({"kind": "quote", "symbol": "SPY"}) + "\n",
                encoding="utf-8")
            result = subprocess.run([
                sys.executable, "research.py", "edge", "discover",
                "--data", str(options_only), "--db", str(root / "edge.sqlite3")],
                cwd=Path(__file__).resolve().parents[2], check=False,
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)

    def test_forward_lifecycle_cannot_be_manually_advanced_without_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity",
                hypothesis="earlier target", config={"strategy": {"target_r": 1.5}})
            candidate_id = candidate["candidate_id"]
            with self.assertRaisesRegex(ValueError, "verified gate evidence"):
                ledger.transition(candidate_id, "backtest_passed", reason="operator request")
            ledger.append_run(
                candidate_id, lane="backtest", vehicle="equity",
                fit=[{"session_date": "2024-01-02"}],
                heldout=[{"session_date": "2024-01-03"}],
                metrics={"gate": {"passes": True}})
            with self.assertRaisesRegex(ValueError, "lacks verified gate evidence"):
                ledger.transition(candidate_id, "backtest_passed", reason="forged metrics")
            _persist_gate(ledger, candidate_id, "backtest")
            self.assertEqual(ledger.transition(
                candidate_id, "backtest_passed", reason="gates passed")["status"],
                "backtest_passed")
            with self.assertRaisesRegex(ValueError, "verified gate evidence"):
                ledger.transition(candidate_id, "shadow", reason="operator request")

    def test_retirement_rollback_and_forged_gate_cannot_bypass_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="earlier target",
                config={"strategy": {"target_r": 1.5}})
            candidate_id = candidate["candidate_id"]
            with self.assertRaisesRegex(ValueError, "verified gate evidence"):
                ledger.transition(candidate_id, "retired", reason="manual retirement")
            with self.assertRaisesRegex(ValueError, "rollback cannot bypass evidence"):
                ledger.transition(candidate_id, "candidate", reason="rollback", rollback=True)
            run, envelope = _persist_gate(
                ledger, candidate_id, "backtest", passes=True, record=False)
            envelope["passes"] = False
            with self.assertRaisesRegex(ValueError, "envelope/hash"):
                ledger.record_verified_gate(run["run_id"], envelope)

    def test_positive_but_inconclusive_gate_cannot_retire_a_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity",
                hypothesis="positive but not statistically authorized",
                config={"strategy": {"target_r": 1.5}})
            candidate_id = candidate["candidate_id"]
            _persist_gate(
                ledger, candidate_id, "backtest", passes=False,
                scores=[1.0] * 8)
            with self.assertRaisesRegex(ValueError, "terminally negative"):
                ledger.transition(
                    candidate_id, "retired", reason="inconclusive gate failure")
            self.assertEqual(ledger.candidate(candidate_id)["status"], "candidate")

    def test_latest_failing_proof_makes_a_champion_ineligible(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.target.1_5r", vehicle="equity", hypothesis="earlier target",
                config={"strategy": {"target_r": 1.5}})
            candidate_id = candidate["candidate_id"]
            _persist_gate(ledger, candidate_id, "backtest")
            ledger.transition(candidate_id, "backtest_passed", reason="backtest proof")
            _persist_gate(ledger, candidate_id, "shadow")
            ledger.transition(candidate_id, "shadow", reason="shadow proof")
            ledger.transition(candidate_id, "validated", reason="validated proof")
            self.assertIsNotNone(ledger.select_champion(
                vehicle="equity", min_confidence=.9))
            self.assertTrue(ledger.eligibility(candidate_id)["eligible"])
            self.assertEqual(
                ledger.latest_verified_run(candidate_id, lane="shadow")["lane"],
                "shadow")
            _persist_gate(ledger, candidate_id, "shadow", passes=False)
            self.assertIsNone(ledger.select_champion(
                vehicle="equity", min_confidence=.9))
            self.assertFalse(ledger.eligibility(candidate_id)["eligible"])
            with self.assertRaisesRegex(
                    ValueError, "passing verified shadow proof"):
                ledger.ingest_paper_outcome(candidate_id, {
                    "opportunity_id": "post-failed-proof",
                    "session_date": "2026-01-01",
                    "net_pnl": 1.0,
                    "risk_usd": 1.0,
                })

    def _validated_candidate(self, ledger, variant, hypothesis, scores):
        candidate = ledger.register_candidate(
            variant, vehicle="equity", hypothesis=hypothesis, config={})
        candidate_id = candidate["candidate_id"]
        for lane in ("backtest", "shadow"):
            _persist_gate(ledger, candidate_id, lane, scores=scores)
            ledger.transition(candidate_id,
                              "backtest_passed" if lane == "backtest" else "shadow",
                              reason=f"{lane} proof")
        ledger.transition(candidate_id, "validated", reason="validated proof")
        return candidate_id

    # A positive point estimate is not evidence.  SPIKY carries the higher
    # mean held-out delta; STEADY the higher lower bound.  Both are positive,
    # so both pass the gate and the ranking rule alone separates them.
    STEADY = [3.5] * 8
    # Chronological runs matter under the moving-block bootstrap.  This series
    # has the higher raw mean, but its clustered high/low regimes produce a
    # lower conservative bound than the steady candidate.
    SPIKY = [9.0, 9.0, 9.0, 9.0, -1.0, -1.0, -1.0, -1.0]
    # Same mean as a flat +1 series, but a lower bound below zero.
    ERRATIC = [30.0, -28.0, 25.0, -22.0, 20.0, -18.0, 15.0, -14.0]

    def test_a_non_positive_lower_bound_cannot_be_recorded_as_a_passing_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.range.30", vehicle="equity",
                hypothesis="positive mean, lower bound below zero", config={})
            run, envelope = _persist_gate(
                ledger, candidate["candidate_id"], "backtest",
                scores=self.ERRATIC, record=False)
            self.assertGreater(envelope["performance"]["heldout_delta"], 0)
            self.assertLessEqual(envelope["performance"]["heldout_delta_lcb"], 0)
            self.assertFalse(envelope["checks"]["heldout_delta_lcb_positive"])
            # The builder has already downgraded the claim to a failed gate.
            # Persisting that negative evidence is valid; only recording it as
            # a pass or using it to advance the lifecycle would be a bypass.
            self.assertFalse(envelope["passes"])
            ledger.record_verified_gate(run["run_id"], envelope)
            with self.assertRaisesRegex(ValueError, "passing backtest"):
                ledger.transition(
                    candidate["candidate_id"], "backtest_passed",
                    reason="non-positive lower bound")

    def test_champion_ranks_by_lower_bound_not_raw_heldout_delta(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            steady = self._validated_candidate(
                ledger, "ibr.baseline", "lower mean, tighter spread", self.STEADY)
            spiky = self._validated_candidate(
                ledger, "ibr.range.45", "higher mean, wider spread", self.SPIKY)
            selected = ledger.select_champion(vehicle="equity", min_confidence=.9)
            self.assertIsNotNone(selected)
            # Ranking on raw held-out delta would have picked ``spiky``.
            self.assertEqual(selected["candidate_id"], steady)
            self.assertNotEqual(selected["candidate_id"], spiky)

    def test_new_champion_keeps_previous_proved_edge_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            candidate = ledger.register_candidate(
                "ibr.baseline", vehicle="equity", hypothesis="proved edge 1",
                config={"strategy": {"target_r": 2.0}})
            first_id = candidate["candidate_id"]
            _persist_gate(ledger, first_id, "backtest")
            ledger.transition(first_id, "backtest_passed", reason="backtest proof")
            _persist_gate(ledger, first_id, "shadow")
            ledger.transition(first_id, "shadow", reason="shadow proof")
            ledger.transition(first_id, "validated", reason="validated proof")
            first = ledger.select_champion(vehicle="equity", min_confidence=.9)
            self.assertIsNotNone(first)

            candidate = ledger.register_candidate(
                "ibr.target.3r", vehicle="equity", hypothesis="proved edge 2",
                config={"strategy": {"target_r": 3.0}})
            second_id = candidate["candidate_id"]
            _persist_gate(ledger, second_id, "backtest", score=2.0)
            ledger.transition(second_id, "backtest_passed", reason="backtest proof")
            _persist_gate(ledger, second_id, "shadow", score=2.0)
            ledger.transition(second_id, "shadow", reason="shadow proof")
            ledger.transition(second_id, "validated", reason="validated proof")

            # Explicit evidence-authorized champion selection is also used by
            # the conservative ranker when a stronger candidate appears.
            ledger.transition(second_id, "champion", reason="stronger conservative evidence")
            selected = ledger.select_champion(vehicle="equity", min_confidence=.9)
            self.assertIsNotNone(selected)
            self.assertEqual((ledger.candidate(first_id) or {})["status"], "validated")
            self.assertEqual((ledger.candidate(second_id) or {})["status"], "champion")

    def test_auto_requires_a_later_forward_tail_for_shadow(self):
        registry = {"variants": [
            {"variant_id": "ibr.baseline", "strategy_id": "ibr",
             "base_version": "v1", "overrides": {}, "vehicles": ["equity"],
             "hypothesis": "registered baseline"},
            {"variant_id": "ibr.target.1_5r", "strategy_id": "ibr",
             "base_version": "v1", "overrides": {"strategy.target_r": 1.5},
             "vehicles": ["equity"], "hypothesis": "earlier target"},
        ]}
        config = {"strategy": {"range_minutes": 1, "range_stop": True,
                                "target_r": 2}}
        first = _sessions(datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc), 20)
        later = _sessions(datetime(2024, 2, 1, 14, 30, tzinfo=timezone.utc), 20)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_one = root / "first.jsonl"
            data_all = root / "all.jsonl"
            variants = root / "variants.yaml"
            db = root / "edge.sqlite3"
            data_one.write_text("\n".join(json.dumps(row) for row in first), encoding="utf-8")
            data_all.write_text("\n".join(json.dumps(row) for row in first + later), encoding="utf-8")
            variants.write_text(json.dumps(registry), encoding="utf-8")

            qualification = edge_lab.qualification_report

            def compact_qualification(*args, **kwargs):
                kwargs.update(min_trades=4, min_sessions=4, min_clusters=4)
                return qualification(*args, **kwargs)

            # This test isolates lifecycle boundary behavior. Production's
            # 30-cluster contract is asserted separately; a compact local
            # floor keeps this already-expensive replay fixture focused.
            with mock.patch.object(
                    edge_discovery_core, "MIN_PROMOTION_CLUSTERS", 4), \
                    mock.patch.object(
                        edge_discovery_core, "ACTUAL_CONTROL_MIN_MATCHED", 4), \
                    mock.patch.object(
                        edge_discovery_core, "MIN_NULL_CONTROL_MATCHED", 4), \
                    mock.patch.object(
                        edge_lab, "qualification_report",
                        side_effect=compact_qualification), \
                    mock.patch.multiple(
                        gates,
                        SERIAL_BLOCK_LENGTH=3,
                        ACTUAL_CONTROL_MIN_MATCHED=4,
                        NULL_CONTROL_MIN_MATCHED=4,
                        PROTOCOL_BACKTEST_MIN_TRADES=4,
                        PROTOCOL_BACKTEST_MIN_SESSIONS=4,
                        PROTOCOL_BACKTEST_MIN_CLUSTERS=4,
                        PROTOCOL_SHADOW_MIN_TRADES=4,
                        PROTOCOL_SHADOW_MIN_SESSIONS=4,
                        PROTOCOL_SHADOW_MIN_CLUSTERS=4,
                        PROTOCOL_QUALIFICATION_MIN_TRADES=4,
                        PROTOCOL_QUALIFICATION_MIN_SESSIONS=4,
                        PROTOCOL_QUALIFICATION_MIN_CLUSTERS=4), \
                    mock.patch.object(edge_lab, "PROTOCOL_SHADOW_MIN_TRADES", 4), \
                    mock.patch.object(edge_lab, "PROTOCOL_SHADOW_MIN_SESSIONS", 4), \
                    mock.patch.object(gates, "QUALIFICATION_MIN_TRADES", 4), \
                    mock.patch.object(gates, "QUALIFICATION_MIN_SESSIONS", 4), \
                    mock.patch.object(gates, "QUALIFICATION_MIN_CLUSTERS", 4):
                initial = discover(data_one, db_path=db, variants_path=variants,
                                   config=config, min_trades=5, min_sessions=5,
                                   lane="auto")
                candidate = initial["variants"][0]
                self.assertEqual(candidate["status"], "backtest_passed")
                self.assertIsNone(candidate["shadow_run_id"])

                with self.assertRaises(DiscoveryError):
                    discover(data_one, db_path=db, variants_path=variants,
                             config=config, min_trades=5, min_sessions=5,
                             lane="shadow")

                forward = discover(data_all, db_path=db, variants_path=variants,
                                   config=config, min_trades=5, min_sessions=5,
                                   lane="auto")
            candidate = forward["variants"][0]
            # Offline forward replay remains diagnostic; lifecycle validation
            # is authorized only by the research-side live-shadow ingester.
            self.assertIn(candidate["status"], {"shadow", "validated", "champion"})
            self.assertEqual(candidate["mode"], "shadow")
            self.assertEqual(candidate["unseen_sessions"], 20)
            trades = EdgeLedger(db).trades(candidate["candidate_id"], lane="shadow")
            self.assertTrue(trades)
            self.assertTrue(all(row["session_date"] >= "2024-02-01" for row in trades))


def _sessions_failing_the_sealed_tail(start: datetime, count: int,
                                     symbols=("SPY", "QQQ", "IWM", "DIA")) -> list[dict]:
    """Profitable development sessions, then a losing final fifth.

    ``seal_final_window`` reserves the last 20% of sessions, so this corpus
    earns every development check and then loses on the one window selection
    never saw.  That is the only shape in which the qualification checks can
    be the sole reason a variant fails.
    """
    winner = [(100, 101, 99, 100), (100, 102, 99, 102),
              (102, 103, 101, 102),     # next-bar entry
              # Gap through the 1.5R target at the executable bar boundary;
              # the 2R baseline remains open and uses the final IEX quote.
              (107, 107.5, 101, 107)]
    # Keep the winner's decline above the 3-dollar stop so the 2R baseline
    # reaches its force-flat boundary and can be priced by the final IEX
    # quote.  The target variant has already exited at the opening gap.
    price = 106.0
    for _ in range(20):
        winner.append((price, price + .05, price - .3, price - .25))
        price -= .25
    loser = [(100, 101, 99, 100), (100, 102, 99, 102),
             (102, 103, 101, 102),
             # Gap through the stop so both arms use the opening IEX quote;
             # the candidate still loses, while qualification remains
             # authorizable and therefore explicitly fails on performance.
             (98, 102, 94, 95)]
    sealed_from = count - max(1, int(count * .2))
    rows: list[dict] = []
    for offset in range(count):
        session = start + timedelta(days=offset)
        values = winner if offset < sealed_from else loser
        for index, symbol in enumerate(symbols):
            shift = index * .01
            for minute, (open_, high, low, close) in enumerate(values):
                timestamp = session + timedelta(minutes=minute)
                rows.append({
                    "symbol": symbol,
                    "timestamp": timestamp.isoformat(),
                    "as_of": (timestamp + timedelta(minutes=1)).isoformat(),
                    "observed_at": (timestamp + timedelta(minutes=1)).isoformat(),
                    "open": open_ + shift, "high": high + shift,
                    "low": low + shift, "close": close + shift,
                    "volume": 1, "provider": "alpaca", "feed": "iex",
                    "source_mode": "forward_observed",
                })
                rows.append({
                    "kind": "quote", "symbol": symbol,
                    "timestamp": timestamp.isoformat(),
                    "as_of": timestamp.isoformat(),
                    "observed_at": timestamp.isoformat(),
                    "bid": open_ + shift - .01, "ask": open_ + shift + .01,
                    "provider": "alpaca", "feed": "iex",
                    "source_mode": "forward_observed",
                })
            final_quote = session + timedelta(minutes=len(values))
            rows.append({
                "kind": "quote", "symbol": symbol,
                "timestamp": final_quote.isoformat(),
                "as_of": final_quote.isoformat(),
                "observed_at": final_quote.isoformat(),
                "bid": values[-1][3] + shift - .01,
                "ask": values[-1][3] + shift + .01,
                "provider": "alpaca", "feed": "iex",
                "source_mode": "forward_observed",
            })
    return rows


def _drift_sessions(start: datetime, count: int,
                    symbols=("SPY", "QQQ", "IWM", "DIA")) -> list[dict]:
    """One session-long move and nothing else: pure directional drift.

    The 1.5R variant still beats its 2R baseline here, so every control the
    IBR lane had before this change is satisfied.  But the move is the whole
    session, so an entry chosen at random catches the same move: none of the
    P&L is bought by the timing, which is what the null control exists to say.
    """
    rows: list[dict] = []
    for offset in range(count):
        session = start + timedelta(days=offset)
        values = [
            (100, 101, 99, 100),     # one-minute opening range
            (100, 102, 99, 102),     # confirmed breakout
            (102, 103, 101, 102),    # next-bar entry
            (107, 107.5, 101, 107),  # 1.5R gap, below 2R target
        ]
        # Alternate a short post-gap give-back.  This makes the 1.5R arm
        # beat its 2R force-flat baseline on enough held-out sessions while
        # preserving mixed-sign cluster evidence against chance entries.
        if offset % 2:
            price = 107.0
            for _ in range(20):
                values.append((price, price + .05, price - .4, price - .35))
                price -= .35
        for index, symbol in enumerate(symbols):
            shift = index * .01
            for minute, (open_, high, low, close) in enumerate(values):
                timestamp = session + timedelta(minutes=minute)
                rows.append({
                    "symbol": symbol,
                    "timestamp": timestamp.isoformat(),
                    "as_of": timestamp.isoformat(),
                    "observed_at": timestamp.isoformat(),
                    "open": open_ + shift, "high": high + shift,
                    "low": low + shift, "close": close + shift,
                    "volume": 1, "provider": "alpaca", "feed": "iex",
                    "source_mode": "forward_observed",
                })
                rows.append({
                    "kind": "quote", "symbol": symbol,
                    "timestamp": timestamp.isoformat(),
                    "as_of": timestamp.isoformat(),
                    "observed_at": timestamp.isoformat(),
                    "bid": open_ + shift - .01, "ask": open_ + shift + .01,
                    "provider": "alpaca", "feed": "iex",
                    "source_mode": "forward_observed",
                })
            final_quote = session + timedelta(minutes=len(values))
            rows.append({
                "kind": "quote", "symbol": symbol,
                "timestamp": final_quote.isoformat(),
                "as_of": final_quote.isoformat(),
                "observed_at": final_quote.isoformat(),
                "bid": values[-1][3] + shift - .01,
                "ask": values[-1][3] + shift + .01,
                "provider": "alpaca", "feed": "iex",
                "source_mode": "forward_observed",
            })
    return rows


class IbrLaneEvidenceParityTests(unittest.TestCase):
    """The IBR lane carries the factory lane's null control and sealed window."""

    REGISTRY = {"variants": [
        {"variant_id": "ibr.baseline", "strategy_id": "ibr",
         "base_version": "v1", "overrides": {}, "vehicles": ["equity"],
         "hypothesis": "registered baseline"},
        {"variant_id": "ibr.target.1_5r", "strategy_id": "ibr",
         "base_version": "v1", "overrides": {"strategy.target_r": 1.5},
         "vehicles": ["equity"], "hypothesis": "earlier target"},
    ]}
    CONFIG = {"strategy": {"range_minutes": 1, "range_stop": True, "target_r": 2}}

    def _discover(self, rows, directory, **kwargs):
        root = Path(directory)
        data = root / "corpus.jsonl"
        variants = root / "variants.yaml"
        data.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        variants.write_text(json.dumps(self.REGISTRY), encoding="utf-8")
        options = {"min_trades": 5, "min_sessions": 5, "lane": "auto", **kwargs}
        qualification = edge_lab.qualification_report

        def compact_qualification(*args, **call_kwargs):
            call_kwargs.update(min_trades=4, min_sessions=4, min_clusters=4)
            return qualification(*args, **call_kwargs)

        # These tests isolate IBR/factory evidence parity rather than the
        # production sample-size policy.  A separate regression asserts the
        # shipped 30-cluster floor; keeping this replay fixture compact avoids
        # multiplying every parity test by 7.5x.
        with mock.patch.object(
                edge_discovery_core, "MIN_PROMOTION_CLUSTERS", 4), \
                mock.patch.object(
                    edge_discovery_core, "ACTUAL_CONTROL_MIN_MATCHED", 4), \
                mock.patch.object(
                    edge_discovery_core, "MIN_NULL_CONTROL_MATCHED", 4), \
                mock.patch.object(
                    edge_lab, "qualification_report",
                    side_effect=compact_qualification), \
                mock.patch.multiple(
                    gates,
                    SERIAL_BLOCK_LENGTH=3,
                    ACTUAL_CONTROL_MIN_MATCHED=4,
                    NULL_CONTROL_MIN_MATCHED=4,
                    PROTOCOL_BACKTEST_MIN_TRADES=4,
                    PROTOCOL_BACKTEST_MIN_SESSIONS=4,
                    PROTOCOL_BACKTEST_MIN_CLUSTERS=4,
                    PROTOCOL_SHADOW_MIN_TRADES=4,
                    PROTOCOL_SHADOW_MIN_SESSIONS=4,
                    PROTOCOL_SHADOW_MIN_CLUSTERS=4,
                    PROTOCOL_QUALIFICATION_MIN_TRADES=4,
                    PROTOCOL_QUALIFICATION_MIN_SESSIONS=4,
                    PROTOCOL_QUALIFICATION_MIN_CLUSTERS=4), \
                mock.patch.object(gates, "QUALIFICATION_MIN_TRADES", 4), \
                mock.patch.object(gates, "QUALIFICATION_MIN_SESSIONS", 4), \
                mock.patch.object(gates, "QUALIFICATION_MIN_CLUSTERS", 4):
            return discover(
                data, db_path=root / "edge.sqlite3", variants_path=variants,
                config=self.CONFIG, **options)

    def test_edge_lab_rejects_invalid_statistical_policy_inputs_before_replay(self):
        rows = _sessions(datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc), 2)
        for kwargs in ({"alpha": 0.0}, {"alpha": float("nan")},
                       {"min_trades": 0}, {"min_sessions": 1.5},
                       {"min_trades": True}):
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(DiscoveryError):
                    self._discover(rows, directory, **kwargs)

    def test_the_gate_reports_a_null_control_and_a_sealed_window(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "edge.sqlite3"
            result = self._discover(
                _sessions(datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc), 20),
                directory)
            fdr_state = FactoryLedger(database).fdr_state(
                f"{CONFIRMATORY_SCOPE_VERSION}:equity")
        gate = result["variants"][0]["gate"]
        for name in ("null_control_available", "null_control_delta_positive",
                     "qualification_net_positive", "qualification_delta_positive"):
            self.assertIn(name, gate["checks_without_family"])
            self.assertTrue(gate["checks_without_family"][name], name)
        self.assertEqual(gate["null_control"]["kind"], "randomized_entry_null")
        self.assertGreater(gate["null_control"]["matched"], 0)
        self.assertTrue(gate["qualification"]["available"])
        # The sealed window is the last fifth of the corpus and is scored, not
        # split, so it appears in the persisted decision as its own evidence.
        self.assertEqual(gate["qualification"]["sessions"],
                         ["2024-01-18", "2024-01-19", "2024-01-20", "2024-01-21"])
        envelope = gate["verified_gate"]
        self.assertEqual(envelope["null_control"], gate["null_control"])
        self.assertEqual(envelope["qualification"], gate["qualification"])
        self.assertEqual(envelope["walk_forward"], gate["walk_forward"])
        self.assertFalse(envelope["online_fdr"]["required"])
        self.assertEqual(envelope["online_fdr"]["status"],
                         "deferred_to_live_shadow")
        self.assertEqual(fdr_state["tests"], 0)

    def test_the_sealed_window_never_reaches_selection_or_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._discover(
                _sessions(datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc), 20),
                directory)
            candidate = result["variants"][0]
            ledger = EdgeLedger(Path(directory) / "edge.sqlite3")
            trades = ledger.trades(candidate["candidate_id"])
        sealed = set(candidate["gate"]["qualification"]["sessions"])
        self.assertTrue(trades)
        self.assertFalse(sealed & {str(row["session_date"]) for row in trades})
        counts = candidate["gate"]["verified_gate"]["counts"]
        self.assertEqual(counts["total"]["trades"],
                         counts["fit"]["trades"] + counts["heldout"]["trades"])

    def test_a_drift_edge_beats_its_baseline_and_still_fails_the_null(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._discover(
                _drift_sessions(datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc), 20),
                directory)
        candidate = result["variants"][0]
        checks = candidate["gate"]["checks_without_family"]
        self.assertTrue(checks["heldout_delta_positive"])
        self.assertTrue(checks["heldout_net_pnl_positive"])
        self.assertTrue(checks["null_control_available"])
        # Everything the lane could ask before this change is satisfied; only
        # the chance-entry comparison is not.
        self.assertFalse(checks["null_control_delta_positive"])
        self.assertFalse(candidate["gate"]["passes"])
        # Failing a randomized-entry null means the thesis is inconclusive,
        # not terminally loss-making.  Keep it available for a later, genuinely
        # different sample instead of retiring it on a diagnostic failure.
        self.assertEqual(candidate["status"], "candidate")

    def test_the_requested_alpha_reaches_the_falsification_decision(self):
        # The factory passed alpha through; the IBR lane decided falsification
        # at a hardcoded .05 whatever discover() was asked for.
        rows = _sessions(datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc), 20)
        with tempfile.TemporaryDirectory() as directory:
            lenient = self._discover(rows, directory)
        with tempfile.TemporaryDirectory() as directory:
            strict = self._discover(rows, directory, alpha=.0005)
        lenient_gate = lenient["variants"][0]["gate"]
        strict_gate = strict["variants"][0]["gate"]
        # Identical corpus and identical p-value; only the threshold moved.
        self.assertEqual(strict_gate["falsification"]["p_value"],
                         lenient_gate["falsification"]["p_value"])
        self.assertEqual(
            lenient_gate["falsification"]["p_value_source"],
            "heldout_paired_cluster_sign_flip")
        self.assertTrue(lenient_gate["falsification"]["independent_supplied"])
        self.assertEqual(lenient_gate["falsification"]["independent_method"],
                         "independent_empirical_null_tail")
        self.assertTrue(lenient_gate["checks_without_family"]["falsification"])
        self.assertFalse(strict_gate["checks_without_family"]["falsification"])

    def test_a_losing_sealed_window_fails_a_variant_that_passed_development(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._discover(
                _sessions_failing_the_sealed_tail(
                    datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc), 20),
                directory)
        candidate = result["variants"][0]
        checks = candidate["gate"]["checks_without_family"]
        # Development evidence is intact: the window is the only thing wrong.
        self.assertTrue(checks["heldout_delta_positive"])
        self.assertTrue(checks["heldout_net_pnl_positive"])
        self.assertTrue(candidate["gate"]["qualification"]["available"])
        self.assertFalse(checks["qualification_net_positive"])
        self.assertFalse(candidate["gate"]["passes"])

    def test_a_corpus_too_thin_to_seal_a_window_is_underpowered_not_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._discover(
                _sessions(datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc), 1),
                directory, min_trades=4, min_sessions=4)
            candidate = result["variants"][0]
            events = EdgeLedger(Path(directory) / "edge.sqlite3").history(
                candidate["candidate_id"])
        self.assertFalse(candidate["gate"]["qualification"]["available"])
        # Structural inadequacy, never a failure: a thin corpus must not retire
        # a hypothesis or otherwise churn the lifecycle.
        self.assertEqual(candidate["status"], "candidate")
        self.assertIn("insufficient_data", {event["event_type"] for event in events})


class NullAdmissibilityTests(unittest.TestCase):
    def test_opening_range_null_waits_for_complete_anchor_and_signal_bar(self):
        from agent.contracts.rule import validate_rule_spec

        spec = validate_rule_spec({
            **ROOT_SPEC, "family": "opening_range_breakout",
            "range_minutes": 15, "lookback": 3, "atr_period": 3,
            "slow_lookback": 5, "confirmation": "none",
            "confirmations": [], "entry_after_minutes": 0,
            "entry_before_minutes": 390,
        })
        opening = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
        bars = [SimpleNamespace(
            symbol="SPY", session_date=date(2026, 1, 5),
            timestamp=opening + timedelta(minutes=index),
            end=opening + timedelta(minutes=index + 1),
            open=100.0, close=100.0,
        ) for index in range(30)]
        policy = edge_discovery_core.ReplayPolicy(strict_market_data=False)
        dependencies = {
            "feature_window_bars": lambda _spec: None,
            "_contiguous": lambda _rows, _start, _stop: True,
            "_available": lambda bar, _policy: bar.end,
        }
        with mock.patch.object(
                edge_discovery_core, "_simulation_dependency",
                side_effect=lambda name: dependencies[name]), \
                mock.patch.object(edge_discovery_core, "quote_fill_record",
                                  return_value=None), \
                mock.patch.object(edge_discovery_core,
                                  "replay_open_is_available", return_value=True), \
                mock.patch.object(edge_discovery_core,
                                  "replay_available_at",
                                  side_effect=lambda bar, **_kwargs: bar.end):
            entries = edge_discovery_core._null_admissible_entry_indices(
                bars, spec, direction="long", policy=policy,
                vehicle="equity", snapshots=(), quote_index=None)

        self.assertTrue(entries)
        # Fifteen anchor bars plus the signal bar are complete before entry.
        self.assertGreaterEqual(min(index for index, _at in entries), 16)

    def test_null_clock_does_not_require_the_candidate_predicate(self):
        """Admissible controls stay available even when no signal fires."""
        from agent.contracts.rule import validate_rule_spec

        spec = validate_rule_spec({**ROOT_SPEC,
                                   "family": "momentum_continuation",
                                   "confirmation": "none",
                                   "confirmations": [],
                                   "lookback": 5,
                                   "slow_lookback": 40,
                                   "atr_period": 14,
                                   "entry_after_minutes": 0,
                                   "entry_before_minutes": 390})
        opening = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
        bars = []
        for index in range(30):
            stamp = opening + timedelta(minutes=index)
            bars.append(SimpleNamespace(
                symbol="SPY", session_date=date(2026, 1, 5),
                timestamp=stamp, end=stamp + timedelta(minutes=1),
                open=100.0, close=100.0,
            ))
        policy = edge_discovery_core.ReplayPolicy(strict_market_data=False)

        dependencies = {
            "feature_window_bars": lambda _spec: 7,
            "_contiguous": lambda _rows, _start, _stop: True,
            "_available": lambda bar, _policy: bar.end,
        }

        def dependency(name):
            if name == "evaluate_rule_signal":
                raise AssertionError("null admissibility must not evaluate predicate")
            return dependencies[name]

        with mock.patch.object(edge_discovery_core,
                               "_simulation_dependency",
                               side_effect=dependency) as resolve, \
             mock.patch.object(edge_discovery_core, "quote_fill_record",
                               return_value=None), \
             mock.patch.object(edge_discovery_core,
                               "replay_open_is_available", return_value=True), \
             mock.patch.object(edge_discovery_core, "replay_available_at",
                               side_effect=lambda bar, **_kwargs: bar.end):
            entries = edge_discovery_core._null_admissible_entry_indices(
                bars, spec, direction="long", policy=policy, vehicle="equity",
                snapshots=(), quote_index=None)

        self.assertTrue(entries)
        self.assertNotIn(mock.call("evaluate_rule_signal"), resolve.call_args_list)


if __name__ == "__main__":
    unittest.main()
