"""Deterministic, cluster-aware tests used by the edge discovery gates."""

from __future__ import annotations

import math
import random


PAIRED_SIGN_FLIP_NULL_ASSUMPTION = (
    "cluster_delta_sign_exchangeability_under_symmetric_null")


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


__all__ = ["benjamini_hochberg", "paired_cluster_sign_flip"]
