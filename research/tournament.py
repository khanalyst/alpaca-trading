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
import dataclasses
import json
import sys
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "research"))

import yaml  # noqa: E402

import gates as gate_mod  # noqa: E402
from agent.registry import REGISTRY, TIERS  # noqa: E402
from edge_lab import (Contract, FlushFadeContract,  # noqa: E402
                      FundingCarryContract, TrendMultidayContract,
                      load_dataset, universe_membership)


HYPOTHESES = REPO / "research" / "hypotheses"

# Which research contract implements each registered strategy. A strategy in
# the register with no entry here is registered but not yet testable, which
# the report states rather than silently skipping.

# Each entry returns the contract at its REGISTERED parameterisation. The
# pre-registration's `settings:` block names the alternatives, and
# `axis_settings` derives them from this one with `dataclasses.replace`, so a
# setting cannot silently change a field the registered point never had.
CONTRACTS = {
    "momentum": lambda cfg: Contract.from_config(cfg) if cfg else Contract(),
    "flush-fade": lambda cfg: FlushFadeContract(),
    "trend-multiday": lambda cfg: TrendMultidayContract(),
    "funding-carry": lambda cfg: FundingCarryContract(),
    # Same entry rule as funding-carry under an opposite mechanism
    # claim. Sharing the contract is deliberate: what differs is the
    # claim about where the money comes from, and the attribution
    # gate is what tells the two apart.
    "funding-unwind": lambda cfg: FundingCarryContract(),
    # ls-ratio-fade is deliberately absent. Its input series is served for
    # ~30 days and is not in the research dataset, so it cannot be
    # backtested honestly - it accumulates forward evidence through shadow
    # evaluation only, and the report says so rather than scoring it on
    # data that does not exist.
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

REGISTERED_SETTING = "registered"


def axis_settings(strategy_id: str, prereg: dict, cfg: dict | None) -> list[dict]:
    """Every pre-registered parameterisation of one hypothesis.

    A hypothesis used to be scored at exactly one point - the contract's
    dataclass defaults - and the battery would turn that single point into a
    tier. `flush-fade` was rejected at `min_move_atr=1.5` and nothing else was
    ever tried, while the register recorded that the mechanism remained
    plausible. That is the failure `protocol.md` criterion 1 exists to prevent,
    and it only ever governed the journal path.

    The alternatives are declared in the pre-registration file, beside the
    mechanism and the falsifier and before any of them is scored, because a
    parameterisation added after seeing the result is not a robustness check.
    Validation is fail-closed for the same reason `agent/variants.py` refuses
    to invent config structure: a typo that registers cleanly would score the
    registered point three times and report that the parameter made no
    difference.
    """
    base = CONTRACTS[strategy_id](cfg)
    known = {field.name for field in dataclasses.fields(base)}
    declared = prereg.get("settings")
    if not declared:
        return [{"setting_id": REGISTERED_SETTING, "params": {},
                 "claim": "the pre-registered parameterisation",
                 "contract": base, "registered": True}]
    if not isinstance(declared, list):
        raise ValueError(f"{strategy_id}: settings must be a list")

    out: list[dict] = []
    seen_ids: set[str] = set()
    seen_values: set[tuple] = set()
    for index, entry in enumerate(declared):
        if not isinstance(entry, dict):
            raise ValueError(f"{strategy_id}: setting #{index} is not a mapping")
        unknown_keys = set(entry) - {"id", "params", "claim"}
        if unknown_keys:
            raise ValueError(
                f"{strategy_id}: setting #{index} has unknown field(s): "
                f"{', '.join(sorted(unknown_keys))}")
        setting_id = str(entry.get("id") or "").strip()
        if not setting_id:
            raise ValueError(f"{strategy_id}: setting #{index} has no id")
        if setting_id in seen_ids:
            raise ValueError(
                f"{strategy_id}: duplicate setting id {setting_id!r}")
        params = entry.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError(f"{strategy_id}: {setting_id} params must be a map")
        invalid = sorted(set(params) - known)
        if invalid:
            raise ValueError(
                f"{strategy_id}: {setting_id} sets {', '.join(invalid)}, which "
                f"{base.__class__.__name__} does not have. Available: "
                f"{', '.join(sorted(known))}")
        claim = str(entry.get("claim") or "").strip()
        if len(claim) < 10:
            raise ValueError(
                f"{strategy_id}: {setting_id} must say what it claims, in a "
                "sentence - an unexplained parameterisation is a grid point")
        fingerprint = tuple(sorted(params.items(), key=lambda kv: kv[0]))
        if fingerprint in seen_values:
            raise ValueError(
                f"{strategy_id}: {setting_id} duplicates another setting's "
                "parameters")
        seen_values.add(fingerprint)
        seen_ids.add(setting_id)
        out.append({
            "setting_id": setting_id, "params": dict(params), "claim": claim,
            "contract": dataclasses.replace(base, **params) if params else base,
            "registered": not params,
        })

    if sum(1 for setting in out if setting["registered"]) != 1:
        raise ValueError(
            f"{strategy_id}: exactly one setting must have empty params - the "
            "registered point every other setting is compared against")
    return out


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

    try:
        settings = axis_settings(spec.id, prereg, cfg)
    except ValueError as exc:
        return {
            "strategy_id": spec.id, "version": spec.version,
            "scored": False,
            "reason": f"pre-registered settings are invalid: {exc}",
            "registered_tier": spec.tier,
        }

    for setting in settings:
        setting["results"] = gate_mod.run_all(
            spec, frames, membership, setting["contract"],
            cost_name=cost_name, exit_policy=exit_policy, prereg=prereg)
        setting["tier"], setting["why"] = gate_mod.tier_from_gates(
            setting["results"])
        setting["headline"] = gate_mod.headline(
            frames, membership, setting["contract"], cost_name, exit_policy)

    # The unit of decision is the hypothesis, not the parameterisation that
    # happened to be tried first.
    tier, why, best = gate_mod.tier_from_settings(settings)
    registered = next(setting for setting in settings if setting["registered"])
    contract = registered["contract"]
                   
    # Provenance cap. A hypothesis GENERATED by looking at the data it is
    # now scored on cannot be confirmed by that data, however clean the
    # gates come back - the gates measure the result, and the result is what
    # suggested the hypothesis. Declared in the pre-registration because
    # nothing in the numbers can reveal it, and enforced here because a fact
    # that only lives in a comment is a fact that gets forgotten.
    if prereg.get("in_sample_by_construction") and tier != "T0_REJECTED":
        tier, why = "T1_HYPOTHESIS", (
            "gates return " + tier + ", but this hypothesis was generated "
            "from the data it is scored on, so the result is in-sample by "
            "construction. Needs data that did not suggest it: "
            + str(prereg.get("what_would_change_the_verdict")
                  or "out-of-sample history or forward evidence").strip()
            .replace("\n", " ")[:200])
    # The headline stays the REGISTERED parameterisation's, not the best
    # setting's. benchmark_check compares it against a stored expectation, so
    # letting a better setting supply it would make the harness-reproduction
    # check drift every time the settings list changed - and that check is the
    # reason this is a tournament rather than a search.
    stats = registered["headline"]
    # A tier registered above this battery's ceiling was granted by the
    # authoritative recorded-replay path, which this run cannot reproduce and
    # therefore cannot contradict. Measuring T2 against a registered T3 is not
    # a demotion, it is the battery reaching the end of what it may say - so it
    # must not be reported or alerted as evidence of failure, or the nightly
    # run would file the same false alarm forever.
    beyond_authority = (TIERS.index(spec.tier)
                        > TIERS.index(gate_mod.EXPLORATORY_CEILING))
    return {
        "strategy_id": spec.id,
        "version": spec.version,
        "scored": True,
        "registered_tier": spec.tier,
        "measured_tier": tier,
        "tier_reason": why,
        "exploratory_ceiling": gate_mod.EXPLORATORY_CEILING,
        "beyond_exploratory_authority": beyond_authority,
        "tier_changed": tier != spec.tier and not beyond_authority,
        "hypotheses_tested": prereg.get("hypotheses_tested"),
        "mechanism": spec.mechanism,
        "falsification": spec.falsification,
        "headline": stats,
        # The registered point's gates, so the report's per-strategy gate table
        # keeps describing the parameterisation the register names.
        "gates": [r.as_dict() for r in registered["results"]],
        "settings_tested": len(settings),
        "best_setting": best["setting_id"],
        "settings": [{
            "setting_id": setting["setting_id"],
            "params": setting["params"],
            "claim": setting["claim"],
            "registered": setting["registered"],
            "tier": setting["tier"],
            "why": setting["why"],
            "trades": (setting["headline"] or {}).get("trades", 0),
            "expectancy_pct": (setting["headline"] or {}).get("expectancy_pct"),
            "expectancy_r": (setting["headline"] or {}).get("expectancy_r"),
            "p_adjusted": setting.get("p_adjusted"),
            "significant_corrected": setting.get("significant_corrected"),
            "gates": [r.as_dict() for r in setting["results"]],
        } for setting in settings],
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


def _finite_float(value: object) -> float | None:
    """Return a finite float, or None for a missing or malformed value."""
    try:
        number = float(value)                              # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _format_signed(value: object, suffix: str = "") -> str:
    """Format an optional number without letting the report raise.

    A missing expectancy is a normal state - a strategy can have resolved
    trades journalled before any of them has an R multiple - and a nightly
    report that raises on it loses every other strategy's result too.
    """
    number = _finite_float(value)
    if number is None:
        return "n/a"
    return f"{number:+.4f}{suffix}"


def apply_forward_evidence(row: dict, forward: dict) -> None:
    """Attach live/shadow results without promoting an exploratory tier.

    Forward agreement is useful evidence, but this tournament is still the
    recomputed-OHLCV path. T3/T4 promotion belongs to an authoritative,
    G2-valid recorded replay with a persisted decision, not to this report.

    A sign DISAGREEMENT is recorded loudly even when the counts are small.
    It is the earliest available warning that a backtest was fitted, and it
    arrives long before the sample is large enough to prove anything.

    A strategy can have fired signals and shadow summaries before any trade
    has enough future data to resolve. That is a pending state, and comparing
    signs against it would read an absent expectancy as a flat one - which is
    the same sign as the backtest half the time, and means nothing either way.
    """
    if not row.get("scored"):
        return
    entry = (forward.get("strategies") or {}).get(row["strategy_id"])
    if not isinstance(entry, dict) or not entry:
        row["forward"] = {"status": "no forward evidence recorded yet"}
        return

    try:
        observed = max(0, int(entry.get("resolved_trades") or 0))
    except (TypeError, ValueError):
        row["forward"] = {
            "status": ("forward evidence is malformed: resolved_trades is "
                       "not an integer")}
        return

    forward_pct = _finite_float(entry.get("forward_expectancy_pct"))
    backtest_pct = _finite_float((row.get("headline") or {}).get(
        "expectancy_pct"))
    needed = 0
    for gate in row.get("gates") or []:
        if gate.get("gate") == "is_detectable":
            needed = _finite_float(
                (gate.get("numbers") or {}).get("trades_needed")) or 0
            needed = max(0, int(needed))
            break

    row["forward"] = {
        "resolved_trades": observed,
        "trades_needed": needed,
        "forward_expectancy_pct": forward_pct,
        "backtest_expectancy_pct": backtest_pct,
        "signs_agree": None,
        "signals_fired": entry.get("signals_fired"),
        "positions_actually_opened": entry.get("positions_actually_opened"),
        "selectivity_pct": entry.get("selectivity_pct"),
        "progress_pct": (round(observed / needed * 100, 1)
                         if needed else None),
    }

    if not observed:
        row["forward"]["status"] = "no resolved forward trades yet"
        return
    if forward_pct is None:
        row["forward"]["status"] = (
            "resolved trades exist but their expectancy is missing; sign "
            "comparison deferred")
        return
    if backtest_pct is None:
        row["forward"]["status"] = (
            "backtest expectancy is unavailable; sign comparison deferred")
        return

    agrees = (forward_pct >= 0) == (backtest_pct >= 0)
    row["forward"]["signs_agree"] = agrees

    if agrees is False:
        row["forward"]["warning"] = (
            "forward and backtest disagree in sign; treat the backtest as "
            "unconfirmed regardless of how clean it looked")
    if agrees and needed and observed >= needed:
        row["forward"]["next_step"] = (
            "sample target reached; run the authoritative recorded-replay "
            "confirmation before changing a tier")


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
        if row.get("beyond_exploratory_authority"):
            return (f"NO CHANGE - already registered "
                    f"{row['registered_tier']} on authoritative evidence this "
                    f"battery cannot reproduce; nothing here revises it")
        return "HOLD - exploratory candidate; authoritative recorded replay is required"
    # Above the ceiling this battery cannot reach. Reachable only if the tier
    # rubric is ever widened, so it states the boundary rather than assuming
    # the tier came from evidence that can authorize anything.
    return (f"HOLD - {tier} is above this battery's "
            f"{row.get('exploratory_ceiling', gate_mod.EXPLORATORY_CEILING)} "
            f"ceiling; only the authoritative recorded-replay path may award "
            f"it")


def alert_on_tier_changes(rows: list[dict], payload: dict) -> None:
    """Notify when a strategy crosses a tier, or the harness breaks.

    Crossing a gate is the only event in this pipeline that should
    interrupt someone. Nobody should have to read a nightly report to find
    out that a strategy was rejected or became eligible - and a broken
    benchmark is more urgent than either, because it invalidates every other
    number in the run.

    Reuses the agent's retried-webhook path, including its local
    failed-delivery queue, so an alert is not lost to a transient outage.
    """
    changes = [r for r in rows
               if r.get("scored") and r.get("tier_changed")]
    check = payload.get("benchmark_check") or {}
    if not changes and check.get("ok", True):
        return
    try:
        import yaml as _yaml

        from agent.alerts import AlertManager
        from agent.config import validate_config
        cfg = validate_config(
            _yaml.safe_load((REPO / "config.yaml").read_text()))
        alerts = AlertManager(cfg)
    except Exception as exc:                               # noqa: BLE001
        print(f"note: tier-change alerting unavailable ({exc})",
              file=sys.stderr)
        return

    if not check.get("ok", True):
        alerts.send(
            "critical", "research_harness_broken",
            "Tournament benchmark did not reproduce; this run's results "
            "are unverified",
            {"reason": check.get("reason"),
             "measured_expectancy_r": check.get("measured_expectancy_r"),
             "expected_expectancy_r": check.get("expected_expectancy_r")})

    for row in changes:
        promoted = (TIERS.index(row["measured_tier"])
                    > TIERS.index(row["registered_tier"]))
        alerts.send(
            "warning",
            "strategy_tier_promoted" if promoted else "strategy_tier_demoted",
            f"{row['strategy_id']}/{row['version']}: "
            f"{row['registered_tier']} -> {row['measured_tier']}",
            {"reason": row["tier_reason"],
             "expectancy_pct": row["headline"].get("expectancy_pct"),
             "trades": row["headline"].get("trades"),
             "note": ("a promotion needs more evidence than a demotion; "
                      "check the run window before acting"
                      if promoted else
                      "evidence of failure - update agent/registry.py")})


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
            f"{_format_signed(check.get('measured_expectancy_r'))} R against "
            f"an expected "
            f"{_format_signed(check.get('expected_expectancy_r'))} R "
            f"(drift {_format_signed(check.get('drift_r'))}, tolerance "
            f"{_format_signed(check.get('tolerance_r'))}).")
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
            f"| {_format_signed(head.get('expectancy_pct'))} "
            f"| {_format_signed(head.get('expectancy_r'))} "
            f"| {row.get('hypotheses_tested', '?')} "
            f"| {recommendation(row)} |")
    lines.append("")

    for row in rows:
        if not row.get("scored"):
            continue
        lines += [f"## {row['strategy_id']}/{row['version']}", "",
                  f"**Measured tier: {row['measured_tier']}** - "
                  f"{row['tier_reason']}", ""]
        if row.get("beyond_exploratory_authority"):
            lines += [
                f"> Registered {row['registered_tier']} is above this "
                f"battery's {row['exploratory_ceiling']} ceiling. The "
                f"measured tier below is what exploratory data can show on "
                f"its own; it is not a demotion and it does not revise the "
                f"registered tier. Only the authoritative recorded-replay "
                f"path can change that.", ""]
        elif row["tier_changed"]:
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
                  "**Falsified by.** " + row["falsification"].strip(), ""]

        settings = row.get("settings") or []
        if len(settings) > 1:
            lines += [
                f"**Pre-registered parameterisations ({len(settings)}).** The "
                "tier above is a verdict on the hypothesis, not on one "
                "threshold: rejection requires every setting to fail, and a "
                "tier above T1 requires the best setting to survive a Holm "
                "correction across them.", "",
                "| Setting | Parameters | Trades | Expectancy % | Tier | "
                "p_adj | Claim |",
                "| --- | --- | ---: | ---: | --- | ---: | --- |"]
            for setting in settings:
                params = (", ".join(f"`{k}={v}`" for k, v in
                                    sorted(setting["params"].items()))
                          or "*registered*")
                mark = ("**" if setting["setting_id"] == row.get("best_setting")
                        else "")
                p_adj = setting.get("p_adjusted")
                lines.append(
                    f"| {mark}{setting['setting_id']}{mark} | {params} "
                    f"| {setting.get('trades', 0)} "
                    f"| {_format_signed(setting.get('expectancy_pct'), '%')} "
                    f"| {setting['tier']} "
                    f"| {f'{float(p_adj):.3f}' if p_adj is not None else '-'} "
                    f"| {setting['claim']} |")
            lines += ["",
                      f"Best setting: `{row.get('best_setting')}`. Gates below "
                      "are the registered parameterisation's.", ""]

        lines += ["| Gate | Result | Detail |", "| --- | --- | --- |"]
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
                "- forward expectancy: "
                f"{_format_signed(fwd.get('forward_expectancy_pct'), '%')} "
                "vs backtest "
                f"{_format_signed(fwd.get('backtest_expectancy_pct'), '%')}",
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
        "A tier is a claim about evidence, not about promise. This OHLCV "
        "tournament is capped at `T2_CANDIDATE`; `T3_VALIDATED` and "
        "`T4_CONFIRMED` require the authoritative recorded-replay path and "
        "persisted confirmation evidence.", "",
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

    alert_on_tier_changes(ordered, payload)

    check = payload["benchmark_check"]
    print(f"\nharness check: {'PASS' if check['ok'] else 'FAIL'} - "
          f"{check['reason']}")
    for row in ordered:
        if row.get("scored"):
            head = row["headline"]
            print(f"  {row['strategy_id']:<16} {row['measured_tier']:<15} "
                  f"{head.get('trades', 0):>6} trades  "
                  f"{_format_signed(head.get('expectancy_pct'), '%')}  "
                  f"{recommendation(row)}")
        else:
            print(f"  {row['strategy_id']:<16} {'NOT SCORED':<15} "
                  f"{row['reason'][:60]}")
    print(f"\nwrote {args.out / 'REPORT.md'}")
    return 0 if check["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
