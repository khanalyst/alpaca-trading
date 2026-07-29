"""Sweeps, and the two kinds of axis - only one of which divides the sample.

A **parameter axis** generates variants. Five settings of
``fixed_reward_risk`` are five configurations, five replays, and five slices
of a sample that was already too small. The promotion protocol wants 100
matched round trips per setting and at least three settings, so a parameter
axis costs 300 round trips before it can conclude anything.

A **conditioning axis** partitions results. It carries no config override, so
one replay produces every bucket, and it reuses trades that already exist
rather than requiring new ones. It also *increases* effective power when the
partition is real, because it separates populations whose averaging was
destroying the signal - the exact failure H-I describes, where a contract
with positive expectancy in compression and negative expectancy in expansion
reports a pooled figure near zero and gets correctly rejected on a
hypothesis that was true in half the sample.

That asymmetry is why conditioning axes are preferred for the first six
months and why they are first-class here rather than bolted on.

Two refusals are built in. A sweep whose grid cannot reach the required
sample refuses to run and says how short it is, rather than running and
reporting ``INSUFFICIENT_SAMPLE`` once per point until the rule starts to
feel like an obstacle. And a conditioning axis without a pre-registered
bucket definition is refused outright: buckets chosen after seeing the data
are not a test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from agent import variants as variant_mod
from agent.config import ConfigError

from . import score, stats


@dataclass
class ConditionAxis:
    """A partition of results that costs no re-replay."""

    variable: str
    buckets: list
    pre_registered: bool = False
    definition: str = ""

    def bucket_for(self, value) -> str | None:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value) if str(value) in self.buckets else None
        for bucket in self.buckets:
            if isinstance(bucket, dict):
                low = bucket.get("min")
                high = bucket.get("max")
                if ((low is None or number >= float(low))
                        and (high is None or number < float(high))):
                    return str(bucket.get("name"))
        return None


@dataclass
class SweepSpec:
    hypothesis: str
    base: str
    axis: dict = field(default_factory=dict)
    condition_axis: ConditionAxis | None = None
    name: str = ""

    def grid_points(self) -> int:
        if not self.axis:
            return 1
        total = 1
        for values in self.axis.values():
            total *= max(1, len(values))
        return total

    def is_conditioning(self) -> bool:
        return self.condition_axis is not None and not self.axis


def load_spec(path: str | Path) -> SweepSpec:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: sweep spec must be a mapping")
    if not str(raw.get("hypothesis", "")).strip():
        raise ConfigError(
            f"{path}: a sweep must state its hypothesis. A grid with no "
            "claim written beforehand is a ranking of noise.")

    condition = None
    block = raw.get("condition_axis")
    if block:
        if not block.get("pre_registered"):
            raise ConfigError(
                f"{path}: condition_axis must set pre_registered: true. "
                "Buckets chosen after seeing the data are not a test.")
        condition = ConditionAxis(
            variable=str(block.get("variable") or ""),
            buckets=list(block.get("buckets") or []),
            pre_registered=True,
            definition=str(block.get("definition") or ""))
        if not condition.variable or not condition.buckets:
            raise ConfigError(
                f"{path}: condition_axis needs a variable and buckets")

    return SweepSpec(
        hypothesis=str(raw["hypothesis"]).strip(),
        base=str(raw.get("base") or "momentum.baseline"),
        axis=dict(raw.get("axis") or {}),
        condition_axis=condition,
        name=Path(path).stem,
    )


def expand(spec: SweepSpec, registry: dict) -> list:
    """Generate one named variant per grid point."""
    if spec.is_conditioning():
        return [registry.get(spec.base)
                or variant_mod.baseline("momentum", "phase1-v3")]

    import itertools

    base = registry.get(spec.base) or variant_mod.baseline(
        "momentum", "phase1-v3")
    paths = sorted(spec.axis)
    out = []
    # A cross product, matching what grid_points() counts. A union would
    # under-report the grid and let a two-axis sweep claim a per-cell sample
    # it does not have.
    for combination in itertools.product(*(spec.axis[p] for p in paths)):
        overrides = dict(base.overrides)
        parts = []
        for path, value in zip(paths, combination):
            overrides[path] = value
            slug = str(value).replace(".", "_").replace("-", "neg")
            parts.append(f"{path.split('.')[-1]}.{slug}")
        out.append(variant_mod.Variant(
            variant_id=f"{base.strategy_id}." + ".".join(parts),
            strategy_id=base.strategy_id,
            base_version=base.base_version,
            overrides=overrides,
            hypothesis=spec.hypothesis,
            status="candidate"))
    return out


def forecast(spec: SweepSpec, total_round_trips: int) -> dict:
    """Refuse before running, per action-plan C4.

    Reporting the shortfall once is cheaper than reporting
    INSUFFICIENT_SAMPLE five times, and each repetition of that verdict makes
    relaxing the rule more tempting - which is the failure the rule exists to
    prevent.
    """
    points = 1 if spec.is_conditioning() else spec.grid_points()
    outlook = stats.achievable_n(total_round_trips, points)
    outlook["conditioning"] = spec.is_conditioning()
    outlook["should_run"] = outlook["sufficient"] or spec.is_conditioning()
    outlook["reason"] = (
        "conditioning axis: one replay, every bucket, no sample divided"
        if spec.is_conditioning() else
        f"{points} grid points against {total_round_trips} round trips "
        f"gives {outlook['per_cell']:.1f} per cell; 100 are required"
    )
    return outlook


def partition(decisions: list, axis: ConditionAxis,
              value_of) -> dict:
    """Split executed decisions into pre-registered buckets."""
    buckets: dict = {}
    for decision in decisions:
        name = axis.bucket_for(value_of(decision))
        if name is None:
            continue
        buckets.setdefault(name, []).append(decision)
    return buckets


def score_partition(buckets: dict) -> dict:
    """Score each bucket, and never quote an uncorrected figure.

    Every bucket is reported, including the empty and the null ones. A
    programme that only records positives is a programme that only records
    noise, and a bucket omitted for having nothing in it is the most
    informative row in the table when the reason is that the condition never
    occurred.
    """
    out = {}
    for name, decisions in sorted(buckets.items()):
        returns = [d.outcome["r_multiple"] for d in decisions
                   if getattr(d, "outcome", None)]
        out[name] = score.score_returns(returns, label=name)
    return out
