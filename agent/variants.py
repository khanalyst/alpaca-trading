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


def _is_variant_id(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    parts = value.split(".")
    return all(
        part and all(c.islower() or c.isdigit() or c == "_" for c in part)
        for part in parts
    )


def apply(variant: Variant, base_cfg: dict) -> dict:
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
        validated = validate_config(cfg)
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
        variant_id=f"{strategy_id}.baseline",
        strategy_id=strategy_id,
        base_version=base_version,
        overrides={},
        hypothesis="The configuration as shipped. The comparison floor for "
                   "every other variant of this strategy.",
        status="testing",
    )


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
            "hypothesis", "status",
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
        )
        if variant.variant_id in out:
            raise ConfigError(
                f"{path}: duplicate variant_id {variant.variant_id!r}")
        out[variant.variant_id] = variant
    return out


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
        }
        for _, v in sorted(variants.items())
    ]}
    Path(path).write_text(
        yaml.safe_dump(payload, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
