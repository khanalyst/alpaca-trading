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
identical corpus, specs, policy, and sizing logic, with only the cost schedule
changed as an input.  Realized quantities and equity may diverge causally as
each cost treatment changes the account path.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from agent.contracts.rule import rule_variant_id, validate_rule_spec
from .cost_counterfactual import (_code_bundle_files, _code_bundle_hash,
                                  _cost_decomposition, load_frozen_specs)
from .costs import CostModel, ReplayPolicy, diagnostic_backfill_policy
from .edge_ledger import content_hash
from .edge_discovery_core import _read_discovery_rows
from .factory_core import (DEFAULT_VARIANTS, FAMILY_TEMPLATES,
                           coordinate_mutation_pool, diagnose, spec_delta,
                           simulate_account, template_hypothesis)
from .quote_costs import (measure_quote_costs, cost_model_from_schedule,
                          measured_cost_resolver, schedule_costs_block,
                          bucket_label, QuoteCostError)
from .stressed_cost_calibration import activation_overlay, calibrate_stressed_cost

RERUN_SCHEMA = "cost-rerun.v1"
EVIDENCE_SCHEMA = "cost-rerun-evidence.v1"
# The diagnosis handed to the deterministic mutation pool when no frozen cohort
# is supplied.  It only selects which coordinate axes are tried first, so the
# generated variants stay comparable to the ones the factory reported.
_DEFAULT_DIAGNOSIS = {"primary_failure": "negative_expectancy"}


def _measure(section: Any) -> float | None:
    if isinstance(section, Mapping):
        value = section.get("value")
        return float(value) if isinstance(value, (int, float)) else None
    return None


def _finite(value: Any) -> float | None:
    """Return finite numeric telemetry without allowing JSON impostors."""
    if isinstance(value, (bool, str, bytes, bytearray)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _session_hash(sessions: Sequence[Any]) -> str:
    """Hash canonical session IDs, retaining split identity in the report."""
    return content_hash(sorted({str(item) for item in sessions if str(item)}))


def _bucket_for_row(row: Mapping[str, Any]) -> str:
    return _row_bucket(row) or "unknown"


def _row_reference_r(row: Mapping[str, Any], risk: float) -> float | None:
    """Reconstruct the no-cost reference outcome from frozen boundary fields."""
    if risk <= 0:
        return None
    quantity = _finite(row.get("quantity", row.get("contracts")))
    multiplier = _finite(row.get("contract_multiplier", row.get("multiplier", 1.0)))
    entry = _finite(row.get("entry_reference"))
    exit_price = _finite(row.get("exit_reference"))
    if quantity is None or multiplier is None or entry is None or exit_price is None:
        return None
    direction = "long" if str(row.get("vehicle") or "equity") == "option" else str(
        row.get("direction") or "long")
    gross = ((exit_price - entry) if direction == "long" else
             (entry - exit_price)) * quantity * multiplier
    return gross / risk


def _breakdown(rows: Sequence[Mapping[str, Any]], *, family: str | None = None
               ) -> list[dict[str, Any]]:
    """Summarize immutable opportunity rows by family/symbol/time bucket.

    This projection deliberately includes refusals and no-signal rows.  A
    zero-execution cell therefore remains attributable to a missing signal,
    portfolio admission, or an unpriceable quote rather than looking empty.
    """
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        key = (str(family or row.get("family") or "unknown"),
               str(row.get("symbol") or "?").upper(), _bucket_for_row(row))
        grouped.setdefault(key, []).append(row)
    output: list[dict[str, Any]] = []
    for (group_family, symbol, bucket), items in sorted(grouped.items()):
        opportunities = [item for item in items
                         if bool(item.get("signal_opportunity"))]
        executed = [item for item in items if item.get("no_trade") is not True]
        refused = [item for item in opportunities if item.get("no_trade") is True]
        net_values: list[float] = []
        reference_values: list[float] = []
        drag_values: list[float] = []
        gross_pnl_values: list[float] = []
        net_pnl_values: list[float] = []
        for item in executed:
            risk = _finite(item.get("risk_usd") or item.get("nominal_risk_usd"))
            net = _finite(item.get("net_pnl"))
            if risk is None or risk <= 0 or net is None:
                continue
            net_r = net / risk
            reference_r = _row_reference_r(item, risk)
            if reference_r is None:
                # Existing decomposition carries the same boundary reference
                # for rows where the raw reference fields are unavailable.
                try:
                    parts = _cost_decomposition(item)
                    drag = _measure(parts.get("execution_drag", {}).get("r"))
                    fee = _measure(parts.get("fee_cost", {}).get("r"))
                except (KeyError, TypeError, ValueError, OverflowError):
                    drag = fee = None
                reference_r = (net_r + drag + fee
                               if drag is not None and fee is not None else None)
            if reference_r is not None:
                reference_values.append(reference_r)
                drag_values.append(reference_r - net_r)
            net_values.append(net_r)
            gross_pnl = _finite(item.get("gross_pnl"))
            if gross_pnl is not None:
                gross_pnl_values.append(gross_pnl)
            net_pnl_values.append(net)
        def mean(values: Sequence[float]) -> float | None:
            return sum(values) / len(values) if values else None
        def drawdown(values: Sequence[float]) -> float:
            peak = cumulative = dd = 0.0
            for value in values:
                cumulative += value
                peak = max(peak, cumulative)
                dd = max(dd, peak - cumulative)
            return dd
        wins = [value for value in net_values if value > 0]
        losses = [-value for value in net_values if value < 0]
        # Keep the report strict-JSON serializable; the wider research stack
        # uses 999 as the finite sentinel for a winning sample with no losses.
        pf = sum(wins) / sum(losses) if losses else (999.0 if wins else 0.0)
        exits = Counter(str(item.get("exit_reason_detail") or
                            item.get("exit_reason") or "unknown")
                        for item in executed)
        model_provenance = Counter(
            str(item.get("cost_model_provenance") or "unknown")
            for item in executed)
        entry_model_provenance = Counter(
            str(item.get("entry_cost_model_provenance") or
                item.get("cost_model_provenance") or "unknown")
            for item in executed)
        exit_model_provenance = Counter(
            str(item.get("exit_cost_model_provenance") or
                item.get("cost_model_provenance") or "unknown")
            for item in executed)
        sigma = None
        if len(net_values) > 1:
            average = sum(net_values) / len(net_values)
            sigma = math.sqrt(sum((value - average) ** 2 for value in net_values) /
                              (len(net_values) - 1))
        stderr = sigma / math.sqrt(len(net_values)) if sigma is not None else None
        output.append({
            "family": group_family, "symbol": symbol, "time_bucket": bucket,
            "opportunities": len(opportunities),
            "admissions": len(executed), "executions": len(executed),
            "refusals": len(refused),
            "trades": len(executed), "sample_count": len(net_values),
            "reference_r": mean(reference_values), "drag_r": mean(drag_values),
            "net_r": mean(net_values),
            "gross_pnl": sum(gross_pnl_values),
            "net_pnl": sum(net_pnl_values),
            "win_rate": (len(wins) / len(net_values) if net_values else 0.0),
            "profit_factor": pf, "drawdown_r": drawdown(net_values),
            "exit_reasons": dict(sorted(exits.items())),
            "cost_model_provenance_counts": dict(sorted(
                model_provenance.items())),
            "entry_cost_model_provenance_counts": dict(sorted(
                entry_model_provenance.items())),
            "exit_cost_model_provenance_counts": dict(sorted(
                exit_model_provenance.items())),
            "uncertainty": {
                "method": "normal_approximation",
                "sample_count": len(net_values), "sample_sigma_r": sigma,
                "standard_error_r": stderr,
                "ci95_r": (None if stderr is None else {
                    "lower": mean(net_values) - 1.96 * stderr,
                    "upper": mean(net_values) + 1.96 * stderr,
                }),
                "deterministic": True,
            },
            "reject_reasons": dict(sorted(Counter(
                str(item.get("reject_reason") or "unknown")
                for item in refused).items())),
        })
    return output


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
    breakdown: list[dict[str, Any]] = field(default_factory=list)
    sample_counts: dict[str, int] = field(default_factory=dict)
    exit_reasons: dict[str, int] = field(default_factory=dict)

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
            "breakdown": self.breakdown,
            "sample_counts": self.sample_counts,
            "exit_reasons": self.exit_reasons,
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
    breakdown = _breakdown(rows)
    exit_reasons = dict(sorted(Counter(
        str(row.get("exit_reason_detail") or row.get("exit_reason") or "unknown")
        for row in executed).items()))
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
            bool(row.get("signal_opportunity")) for row in rows),
        breakdown=breakdown,
        sample_counts={
            "rows": len(rows),
            "opportunities": sum(bool(row.get("signal_opportunity")) for row in rows),
            "admissions": len(executed),
            "executions": len(executed),
            "valid_r": len(net_values),
        },
        exit_reasons=exit_reasons)


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


def _evidence_manifest(*, provenance: Mapping[str, Any],
                       runtime_config: Mapping[str, Any],
                       specs: Sequence[Mapping[str, Any]],
                       schedule: Mapping[str, Any],
                       validation_schedule: Mapping[str, Any] | None,
                       fit_sessions: Sequence[Any],
                       validation_sessions: Sequence[Any],
                       percentile: str, vehicle: str) -> dict[str, Any]:
    """Build the immutable identity of one cost rerun invocation.

    The manifest is intentionally separate from the human-facing metrics: it
    binds the exact frozen corpus, validated specs, runtime policy, measured
    schedule, feed/provider identity, and chronological split.  A report with
    a missing or invalid split remains diagnostic and cannot be mistaken for
    held-out evidence.
    """
    spec_payload = [dict(spec) for spec in specs]
    code_files = _code_bundle_files()
    body = {
        "schema": EVIDENCE_SCHEMA,
        "diagnostic_only": True, "authorizing": False,
        "immutable": True,
        "corpus_hash": provenance.get("corpus_hash"),
        "corpus_rows": provenance.get("corpus_rows"),
        "config_hash": content_hash(runtime_config),
        "spec_hash": content_hash(spec_payload),
        "spec_count": len(spec_payload),
        "measurement_code_hash": _code_bundle_hash(code_files),
        "measurement_code_files": list(code_files),
        "schedule_hash": schedule.get("schedule_hash"),
        "validation_schedule_hash": (validation_schedule or {}).get("schedule_hash"),
        "provider": provenance.get("provider"),
        "feed": provenance.get("feed"),
        "runtime_provider": provenance.get("runtime_provider"),
        "runtime_feed": provenance.get("runtime_feed"),
        "fit_sessions": sorted(str(item) for item in fit_sessions),
        "validation_sessions": sorted(str(item) for item in validation_sessions),
        "fit_sessions_hash": provenance.get("fit_sessions_hash"),
        "validation_sessions_hash": provenance.get("validation_sessions_hash"),
        "split_hash": provenance.get("split_hash"),
        "split_valid": bool(provenance.get("split_valid")),
        "split_reason": provenance.get("split_reason"),
        "percentile": str(percentile), "vehicle": str(vehicle),
    }
    body["manifest_hash"] = content_hash(body)
    return body


def write_immutable_evidence(path: str | Path,
                             report: Mapping[str, Any]) -> dict[str, Any]:
    """Persist one content-addressed JSON report without overwriting it.

    Existing files are never replaced.  This makes reruns auditable: callers
    must choose a new path when any frozen input or result changes.
    """
    if not isinstance(report, Mapping):
        raise TypeError("report must be a mapping")
    payload = dict(report)
    body = dict(payload)
    supplied_hash = body.pop("content_hash", None)
    calculated_hash = content_hash(body)
    if supplied_hash not in (None, "") and str(supplied_hash) != calculated_hash:
        raise ValueError("immutable evidence content hash is invalid")
    payload["content_hash"] = calculated_hash
    encoded = json.dumps(payload, sort_keys=True, indent=2,
                         ensure_ascii=False, allow_nan=False, default=str)
    destination = Path(path)
    try:
        with destination.open("x", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.write("\n")
    except FileExistsError as exc:
        raise ValueError(f"immutable evidence already exists: {destination}") from exc
    return payload


def verify_cost_evidence(report: Mapping[str, Any] | None) -> tuple[bool, str | None]:
    """Verify a persisted diagnostic report without authorizing it."""
    if not isinstance(report, Mapping):
        return False, "report_missing"
    if report.get("schema") != RERUN_SCHEMA:
        return False, "report_schema_mismatch"
    if report.get("diagnostic_only") is not True or report.get("authorizing") is not False:
        return False, "report_authority_invalid"
    digest = report.get("content_hash")
    if not digest:
        return False, "report_content_hash_missing"
    body = dict(report)
    body.pop("content_hash", None)
    if str(digest) != content_hash(body):
        return False, "report_content_hash_invalid"
    manifest = report.get("evidence")
    if not isinstance(manifest, Mapping) or manifest.get("schema") != EVIDENCE_SCHEMA:
        return False, "evidence_manifest_missing"
    manifest_body = dict(manifest)
    manifest_digest = manifest_body.pop("manifest_hash", None)
    if not manifest_digest or str(manifest_digest) != content_hash(manifest_body):
        return False, "evidence_manifest_hash_invalid"
    if (manifest.get("immutable") is not True or
            manifest.get("diagnostic_only") is not True or
            manifest.get("authorizing") is not False):
        return False, "evidence_manifest_authority_invalid"
    code_files = manifest.get("measurement_code_files")
    expected_files = list(_code_bundle_files())
    if code_files != expected_files:
        return False, "measurement_code_files_mismatch"
    if str(manifest.get("measurement_code_hash") or "") != _code_bundle_hash(
            expected_files):
        return False, "measurement_code_hash_mismatch"
    return True, None


def run_cost_rerun(
        corpus: str | Path | Sequence[Mapping[str, Any]], *,
        runtime_config: Mapping[str, Any],
        specs: Sequence[Mapping[str, Any]] | None = None,
        percentile: str = "p75", vehicle: str = "equity",
        starting_cash: float = 100_000.0,
        min_quotes_per_cell: int = 500) -> dict[str, Any]:
    """Fit a cost schedule from the corpus, then replay every spec twice."""
    vehicle = str(vehicle).strip().lower()
    if vehicle != "equity":
        raise ValueError(
            "measured quote-cost rerun currently supports equity only")
    policy_source, bars, snapshots, quotes, schedule, validation_schedule, \
        validation_schedule_reason, stress_calibration, fit_sessions, \
        validation_sessions, fit_quotes, validation_quotes, provenance = _prepare_cost_calibration(
            corpus, runtime_config=runtime_config,
            min_quotes_per_cell=min_quotes_per_cell)
    policy = diagnostic_backfill_policy(policy_source)
    configured = CostModel.from_config(runtime_config, vehicle=vehicle)
    measured = cost_model_from_schedule(schedule, percentile=percentile)
    measured_resolver = measured_cost_resolver(
        schedule, percentile=percentile, vehicle=vehicle)

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
                cost_resolver=(measured_resolver if name == "measured" else None),
                quotes=quotes, policy=policy)
            measured_rows = [dict(item, family=spec["family"])
                             for item in account["rows"]]
            row[name] = _arm(measured_rows, starting_cash=starting_cash).as_dict()
        results.append(row)

    try:
        implied_min_stop_bps = float(scenario) / float(ratio)
    except (TypeError, ValueError, ZeroDivisionError):
        implied_min_stop_bps = None
    report = {
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
            "fit_quotes": fit_quotes,
            "validation_quotes": validation_quotes,
            "fit_schedule_hash": schedule.get("schedule_hash"),
            "validation_schedule": validation_schedule,
            "validation_unavailable_reason": validation_schedule_reason,
            "authorizing": False,
            "split_valid": bool(provenance.get("split_valid")),
            "split_hash": provenance.get("split_hash"),
        },
        "stress_calibration": stress_calibration,
        "stress_calibration_activation": activation_overlay(
            stress_calibration, expected_provider=policy_source.equity_provider,
            expected_feed=policy_source.equity_feed),
        "costs_block": schedule_costs_block(schedule, percentile=percentile),
        "bars": len(bars), "quotes": len(quotes),
        "variants": len(results), "results": results,
        "evidence": _evidence_manifest(
            provenance=provenance, runtime_config=runtime_config,
            specs=cohort, schedule=schedule,
            validation_schedule=validation_schedule,
            fit_sessions=fit_sessions, validation_sessions=validation_sessions,
            percentile=percentile, vehicle=vehicle),
    }
    report["content_hash"] = content_hash(report)
    return report


def _prepare_cost_calibration(
        corpus: str | Path | Sequence[Mapping[str, Any]], *,
        runtime_config: Mapping[str, Any], min_quotes_per_cell: int):
    """Measure fit/held-out quote schedules without replaying a cohort."""
    policy_source = ReplayPolicy.from_config(runtime_config)
    raw, bars, snapshot_map, quote_rows = _read_discovery_rows(
        corpus, require_provenance=True,
        expected_equity_feed=policy_source.equity_feed)
    quotes = (quote_rows if callable(getattr(quote_rows, "quote_fill", None))
              else list(quote_rows))
    def raw_quotes():
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            kind = str(item.get("kind", "quote")).strip().lower()
            if kind in {"quote", "quote_snapshot", "equity_quote",
                        "underlying_quote", ""}:
                yield item

    def quote_session(item: Mapping[str, Any]) -> str:
        stamp = item.get("timestamp", item.get("ts"))
        try:
            parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            return str(item.get("session_date") or "")

    quote_sessions = sorted({quote_session(item) for item in raw_quotes()
                             if quote_session(item)})
    split_at = max(1, min(len(quote_sessions) - 1,
                          int(len(quote_sessions) * .70))) \
        if len(quote_sessions) >= 2 else len(quote_sessions)
    fit_sessions = set(quote_sessions[:split_at])
    validation_sessions = set(quote_sessions[split_at:])
    fit_quote_count = sum(1 for item in raw_quotes()
                          if quote_session(item) in fit_sessions)
    validation_quote_count = sum(1 for item in raw_quotes()
                                 if quote_session(item) in validation_sessions)
    fit_source = (lambda: (item for item in raw_quotes()
                           if quote_session(item) in fit_sessions))
    validation_source = (lambda: (item for item in raw_quotes()
                                  if quote_session(item) in validation_sessions))
    schedule = measure_quote_costs(
        fit_source() if fit_quote_count else raw_quotes(),
        min_quotes_per_cell=int(min_quotes_per_cell))
    validation_schedule = None
    validation_schedule_reason = None
    if validation_quote_count:
        try:
            validation_schedule = measure_quote_costs(
                validation_source(), min_quotes_per_cell=int(min_quotes_per_cell))
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
    split_valid = bool(fit_sessions and validation_sessions and
                       not (fit_sessions & validation_sessions) and
                       max(fit_sessions) < min(validation_sessions))
    provenance = {
        "corpus_hash": content_hash(raw),
            "corpus_rows": len(raw),
        "provider": sorted(schedule.get("measured", {}).get("providers") or []),
        "feed": sorted(schedule.get("measured", {}).get("feeds") or []),
        "runtime_provider": policy_source.equity_provider,
        "runtime_feed": policy_source.equity_feed,
        "fit_sessions": sorted(fit_sessions),
        "validation_sessions": sorted(validation_sessions),
        "fit_sessions_hash": _session_hash(sorted(fit_sessions)),
        "validation_sessions_hash": _session_hash(sorted(validation_sessions)),
        "split_hash": content_hash({"fit": sorted(fit_sessions),
                                     "validation": sorted(validation_sessions)}),
        "split_valid": split_valid,
        "split_reason": (None if split_valid else
                          "disjoint_chronological_validation_sessions_required"),
    }
    return (policy_source, bars, list(snapshot_map.values()), quotes, schedule,
            validation_schedule, validation_schedule_reason, stress_calibration,
            fit_sessions, validation_sessions, fit_quote_count, validation_quote_count,
            provenance)


def run_cost_calibration(
        corpus: str | Path | Sequence[Mapping[str, Any]], *,
        runtime_config: Mapping[str, Any],
        min_quotes_per_cell: int = 500) -> dict[str, Any]:
    """Produce a diagnostic artifact without replaying; enable it separately."""
    policy, bars, snapshots, quotes, schedule, validation_schedule, reason, calibration, \
        fit_sessions, validation_sessions, fit_quotes, validation_quotes, provenance = _prepare_cost_calibration(
            corpus, runtime_config=runtime_config,
            min_quotes_per_cell=min_quotes_per_cell)
    report = {
        "schema": "stressed-cost-calibration-run.v1",
        "diagnostic_only": True, "authorizing": False,
        "provider": policy.equity_provider, "feed": policy.equity_feed,
        "fit_sessions": sorted(fit_sessions),
        "validation_sessions": sorted(validation_sessions),
        "fit_quotes": fit_quotes, "validation_quotes": validation_quotes,
        "cost_schedule": schedule,
        "validation_schedule": validation_schedule,
        "validation_unavailable_reason": reason,
        "stress_calibration": calibration,
        "activation": activation_overlay(
            calibration, expected_provider=policy.equity_provider,
            expected_feed=policy.equity_feed),
        "bars": len(bars), "quotes": len(quotes),
        "evidence": _evidence_manifest(
            provenance=provenance, runtime_config=runtime_config, specs=(),
            schedule=schedule, validation_schedule=validation_schedule,
            fit_sessions=fit_sessions, validation_sessions=validation_sessions,
            percentile="p95", vehicle="equity"),
    }
    report["content_hash"] = content_hash(report)
    return report


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
        write_immutable_evidence(args.out, output_payload)
        print(f"\n  wrote {args.out}")
    if args.schedule_out is not None:
        write_immutable_evidence(args.schedule_out, report["cost_schedule"])
        print(f"  wrote {args.schedule_out}")
    return 0


__all__ = ["ArmResult", "EVIDENCE_SCHEMA", "RERUN_SCHEMA",
           "deterministic_cohort", "main", "render_text",
           "run_cost_calibration", "run_cost_rerun",
           "verify_cost_evidence", "write_immutable_evidence"]


if __name__ == "__main__":
    sys.exit(main())
