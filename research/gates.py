"""The seven evidence gates, and the confidence tier they produce.

A backtest that made money is not an edge. An edge is a claim that survives
every attempt to explain it away, so each gate here is one such attempt. They
run in the order that kills candidates cheapest-first, and each returns the
number it decided on rather than a bare pass/fail - a gate that only says
"failed" cannot be argued with or learned from.

    1. beat_nulls      random timing, random direction, inverted signal
    2. survive_costs   net of realistic costs, plus the breakeven cost
    3. survive_oos     purged walk-forward, discover on 60%, confirm on 40%
    4. survive_placebo destroy the information, re-run the identical procedure
    5. has_mechanism   a stated payer and a stated falsification test
    6. mechanism_is_the_source the claimed mechanism explains the return
    7. is_detectable   effect large enough to confirm in achievable time

Gate 4 is the one that matters most and the one most often skipped. This
project has already watched it kill a candidate that passed everything else:
+1.297% out-of-sample at t=2.93, 8 of 9 quarters positive, beta-neutral - and
its placebo, run on deliberately destroyed information, still scored +0.455%
at t=2.37. Whatever produced that number, it was the method, not the market.

Which is why no gate here tests a t-statistic. On this data, with overlapping
windows and clustered returns, a placebo reached t=2.60 from pure noise.
``t > 2`` is not evidence here; the placebo ratio is.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from agent.registry import TIERS as TIER_ORDER

try:  # package imports are used by research.tournament and external callers
    from .edge_lab import (COST_SCENARIOS, Costs, build_trades,
                           non_overlapping, summarize)
    from .protocol import MIN_AXIS_SETTINGS
except ImportError:  # direct script/test imports keep working
    from edge_lab import (COST_SCENARIOS, Costs, build_trades,
                          non_overlapping, summarize)
    try:
        from research.protocol import MIN_AXIS_SETTINGS
    except ImportError:
        # A direct `python research/gates.py` invocation has no package
        # parent. Keep the shared protocol value visible without creating a
        # second import graph just for that invocation.
        MIN_AXIS_SETTINGS = 3

MIN_SETTINGS_TO_REJECT = MIN_AXIS_SETTINGS


# Discover on the first 60%, confirm on the last 40%. The purge gap drops
# trades straddling the boundary so a position opened in-sample cannot close
# out-of-sample and leak.
WALK_FORWARD_SPLIT = 0.6
PURGE_DAYS = 3

# A candidate whose placebo scores this share of its own result is
# indistinguishable from its own methodology.
PLACEBO_FAIL_RATIO = 0.50
PLACEBO_PASS_RATIO = 0.25

# The highest tier this battery may award. It runs on reconstructed market
# data, so it can reject a strategy or nominate a candidate, but it cannot
# confirm the recorded decision system - that needs G2-clean journal replay
# and a persisted confirmation result. Everything above this line is outside
# the battery's authority: it can neither grant those tiers nor take them
# away, and a consumer comparing a measured tier against a registered one has
# to know where that authority stops.
EXPLORATORY_CEILING = "T2_CANDIDATE"

# Detectability targets: 95% confidence, 80% power.
Z_ALPHA, Z_POWER = 1.96, 0.84
# Beyond this, no forward test of a reasonable length can confirm the effect.
MAX_TRADES_TO_CONFIRM = 3000


@dataclass
class GateResult:
    name: str
    passed: bool
    summary: str
    numbers: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"gate": self.name, "passed": bool(self.passed),
                "summary": self.summary, "numbers": self.numbers}


def _expectancy(trades: pd.DataFrame) -> float:
    if trades is None or trades.empty:
        return float("nan")
    return float(trades["net_pct"].mean())


def _finite(value: float) -> bool:
    return value == value and math.isfinite(value)


def walk_forward_masks(trades: pd.DataFrame,
                       split: float = WALK_FORWARD_SPLIT,
                       purge_days: int = PURGE_DAYS):
    """In-sample and out-of-sample masks with a purge gap between them."""
    if trades.empty:
        return pd.Series(dtype=bool), pd.Series(dtype=bool)
    start, end = trades["entry_ts"].min(), trades["entry_ts"].max()
    cut = start + (end - start) * split
    purge = purge_days * 86_400_000
    return trades["entry_ts"] <= cut, trades["entry_ts"] > cut + purge


# Gates

def has_mechanism(spec) -> GateResult:
    """Gate 5. Run first because it costs nothing and is disqualifying.

    A signal with no economic story cannot be told apart from overfitting,
    and - more practically - you will not know when it has stopped working,
    because you never knew why it worked.
    """
    mechanism = (spec.mechanism or "").strip()
    falsification = (spec.falsification or "").strip()
    # A mechanism must name a payer. "Prices tend to rise" is a description;
    # "leveraged longs are liquidated at market" is a mechanism.
    substantive = len(mechanism) >= 40 and len(falsification) >= 40
    return GateResult(
        "has_mechanism",
        bool(mechanism and falsification and substantive),
        ("mechanism and falsification criterion are both stated"
         if substantive else
         "mechanism or falsification criterion is missing or too thin"),
        {"mechanism_chars": len(mechanism),
         "falsification_chars": len(falsification)},
    )


def beat_nulls(frames, membership, contract, cost_name: str = "base",
               exit_policy: str = "fixed_rr") -> GateResult:
    """Gate 1. A result means nothing without knowing what nothing scores.

    Three nulls, because they fail differently: random timing keeps the
    instruments and the exit logic but destroys the entry moment; random
    direction keeps the moment but flips the side; inversion asks whether
    the signal is simply backwards.
    """
    costs = COST_SCENARIOS[cost_name]
    variants = {
        "signal": {},
        "inverted": {"flip_direction": True},
        "null_random_timing": {"random_timing": 202},
        "null_random_direction": {"random_direction": 101},
    }
    scores, stats = {}, {}
    for name, kwargs in variants.items():
        trades = non_overlapping(
            build_trades(frames, membership, contract, costs,
                         exit_policy=exit_policy, **kwargs))
        scores[name] = _expectancy(trades)
        stats[name] = {"trades": int(len(trades)),
                       "expectancy_pct": scores[name]}
    signal = scores["signal"]
    beaten = [name for name, value in scores.items()
              if name != "signal" and _finite(value) and value >= signal]
    passed = _finite(signal) and not beaten
    return GateResult(
        "beat_nulls", passed,
        (f"signal {signal:+.4f}% beats every null"
         if passed else
         f"signal {signal:+.4f}% does not beat: {', '.join(beaten)}"
         if beaten else "signal expectancy is undefined"),
        {"expectancy_pct": stats,
         "beaten_by": beaten},
    )


def survive_costs(frames, membership, contract,
                  exit_policy: str = "fixed_rr",
                  cost_name: str = "base") -> GateResult:
    """Gate 2. Report the breakeven cost, not the net at one assumption.

    Round-trip taker is 0.10% before spread, and measured gross spreads in
    this market run 0.05-0.3%. When the effect and the fee are the same size,
    a single cost assumption decides the answer, so the honest number is how
    much cost the edge can absorb before it dies.
    """
    results = {}
    scenarios = list(dict.fromkeys((
        "frictionless", "optimistic", "base", cost_name,
        "realistic_alt", "stress")))
    for name in scenarios:
        trades = non_overlapping(
            build_trades(frames, membership, contract, COST_SCENARIOS[name],
                         exit_policy=exit_policy))
        results[name] = _expectancy(trades)

    # Use the exact same explicit economics as the headline evaluation.  The
    # historical implementation silently judged survival at cheaper ``base``
    # costs while the report headline used the account's taker rate.
    headline_costs = COST_SCENARIOS[cost_name]
    execution_free = Costs(
        "execution_frictionless_same_funding", fee_per_side=0.0,
        entry_slippage=0.0, exit_slippage=0.0, stop_slippage=0.0,
        include_funding=headline_costs.include_funding,
        funding_model=headline_costs.funding_model)
    execution_free_trades = non_overlapping(build_trades(
        frames, membership, contract, execution_free,
        exit_policy=exit_policy))
    gross = _expectancy(execution_free_trades)
    results[execution_free.name] = gross
    round_trip_cost = (2 * headline_costs.fee_per_side
                       + headline_costs.entry_slippage
                       + headline_costs.exit_slippage)
    breakeven = gross if _finite(gross) else float("nan")
    headline_net = results.get(cost_name, float("nan"))
    passed = (_finite(headline_net) and headline_net > 0
              and _finite(breakeven)
              and breakeven > 2 * round_trip_cost)
    return GateResult(
        "survive_costs", passed,
        (f"net {headline_net:+.4f}% at {cost_name} costs, breakeven cost "
         f"{breakeven:.4f}% vs {round_trip_cost:.2f}% charged"
         if _finite(headline_net)
         else f"no trades at {cost_name} costs"),
        {"expectancy_pct_by_scenario": results,
         "breakeven_cost_pct": breakeven,
         "headline_cost_scenario": cost_name,
         "headline_fee_per_side_pct": headline_costs.fee_per_side,
         "headline_entry_slippage_pct": headline_costs.entry_slippage,
         "headline_exit_slippage_pct": headline_costs.exit_slippage,
         "headline_stop_slippage_pct": headline_costs.stop_slippage,
         "headline_round_trip_cost_pct": round_trip_cost,
         "breakeven_preserves_headline_funding": True,
         "required_headroom_x": 2.0},
    )


def survive_oos(frames, membership, contract,
                cost_name: str = "base",
                exit_policy: str = "fixed_rr") -> GateResult:
    """Gate 3. Discover on the first 60%, confirm on the last 40%.

    Reported together, always. An in-sample number alone is a description of
    the past; the pair is the only thing that carries information about
    whether the rule generalises.
    """
    trades = non_overlapping(
        build_trades(frames, membership, contract, COST_SCENARIOS[cost_name],
                     exit_policy=exit_policy))
    in_mask, out_mask = walk_forward_masks(trades)
    if trades.empty or not out_mask.any():
        return GateResult("survive_oos", False, "no out-of-sample trades", {})
    in_sample = _expectancy(trades[in_mask])
    out_sample = _expectancy(trades[out_mask])
    passed = _finite(out_sample) and out_sample > 0
    return GateResult(
        "survive_oos", passed,
        f"in-sample {in_sample:+.4f}%, out-of-sample {out_sample:+.4f}%",
        {"in_sample_pct": in_sample, "out_of_sample_pct": out_sample,
         "in_sample_trades": int(in_mask.sum()),
         "out_of_sample_trades": int(out_mask.sum()),
         "split": WALK_FORWARD_SPLIT, "purge_days": PURGE_DAYS},
    )


def survive_placebo(frames, membership, contract,
                    cost_name: str = "base",
                    exit_policy: str = "fixed_rr",
                    draws: int = 5) -> GateResult:
    """Gate 4. Destroy the information; run the identical procedure.

    The placebo here is random entry timing drawn from the same pool of bars
    where the same level derivation is well defined, so it differs from the
    signal in the entry moment alone - not in instrument, not in exit logic,
    not in data availability. A real edge sends this to zero. An artifact of
    the method reproduces itself, because the method is what was being
    measured.

    Several draws, because one placebo is itself a sample of one.
    """
    costs = COST_SCENARIOS[cost_name]
    real = _expectancy(non_overlapping(
        build_trades(frames, membership, contract, costs,
                     exit_policy=exit_policy)))
    placebo_scores = []
    for draw in range(draws):
        trades = non_overlapping(
            build_trades(frames, membership, contract, costs,
                         exit_policy=exit_policy,
                         random_timing=1000 + 137 * draw))
        placebo_scores.append(_expectancy(trades))
    usable = [v for v in placebo_scores if _finite(v)]
    placebo = float(np.mean(usable)) if usable else float("nan")

    # Ratio is only meaningful when the real result is positive; a negative
    # candidate has already failed earlier gates.
    if not _finite(real) or real <= 0:
        ratio = float("nan")
        passed = False
        summary = "candidate expectancy is not positive; placebo not decisive"
    else:
        ratio = (placebo / real) if _finite(placebo) else float("nan")
        passed = _finite(ratio) and ratio < PLACEBO_PASS_RATIO
        summary = (
            f"placebo {placebo:+.4f}% is {ratio:.0%} of candidate "
            f"{real:+.4f}%"
            + ("" if passed else
               f" (needs < {PLACEBO_PASS_RATIO:.0%})"))
    return GateResult(
        "survive_placebo", passed, summary,
        {"candidate_pct": real, "placebo_mean_pct": placebo,
         "placebo_draws": placebo_scores, "ratio": ratio,
         "pass_below": PLACEBO_PASS_RATIO, "fail_at_or_above":
             PLACEBO_FAIL_RATIO},
    )


def mechanism_is_the_source(frames, membership, contract, prereg,
                            cost_name: str = "base",
                            exit_policy: str = "fixed_rr") -> GateResult:
    """Does the money come from where the mechanism says it comes from?

    Gate 5 asks whether a mechanism was STATED. This asks whether it is
    TRUE - and the two come apart in practice. A strategy can post a large,
    null-beating, placebo-surviving result while its claimed return source
    contributes nothing, because the entry rule happens to select for
    something else entirely.

    That is not hypothetical. funding-carry posted +2.008% per trade on this
    data and passed every other gate. Decomposed: funding contributed
    +0.039% and price movement contributed +1.969%. It was a directional
    strategy wearing a carry label, and every number about it was true.

    Why this is disqualifying rather than a curiosity: a result whose source
    is not understood cannot be distinguished from overfitting, and you will
    not know when it stops working, because you never knew why it worked.
    That is the whole content of the "have a mechanism" requirement, and
    stating a mechanism that turns out to be the wrong one fails it.

    Declared per hypothesis via ``return_source`` in the pre-registration.
    Only sources that can be isolated are checked; anything else is reported
    as unchecked rather than silently passed.
    """
    source = (prereg or {}).get("return_source")
    if not source:
        return GateResult(
            "mechanism_is_the_source", True,
            "no isolable return source declared; not checked", {})

    if source not in {"funding", "price"}:
        return GateResult(
            "mechanism_is_the_source", True,
            f"return source {source!r} cannot be isolated by this harness; "
            f"not checked", {"declared_source": source})

    base = COST_SCENARIOS[cost_name]
    stripped = Costs(f"{base.name}_no_funding", base.fee_per_side,
                     base.entry_slippage, base.exit_slippage,
                     base.stop_slippage, False)
    with_source = _expectancy(non_overlapping(
        build_trades(frames, membership, contract, base,
                     exit_policy=exit_policy)))
    without_source = _expectancy(non_overlapping(
        build_trades(frames, membership, contract, stripped,
                     exit_policy=exit_policy)))
    if not _finite(with_source) or with_source == 0:
        return GateResult(
            "mechanism_is_the_source", False,
            "candidate has no measurable result to attribute",
            {"declared_source": source})
    funding_contribution = with_source - without_source
    funding_share = funding_contribution / with_source
    # A strategy claiming PRICE as its source must not turn out to be a
    # carry trade any more than the reverse. The check is symmetric because
    # the failure is symmetric: in both directions it means the result comes
    # from somewhere other than where the claim says.
    share = funding_share if source == "funding" else 1.0 - funding_share
    passed = share >= 0.5
    return GateResult(
        "mechanism_is_the_source", passed,
        (f"declared source {source!r} accounts for {share:.0%} of "
         f"{with_source:+.4f}% (funding {funding_contribution:+.4f}%, "
         f"price {without_source:+.4f}%)"
         + ("" if passed else
            " - the stated mechanism is not the source of the result")),
        {"declared_source": source,
         "total_pct": with_source,
         "funding_contribution_pct": funding_contribution,
         "price_contribution_pct": without_source,
         "source_share": share,
         "pass_at_or_above": 0.5},
    )


def is_detectable(frames, membership, contract,
                  cost_name: str = "base",
                  exit_policy: str = "fixed_rr",
                  trades_per_day: float = 3.0) -> GateResult:
    """Gate 6. Is the effect big enough to ever be confirmed?

    Measured dispersion, not an assumed one: n = (z_a + z_b)^2 * sd^2 / e^2.
    If a claimed edge needs more trades than a forward test can produce, then
    trading it is a decision to act on faith, and it should be labelled that
    way rather than validated.
    """
    trades = non_overlapping(
        build_trades(frames, membership, contract, COST_SCENARIOS[cost_name],
                     exit_policy=exit_policy))
    if trades.empty:
        return GateResult("is_detectable", False, "no trades", {})
    r = trades["r_multiple"].to_numpy(float)
    sd = float(np.nanstd(r, ddof=1))
    effect = float(np.nanmean(r))
    if not _finite(effect) or effect <= 0 or not _finite(sd) or sd <= 0:
        return GateResult(
            "is_detectable", False,
            f"effect {effect:+.4f} R is not positive; nothing to size for",
            {"effect_r": effect, "sd_r": sd})
    needed = int(math.ceil((Z_ALPHA + Z_POWER) ** 2 * sd ** 2 / effect ** 2))
    days = needed / trades_per_day if trades_per_day > 0 else float("inf")
    passed = needed <= MAX_TRADES_TO_CONFIRM
    return GateResult(
        "is_detectable", passed,
        f"{effect:+.4f} R needs {needed} trades (~{days / 30.4:.1f} months "
        f"at {trades_per_day:g}/day)",
        {"effect_r": effect, "sd_r": sd, "trades_needed": needed,
         "days_needed": days, "max_acceptable": MAX_TRADES_TO_CONFIRM,
         "observed_trades": int(len(trades))},
    )


# Tiering

def tier_from_gates(results: list[GateResult]) -> tuple[str, str]:
    """Map gate outcomes onto the register's confidence ladder.

    Deliberately conservative and deliberately mechanical: the tier is a
    function of the gates, so nobody has to decide how impressed to be.
    """
    by_name = {r.name: r for r in results}

    def ok(name: str) -> bool:
        return name in by_name and by_name[name].passed

    if not ok("has_mechanism"):
        return "T0_REJECTED", "no stated mechanism or falsification test"
    if not ok("beat_nulls"):
        return "T0_REJECTED", by_name["beat_nulls"].summary
    if not ok("survive_oos"):
        return "T1_HYPOTHESIS", by_name["survive_oos"].summary
    if not ok("survive_costs"):
        return "T2_CANDIDATE", by_name["survive_costs"].summary
    if not ok("survive_placebo"):
        # Failing the placebo outright is disqualifying, not merely a stall:
        # it means the result came from the procedure.
        placebo = by_name.get("survive_placebo")
        ratio = (placebo.numbers.get("ratio") if placebo else None)
        if ratio is not None and _finite(ratio) and ratio >= PLACEBO_FAIL_RATIO:
            return "T0_REJECTED", placebo.summary
        return "T2_CANDIDATE", placebo.summary if placebo else "placebo unrun"
    if not ok("mechanism_is_the_source"):
        # A result whose source is not what was claimed is a result nobody
        # understands. Gate 5's requirement is not "state something", it is
        # "know why this pays", and a falsified attribution fails it.
        return "T0_REJECTED", by_name["mechanism_is_the_source"].summary
    if not ok("is_detectable"):
        return "T2_CANDIDATE", by_name["is_detectable"].summary

    # Passing every gate on too few trades is not validation, it is a small
    # sample agreeing with itself. The detectability gate already computed
    # how many trades this effect size needs; if the run does not have them,
    # the result cannot be called validated no matter how clean it looks.
    #
    # This is not hypothetical. On a 200-day, 8-instrument window the
    # momentum benchmark beat its own nulls and posted a positive
    # out-of-sample half - on 24 months and 28 instruments it did neither.
    # Whichever way a thin sample points, it cannot carry a promotion.
    detect = by_name["is_detectable"].numbers
    observed = int(detect.get("observed_trades", 0))
    needed = int(detect.get("trades_needed", 0) or 0)
    if needed and observed < needed:
        return "T2_CANDIDATE", (
            f"cleared every gate but on {observed} trades against the "
            f"{needed} its own effect size requires; collect more before "
            f"calling this validated")

    # Exploratory evidence must never authorize capital merely because every
    # exploratory gate agrees; see EXPLORATORY_CEILING for why this stops here.
    return EXPLORATORY_CEILING, (
        "cleared every exploratory offline gate; authoritative recorded "
        "replay confirmation is still required for T3_VALIDATED")

def _structurally_failed(results: list[GateResult]) -> bool:
    """Did this setting fail for a reason no parameter value can fix?

    A missing mechanism or falsifier is a property of the hypothesis, not of
    the thresholds it was tested at, so it is rejected at any number of
    settings. Everything else the battery can fail - the nulls, the placebo,
    the attribution - is measured from data and could in principle come out
    differently at a different threshold, which is exactly why one setting
    must not be allowed to speak for the axis.
    """
    by_name = {r.name: r for r in results}
    return "has_mechanism" in by_name and not by_name["has_mechanism"].passed


def tier_from_settings(settings: list[dict],
                       min_settings: int = MIN_SETTINGS_TO_REJECT
                       ) -> tuple[str, str, dict]:
    """Decide a tier for the hypothesis, not for one parameterisation.

    ``settings`` is every pre-registered parameterisation of one hypothesis,
    each already scored by ``run_all`` and ``tier_from_gates``. The unit of
    decision is the whole set, because a mechanism rejected at one threshold
    has not been tested - it has been guessed at once. ``protocol.md``
    criterion 1 states this rule for the journal path and
    ``protocol.MIN_AXIS_SETTINGS`` is the same number: *a hypothesis is never
    killed by one badly chosen parameter value.*

    Two asymmetries, both deliberate and both inherited from that protocol:

    **Rejection needs the whole set.** Every setting must fail, and there must
    be at least ``min_settings`` of them. Fewer, and the verdict is
    ``T1_HYPOTHESIS`` with the reason stated: the question is open, not
    answered in the negative. This is the offline twin of
    ``INSUFFICIENT_SAMPLE``.

    **Promotion pays for the search.** The awarded tier comes from the best
    setting, and the best of *k* settings is the largest of *k* random numbers
    unless the search is charged for. So a tier above ``T1_HYPOTHESIS``
    additionally requires that setting to survive a Holm correction across the
    settings tested. Without it, adding parameterisations to protect a
    mechanism from a bad guess would quietly become a way to manufacture a
    candidate.
    """
    from research.protocol import correct_family

    if not settings:
        raise ValueError("a hypothesis needs at least one scored setting")

    corrected = correct_family({
        setting["setting_id"]: _significance_row(setting)
        for setting in settings})
    for setting in settings:
        setting["significant_corrected"] = bool(
            corrected[setting["setting_id"]]["significant_corrected"])
        setting["p_adjusted"] = corrected[setting["setting_id"]]["p_adjusted"]

    ranked = sorted(
        settings,
        key=lambda s: (TIER_ORDER.index(s["tier"]),
                       s["significant_corrected"],
                       _finite_or(s.get("headline", {}).get("expectancy_r"),
                                  -1e9)),
        reverse=True)
    best = ranked[0]
    tested = len(settings)

    if any(_structurally_failed(setting["results"]) for setting in settings):
        return "T0_REJECTED", best["why"], best

    if best["tier"] == "T0_REJECTED":
        if tested < min_settings:
            return "T1_HYPOTHESIS", (
                f"every one of {tested} tested parameterisation(s) failed, but "
                f"{min_settings} are required before the hypothesis itself may "
                f"be rejected: a mechanism killed at one threshold has been "
                f"guessed at once, not tested. Best failure: {best['why']}"
            ), best
        return "T0_REJECTED", (
            f"all {tested} pre-registered parameterisations fail, so this is "
            f"the mechanism and not one badly chosen threshold. "
            f"{best['setting_id']}: {best['why']}"), best

    if (TIER_ORDER.index(best["tier"]) > TIER_ORDER.index("T1_HYPOTHESIS")
            and tested > 1 and not best["significant_corrected"]):
        return "T1_HYPOTHESIS", (
            f"{best['setting_id']} reaches {best['tier']} on its gates, but it "
            f"is the best of {tested} parameterisations and does not survive "
            f"the Holm correction across them (p_adj "
            f"{best['p_adjusted']:.3f}). The search has to be paid for before "
            f"the winner counts"), best

    detail = best["why"]
    if tested > 1:
        detail = f"{best['setting_id']} of {tested} parameterisations: {detail}"
    return best["tier"], detail, best


def _significance_row(setting: dict) -> dict:
    """Return a calibrated test when one exists; otherwise correction refuses."""
    headline = setting.get("headline") or {}
    interval = headline.get("expectancy_r_ci95") or (float("nan"),) * 2
    return {"n": int(headline.get("trades") or 0),
            "ci_low": interval[0], "ci_high": interval[1],
            "p_value": headline.get("p_value")}


def _finite_or(value, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) else fallback




def run_all(spec, frames, membership, contract,
            cost_name: str = "base",
            exit_policy: str = "fixed_rr",
            prereg: dict | None = None) -> list[GateResult]:
    """Run the battery cheapest-disqualifier first."""
    results = [has_mechanism(spec)]
    if not results[0].passed:
        return results
    results.append(beat_nulls(frames, membership, contract, cost_name,
                              exit_policy))
    results.append(survive_oos(frames, membership, contract, cost_name,
                               exit_policy))
    results.append(survive_costs(
        frames, membership, contract, exit_policy, cost_name=cost_name))
    results.append(survive_placebo(frames, membership, contract, cost_name,
                                   exit_policy))
    results.append(mechanism_is_the_source(frames, membership, contract,
                                           prereg, cost_name, exit_policy))
    results.append(is_detectable(frames, membership, contract, cost_name,
                                 exit_policy))
    return results


def headline(frames, membership, contract, cost_name: str = "base",
             exit_policy: str = "fixed_rr") -> dict:
    """Summary statistics for the report, at the stated cost scenario."""
    trades = non_overlapping(
        build_trades(frames, membership, contract, COST_SCENARIOS[cost_name],
                     exit_policy=exit_policy))
    return summarize(trades, label=cost_name)
