"""One scorer, shared by the live report and the replay harness.

Two scorers means two sets of numbers that cannot be compared, and the
comparison is the entire job: the question this programme exists to answer is
whether a replayed variant beats the demo record, which is unanswerable if
"expectancy" is computed one way on the left and another on the right.

So ``match_round_trips`` lives here and ``report.py`` imports it. It was moved
verbatim rather than rewritten - its handling of partial closes and the
legacy cumulative-PnL semantics is subtle, tested, and correct, and a
rewrite would have been a second implementation wearing the first one's name.

The funnel is new. Nothing reported it before, and without it "the strategy
takes two trades a day" cannot be turned into "of 412 contract firings, 206
became proposals and 41 survived the vetoes" - which is the difference
between a number and a diagnosis.
"""

from __future__ import annotations

import math
from collections import defaultdict

from .stats import (INSUFFICIENT_SAMPLE, bootstrap_mean,
                    minimum_detectable_effect)


def _number(value, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def match_round_trips(events: list[dict]) -> tuple[list[dict], dict]:
    """Match one open and its final close using only a durable trade ID."""
    opens: dict[str, dict] = {}
    partials: dict[str, list[dict]] = defaultdict(list)
    duplicates: set[str] = set()
    for event in events:
        trade_id = event.get("trade_id")
        if event.get("action") == "partial_close" and trade_id:
            partials[str(trade_id)].append(event)
        if event.get("action") != "open" or not trade_id:
            continue
        if trade_id in opens:
            duplicates.add(str(trade_id))
        else:
            opens[str(trade_id)] = event

    matched = []
    matched_ids = set()
    closed_ids = set()
    unmatchable_closes = 0
    unscored_closes = 0
    for close in events:
        if close.get("action") != "close":
            continue
        trade_id = close.get("trade_id")
        if (not trade_id or str(trade_id) not in opens
                or str(trade_id) in duplicates
                or str(trade_id) in matched_ids):
            unmatchable_closes += 1
            continue
        closed_ids.add(str(trade_id))
        close_rows = partials.get(str(trade_id), []) + [close]
        if close.get("realized_pnl_usd") is None:
            unscored_closes += 1
            continue
        opened = opens[str(trade_id)]
        notional = _number(opened.get("notional"))
        risk = _number(opened.get("risk_usd"), _number(close.get("risk_usd")))
        incremental = close.get("pnl_semantics") == "incremental_v1"
        if incremental:
            if any(row.get("realized_pnl_usd") is None for row in close_rows):
                unscored_closes += 1
                continue
            pnl = sum(_number(row.get("realized_pnl_usd"))
                      for row in close_rows)
        else:
            # Pre-migration close rows historically stored cumulative PnL in
            # most paths. Do not add partial rows and double-count them.
            pnl = _number(close.get("realized_pnl_usd"))
        entry_fee = _number(opened.get("fee_usd"))
        exit_fee = _number(close.get("fee_usd"))
        entry_slippage = _number(opened.get("slippage_usd"))
        exit_slippage = _number(close.get("slippage_usd"))
        partial_fees = sum(_number(row.get("fee_usd"))
                           for row in partials.get(str(trade_id), []))
        partial_funding = sum(_number(row.get("funding_usd"))
                              for row in partials.get(str(trade_id), []))
        partial_slippage = sum(_number(row.get("slippage_usd"))
                               for row in partials.get(str(trade_id), []))
        partial_adverse_slippage = sum(
            _number(row.get("adverse_slippage_usd"))
            for row in partials.get(str(trade_id), []))
        matched.append({
            "trade_id": str(trade_id),
            "symbol": opened.get("symbol"),
            "open_ts": _number(opened.get("ts")),
            "close_ts": _number(close.get("ts")),
            "confidence": opened.get("confidence"),
            "strategy_id": (
                opened.get("strategy_id") or "legacy_unattributed"),
            "strategy_version": (
                opened.get("strategy_version") or "legacy"),
            "setup_id": opened.get("setup_id"),
            "setup_type": opened.get("setup_type") or "legacy",
            "exit_policy": opened.get("exit_policy"),
            "prompt_version": (
                opened.get("prompt_version") or "legacy"),
            "config_version": (
                opened.get("config_version") or "legacy"),
            "code_version": (
                opened.get("code_version") or "legacy"),
            "runtime_mode": (
                opened.get("runtime_mode") or "legacy_unknown"),
            "account_fingerprint": (
                opened.get("account_fingerprint") or "legacy_unknown"),
            "close_trigger": close.get("close_trigger"),
            "close_evidence": close.get("close_evidence"),
            "entry_equity_usd": _number(
                opened.get("entry_equity_usd"), 0.0),
            "pnl_semantics": (
                "incremental_v1" if incremental else "legacy_cumulative"),
            "notional_usd": notional,
            "risk_usd": risk,
            "net_pnl_usd": pnl,
            "return_on_notional_pct": pnl / notional * 100 if notional else None,
            "r_multiple": pnl / risk if risk else None,
            "fees_usd": entry_fee + partial_fees + exit_fee,
            "funding_usd": partial_funding + _number(close.get("funding_usd")),
            "slippage_usd": (entry_slippage + partial_slippage
                             + exit_slippage),
            "adverse_slippage_usd": (
                _number(opened.get("adverse_slippage_usd"))
                + partial_adverse_slippage
                + _number(close.get("adverse_slippage_usd"))
            ),
            # New close rows explicitly prove whether funding was recovered.
            # A missing legacy tag is unknown, not evidence of zero funding.
            "funding_status": (
                close.get("funding_status") or "legacy_unknown"),
            "funding_complete": (
                close.get("funding_status") == "available"),
            "open_fill_status": opened.get("fill_status"),
            "close_fill_status": close.get("fill_status"),
        })
        matched_ids.add(str(trade_id))

    open_ids = set(opens) - duplicates
    diagnostics = {
        "opens": sum(1 for event in events if event.get("action") == "open"),
        "closes": sum(1 for event in events if event.get("action") == "close"),
        "partial_closes": sum(
            1 for event in events if event.get("action") == "partial_close"),
        "unmatched_opens": len(open_ids - closed_ids),
        "unmatchable_closes": unmatchable_closes,
        "unscored_closes": unscored_closes,
        "duplicate_trade_ids": len(duplicates),
        "legacy_pnl_semantics": sum(
            trade["pnl_semantics"] == "legacy_cumulative"
            for trade in matched),
        "funding_incomplete": sum(
            not trade["funding_complete"] for trade in matched),
    }
    return matched, diagnostics


# --------------------------------------------------------------- metrics

def _drawdown(returns: list) -> float:
    """Max peak-to-trough of the cumulative R curve, as a positive number."""
    peak = running = worst = 0.0
    for value in returns:
        running += float(value)
        peak = max(peak, running)
        worst = min(worst, running - peak)
    return abs(worst)


def score_returns(returns: list, label: str = "") -> dict:
    """Metrics with intervals, and a refusal when the sample cannot support them.

    Every figure carries a bootstrap interval and the minimum detectable
    effect at the achieved n. A point estimate on forty trades is noise
    wearing a decimal point, and reporting it without the interval invites
    exactly the decision the promotion protocol exists to prevent.
    """
    clean = [float(r) for r in returns
             if r is not None and math.isfinite(float(r))]
    n = len(clean)
    if n == 0:
        return {"label": label, "n": 0, "verdict": INSUFFICIENT_SAMPLE,
                "expectancy_r": 0.0, "win_rate": 0.0, "profit_factor": 0.0,
                "total_r": 0.0, "max_drawdown_r": 0.0, "mde_r": float("inf"),
                "ci_low": 0.0, "ci_high": 0.0}

    wins = [r for r in clean if r > 0]
    losses = [r for r in clean if r < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    interval = bootstrap_mean(clean)
    mde = minimum_detectable_effect(clean)

    return {
        "label": label,
        "n": n,
        "expectancy_r": interval.point,
        "ci_low": interval.low,
        "ci_high": interval.high,
        "win_rate": len(wins) / n * 100.0,
        "profit_factor": (gross_win / gross_loss if gross_loss > 0
                          else float("inf") if gross_win > 0 else 0.0),
        "total_r": sum(clean),
        "max_drawdown_r": _drawdown(clean),
        "mde_r": mde,
        # The scorer says so rather than ranking noise. This is the guard
        # against rejecting a good hypothesis on twelve trades.
        "verdict": (INSUFFICIENT_SAMPLE if n < 100 else "SCORED"),
    }


def compare(variant_returns: list, baseline_returns: list,
            label: str = "") -> dict:
    """Difference between two arms, with a bootstrap interval on the delta.

    Takes the raw return series rather than two scored dicts. A scored dict
    has already collapsed to a point estimate and an interval, and the
    interval on a *difference* is not recoverable from the two marginal
    intervals - resampling both arms is the only way to get it.

    The distinction matters more than it sounds. Two arms whose intervals
    overlap can still differ significantly, and two whose intervals are wide
    can still have a tightly bounded difference when they move together.
    Reading significance off the marginals is the most common way to get a
    small-sample comparison wrong in the direction of a false positive.
    """
    from .stats import bootstrap_difference

    clean_v = [float(r) for r in variant_returns
               if r is not None and math.isfinite(float(r))]
    clean_b = [float(r) for r in baseline_returns
               if r is not None and math.isfinite(float(r))]
    if not clean_v or not clean_b:
        return {"label": label, "verdict": INSUFFICIENT_SAMPLE,
                "delta_r": 0.0, "ci_low": 0.0, "ci_high": 0.0,
                "significant": False, "n_variant": len(clean_v),
                "n_baseline": len(clean_b)}

    difference = bootstrap_difference(clean_v, clean_b)
    return {
        "label": label,
        "delta_r": difference.point,
        "ci_low": difference.low,
        "ci_high": difference.high,
        # An interval excluding zero is the only evidence of a real
        # difference this module will assert.
        "significant": difference.excludes_zero(),
        "n_variant": len(clean_v),
        "n_baseline": len(clean_b),
        "verdict": (INSUFFICIENT_SAMPLE
                    if min(len(clean_v), len(clean_b)) < 100 else "SCORED"),
    }


# ----------------------------------------------------------------- funnel

FUNNEL_STAGES = ("fired", "proposed", "vetoed", "executed")


def funnel_from_replay(result) -> dict:
    """Normalise a replay's funnel and check that it reconciles.

    The reconciliation matters because a funnel whose stages do not add up is
    worse than no funnel: it looks like a measurement and is arithmetic that
    happens to be wrong.
    """
    raw = dict(result.funnel or {})
    for stage in FUNNEL_STAGES:
        raw.setdefault(stage, 0)
    raw.setdefault("veto_reasons", {})
    raw["reconciles"] = (raw["fired"] == raw["vetoed"] + raw["executed"])
    return raw


def format_funnel(funnel: dict) -> str:
    lines = [f"contract fired   {funnel['fired']:>8,}",
             f"  -> proposed    {funnel['proposed']:>8,}",
             f"  -> vetoed      {funnel['vetoed']:>8,}",
             f"  -> executed    {funnel['executed']:>8,}"]
    reasons = funnel.get("veto_reasons") or {}
    if reasons:
        lines.append("     by reason:")
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            lines.append(f"       {count:>6,}  {reason}")
    if not funnel.get("reconciles", True):
        lines.append("  WARNING: funnel does not reconcile "
                     "(fired != vetoed + executed)")
    return "\n".join(lines)


def dominant_veto(funnel: dict) -> tuple[str | None, float]:
    """The veto that binds hardest, and what share of all vetoes it is.

    Gate G4 turns on this number. If one veto dominates and it sits
    downstream of the strategy contract, then no setting of any contract
    parameter can increase the trade count, and a parameter sweep would
    consume weeks to discover that the funnel narrows somewhere else.
    """
    reasons = funnel.get("veto_reasons") or {}
    total = sum(reasons.values())
    if not total:
        return None, 0.0
    reason, count = max(reasons.items(), key=lambda kv: kv[1])
    return reason, count / total * 100.0
