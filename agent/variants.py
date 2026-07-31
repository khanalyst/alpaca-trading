"""Named parameter variants: the attribution key for everything downstream.

A variant is one hypothesis about one parameter, expressed as an override on
a base configuration. ``momentum.rr.fixed_2_5`` says "the same strategy, with
``strategy.fixed_reward_risk`` set to 2.5, because a 2.5R target may beat the
default 2.0R". That sentence is required: a variant that cannot state what it
claims is a parameter sweep pretending to be a question.

**Why a name rather than a hash.** The journal already fingerprints the whole
configuration, and that value cannot serve as the attribution key. It changes
when ``alerts.timeout_seconds`` changes, so an edit that cannot possibly
affect a decision forks the bucket and halves a sample that is already too
small - and because the identifier is sixteen hex characters, nothing about
the result says which field moved. ``variant_id`` is human-readable, stable
across irrelevant edits, and comparable across runs.

**Why registration validates.** ``apply()`` runs the overridden config through
the same ``validate_config`` the live agent uses, so a variant that would
produce an invalid configuration fails at registration rather than after a
week of replay. That matches the repository's fail-closed house style: the
error arrives when it is cheap.

Live trading writes ``variant_id = "live"``.
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import ConfigError, validate_config


LIVE_VARIANT_ID = "live"

STATUSES = (
    "candidate",    # registered, never run
    "testing",      # has results, decision rule not yet satisfied
    "promoted",     # passed the promotion protocol
    "rejected",     # failed it, or structurally invalid on inspection
    "superseded",   # replaced by a later variant of the same idea
)


@dataclass(frozen=True)
class Variant:
    """One named hypothesis about one set of parameters."""

    variant_id: str
    strategy_id: str
    base_version: str
    overrides: dict = field(default_factory=dict)
    hypothesis: str = ""
    status: str = "candidate"
    hypothesis_id: str | None = None
    hypothesis_params: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _is_variant_id(self.variant_id):
            raise ConfigError(
                f"variant_id {self.variant_id!r} must be dotted lowercase "
                "alphanumerics, e.g. momentum.rr.fixed_2_5")
        if not self.strategy_id:
            raise ConfigError(
                f"{self.variant_id}: strategy_id is required")
        if not self.base_version:
            raise ConfigError(
                f"{self.variant_id}: base_version is required")
        if self.status not in STATUSES:
            raise ConfigError(
                f"{self.variant_id}: status must be one of "
                f"{', '.join(STATUSES)}")
        # A one-sentence claim, required. The rule exists because a sweep
        # without a stated hypothesis produces a table nobody can act on: at
        # these sample sizes something always looks best, and without a claim
        # written beforehand there is no way to tell a result from a ranking
        # of noise.
        if len(self.hypothesis.strip()) < 10:
            raise ConfigError(
                f"{self.variant_id}: hypothesis is required and must say "
                "what the variant claims, in a sentence")
        if not isinstance(self.overrides, dict):
            raise ConfigError(f"{self.variant_id}: overrides must be a map")
        for path in self.overrides:
            if not isinstance(path, str) or not path:
                raise ConfigError(
                    f"{self.variant_id}: override keys must be dotted paths")
        if self.hypothesis_id is not None and not str(self.hypothesis_id).strip():
            raise ConfigError(f"{self.variant_id}: hypothesis_id cannot be empty")
        if not isinstance(self.hypothesis_params, dict):
            raise ConfigError(f"{self.variant_id}: hypothesis_params must be a map")


def _is_variant_id(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    parts = value.split(".")
    return all(
        part and all(c.islower() or c.isdigit() or c == "_" for c in part)
        for part in parts
    )


def strategy_variant_prefix(strategy_id: str) -> str:
    """Return the stable dotted-ID-safe prefix for a strategy."""
    return str(strategy_id).replace("-", "_")


def baseline_variant_id(strategy_id: str) -> str:
    return f"{strategy_variant_prefix(strategy_id)}.baseline"


def apply(variant: Variant, base_cfg: dict, *,
          allow_shadow_strategy: bool = False) -> dict:
    """Return ``base_cfg`` with the variant's overrides applied and validated.

    Deep-copies first: a variant that mutated the base configuration would
    silently contaminate every variant applied after it in the same process,
    and the resulting corruption would look like a real difference between
    variants rather than a bug.
    """
    cfg = copy.deepcopy(base_cfg)
    for path, value in variant.overrides.items():
        _set_dotted(cfg, path, value, variant.variant_id)
    try:
        validated = validate_config(
            cfg, allow_shadow_strategy=allow_shadow_strategy)
    except ConfigError as exc:
        raise ConfigError(
            f"variant {variant.variant_id} produces an invalid config: {exc}"
        ) from exc
    return validated


def _set_dotted(cfg: dict, path: str, value, variant_id: str) -> None:
    """Set ``a.b.c`` in a nested dict, refusing to invent structure.

    An unknown path is an error rather than a new key. A typo like
    ``strategy.fixed_reward_ratio`` would otherwise register cleanly, replay
    for a week, and report that the parameter made no difference - which
    would be true, and completely misleading.
    """
    parts = path.split(".")
    node = cfg
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(
                f"variant {variant_id}: override path {path!r} does not "
                f"exist in the base config")
        node = node[part]
    leaf = parts[-1]
    if not isinstance(node, dict) or leaf not in node:
        raise ConfigError(
            f"variant {variant_id}: override path {path!r} does not exist "
            f"in the base config")
    node[leaf] = value


def baseline(strategy_id: str, base_version: str) -> Variant:
    """The unmodified configuration, as a variant, so it can be compared."""
    return Variant(
        variant_id=baseline_variant_id(strategy_id),
        strategy_id=strategy_id,
        base_version=base_version,
        overrides={},
        hypothesis="The configuration as shipped. The comparison floor for "
                   "every other variant of this strategy.",
        status="testing",
    )


def hypothesis_variants(strategy_id: str, base_version: str) -> list[Variant]:
    """Return deterministic first-class variants for registered hypotheses.

    Hypothesis parameters are stored as experiment metadata, not as invented
    executable config paths.  The deterministic contract consumes them at
    proposal validation time; ordinary strategy overrides remain in
    ``overrides`` and continue to use ``apply`` unchanged.
    """
    from . import hypotheses

    out = []
    for spec in sorted(hypotheses.REGISTRY.values(), key=lambda item: item.id):
        for setting in sorted(spec.settings, key=lambda item: str(item["id"])):
            setting_id = str(setting["id"])
            safe_hypothesis_id = spec.id.replace("-", "_")
            variant_id = f"{strategy_id}.hyp.{safe_hypothesis_id}.{setting_id}"
            params = dict(spec.contract_params)
            params.update(setting.get("params") or {})
            out.append(Variant(
                variant_id=variant_id,
                strategy_id=strategy_id,
                base_version=base_version,
                hypothesis=str(setting.get("claim") or spec.claim),
                status="candidate",
                hypothesis_id=spec.id,
                hypothesis_params=params,
            ))
    return out


def adaptive_hypothesis_variant(strategy_id: str, base_version: str,
                                hypothesis_id: str, setting_id: str,
                                value: float) -> Variant:
    """Materialize one exact, auditable numeric proposal as a variant."""
    from . import hypotheses
    spec = hypotheses.spec_for(hypothesis_id)
    metadata = hypotheses.numeric_setting_metadata(hypothesis_id, setting_id)
    if spec is None or metadata is None:
        raise ConfigError("adaptive proposal is not registered")
    number = float(value)
    if (not math.isfinite(number) or number < metadata["minimum"]
            or number > metadata["maximum"]):
        raise ConfigError("adaptive proposal is outside registered bounds")
    params = dict(spec.contract_params)
    setting = next(s for s in spec.settings if str(s.get("id")) == str(setting_id))
    params.update(setting.get("params") or {})
    params[metadata["target_parameter"]] = number
    safe = str(hypothesis_id).replace("-", "_")
    value_id = format(number, ".15g").replace("-", "m").replace(".", "_")
    return Variant(
        variant_id=f"{strategy_id}.hyp.{safe}.{setting_id}.adaptive_{value_id}",
        strategy_id=strategy_id, base_version=base_version,
        hypothesis=(f"Adaptive value {number:g} for {hypothesis_id}/{setting_id}: "
                    "test the registered mechanism at this exact point."),
        hypothesis_id=hypothesis_id, hypothesis_params=params)


def declared_research_setting(
        variant: Variant, proposal: dict | None = None) -> dict | None:
    """Return the one registered axis represented by an eligible candidate.

    This is the shared eligibility boundary for durable rotation, the LLM
    selector prompt, and selector validation.  A bundle that changes more
    than one executable setting is deliberately absent rather than being
    mislabeled as a single-axis experiment.
    """
    if proposal is not None:
        parameter = str(proposal.get("target_parameter") or "").strip()
        if not parameter:
            return None
        return {
            "axis": f"hypothesis.{variant.hypothesis_id}.{parameter}",
            "setting_id": str(proposal.get("setting_id") or "adaptive"),
            "setting": {parameter: proposal.get("value")},
        }

    from . import hypotheses

    spec = hypotheses.spec_for(str(variant.hypothesis_id or ""))
    if spec is not None:
        for setting in sorted(
                spec.settings, key=lambda item: str(item.get("id") or "")):
            expected = dict(spec.contract_params)
            expected.update(setting.get("params") or {})
            if expected != variant.hypothesis_params:
                continue
            metadata = hypotheses.numeric_setting_metadata(
                spec.id, str(setting["id"]))
            if metadata is None:
                return None
            parameter = metadata["target_parameter"]
            return {
                "axis": f"hypothesis.{spec.id}.{parameter}",
                "setting_id": str(setting["id"]),
                "setting": {parameter: expected[parameter]},
            }
    if len(variant.overrides) != 1:
        return None
    axis, value = next(iter(variant.overrides.items()))
    return {
        "axis": str(axis),
        "setting_id": variant.variant_id.rsplit(".", 1)[-1],
        "setting": {str(axis): value},
    }


def from_record(record: dict) -> Variant:
    """Rebuild an immutable Variant from its persisted findings row."""
    overrides = record.get("overrides", record.get("overrides_json", {}))
    hypothesis_params = record.get(
        "hypothesis_params", record.get("hypothesis_params_json", {}))
    if isinstance(overrides, str):
        overrides = json.loads(overrides)
    if isinstance(hypothesis_params, str):
        hypothesis_params = json.loads(hypothesis_params)
    return Variant(
        variant_id=str(record["variant_id"]),
        strategy_id=str(record["strategy_id"]),
        base_version=str(record["base_version"]),
        overrides=dict(overrides or {}),
        hypothesis=str(record["hypothesis"]),
        status=str(record.get("status") or "candidate"),
        hypothesis_id=record.get("hypothesis_id"),
        hypothesis_params=dict(hypothesis_params or {}),
    )


def preregistered_variants(
        strategy_id: str, base_version: str,
        path: str | Path | None = None) -> list[Variant]:
    """Materialize research/hypotheses/<strategy>.yaml settings.

    YAML is the pre-registration source of truth for research axes; the
    resulting objects use the same immutable FindingsStore identity as
    ordinary shadow variants. No status is changed and no paper/live action
    is implied.
    """
    if path is None:
        path = (Path(__file__).resolve().parents[1] / "research" /
                "hypotheses" / f"{strategy_id}.yaml")
    source = Path(path)
    if not source.exists():
        return []
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    settings = raw.get("settings") or []
    if not isinstance(settings, list):
        raise ConfigError(f"{source}: settings must be a list")
    out = []
    aliases = {
        "flush-fade": {
            "min_move_atr": "flush_min_move_atr",
            "min_oi_drop_pct": "flush_min_oi_drop_pct",
            "min_relative_volume": "flush_min_relative_volume",
        },
        "funding-carry": {
            "percentile": "carry_percentile",
            "min_samples": "carry_min_samples",
        },
        "funding-unwind": {
            "percentile": "unwind_percentile",
            "min_samples": "unwind_min_samples",
        },
        "trend-multiday": {
            "min_range_pos_pct": "trend_min_range_pos_pct",
            "max_atr_ratio": "trend_max_atr_ratio",
            "max_hold_bars": "forward_horizon_hours",
        },
    }
    for entry in settings:
        if not isinstance(entry, dict) or not entry.get("id"):
            raise ConfigError(f"{source}: each setting requires an id")
        setting_id = str(entry["id"])
        params = dict(entry.get("params") or {})
        normalized = {}
        for key, value in params.items():
            target = aliases.get(strategy_id, {}).get(key, key)
            # Tournament bars are 15 minutes; the forward evaluator stores
            # the same declared horizon in hours.
            if strategy_id == "trend-multiday" and key == "max_hold_bars":
                value = float(value) * 0.25
            normalized[target] = value
        overrides = {
            (key if "." in key else f"strategy.{key}"): value
            for key, value in normalized.items()
        }
        out.append(Variant(
            variant_id=(
                f"{strategy_variant_prefix(strategy_id)}.prereg.{setting_id}"),
            strategy_id=strategy_id,
            base_version=base_version,
            overrides=overrides,
            hypothesis=str(entry.get("claim") or raw.get("prediction") or ""),
            status="candidate",
            hypothesis_id=f"{strategy_id}:{setting_id}",
            hypothesis_params=params,
        ))
    return out


def load_registry(path: str | Path) -> dict[str, Variant]:
    """Load and validate ``research/variants.yaml``.

    Validation happens on load rather than on use, so a malformed registry
    fails once at the start of a run instead of halfway through a sweep.
    """
    path = Path(path)
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("variants") if isinstance(raw, dict) else raw
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise ConfigError(
            f"{path}: expected a list of variants under 'variants:'")

    out: dict[str, Variant] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: variant #{index} is not a mapping")
        unknown = set(entry) - {
            "variant_id", "strategy_id", "base_version", "overrides",
            "hypothesis", "status", "hypothesis_id", "hypothesis_params",
        }
        if unknown:
            raise ConfigError(
                f"{path}: variant #{index} has unknown field(s): "
                f"{', '.join(sorted(unknown))}")
        variant = Variant(
            variant_id=str(entry.get("variant_id", "")),
            strategy_id=str(entry.get("strategy_id", "")),
            base_version=str(entry.get("base_version", "")),
            overrides=dict(entry.get("overrides") or {}),
            hypothesis=str(entry.get("hypothesis", "")),
            status=str(entry.get("status", "candidate")),
            hypothesis_id=entry.get("hypothesis_id"),
            hypothesis_params=dict(entry.get("hypothesis_params") or {}),
        )
        if variant.variant_id in out:
            raise ConfigError(
                f"{path}: duplicate variant_id {variant.variant_id!r}")
        out[variant.variant_id] = variant
    return out


def research_selection_catalog() -> dict[str, tuple[dict, ...]]:
    """Enumerate registered, single-axis, non-baseline selector targets."""
    from . import registry as strategy_registry

    registry_path = (
        Path(__file__).resolve().parents[1] / "research" / "variants.yaml")
    registered = load_registry(registry_path)
    for strategy_id in sorted(strategy_registry.REGISTRY):
        spec = strategy_registry.spec_for(strategy_id)
        generated = []
        if strategy_id == "momentum":
            generated.extend(hypothesis_variants(strategy_id, spec.version))
        generated.extend(preregistered_variants(strategy_id, spec.version))
        for variant in generated:
            registered.setdefault(variant.variant_id, variant)

    catalog: dict[str, list[dict]] = {}
    for variant in registered.values():
        if (variant.status not in {"candidate", "testing"}
                or variant.variant_id == baseline_variant_id(variant.strategy_id)):
            continue
        declared = declared_research_setting(variant)
        if declared is None:
            continue
        catalog.setdefault(variant.strategy_id, []).append({
            "strategy_id": variant.strategy_id,
            "variant_id": variant.variant_id,
            **declared,
        })
    return {strategy_id: tuple(items)
            for strategy_id, items in sorted(catalog.items()) if items}


def save_registry(path: str | Path, variants: dict[str, Variant]) -> None:
    """Write the registry back, sorted, so the file diffs cleanly."""
    payload = {"variants": [
        {
            "variant_id": v.variant_id,
            "strategy_id": v.strategy_id,
            "base_version": v.base_version,
            "overrides": dict(v.overrides),
            "hypothesis": v.hypothesis,
            "status": v.status,
            "hypothesis_id": v.hypothesis_id,
            "hypothesis_params": dict(v.hypothesis_params),
        }
        for _, v in sorted(variants.items())
    ]}
    Path(path).write_text(
        yaml.safe_dump(payload, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
