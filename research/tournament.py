"""Score every registered strategy through the six gates, and rank them.

One command answers "what do we currently believe about each strategy, and
why". It reads the register (agent/registry.py) for what exists, the
pre-registration files (research/hypotheses/*.yaml) for what was claimed
before the test ran, and produces a leaderboard plus a written report.

Two rules keep it honest:

**No score without a pre-registration.** A strategy with no hypothesis file
is not scored at all. The file records the parameters and the hypothesis
count before the result exists, which is the only defence against quietly
tuning until something looks good.

**The benchmark must reproduce.** momentum/phase1-v2 is scored on every run
even though its answer is known. If the harness stops reproducing its
measured failure, the harness has broken and every other number in the run
is suspect. That check is the reason this is a tournament and not a search.

    python research/tournament.py --data runtime/research/data
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "research"))

import yaml  # noqa: E402

import gates as gate_mod  # noqa: E402
from agent.registry import REGISTRY, TIERS  # noqa: E402
from edge_lab import (Contract, FlushFadeContract, load_dataset,  # noqa: E402
                      universe_membership)


HYPOTHESES = REPO / "research" / "hypotheses"

# Which research contract implements each registered strategy. A strategy in
# the register with no entry here is registered but not yet testable, which
# the report states rather than silently skipping.
CONTRACTS = {
    "momentum": lambda cfg: Contract.from_config(cfg) if cfg else Contract(),
    "flush-fade": lambda cfg: FlushFadeContract(),
}

# The benchmark's known values, from research/results/edge-audit-2024-2026.
# Reproduction is checked loosely: different data windows legitimately move
# the number, but a sign flip or an order-of-magnitude change means the
# harness is measuring something else.
BENCHMARK_ID = "momentum"
BENCHMARK_EXPECTED_R = -0.096
BENCHMARK_TOLERANCE_R = 0.20


def load_preregistration(strategy_id: str) -> dict | None:
    path = HYPOTHESES / f"{strategy_id}.yaml"
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text())


def score_strategy(spec, frames, membership, cfg, cost_name: str,
                   exit_policy: str) -> dict:
    prereg = load_preregistration(spec.id)
    if prereg is None:
        return {
            "strategy_id": spec.id, "version": spec.version,
            "scored": False,
            "reason": (
                f"no pre-registration at research/hypotheses/{spec.id}.yaml; "
                "write the mechanism, parameters and hypothesis count before "
                "running the test"),
            "registered_tier": spec.tier,
        }
    factory = CONTRACTS.get(spec.id)
    if factory is None:
        return {
            "strategy_id": spec.id, "version": spec.version,
            "scored": False,
            "reason": ("registered but no research contract implements it "
                       "yet; see the register's notes for what blocks it"),
            "registered_tier": spec.tier,
            "notes": spec.notes,
        }

    contract = factory(cfg)
    results = gate_mod.run_all(spec, frames, membership, contract,
                              cost_name=cost_name, exit_policy=exit_policy)
    tier, why = gate_mod.tier_from_gates(results)
    stats = gate_mod.headline(frames, membership, contract, cost_name,
                              exit_policy)
    return {
        "strategy_id": spec.id,
        "version": spec.version,
        "scored": True,
        "registered_tier": spec.tier,
        "measured_tier": tier,
        "tier_reason": why,
        "tier_changed": tier != spec.tier,
        "hypotheses_tested": prereg.get("hypotheses_tested"),
        "mechanism": spec.mechanism,
        "falsification": spec.falsification,
        "headline": stats,
        "gates": [r.as_dict() for r in results],
    }


def benchmark_check(rows: list[dict]) -> dict:
    """Confirm the harness still reproduces the known-failing benchmark."""
    row = next((r for r in rows if r["strategy_id"] == BENCHMARK_ID), None)
    if row is None or not row.get("scored"):
        return {"ok": False, "reason": "benchmark was not scored"}
    measured = row["headline"].get("expectancy_r")
    if measured is None:
        return {"ok": False, "reason": "benchmark produced no trades"}
    drift = abs(float(measured) - BENCHMARK_EXPECTED_R)
    ok = float(measured) < 0 and drift <= BENCHMARK_TOLERANCE_R
    return {
        "ok": bool(ok),
        "measured_expectancy_r": float(measured),
        "expected_expectancy_r": BENCHMARK_EXPECTED_R,
        "drift_r": float(drift),
        "tolerance_r": BENCHMARK_TOLERANCE_R,
        "reason": (
            "benchmark reproduces its measured failure"
            if ok else
            "benchmark did NOT reproduce; treat every other number in this "
            "run as unverified until the harness is fixed"),
    }


def apply_forward_evidence(row: dict, forward: dict) -> None:
    """Attach live/shadow results and decide whether T4 is earned.

    T4_CONFIRMED is the only tier offline data cannot grant, because it is
    the only one that asks whether the effect survived contact with the
    future. Two conditions, both necessary: the forward result agrees in
    sign with the backtest, and there are at least as many forward trades as
    the detectability gate said the effect size requires.

    A sign DISAGREEMENT is recorded loudly even when the counts are small.
    It is the earliest available warning that a backtest was fitted, and it
    arrives long before the sample is large enough to prove anything.
    """
    if not row.get("scored"):
        return
    entry = (forward.get("strategies") or {}).get(row["strategy_id"])
    if not entry:
        row["forward"] = {"status": "no forward evidence recorded yet"}
        return

    observed = int(entry.get("resolved_trades") or 0)
    forward_pct = entry.get("forward_expectancy_pct")
    backtest_pct = row["headline"].get("expectancy_pct")
    needed = 0
    for gate in row["gates"]:
        if gate["gate"] == "is_detectable":
            needed = int(gate["numbers"].get("trades_needed") or 0)

    agrees = None
    if forward_pct is not None and backtest_pct is not None:
        agrees = (forward_pct >= 0) == (backtest_pct >= 0)

    row["forward"] = {
        "resolved_trades": observed,
        "trades_needed": needed,
        "forward_expectancy_pct": forward_pct,
        "backtest_expectancy_pct": backtest_pct,
        "signs_agree": agrees,
        "signals_fired": entry.get("signals_fired"),
        "positions_actually_opened": entry.get("positions_actually_opened"),
        "selectivity_pct": entry.get("selectivity_pct"),
        "progress_pct": (round(observed / needed * 100, 1)
                         if needed else None),
    }

    if agrees is False:
        row["forward"]["warning"] = (
            "forward and backtest disagree in sign; treat the backtest as "
            "unconfirmed regardless of how clean it looked")
    if (row["measured_tier"] == "T3_VALIDATED" and agrees
            and needed and observed >= needed):
        row["measured_tier"] = "T4_CONFIRMED"
        row["tier_reason"] = (
            f"forward evidence agrees in sign over {observed} trades "
            f"against the {needed} required")


def recommendation(row: dict) -> str:
    """Deterministic, so nobody has to decide how impressed to be."""
    if not row.get("scored"):
        return "not scored"
    tier = row["measured_tier"]
    if tier == "T0_REJECTED":
        return "REJECT - archive with the finding; do not retest without a new mechanism"
    if tier == "T1_HYPOTHESIS":
        return "HOLD - keep collecting data; nothing testable has passed yet"
    if tier == "T2_CANDIDATE":
        return "HOLD - promising but not clear of the placebo/cost gates"
    if tier == "T3_VALIDATED":
        return "PROMOTE - eligible for demo; forward evidence required for T4"
    if tier == "T4_CONFIRMED":
        return "CONFIRMED - forward evidence agrees; live capital may be discussed"
    return "HOLD"


def write_report(path: Path, payload: dict) -> None:
    rows = payload["strategies"]
    lines = [
        "# Strategy tournament", "",
        f"Generated {payload['generated_utc']} from `{payload['data_root']}`.",
        "",
        f"- instruments: {payload['instruments']}",
        f"- bars per instrument (min): {payload['min_bars']}",
        f"- window: {payload['window_start']} to {payload['window_end']}",
        f"- cost scenario: `{payload['cost_scenario']}`, "
        f"exit policy: `{payload['exit_policy']}`",
        "",
        "## Harness check", "",
    ]
    check = payload["benchmark_check"]
    lines += [
        f"**{'PASS' if check['ok'] else 'FAIL'}** - {check['reason']}",
        "",
    ]
    if "measured_expectancy_r" in check:
        lines.append(
            f"Benchmark `{BENCHMARK_ID}` measured "
            f"{check['measured_expectancy_r']:+.4f} R against an expected "
            f"{check['expected_expectancy_r']:+.4f} R "
            f"(drift {check['drift_r']:.4f}, tolerance "
            f"{check['tolerance_r']:.2f}).")
        lines.append("")

    lines += ["## Leaderboard", "",
              "| Strategy | Tier | Trades | Expectancy % | Expectancy R | "
              "Hypotheses | Recommendation |",
              "| --- | --- | ---: | ---: | ---: | ---: | --- |"]
    for row in rows:
        if not row.get("scored"):
            lines.append(
                f"| `{row['strategy_id']}` | {row['registered_tier']} "
                f"(registered) | - | - | - | - | {row['reason']} |")
            continue
        head = row["headline"]
        lines.append(
            f"| `{row['strategy_id']}/{row['version']}` "
            f"| {row['measured_tier']} "
            f"| {head.get('trades', 0)} "
            f"| {head.get('expectancy_pct', float('nan')):+.4f} "
            f"| {head.get('expectancy_r', float('nan')):+.4f} "
            f"| {row.get('hypotheses_tested', '?')} "
            f"| {recommendation(row)} |")
    lines.append("")

    for row in rows:
        if not row.get("scored"):
            continue
        lines += [f"## {row['strategy_id']}/{row['version']}", "",
                  f"**Measured tier: {row['measured_tier']}** - "
                  f"{row['tier_reason']}", ""]
        if row["tier_changed"]:
            promoted = (TIERS.index(row["measured_tier"])
                        > TIERS.index(row["registered_tier"]))
            if promoted:
                # An upward move is the direction in which wishful thinking
                # travels, so it gets the sceptical note. A short window can
                # easily flatter a strategy that a longer one condemns.
                lines += [
                    f"> Measured {row['measured_tier']} is ABOVE the "
                    f"registered {row['registered_tier']}. Do not promote on "
                    f"this alone: check that this run's window and instrument "
                    f"count are not thinner than the evidence behind the "
                    f"registered tier. A promotion needs more data than a "
                    f"demotion, not less.", ""]
            else:
                lines += [
                    f"> Measured {row['measured_tier']} is BELOW the "
                    f"registered {row['registered_tier']}. Evidence of "
                    f"failure is worth acting on at a lower bar than "
                    f"evidence of success: update `agent/registry.py` with "
                    f"this report as the record.", ""]
        lines += ["**Mechanism.** " + row["mechanism"].strip(), "",
                  "**Falsified by.** " + row["falsification"].strip(), "",
                  "| Gate | Result | Detail |", "| --- | --- | --- |"]
        for gate in row["gates"]:
            mark = "pass" if gate["passed"] else "FAIL"
            lines.append(
                f"| `{gate['gate']}` | {mark} | {gate['summary']} |")
        lines.append("")

        fwd = row.get("forward") or {}
        if fwd.get("status"):
            lines += [f"*Forward evidence: {fwd['status']}.*", ""]
        elif fwd:
            agree = fwd.get("signs_agree")
            lines += [
                "**Forward (live shadow).** "
                f"{fwd.get('resolved_trades', 0)} resolved trades of the "
                f"{fwd.get('trades_needed') or '?'} this effect size needs"
                + (f" ({fwd['progress_pct']}%)"
                   if fwd.get("progress_pct") is not None else "")
                + ".",
                "",
                f"- forward expectancy: "
                f"{fwd.get('forward_expectancy_pct', float('nan')):+.4f}% "
                f"vs backtest "
                f"{fwd.get('backtest_expectancy_pct', float('nan')):+.4f}%",
                f"- signs agree: "
                f"{'yes' if agree else 'NO' if agree is False else 'unknown'}",
            ]
            if fwd.get("selectivity_pct") is not None:
                lines.append(
                    f"- the contract fired {fwd.get('signals_fired')} times "
                    f"and the account opened "
                    f"{fwd.get('positions_actually_opened')} positions "
                    f"({fwd['selectivity_pct']}%) - the gap is the analyst "
                    f"layer's contribution")
            if fwd.get("warning"):
                lines += ["", f"> {fwd['warning']}"]
            lines.append("")

    lines += [
        "## How to read this", "",
        "No gate here tests a t-statistic. On this data a placebo reached "
        "t = 2.60 on deliberately destroyed information, so `t > 2` is not "
        "evidence - the placebo ratio is.", "",
        "A tier is a claim about evidence, not about promise. `T3_VALIDATED` "
        "means every offline gate passed; `T4_CONFIRMED` additionally "
        "requires forward trades at the sample size the detectability gate "
        "computed, agreeing in sign with the backtest.", "",
    ]
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--out", type=Path,
                        default=REPO / "research" / "results" / "tournament")
    parser.add_argument("--cost", default="base",
                        help="cost scenario the gates score against")
    parser.add_argument("--exit-policy", default="fixed_rr")
    parser.add_argument("--min-bars", type=int, default=8000)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--only", default="",
                        help="comma-separated strategy ids to score")
    parser.add_argument("--forward", type=Path,
                        default=REPO / "runtime" / "research"
                        / "forward_evidence.json",
                        help="output of research/export_live.py")
    args = parser.parse_args()

    frames = load_dataset(args.data, min_bars=args.min_bars)
    if not frames:
        print(f"No instrument in {args.data} has >= {args.min_bars} bars. "
              f"Download more history, or lower --min-bars for a smoke test.",
              file=sys.stderr)
        return 2
    membership = universe_membership(frames, top_n=args.top_n)

    cfg = None
    try:
        from agent.config import validate_config
        cfg = validate_config(
            yaml.safe_load((REPO / "config.yaml").read_text()))
    except Exception as exc:                       # noqa: BLE001
        print(f"note: config.yaml not usable for contract parameters ({exc}); "
              f"falling back to research defaults", file=sys.stderr)

    wanted = {s.strip() for s in args.only.split(",") if s.strip()}
    specs = [s for s in REGISTRY.values() if not wanted or s.id in wanted]
    specs.sort(key=lambda s: (-TIERS.index(s.tier), s.id))

    rows = []
    for spec in specs:
        print(f"scoring {spec.id}/{spec.version} ...", file=sys.stderr)
        rows.append(score_strategy(spec, frames, membership, cfg,
                                   args.cost, args.exit_policy))

    forward = {}
    if args.forward and args.forward.exists():
        try:
            forward = json.loads(args.forward.read_text())
        except (OSError, ValueError) as exc:               # noqa: BLE001
            print(f"note: could not read forward evidence ({exc})",
                  file=sys.stderr)
    for row in rows:
        apply_forward_evidence(row, forward)

    scored = [r for r in rows if r.get("scored")]
    scored.sort(key=lambda r: (-TIERS.index(r["measured_tier"]),
                               -(r["headline"].get("expectancy_pct") or 0)))
    unscored = [r for r in rows if not r.get("scored")]
    ordered = scored + unscored

    starts = [int(f.ts[0]) for f in frames.values()]
    ends = [int(f.ts[-1]) for f in frames.values()]

    def stamp(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime(
            "%Y-%m-%d")

    payload = {
        "generated_utc": datetime.now(timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%SZ"),
        "data_root": str(args.data),
        "instruments": len(frames),
        "min_bars": min(len(f.df) for f in frames.values()),
        "window_start": stamp(min(starts)),
        "window_end": stamp(max(ends)),
        "cost_scenario": args.cost,
        "exit_policy": args.exit_policy,
        "strategies": ordered,
    }
    payload["benchmark_check"] = benchmark_check(ordered)

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "leaderboard.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
    write_report(args.out / "REPORT.md", payload)

    check = payload["benchmark_check"]
    print(f"\nharness check: {'PASS' if check['ok'] else 'FAIL'} - "
          f"{check['reason']}")
    for row in ordered:
        if row.get("scored"):
            head = row["headline"]
            print(f"  {row['strategy_id']:<16} {row['measured_tier']:<15} "
                  f"{head.get('trades', 0):>6} trades  "
                  f"{head.get('expectancy_pct', float('nan')):+.4f}%  "
                  f"{recommendation(row)}")
        else:
            print(f"  {row['strategy_id']:<16} {'NOT SCORED':<15} "
                  f"{row['reason'][:60]}")
    print(f"\nwrote {args.out / 'REPORT.md'}")
    return 0 if check["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
