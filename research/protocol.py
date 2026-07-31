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

from agent.forward_models import require_validated

from .score import score_returns
from .stats import (INSUFFICIENT_SAMPLE,
                    PAIRED_SIGN_FLIP_NULL_ASSUMPTION,
                    cluster_block_bootstrap_difference, holm_bonferroni,
                    paired_cluster_sign_flip)


PROMOTE = "PROMOTE"
REJECT = "REJECT"
CONTINUE = "CONTINUE"

MIN_ROUND_TRIPS = 100
MIN_AXIS_SETTINGS = 3
OUT_OF_SAMPLE_FRACTION = 0.7
MIN_PAIR_COVERAGE_PCT = 80.0
MIN_PAIRED_FIT_OBSERVATIONS = 70
MIN_PAIRED_CONFIRM_OBSERVATIONS = 30
PAIR_BOOTSTRAP_KIND = "paired_cluster_block"
PAIR_CLUSTER_SECONDS = 21_600
PAIR_RANDOMIZATION_KIND = "paired_cluster_sign_flip"
PAIR_RANDOMIZATION_NULL_ASSUMPTION = PAIRED_SIGN_FLIP_NULL_ASSUMPTION
PAIR_RANDOMIZATION_SEED = 20260728
PAIR_RANDOMIZATION_RESAMPLES = 20_000
PAIR_RANDOMIZATION_EXACT_MAX_CLUSTERS = 16
# Independent six-hour market episodes required before a clustered interval is
# allowed to decide anything. The pair minimums above count trades, and trades
# inside one episode are close to one observation: a hundred of them would
# otherwise produce a *narrower* interval than ten spread across a month,
# which inverts the thing the clustered bootstrap exists to correct. Eight
# episodes is two days of distinct market conditions - the least that can
# separate an edge from one afternoon.
MIN_BOOTSTRAP_CLUSTERS = 8


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


def common_time_cutoff(
        populations: list[list], fraction: float = OUT_OF_SAMPLE_FRACTION,
        key=lambda item: getattr(item, "ts", 0.0)) -> float | None:
    """Return one calendar boundary shared by every experimental arm."""
    timestamps = sorted({float(key(item))
                         for population in populations for item in population})
    if len(timestamps) < 2:
        return None
    cut = int(len(timestamps) * fraction)
    cut = min(max(cut, 1), len(timestamps) - 1)
    return timestamps[cut]


def split_at_time(
        items: list, cutoff_ts: float,
        key=lambda item: getattr(item, "ts", 0.0)) -> tuple[list, list]:
    """Split at a fixed timestamp without leaking one episode across windows."""
    ordered = sorted(items, key=key)
    return ([item for item in ordered if float(key(item)) < cutoff_ts],
            [item for item in ordered if float(key(item)) >= cutoff_ts])


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
                  fraction: float = OUT_OF_SAMPLE_FRACTION,
                  cutoff_ts: float | None = None) -> dict:
    """Fit on the first 70% of the corpus, confirm on the last 30%.

    "Survives" means the confirm window's expectancy interval does not
    exclude the fit window's point estimate downward - that is, the confirm
    window is not evidence *against* what the fit window claimed. It is a
    deliberately weak bar, because at these samples a strict one would reject
    everything, and a bar nothing can pass carries no information.
    """
    fit, confirm = (split_at_time(decisions, cutoff_ts)
                    if cutoff_ts is not None
                    else split_by_time(decisions, fraction))
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
                "fit_regime": fit_profile, "confirm_regime": confirm_profile,
                "cutoff_ts": cutoff_ts}

    survives = confirm_score["ci_high"] >= fit_score["expectancy_r"]
    return {
        "survives": survives,
        "reason": ("the confirm window does not contradict the fit window"
                   if survives else
                   "the confirm window's interval lies entirely below the "
                   "fit window's point estimate"),
        "fit": fit_score, "confirm": confirm_score,
        "fit_regime": fit_profile, "confirm_regime": confirm_profile,
        "cutoff_ts": cutoff_ts,
    }


def _returns(decisions: list) -> list:
    return [d.outcome["r_multiple"] for d in decisions
            if getattr(d, "outcome", None)
            and d.outcome.get("r_multiple") is not None]


def paper_trade_decisions(rows: list) -> list:
    """Rebuild exact actions from persisted proposal or legacy trade rows.

    A veto is an observed zero-return action, not missing data. This matters
    for confidence, exposure and discriminator axes: their edge is precisely
    whether accepting a proposal the baseline rejected adds value.
    """
    from .replay import ReplayDecision

    decisions = []
    for row in rows:
        is_ledger = "decision_outcome" in row
        decision_outcome = (str(row.get("decision_outcome") or "").upper()
                            if is_ledger else "PROPOSED")
        stage = "executed" if decision_outcome == "PROPOSED" else "vetoed"
        decision = ReplayDecision(
            cycle_id=row.get("cycle_id"),
            ts=float(row["decision_ts"] if is_ledger else row["entry_ts"]),
            symbol=str(row["symbol"]),
            signal_ts=(int(row["signal_ts"])
                       if row.get("signal_ts") is not None else None),
            stage=stage, direction=row.get("direction"),
            setup_type=row.get("setup_type"), contract_passed=True,
            proposal_id=row.get("proposal_id"))
        if decision_outcome == "VETOED":
            decision.outcome = {"r_multiple": 0.0, "result": "vetoed"}
        elif is_ledger and row.get("trade_status") == "CLOSED" \
                and int(row.get("trade_valid_for_inference", 1) or 0) == 1 \
                and row.get("trade_r_multiple") is not None:
            decision.outcome = {
                "r_multiple": float(row["trade_r_multiple"]),
                "result": row.get("trade_result"),
            }
        elif (not is_ledger and row.get("status") == "CLOSED"
              and int(row.get("valid_for_inference", 1) or 0) == 1
              and row.get("r_multiple") is not None):
            decision.outcome = {
                "r_multiple": float(row["r_multiple"]),
                "result": row.get("result"),
            }
        decisions.append(decision)
    return decisions


def paired_arm_comparison(
        left: list, right: list, *, include_randomization: bool = True) -> dict:
    """Match two research arms on exact proposal identity before inference."""
    def indexed(decisions: list) -> tuple[dict, set, list]:
        resolved: dict = {}
        proposed = set()
        duplicates = []
        for decision in decisions:
            if (not getattr(decision, "contract_passed", False)
                    and getattr(decision, "stage", None) != "executed"):
                continue
            key = decision.proposal_key()
            proposed.add(key)
            outcome = getattr(decision, "outcome", None) or {}
            value = outcome.get("r_multiple")
            if value is None:
                continue
            if key in resolved:
                duplicates.append(key)
                resolved.pop(key, None)
                continue
            resolved[key] = (float(decision.ts), float(value))
        return resolved, proposed, duplicates

    left_resolved, left_proposed, left_duplicates = indexed(left)
    right_resolved, right_proposed, right_duplicates = indexed(right)
    common = sorted(
        set(left_resolved) & set(right_resolved), key=repr)
    pairs = [
        (max(left_resolved[key][0], right_resolved[key][0]),
         left_resolved[key][1], right_resolved[key][1])
        for key in common
    ]
    interval = cluster_block_bootstrap_difference(pairs)
    union = left_proposed | right_proposed
    result = {
        "interval": interval,
        "paired_n": len(common),
        "proposal_union_n": len(union),
        "pair_coverage_pct": (len(common) / len(union) * 100.0
                              if union else 100.0),
        "left_only_proposals": len(left_proposed - right_proposed),
        "right_only_proposals": len(right_proposed - left_proposed),
        "left_unresolved": len(left_proposed - set(left_resolved)),
        "right_unresolved": len(right_proposed - set(right_resolved)),
        "left_duplicates": len(left_duplicates),
        "right_duplicates": len(right_duplicates),
        "mismatch_examples": {
            "left_only": [list(key) for key in sorted(
                left_proposed - right_proposed, key=repr)[:10]],
            "right_only": [list(key) for key in sorted(
                right_proposed - left_proposed, key=repr)[:10]],
            "left_duplicates": [list(key) for key in left_duplicates[:10]],
            "right_duplicates": [list(key) for key in right_duplicates[:10]],
        },
        "bootstrap": {
            "kind": PAIR_BOOTSTRAP_KIND,
            "cluster_seconds": PAIR_CLUSTER_SECONDS,
            "seed": 20260728,
            "clusters": int(getattr(interval, "clusters", 0) or 0),
            "min_clusters": MIN_BOOTSTRAP_CLUSTERS,
        },
    }
    if include_randomization:
        result["randomization"] = paired_cluster_sign_flip(
            pairs, cluster_seconds=PAIR_CLUSTER_SECONDS,
            exact_max_clusters=PAIR_RANDOMIZATION_EXACT_MAX_CLUSTERS,
            iterations=PAIR_RANDOMIZATION_RESAMPLES,
            seed=PAIR_RANDOMIZATION_SEED)
    return result


def calibrated_p_value(record: object) -> float | None:
    """Return a validated raw p-value, or ``None`` when evidence is absent."""
    if not isinstance(record, dict):
        return None
    if (record.get("kind") != PAIR_RANDOMIZATION_KIND
            or record.get("null_assumption")
            != PAIR_RANDOMIZATION_NULL_ASSUMPTION
            or record.get("alternative") != "greater"
            or int(record.get("cluster_seconds") or 0)
            != PAIR_CLUSTER_SECONDS
            or int(record.get("clusters") or 0) < MIN_BOOTSTRAP_CLUSTERS
            or int(record.get("paired_n") or 0)
            < MIN_PAIRED_CONFIRM_OBSERVATIONS):
        return None
    method = record.get("method")
    exact = record.get("exact")
    resamples = int(record.get("resamples") or 0)
    seed = record.get("seed")
    if method == "exact_enumeration":
        if exact is not True or seed is not None or resamples <= 0:
            return None
    elif method == "monte_carlo":
        if exact is not False or resamples <= 0 or seed != PAIR_RANDOMIZATION_SEED:
            return None
    else:
        return None
    try:
        value = float(record["p_value"])
    except (KeyError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) and 0.0 <= value <= 1.0 else None


def paired_window_adequate(comparison: dict, minimum: int) -> bool:
    """Return whether one inference window has defensible paired evidence."""
    bootstrap = comparison.get("bootstrap") or {}
    interval = comparison.get("interval")
    return bool(
        int(comparison.get("paired_n") or 0) >= minimum
        and float(comparison.get("pair_coverage_pct") or 0)
        >= MIN_PAIR_COVERAGE_PCT
        and not int(comparison.get("left_duplicates") or 0)
        and not int(comparison.get("right_duplicates") or 0)
        and bootstrap.get("kind") == PAIR_BOOTSTRAP_KIND
        and int(bootstrap.get("cluster_seconds") or 0) == PAIR_CLUSTER_SECONDS
        and interval is not None
        and int(getattr(interval, "n", -1))
        == int(comparison.get("paired_n") or 0)
        # Enough independent market episodes for the width to mean something,
        # and never a collapsed interval: one cluster resamples to itself, so
        # its zero width is an absence of evidence that reads like proof.
        and int(bootstrap.get("clusters") or 0) >= MIN_BOOTSTRAP_CLUSTERS
        and not interval.is_degenerate()
    )


def _paired_evidence(comparison: dict) -> dict:
    return {key: value for key, value in comparison.items()
            if key != "interval"}


# ------------------------------------------------------------- the rules

def evaluate_axis(settings: list, baseline_decisions: list | None = None,
                  structurally_invalid: str = "",
                  strategy_id: str = "momentum", *,
                  calibrated_confirmation: bool = True) -> Verdict:
    """Apply the promotion protocol to a whole parameter axis.

    New evidence supplies ``(variant_id, decisions, baseline_decisions)`` so
    every serially rotated setting is compared only with its contemporaneous
    baseline. The legacy two-item form remains for immutable v2 analyses.
    """
    # Structural invalidity needs no sample at all. An idea that cannot work
    # for a stated reason is rejected on inspection and the reasoning is the
    # record.
    if structurally_invalid:
        return Verdict(
            REJECT, "structurally invalid",
            structurally_invalid,
            {"settings": len(settings)})

    require_validated(strategy_id)

    normalized = []
    per_setting_baselines = False
    for setting in settings:
        if len(setting) == 2:
            variant_id, decisions = setting
            setting_baseline = baseline_decisions or []
        elif len(setting) == 3:
            variant_id, decisions, setting_baseline = setting
            per_setting_baselines = True
        else:
            raise ValueError(
                "axis settings must contain variant, decisions, and optional "
                "contemporaneous baseline")
        normalized.append({
            "variant_id": variant_id,
            "decisions": decisions,
            "baseline_decisions": setting_baseline,
            "score": score_returns(_returns(decisions), label=variant_id),
            "baseline_score": score_returns(
                _returns(setting_baseline), label=f"{variant_id}:baseline"),
        })

    if per_setting_baselines:
        missing = [row["variant_id"] for row in normalized
                   if row["baseline_score"]["n"] == 0]
        if missing:
            return Verdict(
                INSUFFICIENT_SAMPLE, "no contemporaneous baseline",
                "one or more settings have no resolved baseline round trips "
                "from their own assignment windows",
                {"settings_without_baseline": missing})
    else:
        baseline = score_returns(
            _returns(baseline_decisions or []), label="baseline")
        if baseline["n"] == 0:
            return Verdict(
                INSUFFICIENT_SAMPLE, "no baseline",
                "the baseline variant has no resolved round trips, so there is "
                "nothing to compare against",
                {"baseline_n": 0})

    # Checked before anything reduces over the settings, so an axis with
    # nothing on it yet is a verdict rather than an exception.
    total_settings = len(normalized) + 1  # the explicit baseline is one setting
    if total_settings < MIN_AXIS_SETTINGS:
        return Verdict(
            CONTINUE, "too few settings on the axis",
            f"{total_settings} of {MIN_AXIS_SETTINGS} required settings "
            "tested; a hypothesis is never decided on one parameter value",
            {"settings": total_settings,
             "candidate_settings": len(normalized)})

    # Select on the fit window only. Selecting on the full corpus and then
    # calling the last 30% "confirmation" lets that confirmation window pick
    # its own winner.
    # Each serial setting owns a distinct assignment calendar. Its fit/confirm
    # boundary therefore comes from its own contemporaneous baseline. Legacy
    # v2 populations retain their one shared baseline boundary.
    shared_cutoff = (None if per_setting_baselines else
                     common_time_cutoff([baseline_decisions or []]))
    split_settings = []
    for row in normalized:
        vid = row["variant_id"]
        decisions = row["decisions"]
        setting_baseline = row["baseline_decisions"]
        split_cutoff = (common_time_cutoff([setting_baseline])
                        if per_setting_baselines else shared_cutoff)
        fit, confirm = (split_at_time(decisions, split_cutoff)
                        if split_cutoff is not None else (decisions, []))
        baseline_fit, baseline_confirm = (
            split_at_time(setting_baseline, split_cutoff)
            if split_cutoff is not None else (setting_baseline, []))
        fit_pair = paired_arm_comparison(
            fit, baseline_fit,
            include_randomization=calibrated_confirmation)
        row = {
            **row, "fit": fit, "confirm": confirm,
            "baseline_fit": baseline_fit,
            "baseline_confirm": baseline_confirm,
            "fit_score": score_returns(_returns(fit), label=f"{vid}:fit"),
            "fit_pair": fit_pair, "split_cutoff": split_cutoff,
        }
        row["selection_score"] = (
            fit_pair["interval"].point if per_setting_baselines
            else row["fit_score"]["expectancy_r"])
        split_settings.append(row)
    best = max(split_settings, key=lambda row: row["selection_score"])
    best_id = best["variant_id"]
    best_score = best["score"]
    best_decisions = best["decisions"]
    best_fit = best["fit"]
    best_confirm = best["confirm"]
    baseline = best["baseline_score"]
    baseline_decisions = best["baseline_decisions"]
    baseline_fit = best["baseline_fit"]
    baseline_confirm = best["baseline_confirm"]
    split_cutoff = best["split_cutoff"]

    if best_score["n"] < MIN_ROUND_TRIPS:
        return Verdict(
            INSUFFICIENT_SAMPLE, "sample below the promotion floor",
            f"best setting {best_id} has {best_score['n']} round trips, "
            f"{MIN_ROUND_TRIPS} required. MDE at this n is "
            f"{best_score['mde_r']:.4f}R",
            {"best": best_id, "n": best_score["n"],
             "mde_r": best_score["mde_r"]})

    # Rejection: every setting's upper bound below the baseline's point
    # estimate, with an adequate sample for every pre-registered setting. A
    # nearly empty grid point is an open question, not evidence against the
    # whole axis.
    all_underperform = all(
        row["score"]["ci_high"]
        < row["baseline_score"]["expectancy_r"]
        for row in normalized)
    if all_underperform and any(
            row["score"]["n"] < MIN_ROUND_TRIPS for row in normalized):
        return Verdict(
            INSUFFICIENT_SAMPLE, "axis settings below the rejection floor",
            "every setting currently underperforms, but at least one has "
            f"fewer than {MIN_ROUND_TRIPS} resolved round trips; the whole "
            "axis cannot be rejected on an under-observed setting",
            {"settings": {row["variant_id"]: row["score"]["n"]
                          for row in normalized}})
    if all_underperform:
        baseline_detail = (
            "their contemporaneous baseline point estimates"
            if per_setting_baselines else
            f"the baseline point estimate ({baseline['expectancy_r']:+.4f}R)")
        return Verdict(
            REJECT, "whole axis underperforms the baseline",
            f"all {len(normalized)} settings have an expectancy interval whose "
            f"upper bound is below {baseline_detail}",
            {"settings": len(normalized),
             "baseline_expectancy_r": (
                 None if per_setting_baselines
                 else baseline["expectancy_r"])})

    full_pair = paired_arm_comparison(
        best_decisions, baseline_decisions,
        include_randomization=calibrated_confirmation)
    if not paired_window_adequate(full_pair, MIN_ROUND_TRIPS):
        return Verdict(
            INSUFFICIENT_SAMPLE, "paired proposal coverage is inadequate",
            f"fit-selected setting {best_id} has {full_pair['paired_n']} "
            f"resolved pairs covering {full_pair['pair_coverage_pct']:.1f}% "
            f"of the proposal union across "
            f"{full_pair['bootstrap']['clusters']} market episodes; promotion "
            f"requires {MIN_ROUND_TRIPS} pairs, "
            f"{MIN_PAIR_COVERAGE_PCT:.0f}% coverage, "
            f"{MIN_BOOTSTRAP_CLUSTERS} episodes, and no duplicate proposal "
            "identities",
            {"best": best_id, "paired": _paired_evidence(full_pair)})

    fit_pair = best["fit_pair"]
    if not paired_window_adequate(
            fit_pair, MIN_PAIRED_FIT_OBSERVATIONS):
        return Verdict(
            INSUFFICIENT_SAMPLE, "fit-window paired evidence is inadequate",
            f"fit-selected setting {best_id} has {fit_pair['paired_n']} "
            f"resolved fit pairs covering {fit_pair['pair_coverage_pct']:.1f}% "
            f"of the fit proposal union across "
            f"{fit_pair['bootstrap']['clusters']} market episodes; promotion "
            f"requires {MIN_PAIRED_FIT_OBSERVATIONS} pairs, "
            f"{MIN_PAIR_COVERAGE_PCT:.0f}% coverage, "
            f"{MIN_BOOTSTRAP_CLUSTERS} episodes, no duplicates, and the "
            "registered dependence-aware bootstrap",
            {"best": best_id, "fit_paired": _paired_evidence(fit_pair)})
    fit_difference = fit_pair["interval"]
    if fit_difference.n == 0 or fit_difference.low <= 0:
        return Verdict(
            CONTINUE, "fit-window delta is inside the interval",
            f"fit-selected setting {best_id} has paired fit delta "
            f"{fit_difference}; the entire dependence-aware interval must "
            "clear zero",
            {"best": best_id, "fit_delta": str(fit_difference),
             "fit_paired": _paired_evidence(fit_pair)})

    if best_score["max_drawdown_r"] > baseline["max_drawdown_r"]:
        return Verdict(
            CONTINUE, "drawdown exceeds the baseline",
            f"best setting {best_id} draws down "
            f"{best_score['max_drawdown_r']:.4f}R against the baseline's "
            f"{baseline['max_drawdown_r']:.4f}R; a better expectancy bought "
            "with a deeper hole is not an improvement",
            {"best": best_id})

    split = out_of_sample(best_decisions, cutoff_ts=split_cutoff)
    if not split["survives"]:
        return Verdict(
            CONTINUE, "did not survive the out-of-sample split",
            f"{split['reason']}. Fit window regime "
            f"{split['fit_regime']['median_vol_ratio']}, confirm window "
            f"{split['confirm_regime']['median_vol_ratio']}",
            {"best": best_id, "split": split})

    confirm_pair = paired_arm_comparison(
        best_confirm, baseline_confirm,
        include_randomization=calibrated_confirmation)
    if not paired_window_adequate(
            confirm_pair, MIN_PAIRED_CONFIRM_OBSERVATIONS):
        return Verdict(
            INSUFFICIENT_SAMPLE,
            "confirmation-window paired evidence is inadequate",
            f"fit-selected setting {best_id} has {confirm_pair['paired_n']} "
            f"resolved confirmation pairs covering "
            f"{confirm_pair['pair_coverage_pct']:.1f}% of the confirmation "
            f"proposal union across "
            f"{confirm_pair['bootstrap']['clusters']} market episodes; "
            f"promotion requires {MIN_PAIRED_CONFIRM_OBSERVATIONS} pairs, "
            f"{MIN_PAIR_COVERAGE_PCT:.0f}% coverage, "
            f"{MIN_BOOTSTRAP_CLUSTERS} episodes, no duplicates, and the "
            "registered dependence-aware bootstrap",
            {"best": best_id, "split": split,
             "confirmation_paired": _paired_evidence(confirm_pair)})
    confirm_difference = confirm_pair["interval"]
    confirmation_test = confirm_pair.get("randomization")
    if confirm_difference.n == 0 or confirm_difference.low <= 0:
        statistical = ({"confirmation_test": confirmation_test}
                       if calibrated_confirmation else {})
        return Verdict(
            CONTINUE, "confirmation delta does not clear zero",
            f"fit-selected setting {best_id} has confirmation delta vs "
            f"baseline {confirm_difference}; promotion requires the entire "
            "confirmation interval to be positive",
            {"best": best_id, "split": split,
             "confirm_delta": str(confirm_difference),
             "confirmation_paired": _paired_evidence(confirm_pair),
             **statistical})

    difference = full_pair["interval"]
    criteria = {
        "min_round_trips": best_score["n"] >= MIN_ROUND_TRIPS,
        "min_axis_settings": total_settings >= MIN_AXIS_SETTINGS,
        "paired_sample": full_pair["paired_n"] >= MIN_ROUND_TRIPS,
        "paired_coverage": (
            full_pair["pair_coverage_pct"] >= MIN_PAIR_COVERAGE_PCT),
        "no_duplicate_proposals": not (
            full_pair["left_duplicates"] or full_pair["right_duplicates"]),
        "paired_dependence_aware": paired_window_adequate(
            full_pair, MIN_ROUND_TRIPS),
        "fit_paired_sample": (
            fit_pair["paired_n"] >= MIN_PAIRED_FIT_OBSERVATIONS),
        "fit_paired_coverage": (
            fit_pair["pair_coverage_pct"] >= MIN_PAIR_COVERAGE_PCT),
        "fit_no_duplicate_proposals": not (
            fit_pair["left_duplicates"] or fit_pair["right_duplicates"]),
        "fit_dependence_aware": paired_window_adequate(
            fit_pair, MIN_PAIRED_FIT_OBSERVATIONS),
        "confirmation_paired_sample": (
            confirm_pair["paired_n"] >= MIN_PAIRED_CONFIRM_OBSERVATIONS),
        "confirmation_paired_coverage": (
            confirm_pair["pair_coverage_pct"] >= MIN_PAIR_COVERAGE_PCT),
        "confirmation_no_duplicate_proposals": not (
            confirm_pair["left_duplicates"]
            or confirm_pair["right_duplicates"]),
        "confirmation_dependence_aware": paired_window_adequate(
            confirm_pair, MIN_PAIRED_CONFIRM_OBSERVATIONS),
        "fit_interval_positive": fit_difference.low > 0,
        "drawdown_no_worse": (
            best_score["max_drawdown_r"] <= baseline["max_drawdown_r"]),
        "out_of_sample_survives": split["survives"] is True,
        "confirmation_interval_positive": confirm_difference.low > 0,
    }
    if calibrated_confirmation:
        criteria["confirmation_randomization_valid"] = (
            calibrated_p_value(confirmation_test) is not None)
    statistical = ({"confirmation_test": confirmation_test}
                   if calibrated_confirmation else {})
    return Verdict(
        PROMOTE, "every promotion criterion holds",
        f"{best_id}: {best_score['n']} round trips, expectancy "
        f"{best_score['expectancy_r']:+.4f}R "
        f"[{best_score['ci_low']:+.4f},{best_score['ci_high']:+.4f}], "
        f"delta vs baseline {difference}, survived the out-of-sample split",
        {"best": best_id, "n": best_score["n"], "split": split,
         "axis_settings": total_settings, "criteria": criteria,
         "selection_window": "fit", "confirm_delta": str(confirm_difference),
         "split_cutoff_ts": split_cutoff,
         "fit_interval": {
             "point": fit_difference.point, "low": fit_difference.low,
             "high": fit_difference.high, "n": fit_difference.n},
         "confirmation_interval": {
             "point": confirm_difference.point, "low": confirm_difference.low,
             "high": confirm_difference.high, "n": confirm_difference.n},
         "regime_comparable": split["fit_regime"].get("comparable"),
         "paired": _paired_evidence(full_pair),
         "fit_paired": _paired_evidence(fit_pair),
         "confirmation_paired": _paired_evidence(confirm_pair),
         **statistical})
def correct_axis_family(verdicts: dict, alpha: float = 0.05) -> dict:
    """Holm-correct across every axis evaluated in one qualification run.

    Criterion 10, and the reason it exists: each axis is a separate chance to
    promote something. Five axes are registered today and the count grows with
    the registry, so "the entire confirmation interval is positive" is a 5%
    test performed five times, and the axis that promotes is by construction
    the one that looked best. Correcting per axis and stopping there leaves the
    multiplicity that actually threatens the programme uncorrected.

    The family is every axis evaluated in the run, not only the axes that
    reached PROMOTE. An axis that failed earlier still consumed a chance, and
    excluding it would shrink the family precisely when a candidate needs the
    family to be small - which is the same error as reporting the best of
    seventy-nine walk-forward variants without saying there were seventy-nine.

    The corrected figure is the calibrated one-sided cluster sign-flip p-value
    from the confirmation window. A missing or malformed test fails closed at
    p=1; confidence-interval geometry is never converted into a p-value.
    """
    raw_p = {}
    records = {}
    for axis_id, verdict in verdicts.items():
        record = (getattr(verdict, "evidence", None) or {}).get(
            "confirmation_test")
        value = calibrated_p_value(record)
        raw_p[axis_id] = 1.0 if value is None else value
        records[axis_id] = record if value is not None else None
    corrected = holm_bonferroni(raw_p, alpha=alpha)
    return {
        axis_id: {
            "family_n": len(raw_p),
            "alpha": alpha,
            "axes": sorted(raw_p),
            "p": row["p"],
            "p_adjusted": row["p_adjusted"],
            "significant": bool(row["significant"]),
            "calibrated": records[axis_id] is not None,
            "method": ((records[axis_id] or {}).get("method")
                       or "missing_or_invalid"),
            "null_assumption": ((records[axis_id] or {}).get(
                "null_assumption") if records[axis_id] is not None else None),
            "exact": ((records[axis_id] or {}).get("exact")
                      if records[axis_id] is not None else None),
        }
        for axis_id, row in corrected.items()
    }


def apply_axis_family_correction(verdicts: dict, alpha: float = 0.05) -> dict:
    """Return the verdicts with criterion 10 applied.

    A promotion that does not survive the correction becomes ``CONTINUE``: the
    axis is not refuted, it simply has not cleared a bar that accounts for how
    many axes were asked the same question. Every verdict carries the family
    record either way, so the reason is legible in the persisted analysis
    rather than reconstructed from the axis count later.
    """
    family = correct_axis_family(verdicts, alpha=alpha)
    out = {}
    for axis_id, verdict in verdicts.items():
        record = family[axis_id]
        evidence = dict(getattr(verdict, "evidence", None) or {})
        evidence["family"] = record
        if verdict.verdict == PROMOTE and not record["significant"]:
            out[axis_id] = Verdict(
                CONTINUE, "family-wise correction across the axes tested",
                f"{verdict.detail} — but {record['family_n']} axes were "
                f"evaluated in this run and the Holm-adjusted figure is "
                f"{record['p_adjusted']:.3f} against alpha {alpha}; the "
                "corrected figure is the only one a recommendation may quote",
                evidence)
            continue
        out[axis_id] = Verdict(verdict.verdict, verdict.governing_criterion,
                               verdict.detail, evidence)
    return out


def correct_family(results: dict, alpha: float = 0.05) -> dict:
    """Apply the family-wise correction across one batch of tests.

    Callers must provide a calibrated ``p_value``. A score summary or interval
    alone is not a test and therefore fails closed at p=1.
    """

    raw_p = {}
    for name, scored in results.items():
        try:
            value = float(scored.get("p_value"))
        except (TypeError, ValueError):
            value = 1.0
        raw_p[name] = value if math.isfinite(value) and 0 <= value <= 1 else 1.0

    corrected = holm_bonferroni(raw_p, alpha=alpha)
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
