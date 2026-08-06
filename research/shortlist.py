"""Rank what the evidence currently supports, and say how strongly.

This is the report a human reads after leaving the loop running. Its job is
not to find a winner but to state, for every candidate the platform has
measured, what the evidence is and how far it goes - including the common
case where the honest answer is "not enough trades yet".

Two rules shape it. Nothing is ever labelled proven: thirty days of one market
regime is thirty days of one market regime, and `funding-carry` posted +2.008%
per trade over 116 trades, beat every null and was better out of sample while
being a directional bet wearing a carry label. And a candidate that fired but
was starved of data is reported separately from one that was measured and
lost, because those are opposite conclusions about whether its claim was ever
tested at all.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .stats import (benjamini_hochberg, bootstrap_difference,
                    bootstrap_mean, bootstrap_p_value)

# Sample floors. These are not significance thresholds; they gate what a
# label is allowed to say, so a promising number on nine trades cannot read
# the same as the same number on two hundred.
MIN_FOR_PRELIMINARY = 30
MIN_FOR_SUPPORTED = 100
# Expected proportion of false discoveries tolerated among declared ones.
FDR_ALPHA = 0.05
# The confirmation window is the last 30% of a candidate's trades in time,
# matching the split the deterministic outcome evaluator already uses.
CONFIRMATION_FRACTION = 0.30

NO_EVIDENCE = "NO_EVIDENCE"
INSUFFICIENT = "INSUFFICIENT"
PRELIMINARY = "PRELIMINARY"
INCONCLUSIVE = "INCONCLUSIVE"
NEGATIVE = "NEGATIVE"
SUPPORTED = "SUPPORTED"

# Ordered best-first. SUPPORTED is the strongest thing this system says, and
# it deliberately stops short of a recommendation to trade.
LABEL_ORDER = (SUPPORTED, PRELIMINARY, INCONCLUSIVE, INSUFFICIENT,
               NO_EVIDENCE, NEGATIVE)


@dataclass
class Candidate:
    """One measured arm and everything needed to judge it."""

    variant_id: str
    scope_key: str
    mechanism: str = ""
    payer: str = ""
    configuration: str = ""
    source: str = "registered"
    is_baseline: bool = False
    trades: int = 0
    wins: int = 0
    mean_r: float = 0.0
    ci_low: float = 0.0
    ci_high: float = 0.0
    net_pnl_usd: float = 0.0
    fit_mean_r: float | None = None
    confirmation_mean_r: float | None = None
    baseline_variant_id: str | None = None
    delta_vs_baseline: float | None = None
    delta_ci_low: float | None = None
    delta_ci_high: float | None = None
    starved_decisions: int = 0
    declined_decisions: int = 0
    p_value: float = 1.0
    p_adjusted: float | None = None
    family_size: int = 0
    label: str = NO_EVIDENCE
    reasons: list = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    def rank_key(self) -> tuple:
        # Within a label, the lower confidence bound orders candidates: it is
        # the value the evidence supports rather than the one it produced.
        return (LABEL_ORDER.index(self.label), -self.ci_low, -self.mean_r)


def _closed(trades: list) -> list:
    out = []
    for row in trades or []:
        if str(row.get("status") or "").upper() != "CLOSED":
            continue
        if row.get("result") == "unfilled":
            continue
        if int(row.get("valid_for_inference", 1) or 0) == 0:
            continue
        value = row.get("r_multiple")
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            out.append({**row, "r_multiple": number})
    return sorted(out, key=lambda row: float(row.get("entry_ts") or 0.0))


def _label(candidate: Candidate) -> tuple[str, list]:
    """Decide what the evidence is allowed to claim."""
    reasons: list = []
    n = candidate.trades
    if n == 0:
        if candidate.starved_decisions:
            reasons.append(
                f"{candidate.starved_decisions} decisions were never "
                "evaluated because the market data was absent, so the claim "
                "is untested rather than unsupported; those are not evidence "
                "against the claim")
        elif candidate.declined_decisions:
            reasons.append(
                f"the contract declined {candidate.declined_decisions} times "
                "and never fired, so it has produced no trades to judge")
        return NO_EVIDENCE, reasons

    if candidate.ci_high <= 0:
        reasons.append(
            f"the whole 95% interval is at or below zero "
            f"({candidate.ci_low:+.3f}..{candidate.ci_high:+.3f} R)")
        return NEGATIVE, reasons

    if n < MIN_FOR_PRELIMINARY:
        reasons.append(
            f"{n} closed trades is below the {MIN_FOR_PRELIMINARY} needed "
            "for any reading to mean much")
        return INSUFFICIENT, reasons

    if n < MIN_FOR_SUPPORTED:
        reasons.append(
            f"{n} closed trades supports a preliminary reading only; "
            f"{MIN_FOR_SUPPORTED} is the floor for a supported one")
        return PRELIMINARY, reasons

    if candidate.ci_low <= 0:
        reasons.append(
            f"the 95% interval still includes zero "
            f"({candidate.ci_low:+.3f}..{candidate.ci_high:+.3f} R)")
        return INCONCLUSIVE, reasons

    confirmation = candidate.confirmation_mean_r
    if confirmation is not None and confirmation <= 0:
        reasons.append(
            f"the held-out confirmation window is {confirmation:+.3f} R "
            "while the fitting window is positive, so the result does not "
            "persist out of sample")
        return INCONCLUSIVE, reasons

    if candidate.p_adjusted is not None and candidate.p_adjusted > FDR_ALPHA:
        reasons.append(
            f"the raw interval excludes zero (p={candidate.p_value:.4f}) but "
            f"does not survive false-discovery control across the "
            f"{candidate.family_size} candidates screened alongside it "
            f"(p_adj {candidate.p_adjusted:.3f} > {FDR_ALPHA}). Screening "
            "many candidates produces a few that look significant with no "
            "edge at all; this is what separates them")
        return INCONCLUSIVE, reasons

    reasons.append(
        f"{n} closed trades, interval {candidate.ci_low:+.3f}.."
        f"{candidate.ci_high:+.3f} R excludes zero")
    if candidate.p_adjusted is not None:
        reasons.append(
            f"survives false-discovery control across "
            f"{candidate.family_size} candidates "
            f"(p={candidate.p_value:.4f}, p_adj {candidate.p_adjusted:.3f})")
    if confirmation is not None:
        reasons.append(
            f"held-out confirmation agrees in sign ({confirmation:+.3f} R)")
    if candidate.delta_ci_low is not None and candidate.delta_ci_low > 0:
        reasons.append(
            f"beats its baseline by {candidate.delta_vs_baseline:+.3f} R "
            f"({candidate.delta_ci_low:+.3f}..{candidate.delta_ci_high:+.3f})")
    return SUPPORTED, reasons


def measure(variant_id: str, scope_key: str, trades: list, *,
            mechanism: str = "", payer: str = "", configuration: str = "",
            source: str = "registered", is_baseline: bool = False,
            baseline_trades: list | None = None,
            baseline_variant_id: str | None = None,
            starved: int = 0, declined: int = 0) -> Candidate:
    """Summarise one arm without deciding anything about it yet."""
    closed = _closed(trades)
    returns = [row["r_multiple"] for row in closed]
    candidate = Candidate(
        variant_id=variant_id, scope_key=scope_key, mechanism=mechanism,
        payer=payer, configuration=configuration, source=source,
        is_baseline=is_baseline,
        trades=len(closed), wins=sum(1 for r in returns if r > 0),
        net_pnl_usd=round(sum(
            float(row.get("net_pnl_usd") or 0.0) for row in closed), 2),
        starved_decisions=int(starved), declined_decisions=int(declined),
        baseline_variant_id=baseline_variant_id)
    if returns:
        candidate.p_value = round(bootstrap_p_value(returns), 5)
        interval = bootstrap_mean(returns)
        candidate.mean_r = round(interval.point, 4)
        candidate.ci_low = round(interval.low, 4)
        candidate.ci_high = round(interval.high, 4)
        # Split in time, not at random: an edge that only works in the window
        # it was found in is the failure this split exists to catch.
        cut = max(1, int(round(len(returns) * (1 - CONFIRMATION_FRACTION))))
        if len(returns) - cut >= 2:
            candidate.fit_mean_r = round(sum(returns[:cut]) / cut, 4)
            tail = returns[cut:]
            candidate.confirmation_mean_r = round(sum(tail) / len(tail), 4)
    baseline_returns = [row["r_multiple"] for row in _closed(baseline_trades)]
    if returns and baseline_returns:
        delta = bootstrap_difference(returns, baseline_returns)
        candidate.delta_vs_baseline = round(delta.point, 4)
        candidate.delta_ci_low = round(delta.low, 4)
        candidate.delta_ci_high = round(delta.high, 4)
    candidate.label, candidate.reasons = _label(candidate)
    return candidate


def _scope_tag(scope_key: str) -> str:
    """A short, stable tag; the full scope is printed in the detail section.

    Identical variant ids recur across scopes - a feed fork or a second
    account produces the same arm again - and a table that cannot tell them
    apart invites reading two populations as one.
    """
    parts = [part for part in str(scope_key).split(":") if part]
    return parts[-1] if parts else "?"


def apply_family_correction(candidates: list, alpha: float = FDR_ALPHA) -> list:
    """Correct across everything screened together, then relabel.

    The family is every candidate with enough trades to be judged at all.
    Including the ones too thin to conclude about would inflate the family
    size with tests that were never run, which makes the correction look
    stricter than the search actually was.
    """
    eligible = {
        f"{c.scope_key}|{c.variant_id}": c.p_value
        for c in candidates if c.trades >= MIN_FOR_PRELIMINARY
    }
    if not eligible:
        return candidates
    corrected = benjamini_hochberg(eligible, alpha=alpha)
    for candidate in candidates:
        key = f"{candidate.scope_key}|{candidate.variant_id}"
        row = corrected.get(key)
        if row is None:
            continue
        candidate.p_adjusted = round(row["p_adjusted"], 5)
        candidate.family_size = row["family_size"]
        # Relabel: the correction can only ever demote, never promote.
        candidate.label, candidate.reasons = _label(candidate)
    return candidates


def rank(candidates: list) -> list:
    return sorted(candidates, key=lambda item: item.rank_key())


def render(candidates: list, *, generated_ts: float | None = None) -> str:
    """A report a human can act on, or decline to act on."""
    ordered = rank(candidates)
    lines = ["# Candidate shortlist", ""]
    if not ordered:
        lines.append("Nothing has been measured yet.")
        return "\n".join(lines) + "\n"

    supported = [c for c in ordered if c.label == SUPPORTED]
    lines.append(
        f"{len(ordered)} candidates measured; {len(supported)} supported by "
        "the evidence so far.")
    lines.append("")
    lines.append(
        "No entry here is a recommendation to trade. `SUPPORTED` means the "
        "measured interval excludes zero on an adequate sample and the "
        "held-out window agrees - not that the edge will persist. A result "
        "whose mechanism is not understood cannot be told apart from "
        "overfitting and gives no warning when it stops.")
    lines.append("")
    lines.append("| Rank | Candidate | Scope | Label | Trades | Mean R "
                 "| 95% interval | Held-out | Win rate | Net USD |")
    lines.append("| --- | --- | --- | --- | ---: | ---: | --- | ---: | ---: "
                 "| ---: |")
    for index, candidate in enumerate(ordered, start=1):
        held = ("-" if candidate.confirmation_mean_r is None
                else f"{candidate.confirmation_mean_r:+.3f}")
        name = candidate.variant_id + (" (baseline)"
                                       if candidate.is_baseline else "")
        lines.append(
            f"| {index} | `{name}` | {_scope_tag(candidate.scope_key)} "
            f"| {candidate.label} "
            f"| {candidate.trades} | {candidate.mean_r:+.3f} "
            f"| {candidate.ci_low:+.3f}..{candidate.ci_high:+.3f} "
            f"| {held} | {candidate.win_rate:.1%} "
            f"| {candidate.net_pnl_usd:,.2f} |")
    lines.append("")

    for candidate in ordered:
        lines.append(
            f"## {candidate.variant_id} - {candidate.label}")
        lines.append("")
        lines.append(f"Scope `{candidate.scope_key}`."
                     + (" This arm is its scope's baseline."
                        if candidate.is_baseline else ""))
        lines.append("")
        if candidate.mechanism:
            lines.append(f"**Mechanism.** {candidate.mechanism}")
            lines.append("")
        if candidate.payer:
            lines.append(f"**Who pays.** {candidate.payer}")
            lines.append("")
        if candidate.configuration:
            lines.append(f"**Configuration.** `{candidate.configuration}`")
            lines.append("")
        for reason in candidate.reasons:
            lines.append(f"- {reason}")
        if candidate.starved_decisions and candidate.trades:
            # With no trades ``_label`` has already said this; repeating it
            # would read as two separate findings about one gap.
            lines.append(
                f"- {candidate.starved_decisions} further decisions were "
                "never evaluated because the market data was absent; those "
                "are not evidence against the claim")
        lines.append("")
    return "\n".join(lines) + "\n"


def from_store(findings_store, staging_store=None, *,
               scope_key: str | None = None) -> list:
    """Assemble candidates from persisted evidence.

    Every paper portfolio in scope becomes a candidate, so a mechanism that
    has produced nothing still appears with the reason why. A staged
    mechanism carries the claim it was registered with; a registered variant
    carries its hypothesis. Baselines are excluded from the ranking and used
    as the comparison arm for their own scope instead.
    """
    scopes = ([scope_key] if scope_key
              else list(findings_store.paper_scopes()))
    staged_claims = {}
    if staging_store is not None:
        try:
            from agent.shadow import staged_variant_id

            for contract in staging_store.active():
                staged_claims[staged_variant_id(contract.contract_id)] = contract
        except Exception:  # noqa: BLE001 - claims are annotation, not evidence
            staged_claims = {}

    candidates = []
    for scope in scopes:
        variants = _variants_in(findings_store, scope)
        baselines = {v for v in variants if v.endswith(".baseline")}
        baseline_id = next(iter(sorted(baselines)), None)
        baseline_trades = (
            findings_store.paper_trades_for(scope, baseline_id)
            if baseline_id else None)
        for variant_id in sorted(variants):
            is_baseline = variant_id in baselines
            contract = staged_claims.get(variant_id)
            decisions = _decision_counts(findings_store, scope, variant_id)
            candidates.append(measure(
                variant_id, scope,
                findings_store.paper_trades_for(scope, variant_id),
                mechanism=(contract.mechanism if contract else ""),
                payer=(contract.payer if contract else ""),
                configuration=(contract.describe() if contract else ""),
                source=("staged" if contract else "registered"),
                is_baseline=is_baseline,
                # A baseline is not compared with itself.
                baseline_trades=(None if is_baseline else baseline_trades),
                baseline_variant_id=(None if is_baseline else baseline_id),
                starved=decisions["starved"], declined=decisions["declined"]))
    return apply_family_correction(candidates)


def _variants_in(findings_store, scope_key: str) -> set:
    import sqlite3

    try:
        with sqlite3.connect(f"file:{findings_store.path}?mode=ro",
                             uri=True) as conn:
            rows = conn.execute(
                "SELECT DISTINCT variant_id FROM paper_portfolios "
                "WHERE scope_key=?", (scope_key,)).fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[0]) for row in rows}


def _decision_counts(findings_store, scope_key: str, variant_id: str) -> dict:
    """Separate "never evaluated" from "evaluated and declined"."""
    counts = {"starved": 0, "declined": 0}
    try:
        rows = findings_store.paper_decisions_for(scope_key, variant_id)
    except Exception:  # noqa: BLE001
        return counts
    for row in rows or []:
        if str(row.get("decision_outcome") or "") != "VETOED":
            continue
        reason = str(row.get("reason") or "")
        if reason.startswith("data missing") or reason.startswith(
                "market data invalid"):
            counts["starved"] += 1
        else:
            counts["declined"] += 1
    return counts
