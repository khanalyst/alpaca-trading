"""Versioned momentum strategy contracts and setup idempotency.

The model still decides whether a candidate is worth trading and which setup
best explains it. This module owns the parts that must be reproducible:

- broad setup-contract checks (to prevent relabelling arbitrary trades);
- semantic stop/exit-policy conversion into deterministic percentages;
- an extreme no-chase boundary;
- stable setup IDs tied to the completed signal candle.
"""

from __future__ import annotations

import hashlib
import json
import time

from . import hypotheses
from .contracts import EVIDENCE_BUILDERS, finite as _finite
from .registry import spec_for


# Every setup type any registered strategy may use. The per-strategy subset
# lives on the spec (StrategySpec.setup_types) and is what build_setup_plan
# actually enforces; this union exists so signal_probe can bound an untrusted
# label before the spec is consulted.
SETUP_TYPES = {
    "trend_continuation",
    "range_breakout",
    "funding_squeeze",
    "other",
    "flush_reversion",
    "carry",
    "trend_follow",
    "positioning_fade",
    "positioning_unwind",
    "spread_capture",
}
INVALIDATION_ANCHORS = {"structure", "atr"}
# structure_target was removed in batch 6.1. It computed
# max(stop_pct * fixed_rr, distance_to_recent_extreme), and in both setups it
# was designed for the first term always won: a range_breakout fires at
# range_pos_pct >= 85, so price is already at the highs and the remaining
# distance is ~0, and a trend_continuation pullback sits 1-2% from the swing
# while stop_pct * fixed_rr is ~4%. The policy was therefore identical to
# fixed_rr precisely where it was meant to differ. An inert choice is worse
# than no choice: it makes the decision space look richer than it is, and
# attributes outcomes to a policy that never applied.
EXIT_POLICIES = {"fixed_rr", "extended_rr"}
EXECUTION_CHOICES = {"normal", "retry_smaller"}
SETUP_STATUSES = {
    "proposed", "risk_rejected", "attempted", "execution_rejected",
    "opened", "closed",
}


def _hash(payload: object) -> str:
    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def identity(cfg: dict) -> tuple[str, str]:
    block = cfg["strategy"]
    return str(block["id"]), str(block["version"])


def signal_probe(decision: dict, symbol_snapshot: dict,
                 cfg: dict) -> dict | None:
    """Build an idempotency identity before the full setup contract runs."""
    signal_ts = _finite(symbol_snapshot.get("signal_ts"))
    symbol = decision.get("symbol")
    direction = decision.get("direction")
    if (signal_ts is None or signal_ts < 0 or not isinstance(symbol, str)
            or not symbol or direction not in {"long", "short"}):
        return None
    strategy_id, strategy_version = identity(cfg)
    setup_type = str(decision.get("setup_type") or "invalid")[:80]
    if setup_type == "other":
        # Match the attribution build_setup_plan will assign, so the
        # idempotency identity and the recorded trade agree. An unregistered
        # or absent id keeps a distinct bucket rather than silently joining
        # a real hypothesis's row - the plan will reject it moments later.
        spec = hypotheses.spec_for(decision.get("hypothesis_id"))
        if spec is not None:
            strategy_id, strategy_version = spec.attribution(strategy_id)
        else:
            strategy_id = f"{strategy_id}-experimental-unregistered"
            strategy_version = f"{strategy_version}-experimental-v1"
    setup_key = _hash({
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "symbol": symbol,
        "direction": direction,
        "setup_type": setup_type,
    })
    return {
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        # Rejected evaluations use a per-symbol/per-bar ID. Valid setup IDs
        # remain more specific and include their semantic setup key.
        "setup_id": _hash({
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "symbol": symbol,
            "signal_ts": signal_ts,
        }),
        "setup_key": setup_key,
        "setup_type": setup_type,
        "symbol": symbol,
        "direction": direction,
        "signal_ts": signal_ts,
    }


def setup_evidence(snapshot: dict, cfg: dict) -> dict:
    """Return auditable minimum evidence for the active strategy's setups.

    Dispatches to the contract registered for ``strategy.id``. The contract
    is deterministic and authoritative: the model may only choose among the
    setups it declares valid, never invent one. Configuration validation has
    already proved the id is registered and implemented, so an unknown id
    here means the register and the contract modules have drifted apart,
    which is a bug rather than a runtime condition to absorb.
    """
    strategy_id = str(cfg["strategy"]["id"])
    try:
        builder = EVIDENCE_BUILDERS[strategy_id]
    except KeyError:
        raise KeyError(
            f"no evidence contract is registered for strategy.id "
            f"{strategy_id!r}; registered: "
            f"{', '.join(sorted(EVIDENCE_BUILDERS))}") from None
    return builder(snapshot, cfg)


def enrich_snapshot(snapshot: dict, cfg: dict) -> dict:
    snapshot["setup_evidence"] = setup_evidence(snapshot, cfg)
    return snapshot


def _bucket(value: object, thresholds: tuple[float, ...]) -> int | None:
    number = _finite(value)
    if number is None:
        return None
    return sum(number >= threshold for threshold in thresholds)


def evidence_fingerprint(snapshot: dict) -> str:
    """Fingerprint meaningful evidence buckets, not every market tick."""
    return _hash({
        "trends": [
            snapshot.get("trend_15m"),
            snapshot.get("trend_1h"),
            snapshot.get("trend_4h"),
        ],
        "regime": snapshot.get("regime"),
        "momentum_15m": _bucket(
            snapshot.get("mom_15m_pct"), (-0.5, 0.0, 0.5)),
        "momentum_1h": _bucket(
            snapshot.get("mom_1h_pct"), (-1.0, 0.0, 1.0)),
        "relative_volume": _bucket(
            snapshot.get("relative_volume_1h"), (0.75, 1.0, 1.5, 2.0)),
        "range_position": _bucket(
            snapshot.get("range_pos_pct"), (20, 40, 60, 80)),
        "funding": _bucket(
            snapshot.get("funding_rate_pct"), (-0.05, -0.02, 0, 0.02, 0.05)),
        "fresh_breakout_long": snapshot.get("fresh_breakout_long") is True,
        "fresh_breakout_short": snapshot.get("fresh_breakout_short") is True,
        "price_stabilized_long": (
            snapshot.get("price_stabilized_long") is True),
        "price_stabilized_short": (
            snapshot.get("price_stabilized_short") is True),
    })


def compact_entry_evidence(
        snapshot: dict, market_context: dict | None = None) -> dict:
    """Persist enough original evidence for a later close comparison."""
    fields = (
        "signal_ts", "signal_1h_ts", "trend_15m", "trend_1h", "trend_4h",
        "regime", "mom_15m_pct", "mom_1h_pct", "relative_volume_1h",
        "range_pos_pct", "atr_1h_pct", "atr_1h_ratio",
        "ema20_1h_dist_pct", "funding_rate_pct", "funding_percentile_30",
        "perp_index_basis_pct", "open_interest_musd",
        "corr_btc_1h_72_shrunk", "beta_btc_1h_72",
        "corr_btc_downside_1h_72", "corr_btc_samples",
        "fresh_breakout_long", "fresh_breakout_short",
        "price_stabilized_long", "price_stabilized_short",
    )
    evidence = {
        key: snapshot.get(key) for key in fields if key in snapshot
    }
    evidence["evidence_fingerprint"] = evidence_fingerprint(snapshot)
    if isinstance(snapshot.get("setup_evidence"), dict):
        evidence["setup_evidence"] = snapshot["setup_evidence"]
    if isinstance(market_context, dict):
        evidence["btc_market_context"] = {
            key: market_context.get(key)
            for key in (
                "regime", "atr_1h_ratio", "relative_volume_1h",
                "mom_1h_pct")
            if key in market_context
        }
    return evidence


def build_setup_plan(decision: dict, symbol_snapshot: dict,
                     cfg: dict, *, hypothesis_params: dict | None = None
                     ) -> tuple[dict | None, str | None]:
    """Validate one model-labelled setup and derive deterministic SL/TP."""
    direction = decision.get("direction")
    if direction not in {"long", "short"}:
        return None, "setup direction is invalid"
    setup_type = str(decision.get("setup_type") or "")
    # A setup type belongs to a strategy. Checking against the registered
    # spec rather than a module-level set stops one strategy's archetypes
    # from being labellable while a different strategy is running.
    if setup_type not in spec_for(str(cfg["strategy"]["id"])).setup_types:
        return None, "setup type is not recognised"
    hypothesis_id = str(decision.get("hypothesis_id") or "")
    if setup_type == "other":
        # 6.3: an experimental setup must now name a registered hypothesis.
        # Without one it is the unlabelled bucket again, and a category
        # meaning "everything else" teaches nothing.
        if (cfg["mode"] != "demo"
                or not cfg["strategy"]["allow_experimental_setups_in_demo"]):
            return None, "experimental setups are allowed only in demo mode"
        if not hypothesis_id:
            return None, ("an experimental setup must name a registered "
                          "hypothesis_id")
        fires, why = hypotheses.evaluate(
            hypothesis_id, symbol_snapshot, direction, cfg,
            hypothesis_params)
        if not fires:
            return None, f"hypothesis {hypothesis_id}: {why}"
    elif hypothesis_id:
        return None, ("hypothesis_id belongs to an experimental setup, not "
                      f"to {setup_type}")

    anchor = str(decision.get("invalidation_anchor") or "")
    if anchor not in INVALIDATION_ANCHORS:
        return None, "invalidation anchor is not recognised"
    # Every contracted setup requires a structure invalidation, funding_squeeze
    # included. It was previously the one setup permitted the ATR anchor, which
    # yields exactly min_stop_atr_multiple - the narrowest stop the system can
    # produce - for a counter-trend, mean-reversion entry into a crowded book.
    # That is backwards: fading a crowd needs more room than following one.
    if setup_type in {"trend_continuation", "range_breakout",
                      "funding_squeeze"} and anchor != "structure":
        return None, f"{setup_type} requires a structure invalidation"

    exit_policy = str(decision.get("exit_policy") or "")
    if exit_policy not in EXIT_POLICIES:
        return None, "exit policy is not recognised"
    execution_choice = str(decision.get("execution_choice") or "normal")
    if execution_choice not in EXECUTION_CHOICES:
        return None, "execution choice is not recognised"

    evidence = symbol_snapshot.get("setup_evidence")
    if not isinstance(evidence, dict):
        evidence = setup_evidence(symbol_snapshot, cfg)
    contract = evidence.get(setup_type)
    if setup_type != "other" and (
            not isinstance(contract, dict) or contract.get(direction) is not True):
        return None, f"{setup_type} evidence contract is not met"

    extensions = evidence.get("extension_atr") or {}
    extension = _finite(extensions.get(direction))
    hard_limit = float(cfg["strategy"]["hard_max_entry_extension_atr"])
    if extension is None:
        return None, "entry extension cannot be measured"
    if extension > hard_limit:
        return None, (
            f"entry is {extension:.2f} ATR from the 1h EMA20; "
            f"hard no-chase limit is {hard_limit:.2f} ATR")

    signal_ts = _finite(symbol_snapshot.get("signal_ts"))
    atr = _finite(symbol_snapshot.get("atr_1h_pct"))
    if signal_ts is None or signal_ts < 0:
        return None, "completed signal candle timestamp is unavailable"
    if atr is None or atr <= 0:
        return None, "ATR is unavailable for deterministic stop placement"

    block = cfg["strategy"]
    minimum_stop = atr * float(block["min_stop_atr_multiple"])
    structure_field = (
        "swing_low_pct" if direction == "long" else "swing_high_pct")
    structure_distance = _finite(symbol_snapshot.get(structure_field))
    if anchor == "structure":
        if structure_distance is None or structure_distance < 0:
            return None, "structure invalidation distance is unavailable"
        stop_pct = max(
            minimum_stop,
            structure_distance
            + atr * float(block["structure_buffer_atr_multiple"]),
        )
    else:
        stop_pct = minimum_stop

    fixed_rr = float(block["fixed_reward_risk"])
    if exit_policy == "extended_rr":
        take_pct = stop_pct * float(block["extended_reward_risk"])
    else:
        take_pct = stop_pct * fixed_rr

    strategy_id, strategy_version = identity(cfg)
    if setup_type == "other":
        # Its own attribution, so ten experiments produce ten rows rather
        # than one average that describes none of them.
        strategy_id, strategy_version = hypotheses.spec_for(
            hypothesis_id).attribution(strategy_id)
    setup_key = _hash({
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "symbol": decision.get("symbol"),
        "direction": direction,
        "setup_type": setup_type,
    })
    setup_id = _hash({
        "setup_key": setup_key,
        "signal_ts": signal_ts,
    })
    enriched = dict(decision)
    enriched.update({
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "setup_id": setup_id,
        "setup_key": setup_key,
        "setup_type": setup_type,
        "signal_ts": signal_ts,
        "invalidation_anchor": anchor,
        "exit_policy": exit_policy,
        "execution_choice": execution_choice,
        "what_changed_since_last_loss": str(
            decision.get("what_changed_since_last_loss") or "")[:300],
        "entry_evidence_fingerprint": evidence_fingerprint(symbol_snapshot),
        "entry_signal_1h_ts": _finite(
            symbol_snapshot.get("signal_1h_ts")),
        "stop_loss_pct": round(stop_pct, 6),
        "take_profit_pct": round(take_pct, 6),
        # Risk and leverage are deterministic. Explicitly discard any numeric
        # authority a malformed or legacy model response attempted to include.
        "size_pct_equity": 0.0,
        "leverage": int(cfg["risk"]["entry_leverage"]),
    })
    return enriched, None


def new_setup_record(plan: dict, cfg: dict,
                     now: float | None = None) -> dict:
    current = float(now if now is not None else time.time())
    return {
        "strategy_id": str(plan["strategy_id"]),
        "strategy_version": str(plan["strategy_version"]),
        "setup_key": str(plan["setup_key"]),
        "setup_type": str(plan["setup_type"]),
        "symbol": str(plan["symbol"]),
        "direction": str(plan["direction"]),
        "signal_ts": float(plan["signal_ts"]),
        "first_seen_at": current,
        "last_seen_at": current,
        "blocked_until": 0.0,
        "expires_at": (
            current + float(cfg["strategy"]["setup_memory_hours"]) * 3600),
        "status": "proposed",
        "entry_evidence_fingerprint": plan.get(
            "entry_evidence_fingerprint"),
        "entry_signal_1h_ts": plan.get("entry_signal_1h_ts"),
        "outcome": None,
    }


def mark_setup(record: dict, status: str, cfg: dict,
               *, now: float | None = None,
               apply_cooldown: bool = False,
               realized_pnl_usd: float | None = None) -> None:
    if status not in SETUP_STATUSES:
        raise ValueError(f"invalid setup status: {status}")
    current = float(now if now is not None else time.time())
    record["status"] = status
    record["last_seen_at"] = current
    if status == "closed":
        record["closed_at"] = current
        if realized_pnl_usd is None:
            record["outcome"] = "unknown"
        else:
            pnl = float(realized_pnl_usd)
            record["realized_pnl_usd"] = pnl
            record["outcome"] = (
                "win" if pnl > 0 else "loss" if pnl < 0 else "flat")
    if apply_cooldown:
        record["blocked_until"] = max(
            float(record.get("blocked_until") or 0),
            current + float(cfg["strategy"]["setup_cooldown_minutes"]) * 60,
        )
    record["expires_at"] = max(
        float(record.get("expires_at") or 0),
        current + float(cfg["strategy"]["setup_memory_hours"]) * 3600,
        float(record.get("blocked_until") or 0),
    )


def semantic_block(records: dict, setup_key: str,
                   now: float | None = None) -> dict | None:
    current = float(now if now is not None else time.time())
    matches = [
        record for record in records.values()
        if isinstance(record, dict)
        and record.get("setup_key") == setup_key
        and float(record.get("blocked_until") or 0) > current
    ]
    return max(matches, key=lambda item: float(item["blocked_until"])) \
        if matches else None


def failed_thesis_reentry_reason(
        records: dict, plan: dict, cfg: dict,
        now: float | None = None) -> str | None:
    """Require time, a fresh completed 1h bar and changed objective evidence."""
    current = float(now if now is not None else time.time())
    losses = [
        record for record in records.values()
        if isinstance(record, dict)
        and record.get("setup_key") == plan.get("setup_key")
        and record.get("outcome") == "loss"
    ]
    if not losses:
        return None
    previous = max(
        losses,
        key=lambda record: float(
            record.get("closed_at") or record.get("last_seen_at") or 0))
    closed_at = float(
        previous.get("closed_at") or previous.get("last_seen_at") or 0)
    wait_seconds = float(
        cfg["strategy"]["loss_reentry_min_minutes"]) * 60
    if current < closed_at + wait_seconds:
        remaining = (closed_at + wait_seconds - current) / 60
        return (
            f"failed-thesis re-entry requires {remaining:.1f} more "
            "minute(s)")
    old_hour = _finite(previous.get("entry_signal_1h_ts"))
    new_hour = _finite(plan.get("entry_signal_1h_ts"))
    if old_hour is None or new_hour is None or new_hour <= old_hour:
        return "failed-thesis re-entry requires a fresh completed 1h candle"
    old_evidence = previous.get("entry_evidence_fingerprint")
    new_evidence = plan.get("entry_evidence_fingerprint")
    if not old_evidence or not new_evidence or old_evidence == new_evidence:
        return "failed-thesis re-entry requires changed objective evidence"
    explanation = str(
        plan.get("what_changed_since_last_loss") or "").strip()
    if len(explanation) < 12:
        return (
            "failed-thesis re-entry requires what_changed_since_last_loss")
    return None


def evaluated_signal(records: dict, plan: dict) -> dict | None:
    """Find an earlier evaluation of this symbol's completed signal bar.

    A model cannot evade per-candle idempotency by changing its setup label or
    flipping direction between cycles. A newly completed signal candle remains
    a fresh opportunity for the model to reason about.
    """
    signal_ts = float(plan["signal_ts"])
    for record in records.values():
        if not isinstance(record, dict):
            continue
        if (
            record.get("symbol") == plan.get("symbol")
            and float(record.get("signal_ts") or -1) == signal_ts
        ):
            return record
    return None


def prune_records(records: dict, now: float | None = None) -> dict:
    current = float(now if now is not None else time.time())
    return {
        setup_id: record
        for setup_id, record in records.items()
        if isinstance(record, dict)
        and float(record.get("expires_at") or 0) > current
    }


def recent_setup_view(records: dict, now: float | None = None) -> list[dict]:
    current = float(now if now is not None else time.time())
    rows = []
    for setup_id, record in records.items():
        if not isinstance(record, dict):
            continue
        expires_at = float(record.get("expires_at") or 0)
        if expires_at <= current:
            continue
        blocked_until = float(record.get("blocked_until") or 0)
        rows.append({
            "setup_id": setup_id,
            "symbol": record.get("symbol"),
            "direction": record.get("direction"),
            "setup_type": record.get("setup_type"),
            "status": record.get("status"),
            "signal_ts": record.get("signal_ts"),
            "outcome": record.get("outcome"),
            "closed_at": record.get("closed_at"),
            "retry_after_minutes": round(
                max(0.0, blocked_until - current) / 60, 1),
            "_last_seen_at": float(record.get("last_seen_at") or 0),
        })
    rows.sort(key=lambda row: row["_last_seen_at"])
    for row in rows:
        row.pop("_last_seen_at", None)
    return rows[-20:]
