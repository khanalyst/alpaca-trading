"""Small deterministic empirical world model for ranking research ideas."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from research.discovery import content_hash


STATES = ("target", "stop", "normalized", "timeout")


def _bucket(value: Any, cuts: tuple[float, ...] = (-1.0, 0.0, 1.0)) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "missing"
    if not math.isfinite(number):
        return "missing"
    for index, cut in enumerate(cuts):
        if number < cut:
            return str(index)
    return str(len(cuts))


def model_key(row: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    return (str(row.get("regime") or "unknown"),
            _bucket(row.get("liquidity")),
            _bucket(row.get("observable")),
            str(row.get("direction") or "both"),
            str(row.get("exit_profile") or "observable_normalized"))


@dataclass(frozen=True)
class WorldModel:
    alpha: float
    fit_count: int
    transitions: tuple[tuple[tuple[str, ...], tuple[tuple[str, float], ...]], ...]
    model_hash: str
    fit_fraction: float

    def predict(self, row: Mapping[str, Any]) -> dict[str, float]:
        key = model_key(row)
        for candidate, values in self.transitions:
            if tuple(key) == tuple(candidate):
                return dict(values)
        return {state: 1.0 / len(STATES) for state in STATES}

    def as_dict(self) -> dict:
        return {"alpha": self.alpha, "fit_count": self.fit_count,
                "fit_fraction": self.fit_fraction,
                "transitions": [{"key": list(key), "probabilities": dict(values)} for key, values in self.transitions],
                "model_hash": self.model_hash}

    def held_out_diagnostics(self, rows: Sequence[Mapping[str, Any]], *, state_field: str = "state") -> dict:
        if not rows:
            return {"sample": 0, "brier": None, "calibration": [], "clusters": 0}
        brier_values: list[float] = []
        calibration: dict[str, list[float]] = {}
        seen_clusters = set()
        for row in rows:
            state = str(row.get(state_field) or "")
            if state not in STATES:
                continue
            probs = self.predict(row)
            brier_values.append(sum((probs[name] - float(name == state)) ** 2 for name in STATES))
            calibration.setdefault(state, []).append(probs[state])
            seen_clusters.add(row.get("cluster", row.get("event_ts_ms")))
        return {"sample": len(brier_values), "brier": sum(brier_values) / len(brier_values) if brier_values else None,
                "calibration": {state: {"count": len(values), "mean_probability": sum(values) / len(values)} for state, values in calibration.items()},
                "clusters": len(seen_clusters)}


def fit_world_model(rows: Sequence[Mapping[str, Any]], *, fit_fraction: float = 0.7, alpha: float = 1.0, state_field: str = "state") -> tuple[WorldModel, dict]:
    if not 0.5 <= float(fit_fraction) < 1:
        raise ValueError("fit_fraction must be in [0.5,1)")
    if not math.isfinite(float(alpha)) or not 0 < float(alpha) <= 100:
        raise ValueError("alpha must be finite and positive")
    boundary = max(1, min(len(rows), int(len(rows) * fit_fraction))) if rows else 0
    fit, held_out = list(rows[:boundary]), list(rows[boundary:])
    counts: dict[tuple[str, ...], dict[str, int]] = {}
    for row in fit:
        state = str(row.get(state_field) or "")
        if state not in STATES:
            continue
        key = model_key(row)
        counts.setdefault(key, {name: 0 for name in STATES})[state] += 1
    transitions = []
    for key in sorted(counts):
        total = sum(counts[key].values()) + float(alpha) * len(STATES)
        probabilities = tuple((name, (counts[key][name] + float(alpha)) / total) for name in STATES)
        transitions.append((key, probabilities))
    provisional = {"alpha": float(alpha), "fit_count": len(fit), "fit_fraction": float(fit_fraction), "transitions": [{"key": list(k), "probabilities": dict(v)} for k, v in transitions]}
    model = WorldModel(float(alpha), len(fit), tuple(transitions), content_hash(provisional), float(fit_fraction))
    return model, model.held_out_diagnostics(held_out, state_field=state_field)


__all__ = ["STATES", "WorldModel", "model_key", "fit_world_model"]
