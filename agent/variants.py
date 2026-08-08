"""Small immutable variant helpers for the IBR contract.

Variants tune the IBR parameters for research; they never create a second
alpha family or an independent options signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import copy
import json
from pathlib import Path
from typing import Mapping

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from .config import ConfigError, validate_config
from .registry import validate_variant_id


@dataclass(frozen=True)
class Variant:
    variant_id: str
    strategy_id: str
    base_version: str
    overrides: dict = field(default_factory=dict)
    hypothesis: str = ""
    vehicles: tuple[str, ...] = ("equity", "option")
    vehicle: str | None = None

    def __post_init__(self) -> None:
        if self.vehicle is not None:
            if self.vehicle not in {"equity", "option"}:
                raise ConfigError("variant vehicle must be equity or option")
            object.__setattr__(self, "vehicles", (self.vehicle,))
        elif len(self.vehicles) == 1:
            object.__setattr__(self, "vehicle", self.vehicles[0])


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


def apply(variant: Variant, cfg: dict) -> dict:
    validate_variant_id(variant.strategy_id, variant.variant_id)
    out = copy.deepcopy(cfg)
    strategy = out.setdefault("strategy", {})
    strategy["variant_id"] = variant.variant_id
    for path, value in variant.overrides.items():
        _set_dotted(out, path, value, variant.variant_id)
    try:
        return validate_config(out)
    except Exception as exc:
        raise ConfigError(f"variant {variant.variant_id} produces an invalid config: {exc}") from exc


def from_record(record: Mapping) -> Variant:
    def decode(value):
        if isinstance(value, str):
            try: return json.loads(value)
            except json.JSONDecodeError: return {}
        return value or {}
    vehicles = record.get("vehicles", record.get("vehicle", ("equity", "option")))
    if isinstance(vehicles, str):
        vehicles = (vehicles,)
    vehicles = tuple(str(item) for item in (vehicles or ("equity", "option")))
    if not set(vehicles).issubset({"equity", "option"}):
        raise ConfigError("variant vehicles must be equity and/or option")
    return Variant(str(record["variant_id"]), str(record["strategy_id"]),
                   str(record.get("base_version") or "v1"), dict(decode(record.get("overrides"))),
                   str(record.get("hypothesis") or ""), vehicles)


def load_registry(path: str | Path) -> dict[str, Variant]:
    path = Path(path)
    if not path.exists(): return {}
    source = path.read_text(encoding="utf-8")
    if yaml is not None:
        raw = yaml.safe_load(source) or {}
    else:
        # The checked-in registry is JSON-shaped YAML, so the stdlib fallback
        # remains usable in the minimal research container.
        try:
            raw = json.loads(source) or {}
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path}: PyYAML is unavailable and registry is not JSON") from exc
    entries = raw.get("variants", raw) if isinstance(raw, Mapping) else raw
    if not isinstance(entries, list): raise ConfigError(f"{path}: expected a list of variants")
    out = {}
    for entry in entries:
        if not isinstance(entry, Mapping): raise ConfigError(f"{path}: variant must be a mapping")
        variant = from_record(entry)
        if variant.variant_id in out: raise ConfigError(f"{path}: duplicate variant_id {variant.variant_id!r}")
        out[variant.variant_id] = variant
    return out
