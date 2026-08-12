"""Policy-neutral acceptance checks for deterministic research results.

The checks operate on already normalized, vehicle-local rows.  They do not
know how a signal was generated and never combine equity and option returns.

Every statistic persisted by :func:`verified_gate_envelope` is recomputable
from the evidence the envelope itself carries: matched deltas, their cluster
labels, the draw counts, and the seeds.  Re-verification therefore repeats the
analysis instead of re-hashing a recorded conclusion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from statistics import mean
import math
from typing import Any, Iterable, Mapping, Sequence

from .stats import (
    DEFAULT_BOOTSTRAP_DRAWS, DEFAULT_NULL_DRAWS, cluster_bootstrap_lower_bound,
    paired_cluster_sign_flip, sign_flip_null_statistics, stable_seed,
)


GATE_ENVELOPE_SCHEMA = "verified-research-gate.v2"
CLUSTER_SECONDS = 86_400
LOWER_BOUND_CONFIDENCE = .95
# Qualification observations are source evidence carried outside the run's
# fit/held-out rows.  Keep both storage and verification bounded so a malformed
# recorder row cannot inflate a proof envelope without limit.
QUALIFICATION_MAX_ROWS = 10_000
QUALIFICATION_MAX_BYTES = 2_000_000


class SealedWindowError(RuntimeError):
    """Raised when sealed final-qualification data is used more than once."""


@dataclass(frozen=True)
class AcceptanceFloor:
    min_trades: int = 100
    min_sessions: int = 10
    min_net_pnl: float = 0.0
    max_drawdown: float | None = None
    min_clusters: int = 0

    def check(self, trades: Iterable[Mapping], *, vehicle: str) -> dict:
        rows = [row for row in trades if row.get("vehicle", vehicle) == vehicle]
        # Discovery materializes zero-outcome opportunities to avoid
        # survivorship bias.  They remain part of the session/control sample,
        # but are not executed trades and must not satisfy a trade floor.
        executed = [row for row in rows if row.get("no_trade") is not True]
        sessions = {row.get("session_date") for row in rows
                    if row.get("session_date") is not None}
        net = sum(float(row.get("net_pnl", 0.0)) for row in rows)
        drawdown = max_drawdown_of(rows)
        clusters = len({row.get("cluster", row.get("session_date")) for row in rows
                        if row.get("cluster", row.get("session_date")) is not None})
        checks = {
            "trades": len(executed) >= self.min_trades,
            "sessions": len(sessions) >= self.min_sessions,
            "net_pnl": net >= self.min_net_pnl,
            "clusters": clusters >= self.min_clusters,
        }
        if self.max_drawdown is not None:
            checks["max_drawdown"] = drawdown <= float(self.max_drawdown)
        structural_checks = {
            "trades": checks["trades"],
            "sessions": checks["sessions"],
            "clusters": checks["clusters"],
        }
        performance_checks = {key: value for key, value in checks.items()
                              if key not in structural_checks}
        return {
            "vehicle": vehicle, "trades": len(executed),
            "sessions": len(sessions), "net_pnl": net,
            "max_drawdown": drawdown, "clusters": clusters,
            "structural_passes": all(structural_checks.values()),
            "performance_passes": all(performance_checks.values()),
            "passes": all(checks.values()), "checks": checks,
            "structural_checks": structural_checks,
            "performance_checks": performance_checks,
        }


def chronological_split(rows: Sequence[Mapping], *, fit_fraction: float = .6,
                        require_order: bool = False) -> tuple[list, list]:
    """Split whole trading sessions across one chronological boundary."""
    if not 0 < fit_fraction < 1:
        raise ValueError("fit_fraction must be between zero and one")
    original = list(rows)
    key = lambda row: (_session_key(row), str(row.get("entry_timestamp", "")),
                       str(row.get("opportunity_id", "")))
    if require_order and any(key(left) > key(right)
                             for left, right in zip(original, original[1:])):
        raise ValueError("rows must already be chronological")
    ordered = sorted(original, key=key)
    sessions = sorted({_session_key(row) for row in ordered})
    if len(sessions) < 2:
        return ordered, []
    cut = max(1, min(len(sessions) - 1, int(len(sessions) * fit_fraction)))
    fit_sessions = set(sessions[:cut])
    return ([row for row in ordered if _session_key(row) in fit_sessions],
            [row for row in ordered if _session_key(row) not in fit_sessions])


def _session_key(row: Mapping) -> str:
    return str(row.get("session_date") or row.get("entry_timestamp") or
               row.get("opportunity_id") or "")


def structural_floor(rows: Iterable[Mapping], *, vehicle: str,
                     min_trades: int, min_sessions: int,
                     min_clusters: int = 0, required: bool = True) -> dict:
    """Report structural adequacy without treating profitability as sample size.

    Profitability is deliberately absent here.  ``AcceptanceFloor.min_net_pnl``
    is exercised by :func:`performance_floor`, which is a separate, mandatory
    gate check rather than a sample-size statement.
    """
    report = AcceptanceFloor(
        min_trades=min_trades, min_sessions=min_sessions,
        min_clusters=min_clusters,
    ).check(rows, vehicle=vehicle)
    report["minimums"] = {"trades": int(min_trades),
                          "sessions": int(min_sessions),
                          "clusters": int(min_clusters)}
    report["required"] = bool(required)
    report["adequate"] = bool(report["structural_passes"] if required else True)
    return report


def performance_floor(rows: Iterable[Mapping], *, vehicle: str,
                      min_net_pnl: float = 0.0,
                      min_expectancy: float = 0.0) -> dict:
    """Require absolute, after-cost profitability rather than only a delta.

    A variant that loses money on unseen data has no edge to validate, however
    favourably it compares with the parent specification it was mutated from.
    """
    report = AcceptanceFloor(min_trades=0, min_sessions=0,
                             min_net_pnl=float(min_net_pnl)).check(rows, vehicle=vehicle)
    trades = int(report["trades"])
    net = float(report["net_pnl"])
    expectancy = net / trades if trades else None
    return {
        "vehicle": vehicle, "net_pnl": net, "trades": trades,
        "expectancy": expectancy,
        "min_net_pnl": float(min_net_pnl),
        "min_expectancy": float(min_expectancy),
        "net_pnl_positive": bool(trades > 0 and net > float(min_net_pnl)),
        "expectancy_positive": bool(expectancy is not None and
                                    expectancy > float(min_expectancy)),
    }


def paired_delta(candidate: Iterable[Mapping], baseline: Iterable[Mapping], *, vehicle: str) -> dict:
    """Compare matched vehicle-local rows without pooling unmatched outcomes."""
    left = [row for row in candidate if row.get("vehicle", vehicle) == vehicle]
    right = [row for row in baseline if row.get("vehicle", vehicle) == vehicle]
    def unique(rows: Iterable[Mapping]) -> dict:
        by_key: dict = {}
        duplicates: set = set()
        for row in rows:
            key = row.get("opportunity_id", row.get("entry_timestamp"))
            if key in by_key:
                duplicates.add(key)
            else:
                by_key[key] = row
        for key in duplicates:
            by_key.pop(key, None)
        return by_key

    left_by_key = unique(left)
    right_by_key = unique(right)
    deltas = []
    for key, row in left_by_key.items():
        other = right_by_key.get(key)
        if other is not None:
            deltas.append(float(row.get("net_pnl", 0.0)) - float(other.get("net_pnl", 0.0)))
    return {"vehicle": vehicle, "matched": len(deltas),
            "mean_delta": mean(deltas) if deltas else None,
            "deltas": deltas}


def _unique_by_match_key(rows: Iterable[Mapping], vehicle: str) -> dict[str, Mapping]:
    """Index vehicle-local rows by comparison key, dropping ambiguous keys."""
    values: dict[str, Mapping] = {}
    duplicates: set[str] = set()
    for row in rows:
        if row.get("vehicle", vehicle) != vehicle:
            continue
        key = _match_key(row, vehicle)
        if not key or key in values:
            duplicates.add(key)
        else:
            values[key] = row
    for key in duplicates:
        values.pop(key, None)
    return values


def matched_pairs(candidate: Iterable[Mapping], baseline: Iterable[Mapping], *,
                  vehicle: str) -> dict:
    """Return the matched candidate-minus-baseline deltas and their clusters."""
    left = _unique_by_match_key(candidate, vehicle)
    right = _unique_by_match_key(baseline, vehicle)
    keys: list[str] = []
    deltas: list[float] = []
    clusters: list[int] = []
    stamps: list[float] = []
    for index, key in enumerate(sorted(left)):
        other = right.get(key)
        if other is None:
            continue
        row = left[key]
        stamp = row.get("entry_timestamp") or row.get("session_date")
        try:
            timestamp = datetime.fromisoformat(
                str(stamp).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            timestamp = float(index * CLUSTER_SECONDS)
        keys.append(key)
        stamps.append(timestamp)
        clusters.append(int(timestamp // CLUSTER_SECONDS))
        deltas.append(float(row.get("net_pnl", 0.0)) -
                      float(other.get("net_pnl", 0.0)))
    return {"vehicle": vehicle, "keys": keys, "deltas": deltas,
            "clusters": clusters, "timestamps": stamps,
            "matched": len(deltas)}


def matched_cluster_test(candidate: Iterable[Mapping], baseline: Iterable[Mapping], *,
                         vehicle: str, seed: int = 20260728,
                         confidence: float = LOWER_BOUND_CONFIDENCE) -> dict:
    """Test matched opportunity deltas with deterministic session clustering."""
    pairs = matched_pairs(candidate, baseline, vehicle=vehicle)
    triples = [(stamp, delta, 0.0) for stamp, delta
               in zip(pairs["timestamps"], pairs["deltas"])]
    result = paired_cluster_sign_flip(triples, cluster_seconds=CLUSTER_SECONDS,
                                       seed=seed)
    bound = cluster_bootstrap_lower_bound(
        pairs["deltas"], pairs["clusters"], confidence=confidence)
    result["matched"] = pairs["matched"]
    result["matched_ids_hash"] = _content_hash(pairs["keys"])
    result["deltas"] = list(pairs["deltas"])
    result["delta_clusters"] = list(pairs["clusters"])
    result["mean_delta"] = (sum(pairs["deltas"]) / pairs["matched"]
                            if pairs["matched"] else None)
    result["mean_delta_lcb"] = bound["lower_bound"]
    result["lower_bound"] = {key: bound[key] for key in
                             ("method", "available", "confidence", "draws", "seed")}
    result["actual_control"] = True
    result["available"] = bool(pairs["matched"])
    return result


def placebo_null_distribution(candidate: Iterable[Mapping], baseline: Iterable[Mapping], *,
                              vehicle: str, draws: int = DEFAULT_NULL_DRAWS) -> dict:
    """Draw a seeded cluster sign-flip null distribution for matched deltas.

    ``placebo`` is the null *distribution* of the mean delta, not a single
    reflection of the observations.  The seed is derived from the matched
    content itself, so the same evidence always reproduces the same draws.
    """
    pairs = matched_pairs(candidate, baseline, vehicle=vehicle)
    null = sign_flip_null_statistics(pairs["deltas"], pairs["clusters"],
                                     draws=draws)
    return {"method": "seeded_cluster_sign_flip_null",
            "available": bool(null["available"]) and len(pairs["deltas"]) >= 2,
            "observed": list(pairs["deltas"]),
            "clusters": list(pairs["clusters"]),
            "placebo": list(null["statistics"]),
            "draws": int(null["draws"]), "seed": int(null["seed"]),
            "cluster_count": int(null["clusters"]),
            "p_value": float(null["p_value"]),
            "assignments_hash": _content_hash({"keys": pairs["keys"],
                                               "clusters": pairs["clusters"],
                                               "draws": int(null["draws"]),
                                               "seed": int(null["seed"])})}


# Historical name retained for the discovery facades; the semantics are the
# seeded null distribution above, not a single deterministic sign pattern.
deterministic_placebo_deltas = placebo_null_distribution


def _match_key(row: Mapping, vehicle: str) -> str:
    explicit = row.get("comparison_id")
    if explicit:
        return str(explicit)
    symbol = row.get("symbol")
    session = row.get("session_date")
    if symbol and session:
        return f"{row.get('vehicle', vehicle)}:{symbol}:{session}"
    return str(row.get("opportunity_id") or row.get("entry_timestamp") or "")


def placebo_ratio(observed: Sequence[float], placebo: Sequence[float]) -> float | None:
    """Return the observed mean over the mean absolute null draw."""
    if not placebo:
        return None
    baseline = mean(abs(float(value)) for value in placebo)
    return mean(float(value) for value in observed) / baseline if baseline else None


def max_drawdown_of(rows_or_values: Iterable[Mapping] | Iterable[float]) -> float:
    """Maximum peak-to-trough loss, reported as a non-negative P&L amount."""
    values = []
    for row in rows_or_values:
        value = row.get("net_pnl", row.get("return_value", 0.0)) if isinstance(row, Mapping) else row
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            values.append(number)
    peak = equity = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return float(drawdown)


def heldout_separation(fit: Sequence[Mapping], heldout: Sequence[Mapping]) -> dict:
    """Require one chronological boundary with no shared session."""
    fit_sessions = {str(row.get("session_date")) for row in fit if row.get("session_date") is not None}
    held_sessions = {str(row.get("session_date")) for row in heldout if row.get("session_date") is not None}
    fit_keys = [(str(row.get("session_date", "")), str(row.get("entry_timestamp", ""))) for row in fit]
    held_keys = [(str(row.get("session_date", "")), str(row.get("entry_timestamp", ""))) for row in heldout]
    boundary_ok = bool(fit_keys and held_keys and max(fit_keys) < min(held_keys))
    return {"fit": len(fit), "heldout": len(heldout),
            "overlap_sessions": sorted(fit_sessions & held_sessions),
            "passes": boundary_ok and not (fit_sessions & held_sessions)}


def falsification_gate(observed: Sequence[float], placebo: Sequence[float], *,
                       alpha: float = .05, minimum_ratio: float = 1.0) -> dict:
    """Place the observed mean delta inside a genuine null distribution.

    ``placebo`` is a sample of null mean deltas.  The decision is an empirical
    one-sided p-value against that distribution; the ratio is reported for
    scale but cannot on its own authorize a pass.
    """
    draws = [float(value) for value in placebo]
    observed_values = [float(value) for value in observed]
    observed_mean = mean(observed_values) if observed_values else 0.0
    placebo_mean = mean(draws) if draws else 0.0
    ratio = placebo_ratio(observed_values, draws)
    zero_placebo = bool(draws and all(abs(value) <= 1e-15 for value in draws))
    degenerate = bool(draws and max(draws) - min(draws) <= 1e-15)
    tolerance = 1e-15 * max(1.0, abs(observed_mean))
    extreme = sum(1 for value in draws if value >= observed_mean - tolerance)
    p_value = (extreme + 1) / (len(draws) + 1) if draws else 1.0
    return {"observed_mean": observed_mean, "placebo_mean": placebo_mean,
            "ratio": ratio, "available": bool(draws),
            "draws": len(draws), "p_value": p_value, "alpha": float(alpha),
            "zero_placebo": zero_placebo,
            "distinct": not degenerate,
            "passes": bool(draws) and not zero_placebo and not degenerate and
            observed_mean > 0 and p_value <= float(alpha) and
            ratio is not None and ratio >= minimum_ratio}


def sample_counts(rows: Iterable[Mapping], *, vehicle: str) -> dict:
    selected = [row for row in rows if row.get("vehicle", vehicle) == vehicle]
    return {
        "rows": len(selected),
        "trades": len([row for row in selected if row.get("no_trade") is not True]),
        "sessions": len({_session_key(row) for row in selected if _session_key(row)}),
        "clusters": len({str(row.get("cluster") or _session_key(row)) for row in selected
                         if row.get("cluster") or _session_key(row)}),
    }


def walk_forward_report(candidate: Sequence[Mapping], baseline: Sequence[Mapping], *,
                        vehicle: str, folds: int = 3) -> dict:
    """Roll the origin forward and require a majority of positive test folds.

    Each fold trains on every session before its test block, so no fold ever
    scores a period that precedes its own fit window.  A single chronological
    cut can be survived by one lucky regime; a majority requirement cannot.
    """
    if int(folds) < 2:
        raise ValueError("walk-forward requires at least two folds")
    ordered = sorted(candidate, key=lambda row: (_session_key(row),
                                                 str(row.get("entry_timestamp", ""))))
    sessions = sorted({_session_key(row) for row in ordered if _session_key(row)})
    count = int(folds)
    # One fit block plus ``folds`` test blocks must each hold a whole session.
    if len(sessions) < count + 1:
        return {"available": False, "folds": count, "tested_folds": 0,
                "positive_folds": 0, "majority_positive": False,
                "sessions": len(sessions), "results": []}
    start = len(sessions) - count
    results = []
    for index in range(count):
        test_sessions = {sessions[start + index]}
        fit_sessions = set(sessions[:start + index])
        test_rows = [row for row in ordered if _session_key(row) in test_sessions]
        base_rows = [row for row in baseline if _session_key(row) in test_sessions]
        pairs = matched_pairs(test_rows, base_rows, vehicle=vehicle)
        delta = (sum(pairs["deltas"]) / pairs["matched"]) if pairs["matched"] else None
        net = sum(float(row.get("net_pnl", 0.0)) for row in test_rows)
        results.append({"fold": index, "fit_sessions": len(fit_sessions),
                        "test_sessions": sorted(test_sessions),
                        "matched": pairs["matched"], "mean_delta": delta,
                        "net_pnl": net,
                        "positive": bool(delta is not None and delta > 0 and net > 0)})
    positive = sum(1 for item in results if item["positive"])
    return {"available": True, "folds": count, "tested_folds": len(results),
            "positive_folds": positive,
            "majority_positive": bool(positive * 2 > len(results)),
            "sessions": len(sessions), "results": results}


def qualification_report(rows: Sequence[Mapping], baseline: Sequence[Mapping], *,
                         vehicle: str, sessions: Sequence[str]) -> dict:
    """Score the sealed final window: go/no-go only, never diagnosis."""
    if not rows or not sessions:
        return {"available": False, "sessions": list(sessions), "net_pnl": 0.0,
                "matched": 0, "mean_delta": None, "trades": 0,
                "net_positive": False, "delta_positive": False}
    declared = tuple(str(item) for item in sessions)
    declared_set = set(declared)
    if (len(declared_set) != len(declared) or
            any(not item for item in declared_set)):
        raise ValueError("qualification sessions must be unique, non-empty strings")
    pairs = matched_pairs(rows, baseline, vehicle=vehicle)
    absolute = performance_floor(rows, vehicle=vehicle)
    delta = (sum(pairs["deltas"]) / pairs["matched"]) if pairs["matched"] else None
    # The final window is intentionally outside the run's fit/held-out trades.
    # Carry the source observations and their independent digests in the
    # signed envelope so a proof can be re-verified after it leaves memory.
    # This also makes tampering detectable even when the summary numbers are
    # changed consistently with one another.
    candidate_observations = [dict(row) for row in rows]
    baseline_observations = [dict(row) for row in baseline]
    if len(candidate_observations) + len(baseline_observations) > QUALIFICATION_MAX_ROWS:
        raise ValueError("qualification observations exceed row limit")
    candidate_sessions = {
        str(row.get("session_date") or "") for row in candidate_observations}
    baseline_sessions = {
        str(row.get("session_date") or "") for row in baseline_observations}
    if (candidate_sessions != declared_set or baseline_sessions != declared_set or
            "" in candidate_sessions or "" in baseline_sessions):
        raise ValueError(
            "qualification observation sessions do not match declared sessions")
    serialized = json.dumps(
        {"candidate": candidate_observations, "baseline": baseline_observations},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False, default=str).encode("utf-8")
    if len(serialized) > QUALIFICATION_MAX_BYTES:
        raise ValueError("qualification observations exceed serialized byte limit")
    candidate_digest = _content_hash(candidate_observations)
    baseline_digest = _content_hash(baseline_observations)
    return {"available": True, "sessions": list(declared),
            "net_pnl": absolute["net_pnl"], "trades": absolute["trades"],
            "matched": pairs["matched"], "mean_delta": delta,
            "net_positive": bool(absolute["net_pnl_positive"]),
            "delta_positive": bool(delta is not None and delta > 0),
            "candidate_observations": candidate_observations,
            "baseline_observations": baseline_observations,
            "candidate_observation_digest": candidate_digest,
            "baseline_observation_digest": baseline_digest,
            # Short aliases make the binding explicit to downstream proof
            # consumers while retaining the descriptive field names above.
            "candidate_digest": candidate_digest,
            "baseline_digest": baseline_digest,
            "observation_schema": "qualification-observations.v1"}


@dataclass
class SealedQualificationWindow:
    """Final-qualification data that may be released exactly once.

    Selection, mutation and diagnosis are given the development payload only.
    This object owns the remaining sessions and refuses to be copied,
    serialized, or shipped to a worker process, so the qualification window
    cannot be consumed by accident even if a caller forgets the convention.
    """

    session_dates: tuple[str, ...]
    digest: str
    reason: str = ""
    released: bool = False
    _payload: Any = field(default=None, repr=False)

    def release(self, *, reason: str) -> Any:
        if self.released:
            raise SealedWindowError(
                "the final qualification window has already been consumed")
        if not str(reason).strip():
            raise SealedWindowError("releasing a sealed window requires a reason")
        self.released = True
        self.reason = str(reason)
        payload, self._payload = self._payload, None
        return payload

    def __getstate__(self):
        raise SealedWindowError(
            "a sealed qualification window cannot be serialized or sent to a worker")

    def __deepcopy__(self, memo):
        raise SealedWindowError("a sealed qualification window cannot be copied")

    def __repr__(self) -> str:
        return (f"SealedQualificationWindow(sessions={len(self.session_dates)}, "
                f"digest={self.digest[:12]!r}, released={self.released})")


def seal_final_window(items: Sequence[Any], *, session_of, fraction: float = .2,
                      min_sessions: int = 1) -> tuple[list, SealedQualificationWindow]:
    """Split off the latest sessions into a sealed final-qualification window."""
    if not 0 < float(fraction) < 1:
        raise ValueError("fraction must be between zero and one")
    ordered = list(items)
    sessions = sorted({str(session_of(item)) for item in ordered})
    reserved = max(int(min_sessions), int(len(sessions) * float(fraction)))
    if len(sessions) <= reserved:
        # Not enough history to seal anything without emptying development.
        return ordered, SealedQualificationWindow((), _content_hash([]))
    sealed_sessions = set(sessions[len(sessions) - reserved:])
    development = [item for item in ordered
                   if str(session_of(item)) not in sealed_sessions]
    qualification = [item for item in ordered
                     if str(session_of(item)) in sealed_sessions]
    return development, SealedQualificationWindow(
        tuple(sorted(sealed_sessions)),
        _content_hash(sorted(sealed_sessions)),
        _payload=qualification)


def verified_gate_envelope(*, lane: str, vehicle: str,
                           fit: Sequence[Mapping], heldout: Sequence[Mapping],
                           fit_floor: Mapping, heldout_floor: Mapping,
                           control: Mapping, p_value: float, q_value: float,
                           alpha: float,
                           falsification: Mapping, separation: Mapping,
                           checks: Mapping[str, bool], passes: bool,
                           performance: Mapping | None = None,
                           family_q_value: float | None = None,
                           walk_forward: Mapping | None = None,
                           qualification: Mapping | None = None,
                           null_control: Mapping | None = None) -> dict:
    """Build the immutable, content-addressed gate decision persisted per run.

    ``q_value`` is the cycle-global false-discovery q; ``family_q_value`` is
    the family-local one.  Both are persisted so a proof states exactly which
    correction authorized it.
    """
    reported = dict(performance or {})
    reported.setdefault("heldout_delta", control.get("mean_delta"))
    reported.setdefault("heldout_delta_lcb", control.get("mean_delta_lcb"))
    reported.setdefault("max_drawdown", 0.0)
    body: dict[str, Any] = {
        "schema": GATE_ENVELOPE_SCHEMA,
        "lane": str(lane),
        "vehicle": str(vehicle),
        "counts": {
            "fit": sample_counts(fit, vehicle=vehicle),
            "heldout": sample_counts(heldout, vehicle=vehicle),
            "total": sample_counts([*fit, *heldout], vehicle=vehicle),
        },
        "floors": {"fit": dict(fit_floor), "heldout": dict(heldout_floor)},
        "control": dict(control),
        "statistics": {"p_value": float(p_value), "q_value": float(q_value),
                       "family_q_value": float(q_value if family_q_value is None
                                                else family_q_value),
                       "alpha": float(alpha)},
        "performance": reported,
        "falsification": dict(falsification),
        "separation": dict(separation),
        "walk_forward": dict(walk_forward or {}),
        "qualification": dict(qualification or {}),
        "null_control": dict(null_control or {}),
        "checks": {str(key): bool(value) for key, value in checks.items()},
        "passes": bool(passes),
    }
    return {**body, "content_hash": _content_hash(body)}


def verify_gate_envelope(envelope: Mapping) -> bool:
    try:
        qualification = envelope.get("qualification")
        if not isinstance(qualification, Mapping):
            return False
        if qualification.get("available"):
            candidate = qualification.get("candidate_observations")
            baseline = qualification.get("baseline_observations")
            if (not isinstance(candidate, Sequence) or isinstance(candidate, (str, bytes)) or
                    not isinstance(baseline, Sequence) or isinstance(baseline, (str, bytes)) or
                    not all(isinstance(row, Mapping) for row in candidate) or
                    not all(isinstance(row, Mapping) for row in baseline) or
                    qualification.get("observation_schema") != "qualification-observations.v1" or
                    qualification.get("candidate_observation_digest") != _content_hash(candidate) or
                    qualification.get("baseline_observation_digest") != _content_hash(baseline) or
                    qualification.get("candidate_digest") != qualification.get(
                        "candidate_observation_digest") or
                    qualification.get("baseline_digest") != qualification.get(
                        "baseline_observation_digest")):
                return False
            if (len(candidate) + len(baseline) > QUALIFICATION_MAX_ROWS or
                    len(json.dumps(
                        {"candidate": candidate, "baseline": baseline},
                        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                        allow_nan=False, default=str).encode("utf-8")) >
                    QUALIFICATION_MAX_BYTES):
                return False
            expected = qualification_report(
                candidate, baseline, vehicle=str(envelope.get("vehicle")),
                sessions=qualification.get("sessions") or ())
            for key in ("sessions", "net_pnl", "trades", "matched", "mean_delta",
                        "net_positive", "delta_positive"):
                if expected.get(key) != qualification.get(key):
                    return False
        body = {key: value for key, value in envelope.items() if key != "content_hash"}
        statistics = envelope.get("statistics")
        checks = envelope.get("checks")
        if isinstance(statistics, Mapping) and isinstance(checks, Mapping):
            alpha = float(statistics.get("alpha"))
            global_q = float(statistics.get("q_value"))
            family_q = float(statistics.get("family_q_value"))
            if not (math.isfinite(alpha) and 0.0 < alpha <= 1.0 and
                    math.isfinite(global_q) and 0.0 <= global_q <= 1.0 and
                    math.isfinite(family_q) and 0.0 <= family_q <= 1.0):
                return False
            if ("family_fdr_significant" in checks and
                    bool(checks["family_fdr_significant"]) != (family_q <= alpha)):
                return False
            if ("global_fdr_significant" in checks and
                    bool(checks["global_fdr_significant"]) != (global_q <= alpha)):
                return False
        return bool(
            envelope.get("schema") == GATE_ENVELOPE_SCHEMA and
            envelope.get("lane") in {"backtest", "shadow"} and
            envelope.get("vehicle") in {"equity", "option"} and
            isinstance(envelope.get("passes"), bool) and
            isinstance(envelope.get("counts"), Mapping) and
            isinstance(envelope.get("floors"), Mapping) and
            isinstance(envelope.get("control"), Mapping) and
            isinstance(envelope.get("statistics"), Mapping) and
            isinstance(envelope.get("walk_forward"), Mapping) and
            isinstance(envelope.get("qualification"), Mapping) and
            isinstance(envelope.get("null_control"), Mapping) and
            envelope.get("content_hash") == _content_hash(body))
    except (TypeError, ValueError, OverflowError):
        return False


def recompute_gate_statistics(envelope: Mapping) -> dict:
    """Recompute the envelope's statistics from its own source observations.

    The persisted matched deltas, cluster labels, draw counts and seeds are
    sufficient to reproduce the control p-value, the lower confidence bound
    and the falsification decision.  Re-verification compares these against
    the recorded conclusions rather than trusting them.
    """
    control = envelope.get("control")
    if not isinstance(control, Mapping):
        return {"available": False, "reason": "control evidence is missing"}
    deltas = control.get("deltas")
    clusters = control.get("delta_clusters")
    if not isinstance(deltas, Sequence) or not isinstance(clusters, Sequence) or \
            isinstance(deltas, (str, bytes)) or isinstance(clusters, (str, bytes)):
        return {"available": False, "reason": "matched delta evidence is missing"}
    if len(deltas) != len(clusters):
        return {"available": False, "reason": "matched delta evidence is inconsistent"}
    values: list[float] = []
    for item in deltas:
        if isinstance(item, bool):
            return {"available": False, "reason": "matched delta evidence is invalid"}
        try:
            value = float(item)
        except (TypeError, ValueError):
            return {"available": False, "reason": "matched delta evidence is invalid"}
        if not math.isfinite(value):
            return {"available": False, "reason": "matched delta evidence is invalid"}
        values.append(value)
    triples = [(float(cluster) * CLUSTER_SECONDS, value, 0.0)
               for cluster, value in zip(clusters, values)]
    sign_flip = paired_cluster_sign_flip(triples, cluster_seconds=CLUSTER_SECONDS)
    falsification = envelope.get("falsification")
    draws = None
    seed = None
    if isinstance(falsification, Mapping):
        draws = falsification.get("draws")
        seed = falsification.get("seed")
    null = sign_flip_null_statistics(
        values, [str(cluster) for cluster in clusters],
        draws=int(draws) if isinstance(draws, int) and draws > 0 else DEFAULT_NULL_DRAWS,
        seed=int(seed) if isinstance(seed, int) and not isinstance(seed, bool) else None)
    lower = control.get("lower_bound")
    confidence = LOWER_BOUND_CONFIDENCE
    bootstrap_draws = DEFAULT_BOOTSTRAP_DRAWS
    bootstrap_seed = None
    if isinstance(lower, Mapping):
        if isinstance(lower.get("confidence"), (int, float)) and \
                not isinstance(lower.get("confidence"), bool):
            confidence = float(lower["confidence"])
        if isinstance(lower.get("draws"), int) and not isinstance(lower.get("draws"), bool) \
                and lower["draws"] > 0:
            bootstrap_draws = int(lower["draws"])
        if isinstance(lower.get("seed"), int) and not isinstance(lower.get("seed"), bool):
            bootstrap_seed = int(lower["seed"])
    bound = cluster_bootstrap_lower_bound(
        values, [str(cluster) for cluster in clusters], confidence=confidence,
        draws=bootstrap_draws, seed=bootstrap_seed)
    decision = falsification_gate(
        values, null["statistics"],
        alpha=float(falsification.get("alpha", .05))
        if isinstance(falsification, Mapping) and
        isinstance(falsification.get("alpha"), (int, float)) and
        not isinstance(falsification.get("alpha"), bool) else .05)
    return {
        "available": True,
        "matched": len(values),
        "mean_delta": (sum(values) / len(values)) if values else None,
        "p_value": float(sign_flip["p_value"]),
        "mean_delta_lcb": bound["lower_bound"],
        "falsification_p_value": float(decision["p_value"]),
        "falsification_passes": bool(decision["passes"]),
    }


def _content_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = ["AcceptanceFloor", "CLUSTER_SECONDS", "GATE_ENVELOPE_SCHEMA",
    "LOWER_BOUND_CONFIDENCE", "SealedQualificationWindow",
           "QUALIFICATION_MAX_BYTES", "QUALIFICATION_MAX_ROWS",
           "SealedWindowError", "chronological_split",
           "deterministic_placebo_deltas", "falsification_gate",
           "heldout_separation", "matched_cluster_test", "matched_pairs",
           "max_drawdown_of", "paired_delta", "performance_floor",
           "placebo_null_distribution", "placebo_ratio", "qualification_report",
           "recompute_gate_statistics", "sample_counts", "seal_final_window",
           "structural_floor", "verified_gate_envelope", "verify_gate_envelope",
           "walk_forward_report"]
