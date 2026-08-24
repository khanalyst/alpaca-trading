"""Frozen, non-authorizing stressed-cost policy counterfactual.

The experiment evaluates one preregistered set of rule specifications twice on
the same diagnostic corpus.  Only ``max_stressed_cost_to_risk_ratio`` changes.
It is an engineering reachability test, not a promotion gate and not a way to
choose a threshold from held-out performance.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from agent.config import load_config
from agent.contracts.rule import rule_variant_id, validate_rule_spec

from .costs import (CostModel, ReplayPolicy, SQLiteQuoteIndex,
                    diagnostic_backfill_policy)
from .edge_lab import _read_discovery_rows, content_hash
from .factory_core import simulate_account


SCHEMA = "stressed-cost-ratio-counterfactual.v1"


def load_frozen_specs(source: str | Path | Mapping | Sequence) -> list[dict]:
    """Load and content-deduplicate specs from a diagnostic report or list."""
    if isinstance(source, (str, Path)):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
    else:
        payload = source
    candidates: list[Mapping[str, Any]] = []
    if isinstance(payload, Mapping):
        reports = payload.get("reports")
        if isinstance(reports, Sequence) and not isinstance(reports, (str, bytes)):
            for report in reports:
                if not isinstance(report, Mapping):
                    continue
                variants = report.get("variants")
                if isinstance(variants, Sequence) and not isinstance(
                        variants, (str, bytes)):
                    candidates.extend(
                        item["rule_spec"] for item in variants
                        if isinstance(item, Mapping) and
                        isinstance(item.get("rule_spec"), Mapping))
                elif isinstance(report.get("rule_spec"), Mapping):
                    candidates.append(report["rule_spec"])
        elif isinstance(payload.get("rule_specs"), Sequence):
            candidates.extend(item for item in payload["rule_specs"]
                              if isinstance(item, Mapping))
        elif isinstance(payload.get("rule_spec"), Mapping):
            candidates.append(payload["rule_spec"])
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        for item in payload:
            if isinstance(item, Mapping) and isinstance(item.get("rule_spec"), Mapping):
                candidates.append(item["rule_spec"])
            elif isinstance(item, Mapping):
                candidates.append(item)
    if not candidates:
        raise ValueError("counterfactual requires at least one frozen rule spec")
    unique = {}
    for candidate in candidates:
        spec = validate_rule_spec(candidate)
        unique[rule_variant_id(spec)] = spec
    return [unique[key] for key in sorted(unique)]


def _ratio(value: object, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive finite number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{label} must be a positive finite number")
    return parsed


def _summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    dispositions = Counter()
    reasons = Counter()
    signal_opportunities = 0
    trades = 0
    net_pnl = 0.0
    returns = []
    for row in rows:
        disposition = str(row.get("execution_disposition") or
                          ("executed" if not row.get("no_trade") else
                           "unclassified"))
        dispositions[disposition] += 1
        if row.get("signal_opportunity") is True:
            signal_opportunities += 1
        reason = str(row.get("reject_reason") or "").strip()
        if reason:
            reasons[reason] += 1
        if disposition == "executed" or not row.get("no_trade"):
            trades += 1
            try:
                net_pnl += float(row.get("net_pnl") or 0.0)
                returns.append(float(row.get("return_value") or 0.0))
            except (TypeError, ValueError):
                pass
    stressed = int(reasons.get("stressed_cost_risk_limit", 0))
    denominator = signal_opportunities
    return {
        "rows": len(rows),
        "signal_opportunities": signal_opportunities,
        "dispositions": dict(sorted(dispositions.items())),
        "reject_reasons": dict(sorted(reasons.items())),
        "stressed_cost_risk_limit": stressed,
        "stressed_cost_rejection_rate": (
            stressed / denominator if denominator else None),
        "trades": trades,
        "net_pnl": net_pnl,
        "mean_return_value": (sum(returns) / len(returns) if returns else None),
    }


def run_counterfactual(
        data: str | Path | Sequence[Mapping], *,
        specs: Sequence[Mapping[str, Any]], runtime_config: Mapping[str, Any],
        baseline_ratio: float = .30, alternative_ratio: float = .60,
        vehicle: str = "equity", starting_cash: float = 100_000.0) -> dict:
    """Run the same frozen cohort under exactly two stressed-cost ratios."""
    baseline = _ratio(baseline_ratio, "baseline_ratio")
    alternative = _ratio(alternative_ratio, "alternative_ratio")
    if baseline == alternative:
        raise ValueError("counterfactual ratios must differ")
    frozen = load_frozen_specs(list(specs))
    raw_rows, bars, snapshot_map, quote_rows = _read_discovery_rows(
        data, require_provenance=False,
        expected_equity_feed=ReplayPolicy.from_config(runtime_config).equity_feed)
    quotes = (quote_rows if callable(getattr(quote_rows, "quote_fill", None))
              else list(quote_rows))
    arms: dict[str, dict[str, Any]] = {}
    try:
        for name, ratio in (("baseline", baseline),
                            ("alternative", alternative)):
            config = deepcopy(dict(runtime_config))
            risk = dict(config.get("risk") or {})
            risk["max_stressed_cost_to_risk_ratio"] = ratio
            config["risk"] = risk
            policy = diagnostic_backfill_policy(ReplayPolicy.from_config(config))
            costs = CostModel.from_config(config, vehicle=vehicle)
            combined: list[dict] = []
            per_variant = []
            for spec in frozen:
                variant_id = rule_variant_id(spec)
                account = simulate_account(
                    bars, list(snapshot_map.values()), spec, vehicle=vehicle,
                    account_id=f"counterfactual:{name}:{variant_id}",
                    starting_cash=float(starting_cash), costs=costs,
                    quotes=quotes, policy=policy)
                rows = [dict(row) for row in account.get("rows", ())
                        if isinstance(row, Mapping)]
                combined.extend(rows)
                per_variant.append({
                    "variant_id": variant_id,
                    "summary": _summarize(rows),
                })
            arms[name] = {
                "max_stressed_cost_to_risk_ratio": ratio,
                "replay_policy": policy.as_dict(),
                "summary": _summarize(combined),
                "variants": per_variant,
            }
    finally:
        close = getattr(quote_rows, "close", None)
        if callable(close) and isinstance(quote_rows, SQLiteQuoteIndex):
            close()
    base = arms["baseline"]["summary"]
    alt = arms["alternative"]["summary"]
    return {
        "schema": SCHEMA,
        "status": ("no_signal_reachability" if
                   not base["signal_opportunities"] and
                   not alt["signal_opportunities"] else "measured"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "diagnostic_only": True,
        "authorizing": False,
        "promotion_allowed": False,
        "primary_endpoint": "stressed_cost_risk_limit refusal rate",
        "dataset_hash": content_hash(raw_rows),
        "frozen_variant_ids": [rule_variant_id(spec) for spec in frozen],
        "frozen_cohort_hash": hashlib.sha256(json.dumps(
            frozen, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "only_changed_field": "risk.max_stressed_cost_to_risk_ratio",
        "arms": arms,
        "difference": {
            "stressed_cost_rejections": (
                alt["stressed_cost_risk_limit"] -
                base["stressed_cost_risk_limit"]),
            "admitted_trades": alt["trades"] - base["trades"],
            "signal_opportunities": (
                alt["signal_opportunities"] - base["signal_opportunities"]),
        },
        "decision_rule": (
            "This one-cycle counterfactual may justify a separately reviewed "
            "policy experiment; it cannot change production or promote an edge."),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True)
    parser.add_argument("--specs", required=True)
    parser.add_argument("--agent-config", default="config.yaml")
    parser.add_argument("--vehicle", choices=("equity", "option"), default="equity")
    parser.add_argument("--baseline", type=float, default=.30)
    parser.add_argument("--alternative", type=float, default=.60)
    parser.add_argument("--starting-cash", type=float, default=100_000.0)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    result = run_counterfactual(
        args.data, specs=load_frozen_specs(args.specs),
        runtime_config=load_config(args.agent_config),
        baseline_ratio=args.baseline, alternative_ratio=args.alternative,
        vehicle=args.vehicle, starting_cash=args.starting_cash)
    serialized = json.dumps(result, sort_keys=True, default=str) + "\n"
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(serialized, encoding="utf-8")
        os.replace(temporary, target)
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
