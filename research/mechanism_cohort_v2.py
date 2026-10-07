"""Diagnostic-only one-arm mechanism cohort for the v6 rule grammar.

The v1 cohort is frozen in :mod:`research.mechanism_cohort`.  This manifest is
an additive preregistration: each family has one authored arm, all arms share
the same stop/risk geometry, and no arm is eligible for promotion or automatic
winner selection.
"""

from __future__ import annotations

from typing import Any

from agent.contracts.rule import RULE_SCHEMA_V6, validate_rule_spec, rule_variant_id
from .edge_ledger_store import content_hash
from .factory_core import family_template


COHORT_ID = "intraday-mechanisms.v2"


def _arm(family: str, *, after: int, before: int, hold: int,
         target_mode: str = "fixed_r", target_lookback: int = 20,
         exit_before: int | None = None, confirmation: str = "none",
         trailing: float | None = 1.5, **changes: Any) -> dict[str, Any]:
    """Build one normalized v6 arm from the audited family template."""
    spec = family_template(family)
    spec.update({
        "schema": RULE_SCHEMA_V6,
        "side": "both",
        "confirmation": confirmation,
        "atr_period": 14,
        "stop_atr": 1.0,
        "target_r": 2.0,
        "max_hold_bars": hold,
        "entry_after_minutes": after,
        "entry_before_minutes": before,
        "target_mode": target_mode,
        "target_lookback": target_lookback,
        "trailing_stop_r": trailing,
        "exit_before_minutes": exit_before,
        **changes,
    })
    normalized = validate_rule_spec(spec)
    return {"arm": 0, "variant_id": rule_variant_id(normalized),
            "rule_spec": normalized}


def mechanism_cohort(name: str = COHORT_ID) -> dict[str, Any]:
    if name != COHORT_ID:
        raise ValueError(f"unknown mechanism cohort: {name}")

    definitions = [
        ("opening_range_breakout", {
            "after": 15, "before": 180, "hold": 30,
            "opening_hold_bars": 2,
        }, "ORB requires two post-window closes beyond the completed range boundary."),
        ("opening_range_fade", {
            "after": 20, "before": 180, "hold": 30,
            "target_mode": "session_vwap", "exit_before": 360,
        }, "Existing failed-break closeback fade toward session VWAP."),
        ("momentum_continuation", {
            "after": 15, "before": 180, "hold": 30,
            "confirmation": "volume",
        }, "Momentum with completed-bar volume confirmation."),
        ("mean_reversion", {
            "after": 15, "before": 180, "hold": 30,
            "entry_trigger": "reclaim", "pullback_bars": 2,
            "target_mode": "rolling_mean", "target_lookback": 20,
            "exit_before": 360,
        }, "Two-bar reclaim toward the completed rolling mean."),
        ("trend_pullback", {
            "after": 15, "before": 240, "hold": 60,
            "entry_trigger": "reclaim", "pullback_bars": 2,
            "regime_mode": "trend", "regime_timeframe_minutes": 5,
            "regime_lookback_bars": 3, "regime_efficiency_threshold": .4,
        }, "Reclaim conditioned on completed five-minute trend context."),
        ("volatility_breakout", {
            "after": 15, "before": 180, "hold": 30,
            "compression_mode": "atr", "compression_atr_multiple": 3.0,
        }, "Breakout after prior-bar ATR compression."),
        ("volume_breakout", {
            "after": 15, "before": 180, "hold": 30,
            "confirmation": "volume",
            "relative_volume_mode": "same_clock",
            "relative_volume_sessions": 5,
        }, "Causal same-clock RVOL from the exact five prior same-feed sessions; missing samples produce no signal."),
        ("vwap_reversion", {
            "after": 30, "before": 300, "hold": 15,
            "entry_trigger": "reclaim", "pullback_bars": 2,
            "regime_mode": "range", "regime_timeframe_minutes": 5,
            "regime_lookback_bars": 3, "regime_efficiency_threshold": .4,
            "target_mode": "session_vwap", "exit_before": 360,
        }, "VWAP reclaim conditioned on a completed range regime."),
        ("vwap_trend", {
            "after": 15, "before": 180, "hold": 30,
            "vwap_slope_mode": "rolling", "vwap_slope_window": 5,
        }, "Stable equal-window rolling VWAP slope diagnostic using two completed five-bar windows."),
        ("range_expansion", {
            "after": 15, "before": 180, "hold": 30,
            "body_fraction_min": .5, "close_location_min": .75,
        }, "Directional expansion requires a half-body and 0.75 close location."),
        ("opening_drive", {
            "after": 30, "before": 180, "hold": 30,
            "opening_hold_bars": 3,
        }, "Opening drive requires three post-window closes beyond its drive endpoint."),
        ("cross_sectional_residual", {
            "after": 15, "before": 180, "hold": 30,
        }, "Outright relative-strength diagnostic against SPY; it is not hedged or beta-neutral."),
    ]

    families = []
    for family, options, thesis in definitions:
        arm = _arm(family, **options)
        arm["reason"] = thesis
        families.append({
            "name": family,
            "thesis": thesis,
            "diagnostic_labels": [thesis],
            "arms": [arm],
        })
    manifest = {
        "cohort_id": COHORT_ID,
        "families": families,
        "registered_arms": len(families),
        "selection_policy": "diagnostic_only_no_automatic_winner_selection",
        "risk_policy": "unchanged_runtime_limits_and_authored_stop_stress_veto",
        "diagnostic_only": True,
        "authorizing": False,
    }
    return {**manifest, "manifest_hash": content_hash(manifest)}


__all__ = ["COHORT_ID", "mechanism_cohort"]
