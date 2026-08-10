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


PAIRED_SIGN_FLIP_NULL_ASSUMPTION = (
    "cluster_delta_sign_exchangeability_under_symmetric_null")
DEFAULT_NULL_DRAWS = 10_000
DEFAULT_BOOTSTRAP_DRAWS = 4_000


def stable_seed(value) -> int:
    """Derive a reproducible 63-bit seed from arbitrary JSON-able content."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False, default=str)
    digest = hashlib.sha256(encoded.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


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
                                  seed: int | None = None) -> dict:
    """Bootstrap a one-sided lower bound on the mean delta over whole clusters.

    Clusters, not observations, are resampled: intraday deltas inside one
    session are not independent and must move together.
    """
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
    if not 0 < float(confidence) < 1:
        raise ValueError("confidence must be between zero and one")
    resolved_seed = int(stable_seed({"bootstrap": [grouped[key] for key in keys],
                                     "draws": resamples,
                                     "confidence": float(confidence)})
                        if seed is None else seed)
    if not keys or total == 0:
        return {"method": "cluster_bootstrap", "available": False,
                "lower_bound": None, "mean": None, "clusters": 0,
                "observations": 0, "draws": 0, "seed": resolved_seed,
                "confidence": float(confidence)}
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
    return {"method": "cluster_bootstrap", "available": True,
            "lower_bound": means[index],
            "mean": sum(sum(grouped[key]) for key in keys) / total,
            "clusters": size, "observations": total, "draws": resamples,
            "seed": resolved_seed, "confidence": float(confidence)}


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
    ordered = sorted(pvalues.items(), key=lambda item: float(item[1]))
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
        name: {"p": float(pvalues[name]), "p_adjusted": adjusted[name],
               "significant": adjusted[name] <= alpha,
               "family_size": total}
        for name in pvalues
    }


__all__ = ["DEFAULT_BOOTSTRAP_DRAWS", "DEFAULT_NULL_DRAWS",
           "benjamini_hochberg", "cluster_bootstrap_lower_bound",
           "cluster_contributions", "paired_cluster_sign_flip",
           "sign_flip_null_statistics", "stable_seed"]
