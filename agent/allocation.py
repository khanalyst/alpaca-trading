"""Deterministic portfolio allocation across concurrently proved edges.

``all_proved`` paper execution may hold several independently proved
candidates at once.  Iterating them in ledger or family-name order lets an
alphabetically early family consume the shared risk slots before a stronger
one is considered, and lets several expressions of the same directional bet
count as independent risks.  This module ranks candidates on the single
evidence notion the ledger already champions on, estimates their pairwise
correlation from persisted held-out sessions, and admits the best feasible set.

Allocation only ever narrows and reorders the candidate set.  Every per-order
risk check in the cycle still runs unchanged, so no configured cap can be
exceeded by anything decided here.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Mapping, Sequence

from research.edge_lab import DEFAULT_DB_PATH, EdgeLedger
from research.factory_ledger import FactoryLedger

# Correlation is estimated on held-out per-session R, matched by session date.
# Fewer than this many shared sessions is not an estimate, so the pair is
# treated as maximally correlated rather than independent.
MIN_OVERLAP_SESSIONS = 10
UNKNOWN_CORRELATION = 1.0
CORRELATION_THRESHOLD = 0.5
# How many already-admitted candidates a further candidate may be correlated
# with at or above the threshold.  One means one expression per cluster.
MAX_CORRELATED_ADMISSIONS = 1


def _record_family(record: Mapping) -> str:
    """Extract an executable family without changing candidate identity."""
    if record.get("family"):
        return str(record["family"])
    axes = record.get("axes") if isinstance(record.get("axes"), Mapping) else {}
    config = record.get("config") if isinstance(record.get("config"), Mapping) else {}
    strategy = config.get("strategy") if isinstance(config.get("strategy"), Mapping) else {}
    spec = strategy.get("rule_spec") if isinstance(strategy.get("rule_spec"), Mapping) else {}
    return str(spec.get("family") or axes.get("family") or "")


def _frozen_cluster(record: Mapping, policy: Mapping | None) -> str | None:
    """Return a verified persisted cluster assignment, if one exists."""
    if not isinstance(policy, Mapping) or policy.get("verified_persisted") is not True:
        return None
    mapping = policy.get("cluster_map")
    if not isinstance(mapping, Mapping):
        return None
    value = mapping.get(_record_family(record))
    return str(value) if value else None


def _finite(value) -> float | None:
    if isinstance(value, (bool, bytes, bytearray, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def evidence_rank(record: Mapping) -> tuple:
    """Rank a proved candidate on the ledger's own conservative ordering.

    ``EdgeLedger.select_champion`` ranks on held-out delta lower confidence
    bound, then drawdown, sample size, and point estimate.  The same ordering
    is used here so "better evidence" means one thing in this codebase; the
    trailing ids only make ties deterministic.  A missing or non-finite metric
    collapses to its worst admissible value: unmeasured evidence never
    outranks measured evidence.  The confidence floor is not a rank key
    because eligibility has already enforced it.
    """
    proof = record.get("latest_proof") if isinstance(
        record.get("latest_proof"), Mapping) else {}
    gate = proof.get("verified_gate") if isinstance(
        proof.get("verified_gate"), Mapping) else {}
    performance = gate.get("performance") if isinstance(
        gate.get("performance"), Mapping) else {}
    counts = gate.get("counts") if isinstance(gate.get("counts"), Mapping) else {}
    heldout = counts.get("heldout") if isinstance(counts.get("heldout"), Mapping) else {}
    lower_bound = _finite(performance.get("heldout_delta_lcb"))
    drawdown = _finite(performance.get("max_drawdown"))
    trades = _finite(heldout.get("trades"))
    delta = _finite(performance.get("heldout_delta"))
    return (lower_bound if lower_bound is not None else -math.inf,
            -(drawdown if drawdown is not None and drawdown >= 0 else math.inf),
            int(trades) if trades is not None and trades >= 0 else 0,
            delta if delta is not None else -math.inf,
            str(record.get("variant_id") or ""),
            str(record.get("candidate_id") or ""))


def heldout_sessions(ledger: EdgeLedger, record: Mapping) -> dict[str, float]:
    """Return held-out R summed per session for the candidate's proving run.

    The rows are the same persisted held-out trades the latest re-verified
    shadow proof was computed over, so correlation is measured on the evidence
    that authorized deployment.  An unreadable run yields no sessions, which
    the estimator reports as unknown rather than as independence.
    """
    candidate_id = str(record.get("candidate_id") or "")
    if not candidate_id:
        return {}
    try:
        run = ledger.latest_verified_run(candidate_id, lane="shadow")
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(run, Mapping):
        return {}
    run_id = str(run.get("run_id") or "")
    start, end = run.get("heldout_start"), run.get("heldout_end")
    if not run_id or not isinstance(start, str) or not isinstance(end, str):
        return {}
    try:
        rows = ledger.trades(candidate_id, lane="shadow")
    except Exception:  # noqa: BLE001
        return {}
    sessions: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, Mapping) or str(row.get("run_id") or "") != run_id:
            continue
        session = str(row.get("session_date") or "")
        if not session or not start <= session <= end:
            continue
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping) or payload.get("no_trade") is True:
            continue
        value = _finite(payload.get("r_multiple"))
        if value is None:
            continue
        sessions[session] = sessions.get(session, 0.0) + value
    return sessions


def correlation(left: Mapping[str, float], right: Mapping[str, float]) -> float:
    """Pearson correlation of two session-matched R series, fail-closed.

    Insufficient overlap or a degenerate (zero variance) series is not an
    estimate of independence; it is an absence of evidence, and is reported as
    maximal correlation.  A negative estimate is floored at zero: this
    allocator declines to grant an extra slot on the strength of a claimed
    hedge.
    """
    shared = sorted(set(left) & set(right))
    if len(shared) < MIN_OVERLAP_SESSIONS:
        return UNKNOWN_CORRELATION
    a = [float(left[key]) for key in shared]
    b = [float(right[key]) for key in shared]
    mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
    da = [value - mean_a for value in a]
    db = [value - mean_b for value in b]
    var_a = sum(value * value for value in da)
    var_b = sum(value * value for value in db)
    if var_a <= 0 or var_b <= 0:
        return UNKNOWN_CORRELATION
    covariance = sum(x * y for x, y in zip(da, db))
    try:
        value = covariance / math.sqrt(var_a * var_b)
    except (ValueError, ZeroDivisionError, OverflowError):
        return UNKNOWN_CORRELATION
    if not math.isfinite(value):
        return UNKNOWN_CORRELATION
    return max(0.0, min(1.0, value))


def allocate(records: Sequence[Mapping], *, free_slots: int,
             db_path: str | Path | None = None,
             ledger: EdgeLedger | None = None,
             dependence_policy: Mapping | None = None) -> dict:
    """Choose the best feasible candidate set for one cycle.

    Greedy over the evidence ranking: the strongest candidate is admitted
    first, and each further candidate is admitted only while a free position
    slot remains and it is not already represented by a correlated admission.
    Every input appears exactly once in ``admitted`` or ``rejected``, and each
    rejection carries a durable reason.  The result is a pure function of
    persisted evidence, so repeated cycles select the same set.
    """
    ranked = sorted(records, key=evidence_rank, reverse=True)
    if ledger is None:
        try:
            ledger = EdgeLedger(db_path or DEFAULT_DB_PATH)
        except Exception:  # noqa: BLE001
            ledger = None
    policy = dependence_policy
    if policy is None:
        try:
            vehicles = {str(record.get("vehicle") or "equity") for record in ranked}
            vehicle = next(iter(vehicles)) if len(vehicles) == 1 else "equity"
            policy_path = db_path or getattr(ledger, "path", None) or DEFAULT_DB_PATH
            policy = FactoryLedger(policy_path).latest_dependence_policy(vehicle=vehicle)
        except Exception:  # noqa: BLE001 - unreadable policy is safe fallback
            policy = None
    sessions: list[dict[str, float]] = []
    for record in ranked:
        try:
            sessions.append(heldout_sessions(ledger, record) if ledger else {})
        except Exception:  # noqa: BLE001
            sessions.append({})
    admitted: list[Mapping] = []
    admitted_index: list[int] = []
    admitted_clusters: dict[str, int] = {}
    rejected: list[dict] = []
    slots = max(0, int(free_slots))
    for index, record in enumerate(ranked):
        identity = {"candidate_id": record.get("candidate_id"),
                    "variant_id": record.get("variant_id")}
        if len(admitted) >= slots:
            rejected.append({**identity, "reason": "no free concurrent position slot",
                             "free_slots": slots})
            continue
        cluster = _frozen_cluster(record, policy)
        if cluster is not None and cluster in admitted_clusters:
            peer_index = admitted_clusters[cluster]
            rejected.append({**identity,
                             "reason": "frozen dependence cluster",
                             "dependence_cluster": cluster,
                             "cluster_source": policy.get("policy_hash"),
                             "correlated_with": ranked[peer_index].get("variant_id")})
            continue
        peers = []
        for other in admitted_index:
            value = correlation(sessions[index], sessions[other])
            if value >= CORRELATION_THRESHOLD:
                peers.append((ranked[other].get("variant_id"), value))
        if len(peers) >= MAX_CORRELATED_ADMISSIONS:
            worst = max(peers, key=lambda item: item[1])
            rejected.append({**identity,
                             "reason": "correlated with an admitted candidate",
                             "correlated_with": worst[0],
                             "correlation": worst[1]})
            continue
        admitted.append(record)
        admitted_index.append(index)
        if cluster is not None:
            admitted_clusters[cluster] = index
    return {"admitted": admitted, "rejected": rejected,
            "dependence_policy": (dict(policy) if isinstance(policy, Mapping) else None),
            "dependence_policy_hash": (policy.get("policy_hash")
                                        if isinstance(policy, Mapping) else None)}


__all__ = ["CORRELATION_THRESHOLD", "MAX_CORRELATED_ADMISSIONS",
           "MIN_OVERLAP_SESSIONS", "UNKNOWN_CORRELATION", "allocate",
           "correlation", "evidence_rank", "heldout_sessions",
           "_frozen_cluster"]
