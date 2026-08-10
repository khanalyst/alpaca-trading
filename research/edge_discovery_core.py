"""Deterministic discovery helpers shared by the discovery facades.

The helper implementations in this module intentionally remain independent of
the lifecycle/ledger orchestration in :mod:`research.edge_lab`.  Dependency
proxies resolve through that facade at call time, preserving the historical
patch seams while keeping this module import-safe on its own.
"""

from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
from typing import Mapping, Sequence

from .costs import CostModel
from .market_data import OptionSnapshot, QuoteSnapshot, UnderlyingBar


def _facade_dependency(name: str):
    from . import edge_lab
    return getattr(edge_lab, name)


def normalize_underlying_bar(*args, **kwargs):
    return _facade_dependency("normalize_underlying_bar")(*args, **kwargs)


def normalize_option_snapshot(*args, **kwargs):
    return _facade_dependency("normalize_option_snapshot")(*args, **kwargs)


def normalize_quote(*args, **kwargs):
    return _facade_dependency("normalize_quote")(*args, **kwargs)


def IBRConfig(*args, **kwargs):
    return _facade_dependency("IBRConfig")(*args, **kwargs)


def ZoneInfo(*args, **kwargs):
    return _facade_dependency("ZoneInfo")(*args, **kwargs)


def chronological_split(*args, **kwargs):
    return _facade_dependency("chronological_split")(*args, **kwargs)


def structural_floor(*args, **kwargs):
    return _facade_dependency("structural_floor")(*args, **kwargs)


def heldout_separation(*args, **kwargs):
    return _facade_dependency("heldout_separation")(*args, **kwargs)


def paired_delta(*args, **kwargs):
    return _facade_dependency("paired_delta")(*args, **kwargs)


def matched_cluster_test(*args, **kwargs):
    return _facade_dependency("matched_cluster_test")(*args, **kwargs)


def deterministic_placebo_deltas(*args, **kwargs):
    return _facade_dependency("deterministic_placebo_deltas")(*args, **kwargs)


def falsification_gate(*args, **kwargs):
    return _facade_dependency("falsification_gate")(*args, **kwargs)


def max_drawdown_of(*args, **kwargs):
    return _facade_dependency("max_drawdown_of")(*args, **kwargs)


def sample_counts(*args, **kwargs):
    return _facade_dependency("sample_counts")(*args, **kwargs)


def verified_gate_envelope(*args, **kwargs):
    return _facade_dependency("verified_gate_envelope")(*args, **kwargs)


class DiscoveryError(ValueError):
    """Raised when a discovery corpus cannot be evaluated safely."""


def _read_discovery_rows(data: str | Path | Sequence[Mapping]) -> tuple[
        list[dict], list[UnderlyingBar], dict[str, OptionSnapshot], list[QuoteSnapshot]]:
    """Load one normalized JSONL corpus: bars, option quotes, equity quotes."""
    if isinstance(data, (str, Path)):
        source = Path(data)
        try:
            raw_rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()
                        if line.strip()]
        except (OSError, json.JSONDecodeError) as exc:
            raise DiscoveryError(f"invalid discovery JSONL {source}: {exc}") from exc
    else:
        raw_rows = [dict(row) for row in data]
    if any(not isinstance(row, Mapping) for row in raw_rows):
        raise DiscoveryError("discovery rows must be JSON objects")
    bars: list[UnderlyingBar] = []
    snapshots: dict[str, OptionSnapshot] = {}
    quotes: list[QuoteSnapshot] = []
    for number, source_row in enumerate(raw_rows, 1):
        row = dict(source_row)
        kind = str(row.get("kind", "bar")).lower()
        provider = str(row.get("provider") or "alpaca")
        feed = str(row.get("feed") or ("opra" if "option" in kind else "sip"))
        try:
            if kind in {"bar", "underlying", "underlying_bar"}:
                bars.append(normalize_underlying_bar(row, provider=provider, feed=feed))
            elif kind in {"option", "option_snapshot", "option_quote"}:
                contract = row.get("contract")
                if isinstance(contract, Mapping):
                    flattened = dict(contract)
                    flattened.update({key: value for key, value in row.items()
                                      if key != "contract"})
                    row = flattened
                snap = normalize_option_snapshot(row, provider=provider, feed=feed)
                snapshots[f"{snap.timestamp.isoformat()}|{snap.contract.symbol}"] = snap
            elif kind in {"quote", "equity_quote", "underlying_quote"}:
                # An equity quote is the executable price at its instant.  It
                # is used only where a fill lands on that instant; everything
                # else still falls back to the bar and says so.
                quotes.append(normalize_quote(row, provider=provider, feed=feed))
            # Other metadata stays in the dataset hash without being fed into
            # an OHLC replay.
        except (TypeError, ValueError) as exc:
            raise DiscoveryError(f"row {number}: {exc}") from exc
    if not bars:
        raise DiscoveryError("discovery corpus contains no underlying bars")
    quotes.sort(key=lambda item: (item.symbol, item.timestamp))
    return raw_rows, bars, snapshots, quotes


def _effective_ibr_config(base: Mapping | None, overrides: Mapping,
                          *, close_confirmed: bool = True) -> tuple[IBRConfig, dict]:
    """Build the replay config used by every variant from one immutable base."""
    source = dict(base or {})
    strategy = dict(source.get("strategy") or {})
    # The runtime contract's defaults are explicit here rather than inherited
    # from a notebook's replay defaults.
    strategy.setdefault("range_minutes", 15)
    strategy.setdefault("breakout_buffer_bps", 5.0)
    strategy.setdefault("target_r", 2.0)
    strategy.setdefault("range_stop", True)
    strategy.setdefault("stop_pct", .003)
    strategy.setdefault("target_pct", .006)
    for path, value in overrides.items():
        parts = str(path).split(".")
        if parts and parts[0] == "strategy":
            parts = parts[1:]
        node = strategy
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        if parts:
            node[parts[-1]] = value
    # One cost model for every lane.  `execution.max_slippage_bps` is a
    # rejection cap, not an expectation: it bounds the model rather than
    # supplying it.
    costs = CostModel.from_config(source)
    cfg = IBRConfig(
        range_minutes=int(strategy.get("range_minutes", 15)),
        stop_pct=float(strategy.get("stop_pct", .003)),
        target_pct=float(strategy.get("target_pct", .006)),
        target_r=float(strategy["target_r"]) if strategy.get("target_r") is not None else None,
        range_stop=bool(strategy.get("range_stop", True)),
        breakout_buffer_bps=float(strategy.get("breakout_buffer_bps", 5.0)),
        costs=costs,
        close_confirmed=bool(close_confirmed),
        timezone=str((source.get("session") or {}).get("timezone", "America/New_York")),
    )
    effective = dict(source)
    effective["strategy"] = strategy
    effective["replay"] = {"close_confirmed": cfg.close_confirmed,
                            "range_stop": cfg.range_stop}
    effective["costs"] = costs.as_dict()
    return cfg, effective


def _opportunity_rows(result, bars: Sequence[UnderlyingBar], vehicle: str) -> list[dict]:
    """Materialize one row per symbol/session, including no-trade zeros."""
    zone = ZoneInfo("America/New_York")
    sessions = sorted({(bar.symbol, bar.timestamp.astimezone(zone).date()) for bar in bars})
    by_session = {(trade.symbol, trade.session_date): trade for trade in result.trades}
    rows: list[dict] = []
    for symbol, day in sessions:
        opportunity_id = f"ibr:{vehicle}:{symbol}:{day.isoformat()}"
        trade = by_session.get((symbol, day))
        if trade is None:
            rows.append({"vehicle": vehicle, "symbol": symbol,
                         "session_date": day.isoformat(),
                         "opportunity_id": opportunity_id, "net_pnl": 0.0,
                         "return_value": 0.0, "no_trade": True})
            continue
        row = {key: value for key, value in vars(trade).items()}
        row.update({"session_date": trade.session_date.isoformat(),
                    "opportunity_id": opportunity_id, "no_trade": False,
                    "return_value": float(trade.net_pnl)})
        for key, value in list(row.items()):
            if isinstance(value, (datetime, date)):
                row[key] = value.isoformat()
        rows.append(row)
    return rows


def _discover_gate(candidate: Sequence[Mapping], baseline: Sequence[Mapping], *,
                   vehicle: str, min_trades: int, min_sessions: int,
                   alpha: float, shadow: bool = False,
                   actual_control: bool = True,
                   control_kind: str = "matched_actual_baseline") -> dict:
    """Evaluate one chronological backtest or a genuinely new shadow sample.

    A backtest is split into fit/held-out partitions.  A shadow evaluation is
    already supplied as a later corpus, so every row is held out and there is
    deliberately no in-sample fit partition to accidentally reuse for a
    lifecycle transition.
    """
    ordered = sorted(candidate, key=lambda row: (str(row.get("session_date", "")),
                                                  str(row.get("entry_timestamp", ""))))
    base_ordered = sorted(baseline, key=lambda row: (str(row.get("session_date", "")),
                                                      str(row.get("entry_timestamp", ""))))
    if shadow:
        fit, heldout = [], ordered
        base_fit, base_heldout = [], base_ordered
    else:
        fit, heldout = chronological_split(ordered, fit_fraction=.7)
        fit_sessions = {str(row.get("session_date") or "") for row in fit}
        held_sessions = {str(row.get("session_date") or "") for row in heldout}
        base_fit = [row for row in base_ordered
                    if str(row.get("session_date") or "") in fit_sessions]
        base_heldout = [row for row in base_ordered
                       if str(row.get("session_date") or "") in held_sessions]
    fit_floor = structural_floor(
        fit, vehicle=vehicle, min_trades=min_trades, min_sessions=min_sessions,
        required=not shadow)
    held_floor = structural_floor(
        heldout, vehicle=vehicle, min_trades=min_trades, min_sessions=min_sessions)
    overall_floor = structural_floor(
        ordered, vehicle=vehicle, min_trades=min_trades, min_sessions=min_sessions)
    separation = (heldout_separation(fit, heldout) if not shadow else
                  {"fit": 0, "heldout": len(heldout), "overlap_sessions": [],
                   "passes": bool(heldout), "mode": "new_data"})
    delta_all = paired_delta(ordered, baseline, vehicle=vehicle)
    delta_fit = (matched_cluster_test(fit, base_fit, vehicle=vehicle) if not shadow else
                 {"available": True, "actual_control": True, "matched": 0,
                  "mean_delta": None, "p_value": 1.0, "mode": "prior_backtest"})
    delta_held = matched_cluster_test(heldout, base_heldout, vehicle=vehicle)
    delta_fit["actual_control"] = bool(actual_control)
    delta_held["actual_control"] = bool(actual_control)
    placebo = deterministic_placebo_deltas(
        heldout, base_heldout, vehicle=vehicle)
    falsification = {
        **falsification_gate(placebo["observed"], placebo["placebo"]),
        "method": placebo["method"],
        "assignments_hash": placebo["assignments_hash"],
        "observations": len(placebo["observed"]),
    }
    candidate_p = float(delta_held.get("p_value", 1.0))
    checks = {
        "fit_structurally_adequate": bool(fit_floor["adequate"]),
        "heldout_structurally_adequate": bool(held_floor["adequate"]),
        "separated": bool(separation["passes"]),
        "actual_control_available": bool(delta_held.get("available") and
                                         delta_held.get("actual_control")),
        "fit_delta_positive": bool(shadow or (
            delta_fit.get("mean_delta") is not None and float(delta_fit["mean_delta"]) > 0)),
        "heldout_delta_positive": bool(delta_held.get("mean_delta") is not None and
                                        float(delta_held["mean_delta"]) > 0),
        "heldout_p_significant": candidate_p <= float(alpha),
        "falsification": bool(falsification["passes"]),
    }
    passes_without_family = bool(
        all(checks.values()) and delta_all.get("mean_delta") is not None and
        float(delta_all["mean_delta"]) > 0)
    return {"vehicle": vehicle, "shadow": shadow,
            "alpha": float(alpha),
            "passes_without_family": passes_without_family,
            "candidate_p_raw": candidate_p,
            "floor": overall_floor, "fit_floor": fit_floor, "heldout_floor": held_floor,
            "heldout_separation": separation, "paired_baseline": delta_all,
            "fit_paired_baseline": delta_fit,
            "heldout_paired_baseline": delta_held,
            "control": {**delta_held, "kind": control_kind},
            "falsification": falsification,
            "checks_without_family": checks,
            "max_drawdown": max_drawdown_of(ordered),
            "fit_trades": sample_counts(fit, vehicle=vehicle)["trades"],
            "heldout_trades": sample_counts(heldout, vehicle=vehicle)["trades"],
            "fit_sessions": len({row.get("session_date") for row in fit}),
            "heldout_sessions": len({row.get("session_date") for row in heldout}),
            "_fit_rows": fit, "_heldout_rows": heldout}


def _finalize_gate(gate: dict, *, lane: str, family: Mapping) -> dict:
    checks = {**gate["checks_without_family"],
              "family_fdr_significant": bool(family.get("significant", False))}
    passes = bool(gate["passes_without_family"] and checks["family_fdr_significant"])
    gate["multiple_tests"] = {"candidate": dict(family),
                              "method": "benjamini_hochberg"}
    gate["passes"] = passes
    fit = gate.pop("_fit_rows")
    heldout = gate.pop("_heldout_rows")
    envelope = verified_gate_envelope(
        lane=lane, vehicle=gate["vehicle"], fit=fit, heldout=heldout,
        fit_floor=gate["fit_floor"], heldout_floor=gate["heldout_floor"],
        control=gate["control"], p_value=gate["candidate_p_raw"],
        q_value=float(family.get("p_adjusted", 1.0)), alpha=gate.get("alpha", 0.05),
        falsification=gate["falsification"], separation=gate["heldout_separation"],
        checks=checks, passes=passes,
        performance={"heldout_delta": gate["heldout_paired_baseline"].get("mean_delta"),
                     "max_drawdown": gate["max_drawdown"]})
    gate["verified_gate"] = envelope
    gate["gate_hash"] = envelope["content_hash"]
    return gate


__all__ = ["DiscoveryError", "_read_discovery_rows", "_effective_ibr_config",
           "_opportunity_rows", "_discover_gate", "_finalize_gate"]
