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
    if sessions < int(policy["min_sessions"]) or trades < int(policy["min_trades"]):
        return {"state": "running", "sessions": sessions, "trades": trades,
                "sessions_required": int(policy["min_sessions"]),
                "trades_required": int(policy["min_trades"]),
                "total_r": total_r, "mean_r": mean_r}
    # A concluded window with no measurable R is not a pass.  It means the
    # outcomes carried no risk reference, which is a data problem, not an edge.
    if total_r is None or mean_r is None:
        return {"state": "inconclusive", "sessions": sessions, "trades": trades,
                "reason": "outcomes carry no usable R multiple",
                "total_r": total_r, "mean_r": mean_r}
    clears = (float(total_r) > float(policy["min_total_r"]) and
              float(mean_r) > float(policy["min_mean_r"]))
    return {"state": "passed" if clears else "failed",
            "sessions": sessions, "trades": trades,
            "total_r": float(total_r), "mean_r": float(mean_r),
            "min_total_r": float(policy["min_total_r"]),
            "min_mean_r": float(policy["min_mean_r"]),
            "net_pnl": performance.get("net_pnl"),
            "win_rate": performance.get("win_rate")}


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
        verdict = _verdict(performance, policy)
        review = {
            "candidate_id": candidate_id, "variant_id": variant_id,
            "vehicle": candidate_vehicle, "status": candidate.get("status"),
            "pinned": is_pinned, "verdict": verdict,
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
                               "trades": verdict["trades"]})
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
            _record_live_lesson(factory, ledger, candidate, verdict, reason)
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
                        reason: str) -> None:
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
            evidence={"schema": TRIAL_SCHEMA})
        factory.grade_lesson(
            hypothesis_id, variant_id, kind="trial",
            outcome={"passed": False, "underpowered": False,
                     "heldout_delta": verdict["mean_r"],
                     "heldout_net_pnl": verdict.get("net_pnl"),
                     "failed_checks": ["live_paper_total_r_positive"]})
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
            "return_pct": _return_pct(performance),
            "config_snippet": (None if already else promotion_snippet(
                item["variant_id"], item["vehicle"],
                note=(f"live paper: {verdict['trades']} trades over "
                      f"{verdict['sessions']} sessions, total R "
                      f"{verdict['total_r']:.2f}"))),
        })
    return sorted(rows, key=lambda row: row["total_r"], reverse=True)


def _return_pct(performance: Mapping[str, Any]) -> float | None:
    """Realized P&L as a percentage of the risk actually deployed.

    Expressed against risk rather than account equity: the account is shared by
    every edge, so an equity-relative number would say more about the other
    edges than about this one.
    """
    total_r = performance.get("total_r")
    trades = performance.get("outcomes")
    if total_r is None or not trades:
        return None
    try:
        return round(100.0 * float(total_r) / float(trades), 4)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


__all__ = ["DEFAULT_MIN_SESSIONS", "DEFAULT_MIN_TRADES", "TRIAL_SCHEMA",
           "promotable_report", "review_trials", "trial_policy"]
