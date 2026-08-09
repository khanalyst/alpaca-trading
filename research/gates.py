"""Policy-neutral acceptance checks for deterministic research results.

The checks operate on already normalized, vehicle-local rows.  They do not
know how a signal was generated and never combine equity and option returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from statistics import mean
import math
from typing import Any, Iterable, Mapping, Sequence


GATE_ENVELOPE_SCHEMA = "verified-research-gate.v1"


@dataclass(frozen=True)
class AcceptanceFloor:
    min_trades: int = 100
    min_sessions: int = 10
    min_net_pnl: float = 0.0
    max_drawdown: float | None = None
    min_clusters: int = 0

    def check(self, trades: Iterable[Mapping], *, vehicle: str) -> dict:
        rows = [row for row in trades if row.get("vehicle", vehicle) == vehicle]
        # Discovery materializes zero-outcome opportunities to avoid
        # survivorship bias.  They remain part of the session/control sample,
        # but are not executed trades and must not satisfy a trade floor.
        executed = [row for row in rows if row.get("no_trade") is not True]
        sessions = {row.get("session_date") for row in rows
                    if row.get("session_date") is not None}
        net = sum(float(row.get("net_pnl", 0.0)) for row in rows)
        drawdown = max_drawdown_of(rows)
        clusters = len({row.get("cluster", row.get("session_date")) for row in rows
                        if row.get("cluster", row.get("session_date")) is not None})
        checks = {
            "trades": len(executed) >= self.min_trades,
            "sessions": len(sessions) >= self.min_sessions,
            "net_pnl": net >= self.min_net_pnl,
            "clusters": clusters >= self.min_clusters,
        }
        if self.max_drawdown is not None:
            checks["max_drawdown"] = drawdown <= float(self.max_drawdown)
        structural_checks = {
            "trades": checks["trades"],
            "sessions": checks["sessions"],
            "clusters": checks["clusters"],
        }
        performance_checks = {key: value for key, value in checks.items()
                              if key not in structural_checks}
        return {
            "vehicle": vehicle, "trades": len(executed),
            "sessions": len(sessions), "net_pnl": net,
            "max_drawdown": drawdown, "clusters": clusters,
            "structural_passes": all(structural_checks.values()),
            "performance_passes": all(performance_checks.values()),
            "passes": all(checks.values()), "checks": checks,
            "structural_checks": structural_checks,
            "performance_checks": performance_checks,
        }


def chronological_split(rows: Sequence[Mapping], *, fit_fraction: float = .6,
                        require_order: bool = False) -> tuple[list, list]:
    """Split whole trading sessions across one chronological boundary."""
    if not 0 < fit_fraction < 1:
        raise ValueError("fit_fraction must be between zero and one")
    original = list(rows)
    key = lambda row: (_session_key(row), str(row.get("entry_timestamp", "")),
                       str(row.get("opportunity_id", "")))
    if require_order and any(key(left) > key(right)
                             for left, right in zip(original, original[1:])):
        raise ValueError("rows must already be chronological")
    ordered = sorted(original, key=key)
    sessions = sorted({_session_key(row) for row in ordered})
    if len(sessions) < 2:
        return ordered, []
    cut = max(1, min(len(sessions) - 1, int(len(sessions) * fit_fraction)))
    fit_sessions = set(sessions[:cut])
    return ([row for row in ordered if _session_key(row) in fit_sessions],
            [row for row in ordered if _session_key(row) not in fit_sessions])


def _session_key(row: Mapping) -> str:
    return str(row.get("session_date") or row.get("entry_timestamp") or
               row.get("opportunity_id") or "")


def structural_floor(rows: Iterable[Mapping], *, vehicle: str,
                     min_trades: int, min_sessions: int,
                     min_clusters: int = 0, required: bool = True) -> dict:
    """Report structural adequacy without treating profitability as sample size."""
    report = AcceptanceFloor(
        min_trades=min_trades, min_sessions=min_sessions,
        min_clusters=min_clusters,
    ).check(rows, vehicle=vehicle)
    report["minimums"] = {"trades": int(min_trades),
                          "sessions": int(min_sessions),
                          "clusters": int(min_clusters)}
    report["required"] = bool(required)
    report["adequate"] = bool(report["structural_passes"] if required else True)
    return report


def paired_delta(candidate: Iterable[Mapping], baseline: Iterable[Mapping], *, vehicle: str) -> dict:
    """Compare matched vehicle-local rows without pooling unmatched outcomes."""
    left = [row for row in candidate if row.get("vehicle", vehicle) == vehicle]
    right = [row for row in baseline if row.get("vehicle", vehicle) == vehicle]
    def unique(rows: Iterable[Mapping]) -> dict:
        by_key: dict = {}
        duplicates: set = set()
        for row in rows:
            key = row.get("opportunity_id", row.get("entry_timestamp"))
            if key in by_key:
                duplicates.add(key)
            else:
                by_key[key] = row
        for key in duplicates:
            by_key.pop(key, None)
        return by_key

    left_by_key = unique(left)
    right_by_key = unique(right)
    deltas = []
    for key, row in left_by_key.items():
        other = right_by_key.get(key)
        if other is not None:
            deltas.append(float(row.get("net_pnl", 0.0)) - float(other.get("net_pnl", 0.0)))
    return {"vehicle": vehicle, "matched": len(deltas),
            "mean_delta": mean(deltas) if deltas else None,
            "deltas": deltas}


def matched_cluster_test(candidate: Iterable[Mapping], baseline: Iterable[Mapping], *,
                         vehicle: str, seed: int = 20260728) -> dict:
    """Test matched opportunity deltas with deterministic session clustering."""
    from .stats import paired_cluster_sign_flip

    def unique(rows: Iterable[Mapping]) -> dict[str, Mapping]:
        values: dict[str, Mapping] = {}
        duplicates: set[str] = set()
        for row in rows:
            if row.get("vehicle", vehicle) != vehicle:
                continue
            key = _match_key(row, vehicle)
            if not key or key in values:
                duplicates.add(key)
            else:
                values[key] = row
        for key in duplicates:
            values.pop(key, None)
        return values

    left = unique(candidate)
    right = unique(baseline)
    pairs: list[tuple[float, float, float]] = []
    matched_ids: list[str] = []
    for index, key in enumerate(sorted(left)):
        other = right.get(key)
        if other is None:
            continue
        stamp = left[key].get("entry_timestamp") or left[key].get("session_date")
        try:
            timestamp = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            timestamp = float(index * 86_400)
        pairs.append((timestamp, float(left[key].get("net_pnl", 0.0)),
                      float(other.get("net_pnl", 0.0))))
        matched_ids.append(key)
    result = paired_cluster_sign_flip(pairs, cluster_seconds=86_400, seed=seed)
    result["matched"] = len(pairs)
    result["matched_ids_hash"] = _content_hash(matched_ids)
    result["mean_delta"] = (sum(left - right for _, left, right in pairs) / len(pairs)
                            if pairs else None)
    result["actual_control"] = True
    result["available"] = bool(pairs)
    return result


def deterministic_placebo_deltas(candidate: Iterable[Mapping], baseline: Iterable[Mapping], *,
                                  vehicle: str) -> dict:
    """Apply a deterministic mixed-sign session-label falsification to matched deltas."""
    def unique(rows: Iterable[Mapping]) -> dict[str, Mapping]:
        values: dict[str, Mapping] = {}
        duplicates: set[str] = set()
        for row in rows:
            if row.get("vehicle", vehicle) != vehicle:
                continue
            key = _match_key(row, vehicle)
            if not key or key in values:
                duplicates.add(key)
            else:
                values[key] = row
        for key in duplicates:
            values.pop(key, None)
        return values

    left = unique(candidate)
    right = unique(baseline)
    keys = sorted(key for key in left if key and key in right)
    observed = [float(left[key].get("net_pnl", 0.0)) -
                float(right[key].get("net_pnl", 0.0)) for key in keys]
    if len(observed) < 2:
        return {"method": "deterministic_mixed_sign_session_labels",
                "available": False, "observed": observed, "placebo": [],
                "assignments_hash": _content_hash(keys)}
    phase = int(hashlib.sha256("|".join(keys).encode("utf-8")).hexdigest(), 16) % 2
    placebo = [value if (index + phase) % 2 == 0 else -value
               for index, value in enumerate(observed)]
    return {"method": "deterministic_mixed_sign_session_labels",
            "available": True, "observed": observed, "placebo": placebo,
            "assignments_hash": _content_hash({"keys": keys, "phase": phase})}


def _match_key(row: Mapping, vehicle: str) -> str:
    explicit = row.get("comparison_id")
    if explicit:
        return str(explicit)
    symbol = row.get("symbol")
    session = row.get("session_date")
    if symbol and session:
        return f"{row.get('vehicle', vehicle)}:{symbol}:{session}"
    return str(row.get("opportunity_id") or row.get("entry_timestamp") or "")


def placebo_ratio(observed: Sequence[float], placebo: Sequence[float]) -> float | None:
    """Return the observed/placebo mean ratio, or ``None`` for no placebo."""
    if not placebo:
        return None
    baseline = mean(abs(float(value)) for value in placebo)
    return mean(float(value) for value in observed) / baseline if baseline else None


def max_drawdown_of(rows_or_values: Iterable[Mapping] | Iterable[float]) -> float:
    """Maximum peak-to-trough loss, reported as a non-negative P&L amount."""
    values = []
    for row in rows_or_values:
        value = row.get("net_pnl", row.get("return_value", 0.0)) if isinstance(row, Mapping) else row
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            values.append(number)
    peak = equity = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return float(drawdown)


def heldout_separation(fit: Sequence[Mapping], heldout: Sequence[Mapping]) -> dict:
    """Require one chronological boundary with no shared session."""
    fit_sessions = {str(row.get("session_date")) for row in fit if row.get("session_date") is not None}
    held_sessions = {str(row.get("session_date")) for row in heldout if row.get("session_date") is not None}
    fit_keys = [(str(row.get("session_date", "")), str(row.get("entry_timestamp", ""))) for row in fit]
    held_keys = [(str(row.get("session_date", "")), str(row.get("entry_timestamp", ""))) for row in heldout]
    boundary_ok = bool(fit_keys and held_keys and max(fit_keys) < min(held_keys))
    return {"fit": len(fit), "heldout": len(heldout),
            "overlap_sessions": sorted(fit_sessions & held_sessions),
            "passes": boundary_ok and not (fit_sessions & held_sessions)}


def falsification_gate(observed: Sequence[float], placebo: Sequence[float], *,
                       minimum_ratio: float = 1.0) -> dict:
    ratio = placebo_ratio(observed, placebo)
    observed_mean = mean([float(x) for x in observed]) if observed else 0.0
    placebo_mean = mean([float(x) for x in placebo]) if placebo else 0.0
    zero_placebo = bool(placebo and all(abs(float(x)) <= 1e-15 for x in placebo))
    distinct = bool(placebo and (len(observed) != len(placebo) or
                    any(abs(float(left) - float(right)) > 1e-15
                        for left, right in zip(observed, placebo))))
    return {"observed_mean": observed_mean, "placebo_mean": placebo_mean,
            "ratio": ratio, "available": bool(placebo),
            "zero_placebo": zero_placebo,
            "distinct": distinct,
            "passes": bool(placebo) and not zero_placebo and distinct and
            observed_mean > 0 and observed_mean > placebo_mean and
            ratio is not None and ratio >= minimum_ratio}


def sample_counts(rows: Iterable[Mapping], *, vehicle: str) -> dict:
    selected = [row for row in rows if row.get("vehicle", vehicle) == vehicle]
    return {
        "rows": len(selected),
        "trades": len([row for row in selected if row.get("no_trade") is not True]),
        "sessions": len({_session_key(row) for row in selected if _session_key(row)}),
        "clusters": len({str(row.get("cluster") or _session_key(row)) for row in selected
                         if row.get("cluster") or _session_key(row)}),
    }


def verified_gate_envelope(*, lane: str, vehicle: str,
                           fit: Sequence[Mapping], heldout: Sequence[Mapping],
                           fit_floor: Mapping, heldout_floor: Mapping,
                           control: Mapping, p_value: float, q_value: float,
                           alpha: float,
                           falsification: Mapping, separation: Mapping,
                           checks: Mapping[str, bool], passes: bool,
                           performance: Mapping | None = None) -> dict:
    """Build the immutable, content-addressed gate decision persisted per run."""
    body: dict[str, Any] = {
        "schema": GATE_ENVELOPE_SCHEMA,
        "lane": str(lane),
        "vehicle": str(vehicle),
        "counts": {
            "fit": sample_counts(fit, vehicle=vehicle),
            "heldout": sample_counts(heldout, vehicle=vehicle),
            "total": sample_counts([*fit, *heldout], vehicle=vehicle),
        },
        "floors": {"fit": dict(fit_floor), "heldout": dict(heldout_floor)},
        "control": dict(control),
        "statistics": {"p_value": float(p_value), "q_value": float(q_value),
                       "alpha": float(alpha)},
        "performance": dict(performance or {}),
        "falsification": dict(falsification),
        "separation": dict(separation),
        "checks": {str(key): bool(value) for key, value in checks.items()},
        "passes": bool(passes),
    }
    return {**body, "content_hash": _content_hash(body)}


def verify_gate_envelope(envelope: Mapping) -> bool:
    try:
        body = {key: value for key, value in envelope.items() if key != "content_hash"}
        return bool(
            envelope.get("schema") == GATE_ENVELOPE_SCHEMA and
            envelope.get("lane") in {"backtest", "shadow"} and
            envelope.get("vehicle") in {"equity", "option"} and
            isinstance(envelope.get("passes"), bool) and
            isinstance(envelope.get("counts"), Mapping) and
            isinstance(envelope.get("floors"), Mapping) and
            isinstance(envelope.get("control"), Mapping) and
            isinstance(envelope.get("statistics"), Mapping) and
            envelope.get("content_hash") == _content_hash(body))
    except (TypeError, ValueError, OverflowError):
        return False


def _content_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = ["AcceptanceFloor", "GATE_ENVELOPE_SCHEMA", "chronological_split",
           "deterministic_placebo_deltas", "falsification_gate",
           "heldout_separation", "matched_cluster_test", "max_drawdown_of",
           "paired_delta", "placebo_ratio", "sample_counts", "structural_floor",
           "verified_gate_envelope", "verify_gate_envelope"]
