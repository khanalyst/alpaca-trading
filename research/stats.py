"""Deterministic, cluster-aware tests used by the edge discovery gates.

Every statistic here is reproducible from its inputs alone: draw counts and
seeds are explicit arguments and are reported back so a persisted proof can be
recomputed rather than merely re-hashed.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from statistics import NormalDist
from typing import Mapping


PAIRED_SIGN_FLIP_NULL_ASSUMPTION = (
    "cluster_delta_sign_exchangeability_under_symmetric_null")
DEFAULT_NULL_DRAWS = 10_000
DEFAULT_BOOTSTRAP_DRAWS = 4_000
DEFAULT_BREADTH_MIN_CLUSTERS = 2
DEFAULT_BREADTH_MIN_SESSIONS = 3
DEFAULT_CLUSTER_BLOCK_LENGTH = 5

# Dependence policies are deliberately stricter than the existing diagnostic
# cross-family report below.  They are built only from completed prior cycles,
# use paired candidate-minus-baseline session deltas, and are unavailable
# unless both breadth and temporal replication are present.
DEPENDENCE_POLICY_SCHEMA = "dependence-policy.v1"
DEPENDENCE_MIN_COMPLETE_SESSIONS = 20
DEPENDENCE_MIN_PRIOR_CYCLES = 2
DEPENDENCE_CORRELATION_THRESHOLD = .8
# Short names retained for callers that describe the floor as shared-session
# evidence rather than complete cells.
DEPENDENCE_MIN_SESSIONS = DEPENDENCE_MIN_COMPLETE_SESSIONS
DEPENDENCE_MIN_CYCLES = DEPENDENCE_MIN_PRIOR_CYCLES
DEPENDENCE_ABS_CORRELATION = DEPENDENCE_CORRELATION_THRESHOLD


def stable_seed(value) -> int:
    """Derive a reproducible 63-bit seed from arbitrary JSON-able content."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False, default=str)
    digest = hashlib.sha256(encoded.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


def deterministic_dependence_map(rows, *, cutoff: float | None = None,
                                 min_sessions: int = DEPENDENCE_MIN_COMPLETE_SESSIONS,
                                 min_cycles: int = DEPENDENCE_MIN_PRIOR_CYCLES,
                                 threshold: float = DEPENDENCE_CORRELATION_THRESHOLD) -> dict:
    """Build a frozen family dependence map from prior paired observations.

    ``rows`` must contain ``cycle_id``, ``family``, ``session`` and ``delta``.
    Repeated observations in one cycle/family/session are averaged so a
    variant sweep cannot overweight a family.  Correlations are then computed
    on the sorted union of complete cycle/session cells.  Strong edges are
    traversed in lexical order by a deterministic union-find, making cluster
    identifiers reproducible and independent of input order.

    This helper is policy-only: it does not alter BH or any gate.  An
    unavailable map is returned with its evidence and explicit reason so the
    caller can persist that conservative decision before a new cycle starts.
    """
    try:
        minimum_sessions = max(1, int(min_sessions))
        minimum_cycles = max(1, int(min_cycles))
        correlation_threshold = float(threshold)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("dependence policy thresholds must be numeric") from exc
    if not math.isfinite(correlation_threshold) or not 0.0 <= correlation_threshold <= 1.0:
        raise ValueError("dependence correlation threshold must be between zero and one")

    cells: dict[tuple[str, str, str], list[float]] = {}
    invalid = 0
    source_cycles: set[str] = set()
    for row in rows or ():
        if not isinstance(row, Mapping):
            invalid += 1
            continue
        cycle = str(row.get("cycle_id") or row.get("cycle") or "").strip()
        family = str(row.get("family") or "").strip()
        session = str(row.get("session") or row.get("session_date") or "").strip()
        try:
            value = float(row.get("delta"))
        except (TypeError, ValueError, OverflowError):
            invalid += 1
            continue
        if not cycle or not family or not session or not math.isfinite(value):
            invalid += 1
            continue
        cells.setdefault((cycle, family, session), []).append(value)
        source_cycles.add(cycle)
    reduced = {key: sum(values) / len(values)
               for key, values in cells.items() if values}
    cycles = sorted({key[0] for key in reduced})
    families = sorted({key[1] for key in reduced})
    parent = {family: family for family in families}

    def find(value: str) -> str:
        root = value
        while parent[root] != root:
            root = parent[root]
        while parent[value] != value:
            next_value = parent[value]
            parent[value] = root
            value = next_value
        return root

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return
        if left_root < right_root:
            parent[right_root] = left_root
        else:
            parent[left_root] = right_root

    pairwise: list[dict] = []
    strong_pairs: list[list[str]] = []
    for index, left in enumerate(families):
        for right in families[index + 1:]:
            shared_keys = sorted({(cycle, session) for cycle, family, session in reduced
                                  if family == left} &
                                 {(cycle, session) for cycle, family, session in reduced
                                  if family == right})
            left_values = [reduced[(cycle, left, session)] for cycle, session in shared_keys]
            right_values = [reduced[(cycle, right, session)] for cycle, session in shared_keys]
            pair_cycles = sorted({cycle for cycle, _session in shared_keys})
            correlation = None
            if len(shared_keys) >= minimum_sessions and len(pair_cycles) >= minimum_cycles:
                left_mean = sum(left_values) / len(left_values)
                right_mean = sum(right_values) / len(right_values)
                left_centered = [value - left_mean for value in left_values]
                right_centered = [value - right_mean for value in right_values]
                left_var = sum(value * value for value in left_centered)
                right_var = sum(value * value for value in right_centered)
                if left_var > 1e-18 and right_var > 1e-18:
                    correlation = sum(a * b for a, b in zip(left_centered, right_centered)) / math.sqrt(left_var * right_var)
                    correlation = max(-1.0, min(1.0, correlation))
            absolute = abs(correlation) if correlation is not None else None
            item = {
                "families": [left, right], "left": left, "right": right,
                "complete_sessions": len(shared_keys), "prior_cycles": pair_cycles,
                "correlation": correlation, "absolute_correlation": absolute,
                "eligible": bool(correlation is not None),
                "strong": bool(absolute is not None and absolute >= correlation_threshold),
                "reason": (None if correlation is not None else
                           "insufficient_complete_sessions_or_cycles"),
            }
            pairwise.append(item)
            if item["strong"]:
                strong_pairs.append([left, right])
                union(left, right)

    groups: dict[str, list[str]] = {}
    for family in families:
        groups.setdefault(find(family), []).append(family)
    ordered_groups = sorted((sorted(values) for values in groups.values()),
                            key=lambda values: tuple(values))
    cluster_map = {
        family: f"dependence-{index:04d}"
        for index, members in enumerate(ordered_groups, start=1)
        for family in members
    }
    complete_session_count = len({(cycle, session) for cycle, _family, session in reduced})
    available = bool(len(cycles) >= minimum_cycles and
                     complete_session_count >= minimum_sessions and
                     any(item["strong"] for item in pairwise))
    reason = None if available else (
        "insufficient_prior_cycles" if len(cycles) < minimum_cycles else
        "insufficient_complete_sessions" if complete_session_count < minimum_sessions else
        "no_strong_dependence_pairs")
    body = {
        "schema": DEPENDENCE_POLICY_SCHEMA,
        "version": 1,
        "cutoff": cutoff,
        "available": available,
        "reason": reason,
        "minimum_complete_sessions": minimum_sessions,
        "minimum_prior_cycles": minimum_cycles,
        "correlation_threshold": correlation_threshold,
        "source_cycles": cycles,
        "families": families,
        "complete_session_count": complete_session_count,
        "invalid_observations": invalid,
        "pairwise": pairwise,
        "strong_pairs": sorted(strong_pairs),
        "cluster_map": cluster_map if available else {},
        "cluster_members": {cluster: sorted(family for family, value in cluster_map.items()
                                             if value == cluster)
                            for cluster in sorted(set(cluster_map.values()))}
        if available else {},
    }
    return body


# Readable aliases for policy callers and compatibility with external research
# tooling that uses "dependence policy" terminology.
build_dependence_map = deterministic_dependence_map
dependence_policy_map = deterministic_dependence_map
build_dependence_policy = deterministic_dependence_map


def cluster_contributions(deltas, clusters) -> list[float]:
    """Sum finite paired deltas into their exchangeable cluster totals."""
    grouped: dict[str, float] = {}
    order: list[str] = []
    for delta, cluster in zip(deltas, clusters):
        try:
            value = float(delta)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        key = str(cluster)
        if key not in grouped:
            grouped[key] = 0.0
            order.append(key)
        grouped[key] += value
    return [grouped[key] for key in sorted(order)]


def _sign_flip(contributions: list[float], observed_sum: float, *,
               exact_max_clusters: int, iterations: int, seed: int) -> dict:
    """Return a reproducible one-sided sign-flip p-value."""
    cluster_count = len(contributions)
    tolerance = 1e-15 * max(1.0, abs(observed_sum))
    if cluster_count <= max(0, int(exact_max_clusters)):
        resamples = 1 << cluster_count
        extreme = 0
        for mask in range(resamples):
            randomized = sum(
                value if mask & (1 << index) else -value
                for index, value in enumerate(contributions))
            if randomized >= observed_sum - tolerance:
                extreme += 1
        return {"method": "exact_enumeration", "exact": True,
                "resamples": resamples, "seed": None,
                "p_value": extreme / resamples}

    resamples = max(1, int(iterations))
    rng = random.Random(seed)
    extreme = 0
    for _ in range(resamples):
        randomized = sum(
            value if rng.getrandbits(1) else -value
            for value in contributions)
        if randomized >= observed_sum - tolerance:
            extreme += 1
    return {"method": "monte_carlo", "exact": False,
            "resamples": resamples, "seed": int(seed),
            "p_value": (extreme + 1) / (resamples + 1)}


def sign_flip_null_statistics(deltas, clusters, *, draws: int = DEFAULT_NULL_DRAWS,
                              seed: int | None = None) -> dict:
    """Draw a seeded cluster sign-flip null distribution of the mean delta.

    The returned ``statistics`` are the null draws themselves, so a caller can
    place the observed mean inside a real distribution instead of comparing it
    with a single deterministic reflection of its own observations.
    """
    values = []
    keys = []
    for delta, cluster in zip(deltas, clusters):
        try:
            value = float(delta)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            values.append(value)
            keys.append(str(cluster))
    contributions = cluster_contributions(values, keys)
    count = len(values)
    resamples = max(1, int(draws))
    resolved_seed = int(stable_seed({"deltas": values, "clusters": keys,
                                     "draws": resamples})
                        if seed is None else seed)
    if not values or not contributions:
        return {"method": "cluster_sign_flip_null", "available": False,
                "statistics": [], "observed_mean": 0.0, "clusters": 0,
                "observations": 0, "draws": 0, "seed": resolved_seed,
                "p_value": 1.0, "null_mean_abs": 0.0, "degenerate": True}
    observed_mean = sum(values) / count
    rng = random.Random(resolved_seed)
    statistics = []
    for _ in range(resamples):
        total = 0.0
        for value in contributions:
            total += value if rng.getrandbits(1) else -value
        statistics.append(total / count)
    tolerance = 1e-15 * max(1.0, abs(observed_mean))
    extreme = sum(1 for value in statistics if value >= observed_mean - tolerance)
    null_mean_abs = sum(abs(value) for value in statistics) / resamples
    return {"method": "cluster_sign_flip_null", "available": True,
            "statistics": statistics, "observed_mean": observed_mean,
            "clusters": len(contributions), "observations": count,
            "draws": resamples, "seed": resolved_seed,
            "p_value": (extreme + 1) / (resamples + 1),
            "null_mean_abs": null_mean_abs,
            "degenerate": bool(null_mean_abs <= 1e-15)}


def cluster_bootstrap_lower_bound(deltas, clusters, *, confidence: float = .95,
                                  draws: int = DEFAULT_BOOTSTRAP_DRAWS,
                                  seed: int | None = None,
                                  block_length: int | None = None,
                                  min_clusters: int = 1) -> dict:
    """Bootstrap one-sided bounds on the mean over whole clusters.

    Clusters, not observations, are resampled: intraday deltas inside one
    session are not independent and must move together.  Passing
    ``block_length`` opts into :func:`moving_block_cluster_bootstrap_lower_bound`
    while retaining this historical function name for callers of the research
    gates.
    """
    if block_length is not None:
        return moving_block_cluster_bootstrap_lower_bound(
            deltas, clusters, confidence=confidence, draws=draws, seed=seed,
            block_length=block_length, min_clusters=min_clusters)
    grouped: dict[str, list[float]] = {}
    for delta, cluster in zip(deltas, clusters):
        try:
            value = float(delta)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            grouped.setdefault(str(cluster), []).append(value)
    keys = sorted(grouped)
    total = sum(len(grouped[key]) for key in keys)
    resamples = max(1, int(draws))
    try:
        minimum = max(1, int(min_clusters))
    except (TypeError, ValueError) as exc:
        raise ValueError("min_clusters must be a non-negative integer") from exc
    if not 0 < float(confidence) < 1:
        raise ValueError("confidence must be between zero and one")
    resolved_seed = int(stable_seed({"bootstrap": [grouped[key] for key in keys],
                                     "draws": resamples,
                                     "confidence": float(confidence)})
                        if seed is None else seed)
    if not keys or total == 0:
        return {"method": "cluster_bootstrap", "available": False,
                "lower_bound": None, "upper_bound": None,
                "mean": None, "clusters": 0,
                "observations": 0, "draws": 0, "seed": resolved_seed,
                "confidence": float(confidence)}
    if len(keys) < minimum:
        return {"method": "cluster_bootstrap", "available": False,
                "lower_bound": None, "upper_bound": None,
                "mean": sum(sum(grouped[key]) for key in keys) / total,
                "clusters": len(keys), "observations": total,
                "draws": resamples, "seed": resolved_seed,
                "confidence": float(confidence),
                "minimum_clusters": minimum,
                "reason": ("no_finite_observations" if not keys or total == 0
                           else "insufficient_clusters")}
    rng = random.Random(resolved_seed)
    size = len(keys)
    means = []
    for _ in range(resamples):
        pooled = 0.0
        count = 0
        for _ in range(size):
            block = grouped[keys[rng.randrange(size)]]
            pooled += sum(block)
            count += len(block)
        means.append(pooled / count if count else 0.0)
    means.sort()
    index = int(math.floor((1.0 - float(confidence)) * resamples))
    index = min(max(index, 0), resamples - 1)
    upper_index = int(math.ceil(float(confidence) * resamples)) - 1
    upper_index = min(max(upper_index, 0), resamples - 1)
    return {"method": "cluster_bootstrap", "available": True,
            "lower_bound": means[index],
            "upper_bound": means[upper_index],
            "mean": sum(sum(grouped[key]) for key in keys) / total,
            "clusters": size, "observations": total, "draws": resamples,
            "seed": resolved_seed, "confidence": float(confidence)}


def moving_block_cluster_bootstrap_lower_bound(
        deltas, clusters, *, confidence: float = .95,
        draws: int = DEFAULT_BOOTSTRAP_DRAWS, block_length: int = 1,
        seed: int | None = None,
        min_clusters: int = DEFAULT_BREADTH_MIN_CLUSTERS,
        include_replicates: bool = False) -> dict:
    """Return a seeded lower bound using a moving-block cluster bootstrap.

    ``clusters`` are expected in chronological order.  The first occurrence
    of each cluster establishes that order; observations belonging to the same
    cluster move together.  Circular overlapping blocks preserve short-range
    serial dependence while allowing a fixed-length bootstrap series.  The
    statistic is the observation-weighted mean, matching the historical
    cluster bootstrap contract.

    Fewer than ``min_clusters`` independent clusters are reported as
    unavailable.  This is deliberately conservative: one cluster cannot
    identify a sampling distribution, even when it contains many observations.
    All result metadata (including the resolved seed and requested draw count)
    is returned so persisted evidence can be recomputed exactly.  The optional
    ``include_replicates`` flag exposes the sorted bootstrap means for
    diagnostic power calculations; it is disabled by default to keep durable
    gate payloads compact.
    """
    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence must be between zero and one") from exc
    if not 0 < confidence_value < 1:
        raise ValueError("confidence must be between zero and one")
    try:
        length = int(block_length)
    except (TypeError, ValueError) as exc:
        raise ValueError("block_length must be a positive integer") from exc
    if length <= 0:
        raise ValueError("block_length must be a positive integer")
    try:
        minimum = max(1, int(min_clusters))
    except (TypeError, ValueError) as exc:
        raise ValueError("min_clusters must be a non-negative integer") from exc
    resamples = max(1, int(draws))

    grouped: dict[str, list[float]] = {}
    order: list[str] = []
    for delta, cluster in zip(deltas, clusters):
        try:
            value = float(delta)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        key = str(cluster)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(value)

    observations = sum(len(grouped[key]) for key in order)
    cluster_count = len(order)
    payload = {
        "moving_block_bootstrap": [
            {"cluster": key, "values": grouped[key]} for key in order],
        "confidence": confidence_value, "draws": resamples,
        "block_length": length, "min_clusters": minimum,
    }
    resolved_seed = int(stable_seed(payload) if seed is None else seed)
    mean = (sum(sum(grouped[key]) for key in order) / observations
            if observations else None)
    common = {
        "method": "moving_block_cluster_bootstrap",
        "bootstrap_method": "moving_block",
        "available": False,
        "lower_bound": None, "upper_bound": None, "mean": mean,
        "clusters": cluster_count, "cluster_count": cluster_count,
        "observations": observations, "draws": resamples,
        "block_length": length, "seed": resolved_seed,
        "confidence": confidence_value, "minimum_clusters": minimum,
    }
    if observations == 0:
        return {**common, "reason": "no_finite_observations"}
    if cluster_count < minimum:
        return {**common, "reason": "insufficient_clusters"}

    # The moving-block bootstrap has one circular block for every possible
    # starting cluster.  Truncate a concatenation of ceil(n/L) blocks to n
    # clusters so every replicate has the same effective sample size.
    size = cluster_count
    block_count = (size + length - 1) // length
    rng = random.Random(resolved_seed)
    means: list[float] = []
    for _ in range(resamples):
        selected: list[str] = []
        for _ in range(block_count):
            start = rng.randrange(size)
            selected.extend(order[(start + offset) % size]
                           for offset in range(length))
        selected = selected[:size]
        pooled = sum(sum(grouped[key]) for key in selected)
        count = sum(len(grouped[key]) for key in selected)
        means.append(pooled / count if count else 0.0)
    means.sort()
    lower_index = int(math.floor((1.0 - confidence_value) * resamples))
    lower_index = min(max(lower_index, 0), resamples - 1)
    upper_index = int(math.ceil(confidence_value * resamples)) - 1
    upper_index = min(max(upper_index, 0), resamples - 1)
    result = {**common, "available": True,
              "lower_bound": means[lower_index],
              "upper_bound": means[upper_index],
              "replicate_min": means[0], "replicate_max": means[-1],
              }
    if include_replicates:
        # Kept opt-in because persisted confidence-bound envelopes only need
        # summary quantiles; diagnostic power reports may inspect the exact
        # deterministic replicate distribution.
        result["replicate_means"] = list(means)
    return result


def clustered_mde_power_report(
        deltas, clusters, *, target_effect: float = 0.05,
        minimum_useful_edge: float | None = None,
        alpha: float = 0.05, target_power: float = 0.80,
        draws: int = DEFAULT_BOOTSTRAP_DRAWS,
        block_length: int = DEFAULT_CLUSTER_BLOCK_LENGTH,
        seed: int | None = None,
        min_clusters: int = DEFAULT_BREADTH_MIN_CLUSTERS,
        effect_unit: str = "delta_per_observation",
        cluster_unit: str = "session") -> dict:
    """Return a deterministic, diagnostic clustered MDE/power estimate.

    The estimate uses the same moving-block cluster bootstrap as the
    confidence-bound gate.  Bootstrap means are centred to form a null
    distribution, then shifted by ``target_effect`` to estimate one-sided
    rejection power.  This report is explicitly diagnostic: it never changes
    an acceptance check or an authorizing floor.  ``effect_unit`` describes
    both the observed effect and MDE (for example ``r_multiple``), while
    ``cluster_unit`` identifies the independent resampling unit (normally a
    market session).
    """
    try:
        effect = float(target_effect if minimum_useful_edge is None
                       else minimum_useful_edge)
        alpha_value = float(alpha)
        power_target = float(target_power)
    except (TypeError, ValueError) as exc:
        raise ValueError("target_effect, alpha, and target_power must be numeric") from exc
    if not math.isfinite(effect):
        raise ValueError("target_effect must be finite")
    if not math.isfinite(alpha_value) or not 0.0 < alpha_value < 1.0:
        raise ValueError("alpha must be between zero and one")
    if not math.isfinite(power_target) or not 0.0 < power_target < 1.0:
        raise ValueError("target_power must be between zero and one")
    if not isinstance(effect_unit, str) or not effect_unit.strip():
        raise ValueError("effect_unit must be a non-empty string")
    if not isinstance(cluster_unit, str) or not cluster_unit.strip():
        raise ValueError("cluster_unit must be a non-empty string")

    # Keep the normalisation and deterministic seed exactly aligned with the
    # moving-block confidence-bound implementation.  The helper below is
    # intentionally local so the existing bound's public payload remains
    # unchanged for legacy proofs.
    grouped: dict[str, list[float]] = {}
    order: list[str] = []
    for delta, cluster in zip(deltas, clusters):
        try:
            value = float(delta)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        key = str(cluster)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(value)
    observations = sum(len(grouped[key]) for key in order)
    cluster_count = len(order)
    try:
        length = int(block_length)
        minimum = max(1, int(min_clusters))
    except (TypeError, ValueError) as exc:
        raise ValueError("block_length and min_clusters must be integers") from exc
    if length <= 0:
        raise ValueError("block_length must be a positive integer")
    resamples = max(1, int(draws))
    payload = {
        "moving_block_bootstrap": [
            {"cluster": key, "values": grouped[key]} for key in order],
        "draws": resamples, "block_length": length,
        "min_clusters": minimum,
    }
    resolved_seed = int(stable_seed(payload) if seed is None else seed)
    observed_mean = (sum(sum(grouped[key]) for key in order) / observations
                     if observations else None)
    common = {
        "schema": "clustered-mde-power.v1",
        "method": "moving_block_cluster_bootstrap_mde_power",
        "method_version": "moving_block_cluster_bootstrap_mde_power.v1",
        "bootstrap_method": "moving_block",
        "diagnostic_only": True,
        "authorizing": False,
        "available": False,
        "reason": None,
        "effect_unit": effect_unit.strip(),
        "cluster_unit": cluster_unit.strip(),
        "units": {"effect": effect_unit.strip(), "mde": effect_unit.strip(),
                  "cluster": cluster_unit.strip()},
        "observed_mean": observed_mean,
        "target_effect": effect,
        "target_power": power_target,
        "alpha": alpha_value,
        "clusters": cluster_count,
        "observations": observations,
        "draws": resamples,
        "requested_draws": int(draws),
        "block_length": length,
        "seed": resolved_seed,
        "minimum_clusters": minimum,
        "mde": None,
        "standard_error": None,
        "estimated_power": None,
        "power": None,
    }
    if observations == 0:
        return {**common, "reason": "no_finite_observations", "draws": 0}
    if cluster_count < minimum:
        return {**common, "reason": "insufficient_clusters"}

    bootstrap = moving_block_cluster_bootstrap_lower_bound(
        [value for key in order for value in grouped[key]],
        [key for key in order for _ in grouped[key]],
        confidence=.95, draws=resamples, block_length=length,
        seed=resolved_seed, min_clusters=minimum, include_replicates=True)
    means = list(bootstrap.get("replicate_means") or ())
    if not means:
        return {**common, "reason": "bootstrap_unavailable"}
    center = sum(means) / len(means)
    variance = sum((value - center) ** 2 for value in means) / len(means)
    standard_error = math.sqrt(max(0.0, variance))
    normal = NormalDist()
    critical = normal.inv_cdf(1.0 - alpha_value)
    power_quantile = normal.inv_cdf(power_target)
    mde = (critical + power_quantile) * standard_error
    # Shift the centred bootstrap null by the target effect.  Counting draws
    # against a one-sided normal critical boundary keeps the result entirely
    # deterministic while retaining the cluster dependence in each draw.
    null_draws = [value - center for value in means]
    threshold = critical * standard_error
    estimated_power = (sum(1 for value in null_draws
                           if value + effect >= threshold) / len(null_draws)
                       if standard_error > 1e-15 else
                       (1.0 if effect > 0 else 0.0))
    return {**common, "available": True, "standard_error": standard_error,
            "mde": mde, "minimum_detectable_effect": mde,
            "mde_effect": mde,
            "estimated_power": estimated_power, "power": estimated_power,
            "power_at_target": estimated_power,
            "critical_value": critical, "bootstrap_mean": center,
            "bootstrap_min": min(means), "bootstrap_max": max(means),
            "reason": None}


# Short compatibility aliases for callers that use the conventional MDE
# terminology.  They intentionally resolve to the same diagnostic-only
# implementation and therefore carry identical method/version metadata.
mde_power_report = clustered_mde_power_report
clustered_mde_report = clustered_mde_power_report
clustered_mde_power = clustered_mde_power_report


def _breadth_value(row, keys):
    """Read the first present field from a mapping without truthiness bugs."""
    for key in keys:
        if key in row:
            return row[key]
    return None


def _coerce_breadth_observations(
        paired_deltas, sessions=None, symbols=None, *,
        delta_key: str = "delta", session_key: str = "session",
        symbol_key: str = "symbol") -> tuple[list[tuple[str, str, float]],
                                               set[str], set[str], int]:
    """Normalize common row, tuple, matrix, and mapping breadth inputs."""
    observations: list[tuple[str, str, float]] = []
    session_labels: set[str] = set()
    symbol_labels: set[str] = set()
    invalid = 0

    def add(session, symbol, delta):
        nonlocal invalid
        if session is None or symbol is None:
            invalid += 1
            return
        session_key_value = str(session)
        symbol_key_value = str(symbol)
        session_labels.add(session_key_value)
        symbol_labels.add(symbol_key_value)
        try:
            value = float(delta)
        except (TypeError, ValueError):
            invalid += 1
            return
        if not math.isfinite(value):
            invalid += 1
            return
        observations.append((session_key_value, symbol_key_value, value))

    # A flat delta vector plus parallel session and symbol vectors is useful
    # for callers that already materialize paired values separately.
    if sessions is not None or symbols is not None:
        if sessions is None or symbols is None:
            raise ValueError("sessions and symbols must be supplied together")
        values = list(paired_deltas or ())
        session_values = list(sessions)
        symbol_values = list(symbols)
        # Also accept a rectangular matrix with explicit row/column labels.
        # This form retains labels for entirely missing cells, which improves
        # the missing-data diagnostics over inferring labels from observations.
        if (len(values) == len(session_values) and
                all(isinstance(row, (list, tuple)) for row in values) and
                all(len(row) == len(symbol_values) for row in values)):
            for row, session in zip(values, session_values):
                for value, symbol in zip(row, symbol_values):
                    add(session, symbol, value)
            return observations, session_labels, symbol_labels, invalid
        if not (len(values) == len(session_values) == len(symbol_values)):
            raise ValueError("paired deltas, sessions, and symbols must align")
        for value, session, symbol in zip(values, session_values, symbol_values):
            add(session, symbol, value)
        return observations, session_labels, symbol_labels, invalid

    # A mapping of session -> {symbol: delta} is an unambiguous matrix form.
    if isinstance(paired_deltas, dict):
        for session, values in paired_deltas.items():
            if isinstance(values, dict):
                for symbol, value in values.items():
                    add(session, symbol, value)
            elif isinstance(session, (tuple, list)) and len(session) == 2:
                add(session[0], session[1], values)
            else:
                invalid += 1
        return observations, session_labels, symbol_labels, invalid

    for row in paired_deltas or ():
        if isinstance(row, dict):
            session = _breadth_value(
                row, (session_key, "session", "session_id", "session_date",
                      "date"))
            symbol = _breadth_value(row, (symbol_key, "symbol", "ticker"))
            delta = _breadth_value(
                row, (delta_key, "delta", "paired_delta", "difference",
                      "candidate_minus_baseline"))
            add(session, symbol, delta)
        else:
            try:
                session, symbol, delta = row[:3]
            except (TypeError, ValueError, IndexError):
                invalid += 1
                continue
            add(session, symbol, delta)
    return observations, session_labels, symbol_labels, invalid


def _symmetric_eigenvalues(matrix: list[list[float]]) -> list[float]:
    """Compute eigenvalues of a small real symmetric matrix with Jacobi sweeps."""
    size = len(matrix)
    if size == 0:
        return []
    values = [list(row) for row in matrix]
    # A deterministic Jacobi diagonalization avoids a numpy dependency and is
    # stable for the small symbol counts used in research breadth reports.
    max_sweeps = max(12, 50 * size * size)
    tolerance = 1e-12
    for _ in range(max_sweeps):
        p, q = 0, 0
        largest = 0.0
        for row in range(size):
            for col in range(row + 1, size):
                magnitude = abs(values[row][col])
                if magnitude > largest:
                    largest, p, q = magnitude, row, col
        if largest <= tolerance:
            break
        angle = .5 * math.atan2(
            2.0 * values[p][q], values[q][q] - values[p][p])
        cosine, sine = math.cos(angle), math.sin(angle)
        app, aqq, apq = values[p][p], values[q][q], values[p][q]
        values[p][p] = (cosine * cosine * app - 2.0 * sine * cosine * apq
                        + sine * sine * aqq)
        values[q][q] = (sine * sine * app + 2.0 * sine * cosine * apq
                        + cosine * cosine * aqq)
        values[p][q] = values[q][p] = 0.0
        for index in range(size):
            if index in (p, q):
                continue
            aip, aiq = values[index][p], values[index][q]
            values[index][p] = values[p][index] = cosine * aip - sine * aiq
            values[index][q] = values[q][index] = sine * aip + cosine * aiq
    result = []
    for index in range(size):
        value = values[index][index]
        # Correlation matrices are positive semidefinite; round only tiny
        # Jacobi residuals below zero, while rejecting materially bad results.
        result.append(0.0 if value < 0.0 and value > -1e-9 else max(0.0, value))
    return sorted(result, reverse=True)


def effective_breadth_report(
        paired_deltas, sessions=None, symbols=None, *, delta_key: str = "delta",
        session_key: str = "session", symbol_key: str = "symbol",
        min_sessions: int = DEFAULT_BREADTH_MIN_SESSIONS) -> dict:
    """Estimate independent breadth from session-by-symbol paired deltas.

    The report uses complete sessions to form a centered symbol matrix, then a
    symmetric correlation matrix and its eigenvalue participation ratio
    ``(sum(lambda))**2 / sum(lambda**2)``.  Complete-case construction keeps
    the matrix positive semidefinite under missing data; diagnostics expose
    how much data was omitted rather than silently imputing it.
    """
    observations, session_labels, symbol_labels, invalid = (
        _coerce_breadth_observations(
            paired_deltas, sessions, symbols, delta_key=delta_key,
            session_key=session_key, symbol_key=symbol_key))
    ordered_sessions = sorted(session_labels)
    ordered_symbols = sorted(symbol_labels)
    expected = len(ordered_sessions) * len(ordered_symbols)
    cells: dict[tuple[str, str], list[float]] = {}
    for session, symbol, value in observations:
        cells.setdefault((session, symbol), []).append(value)
    duplicate_cells = sum(max(0, len(values) - 1) for values in cells.values())
    # Duplicate cells are averaged in input order.  This is deterministic and
    # prevents accidental duplicate rows from overweighting one symbol/session.
    matrix = {key: sum(values) / len(values) for key, values in cells.items()}
    observed = len(matrix)
    missing_cells = max(0, expected - observed)
    complete = [session for session in ordered_sessions
                if all((session, symbol) in matrix for symbol in ordered_symbols)]
    complete_count = len(complete)
    symbol_counts = {
        symbol: sum((session, symbol) in matrix for session in ordered_sessions)
        for symbol in ordered_symbols}
    common = {
        "method": "symmetric_correlation_eigenvalue_participation_ratio",
        "available": False, "effective_breadth": None, "breadth": None,
        "participation_ratio": None, "eigenvalues": [],
        "sessions": len(ordered_sessions), "symbols": len(ordered_symbols),
        "complete_sessions": complete_count,
        "observations": observed, "expected_observations": expected,
        "missing_cells": missing_cells,
        "missing_rate": (missing_cells / expected if expected else 1.0),
        "invalid_observations": invalid, "duplicate_cells": duplicate_cells,
        "symbol_counts": symbol_counts,
        "missing_data": {
            "expected_cells": expected, "observed_cells": observed,
            "missing_cells": missing_cells,
            "missing_rate": (missing_cells / expected if expected else 1.0),
            "complete_sessions": complete_count,
            "invalid_observations": invalid,
            "duplicate_cells": duplicate_cells,
        },
        "missing": {
            "expected_cells": expected, "observed_cells": observed,
            "missing_cells": missing_cells,
            "missing_rate": (missing_cells / expected if expected else 1.0),
        },
        "concentration": None, "concentration_index": None,
        "concentration_diagnostics": {},
    }
    try:
        minimum = max(2, int(min_sessions))
    except (TypeError, ValueError) as exc:
        raise ValueError("min_sessions must be a positive integer") from exc
    if len(ordered_symbols) < 2:
        return {**common, "reason": "insufficient_symbols",
                "minimum_sessions": minimum}
    if complete_count < minimum:
        return {**common, "reason": "insufficient_complete_sessions",
                "minimum_sessions": minimum}

    columns = [[matrix[(session, symbol)] for session in complete]
               for symbol in ordered_symbols]
    means = [sum(column) / len(column) for column in columns]
    centered = [[value - means[index] for value in column]
                for index, column in enumerate(columns)]
    variances = [sum(value * value for value in column)
                 for column in centered]
    if any(value <= 1e-18 for value in variances):
        return {**common, "reason": "degenerate_symbol_variance",
                "minimum_sessions": minimum}
    correlations = [[0.0] * len(ordered_symbols)
                    for _ in ordered_symbols]
    for row in range(len(ordered_symbols)):
        correlations[row][row] = 1.0
        for col in range(row):
            covariance = sum(centered[row][i] * centered[col][i]
                             for i in range(complete_count))
            denominator = math.sqrt(variances[row] * variances[col])
            correlation = covariance / denominator if denominator else 0.0
            correlation = min(1.0, max(-1.0, correlation))
            correlations[row][col] = correlations[col][row] = correlation
    eigenvalues = _symmetric_eigenvalues(correlations)
    total = sum(eigenvalues)
    squared = sum(value * value for value in eigenvalues)
    if total <= 1e-12 or squared <= 1e-12:
        return {**common, "reason": "degenerate_correlation_spectrum",
                "minimum_sessions": minimum}
    breadth = min(float(len(ordered_symbols)), max(1.0, total * total / squared))
    shares = [value / total for value in eigenvalues]
    herfindahl = sum(value * value for value in shares)
    concentration = {
        "herfindahl": herfindahl,
        "max_eigenvalue_share": max(shares),
        "top_eigenvalue": eigenvalues[0],
        "independent_limit": len(ordered_symbols),
    }
    return {**common, "available": True, "reason": None,
            "minimum_sessions": minimum, "eigenvalues": eigenvalues,
            "effective_breadth": breadth, "breadth": breadth,
            "participation_ratio": breadth, "concentration": herfindahl,
            "concentration_index": herfindahl,
            "concentration_diagnostics": concentration,
            "correlation": correlations}


def _family_session_observations(values, sessions=None, *,
                                 family_key: str = "family",
                                 session_key: str = "session",
                                 value_key: str = "statistic"):
    """Coerce common family/session vector shapes into finite observations.

    This helper is deliberately permissive because the report is an
    observability surface, not an authorizing gate.  It accepts nested
    ``family -> session -> value`` mappings, row mappings, and triples of
    ``(family, session, value)``.  A sequence value is reduced to its finite
    arithmetic mean; callers that need a separate statistic should pass one
    report per statistic.  Duplicate family/session cells are retained so the
    public report can disclose that they were averaged.
    """
    observations: list[tuple[str, str, float]] = []
    invalid = 0
    dimensions: dict[tuple[str, str], int] = {}

    def scalar(value):
        nonlocal invalid
        if isinstance(value, bool) or value is None:
            invalid += 1
            return None
        if isinstance(value, (list, tuple)):
            finite = []
            for item in value:
                try:
                    number = float(item)
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(number):
                    finite.append(number)
            if not finite:
                invalid += 1
                return None
            return sum(finite) / len(finite)
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            invalid += 1
            return None
        if not math.isfinite(number):
            invalid += 1
            return None
        return number

    def add(family, session, value):
        number = scalar(value)
        if number is None:
            return
        key = (str(family), str(session))
        observations.append((key[0], key[1], number))
        if isinstance(value, (list, tuple)):
            dimensions[key] = max(dimensions.get(key, 0), len(value))

    if isinstance(values, Mapping):
        # Also accept a flat tuple-key mapping: {(family, session): value}.
        for family, per_family in values.items():
            if (isinstance(family, (tuple, list)) and len(family) >= 2 and
                    not isinstance(per_family, Mapping)):
                add(family[0], family[1], per_family)
                continue
            if isinstance(per_family, Mapping):
                for session, value in per_family.items():
                    add(family, session, value)
                continue
            if sessions is None:
                continue
            try:
                paired_sessions = list(sessions)
                paired_values = list(per_family)
            except (TypeError, ValueError):
                invalid += 1
                continue
            for session, value in zip(paired_sessions, paired_values):
                add(family, session, value)
    else:
        try:
            iterable = list(values or ())
        except TypeError:
            iterable = []
            invalid += 1
        for row in iterable:
            if isinstance(row, Mapping):
                family = row.get(family_key, row.get("family"))
                session = row.get(session_key, row.get("session"))
                value = row.get(value_key, row.get("value"))
                if value is None:
                    value = row.get("delta", row.get("net_pnl"))
            else:
                try:
                    family, session, value = row[:3]
                except (TypeError, ValueError, IndexError):
                    invalid += 1
                    continue
            if family is None or session is None:
                invalid += 1
                continue
            add(family, session, value)
    return observations, dimensions, invalid


def cross_family_dependence_report(
        family_session_vectors, sessions=None, *,
        family_key: str = "family", session_key: str = "session",
        value_key: str = "statistic", min_sessions: int = 3,
        strong_threshold: float = .8) -> dict:
    """Describe dependence between strategy families over shared sessions.

    The input is interpreted as one statistic per family/session cell (or a
    short vector whose finite mean is used for that cell).  Correlations are
    pairwise Pearson correlations over complete shared sessions and are
    strictly diagnostic: this function never changes a gate, BH correction,
    allocation, or runtime grouping.  Missing cells, duplicate cells, and
    degenerate vectors are reported explicitly instead of being imputed.
    """
    try:
        minimum = max(2, int(min_sessions))
    except (TypeError, ValueError) as exc:
        raise ValueError("min_sessions must be a positive integer") from exc
    try:
        threshold = float(strong_threshold)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("strong_threshold must be a finite number") from exc
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("strong_threshold must be between zero and one")
    observations, dimensions, invalid = _family_session_observations(
        family_session_vectors, sessions, family_key=family_key,
        session_key=session_key, value_key=value_key)
    grouped: dict[tuple[str, str], list[float]] = {}
    for family, session, value in observations:
        grouped.setdefault((family, session), []).append(value)
    families = sorted({family for family, _ in grouped})
    ordered_sessions = sorted({session for _, session in grouped})
    matrix = {key: sum(values) / len(values) for key, values in grouped.items()}
    duplicate_cells = sum(max(0, len(values) - 1) for values in grouped.values())
    expected = len(families) * len(ordered_sessions)
    complete = [session for session in ordered_sessions
                if all((family, session) in matrix for family in families)]
    vectors = {
        family: [matrix[(family, session)] for session in complete]
        for family in families
    }
    common = {
        "schema": "cross-family-dependence.v1",
        "method": "pairwise_complete_session_pearson",
        "available": False,
        "families": families,
        "sessions": ordered_sessions,
        "complete_sessions": complete,
        "vectors": vectors,
        "family_session_vectors": vectors,
        "pairwise": [],
        "pair_reports": [],
        "correlations": {},
        "correlation_matrix": {},
        "family_count": len(families),
        "session_count": len(ordered_sessions),
        "complete_session_count": len(complete),
        "minimum_sessions": minimum,
        "observations": len(matrix),
        "expected_observations": expected,
        "missing_cells": max(0, expected - len(matrix)),
        "duplicate_cells": duplicate_cells,
        "invalid_observations": invalid,
        "vector_dimensions": {
            f"{family}:{session}": dimensions[(family, session)]
            for family, session in sorted(dimensions)
        },
        "strong_threshold": threshold,
        "dependence": {
            "strong_pairs": [], "moderate_pairs": [],
            "mean_absolute_correlation": None,
            "max_absolute_correlation": None,
        },
    }
    if len(families) < 2:
        return {**common, "reason": "insufficient_families"}
    if len(complete) < minimum:
        return {**common, "reason": "insufficient_complete_sessions"}
    pairwise = []
    for index, left in enumerate(families):
        for right in families[index + 1:]:
            left_values = vectors[left]
            right_values = vectors[right]
            left_mean = sum(left_values) / len(left_values)
            right_mean = sum(right_values) / len(right_values)
            left_centered = [value - left_mean for value in left_values]
            right_centered = [value - right_mean for value in right_values]
            left_var = sum(value * value for value in left_centered)
            right_var = sum(value * value for value in right_centered)
            covariance = sum(a * b for a, b in zip(left_centered, right_centered))
            correlation = (covariance / math.sqrt(left_var * right_var)
                           if left_var > 1e-18 and right_var > 1e-18 else None)
            if correlation is not None:
                correlation = min(1.0, max(-1.0, correlation))
            absolute = abs(correlation) if correlation is not None else None
            item = {
                "families": [left, right],
                "left": left,
                "right": right,
                "sessions": len(complete),
                "correlation": correlation,
                "absolute_correlation": absolute,
                "available": correlation is not None,
                "reason": None if correlation is not None else "degenerate_vector",
            }
            pairwise.append(item)
    available_pairs = [item for item in pairwise if item["available"]]
    correlations = {
        f"{item['left']}|{item['right']}": item["correlation"]
        for item in available_pairs
    }
    correlation_matrix = {family: {other: (1.0 if family == other else None)
                                   for other in families}
                          for family in families}
    for item in available_pairs:
        correlation_matrix[item["left"]][item["right"]] = item["correlation"]
        correlation_matrix[item["right"]][item["left"]] = item["correlation"]
    absolute_values = [item["absolute_correlation"] for item in available_pairs]
    strong = [item["families"] for item in available_pairs
              if item["absolute_correlation"] >= threshold]
    moderate = [item["families"] for item in available_pairs
                if item["absolute_correlation"] >= .5]
    dependence = {
        "strong_pairs": strong,
        "moderate_pairs": moderate,
        "mean_absolute_correlation": (
            sum(absolute_values) / len(absolute_values)
            if absolute_values else None),
        "max_absolute_correlation": max(absolute_values)
        if absolute_values else None,
        "mean_abs_correlation": (
            sum(absolute_values) / len(absolute_values)
            if absolute_values else None),
        "max_abs_correlation": max(absolute_values)
        if absolute_values else None,
    }
    return {**common, "available": bool(available_pairs), "reason": None
            if available_pairs else "no_non_degenerate_pairs",
            "pairwise": pairwise, "correlations": correlations,
            "pair_reports": pairwise, "correlation_matrix": correlation_matrix,
            "dependence": dependence}


# Explicit aliases make the report easy to discover for callers that describe
# the input as a mapping rather than a vector table.
family_session_dependence_report = cross_family_dependence_report
cross_family_report = cross_family_dependence_report
cross_family_dependence = cross_family_dependence_report
family_dependence_report = cross_family_dependence_report


def paired_cluster_sign_flip(
        pairs: list[tuple[float, float, float]], *,
        cluster_seconds: int = 21_600, exact_max_clusters: int = 16,
        iterations: int = 20_000, seed: int = 20260728) -> dict:
    """Test paired candidate-minus-baseline deltas by market-time cluster."""
    clean: list[tuple[float, float]] = []
    for timestamp, candidate, baseline in pairs:
        try:
            values = (float(timestamp), float(candidate), float(baseline))
        except (TypeError, ValueError):
            continue
        if all(math.isfinite(value) for value in values):
            clean.append((values[0], values[1] - values[2]))
    clusters: dict[int, list[float]] = {}
    for timestamp, delta in clean:
        clusters.setdefault(int(timestamp // cluster_seconds), []).append(delta)
    contributions = [sum(clusters[key]) for key in sorted(clusters)]
    observed_sum = sum(contributions)
    common = {
        "kind": "paired_cluster_sign_flip",
        "null_assumption": PAIRED_SIGN_FLIP_NULL_ASSUMPTION,
        "alternative": "greater", "cluster_seconds": int(cluster_seconds),
        "clusters": len(contributions), "paired_n": len(clean),
        "observed_mean": observed_sum / len(clean) if clean else 0.0,
    }
    if not clean or not contributions:
        return {**common, "method": "unavailable", "exact": False,
                "resamples": 0, "seed": None, "p_value": 1.0}
    return {**common, **_sign_flip(
        contributions, observed_sum, exact_max_clusters=exact_max_clusters,
        iterations=iterations, seed=seed)}


def benjamini_hochberg(pvalues: dict, alpha: float = 0.05) -> dict:
    """Control false discoveries across one preregistered candidate family."""
    try:
        nominal = float(alpha)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("alpha must be a finite probability in (0,1]") from exc
    if not math.isfinite(nominal) or not 0.0 < nominal <= 1.0:
        raise ValueError("alpha must be a finite probability in (0,1]")
    clean: dict[str, float] = {}
    for name, raw in pvalues.items():
        if isinstance(raw, bool):
            raise ValueError(f"p-value for {name!r} must be a finite probability")
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                f"p-value for {name!r} must be a finite probability") from exc
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"p-value for {name!r} must be in [0,1]")
        clean[str(name)] = value
    ordered = sorted(clean.items(), key=lambda item: item[1])
    total = len(ordered)
    if not total:
        return {}
    adjusted: dict[str, float] = {}
    running = 1.0
    for index in range(total - 1, -1, -1):
        name, raw = ordered[index]
        running = min(running, min(1.0, float(raw) * total / (index + 1)))
        adjusted[name] = running
    return {
        name: {"p": clean[str(name)], "p_adjusted": adjusted[str(name)],
               "significant": adjusted[str(name)] <= nominal,
               "family_size": total}
        for name in pvalues
    }


__all__ = ["DEPENDENCE_POLICY_SCHEMA", "DEPENDENCE_MIN_COMPLETE_SESSIONS",
           "DEPENDENCE_MIN_PRIOR_CYCLES", "DEPENDENCE_CORRELATION_THRESHOLD",
           "DEPENDENCE_MIN_SESSIONS", "DEPENDENCE_MIN_CYCLES",
           "DEPENDENCE_ABS_CORRELATION",
           "deterministic_dependence_map", "build_dependence_map",
           "dependence_policy_map", "build_dependence_policy",
           "DEFAULT_BREADTH_MIN_CLUSTERS", "DEFAULT_BREADTH_MIN_SESSIONS",
           "DEFAULT_BOOTSTRAP_DRAWS", "DEFAULT_NULL_DRAWS",
           "DEFAULT_CLUSTER_BLOCK_LENGTH",
           "benjamini_hochberg",
           "cluster_bootstrap_lower_bound", "cluster_contributions",
           "clustered_mde_power_report", "clustered_mde_report",
           "clustered_mde_power", "mde_power_report",
           "effective_breadth_report",
           "cross_family_dependence_report", "family_session_dependence_report",
           "cross_family_report", "cross_family_dependence",
           "family_dependence_report",
           "moving_block_cluster_bootstrap_lower_bound",
           "paired_cluster_sign_flip", "sign_flip_null_statistics",
           "stable_seed"]
