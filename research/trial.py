"""The paper-account trial lane: live paper evidence that feeds back into search.

Backtests answer "would this have worked". Only a live paper book answers "does
this work now", and until this existed that answer went nowhere: paper outcomes
drove surveillance and were never read by the thing that proposes what to try
next.

A trial is the window in which an auto-lane edge trades the same Alpaca paper
account as the runtime and its real results are collected. When the window
closes, the trial is judged
against an explicit floor. An edge that clears it keeps trading and becomes a
*promotable* candidate — something the operator may pin into configuration, a
decision this module never makes. An edge that misses it is parked, and the
reason is written into the lesson ledger as live evidence, so the next tuning
cycle proposes parameter values against what actually happened on the book
rather than only against a replay.

Two boundaries hold throughout. A pinned edge remains operator-selected, but
that selection cannot bypass a hard safety stop: a failed trial parks it just
like an automatic edge and records the pin context. Parking an underperformer
never promotes anything in its place — the replacement still has to earn
`backtest_passed`, a strictly later shadow pass, and every gate.
"""

from __future__ import annotations

from pathlib import Path
from contextlib import closing
import json
import math
import sqlite3
from typing import Any, Mapping, Sequence

from .edge_lab import DEFAULT_DB_PATH, EdgeLedger
from .edge_ledger import DEPLOYED_STATUSES
from .factory_ledger import FactoryError, FactoryLedger


TRIAL_SCHEMA = "paper-trial.v1"
# Defaults chosen to need a real sample before acting.  A trial that concludes
# from four trades is measuring noise, and parking an edge on noise costs more
# search than it saves.
DEFAULT_MIN_SESSIONS = 30
DEFAULT_MIN_TRADES = 100
DEFAULT_MIN_MEAN_R = 0.0
DEFAULT_MIN_TOTAL_R = 0.0


def trial_policy(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Resolve the trial window and its floor from configuration."""
    research = (config or {}).get("research") if isinstance(config, Mapping) else {}
    raw = (research or {}).get("trial") if isinstance(research, Mapping) else {}
    raw = raw if isinstance(raw, Mapping) else {}

    def number(key: str, default: float) -> float:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return float(default)
        return float(value)

    return {
        "enabled": bool(raw.get("enabled", True)),
        "min_sessions": max(1, int(number("min_sessions", DEFAULT_MIN_SESSIONS))),
        "min_trades": max(1, int(number("min_trades", DEFAULT_MIN_TRADES))),
        "min_mean_r": number("min_mean_r", DEFAULT_MIN_MEAN_R),
        "min_total_r": number("min_total_r", DEFAULT_MIN_TOTAL_R),
    }


def _verdict(performance: Mapping[str, Any], policy: Mapping[str, Any]) -> dict:
    """Judge one edge's live paper record against the trial floor."""
    sessions = int(performance.get("sessions") or 0)
    trades = int(performance.get("outcomes") or 0)
    total_r = performance.get("total_r")
    mean_r = performance.get("mean_r")
    confidence = performance.get("session_cluster_confidence")
    if sessions < int(policy["min_sessions"]) or trades < int(policy["min_trades"]):
        return {"state": "running", "sessions": sessions, "trades": trades,
                "sessions_required": int(policy["min_sessions"]),
                "trades_required": int(policy["min_trades"]),
                "total_r": total_r, "mean_r": mean_r,
                "session_cluster_confidence": confidence}
    # A concluded window with no measurable R is not a pass.  It means the
    # outcomes carried no risk reference, which is a data problem, not an edge.
    if total_r is None or mean_r is None:
        return {"state": "inconclusive", "sessions": sessions, "trades": trades,
                "reason": "outcomes carry no usable R multiple",
                "total_r": total_r, "mean_r": mean_r,
                "session_cluster_confidence": confidence}
    clears = (float(total_r) > float(policy["min_total_r"]) and
              float(mean_r) > float(policy["min_mean_r"]))
    if not clears:
        return {"state": "failed",
                "sessions": sessions, "trades": trades,
                "total_r": float(total_r), "mean_r": float(mean_r),
                "min_total_r": float(policy["min_total_r"]),
                "min_mean_r": float(policy["min_mean_r"]),
                "net_pnl": performance.get("net_pnl"),
                "win_rate": performance.get("win_rate"),
                "session_cluster_confidence": confidence}
    # A positive point estimate is only promotable when the independent
    # session-cluster interval also clears the mean-R floor.  Missing or
    # underpowered uncertainty evidence is not a failure of the edge, so it
    # stays inconclusive and cannot park a live candidate.
    usable = confidence if isinstance(confidence, Mapping) else {}
    try:
        confidence_level = float(usable.get("confidence"))
        lower_bound = float(usable.get("lower_bound"))
        usable_observations = int(usable.get("observations") or 0)
        usable_clusters = int(usable.get("session_clusters") or
                              usable.get("clusters") or 0)
    except (TypeError, ValueError, OverflowError):
        confidence_level = lower_bound = float("nan")
        usable_observations = usable_clusters = 0
    confidence_ready = bool(
        usable.get("available") is True and
        confidence_level >= 0.95 and
        math.isfinite(lower_bound) and
        usable_observations >= int(policy["min_trades"]) and
        usable_clusters >= int(policy["min_sessions"]))
    if not confidence_ready or lower_bound <= float(policy["min_mean_r"]):
        return {"state": "inconclusive", "sessions": sessions, "trades": trades,
                "reason": ("positive point estimate lacks a session-cluster "
                           "lower bound above the configured mean-R floor"),
                "total_r": float(total_r), "mean_r": float(mean_r),
                "min_total_r": float(policy["min_total_r"]),
                "min_mean_r": float(policy["min_mean_r"]),
                "net_pnl": performance.get("net_pnl"),
                "win_rate": performance.get("win_rate"),
                "session_cluster_confidence": confidence}
    return {"state": "passed",
            "sessions": sessions, "trades": trades,
            "total_r": float(total_r), "mean_r": float(mean_r),
            "min_total_r": float(policy["min_total_r"]),
            "min_mean_r": float(policy["min_mean_r"]),
            "net_pnl": performance.get("net_pnl"),
            "win_rate": performance.get("win_rate"),
            "session_cluster_confidence": confidence}


def _session_cluster_confidence(ledger: EdgeLedger, candidate_id: str) -> dict:
    """Estimate uncertainty from independent live-paper session clusters.

    The trial floor remains the lifecycle decision.  This is evidence attached
    to that decision, using the same deterministic moving-block helper as the
    research gates so repeated reviews produce the same interval.
    """
    empty = {
        "schema": "session-cluster-confidence.v1", "available": False,
        "confidence": 0.95, "lower_bound": None, "upper_bound": None,
        "mean": None, "clusters": 0, "observations": 0,
        "method": "moving_block_cluster_bootstrap",
        "reason": "no_usable_r_observations",
    }
    try:
        epoch, has_shadow = ledger._trial_epoch_state(candidate_id)
        with closing(sqlite3.connect(ledger.path)) as db:
            db.row_factory = sqlite3.Row
            if has_shadow and epoch is None:
                rows = []
            elif epoch is None:
                rows = db.execute(
                    "SELECT session_date,net_pnl,outcome_json FROM paper_outcomes "
                    "WHERE candidate_id=? AND proof_run_id IS NULL "
                    "ORDER BY session_date,created_at,outcome_id", (candidate_id,)).fetchall()
            else:
                rows = db.execute(
                    "SELECT session_date,net_pnl,outcome_json FROM paper_outcomes "
                    "WHERE candidate_id=? AND proof_run_id=? "
                    "ORDER BY session_date,created_at,outcome_id", (candidate_id, epoch)).fetchall()
        values: list[float] = []
        clusters: list[str] = []
        for row in rows:
            session = str(row["session_date"] or "").strip()
            if not session:
                continue
            try:
                payload = json.loads(row["outcome_json"])
                if not isinstance(payload, Mapping):
                    continue
                value = payload.get("r_multiple")
                if value is None:
                    net = row["net_pnl"]
                    risk = payload.get("risk_usd")
                    net_value = float(net)
                    risk_value = float(risk)
                    value = (net_value / risk_value
                             if math.isfinite(net_value) and
                             math.isfinite(risk_value) and risk_value > 0
                             else None)
                value = float(value)
            except (TypeError, ValueError, OverflowError, json.JSONDecodeError,
                    AttributeError):
                continue
            if math.isfinite(value):
                values.append(value)
                clusters.append(session)
        if not values:
            return empty
        from .stats import moving_block_cluster_bootstrap_lower_bound
        cluster_count = len(set(clusters))
        bound = moving_block_cluster_bootstrap_lower_bound(
            values, clusters, confidence=.95, draws=1000,
            block_length=max(1, min(5, cluster_count - 1)), min_clusters=2)
        return {
            "schema": "session-cluster-confidence.v1",
            **{key: bound.get(key) for key in (
                "available", "confidence", "lower_bound", "upper_bound",
                "mean", "clusters", "observations", "method", "reason")},
            "session_clusters": cluster_count,
            "proof_run_id": epoch,
        }
    except (OSError, sqlite3.Error, TypeError, ValueError, KeyError):
        return {**empty, "reason": "confidence_unavailable"}


def _reason(record: Mapping[str, Any], verdict: Mapping[str, Any]) -> str:
    """State what the live book showed, in the same shape as a tuning reason."""
    return (
        f"Live paper trial over {verdict['sessions']} session(s) and "
        f"{verdict['trades']} trade(s) returned total R "
        f"{verdict['total_r']:.2f} (mean {verdict['mean_r']:.3f}), below the "
        f"floor of {verdict['min_total_r']:.2f}/{verdict['min_mean_r']:.3f}; "
        f"the replay evidence did not survive contact with the book."
    )[:240]


def _family_of(ledger: EdgeLedger, record: Mapping[str, Any]) -> str | None:
    import json
    try:
        config = json.loads(record.get("config_json") or "{}")
    except (TypeError, ValueError):
        return None
    spec = (config.get("strategy") or {}).get("rule_spec")
    return str(spec["family"]) if isinstance(spec, Mapping) and spec.get("family") else None


def _hypothesis_of(record: Mapping[str, Any]) -> str | None:
    import json
    try:
        axes = json.loads(record.get("axes_json") or "{}")
    except (TypeError, ValueError):
        return None
    value = axes.get("hypothesis_id")
    return str(value) if value else None


def _learning_epoch_of(ledger: EdgeLedger, candidate_id: str) -> str | None:
    """Read the exact epoch from the candidate's latest verified proof.

    Candidate provenance is intentionally stored as a hash, so trial review
    must not reconstruct an epoch from ambient configuration.  Factory proof
    runs persist the token in their immutable metrics; candidates without that
    durable stamp remain legacy/audit-only rather than being relabeled.
    """
    try:
        proof = ledger.latest_verified_run(candidate_id, lane="shadow")
    except (TypeError, ValueError, KeyError, sqlite3.Error):
        return None
    if not isinstance(proof, Mapping):
        return None
    gate = proof.get("verified_gate")
    if not isinstance(gate, Mapping) or gate.get("passes") is not True:
        return None
    metrics = proof.get("metrics")
    value = metrics.get("learning_epoch") if isinstance(metrics, Mapping) else None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def review_trials(db_path: str | Path = DEFAULT_DB_PATH, *,
                  config: Mapping[str, Any] | None = None,
                  vehicle: str | None = None,
                  pinned: Sequence[tuple[str, str]] = (),
                  apply: bool = True) -> dict:
    """Judge every auto-lane trial, park the failures, and record what they taught.

    ``apply=False`` reports the same verdicts without touching a lifecycle, so
    an operator can see what a review would do before letting it do it.
    """
    policy = trial_policy(config)
    ledger = EdgeLedger(db_path)
    factory = FactoryLedger(db_path)
    frozen = {(str(item[0]), str(item[1])) for item in pinned}
    reviews: list[dict] = []
    parked: list[dict] = []
    promotable: list[dict] = []
    if not policy["enabled"]:
        return {"schema": TRIAL_SCHEMA, "enabled": False, "policy": policy,
                "reviews": [], "parked": [], "promotable": []}
    for candidate in ledger.status(vehicle=vehicle):
        if candidate.get("status") not in DEPLOYED_STATUSES:
            continue
        candidate_id = str(candidate["candidate_id"])
        variant_id = str(candidate.get("variant_id") or "")
        candidate_vehicle = str(candidate.get("vehicle") or "")
        is_pinned = (variant_id, candidate_vehicle) in frozen
        performance = ledger.paper_performance(candidate_id)
        performance = {
            **performance,
            "session_cluster_confidence": _session_cluster_confidence(
                ledger, candidate_id),
        }
        verdict = _verdict(performance, policy)
        learning_epoch = _learning_epoch_of(ledger, candidate_id)
        review = {
            "candidate_id": candidate_id, "variant_id": variant_id,
            "vehicle": candidate_vehicle, "status": candidate.get("status"),
            "pinned": is_pinned, "verdict": verdict,
            "learning_epoch": learning_epoch,
            "family": _family_of(ledger, candidate),
        }
        if verdict["state"] == "passed":
            # A pin is already the operator's selection; keep it out of the
            # promotable hand-off while still evaluating its safety verdict.
            if is_pinned:
                review["action"] = "none_pinned"
                reviews.append(review)
                continue
            review["action"] = "promotable"
            promotable.append({"candidate_id": candidate_id,
                               "variant_id": variant_id,
                               "vehicle": candidate_vehicle,
                               "total_r": verdict["total_r"],
                               "mean_r": verdict["mean_r"],
                               "net_pnl": verdict.get("net_pnl"),
                               "sessions": verdict["sessions"],
                               "trades": verdict["trades"],
                               "session_cluster_confidence": verdict.get(
                                   "session_cluster_confidence")})
        elif verdict["state"] == "failed" and apply:
            review["action"] = "parked"
            reason = _reason(candidate, verdict)
            ledger.transition(
                candidate_id, "demoted",
                reason="live paper trial finished below its floor",
                actor="trial", payload={
                    "schema": TRIAL_SCHEMA, **verdict,
                    "pinned": is_pinned,
                    "pin_context": ({"variant_id": variant_id,
                                     "vehicle": candidate_vehicle}
                                    if is_pinned else {}),
                })
            _record_live_lesson(factory, ledger, candidate, verdict, reason,
                                learning_epoch=learning_epoch)
            parked.append({"candidate_id": candidate_id,
                           "variant_id": variant_id,
                           "vehicle": candidate_vehicle,
                           "total_r": verdict["total_r"],
                           "sessions": verdict["sessions"], "reason": reason})
        elif verdict["state"] == "failed":
            review["action"] = "would_park"
        else:
            review["action"] = "none"
        reviews.append(review)
    return {"schema": TRIAL_SCHEMA, "enabled": True, "policy": policy,
            "applied": bool(apply), "reviews": reviews, "parked": parked,
            "promotable": promotable}


def _record_live_lesson(factory: FactoryLedger, ledger: EdgeLedger,
                        candidate: Mapping[str, Any], verdict: Mapping[str, Any],
                        reason: str, *, learning_epoch: str | None = None) -> None:
    """Write the trial result into the lesson ledger as live evidence.

    This is the point of the lane.  A parked edge whose failure is only a
    lifecycle row teaches nothing; the same failure recorded as a graded lesson
    is read by the next tuning request, so the parameters proposed next are
    answering the book rather than the backtest.
    """
    hypothesis_id = _hypothesis_of(candidate)
    if not hypothesis_id:
        return
    variant_id = str(candidate.get("variant_id") or "")
    vehicle = str(candidate.get("vehicle") or "equity")
    family = _family_of(ledger, candidate) or "unknown"
    try:
        factory.record_lesson(
            hypothesis_id, vehicle=vehicle, family=family,
            variant_id=variant_id, kind="trial", source="live_paper",
            reason=reason,
            changed={"lane": "live_paper_trial"},
            diagnosis={"primary_failure": "live_paper_underperformance",
                       "sessions": verdict["sessions"],
                       "trades": verdict["trades"],
                       "total_r": verdict["total_r"],
                       "mean_r": verdict["mean_r"]},
            evidence={"schema": TRIAL_SCHEMA},
            learning_epoch=learning_epoch)
        factory.grade_lesson(
            hypothesis_id, variant_id, kind="trial",
            outcome={"passed": False, "underpowered": False,
                     "heldout_delta": verdict["mean_r"],
                     "heldout_net_pnl": verdict.get("net_pnl"),
                     "failed_checks": ["live_paper_total_r_positive"]},
            learning_epoch=learning_epoch)
    except (FactoryError, KeyError, Exception):  # noqa: BLE001
        # The lifecycle change already happened and is the safety-relevant
        # half.  Losing its annotation must not raise into a scheduled job.
        return


def promotable_report(db_path: str | Path = DEFAULT_DB_PATH, *,
                      config: Mapping[str, Any] | None = None,
                      vehicle: str | None = None,
                      pinned: Sequence[tuple[str, str]] = ()) -> list[dict]:
    """Edges whose live paper record clears the floor, with how to promote them.

    Nothing here promotes anything.  It answers the operator's question — which
    variant, under which edge, actually returned positive — and hands over the
    exact configuration block, because retyping a content-addressed id is how a
    promotion silently points at the wrong thing.
    """
    from agent.governance import promotion_snippet

    review = review_trials(db_path, config=config, vehicle=vehicle,
                           pinned=pinned, apply=False)
    frozen = {(str(item[0]), str(item[1])) for item in pinned}
    ledger = EdgeLedger(db_path)
    rows = []
    for item in review["reviews"]:
        verdict = item["verdict"]
        if verdict["state"] != "passed":
            continue
        performance = ledger.paper_performance(item["candidate_id"])
        already = (item["variant_id"], item["vehicle"]) in frozen
        rows.append({
            "candidate_id": item["candidate_id"],
            "variant_id": item["variant_id"],
            "vehicle": item["vehicle"],
            "family": item["family"],
            "status": item["status"],
            "already_pinned": already,
            "sessions": verdict["sessions"], "trades": verdict["trades"],
            "total_r": verdict["total_r"], "mean_r": verdict["mean_r"],
            "win_rate": performance.get("win_rate"),
            "net_pnl": performance.get("net_pnl"),
            "mean_r_pct": _mean_r_pct(performance),
            "capital_return_pct": _capital_return_pct(performance),
            # Compatibility alias for existing consumers.  It is explicitly
            # tied to the mean-R basis; new consumers should use
            # ``mean_r_pct`` or the denominator-gated capital field above.
            "return_pct": _mean_r_pct(performance),
            "return_pct_basis": "mean_r_pct",
            "session_cluster_confidence": verdict.get(
                "session_cluster_confidence"),
            "config_snippet": (None if already else promotion_snippet(
                item["variant_id"], item["vehicle"],
                note=(f"live paper: {verdict['trades']} trades over "
                      f"{verdict['sessions']} sessions, total R "
                      f"{verdict['total_r']:.2f}"))),
        })
    return sorted(rows, key=lambda row: row["total_r"], reverse=True)


def _mean_r_pct(performance: Mapping[str, Any]) -> float | None:
    """Display mean R as a percentage, with a label that states its basis."""
    mean_r = performance.get("mean_r")
    if mean_r is None:
        return None
    try:
        value = float(mean_r)
    except (TypeError, ValueError, OverflowError):
        return None
    return round(100.0 * value, 4) if math.isfinite(value) else None


def _capital_return_pct(performance: Mapping[str, Any]) -> float | None:
    """Return capital P&L only when an explicit capital denominator exists."""
    pnl = performance.get("net_pnl")
    denominator = next((performance.get(key) for key in (
        "capital_return_denominator", "starting_equity", "initial_equity",
        "equity_denominator") if performance.get(key) is not None), None)
    try:
        pnl_value = float(pnl)
        denominator_value = float(denominator)
    except (TypeError, ValueError, OverflowError):
        return None
    if (not math.isfinite(pnl_value) or not math.isfinite(denominator_value)
            or denominator_value <= 0):
        return None
    return round(100.0 * pnl_value / denominator_value, 4)


__all__ = ["DEFAULT_MIN_SESSIONS", "DEFAULT_MIN_TRADES", "TRIAL_SCHEMA",
           "promotable_report", "review_trials", "trial_policy"]
