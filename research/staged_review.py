"""Give a staged mechanism a coded verdict, so the next generation learns.

Registered variants get a terminal verdict from the rotation evaluator, with
reason codes the planner reads back. Machine-authored mechanisms had nothing
equivalent: they accumulated trades and were never adjudicated, so a proposer
asked for a new generation could see what was staged but not what had
happened to it. That produces the failure the whole loop exists to avoid -
reproposing a dead claim with a slightly different threshold.

The verdict here is deliberately thinner than the rotation evaluator's. That
one adjudicates a paired baseline-versus-candidate experiment against a dozen
adequacy gates. This one answers a narrower question: has this mechanism
earned more lane time, and if not, why not. The codes exist so "it fired and
lost money" and "it never got the data" never read the same to whatever asks
next.
"""

from __future__ import annotations

import time

from . import shortlist as shortlist_mod

# Retirement codes. The wording of each is what a proposer reads, so it says
# what to do differently rather than only what happened.
NEGATIVE_EXPECTANCY = "NEGATIVE_EXPECTANCY"
DIED_OUT_OF_SAMPLE = "DIED_OUT_OF_SAMPLE"
NEVER_FIRED = "NEVER_FIRED"
STARVED_OF_DATA = "STARVED_OF_DATA"

# Codes that keep a mechanism running. Collecting is not a verdict.
COLLECTING = "COLLECTING"
SUPPORTED = "SUPPORTED"

# The funnel. Applying the strictest adequacy gate from the first bar is why
# nothing ever finished: a candidate needed a hundred paired observations
# before anything could be said about it, so an obviously dead mechanism
# occupied a lane for weeks to establish what thirty trades already showed.
#
# SCREEN is cheap and decisive only in one direction. It can retire a
# mechanism that is clearly losing, and it can never promote one: passing a
# screen means "not yet excluded", which is not evidence of an edge.
SCREEN, MEASURE, CONFIRM = "SCREEN", "MEASURE", "CONFIRM"
SCREEN_FLOOR = 30
MEASURE_FLOOR = 100
# At the screen stage a mechanism is retired only when the whole interval is
# clearly adverse, not merely negative. A borderline loser at n=30 is exactly
# the case where a small sample misleads, so it is given the longer look.
SCREEN_RETIRE_CI_HIGH = -0.10

# A mechanism that has never fired despite this many evaluated decisions is
# not selective, it is unreachable: its thresholds do not describe a state
# this market reaches. Retiring it frees the lane; the claim stays recorded.
NEVER_FIRED_DECISION_FLOOR = 2_000
# Below this, an absent result is far more likely to be missing data than an
# unreachable threshold, so it is never held against the claim.
STARVED_FRACTION = 0.5


def stage_of(candidate) -> str:
    """Which funnel stage a candidate's evidence currently supports."""
    if candidate.trades >= MEASURE_FLOOR:
        return CONFIRM
    if candidate.trades >= SCREEN_FLOOR:
        return MEASURE
    return SCREEN


def verdict_for(candidate) -> tuple[str, str]:
    """Return ``(code, explanation)`` for one measured staged mechanism."""
    evaluated = (getattr(candidate, "eligible_opportunities", 0)
                 or candidate.declined_decisions + candidate.trades)
    starved = candidate.starved_decisions
    total = evaluated + starved

    if total and starved / total >= STARVED_FRACTION:
        return STARVED_OF_DATA, (
            f"{starved} of {total} decisions were never evaluated because the "
            "market data was absent. This is a pipeline result, not a market "
            "one: the claim has not been tested and is kept staged.")

    stage = stage_of(candidate)
    if (stage == SCREEN and candidate.trades >= SCREEN_FLOOR // 2
            and candidate.ci_high <= SCREEN_RETIRE_CI_HIGH):
        return NEGATIVE_EXPECTANCY, (
            f"screened out at {candidate.trades} trades: mean "
            f"{candidate.mean_r:+.3f} R with the whole 95% interval "
            f"{candidate.ci_low:+.3f}..{candidate.ci_high:+.3f} clearly "
            f"adverse. Waiting for {MEASURE_FLOOR} would spend weeks of lane "
            "time to establish what this already shows.")

    if candidate.label == shortlist_mod.NEGATIVE:
        return NEGATIVE_EXPECTANCY, (
            f"fired {candidate.trades} times for a mean of "
            f"{candidate.mean_r:+.3f} R, whole 95% interval "
            f"{candidate.ci_low:+.3f}..{candidate.ci_high:+.3f} at or below "
            "zero. The mechanism does not pay at this parameterisation; a "
            "nearby threshold on the same fields is the same claim.")

    if (candidate.label == shortlist_mod.INCONCLUSIVE
            and candidate.confirmation_mean_r is not None
            and candidate.confirmation_mean_r <= 0
            and (candidate.fit_mean_r or 0) > 0):
        return DIED_OUT_OF_SAMPLE, (
            f"positive in the fitting window ({candidate.fit_mean_r:+.3f} R) "
            f"and negative held out ({candidate.confirmation_mean_r:+.3f} R). "
            "A result that does not survive its own out-of-sample window is "
            "the shape of an edge fitted to where it was found.")

    if (candidate.trades == 0
            and evaluated >= NEVER_FIRED_DECISION_FLOOR):
        return NEVER_FIRED, (
            f"evaluated {evaluated} times and never fired. "
            "The thresholds do not describe a state this market reaches, so "
            "the claim is unreachable rather than wrong. Propose the same "
            "mechanism at a condition the market actually visits.")

    # ``Candidate.label`` is normally produced by shortlist._label, but this
    # guard is intentionally repeated at the immutable staged-verdict
    # boundary. A hand-built/legacy candidate cannot claim support when its
    # persisted opportunity coverage is known to be inadequate.
    coverage = getattr(candidate, "coverage_pct", None)
    pair_coverage = getattr(candidate, "pair_coverage_pct", None)
    firing_rate = getattr(candidate, "firing_rate", None)
    support_gap = None
    if (coverage is not None
            and coverage < shortlist_mod.MIN_OPPORTUNITY_COVERAGE_PCT):
        support_gap = (
            f"resolved opportunity coverage is {coverage:.1f}%, below the "
            f"{shortlist_mod.MIN_OPPORTUNITY_COVERAGE_PCT:.0f}% floor")
    elif (firing_rate is not None
          and firing_rate < shortlist_mod.MIN_FIRING_RATE):
        support_gap = (
            f"firing rate is {firing_rate * 100:.2f}%, below the "
            f"{shortlist_mod.MIN_FIRING_RATE * 100:.1f}% floor")
    elif (pair_coverage is not None
          and pair_coverage < shortlist_mod.MIN_OPPORTUNITY_COVERAGE_PCT):
        support_gap = (
            f"candidate/baseline opportunity coverage is "
            f"{pair_coverage:.1f}%, below the "
            f"{shortlist_mod.MIN_OPPORTUNITY_COVERAGE_PCT:.0f}% floor")
    elif (pair_coverage is not None
          and getattr(candidate, "delta_ci_low", None) is None):
        support_gap = "matched baseline delta evidence is unavailable"
    elif (pair_coverage is not None
          and getattr(candidate, "delta_ci_low", 0.0) <= 0):
        support_gap = "matched baseline delta interval does not clear zero"

    if candidate.label == shortlist_mod.SUPPORTED and support_gap:
        return COLLECTING, (
            f"{support_gap}. The result remains evidence-gated and cannot "
            "be called supported until the opportunity ledger is adequate.")

    if candidate.label == shortlist_mod.SUPPORTED:
        return SUPPORTED, (
            f"{candidate.trades} trades, interval {candidate.ci_low:+.3f}.."
            f"{candidate.ci_high:+.3f} R excludes zero and the held-out "
            "window agrees"
            + (f"; {firing_rate * 100:.2f}% firing and "
               f"{coverage:.1f}% opportunity coverage"
               if firing_rate is not None and coverage is not None else "")
            + ". Kept running; this is evidence, not authority.")

    return COLLECTING, (
        f"{candidate.trades} closed trades so far ({candidate.label}). "
        "Not enough to conclude either way.")


RETIRING = frozenset({NEGATIVE_EXPECTANCY, DIED_OUT_OF_SAMPLE, NEVER_FIRED})


def review(staging_store, findings_store, *, scope_key: str | None = None,
           retire: bool = True, now: float | None = None) -> dict:
    """Adjudicate every staged mechanism and retire the ones that are done.

    ``retire=False`` reports the same verdicts without acting on them, so an
    operator can see what a run would do before it does it.
    """
    from agent.shadow import staged_variant_id

    timestamp = time.time() if now is None else float(now)
    active = staging_store.active()
    variant_ids = {
        staged_variant_id(contract.contract_id) for contract in active}
    measured = shortlist_mod.from_store(
        findings_store, staging_store, scope_key=scope_key)

    # A variant id is stable across feed/account scopes.  Keying only by that
    # id silently replaced one population with whichever scope happened to be
    # iterated last, so a bad staged arm in one account could be reported as a
    # good arm from another.  Keep scope in the identity all the way through
    # adjudication and only retire globally when the decision is unambiguous.
    candidates = {
        (str(item.scope_key), str(item.variant_id)): item
        for item in measured
        if item.variant_id in variant_ids
    }
    if scope_key is not None:
        scopes = [str(scope_key)]
    else:
        scopes = sorted({scope for scope, _ in candidates})
        # No paper portfolio exists yet. Preserve the old one verdict per
        # active contract so callers still get a useful COLLECTING result.
        if not scopes:
            scopes = [None]

    verdicts, retired = [], []
    by_contract: dict[str, list[dict]] = {}
    for scope in scopes:
        for contract in active:
            variant_id = staged_variant_id(contract.contract_id)
            candidate = (candidates.get((scope, variant_id))
                         if scope is not None else None)
            if candidate is None:
                # Registered but never evaluated in this exact scope: the
                # lane has not run yet.
                code, explanation = COLLECTING, (
                    "no evaluations recorded yet; the lane has not run.")
            else:
                code, explanation = verdict_for(candidate)
            entry = {
                "contract_id": contract.contract_id,
                "variant_id": variant_id,
                "scope_key": scope,
                "stage": (stage_of(candidate)
                           if candidate is not None else SCREEN),
                "code": code,
                "explanation": explanation,
                "mechanism": contract.mechanism,
                "payer": contract.payer,
                "trades": getattr(candidate, "trades", 0),
                "mean_r": getattr(candidate, "mean_r", 0.0),
                "eligible_opportunities": getattr(
                    candidate, "eligible_opportunities", 0),
                "firing_rate": getattr(candidate, "firing_rate", None),
                "coverage_pct": getattr(candidate, "coverage_pct", None),
                "pair_coverage_pct": getattr(
                    candidate, "pair_coverage_pct", None),
            }
            verdicts.append(entry)
            by_contract.setdefault(contract.contract_id, []).append(entry)

    if retire:
        for contract in active:
            entries = by_contract.get(contract.contract_id, [])
            # An explicit scope is an operator request to adjudicate that
            # account.  Without one, require every observed scope to agree;
            # one scope's negative result must not retire a claim still
            # collecting or supported in another isolated account.
            should_retire = bool(entries) and all(
                entry["code"] in RETIRING for entry in entries)
            if not should_retire:
                continue
            entry = entries[0]
            try:
                staging_store.retire(
                    contract.contract_id,
                    f"{entry['code']}: {entry['explanation']}",
                    now=timestamp)
                retired.append(contract.contract_id)
            except Exception as exc:  # noqa: BLE001 - one failure is not fatal
                for item in entries:
                    item["retire_error"] = f"{type(exc).__name__}: {exc}"
    return {
        "reviewed": len(verdicts),
        "retired": retired,
        "verdicts": verdicts,
    }


def authoring_history(staging_store, findings_store,
                      *, scope_key: str | None = None) -> dict:
    """What a proposer needs to avoid reproposing a dead claim.

    Retired mechanisms carry the code and the explanation, because a proposer
    told only that something failed will restate it with a different
    threshold - which is the same claim wearing a different number.
    """
    falsified, inconclusive = [], []
    for row in staging_store.retired():
        reason = str(row.get("retired_reason") or "")
        code = reason.split(":", 1)[0].strip() if ":" in reason else ""
        item = {
            "contract_id": row.get("contract_id"),
            "mechanism": row.get("mechanism"),
            "payer": row.get("payer"),
            "code": code,
            "reason": reason,
        }
        if code == STARVED_OF_DATA:
            inconclusive.append(item)
        else:
            falsified.append(item)
    # A still-running mechanism that is merely collecting is not a lesson,
    # but one that already reads NEGATIVE is, so it is surfaced before it is
    # formally retired.
    outcome = review(staging_store, findings_store,
                     scope_key=scope_key, retire=False)
    for entry in outcome["verdicts"]:
        if entry["code"] in (STARVED_OF_DATA,):
            inconclusive.append(entry)
    return {"falsified": falsified, "inconclusive": inconclusive}
