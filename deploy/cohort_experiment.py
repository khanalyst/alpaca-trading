#!/usr/bin/env python3
"""Run the registered mechanism cohort against one verified sealed snapshot.

This command has no broker and writes only under its new output directory.
Registration precedes evaluation; all twelve arms and full replay rows survive.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.config import load_config
from deploy.provenance import deployment_provenance
from deploy.research_dataset import build_views
from deploy.research_snapshot import verify_snapshot
from research.costs import cost_model_for_vehicle
from research.mechanism_cohort import COHORT_ID, mechanism_cohort
from research.market_context_study import context_study, factor_beta
from research.strategy_factory import run_factory


def write(path, value):
    with path.open("x") as handle:
        json.dump(value, handle, sort_keys=True, default=str, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def run(snapshot, output, config):
    sealed = verify_snapshot(snapshot)
    output.mkdir(parents=True, exist_ok=False)
    cfg = load_config(config)
    costs = cost_model_for_vehicle(cfg, "equity")
    registered = {"schema": "cohort-experiment-registration.v1", "diagnostic_only": True,
                  "authorizing": False, "registered_at": datetime.now(timezone.utc).isoformat(),
                  "snapshot": sealed, "cohort": mechanism_cohort(), "costs": costs.as_dict(),
                  "provenance": deployment_provenance(), "automatic_winner_selection": False,
                  "confirmation_start_not_before": (datetime.now(timezone.utc).date()+timedelta(days=1)).isoformat(),
                  "confirmation_policy": "Keep every registered arm; use only unseen forward observations, paired baselines and the unchanged repository proof/FDR and cost gates. Historical point estimates do not select or authorize a winner."}
    write(output / "registration.json", registered)
    print(json.dumps({"phase": "preparing", "snapshot": sealed["identity"]}), flush=True)
    views = build_views(partition_root=snapshot / "sessions", recorded_root=snapshot,
                        normalized=output / "market.jsonl", bars=output / "bars.jsonl",
                        options=output / "options.jsonl", replay=output / "replay.jsonl",
                        from_recorder=True, selected_vehicles="equity", agent_config=cfg)
    write(output / "views.json", views)
    # Context is a separate diagnostic, using the same resolved frozen bars.
    # Its historical option never changes the trading/research source policy.
    with (output / "bars.jsonl").open() as handle:
        bars = []
        for line in handle:
            if line.strip():
                if len(bars) >= 500000:
                    raise ValueError("diagnostic cohort exceeds the 500,000 bar bound")
                bars.append(json.loads(line))
    symbols = sorted({row["symbol"] for row in bars})
    if bars:
        from deploy.market_observations import epoch
        cutoff = max(epoch(row["timestamp"]) + 60 for row in bars)
        contexts = [context_study(bars, as_of=cutoff, symbol=symbol, universe=symbols,
                                  feed=cfg["broker"]["data_feed"], allow_backfill=True)
                    for symbol in symbols]
        benchmark = [r for r in bars if r["symbol"] == "SPY"]
        betas = {symbol: factor_beta([r for r in bars if r["symbol"] == symbol], benchmark)
                 for symbol in symbols if symbol != "SPY"}
        write(output / "market-context.json", {"contexts": contexts, "spy_beta": betas,
             "diagnostic_only": True, "authorizing": False, "snapshot_identity": sealed["identity"]})
    result = run_factory(output / "market.jsonl", vehicle="equity", diagnostic_only=True,
                         cohort=COHORT_ID, strategy_llm={"enabled": False}, costs=costs,
                         runtime_config=cfg, progress_callback=lambda phase, done, total:
                         print(json.dumps({"phase": phase, "done": done, "total": total}), flush=True))
    verify_snapshot(snapshot)
    result.update(snapshot_identity=sealed["identity"], created_at=datetime.now(timezone.utc).isoformat(),
                  feed=cfg["broker"]["data_feed"])
    write(output / "full-result.json", result)
    # Small dashboard artifact. Full account rows remain in full-result.json.
    summary = {key: result.get(key) for key in ("schema", "diagnostic_only", "authorizing", "status",
        "feed", "created_at", "snapshot_identity", "source_mode_counts", "bar_coverage", "mechanism_cohort")}
    summary["reports"] = [{"family": row["family"], "variants": [{"variant_id": arm["variant_id"],
        "rule_spec": arm["rule_spec"], "diagnostic": arm["diagnostic"]} for arm in row["variants"]]}
        for row in result["reports"]]
    write(output / "summary.json", summary)
    print(json.dumps({"phase": "completed", "arms": result["variants"], "output": str(output),
                      "authorizing": False}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    args = parser.parse_args()
    run(args.snapshot, args.output, args.config)


if __name__ == "__main__":
    main()
