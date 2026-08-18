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


PAIRED_SIGN_FLIP_NULL_ASSUMPTION = (
    "cluster_delta_sign_exchangeability_under_symmetric_null")
DEFAULT_NULL_DRAWS = 10_000
DEFAULT_BOOTSTRAP_DRAWS = 4_000
DEFAULT_BREADTH_MIN_CLUSTERS = 2
DEFAULT_BREADTH_MIN_SESSIONS = 3
DEFAULT_CLUSTER_BLOCK_LENGTH = 5


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


__all__ = ["DEFAULT_BREADTH_MIN_CLUSTERS", "DEFAULT_BREADTH_MIN_SESSIONS",
           "DEFAULT_BOOTSTRAP_DRAWS", "DEFAULT_NULL_DRAWS",
           "DEFAULT_CLUSTER_BLOCK_LENGTH",
           "benjamini_hochberg",
           "cluster_bootstrap_lower_bound", "cluster_contributions",
           "clustered_mde_power_report", "clustered_mde_report",
           "clustered_mde_power", "mde_power_report",
           "effective_breadth_report",
           "moving_block_cluster_bootstrap_lower_bound",
           "paired_cluster_sign_flip", "sign_flip_null_statistics",
           "stable_seed"]
