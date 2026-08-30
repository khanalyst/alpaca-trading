"""Replay a frozen cohort under the configured and the measured cost model.

The diagnostic factory reported a near-constant 0.16-0.18R execution drag
across every family it could execute.  That constancy is the finding: a fixed
round trip divided by a stop the risk gate pins near a fixed width is the same
number whatever the strategy does.  This module answers the obvious follow-up
question directly — what do those same variants look like when the cost model
is fitted to the recorded quotes instead of assumed?

It is a measurement tool, not a self-authorizing one.  It replays frozen specs,
never tunes or promotes, and writes no ledger state.  An operator may apply a
validated artifact explicitly through runtime configuration; both arms use the
identical corpus, policy, and sizing so the only thing that differs is the cost
schedule.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from agent.contracts.rule import rule_variant_id, validate_rule_spec
from .cost_counterfactual import _cost_decomposition, load_frozen_specs
from .costs import CostModel, ReplayPolicy, diagnostic_backfill_policy
from .edge_discovery_core import _read_discovery_rows
from .factory_core import (DEFAULT_VARIANTS, FAMILY_TEMPLATES,
                           coordinate_mutation_pool, diagnose, spec_delta,
                           simulate_account, template_hypothesis)
from .quote_costs import (measure_quote_costs, cost_model_from_schedule,
                          schedule_costs_block, bucket_label, QuoteCostError)
from .stressed_cost_calibration import activation_overlay, calibrate_stressed_cost

RERUN_SCHEMA = "cost-rerun.v1"
# The diagnosis handed to the deterministic mutation pool when no frozen cohort
# is supplied.  It only selects which coordinate axes are tried first, so the
# generated variants stay comparable to the ones the factory reported.
_DEFAULT_DIAGNOSIS = {"primary_failure": "negative_expectancy"}


def _measure(section: Any) -> float | None:
    if isinstance(section, Mapping):
        value = section.get("value")
        return float(value) if isinstance(value, (int, float)) else None
    return None


@dataclass(frozen=True)
class ArmResult:
    trades: int
    net_pnl: float
    expectancy: float
    win_rate: float
    profit_factor: float
    max_drawdown: float
    reference_r: float | None
    drag_r: float | None
    net_r: float | None
    time_expiry_rate: float
    target_rate: float
    stop_rate: float
    # The stressed-cost veto is an admission gate, not an expected cost, so it
    # is unmoved by a better cost fit.  Counting it here is what keeps a
    # zero-trade row from reading as "the strategy found nothing".
    stressed_cost_rejections: int
    signal_opportunities: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "trades": self.trades, "net_pnl": self.net_pnl,
            "expectancy": self.expectancy, "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "max_drawdown": self.max_drawdown,
            "reference_r": self.reference_r, "drag_r": self.drag_r,
            "net_r": self.net_r, "time_expiry_rate": self.time_expiry_rate,
            "target_rate": self.target_rate, "stop_rate": self.stop_rate,
            "stressed_cost_rejections": self.stressed_cost_rejections,
            "signal_opportunities": self.signal_opportunities,
        }


def _arm(rows: Sequence[Mapping[str, Any]], *, starting_cash: float) -> ArmResult:
    """Summarize one replay, including the R decomposition the report shows."""
    summary = diagnose(rows, starting_cash=starting_cash)
    executed = [row for row in rows if row.get("no_trade") is not True]
    net_values: list[float] = []
    drag_values: list[float] = []
    reference_values: list[float] = []
    for row in executed:
        risk = row.get("risk_usd") or row.get("nominal_risk_usd")
        try:
            risk = float(risk)
        except (TypeError, ValueError):
            continue
        if risk <= 0:
            continue
        decomposed = _cost_decomposition(row)
        drag = _measure(decomposed["execution_drag"]["r"])
        fee = _measure(decomposed["fee_cost"]["r"])
        net = float(row.get("net_pnl", 0.0)) / risk
        net_values.append(net)
        if drag is not None and fee is not None:
            # Reference is reconstructed from the same components the report
            # decomposes, so reference - drag - fee ties out to net exactly.
            drag_values.append(drag + fee)
            reference_values.append(net + drag + fee)

    def average(values: Sequence[float]) -> float | None:
        return sum(values) / len(values) if values else None

    hold = summary.get("hold_telemetry") or {}
    return ArmResult(
        trades=summary["trades"], net_pnl=summary["net_pnl"],
        expectancy=summary["expectancy"], win_rate=summary["win_rate"],
        profit_factor=summary["profit_factor"],
        max_drawdown=summary["max_drawdown"],
        reference_r=average(reference_values), drag_r=average(drag_values),
        net_r=average(net_values),
        time_expiry_rate=float(hold.get("time_expiry_rate") or 0.0),
        target_rate=summary["target_rate"], stop_rate=summary["stop_rate"],
        stressed_cost_rejections=sum(
            str(row.get("reject_reason") or "") == "stressed_cost_risk_limit"
            for row in rows),
        signal_opportunities=sum(
            bool(row.get("signal_opportunity")) for row in rows))


def _row_bucket(row: Mapping[str, Any]) -> str | None:
    """Resolve the measured schedule bucket from a causal entry timestamp."""
    raw = row.get("entry_timestamp")
    if raw in (None, ""):
        return None
    try:
        text = str(raw).replace("Z", "+00:00")
        stamp = datetime.fromisoformat(text)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        local = stamp.astimezone(ZoneInfo("America/New_York"))
        return bucket_label((local.hour * 60 + local.minute +
                             local.second / 60.0) - (9 * 60 + 30))
    except (TypeError, ValueError, OverflowError):
        return None


def _reprice_with_schedule(rows: Sequence[Mapping[str, Any]], schedule: Mapping[str, Any],
                           *, percentile: str, vehicle: str) -> list[dict[str, Any]]:
    """Apply symbol/bucket/depth measured costs to each executed opportunity.

    ``simulate_account`` takes one model for portfolio admission.  The
    measured rerun therefore keeps that deterministic trade population, then
    reprices each realized opportunity with its own symbol/time/depth cell.
    This avoids silently collapsing the measured schedule back to a universe
    no-slippage constant while preserving the frozen-cohort comparison.
    """
    repriced: list[dict[str, Any]] = []
    for original in rows:
        row = dict(original)
        if row.get("no_trade") is True:
            repriced.append(row)
            continue
        try:
            quantity = float(row.get("quantity", row.get("contracts", 0.0)))
            multiplier = float(row.get(
                "contract_multiplier", row.get("multiplier", 100.0
                                                  if vehicle == "option" else 1.0)))
            entry_reference = float(row["entry_reference"])
            exit_reference = float(row["exit_reference"])
            direction = "long" if vehicle == "option" else str(row["direction"])
            bucket = _row_bucket(row)
            model = cost_model_from_schedule(
                schedule, symbol=str(row.get("symbol") or ""), bucket=bucket,
                percentile=percentile, order_shares=quantity)
            entry = model.execution_price(
                entry_reference, direction, entry=True,
                executable_quote=(row.get("entry_fill_source") == "quote"))
            exit_price = model.execution_price(
                exit_reference, direction, entry=False,
                executable_quote=(row.get("exit_fill_source") == "quote"))
            gross = ((exit_price - entry) if direction == "long" else
                     (entry - exit_price)) * quantity * multiplier
            fees = model.fees(entry, exit_price, quantity, multiplier,
                              vehicle=vehicle)
            row.update({"entry_price": entry, "exit_price": exit_price,
                        "gross_pnl": gross, "costs": fees,
                        "net_pnl": gross - fees,
                        "measured_cost_symbol": str(row.get("symbol") or "").upper(),
                        "measured_cost_bucket": bucket,
                        "measured_cost_order_shares": quantity,
                        "measured_cost_provenance": model.provenance})
        except (KeyError, TypeError, ValueError, OverflowError):
            # A malformed frozen row remains visible to the arm summary rather
            # than being silently dropped from the cohort.
            row["measured_cost_error"] = "unpriceable_frozen_row"
        repriced.append(row)
    return repriced


def deterministic_cohort(variants_per_strategy: int = DEFAULT_VARIANTS
                         ) -> list[dict[str, Any]]:
    """The catalog's roots plus their leading coordinate variants.

    Used when no frozen cohort is supplied.  This reproduces the *shape* of a
    factory cycle without an LLM: the exact tuned variants of a past run can
    only come from that run's own report, which ``--specs`` accepts.
    """
    cohort: list[dict[str, Any]] = []
    for slot in range(len(FAMILY_TEMPLATES)):
        root = template_hypothesis(slot).rule_spec
        pool = coordinate_mutation_pool(root, _DEFAULT_DIAGNOSIS)
        cohort.extend(spec for spec, _reason in pool[:int(variants_per_strategy)])
    return cohort


def run_cost_rerun(
        corpus: str | Path | Sequence[Mapping[str, Any]], *,
        runtime_config: Mapping[str, Any],
        specs: Sequence[Mapping[str, Any]] | None = None,
        percentile: str = "p75", vehicle: str = "equity",
        starting_cash: float = 100_000.0,
        min_quotes_per_cell: int = 500) -> dict[str, Any]:
    """Fit a cost schedule from the corpus, then replay every spec twice."""
    policy_source, bars, snapshots, quotes, schedule, validation_schedule, \
        validation_schedule_reason, stress_calibration, fit_sessions, \
        validation_sessions, fit_quotes, validation_quotes = _prepare_cost_calibration(
            corpus, runtime_config=runtime_config,
            min_quotes_per_cell=min_quotes_per_cell)
    policy = diagnostic_backfill_policy(policy_source)
    configured = CostModel.from_config(runtime_config, vehicle=vehicle)
    measured = cost_model_from_schedule(schedule, percentile=percentile)

    risk = runtime_config.get("risk") or {}
    scenario = risk.get("stressed_cost_scenario_bps")
    ratio = risk.get("max_stressed_cost_to_risk_ratio")

    cohort = [validate_rule_spec(spec) for spec in
              (specs if specs is not None else deterministic_cohort())]
    roots = {str(template_hypothesis(slot).rule_spec["family"]):
             template_hypothesis(slot).rule_spec
             for slot in range(len(FAMILY_TEMPLATES))}
    results: list[dict[str, Any]] = []
    for spec in cohort:
        variant_id = rule_variant_id(spec)
        delta = spec_delta(roots.get(str(spec["family"]), spec), spec)
        row: dict[str, Any] = {
            "family": spec["family"], "variant_id": variant_id,
            # A truncated content hash cannot be read; name the variant by what
            # it actually changed relative to its family root.
            "label": ("root" if not delta else
                      ",".join(f"{key}={value['to']}"
                               for key, value in sorted(delta.items()))),
            "stop_atr": spec.get("stop_atr"), "rule_spec": spec}
        for name, model in (("configured", configured), ("measured", measured)):
            account = simulate_account(
                bars, snapshots, spec, vehicle=vehicle,
                account_id=f"{name}:{variant_id}", starting_cash=starting_cash,
                risk_pct=policy.risk_per_trade_pct, costs=model,
                quotes=quotes, policy=policy)
            measured_rows = account["rows"]
            if name == "measured":
                measured_rows = _reprice_with_schedule(
                    measured_rows, schedule, percentile=percentile,
                    vehicle=vehicle)
            row[name] = _arm(measured_rows, starting_cash=starting_cash).as_dict()
        results.append(row)

    try:
        implied_min_stop_bps = float(scenario) / float(ratio)
    except (TypeError, ValueError, ZeroDivisionError):
        implied_min_stop_bps = None
    return {
        "schema": RERUN_SCHEMA,
        "diagnostic_only": True, "authorizing": False,
        # The admission gate is a separate control from the expected-cost
        # model and a better cost fit does not move it.  Report the stop width
        # it implies so a zero-trade cohort is attributable.
        "stressed_cost_gate": {
            "scenario_bps": scenario, "max_cost_to_risk_ratio": ratio,
            "implied_min_stop_bps": implied_min_stop_bps,
            "note": ("admission gate, not an expected cost; unchanged by the "
                     "measured model"),
        },
        "cost_models": {
            "configured": configured.as_dict(),
            "measured": measured.as_dict(),
            "configured_round_trip_bps": 2 * configured.entry_cost_bps +
                                          2 * configured.fee_bps,
            "measured_round_trip_bps": 2 * measured.entry_cost_bps +
                                        2 * measured.fee_bps,
        },
        "cost_schedule": schedule,
        "quote_schedule_evaluation": {
            "method": "chronological_quote_fit_validation_split",
            "fit_fraction": .70,
            "fit_sessions": sorted(fit_sessions),
            "validation_sessions": sorted(validation_sessions),
            "fit_quotes": len(fit_quotes),
            "validation_quotes": len(validation_quotes),
            "fit_schedule_hash": schedule.get("schedule_hash"),
            "validation_schedule": validation_schedule,
            "validation_unavailable_reason": validation_schedule_reason,
            "authorizing": False,
        },
        "stress_calibration": stress_calibration,
        "stress_calibration_activation": activation_overlay(
            stress_calibration, expected_provider=policy_source.equity_provider,
            expected_feed=policy_source.equity_feed),
        "costs_block": schedule_costs_block(schedule, percentile=percentile),
        "bars": len(bars), "quotes": len(quotes),
        "variants": len(results), "results": results,
    }


def _prepare_cost_calibration(
        corpus: str | Path | Sequence[Mapping[str, Any]], *,
        runtime_config: Mapping[str, Any], min_quotes_per_cell: int):
    """Measure fit/held-out quote schedules without replaying a cohort."""
    policy_source = ReplayPolicy.from_config(runtime_config)
    _raw, bars, snapshot_map, quote_rows = _read_discovery_rows(
        corpus, require_provenance=True,
        expected_equity_feed=policy_source.equity_feed)
    quotes = (quote_rows if callable(getattr(quote_rows, "quote_fill", None))
              else list(quote_rows))
    quote_rows_list = quotes if isinstance(quotes, list) else list(quotes)
    quote_sessions = sorted({
        str(getattr(item, "session_date", "") or
            (item.get("session_date") if isinstance(item, Mapping) else ""))
        for item in quote_rows_list
        if (getattr(item, "session_date", None) is not None or
            isinstance(item, Mapping))})
    split_at = max(1, min(len(quote_sessions) - 1,
                          int(len(quote_sessions) * .70))) \
        if len(quote_sessions) >= 2 else len(quote_sessions)
    fit_sessions = set(quote_sessions[:split_at])
    validation_sessions = set(quote_sessions[split_at:])
    quote_session = lambda item: str(
        getattr(item, "session_date", "") or
        (item.get("session_date", "") if isinstance(item, Mapping) else ""))
    fit_quotes = [item for item in quote_rows_list
                  if quote_session(item) in fit_sessions]
    validation_quotes = [item for item in quote_rows_list
                         if quote_session(item) in validation_sessions]
    schedule = measure_quote_costs(
        fit_quotes or quote_rows_list,
        min_quotes_per_cell=int(min_quotes_per_cell))
    validation_schedule = None
    validation_schedule_reason = None
    if validation_quotes:
        try:
            validation_schedule = measure_quote_costs(
                validation_quotes, min_quotes_per_cell=int(min_quotes_per_cell))
        except QuoteCostError as exc:
            validation_schedule_reason = str(exc)
    risk = runtime_config.get("risk") or {}
    try:
        ratio = float(risk.get("max_stressed_cost_to_risk_ratio", .30))
        if not math.isfinite(ratio) or ratio <= 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        ratio = .30
    stress_calibration = calibrate_stressed_cost(
        schedule, validation_schedule=validation_schedule, percentile="p95",
        min_quotes_per_cell=int(min_quotes_per_cell),
        expected_provider=policy_source.equity_provider,
        expected_feed=policy_source.equity_feed,
        validation_failure_reason=validation_schedule_reason,
        max_cost_to_risk_ratio=ratio)
    return (policy_source, bars, list(snapshot_map.values()), quotes, schedule,
            validation_schedule, validation_schedule_reason, stress_calibration,
            fit_sessions, validation_sessions, fit_quotes, validation_quotes)


def run_cost_calibration(
        corpus: str | Path | Sequence[Mapping[str, Any]], *,
        runtime_config: Mapping[str, Any],
        min_quotes_per_cell: int = 500) -> dict[str, Any]:
    """Produce a diagnostic artifact without replaying; enable it separately."""
    policy, bars, snapshots, quotes, schedule, validation_schedule, reason, calibration, \
        fit_sessions, validation_sessions, fit_quotes, validation_quotes = _prepare_cost_calibration(
            corpus, runtime_config=runtime_config,
            min_quotes_per_cell=min_quotes_per_cell)
    return {
        "schema": "stressed-cost-calibration-run.v1",
        "diagnostic_only": True, "authorizing": False,
        "provider": policy.equity_provider, "feed": policy.equity_feed,
        "fit_sessions": sorted(fit_sessions),
        "validation_sessions": sorted(validation_sessions),
        "fit_quotes": len(fit_quotes), "validation_quotes": len(validation_quotes),
        "cost_schedule": schedule,
        "validation_schedule": validation_schedule,
        "validation_unavailable_reason": reason,
        "stress_calibration": calibration,
        "activation": activation_overlay(
            calibration, expected_provider=policy.equity_provider,
            expected_feed=policy.equity_feed),
        "bars": len(bars), "quotes": len(quotes),
    }


def _fmt(value: Any, places: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float) and value == float("inf"):
        return "inf"
    return f"{value:.{places}f}"


def render_text(report: Mapping[str, Any]) -> str:
    models = report["cost_models"]
    gate = report.get("stressed_cost_gate") or {}
    calibration = report.get("stress_calibration") or {}
    activation = report.get("stress_calibration_activation") or {}
    lines = [
        "Cost re-run — configured vs measured execution cost",
        f"  corpus: {report['bars']} bars, {report['quotes']} quotes, "
        f"{report['variants']} variants",
        f"  configured: spread {_fmt(models['configured']['spread_bps'])} bps, "
        f"slippage {_fmt(models['configured']['slippage_bps'])} bps "
        f"-> round trip {_fmt(models['configured_round_trip_bps'])} bps",
        f"  measured:   spread {_fmt(models['measured']['spread_bps'])} bps, "
        f"slippage {_fmt(models['measured']['slippage_bps'])} bps "
        f"-> round trip {_fmt(models['measured_round_trip_bps'])} bps",
        f"  provenance: {models['measured']['provenance']}",
        "  quote schedule: fit on the earlier 70% of sessions; later sessions "
        "are validation-only",
        "",
        f"  stressed-cost gate: {_fmt(gate.get('scenario_bps'))} bps at ratio "
        f"{_fmt(gate.get('max_cost_to_risk_ratio'))} implies a minimum stop of "
        f"{_fmt(gate.get('implied_min_stop_bps'), 1)} bps.",
        "  That gate is an admission control, not an expected cost: "
        "the measured model does",
        "  not move it, and any variant whose stop is tighter is refused in "
        "both arms.",
        "",
        f"  empirical stress (fit/validation diagnostic): "
        f"{_fmt(calibration.get('aggregate_conservative_scenario_bps'))} bps "
        f"-> feasible minimum stop "
        f"{_fmt(calibration.get('aggregate_feasible_minimum_stop_bps'), 1)} bps",
        f"  activation-ready: {bool(activation.get('ready'))}"
        + (f" ({', '.join(activation.get('reasons') or ())})"
           if activation.get('reasons') else ""),
    ]
    for cell in calibration.get("cells") or ():
        symbol = cell.get("symbol") or "*"
        bucket = cell.get("bucket") or "unbucketed"
        lines.append(
            f"    {symbol}/{bucket}: empirical "
            f"{_fmt(cell.get('selected_scenario_bps'))} bps -> minimum stop "
            f"{_fmt(cell.get('feasible_minimum_stop_bps'), 1)} bps"
            + (f" ({cell.get('fallback_reason')})"
               if cell.get("fallback_reason") else ""))
    lines += [
        "",
        "  This is diagnostic only. It cannot promote or authorize anything, "
        "and a variant",
        "  that turns positive here is a hypothesis for a held-out test, "
        "not a result.",
        "",
    ]
    header = (f"  {'family':<23}{'variant':<22}{'trades':>7}{'gated':>7}"
              f"{'net $':>11}{'exp $':>9}{'win':>6}{'PF':>6}"
              f"{'refR':>8}{'dragR':>8}{'netR':>8}")
    for arm in ("configured", "measured"):
        lines += [f"  === {arm} costs ===", header]
        for item in report["results"]:
            row = item[arm]
            lines.append(
                f"  {item['family']:<23}{item['label'][:21]:<22}"
                f"{row['trades']:>7}{row['stressed_cost_rejections']:>7}"
                f"{row['net_pnl']:>11.2f}"
                f"{row['expectancy']:>9.2f}{row['win_rate']:>6.0%}"
                f"{_fmt(row['profit_factor'], 2):>6}"
                f"{_fmt(row['reference_r'], 3):>8}"
                f"{_fmt(row['drag_r'], 3):>8}{_fmt(row['net_r'], 3):>8}")
        lines.append("")

    flipped = [item for item in report["results"]
               if (item["configured"]["net_r"] is not None and
                   item["measured"]["net_r"] is not None and
                   item["configured"]["net_r"] <= 0 < item["measured"]["net_r"])]
    gated = sum(1 for item in report["results"]
                if item["measured"]["trades"] == 0 and
                item["measured"]["stressed_cost_rejections"] > 0)
    if gated:
        lines.append(f"  variants with no trades because every opportunity was "
                     f"refused by the stressed-cost gate: {gated}")
    lines.append(f"  variants crossing zero on the measured model: {len(flipped)}")
    for item in flipped:
        lines.append(f"    {item['family']} {item['variant_id'][:12]} "
                     f"{_fmt(item['configured']['net_r'], 3)} -> "
                     f"{_fmt(item['measured']['net_r'], 3)} R "
                     f"({item['measured']['trades']} trades)")
    if flipped:
        lines += [
            "",
            "  A sign change here is necessary, not sufficient: these are "
            "in-sample fit",
            "  numbers on one corpus, with no held-out test, no multiple-test "
            "correction,",
            "  and trade counts below the 100-trade evidence floor.",
        ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cost-rerun",
        description="Replay frozen variants under configured and measured costs")
    parser.add_argument("--corpus", required=True, type=Path,
                        help="JSONL corpus file or directory of partitions")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"),
                        help="runtime config supplying the configured costs")
    parser.add_argument("--specs", type=Path, default=None,
                        help="factory report JSON to take the frozen cohort "
                             "from; omit to use the catalog roots and their "
                             "leading coordinate variants")
    parser.add_argument("--percentile", default="p75",
                        help="measured spread percentile (default p75)")
    parser.add_argument("--min-quotes-per-cell", type=int, default=500)
    parser.add_argument("--starting-cash", type=float, default=100_000.0)
    parser.add_argument("--out", type=Path, default=None,
                        help="write the full JSON report here")
    parser.add_argument("--schedule-out", type=Path, default=None,
                        help="write the fitted cost schedule here")
    parser.add_argument("--calibration-only", action="store_true",
                        help="measure held-out stress without replaying; --out writes the runtime artifact")
    args = parser.parse_args(argv)

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    specs = load_frozen_specs(args.specs) if args.specs is not None else None
    if args.calibration_only:
        report = run_cost_calibration(
            args.corpus, runtime_config=config,
            min_quotes_per_cell=args.min_quotes_per_cell)
    else:
        report = run_cost_rerun(
            args.corpus, runtime_config=config, specs=specs,
            percentile=args.percentile, starting_cash=args.starting_cash,
            min_quotes_per_cell=args.min_quotes_per_cell)
    if args.calibration_only:
        activation = report.get("activation") or {}
        print(json.dumps({
            "schema": report.get("schema"),
            "diagnostic_only": True, "authorizing": False,
            "activation_ready": bool(activation.get("ready")),
            "activation_reasons": activation.get("reasons") or [],
            "artifact_content_hash": (
                (report.get("stress_calibration") or {}).get("content_hash")),
        }, sort_keys=True))
    else:
        print(render_text(report))
    if args.out is not None:
        output_payload = (report.get("stress_calibration")
                          if args.calibration_only else report)
        args.out.write_text(json.dumps(output_payload, indent=2, default=str),
                            encoding="utf-8")
        print(f"\n  wrote {args.out}")
    if args.schedule_out is not None:
        args.schedule_out.write_text(
            json.dumps(report["cost_schedule"], indent=2, default=str),
            encoding="utf-8")
        print(f"  wrote {args.schedule_out}")
    return 0


__all__ = ["ArmResult", "RERUN_SCHEMA", "deterministic_cohort", "main",
           "render_text", "run_cost_calibration", "run_cost_rerun"]


if __name__ == "__main__":
    sys.exit(main())
