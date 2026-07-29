"""The promotion protocol: promote and reject become rules, not judgements.

At these sample sizes, deciding after looking at the results is p-hacking with
extra steps. So the rule is written here, applied by code, and the code is
allowed to return an answer nobody wants.

``INSUFFICIENT_SAMPLE`` is the most important verdict in this module and the
one most likely to be argued with. It will be returned four times in a row,
it will feel like the harness is broken, and the temptation at that moment is
to relax the threshold "just to see". That moment is precisely what the
threshold exists for: a sweep that names a winner on twelve trades has not
found a winner, it has found the largest of twelve random numbers.

The asymmetry between promotion and rejection is deliberate. Promotion
requires every criterion to hold; rejection requires the whole axis to have
been tried, because intention #4 is that a hypothesis is never killed by one
badly chosen parameter value.

See ``protocol.md`` for the prose version, which is the one to read first.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .score import score_returns
from .stats import INSUFFICIENT_SAMPLE, bootstrap_difference, holm_bonferroni


PROMOTE = "PROMOTE"
REJECT = "REJECT"
CONTINUE = "CONTINUE"

MIN_ROUND_TRIPS = 100
MIN_AXIS_SETTINGS = 3
OUT_OF_SAMPLE_FRACTION = 0.7


@dataclass
class Verdict:
    """A decision, and the single criterion that governed it."""

    verdict: str
    governing_criterion: str
    detail: str = ""
    evidence: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.verdict} ({self.governing_criterion}): {self.detail}"


# --------------------------------------------------------- out-of-sample

def split_by_time(items: list, fraction: float = OUT_OF_SAMPLE_FRACTION,
                  key=lambda item: getattr(item, "ts", 0.0)) -> tuple:
    """Split chronologically into a fit window and a confirm window.

    By time, never at random. A random split lets the same market episode
    appear on both sides, so a variant fitted to one afternoon's regime
    confirms on the same afternoon and looks robust. The whole point of the
    exercise is that the confirm window contains market the fit window has
    never seen.
    """
    ordered = sorted(items, key=key)
    if len(ordered) < 2:
        return ordered, []
    cut = int(len(ordered) * fraction)
    cut = min(max(cut, 1), len(ordered) - 1)
    return ordered[:cut], ordered[cut:]


def regime_profile(decisions: list) -> dict:
    """Realised-volatility profile of a window, reported beside every split.

    From the risk register: a corpus spanning a volatility regime change
    makes the out-of-sample split a regime test rather than a robustness
    test. Those have opposite meanings - one says the variant generalises,
    the other says the market changed - and they are indistinguishable from
    the pass/fail alone. So the profile of both windows is always quoted.
    """
    ratios = []
    for decision in decisions:
        value = (getattr(decision, "enrichment", None) or {}).get(
            "realised_vol_ratio_8_96")
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            ratios.append(number)
    if not ratios:
        return {"n": 0, "median_vol_ratio": None, "comparable": None}
    ratios.sort()
    return {"n": len(ratios),
            "median_vol_ratio": ratios[len(ratios) // 2],
            "comparable": None}


def out_of_sample(decisions: list,
                  fraction: float = OUT_OF_SAMPLE_FRACTION) -> dict:
    """Fit on the first 70% of the corpus, confirm on the last 30%.

    "Survives" means the confirm window's expectancy interval does not
    exclude the fit window's point estimate downward - that is, the confirm
    window is not evidence *against* what the fit window claimed. It is a
    deliberately weak bar, because at these samples a strict one would reject
    everything, and a bar nothing can pass carries no information.
    """
    fit, confirm = split_by_time(decisions, fraction)
    fit_returns = _returns(fit)
    confirm_returns = _returns(confirm)
    fit_score = score_returns(fit_returns, label="fit")
    confirm_score = score_returns(confirm_returns, label="confirm")

    fit_profile = regime_profile(fit)
    confirm_profile = regime_profile(confirm)
    both = (fit_profile["median_vol_ratio"], confirm_profile["median_vol_ratio"])
    if all(v is not None for v in both) and max(both) > 0:
        # A ratio beyond 1.5x between windows means the split is measuring a
        # regime change at least as much as it is measuring robustness.
        spread = max(both) / min(both) if min(both) > 0 else float("inf")
        comparable = spread <= 1.5
        fit_profile["comparable"] = comparable
        confirm_profile["comparable"] = comparable

    if not confirm_returns or not fit_returns:
        return {"survives": None, "reason": "one window is empty",
                "fit": fit_score, "confirm": confirm_score,
                "fit_regime": fit_profile, "confirm_regime": confirm_profile}

    survives = confirm_score["ci_high"] >= fit_score["expectancy_r"]
    return {
        "survives": survives,
        "reason": ("the confirm window does not contradict the fit window"
                   if survives else
                   "the confirm window's interval lies entirely below the "
                   "fit window's point estimate"),
        "fit": fit_score, "confirm": confirm_score,
        "fit_regime": fit_profile, "confirm_regime": confirm_profile,
    }


def _returns(decisions: list) -> list:
    return [d.outcome["r_multiple"] for d in decisions
            if getattr(d, "outcome", None)
            and d.outcome.get("r_multiple") is not None]


# ------------------------------------------------------------- the rules

def evaluate_axis(settings: list, baseline_decisions: list,
                  structurally_invalid: str = "") -> Verdict:
    """Apply the promotion protocol to a whole parameter axis.

    ``settings`` is a list of ``(variant_id, decisions)`` pairs - every point
    tested along one axis. The axis is the unit of decision, not the
    individual setting, because intention #4 is that a hypothesis is never
    rejected on one parameter value.
    """
    # Structural invalidity needs no sample at all. An idea that cannot work
    # for a stated reason is rejected on inspection and the reasoning is the
    # record.
    if structurally_invalid:
        return Verdict(
            REJECT, "structurally invalid",
            structurally_invalid,
            {"settings": len(settings)})

    baseline = score_returns(_returns(baseline_decisions), label="baseline")
    scored = [(vid, score_returns(_returns(d), label=vid), d)
              for vid, d in settings]

    if baseline["n"] == 0:
        return Verdict(
            INSUFFICIENT_SAMPLE, "no baseline",
            "the baseline variant has no resolved round trips, so there is "
            "nothing to compare against",
            {"baseline_n": 0})

    # Checked before anything reduces over the settings, so an axis with
    # nothing on it yet is a verdict rather than an exception.
    if len(scored) < MIN_AXIS_SETTINGS:
        return Verdict(
            CONTINUE, "too few settings on the axis",
            f"{len(scored)} of {MIN_AXIS_SETTINGS} required settings "
            "tested; a hypothesis is never decided on one parameter value",
            {"settings": len(scored)})

    best_id, best_score, best_decisions = max(
        scored, key=lambda row: row[1]["expectancy_r"])

    if best_score["n"] < MIN_ROUND_TRIPS:
        return Verdict(
            INSUFFICIENT_SAMPLE, "sample below the promotion floor",
            f"best setting {best_id} has {best_score['n']} round trips, "
            f"{MIN_ROUND_TRIPS} required. MDE at this n is "
            f"{best_score['mde_r']:.4f}R",
            {"best": best_id, "n": best_score["n"],
             "mde_r": best_score["mde_r"]})

    # Rejection: every setting's upper bound below the baseline's point
    # estimate. The whole axis has to be bad, not just the one that was tried.
    if all(row[1]["ci_high"] < baseline["expectancy_r"] for row in scored):
        return Verdict(
            REJECT, "whole axis underperforms the baseline",
            f"all {len(scored)} settings have an expectancy interval whose "
            f"upper bound is below the baseline point estimate "
            f"({baseline['expectancy_r']:+.4f}R)",
            {"settings": len(scored),
             "baseline_expectancy_r": baseline["expectancy_r"]})

    if best_score["ci_low"] <= baseline["expectancy_r"]:
        return Verdict(
            CONTINUE, "delta is inside the interval",
            f"best setting {best_id} has a lower bound of "
            f"{best_score['ci_low']:+.4f}R against a baseline point estimate "
            f"of {baseline['expectancy_r']:+.4f}R",
            {"best": best_id, "ci_low": best_score["ci_low"]})

    if best_score["max_drawdown_r"] > baseline["max_drawdown_r"]:
        return Verdict(
            CONTINUE, "drawdown exceeds the baseline",
            f"best setting {best_id} draws down "
            f"{best_score['max_drawdown_r']:.4f}R against the baseline's "
            f"{baseline['max_drawdown_r']:.4f}R; a better expectancy bought "
            "with a deeper hole is not an improvement",
            {"best": best_id})

    split = out_of_sample(best_decisions)
    if not split["survives"]:
        return Verdict(
            CONTINUE, "did not survive the out-of-sample split",
            f"{split['reason']}. Fit window regime "
            f"{split['fit_regime']['median_vol_ratio']}, confirm window "
            f"{split['confirm_regime']['median_vol_ratio']}",
            {"best": best_id, "split": split})

    difference = bootstrap_difference(
        _returns(best_decisions), _returns(baseline_decisions))
    return Verdict(
        PROMOTE, "every promotion criterion holds",
        f"{best_id}: {best_score['n']} round trips, expectancy "
        f"{best_score['expectancy_r']:+.4f}R "
        f"[{best_score['ci_low']:+.4f},{best_score['ci_high']:+.4f}], "
        f"delta vs baseline {difference}, survived the out-of-sample split",
        {"best": best_id, "n": best_score["n"], "split": split,
         "regime_comparable": split["fit_regime"].get("comparable")})


def correct_family(results: dict, alpha: float = 0.05) -> dict:
    """Apply the family-wise correction across one batch of tests.

    Six hypotheses with several conditioning cells each, against a few
    hundred round trips: something will look significant. The corrected
    figure is the only one any recommendation may quote, so the correction is
    applied here rather than left to whoever writes the summary.

    A p-value is approximated from each bucket's bootstrap interval, which is
    coarse and is the honest resolution available at this sample size. It is
    used to rank and to correct, never quoted as a p-value on its own.
    """
    pseudo_p = {}
    for name, scored in results.items():
        n = scored.get("n") or 0
        if n < 2:
            pseudo_p[name] = 1.0
            continue
        width = abs(scored["ci_high"] - scored["ci_low"])
        if width <= 0:
            pseudo_p[name] = 1.0
            continue
        # Distance of the interval from zero, in interval half-widths. Two
        # half-widths out is roughly the 95% boundary.
        margin = min(abs(scored["ci_low"]), abs(scored["ci_high"]))
        crosses_zero = scored["ci_low"] <= 0 <= scored["ci_high"]
        pseudo_p[name] = 1.0 if crosses_zero else max(
            1e-6, 0.05 / (1.0 + 4.0 * margin / width))

    corrected = holm_bonferroni(pseudo_p, alpha=alpha)
    out = {}
    for name, scored in results.items():
        row = dict(scored)
        row["p_uncorrected"] = corrected[name]["p"]
        row["p_adjusted"] = corrected[name]["p_adjusted"]
        row["significant_corrected"] = corrected[name]["significant"]
        # An uncorrected claim of significance is exactly what the family
        # correction exists to suppress, so it is not carried forward.
        row["verdict"] = (
            "SCORED" if corrected[name]["significant"]
            else INSUFFICIENT_SAMPLE if (scored.get("n") or 0) < 100
            else "NO_EFFECT_AT_FAMILY_ALPHA")
        out[name] = row
    return out
