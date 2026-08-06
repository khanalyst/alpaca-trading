"""Bounded, research-only evidence context for mechanism authoring.

The authoring prompt must see what the persisted evidence actually says, not
only the names of retired mechanisms.  This module reads the public
``FindingsStore`` projections and emits small deterministic aggregates.  It
never writes findings, changes a verdict, or reaches runtime/exchange state.

Some useful summaries cannot be recovered from this store: most paper rows do
not retain the original observable feature values or a regime label.  Those
keys are deliberately omitted rather than reconstructed from a model or
filled with samples that were never persisted.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping


MAX_OUTCOMES = 24
MAX_VARIANTS = 32
MAX_CONDITIONAL_ROWS = 96
MAX_CONTEXT_ROWS = 64
MAX_NESTED_ITEMS = 64
MAX_TEXT_CHARS = 512

_MISSING_REASON_PREFIXES = (
    "data missing",
    "market data invalid",
    "market data missing",
)
_NULL_CODES = frozenset({
    "NO_BASELINE_RELATIVE_IMPROVEMENT",
    "NONPOSITIVE_AFTER_COST_EXPECTANCY",
    "BASELINE_DELTA_NONPOSITIVE",
    "SEGMENT_INCONSISTENT",
})
_NEAR_MISS_CODES = frozenset({
    "BASELINE_DELTA_NOT_ESTABLISHED",
    "CLOSED_SAMPLE_BELOW_FLOOR",
    "PAIRED_EVIDENCE_INADEQUATE",
    "SEGMENT_EXPECTANCY_UNAVAILABLE",
    "TWO_SEGMENTS_UNAVAILABLE",
})


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text(value) -> str:
    return str(value) if value is not None else ""


def _missing_reason(reason: object) -> bool:
    text = _text(reason).strip().lower()
    return any(text.startswith(prefix) for prefix in _MISSING_REASON_PREFIXES)


def _reason_codes(outcome: Mapping) -> list[str]:
    codes = {
        _text(item.get("code"))
        for item in (outcome.get("payload") or {}).get("reasons", [])
        if isinstance(item, Mapping) and _text(item.get("code"))
    }
    return sorted(codes)


def _interval(value: object) -> dict | None:
    if not isinstance(value, Mapping):
        return None
    result = {}
    for key in ("point", "low", "high"):
        number = _finite(value.get(key))
        if number is not None:
            result[key] = number
    if "n" in value:
        result["n"] = _int(value.get("n"))
    if "clusters" in value:
        result["clusters"] = _int(value.get("clusters"))
    return result or None


def _arm_metrics(arm: Mapping | None) -> dict:
    arm = arm if isinstance(arm, Mapping) else {}
    metrics = arm.get("trade_r_metrics")
    metrics = metrics if isinstance(metrics, Mapping) else {}
    return {
        "variant_id": _text(arm.get("variant_id")),
        "decisions": _int(arm.get("decision_count")),
        "fired": _int(arm.get("proposed_decisions")),
        "declined": _int(arm.get("vetoed_decisions")),
        "closed": _int(arm.get("closes")),
        "unfilled": _int(arm.get("unfilled")),
        "expectancy_r": _finite(metrics.get("expectancy_r")),
        "ci_low": _finite(metrics.get("ci_low")),
        "ci_high": _finite(metrics.get("ci_high")),
        "win_rate_pct": _finite(arm.get("win_rate_pct")),
        "net_pnl_usdt": _finite(arm.get("net_pnl_usdt")),
    }


def _outcome_context(outcomes: list[dict]) -> tuple[list, list, list, list, list]:
    nulls, near_misses, oos, family, segments = [], [], [], [], []
    for outcome in outcomes:
        payload = outcome.get("payload") or {}
        if not isinstance(payload, Mapping):
            continue
        codes = _reason_codes(outcome)
        base = {
            "outcome_id": _text(outcome.get("outcome_id")),
            "scope_key": _text(outcome.get("scope_key")),
            "strategy_id": _text(outcome.get("strategy_id")),
            "variant_id": _text(outcome.get("candidate_variant_id")),
            "verdict": _text(outcome.get("verdict")),
            "reason_codes": codes,
        }
        if codes and set(codes) & _NULL_CODES:
            nulls.append({
                **base,
                "candidate": _arm_metrics(payload.get("candidate")),
                "baseline": _arm_metrics(payload.get("baseline")),
            })
        if codes and set(codes) & _NEAR_MISS_CODES:
            near_misses.append(base)

        paired = payload.get("paired")
        if isinstance(paired, Mapping):
            fit = paired.get("fit")
            confirm = paired.get("confirm")
            if isinstance(fit, Mapping) or isinstance(confirm, Mapping):
                item = {
                    **base,
                    "split_cutoff_ts": _finite(
                        (payload.get("data_window") or {}).get(
                            "split_cutoff_ts")),
                    "fit": _paired_window(fit),
                    "confirm": _paired_window(confirm),
                }
                # Keep the row only when at least one persisted window has
                # usable evidence; an empty split is represented by nulls.
                if item["fit"] or item["confirm"]:
                    oos.append(item)

        split_cutoff = _finite(
            (payload.get("data_window") or {}).get("split_cutoff_ts"))
        if split_cutoff is not None:
            for arm_name in ("baseline", "candidate"):
                arm = payload.get(arm_name)
                if not isinstance(arm, Mapping):
                    continue
                rows = arm.get("decisions")
                if not isinstance(rows, list):
                    continue
                buckets = {"fit": [], "confirm": []}
                for row in rows:
                    if not isinstance(row, Mapping):
                        continue
                    ts = _finite(row.get("decision_ts"))
                    if ts is None:
                        continue
                    buckets["fit" if ts < split_cutoff else "confirm"].append(row)
                for segment_name, segment_rows in buckets.items():
                    segments.append({
                        **base,
                        "arm": arm_name,
                        "segment": segment_name,
                        **_segment_metrics(segment_rows),
                    })

        # Forward qualification stores family correction under an immutable
        # qualification event.  If the outcome itself carries a family
        # record, preserve it as well; no p-value is inferred from intervals.
        evidence = payload.get("evidence")
        family_record = evidence.get("family") if isinstance(evidence, Mapping) else None
        if isinstance(family_record, Mapping):
            family.append({**base, "family": _family_record(family_record)})

    return (
        _cap_sorted(nulls, MAX_CONTEXT_ROWS),
        _cap_sorted(near_misses, MAX_CONTEXT_ROWS),
        _cap_sorted(oos, MAX_CONTEXT_ROWS),
        _cap_sorted(family, MAX_CONTEXT_ROWS),
        _cap_sorted(segments, MAX_CONTEXT_ROWS),
    )


def _segment_metrics(rows: list[Mapping]) -> dict:
    fired = sum(_text(row.get("decision_outcome")).upper() == "PROPOSED"
                for row in rows)
    declined = sum(_text(row.get("decision_outcome")).upper() == "VETOED"
                   for row in rows)
    returns = []
    for row in rows:
        if (_text(row.get("trade_status")).upper() != "CLOSED"
                or _int(row.get("trade_valid_for_inference"), 1) == 0):
            continue
        value = _finite(row.get("trade_r_multiple"))
        if value is not None:
            returns.append(value)
    return {
        "opportunities": len(rows),
        "fired": fired,
        "declined": declined,
        "closed": len(returns),
        "mean_r": (sum(returns) / len(returns) if returns else None),
        "win_rate_pct": (sum(value > 0 for value in returns) / len(returns) * 100.0
                         if returns else None),
    }


def _paired_window(value: object) -> dict | None:
    if not isinstance(value, Mapping):
        return None
    result = {}
    for key in (
            "paired_n", "informative_pairs", "concordant_veto_pairs",
            "proposal_union_n", "left_only_proposals", "right_only_proposals",
            "left_unresolved", "right_unresolved", "left_duplicates",
            "right_duplicates"):
        if key in value:
            result[key] = _int(value.get(key))
    for key in ("pair_coverage_pct",):
        if key in value:
            result[key] = _finite(value.get(key))
    interval = _interval(value.get("interval"))
    if interval is not None:
        result["interval"] = interval
    bootstrap = value.get("bootstrap")
    if isinstance(bootstrap, Mapping):
        result["bootstrap"] = {
            key: (_int(bootstrap.get(key)) if key in {"clusters", "min_clusters"}
                  else bootstrap.get(key))
            for key in ("kind", "cluster_seconds", "seed", "clusters", "min_clusters")
            if key in bootstrap
        }
    return result or None


def _family_record(value: Mapping) -> dict:
    result = {}
    for key in ("family_n", "alpha", "p", "p_adjusted", "significant",
                "calibrated", "method", "null_assumption", "exact"):
        if key not in value:
            continue
        if key in {"family_n"}:
            result[key] = _int(value.get(key))
        elif key in {"alpha", "p", "p_adjusted"}:
            result[key] = _finite(value.get(key))
        else:
            result[key] = value.get(key)
    axes = value.get("axes")
    if isinstance(axes, list):
        result["axes"] = sorted(_text(item) for item in axes)[:MAX_CONTEXT_ROWS]
    return result


def _cap_sorted(items: list[dict], limit: int) -> list[dict]:
    return sorted(items, key=lambda item: tuple(
        _text(item.get(key)) for key in ("scope_key", "variant_id", "outcome_id")))[:limit]


def _paper_context(findings_store, scopes: list[str], variant_ids: list[str]):
    rates, conditional = [], []
    for scope in scopes:
        for variant_id in variant_ids:
            try:
                decisions = findings_store.paper_decisions_for(scope, variant_id)
                trades = findings_store.paper_trades_for(scope, variant_id)
            except Exception:  # noqa: BLE001 - evidence is advisory input
                continue
            if not decisions and not trades:
                continue
            trade_by_id = {
                _text(row.get("trade_id")): row for row in trades
                if isinstance(row, Mapping)
            }
            trade_by_proposal = {
                _text(row.get("proposal_id")): row for row in trades
                if isinstance(row, Mapping)
            }
            groups = defaultdict(lambda: {
                "opportunities": 0, "fired": 0, "declined": 0,
                "closed": 0, "wins": 0, "returns": [], "net_pnl": 0.0,
            })
            missing = declined = fired = 0
            for decision in decisions:
                if not isinstance(decision, Mapping):
                    continue
                outcome = _text(decision.get("decision_outcome")).upper()
                declined += outcome == "VETOED"
                fired += outcome == "PROPOSED"
                reason = decision.get("reason")
                missing += outcome == "VETOED" and _missing_reason(reason)
                for dimension in ("symbol", "direction", "setup_type"):
                    value = decision.get(dimension)
                    if value is None or not _text(value):
                        continue
                    group = groups[(dimension, _text(value))]
                    group["opportunities"] += 1
                    group["fired"] += outcome == "PROPOSED"
                    group["declined"] += outcome == "VETOED"
                    trade = trade_by_id.get(_text(decision.get("paper_trade_id")))
                    if trade is None:
                        trade = trade_by_proposal.get(_text(decision.get("proposal_id")))
                    if not isinstance(trade, Mapping):
                        continue
                    if (_text(trade.get("status")).upper() != "CLOSED"
                            or _text(trade.get("result")) == "unfilled"
                            or _int(trade.get("valid_for_inference"), 1) == 0):
                        continue
                    result = _finite(trade.get("r_multiple"))
                    if result is None:
                        continue
                    group["closed"] += 1
                    group["wins"] += result > 0
                    group["returns"].append(result)
                    group["net_pnl"] += _finite(trade.get("net_pnl_usd")) or 0.0

            total = len(decisions)
            rates.append({
                "scope_key": scope,
                "variant_id": variant_id,
                "opportunities": total,
                "fired": fired,
                "declined": declined,
                "fire_rate_pct": (fired / total * 100.0 if total else None),
                "decline_rate_pct": (declined / total * 100.0 if total else None),
                "missing_data": missing,
                "missing_data_rate_pct": (missing / total * 100.0 if total else None),
            })
            for (dimension, value), group in sorted(groups.items()):
                returns = group["returns"]
                conditional.append({
                    "scope_key": scope,
                    "variant_id": variant_id,
                    "dimension": dimension,
                    "value": value,
                    "opportunities": group["opportunities"],
                    "fired": group["fired"],
                    "declined": group["declined"],
                    "closed": group["closed"],
                    "mean_r": (sum(returns) / len(returns) if returns else None),
                    "win_rate_pct": (group["wins"] / len(returns) * 100.0
                                     if returns else None),
                    "net_pnl_usdt": group["net_pnl"],
                })
    return (_cap_sorted(rates, MAX_CONTEXT_ROWS),
            _cap_sorted(conditional, MAX_CONDITIONAL_ROWS))


def _outcome_paper_context(outcomes: list[dict]):
    """Fallback aggregates from immutable outcome arm decisions.

    Some historical stores expose terminal outcomes before the paper
    projection methods were added.  The decision rows embedded in the
    immutable payload still support firing/decline and conditional returns,
    so use them only for scope/variant pairs not already covered by paper
    rows.
    """
    rates, conditional = [], []
    for outcome in outcomes:
        payload = outcome.get("payload") or {}
        if not isinstance(payload, Mapping):
            continue
        scope = _text(outcome.get("scope_key"))
        for arm_name in ("baseline", "candidate"):
            arm = payload.get(arm_name)
            if not isinstance(arm, Mapping):
                continue
            variant_id = _text(arm.get("variant_id")) or _text(
                outcome.get(f"{arm_name}_variant_id"))
            rows = arm.get("decisions")
            if not variant_id or not isinstance(rows, list):
                continue
            groups = defaultdict(lambda: {
                "opportunities": 0, "fired": 0, "declined": 0,
                "closed": 0, "wins": 0, "returns": [], "net_pnl": None,
            })
            fired = declined = missing = 0
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                decision = _text(row.get("decision_outcome")).upper()
                is_fired = decision == "PROPOSED"
                is_declined = decision == "VETOED"
                fired += is_fired
                declined += is_declined
                missing += is_declined and _missing_reason(row.get("reason"))
                trade_r = _finite(row.get("trade_r_multiple"))
                valid = (_text(row.get("trade_status")).upper() == "CLOSED"
                         and _int(row.get("trade_valid_for_inference"), 1) == 1
                         and trade_r is not None)
                for dimension in ("symbol", "direction", "setup_type"):
                    value = row.get(dimension)
                    if value is None or not _text(value):
                        continue
                    group = groups[(dimension, _text(value))]
                    group["opportunities"] += 1
                    group["fired"] += is_fired
                    group["declined"] += is_declined
                    if valid:
                        group["closed"] += 1
                        group["wins"] += trade_r > 0
                        group["returns"].append(trade_r)
                        net_pnl = _finite(row.get("trade_net_pnl_usdt"))
                        if net_pnl is not None:
                            group["net_pnl"] = (
                                (group["net_pnl"] or 0.0) + net_pnl)
            total = sum(isinstance(row, Mapping) for row in rows)
            rates.append({
                "scope_key": scope,
                "variant_id": variant_id,
                "opportunities": total,
                "fired": fired,
                "declined": declined,
                "fire_rate_pct": (fired / total * 100.0 if total else None),
                "decline_rate_pct": (declined / total * 100.0 if total else None),
                "missing_data": missing,
                "missing_data_rate_pct": (missing / total * 100.0 if total else None),
            })
            for (dimension, value), group in sorted(groups.items()):
                returns = group["returns"]
                conditional.append({
                    "scope_key": scope,
                    "variant_id": variant_id,
                    "dimension": dimension,
                    "value": value,
                    "opportunities": group["opportunities"],
                    "fired": group["fired"],
                    "declined": group["declined"],
                    "closed": group["closed"],
                    "mean_r": (sum(returns) / len(returns) if returns else None),
                    "win_rate_pct": (group["wins"] / len(returns) * 100.0
                                     if returns else None),
                    "net_pnl_usdt": group["net_pnl"],
                })
    return (_cap_sorted(rates, MAX_CONTEXT_ROWS),
            _cap_sorted(conditional, MAX_CONDITIONAL_ROWS))


def _qualification_family_context(findings_store, variant_ids: list[str], scopes: list[str]):
    rows = []
    for variant_id in variant_ids:
        try:
            events = findings_store.qualification_events(variant_id)
        except Exception:  # noqa: BLE001
            continue
        for event in events or []:
            if not isinstance(event, Mapping):
                continue
            event_scope = _text(event.get("scope_key"))
            if scopes and event_scope not in set(scopes) | {"*"}:
                continue
            detail = event.get("detail")
            if not isinstance(detail, Mapping):
                continue
            family = detail.get("family")
            if not isinstance(family, Mapping):
                continue
            rows.append({
                "variant_id": variant_id,
                "scope_key": event_scope,
                "status": _text(event.get("status")),
                "event_id": _text(event.get("event_id")),
                "family": _family_record(family),
            })
    return _cap_sorted(rows, MAX_CONTEXT_ROWS)


def _stored_dimension_context(findings_store, variant_ids: list[str], scopes: list[str]):
    """Read optional feature/regime summaries only when an analysis stores them."""
    features, regimes, out_of_sample = [], [], []
    allowed_scopes = set(scopes) | {"*"}
    for variant_id in variant_ids:
        try:
            events = findings_store.qualification_events(variant_id)
        except Exception:  # noqa: BLE001
            continue
        for event in events or []:
            if not isinstance(event, Mapping):
                continue
            event_scope = _text(event.get("scope_key"))
            if allowed_scopes and event_scope not in allowed_scopes:
                continue
            analysis_id = _text(event.get("source_analysis_id"))
            if not analysis_id:
                continue
            try:
                analysis = findings_store.analysis(analysis_id)
            except Exception:  # noqa: BLE001
                continue
            payload = analysis.get("payload") if isinstance(analysis, Mapping) else None
            if not isinstance(payload, Mapping):
                continue
            evidence = payload.get("evidence")
            candidates = [payload]
            if isinstance(evidence, Mapping):
                candidates.append(evidence)
            for source in candidates:
                if not isinstance(source, Mapping):
                    continue
                for key, target in (("feature_summary", features),
                                    ("features", features),
                                    ("feature_context", features)):
                    value = source.get(key)
                    if isinstance(value, (Mapping, list)):
                        target.append({
                            "variant_id": variant_id,
                            "scope_key": event_scope,
                            "analysis_id": analysis_id,
                            "summary": _bounded_value(value),
                        })
                split = source.get("split")
                if not isinstance(split, Mapping):
                    continue
                if (isinstance(split.get("fit"), Mapping)
                        or isinstance(split.get("confirm"), Mapping)):
                    out_of_sample.append({
                        "variant_id": variant_id,
                        "scope_key": event_scope,
                        "analysis_id": analysis_id,
                        "survives": split.get("survives"),
                        "reason": _text(split.get("reason"))[:MAX_TEXT_CHARS],
                        "fit": _score_summary(split.get("fit")),
                        "confirm": _score_summary(split.get("confirm")),
                    })
                for segment in ("fit", "confirm"):
                    regime = split.get(f"{segment}_regime")
                    if isinstance(regime, Mapping):
                        regimes.append({
                            "variant_id": variant_id,
                            "scope_key": event_scope,
                            "analysis_id": analysis_id,
                            "segment": segment,
                            "regime": regime,
                        })
    return (
        _cap_sorted(features, MAX_CONTEXT_ROWS),
        _cap_sorted(regimes, MAX_CONTEXT_ROWS),
        _cap_sorted(out_of_sample, MAX_CONTEXT_ROWS),
    )


def _score_summary(value: object) -> dict | None:
    if not isinstance(value, Mapping):
        return None
    result = {}
    for key in ("n", "clusters"):
        if key in value:
            result[key] = _int(value.get(key))
    for key in ("expectancy_r", "ci_low", "ci_high", "mean_r", "net_pnl_usdt"):
        if key in value:
            result[key] = _finite(value.get(key))
    return result or None


def build_evidence_context(
        findings_store=None, *, scope_key: str | None = None,
        history: Mapping | None = None) -> dict:
    """Build a stable, JSON-safe evidence summary from persisted findings.

    ``history`` is accepted so callers that already loaded a durable context
    can pass it through without another store read.  A supplied history
    ``evidence``/``evidence_summary`` is treated as data, then normalized by
    the same bounded JSON contract.
    """
    supplied = (history or {}).get("evidence_summary")
    if supplied is None:
        supplied = (history or {}).get("evidence")
    if isinstance(supplied, Mapping):
        return _bound_supplied(supplied)
    if findings_store is None:
        return {"schema": "authoring_evidence.v1", "source": "none"}

    try:
        scopes = [str(value) for value in findings_store.paper_scopes()]
    except Exception:  # noqa: BLE001
        scopes = []
    if scope_key is not None:
        scopes = [str(scope_key)] if str(scope_key) in scopes else [str(scope_key)]

    try:
        outcomes = list(findings_store.experiment_outcomes(scope_key))
    except Exception:  # noqa: BLE001
        outcomes = []
    outcomes = sorted(
        (item for item in outcomes if isinstance(item, Mapping)),
        key=lambda item: (_finite(item.get("created_ts")) or 0.0,
                          _text(item.get("outcome_id"))))[-MAX_OUTCOMES:]

    variant_ids = set()
    for item in outcomes:
        for key in ("baseline_variant_id", "candidate_variant_id"):
            if item.get(key):
                variant_id = str(item[key])
                if not variant_id.endswith(".baseline"):
                    variant_ids.add(variant_id)
    try:
        registered = findings_store.variants()
    except Exception:  # noqa: BLE001
        registered = []
    for row in registered or []:
        if isinstance(row, Mapping) and row.get("variant_id"):
            variant_id = str(row["variant_id"])
            if not variant_id.endswith(".baseline"):
                variant_ids.add(variant_id)
    variant_ids = sorted(variant_ids)[:MAX_VARIANTS]

    rates, conditional = _paper_context(findings_store, scopes, variant_ids)
    fallback_rates, fallback_conditional = _outcome_paper_context(outcomes)
    covered = {(row.get("scope_key"), row.get("variant_id")) for row in rates}
    rates.extend(row for row in fallback_rates
                 if (row.get("scope_key"), row.get("variant_id")) not in covered)
    covered_conditional = {
        (row.get("scope_key"), row.get("variant_id")) for row in conditional}
    conditional.extend(
        row for row in fallback_conditional
        if (row.get("scope_key"), row.get("variant_id"))
        not in covered_conditional)
    rates = _cap_sorted(rates, MAX_CONTEXT_ROWS)
    conditional = _cap_sorted(conditional, MAX_CONDITIONAL_ROWS)
    nulls, near_misses, oos, family, segments = _outcome_context(outcomes)
    family.extend(_qualification_family_context(findings_store, variant_ids, scopes))
    feature_context, regime_context, analysis_oos = _stored_dimension_context(
        findings_store, variant_ids, scopes)
    oos = _cap_sorted(oos + analysis_oos, MAX_CONTEXT_ROWS)

    result = {
        "schema": "authoring_evidence.v1",
        "source": "persisted_findings_store",
        "scope_keys": sorted(scopes),
        "firing_rates": rates,
        "conditional_returns": conditional,
        "null_context": nulls,
        "near_miss_context": near_misses,
        "out_of_sample": oos,
        "segment_context": segments,
        "family_context": _cap_sorted(family, MAX_CONTEXT_ROWS),
    }
    if feature_context:
        result["feature_context"] = feature_context
    if regime_context:
        result["regime_context"] = regime_context
    # Keys for unsupported feature/segment/regime tables are intentionally not
    # emitted. If a future persisted API adds those values, it can add them in
    # a dedicated reader without changing this contract's safety properties.
    return _bounded_value(_json_safe(result))


def _json_safe(value):
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_json_safe(item) for item in value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _bound_supplied(value: Mapping) -> dict:
    """Keep caller-provided evidence within the same prompt-size contract."""
    result = {str(key): value[key] for key in value}
    for key, limit in (
            ("firing_rates", MAX_CONTEXT_ROWS),
            ("conditional_returns", MAX_CONDITIONAL_ROWS),
            ("null_context", MAX_CONTEXT_ROWS),
            ("near_miss_context", MAX_CONTEXT_ROWS),
            ("out_of_sample", MAX_CONTEXT_ROWS),
            ("segment_context", MAX_CONTEXT_ROWS),
            ("family_context", MAX_CONTEXT_ROWS),
            ("feature_context", MAX_CONTEXT_ROWS),
            ("regime_context", MAX_CONTEXT_ROWS)):
        rows = result.get(key)
        if isinstance(rows, list):
            result[key] = _cap_sorted(
                [dict(row) for row in rows if isinstance(row, Mapping)], limit)
    return _bounded_value(_json_safe(result))


def _bounded_value(value, depth: int = 0):
    """Normalize arbitrary persisted metadata without unbounded prompt text."""
    if depth > 4:
        if isinstance(value, (Mapping, list, tuple, set, frozenset)):
            return "[truncated]"
        if isinstance(value, str):
            return value[:MAX_TEXT_CHARS]
        return _json_safe(value)
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_value(value[key], depth + 1)
            for key in sorted(value, key=str)[:MAX_NESTED_ITEMS]
        }
    if isinstance(value, (list, tuple)):
        return [_bounded_value(item, depth + 1)
                for item in value[:MAX_NESTED_ITEMS]]
    if isinstance(value, (set, frozenset)):
        return [_bounded_value(item, depth + 1)
                for item in sorted(value, key=str)[:MAX_NESTED_ITEMS]]
    if isinstance(value, str):
        return value[:MAX_TEXT_CHARS]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (int, bool)):
        return value
    return str(value)[:MAX_TEXT_CHARS]
