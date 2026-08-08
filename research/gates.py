"""Policy-neutral acceptance checks for deterministic research results.

The checks operate on already normalized, vehicle-local rows.  They do not
know how a signal was generated and never combine equity and option returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from statistics import mean
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class AcceptanceFloor:
    min_trades: int = 100
    min_sessions: int = 10
    min_net_pnl: float = 0.0

    def check(self, trades: Iterable[Mapping], *, vehicle: str) -> dict:
        rows = [row for row in trades if row.get("vehicle", vehicle) == vehicle]
        sessions = {row.get("session_date") for row in rows}
        net = sum(float(row.get("net_pnl", 0.0)) for row in rows)
        checks = {
            "trades": len(rows) >= self.min_trades,
            "sessions": len(sessions) >= self.min_sessions,
            "net_pnl": net >= self.min_net_pnl,
        }
        return {
            "vehicle": vehicle, "trades": len(rows),
            "sessions": len(sessions), "net_pnl": net,
            "passes": all(checks.values()), "checks": checks,
        }


def chronological_split(rows: Sequence[Mapping], *, fit_fraction: float = .6) -> tuple[list, list]:
    """Split rows chronologically, preserving the no-look-ahead boundary."""
    if not 0 < fit_fraction < 1:
        raise ValueError("fit_fraction must be between zero and one")
    ordered = sorted(rows, key=lambda row: (str(row.get("session_date", "")),
                                             str(row.get("entry_timestamp", ""))))
    cut = max(1, min(len(ordered) - 1, int(len(ordered) * fit_fraction))) if len(ordered) > 1 else len(ordered)
    return ordered[:cut], ordered[cut:]


def paired_delta(candidate: Iterable[Mapping], baseline: Iterable[Mapping], *, vehicle: str) -> dict:
    """Compare matched vehicle-local rows without pooling unmatched outcomes."""
    left = [row for row in candidate if row.get("vehicle", vehicle) == vehicle]
    right = [row for row in baseline if row.get("vehicle", vehicle) == vehicle]
    by_key = {row.get("opportunity_id", row.get("entry_timestamp")): row for row in right}
    deltas = []
    for row in left:
        key = row.get("opportunity_id", row.get("entry_timestamp"))
        other = by_key.get(key)
        if other is not None:
            deltas.append(float(row.get("net_pnl", 0.0)) - float(other.get("net_pnl", 0.0)))
    return {"vehicle": vehicle, "matched": len(deltas),
            "mean_delta": mean(deltas) if deltas else None,
            "deltas": deltas}


def placebo_ratio(observed: Sequence[float], placebo: Sequence[float]) -> float | None:
    """Return the observed/placebo mean ratio, or ``None`` for no placebo."""
    if not placebo:
        return None
    baseline = mean(abs(float(value)) for value in placebo)
    return mean(float(value) for value in observed) / baseline if baseline else None


__all__ = ["AcceptanceFloor", "chronological_split", "paired_delta", "placebo_ratio"]
