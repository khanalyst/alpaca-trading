"""The findings store: append-only, so a rejection stays legible afterwards.

Intention #5 - every learning and recommendation persisted, per strategy and
per variant, reviewable later. ``report.py`` printed to stdout and forgot,
which meant the reasoning behind a decision survived only as long as the
terminal scrollback.

Two design commitments, both about what happens to negative results.

**Findings are append-only.** A rejection is a row, not a deletion. Six
months from now the question that matters is not "which variants are alive"
but "why was this rejected, and on what sample" - and if the answer was
deleted the same idea comes back, gets tested again, and consumes the same
calendar time a second time.

**Null results are recorded with the same weight as positive ones.** A
programme that only writes down what worked is a programme that only writes
down noise: at these sample sizes, filtering for positives is filtering for
the largest random numbers. ``INSUFFICIENT_SAMPLE`` is a finding, and often
the most useful one, because it says the question is still open rather than
answered in the negative.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
import uuid
from pathlib import Path


DEFAULT_STORE = Path(__file__).resolve().parent / "cache" / "findings.db"
SCHEMA_VERSION = 16

KINDS = ("observation", "recommendation", "decision")

T3_MANUAL_CHECK = "manual_registry_review"
T3_REQUIRED_CHECKS = frozenset({
    "current_g2_pass",
    "held_out_confirmation",
    "corpus_provenance",
    "config_provenance",
    "code_provenance",
    "forward_sample",
    "axis_family_corrected",
    "saved_edge_candidate",
    "paper_sample",
    "paper_positive",
    T3_MANUAL_CHECK,
})


def resolve_store_path(configured: str | Path | None = None) -> Path:
    """Resolve configured storage deterministically against the repository."""
    if configured is None or not str(configured).strip():
        return DEFAULT_STORE
    path = Path(configured)
    return path if path.is_absolute() else DEFAULT_STORE.parents[2] / path


def _json_safe(value: object):
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _positive_external_target_evidence(evidence: object) -> bool:
    """Accept external classification only with coherent st_dev evidence."""
    if not isinstance(evidence, dict):
        return False
    if (evidence.get("method") != "st_dev"
            or evidence.get("external_proven") is not True):
        return False
    target_device = evidence.get("target_device")
    source_devices = evidence.get("source_devices")
    if not isinstance(target_device, int) or not isinstance(source_devices, list):
        return False
    if not source_devices or not all(
            isinstance(device, int) for device in source_devices):
        return False
    return target_device not in source_devices


def _canonical_json(value: object) -> str:
    return json.dumps(
        _json_safe(value), sort_keys=True, separators=(",", ":"), default=str,
        allow_nan=False)


def _content_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_axis(axis: object) -> list[str]:
    if (not isinstance(axis, list) or not axis
            or not all(isinstance(path, str) and path.strip() for path in axis)):
        raise ValueError("forward axis must be a non-empty list of dotted paths")
    normalized = sorted(path.strip() for path in axis)
    if len(set(normalized)) != len(normalized):
        raise ValueError("forward axis contains duplicate paths")
    return normalized


def _dotted_value(mapping: dict, path: str):
    node = mapping
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ValueError(
                f"forward axis path {path!r} is not executable configuration")
        node = node[part]
    return node


def _without_dotted_paths(mapping: dict, paths: list[str]) -> dict:
    result = json.loads(_canonical_json(mapping))
    for path in paths:
        parts = path.split(".")
        node = result
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                raise ValueError(
                    f"forward axis path {path!r} is not executable configuration")
            node = node[part]
        if not isinstance(node, dict) or parts[-1] not in node:
            raise ValueError(
                f"forward axis path {path!r} is not executable configuration")
        del node[parts[-1]]
    return result


def _analysis_canonical(
        kind: str, subject_id: str, payload: dict) -> tuple[str, str]:
    payload_json = _canonical_json(payload)
    digest = _content_hash({
        "kind": str(kind), "subject_id": str(subject_id),
        "payload": json.loads(payload_json),
    })
    return payload_json, digest


def _insert_analysis_conn(
        conn: sqlite3.Connection, kind: str, subject_id: str,
        payload: dict) -> str:
    canonical, payload_hash = _analysis_canonical(kind, subject_id, payload)
    existing = conn.execute(
        "SELECT analysis_id FROM analysis_runs WHERE payload_hash=?",
        (payload_hash,)).fetchone()
    if existing is not None:
        return str(existing["analysis_id"])
    analysis_id = payload_hash[:32]
    conn.execute(
        "INSERT INTO analysis_runs (analysis_id, kind, subject_id, ts, "
        "payload_json, payload_hash) VALUES (?,?,?,?,?,?)",
        (analysis_id, kind, subject_id, time.time(), canonical, payload_hash))
    return analysis_id


def _rewrite_analysis_references(value: object, remap: dict[str, str]):
    """Rewrite structured analysis references through a final-id mapping."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if (key in {"analysis_id", "source_analysis_id"}
                    and isinstance(item, str)):
                out[key] = remap.get(item, item)
            else:
                out[key] = _rewrite_analysis_references(item, remap)
        return out
    if isinstance(value, list):
        return [_rewrite_analysis_references(item, remap) for item in value]
    return value


def _variant_identity(row_or_variant) -> dict:
    if isinstance(row_or_variant, sqlite3.Row):
        overrides = json.loads(row_or_variant["overrides_json"])
        columns = set(row_or_variant.keys())
        return {
            "variant_id": str(row_or_variant["variant_id"]),
            "strategy_id": str(row_or_variant["strategy_id"]),
            "base_version": str(row_or_variant["base_version"]),
            "overrides": overrides,
            "hypothesis": str(row_or_variant["hypothesis"]),
            "hypothesis_id": str(row_or_variant["hypothesis_id"] or "")
            if "hypothesis_id" in columns else "",
            "hypothesis_params": json.loads(
                row_or_variant["hypothesis_params_json"] or "{}")
            if "hypothesis_params_json" in columns else {},
        }
    return {
        "variant_id": str(row_or_variant.variant_id),
        "strategy_id": str(row_or_variant.strategy_id),
        "base_version": str(row_or_variant.base_version),
        "overrides": dict(row_or_variant.overrides),
        "hypothesis": str(row_or_variant.hypothesis),
        "hypothesis_id": str(getattr(row_or_variant, "hypothesis_id", None) or ""),
        "hypothesis_params": dict(getattr(row_or_variant, "hypothesis_params", {}) or {}),
    }


def variant_identity_hash(row_or_variant) -> str:
    return _content_hash(_variant_identity(row_or_variant))


FORWARD_TRADE_FIELDS = (
    "trade_id", "proposal_id", "scope_key", "variant_id", "cycle_id",
    "symbol", "direction", "setup_type", "signal_ts", "model_id",
    "entry_ts", "entry_price", "notional", "risk_usd", "stop_price",
    "take_price", "exit_ts", "exit_price", "result", "net_pnl_usd",
    "r_multiple", "status", "failure", "valid_for_inference",
    "execution_json",
)

FORWARD_DECISION_FIELDS = (
    "decision_id", "proposal_id", "scope_key", "variant_id", "cycle_id",
    "symbol", "direction", "setup_type", "signal_ts", "confidence",
    "decision_outcome", "reason", "paper_trade_id", "model_id",
    "decision_ts", "trade_proposal_id", "trade_scope_key",
    "trade_variant_id", "trade_model_id", "trade_entry_ts",
    "trade_exit_ts", "trade_result", "trade_r_multiple", "trade_status",
    "trade_valid_for_inference",
)


def _forward_trade_evidence(row: dict | sqlite3.Row) -> dict:
    item = {field: row[field] for field in FORWARD_TRADE_FIELDS}
    try:
        assumptions = json.loads(row["assumptions_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"paper trade {row['trade_id']} has invalid assumptions") from exc
    if not isinstance(assumptions, dict):
        raise ValueError(
            f"paper trade {row['trade_id']} assumptions are not a mapping")
    item["assumptions"] = assumptions
    try:
        item["execution"] = json.loads(row["execution_json"] or "null")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"paper trade {row['trade_id']} has invalid execution evidence") from exc
    return json.loads(_canonical_json(item))


def _forward_decision_evidence(row: dict | sqlite3.Row) -> dict:
    """Canonical evidence for every evaluated proposal, including vetoes."""
    item = {field: row[field] for field in FORWARD_DECISION_FIELDS}
    try:
        assumptions = json.loads(row["assumptions_json"])
        proposal = json.loads(row["proposal_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"paper decision {row['decision_id']} has invalid JSON") from exc
    if not isinstance(assumptions, dict) or not isinstance(proposal, dict):
        raise ValueError(
            f"paper decision {row['decision_id']} evidence is not a mapping")
    item["assumptions"] = assumptions
    item["proposal"] = proposal
    return json.loads(_canonical_json(item))


def _finite_number(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


_RETRYABLE_INCONCLUSIVE_CODES = {
    "NO_DECISIONS",
    "UNRESOLVED_TRADES",
    "CLOSED_SAMPLE_BELOW_FLOOR",
    "OPERATIONAL_FAILURE_IN_WINDOW",
    "MODEL_IDENTITY_MISMATCH",
    "MIXED_EXPERIMENT_PROVENANCE",
    "PAIRED_EVIDENCE_INADEQUATE",
    "TWO_SEGMENTS_UNAVAILABLE",
    "SEGMENT_EXPECTANCY_UNAVAILABLE",
    "BASELINE_DELTA_NOT_ESTABLISHED",
}
MAX_AUTOMATIC_EXPERIMENT_ATTEMPTS = 3


def _inconclusive_reason_codes(payload: object) -> set[str]:
    if not isinstance(payload, dict) or payload.get("verdict") != "INCONCLUSIVE":
        return set()
    return {
        str(reason.get("code")) for reason in payload.get("reasons") or []
        if isinstance(reason, dict)}


def _retryable_inconclusive_payload(payload: object) -> bool:
    """Return whether more clean data can resolve an inconclusive result."""
    return bool(
        _inconclusive_reason_codes(payload) & _RETRYABLE_INCONCLUSIVE_CODES)


def _automatic_retry_payload(payload: object) -> bool:
    """Only a closed but under-floor sample is retried without a new request."""
    return (
        _retryable_inconclusive_payload(payload)
        and "CLOSED_SAMPLE_BELOW_FLOOR" in _inconclusive_reason_codes(payload))


def _assignment_trade_costs(row: sqlite3.Row) -> tuple[dict, list[dict]]:
    """Rebuild the modeled paper costs without relabeling them as fills."""
    limitations = []
    notional = _finite_number(row["notional"])
    entry = _finite_number(row["entry_price"])
    exit_price = _finite_number(row["exit_price"])
    sign = 1.0 if str(row["direction"]) == "long" else -1.0
    gross = (notional * sign * (exit_price - entry) / entry
             if notional > 0 and entry > 0 and exit_price > 0 else 0.0)
    net = _finite_number(row["net_pnl_usd"])
    modeled_total = gross - net
    try:
        assumptions = json.loads(row["assumptions_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        assumptions = {}
        limitations.append({
            "code": "INVALID_COST_ASSUMPTIONS",
            "detail": f"trade {row['trade_id']} has invalid assumptions JSON",
        })
    components = (assumptions.get("cost_components")
                  if isinstance(assumptions, dict) else None)
    if not isinstance(components, dict):
        components = {}
        limitations.append({
            "code": "COST_BREAKDOWN_UNAVAILABLE",
            "detail": (
                f"trade {row['trade_id']} has net modeled costs but no "
                "component breakdown"),
        })
    if str(row["result"]) == "unfilled":
        fee_pct = execution_pct = funding_pct = 0.0
        funding_intervals = 0
    else:
        fee_pct = sum(_finite_number(components.get(key)) for key in (
            "entry_fee_pct", "exit_fee_pct"))
        try:
            execution_evidence = json.loads(row["execution_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            execution_evidence = {}
        exit_evidence = execution_evidence.get("exit") or {}
        execution_pct = _finite_number(components.get("entry_slippage_pct"))
        if not (isinstance(exit_evidence, dict)
                and exit_evidence.get("spread_in_exit_price")):
            execution_pct += _finite_number(components.get("spread_pct"))
        if str(row["result"]) in {"stop", "paper_insolvency"}:
            execution_pct += _finite_number(
                components.get("stop_slippage_pct"))
        events = execution_evidence.get("funding_events") or []
        realized_rates = [
            _finite_number(event.get("rate_pct")) for event in events
            if isinstance(event, dict) and event.get("status") == "realized"]
        funding_intervals = len(realized_rates)
        funding_pct = sign * sum(realized_rates)
    fees = notional * fee_pct / 100.0
    execution = notional * execution_pct / 100.0
    funding = notional * funding_pct / 100.0
    unexplained = modeled_total - fees - execution - funding
    if abs(unexplained) > max(0.01, abs(modeled_total) * 1e-6):
        limitations.append({
            "code": "COST_COMPONENT_RECONCILIATION_GAP",
            "detail": (
                f"trade {row['trade_id']} modeled cost components differ "
                f"from gross-minus-net by {unexplained:+.6f} USDT"),
        })
    return ({
        "gross_pnl_usdt": gross,
        "fees_usdt": fees,
        "execution_cost_usdt": execution,
        "funding_cost_usdt": funding,
        "modeled_total_cost_usdt": modeled_total,
        "unexplained_cost_usdt": unexplained,
        "funding_intervals": funding_intervals,
    }, limitations)


def _interval_payload(interval) -> dict:
    return {
        "point": _finite_number(getattr(interval, "point", 0.0)),
        "low": _finite_number(getattr(interval, "low", 0.0)),
        "high": _finite_number(getattr(interval, "high", 0.0)),
        "n": int(getattr(interval, "n", 0) or 0),
        "clusters": int(getattr(interval, "clusters", 0) or 0),
    }


def _paired_payload(comparison: dict) -> dict:
    return {
        **{key: value for key, value in comparison.items()
           if key != "interval"},
        "interval": _interval_payload(comparison.get("interval")),
    }


def _experiment_arm_evidence(
        conn: sqlite3.Connection, scope_key: str, variant_id: str,
        started_ts: float, ended_ts: float) -> dict:
    from research import protocol
    from research.score import score_returns

    has_execution_validity = _stored_version(conn) >= 16
    stored_validity = (
        "t.valid_for_inference" if has_execution_validity else "1")
    validity_alias = (
        "CASE WHEN t.result='unfilled' THEN 0 "
        f"ELSE {stored_validity} END")
    rows = conn.execute(
        "SELECT d.*, t.proposal_id AS trade_proposal_id, "
        "t.scope_key AS trade_scope_key, t.variant_id AS trade_variant_id, "
        "t.model_id AS trade_model_id, t.entry_ts AS trade_entry_ts, "
        "t.exit_ts AS trade_exit_ts, t.result AS trade_result, "
        "t.r_multiple AS trade_r_multiple, t.status AS trade_status, "
        f"{validity_alias} AS trade_valid_for_inference "
        "FROM paper_decisions AS d LEFT JOIN paper_trades AS t "
        "ON t.trade_id=d.paper_trade_id "
        "WHERE d.scope_key=? AND d.variant_id=? "
        "AND d.decision_ts>=? AND d.decision_ts<=? "
        "ORDER BY d.decision_ts, d.decision_id",
        (scope_key, variant_id, started_ts, ended_ts)).fetchall()
    decisions = []
    for row in rows:
        evidence = _forward_decision_evidence(row)
        exit_ts = evidence.get("trade_exit_ts")
        if exit_ts is not None and float(exit_ts) > ended_ts:
            evidence.update({
                "trade_exit_ts": None, "trade_result": None,
                "trade_r_multiple": None, "trade_status": "OPEN",
            })
        decisions.append(evidence)

    trade_rows = conn.execute(
        "SELECT DISTINCT t.* FROM paper_trades AS t "
        "JOIN paper_decisions AS d ON d.paper_trade_id=t.trade_id "
        "WHERE d.scope_key=? AND d.variant_id=? "
        "AND d.decision_ts>=? AND d.decision_ts<=? "
        "ORDER BY t.entry_ts, t.trade_id",
        (scope_key, variant_id, started_ts, ended_ts)).fetchall()
    unfilled = [row for row in trade_rows
                if str(row["status"]) == "CLOSED"
                and str(row["result"]) == "unfilled"
                and row["exit_ts"] is not None
                and float(row["exit_ts"]) <= ended_ts]
    closed = [row for row in trade_rows
              if row["exit_ts"] is not None
              and float(row["exit_ts"]) <= ended_ts
              and str(row["result"]) != "unfilled"
              and row["r_multiple"] is not None
              and (not has_execution_validity
                   or int(row["valid_for_inference"] or 0) == 1)]
    unresolved = [row for row in trade_rows
                  if row not in closed and row not in unfilled]
    costs = {
        "gross_pnl_usdt": 0.0, "fees_usdt": 0.0,
        "execution_cost_usdt": 0.0, "funding_cost_usdt": 0.0,
        "modeled_total_cost_usdt": 0.0, "unexplained_cost_usdt": 0.0,
    }
    cost_limitations = []
    for trade in closed:
        trade_costs, limitations = _assignment_trade_costs(trade)
        for key in costs:
            costs[key] += _finite_number(trade_costs.get(key))
        cost_limitations.extend(limitations)
    trade_returns = [_finite_number(row["r_multiple"]) for row in closed]
    trade_score = score_returns(trade_returns, label=variant_id)
    policy = protocol.paper_trade_decisions(decisions)
    policy_returns = [
        _finite_number(item.outcome.get("r_multiple"))
        for item in policy if getattr(item, "outcome", None)
        and item.outcome.get("r_multiple") is not None
    ]
    policy_score = score_returns(policy_returns, label=f"{variant_id}:policy")
    net_pnl = sum(_finite_number(row["net_pnl_usd"]) for row in closed)
    wins = sum(_finite_number(row["net_pnl_usd"]) > 0 for row in closed)
    losses = sum(_finite_number(row["net_pnl_usd"]) < 0 for row in closed)
    breakeven = len(closed) - wins - losses
    portfolio_row = conn.execute(
        "SELECT state_json FROM paper_portfolios WHERE scope_key=? "
        "AND variant_id=?", (scope_key, variant_id)).fetchone()
    portfolio_state = (json.loads(portfolio_row["state_json"])
                       if portfolio_row is not None else {})
    assignment_matches = (
        portfolio_state.get("experiment_assignment_started_ts") is not None
        and abs(float(portfolio_state["experiment_assignment_started_ts"])
                - float(started_ts)) <= 1e-6)
    max_drawdown_pct = (
        _finite_number(portfolio_state.get("max_drawdown_pct"))
        if assignment_matches else None)
    starting_equity = (
        _finite_number(portfolio_state.get("assignment_initial_balance_usdt")
                       or portfolio_state.get("day_start_equity"))
        if assignment_matches else 0.0)
    max_drawdown_usdt = (
        starting_equity * float(max_drawdown_pct) / 100.0
        if max_drawdown_pct is not None else None)
    model_ids = sorted({str(row["model_id"]) for row in rows
                        if row["model_id"]})
    provenances = []
    for decision in decisions:
        provenance = (decision.get("assumptions") or {}).get(
            "experiment_provenance")
        if isinstance(provenance, dict) and provenance not in provenances:
            provenances.append(provenance)
    failures = [dict(row) for row in conn.execute(
        "SELECT ts, kind, detail_json FROM paper_failures "
        "WHERE scope_key=? AND variant_id=? AND ts>=? AND ts<=? "
        "ORDER BY ts, failure_id",
        (scope_key, variant_id, started_ts, ended_ts)).fetchall()]
    for failure in failures:
        try:
            failure["detail"] = json.loads(failure.pop("detail_json"))
        except (TypeError, json.JSONDecodeError):
            failure["detail"] = {"raw": failure.pop("detail_json", None)}
    return {
        "variant_id": variant_id,
        "proposal_count": len({str(row["proposal_id"]) for row in rows}),
        "decision_count": len(rows),
        "proposed_decisions": sum(
            str(row["decision_outcome"]) == "PROPOSED" for row in rows),
        "vetoed_decisions": sum(
            str(row["decision_outcome"]) == "VETOED" for row in rows),
        "opens": len(trade_rows), "closes": len(closed),
        "fill_opportunities": len(trade_rows), "unfilled": len(unfilled),
        "unresolved_opens": len(unresolved),
        "wins": wins, "losses": losses, "breakeven": breakeven,
        "win_rate_pct": (wins / len(closed) * 100.0 if closed else None),
        "net_pnl_usdt": net_pnl,
        "max_drawdown_usdt": max_drawdown_usdt,
        "max_drawdown_pct": max_drawdown_pct,
        "drawdown_source": (
            "assignment_rebased_mark_to_market_portfolio"
            if assignment_matches else "unavailable"),
        "trade_r_metrics": trade_score,
        "policy_r_metrics": policy_score,
        "costs": costs,
        "model_ids": model_ids,
        "experiment_provenance": provenances,
        "operational_failures": failures,
        "cost_limitations": cost_limitations,
        "decisions": decisions,
        "policy_decisions": policy,
    }


def _deterministic_experiment_payload(
        conn: sqlite3.Connection, assignment: sqlite3.Row,
        terminal_event: sqlite3.Row) -> dict:
    from research import protocol

    assignment_id = str(assignment["assignment_id"])
    scope_key = str(assignment["scope_key"])
    started_ts = float(assignment["started_ts"])
    ended_ts = float(terminal_event["event_ts"])
    baseline_id = str(assignment["baseline_variant_id"])
    candidate_id = str(assignment["candidate_variant_id"])
    baseline_variant = conn.execute(
        "SELECT * FROM variants WHERE variant_id=?", (baseline_id,)).fetchone()
    candidate_variant = conn.execute(
        "SELECT * FROM variants WHERE variant_id=?", (candidate_id,)).fetchone()
    baseline = _experiment_arm_evidence(
        conn, scope_key, baseline_id, started_ts, ended_ts)
    candidate = _experiment_arm_evidence(
        conn, scope_key, candidate_id, started_ts, ended_ts)
    full = protocol.paired_arm_comparison(
        candidate["policy_decisions"], baseline["policy_decisions"])
    cutoff = protocol.common_time_cutoff([baseline["policy_decisions"]])
    if cutoff is None:
        candidate_fit, candidate_confirm = candidate["policy_decisions"], []
        baseline_fit, baseline_confirm = baseline["policy_decisions"], []
    else:
        candidate_fit, candidate_confirm = protocol.split_at_time(
            candidate["policy_decisions"], cutoff)
        baseline_fit, baseline_confirm = protocol.split_at_time(
            baseline["policy_decisions"], cutoff)
    fit = protocol.paired_arm_comparison(candidate_fit, baseline_fit)
    confirm = protocol.paired_arm_comparison(
        candidate_confirm, baseline_confirm)

    def policy_expectancy(items: list) -> float | None:
        values = [
            _finite_number(item.outcome.get("r_multiple"))
            for item in items if getattr(item, "outcome", None)
            and item.outcome.get("r_multiple") is not None
        ]
        return sum(values) / len(values) if values else None

    reasons: list[dict] = []
    limitations: list[dict] = [{
        "code": "MODELED_PAPER_COSTS",
        "detail": (
            "fees, execution costs, funding, and PnL are deterministic paper "
            "model outputs rather than exchange fills"),
    }]
    limitations.extend(baseline["cost_limitations"])
    limitations.extend(candidate["cost_limitations"])

    def add(code: str, gate: str, detail: str) -> None:
        reasons.append({"code": code, "gate": gate, "detail": detail})

    status = str(terminal_event["status"])
    if status == "REJECTED":
        add("ASSIGNMENT_REJECTED", "terminal_status",
            str(terminal_event["reason"] or "assignment rejected"))
        verdict = "FAILED"
    else:
        verdict = "WORKED"
        hard_limitations = []
        for arm in (baseline, candidate):
            if arm["decision_count"] == 0:
                hard_limitations.append((
                    "NO_DECISIONS", f"{arm['variant_id']} has no decisions"))
            if arm["unresolved_opens"]:
                hard_limitations.append((
                    "UNRESOLVED_TRADES",
                    f"{arm['variant_id']} has {arm['unresolved_opens']} "
                    "assignment-window trade(s) unresolved at terminal time"))
            if arm["closes"] < protocol.MIN_ROUND_TRIPS:
                hard_limitations.append((
                    "CLOSED_SAMPLE_BELOW_FLOOR",
                    f"{arm['variant_id']} has {arm['closes']} closed trades; "
                    f"{protocol.MIN_ROUND_TRIPS} required"))
            if arm["operational_failures"]:
                hard_limitations.append((
                    "OPERATIONAL_FAILURE_IN_WINDOW",
                    f"{arm['variant_id']} has persisted operational failures"))
            if arm["max_drawdown_pct"] is None:
                hard_limitations.append((
                    "MARK_TO_MARKET_DRAWDOWN_UNAVAILABLE",
                    f"{arm['variant_id']} has no assignment-rebased "
                    "mark-to-market drawdown state"))
            expected_model = json.loads(
                assignment["code_identity_json"] or "{}").get(
                    "baseline" if arm is baseline else "candidate", {}).get(
                        "forward_model_id")
            if (len(arm["model_ids"]) != 1
                    or (expected_model
                        and arm["model_ids"] != [str(expected_model)])):
                hard_limitations.append((
                    "MODEL_IDENTITY_MISMATCH",
                    f"{arm['variant_id']} model identities are "
                    f"{arm['model_ids']}, expected {expected_model}"))
            if len(arm["experiment_provenance"]) != 1:
                hard_limitations.append((
                    "MIXED_EXPERIMENT_PROVENANCE",
                    f"{arm['variant_id']} has "
                    f"{len(arm['experiment_provenance'])} provenance records"))
        for comparison, minimum, label in (
                (full, protocol.MIN_ROUND_TRIPS, "full"),
                (fit, protocol.MIN_PAIRED_FIT_OBSERVATIONS, "fit"),
                (confirm, protocol.MIN_PAIRED_CONFIRM_OBSERVATIONS, "confirm")):
            if not protocol.paired_window_adequate(comparison, minimum):
                hard_limitations.append((
                    "PAIRED_EVIDENCE_INADEQUATE",
                    f"{label} window has {comparison['paired_n']} pairs, "
                    f"{comparison['pair_coverage_pct']:.1f}% coverage, and "
                    f"{comparison['bootstrap']['clusters']} clusters"))
        if cutoff is None:
            hard_limitations.append((
                "TWO_SEGMENTS_UNAVAILABLE",
                "the persisted proposal calendar cannot form two time segments"))
        if hard_limitations:
            verdict = "INCONCLUSIVE"
            for code, detail in hard_limitations:
                add(code, "evidence_adequacy", detail)
        else:
            candidate_expectancy = _finite_number(
                candidate["trade_r_metrics"].get("expectancy_r"))
            baseline_expectancy = _finite_number(
                baseline["trade_r_metrics"].get("expectancy_r"))
            fit_candidate = policy_expectancy(candidate_fit)
            fit_baseline = policy_expectancy(baseline_fit)
            confirm_candidate = policy_expectancy(candidate_confirm)
            confirm_baseline = policy_expectancy(baseline_confirm)
            negative = []
            uncertain = []
            if candidate["net_pnl_usdt"] <= 0 or candidate_expectancy <= 0:
                negative.append((
                    "NONPOSITIVE_AFTER_COST_EXPECTANCY",
                    "candidate net PnL and trade expectancy must both be positive"))
            if candidate_expectancy <= baseline_expectancy:
                negative.append((
                    "NO_BASELINE_RELATIVE_IMPROVEMENT",
                    "candidate trade expectancy does not exceed baseline"))
            if candidate["max_drawdown_usdt"] > baseline["max_drawdown_usdt"]:
                negative.append((
                    "DRAWDOWN_WORSE_THAN_BASELINE",
                    "candidate assignment-window drawdown exceeds baseline"))
            for label, comparison, cand_exp, base_exp in (
                    ("full", full, _finite_number(
                        candidate["policy_r_metrics"].get("expectancy_r")),
                     _finite_number(
                         baseline["policy_r_metrics"].get("expectancy_r"))),
                    ("fit", fit, fit_candidate, fit_baseline),
                    ("confirm", confirm, confirm_candidate, confirm_baseline)):
                interval = comparison["interval"]
                if cand_exp is None or base_exp is None:
                    uncertain.append((
                        "SEGMENT_EXPECTANCY_UNAVAILABLE",
                        f"{label} expectancy is unavailable"))
                    continue
                if cand_exp <= 0 or cand_exp <= base_exp:
                    negative.append((
                        "SEGMENT_INCONSISTENT",
                        f"{label} candidate expectancy is not positive and "
                        "above baseline"))
                if interval.high <= 0:
                    negative.append((
                        "BASELINE_DELTA_NONPOSITIVE",
                        f"{label} paired delta interval is wholly nonpositive"))
                elif interval.low <= 0:
                    uncertain.append((
                        "BASELINE_DELTA_NOT_ESTABLISHED",
                        f"{label} paired delta interval does not clear zero"))
            qualification = conn.execute(
                "SELECT status, detail_json, source_analysis_id "
                "FROM edge_qualification_events "
                "WHERE variant_id=? AND scope_key IN ('*', ?) "
                "ORDER BY ts DESC, rowid DESC LIMIT 1",
                (candidate_id, scope_key)).fetchone()
            if qualification is not None and qualification["status"] == "REVOKED":
                negative.append((
                    "EXISTING_DISQUALIFYING_GATE",
                    "candidate has a persisted revoked qualification"))
            if str(candidate_variant["status"]) in {"rejected", "superseded"}:
                negative.append((
                    "EXISTING_DISQUALIFYING_GATE",
                    f"candidate variant status is {candidate_variant['status']}"))
            portfolio = conn.execute(
                "SELECT state_json FROM paper_portfolios "
                "WHERE scope_key=? AND variant_id=?",
                (scope_key, candidate_id)).fetchone()
            if portfolio is not None:
                try:
                    portfolio_status = json.loads(
                        portfolio["state_json"]).get("status")
                except (TypeError, json.JSONDecodeError):
                    portfolio_status = "INVALID"
                if portfolio_status in {"REVOKED", "INVALID"}:
                    negative.append((
                        "EXISTING_DISQUALIFYING_GATE",
                        f"candidate paper portfolio status is "
                        f"{portfolio_status}"))
            if negative:
                verdict = "FAILED"
                for code, detail in negative:
                    add(code, "performance", detail)
            elif uncertain:
                verdict = "INCONCLUSIVE"
                for code, detail in uncertain:
                    add(code, "statistical_confidence", detail)
            else:
                limitations.append({
                    "code": "FORWARD_AXIS_QUALIFICATION_REQUIRED",
                    "detail": (
                        "WORKED is an assignment-level research result. A "
                        "current v3 forward-axis analysis, complete family "
                        "correction, and isolated PAPER stage remain required "
                        "before a reviewed T3 packet can be created."),
                })
                add("ALL_CONSERVATIVE_GATES_PASSED", "edge_evidence",
                    "assignment-level sample, costs, baseline delta, two "
                    "segments, drawdown, and disqualifying-gate checks all "
                    "passed")

    for limitation in limitations:
        limitation.setdefault("severity", "disclosure")
    for arm in (baseline, candidate):
        arm.pop("policy_decisions", None)
        arm.pop("decisions", None)
        arm.pop("cost_limitations", None)
    payload = {
        "schema": "experiment_outcome.v1",
        "assignment_id": assignment_id,
        "terminal_status": status,
        "verdict": verdict,
        "reasons": reasons,
        "scope_key": scope_key,
        "strategy_id": str(assignment["strategy_id"]),
        "axis": str(assignment["axis"]),
        "setting_id": str(assignment["setting_id"]),
        "setting": json.loads(assignment["setting_json"]),
        "source": str(assignment["source"]),
        "proposal_id": assignment["proposal_id"],
        "data_window": {
            "from_ts": started_ts, "to_ts": ended_ts,
            "split_cutoff_ts": cutoff,
            "elapsed_seconds": max(0.0, ended_ts - started_ts),
        },
        "feed_identity": {
            "scope_key": scope_key,
            "forward_feed_version": (
                int(scope_key.rsplit(":feed-v", 1)[1])
                if ":feed-v" in scope_key
                and scope_key.rsplit(":feed-v", 1)[1].isdigit()
                else None),
        },
        "code_identity": json.loads(assignment["code_identity_json"]),
        "config_identity": json.loads(assignment["config_identity_json"]),
        "baseline_variant": _variant_identity(baseline_variant),
        "candidate_variant": _variant_identity(candidate_variant),
        "baseline": baseline,
        "candidate": candidate,
        "paired": {
            "full": _paired_payload(full),
            "fit": _paired_payload(fit),
            "confirm": _paired_payload(confirm),
        },
        "data_limitations": limitations,
        "terminal_reason": terminal_event["reason"],
    }
    return json.loads(_canonical_json(payload))


def _ensure_experiment_outcome_conn(
        conn: sqlite3.Connection, assignment_id: str) -> tuple[dict, bool]:
    existing = conn.execute(
        "SELECT * FROM experiment_outcomes WHERE assignment_id=?",
        (assignment_id,)).fetchone()
    if existing is not None:
        item = dict(existing)
        item["payload"] = json.loads(item.pop("payload_json"))
        return item, False
    assignment = conn.execute(
        "SELECT * FROM strategy_experiment_assignments WHERE assignment_id=?",
        (assignment_id,)).fetchone()
    if assignment is None:
        raise ValueError(f"unknown assignment_id {assignment_id!r}")
    terminal = conn.execute(
        "SELECT * FROM strategy_experiment_assignment_events "
        "WHERE assignment_id=? AND status IN ('COMPLETED','REJECTED') "
        "ORDER BY event_ts DESC, rowid DESC LIMIT 1",
        (assignment_id,)).fetchone()
    if terminal is None:
        raise ValueError("experiment outcome requires a terminal assignment")
    payload = _deterministic_experiment_payload(conn, assignment, terminal)
    payload_json = _canonical_json(payload)
    payload_hash = _content_hash(payload)
    outcome_id = _content_hash({
        "assignment_id": assignment_id, "payload_hash": payload_hash,
    })[:32]
    analysis_id = _insert_analysis_conn(
        conn, "experiment_outcome", assignment_id, payload)
    finding = {
        "type": "experiment_outcome",
        "outcome_id": outcome_id,
        "assignment_id": assignment_id,
        "verdict": payload["verdict"],
        "reasons": payload["reasons"],
        "data_limitations": payload["data_limitations"],
    }
    cursor = conn.execute(
        "INSERT INTO findings (variant_id, ts, author, kind, text, run_id) "
        "VALUES (?,?,?,?,?,NULL)",
        (assignment["candidate_variant_id"], float(terminal["event_ts"]),
         "deterministic-research", "decision", _canonical_json(finding)))
    finding_id = int(cursor.lastrowid)
    conn.execute(
        "INSERT INTO experiment_outcomes (outcome_id, assignment_id, "
        "scope_key, strategy_id, baseline_variant_id, candidate_variant_id, "
        "terminal_status, verdict, created_ts, payload_json, payload_hash, "
        "analysis_id, finding_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (outcome_id, assignment_id, assignment["scope_key"],
         assignment["strategy_id"], assignment["baseline_variant_id"],
         assignment["candidate_variant_id"], terminal["status"],
         payload["verdict"], float(terminal["event_ts"]), payload_json,
         payload_hash, analysis_id, finding_id))
    if payload["verdict"] == "WORKED":
        edge_payload = {
            "schema": "research_edge_evidence.v1",
            "authority": "RESEARCH_ONLY",
            "promotion_allowed": False,
            "forward_qualification_required": True,
            "outcome_id": outcome_id,
            "outcome_hash": payload_hash,
            "assignment_id": assignment_id,
            "scope_key": assignment["scope_key"],
            "strategy_id": assignment["strategy_id"],
            "variant_id": assignment["candidate_variant_id"],
            "variant_identity": payload["candidate_variant"],
            "data_window": payload["data_window"],
            "feed_identity": payload["feed_identity"],
            "code_identity": payload["code_identity"],
            "config_identity": payload["config_identity"],
            "model_ids": payload["candidate"]["model_ids"],
            "criteria": payload["reasons"],
        }
        edge_id = _content_hash(edge_payload)[:32]
        conn.execute(
            "INSERT INTO research_edge_evidence (edge_id, outcome_id, "
            "assignment_id, scope_key, strategy_id, variant_id, status, "
            "created_ts, evidence_json, evidence_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (edge_id, outcome_id, assignment_id, assignment["scope_key"],
             assignment["strategy_id"], assignment["candidate_variant_id"],
             "EDGE_CANDIDATE", float(terminal["event_ts"]),
             _canonical_json(edge_payload), _content_hash(edge_payload)))
    row = conn.execute(
        "SELECT * FROM experiment_outcomes WHERE outcome_id=?",
        (outcome_id,)).fetchone()
    item = dict(row)
    item["payload"] = json.loads(item.pop("payload_json"))
    return item, True


class MigrationError(RuntimeError):
    """The findings database cannot be opened without risking its history."""


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, factory=_ClosingConnection)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        _migrate(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _execute_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute a SQL script without sqlite3's implicit pre-script commit."""
    statement = ""
    for line in script.splitlines():
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            if sql:
                conn.execute(sql)
            statement = ""
    if statement.strip():
        raise MigrationError("incomplete SQL statement in findings migration")


def _t3_canonical(
        variant_id: str, payload: dict, review_status: str,
        reviewed_by: str | None, registry_change_ref: str | None) -> tuple[str, str]:
    payload_json = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str,
        allow_nan=False)
    envelope = json.dumps({
        "variant_id": variant_id,
        "review_status": review_status,
        "reviewed_by": reviewed_by or None,
        "registry_change_ref": registry_change_ref or None,
        "payload": json.loads(payload_json),
    }, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return payload_json, hashlib.sha256(envelope.encode("utf-8")).hexdigest()


def _stored_version(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "schema_meta"):
        # The pre-migration findings store already had the four core tables.
        return 1 if _table_exists(conn, "variants") else 0
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    if row is None:
        return 1 if _table_exists(conn, "variants") else 0
    try:
        return int(row[0])
    except (TypeError, ValueError) as exc:
        raise MigrationError("findings schema version is not an integer") from exc


def _migration_1(conn: sqlite3.Connection) -> None:
    _execute_script(conn, """
        CREATE TABLE IF NOT EXISTS variants (
            variant_id TEXT PRIMARY KEY, strategy_id TEXT,
            base_version TEXT, overrides_json TEXT, hypothesis TEXT,
            status TEXT, created_ts REAL, updated_ts REAL);
        CREATE TABLE IF NOT EXISTS variant_runs (
            run_id TEXT PRIMARY KEY, variant_id TEXT, corpus_from_ts REAL,
            corpus_to_ts REAL, corpus_cycles INTEGER, mode TEXT,
            code_version TEXT, scorer_version TEXT, ts REAL,
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id));
        CREATE TABLE IF NOT EXISTS variant_results (
            run_id TEXT, metric TEXT, value REAL, ci_low REAL,
            ci_high REAL, n INTEGER,
            FOREIGN KEY (run_id) REFERENCES variant_runs(run_id),
            UNIQUE (run_id, metric));
        CREATE TABLE IF NOT EXISTS findings (
            finding_id INTEGER PRIMARY KEY AUTOINCREMENT,
            variant_id TEXT, ts REAL, author TEXT, kind TEXT, text TEXT,
            run_id TEXT,
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id),
            FOREIGN KEY (run_id) REFERENCES variant_runs(run_id));
        CREATE INDEX IF NOT EXISTS findings_variant
            ON findings (variant_id, ts);
        CREATE UNIQUE INDEX IF NOT EXISTS variant_result_metric
            ON variant_results (run_id, metric);
    """)


def _drop_history_triggers(conn: sqlite3.Connection) -> None:
    _execute_script(conn, """
        DROP TRIGGER IF EXISTS findings_no_update;
        DROP TRIGGER IF EXISTS findings_no_delete;
        DROP TRIGGER IF EXISTS variant_runs_no_update;
        DROP TRIGGER IF EXISTS variant_runs_no_delete;
        DROP TRIGGER IF EXISTS variant_results_no_update;
        DROP TRIGGER IF EXISTS variant_results_no_delete;
    """)


def _install_history_triggers(conn: sqlite3.Connection) -> None:
    _execute_script(conn, """
        CREATE TRIGGER IF NOT EXISTS findings_no_update
            BEFORE UPDATE ON findings BEGIN
                SELECT RAISE(ABORT, 'findings are append-only');
            END;
        CREATE TRIGGER IF NOT EXISTS findings_no_delete
            BEFORE DELETE ON findings BEGIN
                SELECT RAISE(ABORT, 'findings are append-only');
            END;
        CREATE TRIGGER IF NOT EXISTS variant_runs_no_update
            BEFORE UPDATE ON variant_runs BEGIN
                SELECT RAISE(ABORT, 'variant runs are immutable');
            END;
        CREATE TRIGGER IF NOT EXISTS variant_runs_no_delete
            BEFORE DELETE ON variant_runs BEGIN
                SELECT RAISE(ABORT, 'variant runs are immutable');
            END;
        CREATE TRIGGER IF NOT EXISTS variant_results_no_update
            BEFORE UPDATE ON variant_results BEGIN
                SELECT RAISE(ABORT, 'variant results are immutable');
            END;
        CREATE TRIGGER IF NOT EXISTS variant_results_no_delete
            BEFORE DELETE ON variant_results BEGIN
                SELECT RAISE(ABORT, 'variant results are immutable');
            END;
    """)


def _migration_2(conn: sqlite3.Connection) -> None:
    """Rebuild legacy tables so constraints are real, without losing rows."""
    _drop_history_triggers(conn)
    conn.execute("DROP INDEX IF EXISTS variant_result_metric")
    conn.execute("DROP INDEX IF EXISTS findings_variant")
    _execute_script(conn, """
        ALTER TABLE variants RENAME TO variants_legacy;
        ALTER TABLE variant_runs RENAME TO variant_runs_legacy;
        ALTER TABLE variant_results RENAME TO variant_results_legacy;
        ALTER TABLE findings RENAME TO findings_legacy;

        CREATE TABLE variants (
            variant_id TEXT PRIMARY KEY,
            strategy_id TEXT NOT NULL,
            base_version TEXT NOT NULL,
            overrides_json TEXT NOT NULL,
            hypothesis TEXT NOT NULL,
            status TEXT NOT NULL,
            created_ts REAL NOT NULL,
            updated_ts REAL NOT NULL);
        CREATE TABLE variant_runs (
            run_id TEXT PRIMARY KEY,
            variant_id TEXT NOT NULL,
            corpus_from_ts REAL,
            corpus_to_ts REAL,
            corpus_cycles INTEGER NOT NULL DEFAULT 0,
            mode TEXT NOT NULL,
            code_version TEXT NOT NULL DEFAULT '',
            scorer_version TEXT NOT NULL DEFAULT '',
            ts REAL NOT NULL,
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id));
        CREATE TABLE variant_results (
            run_id TEXT NOT NULL,
            metric TEXT NOT NULL,
            value REAL,
            ci_low REAL,
            ci_high REAL,
            n INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (run_id, metric),
            FOREIGN KEY (run_id) REFERENCES variant_runs(run_id));
        CREATE TABLE findings (
            finding_id INTEGER PRIMARY KEY AUTOINCREMENT,
            variant_id TEXT NOT NULL,
            ts REAL NOT NULL,
            author TEXT NOT NULL,
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            run_id TEXT,
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id),
            FOREIGN KEY (run_id) REFERENCES variant_runs(run_id));
    """)
    conn.execute("""
        INSERT INTO variants
        SELECT variant_id, COALESCE(strategy_id, 'legacy'),
               COALESCE(base_version, 'legacy'), COALESCE(overrides_json, '{}'),
               COALESCE(hypothesis, 'Legacy record; hypothesis was not stored.'),
               COALESCE(status, 'testing'), COALESCE(created_ts, 0),
               COALESCE(updated_ts, created_ts, 0)
        FROM variants_legacy
    """)
    conn.execute("""
        INSERT INTO variant_runs
        SELECT run_id, variant_id, corpus_from_ts, corpus_to_ts,
               COALESCE(corpus_cycles, 0), COALESCE(mode, 'unknown'),
               COALESCE(code_version, ''), COALESCE(scorer_version, ''),
               COALESCE(ts, 0)
        FROM variant_runs_legacy
    """)
    conn.execute("""
        INSERT INTO variant_results
        SELECT run_id, metric, value, ci_low, ci_high, COALESCE(n, 0)
        FROM variant_results_legacy
    """)
    conn.execute("""
        INSERT INTO findings
        SELECT finding_id, variant_id, COALESCE(ts, 0),
               COALESCE(author, 'legacy'), COALESCE(kind, 'observation'),
               COALESCE(text, ''), NULLIF(run_id, '')
        FROM findings_legacy
    """)
    _execute_script(conn, """
        DROP TABLE findings_legacy;
        DROP TABLE variant_results_legacy;
        DROP TABLE variant_runs_legacy;
        DROP TABLE variants_legacy;
        CREATE INDEX findings_variant ON findings (variant_id, ts);
    """)
    _install_history_triggers(conn)


def _migration_3(conn: sqlite3.Connection) -> None:
    _execute_script(conn, """
        CREATE TABLE variant_scheduler (
            scope_key TEXT NOT NULL,
            variant_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            evaluations INTEGER NOT NULL DEFAULT 0,
            skips INTEGER NOT NULL DEFAULT 0,
            last_evaluated_ts REAL,
            last_skipped_ts REAL,
            PRIMARY KEY (scope_key, variant_id),
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id));
        CREATE TABLE paper_portfolios (
            scope_key TEXT NOT NULL,
            variant_id TEXT NOT NULL,
            state_json TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            updated_ts REAL NOT NULL,
            revoked_ts REAL,
            revoke_reason TEXT,
            PRIMARY KEY (scope_key, variant_id),
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id));
        CREATE TABLE paper_trades (
            trade_id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL UNIQUE,
            scope_key TEXT NOT NULL,
            variant_id TEXT NOT NULL,
            cycle_id TEXT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            setup_type TEXT,
            signal_ts REAL,
            model_id TEXT NOT NULL,
            assumptions_json TEXT NOT NULL,
            entry_ts REAL NOT NULL,
            entry_price REAL NOT NULL,
            notional REAL NOT NULL,
            risk_usd REAL NOT NULL,
            stop_price REAL NOT NULL,
            take_price REAL NOT NULL,
            exit_ts REAL,
            exit_price REAL,
            result TEXT,
            net_pnl_usd REAL,
            r_multiple REAL,
            status TEXT NOT NULL,
            failure TEXT,
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id));
        CREATE INDEX paper_trades_variant_ts
            ON paper_trades (scope_key, variant_id, entry_ts);
        CREATE TABLE paper_failures (
            failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_key TEXT NOT NULL,
            variant_id TEXT NOT NULL,
            ts REAL NOT NULL,
            kind TEXT NOT NULL,
            detail_json TEXT NOT NULL,
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id));
        CREATE TABLE run_evidence (
            run_id TEXT PRIMARY KEY,
            evidence_json TEXT NOT NULL,
            created_ts REAL NOT NULL,
            FOREIGN KEY (run_id) REFERENCES variant_runs(run_id));
        CREATE TABLE analysis_runs (
            analysis_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            ts REAL NOT NULL,
            payload_json TEXT NOT NULL);
        CREATE TABLE t3_evidence_packets (
            packet_id TEXT PRIMARY KEY,
            variant_id TEXT NOT NULL,
            created_ts REAL NOT NULL,
            review_status TEXT NOT NULL,
            reviewed_by TEXT,
            registry_change_ref TEXT,
            payload_json TEXT NOT NULL,
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id));
        CREATE TRIGGER t3_packets_no_update
            BEFORE UPDATE ON t3_evidence_packets BEGIN
                SELECT RAISE(ABORT, 'T3 evidence packets are immutable');
            END;
        CREATE TRIGGER t3_packets_no_delete
            BEFORE DELETE ON t3_evidence_packets BEGIN
                SELECT RAISE(ABORT, 'T3 evidence packets are immutable');
            END;
    """)


def _migration_4(conn: sqlite3.Connection) -> None:
    """Scope paper proposal identity and append qualification history."""
    _execute_script(conn, """
        DROP INDEX IF EXISTS paper_trades_variant_ts;
        ALTER TABLE paper_trades RENAME TO paper_trades_legacy;
        CREATE TABLE paper_trades (
            trade_id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            variant_id TEXT NOT NULL,
            cycle_id TEXT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            setup_type TEXT,
            signal_ts REAL,
            model_id TEXT NOT NULL,
            assumptions_json TEXT NOT NULL,
            entry_ts REAL NOT NULL,
            entry_price REAL NOT NULL,
            notional REAL NOT NULL,
            risk_usd REAL NOT NULL,
            stop_price REAL NOT NULL,
            take_price REAL NOT NULL,
            exit_ts REAL,
            exit_price REAL,
            result TEXT,
            net_pnl_usd REAL,
            r_multiple REAL,
            status TEXT NOT NULL,
            failure TEXT,
            UNIQUE (scope_key, variant_id, proposal_id),
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id));
        INSERT INTO paper_trades
            SELECT * FROM paper_trades_legacy;
        DROP TABLE paper_trades_legacy;
        CREATE INDEX paper_trades_variant_ts
            ON paper_trades (scope_key, variant_id, entry_ts);
        CREATE TABLE edge_qualification_events (
            event_id TEXT PRIMARY KEY,
            variant_id TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('QUALIFIED','REVOKED')),
            ts REAL NOT NULL,
            source_analysis_id TEXT,
            detail_json TEXT NOT NULL,
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id));
        CREATE INDEX edge_qualification_variant_ts
            ON edge_qualification_events (variant_id, ts, event_id);
        CREATE TRIGGER edge_qualification_no_update
            BEFORE UPDATE ON edge_qualification_events BEGIN
                SELECT RAISE(ABORT, 'qualification history is immutable');
            END;
        CREATE TRIGGER edge_qualification_no_delete
            BEFORE DELETE ON edge_qualification_events BEGIN
                SELECT RAISE(ABORT, 'qualification history is immutable');
            END;
    """)


def _migration_5(conn: sqlite3.Connection) -> None:
    """Make every T3 packet content-addressed and independently verifiable."""
    conn.execute("DROP TRIGGER IF EXISTS t3_packets_no_update")
    conn.execute(
        "ALTER TABLE t3_evidence_packets ADD COLUMN payload_hash TEXT")
    seen: dict[str, str] = {}
    for row in conn.execute(
            "SELECT packet_id, variant_id, review_status, reviewed_by, "
            "registry_change_ref, payload_json FROM t3_evidence_packets"):
        try:
            payload = json.loads(row["payload_json"])
            canonical, payload_hash = _t3_canonical(
                str(row["variant_id"]), payload, str(row["review_status"]),
                row["reviewed_by"], row["registry_change_ref"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MigrationError(
                f"T3 packet {row['packet_id']} has invalid JSON") from exc
        if payload_hash in seen:
            raise MigrationError(
                "duplicate historical T3 packet payloads prevent content "
                f"addressing: {seen[payload_hash]} and {row['packet_id']}")
        seen[payload_hash] = str(row["packet_id"])
        conn.execute(
            "UPDATE t3_evidence_packets SET payload_json=?, payload_hash=? "
            "WHERE packet_id=?", (canonical, payload_hash, row["packet_id"]))
    conn.execute(
        "CREATE UNIQUE INDEX t3_packet_payload_hash "
        "ON t3_evidence_packets (payload_hash)")
    _execute_script(conn, """
        CREATE TRIGGER t3_packets_no_update
            BEFORE UPDATE ON t3_evidence_packets BEGIN
                SELECT RAISE(ABORT, 'T3 evidence packets are immutable');
            END;
    """)


def _migration_6(conn: sqlite3.Connection) -> None:
    """Content-address analyses, merging valid historical duplicates safely."""
    conn.execute("ALTER TABLE analysis_runs ADD COLUMN payload_hash TEXT")
    rows = conn.execute(
        "SELECT analysis_id, kind, subject_id, payload_json "
        "FROM analysis_runs ORDER BY analysis_id").fetchall()
    records = {}
    for row in rows:
        analysis_id = str(row["analysis_id"])
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise MigrationError(
                f"analysis {analysis_id} has invalid JSON") from exc
        if not isinstance(payload, dict):
            raise MigrationError(
                f"analysis {analysis_id} payload is not a mapping")
        records[analysis_id] = {
            "kind": str(row["kind"]),
            "subject_id": str(row["subject_id"]),
            "payload": payload,
        }

    parent = {analysis_id: analysis_id for analysis_id in records}

    def find(analysis_id: str) -> str:
        while parent[analysis_id] != analysis_id:
            parent[analysis_id] = parent[parent[analysis_id]]
            analysis_id = parent[analysis_id]
        return analysis_id

    def union(left: str, right: str) -> bool:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return False
        keeper, duplicate = sorted((left_root, right_root))
        parent[duplicate] = keeper
        return True

    payloads = {analysis_id: record["payload"]
                for analysis_id, record in records.items()}
    for _ in range(len(records) + 2):
        remap = {analysis_id: find(analysis_id) for analysis_id in records}
        payloads = {
            analysis_id: _rewrite_analysis_references(payload, remap)
            for analysis_id, payload in payloads.items()
        }
        groups: dict[str, list[str]] = {}
        for analysis_id, record in records.items():
            _, payload_hash = _analysis_canonical(
                record["kind"], record["subject_id"], payloads[analysis_id])
            groups.setdefault(payload_hash, []).append(analysis_id)
        changed = False
        for members in groups.values():
            for duplicate in members[1:]:
                changed = union(members[0], duplicate) or changed
        if not changed:
            break
    else:
        raise MigrationError(
            "analysis reference deduplication did not converge")

    remap = {analysis_id: find(analysis_id) for analysis_id in records}
    payloads = {
        analysis_id: _rewrite_analysis_references(payload, remap)
        for analysis_id, payload in payloads.items()
    }

    _execute_script(conn, """
        DROP TRIGGER IF EXISTS edge_qualification_no_update;
        DROP TRIGGER IF EXISTS edge_qualification_no_delete;
        DROP TRIGGER IF EXISTS t3_packets_no_update;
        DROP TRIGGER IF EXISTS t3_packets_no_delete;
        DROP INDEX IF EXISTS t3_packet_payload_hash;
    """)

    for row in conn.execute(
            "SELECT event_id, source_analysis_id, detail_json "
            "FROM edge_qualification_events"):
        source_id = row["source_analysis_id"]
        if source_id is not None and str(source_id) not in records:
            raise MigrationError(
                f"qualification {row['event_id']} references missing analysis "
                f"{source_id}")
        try:
            detail = json.loads(row["detail_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise MigrationError(
                f"qualification {row['event_id']} has invalid detail JSON") \
                from exc
        conn.execute(
            "UPDATE edge_qualification_events SET source_analysis_id=?, "
            "detail_json=? WHERE event_id=?",
            (remap.get(str(source_id), str(source_id))
             if source_id is not None else None,
             _canonical_json(_rewrite_analysis_references(detail, remap)),
             row["event_id"]))

    packet_rows = conn.execute(
        "SELECT * FROM t3_evidence_packets ORDER BY packet_id").fetchall()
    packets = []
    for row in packet_rows:
        try:
            payload = json.loads(row["payload_json"])
            payload = _rewrite_analysis_references(payload, remap)
            canonical, payload_hash = _t3_canonical(
                str(row["variant_id"]), payload, str(row["review_status"]),
                row["reviewed_by"], row["registry_change_ref"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MigrationError(
                f"T3 packet {row['packet_id']} has invalid JSON") from exc
        packets.append((str(row["packet_id"]), canonical, payload_hash))
    packet_keepers: dict[str, str] = {}
    for packet_id, canonical, payload_hash in packets:
        keeper = packet_keepers.setdefault(payload_hash, packet_id)
        if keeper != packet_id:
            conn.execute(
                "DELETE FROM t3_evidence_packets WHERE packet_id=?",
                (packet_id,))
            continue
        conn.execute(
            "UPDATE t3_evidence_packets SET payload_json=?, payload_hash=? "
            "WHERE packet_id=?", (canonical, payload_hash, packet_id))

    for analysis_id, keeper in remap.items():
        if analysis_id != keeper:
            conn.execute(
                "DELETE FROM analysis_runs WHERE analysis_id=?", (analysis_id,))
    for analysis_id, record in records.items():
        if remap[analysis_id] != analysis_id:
            continue
        canonical, payload_hash = _analysis_canonical(
            record["kind"], record["subject_id"], payloads[analysis_id])
        conn.execute(
            "UPDATE analysis_runs SET payload_json=?, payload_hash=? "
            "WHERE analysis_id=?", (canonical, payload_hash, analysis_id))

    conn.execute(
        "CREATE UNIQUE INDEX analysis_payload_hash "
        "ON analysis_runs (payload_hash)")
    conn.execute(
        "CREATE UNIQUE INDEX t3_packet_payload_hash "
        "ON t3_evidence_packets (payload_hash)")
    _execute_script(conn, """
        CREATE TRIGGER edge_qualification_no_update
            BEFORE UPDATE ON edge_qualification_events BEGIN
                SELECT RAISE(ABORT, 'qualification history is immutable');
            END;
        CREATE TRIGGER edge_qualification_no_delete
            BEFORE DELETE ON edge_qualification_events BEGIN
                SELECT RAISE(ABORT, 'qualification history is immutable');
            END;
        CREATE TRIGGER t3_packets_no_update
            BEFORE UPDATE ON t3_evidence_packets BEGIN
                SELECT RAISE(ABORT, 'T3 evidence packets are immutable');
            END;
        CREATE TRIGGER t3_packets_no_delete
            BEFORE DELETE ON t3_evidence_packets BEGIN
                SELECT RAISE(ABORT, 'T3 evidence packets are immutable');
            END;
        CREATE TRIGGER analysis_runs_no_update
            BEFORE UPDATE ON analysis_runs BEGIN
                SELECT RAISE(ABORT, 'analyses are immutable');
            END;
        CREATE TRIGGER analysis_runs_no_delete
            BEFORE DELETE ON analysis_runs BEGIN
                SELECT RAISE(ABORT, 'analyses are immutable');
            END;
    """)


def _migration_7(conn: sqlite3.Connection) -> None:
    """Persist every arm decision so policy vetoes remain attributable."""
    _execute_script(conn, """
        CREATE TABLE paper_decisions (
            decision_id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            variant_id TEXT NOT NULL,
            cycle_id TEXT,
            symbol TEXT NOT NULL,
            direction TEXT,
            setup_type TEXT,
            signal_ts REAL,
            confidence REAL,
            decision_outcome TEXT NOT NULL
                CHECK (decision_outcome IN ('PROPOSED','VETOED')),
            reason TEXT,
            paper_trade_id TEXT,
            model_id TEXT NOT NULL,
            assumptions_json TEXT NOT NULL,
            proposal_json TEXT NOT NULL,
            decision_ts REAL NOT NULL,
            UNIQUE (scope_key, variant_id, proposal_id),
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id),
            FOREIGN KEY (paper_trade_id) REFERENCES paper_trades(trade_id));
        CREATE INDEX paper_decisions_variant_ts
            ON paper_decisions (scope_key, variant_id, decision_ts);
    """)


def _migration_8(conn: sqlite3.Connection) -> None:
    """Persist hypothesis identity and immutable adaptive proposals."""
    conn.execute("ALTER TABLE variants ADD COLUMN hypothesis_id TEXT")
    conn.execute("ALTER TABLE variants ADD COLUMN hypothesis_params_json TEXT NOT NULL DEFAULT '{}'")
    _execute_script(conn, """
        CREATE TABLE hypothesis_proposals (
            proposal_id TEXT PRIMARY KEY,
            strategy_id TEXT NOT NULL,
            hypothesis_id TEXT NOT NULL,
            setting_id TEXT NOT NULL,
            value_json TEXT NOT NULL,
            run_id TEXT NOT NULL,
            proposed_ts REAL NOT NULL,
            observation_until_ts REAL NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('PROPOSED','OBSERVED','REJECTED')),
            UNIQUE (strategy_id, hypothesis_id, setting_id, run_id)
        );
        CREATE INDEX hypothesis_proposals_lock
            ON hypothesis_proposals(strategy_id, hypothesis_id, proposed_ts);
        CREATE TRIGGER hypothesis_proposals_no_update
            BEFORE UPDATE ON hypothesis_proposals BEGIN
                SELECT RAISE(ABORT, 'hypothesis proposal history is immutable');
            END;
        CREATE TRIGGER hypothesis_proposals_no_delete
            BEFORE DELETE ON hypothesis_proposals BEGIN
                SELECT RAISE(ABORT, 'hypothesis proposal history is immutable');
            END;
    """)

    # Executed trades are the only historical decisions that can be recovered.
    # They are retained for audit, but the ledger watermark below deliberately
    # excludes them from new forward qualification because historical vetoes
    # were not stored and therefore cannot be reconstructed honestly.
    for row in conn.execute(
            "SELECT * FROM paper_trades ORDER BY entry_ts, trade_id"):
        raw_id = "\0".join((
            "legacy", str(row["scope_key"]), str(row["variant_id"]),
            str(row["proposal_id"])))
        decision_id = hashlib.sha256(raw_id.encode()).hexdigest()[:32]
        proposal = {
            "symbol": row["symbol"], "direction": row["direction"],
            "setup_type": row["setup_type"],
            "signal_ts": row["signal_ts"],
        }
        conn.execute(
            "INSERT OR IGNORE INTO paper_decisions (decision_id, proposal_id, "
            "scope_key, variant_id, cycle_id, symbol, direction, setup_type, "
            "signal_ts, confidence, decision_outcome, reason, "
            "paper_trade_id, model_id, assumptions_json, proposal_json, "
            "decision_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (decision_id, row["proposal_id"], row["scope_key"],
             row["variant_id"], row["cycle_id"], row["symbol"],
             row["direction"], row["setup_type"], row["signal_ts"], None,
             "PROPOSED", "legacy executed trade; veto ledger unavailable",
             row["trade_id"], row["model_id"], row["assumptions_json"],
             _canonical_json(proposal), row["entry_ts"]))

    ledger_started = time.time()
    for row in conn.execute(
            "SELECT scope_key, variant_id, state_json FROM paper_portfolios"):
        try:
            state = json.loads(row["state_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise MigrationError(
                f"paper portfolio {row['scope_key']}:{row['variant_id']} "
                "has invalid state JSON") from exc
        if not isinstance(state, dict):
            raise MigrationError(
                f"paper portfolio {row['scope_key']}:{row['variant_id']} "
                "state is not a mapping")
        state["decision_ledger_started_ts"] = ledger_started
        conn.execute(
            "UPDATE paper_portfolios SET state_json=? WHERE scope_key=? "
            "AND variant_id=?",
            (_canonical_json(state), row["scope_key"], row["variant_id"]))

    # Analyses created before complete veto persistence cannot support an edge.
    # Qualification history stays immutable: append a revocation rather than
    # mutating or deleting the original event.
    latest = {}
    for row in conn.execute(
            "SELECT * FROM edge_qualification_events ORDER BY ts, event_id"):
        latest[(str(row["variant_id"]), str(row["scope_key"]))] = row
    for (variant_id, scope_key), event in latest.items():
        if event["status"] != "QUALIFIED":
            continue
        analysis_id = event["source_analysis_id"]
        analysis = (conn.execute(
            "SELECT payload_json FROM analysis_runs WHERE analysis_id=?",
            (analysis_id,)).fetchone() if analysis_id else None)
        schema = None
        if analysis is not None:
            try:
                payload = json.loads(analysis["payload_json"])
                schema = (payload.get("source_evidence") or {}).get("schema")
            except (TypeError, json.JSONDecodeError, AttributeError):
                schema = None
        if schema == "paper_decision_evidence.v2":
            continue
        conn.execute(
            "INSERT INTO edge_qualification_events (event_id, variant_id, "
            "scope_key, status, ts, source_analysis_id, detail_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, variant_id, scope_key, "REVOKED",
             ledger_started, analysis_id, _canonical_json({
                 "source": "schema_migration_7",
                 "reason": (
                     "qualification predates complete persisted veto/accept "
                     "decision evidence; collect a new forward sample"),
             })))

    _execute_script(conn, """
        CREATE TRIGGER IF NOT EXISTS paper_decisions_no_update
            BEFORE UPDATE ON paper_decisions BEGIN
                SELECT RAISE(ABORT, 'paper decisions are immutable');
            END;
        CREATE TRIGGER IF NOT EXISTS paper_decisions_no_delete
            BEFORE DELETE ON paper_decisions BEGIN
                SELECT RAISE(ABORT, 'paper decisions are immutable');
            END;
    """)


def _migration_9(conn: sqlite3.Connection) -> None:
    """Make adaptive proposals exact, attributable, and lifecycle-complete."""
    conn.execute("DROP TRIGGER IF EXISTS hypothesis_proposals_no_update")
    conn.execute("DROP TRIGGER IF EXISTS hypothesis_proposals_no_delete")
    conn.execute("ALTER TABLE hypothesis_proposals ADD COLUMN variant_id TEXT")
    conn.execute(
        "ALTER TABLE hypothesis_proposals ADD COLUMN target_parameter TEXT")
    conn.execute(
        "ALTER TABLE hypothesis_proposals ADD COLUMN minimum_json TEXT")
    conn.execute(
        "ALTER TABLE hypothesis_proposals ADD COLUMN maximum_json TEXT")
    conn.execute("ALTER TABLE hypothesis_proposals ADD COLUMN reasoning TEXT")
    try:
        from agent import hypotheses as hypothesis_registry
    except ImportError:
        hypothesis_registry = None
    for row in conn.execute(
            "SELECT * FROM hypothesis_proposals "
            "ORDER BY proposed_ts, proposal_id").fetchall():
        metadata = None
        if hypothesis_registry is not None:
            try:
                metadata = hypothesis_registry.numeric_setting_metadata(
                    row["hypothesis_id"], row["setting_id"])
            except ValueError:
                metadata = None
        if metadata is None:
            target_parameter = f"legacy:{row['setting_id']}"
            minimum_json = row["value_json"]
            maximum_json = row["value_json"]
            reasoning = (
                "Legacy adaptive proposal migrated from schema 8; exact "
                "target metadata was not persisted and is no longer "
                "registered.")
        else:
            target_parameter = metadata["target_parameter"]
            minimum_json = _canonical_json(metadata["minimum"])
            maximum_json = _canonical_json(metadata["maximum"])
            reasoning = (
                "Legacy adaptive proposal migrated from schema 8; target "
                "parameter and semantic bounds recovered from registered "
                "hypothesis metadata.")
        conn.execute(
            "UPDATE hypothesis_proposals SET target_parameter=?, "
            "minimum_json=?, maximum_json=?, reasoning=? WHERE proposal_id=?",
            (target_parameter, minimum_json, maximum_json, reasoning,
             row["proposal_id"]))
    _execute_script(conn, """
        CREATE TABLE hypothesis_proposal_events (
            event_id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('PROPOSED','OBSERVING','OBSERVED','REJECTED')),
            event_ts REAL NOT NULL,
            detail_json TEXT NOT NULL,
            UNIQUE (proposal_id, status),
            FOREIGN KEY (proposal_id)
                REFERENCES hypothesis_proposals(proposal_id));
        CREATE INDEX hypothesis_proposal_events_order
            ON hypothesis_proposal_events(proposal_id, event_ts, event_id);
        CREATE TABLE hypothesis_proposal_value_locks (
            value_key TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL UNIQUE,
            strategy_id TEXT NOT NULL,
            hypothesis_id TEXT NOT NULL,
            target_parameter TEXT NOT NULL,
            value_json TEXT NOT NULL,
            created_ts REAL NOT NULL,
            FOREIGN KEY (proposal_id)
                REFERENCES hypothesis_proposals(proposal_id));
    """)
    for row in conn.execute(
            "SELECT * FROM hypothesis_proposals "
            "ORDER BY proposed_ts, proposal_id"):
        proposed_event_id = _content_hash({
            "schema": 9, "proposal_id": row["proposal_id"],
            "status": "PROPOSED",
        })[:32]
        conn.execute(
            "INSERT INTO hypothesis_proposal_events "
            "(event_id, proposal_id, status, event_ts, detail_json) "
            "VALUES (?,?,?,?,?)",
            (proposed_event_id, row["proposal_id"], "PROPOSED",
             row["proposed_ts"], _canonical_json({
                 "source": "schema_migration_9",
                 "legacy_status": row["status"],
             })))
        if row["status"] in {"OBSERVED", "REJECTED"}:
            terminal_event_id = _content_hash({
                "schema": 9, "proposal_id": row["proposal_id"],
                "status": row["status"],
            })[:32]
            conn.execute(
                "INSERT INTO hypothesis_proposal_events "
                "(event_id, proposal_id, status, event_ts, detail_json) "
                "VALUES (?,?,?,?,?)",
                (terminal_event_id, row["proposal_id"], row["status"],
                 row["observation_until_ts"], _canonical_json({
                     "source": "schema_migration_9",
                     "reason": "legacy terminal status",
                 })))
        value_key = _content_hash({
            "strategy_id": row["strategy_id"],
            "hypothesis_id": row["hypothesis_id"],
            "target_parameter": row["target_parameter"],
            "value": json.loads(row["value_json"]),
        })
        conn.execute(
            "INSERT OR IGNORE INTO hypothesis_proposal_value_locks "
            "(value_key, proposal_id, strategy_id, hypothesis_id, "
            "target_parameter, value_json, created_ts) VALUES (?,?,?,?,?,?,?)",
            (value_key, row["proposal_id"], row["strategy_id"],
             row["hypothesis_id"], row["target_parameter"],
             row["value_json"], row["proposed_ts"]))
    _execute_script(conn, """
        CREATE TRIGGER hypothesis_proposals_require_registered_variant
            BEFORE INSERT ON hypothesis_proposals
            WHEN NEW.variant_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM variants
                WHERE variant_id=NEW.variant_id
                  AND strategy_id=NEW.strategy_id
                  AND hypothesis_id=NEW.hypothesis_id)
            BEGIN
                SELECT RAISE(ABORT, 'adaptive proposal requires its exact registered variant');
            END;
        CREATE TRIGGER hypothesis_proposals_no_update
            BEFORE UPDATE ON hypothesis_proposals BEGIN
                SELECT RAISE(ABORT, 'hypothesis proposal history is immutable');
            END;
        CREATE TRIGGER hypothesis_proposals_no_delete
            BEFORE DELETE ON hypothesis_proposals BEGIN
                SELECT RAISE(ABORT, 'hypothesis proposal history is immutable');
            END;
        CREATE TRIGGER hypothesis_proposal_events_no_update
            BEFORE UPDATE ON hypothesis_proposal_events BEGIN
                SELECT RAISE(ABORT, 'hypothesis proposal events are immutable');
            END;
        CREATE TRIGGER hypothesis_proposal_events_no_delete
            BEFORE DELETE ON hypothesis_proposal_events BEGIN
                SELECT RAISE(ABORT, 'hypothesis proposal events are immutable');
            END;
        CREATE TRIGGER hypothesis_proposal_value_locks_no_update
            BEFORE UPDATE ON hypothesis_proposal_value_locks BEGIN
                SELECT RAISE(ABORT, 'hypothesis proposal value locks are immutable');
            END;
        CREATE TRIGGER hypothesis_proposal_value_locks_no_delete
            BEFORE DELETE ON hypothesis_proposal_value_locks BEGIN
                SELECT RAISE(ABORT, 'hypothesis proposal value locks are immutable');
            END;
    """)


def _migration_10(conn: sqlite3.Connection) -> None:
    """Add durable, append-only one-setting-per-strategy rotation."""
    _execute_script(conn, """
        CREATE TABLE strategy_experiment_assignments (
            assignment_id TEXT PRIMARY KEY,
            scope_key TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            baseline_variant_id TEXT NOT NULL,
            candidate_variant_id TEXT NOT NULL,
            candidate_key TEXT NOT NULL,
            axis TEXT NOT NULL,
            setting_id TEXT NOT NULL,
            setting_json TEXT NOT NULL,
            source TEXT NOT NULL CHECK (source IN ('static','adaptive')),
            proposal_id TEXT,
            started_ts REAL NOT NULL,
            minimum_duration_seconds REAL NOT NULL CHECK (
                minimum_duration_seconds >= 0),
            minimum_observations INTEGER NOT NULL CHECK (
                minimum_observations > 0),
            code_identity_json TEXT NOT NULL,
            config_identity_json TEXT NOT NULL,
            FOREIGN KEY (baseline_variant_id)
                REFERENCES variants(variant_id),
            FOREIGN KEY (candidate_variant_id)
                REFERENCES variants(variant_id),
            FOREIGN KEY (proposal_id)
                REFERENCES hypothesis_proposals(proposal_id));
        CREATE INDEX strategy_experiment_assignments_scope
            ON strategy_experiment_assignments(
                scope_key, strategy_id, started_ts, assignment_id);
        CREATE INDEX strategy_experiment_assignments_candidate
            ON strategy_experiment_assignments(
                scope_key, strategy_id, candidate_variant_id, candidate_key);

        CREATE TABLE strategy_experiment_active_slots (
            scope_key TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            assignment_id TEXT NOT NULL UNIQUE,
            PRIMARY KEY (scope_key, strategy_id),
            FOREIGN KEY (assignment_id)
                REFERENCES strategy_experiment_assignments(assignment_id));

        CREATE TABLE strategy_experiment_assignment_events (
            event_id TEXT PRIMARY KEY,
            assignment_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('STARTED','OBSERVING','COMPLETED','REJECTED')),
            event_ts REAL NOT NULL,
            reason TEXT,
            detail_json TEXT NOT NULL,
            UNIQUE (assignment_id, status),
            FOREIGN KEY (assignment_id)
                REFERENCES strategy_experiment_assignments(assignment_id));
        CREATE INDEX strategy_experiment_assignment_events_order
            ON strategy_experiment_assignment_events(
                assignment_id, event_ts, event_id);

        CREATE TABLE strategy_experiment_observations (
            observation_id TEXT PRIMARY KEY,
            assignment_id TEXT NOT NULL,
            observation_key TEXT NOT NULL,
            observed_ts REAL NOT NULL,
            detail_json TEXT NOT NULL,
            UNIQUE (assignment_id, observation_key),
            FOREIGN KEY (assignment_id)
                REFERENCES strategy_experiment_assignments(assignment_id));
        CREATE INDEX strategy_experiment_observations_order
            ON strategy_experiment_observations(
                assignment_id, observed_ts, observation_id);

        CREATE TRIGGER strategy_experiment_assignments_no_update
            BEFORE UPDATE ON strategy_experiment_assignments BEGIN
                SELECT RAISE(ABORT, 'experiment assignments are immutable');
            END;
        CREATE TRIGGER strategy_experiment_assignments_no_delete
            BEFORE DELETE ON strategy_experiment_assignments BEGIN
                SELECT RAISE(ABORT, 'experiment assignments are immutable');
            END;
        CREATE TRIGGER strategy_experiment_assignment_events_no_update
            BEFORE UPDATE ON strategy_experiment_assignment_events BEGIN
                SELECT RAISE(ABORT, 'experiment assignment events are immutable');
            END;
        CREATE TRIGGER strategy_experiment_assignment_events_no_delete
            BEFORE DELETE ON strategy_experiment_assignment_events BEGIN
                SELECT RAISE(ABORT, 'experiment assignment events are immutable');
            END;
        CREATE TRIGGER strategy_experiment_observations_no_update
            BEFORE UPDATE ON strategy_experiment_observations BEGIN
                SELECT RAISE(ABORT, 'experiment observations are immutable');
            END;
        CREATE TRIGGER strategy_experiment_observations_no_delete
            BEFORE DELETE ON strategy_experiment_observations BEGIN
                SELECT RAISE(ABORT, 'experiment observations are immutable');
            END;
    """)
    migration_ts = time.time()
    for row in conn.execute(
            "SELECT proposal.* FROM hypothesis_proposals AS proposal "
            "WHERE proposal.variant_id IS NULL AND COALESCE(("
            "SELECT event.status FROM hypothesis_proposal_events AS event "
            "WHERE event.proposal_id=proposal.proposal_id "
            "ORDER BY event.event_ts DESC, event.rowid DESC LIMIT 1"
            "), proposal.status) NOT IN ('OBSERVED','REJECTED') "
            "ORDER BY proposal.proposed_ts, proposal.proposal_id"):
        conn.execute(
            "INSERT INTO hypothesis_proposal_events "
            "(event_id, proposal_id, status, event_ts, detail_json) "
            "VALUES (?,?,?,?,?)",
            (_content_hash({
                "schema": 10,
                "proposal_id": row["proposal_id"],
                "status": "REJECTED",
            })[:32], row["proposal_id"], "REJECTED", migration_ts,
             _canonical_json({
                 "source": "schema_migration_10",
                 "reason": (
                     "legacy proposal has no exact registered variant and "
                     "cannot enter durable experiment rotation"),
             })))


def _migration_11(conn: sqlite3.Connection) -> None:
    """Add an append-only LLM research-selection and assignment ledger."""
    _execute_script(conn, """
        CREATE TABLE research_selections (
            selection_id TEXT PRIMARY KEY,
            scope_key TEXT NOT NULL,
            run_id TEXT NOT NULL,
            cycle_id TEXT,
            model_id TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            requested_strategy_id TEXT NOT NULL,
            requested_variant_id TEXT,
            resolved_variant_id TEXT,
            original_reasoning TEXT NOT NULL,
            request_json TEXT NOT NULL,
            requested_ts REAL NOT NULL,
            FOREIGN KEY (resolved_variant_id) REFERENCES variants(variant_id));
        CREATE INDEX research_selections_scope
            ON research_selections(
                scope_key, requested_strategy_id, requested_ts, selection_id);
        CREATE INDEX research_selections_resolved
            ON research_selections(
                scope_key, requested_strategy_id, resolved_variant_id);

        CREATE TABLE research_selection_events (
            event_id TEXT PRIMARY KEY,
            selection_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('ACCEPTED','ASSIGNED','TESTED','REJECTED')),
            event_ts REAL NOT NULL,
            reason TEXT,
            assignment_id TEXT,
            detail_json TEXT NOT NULL,
            UNIQUE (selection_id, status),
            FOREIGN KEY (selection_id)
                REFERENCES research_selections(selection_id),
            FOREIGN KEY (assignment_id)
                REFERENCES strategy_experiment_assignments(assignment_id));
        CREATE INDEX research_selection_events_order
            ON research_selection_events(selection_id, event_ts, event_id);
        CREATE INDEX research_selection_events_assignment
            ON research_selection_events(assignment_id, status);

        CREATE TRIGGER research_selections_no_update
            BEFORE UPDATE ON research_selections BEGIN
                SELECT RAISE(ABORT, 'research selections are immutable');
            END;
        CREATE TRIGGER research_selections_no_delete
            BEFORE DELETE ON research_selections BEGIN
                SELECT RAISE(ABORT, 'research selections are immutable');
            END;
        CREATE TRIGGER research_selection_events_no_update
            BEFORE UPDATE ON research_selection_events BEGIN
                SELECT RAISE(ABORT, 'research selection events are immutable');
            END;
        CREATE TRIGGER research_selection_events_no_delete
            BEFORE DELETE ON research_selection_events BEGIN
                SELECT RAISE(ABORT, 'research selection events are immutable');
            END;
    """)


def _migration_12(conn: sqlite3.Connection) -> None:
    """Close terminal experiments into outcomes, evidence, and LLM reviews."""
    _execute_script(conn, """
        CREATE TABLE experiment_outcomes (
            outcome_id TEXT PRIMARY KEY,
            assignment_id TEXT NOT NULL UNIQUE,
            scope_key TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            baseline_variant_id TEXT NOT NULL,
            candidate_variant_id TEXT NOT NULL,
            terminal_status TEXT NOT NULL CHECK (
                terminal_status IN ('COMPLETED','REJECTED')),
            verdict TEXT NOT NULL CHECK (
                verdict IN ('WORKED','FAILED','INCONCLUSIVE')),
            created_ts REAL NOT NULL,
            payload_json TEXT NOT NULL,
            payload_hash TEXT NOT NULL UNIQUE,
            analysis_id TEXT NOT NULL UNIQUE,
            finding_id INTEGER NOT NULL UNIQUE,
            FOREIGN KEY (assignment_id)
                REFERENCES strategy_experiment_assignments(assignment_id),
            FOREIGN KEY (baseline_variant_id) REFERENCES variants(variant_id),
            FOREIGN KEY (candidate_variant_id) REFERENCES variants(variant_id),
            FOREIGN KEY (analysis_id) REFERENCES analysis_runs(analysis_id),
            FOREIGN KEY (finding_id) REFERENCES findings(finding_id));
        CREATE INDEX experiment_outcomes_pending_review
            ON experiment_outcomes(created_ts, outcome_id);

        CREATE TABLE research_edge_evidence (
            edge_id TEXT PRIMARY KEY,
            outcome_id TEXT NOT NULL UNIQUE,
            assignment_id TEXT NOT NULL UNIQUE,
            scope_key TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            variant_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status='EDGE_CANDIDATE'),
            created_ts REAL NOT NULL,
            evidence_json TEXT NOT NULL,
            evidence_hash TEXT NOT NULL UNIQUE,
            FOREIGN KEY (outcome_id) REFERENCES experiment_outcomes(outcome_id),
            FOREIGN KEY (assignment_id)
                REFERENCES strategy_experiment_assignments(assignment_id),
            FOREIGN KEY (variant_id) REFERENCES variants(variant_id));

        CREATE TABLE experiment_review_attempts (
            attempt_id TEXT PRIMARY KEY,
            outcome_id TEXT NOT NULL,
            requested_ts REAL NOT NULL,
            completed_ts REAL NOT NULL,
            provider TEXT NOT NULL,
            model_id TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            request_json TEXT NOT NULL,
            raw_response TEXT,
            response_json TEXT,
            parse_error TEXT,
            status TEXT NOT NULL CHECK (status IN ('SUCCEEDED','FAILED')),
            FOREIGN KEY (outcome_id) REFERENCES experiment_outcomes(outcome_id));
        CREATE INDEX experiment_review_attempts_outcome
            ON experiment_review_attempts(outcome_id, requested_ts, attempt_id);

        CREATE TABLE experiment_reviews (
            review_id TEXT PRIMARY KEY,
            outcome_id TEXT NOT NULL UNIQUE,
            attempt_id TEXT NOT NULL UNIQUE,
            created_ts REAL NOT NULL,
            explanation TEXT NOT NULL,
            limitations_json TEXT NOT NULL,
            next_selection_json TEXT,
            selection_id TEXT,
            FOREIGN KEY (outcome_id) REFERENCES experiment_outcomes(outcome_id),
            FOREIGN KEY (attempt_id)
                REFERENCES experiment_review_attempts(attempt_id),
            FOREIGN KEY (selection_id) REFERENCES research_selections(selection_id));

        CREATE TRIGGER experiment_outcomes_no_update
            BEFORE UPDATE ON experiment_outcomes BEGIN
                SELECT RAISE(ABORT, 'experiment outcomes are immutable');
            END;
        CREATE TRIGGER experiment_outcomes_no_delete
            BEFORE DELETE ON experiment_outcomes BEGIN
                SELECT RAISE(ABORT, 'experiment outcomes are immutable');
            END;
        CREATE TRIGGER research_edge_evidence_no_update
            BEFORE UPDATE ON research_edge_evidence BEGIN
                SELECT RAISE(ABORT, 'research edge evidence is immutable');
            END;
        CREATE TRIGGER research_edge_evidence_no_delete
            BEFORE DELETE ON research_edge_evidence BEGIN
                SELECT RAISE(ABORT, 'research edge evidence is immutable');
            END;
        CREATE TRIGGER experiment_review_attempts_no_update
            BEFORE UPDATE ON experiment_review_attempts BEGIN
                SELECT RAISE(ABORT, 'experiment review attempts are immutable');
            END;
        CREATE TRIGGER experiment_review_attempts_no_delete
            BEFORE DELETE ON experiment_review_attempts BEGIN
                SELECT RAISE(ABORT, 'experiment review attempts are immutable');
            END;
        CREATE TRIGGER experiment_reviews_no_update
            BEFORE UPDATE ON experiment_reviews BEGIN
                SELECT RAISE(ABORT, 'experiment reviews are immutable');
            END;
        CREATE TRIGGER experiment_reviews_no_delete
            BEFORE DELETE ON experiment_reviews BEGIN
                SELECT RAISE(ABORT, 'experiment reviews are immutable');
            END;
    """)
    terminal = conn.execute(
        "SELECT assignment.assignment_id "
        "FROM strategy_experiment_assignments AS assignment "
        "WHERE (SELECT event.status "
        "FROM strategy_experiment_assignment_events AS event "
        "WHERE event.assignment_id=assignment.assignment_id "
        "ORDER BY event.event_ts DESC, event.rowid DESC LIMIT 1) "
        "IN ('COMPLETED','REJECTED') "
        "ORDER BY assignment.started_ts, assignment.assignment_id"
    ).fetchall()
    for row in terminal:
        _ensure_experiment_outcome_conn(conn, str(row["assignment_id"]))


def _migration_13(conn: sqlite3.Connection) -> None:
    """Add immutable tournament evidence and verified backup history."""
    _execute_script(conn, """
        CREATE TABLE tournament_runs (
            run_id TEXT PRIMARY KEY,
            content_id TEXT NOT NULL,
            started_ts REAL NOT NULL,
            run_directory TEXT NOT NULL UNIQUE,
            command_json TEXT NOT NULL,
            options_json TEXT NOT NULL,
            code_identity_json TEXT NOT NULL,
            config_identity_json TEXT NOT NULL,
            data_manifest_identity_json TEXT NOT NULL,
            input_hashes_json TEXT NOT NULL);
        CREATE INDEX tournament_runs_content
            ON tournament_runs(content_id, started_ts, run_id);

        CREATE TABLE tournament_run_events (
            event_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('STARTED','SUCCEEDED','FAILED')),
            event_ts REAL NOT NULL,
            detail_json TEXT NOT NULL,
            UNIQUE (run_id, status),
            FOREIGN KEY (run_id) REFERENCES tournament_runs(run_id));
        CREATE INDEX tournament_run_events_order
            ON tournament_run_events(run_id, event_ts, event_id);

        CREATE TABLE tournament_strategy_results (
            strategy_result_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            version TEXT,
            scored INTEGER NOT NULL CHECK (scored IN (0,1)),
            registered_tier TEXT,
            measured_tier TEXT,
            verdict TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            result_json TEXT NOT NULL,
            UNIQUE (run_id, strategy_id),
            FOREIGN KEY (run_id) REFERENCES tournament_runs(run_id));
        CREATE INDEX tournament_strategy_results_run
            ON tournament_strategy_results(run_id, strategy_id);

        CREATE TABLE tournament_setting_results (
            setting_result_id TEXT PRIMARY KEY,
            strategy_result_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            setting_id TEXT NOT NULL,
            registered INTEGER NOT NULL CHECK (registered IN (0,1)),
            verdict TEXT NOT NULL,
            params_json TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            result_json TEXT NOT NULL,
            UNIQUE (run_id, strategy_id, setting_id),
            FOREIGN KEY (strategy_result_id)
                REFERENCES tournament_strategy_results(strategy_result_id),
            FOREIGN KEY (run_id) REFERENCES tournament_runs(run_id));
        CREATE INDEX tournament_setting_results_run
            ON tournament_setting_results(run_id, strategy_id, setting_id);

        CREATE TABLE backup_runs (
            backup_id TEXT PRIMARY KEY,
            request_content_id TEXT NOT NULL,
            started_ts REAL NOT NULL,
            target_root TEXT NOT NULL,
            target_kind TEXT NOT NULL CHECK (
                target_kind IN ('local_default','external_configured')),
            require_external INTEGER NOT NULL CHECK (
                require_external IN (0,1)),
            command_json TEXT NOT NULL,
            options_json TEXT NOT NULL,
            source_paths_json TEXT NOT NULL);
        CREATE INDEX backup_runs_target
            ON backup_runs(target_kind, started_ts, backup_id);

        CREATE TABLE backup_run_events (
            event_id TEXT PRIMARY KEY,
            backup_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('STARTED','VERIFIED','FAILED')),
            event_ts REAL NOT NULL,
            detail_json TEXT NOT NULL,
            UNIQUE (backup_id, status),
            FOREIGN KEY (backup_id) REFERENCES backup_runs(backup_id));
        CREATE INDEX backup_run_events_order
            ON backup_run_events(backup_id, event_ts, event_id);

        CREATE TABLE backup_files (
            backup_file_id TEXT PRIMARY KEY,
            backup_id TEXT NOT NULL,
            archive_path TEXT NOT NULL,
            source_path TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('sqlite','file')),
            size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
            sha256 TEXT NOT NULL,
            verification_json TEXT NOT NULL,
            UNIQUE (backup_id, archive_path),
            FOREIGN KEY (backup_id) REFERENCES backup_runs(backup_id));
        CREATE INDEX backup_files_backup
            ON backup_files(backup_id, archive_path);

        CREATE TABLE backup_verifications (
            verification_id TEXT PRIMARY KEY,
            backup_id TEXT,
            backup_path TEXT NOT NULL,
            verified_ts REAL NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('VERIFIED','FAILED')),
            detail_json TEXT NOT NULL,
            FOREIGN KEY (backup_id) REFERENCES backup_runs(backup_id));
        CREATE INDEX backup_verifications_backup
            ON backup_verifications(backup_id, verified_ts, verification_id);

        CREATE TRIGGER tournament_runs_no_update
            BEFORE UPDATE ON tournament_runs BEGIN
                SELECT RAISE(ABORT, 'tournament runs are immutable');
            END;
        CREATE TRIGGER tournament_runs_no_delete
            BEFORE DELETE ON tournament_runs BEGIN
                SELECT RAISE(ABORT, 'tournament runs are immutable');
            END;
        CREATE TRIGGER tournament_run_events_no_update
            BEFORE UPDATE ON tournament_run_events BEGIN
                SELECT RAISE(ABORT, 'tournament run events are immutable');
            END;
        CREATE TRIGGER tournament_run_events_no_delete
            BEFORE DELETE ON tournament_run_events BEGIN
                SELECT RAISE(ABORT, 'tournament run events are immutable');
            END;
        CREATE TRIGGER tournament_strategy_results_no_update
            BEFORE UPDATE ON tournament_strategy_results BEGIN
                SELECT RAISE(ABORT, 'tournament strategy results are immutable');
            END;
        CREATE TRIGGER tournament_strategy_results_no_delete
            BEFORE DELETE ON tournament_strategy_results BEGIN
                SELECT RAISE(ABORT, 'tournament strategy results are immutable');
            END;
        CREATE TRIGGER tournament_setting_results_no_update
            BEFORE UPDATE ON tournament_setting_results BEGIN
                SELECT RAISE(ABORT, 'tournament setting results are immutable');
            END;
        CREATE TRIGGER tournament_setting_results_no_delete
            BEFORE DELETE ON tournament_setting_results BEGIN
                SELECT RAISE(ABORT, 'tournament setting results are immutable');
            END;
        CREATE TRIGGER backup_runs_no_update
            BEFORE UPDATE ON backup_runs BEGIN
                SELECT RAISE(ABORT, 'backup runs are immutable');
            END;
        CREATE TRIGGER backup_runs_no_delete
            BEFORE DELETE ON backup_runs BEGIN
                SELECT RAISE(ABORT, 'backup runs are immutable');
            END;
        CREATE TRIGGER backup_run_events_no_update
            BEFORE UPDATE ON backup_run_events BEGIN
                SELECT RAISE(ABORT, 'backup run events are immutable');
            END;
        CREATE TRIGGER backup_run_events_no_delete
            BEFORE DELETE ON backup_run_events BEGIN
                SELECT RAISE(ABORT, 'backup run events are immutable');
            END;
        CREATE TRIGGER backup_files_no_update
            BEFORE UPDATE ON backup_files BEGIN
                SELECT RAISE(ABORT, 'backup files are immutable');
            END;
        CREATE TRIGGER backup_files_no_delete
            BEFORE DELETE ON backup_files BEGIN
                SELECT RAISE(ABORT, 'backup files are immutable');
            END;
        CREATE TRIGGER backup_verifications_no_update
            BEFORE UPDATE ON backup_verifications BEGIN
                SELECT RAISE(ABORT, 'backup verifications are immutable');
            END;
        CREATE TRIGGER backup_verifications_no_delete
            BEFORE DELETE ON backup_verifications BEGIN
                SELECT RAISE(ABORT, 'backup verifications are immutable');
            END;
    """)


def _migration_14(conn: sqlite3.Connection) -> None:
    """Only call a configured backup external with different-device proof."""
    _execute_script(conn, """
        DROP TRIGGER backup_runs_no_update;
        DROP TRIGGER backup_runs_no_delete;
        DROP INDEX backup_runs_target;

        CREATE TABLE backup_runs_v14 (
            backup_id TEXT PRIMARY KEY,
            request_content_id TEXT NOT NULL,
            started_ts REAL NOT NULL,
            target_root TEXT NOT NULL,
            target_kind TEXT NOT NULL CHECK (
                target_kind IN (
                    'local_default','configured_local','external_mounted')),
            target_evidence_json TEXT NOT NULL,
            require_external INTEGER NOT NULL CHECK (
                require_external IN (0,1)),
            command_json TEXT NOT NULL,
            options_json TEXT NOT NULL,
            source_paths_json TEXT NOT NULL);
    """)
    rows = conn.execute(
        "SELECT * FROM backup_runs ORDER BY started_ts, backup_id").fetchall()
    for row in rows:
        legacy_kind = str(row["target_kind"])
        target_kind = (
            "local_default" if legacy_kind == "local_default"
            else "configured_local")
        evidence = {
            "external_proven": False,
            "method": "schema_migration_14",
            "reason": (
                "legacy configured targets had no different-device proof; "
                "reclassified as configured_local"
            ),
        }
        conn.execute(
            "INSERT INTO backup_runs_v14 (backup_id, request_content_id, "
            "started_ts, target_root, target_kind, target_evidence_json, "
            "require_external, command_json, options_json, "
            "source_paths_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (row["backup_id"], row["request_content_id"], row["started_ts"],
             row["target_root"], target_kind, _canonical_json(evidence),
             row["require_external"], row["command_json"],
             row["options_json"], row["source_paths_json"]))
    _execute_script(conn, """
        DROP TABLE backup_runs;
        ALTER TABLE backup_runs_v14 RENAME TO backup_runs;

        CREATE INDEX backup_runs_target
            ON backup_runs(target_kind, started_ts, backup_id);
        CREATE TRIGGER backup_runs_no_update
            BEFORE UPDATE ON backup_runs BEGIN
                SELECT RAISE(ABORT, 'backup runs are immutable');
            END;
        CREATE TRIGGER backup_runs_no_delete
            BEFORE DELETE ON backup_runs BEGIN
                SELECT RAISE(ABORT, 'backup runs are immutable');
            END;
    """)


def _migration_15(conn: sqlite3.Connection) -> None:
    """Add explicit experiment draining and immutable retry lineage."""
    conn.execute(
        "ALTER TABLE strategy_experiment_assignments "
        "ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0)")
    conn.execute(
        "ALTER TABLE strategy_experiment_assignments "
        "ADD COLUMN retry_of_assignment_id TEXT REFERENCES "
        "strategy_experiment_assignments(assignment_id)")
    _execute_script(conn, """
        DROP TRIGGER IF EXISTS strategy_experiment_assignment_events_no_update;
        DROP TRIGGER IF EXISTS strategy_experiment_assignment_events_no_delete;
        DROP INDEX IF EXISTS strategy_experiment_assignment_events_order;

        CREATE TABLE strategy_experiment_assignment_events_v15 (
            event_id TEXT PRIMARY KEY,
            assignment_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN (
                    'STARTED','OBSERVING','DRAINING','COMPLETED','REJECTED')),
            event_ts REAL NOT NULL,
            reason TEXT,
            detail_json TEXT NOT NULL,
            UNIQUE (assignment_id, status),
            FOREIGN KEY (assignment_id)
                REFERENCES strategy_experiment_assignments(assignment_id));
        INSERT INTO strategy_experiment_assignment_events_v15
            (event_id, assignment_id, status, event_ts, reason, detail_json)
        SELECT event_id, assignment_id, status, event_ts, reason, detail_json
        FROM strategy_experiment_assignment_events;
        DROP TABLE strategy_experiment_assignment_events;
        ALTER TABLE strategy_experiment_assignment_events_v15
            RENAME TO strategy_experiment_assignment_events;
        CREATE INDEX strategy_experiment_assignment_events_order
            ON strategy_experiment_assignment_events(
                assignment_id, event_ts, event_id);
        CREATE TRIGGER strategy_experiment_assignment_events_no_update
            BEFORE UPDATE ON strategy_experiment_assignment_events BEGIN
                SELECT RAISE(ABORT, 'experiment assignment events are immutable');
            END;
        CREATE TRIGGER strategy_experiment_assignment_events_no_delete
            BEFORE DELETE ON strategy_experiment_assignment_events BEGIN
                SELECT RAISE(ABORT, 'experiment assignment events are immutable');
            END;
    """)


def _migration_16(conn: sqlite3.Connection) -> None:
    """Persist simulated execution evidence and inference validity."""
    conn.execute(
        "ALTER TABLE paper_trades ADD COLUMN execution_json TEXT")
    conn.execute(
        "ALTER TABLE paper_trades ADD COLUMN valid_for_inference INTEGER "
        "NOT NULL DEFAULT 1 CHECK (valid_for_inference IN (0,1))")


MIGRATIONS = {
    1: ("create_initial_store", _migration_1),
    2: ("rebuild_legacy_constraints", _migration_2),
    3: ("paper_research_and_t3_evidence", _migration_3),
    4: ("scoped_paper_identity_and_qualification", _migration_4),
    5: ("content_addressed_t3_packets", _migration_5),
    6: ("content_addressed_analyses", _migration_6),
    7: ("complete_paper_decision_ledger", _migration_7),
    8: ("hypothesis_identity_and_adaptive_proposals", _migration_8),
    9: ("exact_adaptive_proposal_lifecycle", _migration_9),
    10: ("durable_strategy_experiment_rotation", _migration_10),
    11: ("bounded_llm_research_selection", _migration_11),
    12: ("deterministic_experiment_learning_loop", _migration_12),
    13: ("durable_tournament_and_verified_backups", _migration_13),
    14: ("verified_external_mount_classification", _migration_14),
    15: ("experiment_draining_and_retry_lineage", _migration_15),
    16: ("paper_execution_evidence_and_validity", _migration_16),
}


def _validate_schema(conn: sqlite3.Connection) -> None:
    required = {
        "schema_meta", "schema_migrations", "variants", "variant_runs",
        "variant_results", "findings", "variant_scheduler",
        "paper_portfolios", "paper_trades", "paper_failures",
        "run_evidence", "analysis_runs", "t3_evidence_packets",
        "edge_qualification_events",
    }
    if _stored_version(conn) >= 7:
        required.add("paper_decisions")
    if _stored_version(conn) >= 8:
        required.add("hypothesis_proposals")
    if _stored_version(conn) >= 9:
        required.update({"hypothesis_proposal_events",
                         "hypothesis_proposal_value_locks"})
    if _stored_version(conn) >= 10:
        required.update({
            "strategy_experiment_assignments",
            "strategy_experiment_active_slots",
            "strategy_experiment_assignment_events",
            "strategy_experiment_observations",
        })
    if _stored_version(conn) >= 11:
        required.update({"research_selections", "research_selection_events"})
    if _stored_version(conn) >= 12:
        required.update({
            "experiment_outcomes", "research_edge_evidence",
            "experiment_review_attempts", "experiment_reviews",
        })
    if _stored_version(conn) >= 13:
        required.update({
            "tournament_runs", "tournament_run_events",
            "tournament_strategy_results", "tournament_setting_results",
            "backup_runs", "backup_run_events", "backup_files",
            "backup_verifications",
        })
    missing = sorted(table for table in required
                     if not _table_exists(conn, table))
    if missing:
        raise MigrationError(
            "findings schema claims to be current but is missing: "
            + ", ".join(missing))


def _migrate(conn: sqlite3.Connection) -> None:
    current = _stored_version(conn)
    if current > SCHEMA_VERSION:
        raise MigrationError(
            f"findings.db schema {current} is newer than supported "
            f"schema {SCHEMA_VERSION}; downgrade refused")
    if current == SCHEMA_VERSION:
        _validate_schema(conn)
        return
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "applied_ts REAL NOT NULL)"
        )
        if current:
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations "
                "(version, name, applied_ts) VALUES (?,?,?)",
                (current, MIGRATIONS[current][0], 0.0))
        for version in range(current + 1, SCHEMA_VERSION + 1):
            name, migrate = MIGRATIONS[version]
            migrate(conn)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) "
                "VALUES ('schema_version', ?)", (str(version),))
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_ts) "
                "VALUES (?,?,?)", (version, name, time.time()))
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise MigrationError(
                f"findings migration would leave {len(violations)} foreign-key "
                "violation(s); history was not modified")
        _validate_schema(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


class FindingsStore:
    def __init__(self, path: str | Path = DEFAULT_STORE) -> None:
        self.path = Path(path)

    @property
    def backup_path(self) -> Path:
        root = self.path.with_name(f"{self.path.name}.snapshots")
        snapshots = sorted(root.glob("*.db")) if root.is_dir() else []
        if snapshots:
            return snapshots[-1]
        return self.path.with_name(f"{self.path.name}.backup")

    def backup(self, destination: str | Path | None = None) -> Path:
        """Write one immutable findings-only online snapshot.

        The full recorder/journal/results backup lives in ``research.backup``.
        This compatibility method remains for call sites that want an
        immediate findings snapshot, but it never overwrites an older one.
        """
        if destination is not None:
            target = Path(destination)
            if target.exists():
                raise FileExistsError(f"backup destination exists: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = target
        else:
            root = self.path.with_name(f"{self.path.name}.snapshots")
            root.mkdir(parents=True, exist_ok=True)
            staging = root / f".incomplete-{time.time_ns()}-{uuid.uuid4().hex}.db"
        with _connect(self.path) as source, sqlite3.connect(
                staging, factory=_ClosingConnection) as copy:
            source.backup(copy)
        with sqlite3.connect(
                f"file:{staging}?mode=ro", uri=True,
                factory=_ClosingConnection) as check:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("findings snapshot failed integrity_check")
            if check.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("findings snapshot failed foreign_key_check")
        if destination is None:
            hasher = hashlib.sha256()
            with staging.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
            digest = hasher.hexdigest()
            target = staging.with_name(
                f"{time.time_ns()}-{digest[:16]}-{uuid.uuid4().hex[:12]}.db")
            staging.rename(target)
        return target

    def schema_version(self) -> int:
        with _connect(self.path) as conn:
            return _stored_version(conn)

    def migration_history(self) -> list:
        with _connect(self.path) as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM schema_migrations ORDER BY version")]

    # ---------------------------------------- tournament and backup evidence

    def start_tournament_run(self, run: dict) -> None:
        """Append one immutable tournament invocation and its start event."""
        with _connect(self.path) as conn:
            conn.execute(
                "INSERT INTO tournament_runs (run_id, content_id, started_ts, "
                "run_directory, command_json, options_json, "
                "code_identity_json, config_identity_json, "
                "data_manifest_identity_json, input_hashes_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (str(run["run_id"]), str(run["content_id"]),
                 float(run["started_ts"]), str(run["run_directory"]),
                 _canonical_json(run.get("command") or []),
                 _canonical_json(run.get("options") or {}),
                 _canonical_json(run.get("code_identity") or {}),
                 _canonical_json(run.get("config_identity") or {}),
                 _canonical_json(run.get("data_manifest_identity") or {}),
                 _canonical_json(run.get("input_hashes") or {})))
            conn.execute(
                "INSERT INTO tournament_run_events "
                "(event_id, run_id, status, event_ts, detail_json) "
                "VALUES (?,?,?,?,?)",
                (uuid.uuid4().hex, str(run["run_id"]), "STARTED",
                 float(run["started_ts"]), _canonical_json({
                     "run_directory": str(run["run_directory"]),
                 })))

    def finish_tournament_run(
            self, run_id: str, status: str, *, ended_ts: float,
            strategies: list[dict] | None = None,
            detail: dict | None = None) -> None:
        """Atomically append a tournament's structured results and outcome."""
        if status not in {"SUCCEEDED", "FAILED"}:
            raise ValueError("tournament status must be SUCCEEDED or FAILED")
        rows = list(strategies or [])
        with _connect(self.path) as conn:
            if conn.execute(
                    "SELECT 1 FROM tournament_runs WHERE run_id=?",
                    (run_id,)).fetchone() is None:
                raise ValueError(f"unknown tournament run_id {run_id!r}")
            if conn.execute(
                    "SELECT 1 FROM tournament_run_events WHERE run_id=? "
                    "AND status IN ('SUCCEEDED','FAILED')",
                    (run_id,)).fetchone() is not None:
                raise ValueError(f"tournament run {run_id!r} is already final")
            for row in rows:
                strategy_id = str(row.get("strategy_id") or "")
                if not strategy_id:
                    raise ValueError("tournament strategy result has no id")
                strategy_result_id = _content_hash({
                    "run_id": run_id, "strategy_id": strategy_id,
                })[:32]
                scored = bool(row.get("scored"))
                headline = row.get("headline") or {}
                metrics = {
                    **headline,
                    "settings_tested": row.get("settings_tested"),
                    "best_setting": row.get("best_setting"),
                    "tier_changed": row.get("tier_changed"),
                    "benchmark_authority_ceiling": row.get(
                        "exploratory_ceiling"),
                }
                verdict = str(
                    row.get("measured_tier") if scored
                    else row.get("reason") or "NOT_SCORED")
                conn.execute(
                    "INSERT INTO tournament_strategy_results "
                    "(strategy_result_id, run_id, strategy_id, version, "
                    "scored, registered_tier, measured_tier, verdict, "
                    "metrics_json, result_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (strategy_result_id, run_id, strategy_id,
                     str(row.get("version") or "") or None, int(scored),
                     row.get("registered_tier"), row.get("measured_tier"),
                     verdict, _canonical_json(metrics), _canonical_json(row)))
                for setting in row.get("settings") or []:
                    setting_id = str(setting.get("setting_id") or "")
                    if not setting_id:
                        raise ValueError(
                            f"{strategy_id} tournament setting has no id")
                    setting_result_id = _content_hash({
                        "run_id": run_id, "strategy_id": strategy_id,
                        "setting_id": setting_id,
                    })[:32]
                    setting_metrics = {
                        key: setting.get(key) for key in (
                            "trades", "expectancy_pct", "expectancy_r",
                            "p_adjusted", "significant_corrected")
                    }
                    conn.execute(
                        "INSERT INTO tournament_setting_results "
                        "(setting_result_id, strategy_result_id, run_id, "
                        "strategy_id, setting_id, registered, verdict, "
                        "params_json, metrics_json, result_json) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (setting_result_id, strategy_result_id, run_id,
                         strategy_id, setting_id,
                         int(bool(setting.get("registered"))),
                         str(setting.get("tier") or setting.get("why")
                             or "UNSCORED"),
                         _canonical_json(setting.get("params") or {}),
                         _canonical_json(setting_metrics),
                         _canonical_json(setting)))
            conn.execute(
                "INSERT INTO tournament_run_events "
                "(event_id, run_id, status, event_ts, detail_json) "
                "VALUES (?,?,?,?,?)",
                (uuid.uuid4().hex, run_id, status, float(ended_ts),
                 _canonical_json(detail or {})))

    @staticmethod
    def _decode_json_columns(item: dict, names: tuple[str, ...]) -> dict:
        for name in names:
            raw = item.pop(f"{name}_json", None)
            item[name] = json.loads(raw) if raw is not None else None
        return item

    def tournament_runs(self) -> list[dict]:
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT run.*, event.status, event.event_ts AS status_ts, "
                "event.detail_json FROM tournament_runs AS run "
                "JOIN tournament_run_events AS event ON event.event_id=("
                "SELECT latest.event_id FROM tournament_run_events AS latest "
                "WHERE latest.run_id=run.run_id "
                "ORDER BY latest.event_ts DESC, latest.rowid DESC LIMIT 1) "
                "ORDER BY run.started_ts, run.run_id").fetchall()
        out = []
        names = ("command", "options", "code_identity", "config_identity",
                 "data_manifest_identity", "input_hashes", "detail")
        for row in rows:
            item = self._decode_json_columns(dict(row), names)
            item["ended_ts"] = (
                item["status_ts"] if item["status"] != "STARTED" else None)
            out.append(item)
        return out

    def tournament_results(self, run_id: str) -> dict:
        with _connect(self.path) as conn:
            strategies = conn.execute(
                "SELECT * FROM tournament_strategy_results WHERE run_id=? "
                "ORDER BY strategy_id", (run_id,)).fetchall()
            settings = conn.execute(
                "SELECT * FROM tournament_setting_results WHERE run_id=? "
                "ORDER BY strategy_id, setting_id", (run_id,)).fetchall()
        return {
            "strategies": [self._decode_json_columns(
                dict(row), ("metrics", "result")) for row in strategies],
            "settings": [self._decode_json_columns(
                dict(row), ("params", "metrics", "result"))
                for row in settings],
        }

    def start_backup_run(self, run: dict) -> None:
        """Append a backup request before touching any destination files."""
        with _connect(self.path) as conn:
            conn.execute(
                "INSERT INTO backup_runs (backup_id, request_content_id, "
                "started_ts, target_root, target_kind, target_evidence_json, "
                "require_external, command_json, options_json, "
                "source_paths_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (str(run["backup_id"]), str(run["request_content_id"]),
                 float(run["started_ts"]), str(run["target_root"]),
                 str(run["target_kind"]),
                 _canonical_json(run.get("target_evidence") or {}),
                 int(bool(run.get("require_external"))),
                 _canonical_json(run.get("command") or []),
                 _canonical_json(run.get("options") or {}),
                 _canonical_json(run.get("source_paths") or [])))
            conn.execute(
                "INSERT INTO backup_run_events "
                "(event_id, backup_id, status, event_ts, detail_json) "
                "VALUES (?,?,?,?,?)",
                (uuid.uuid4().hex, str(run["backup_id"]), "STARTED",
                 float(run["started_ts"]), _canonical_json({
                     "target_root": str(run["target_root"]),
                     "target_kind": str(run["target_kind"]),
                     "target_evidence": run.get("target_evidence") or {},
                 })))

    def finish_backup_run(
            self, backup_id: str, status: str, *, ended_ts: float,
            backup_path: str | Path, files: list[dict] | None = None,
            detail: dict | None = None) -> None:
        if status not in {"VERIFIED", "FAILED"}:
            raise ValueError("backup status must be VERIFIED or FAILED")
        file_rows = list(files or [])
        with _connect(self.path) as conn:
            if conn.execute(
                    "SELECT 1 FROM backup_runs WHERE backup_id=?",
                    (backup_id,)).fetchone() is None:
                raise ValueError(f"unknown backup_id {backup_id!r}")
            if conn.execute(
                    "SELECT 1 FROM backup_run_events WHERE backup_id=? "
                    "AND status IN ('VERIFIED','FAILED')",
                    (backup_id,)).fetchone() is not None:
                raise ValueError(f"backup run {backup_id!r} is already final")
            for item in file_rows:
                archive_path = str(item["archive_path"])
                conn.execute(
                    "INSERT INTO backup_files (backup_file_id, backup_id, "
                    "archive_path, source_path, kind, size_bytes, sha256, "
                    "verification_json) VALUES (?,?,?,?,?,?,?,?)",
                    (_content_hash({
                        "backup_id": backup_id,
                        "archive_path": archive_path,
                    })[:32], backup_id, archive_path,
                     str(item.get("source_path") or ""),
                     str(item.get("kind") or "file"),
                     int(item.get("size_bytes") or 0), str(item["sha256"]),
                     _canonical_json(item.get("verification") or {})))
            verification_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO backup_verifications (verification_id, "
                "backup_id, backup_path, verified_ts, status, detail_json) "
                "VALUES (?,?,?,?,?,?)",
                (verification_id, backup_id, str(backup_path),
                 float(ended_ts), status, _canonical_json(detail or {})))
            conn.execute(
                "INSERT INTO backup_run_events "
                "(event_id, backup_id, status, event_ts, detail_json) "
                "VALUES (?,?,?,?,?)",
                (uuid.uuid4().hex, backup_id, status, float(ended_ts),
                 _canonical_json({
                     "backup_path": str(backup_path), **(detail or {}),
                 })))

    def record_backup_verification(
            self, backup_id: str | None, backup_path: str | Path,
            status: str, detail: dict, *, verified_ts: float | None = None
            ) -> str:
        if status not in {"VERIFIED", "FAILED"}:
            raise ValueError("verification status must be VERIFIED or FAILED")
        verification_id = uuid.uuid4().hex
        with _connect(self.path) as conn:
            if backup_id is not None and conn.execute(
                    "SELECT 1 FROM backup_runs WHERE backup_id=?",
                    (backup_id,)).fetchone() is None:
                backup_id = None
            conn.execute(
                "INSERT INTO backup_verifications (verification_id, "
                "backup_id, backup_path, verified_ts, status, detail_json) "
                "VALUES (?,?,?,?,?,?)",
                (verification_id, backup_id, str(backup_path),
                 time.time() if verified_ts is None else float(verified_ts),
                 status, _canonical_json(detail)))
        return verification_id

    def backup_runs(self) -> list[dict]:
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT run.*, event.status, event.event_ts AS status_ts, "
                "event.detail_json FROM backup_runs AS run "
                "JOIN backup_run_events AS event ON event.event_id=("
                "SELECT latest.event_id FROM backup_run_events AS latest "
                "WHERE latest.backup_id=run.backup_id "
                "ORDER BY latest.event_ts DESC, latest.rowid DESC LIMIT 1) "
                "ORDER BY run.started_ts, run.backup_id").fetchall()
        out = []
        for row in rows:
            item = self._decode_json_columns(
                dict(row), ("target_evidence", "command", "options",
                            "source_paths", "detail"))
            item["ended_ts"] = (
                item["status_ts"] if item["status"] != "STARTED" else None)
            out.append(item)
        return out

    def backup_verifications(self, backup_id: str | None = None) -> list[dict]:
        with _connect(self.path) as conn:
            if backup_id is None:
                rows = conn.execute(
                    "SELECT * FROM backup_verifications "
                    "ORDER BY verified_ts, verification_id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM backup_verifications WHERE backup_id=? "
                    "ORDER BY verified_ts, verification_id",
                    (backup_id,)).fetchall()
        return [self._decode_json_columns(dict(row), ("detail",))
                for row in rows]

    def latest_verified_external_backup(self) -> dict | None:
        """Return a verified backup proven to use a different device."""
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT run.*, verification.backup_path, "
                "verification.verified_ts, verification.detail_json "
                "FROM backup_runs AS run JOIN backup_verifications AS "
                "verification ON verification.verification_id=("
                "SELECT latest.verification_id FROM backup_verifications AS "
                "latest WHERE latest.backup_id=run.backup_id "
                "ORDER BY latest.verified_ts DESC, latest.rowid DESC LIMIT 1) "
                "WHERE run.target_kind='external_mounted' "
                "AND verification.status='VERIFIED' "
                "ORDER BY verification.verified_ts DESC").fetchall()
        for row in rows:
            item = self._decode_json_columns(
                dict(row), ("target_evidence", "detail"))
            if _positive_external_target_evidence(item["target_evidence"]):
                return item
        return None

    # ------------------------------------------------------- paper research

    @staticmethod
    def _paper_default(initial_balance: float, now: float) -> dict:
        balance = float(initial_balance)
        return {
            "state_version": 1,
            "status": "SHADOW",
            "paper_started_ts": None,
            "decision_ledger_started_ts": now,
            "resume_status": "SHADOW",
            "cash_usdt": balance,
            "equity_usdt": balance,
            "realized_pnl_usdt": 0.0,
            "unrealized_pnl_usdt": 0.0,
            "high_water_mark": balance,
            "day": time.strftime("%Y-%m-%d", time.gmtime(now)),
            "day_start_equity": balance,
            "positions": [],
            "gross_notional": 0.0,
            "net_notional": 0.0,
            "open_risk_usdt": 0.0,
            "reserved_margin_usdt": 0.0,
            "margin_usage_pct": 0.0,
            "cooldowns": {},
            "active_trades": {},
            "seen_proposals": {},
            "unpriced_positions": 0,
            "loss_count": 0,
            "consecutive_losses": 0,
            "win_count": 0,
            "unfilled_count": 0,
            "invalid_outcome_count": 0,
            "max_drawdown_pct": 0.0,
            "last_mark_ts": now,
            "failure_count": 0,
            "revoked_reason": None,
            "experiment_provenance": None,
        }

    @classmethod
    def _reset_assignment_accounts_conn(
            cls, conn: sqlite3.Connection, assignment_id: str,
            scope_key: str, assignment_started_ts: float,
            attempt: int, baseline_variant_id: str,
            candidate_variant_id: str, code_identity: dict,
            config_identity: dict, initial_balance: float) -> None:
        """Start both assignment arms from one equivalent durable boundary.

        Historical trades and decisions remain append-only. Only the mutable
        account projections are rebased, and only after both arms are proven
        flat, so no open trade can be orphaned by a new assignment.
        """
        balance = float(initial_balance)
        if not math.isfinite(balance) or balance <= 0:
            raise ValueError("paper initial balance must be positive and finite")
        lanes = (
            ("baseline", str(baseline_variant_id)),
            ("candidate", str(candidate_variant_id)),
        )
        prepared = []
        required_provenance = {
            "variant_definition_hash", "strategy_config_version",
            "experiment_config", "code_version", "forward_model_id",
            "forward_model_assumptions_hash",
        }
        for lane, variant_id in lanes:
            variant = cls._require_variant(conn, variant_id)
            row = conn.execute(
                "SELECT state_json, version FROM paper_portfolios "
                "WHERE scope_key=? AND variant_id=?",
                (scope_key, variant_id)).fetchone()
            existing_state = (
                json.loads(row["state_json"]) if row is not None else {})
            open_rows = int(conn.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE scope_key=? "
                "AND variant_id=? AND status='OPEN'",
                (scope_key, variant_id)).fetchone()[0])
            if existing_state.get("positions") or open_rows:
                raise ValueError(
                    f"{variant_id}: cannot start a fresh assignment while "
                    "the previous account has unresolved paper trades")
            provenance = {
                **dict((config_identity or {}).get(lane) or {}),
                **dict((code_identity or {}).get(lane) or {}),
            }
            canonical_provenance = None
            if required_provenance.issubset(provenance):
                canonical_provenance = cls._validate_experiment_provenance(
                    variant, provenance)
            state = cls._paper_default(balance, assignment_started_ts)
            state.update({
                "experiment_provenance": canonical_provenance,
                "experiment_assignment_id": assignment_id,
                "experiment_assignment_started_ts": assignment_started_ts,
                "experiment_assignment_attempt": int(attempt),
                "assignment_initial_balance_usdt": balance,
            })
            prepared.append((variant_id, row, state))

        for variant_id, row, state in prepared:
            if row is None:
                conn.execute(
                    "INSERT INTO paper_portfolios (scope_key, variant_id, "
                    "state_json, version, updated_ts) VALUES (?,?,?,?,?)",
                    (scope_key, variant_id, _canonical_json(state), 1,
                     assignment_started_ts))
                continue
            conn.execute(
                "UPDATE paper_portfolios SET state_json=?, version=version+1, "
                "updated_ts=?, revoked_ts=NULL, revoke_reason=NULL "
                "WHERE scope_key=? AND variant_id=?",
                (_canonical_json(state), assignment_started_ts,
                 scope_key, variant_id))

    def load_paper_portfolio(
            self, scope_key: str, variant_id: str,
            initial_balance: float = 10_000.0,
            now: float | None = None) -> tuple[dict, int]:
        timestamp = time.time() if now is None else float(now)
        with _connect(self.path) as conn:
            self._require_variant(conn, variant_id)
            row = conn.execute(
                "SELECT state_json, version FROM paper_portfolios "
                "WHERE scope_key=? AND variant_id=?",
                (scope_key, variant_id)).fetchone()
            if row is None:
                state = self._paper_default(initial_balance, timestamp)
                conn.execute(
                    "INSERT INTO paper_portfolios (scope_key, variant_id, "
                    "state_json, version, updated_ts) VALUES (?,?,?,?,?)",
                    (scope_key, variant_id,
                     json.dumps(state, sort_keys=True), 1, timestamp))
                return state, 1
        return json.loads(row["state_json"]), int(row["version"])

    @staticmethod
    def _validate_experiment_provenance(
            variant: sqlite3.Row, provenance: dict) -> dict:
        from agent import state as runtime_state

        if not isinstance(provenance, dict):
            raise ValueError("experiment provenance must be a mapping")
        required = {
            "variant_definition_hash", "strategy_config_version",
            "experiment_config",
            "code_version", "forward_model_id",
            "forward_model_assumptions_hash",
        }
        missing = sorted(required - set(provenance))
        if missing:
            raise ValueError(
                "experiment provenance is incomplete: " + ", ".join(missing))
        canonical = json.loads(_canonical_json(provenance))
        expected = variant_identity_hash(variant)
        if canonical["variant_definition_hash"] != expected:
            raise ValueError(
                f"{variant['variant_id']}: variant definition provenance "
                "does not match the immutable registry identity")
        experiment_config = canonical.get("experiment_config")
        if not isinstance(experiment_config, dict):
            raise ValueError("experiment provenance config is not a mapping")
        if runtime_state.experiment_fingerprint_material(
                experiment_config) != experiment_config:
            raise ValueError(
                "experiment provenance config contains secrets or "
                "non-executable settings")
        if canonical["strategy_config_version"] != _content_hash(
                experiment_config)[:16]:
            raise ValueError(
                "experiment provenance config does not match its fingerprint")
        for key in required - {"experiment_config"}:
            if not str(canonical.get(key) or "").strip():
                raise ValueError(f"experiment provenance {key} is empty")
        return canonical

    def bind_paper_experiment(
            self, scope_key: str, variant_id: str, provenance: dict,
            initial_balance: float = 10_000.0,
            now: float | None = None) -> tuple[dict, int]:
        """Bind one portfolio to one immutable executable experiment.

        Existing evidence without provenance is deliberately not adopted. A
        code/config/model change must use a new variant id (or an explicitly
        migrated empty account), otherwise old and new outcomes would share a
        bucket while claiming to describe one experiment.
        """
        timestamp = time.time() if now is None else float(now)
        with _connect(self.path) as conn:
            variant = self._require_variant(conn, variant_id)
            canonical = self._validate_experiment_provenance(
                variant, provenance)
            row = conn.execute(
                "SELECT state_json, version FROM paper_portfolios "
                "WHERE scope_key=? AND variant_id=?",
                (scope_key, variant_id)).fetchone()
            if row is None:
                state = self._paper_default(initial_balance, timestamp)
                state["experiment_provenance"] = canonical
                conn.execute(
                    "INSERT INTO paper_portfolios (scope_key, variant_id, "
                    "state_json, version, updated_ts) VALUES (?,?,?,?,?)",
                    (scope_key, variant_id, _canonical_json(state), 1,
                     timestamp))
                return state, 1

            state = json.loads(row["state_json"])
            version = int(row["version"])
            existing = state.get("experiment_provenance")
            if existing == canonical:
                return state, version
            trades = conn.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE scope_key=? "
                "AND variant_id=?", (scope_key, variant_id)).fetchone()[0]
            decisions = conn.execute(
                "SELECT COUNT(*) FROM paper_decisions WHERE scope_key=? "
                "AND variant_id=?", (scope_key, variant_id)).fetchone()[0]
            has_evidence = bool(
                trades or decisions or state.get("positions")
                or state.get("active_trades")
                or state.get("seen_proposals") or state.get("paper_started_ts")
                or state.get("win_count") or state.get("loss_count"))
            if existing is None and not has_evidence:
                state["experiment_provenance"] = canonical
                version = self._update_paper_portfolio(
                    conn, scope_key, variant_id, state, version, timestamp)
                return state, version
            raise ValueError(
                f"{variant_id}: persisted paper evidence belongs to a "
                "different or unprovenanced experiment; use a new variant id")

    def save_paper_portfolio(
            self, scope_key: str, variant_id: str, state: dict,
            expected_version: int, now: float | None = None) -> int:
        return self.commit_paper_portfolio(
            scope_key, variant_id, state, expected_version, now=now)

    @staticmethod
    def _update_paper_portfolio(
            conn: sqlite3.Connection, scope_key: str, variant_id: str,
            state: dict, expected_version: int, timestamp: float) -> int:
        payload = json.dumps(state, sort_keys=True, allow_nan=False)
        cursor = conn.execute(
            "UPDATE paper_portfolios SET state_json=?, version=version+1, "
            "updated_ts=?, revoked_ts=?, revoke_reason=? "
            "WHERE scope_key=? AND variant_id=? AND version=?",
            (payload, timestamp,
             timestamp if state.get("status") == "REVOKED" else None,
             state.get("revoked_reason"), scope_key, variant_id,
             int(expected_version)))
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"paper portfolio version conflict for {variant_id}")
        return int(expected_version) + 1

    @staticmethod
    def _insert_paper_trade(conn: sqlite3.Connection, trade: dict) -> str:
        trade_id = str(trade.get("trade_id") or uuid.uuid4().hex)
        initial_execution = {
            "entry": trade.get("entry_execution")
                     or (trade.get("assumptions") or {}).get(
                         "entry_execution"),
            "status": str(trade.get("order_status") or "filled"),
        }
        columns = {str(row[1]) for row in conn.execute(
            "PRAGMA table_info(paper_trades)").fetchall()}
        values = (
            trade_id, trade["proposal_id"], trade["scope_key"],
            trade["variant_id"], trade.get("cycle_id"), trade["symbol"],
            trade["direction"], trade.get("setup_type"),
            trade.get("signal_ts"), trade["model_id"],
            json.dumps(trade.get("assumptions") or {}, sort_keys=True),
            trade["entry_ts"], trade["entry_price"], trade["notional"],
            trade["risk_usd"], trade["stop_price"], trade["take_price"],
        )
        if "execution_json" in columns:
            conn.execute(
                "INSERT INTO paper_trades (trade_id, proposal_id, scope_key, "
                "variant_id, cycle_id, symbol, direction, setup_type, signal_ts, "
                "model_id, assumptions_json, entry_ts, entry_price, notional, "
                "risk_usd, stop_price, take_price, status, execution_json, "
                "valid_for_inference) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN',?,1)",
                (*values, _canonical_json(initial_execution)))
        else:
            conn.execute(
                "INSERT INTO paper_trades (trade_id, proposal_id, scope_key, "
                "variant_id, cycle_id, symbol, direction, setup_type, signal_ts, "
                "model_id, assumptions_json, entry_ts, entry_price, notional, "
                "risk_usd, stop_price, take_price, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN')", values)
        return trade_id

    @staticmethod
    def _insert_paper_decision(
            conn: sqlite3.Connection, decision: dict) -> str:
        decision_id = str(decision.get("decision_id") or uuid.uuid4().hex)
        outcome = str(decision.get("decision_outcome") or "").upper()
        if outcome not in {"PROPOSED", "VETOED"}:
            raise ValueError("paper decision outcome must be PROPOSED or VETOED")
        conn.execute(
            "INSERT INTO paper_decisions (decision_id, proposal_id, "
            "scope_key, variant_id, cycle_id, symbol, direction, setup_type, "
            "signal_ts, confidence, decision_outcome, reason, paper_trade_id, "
            "model_id, assumptions_json, proposal_json, decision_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (decision_id, decision["proposal_id"], decision["scope_key"],
             decision["variant_id"], decision.get("cycle_id"),
             decision["symbol"], decision.get("direction"),
             decision.get("setup_type"), decision.get("signal_ts"),
             decision.get("confidence"), outcome, decision.get("reason"),
             decision.get("paper_trade_id"), decision["model_id"],
             _canonical_json(decision.get("assumptions") or {}),
             _canonical_json(decision.get("proposal") or {}),
             decision["decision_ts"]))
        return decision_id

    @staticmethod
    def _close_paper_trade(conn: sqlite3.Connection, close: dict) -> None:
        columns = {str(row[1]) for row in conn.execute(
            "PRAGMA table_info(paper_trades)").fetchall()}
        if "execution_json" in columns:
            cursor = conn.execute(
                "UPDATE paper_trades SET exit_ts=?, exit_price=?, result=?, "
                "net_pnl_usd=?, r_multiple=?, execution_json=?, "
                "valid_for_inference=?, status='CLOSED' "
                "WHERE trade_id=? AND status='OPEN'",
                (close["exit_ts"], close["exit_price"], close["result"],
                 close["net_pnl_usd"], close["r_multiple"],
                 _canonical_json(close.get("execution") or {}),
                 1 if close.get("valid_for_inference", True) else 0,
                 close["trade_id"]))
        else:
            cursor = conn.execute(
                "UPDATE paper_trades SET exit_ts=?, exit_price=?, result=?, "
                "net_pnl_usd=?, r_multiple=?, status='CLOSED' "
                "WHERE trade_id=? AND status='OPEN'",
                (close["exit_ts"], close["exit_price"], close["result"],
                 close["net_pnl_usd"], close["r_multiple"],
                 close["trade_id"]))
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"paper trade {close['trade_id']} was not open")

    def commit_paper_portfolio(
            self, scope_key: str, variant_id: str, state: dict,
            expected_version: int, *, opened_trades: list | None = None,
            decisions: list | None = None,
            closed_trades: list | None = None,
            now: float | None = None) -> int:
        """Atomically persist trade mutations and their resulting account."""
        timestamp = time.time() if now is None else float(now)
        with _connect(self.path) as conn:
            self._require_variant(conn, variant_id)
            for trade in opened_trades or []:
                if (trade.get("scope_key") != scope_key
                        or trade.get("variant_id") != variant_id):
                    raise ValueError("paper trade attribution mismatch")
                self._insert_paper_trade(conn, trade)
            for decision in decisions or []:
                if (decision.get("scope_key") != scope_key
                        or decision.get("variant_id") != variant_id):
                    raise ValueError("paper decision attribution mismatch")
                self._insert_paper_decision(conn, decision)
            for close in closed_trades or []:
                self._close_paper_trade(conn, close)
            return self._update_paper_portfolio(
                conn, scope_key, variant_id, state, expected_version, timestamp)

    def scheduler_order(self, scope_key: str, variant_ids: list[str]) -> list[str]:
        """Least-observed-first ordering: durable fair round-robin."""
        with _connect(self.path) as conn:
            for ordinal, variant_id in enumerate(variant_ids):
                self._require_variant(conn, variant_id)
                conn.execute(
                    "INSERT INTO variant_scheduler (scope_key, variant_id, "
                    "ordinal) VALUES (?,?,?) ON CONFLICT(scope_key, variant_id) "
                    "DO UPDATE SET ordinal=excluded.ordinal",
                    (scope_key, variant_id, ordinal))
            placeholders = ",".join("?" for _ in variant_ids)
            if not placeholders:
                return []
            rows = conn.execute(
                "SELECT variant_id FROM variant_scheduler WHERE scope_key=? "
                f"AND variant_id IN ({placeholders}) ORDER BY evaluations, "
                "COALESCE(last_evaluated_ts, 0), ordinal, variant_id",
                (scope_key, *variant_ids)).fetchall()
        return [str(row[0]) for row in rows]

    def record_scheduler_cycle(
            self, scope_key: str, evaluated: list[str], skipped: list[str],
            now: float | None = None) -> None:
        timestamp = time.time() if now is None else float(now)
        with _connect(self.path) as conn:
            conn.executemany(
                "UPDATE variant_scheduler SET evaluations=evaluations+1, "
                "last_evaluated_ts=? WHERE scope_key=? AND variant_id=?",
                [(timestamp, scope_key, variant_id)
                 for variant_id in evaluated])
            conn.executemany(
                "UPDATE variant_scheduler SET skips=skips+1, "
                "last_skipped_ts=? WHERE scope_key=? AND variant_id=?",
                [(timestamp, scope_key, variant_id) for variant_id in skipped])

    def scheduler_coverage(self, scope_key: str) -> list:
        with _connect(self.path) as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM variant_scheduler WHERE scope_key=? "
                "ORDER BY ordinal, variant_id", (scope_key,))]

    def paper_scopes(self) -> list[str]:
        with _connect(self.path) as conn:
            return [str(row[0]) for row in conn.execute(
                "SELECT DISTINCT scope_key FROM paper_portfolios "
                "ORDER BY scope_key")]

    def paper_portfolio_state(
            self, scope_key: str, variant_id: str) -> dict | None:
        """Read an enrolled portfolio without creating one as a side effect."""
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT state_json, version, updated_ts FROM paper_portfolios "
                "WHERE scope_key=? AND variant_id=?",
                (scope_key, variant_id)).fetchone()
        if row is None:
            return None
        state = json.loads(row["state_json"])
        state["portfolio_version"] = int(row["version"])
        state["updated_ts"] = float(row["updated_ts"])
        return state

    def record_paper_trade_open(self, trade: dict) -> str:
        with _connect(self.path) as conn:
            return self._insert_paper_trade(conn, trade)

    def close_paper_trade(
            self, trade_id: str, *, exit_ts: float, exit_price: float,
            result: str, net_pnl_usd: float, r_multiple: float) -> None:
        with _connect(self.path) as conn:
            self._close_paper_trade(conn, {
                "trade_id": trade_id, "exit_ts": exit_ts,
                "exit_price": exit_price, "result": result,
                "net_pnl_usd": net_pnl_usd, "r_multiple": r_multiple})

    def record_paper_failure(
            self, scope_key: str, variant_id: str, kind: str, detail: dict,
            now: float | None = None) -> int:
        timestamp = time.time() if now is None else float(now)
        with _connect(self.path) as conn:
            cursor = conn.execute(
                "INSERT INTO paper_failures (scope_key, variant_id, ts, kind, "
                "detail_json) VALUES (?,?,?,?,?)",
                (scope_key, variant_id, timestamp, kind,
                 json.dumps(detail, sort_keys=True, default=str)))
            return int(cursor.lastrowid)

    def paper_trades_for(self, scope_key: str, variant_id: str) -> list:
        with _connect(self.path) as conn:
            rows = [dict(row) for row in conn.execute(
                "SELECT * FROM paper_trades WHERE scope_key=? AND variant_id=? "
                "ORDER BY entry_ts, trade_id", (scope_key, variant_id))]
        for row in rows:
            row["execution"] = json.loads(row.get("execution_json") or "null")
        return rows

    def paper_decisions_for(self, scope_key: str, variant_id: str) -> list:
        with _connect(self.path) as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM paper_decisions WHERE scope_key=? AND "
                "variant_id=? ORDER BY decision_ts, decision_id",
                (scope_key, variant_id))]

    def paper_summary(
            self, scope_key: str, variant_id: str,
            paper_only: bool = True) -> dict:
        state, _ = self.load_paper_portfolio(scope_key, variant_id)
        trades = self.paper_trades_for(scope_key, variant_id)
        decisions = self.paper_decisions_for(scope_key, variant_id)
        paper_started = state.get("paper_started_ts")
        if paper_only:
            trades = [row for row in trades if paper_started is not None
                      and float(row["entry_ts"]) >= float(paper_started)]
            decisions = [
                row for row in decisions if paper_started is not None
                and float(row["decision_ts"]) >= float(paper_started)]
        unfilled = [row for row in trades if row["status"] == "CLOSED"
                    and row.get("result") == "unfilled"]
        closed = [row for row in trades if row["status"] == "CLOSED"
                  and row.get("result") != "unfilled"
                  and int(row.get("valid_for_inference", 1)) == 1]
        invalid = [row for row in trades if row["status"] == "CLOSED"
                   and row.get("result") != "unfilled"
                   and int(row.get("valid_for_inference", 1)) == 0]
        accepted = [row for row in decisions
                    if row["decision_outcome"] == "PROPOSED"]
        vetoed = [row for row in decisions
                  if row["decision_outcome"] == "VETOED"]
        return {
            "scope_key": scope_key,
            "variant_id": variant_id,
            "status": state.get("status"),
            "paper_started_ts": paper_started,
            "decision_ledger_started_ts": state.get(
                "decision_ledger_started_ts"),
            "decisions": len(decisions),
            "accepted_decisions": len(accepted),
            "vetoed_decisions": len(vetoed),
            "equity_usdt": state.get("equity_usdt"),
            "cash_usdt": state.get("cash_usdt"),
            "max_drawdown_pct": state.get("max_drawdown_pct"),
            "open_positions": len(state.get("positions") or []),
            "closed_trades": len(closed),
            "fill_opportunities": len(trades),
            "unfilled_trades": len(unfilled),
            "invalid_closed_trades": len(invalid),
            "net_pnl_usdt": sum(float(row["net_pnl_usd"] or 0) for row in closed),
            "expectancy_r": (sum(float(row["r_multiple"] or 0) for row in closed)
                             / len(closed) if closed else None),
            "failures": int(state.get("failure_count") or 0),
            "revoked_reason": state.get("revoked_reason"),
        }

    # ------------------------------------------------ edge qualification

    def qualification_events(self, variant_id: str) -> list:
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT * FROM edge_qualification_events "
                "WHERE variant_id=? ORDER BY ts, event_id", (variant_id,)
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            out.append(item)
        return out

    def qualification_status(
            self, variant_id: str, scope_key: str = "*") -> dict | None:
        eligible = [event for event in self.qualification_events(variant_id)
                    if event["scope_key"] in {"*", scope_key}]
        return eligible[-1] if eligible else None

    def qualified_variant_ids(self, scope_key: str = "*") -> list[str]:
        with _connect(self.path) as conn:
            ids = [str(row[0]) for row in conn.execute(
                "SELECT variant_id FROM variants ORDER BY variant_id")]
        return [variant_id for variant_id in ids
                if (self.qualification_status(variant_id, scope_key) or {})
                .get("status") == "QUALIFIED"]

    @staticmethod
    def _analysis_payload(row: sqlite3.Row) -> dict:
        payload = json.loads(row["payload_json"])
        canonical, payload_hash = _analysis_canonical(
            str(row["kind"]), str(row["subject_id"]), payload)
        if canonical != row["payload_json"] or payload_hash != row["payload_hash"]:
            raise ValueError(
                f"analysis {row['analysis_id']} failed its content hash")
        return payload

    @classmethod
    def _validate_forward_axis(
            cls, conn: sqlite3.Connection, strategy_id: str, axis: object,
            baseline_id: str, setting_ids: list[str], arms: dict) -> list[str]:
        """Prove arms are one version of one strategy differing only on axis."""
        canonical_axis = _canonical_axis(axis)
        # Hypothesis settings are metadata-backed experiment axes rather than
        # executable strategy-config paths. They still need the same
        # uniqueness/version/mechanism boundary before forward evidence can be
        # used, but their exact values live in hypothesis_params_json.
        if canonical_axis[0].startswith("hypothesis."):
            prefixes = [path.split(".", 2) for path in canonical_axis]
            if (not all(len(parts) == 3 and parts[0] == "hypothesis"
                        for parts in prefixes)
                    or len({parts[1] for parts in prefixes}) != 1):
                raise ValueError("hypothesis forward axis is malformed")
            hypothesis_id = prefixes[0][1]
            keys = [parts[2] for parts in prefixes]
            baseline = cls._require_variant(conn, baseline_id)
            if (baseline["strategy_id"] != strategy_id
                    or baseline["hypothesis_id"] != hypothesis_id):
                raise ValueError(
                    "hypothesis forward baseline does not match the axis")
            try:
                baseline_params = json.loads(
                    baseline["hypothesis_params_json"] or "{}")
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("hypothesis baseline has invalid params") from exc
            if not isinstance(baseline_params, dict):
                raise ValueError("hypothesis baseline params are not a mapping")
            if any(key not in baseline_params for key in keys):
                raise ValueError("hypothesis baseline is missing an axis value")
            baseline_other = {
                key: value for key, value in baseline_params.items()
                if key not in keys}
            seen_values = {_canonical_json([baseline_params[key] for key in keys])}
            for variant_id in setting_ids:
                variant = cls._require_variant(conn, variant_id)
                if (variant["strategy_id"] != strategy_id
                        or str(variant["base_version"])
                        != str(baseline["base_version"])
                        or variant["hypothesis_id"] != hypothesis_id):
                    raise ValueError(
                        "hypothesis axis mixes strategies, versions or hypotheses")
                try:
                    params = json.loads(
                        variant["hypothesis_params_json"] or "{}")
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        f"{variant_id}: invalid hypothesis params") from exc
                if not isinstance(params, dict) or any(
                        key not in params for key in keys):
                    raise ValueError(
                        f"{variant_id}: hypothesis params omit the declared axis")
                if {key: value for key, value in params.items()
                        if key not in keys} != baseline_other:
                    raise ValueError(
                        f"{variant_id}: non-axis hypothesis params differ "
                        "from the baseline")
                values = _canonical_json([params[key] for key in keys])
                if values in seen_values:
                    raise ValueError(
                        f"{variant_id}: hypothesis setting duplicates another arm")
                seen_values.add(values)
            return canonical_axis

        expected_baseline = f"{str(strategy_id).replace('-', '_')}.baseline"
        if baseline_id != expected_baseline:
            raise ValueError(
                f"forward baseline must be {expected_baseline!r}, not "
                f"{baseline_id!r}")
        if len(set(setting_ids)) != len(setting_ids):
            raise ValueError("forward candidates must be unique")

        baseline = cls._require_variant(conn, baseline_id)
        try:
            baseline_overrides = json.loads(baseline["overrides_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("forward baseline has invalid overrides") from exc
        if (not isinstance(baseline_overrides, dict)
                or baseline["strategy_id"] != strategy_id
                or baseline_overrides):
            raise ValueError(
                "forward baseline must be the unmodified registered strategy")
        base_version = str(baseline["base_version"])
        baseline_provenance = cls._validate_experiment_provenance(
            baseline, arms[baseline_id].get("experiment_provenance") or {})
        baseline_config = baseline_provenance["experiment_config"]
        strategy_config = baseline_config.get("strategy") or {}
        if (strategy_config.get("id") != strategy_id
                or strategy_config.get("version") != base_version):
            raise ValueError(
                "forward baseline executable config does not match its "
                "registered strategy/version")

        baseline_without_axis = _without_dotted_paths(
            baseline_config, canonical_axis)
        seen_values = {
            _canonical_json([
                _dotted_value(baseline_config, path)
                for path in canonical_axis
            ])
        }
        for variant_id in setting_ids:
            variant = cls._require_variant(conn, variant_id)
            if (variant["strategy_id"] != strategy_id
                    or str(variant["base_version"]) != base_version):
                raise ValueError(
                    "forward axis mixes strategy versions or strategies")
            try:
                overrides = json.loads(variant["overrides_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"{variant_id}: invalid registered overrides") from exc
            if not isinstance(overrides, dict):
                raise ValueError(
                    f"{variant_id}: registered overrides are not a mapping")
            if set(overrides) != set(canonical_axis):
                raise ValueError(
                    f"{variant_id}: overrides {sorted(overrides)} do not "
                    f"exactly match declared axis {canonical_axis}")
            provenance = cls._validate_experiment_provenance(
                variant, arms[variant_id].get("experiment_provenance") or {})
            config = provenance["experiment_config"]
            candidate_strategy = config.get("strategy") or {}
            if (candidate_strategy.get("id") != strategy_id
                    or candidate_strategy.get("version") != base_version):
                raise ValueError(
                    f"{variant_id}: executable config is not the registered "
                    "strategy/version")
            for path in canonical_axis:
                if _canonical_json(_dotted_value(config, path)) != _canonical_json(
                        overrides[path]):
                    raise ValueError(
                        f"{variant_id}: executable value for {path} does not "
                        "match its registered override")
            if _canonical_json(_without_dotted_paths(
                    config, canonical_axis)) != _canonical_json(
                        baseline_without_axis):
                raise ValueError(
                    f"{variant_id}: non-axis executable configuration differs "
                    "from the baseline")
            values = _canonical_json([
                _dotted_value(config, path) for path in canonical_axis
            ])
            if values in seen_values:
                raise ValueError(
                    f"{variant_id}: axis setting duplicates the baseline or "
                    "another candidate")
            seen_values.add(values)
        return canonical_axis

    @staticmethod
    def _forward_v3_source_hash(source: dict) -> str:
        unsigned = dict(source)
        unsigned.pop("sha256", None)
        return _content_hash(unsigned)

    @staticmethod
    def _forward_assignment_rows(
            conn: sqlite3.Connection, scope_key: str, strategy_id: str,
            baseline_id: str, candidate_id: str,
            completed_through_ts: float | None = None) -> list[sqlite3.Row]:
        event_cutoff = ""
        assignment_cutoff = ""
        params: list = []
        if completed_through_ts is not None:
            params.append(float(completed_through_ts))
            event_cutoff = " AND event.event_ts<=?"
            assignment_cutoff = " AND assignment.started_ts<=?"
        params.extend([scope_key, strategy_id, baseline_id, candidate_id])
        if completed_through_ts is not None:
            params.append(float(completed_through_ts))
        return conn.execute(
            "SELECT assignment.*, terminal.event_id AS terminal_event_id, "
            "COALESCE(terminal.status, 'STARTED') AS terminal_status, "
            "COALESCE(terminal.event_ts, assignment.started_ts) AS ended_ts, "
            "terminal.reason AS terminal_reason "
            "FROM strategy_experiment_assignments AS assignment "
            "LEFT JOIN strategy_experiment_assignment_events AS terminal "
            "ON terminal.rowid=(SELECT event.rowid FROM "
            "strategy_experiment_assignment_events AS event "
            "WHERE event.assignment_id=assignment.assignment_id "
            + event_cutoff + " "
            "ORDER BY event.event_ts DESC, event.rowid DESC LIMIT 1) "
            "WHERE assignment.scope_key=? AND assignment.strategy_id=? "
            "AND assignment.baseline_variant_id=? "
            "AND assignment.candidate_variant_id=? "
            + assignment_cutoff + " "
            "ORDER BY assignment.started_ts, assignment.attempt, "
            "assignment.assignment_id", params).fetchall()

    @classmethod
    def _assignment_provenances(
            cls, conn: sqlite3.Connection,
            assignment: sqlite3.Row) -> tuple[dict, dict, dict, dict]:
        try:
            code_identity = json.loads(assignment["code_identity_json"])
            config_identity = json.loads(assignment["config_identity_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"assignment {assignment['assignment_id']} has invalid identity "
                "JSON") from exc
        if not isinstance(code_identity, dict) or not isinstance(
                config_identity, dict):
            raise ValueError("forward assignment identities must be mappings")
        provenances = []
        for lane, variant_id in (
                ("baseline", assignment["baseline_variant_id"]),
                ("candidate", assignment["candidate_variant_id"])):
            code = code_identity.get(lane)
            config = config_identity.get(lane)
            if not isinstance(code, dict) or not isinstance(config, dict):
                raise ValueError(
                    f"assignment {assignment['assignment_id']} has incomplete "
                    f"{lane} identity")
            variant = cls._require_variant(conn, str(variant_id))
            provenances.append(cls._validate_experiment_provenance(
                variant, {**config, **code}))
        return (json.loads(_canonical_json(code_identity)),
                json.loads(_canonical_json(config_identity)),
                provenances[0], provenances[1])

    @staticmethod
    def _forward_assignment_decisions(
            conn: sqlite3.Connection, scope_key: str, variant_id: str,
            started_ts: float, ended_ts: float) -> list[dict]:
        rows = conn.execute(
            "SELECT d.*, t.proposal_id AS trade_proposal_id, "
            "t.scope_key AS trade_scope_key, "
            "t.variant_id AS trade_variant_id, "
            "t.model_id AS trade_model_id, "
            "t.entry_ts AS trade_entry_ts, t.exit_ts AS trade_exit_ts, "
            "t.result AS trade_result, t.r_multiple AS trade_r_multiple, "
            "t.status AS trade_status, "
            "t.valid_for_inference AS trade_valid_for_inference "
            "FROM paper_decisions AS d LEFT JOIN paper_trades AS t "
            "ON t.trade_id=d.paper_trade_id "
            "WHERE d.scope_key=? AND d.variant_id=? "
            "AND d.decision_ts>=? AND d.decision_ts<=? "
            "ORDER BY d.decision_ts, d.decision_id",
            (scope_key, variant_id, started_ts, ended_ts)).fetchall()
        decisions = []
        for row in rows:
            evidence = _forward_decision_evidence(row)
            exit_ts = evidence.get("trade_exit_ts")
            if exit_ts is not None and float(exit_ts) > ended_ts:
                evidence.update({
                    "trade_exit_ts": None, "trade_result": None,
                    "trade_r_multiple": None, "trade_status": "OPEN",
                })
            decisions.append(evidence)
        return decisions

    @classmethod
    def _forward_assignment_record(
            cls, conn: sqlite3.Connection,
            assignment: sqlite3.Row) -> dict:
        code_identity, config_identity, baseline_provenance, candidate_provenance = (
            cls._assignment_provenances(conn, assignment))
        started_ts = float(assignment["started_ts"])
        ended_ts = float(assignment["ended_ts"])
        for variant_id in (
                str(assignment["baseline_variant_id"]),
                str(assignment["candidate_variant_id"])):
            failure = conn.execute(
                "SELECT kind, ts FROM paper_failures WHERE scope_key=? "
                "AND variant_id=? AND ts>=? AND ts<=? ORDER BY ts LIMIT 1",
                (assignment["scope_key"], variant_id,
                 started_ts, ended_ts)).fetchone()
            if failure is not None:
                raise ValueError(
                    f"{variant_id}: operational failure {failure['kind']} "
                    f"occurred inside assignment {assignment['assignment_id']}")
        try:
            setting = json.loads(assignment["setting_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"assignment {assignment['assignment_id']} has invalid setting") \
                from exc
        return {
            "assignment_id": str(assignment["assignment_id"]),
            "attempt": int(assignment["attempt"] or 1),
            "retry_of_assignment_id": assignment["retry_of_assignment_id"],
            "candidate_key": str(assignment["candidate_key"]),
            "axis": str(assignment["axis"]),
            "setting_id": str(assignment["setting_id"]),
            "setting": setting,
            "source": str(assignment["source"]),
            "proposal_id": assignment["proposal_id"],
            "started_ts": started_ts,
            "ended_ts": ended_ts,
            "terminal_event_id": str(assignment["terminal_event_id"]),
            "terminal_status": str(assignment["terminal_status"]),
            "terminal_reason": assignment["terminal_reason"],
            "code_identity": code_identity,
            "code_identity_hash": _content_hash(code_identity),
            "config_identity": config_identity,
            "config_identity_hash": _content_hash(config_identity),
            "baseline": {
                "variant_id": str(assignment["baseline_variant_id"]),
                "experiment_provenance": baseline_provenance,
                "provenance_hash": _content_hash(baseline_provenance),
                "decisions": cls._forward_assignment_decisions(
                    conn, str(assignment["scope_key"]),
                    str(assignment["baseline_variant_id"]),
                    started_ts, ended_ts),
            },
            "candidate": {
                "variant_id": str(assignment["candidate_variant_id"]),
                "experiment_provenance": candidate_provenance,
                "provenance_hash": _content_hash(candidate_provenance),
                "decisions": cls._forward_assignment_decisions(
                    conn, str(assignment["scope_key"]),
                    str(assignment["candidate_variant_id"]),
                    started_ts, ended_ts),
            },
        }

    @classmethod
    def _eligible_forward_assignment_records(
            cls, conn: sqlite3.Connection, scope_key: str, strategy_id: str,
            baseline_id: str, candidate_id: str,
            baseline_provenance: dict, candidate_provenance: dict,
            completed_through_ts: float | None = None) -> list[dict]:
        records = []
        for assignment in cls._forward_assignment_rows(
                conn, scope_key, strategy_id, baseline_id, candidate_id,
                completed_through_ts):
            _, _, observed_baseline, observed_candidate = (
                cls._assignment_provenances(conn, assignment))
            if (observed_baseline != baseline_provenance
                    or observed_candidate != candidate_provenance):
                continue
            assignment_id = str(assignment["assignment_id"])
            status = str(assignment["terminal_status"])
            if status in {"STARTED", "OBSERVING", "DRAINING"}:
                raise ValueError(
                    f"{candidate_id}: matching assignment {assignment_id} is "
                    f"unfinished at status {status}")
            if status == "REJECTED":
                reason = str(assignment["terminal_reason"] or
                             "no rejection reason recorded")
                raise ValueError(
                    f"{candidate_id}: matching assignment {assignment_id} "
                    f"was REJECTED: {reason}")
            if status != "COMPLETED":
                raise ValueError(
                    f"{candidate_id}: matching assignment {assignment_id} "
                    f"has unsupported status {status}")
            records.append(cls._forward_assignment_record(conn, assignment))
        return records

    @classmethod
    def _validate_forward_arm_decisions(
            cls, payload: dict, variant_id: str, provenance: dict,
            decisions: object, model) -> None:
        if not isinstance(decisions, list):
            raise ValueError("forward decision corpus is invalid")
        for decision in decisions:
            assumptions = decision.get("assumptions") or {}
            outcome = str(decision.get("decision_outcome") or "")
            proposed = outcome == "PROPOSED"
            vetoed = outcome == "VETOED"
            if (decision.get("scope_key") != payload.get("scope_key")
                    or decision.get("variant_id") != variant_id
                    or decision.get("model_id") != model.model_id
                    or assumptions.get("forward_model") != json.loads(
                        _canonical_json(model.as_dict()))
                    or assumptions.get("experiment_provenance") != provenance
                    or not (proposed or vetoed)
                    or (proposed and not decision.get("paper_trade_id"))
                    or (proposed and (
                        decision.get("trade_proposal_id")
                        != decision.get("proposal_id")
                        or decision.get("trade_scope_key")
                        != decision.get("scope_key")
                        or decision.get("trade_variant_id") != variant_id
                        or decision.get("trade_model_id") != model.model_id
                        or decision.get("trade_status")
                        not in {"OPEN", "CLOSED"}))
                    or (vetoed and (
                        decision.get("paper_trade_id")
                        or decision.get("trade_status") is not None))):
                raise ValueError(
                    "forward evidence contains mixed model, assumptions, "
                    "scope, experiment provenance, or decision linkage")

    @classmethod
    def _forward_verdict_from_payload(
            cls, conn: sqlite3.Connection, payload: dict):
        from agent.forward_models import require_validated
        from research import protocol

        source = payload.get("source_evidence") or {}
        strategy_id = str(payload.get("strategy_id") or "")
        model = require_validated(strategy_id)
        baseline_id = str(source.get("baseline_id") or "")
        setting_ids = [str(value) for value in source.get("setting_ids") or []]
        if (not baseline_id or len(setting_ids) < 1
                or len(set(setting_ids)) != len(setting_ids)):
            raise ValueError("forward source evidence has inconsistent arms")
        schema = source.get("schema")
        if schema == "paper_decision_evidence.v2":
            arms = source.get("arms")
            if (not isinstance(arms, dict)
                    or source.get("sha256") != _content_hash(arms)
                    or set(arms) != {baseline_id, *setting_ids}
                    or not all(isinstance(arm, dict)
                               for arm in arms.values())):
                raise ValueError(
                    "forward source evidence failed its content hash")
            cls._validate_forward_axis(
                conn, strategy_id, payload.get("axis"), baseline_id,
                setting_ids, arms)
            common_code_versions = set()
            for variant_id, arm in arms.items():
                variant = cls._require_variant(conn, variant_id)
                if variant["strategy_id"] != strategy_id:
                    raise ValueError("forward evidence mixes strategies")
                provenance = cls._validate_experiment_provenance(
                    variant, arm.get("experiment_provenance") or {})
                common_code_versions.add(provenance["code_version"])
                if provenance["forward_model_id"] != model.model_id:
                    raise ValueError("forward evidence mixes outcome models")
                if provenance["forward_model_assumptions_hash"] != _content_hash(
                        model.as_dict()):
                    raise ValueError("forward model assumptions changed")
                cls._validate_forward_arm_decisions(
                    payload, variant_id, provenance, arm.get("decisions"), model)
            if len(common_code_versions) != 1:
                raise ValueError("forward evidence mixes code versions")
            baseline = protocol.paper_trade_decisions(
                arms[baseline_id]["decisions"])
            settings = [
                (variant_id, protocol.paper_trade_decisions(
                    arms[variant_id]["decisions"]))
                for variant_id in setting_ids]
            return protocol.evaluate_axis(
                settings, baseline, strategy_id=strategy_id,
                calibrated_confirmation=False)

        if (schema != "paper_decision_evidence.v3"
                or source.get("sha256")
                != cls._forward_v3_source_hash(source)):
            raise ValueError("forward source evidence failed its content hash")
        settings_source = source.get("settings")
        eligibility = source.get("eligibility")
        if (not isinstance(settings_source, dict)
                or set(settings_source) != set(setting_ids)
                or not isinstance(eligibility, dict)):
            raise ValueError("forward source evidence has inconsistent settings")
        try:
            completed_through_ts = float(source["completed_through_ts"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "forward source evidence has no assignment cutoff") from exc
        if not math.isfinite(completed_through_ts):
            raise ValueError("forward assignment cutoff is not finite")

        baseline_provenance = eligibility.get("baseline_provenance")
        setting_provenances = eligibility.get("setting_provenances")
        setting_hashes = eligibility.get("setting_provenance_hashes")
        if (not isinstance(baseline_provenance, dict)
                or not isinstance(setting_provenances, dict)
                or set(setting_provenances) != set(setting_ids)
                or not isinstance(setting_hashes, dict)
                or set(setting_hashes) != set(setting_ids)
                or eligibility.get("baseline_provenance_hash")
                != _content_hash(baseline_provenance)):
            raise ValueError("forward eligibility provenance is inconsistent")
        expected_model_hash = _content_hash(model.as_dict())
        if (eligibility.get("forward_model_id") != model.model_id
                or eligibility.get("forward_model_assumptions_hash")
                != expected_model_hash):
            raise ValueError("forward model assumptions changed")

        axis_arms = {
            baseline_id: {"experiment_provenance": baseline_provenance}}
        settings = []
        assignment_ids = set()
        for variant_id in setting_ids:
            candidate_provenance = setting_provenances.get(variant_id)
            if (not isinstance(candidate_provenance, dict)
                    or setting_hashes.get(variant_id)
                    != _content_hash(candidate_provenance)):
                raise ValueError(
                    f"{variant_id}: forward eligibility provenance is invalid")
            for provenance in (baseline_provenance, candidate_provenance):
                if (provenance.get("code_version")
                        != eligibility.get("code_version")
                        or provenance.get("forward_model_id") != model.model_id
                        or provenance.get("forward_model_assumptions_hash")
                        != expected_model_hash):
                    raise ValueError(
                        "forward evidence mixes code or outcome-model versions")
            variant = cls._require_variant(conn, variant_id)
            cls._validate_experiment_provenance(
                variant, candidate_provenance)
            setting_source = settings_source.get(variant_id)
            if (not isinstance(setting_source, dict)
                    or setting_source.get("candidate_variant_id") != variant_id
                    or not isinstance(setting_source.get("assignments"), list)):
                raise ValueError(
                    f"{variant_id}: forward assignment evidence is invalid")
            expected_assignments = cls._eligible_forward_assignment_records(
                conn, str(payload.get("scope_key") or ""), strategy_id,
                baseline_id, variant_id, baseline_provenance,
                candidate_provenance, completed_through_ts)
            supplied_assignments = setting_source["assignments"]
            if not expected_assignments:
                raise ValueError(
                    f"{variant_id}: no eligible completed assignment evidence")
            if (_canonical_json(supplied_assignments)
                    != _canonical_json(expected_assignments)):
                raise ValueError(
                    f"{variant_id}: forward evidence does not contain every "
                    "eligible assignment attempt")

            candidate_decisions = []
            baseline_decisions = []
            for assignment in supplied_assignments:
                assignment_id = str(assignment.get("assignment_id") or "")
                if not assignment_id or assignment_id in assignment_ids:
                    raise ValueError(
                        "forward evidence contains duplicate assignments")
                assignment_ids.add(assignment_id)
                if (assignment.get("terminal_status") != "COMPLETED"
                        or assignment.get("candidate", {}).get("variant_id")
                        != variant_id
                        or assignment.get("baseline", {}).get("variant_id")
                        != baseline_id):
                    raise ValueError(
                        "forward evidence contains a mismatched assignment")
                baseline_arm = assignment["baseline"]
                candidate_arm = assignment["candidate"]
                if (baseline_arm.get("experiment_provenance")
                        != baseline_provenance
                        or candidate_arm.get("experiment_provenance")
                        != candidate_provenance):
                    raise ValueError(
                        "forward evidence mixes assignment provenance")
                cls._validate_forward_arm_decisions(
                    payload, baseline_id, baseline_provenance,
                    baseline_arm.get("decisions"), model)
                cls._validate_forward_arm_decisions(
                    payload, variant_id, candidate_provenance,
                    candidate_arm.get("decisions"), model)
                baseline_decisions.extend(baseline_arm["decisions"])
                candidate_decisions.extend(candidate_arm["decisions"])
            axis_arms[variant_id] = {
                "experiment_provenance": candidate_provenance}
            settings.append((
                variant_id,
                protocol.paper_trade_decisions(candidate_decisions),
                protocol.paper_trade_decisions(baseline_decisions)))

        baseline_variant = cls._require_variant(conn, baseline_id)
        cls._validate_experiment_provenance(
            baseline_variant, baseline_provenance)
        cls._validate_forward_axis(
            conn, strategy_id, payload.get("axis"), baseline_id,
            setting_ids, axis_arms)
        return protocol.evaluate_axis(
            settings, strategy_id=strategy_id,
            calibrated_confirmation=True)

    def record_forward_analysis(
            self, scope_key: str, strategy_id: str, axis: list[str],
            baseline_id: str, setting_ids: list[str]) -> tuple[str, object]:
        """Create assignment-scoped evidence from every eligible completed try."""
        from agent import state as runtime_state
        from agent.forward_models import require_validated

        model = require_validated(strategy_id)
        axis = _canonical_axis(axis)
        current_code = runtime_state.code_fingerprint()
        ordered_ids = [str(baseline_id)] + [str(value) for value in setting_ids]
        if len(set(ordered_ids)) != len(ordered_ids):
            raise ValueError("forward analysis arms must be unique")
        statuses = {}
        with _connect(self.path) as conn:
            provenances = {}
            for variant_id in ordered_ids:
                variant = self._require_variant(conn, variant_id)
                if variant["strategy_id"] != strategy_id:
                    raise ValueError("forward analysis cannot mix strategies")
                portfolio = conn.execute(
                    "SELECT state_json FROM paper_portfolios WHERE "
                    "scope_key=? AND variant_id=?",
                    (scope_key, variant_id)).fetchone()
                if portfolio is None:
                    raise ValueError(
                        f"{variant_id}: no enrolled paper portfolio")
                state = json.loads(portfolio["state_json"])
                provenance = self._validate_experiment_provenance(
                    variant, state.get("experiment_provenance") or {})
                if provenance["code_version"] != current_code:
                    raise ValueError(
                        f"{variant_id}: evidence code version is not current")
                if provenance["forward_model_id"] != model.model_id:
                    raise ValueError(
                        f"{variant_id}: evidence outcome model is not current")
                if provenance["forward_model_assumptions_hash"] != _content_hash(
                        model.as_dict()):
                    raise ValueError(
                        f"{variant_id}: forward model assumptions are not current")
                if state.get("status") == "REVOKED":
                    raise ValueError(
                        f"{variant_id}: revoked portfolio cannot contribute "
                        "forward evidence")
                provenances[variant_id] = provenance
                statuses[variant_id] = state.get("status")

            axis_arms = {
                variant_id: {"experiment_provenance": provenance}
                for variant_id, provenance in provenances.items()}
            self._validate_forward_axis(
                conn, strategy_id, axis, str(baseline_id),
                [str(value) for value in setting_ids], axis_arms)

            settings_source = {}
            completed_records = []
            for variant_id in map(str, setting_ids):
                assignments = self._eligible_forward_assignment_records(
                    conn, scope_key, strategy_id, str(baseline_id), variant_id,
                    provenances[str(baseline_id)], provenances[variant_id])
                if not assignments:
                    raise ValueError(
                        f"{variant_id}: no eligible completed assignment evidence")
                if any(record["axis"] not in axis for record in assignments):
                    raise ValueError(
                        f"{variant_id}: completed assignment does not match the "
                        "declared axis")
                settings_source[variant_id] = {
                    "candidate_variant_id": variant_id,
                    "assignments": assignments,
                }
                completed_records.extend(assignments)

            completed_through_ts = max(
                float(record["ended_ts"]) for record in completed_records)
            eligibility = {
                "code_version": current_code,
                "forward_model_id": model.model_id,
                "forward_model_assumptions_hash": _content_hash(model.as_dict()),
                "baseline_provenance": provenances[str(baseline_id)],
                "baseline_provenance_hash": _content_hash(
                    provenances[str(baseline_id)]),
                "setting_provenances": {
                    variant_id: provenances[variant_id]
                    for variant_id in map(str, setting_ids)},
                "setting_provenance_hashes": {
                    variant_id: _content_hash(provenances[variant_id])
                    for variant_id in map(str, setting_ids)},
            }
            source = {
                "schema": "paper_decision_evidence.v3",
                "baseline_id": str(baseline_id),
                "setting_ids": [str(value) for value in setting_ids],
                "completed_through_ts": completed_through_ts,
                "eligibility": eligibility,
                "settings": settings_source,
            }
            source["sha256"] = self._forward_v3_source_hash(source)
            provisional = {
                "source": "real_time_shadow_portfolios",
                "source_evidence": source,
                "scope_key": scope_key,
                "strategy_id": strategy_id,
                "axis": list(axis),
            }
            verdict = self._forward_verdict_from_payload(conn, provisional)
            corpus = [
                decision
                for record in completed_records
                for arm in (record["baseline"], record["candidate"])
                for decision in arm["decisions"]]
            resolved = [row for row in corpus
                        if row.get("decision_outcome") == "VETOED"
                        or (row.get("trade_status") == "CLOSED"
                            and int(row.get("trade_valid_for_inference") or 0) == 1
                            and row.get("trade_r_multiple") is not None)]
            payload = {
                **provisional,
                "settings": list(setting_ids),
                "portfolio_statuses": statuses,
                "verdict": verdict.verdict,
                "governing_criterion": verdict.governing_criterion,
                "detail": verdict.detail,
                "evidence": verdict.evidence,
                "corpus_from_ts": min(
                    (row["decision_ts"] for row in corpus), default=None),
                "corpus_to_ts": max(
                    (row["decision_ts"] for row in corpus), default=None),
                "resolved_outcomes": len(resolved),
                "code_version": current_code,
                "forward_model_id": model.model_id,
                "experiment_provenance": provenances,
            }
            subject = (
                f"{scope_key}:{strategy_id}:{'+'.join(map(str, axis))}")
            analysis_id = self._insert_analysis(
                conn, "forward_parameter_axis", subject, payload)
        self.backup()
        return analysis_id, verdict

    @staticmethod
    def _latest_qualification(
            conn: sqlite3.Connection, variant_id: str,
            scope_key: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM edge_qualification_events WHERE variant_id=? "
            "AND scope_key IN ('*', ?) ORDER BY ts DESC, event_id DESC LIMIT 1",
            (variant_id, scope_key)).fetchone()

    @classmethod
    def _validated_forward_analysis(
            cls, conn: sqlite3.Connection, variant_id: str,
            source_analysis_id: str | None, scope_key: str) -> dict:
        from agent import state as runtime_state
        from agent.forward_models import require_validated
        from research import protocol

        if not source_analysis_id:
            raise ValueError(
                "edge qualification requires a persisted forward analysis")
        variant = cls._require_variant(conn, variant_id)
        row = conn.execute(
            "SELECT * FROM analysis_runs WHERE analysis_id=?",
            (source_analysis_id,)).fetchone()
        if row is None:
            raise ValueError(
                f"unknown forward analysis {source_analysis_id!r}")
        payload = cls._analysis_payload(row)
        source = payload.get("source_evidence") or {}
        if source.get("schema") != "paper_decision_evidence.v3":
            raise ValueError(
                "edge qualification requires assignment-scoped forward "
                "evidence v3")
        eligibility = source.get("eligibility") or {}
        model = require_validated(str(variant["strategy_id"]))
        if (eligibility.get("code_version")
                != runtime_state.code_fingerprint()
                or eligibility.get("forward_model_id") != model.model_id
                or eligibility.get("forward_model_assumptions_hash")
                != _content_hash(model.as_dict())):
            raise ValueError(
                "forward analysis is not current-code/current-model evidence")
        current_provenances = {
            str(source.get("baseline_id") or ""):
                eligibility.get("baseline_provenance"),
            **dict(eligibility.get("setting_provenances") or {}),
        }
        for current_variant_id, expected_provenance in current_provenances.items():
            portfolio = conn.execute(
                "SELECT state_json FROM paper_portfolios WHERE scope_key=? "
                "AND variant_id=?", (scope_key, current_variant_id)).fetchone()
            if portfolio is None:
                raise ValueError(
                    "forward analysis portfolio provenance is unavailable")
            state = json.loads(portfolio["state_json"])
            current_variant = cls._require_variant(conn, current_variant_id)
            observed = cls._validate_experiment_provenance(
                current_variant, state.get("experiment_provenance") or {})
            if observed != expected_provenance or state.get("status") == "REVOKED":
                raise ValueError(
                    "forward analysis is not current-config portfolio evidence")
        recomputed = cls._forward_verdict_from_payload(conn, payload)
        stored_claim = {
            "verdict": payload.get("verdict"),
            "governing_criterion": payload.get("governing_criterion"),
            "detail": payload.get("detail"),
            "evidence": payload.get("evidence") or {},
        }
        recomputed_claim = {
            "verdict": recomputed.verdict,
            "governing_criterion": recomputed.governing_criterion,
            "detail": recomputed.detail,
            "evidence": recomputed.evidence,
        }
        if _canonical_json(stored_claim) != _canonical_json(recomputed_claim):
            raise ValueError(
                "forward analysis claims do not recompute from source evidence")
        evidence = payload.get("evidence") or {}
        paired = evidence.get("paired") or {}
        fit_paired = evidence.get("fit_paired") or {}
        confirmation_paired = evidence.get("confirmation_paired") or {}
        criteria = evidence.get("criteria") or {}
        required_criteria = {
            "min_round_trips", "min_axis_settings", "paired_sample",
            "paired_coverage", "no_duplicate_proposals",
            "paired_dependence_aware", "fit_paired_sample",
            "fit_paired_coverage", "fit_no_duplicate_proposals",
            "fit_dependence_aware", "confirmation_paired_sample",
            "confirmation_paired_coverage",
            "confirmation_no_duplicate_proposals",
            "confirmation_dependence_aware",
            "fit_interval_positive", "drawdown_no_worse",
            "out_of_sample_survives", "confirmation_interval_positive",
            "confirmation_randomization_valid",
        }

        def evidence_pair_ok(pair: dict, minimum: int) -> bool:
            bootstrap = pair.get("bootstrap") or {}
            return bool(
                int(pair.get("paired_n") or 0) >= minimum
                and float(pair.get("pair_coverage_pct") or 0)
                >= protocol.MIN_PAIR_COVERAGE_PCT
                and not int(pair.get("left_duplicates") or 0)
                and not int(pair.get("right_duplicates") or 0)
                and bootstrap.get("kind") == protocol.PAIR_BOOTSTRAP_KIND
                and int(bootstrap.get("cluster_seconds") or 0)
                == protocol.PAIR_CLUSTER_SECONDS)

        checks = {
            "analysis kind": row["kind"] == "forward_parameter_axis",
            "real-time source": (
                payload.get("source") == "real_time_shadow_portfolios"),
            "scope": payload.get("scope_key") == scope_key,
            "strategy": payload.get("strategy_id") == variant["strategy_id"],
            "promotion verdict": payload.get("verdict") == protocol.PROMOTE,
            "selected variant": evidence.get("best") == variant_id,
            "fit-only selection": evidence.get("selection_window") == "fit",
            "minimum sample": int(evidence.get("n") or 0)
            >= protocol.MIN_ROUND_TRIPS,
            "axis settings": int(evidence.get("axis_settings") or 0)
            >= protocol.MIN_AXIS_SETTINGS,
            "paired sample": int(paired.get("paired_n") or 0)
            >= protocol.MIN_ROUND_TRIPS,
            "paired coverage": float(paired.get("pair_coverage_pct") or 0)
            >= protocol.MIN_PAIR_COVERAGE_PCT,
            "no duplicates": not (
                int(paired.get("left_duplicates") or 0)
                or int(paired.get("right_duplicates") or 0)),
            "dependence-aware bootstrap": (
                evidence_pair_ok(paired, protocol.MIN_ROUND_TRIPS)),
            "fit paired evidence": evidence_pair_ok(
                fit_paired, protocol.MIN_PAIRED_FIT_OBSERVATIONS),
            "confirmation paired evidence": evidence_pair_ok(
                confirmation_paired,
                protocol.MIN_PAIRED_CONFIRM_OBSERVATIONS),
            "out-of-sample survival": (
                (evidence.get("split") or {}).get("survives") is True),
            "fit interval": float(
                (evidence.get("fit_interval") or {}).get("low") or 0) > 0,
            "confirmation interval": float(
                (evidence.get("confirmation_interval") or {}).get("low") or 0)
            > 0,
            "calibrated confirmation test": protocol.calibrated_p_value(
                evidence.get("confirmation_test")) is not None,
            "criteria": required_criteria.issubset(criteria)
            and all(criteria.get(name) is True for name in required_criteria),
        }
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            raise ValueError(
                "forward analysis does not satisfy qualification protocol: "
                + ", ".join(failed))
        return payload

    @classmethod
    def _validated_family_correction(
            cls, conn: sqlite3.Connection, variant_id: str,
            source_analysis_id: str, scope_key: str, detail: dict,
            source_payload: dict | None = None) -> dict:
        """Prove qualification is backed by the persisted axis family result."""
        from research import protocol

        if not isinstance(detail, dict):
            raise ValueError("edge qualification detail must be a mapping")
        family_analysis_id = str(detail.get("family_analysis_id") or "")
        if not family_analysis_id:
            raise ValueError(
                "edge qualification requires a persisted forward_axis_family "
                "analysis")

        variant = cls._require_variant(conn, variant_id)
        family_row = conn.execute(
            "SELECT * FROM analysis_runs WHERE analysis_id=?",
            (family_analysis_id,)).fetchone()
        if family_row is None or family_row["kind"] != "forward_axis_family":
            raise ValueError(
                "edge qualification requires a valid forward_axis_family "
                "analysis")
        family_payload = cls._analysis_payload(family_row)
        strategy_id = str(variant["strategy_id"])
        if (family_row["subject_id"] != f"{strategy_id}:{scope_key}"
                or family_payload.get("strategy_id") != strategy_id
                or family_payload.get("scope_key") != scope_key):
            raise ValueError(
                "family correction does not match the qualification "
                "strategy or scope")

        expected_axis_ids = set()
        baseline_id = f"{strategy_id.replace('-', '_')}.baseline"
        hypothesis_groups = {}
        for registered in conn.execute(
                "SELECT variant_id, strategy_id, status, overrides_json, "
                "hypothesis_id, hypothesis_params_json "
                "FROM variants"):
            if (registered["strategy_id"] != strategy_id
                    or registered["status"] not in {"candidate", "testing"}):
                continue
            hypothesis_id = registered["hypothesis_id"]
            if (hypothesis_id and str(registered["variant_id"]).startswith(
                    f"{strategy_id}.hyp.")):
                hypothesis_groups.setdefault(str(hypothesis_id), []).append(
                    registered)
                continue
            if registered["variant_id"] == baseline_id:
                continue
            try:
                overrides = json.loads(registered["overrides_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    "active registered variant has invalid overrides") from exc
            if not isinstance(overrides, dict):
                raise ValueError(
                    "active registered variant overrides are not a mapping")
            axis = tuple(sorted(str(path) for path in overrides))
            if axis:
                expected_axis_ids.add("+".join(axis))

        for hypothesis_id, rows in hypothesis_groups.items():
            baseline = next((row for row in rows
                             if str(row["variant_id"]).endswith(".registered")),
                            None)
            if baseline is None:
                continue
            params_by_id = {}
            for row in rows:
                try:
                    params = json.loads(row["hypothesis_params_json"] or "{}")
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        "active hypothesis variant has invalid params") from exc
                if not isinstance(params, dict):
                    raise ValueError(
                        "active hypothesis variant params are not a mapping")
                params_by_id[str(row["variant_id"])] = params
            keys = sorted({str(key) for params in params_by_id.values()
                           for key in params})
            if keys and len(rows) > 1:
                expected_axis_ids.add("+".join(
                    f"hypothesis.{hypothesis_id}.{key}" for key in keys))

        source_payload = source_payload or cls._validated_forward_analysis(
            conn, variant_id, source_analysis_id, scope_key)
        axis = _canonical_axis(source_payload.get("axis"))
        axis_id = "+".join(axis)
        supplied_axis = detail.get("axis")
        if supplied_axis is None or _canonical_axis(supplied_axis) != axis:
            raise ValueError(
                "family correction does not match the selected forward axis")

        axes = family_payload.get("axes")
        if not isinstance(axes, dict) or not axes:
            raise ValueError("forward_axis_family analysis has no axis records")
        if set(axes) != expected_axis_ids:
            raise ValueError(
                "forward_axis_family analysis does not contain the complete "
                "active axis set")
        selected = axes.get(axis_id)
        if not isinstance(selected, dict):
            raise ValueError(
                "forward_axis_family analysis does not contain the selected "
                "axis")
        if selected.get("analysis_id") != source_analysis_id:
            raise ValueError(
                "family correction is not linked to the selected forward "
                "analysis")

        persisted_verdicts = {}
        for persisted_axis_id, record in axes.items():
            if (not isinstance(persisted_axis_id, str)
                    or not isinstance(record, dict)):
                raise ValueError(
                    "forward_axis_family analysis has invalid axis records")
            analysis_id = str(record.get("analysis_id") or "")
            axis_row = conn.execute(
                "SELECT * FROM analysis_runs WHERE analysis_id=?",
                (analysis_id,)).fetchone()
            if axis_row is None or axis_row["kind"] != "forward_parameter_axis":
                raise ValueError(
                    "forward_axis_family analysis references an invalid axis "
                    "analysis")
            axis_payload = cls._analysis_payload(axis_row)
            if (axis_payload.get("strategy_id") != strategy_id
                    or axis_payload.get("scope_key") != scope_key
                    or "+".join(_canonical_axis(axis_payload.get("axis")))
                    != persisted_axis_id):
                raise ValueError(
                    "forward_axis_family analysis mixes strategy, scope, or "
                    "axis records")
            recomputed = cls._forward_verdict_from_payload(conn, axis_payload)
            stored_claim = {
                "verdict": axis_payload.get("verdict"),
                "governing_criterion": axis_payload.get("governing_criterion"),
                "detail": axis_payload.get("detail"),
                "evidence": axis_payload.get("evidence") or {},
            }
            recomputed_claim = {
                "verdict": recomputed.verdict,
                "governing_criterion": recomputed.governing_criterion,
                "detail": recomputed.detail,
                "evidence": recomputed.evidence,
            }
            if _canonical_json(stored_claim) != _canonical_json(recomputed_claim):
                raise ValueError(
                    "family axis analysis does not recompute from evidence")
            persisted_verdicts[persisted_axis_id] = recomputed

        corrected = protocol.apply_axis_family_correction(persisted_verdicts)
        expected = corrected[axis_id]
        expected_record = {
            "analysis_id": source_analysis_id,
            "verdict_before_correction": source_payload.get("verdict"),
            "verdict": expected.verdict,
            "governing_criterion": expected.governing_criterion,
            "family": expected.evidence.get("family"),
        }
        if _canonical_json(selected) != _canonical_json(expected_record):
            raise ValueError(
                "forward_axis_family analysis failed its correction check")
        family = expected.evidence.get("family") or {}
        if expected.verdict != protocol.PROMOTE or not family.get("significant"):
            raise ValueError(
                "selected forward axis did not survive the family correction")
        return json.loads(_canonical_json(family))

    def qualify_variant(
            self, variant_id: str, detail: dict, *,
            source_analysis_id: str | None = None,
            scope_key: str = "*") -> str:
        from agent.forward_models import require_validated

        with _connect(self.path) as conn:
            variant = self._require_variant(conn, variant_id)
            require_validated(str(variant["strategy_id"]))
            source_payload = self._validated_forward_analysis(
                conn, variant_id, source_analysis_id, scope_key)
            family = self._validated_family_correction(
                conn, variant_id, str(source_analysis_id or ""), scope_key,
                detail, source_payload)
            detail = dict(detail)
            detail["family"] = family
            detail.setdefault("variant_id", variant_id)
            detail.setdefault("hypothesis_id", variant["hypothesis_id"])
            detail.setdefault(
                "hypothesis_params",
                json.loads(variant["hypothesis_params_json"] or "{}"))
            detail.setdefault(
                "exact_overrides", json.loads(variant["overrides_json"] or "{}"))
            current = self._latest_qualification(
                conn, variant_id, scope_key)
            if current and current["status"] == "QUALIFIED":
                return str(current["event_id"])
            event_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO edge_qualification_events (event_id, variant_id, "
                "scope_key, status, ts, source_analysis_id, detail_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (event_id, variant_id, scope_key, "QUALIFIED", time.time(),
                 source_analysis_id,
                json.dumps(detail, sort_keys=True, default=str)))
        return event_id

    def revoke_variant(
            self, variant_id: str, detail: dict, *,
            scope_key: str = "*") -> str:
        current = self.qualification_status(variant_id, scope_key)
        if current and current["status"] == "REVOKED":
            return str(current["event_id"])
        event_id = uuid.uuid4().hex
        with _connect(self.path) as conn:
            self._require_variant(conn, variant_id)
            conn.execute(
                "INSERT INTO edge_qualification_events (event_id, variant_id, "
                "scope_key, status, ts, detail_json) VALUES (?,?,?,?,?,?)",
                (event_id, variant_id, scope_key, "REVOKED", time.time(),
                 json.dumps(detail, sort_keys=True, default=str)))
        return event_id

    # --------------------------------------------------------- evidence links

    def record_run_evidence(self, run_id: str, evidence: dict) -> None:
        with _connect(self.path) as conn:
            conn.execute(
                "INSERT INTO run_evidence (run_id, evidence_json, created_ts) "
                "VALUES (?,?,?)",
                (run_id, json.dumps(evidence, sort_keys=True, default=str),
                 time.time()))

    def run_evidence(self, run_id: str) -> dict | None:
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT evidence_json FROM run_evidence WHERE run_id=?",
                (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _insert_analysis(
            conn: sqlite3.Connection, kind: str, subject_id: str,
            payload: dict) -> str:
        return _insert_analysis_conn(conn, kind, subject_id, payload)

    def record_analysis(self, kind: str, subject_id: str, payload: dict) -> str:
        if kind == "forward_parameter_axis":
            raise ValueError(
                "forward_parameter_axis analyses must be created from "
                "persisted trades by record_forward_analysis")
        with _connect(self.path) as conn:
            analysis_id = self._insert_analysis(
                conn, kind, subject_id, payload)
        self.backup()
        return analysis_id

    def analysis(self, analysis_id: str) -> dict | None:
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT * FROM analysis_runs WHERE analysis_id=?",
                (analysis_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = self._analysis_payload(row)
        item.pop("payload_json")
        return item

    @classmethod
    def _t3_evidence_is_backed_by_store(
            cls, conn: sqlite3.Connection, variant_id: str,
            payload: dict) -> bool:
        """Refuse reviewed status for a caller-authored checklist alone."""
        from research import protocol

        try:
            if payload.get("variant_id") != variant_id:
                return False
            scope_key = str(payload.get("scope_key") or "")
            if not scope_key:
                return False
            qualification = cls._latest_qualification(
                conn, variant_id, scope_key)
            if qualification is None or qualification["status"] != "QUALIFIED":
                return False
            supplied_qualification = payload.get("qualification") or {}
            if supplied_qualification.get("event_id") != qualification["event_id"]:
                return False
            analysis_id = str(qualification["source_analysis_id"] or "")
            source_payload = cls._validated_forward_analysis(
                conn, variant_id, analysis_id, scope_key)
            qualification_detail = json.loads(qualification["detail_json"])
            cls._validated_family_correction(
                conn, variant_id, analysis_id, scope_key,
                qualification_detail, source_payload)
            supplied_analysis = payload.get("forward_analysis") or {}
            if supplied_analysis.get("analysis_id") != analysis_id:
                return False
            source_evidence = source_payload.get("source_evidence") or {}
            selected_setting = (
                (source_evidence.get("settings") or {}).get(variant_id) or {})
            assignment_ids = {
                str(item.get("assignment_id") or "")
                for item in selected_setting.get("assignments") or []
                if isinstance(item, dict) and item.get("assignment_id")}
            if not assignment_ids:
                return False
            placeholders = ",".join("?" for _ in assignment_ids)
            edge_rows = conn.execute(
                "SELECT * FROM research_edge_evidence WHERE scope_key=? "
                "AND variant_id=? AND assignment_id IN (" + placeholders + ") "
                "ORDER BY created_ts, edge_id",
                (scope_key, variant_id, *sorted(assignment_ids))).fetchall()
            expected_edges = [cls._edge_candidate_dict(row) for row in edge_rows]
            if (not expected_edges
                    or _canonical_json(payload.get("edge_candidates") or [])
                    != _canonical_json(expected_edges)):
                return False

            portfolio = conn.execute(
                "SELECT state_json FROM paper_portfolios "
                "WHERE scope_key=? AND variant_id=?",
                (scope_key, variant_id)).fetchone()
            if portfolio is None:
                return False
            state = json.loads(portfolio["state_json"])
            paper_started = state.get("paper_started_ts")
            if (state.get("status") != "PAPER" or paper_started is None
                    or state.get("revoked_reason")):
                return False
            paper = conn.execute(
                "SELECT COUNT(*) AS n, AVG(r_multiple) AS expectancy "
                "FROM paper_trades WHERE scope_key=? AND variant_id=? "
                "AND status='CLOSED' AND valid_for_inference=1 "
                "AND entry_ts>=?",
                (scope_key, variant_id, float(paper_started))).fetchone()
            if (int(paper["n"] or 0) < protocol.MIN_ROUND_TRIPS
                    or paper["expectancy"] is None
                    or float(paper["expectancy"]) <= 0):
                return False

            g2 = payload.get("g2") or {}
            provenance = payload.get("current_provenance") or {}
            if g2.get("status") != "PASS":
                return False
            if (g2.get("strategy_config_version")
                    != provenance.get("strategy_config_version")):
                return False
            if (g2.get("fidelity_code_version")
                    != provenance.get("fidelity_code_version")):
                return False
            return True
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def create_t3_packet(
            self, variant_id: str, payload: dict, *, reviewed_by: str = "",
            registry_change_ref: str = "") -> str:
        with _connect(self.path) as conn:
            self._require_variant(conn, variant_id)
            checklist = payload.get("checklist") or {}
            complete = (
                T3_REQUIRED_CHECKS.issubset(checklist)
                and all(bool(checklist[key]) for key in T3_REQUIRED_CHECKS)
                and self._t3_evidence_is_backed_by_store(
                    conn, variant_id, payload))
            status = (
                "REVIEWED"
                if reviewed_by and registry_change_ref and complete
                else "DRAFT_REVIEW_REQUIRED")
            canonical, payload_hash = _t3_canonical(
                variant_id, payload, status, reviewed_by or None,
                registry_change_ref or None)
            packet_id = payload_hash[:32]
            existing = conn.execute(
                "SELECT packet_id FROM t3_evidence_packets "
                "WHERE payload_hash=?", (payload_hash,)).fetchone()
            if existing:
                return str(existing[0])
            conn.execute(
                "INSERT INTO t3_evidence_packets (packet_id, variant_id, "
                "created_ts, review_status, reviewed_by, registry_change_ref, "
                "payload_json, payload_hash) VALUES (?,?,?,?,?,?,?,?)",
                (packet_id, variant_id, time.time(), status,
                 reviewed_by or None, registry_change_ref or None,
                 canonical, payload_hash))
        self.backup()
        return packet_id

    def t3_evidence_is_backed(self, variant_id: str, payload: dict) -> bool:
        """Return whether a review payload recomputes from persisted evidence."""
        with _connect(self.path) as conn:
            self._require_variant(conn, variant_id)
            return self._t3_evidence_is_backed_by_store(
                conn, variant_id, payload)

    def t3_packets_for(self, variant_id: str) -> list:
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT * FROM t3_evidence_packets WHERE variant_id=? "
                "ORDER BY created_ts", (variant_id,)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            out.append(item)
        return out

    # ------------------------------------------------------------- variants

    @staticmethod
    def _register_variant(
            conn: sqlite3.Connection, variant, now: float) -> None:
        overrides = json.dumps(variant.overrides, sort_keys=True)
        existing = conn.execute(
            "SELECT * FROM variants WHERE variant_id=?",
            (variant.variant_id,)).fetchone()
        if existing is not None:
            if _variant_identity(existing) != _variant_identity(variant):
                raise ValueError(
                    f"{variant.variant_id}: registered experiment identity "
                    "is immutable; use a new variant_id for changes to "
                    "strategy, version, overrides, or hypothesis")
            if existing["status"] != variant.status:
                conn.execute(
                    "UPDATE variants SET status=?, updated_ts=? "
                    "WHERE variant_id=?",
                    (variant.status, now, variant.variant_id))
            return
        if "hypothesis_id" in {
                str(row[1]) for row in conn.execute(
                    "PRAGMA table_info(variants)").fetchall()}:
            conn.execute(
                "INSERT INTO variants (variant_id, strategy_id, "
                "base_version, overrides_json, hypothesis, status, "
                "created_ts, updated_ts, hypothesis_id, hypothesis_params_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (variant.variant_id, variant.strategy_id,
                 variant.base_version, overrides, variant.hypothesis,
                 variant.status, now, now, getattr(variant, "hypothesis_id", None),
                 _canonical_json(
                     getattr(variant, "hypothesis_params", {}) or {})))
        else:
            conn.execute(
                "INSERT INTO variants (variant_id, strategy_id, "
                "base_version, overrides_json, hypothesis, status, "
                "created_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?)",
                (variant.variant_id, variant.strategy_id,
                 variant.base_version, overrides, variant.hypothesis,
                 variant.status, now, now))

    def register(self, variant) -> None:
        """Register immutable experiment identity; status alone may change.

        ``updated_ts`` reaches the committed index, so bumping it on every
        run would make ``research.py report`` produce a diff every time it
        was invoked - and a diff that always appears is a diff nobody reads.
        """
        now = time.time()
        with _connect(self.path) as conn:
            self._register_variant(conn, variant, now)

    @staticmethod
    def _proposal_dict(
            conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
        item = dict(row)
        item["value"] = json.loads(item["value_json"])
        item["minimum"] = json.loads(item["minimum_json"])
        item["maximum"] = json.loads(item["maximum_json"])
        latest = conn.execute(
            "SELECT status FROM hypothesis_proposal_events "
            "WHERE proposal_id=? ORDER BY event_ts DESC, rowid DESC LIMIT 1",
            (item["proposal_id"],)).fetchone()
        item["current_status"] = (
            str(latest["status"]) if latest is not None else item["status"])
        return item

    def hypothesis_proposals(self, strategy_id: str, hypothesis_id: str) -> list:
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT * FROM hypothesis_proposals WHERE strategy_id=? "
                "AND hypothesis_id=? ORDER BY proposed_ts, proposal_id",
                (strategy_id, hypothesis_id)).fetchall()
            return [self._proposal_dict(conn, row) for row in rows]

    def hypothesis_proposal(self, proposal_id: str) -> dict | None:
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT * FROM hypothesis_proposals WHERE proposal_id=?",
                (proposal_id,)).fetchone()
            return self._proposal_dict(conn, row) if row is not None else None

    def proposal_events(self, proposal_id: str) -> list[dict]:
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT * FROM hypothesis_proposal_events WHERE proposal_id=? "
                "ORDER BY event_ts, rowid", (proposal_id,)).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event["detail"] = json.loads(event.pop("detail_json"))
            events.append(event)
        return events

    @staticmethod
    def _append_proposal_event_conn(
            conn: sqlite3.Connection, proposal_id: str, status: str,
            detail: dict | None, timestamp: float) -> dict:
        if conn.execute(
                "SELECT 1 FROM hypothesis_proposals WHERE proposal_id=?",
                (proposal_id,)).fetchone() is None:
            raise ValueError(f"unknown proposal_id {proposal_id!r}")
        latest = conn.execute(
            "SELECT * FROM hypothesis_proposal_events "
            "WHERE proposal_id=? ORDER BY event_ts DESC, rowid DESC LIMIT 1",
            (proposal_id,)).fetchone()
        if latest is None:
            raise ValueError("proposal lifecycle has no initial event")
        if latest["status"] == status:
            event = dict(latest)
            event["detail"] = json.loads(event.pop("detail_json"))
            return event
        if latest["status"] in {"OBSERVED", "REJECTED"}:
            raise ValueError("proposal lifecycle is already terminal")
        if status == "OBSERVED" and latest["status"] != "OBSERVING":
            raise ValueError("proposal must be observing before it is observed")
        event_id = uuid.uuid4().hex
        detail_json = _canonical_json(detail or {})
        conn.execute(
            "INSERT INTO hypothesis_proposal_events "
            "(event_id, proposal_id, status, event_ts, detail_json) "
            "VALUES (?,?,?,?,?)",
            (event_id, proposal_id, status, timestamp, detail_json))
        return {
            "event_id": event_id, "proposal_id": proposal_id,
            "status": status, "event_ts": timestamp,
            "detail": json.loads(detail_json),
        }

    def append_proposal_event(
            self, proposal_id: str, status: str, detail: dict | None = None,
            *, now: float | None = None) -> dict:
        """Append one valid lifecycle transition without mutating history."""
        status = str(status).upper()
        if status not in {"OBSERVING", "OBSERVED", "REJECTED"}:
            raise ValueError("proposal event status is invalid")
        timestamp = time.time() if now is None else float(now)
        with _connect(self.path) as conn:
            return self._append_proposal_event_conn(
                conn, proposal_id, status, detail, timestamp)

    def pending_hypothesis_proposals(self, strategy_id: str) -> list[dict]:
        """Return non-terminal exact adaptive proposals in queue order."""
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT proposal.* FROM hypothesis_proposals AS proposal "
                "WHERE proposal.strategy_id=? AND COALESCE(("
                "SELECT event.status FROM hypothesis_proposal_events AS event "
                "WHERE event.proposal_id=proposal.proposal_id "
                "ORDER BY event.event_ts DESC, event.rowid DESC LIMIT 1"
                "), proposal.status) NOT IN ('OBSERVED','REJECTED') "
                "ORDER BY proposal.proposed_ts, proposal.proposal_id",
                (strategy_id,)).fetchall()
            return [self._proposal_dict(conn, row) for row in rows]

    @staticmethod
    def _research_selection_event(
            conn: sqlite3.Connection, selection_id: str
            ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM research_selection_events WHERE selection_id=? "
            "ORDER BY event_ts DESC, rowid DESC LIMIT 1",
            (selection_id,)).fetchone()

    @classmethod
    def _research_selection_dict(
            cls, conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
        item = dict(row)
        item["request"] = json.loads(item.pop("request_json"))
        latest = cls._research_selection_event(conn, item["selection_id"])
        item["current_status"] = (
            str(latest["status"]) if latest is not None else None)
        item["status_reason"] = (
            str(latest["reason"]) if latest is not None and latest["reason"]
            else None)
        item["assignment_id"] = (
            str(latest["assignment_id"])
            if latest is not None and latest["assignment_id"] else None)
        return item

    @classmethod
    def _append_research_selection_event_conn(
            cls, conn: sqlite3.Connection, selection_id: str, status: str,
            *, reason: str | None = None, assignment_id: str | None = None,
            detail: dict | None = None, timestamp: float) -> dict:
        status = str(status).upper()
        if status not in {"ACCEPTED", "ASSIGNED", "TESTED", "REJECTED"}:
            raise ValueError("research selection event status is invalid")
        selection = conn.execute(
            "SELECT * FROM research_selections WHERE selection_id=?",
            (selection_id,)).fetchone()
        if selection is None:
            raise ValueError(f"unknown research selection {selection_id!r}")
        latest = cls._research_selection_event(conn, selection_id)
        if latest is not None and latest["status"] == status:
            event = dict(latest)
            event["detail"] = json.loads(event.pop("detail_json"))
            return event
        if latest is None:
            if status not in {"ACCEPTED", "REJECTED"}:
                raise ValueError("research selection requires an initial status")
        elif latest["status"] in {"TESTED", "REJECTED"}:
            raise ValueError("research selection lifecycle is already terminal")
        elif latest["status"] == "ACCEPTED" and status not in {
                "ASSIGNED", "REJECTED"}:
            raise ValueError("accepted research selection must be assigned first")
        elif latest["status"] == "ASSIGNED" and status not in {
                "TESTED", "REJECTED"}:
            raise ValueError("assigned research selection has invalid transition")
        if status == "REJECTED" and not str(reason or "").strip():
            raise ValueError("research selection rejection reason is required")
        if status in {"ASSIGNED", "TESTED"} and not assignment_id:
            raise ValueError("research selection assignment link is required")
        event_id = uuid.uuid4().hex
        detail_json = _canonical_json(detail or {})
        conn.execute(
            "INSERT INTO research_selection_events "
            "(event_id, selection_id, status, event_ts, reason, assignment_id, "
            "detail_json) VALUES (?,?,?,?,?,?,?)",
            (event_id, selection_id, status, timestamp,
             str(reason).strip() if reason else None, assignment_id,
             detail_json))
        return {
            "event_id": event_id, "selection_id": selection_id,
            "status": status, "event_ts": timestamp,
            "reason": str(reason).strip() if reason else None,
            "assignment_id": assignment_id,
            "detail": json.loads(detail_json),
        }

    def record_research_selection(
            self, selection: dict, candidates: list[dict], *,
            scope_key: str, run_id: str, cycle_id: str | None,
            model_id: str, prompt_version: str,
            validation_error: str | None = None,
            now: float | None = None) -> dict:
        """Resolve and append one accepted or rejected selector request."""
        timestamp = time.time() if now is None else float(now)
        requested_strategy = str(selection.get("strategy_id") or "").strip()
        requested_variant = str(selection.get("variant_id") or "").strip()
        reasoning_value = selection.get("reasoning")
        reasoning = reasoning_value if isinstance(reasoning_value, str) else ""
        request = selection.get("request", selection)
        resolved_variant = None
        resolved_retry_of = None
        rejection = str(validation_error or "").strip() or None

        with _connect(self.path) as conn:
            static_candidates = [
                dict(candidate) for candidate in candidates
                if str(candidate.get("source") or "static") == "static"
            ]
            by_variant = {
                str(candidate.get("variant_id") or ""): candidate
                for candidate in static_candidates
                if candidate.get("variant_id")
            }
            active = conn.execute(
                "SELECT assignment.candidate_variant_id "
                "FROM strategy_experiment_active_slots AS slot "
                "JOIN strategy_experiment_assignments AS assignment "
                "ON assignment.assignment_id=slot.assignment_id "
                "WHERE slot.scope_key=? AND slot.strategy_id=?",
                (scope_key, requested_strategy)).fetchone()
            active_variant = (
                str(active["candidate_variant_id"]) if active is not None else None)
            terminal_rows = self._terminal_assignment_rows(
                conn, scope_key, requested_strategy)
            terminal_variants = {
                str(row["candidate_variant_id"]) for row in terminal_rows}
            terminal_keys = {str(row["candidate_key"]) for row in terminal_rows}
            retryable = {}
            for row in sorted(
                    terminal_rows, key=lambda item: int(item["attempt"] or 1)):
                retryable[str(row["candidate_variant_id"])] = row
            pending_rows = conn.execute(
                "SELECT selection.resolved_variant_id "
                "FROM research_selections AS selection "
                "WHERE selection.scope_key=? "
                "AND selection.requested_strategy_id=? "
                "AND (SELECT event.status FROM research_selection_events AS event "
                "WHERE event.selection_id=selection.selection_id "
                "ORDER BY event.event_ts DESC, event.rowid DESC LIMIT 1)='ACCEPTED'",
                (scope_key, requested_strategy)).fetchall()
            pending_variants = {
                str(row["resolved_variant_id"]) for row in pending_rows
                if row["resolved_variant_id"]}

            def eligible(
                    candidate: dict, *, explicit: bool = False
                    ) -> tuple[bool, str | None, str | None]:
                variant_id = str(candidate.get("variant_id") or "")
                candidate_key = str(candidate.get("candidate_key") or "")
                if not variant_id or not candidate_key:
                    return False, "candidate identity is incomplete", None
                registered = conn.execute(
                    "SELECT status FROM variants WHERE variant_id=?",
                    (variant_id,)).fetchone()
                if registered is None:
                    return False, "candidate is no longer registered", None
                if registered["status"] not in {"candidate", "testing"}:
                    return False, (
                        f"candidate status is {registered['status']}, not eligible"
                    ), None
                if variant_id == active_variant:
                    return False, "candidate is already the active assignment", None
                if variant_id in pending_variants:
                    return False, (
                        "exact candidate already has a pending selection"), None
                if variant_id in terminal_variants or candidate_key in terminal_keys:
                    previous = retryable.get(variant_id)
                    if (explicit
                            and self._terminal_assignment_retryable(previous)
                            and str(previous["candidate_key"]) == candidate_key):
                        return True, None, str(previous["assignment_id"])
                    return False, (
                        "silent retest refused: exact candidate already reached "
                        "a conclusive result, or an inconclusive retry was not "
                        "explicitly requested"
                    ), None
                return True, None, None

            if rejection is None:
                if requested_variant:
                    candidate = by_variant.get(requested_variant)
                    if candidate is None:
                        rejection = (
                            "requested variant is not an eligible registered "
                            "single-axis setting for this strategy")
                    else:
                        is_eligible, why, retry_of = eligible(
                            candidate, explicit=True)
                        if is_eligible:
                            resolved_variant = requested_variant
                            resolved_retry_of = retry_of
                        else:
                            rejection = why
                else:
                    ordered = sorted(static_candidates, key=lambda item: (
                        int(item.get("priority", 100)),
                        str(item.get("order_key") or ""),
                        str(item.get("variant_id") or ""),
                    ))
                    for candidate in ordered:
                        previous = retryable.get(
                            str(candidate.get("variant_id") or ""))
                        if (previous is None
                                or str(previous["candidate_key"])
                                != str(candidate.get("candidate_key") or "")
                                or not self._terminal_assignment_automatic_retryable(
                                    previous)
                                or int(previous["attempt"] or 1)
                                >= MAX_AUTOMATIC_EXPERIMENT_ATTEMPTS):
                            continue
                        is_eligible, _, retry_of = eligible(
                            candidate, explicit=True)
                        if is_eligible:
                            resolved_variant = str(candidate["variant_id"])
                            resolved_retry_of = retry_of
                            break
                if rejection is None and resolved_variant is None:
                    ordered = sorted(static_candidates, key=lambda item: (
                        int(item.get("priority", 100)),
                        str(item.get("order_key") or ""),
                        str(item.get("variant_id") or ""),
                    ))
                    for candidate in ordered:
                        is_eligible, _, _ = eligible(candidate)
                        if is_eligible:
                            resolved_variant = str(candidate["variant_id"])
                            break
                    if resolved_variant is None:
                        rejection = (
                            "no eligible untested single-axis setting remains "
                            "for the requested strategy")

            selection_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO research_selections "
                "(selection_id, scope_key, run_id, cycle_id, model_id, "
                "prompt_version, requested_strategy_id, requested_variant_id, "
                "resolved_variant_id, original_reasoning, request_json, "
                "requested_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (selection_id, str(scope_key), str(run_id),
                 str(cycle_id) if cycle_id else None, str(model_id),
                 str(prompt_version), requested_strategy,
                 requested_variant or None, resolved_variant, reasoning,
                 _canonical_json(request), timestamp))
            if rejection is None:
                self._append_research_selection_event_conn(
                    conn, selection_id, "ACCEPTED", timestamp=timestamp,
                    detail={
                        "resolved_variant_id": resolved_variant,
                        "retry_of_assignment_id": resolved_retry_of,
                    })
            else:
                self._append_research_selection_event_conn(
                    conn, selection_id, "REJECTED", reason=rejection,
                    timestamp=timestamp,
                    detail={"resolved_variant_id": resolved_variant})
            row = conn.execute(
                "SELECT * FROM research_selections WHERE selection_id=?",
                (selection_id,)).fetchone()
            return self._research_selection_dict(conn, row)

    def research_selection(self, selection_id: str) -> dict | None:
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT * FROM research_selections WHERE selection_id=?",
                (selection_id,)).fetchone()
            return (self._research_selection_dict(conn, row)
                    if row is not None else None)

    def research_selections(
            self, scope_key: str | None = None,
            strategy_id: str | None = None) -> list[dict]:
        clauses = []
        params: list = []
        if scope_key is not None:
            clauses.append("scope_key=?")
            params.append(str(scope_key))
        if strategy_id is not None:
            clauses.append("requested_strategy_id=?")
            params.append(str(strategy_id))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT * FROM research_selections" + where +
                " ORDER BY requested_ts, selection_id", params).fetchall()
            return [self._research_selection_dict(conn, row) for row in rows]

    def research_selection_events(self, selection_id: str) -> list[dict]:
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT * FROM research_selection_events WHERE selection_id=? "
                "ORDER BY event_ts, rowid", (selection_id,)).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event["detail"] = json.loads(event.pop("detail_json"))
            events.append(event)
        return events

    @staticmethod
    def _terminal_assignment_rows(
            conn: sqlite3.Connection, scope_key: str,
            strategy_id: str) -> list[sqlite3.Row]:
        """Read terminal lineage across both legacy and schema-15 stores."""
        has_outcomes = _table_exists(conn, "experiment_outcomes")
        has_attempt = _stored_version(conn) >= 15
        attempt = "assignment.attempt" if has_attempt else "1"
        join = (
            "LEFT JOIN experiment_outcomes AS outcome "
            "ON outcome.assignment_id=assignment.assignment_id "
            if has_outcomes else "")
        verdict = "outcome.verdict" if has_outcomes else "NULL"
        payload = "outcome.payload_json" if has_outcomes else "NULL"
        return conn.execute(
            "SELECT assignment.candidate_variant_id, "
            "assignment.candidate_key, assignment.assignment_id, "
            f"{attempt} AS attempt, {verdict} AS verdict, "
            f"{payload} AS outcome_payload_json "
            "FROM strategy_experiment_assignments AS assignment "
            + join
            + "WHERE assignment.scope_key=? AND assignment.strategy_id=? "
            "AND (SELECT event.status "
            "FROM strategy_experiment_assignment_events AS event "
            "WHERE event.assignment_id=assignment.assignment_id "
            "ORDER BY event.event_ts DESC, event.rowid DESC LIMIT 1) "
            "IN ('COMPLETED','REJECTED') "
            "ORDER BY attempt, assignment.started_ts, assignment.assignment_id",
            (scope_key, strategy_id)).fetchall()

    @staticmethod
    def _terminal_assignment_retryable(row: sqlite3.Row | None) -> bool:
        if row is None or row["verdict"] != "INCONCLUSIVE":
            return False
        try:
            payload = json.loads(row["outcome_payload_json"] or "null")
        except (TypeError, json.JSONDecodeError):
            return False
        return _retryable_inconclusive_payload(payload)

    @staticmethod
    def _terminal_assignment_automatic_retryable(
            row: sqlite3.Row | None) -> bool:
        if row is None or row["verdict"] != "INCONCLUSIVE":
            return False
        try:
            payload = json.loads(row["outcome_payload_json"] or "null")
        except (TypeError, json.JSONDecodeError):
            return False
        return _automatic_retry_payload(payload)

    def prioritized_experiment_candidates(
            self, scope_key: str, strategy_id: str,
            candidates: list[dict], *, now: float | None = None) -> list[dict]:
        """Apply durable selector priority and reject stale queued targets."""
        timestamp = time.time() if now is None else float(now)
        out = [dict(candidate) for candidate in candidates]
        available = {
            str(candidate.get("variant_id") or ""): candidate
            for candidate in out
            if (candidate.get("variant_id")
                and str(candidate.get("source") or "static") == "static")
        }
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT selection.* FROM research_selections AS selection "
                "WHERE selection.scope_key=? "
                "AND selection.requested_strategy_id=? "
                "AND (SELECT event.status FROM research_selection_events AS event "
                "WHERE event.selection_id=selection.selection_id "
                "ORDER BY event.event_ts DESC, event.rowid DESC LIMIT 1)='ACCEPTED' "
                "ORDER BY selection.requested_ts, selection.selection_id",
                (scope_key, strategy_id)).fetchall()
            terminal_rows = self._terminal_assignment_rows(
                conn, scope_key, strategy_id)
            terminal = {
                str(row["candidate_variant_id"]): row
                for row in terminal_rows}
            priority_by_variant = {}
            for ordinal, row in enumerate(rows):
                selection_id = str(row["selection_id"])
                variant_id = str(row["resolved_variant_id"] or "")
                candidate = available.get(variant_id)
                accepted = self._research_selection_event(conn, selection_id)
                accepted_detail = (
                    json.loads(accepted["detail_json"])
                    if accepted is not None else {})
                retry_of = str(
                    accepted_detail.get("retry_of_assignment_id")
                    or "").strip() or None
                registered = conn.execute(
                    "SELECT status FROM variants WHERE variant_id=?",
                    (variant_id,)).fetchone()
                reason = None
                terminal_row = terminal.get(variant_id)
                retry_valid = bool(
                    self._terminal_assignment_retryable(terminal_row)
                    and retry_of == str(terminal_row["assignment_id"]))
                if terminal_row is not None and not retry_valid:
                    reason = (
                        "exact candidate became terminal before assignment; "
                        "only an explicit retry of its latest INCONCLUSIVE "
                        "assignment is allowed")
                elif candidate is None:
                    reason = "resolved exact candidate is no longer available"
                elif (registered is None
                      or registered["status"] not in {"candidate", "testing"}):
                    reason = "resolved exact candidate is no longer eligible"
                if reason:
                    self._append_research_selection_event_conn(
                        conn, selection_id, "REJECTED", reason=reason,
                        timestamp=timestamp)
                    continue
                priority_by_variant[variant_id] = {
                    "selection_id": selection_id,
                    "retry_of_assignment_id": retry_of,
                    "priority": -100,
                    "order_key": (
                        f"{float(row['requested_ts']):020.6f}:"
                        f"{ordinal:08d}:{selection_id}"),
                }
            for variant_id, candidate in sorted(available.items()):
                if variant_id in priority_by_variant:
                    continue
                terminal_row = terminal.get(variant_id)
                if (terminal_row is None
                        or str(terminal_row["candidate_key"])
                        != str(candidate.get("candidate_key") or "")
                        or not self._terminal_assignment_automatic_retryable(
                            terminal_row)
                        or int(terminal_row["attempt"] or 1)
                        >= MAX_AUTOMATIC_EXPERIMENT_ATTEMPTS):
                    continue
                priority_by_variant[variant_id] = {
                    "retry_of_assignment_id": str(
                        terminal_row["assignment_id"]),
                    # Explicit accepted selections (-100) and exact adaptive
                    # proposals (0) retain their durable precedence. A retry
                    # still runs before untouched static settings (normally 10).
                    "priority": 1,
                    "order_key": (
                        f"automatic-retry:{int(terminal_row['attempt'] or 1):08d}:"
                        f"{terminal_row['assignment_id']}"),
                }
        for index, candidate in enumerate(out):
            selected = priority_by_variant.get(
                str(candidate.get("variant_id") or ""))
            if selected:
                out[index] = {**candidate, **selected}
        return out

    @staticmethod
    def _assignment_event(
            conn: sqlite3.Connection, assignment_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM strategy_experiment_assignment_events "
            "WHERE assignment_id=? "
            "ORDER BY event_ts DESC, rowid DESC LIMIT 1",
            (assignment_id,)).fetchone()

    @staticmethod
    def _assignment_resolution_state(
            conn: sqlite3.Connection, assignment: sqlite3.Row,
            draining_ts: float | None) -> dict:
        """Count assignment-window actions that cannot yet be scored."""
        if draining_ts is None:
            return {"total": None, "by_variant": {}}
        by_variant = {}
        for variant_id in (
                str(assignment["baseline_variant_id"]),
                str(assignment["candidate_variant_id"])):
            unresolved_trades = int(conn.execute(
                "SELECT COUNT(DISTINCT trade.trade_id) "
                "FROM paper_decisions AS decision "
                "JOIN paper_trades AS trade "
                "ON trade.trade_id=decision.paper_trade_id "
                "WHERE decision.scope_key=? AND decision.variant_id=? "
                "AND decision.decision_ts>=? AND decision.decision_ts<=? "
                "AND decision.decision_outcome='PROPOSED' "
                "AND (trade.status!='CLOSED' OR trade.exit_ts IS NULL "
                "OR trade.r_multiple IS NULL)",
                (assignment["scope_key"], variant_id,
                 float(assignment["started_ts"]), float(draining_ts))
            ).fetchone()[0])
            unresolved_actions = int(conn.execute(
                "SELECT COUNT(*) FROM paper_decisions AS decision "
                "LEFT JOIN paper_trades AS trade "
                "ON trade.trade_id=decision.paper_trade_id "
                "WHERE decision.scope_key=? AND decision.variant_id=? "
                "AND decision.decision_ts>=? AND decision.decision_ts<=? "
                "AND decision.decision_outcome='PROPOSED' "
                "AND (decision.paper_trade_id IS NULL "
                "OR trade.trade_id IS NULL)",
                (assignment["scope_key"], variant_id,
                 float(assignment["started_ts"]), float(draining_ts))
            ).fetchone()[0])
            by_variant[variant_id] = {
                "unresolved_trades": unresolved_trades,
                "unresolved_actions": unresolved_actions,
                "total": unresolved_trades + unresolved_actions,
            }
        return {
            "total": sum(item["total"] for item in by_variant.values()),
            "by_variant": by_variant,
        }

    @classmethod
    def _assignment_dict(
            cls, conn: sqlite3.Connection, row: sqlite3.Row,
            now: float | None = None) -> dict:
        item = dict(row)
        item["setting"] = json.loads(item.pop("setting_json"))
        item["code_identity"] = json.loads(item.pop("code_identity_json"))
        item["config_identity"] = json.loads(item.pop("config_identity_json"))
        latest = cls._assignment_event(conn, item["assignment_id"])
        status = str(latest["status"]) if latest is not None else "STARTED"
        draining = conn.execute(
            "SELECT * FROM strategy_experiment_assignment_events "
            "WHERE assignment_id=? AND status='DRAINING' LIMIT 1",
            (item["assignment_id"],)).fetchone()
        observed_count = int(conn.execute(
            "SELECT COUNT(*) FROM strategy_experiment_observations "
            "WHERE assignment_id=?", (item["assignment_id"],)).fetchone()[0])
        terminal = status in {"COMPLETED", "REJECTED"}
        effective_now = (float(latest["event_ts"]) if terminal and latest
                         else time.time() if now is None else float(now))
        elapsed = max(0.0, effective_now - float(item["started_ts"]))
        resolution = cls._assignment_resolution_state(
            conn, row, float(draining["event_ts"])
            if draining is not None else None)
        item.update({
            "status": status,
            "end_ts": (float(latest["event_ts"])
                       if terminal and latest is not None else None),
            "observed_count": observed_count,
            "elapsed_seconds": elapsed,
            "duration_satisfied": (
                elapsed >= float(item["minimum_duration_seconds"])),
            "observations_satisfied": (
                observed_count >= int(item["minimum_observations"])),
            "completion_reason": (
                latest["reason"] if status == "COMPLETED" else None),
            "rejection_reason": (
                latest["reason"] if status == "REJECTED" else None),
            "draining_started_ts": (
                float(draining["event_ts"]) if draining is not None else None),
            "unresolved_actions": resolution,
        })
        item["ready_to_drain"] = (
            not terminal and status != "DRAINING"
            and item["duration_satisfied"]
            and item["observations_satisfied"])
        item["ready_to_complete"] = (
            status == "DRAINING" and resolution["total"] == 0)
        return item

    def experiment_assignment(
            self, assignment_id: str, *, now: float | None = None) -> dict | None:
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT * FROM strategy_experiment_assignments "
                "WHERE assignment_id=?", (assignment_id,)).fetchone()
            return (self._assignment_dict(conn, row, now)
                    if row is not None else None)

    def experiment_assignments(
            self, scope_key: str | None = None,
            strategy_id: str | None = None, *,
            now: float | None = None) -> list[dict]:
        clauses = []
        params: list = []
        if scope_key is not None:
            clauses.append("scope_key=?")
            params.append(str(scope_key))
        if strategy_id is not None:
            clauses.append("strategy_id=?")
            params.append(str(strategy_id))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT * FROM strategy_experiment_assignments" + where +
                " ORDER BY started_ts, assignment_id", params).fetchall()
            return [self._assignment_dict(conn, row, now) for row in rows]

    def experiment_assignment_history(self, assignment_id: str) -> list[dict]:
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT * FROM strategy_experiment_assignment_events "
                "WHERE assignment_id=? ORDER BY event_ts, rowid",
                (assignment_id,)).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event["detail"] = json.loads(event.pop("detail_json"))
            events.append(event)
        return events

    def experiment_rotation_report(
            self, scope_key: str | None = None,
            strategy_id: str | None = None, *,
            now: float | None = None) -> dict:
        """Return an operator/simulation-ready assignment and gate summary."""
        assignments = self.experiment_assignments(
            scope_key, strategy_id, now=now)
        return {
            "schema": "strategy_experiment_rotation.v1",
            "scope_key": scope_key,
            "strategy_id": strategy_id,
            "active": [item for item in assignments
                       if item["status"] not in {"COMPLETED", "REJECTED"}],
            "terminal": [item for item in assignments
                         if item["status"] in {"COMPLETED", "REJECTED"}],
        }

    def experiment_observations(self, assignment_id: str) -> list[dict]:
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT * FROM strategy_experiment_observations "
                "WHERE assignment_id=? ORDER BY observed_ts, observation_id",
                (assignment_id,)).fetchall()
        observations = []
        for row in rows:
            observation = dict(row)
            observation["detail"] = json.loads(
                observation.pop("detail_json"))
            observations.append(observation)
        return observations

    def active_experiment_assignment(
            self, scope_key: str, strategy_id: str, *,
            now: float | None = None) -> dict | None:
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT assignment.* "
                "FROM strategy_experiment_active_slots AS slot "
                "JOIN strategy_experiment_assignments AS assignment "
                "ON assignment.assignment_id=slot.assignment_id "
                "WHERE slot.scope_key=? AND slot.strategy_id=?",
                (scope_key, strategy_id)).fetchone()
            if row is None:
                return None
            assignment = self._assignment_dict(conn, row, now)
            if assignment["status"] in {"COMPLETED", "REJECTED"}:
                raise MigrationError(
                    f"{scope_key}:{strategy_id} active slot points to a "
                    "terminal experiment")
            return assignment

    def next_experiment_candidate(
            self, scope_key: str, strategy_id: str,
            candidates: list[dict], *,
            retry_of_assignment_id: str | None = None) -> dict | None:
        """Choose the next exact setting, or one explicit inconclusive retry."""
        with _connect(self.path) as conn:
            terminal_rows = self._terminal_assignment_rows(
                conn, scope_key, strategy_id)
            terminal_variants = {
                str(row["candidate_variant_id"]) for row in terminal_rows}
            terminal_keys = {str(row["candidate_key"]) for row in terminal_rows}
            terminal_by_assignment = {
                str(row["assignment_id"]): row for row in terminal_rows}
            ordered = sorted(candidates, key=lambda item: (
                int(item.get("priority", 100)),
                str(item.get("order_key") or ""),
                str(item.get("variant_id") or ""),
            ))
            for candidate in ordered:
                variant_id = str(candidate.get("variant_id") or "")
                candidate_key = str(candidate.get("candidate_key") or "")
                if not variant_id or not candidate_key:
                    continue
                retry_of = str(
                    candidate.get("retry_of_assignment_id")
                    or retry_of_assignment_id or "").strip()
                if retry_of_assignment_id:
                    requested_retry = terminal_by_assignment.get(
                        str(retry_of_assignment_id))
                    if (requested_retry is None
                            or str(requested_retry["candidate_variant_id"])
                            != variant_id
                            or str(requested_retry["candidate_key"])
                            != candidate_key):
                        continue
                if variant_id in terminal_variants or candidate_key in terminal_keys:
                    previous = terminal_by_assignment.get(retry_of)
                    same_candidate = bool(
                        previous is not None
                        and str(previous["candidate_variant_id"]) == variant_id
                        and str(previous["candidate_key"]) == candidate_key)
                    attempts = [
                        int(row["attempt"] or 1) for row in terminal_rows
                        if (str(row["candidate_variant_id"]) == variant_id
                            or str(row["candidate_key"]) == candidate_key)]
                    latest_attempt = max(attempts, default=0)
                    if not (
                            same_candidate
                            and self._terminal_assignment_retryable(previous)
                            and int(previous["attempt"] or 1) == latest_attempt):
                        continue
                    candidate = {
                        **candidate,
                        "retry_of_assignment_id": retry_of,
                        "attempt": latest_attempt + 1,
                    }
                proposal_id = candidate.get("proposal_id")
                if proposal_id:
                    proposal = conn.execute(
                        "SELECT * FROM hypothesis_proposals WHERE proposal_id=?",
                        (proposal_id,)).fetchone()
                    if proposal is None:
                        continue
                    current = self._proposal_dict(conn, proposal)["current_status"]
                    if (current in {"OBSERVED", "REJECTED"}
                            and not candidate.get("retry_of_assignment_id")):
                        continue
                return dict(candidate)
        return None

    def ensure_experiment_assignment(
            self, scope_key: str, strategy_id: str,
            baseline_variant_id: str, candidates: list[dict], *,
            minimum_duration_seconds: float,
            minimum_observations: int,
            retry_of_assignment_id: str | None = None,
            initial_balance_usdt: float = 10_000.0,
            now: float | None = None) -> dict | None:
        """Resume the active assignment or append the deterministic next one."""
        timestamp = time.time() if now is None else float(now)
        if float(minimum_duration_seconds) < 0:
            raise ValueError("minimum_duration_seconds cannot be negative")
        if int(minimum_observations) <= 0:
            raise ValueError("minimum_observations must be positive")
        active = self.active_experiment_assignment(
            scope_key, strategy_id, now=timestamp)
        if active is not None:
            return active
        candidate = self.next_experiment_candidate(
            scope_key, strategy_id, candidates,
            retry_of_assignment_id=retry_of_assignment_id)
        if candidate is None:
            return None
        variant_id = str(candidate["variant_id"])
        candidate_key = str(candidate["candidate_key"])
        axis = str(candidate.get("axis") or "").strip()
        setting_id = str(candidate.get("setting_id") or "").strip()
        source = str(candidate.get("source") or "static")
        if not axis or not setting_id:
            raise ValueError("experiment axis and setting_id are required")
        if source not in {"static", "adaptive"}:
            raise ValueError("experiment source must be static or adaptive")
        setting_json = _canonical_json(candidate.get("setting") or {})
        code_identity_json = _canonical_json(candidate.get("code_identity") or {})
        config_identity_json = _canonical_json(
            candidate.get("config_identity") or {})
        proposal_id = candidate.get("proposal_id")
        selection_id = candidate.get("selection_id")
        retry_of = str(
            candidate.get("retry_of_assignment_id") or "").strip() or None
        attempt = int(candidate.get("attempt") or 1)
        with _connect(self.path) as conn:
            schema15 = _stored_version(conn) >= 15
            if retry_of and not schema15:
                raise ValueError("experiment retries require findings schema 15")
            baseline = self._require_variant(conn, baseline_variant_id)
            experiment = self._require_variant(conn, variant_id)
            if (baseline["strategy_id"] != strategy_id
                    or experiment["strategy_id"] != strategy_id):
                raise ValueError("experiment variants must match strategy_id")
            if baseline_variant_id == variant_id:
                raise ValueError("experiment candidate cannot be its baseline")
            if proposal_id:
                proposal = conn.execute(
                    "SELECT * FROM hypothesis_proposals WHERE proposal_id=?",
                    (proposal_id,)).fetchone()
                if (proposal is None or proposal["variant_id"] != variant_id
                        or proposal["strategy_id"] != strategy_id):
                    raise ValueError(
                        "adaptive assignment does not match its exact proposal")
                if (self._proposal_dict(conn, proposal)["current_status"] in {
                        "OBSERVED", "REJECTED"} and not retry_of):
                    raise ValueError("adaptive proposal is already terminal")
            if retry_of:
                previous = conn.execute(
                    "SELECT assignment.*, outcome.verdict, "
                    "outcome.payload_json AS outcome_payload_json "
                    "FROM strategy_experiment_assignments AS assignment "
                    "JOIN experiment_outcomes AS outcome "
                    "ON outcome.assignment_id=assignment.assignment_id "
                    "WHERE assignment.assignment_id=?",
                    (retry_of,)).fetchone()
                if (previous is None
                        or previous["scope_key"] != scope_key
                        or previous["strategy_id"] != strategy_id
                        or previous["baseline_variant_id"]
                        != baseline_variant_id
                        or previous["candidate_variant_id"] != variant_id
                        or previous["candidate_key"] != candidate_key
                        or not self._terminal_assignment_retryable(previous)):
                    raise ValueError(
                        "retry must name a matching terminal INCONCLUSIVE "
                        "assignment")
                attempt = int(previous["attempt"] or 1) + 1
            assignment_id = _content_hash({
                "scope_key": str(scope_key),
                "strategy_id": str(strategy_id),
                "baseline_variant_id": str(baseline_variant_id),
                "candidate_variant_id": variant_id,
                "candidate_key": candidate_key,
                "code_identity": json.loads(code_identity_json),
                "config_identity": json.loads(config_identity_json),
                "attempt": attempt,
                "retry_of_assignment_id": retry_of,
            })[:32]
            if selection_id:
                selection = conn.execute(
                    "SELECT * FROM research_selections WHERE selection_id=?",
                    (selection_id,)).fetchone()
                if (selection is None
                        or selection["scope_key"] != scope_key
                        or selection["requested_strategy_id"] != strategy_id
                        or selection["resolved_variant_id"] != variant_id):
                    raise ValueError(
                        "research selection does not match this assignment")
                latest_selection = self._research_selection_event(
                    conn, str(selection_id))
                if (latest_selection is None
                        or latest_selection["status"] != "ACCEPTED"):
                    raise ValueError("research selection is not pending")
            columns = (
                "assignment_id, scope_key, strategy_id, baseline_variant_id, "
                "candidate_variant_id, candidate_key, axis, setting_id, "
                "setting_json, source, proposal_id, started_ts, "
                "minimum_duration_seconds, minimum_observations, "
                "code_identity_json, config_identity_json")
            values = [
                assignment_id, scope_key, strategy_id, baseline_variant_id,
                variant_id, candidate_key, axis, setting_id, setting_json,
                source, proposal_id, timestamp,
                float(minimum_duration_seconds), int(minimum_observations),
                code_identity_json, config_identity_json,
            ]
            if schema15:
                columns += ", attempt, retry_of_assignment_id"
                values.extend((attempt, retry_of))
            conn.execute(
                "INSERT INTO strategy_experiment_assignments (" + columns
                + ") VALUES (" + ",".join("?" for _ in values) + ")",
                values)
            self._reset_assignment_accounts_conn(
                conn, assignment_id, scope_key, timestamp, attempt,
                baseline_variant_id, variant_id,
                json.loads(code_identity_json),
                json.loads(config_identity_json), initial_balance_usdt)
            for status in ("STARTED", "OBSERVING"):
                conn.execute(
                    "INSERT INTO strategy_experiment_assignment_events "
                    "(event_id, assignment_id, status, event_ts, reason, "
                    "detail_json) VALUES (?,?,?,?,?,?)",
                    (uuid.uuid4().hex, assignment_id, status, timestamp, None,
                     _canonical_json({
                         "baseline_variant_id": baseline_variant_id,
                         "candidate_variant_id": variant_id,
                         "axis": axis, "setting_id": setting_id,
                         "attempt": attempt,
                         "retry_of_assignment_id": retry_of,
                         "fresh_account_boundary_ts": timestamp,
                     })))
            conn.execute(
                "INSERT INTO strategy_experiment_active_slots "
                "(scope_key, strategy_id, assignment_id) VALUES (?,?,?)",
                (scope_key, strategy_id, assignment_id))
            if proposal_id and not retry_of:
                self._append_proposal_event_conn(
                    conn, str(proposal_id), "OBSERVING", {
                        "assignment_id": assignment_id,
                        "scope_key": scope_key,
                        "variant_id": variant_id,
                    }, timestamp)
            if selection_id:
                self._append_research_selection_event_conn(
                    conn, str(selection_id), "ASSIGNED",
                    assignment_id=assignment_id, timestamp=timestamp,
                    detail={
                        "scope_key": scope_key,
                        "strategy_id": strategy_id,
                        "variant_id": variant_id,
                    })
            row = conn.execute(
                "SELECT * FROM strategy_experiment_assignments "
                "WHERE assignment_id=?", (assignment_id,)).fetchone()
            return self._assignment_dict(conn, row, timestamp)

    def record_experiment_observations(
            self, assignment_id: str, observations: list[dict], *,
            now: float | None = None) -> dict:
        """Append idempotent baseline/candidate-comparable observations."""
        timestamp = time.time() if now is None else float(now)
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT * FROM strategy_experiment_assignments "
                "WHERE assignment_id=?", (assignment_id,)).fetchone()
            if row is None:
                raise ValueError(f"unknown assignment_id {assignment_id!r}")
            latest = self._assignment_event(conn, assignment_id)
            if latest and latest["status"] in {"COMPLETED", "REJECTED"}:
                raise ValueError("experiment assignment is already terminal")
            if latest and latest["status"] == "DRAINING":
                raise ValueError("experiment assignment is draining")
            for observation in observations:
                key = str(observation.get("observation_key") or "").strip()
                if not key:
                    raise ValueError("experiment observation_key is required")
                observation_id = _content_hash({
                    "assignment_id": assignment_id,
                    "observation_key": key,
                })[:32]
                conn.execute(
                    "INSERT OR IGNORE INTO strategy_experiment_observations "
                    "(observation_id, assignment_id, observation_key, "
                    "observed_ts, detail_json) VALUES (?,?,?,?,?)",
                    (observation_id, assignment_id, key,
                     float(observation.get("observed_ts", timestamp)),
                     _canonical_json(observation.get("detail") or {})))
            return self._assignment_dict(conn, row, timestamp)

    def maybe_complete_experiment_assignment(
            self, assignment_id: str, *, now: float | None = None,
            reason: str = (
                "collection gates satisfied and assignment-window actions resolved"
            )) -> dict:
        timestamp = time.time() if now is None else float(now)
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT * FROM strategy_experiment_assignments "
                "WHERE assignment_id=?", (assignment_id,)).fetchone()
            if row is None:
                raise ValueError(f"unknown assignment_id {assignment_id!r}")
            assignment = self._assignment_dict(conn, row, timestamp)
            if assignment["status"] in {"COMPLETED", "REJECTED"}:
                if _table_exists(conn, "experiment_outcomes"):
                    _ensure_experiment_outcome_conn(conn, assignment_id)
                return assignment
            if assignment["status"] != "DRAINING":
                if not assignment["ready_to_drain"]:
                    return assignment
                conn.execute(
                    "INSERT INTO strategy_experiment_assignment_events "
                    "(event_id, assignment_id, status, event_ts, reason, "
                    "detail_json) VALUES (?,?,?,?,?,?)",
                    (uuid.uuid4().hex, assignment_id, "DRAINING", timestamp,
                     "minimum duration and comparable observation gates "
                     "satisfied; new opens stopped",
                     _canonical_json({
                         "observed_count": assignment["observed_count"],
                         "elapsed_seconds": assignment["elapsed_seconds"],
                     })))
                assignment = self._assignment_dict(conn, row, timestamp)
            if not assignment["ready_to_complete"]:
                return assignment
            conn.execute(
                "INSERT INTO strategy_experiment_assignment_events "
                "(event_id, assignment_id, status, event_ts, reason, "
                "detail_json) VALUES (?,?,?,?,?,?)",
                (uuid.uuid4().hex, assignment_id, "COMPLETED", timestamp,
                 str(reason), _canonical_json({
                     "observed_count": assignment["observed_count"],
                     "elapsed_seconds": assignment["elapsed_seconds"],
                     "draining_started_ts": assignment["draining_started_ts"],
                     "unresolved_actions": assignment["unresolved_actions"],
                 })))
            conn.execute(
                "DELETE FROM strategy_experiment_active_slots "
                "WHERE assignment_id=?", (assignment_id,))
            retry_of = (row["retry_of_assignment_id"]
                        if "retry_of_assignment_id" in row.keys() else None)
            if row["proposal_id"] and not retry_of:
                self._append_proposal_event_conn(
                    conn, str(row["proposal_id"]), "OBSERVED", {
                        "assignment_id": assignment_id,
                        "observed_count": assignment["observed_count"],
                        "elapsed_seconds": assignment["elapsed_seconds"],
                    }, timestamp)
            selected = conn.execute(
                "SELECT selection_id FROM research_selection_events "
                "WHERE assignment_id=? AND status='ASSIGNED'",
                (assignment_id,)).fetchall()
            for selection in selected:
                self._append_research_selection_event_conn(
                    conn, str(selection["selection_id"]), "TESTED",
                    assignment_id=assignment_id, timestamp=timestamp,
                    detail={
                        "observed_count": assignment["observed_count"],
                        "elapsed_seconds": assignment["elapsed_seconds"],
                    })
            if _table_exists(conn, "experiment_outcomes"):
                _ensure_experiment_outcome_conn(conn, assignment_id)
            return self._assignment_dict(conn, row, timestamp)

    def reject_experiment_assignment(
            self, assignment_id: str, reason: str, *,
            detail: dict | None = None, now: float | None = None) -> dict:
        timestamp = time.time() if now is None else float(now)
        reason = str(reason or "").strip()
        if not reason:
            raise ValueError("experiment rejection reason is required")
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT * FROM strategy_experiment_assignments "
                "WHERE assignment_id=?", (assignment_id,)).fetchone()
            if row is None:
                raise ValueError(f"unknown assignment_id {assignment_id!r}")
            assignment = self._assignment_dict(conn, row, timestamp)
            if assignment["status"] in {"COMPLETED", "REJECTED"}:
                if _table_exists(conn, "experiment_outcomes"):
                    _ensure_experiment_outcome_conn(conn, assignment_id)
                return assignment
            conn.execute(
                "INSERT INTO strategy_experiment_assignment_events "
                "(event_id, assignment_id, status, event_ts, reason, "
                "detail_json) VALUES (?,?,?,?,?,?)",
                (uuid.uuid4().hex, assignment_id, "REJECTED", timestamp,
                 reason, _canonical_json(detail or {})))
            conn.execute(
                "DELETE FROM strategy_experiment_active_slots "
                "WHERE assignment_id=?", (assignment_id,))
            retry_of = (row["retry_of_assignment_id"]
                        if "retry_of_assignment_id" in row.keys() else None)
            if row["proposal_id"] and not retry_of:
                self._append_proposal_event_conn(
                    conn, str(row["proposal_id"]), "REJECTED", {
                        "assignment_id": assignment_id,
                        "reason": reason,
                        **(detail or {}),
                    }, timestamp)
            selected = conn.execute(
                "SELECT selection_id FROM research_selection_events "
                "WHERE assignment_id=? AND status='ASSIGNED'",
                (assignment_id,)).fetchall()
            for selection in selected:
                self._append_research_selection_event_conn(
                    conn, str(selection["selection_id"]), "REJECTED",
                    reason=reason, assignment_id=assignment_id,
                    timestamp=timestamp, detail=detail or {})
            if _table_exists(conn, "experiment_outcomes"):
                _ensure_experiment_outcome_conn(conn, assignment_id)
            return self._assignment_dict(conn, row, timestamp)

    # ------------------------------------------------ experiment learning

    @staticmethod
    def _outcome_dict(row: sqlite3.Row) -> dict:
        item = dict(row)
        payload_json = item.pop("payload_json")
        payload = json.loads(payload_json)
        if (_canonical_json(payload) != payload_json
                or _content_hash(payload) != item["payload_hash"]):
            raise ValueError(
                f"experiment outcome {item['outcome_id']} failed its hash")
        item["payload"] = payload
        return item

    def ensure_terminal_experiment_outcomes(self) -> dict:
        """Idempotently backfill any terminal assignment missing its outcome."""
        created = []
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT assignment.assignment_id "
                "FROM strategy_experiment_assignments AS assignment "
                "WHERE (SELECT event.status "
                "FROM strategy_experiment_assignment_events AS event "
                "WHERE event.assignment_id=assignment.assignment_id "
                "ORDER BY event.event_ts DESC, event.rowid DESC LIMIT 1) "
                "IN ('COMPLETED','REJECTED') "
                "ORDER BY assignment.started_ts, assignment.assignment_id"
            ).fetchall()
            for row in rows:
                outcome, was_created = _ensure_experiment_outcome_conn(
                    conn, str(row["assignment_id"]))
                if was_created:
                    created.append(outcome["outcome_id"])
            total = int(conn.execute(
                "SELECT COUNT(*) FROM experiment_outcomes").fetchone()[0])
            pending = int(conn.execute(
                "SELECT COUNT(*) FROM experiment_outcomes AS outcome "
                "LEFT JOIN experiment_reviews AS review "
                "ON review.outcome_id=outcome.outcome_id "
                "WHERE review.outcome_id IS NULL").fetchone()[0])
        return {"created": created, "total": total, "pending_reviews": pending}

    def experiment_outcome(
            self, assignment_id: str | None = None, *,
            outcome_id: str | None = None) -> dict | None:
        if bool(assignment_id) == bool(outcome_id):
            raise ValueError("provide exactly one of assignment_id or outcome_id")
        column, value = (("assignment_id", assignment_id)
                         if assignment_id else ("outcome_id", outcome_id))
        with _connect(self.path) as conn:
            row = conn.execute(
                f"SELECT * FROM experiment_outcomes WHERE {column}=?",
                (value,)).fetchone()
        return self._outcome_dict(row) if row is not None else None

    def experiment_outcomes(
            self, scope_key: str | None = None,
            strategy_id: str | None = None) -> list[dict]:
        clauses = []
        params = []
        if scope_key is not None:
            clauses.append("scope_key=?")
            params.append(str(scope_key))
        if strategy_id is not None:
            clauses.append("strategy_id=?")
            params.append(str(strategy_id))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT * FROM experiment_outcomes" + where
                + " ORDER BY created_ts, outcome_id", params).fetchall()
        return [self._outcome_dict(row) for row in rows]

    def pending_experiment_outcome(self) -> dict | None:
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT outcome.* FROM experiment_outcomes AS outcome "
                "LEFT JOIN experiment_reviews AS review "
                "ON review.outcome_id=outcome.outcome_id "
                "WHERE review.outcome_id IS NULL "
                "ORDER BY outcome.created_ts, outcome.outcome_id LIMIT 1"
            ).fetchone()
        return self._outcome_dict(row) if row is not None else None

    def edge_evidence(self, scope_key: str | None = None) -> list[dict]:
        candidates = self.edge_candidates_for(scope_key=scope_key)
        with _connect(self.path) as conn:
            out = []
            for item in candidates:
                qualification = self._latest_qualification(
                    conn, str(item["variant_id"]), str(item["scope_key"]))
                item["protocol_satisfied"] = bool(
                    qualification is not None
                    and qualification["status"] == "QUALIFIED")
                item["qualification_status"] = (
                    str(qualification["status"])
                    if qualification is not None else None)
                item["current_status"] = (
                    "REVOKED" if item["qualification_status"] == "REVOKED"
                    else "EDGE_CANDIDATE")
                out.append(item)
        return out

    @staticmethod
    def _edge_candidate_dict(row: sqlite3.Row) -> dict:
        item = dict(row)
        raw = item.pop("evidence_json")
        evidence = json.loads(raw)
        if (_canonical_json(evidence) != raw
                or _content_hash(evidence) != item["evidence_hash"]):
            raise ValueError(
                f"research edge evidence {item['edge_id']} failed its hash")
        item["evidence"] = evidence
        return item

    def edge_candidates_for(
            self, variant_id: str | None = None,
            scope_key: str | None = None) -> list[dict]:
        clauses = []
        params = []
        if variant_id is not None:
            clauses.append("variant_id=?")
            params.append(str(variant_id))
        if scope_key is not None:
            clauses.append("scope_key=?")
            params.append(str(scope_key))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT * FROM research_edge_evidence" + where
                + " ORDER BY created_ts, edge_id", tuple(params)).fetchall()
        return [self._edge_candidate_dict(row) for row in rows]

    @staticmethod
    def _review_dict(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["limitations"] = json.loads(item.pop("limitations_json"))
        raw_selection = item.pop("next_selection_json")
        item["next_selection"] = (
            json.loads(raw_selection) if raw_selection else None)
        return item

    def experiment_review(self, outcome_id: str) -> dict | None:
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT * FROM experiment_reviews WHERE outcome_id=?",
                (outcome_id,)).fetchone()
        return self._review_dict(row) if row is not None else None

    def experiment_review_attempts(self, outcome_id: str) -> list[dict]:
        with _connect(self.path) as conn:
            rows = conn.execute(
                "SELECT * FROM experiment_review_attempts WHERE outcome_id=? "
                "ORDER BY requested_ts, attempt_id", (outcome_id,)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["request"] = json.loads(item.pop("request_json"))
            raw_response = item.pop("response_json")
            item["response"] = (
                json.loads(raw_response) if raw_response else None)
            out.append(item)
        return out

    def record_experiment_review_failure(
            self, outcome_id: str, *, provider: str, model_id: str,
            prompt_version: str, request: dict, parse_error: str,
            raw_response: str | None = None,
            requested_ts: float | None = None,
            completed_ts: float | None = None) -> dict:
        requested = time.time() if requested_ts is None else float(requested_ts)
        completed = time.time() if completed_ts is None else float(completed_ts)
        error = str(parse_error or "").strip()
        if not error:
            raise ValueError("failed review attempt requires parse_error")
        with _connect(self.path) as conn:
            if conn.execute(
                    "SELECT 1 FROM experiment_outcomes WHERE outcome_id=?",
                    (outcome_id,)).fetchone() is None:
                raise ValueError(f"unknown outcome_id {outcome_id!r}")
            if conn.execute(
                    "SELECT 1 FROM experiment_reviews WHERE outcome_id=?",
                    (outcome_id,)).fetchone() is not None:
                return {"status": "ALREADY_REVIEWED", "outcome_id": outcome_id}
            attempt_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO experiment_review_attempts (attempt_id, "
                "outcome_id, requested_ts, completed_ts, provider, model_id, "
                "prompt_version, request_json, raw_response, response_json, "
                "parse_error, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,'FAILED')",
                (attempt_id, outcome_id, requested, completed, provider,
                 model_id, prompt_version, _canonical_json(request),
                 (str(raw_response)[:16_000]
                  if raw_response is not None else None), None,
                 error[:4_000]))
        return {"status": "FAILED", "outcome_id": outcome_id,
                "attempt_id": attempt_id, "parse_error": error[:4_000]}

    def record_experiment_review_success(
            self, outcome_id: str, *, provider: str, model_id: str,
            prompt_version: str, request: dict, raw_response: str,
            response: dict, explanation: str, limitations: list,
            next_selection: dict | None = None,
            selection_id: str | None = None,
            requested_ts: float | None = None,
            completed_ts: float | None = None) -> dict:
        requested = time.time() if requested_ts is None else float(requested_ts)
        completed = time.time() if completed_ts is None else float(completed_ts)
        explanation = str(explanation or "").strip()
        if not explanation:
            raise ValueError("research review explanation is required")
        with _connect(self.path) as conn:
            existing = conn.execute(
                "SELECT * FROM experiment_reviews WHERE outcome_id=?",
                (outcome_id,)).fetchone()
            if existing is not None:
                return self._review_dict(existing)
            if conn.execute(
                    "SELECT 1 FROM experiment_outcomes WHERE outcome_id=?",
                    (outcome_id,)).fetchone() is None:
                raise ValueError(f"unknown outcome_id {outcome_id!r}")
            if selection_id and conn.execute(
                    "SELECT 1 FROM research_selections WHERE selection_id=?",
                    (selection_id,)).fetchone() is None:
                raise ValueError("review selection_id is not persisted")
            attempt_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO experiment_review_attempts (attempt_id, "
                "outcome_id, requested_ts, completed_ts, provider, model_id, "
                "prompt_version, request_json, raw_response, response_json, "
                "parse_error, status) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,'SUCCEEDED')",
                (attempt_id, outcome_id, requested, completed, provider,
                 model_id, prompt_version, _canonical_json(request),
                 str(raw_response)[:16_000], _canonical_json(response)))
            review_id = _content_hash({
                "outcome_id": outcome_id, "attempt_id": attempt_id,
                "response": response,
            })[:32]
            conn.execute(
                "INSERT INTO experiment_reviews (review_id, outcome_id, "
                "attempt_id, created_ts, explanation, limitations_json, "
                "next_selection_json, selection_id) VALUES (?,?,?,?,?,?,?,?)",
                (review_id, outcome_id, attempt_id, completed,
                 explanation[:4_000], _canonical_json(limitations[:8]),
                 (_canonical_json(next_selection)
                  if next_selection is not None else None), selection_id))
            row = conn.execute(
                "SELECT * FROM experiment_reviews WHERE review_id=?",
                (review_id,)).fetchone()
        return self._review_dict(row)

    def research_selection_by_attribution(
            self, scope_key: str, run_id: str,
            cycle_id: str | None) -> dict | None:
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT * FROM research_selections WHERE scope_key=? "
                "AND run_id=? AND COALESCE(cycle_id,'')=COALESCE(?,'') "
                "ORDER BY requested_ts, selection_id LIMIT 1",
                (scope_key, run_id, cycle_id)).fetchone()
            return (self._research_selection_dict(conn, row)
                    if row is not None else None)

    def research_history_context(
            self, scope_key: str | None = None, *, limit: int = 20) -> dict:
        """Concise research-only planner context; never used by live prompts."""
        outcomes = self.experiment_outcomes(scope_key)[-max(1, int(limit)):]
        assignments = self.experiment_assignments(scope_key)
        selections = self.research_selections(scope_key)
        edges = self.edge_evidence(scope_key)[-max(1, int(limit)):]
        return {
            "schema": "research_history_context.v1",
            "completed_outcomes": [{
                "outcome_id": item["outcome_id"],
                "assignment_id": item["assignment_id"],
                "strategy_id": item["strategy_id"],
                "candidate_variant_id": item["candidate_variant_id"],
                "verdict": item["verdict"],
                "reasons": item["payload"]["reasons"],
                "data_window": item["payload"]["data_window"],
            } for item in outcomes],
            "failed_exact_settings": [{
                "strategy_id": item["strategy_id"],
                "variant_id": item["candidate_variant_id"],
                "assignment_id": item["assignment_id"],
                "reasons": item["payload"]["reasons"],
            } for item in outcomes if item["verdict"] == "FAILED"],
            "edge_candidates": [{
                "edge_id": item["edge_id"],
                "strategy_id": item["strategy_id"],
                "variant_id": item["variant_id"],
                "authority": item["evidence"]["authority"],
                "data_window": item["evidence"]["data_window"],
                "protocol_satisfied": item["protocol_satisfied"],
                "qualification_status": item["qualification_status"],
                "current_status": item["current_status"],
            } for item in edges],
            "active_assignments": [{
                "assignment_id": item["assignment_id"],
                "strategy_id": item["strategy_id"],
                "candidate_variant_id": item["candidate_variant_id"],
                "axis": item["axis"], "setting_id": item["setting_id"],
                "observed_count": item["observed_count"],
            } for item in assignments
                if item["status"] not in {"COMPLETED", "REJECTED"}],
            "pending_selections": [{
                "selection_id": item["selection_id"],
                "strategy_id": item["requested_strategy_id"],
                "resolved_variant_id": item["resolved_variant_id"],
                "status": item["current_status"],
            } for item in selections
                if item["current_status"] in {"ACCEPTED", "ASSIGNED"}],
            "retryable_inconclusive_assignments": [{
                "assignment_id": item["assignment_id"],
                "strategy_id": item["strategy_id"],
                "candidate_variant_id": item["candidate_variant_id"],
                "reasons": item["payload"]["reasons"],
            } for item in outcomes
                if _retryable_inconclusive_payload(item["payload"])],
            "terminal_variant_ids": sorted({
                item["candidate_variant_id"] for item in outcomes
                if (item["verdict"] in {"WORKED", "FAILED"}
                    or (item["verdict"] == "INCONCLUSIVE"
                        and not _retryable_inconclusive_payload(
                            item["payload"])))}),
        }

    def propose_numeric_setting(
            self, strategy_id: str, hypothesis_id: str, setting_id: str,
            value, run_id: str, *, minimum: float, maximum: float,
            target_parameter: str, variant, reasoning: str = "",
            observation_lock_seconds: float = 3600.0,
            now: float | None = None) -> dict:
        """Atomically register, persist, explain, and lock an exact proposal."""
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("adaptive proposal value must be numeric") from exc
        if not math.isfinite(number) or number < float(minimum) or number > float(maximum):
            raise ValueError("adaptive proposal value is outside its registered bounds")
        if float(observation_lock_seconds) <= 0:
            raise ValueError("observation_lock_seconds must be positive")
        target_parameter = str(target_parameter or "").strip()
        if not target_parameter:
            raise ValueError("adaptive proposal target_parameter is required")
        if (str(variant.strategy_id) != str(strategy_id)
                or str(getattr(variant, "hypothesis_id", ""))
                != str(hypothesis_id)):
            raise ValueError("adaptive variant does not match proposal identity")
        try:
            variant_value = float(variant.hypothesis_params[target_parameter])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "adaptive variant does not carry the exact target parameter") from exc
        if variant_value != number:
            raise ValueError("adaptive variant target value does not match proposal")
        reasoning = str(reasoning or "").strip()[:300]
        if len(reasoning) < 10:
            raise ValueError("adaptive proposal reasoning is required")
        timestamp = time.time() if now is None else float(now)
        value_json = _canonical_json(number)
        minimum_json = _canonical_json(float(minimum))
        maximum_json = _canonical_json(float(maximum))
        value_key = _content_hash({
            "strategy_id": str(strategy_id),
            "hypothesis_id": str(hypothesis_id),
            "target_parameter": target_parameter,
            "value": number,
        })
        proposal_id = _content_hash({
            "strategy_id": str(strategy_id), "hypothesis_id": str(hypothesis_id),
            "setting_id": str(setting_id),
            "target_parameter": target_parameter,
            "value": number, "run_id": str(run_id),
        })[:32]
        row = {
            "proposal_id": proposal_id, "strategy_id": str(strategy_id),
            "hypothesis_id": str(hypothesis_id), "setting_id": str(setting_id),
            "variant_id": str(variant.variant_id),
            "target_parameter": target_parameter,
            "value": number, "run_id": str(run_id), "proposed_ts": timestamp,
            "observation_until_ts": timestamp + float(observation_lock_seconds),
            "minimum": float(minimum), "maximum": float(maximum),
            "status": "PROPOSED", "current_status": "PROPOSED",
            "reasoning": reasoning,
        }
        with _connect(self.path) as conn:
            if conn.execute(
                    "SELECT 1 FROM hypothesis_proposals WHERE strategy_id=? "
                    "AND hypothesis_id=? AND setting_id=? AND value_json=?",
                    (strategy_id, hypothesis_id, setting_id,
                     value_json)).fetchone():
                raise ValueError("duplicate exact adaptive hypothesis value")
            if conn.execute(
                    "SELECT 1 FROM hypothesis_proposal_value_locks "
                    "WHERE value_key=?", (value_key,)).fetchone():
                raise ValueError("duplicate exact adaptive hypothesis value")
            locked = conn.execute(
                "SELECT 1 FROM hypothesis_proposals AS proposal "
                "WHERE proposal.strategy_id=? AND proposal.proposed_ts<=? "
                "AND proposal.observation_until_ts>? AND COALESCE(("
                "SELECT event.status FROM hypothesis_proposal_events AS event "
                "WHERE event.proposal_id=proposal.proposal_id "
                "ORDER BY event.event_ts DESC, event.rowid DESC LIMIT 1"
                "), proposal.status) NOT IN ('OBSERVED','REJECTED')",
                (strategy_id, timestamp, timestamp)).fetchone()
            if locked:
                raise ValueError("hypothesis observation lock is active")
            duplicate = conn.execute(
                "SELECT 1 FROM hypothesis_proposals WHERE strategy_id=? "
                "AND hypothesis_id=? AND setting_id=? AND run_id=?",
                (strategy_id, hypothesis_id, setting_id, run_id)).fetchone()
            if duplicate:
                raise ValueError("duplicate adaptive hypothesis proposal")
            self._register_variant(conn, variant, timestamp)
            conn.execute(
                "INSERT INTO hypothesis_proposals "
                "(proposal_id, strategy_id, hypothesis_id, setting_id, "
                "value_json, run_id, proposed_ts, observation_until_ts, status, "
                "variant_id, target_parameter, minimum_json, maximum_json, "
                "reasoning) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (proposal_id, strategy_id, hypothesis_id, setting_id,
                 value_json, run_id, timestamp, row["observation_until_ts"],
                 "PROPOSED", variant.variant_id, target_parameter,
                 minimum_json, maximum_json, reasoning))
            try:
                conn.execute(
                    "INSERT INTO hypothesis_proposal_value_locks "
                    "(value_key, proposal_id, strategy_id, hypothesis_id, "
                    "target_parameter, value_json, created_ts) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (value_key, proposal_id, strategy_id, hypothesis_id,
                     target_parameter, value_json, timestamp))
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    "duplicate exact adaptive hypothesis value") from exc
            conn.execute(
                "INSERT INTO hypothesis_proposal_events "
                "(event_id, proposal_id, status, event_ts, detail_json) "
                "VALUES (?,?,?,?,?)",
                (uuid.uuid4().hex, proposal_id, "PROPOSED", timestamp,
                 _canonical_json({
                     "variant_id": variant.variant_id,
                     "target_parameter": target_parameter,
                     "minimum": float(minimum), "maximum": float(maximum),
                     "reasoning": reasoning,
                 })))
            conn.execute(
                "INSERT INTO findings (variant_id, ts, author, kind, text, "
                "run_id) VALUES (?,?,?,?,?,NULL)",
                (variant.variant_id, timestamp, "llm", "observation",
                 _canonical_json({
                     "type": "llm_numeric_proposal", "proposal": row,
                 })))
        return row

    def set_status(self, variant_id: str, status: str) -> None:
        with _connect(self.path) as conn:
            conn.execute(
                "UPDATE variants SET status=?, updated_ts=? "
                "WHERE variant_id=?", (status, time.time(), variant_id))

    def variants(self) -> list:
        with _connect(self.path) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM variants ORDER BY variant_id")]

    def variant(self, variant_id: str) -> dict | None:
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT * FROM variants WHERE variant_id=?",
                (variant_id,)).fetchone()
        return dict(row) if row else None

    # ----------------------------------------------------------------- runs

    def record_run(self, run_id: str, variant_id: str, result,
                   scorer_version: str = "1", code_version: str = "") -> None:
        with _connect(self.path) as conn:
            self._require_variant(conn, variant_id)
            conn.execute(
                "INSERT INTO variant_runs (run_id, variant_id, "
                "corpus_from_ts, corpus_to_ts, corpus_cycles, mode, "
                "code_version, scorer_version, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, variant_id, result.corpus_from_ts,
                 result.corpus_to_ts, result.cycles, result.mode,
                 code_version, scorer_version, time.time()))

    def record_metrics(self, run_id: str, scored: dict) -> None:
        rows = self._metric_rows(run_id, scored)
        with _connect(self.path) as conn:
            if conn.execute(
                    "SELECT 1 FROM variant_runs WHERE run_id=?",
                    (run_id,)).fetchone() is None:
                raise ValueError(f"unknown run_id {run_id!r}")
            conn.executemany(
                "INSERT INTO variant_results (run_id, metric, value, "
                "ci_low, ci_high, n) VALUES (?,?,?,?,?,?)", rows)

    def record_evaluation(
            self, run_id: str, variant_id: str, result, scored: dict,
            finding_text: str, kind: str = "decision",
            author: str = "research", scorer_version: str = "1",
            code_version: str = "") -> int:
        """Atomically append one run, its metrics, and its conclusion."""
        if kind not in KINDS:
            raise ValueError(
                f"kind must be one of {', '.join(KINDS)}, got {kind!r}")
        rows = self._metric_rows(run_id, scored)
        now = time.time()
        with _connect(self.path) as conn:
            self._require_variant(conn, variant_id)
            conn.execute(
                "INSERT INTO variant_runs (run_id, variant_id, "
                "corpus_from_ts, corpus_to_ts, corpus_cycles, mode, "
                "code_version, scorer_version, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, variant_id, result.corpus_from_ts,
                 result.corpus_to_ts, result.cycles, result.mode,
                 code_version, scorer_version, now))
            conn.executemany(
                "INSERT INTO variant_results (run_id, metric, value, "
                "ci_low, ci_high, n) VALUES (?,?,?,?,?,?)", rows)
            cursor = conn.execute(
                "INSERT INTO findings (variant_id, ts, author, kind, text, "
                "run_id) VALUES (?,?,?,?,?,?)",
                (variant_id, now, author, kind, finding_text, run_id))
            finding_id = int(cursor.lastrowid)
        self.backup()
        return finding_id

    @staticmethod
    def _metric_rows(run_id: str, scored: dict) -> list:
        rows = []
        for metric, value in scored.items():
            if metric in ("label", "verdict"):
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            rows.append((run_id, metric, numeric,
                         float(scored.get("ci_low") or 0.0),
                         float(scored.get("ci_high") or 0.0),
                         int(scored.get("n") or 0)))
        return rows

    @staticmethod
    def _require_variant(
            conn: sqlite3.Connection, variant_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM variants WHERE variant_id=?", (variant_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown variant_id {variant_id!r}")
        return row

    def runs_for(self, variant_id: str) -> list:
        with _connect(self.path) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM variant_runs WHERE variant_id=? "
                "ORDER BY ts DESC", (variant_id,))]

    def metrics_for(self, run_id: str) -> dict:
        with _connect(self.path) as conn:
            return {r["metric"]: dict(r) for r in conn.execute(
                "SELECT * FROM variant_results WHERE run_id=?", (run_id,))}

    # ------------------------------------------------------------- findings

    def add_finding(self, variant_id: str, kind: str, text: str,
                    author: str = "research", run_id: str = "",
                    ts: float | None = None) -> int:
        """Append a finding. Nothing in this class ever deletes one."""
        if kind not in KINDS:
            raise ValueError(
                f"kind must be one of {', '.join(KINDS)}, got {kind!r}")
        with _connect(self.path) as conn:
            self._require_variant(conn, variant_id)
            if run_id and conn.execute(
                    "SELECT 1 FROM variant_runs WHERE run_id=?",
                    (run_id,)).fetchone() is None:
                raise ValueError(f"unknown run_id {run_id!r}")
            cursor = conn.execute(
                "INSERT INTO findings (variant_id, ts, author, kind, text, "
                "run_id) VALUES (?,?,?,?,?,?)",
                (variant_id, ts if ts is not None else time.time(),
                 author, kind, text, run_id or None))
            finding_id = int(cursor.lastrowid)
        self.backup()
        return finding_id

    def findings_for(self, variant_id: str) -> list:
        with _connect(self.path) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM findings WHERE variant_id=? "
                "ORDER BY ts ASC, finding_id ASC", (variant_id,))]


# --------------------------------------------------------------- scorecards

def _fmt(value, spec: str = "+.4f") -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if number == float("inf"):
        return "inf"
    return format(number, spec)


def scorecard(store: FindingsStore, variant_id: str,
              baseline_id: str = "momentum.baseline") -> str:
    """One markdown file per variant, regenerated deterministically.

    Deterministic matters because these are committed: a generator whose
    output moved on every run would produce a diff on every run, and a diff
    that always appears is a diff nobody reads.

    Nothing here is derived from ``time.time()`` for that reason - the
    timestamps come from the stored rows.
    """
    variant = store.variant(variant_id)
    if variant is None:
        return f"# {variant_id}\n\nNot registered.\n"

    runs = store.runs_for(variant_id)
    latest = runs[0] if runs else None
    metrics = store.metrics_for(latest["run_id"]) if latest else {}
    overrides = json.loads(variant["overrides_json"] or "{}")

    lines = [f"# {variant_id}", ""]
    lines.append(f"Status: {variant['status']}")
    lines.append(f"Hypothesis: {variant['hypothesis']}")
    if overrides:
        rendered = ", ".join(f"{k} = {v}" for k, v in sorted(overrides.items()))
        lines.append(f"Overrides: {rendered}")
    else:
        lines.append("Overrides: none (this is the comparison floor)")
    lines.append("")

    lines.append("## Sample")
    if latest is None:
        lines += ["", "Registered but never run. No sample, and therefore no "
                      "result to report.", ""]
    else:
        n = int((metrics.get("n") or {}).get("n")
                or (metrics.get("expectancy_r") or {}).get("n") or 0)
        mde = (metrics.get("mde_r") or {}).get("value")
        lines += [
            "",
            f"corpus {int(latest['corpus_cycles'] or 0):,} cycles | "
            f"mode {latest['mode']} | {n} round trips",
            f"MDE at n={n}: {_fmt(mde, '.4f')}R "
            f"-- effects below this are undetectable",
            "",
        ]
        lines.append("## Results")
        lines.append("")
        lines.append("| metric | value | 95% interval |")
        lines.append("| --- | --- | --- |")
        for name in ("expectancy_r", "win_rate", "profit_factor", "total_r",
                     "max_drawdown_r"):
            row = metrics.get(name)
            if not row:
                continue
            interval = (f"[{_fmt(row['ci_low'])}, {_fmt(row['ci_high'])}]"
                        if name == "expectancy_r" else "")
            lines.append(f"| {name} | {_fmt(row['value'])} | {interval} |")
        lines.append("")

    runtime_rows = []
    for scope in store.paper_scopes():
        state = store.paper_portfolio_state(scope, variant_id)
        if state is None:
            continue
        all_summary = store.paper_summary(
            scope, variant_id, paper_only=False)
        paper_summary = store.paper_summary(
            scope, variant_id, paper_only=True)
        coverage = next((row for row in store.scheduler_coverage(scope)
                         if row["variant_id"] == variant_id), {})
        qualification = store.qualification_status(variant_id, scope) or {}
        runtime_rows.append((scope, state, all_summary, paper_summary,
                             coverage, qualification))
    if runtime_rows:
        lines += [
            "## Real-time learning and paper",
            "",
            "| scope | stage | evaluations / skips | accepts / vetoes | "
            "shadow closed | "
            "paper closed | paper expectancy | qualification |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for (scope, state, all_summary, paper_summary, coverage,
             qualification) in runtime_rows:
            paper_closed = int(paper_summary["closed_trades"] or 0)
            shadow_closed = max(
                0, int(all_summary["closed_trades"] or 0) - paper_closed)
            expectancy = _fmt(paper_summary.get("expectancy_r"))
            lines.append(
                f"| {scope} | {state.get('status', '-')} | "
                f"{int(coverage.get('evaluations') or 0)} / "
                f"{int(coverage.get('skips') or 0)} | "
                f"{int(all_summary.get('accepted_decisions') or 0)} / "
                f"{int(all_summary.get('vetoed_decisions') or 0)} | "
                f"{shadow_closed} | "
                f"{paper_closed} | {expectancy} | "
                f"{qualification.get('status', '-')} |")
        lines.append("")

    packets = store.t3_packets_for(variant_id)
    if packets:
        lines += ["## T3 evidence packets", ""]
        for packet in packets:
            lines.append(
                f"- `{packet['packet_id']}` — {packet['review_status']} — "
                f"SHA-256 `{packet.get('payload_hash') or 'legacy-unhashed'}`")
        lines.append("")

    log = store.findings_for(variant_id)
    lines.append("## Findings log")
    lines.append("")
    if not log:
        lines.append("No findings recorded yet.")
    else:
        for entry in log:
            stamp = time.strftime("%Y-%m-%d",
                                  time.gmtime(float(entry["ts"] or 0)))
            lines.append(f"- **{stamp}  {entry['kind']}** — {entry['text']}")
    lines.append("")
    return "\n".join(lines)


def audit_documents(root: str | Path) -> list:
    """Hand-written documents sitting beside the generated index.

    The index is the only entry point to ``findings/``, and it is rewritten
    from the store on every nightly run. Anything a person put there that the
    generator does not know about therefore becomes unreachable the first time
    the report runs - the file survives on disk and the link to it does not,
    which is the failure that is hardest to notice because nothing errors.

    So the section is discovered from disk rather than maintained by hand. A
    generator may overwrite what it wrote; it may not silently orphan what it
    did not.
    """
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(path for path in root.glob("*.md")
                  if path.name != "README.md")


def _document_title(path: Path) -> str:
    """The document's own H1, falling back to its filename."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    except OSError:
        pass
    return path.stem


def index(store: FindingsStore, root: str | Path | None = None) -> str:
    """``findings/README.md``: every variant, status, sample, last updated."""
    lines = [
        "# Findings index",
        "",
        "Every registered variant, including the rejected ones. A rejection "
        "is a row here, never a deletion: the question six months from now "
        "is not which variants are alive but why this one was rejected and "
        "on what sample.",
        "",
    ]
    documents = audit_documents(root) if root is not None else []
    if documents:
        lines += ["## Repository audits", ""]
        for path in documents:
            lines.append(f"- [{_document_title(path)}]({path.name})")
        lines += ["", "## Variant scorecards", ""]
    lines += [
        "| variant | status | round trips | expectancy | last updated |",
        "| --- | --- | --- | --- | --- |",
    ]
    for variant in store.variants():
        runs = store.runs_for(variant["variant_id"])
        metrics = store.metrics_for(runs[0]["run_id"]) if runs else {}
        expectancy = metrics.get("expectancy_r")
        n = int((expectancy or {}).get("n") or 0)
        updated = time.strftime(
            "%Y-%m-%d", time.gmtime(float(variant["updated_ts"] or 0)))
        lines.append(
            f"| [{variant['variant_id']}]"
            f"({variant['strategy_id']}/{variant['variant_id']}.md) "
            f"| {variant['status']} | {n} "
            f"| {_fmt((expectancy or {}).get('value'))} | {updated} |")
    lines.append("")
    return "\n".join(lines)


def write_scorecards(store: FindingsStore, root: str | Path) -> list:
    """Regenerate every scorecard. Running twice with no new data is a no-op."""
    root = Path(root)
    written = []
    for variant in store.variants():
        directory = root / variant["strategy_id"]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{variant['variant_id']}.md"
        path.write_text(scorecard(store, variant["variant_id"]),
                        encoding="utf-8")
        written.append(path)
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text(index(store, root), encoding="utf-8")
    written.append(root / "README.md")
    return written
