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
                    bootstrap_mean, bootstrap_p_value,
                    cluster_block_bootstrap_difference,
                    paired_cluster_sign_flip)
from .evidence_primitives import (canonical_opportunity_key, index_rows,
                                  pair_sets)

# Sample floors. These are not significance thresholds; they gate what a
# label is allowed to say, so a promising number on nine trades cannot read
# the same as the same number on two hundred.
MIN_FOR_PRELIMINARY = 30
MIN_FOR_SUPPORTED = 100
# A positive conditional result on a vanishingly rare signal is not enough
# to support the claim.  The rate is measured over eligible ledger
# opportunities (PROPOSED + eligible VETOED rows), not over market bars.
MIN_FIRING_RATE = 0.01
MIN_FIRE_RATE = MIN_FIRING_RATE
# Candidate and baseline arms must resolve the same opportunity population.
# Keep this aligned with the immutable promotion protocol's pair gate.
MIN_OPPORTUNITY_COVERAGE_PCT = 80.0
# Compatibility names for report/selector integrations that use the protocol
# vocabulary for the same persisted proposal-union gate.
MIN_COVERAGE_PCT = MIN_OPPORTUNITY_COVERAGE_PCT
MIN_PAIR_COVERAGE_PCT = MIN_OPPORTUNITY_COVERAGE_PCT
# Expected proportion of false discoveries tolerated among declared ones.
FDR_ALPHA = 0.05
# The confirmation window is the last 30% of a candidate's trades in time,
# matching the split the deterministic outcome evaluator already uses.
CONFIRMATION_FRACTION = 0.30
# Keep the persisted shortlist's paired-evidence floor aligned with the
# registered promotion protocol.  A hundred trades inside one afternoon are
# not a hundred independent observations.
MIN_CONFIRMATION_PAIRS = 30
MIN_PAIRED_CLUSTERS = 8
PAIR_CLUSTER_SECONDS = 21_600

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
    paired_clusters: int = 0
    confirmation_pairs: int = 0
    confirmation_clusters: int = 0
    confirmation_delta_vs_baseline: float | None = None
    confirmation_delta_ci_low: float | None = None
    confirmation_delta_ci_high: float | None = None
    confirmation_p_value: float | None = None
    # Duplicate identities are data-integrity failures, not extra evidence.
    # They are retained in the report so a ledger repair is actionable, but
    # any duplicate blocks the strongest label.
    duplicate_decisions: int = 0
    duplicate_trades: int = 0
    baseline_duplicate_decisions: int = 0
    baseline_duplicate_trades: int = 0
    starved_decisions: int = 0
    declined_decisions: int = 0
    # Opportunity-level evidence.  ``trades`` remains the count of closed,
    # valid accepted trades; the fields below describe the larger decision
    # population that the policy was asked to act on.
    eligible_opportunities: int = 0
    resolved_opportunities: int = 0
    firing_rate: float | None = None
    coverage_pct: float | None = None
    baseline_eligible_opportunities: int = 0
    baseline_resolved_opportunities: int = 0
    baseline_firing_rate: float | None = None
    baseline_coverage_pct: float | None = None
    matched_opportunities: int = 0
    pair_coverage_pct: float | None = None
    opportunity_mean_r: float | None = None
    opportunity_ci_low: float | None = None
    opportunity_ci_high: float | None = None
    trade_mean_r: float | None = None
    p_value: float = 1.0
    p_adjusted: float | None = None
    family_size: int = 0
    label: str = NO_EVIDENCE
    reasons: list = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def firing_rate_pct(self) -> float | None:
        """Human-readable firing rate, as a percentage."""
        return (None if self.firing_rate is None else self.firing_rate * 100.0)

    @property
    def firing_rate_percent(self) -> float | None:
        """Descriptive alias for integrations that spell out ``percent``."""
        return self.firing_rate_pct

    @property
    def opportunity_count(self) -> int:
        """Alias for the eligible decision/opportunity denominator."""
        return self.eligible_opportunities

    @property
    def opportunity_coverage_pct(self) -> float | None:
        """Alias used by callers that distinguish pair coverage."""
        return self.coverage_pct

    @property
    def mean_opportunity_r(self) -> float:
        """Opportunity-normalized mean (the value used for labels)."""
        return (self.mean_r if self.opportunity_mean_r is None
                else self.opportunity_mean_r)

    @property
    def duplicate_count(self) -> int:
        return (self.duplicate_decisions + self.duplicate_trades
                + self.baseline_duplicate_decisions
                + self.baseline_duplicate_trades)

    @property
    def duplicates(self) -> int:
        """Compact compatibility alias used by report consumers."""
        return self.duplicate_count

    def rank_key(self) -> tuple:
        # Within a label, the lower confidence bound orders candidates: it is
        # the value the evidence supports rather than the one it produced.
        return (LABEL_ORDER.index(self.label), 1 if self.is_baseline else 0,
                -self.ci_low, -self.mean_r)


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


def _value(row, key: str, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _proposal_key(row):
    """Return the immutable identity shared with replay and protocol."""
    return canonical_opportunity_key(row)


def _starved_reason(reason: object) -> bool:
    text = str(reason or "").strip().lower()
    return text.startswith("data missing") or text.startswith(
        "market data invalid") or text.startswith("market data missing")


def _opportunity_evidence(trades: list, decisions: list | None = None) -> dict:
    """Build returns and coverage from the immutable decision/trade rows.

    A non-starved VETOED decision is an eligible opportunity with a literal
    zero return.  A PROPOSED decision contributes its closed valid trade when
    one exists; unresolved/open/invalid accepted actions stay in the
    denominator but are not silently converted to a zero.  That distinction
    prevents missing close data from becoming performance evidence.
    """
    raw_closed = _closed(trades)
    # Index before joining.  A dict comprehension would let a second row
    # replace the first, and a later third row could then become evidence;
    # ``index_rows`` permanently excludes every repeated identity.
    closed_index = index_rows(
        raw_closed,
        value_fn=lambda row: float(row["r_multiple"]),
        timestamp_fn=lambda row: float(_value(row, "entry_ts", 0.0) or 0.0))
    closed = sorted(
        closed_index.resolved_rows.values(),
        key=lambda row: float(_value(row, "entry_ts", 0.0) or 0.0))
    by_key = dict(closed_index.resolved)
    by_trade_id: dict[str, dict] = {}
    for row in closed:
        trade_id = _value(row, "trade_id")
        if trade_id is not None:
            by_trade_id.setdefault(str(trade_id), row)

    # Direct callers and old fixtures have no ledger.  Preserve the original
    # trade-only behaviour while still allowing a supplied declined count to
    # add explicit zero-return opportunities in ``measure``.
    if decisions is None:
        returns = [row["r_multiple"] for row in closed]
        keys = [_proposal_key(row) for row in closed]
        return {
            "closed": closed, "returns": returns, "keys": keys,
            "eligible": len(closed), "resolved": len(closed),
            "fired": len(closed), "declined": 0, "starved": 0,
            "resolved_by_key": dict(closed_index.resolved),
            "resolved_ts_by_key": {
                key: closed_index.resolved_ts.get(key, 0.0)
                for key in keys},
            "eligible_keys": set(closed_index.unique_keys),
            "coverage_known": False,
            "duplicate_rows": closed_index.duplicate_rows,
            "duplicate_keys": set(closed_index.duplicate_keys),
            "duplicate_reasons": closed_index.duplicate_reasons,
            "duplicate_decisions": 0,
            "duplicate_trades": closed_index.duplicate_rows,
        }

    eligible_rows: list = []
    starved = 0
    def _decision_ts(row):
        value = _value(row, "decision_ts",
                       _value(row, "entry_ts", _value(row, "signal_ts", 0.0)))
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    ordered_decisions = sorted(decisions or [], key=_decision_ts)
    for row in ordered_decisions:
        outcome = str(_value(row, "decision_outcome", "") or "").upper()
        if outcome == "VETOED":
            if _starved_reason(_value(row, "reason")):
                starved += 1
                continue
            eligible_rows.append(row)
        elif outcome == "PROPOSED":
            # The signal fired when accepted, even if its paper trade is
            # still open or invalid.  Such a row remains eligible but
            # unresolved until a valid close is present.
            eligible_rows.append(row)

    def value_for(row):
        outcome = str(_value(row, "decision_outcome", "") or "").upper()
        if outcome == "VETOED":
            return 0.0
        key = _proposal_key(row)
        trade = by_key.get(key)
        if trade is None and _value(row, "paper_trade_id") is not None:
            trade = by_trade_id.get(str(_value(row, "paper_trade_id")))
        if trade is None:
            joined_status = str(_value(row, "trade_status") or "").upper()
            joined_value = _value(row, "trade_r_multiple")
            if (joined_status == "CLOSED"
                    and str(_value(row, "trade_result") or "") != "unfilled"
                    and int(_value(row, "trade_valid_for_inference", 1)
                            or 0) == 1
                    and joined_value is not None):
                try:
                    joined_value = float(joined_value)
                except (TypeError, ValueError):
                    joined_value = None
                if joined_value is not None and math.isfinite(joined_value):
                    return joined_value
            return None
        # Closed rows are indexed as key -> scalar return.  Keep this join
        # scalar so an index can never be mistaken for a raw row mapping.
        if isinstance(trade, dict):
            return float(trade["r_multiple"])
        return float(trade)

    decision_index = index_rows(
        eligible_rows, value_fn=value_for, timestamp_fn=_decision_ts)
    eligible_keys = set(decision_index.unique_keys)
    first_rows = decision_index.first_rows
    unique_rows = [first_rows[key] for key in eligible_keys]
    unique_rows.sort(key=_decision_ts)
    resolved_by_key = dict(decision_index.resolved)
    keys = [key for row in unique_rows
            if (key := _proposal_key(row)) in resolved_by_key]
    returns = [resolved_by_key[key] for key in keys]
    resolved_ts_by_key = {
        key: decision_index.resolved_ts.get(key, 0.0)
        for key in keys}
    eligible = len(unique_rows)
    resolved = len(keys)
    fired = sum(1 for row in unique_rows
                if str(_value(row, "decision_outcome", "") or "").upper()
                == "PROPOSED")
    declined = sum(1 for row in unique_rows
                   if str(_value(row, "decision_outcome", "") or "").upper()
                   == "VETOED")

    return {
        "closed": closed, "returns": returns, "keys": keys,
        "eligible": eligible, "resolved": resolved,
        "fired": fired, "declined": declined, "starved": starved,
        "resolved_by_key": resolved_by_key,
        "resolved_ts_by_key": resolved_ts_by_key,
        "eligible_keys": eligible_keys, "coverage_known": True,
        "duplicate_rows": (closed_index.duplicate_rows
                            + decision_index.duplicate_rows),
        "duplicate_keys": (set(closed_index.duplicate_keys)
                            | set(decision_index.duplicate_keys)),
        "duplicate_reasons": {
            "trades": closed_index.duplicate_reasons,
            "decisions": decision_index.duplicate_reasons,
        },
        "duplicate_decisions": decision_index.duplicate_rows,
        "duplicate_trades": closed_index.duplicate_rows,
    }


def _paired_opportunity_evidence(left: dict, right: dict) -> dict:
    """Match resolved candidate/baseline opportunities by immutable identity."""
    paired_sets = pair_sets(
        left["eligible_keys"], right["eligible_keys"],
        left["resolved_by_key"], right["resolved_by_key"])
    union = paired_sets["union"]
    common = list(paired_sets["matched"])
    common.sort(key=lambda key: (
        left["resolved_ts_by_key"].get(
            key, right["resolved_ts_by_key"].get(key, 0.0)), repr(key)))
    deltas = [left["resolved_by_key"][key] -
              right["resolved_by_key"][key] for key in common]
    pairs = [(
        left["resolved_ts_by_key"].get(
            key, right["resolved_ts_by_key"].get(key, 0.0)),
        left["resolved_by_key"][key], right["resolved_by_key"][key])
        for key in common]
    return {
        "deltas": deltas, "pairs": pairs, "matched": len(common),
        "union": len(union),
        "coverage_pct": (len(common) / len(union) * 100.0
                          if union else 0.0),
        "duplicate_rows": (left.get("duplicate_rows", 0)
                            + right.get("duplicate_rows", 0)),
        "left_duplicate_rows": left.get("duplicate_rows", 0),
        "right_duplicate_rows": right.get("duplicate_rows", 0),
        "duplicate_reasons": {
            "left": left.get("duplicate_reasons", {}),
            "right": right.get("duplicate_reasons", {}),
        },
    }


def _label(candidate: Candidate) -> tuple[str, list]:
    """Decide what the evidence is allowed to claim."""
    reasons: list = []
    n = candidate.trades
    duplicate_reason = None
    if candidate.duplicate_count:
        duplicate_reason = (
            f"{candidate.duplicate_count} duplicate opportunity rows were "
            "excluded permanently; duplicate identities require ledger "
            "repair")
        reasons.append(duplicate_reason)
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

    if candidate.duplicate_count:
        reasons.append("duplicate identities block SUPPORTED until the "
                       "evidence ledger is repaired")
        return INCONCLUSIVE, reasons

    if (candidate.source == "staged" and not candidate.is_baseline
            and candidate.pair_coverage_pct is None):
        reasons.append(
            "staged support requires matched candidate/baseline decisions; "
            "trade-only or independently sampled arms cannot supply paired "
            "held-out evidence")
        return INCONCLUSIVE, reasons

    # Once ledger evidence is available, a conditional trade result is not
    # enough. Require adequate resolved opportunity coverage and a minimum
    # firing rate before allowing the strongest label. Legacy trade-only
    # callers leave these fields unknown and retain descriptive behaviour.
    if candidate.coverage_pct is not None \
            and candidate.coverage_pct < MIN_OPPORTUNITY_COVERAGE_PCT:
        reasons.append(
            f"only {candidate.coverage_pct:.1f}% of eligible opportunities "
            f"resolved; {MIN_OPPORTUNITY_COVERAGE_PCT:.0f}% coverage is "
            "required before a supported label")
        return INCONCLUSIVE, reasons
    if (candidate.firing_rate is not None
            and candidate.firing_rate < MIN_FIRING_RATE):
        reasons.append(
            f"the arm fired on only {candidate.firing_rate * 100:.2f}% of "
            f"eligible opportunities; {MIN_FIRING_RATE * 100:.1f}% is the "
            "minimum firing rate for a supported claim")
        return INCONCLUSIVE, reasons
    if candidate.pair_coverage_pct is not None \
            and candidate.pair_coverage_pct < MIN_OPPORTUNITY_COVERAGE_PCT:
        reasons.append(
            "matched candidate/baseline opportunities cover only "
            f"{candidate.pair_coverage_pct:.1f}% of the proposal union; "
            f"{MIN_OPPORTUNITY_COVERAGE_PCT:.0f}% is required")
        return INCONCLUSIVE, reasons
    if candidate.pair_coverage_pct is not None:
        if candidate.delta_ci_low is None:
            reasons.append(
                "the matched baseline delta is unavailable, so the arm "
                "cannot be supported on its unconditional return")
            return INCONCLUSIVE, reasons
        if candidate.paired_clusters < MIN_PAIRED_CLUSTERS:
            reasons.append(
                f"the paired comparison spans only "
                f"{candidate.paired_clusters} independent market episodes; "
                f"{MIN_PAIRED_CLUSTERS} are required")
            return INCONCLUSIVE, reasons
        if candidate.confirmation_pairs < MIN_CONFIRMATION_PAIRS:
            reasons.append(
                f"the held-out candidate/baseline comparison has only "
                f"{candidate.confirmation_pairs} pairs; "
                f"{MIN_CONFIRMATION_PAIRS} are required")
            return INCONCLUSIVE, reasons
        if candidate.confirmation_clusters < MIN_PAIRED_CLUSTERS:
            reasons.append(
                f"the held-out candidate/baseline comparison spans only "
                f"{candidate.confirmation_clusters} market episodes; "
                f"{MIN_PAIRED_CLUSTERS} are required")
            return INCONCLUSIVE, reasons
        if (candidate.confirmation_delta_ci_low is None
                or candidate.confirmation_delta_ci_low <= 0):
            low = candidate.confirmation_delta_ci_low
            high = candidate.confirmation_delta_ci_high
            interval = ("unavailable" if low is None or high is None else
                        f"{low:+.3f}..{high:+.3f} R")
            reasons.append(
                "the held-out paired candidate-minus-baseline interval does "
                f"not clear zero ({interval})")
            return INCONCLUSIVE, reasons
        if (candidate.confirmation_p_value is None
                or candidate.confirmation_p_value > FDR_ALPHA):
            reasons.append(
                "the held-out clustered paired test is unavailable or not "
                f"significant (p={candidate.confirmation_p_value})")
            return INCONCLUSIVE, reasons
        if candidate.delta_ci_low <= 0:
            reasons.append(
                f"the matched baseline delta interval includes zero "
                f"({candidate.delta_ci_low:+.3f}.."
                f"{candidate.delta_ci_high:+.3f} R)")
            return INCONCLUSIVE, reasons

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
    if candidate.firing_rate is not None and candidate.coverage_pct is not None:
        reasons.append(
            f"{candidate.eligible_opportunities} eligible opportunities, "
            f"{candidate.firing_rate * 100:.2f}% firing and "
            f"{candidate.coverage_pct:.1f}% resolved coverage")
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
    if candidate.confirmation_delta_ci_low is not None:
        reasons.append(
            "held-out paired delta is positive at "
            f"{candidate.confirmation_delta_vs_baseline:+.3f} R "
            f"({candidate.confirmation_delta_ci_low:+.3f}.."
            f"{candidate.confirmation_delta_ci_high:+.3f}; "
            f"{candidate.confirmation_clusters} clusters, "
            f"p={candidate.confirmation_p_value:.4f})")
    return SUPPORTED, reasons


def measure(variant_id: str, scope_key: str, trades: list, *,
            mechanism: str = "", payer: str = "", configuration: str = "",
            source: str = "registered", is_baseline: bool = False,
            baseline_trades: list | None = None,
            decisions: list | None = None,
            baseline_decisions: list | None = None,
            baseline_variant_id: str | None = None,
            starved: int = 0, declined: int = 0) -> Candidate:
    """Summarise one arm without deciding anything about it yet."""
    evidence = _opportunity_evidence(trades, decisions)
    closed = evidence["closed"]
    returns = list(evidence["returns"])
    # Direct callers predate the decision ledger. A supplied declined count is
    # still an explicit zero-return opportunity when the arm has fired; with
    # no trades it remains NO_EVIDENCE rather than a synthetic negative sample.
    if decisions is None and declined and returns:
        returns.extend([0.0] * int(declined))
        evidence["eligible"] += int(declined)
        evidence["resolved"] += int(declined)
        evidence["declined"] += int(declined)
        evidence["coverage_known"] = True
    elif decisions is None and starved and returns:
        # The caller supplied an explicit starvation count but no ledger.
        # Starved observations are excluded from the opportunity denominator;
        # retain a known firing/coverage rate for the eligible closed arm.
        evidence["coverage_known"] = True
    starved_count = evidence["starved"] + int(starved)
    declined_count = evidence["declined"] + int(declined)
    candidate = Candidate(
        variant_id=variant_id, scope_key=scope_key, mechanism=mechanism,
        payer=payer, configuration=configuration, source=source,
        is_baseline=is_baseline,
        trades=len(closed), wins=sum(1 for r in returns if r > 0),
        net_pnl_usd=round(sum(
            float(row.get("net_pnl_usd") or 0.0) for row in closed), 2),
        duplicate_decisions=int(evidence.get("duplicate_decisions", 0)),
        duplicate_trades=int(evidence.get("duplicate_trades", 0)),
        starved_decisions=starved_count, declined_decisions=declined_count,
        eligible_opportunities=int(evidence["eligible"]),
        resolved_opportunities=int(evidence["resolved"]),
        baseline_variant_id=baseline_variant_id)
    if evidence["coverage_known"]:
        candidate.firing_rate = (evidence["fired"] / evidence["eligible"]
                                 if evidence["eligible"] else 0.0)
        candidate.coverage_pct = (
            evidence["resolved"] / evidence["eligible"] * 100.0
            if evidence["eligible"] else 0.0)
    if returns:
        candidate.opportunity_mean_r = round(sum(returns) / len(returns), 4)
        if closed:
            candidate.trade_mean_r = round(
                sum(row["r_multiple"] for row in closed) / len(closed), 4)
    if returns:
        candidate.p_value = round(bootstrap_p_value(returns), 5)
        interval = bootstrap_mean(returns)
        candidate.mean_r = round(interval.point, 4)
        candidate.ci_low = round(interval.low, 4)
        candidate.ci_high = round(interval.high, 4)
        candidate.opportunity_ci_low = candidate.ci_low
        candidate.opportunity_ci_high = candidate.ci_high
        # Split in time, not at random: an edge that only works in the window
        # it was found in is the failure this split exists to catch.
        cut = max(1, int(round(len(returns) * (1 - CONFIRMATION_FRACTION))))
        if len(returns) - cut >= 2:
            candidate.fit_mean_r = round(sum(returns[:cut]) / cut, 4)
            tail = returns[cut:]
            candidate.confirmation_mean_r = round(sum(tail) / len(tail), 4)
    baseline_evidence = None
    if baseline_decisions is not None or baseline_trades is not None:
        baseline_evidence = _opportunity_evidence(
            baseline_trades or [], baseline_decisions)
        candidate.baseline_eligible_opportunities = int(
            baseline_evidence["eligible"])
        candidate.baseline_resolved_opportunities = int(
            baseline_evidence["resolved"])
        if baseline_evidence["coverage_known"]:
            candidate.baseline_firing_rate = (
                baseline_evidence["fired"] /
                baseline_evidence["eligible"]
                if baseline_evidence["eligible"] else 0.0)
            candidate.baseline_coverage_pct = (
                baseline_evidence["resolved"] /
                baseline_evidence["eligible"] * 100.0
                if baseline_evidence["eligible"] else 0.0)
        candidate.baseline_duplicate_decisions = int(
            baseline_evidence.get("duplicate_decisions", 0))
        candidate.baseline_duplicate_trades = int(
            baseline_evidence.get("duplicate_trades", 0))
    delta = None
    if (baseline_evidence is not None
            and decisions is not None and baseline_decisions is not None):
        # Pair coverage is reported even when one side has no resolved rows;
        # an empty or duplicate-only union is deliberately fail-closed.
        paired = _paired_opportunity_evidence(evidence,
                                               baseline_evidence)
        candidate.matched_opportunities = paired["matched"]
        candidate.pair_coverage_pct = paired["coverage_pct"]
        if returns and baseline_evidence["returns"]:
            delta = (cluster_block_bootstrap_difference(
                paired["pairs"], cluster_seconds=PAIR_CLUSTER_SECONDS)
                     if paired["pairs"] else None)
            if delta is not None:
                candidate.paired_clusters = delta.clusters
            cut = max(1, int(round(
                len(paired["pairs"]) * (1 - CONFIRMATION_FRACTION))))
            confirmation_pairs = paired["pairs"][cut:]
            candidate.confirmation_pairs = len(confirmation_pairs)
            if confirmation_pairs:
                confirmation_delta = cluster_block_bootstrap_difference(
                    confirmation_pairs,
                    cluster_seconds=PAIR_CLUSTER_SECONDS)
                confirmation_test = paired_cluster_sign_flip(
                    confirmation_pairs,
                    cluster_seconds=PAIR_CLUSTER_SECONDS)
                candidate.confirmation_clusters = (
                    confirmation_delta.clusters)
                candidate.confirmation_delta_vs_baseline = round(
                    confirmation_delta.point, 4)
                candidate.confirmation_delta_ci_low = round(
                    confirmation_delta.low, 4)
                candidate.confirmation_delta_ci_high = round(
                    confirmation_delta.high, 4)
                candidate.confirmation_p_value = round(
                    float(confirmation_test["p_value"]), 5)
                # Multiplicity correction must consume the confirmatory
                # paired test, not a candidate-vs-zero exploratory test.
                candidate.p_value = candidate.confirmation_p_value
        if delta is not None:
            candidate.delta_vs_baseline = round(delta.point, 4)
            candidate.delta_ci_low = round(delta.low, 4)
            candidate.delta_ci_high = round(delta.high, 4)
    elif (baseline_evidence is not None and returns
          and baseline_evidence["returns"]):
        # Legacy trade-only evidence has no proposal identity to match;
        # retain the historical independent comparison and leave pair
        # coverage unknown rather than manufacturing a match.
        delta = bootstrap_difference(returns, baseline_evidence["returns"])
        candidate.delta_vs_baseline = round(delta.point, 4)
        candidate.delta_ci_low = round(delta.low, 4)
        candidate.delta_ci_high = round(delta.high, 4)
    elif baseline_decisions is not None:
        # Explicitly supplied empty baseline evidence is inadequate, not an
        # absent legacy argument. This makes SUPPORTED fail closed when a
        # persisted candidate has no matched baseline opportunity set.
        candidate.pair_coverage_pct = 0.0
    elif decisions is not None and not is_baseline:
        # A decision ledger without a baseline ledger is also an explicit
        # absence of matched opportunity evidence. Keep the legacy
        # trade-only path above for old callers, but never call a persisted
        # candidate SUPPORTED without this comparison population.
        candidate.pair_coverage_pct = 0.0
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
        for c in candidates
        if not c.is_baseline and c.trades >= MIN_FOR_PRELIMINARY
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
        "measured interval excludes zero on an adequate sample and a paired "
        "held-out candidate-minus-baseline interval also clears zero - not "
        "that the edge will persist. A result "
        "whose mechanism is not understood cannot be told apart from "
        "overfitting and gives no warning when it stops.")
    lines.append("")
    lines.append("| Rank | Candidate | Scope | Label | Trades | Opportunities "
                 "| Fire rate | Coverage | Mean R | 95% interval | Held-out ΔR "
                 "| Win rate | Net USD |")
    lines.append("| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: "
                 "| --- | ---: | ---: | ---: |")
    for index, candidate in enumerate(ordered, start=1):
        held_value = (candidate.confirmation_delta_vs_baseline
                      if candidate.confirmation_delta_vs_baseline is not None
                      else candidate.confirmation_mean_r)
        held = "-" if held_value is None else f"{held_value:+.3f}"
        firing = ("-" if candidate.firing_rate is None
                  else f"{candidate.firing_rate:.1%}")
        coverage = ("-" if candidate.coverage_pct is None
                    else f"{candidate.coverage_pct:.1f}%")
        name = candidate.variant_id + (" (baseline)"
                                       if candidate.is_baseline else "")
        lines.append(
            f"| {index} | `{name}` | {_scope_tag(candidate.scope_key)} "
            f"| {candidate.label} "
            f"| {candidate.trades} | {candidate.eligible_opportunities} "
            f"| {firing} | {coverage} | {candidate.mean_r:+.3f} "
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
        if candidate.pair_coverage_pct is not None:
            lines.append(
                f"- matched candidate/baseline opportunity coverage: "
                f"{candidate.pair_coverage_pct:.1f}% "
                f"({candidate.matched_opportunities} matched opportunities)")
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
        if baseline_id:
            try:
                baseline_decisions = findings_store.paper_decisions_for(
                    scope, baseline_id)
            except Exception:  # noqa: BLE001 - legacy pre-ledger store
                baseline_decisions = None
        else:
            baseline_decisions = []
        for variant_id in sorted(variants):
            is_baseline = variant_id in baselines
            contract = staged_claims.get(variant_id)
            decisions = _decision_counts(findings_store, scope, variant_id)
            try:
                decision_rows = findings_store.paper_decisions_for(
                    scope, variant_id)
            except Exception:  # noqa: BLE001 - legacy pre-ledger store
                decision_rows = None
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
                baseline_decisions=(None if is_baseline else baseline_decisions),
                baseline_variant_id=(None if is_baseline else baseline_id),
                decisions=decision_rows,
                # ``decision_rows`` is the source of truth for both counts;
                # passing the summary too would count every veto twice.
                starved=0, declined=0))
    return apply_family_correction(candidates)


def _variants_in(findings_store, scope_key: str) -> set:
    import sqlite3

    try:
        with sqlite3.connect(f"file:{findings_store.path}?mode=ro",
                             uri=True) as conn:
            try:
                rows = conn.execute(
                    "SELECT DISTINCT variant_id FROM paper_portfolios "
                    "WHERE scope_key=? UNION SELECT DISTINCT variant_id "
                    "FROM paper_decisions WHERE scope_key=?",
                    (scope_key, scope_key)).fetchall()
            except sqlite3.Error:
                # Pre-ledger stores still have useful trade portfolios. Keep
                # the legacy shortlist readable rather than dropping the
                # whole scope because the optional decision table is absent.
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
        if _starved_reason(reason):
            counts["starved"] += 1
        else:
            counts["declined"] += 1
    return counts
