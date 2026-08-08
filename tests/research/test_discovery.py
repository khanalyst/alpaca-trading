import json
import tempfile
import unittest
from pathlib import Path

from research.discovery import (
    CausalClaim,
    CounterfactualSpec,
    DiscoveryProgram,
    EntryRule,
    ExitProfile,
    ObservableSpec,
    choose_program,
    content_hash,
    load_discovery_rows,
    load_discovery_episodes,
    load_artifact,
    prior_program_hashes,
    program_identity_hash,
    resolve_episode,
    run_discovery,
    seed_forced_flow_pressure,
    validate_generated_source,
    verify_artifact,
    write_artifact,
)
from research.discovery_counterfactual import run_counterfactual
from research.discovery_world_model import fit_world_model


class DiscoveryTests(unittest.TestCase):
    CONTRACT_IDENTITY = {"strategy_id": "test", "strategy_version": "v1",
                         "variant_id": "test.baseline", "semantic_hash": "a" * 64}
    def program(self, expression=None):
        return DiscoveryProgram(
            "test.v1",
            ObservableSpec("test.v1", expression or {
                "op": "ratio",
                "args": [{"op": "source", "field": "price"},
                         {"op": "source", "field": "relative_volume_1h"}],
            }),
            CausalClaim("payer", "mechanism", "prediction", "falsifier",
                        ("limitation",)),
            EntryRule(), ExitProfile(), CounterfactualSpec())

    def test_frozen_canonical_identity_and_bounds(self):
        program = seed_forced_flow_pressure()
        self.assertEqual(program.content_hash,
                         DiscoveryProgram.from_dict(program.as_dict()).content_hash)
        with self.assertRaises((TypeError, AttributeError)):
            program.observable.expression["op"] = "source"
        with self.assertRaises(ValueError):
            ObservableSpec("bad", {"op": "source", "field": "not_saved"})
        with self.assertRaises(ValueError):
            ObservableSpec("bad", {"op": "rolling_mean", "window": 257,
                                    "arg": {"op": "source", "field": "price"}})

    def test_candidate_neighborhood_exhausts_without_abs_negate_equivalent(self):
        candidates = []
        seen = []
        for _ in range(4):
            candidate = choose_program(seen)
            candidates.append(candidate)
            if candidate is not None:
                seen.append(candidate.content_hash)
        self.assertEqual([candidate.program_id if candidate else None
                          for candidate in candidates], [
                              "forced_flow_pressure.v1",
                              "forced_flow_pressure.oi_volume.v1",
                              "forced_flow_pressure.volume_depth.v1",
                              None])

    def test_seed_is_observational_and_unavailable_is_none(self):
        observable = seed_forced_flow_pressure().observable
        self.assertAlmostEqual(observable.evaluate({
            "oi_change_4h_pct": -2, "relative_volume_1h": 2,
            "book_bid_depth_usd": 120, "book_ask_depth_usd": 80}), 0.8)
        self.assertIsNone(observable.evaluate({
            "oi_change_4h_pct": -2, "relative_volume_1h": 2,
            "book_bid_depth_usd": None, "book_ask_depth_usd": 80}))

    def test_event_plane_composite_uses_actual_recorder_series(self):
        from research.recorded_data import EventPlane
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "recorded"
            for series in ("order_book", "open_interest", "taker_volume"):
                (root / series).mkdir(parents=True)
            (root / "order_book" / "x.csv").write_text(
                "ts,inst_id,imbalance_5bps,observed_at,available_at\n"
                "18000000,BTC-USDT-SWAP,0.2,18000000,18000000\n")
            (root / "open_interest" / "x.csv").write_text(
                "ts,inst_id,oi_usd,observed_at,available_at\n"
                "3600000,BTC-USDT-SWAP,100,3600000,3600000\n"
                "18000000,BTC-USDT-SWAP,120,18000000,18000000\n")
            (root / "taker_volume" / "x.csv").write_text(
                "ts,ccy,sell_vol,buy_vol,observed_at,available_at\n"
                "3600000,BTC,10,10,3600000,3600000\n"
                "7200000,BTC,10,10,7200000,7200000\n"
                "10800000,BTC,10,10,10800000,10800000\n"
                "14400000,BTC,10,10,14400000,14400000\n"
                "18000000,BTC,20,20,18000000,18000000\n")
            plane = EventPlane(Path(directory) / "market_events.db")
            plane.ingest(root, archive_root=Path(directory) / "archive")
            rows = load_discovery_rows(
                plane, decision_time_ms=20_000_000,
                instrument="BTC-USDT-SWAP")
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(seed_forced_flow_pressure().observable.evaluate(rows[0]), 8.0)
            self.assertTrue(rows[0]["event_join_digest"])
            self.assertEqual(len(rows[0]["source_event_ids"]), 8)
            self.assertTrue(rows[0]["source_snapshot_ids"])
            self.assertEqual(rows[0]["source_feeds"], ["legacy"])
            self.assertEqual(rows[0]["source_schemas"], ["legacy_unknown"])

            contract_identity = self.CONTRACT_IDENTITY
            first = run_discovery(
                rows, result_root=Path(directory) / "artifacts",
                analysis_root=Path(directory) / "results",
                contract_identity=contract_identity)
            (root / "order_book" / "x.csv").write_text(
                "ts,inst_id,imbalance_5bps,observed_at,available_at\n"
                "18000000,BTC-USDT-SWAP,0.3,18000000,18000000\n")
            plane.ingest(root, archive_root=Path(directory) / "archive")
            revised_rows = load_discovery_rows(
                plane, decision_time_ms=20_000_000,
                instrument="BTC-USDT-SWAP")
            revised = run_discovery(
                revised_rows, result_root=Path(directory) / "artifacts",
                analysis_root=Path(directory) / "results",
                contract_identity=contract_identity)
            self.assertNotEqual(first["event_join_digest"],
                                revised["event_join_digest"])
            self.assertNotEqual(first["artifact_hash"], revised["artifact_hash"])
            self.assertNotEqual(first["content_hash"], revised["content_hash"])
            with self.assertRaises(ValueError):
                verify_artifact(
                    Path(directory) / "artifacts" / revised["artifact_hash"],
                    contract_identity,
                    first["event_evidence"])

    def test_event_plane_episode_assembly_reaches_complete_only_with_bars(self):
        """Recorder evidence joins into contiguous episodes and terminal state."""
        from research.recorded_data import EventPlane
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "recorded"
            for series in ("order_book", "open_interest", "taker_volume",
                           "execution_bar_1m", "funding"):
                (root / series).mkdir(parents=True)
            base = 24 * 3_600_000
            order = ["ts,inst_id,mid,imbalance_5bps,observed_at,available_at"]
            oi = ["ts,inst_id,oi_usd,observed_at,available_at"]
            taker = ["ts,ccy,sell_vol,buy_vol,observed_at,available_at"]
            bars = ["ts,inst_id,open,high,low,close,closed,observed_at,available_at"]
            # Six prior hourly volumes make the relative-volume baseline
            # deterministic for every signal.  OI baselines are exactly four
            # hours behind each signal, as required by load_discovery_rows.
            for hour in range(0, 40):
                ts = hour * 3_600_000
                sell, buy = ((20, 10) if hour >= 24 and hour % 2 == 0
                             else (10, 20) if hour >= 24 else (10, 10))
                taker.append(f"{ts},BTC,{sell},{buy},{ts},{ts}")
            for index in range(10):
                ts = base + index * 3_600_000
                order.append(f"{ts},BTC-USDT-SWAP,100,0.2,{ts},{ts}")
                oi.append(f"{ts - 4 * 3_600_000},BTC-USDT-SWAP,100,{ts - 4 * 3_600_000},{ts - 4 * 3_600_000}")
                oi.append(f"{ts},BTC-USDT-SWAP,120,{ts},{ts}")
                bar_ts = ts + 60_000
                bars.append(f"{bar_ts},BTC-USDT-SWAP,100,100.5,99.5,100,true,{bar_ts},{bar_ts}")
            (root / "order_book" / "signals.csv").write_text("\n".join(order) + "\n")
            (root / "open_interest" / "oi.csv").write_text("\n".join(oi) + "\n")
            (root / "taker_volume" / "taker.csv").write_text("\n".join(taker) + "\n")
            (root / "execution_bar_1m" / "bars.csv").write_text("\n".join(bars) + "\n")
            (root / "funding" / "funding.csv").write_text(
                "ts,inst_id,funding_rate,realized_rate,settlement_time,status,observed_at,available_at\n")
            plane = EventPlane(Path(directory) / "market_events.db")
            plane.ingest(root, archive_root=Path(directory) / "archive")
            program = seed_forced_flow_pressure()
            rows = load_discovery_episodes(
                plane, instrument="BTC-USDT-SWAP",
                decision_time_ms=100 * 3_600_000,
                cutoff_event_ts_ms=base + 9 * 3_600_000,
                episode_decision_time_ms=100 * 3_600_000,
                episode_cutoff_event_ts_ms=base + 9 * 3_600_000 + 60_000,
                max_horizon_bars=1, program=program)
            self.assertEqual(len(rows), 10)
            self.assertTrue(all(row["funding_complete"] for row in rows))
            self.assertEqual({row["state"] for row in rows},
                             {"normalized", "timeout"})
            self.assertEqual({row["direction"] for row in rows}, {"long", "short"})
            self.assertTrue(all(len(row["bars"]) == 1 for row in rows))
            self.assertEqual(len({row["event_join_digest"] for row in rows}), 10)
            normalized = next(row for row in rows if row["state"] == "normalized")
            self.assertEqual(
                resolve_episode(program, normalized,
                                normalized["observable_values"])["status"],
                "normalized")
            result = run_discovery(
                rows, program=program,
                result_root=Path(directory) / "artifacts",
                analysis_root=Path(directory) / "results",
                contract_identity=self.CONTRACT_IDENTITY)
            self.assertEqual(result["status"], "COMPLETE")
            self.assertEqual(result["counterfactual"]["status"], "COMPLETE")
            self.assertEqual(
                result["counterfactual"]["exit_semantics_hash"],
                content_hash(program.exit_profile.as_dict()))

    def test_exit_stop_first_normalization_then_timeout(self):
        profile = ExitProfile(normalization_threshold=0.1, max_horizon_bars=3)
        self.assertEqual(profile.resolve([1, 0.05]), ("normalized", 1))
        self.assertEqual(profile.resolve([0.01], stop_index=0), ("stop", 0))
        self.assertEqual(profile.resolve([1, 1]), ("timeout", 1))
        self.assertEqual(profile.resolve([]), ("no_data", None))

    def test_episode_resolver_matches_production_close_and_bars_held(self):
        from research.outcomes import SetupPlan, resolve
        from research.prices import Bar

        program = DiscoveryProgram(
            "parity.v1",
            ObservableSpec("parity.v1", {"op": "source", "field": "oi_change_4h_pct"}),
            CausalClaim("payer", "mechanism", "prediction", "falsifier"),
            EntryRule(), ExitProfile(normalization_threshold=0.0,
                                     max_horizon_bars=3,
                                     taker_fee_pct_per_side=.1,
                                     funding_interval_hours=.5,
                                     funding_rate_pct=.2), CounterfactualSpec())
        base = {
            "direction": "long", "entry_price": 100, "stop_pct": 2,
            "signal_ts": 0, "entry_ts": 60_000,
            "signal_timeframe": "1m", "funding_complete": True,
            "funding_provenance": {"complete": True},
        }

        def production(bars, *, take_pct=4, max_hold_hours=0.05):
            return resolve(SetupPlan(
                symbol="parity", direction="long", entry_price=100,
                stop_pct=2, take_pct=take_pct, signal_ts=0,
                entry_ts=60_000, signal_timeframe="1m",
                funding_rate_pct=.2, funding_interval_hours=.5,
                taker_fee_pct_per_side=.1), bars,
                max_hold_hours=max_hold_hours)

        cases = {
            "stop": [(101, 97, 98, .5)],
            # The observable-normalized arm exits at this bar's close, which
            # is also the production target fill price for parity purposes.
            "target": [(104, 99, 104, 0.0)],
            "timeout": [(101, 99, 100, .5)] * 3,
        }
        for expected, values in cases.items():
            rows = [{**base, "bars": [{
                "ts": 60_000 * (index + 1), "high": high,
                "low": low, "close": close,
                "oi_change_4h_pct": observable,
            } for index, (high, low, close, observable) in enumerate(values)]}]
            bars = [Bar(item["ts"], 100, item["high"], item["low"],
                        item["close"], 1) for item in rows[0]["bars"]]
            production_outcome = production(
                bars, max_hold_hours=(0.05 if expected == "timeout" else 0.05))
            discovery_outcome = resolve_episode(
                program, rows[0], [item["oi_change_4h_pct"]
                                  for item in rows[0]["bars"]])
            self.assertEqual(
                discovery_outcome["status"],
                "normalized" if expected == "target" else expected)
            self.assertEqual(discovery_outcome["exit_ts"],
                             production_outcome.exit_ts)
            self.assertEqual(discovery_outcome["bars_held"],
                             production_outcome.bars_held)
            self.assertAlmostEqual(discovery_outcome["net_pct"],
                                   production_outcome.net_pct, places=6)
            self.assertEqual(discovery_outcome["costs"]["funding_intervals"],
                             production_outcome.costs["funding_intervals"])

    def test_episode_resolver_rejects_pre_entry_bars(self):
        program = seed_forced_flow_pressure()
        row = {"direction": "long", "entry_price": 100, "stop_pct": 2,
               "signal_ts": 0, "entry_ts": 60_000,
               "signal_timeframe": "1m", "funding_complete": True,
               "funding_provenance": {"complete": True},
               "bars": [{"ts": 0, "high": 101, "low": 99, "close": 100,
                         "oi_change_4h_pct": 1}]}
        result = resolve_episode(program, row, [1])
        self.assertEqual(result["status"], "invalid")
        self.assertIn("first visible", result["reason"])

    def test_nested_history_transforms_are_rejected(self):
        with self.assertRaises(ValueError):
            ObservableSpec("nested", {
                "op": "rolling_mean", "window": 2,
                "arg": {"op": "lag", "window": 1,
                        "arg": {"op": "source", "field": "price"}},
            })

    def test_generated_artifact_tamper_and_ast(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = write_artifact(self.program(), directory,
                                      [{"price": 2, "relative_volume_1h": 1}],
                                      contract_identity=self.CONTRACT_IDENTITY)
            self.assertEqual(load_artifact(artifact, self.CONTRACT_IDENTITY)({"price": 2,
                                                      "relative_volume_1h": 1}), 2)
            verify_artifact(artifact, self.CONTRACT_IDENTITY)
            Path(artifact.path, "evaluate.py").write_text(
                Path(artifact.path, "evaluate.py").read_text() + "\n# tamper\n")
            with self.assertRaises(ValueError):
                verify_artifact(artifact, self.CONTRACT_IDENTITY)
        with self.assertRaises(ValueError):
            validate_generated_source("import os\ndef evaluate(row): return open('x')")

    def test_forged_self_consistent_safe_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = write_artifact(self.program(), directory, [],
                                      contract_identity=self.CONTRACT_IDENTITY)
            manifest_path = Path(artifact.path, "manifest.json")
            manifest = json.loads(manifest_path.read_text())
            source_path = Path(artifact.path, "evaluate.py")
            source_path.write_text(source_path.read_text().replace(
                "return _finite(_eval(_EXPRESSION, row, row.get(\"_history\")))",
                "return 0.0"))
            manifest["source_hash"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest, sort_keys=True))
            with self.assertRaises(ValueError):
                verify_artifact(artifact, self.CONTRACT_IDENTITY)

    def test_counterfactual_pairing_and_permutation_are_deterministic(self):
        program = seed_forced_flow_pressure()
        rows = [{"oi_change_4h_pct": i, "relative_volume_1h": 1,
                 "book_imbalance": .2, "event_ts_ms": i * 3600000}
                for i in range(20)]
        rows = [{**row, "direction": "long", "entry_price": 100,
                 "stop_pct": 1, "funding_complete": True,
                 "funding_provenance": {"complete": True},
                 "bars": [{"ts": row["event_ts_ms"] + 60000,
                           "high": 100.5, "low": 99.5, "close": 100,
                           "oi_change_4h_pct": row["oi_change_4h_pct"],
                           "relative_volume_1h": 1, "book_imbalance": .2}]}
                for row in rows]
        observed = [None for _ in rows]
        one = run_counterfactual(program, rows, observed)
        two = run_counterfactual(program, rows, observed)
        self.assertEqual(one.digest, two.digest)
        self.assertEqual(one.paired_count, 6)
        self.assertEqual(one.exit_semantics_hash,
                         two.exit_semantics_hash)

    def test_counterfactual_intervention_keeps_missing_middle_index(self):
        from research.discovery_counterfactual import intervene_rows
        program = seed_forced_flow_pressure()
        rows = [{"event_ts_ms": i * 3600000} for i in range(5)]
        rows[2]["outcome"] = None
        result = intervene_rows(rows, [1, 2, 3, 4, 5],
                                program.counterfactual)
        self.assertEqual([item["__discovery_index__"] for item in result], [3, 4])

    def test_episode_resolver_applies_stop_normalization_and_costs(self):
        program = DiscoveryProgram(
            "resolver.v1",
            ObservableSpec("resolver.v1", {"op": "source", "field": "oi_change_4h_pct"}),
            CausalClaim("payer", "mechanism", "prediction", "falsifier"),
            EntryRule(), ExitProfile(normalization_threshold=.1,
                                     taker_fee_pct_per_side=.1,
                                     funding_rate_pct=.2), CounterfactualSpec())
        row = {"direction": "long", "entry_price": 100, "stop_pct": 2,
               "funding_complete": True,
               "funding_provenance": {"complete": True},
               "bars": [{"ts": 1000, "high": 101, "low": 99, "close": 100,
                           "oi_change_4h_pct": .05}]}
        result = resolve_episode(program, row, [.05])
        self.assertEqual(result["status"], "normalized")
        self.assertGreater(result["costs"]["fees_pct"], 0)

    def test_episode_resolver_fails_closed_on_forecast_funding(self):
        program = seed_forced_flow_pressure()
        row = {
            "direction": "long", "entry_price": 100, "stop_pct": 1,
            "funding_complete": True,
            "funding_provenance": {"complete": True, "status": "forecast"},
            "bars": [{"ts": 60_000, "high": 100.1, "low": 100,
                      "close": 100, "oi_change_4h_pct": 1,
                      "relative_volume_1h": 1, "book_imbalance": .1}],
        }
        result = resolve_episode(program, row, [1])
        self.assertEqual(result["status"], "no_data")
        self.assertIn("funding", result["reason"])

    def test_world_model_is_fit_only_and_has_diagnostics(self):
        rows = [{"regime": "r", "liquidity": 1, "observable": i,
                 "direction": "long", "state": "target" if i % 2 else "stop",
                 "cluster": i} for i in range(20)]
        model, diagnostics = fit_world_model(rows)
        self.assertEqual(model.fit_count, 14)
        self.assertEqual(diagnostics["sample"], 6)
        self.assertAlmostEqual(sum(model.predict(rows[0]).values()), 1.0)
        self.assertIsNotNone(diagnostics["brier"])

    def test_lifecycle_no_data_and_idempotence_are_non_authorizing(self):
        from agent.registry import contract_for
        contract_identity = contract_for("flush-fade").as_dict()
        with tempfile.TemporaryDirectory() as directory:
            empty = run_discovery([], result_root=Path(directory) / "a",
                                  analysis_root=Path(directory) / "r",
                                  contract_identity=contract_identity)
            self.assertEqual(empty["verdict"], "INCONCLUSIVE")
            self.assertFalse(empty["authorizes"])
            rows = [{"oi_change_4h_pct": 1, "relative_volume_1h": 2,
                     "book_bid_depth_usd": 120, "book_ask_depth_usd": 80,
                     "event_ts_ms": i * 3600000, "state": "normalized",
                     "outcome_value": 1} for i in range(10)]
            one = run_discovery(rows, result_root=Path(directory) / "a",
                                analysis_root=Path(directory) / "r",
                                contract_identity=contract_identity)
            two = run_discovery(rows, result_root=Path(directory) / "a",
                                analysis_root=Path(directory) / "r",
                                contract_identity=contract_identity)
            self.assertEqual(one["content_hash"], two["content_hash"])
            self.assertFalse(one["authorizes"])

    def test_lifecycle_requires_world_model_state_data(self):
        from agent.registry import contract_for
        contract_identity = contract_for("flush-fade").as_dict()
        program = seed_forced_flow_pressure()
        exit_hash = content_hash(program.exit_profile.as_dict())
        rows = [{
            "oi_change_4h_pct": 1,
            "relative_volume_1h": 2,
            "book_bid_depth_usd": 120,
            "book_ask_depth_usd": 80,
            "event_ts_ms": i * 3600000,
            "outcome_value": 1,
            "exit_semantics_hash": exit_hash,
            "funding_complete": True,
            "funding_provenance": {"complete": True},
        } for i in range(10)]
        with tempfile.TemporaryDirectory() as directory:
            result = run_discovery(
                rows, result_root=Path(directory) / "a",
                analysis_root=Path(directory) / "r",
                contract_identity=contract_identity)
            self.assertEqual(result["status"], "NO_STATE_DATA")
            self.assertEqual(result["verdict"], "INCONCLUSIVE")
            self.assertFalse(result["authorizes"])
            self.assertTrue(Path(result["result_path"]).joinpath("result.json").exists())

    def test_scalar_results_fail_closed_and_do_not_exhaust_candidate_neighborhood(self):
        from agent.registry import contract_for
        contract_identity = contract_for("flush-fade").as_dict()
        program = seed_forced_flow_pressure()
        exit_hash = content_hash(program.exit_profile.as_dict())
        rows = [{
            "oi_change_4h_pct": 1,
            "relative_volume_1h": 2,
            "book_bid_depth_usd": 120,
            "book_ask_depth_usd": 80,
            "event_ts_ms": i * 3600000,
            "state": "normalized",
            "outcome_value": 1,
            "exit_semantics_hash": exit_hash,
            "funding_complete": True,
            "funding_provenance": {"complete": True},
        } for i in range(10)]
        with tempfile.TemporaryDirectory() as directory:
            results_root = Path(directory) / "r"
            run_discovery([], result_root=Path(directory) / "a",
                          analysis_root=results_root, program=program,
                          contract_identity=contract_identity)
            prior = prior_program_hashes(analysis_root=results_root)
            self.assertEqual(prior, ())
            self.assertEqual(
                choose_program(prior, contract_identity).program_id,
                program.program_id)

            completed = run_discovery(
                rows, result_root=Path(directory) / "a",
                analysis_root=results_root, program=program,
                contract_identity=contract_identity)
            prior = prior_program_hashes(analysis_root=results_root)
            # Legacy scalar outcome rows remain auditable but cannot consume
            # a candidate or manufacture a COMPLETE counterfactual.
            self.assertNotIn(completed["program_identity_hash"], prior)
            self.assertIn(completed["status"], {"NO_EPISODE_DATA", "NO_STATE_DATA"})
            self.assertIsNone(completed["counterfactual"])
            self.assertEqual(
                choose_program(prior, contract_identity).program_id,
                program.program_id)

    def test_idle_and_disabled_persist_contract_and_event_context(self):
        contract_identity = self.CONTRACT_IDENTITY
        rows = [{
            "event_join_digest": "evidence-row",
            "source_snapshot_ids": ["snapshot-1"],
            "source_feeds": ["book"],
            "source_schemas": ["v1"],
        }]
        as_of = {"decision_time_ms": 200, "cutoff_event_ts_ms": 100}
        seen = []
        for _ in range(3):
            candidate = choose_program(seen, contract_identity)
            self.assertIsNotNone(candidate)
            seen.append(program_identity_hash(candidate, contract_identity))
        with tempfile.TemporaryDirectory() as directory:
            idle = run_discovery(
                rows, analysis_root=Path(directory) / "idle",
                previous_hashes=seen, as_of=as_of,
                contract_identity=contract_identity)
            self.assertEqual(idle["status"], "IDLE")
            self.assertEqual(idle["contract_identity"], contract_identity)
            self.assertEqual(idle["as_of"], as_of)
            self.assertTrue(idle["event_join_digest"])
            self.assertEqual(idle["source_snapshot_ids"], ["snapshot-1"])

            disabled = run_discovery(
                rows, analysis_root=Path(directory) / "disabled",
                enabled=False, as_of=as_of,
                contract_identity=contract_identity)
            self.assertEqual(disabled["status"], "DISABLED")
            self.assertEqual(disabled["contract_identity"], contract_identity)
            self.assertEqual(disabled["as_of"], as_of)
            self.assertEqual(disabled["event_join_digest"],
                             idle["event_join_digest"])

    def test_nightly_discovery_is_after_fidelity_gate(self):
        script = Path(__file__).parents[2] / "research" / "nightly.sh"
        text = script.read_text(encoding="utf-8")
        self.assertLess(text.index("research.py ingest-recorded"),
                        text.index("research.py discover"))
        self.assertIn("market_events.db", text)
        self.assertLess(text.index("proposal_fidelity_ready"),
                        text.index("research.py discover"))

    def test_ingest_missing_recorder_is_no_data_and_quarantine_fails(self):
        import importlib.util
        import argparse
        cli_path = Path(__file__).parents[2] / "research.py"
        spec = importlib.util.spec_from_file_location("research_cli_ingest", cli_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            missing = module.cmd_ingest_recorded(argparse.Namespace(
                recorded=Path(directory) / "missing", db=Path(directory) / "db",
                archive=Path(directory) / "archive"))
            self.assertEqual(missing, 0)
            root = Path(directory) / "bad"
            (root / "order_book").mkdir(parents=True)
            (root / "order_book" / "bad.csv").write_text("bad\n1\n")
            failed = module.cmd_ingest_recorded(argparse.Namespace(
                recorded=root, db=Path(directory) / "db2",
                archive=Path(directory) / "archive2"))
            self.assertEqual(failed, 2)

    def test_ingest_uses_configured_collector_output_when_not_overridden(self):
        import importlib.util
        import argparse
        from research.recorded_data import EventPlane
        cli_path = Path(__file__).parents[2] / "research.py"
        spec = importlib.util.spec_from_file_location("research_cli_configured_ingest", cli_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            recorded = Path(directory) / "collector.out"
            (recorded / "order_book").mkdir(parents=True)
            (recorded / "order_book" / "book.csv").write_text(
                "ts,inst_id,mid,observed_at,available_at\n"
                "100,BTC,100,100,100\n", encoding="utf-8")
            original_loader = module._load_config
            module._load_config = lambda: {"research": {
                "collector": {"out": str(recorded)}}}
            try:
                result = module.cmd_ingest_recorded(argparse.Namespace(
                    recorded=None, db=Path(directory) / "db",
                    archive=Path(directory) / "archive"))
            finally:
                module._load_config = original_loader
            self.assertEqual(result, 0)
            self.assertEqual(len(EventPlane(Path(directory) / "db").query(
                series="order_book", instrument="BTC",
                decision_time_ms=200)), 1)

    def test_cli_wiring_exposes_discover_without_authority_flags(self):
        import importlib.util
        cli_path = Path(__file__).parents[2] / "research.py"
        spec = importlib.util.spec_from_file_location("research_cli_discovery", cli_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        parser = module.build_parser()
        help_text = parser.format_help()
        self.assertIn("ingest recorder CSVs", help_text)
        self.assertIn("research-only observable discovery", help_text)
        args = parser.parse_args(["discover", "--rows", "rows.json"])
        self.assertEqual(args.group, "discover")
        self.assertFalse(hasattr(args, "promote"))


if __name__ == "__main__":
    unittest.main()
