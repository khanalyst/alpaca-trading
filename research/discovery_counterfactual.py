"""Paired, deterministic counterfactual interventions for discovery IR."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from statistics import median
from typing import Any, Callable, Mapping, Sequence

from research.discovery import (CounterfactualSpec, DiscoveryProgram,
                                content_hash, resolve_episode)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False, default=str)


def _cluster_key(row: Mapping[str, Any], field: str) -> Any:
    value = row.get(field)
    # 1h clusters for millisecond timestamps; string/categorical clusters are
    # retained exactly.  This prevents a random shuffle from crossing time.
    try:
        return int(float(value)) // 3_600_000
    except (TypeError, ValueError):
        return str(value)


def fit_window(rows: Sequence[Mapping[str, Any]], fit_fraction: float) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    if not rows:
        return [], []
    boundary = max(1, min(len(rows) - 1, int(len(rows) * float(fit_fraction))))
    return list(rows[:boundary]), list(rows[boundary:])


def _median(values: Sequence[float]) -> float | None:
    finite = [float(value) for value in values if isinstance(value, (int, float)) and value == value and abs(float(value)) != float("inf")]
    return float(median(finite)) if finite else None


def intervene_rows(rows: Sequence[Mapping[str, Any]], values: Sequence[float | None], spec: CounterfactualSpec) -> list[dict]:
    """Replace only the generated observable while preserving every row key."""
    if len(rows) != len(values):
        raise ValueError("rows and observable values must be paired")
    fit, confirm = fit_window(rows, spec.fit_fraction)
    fit_values = list(values[:len(fit)])
    baseline = _median(fit_values)
    if baseline is None:
        return []
    if spec.intervention == "fit_window_median":
        return [{**dict(row), "__discovery_observable__": baseline,
                 "__discovery_index__": len(fit) + index}
                for index, row in enumerate(confirm)]
    # Permutations happen within the confirmation rows' time cluster and use
    # a local RNG.  No module/global RNG state can affect a result.
    grouped: dict[Any, list[int]] = {}
    for index, row in enumerate(confirm):
        grouped.setdefault(_cluster_key(row, spec.cluster_field), []).append(index)
    result = [{**dict(row), "__discovery_observable__": values[len(fit) + index],
               "__discovery_index__": len(fit) + index}
              for index, row in enumerate(confirm)]
    rng = random.Random(int(spec.seed))
    for indices in sorted(grouped.values(), key=lambda group: tuple(group)):
        source = [result[index]["__discovery_observable__"] for index in indices]
        rng.shuffle(source)
        for index, value in zip(indices, source):
            result[index]["__discovery_observable__"] = value
    return result


@dataclass(frozen=True)
class CounterfactualResult:
    intervention: str
    fit_count: int
    confirm_count: int
    paired_count: int
    observed_mean: float | None
    counterfactual_mean: float | None
    effect: float | None
    digest: str
    status: str
    exit_semantics_hash: str = ""
    reason: str | None = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def run_counterfactual(program: DiscoveryProgram, rows: Sequence[Mapping[str, Any]], observed_results: Sequence[float | Mapping[str, Any] | None], *, outcome_key: str = "outcome") -> CounterfactualResult:
    if len(rows) != len(observed_results):
        raise ValueError("rows and observed_results must be paired")
    fit, confirm = fit_window(rows, program.counterfactual.fit_fraction)
    if not fit or not confirm:
        return CounterfactualResult(program.counterfactual.intervention, len(fit), len(confirm), 0, None, None, None, content_hash([]), "INCONCLUSIVE", content_hash(program.exit_profile.as_dict()))
    episode_flags = [
        isinstance(row.get("bars", row.get("episode_bars")), (list, tuple))
        and bool(row.get("bars", row.get("episode_bars")))
        for row in rows]
    episode_mode = bool(episode_flags) and all(episode_flags)
    if not episode_mode:
        # A scalar outcome and an episode net percentage are not paired units:
        # the former may include a different exit/cost/funding contract.  A
        # mixed batch is therefore an explicit inconclusive result rather than
        # a silent fallback to the old scalar intervention path.
        return CounterfactualResult(
            program.counterfactual.intervention, len(fit), len(confirm), 0,
            None, None, None, content_hash([]), "INCONCLUSIVE",
            content_hash(program.exit_profile.as_dict()),
            "episode rows are required for counterfactual pairing")
    generated_values = [
        # The generated observable is the signal value for this episode.  It
        # is evaluated from the persisted signal row, never from a future bar.
        program.observable.evaluate(row, rows[:index])
        for index, row in enumerate(rows)]
    observed_results = [resolve_episode(
        program, row,
        (row.get("observable_values") if row.get("observable_values") is not None
         else [program.observable.evaluate(
             bar, row.get("bars", row.get("episode_bars"))[:index])
               for index, bar in enumerate(
                   row.get("bars", row.get("episode_bars")))]))
        for row in rows]
    intervened = intervene_rows(rows, generated_values, program.counterfactual)
    observed = []
    counter = []
    for row in intervened:
        index = int(row["__discovery_index__"])
        value = observed_results[index]
        if isinstance(value, Mapping):
            if episode_mode:
                if value.get("exit_semantics_hash") != content_hash(program.exit_profile.as_dict()):
                    value = None
                else:
                    intervention_value = row.get("__discovery_observable__")
                    bars = rows[index].get("bars", rows[index].get("episode_bars"))
                    replacement = resolve_episode(
                        program, rows[index], [intervention_value] * len(bars))
                    generated = replacement.get("net_pct")
                    value = value.get("net_pct")
                    if replacement.get("status") in {"no_data", "invalid"}:
                        value = None
                    else:
                        # ``generated`` is the paired intervention outcome.
                        row["__discovery_counterfactual_outcome__"] = generated
        generated = row.get("__discovery_observable__")
        if episode_mode:
            generated = row.get("__discovery_counterfactual_outcome__")
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and value == value and abs(float(value)) != float("inf")
                and isinstance(generated, (int, float))
                and not isinstance(generated, bool)
                and generated == generated
                and abs(float(generated)) != float("inf")):
            observed.append(float(value))
            counter.append(float(generated))
    paired = len(observed)
    if not paired:
        return CounterfactualResult(program.counterfactual.intervention, len(fit), len(confirm), 0, None, None, None, content_hash([]), "INCONCLUSIVE", content_hash(program.exit_profile.as_dict()))
    observed, counter = observed[:paired], counter[:paired]
    observed_mean, counter_mean = sum(observed) / paired, sum(counter) / paired
    payload = {"program": program.content_hash, "fit": len(fit), "confirm": len(confirm), "observed": observed, "counterfactual": counter}
    return CounterfactualResult(program.counterfactual.intervention, len(fit), len(confirm), paired, observed_mean, counter_mean, observed_mean - counter_mean, content_hash(payload), "COMPLETE", content_hash(program.exit_profile.as_dict()))


__all__ = ["CounterfactualResult", "fit_window", "intervene_rows", "run_counterfactual"]
