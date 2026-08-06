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

# A mechanism that has never fired despite this many evaluated decisions is
# not selective, it is unreachable: its thresholds do not describe a state
# this market reaches. Retiring it frees the lane; the claim stays recorded.
NEVER_FIRED_DECISION_FLOOR = 2_000
# Below this, an absent result is far more likely to be missing data than an
# unreachable threshold, so it is never held against the claim.
STARVED_FRACTION = 0.5


def verdict_for(candidate) -> tuple[str, str]:
    """Return ``(code, explanation)`` for one measured staged mechanism."""
    evaluated = candidate.declined_decisions + candidate.trades
    starved = candidate.starved_decisions
    total = evaluated + starved

    if total and starved / total >= STARVED_FRACTION:
        return STARVED_OF_DATA, (
            f"{starved} of {total} decisions were never evaluated because the "
            "market data was absent. This is a pipeline result, not a market "
            "one: the claim has not been tested and is kept staged.")

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
            and candidate.declined_decisions >= NEVER_FIRED_DECISION_FLOOR):
        return NEVER_FIRED, (
            f"evaluated {candidate.declined_decisions} times and never fired. "
            "The thresholds do not describe a state this market reaches, so "
            "the claim is unreachable rather than wrong. Propose the same "
            "mechanism at a condition the market actually visits.")

    if candidate.label == shortlist_mod.SUPPORTED:
        return SUPPORTED, (
            f"{candidate.trades} trades, interval {candidate.ci_low:+.3f}.."
            f"{candidate.ci_high:+.3f} R excludes zero and the held-out "
            "window agrees. Kept running; this is evidence, not authority.")

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
    candidates = {
        item.variant_id: item
        for item in shortlist_mod.from_store(
            findings_store, staging_store, scope_key=scope_key)
    }
    verdicts, retired = [], []
    for contract in staging_store.active():
        variant_id = staged_variant_id(contract.contract_id)
        candidate = candidates.get(variant_id)
        if candidate is None:
            # Registered but never evaluated: the lane has not run yet.
            code, explanation = COLLECTING, (
                "no evaluations recorded yet; the lane has not run.")
        else:
            code, explanation = verdict_for(candidate)
        entry = {
            "contract_id": contract.contract_id,
            "variant_id": variant_id,
            "code": code,
            "explanation": explanation,
            "mechanism": contract.mechanism,
            "payer": contract.payer,
            "trades": getattr(candidate, "trades", 0),
            "mean_r": getattr(candidate, "mean_r", 0.0),
        }
        verdicts.append(entry)
        if retire and code in RETIRING:
            try:
                staging_store.retire(
                    contract.contract_id, f"{code}: {explanation}",
                    now=timestamp)
                retired.append(contract.contract_id)
            except Exception as exc:  # noqa: BLE001 - one failure is not fatal
                entry["retire_error"] = f"{type(exc).__name__}: {exc}"
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
