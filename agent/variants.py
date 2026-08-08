"""Small immutable variant helpers for the IBR contract.

Variants tune the IBR parameters for research; they never create a second
alpha family or an independent options signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import copy
import json
import math
from pathlib import Path
from typing import Mapping

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from .config import ConfigError, validate_config
from .registry import baseline_variant_id, spec_for

LIVE_VARIANT_ID = "live"
MIN_AXIS_SETTINGS = 3
TUNED_LS_VARIANT_ID = ""


@dataclass(frozen=True)
class Variant:
    variant_id: str
    strategy_id: str
    base_version: str
    overrides: dict = field(default_factory=dict)
    hypothesis: str = ""
    status: str = "candidate"
    hypothesis_id: str | None = None
    hypothesis_params: dict = field(default_factory=dict)


def strategy_variant_prefix(strategy_id: str) -> str:
    return str(strategy_id).replace("-", "_")


def baseline(strategy_id: str, base_version: str) -> Variant:
    return Variant(baseline_variant_id(strategy_id), strategy_id, base_version,
                   hypothesis="The registered IBR configuration baseline.", status="testing")


def _set_dotted(cfg: dict, path: str, value, variant_id: str):
    node = cfg
    parts = path.split(".")
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(f"variant {variant_id}: override path {path!r} does not exist")
        node = node[part]
    if not isinstance(node, dict) or parts[-1] not in node:
        raise ConfigError(f"variant {variant_id}: override path {path!r} does not exist")
    node[parts[-1]] = value


def apply(variant: Variant, cfg: dict, *, allow_shadow_strategy: bool = False) -> dict:
    out = copy.deepcopy(cfg)
    strategy = out.setdefault("strategy", {})
    strategy["variant_id"] = variant.variant_id
    for path, value in variant.overrides.items():
        _set_dotted(out, path, value, variant.variant_id)
    try:
        return validate_config(out, allow_shadow_strategy=allow_shadow_strategy)
    except Exception as exc:
        raise ConfigError(f"variant {variant.variant_id} produces an invalid config: {exc}") from exc


def from_record(record: Mapping) -> Variant:
    def decode(value):
        if isinstance(value, str):
            try: return json.loads(value)
            except json.JSONDecodeError: return {}
        return value or {}
    return Variant(str(record["variant_id"]), str(record["strategy_id"]),
                   str(record.get("base_version") or "v1"), dict(decode(record.get("overrides"))),
                   str(record.get("hypothesis") or ""), str(record.get("status") or "candidate"),
                   record.get("hypothesis_id"), dict(decode(record.get("hypothesis_params"))))


def load_registry(path: str | Path) -> dict[str, Variant]:
    path = Path(path)
    if not path.exists() or yaml is None: return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("variants", raw) if isinstance(raw, Mapping) else raw
    if not isinstance(entries, list): raise ConfigError(f"{path}: expected a list of variants")
    out = {}
    for entry in entries:
        if not isinstance(entry, Mapping): raise ConfigError(f"{path}: variant must be a mapping")
        variant = from_record(entry)
        if variant.variant_id in out: raise ConfigError(f"{path}: duplicate variant_id {variant.variant_id!r}")
        out[variant.variant_id] = variant
    return out


def save_registry(path: str | Path, variants: Mapping[str, Variant]) -> None:
    if yaml is None: raise ConfigError("PyYAML is unavailable")
    payload = {"variants": [{"variant_id": v.variant_id, "strategy_id": v.strategy_id,
                              "base_version": v.base_version, "overrides": dict(v.overrides),
                              "hypothesis": v.hypothesis, "status": v.status,
                              "hypothesis_id": v.hypothesis_id,
                              "hypothesis_params": dict(v.hypothesis_params)}
                             for _, v in sorted(variants.items())]}
    Path(path).write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def preregistered_variants(strategy_id: str, base_version: str, path: str | Path | None = None) -> list[Variant]:
    if path is None:
        path = Path(__file__).resolve().parents[1] / "research" / "hypotheses" / f"{strategy_id}.yaml"
    if yaml is None or not Path(path).exists(): return []
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    result = []
    for item in raw.get("settings", []) if isinstance(raw, Mapping) else []:
        if not isinstance(item, Mapping) or not item.get("id"): continue
        params = dict(item.get("params") or {})
        overrides = {(key if "." in key else f"strategy.{key}"): value for key, value in params.items()}
        result.append(Variant(f"{strategy_variant_prefix(strategy_id)}.prereg.{item['id']}", strategy_id,
                              base_version, overrides, str(item.get("claim") or raw.get("prediction") or ""),
                              "candidate", f"{strategy_id}:{item['id']}", params))
    return result


def hypothesis_variants(strategy_id: str, base_version: str) -> list[Variant]:
    return preregistered_variants(strategy_id, base_version)


def adaptive_hypothesis_variant(strategy_id: str, base_version: str, hypothesis_id: str,
                                setting_id: str, value: float) -> Variant:
    number = float(value)
    if not math.isfinite(number): raise ConfigError("adaptive proposal is not finite")
    return Variant(f"{strategy_variant_prefix(strategy_id)}.hyp.{hypothesis_id.replace('-', '_')}.{setting_id}.adaptive_{number:g}",
                   strategy_id, base_version, {f"strategy.{setting_id}": number},
                   "Adaptive IBR parameter proposal.", "candidate", hypothesis_id, {setting_id: number})


def declared_research_setting(variant: Variant, proposal: Mapping | None = None) -> dict | None:
    if proposal is not None:
        key = str(proposal.get("target_parameter") or "")
        return {"axis": f"hypothesis.{variant.hypothesis_id}.{key}", "setting_id": str(proposal.get("setting_id") or "adaptive"), "setting": {key: proposal.get("value")}} if key else None
    if len(variant.overrides) != 1: return None
    key, value = next(iter(variant.overrides.items()))
    return {"axis": key, "setting_id": variant.variant_id.rsplit(".", 1)[-1], "setting": {key: value}}


def research_selection_catalog() -> dict[str, tuple[dict, ...]]:
    path = Path(__file__).resolve().parents[1] / "research" / "variants.yaml"
    registered = load_registry(path)
    result = {}
    for variant in registered.values():
        if variant.strategy_id != "ibr" or variant.status not in {"candidate", "testing"}: continue
        declared = declared_research_setting(variant)
        if declared is not None: result.setdefault("ibr", []).append({"strategy_id": "ibr", "variant_id": variant.variant_id, **declared})
    return {key: tuple(value) for key, value in result.items()}
