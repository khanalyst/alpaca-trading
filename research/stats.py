"""Small-sample honesty: intervals, minimum detectable effects, and refusal.

At a few dozen round trips a point estimate is noise wearing a decimal point.
Every metric this programme reports carries a bootstrap interval, and the
scorer is allowed - required, in fact - to say ``INSUFFICIENT_SAMPLE`` and
decline to rank.

That refusal is the single most valuable behaviour in the module. Four
consecutive ``INSUFFICIENT_SAMPLE`` results will feel like failure and will
invite relaxing the rule, which is exactly the moment the rule exists for.
A sweep that reports a winner on twelve trades has not found a winner; it has
found the largest of twelve random numbers.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass


INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"


@dataclass(frozen=True)
class Interval:
    point: float
    low: float
    high: float
    n: int

    def excludes_zero(self) -> bool:
        return (self.low > 0) or (self.high < 0)

    def __str__(self) -> str:
        return f"{self.point:+.4f} [{self.low:+.4f},{self.high:+.4f}] n={self.n}"


def bootstrap_mean(values: list, iterations: int = 2000,
                   confidence: float = 0.95, seed: int = 20260728) -> Interval:
    """Percentile bootstrap on the mean, with a fixed seed.

    The seed is not a detail. An unseeded bootstrap makes every reported
    interval slightly different on every run, so two readings of the same
    result disagree and neither can be checked. Reproducibility beats
    theoretical purity here: the interval is an estimate either way.
    """
    clean = [float(v) for v in values
             if v is not None and math.isfinite(float(v))]
    n = len(clean)
    if n == 0:
        return Interval(0.0, 0.0, 0.0, 0)
    if n == 1:
        return Interval(clean[0], clean[0], clean[0], 1)

    point = sum(clean) / n
    rng = random.Random(seed)
    means = []
    for _ in range(iterations):
        sample = [clean[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    tail = (1.0 - confidence) / 2.0
    low = means[int(tail * len(means))]
    high = means[min(len(means) - 1, int((1.0 - tail) * len(means)))]
    return Interval(point, low, high, n)


def bootstrap_difference(a: list, b: list, iterations: int = 2000,
                         confidence: float = 0.95,
                         seed: int = 20260728) -> Interval:
    """Interval on ``mean(a) - mean(b)``, resampling both arms independently."""
    clean_a = [float(v) for v in a if v is not None and math.isfinite(float(v))]
    clean_b = [float(v) for v in b if v is not None and math.isfinite(float(v))]
    if not clean_a or not clean_b:
        return Interval(0.0, 0.0, 0.0, 0)

    point = (sum(clean_a) / len(clean_a)) - (sum(clean_b) / len(clean_b))
    rng = random.Random(seed)
    diffs = []
    for _ in range(iterations):
        sa = [clean_a[rng.randrange(len(clean_a))] for _ in clean_a]
        sb = [clean_b[rng.randrange(len(clean_b))] for _ in clean_b]
        diffs.append(sum(sa) / len(sa) - sum(sb) / len(sb))
    diffs.sort()
    tail = (1.0 - confidence) / 2.0
    return Interval(point, diffs[int(tail * len(diffs))],
                    diffs[min(len(diffs) - 1, int((1.0 - tail) * len(diffs)))],
                    min(len(clean_a), len(clean_b)))


def stdev(values: list) -> float:
    clean = [float(v) for v in values
             if v is not None and math.isfinite(float(v))]
    if len(clean) < 2:
        return 0.0
    mean = sum(clean) / len(clean)
    return math.sqrt(sum((v - mean) ** 2 for v in clean) / (len(clean) - 1))


def minimum_detectable_effect(values: list, power: float = 0.8,
                              alpha: float = 0.05) -> float:
    """The smallest effect this sample could distinguish from zero.

    Reported alongside every result so a null can be read correctly. "No
    difference detected, MDE 0.34R" and "no difference" are very different
    statements, and only the first one is honest about a sample of forty.
    """
    n = len([v for v in values if v is not None and math.isfinite(float(v))])
    if n < 2:
        return float("inf")
    # Normal approximation: z(1-a/2) + z(power) = 1.96 + 0.84 at 95%/80%.
    z = 1.959964 + 0.8416212
    return z * stdev(values) / math.sqrt(n)


def achievable_n(total_round_trips: int, grid_points: int) -> dict:
    """Forecast per-cell sample before a sweep runs, per action-plan C4.

    The promotion protocol needs 100 matched round trips per variant across
    at least three settings. Running a five-point sweep against forty trades
    and reporting INSUFFICIENT_SAMPLE five times wastes the calendar time the
    forecast could have saved, and each repetition makes relaxing the rule
    more tempting.
    """
    points = max(1, int(grid_points))
    per_cell = total_round_trips / points
    return {
        "total_round_trips": total_round_trips,
        "grid_points": points,
        "per_cell": per_cell,
        "required_per_cell": 100,
        "sufficient": per_cell >= 100,
        "shortfall": max(0.0, 100 - per_cell),
    }


def holm_bonferroni(pvalues: dict, alpha: float = 0.05) -> dict:
    """Family-wise correction across a batch of tests.

    Six hypotheses, several conditioning cells each, against a few hundred
    round trips: something will look significant. The corrected figure is the
    only one any recommendation may quote.
    """
    ordered = sorted(pvalues.items(), key=lambda kv: kv[1])
    total = len(ordered)
    out, previous = {}, 0.0
    for index, (name, p) in enumerate(ordered):
        adjusted = min(1.0, max(previous, (total - index) * float(p)))
        previous = adjusted
        out[name] = {
            "p": float(p), "p_adjusted": adjusted,
            "significant": adjusted <= alpha,
        }
    return out
