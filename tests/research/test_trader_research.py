"""Independent verification of the frozen trader research runner."""

from __future__ import annotations

from contextlib import ExitStack
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.config import load_config
from deploy.trader_projection import ProjectionLimits, project_corpus
from research import trader_research as research


_PROJECTION_FIELDS = [
    "event_key", "event_type", "provider", "feed", "symbol", "timestamp",
    "observed_at", "as_of", "session_open", "session_close", "open", "high",
    "low", "close", "volume", "bid", "ask", "bid_size", "ask_size",
]


def _write_bar_partition(path: Path, day: str, event_key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    opening = f"{day}T14:30:00+00:00"
    closing = f"{day}T21:00:00+00:00"
    row = {
        "event_key": event_key,
        "event_type": "bar_1m",
        "provider": "alpaca",
        "feed": "iex",
        "symbol": "SPY",
        "timestamp": f"{day}T14:31:00+00:00",
        "observed_at": f"{day}T14:31:05+00:00",
        "as_of": f"{day}T14:31:00+00:00",
        "session_open": opening,
        "session_close": closing,
        "open": "100",
        "high": "101",
        "low": "99",
        "close": "100",
        "volume": "10",
    }
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_PROJECTION_FIELDS)
        writer.writeheader()
        writer.writerow(row)


def _small_projection(root: Path) -> tuple[dict, Path, dict]:
    corpus = root / "corpus"
    _write_bar_partition(corpus / "sessions/market-2026-01-05.csv",
                         "2026-01-05", "bar-a")
    _write_bar_partition(corpus / "sessions/market-2026-01-06.csv",
                         "2026-01-06", "bar-b")
    config = load_config(Path("config.yaml"))
    output = root / "projection"
    manifest = project_corpus(
        corpus, config, output,
        limits=ProjectionLimits(disk_floor_bytes=1, max_retained_rows=1000),
    )
    return config, output, manifest


def _inventory() -> dict:
    arms = []
    for index in range(43):
        arms.append({
            "variant_id": f"control-{index}",
            "strategy_id": "rule",
            "family": "control",
            "role": "control",
            "cohort": "controls",
            "rule_spec": {},
        })
    for index in range(12):
        arms.append({
            "variant_id": f"mechanism-{index}",
            "strategy_id": "rule",
            "family": "mechanism",
            "role": "new_mechanism",
            "cohort": "mechanisms",
            "rule_spec": {},
        })
    return {
        "arms": arms,
        "controls": {"arms": arms[:43], "registered_arms": 43},
        "mechanisms": {"families": [{"arms": [arm]}
                                      for arm in arms[43:]],
                        "registered_arms": 12},
        "inventory_hash": "inventory-hash",
        "registered_arms": 55,
        "diagnostic_only": True,
        "authorizing": False,
        "selection": "none",
    }


class TraderResearchRunnerTests(unittest.TestCase):
    def _common_patches(self, stack: ExitStack, inventory: dict) -> None:
        stack.enter_context(patch.object(research, "_policy_config",
                                          return_value={}))
        stack.enter_context(patch.object(
            research, "validate_source",
            return_value={"content_hash": "source-hash", "rows": 1}))
        stack.enter_context(patch.object(research, "_code_bundle_files",
                                          return_value=("research/trader_research.py",)))
        stack.enter_context(patch.object(research, "_code_bundle_hash",
                                          return_value="a" * 64))
        stack.enter_context(patch.object(research, "frozen_inventory",
                                          return_value=inventory))
        stack.enter_context(patch.object(
            research, "run_cost_calibration",
            return_value={"schema": "calibration.v1", "diagnostic_only": True,
                          "authorizing": False}))
        stack.enter_context(patch.object(research, "_initialize_worker"))
        stack.enter_context(patch.object(research, "_close_worker"))
        stack.enter_context(patch.object(research, "build_diagnostic_cohort",
                                          return_value={"arms": []}))
        stack.enter_context(patch.object(
            research, "run_offline_forward_ibr",
            return_value={"status": "unavailable"}))
        stack.enter_context(patch.object(research, "_attach_ibr_runtime"))
        stack.enter_context(patch.object(
            research, "build_trader_diagnostics",
            return_value={"schema": "trader-diagnostics.v1",
                          "diagnostic_only": True,
                          "authorizing": False}))
        signal_ids = [arm["variant_id"] for arm in inventory["arms"]
                      if arm["role"] in {
                          "new_mechanism", "cost_compatible_mechanism"}]
        signal_calls = []
        def signal_quality(*args, **kwargs):
            index = len(signal_calls)
            signal_calls.append((args, kwargs))
            return {
                "schema": "signal-quality.v1",
                "variant_id": signal_ids[index],
                "measurement_scope": (
                    "previously_examined_retrospective_diagnostic"),
                "horizons": [5, 15, 30, 60],
                "diagnostic_only": True,
                "authorizing": False,
            }
        stack.enter_context(patch.object(
            research, "measure_signal_quality",
            side_effect=signal_quality))
        stack.enter_context(patch.object(
            research, "_fit_prefixes", return_value={
                "first_signals": [{"symbol": "SPY", "session": "2026-01-05"}],
                "eligibility_provenance": {"total_prefixes": 37,
                                            "mature_prefixes": 30}}))
        stack.enter_context(patch.object(
            research, "_WORKER", {"bars": [], "policy": {}}))

    def test_frozen_inventory_is_exactly_55_distinct_non_authorizing_arms(self):
        inventory = research.frozen_inventory(
            load_config(Path("config.yaml")), "a" * 64)

        self.assertEqual(inventory["registered_arms"], 55)
        self.assertEqual(len(inventory["arms"]), 55)
        self.assertEqual(len({arm["variant_id"] for arm in inventory["arms"]}), 55)
        self.assertEqual(inventory["controls"]["registered_arms"], 43)
        self.assertEqual(inventory["mechanisms"]["registered_arms"], 12)
        self.assertTrue(inventory["diagnostic_only"])
        self.assertFalse(inventory["authorizing"])
        self.assertEqual(inventory["selection"], "none")

    def test_success_writes_only_immutable_diagnostic_artifacts(self):
        inventory = _inventory()
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            self._common_patches(stack, inventory)
            stack.enter_context(patch.object(
                research, "_evaluate_arm",
                side_effect=lambda arm: {
                    **arm, "outcome": "no_signal", "rows": [],
                    "diagnostic_only": True, "authorizing": False,
                }))
            data = Path(directory) / "market.jsonl"
            data.write_text("synthetic\n", encoding="utf-8")
            output = Path(directory) / "run"

            receipt = research.run(data, output, runtime_config={},
                                   signal_quality=True)

            self.assertEqual(receipt["status"], "diagnostic_complete")
            self.assertTrue(receipt["diagnostic_only"])
            self.assertFalse(receipt["authorizing"])
            self.assertEqual(receipt["eligible"], [])
            self.assertEqual(receipt["proofs"], [])
            self.assertEqual(receipt["selection"], "none")
            self.assertIsNone(receipt["portfolio_pnl"])
            self.assertEqual(len(receipt["results"]), 55)
            self.assertEqual(len(receipt["components"]), 55)
            self.assertEqual(len(receipt["signal_quality"]), 12)
            self.assertEqual(receipt["signal_quality_count"], 12)
            self.assertEqual(receipt["signal_quality_variant_ids"], [
                arm["variant_id"] for arm in inventory["arms"]
                if arm["role"] == "new_mechanism"])
            handed = research.measure_signal_quality.call_args.kwargs
            self.assertEqual(handed["eligibility_provenance"]["total_prefixes"], 37)
            self.assertEqual(handed["precomputed_first_signals"][0]["symbol"], "SPY")
            self.assertEqual(handed["measurement_scope"],
                             "previously_examined_retrospective_diagnostic")

            result_path = output / "result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["content_hash"], receipt["content_hash"])
            self.assertFalse(result["authorizing"])
            self.assertEqual(result["proofs"], [])
            self.assertEqual(len(list(output.glob("arm-*.json"))), 55)
            self.assertEqual(len(list(output.glob("signal-*.json"))), 12)

            # Every runner-owned arm artifact is explicitly partial/unsealed;
            # only result.json is allowed to claim completion.
            for path in output.glob("arm-*.json"):
                component = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(component["status"],
                                 "unsealed_partial_diagnostic")
                self.assertFalse(component["authorizing"])
                self.assertTrue(component["diagnostic_only"])

    def test_rule_worker_is_released_before_strict_ibr_allocates(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            self._common_patches(stack, _inventory())
            stack.enter_context(patch.object(
                research, "_evaluate_arm", side_effect=lambda arm: {
                    **arm, "outcome": "no_signal", "rows": []}))
            events = []
            research._close_worker.side_effect = lambda: events.append("closed")
            def ibr(*args, **kwargs):
                self.assertEqual(events, ["closed"])
                events.append("ibr")
                return {"status": "unavailable"}
            research.run_offline_forward_ibr.side_effect = ibr
            data = Path(directory) / "market.jsonl"
            data.write_text("synthetic\n")
            research.run(data, Path(directory) / "run", runtime_config={},
                         signal_quality=True)
            self.assertEqual(events, ["closed", "ibr", "closed"])

    def test_additive_cost_inventory_preserves_all_55_frozen_comparisons(self):
        config = load_config(Path("config.yaml"))
        original = research.frozen_inventory(config, "a" * 64)
        expanded = research.frozen_inventory(config, "a" * 64,
                                             include_cost_compatible=True)
        self.assertEqual(expanded["arms"][:55], original["arms"])
        self.assertEqual(expanded["registered_arms"], 67)
        self.assertEqual(len({arm["variant_id"] for arm in expanded["arms"]}), 67)
        self.assertTrue(all(arm["role"] == "cost_compatible_mechanism"
                            for arm in expanded["arms"][55:]))

    def test_additive_scope_requires_signal_quality(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            self._common_patches(stack, _inventory())
            data = Path(directory) / "market.jsonl"
            data.write_text("synthetic\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "signal_quality=True"):
                research.run(
                    data, Path(directory) / "run", runtime_config={},
                    signal_quality=False, include_cost_compatible=True,
                    evaluation_scope="additive_geometry")

    def test_additive_run_binds_baseline_and_measures_only_new_geometry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, projection_dir, manifest = _small_projection(root)
            with patch.object(
                    research, "_code_bundle_hash",
                    side_effect=["a" * 64, "a" * 64,
                                 "b" * 64, "b" * 64]):
                baseline = research.run(
                    manifest["compact_jsonl"], root / "baseline",
                    runtime_config=config, signal_quality=False,
                    ibr_max_events=10,
                    projection_manifest=projection_dir / "manifest.json")
                current = research.run(
                    manifest["compact_jsonl"], root / "additive",
                    runtime_config=config, signal_quality=True,
                    include_cost_compatible=True,
                    evaluation_scope="additive_geometry",
                    baseline_result=root / "baseline/result.json",
                    projection_manifest=projection_dir / "manifest.json")
            self.assertEqual(current["registered_arms"], 67)
            self.assertEqual(current["evaluated_arms"], 12)
            self.assertEqual(len(current["components"]), 12)
            self.assertEqual(len(current["not_recomputed_variant_ids"]), 55)
            self.assertEqual(len(current["not_recomputed_execution_variant_ids"]), 55)
            self.assertEqual(current["not_recomputed_execution_count"], 55)
            self.assertEqual(len(current["signal_quality"]), 24)
            self.assertEqual(current["signal_quality_count"], 24)
            self.assertEqual(
                current["signal_quality_variant_ids"],
                [arm["variant_id"] for arm in research.frozen_inventory(
                    config, "b" * 64, include_cost_compatible=True)["arms"]
                 if arm["role"] in {
                     "new_mechanism", "cost_compatible_mechanism"}],
            )
            self.assertEqual(current["baseline_comparison"]["content_hash"],
                             baseline["content_hash"])
            self.assertNotEqual(current["code_hash"],
                                current["baseline_comparison"]["code_hash"])
            self.assertEqual(current["ibr_runtime"]["status"], "not_recomputed")
            self.assertFalse(current["authorizing"])
            self.assertEqual(current["proofs"], [])
            for ref in current["signal_quality"]:
                quality = json.loads((root / "additive" / ref["path"]).read_text())
                self.assertEqual(quality["scope"],
                                 "previously_examined_retrospective_diagnostic")
            path = root / "baseline/result.json"
            tampered = json.loads(path.read_text())
            tampered["selection"] = "winner"
            path.write_text(json.dumps(tampered))
            with self.assertRaisesRegex(ValueError, "sealed non-authorizing"):
                research.run(manifest["compact_jsonl"], root / "rejected",
                    runtime_config=config, include_cost_compatible=True,
                    evaluation_scope="additive_geometry", baseline_result=path)
            self.assertFalse((root / "rejected").exists())

    def test_mid_run_failure_keeps_partial_components_and_blocks_final_result(self):
        inventory = _inventory()
        calls = 0

        def fail_on_third(arm):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError("synthetic arm failure")
            return {**arm, "outcome": "no_signal", "rows": [],
                    "diagnostic_only": True, "authorizing": False}

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            self._common_patches(stack, inventory)
            stack.enter_context(patch.object(research, "_evaluate_arm",
                                              side_effect=fail_on_third))
            data = Path(directory) / "market.jsonl"
            data.write_text("synthetic\n", encoding="utf-8")
            output = Path(directory) / "run"

            with self.assertRaisesRegex(RuntimeError, "synthetic arm failure"):
                research.run(data, output, runtime_config={},
                             signal_quality=False)

            self.assertEqual(calls, 3)
            self.assertTrue((output / "registration.json").exists())
            self.assertTrue((output / "cost-calibration.json").exists())
            self.assertEqual(len(list(output.glob("arm-*.json"))), 2)
            self.assertFalse((output / "trader-diagnostics.json").exists())
            self.assertFalse((output / "result.json").exists())

            # The existing directory is the durable run reservation; retrying
            # cannot overwrite the registration or partial evidence.
            with self.assertRaisesRegex(ValueError, "new immutable research output"):
                research.run(data, output, runtime_config={}, signal_quality=False)

    def test_unavailable_cost_quotes_are_retained_as_diagnostic_evidence(self):
        inventory = _inventory()
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            self._common_patches(stack, inventory)
            stack.enter_context(patch.object(
                research, "run_cost_calibration",
                side_effect=ValueError("no usable two-sided quotes in the corpus")))
            stack.enter_context(patch.object(
                research, "_evaluate_arm",
                side_effect=lambda arm: {
                    **arm, "outcome": "unavailable", "rows": [],
                    "diagnostic_only": True, "authorizing": False,
                }))
            data = Path(directory) / "bars-only.jsonl"
            data.write_text("synthetic\n", encoding="utf-8")
            output = Path(directory) / "run"

            result = research.run(data, output, runtime_config={},
                                  signal_quality=False)

            self.assertEqual(result["status"], "diagnostic_complete")
            calibration = json.loads(
                (output / "cost-calibration.json").read_text(encoding="utf-8"))
            self.assertEqual(calibration["status"], "unavailable")
            self.assertFalse(calibration["authorizing"])
            self.assertTrue(calibration["diagnostic_only"])
            self.assertIn("two-sided quotes", calibration["reason"])

    def test_projection_manifest_drives_real_small_diagnostic_run(self):
        """A sealed bar-only projection remains diagnostic when quotes are absent."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, projection_dir, manifest = _small_projection(root)

            receipt = research.run(
                manifest["compact_jsonl"], root / "run",
                runtime_config=config, signal_quality=False, ibr_max_events=10,
                projection_manifest=projection_dir / "manifest.json",
            )

            self.assertEqual(receipt["status"], "diagnostic_complete")
            self.assertEqual(len(receipt["results"]), 55)
            self.assertEqual(receipt["proofs"], [])
            self.assertFalse(receipt["authorizing"])
            calibration = json.loads(
                (root / "run/cost-calibration.json").read_text(encoding="utf-8"))
            self.assertEqual(calibration["schema"],
                             "unthinned-quote-calibration-run.v1")
            self.assertEqual(calibration["projection_manifest_hash"],
                             manifest["manifest_hash"])
            self.assertEqual(calibration["stress_calibration"]["status"],
                             "unavailable")
            registration = json.loads(
                (root / "run/registration.json").read_text(encoding="utf-8"))
            self.assertEqual(registration["projection"]["manifest_hash"],
                             manifest["manifest_hash"])
            result = json.loads(
                (root / "run/result.json").read_text(encoding="utf-8"))
            self.assertFalse(result["authorizing"])

    def test_projection_tampering_is_rejected_before_any_result(self):
        cases = (
            ("compact", "projection data hash differs"),
            ("calibration", "raw quote-cost artifact hash differs: calibration.json"),
            ("config", "projection runtime configuration differs"),
        )
        for kind, message in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config, projection_dir, manifest = _small_projection(root)
                compact = Path(manifest["compact_jsonl"])
                if kind == "compact":
                    compact.write_text(compact.read_text(encoding="utf-8") + "\n",
                                       encoding="utf-8")
                elif kind == "calibration":
                    (projection_dir / "calibration.json").write_text(
                        json.dumps({"tampered": True}), encoding="utf-8")
                else:
                    config = {**config, "research": {
                        **config.get("research", {}), "tampered": True}}

                output = root / "run"
                with self.assertRaisesRegex(ValueError, message):
                    research.run(
                        compact, output, runtime_config=config,
                        signal_quality=False, ibr_max_events=10,
                        projection_manifest=projection_dir / "manifest.json",
                    )
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
