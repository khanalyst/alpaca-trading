"""Bounded, research-only observable discovery.

This module deliberately has no import path into the trading engine.  A
discovery program is a small, immutable description of an observable and its
pre-registered mechanism.  Programs are evaluated against point-in-time rows,
compiled into a content-addressed artifact, and can only produce research
evidence.  They never register a strategy or alter configuration.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import random
import statistics
import types
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence


SCHEMA = "discovery-ir.v1"
ARTIFACT_SCHEMA = "discovery-artifact.v1"
MAX_NODES = 32
MAX_DEPTH = 8
MAX_WINDOW = 256
MAX_ROWS = 50_000
MAX_CONSTANT = 1_000_000.0
_OPS = frozenset({
    "source", "constant", "negate", "lag", "rolling_mean", "rolling_std",
    "zscore", "difference", "ratio", "product", "abs", "clip",
    "book_imbalance",
})
_SOURCE_FIELDS = frozenset({
    # These are persisted snapshot/event-plane fields only.  Enrichment is
    # flattened before evaluation and may not introduce network state.
    "oi_change_4h_pct", "oi_change_1h_pct", "relative_volume_1h",
    "book_bid_depth_usd", "book_ask_depth_usd", "bid_depth_usd",
    "ask_depth_usd", "book_imbalance", "signal_ts", "event_ts_ms",
    "available_at_ms", "price", "direction", "symbol", "regime",
    "state", "outcome", "outcome_value", "net_pct", "liquidity",
    "exit_profile",
    "oi_usd", "buy_vol", "sell_vol", "imbalance_5bps", "ts", "inst_id",
    "ccy", "mid", "bid_depth_5bps", "ask_depth_5bps",
    "taker_imbalance",
    # Closed execution bars are flattened into the episode, not fetched by
    # an evaluator.  Keeping the OHLC fields in the persisted source set
    # allows an observable to be evaluated at any point in that bar path.
    "open", "high", "low", "close", "volume", "closed", "confirm",
    "bar_interval_ms", "funding_rate_pct", "realized_rate", "funding_rate",
    "settlement_time", "status",
})
_ROW_METADATA_FIELDS = frozenset({
    "bars", "episode_bars", "funding_complete", "funding_provenance",
    "entry_price", "stop_pct", "direction", "symbol", "signal_ts",
    "entry_ts", "signal_timeframe", "spread_pct", "funding_rate_pct",
    "funding_interval_hours", "taker_fee_pct_per_side", "state",
    "outcome", "outcome_value", "net_pct", "liquidity", "exit_profile",
    "bar_interval_ms", "episode_status", "terminal_state",
    "observable_values", "observable_path_status", "taker_imbalance",
    "funding_status",
})
_EVENT_PROVENANCE_FIELDS = frozenset({
    # Immutable event-plane identity and provenance.  These fields are
    # carried alongside (but are never expression sources) so an observable
    # can be replayed against exactly the evidence that produced it.
    "event_id", "feed", "schema", "quality", "payload_hash",
    "snapshot_id", "ingest_id", "revision_id", "source", "series",
    "instrument", "status", "source_file", "source_row",
})
_WINDOW_OPS = frozenset({"lag", "rolling_mean", "rolling_std", "zscore"})


def _safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_safe(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(_safe(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(v) for v in value]
    return value


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _number(value: Any, name: str, *, lo: float = -MAX_CONSTANT,
            hi: float = MAX_CONSTANT) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    result = _finite(value)
    if result is None or not lo <= result <= hi:
        raise ValueError(f"{name} must be a finite number in [{lo:g}, {hi:g}]")
    return result


def _validate_node(raw: Any, depth: int = 0) -> tuple[Any, int]:
    if depth > MAX_DEPTH:
        raise ValueError(f"observable expression exceeds depth {MAX_DEPTH}")
    if not isinstance(raw, Mapping):
        raise ValueError("observable expression nodes must be mappings")
    op = raw.get("op", raw.get("type"))
    if not isinstance(op, str) or op not in _OPS:
        raise ValueError(f"unsupported observable operator {op!r}")
    if op == "source":
        if set(raw) - {"op", "type", "field"}:
            raise ValueError("source node has unknown fields")
        field_name = raw.get("field")
        if field_name not in _SOURCE_FIELDS:
            raise ValueError(f"source field {field_name!r} is not persisted")
        return {"op": op, "field": str(field_name)}, 1
    if op == "constant":
        if set(raw) - {"op", "type", "value"}:
            raise ValueError("constant node has unknown fields")
        return {"op": op, "value": _number(raw.get("value"), "constant")}, 1
    if op == "book_imbalance":
        if set(raw) - {"op", "type"}:
            raise ValueError("book_imbalance node has unknown fields")
        return {"op": op}, 1
    if op in {"negate", "abs"}:
        if set(raw) - {"op", "type", "arg"}:
            raise ValueError(f"{op} node has unknown fields")
        child, count = _validate_node(raw.get("arg"), depth + 1)
        return {"op": op, "arg": child}, count + 1
    if op in {"lag", "rolling_mean", "rolling_std", "zscore"}:
        if set(raw) - {"op", "type", "arg", "window", "period"}:
            raise ValueError(f"{op} node has unknown fields")
        child, count = _validate_node(raw.get("arg"), depth + 1)
        if child.get("op") in _WINDOW_OPS or _contains_window(child):
            raise ValueError("nested history/window transforms are not supported")
        window = raw.get("window", raw.get("period", 1))
        if isinstance(window, bool) or not isinstance(window, int):
            raise ValueError(f"{op} window must be an integer")
        if not 1 <= window <= MAX_WINDOW:
            raise ValueError(f"{op} window must be between 1 and {MAX_WINDOW}")
        return {"op": op, "arg": child, "window": window}, count + 1
    if op in {"difference", "ratio", "product"}:
        if set(raw) - {"op", "type", "args"}:
            raise ValueError(f"{op} node has unknown fields")
        args = raw.get("args")
        if not isinstance(args, (list, tuple)) or len(args) != 2:
            raise ValueError(f"{op} requires exactly two args")
        left, left_count = _validate_node(args[0], depth + 1)
        right, right_count = _validate_node(args[1], depth + 1)
        return {"op": op, "args": [left, right]}, left_count + right_count + 1
    if op == "clip":
        if set(raw) - {"op", "type", "arg", "lo", "hi"}:
            raise ValueError("clip node has unknown fields")
        child, count = _validate_node(raw.get("arg"), depth + 1)
        lo = _number(raw.get("lo"), "clip.lo")
        hi = _number(raw.get("hi"), "clip.hi")
        if lo > hi:
            raise ValueError("clip.lo must not exceed clip.hi")
        return {"op": op, "arg": child, "lo": lo, "hi": hi}, count + 1
    raise AssertionError(op)


def _contains_window(node: Mapping[str, Any]) -> bool:
    if node.get("op") in _WINDOW_OPS:
        return True
    if isinstance(node.get("arg"), Mapping) and _contains_window(node["arg"]):
        return True
    return any(_contains_window(child) for child in node.get("args", [])
               if isinstance(child, Mapping))


def _validate_count(node: Mapping[str, Any]) -> int:
    normalized, count = _validate_node(node)
    del normalized
    if count > MAX_NODES:
        raise ValueError(f"observable expression exceeds {MAX_NODES} nodes")
    return count


def _max_node_window(node: Mapping[str, Any]) -> int:
    values = [int(node.get("window", 1))]
    for child in node.get("args", []):
        values.append(_max_node_window(child))
    if isinstance(node.get("arg"), Mapping):
        values.append(_max_node_window(node["arg"]))
    return max(values)


def _node_depth(node: Mapping[str, Any]) -> int:
    children = []
    if isinstance(node.get("arg"), Mapping):
        children.append(node["arg"])
    children.extend(child for child in node.get("args", [])
                    if isinstance(child, Mapping))
    return 1 + max((_node_depth(child) for child in children), default=0)


@dataclass(frozen=True)
class ObservableSpec:
    name: str
    expression: Mapping[str, Any]
    source_version: str = "event-plane.v1"
    max_window: int = MAX_WINDOW

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("observable name is required")
        normalized, count = _validate_node(self.expression)
        if count > MAX_NODES:
            raise ValueError(f"observable expression exceeds {MAX_NODES} nodes")
        if _max_node_window(normalized) > int(self.max_window):
            raise ValueError("observable expression window exceeds max_window")
        if isinstance(self.max_window, bool) or not 1 <= int(self.max_window) <= MAX_WINDOW:
            raise ValueError("observable max_window is outside its bound")
        object.__setattr__(self, "expression", _freeze(normalized))

    @property
    def node_count(self) -> int:
        return _validate_count(_thaw(self.expression))

    @property
    def depth(self) -> int:
        return _node_depth(_thaw(self.expression))

    @property
    def identity(self) -> dict:
        return {"schema": SCHEMA, "name": self.name,
                "source_version": self.source_version,
                "expression": _thaw(self.expression),
                "max_window": int(self.max_window)}

    @property
    def content_hash(self) -> str:
        return content_hash(self.identity)

    @property
    def hash(self) -> str:
        return self.content_hash

    def as_dict(self) -> dict:
        return self.identity | {"content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ObservableSpec":
        return cls(name=str(payload["name"]), expression=payload["expression"],
                   source_version=str(payload.get("source_version", "event-plane.v1")),
                   max_window=int(payload.get("max_window", MAX_WINDOW)))

    def evaluate(self, row: Mapping[str, Any], history: Sequence[Mapping[str, Any]] | None = None) -> float | None:
        return evaluate_expression(_thaw(self.expression), row, history)


@dataclass(frozen=True)
class CausalClaim:
    payer: str
    mechanism: str
    prediction: str
    falsifier: str
    confounders_or_limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("payer", "mechanism", "prediction", "falsifier"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"causal claim {field_name} is required")
        object.__setattr__(self, "confounders_or_limitations", tuple(
            str(value).strip() for value in self.confounders_or_limitations
            if str(value).strip()))

    def as_dict(self) -> dict:
        return {"payer": self.payer, "mechanism": self.mechanism,
                "prediction": self.prediction, "falsifier": self.falsifier,
                "confounders_or_limitations": list(self.confounders_or_limitations)}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CausalClaim":
        return cls(str(payload["payer"]), str(payload["mechanism"]),
                   str(payload["prediction"]), str(payload["falsifier"]),
                   tuple(payload.get("confounders_or_limitations", ())))


@dataclass(frozen=True)
class EntryRule:
    direction: str = "both"
    threshold: float = 0.0
    operator: str = ">="
    minimum_observable: float | None = None

    def __post_init__(self) -> None:
        if self.direction not in {"long", "short", "both"}:
            raise ValueError("entry direction must be long, short, or both")
        if self.operator not in {">", ">=", "<", "<=", "abs>=", "abs>"}:
            raise ValueError("unsupported entry operator")
        _number(self.threshold, "entry threshold")
        if self.minimum_observable is not None:
            _number(self.minimum_observable, "entry minimum_observable")

    def matches(self, value: float | None, direction: str | None = None) -> bool:
        value = _finite(value)
        if value is None or (direction and self.direction not in {"both", direction}):
            return False
        target = self.threshold
        if self.operator == ">": return value > target
        if self.operator == ">=": return value >= target
        if self.operator == "<": return value < target
        if self.operator == "<=": return value <= target
        if self.operator == "abs>=": return abs(value) >= abs(target)
        return abs(value) > abs(target)

    def as_dict(self) -> dict:
        return {"direction": self.direction, "threshold": self.threshold,
                "operator": self.operator,
                "minimum_observable": self.minimum_observable}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EntryRule":
        return cls(direction=str(payload.get("direction", "both")),
                   threshold=float(payload.get("threshold", 0.0)),
                   operator=str(payload.get("operator", ">=")),
                   minimum_observable=(None if payload.get("minimum_observable") is None
                                        else float(payload["minimum_observable"])))


@dataclass(frozen=True)
class ExitProfile:
    mechanism: str = "reversion"
    kind: str = "observable_normalized"
    stop_first: bool = True
    normalization_threshold: float = 0.0
    max_horizon_bars: int = 96
    taker_fee_pct_per_side: float = 0.25
    funding_interval_hours: float = 8.0
    funding_rate_pct: float = 0.0

    def __post_init__(self) -> None:
        if self.mechanism != "reversion" or self.kind != "observable_normalized":
            raise ValueError("only bounded reversion observable_normalized exit is supported")
        if self.stop_first is not True:
            raise ValueError("observable_normalized exits are always stop-first")
        _number(self.normalization_threshold, "normalization_threshold", lo=0)
        if isinstance(self.max_horizon_bars, bool) or not 1 <= int(self.max_horizon_bars) <= MAX_WINDOW:
            raise ValueError("max_horizon_bars is outside its bound")
        _number(self.taker_fee_pct_per_side, "taker_fee_pct_per_side", lo=0, hi=10)
        _number(self.funding_interval_hours, "funding_interval_hours", lo=0.1, hi=168)
        _number(self.funding_rate_pct, "funding_rate_pct", lo=-100, hi=100)

    def as_dict(self) -> dict:
        return {"mechanism": self.mechanism, "kind": self.kind,
                "stop_first": self.stop_first,
                "normalization_threshold": self.normalization_threshold,
                "max_horizon_bars": int(self.max_horizon_bars),
                "taker_fee_pct_per_side": self.taker_fee_pct_per_side,
                "funding_interval_hours": self.funding_interval_hours,
                "funding_rate_pct": self.funding_rate_pct}

    @property
    def normalization_tolerance(self) -> float:
        return self.normalization_threshold

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExitProfile":
        return cls(**{key: payload[key] for key in cls.__dataclass_fields__ if key in payload})

    def resolve(self, observable_values: Sequence[float | None], *, stop_index: int | None = None) -> tuple[str, int | None]:
        """Resolve stop-first, normalize, then bounded timeout semantics."""
        if not observable_values:
            return "no_data", None
        for index, value in enumerate(observable_values[:int(self.max_horizon_bars)]):
            if stop_index is not None and index == int(stop_index):
                return "stop", index
            finite = _finite(value)
            if finite is not None and abs(finite) <= self.normalization_threshold:
                return "normalized", index
        return "timeout", min(len(observable_values), int(self.max_horizon_bars)) - 1


@dataclass(frozen=True)
class CounterfactualSpec:
    intervention: str = "fit_window_median"
    seed: int = 0
    fit_fraction: float = 0.7
    cluster_field: str = "event_ts_ms"

    def __post_init__(self) -> None:
        aliases = {"median": "fit_window_median", "permutation": "within_time_cluster_permutation"}
        if self.intervention in aliases:
            object.__setattr__(self, "intervention", aliases[self.intervention])
        if self.intervention not in {"fit_window_median", "within_time_cluster_permutation"}:
            raise ValueError("unsupported counterfactual intervention")
        if isinstance(self.seed, bool) or not 0 <= int(self.seed) <= 2**31 - 1:
            raise ValueError("counterfactual seed is outside its bound")
        if not 0.5 <= float(self.fit_fraction) < 1:
            raise ValueError("counterfactual fit_fraction must be in [0.5,1)")
        if not isinstance(self.cluster_field, str) or not self.cluster_field.strip():
            raise ValueError("counterfactual cluster_field is required")

    def as_dict(self) -> dict:
        return {"intervention": self.intervention, "seed": int(self.seed),
                "fit_fraction": float(self.fit_fraction),
                "cluster_field": self.cluster_field}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CounterfactualSpec":
        return cls(**{key: payload[key] for key in cls.__dataclass_fields__ if key in payload})


@dataclass(frozen=True)
class DiscoveryProgram:
    program_id: str
    observable: ObservableSpec
    causal_claim: CausalClaim
    entry_rule: EntryRule
    exit_profile: ExitProfile
    counterfactual: CounterfactualSpec
    direction: str = "both"
    proposal_fidelity: str = "event-plane.v1"

    def __post_init__(self) -> None:
        if not isinstance(self.program_id, str) or not self.program_id.strip():
            raise ValueError("program_id is required")
        if self.direction not in {"long", "short", "both"}:
            raise ValueError("program direction is invalid")
        if self.entry_rule.direction not in {"both", self.direction} and self.direction != "both":
            raise ValueError("entry direction conflicts with program direction")

    @property
    def identity(self) -> dict:
        return {"schema": SCHEMA, "program_id": self.program_id,
                "observable": self.observable.identity,
                "causal_claim": self.causal_claim.as_dict(),
                "entry_rule": self.entry_rule.as_dict(),
                "exit_profile": self.exit_profile.as_dict(),
                "counterfactual": self.counterfactual.as_dict(),
                "direction": self.direction,
                "proposal_fidelity": self.proposal_fidelity}

    @property
    def content_hash(self) -> str:
        return content_hash(self.identity)

    @property
    def hash(self) -> str:
        return self.content_hash

    def as_dict(self) -> dict:
        return self.identity | {"content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DiscoveryProgram":
        return cls(
            program_id=str(payload["program_id"]),
            observable=ObservableSpec.from_dict(payload["observable"]),
            causal_claim=CausalClaim.from_dict(payload["causal_claim"]),
            entry_rule=EntryRule.from_dict(payload["entry_rule"]),
            exit_profile=ExitProfile.from_dict(payload["exit_profile"]),
            counterfactual=CounterfactualSpec.from_dict(payload["counterfactual"]),
            direction=str(payload.get("direction", "both")),
            proposal_fidelity=str(payload.get("proposal_fidelity", "event-plane.v1")),
        )

    @property
    def canonical_hash(self) -> str:
        return self.content_hash


def _values_for(node: Mapping[str, Any], row: Mapping[str, Any], history: Sequence[Mapping[str, Any]] | None) -> float | None:
    op = node["op"]
    if op == "source":
        value = row.get(node["field"])
        if value is None and node["field"] == "imbalance_5bps":
            value = row.get("book_imbalance")
            if value is None:
                bid = _finite(row.get("book_bid_depth_usd", row.get("bid_depth_usd")))
                ask = _finite(row.get("book_ask_depth_usd", row.get("ask_depth_usd")))
                if bid is None or ask is None:
                    bid = _finite(row.get("bid_depth_5bps"))
                    ask = _finite(row.get("ask_depth_5bps"))
                if bid is not None and ask is not None and bid + ask > 0:
                    value = (bid - ask) / (bid + ask)
        return _finite(value)
    if op == "constant":
        return _finite(node["value"])
    if op == "book_imbalance":
        direct = _finite(row.get("book_imbalance"))
        if direct is not None:
            return direct
        bid = _finite(row.get("book_bid_depth_usd", row.get("bid_depth_usd")))
        ask = _finite(row.get("book_ask_depth_usd", row.get("ask_depth_usd")))
        if bid is None or ask is None or bid + ask <= 0:
            return None
        return (bid - ask) / (bid + ask)
    if op in {"negate", "abs"}:
        value = _values_for(node["arg"], row, history)
        return None if value is None else (-value if op == "negate" else abs(value))
    if op in {"difference", "ratio", "product"}:
        left = _values_for(node["args"][0], row, history)
        right = _values_for(node["args"][1], row, history)
        if left is None or right is None:
            return None
        if op == "difference": return left - right
        if op == "product": return left * right
        return None if right == 0 else left / right
    if op == "clip":
        value = _values_for(node["arg"], row, history)
        return None if value is None else min(float(node["hi"]), max(float(node["lo"]), value))
    values = []
    if history is not None:
        values = [value for item in history for value in [_values_for(node["arg"], item, None)] if value is not None]
    current = _values_for(node["arg"], row, None)
    if op == "lag":
        index = int(node["window"])
        if history is None or len(history) < index:
            return None
        return _values_for(node["arg"], history[-index], None)
    if op == "rolling_mean":
        window = int(node["window"])
        sample = (values + ([current] if current is not None else []))[-window:]
        return statistics.fmean(sample) if len(sample) == window else None
    if op == "rolling_std":
        window = int(node["window"])
        sample = (values + ([current] if current is not None else []))[-window:]
        return statistics.pstdev(sample) if len(sample) == window else None
    if op == "zscore":
        window = int(node["window"])
        sample = (values + ([current] if current is not None else []))[-window:]
        if len(sample) != window:
            return None
        mean = statistics.fmean(sample)
        std = statistics.pstdev(sample)
        return None if std == 0 else (sample[-1] - mean) / std
    raise ValueError(f"unknown op {op}")


def evaluate_expression(expression: Mapping[str, Any], row: Mapping[str, Any], history: Sequence[Mapping[str, Any]] | None = None) -> float | None:
    if not isinstance(row, Mapping):
        return None
    try:
        result = _values_for(expression, row, history)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError, statistics.StatisticsError):
        return None
    return _finite(result)


def resolve_episode(program: DiscoveryProgram, row: Mapping[str, Any],
                    observable_values: Sequence[float | None] | None = None) -> dict:
    """Resolve one episode with the pre-registered exit and exact costs.

    Discovery rows must carry the post-entry bar path and complete funding
    provenance.  A bare precomputed outcome is never scored here.  The same
    function is used by observed and intervention arms; intervention callers
    may only replace the observable values supplied to this resolver.
    """
    bars = row.get("bars", row.get("episode_bars"))
    if not isinstance(bars, (list, tuple)) or not bars:
        return {"status": "no_data", "net_pct": None,
                "reason": "episode bars are missing",
                "exit_semantics_hash": content_hash(program.exit_profile.as_dict())}
    provenance = row.get("funding_provenance")
    funding_status = (str(provenance.get("status")
                             or provenance.get("funding_status")
                             or row.get("funding_status") or "").lower()
                      if isinstance(provenance, Mapping) else "")
    # A legacy ``{"complete": true}`` marker remains accepted for manually
    # authored offline fixtures.  When a status is present it must prove a
    # realized settlement (or explicitly prove that none was due); forecast
    # rows and unknown statuses are never allowed to become zero-cost data.
    verified_status = not funding_status or funding_status in {
        "verified_realized", "verified_no_settlement_due", "realized",
        "settled", "complete",
    }
    if (funding_status == "verified_realized"
            and not provenance.get("event_ids")):
        verified_status = False
    funding_complete = bool(row.get("funding_complete") is True
                            and isinstance(provenance, Mapping)
                            and provenance.get("complete") is True
                            and verified_status)
    if not funding_complete:
        return {"status": "no_data", "net_pct": None,
                "reason": "funding provenance incomplete",
                "exit_semantics_hash": content_hash(program.exit_profile.as_dict())}
    raw_direction = row.get("direction")
    if raw_direction is None or str(raw_direction) not in {"long", "short"}:
        return {"status": "invalid", "net_pct": None,
                "reason": "episode direction evidence is missing",
                "exit_semantics_hash": content_hash(
                    program.exit_profile.as_dict())}
    direction = str(raw_direction)
    entry_price = _finite(row.get("entry_price"))
    stop_pct = _finite(row.get("stop_pct"))
    if direction not in {"long", "short"} or entry_price is None \
            or entry_price <= 0 or stop_pct is None or stop_pct <= 0:
        return {"status": "invalid", "net_pct": None,
                "reason": "episode entry/stop fields are incomplete",
                "exit_semantics_hash": content_hash(program.exit_profile.as_dict())}
    prior_ts = None
    parsed_bars = []
    for bar in bars:
        if not isinstance(bar, Mapping):
            return {"status": "invalid", "net_pct": None,
                    "reason": "episode bar is not a mapping"}
        ts = _finite(bar.get("ts", bar.get("event_ts_ms")))
        high, low, close = (_finite(bar.get(name))
                            for name in ("high", "low", "close"))
        close_ts = _finite(bar.get("close_ts"))
        if close_ts is None:
            # Production ``research.prices.Bar`` is a one-minute OHLC bar;
            # its close timestamp is always one minute after the open.
            close_ts = None if ts is None else ts + 60_000
        interval = _finite(row.get("bar_interval_ms"))
        if (ts is None or high is None or low is None or close is None
                or close_ts is None or close_ts <= ts or high < low
                or (interval is not None and interval != 60_000)
                or (prior_ts is not None and ts <= prior_ts)):
            return {"status": "invalid", "net_pct": None,
                    "reason": "episode bars are not chronological/complete"
                    if interval in (None, 60_000)
                    else "production outcomes require 1m bars"}
        prior_ts = ts
        parsed_bars.append((bar, int(ts), high, low, close, int(close_ts)))
    try:
        from research.outcomes import CostModel, TIMEFRAME_MS
        fees = _finite(row.get("taker_fee_pct_per_side"))
        funding_rate = _finite(row.get("funding_rate_pct"))
        funding_interval = _finite(row.get("funding_interval_hours"))
        if fees is None:
            fees = program.exit_profile.taker_fee_pct_per_side
        if funding_rate is None:
            funding_rate = program.exit_profile.funding_rate_pct
        if funding_interval is None:
            funding_interval = program.exit_profile.funding_interval_hours
        signal_timeframe = str(row.get("signal_timeframe") or "1m")
        signal_duration = TIMEFRAME_MS[signal_timeframe]
        first_bar_ts = parsed_bars[0][1]
        supplied_signal_ts = _finite(row.get("signal_ts"))
        signal_ts = (int(supplied_signal_ts) if supplied_signal_ts is not None
                     else first_bar_ts - signal_duration)
        supplied_entry_ts = _finite(row.get("entry_ts"))
        entry_ts = (int(supplied_entry_ts)
                    if supplied_entry_ts is not None
                    else (first_bar_ts if supplied_signal_ts is None else None))
        from research.outcomes import SetupPlan
        plan = SetupPlan(
            symbol=str(row.get("symbol") or "discovery"), direction=direction,
            entry_price=entry_price, stop_pct=stop_pct, take_pct=100.0,
            signal_ts=signal_ts, entry_ts=entry_ts,
            signal_timeframe=signal_timeframe,
            spread_pct=float(_finite(row.get("spread_pct")) or 0.0),
            funding_rate_pct=funding_rate,
            funding_interval_hours=funding_interval,
            taker_fee_pct_per_side=fees)
        costs = CostModel()
    except (ImportError, KeyError, TypeError, ValueError):
        return {"status": "invalid", "net_pct": None,
                "reason": "episode cost fields are invalid"}
    # Match the production no-lookahead floor exactly.  A pre-entry bar is a
    # data-integrity failure, not a row to silently discard or score.
    if any(ts < plan.first_visible_ts for _, ts, *_ in parsed_bars):
        return {"status": "invalid", "net_pct": None,
                "reason": "episode bar precedes first visible post-entry bar"}
    stop = plan.stop_price()
    path_supplied = observable_values is not None or row.get("observable_values") is not None
    values = list(observable_values if observable_values is not None
                  else row.get("observable_values") or ())
    result = "timeout"
    exit_price = parsed_bars[-1][4]
    exit_ts = parsed_bars[-1][5]
    deadline = plan.first_visible_ts + int(
        int(program.exit_profile.max_horizon_bars) * 60_000)
    expected_ts = plan.first_visible_ts + (
        (plan.first_visible_ts - (plan.signal_ts + signal_duration)
         + 60_000 - 1) // 60_000) * 60_000
    held = 0
    for index, (bar, ts, high, low, close, close_ts) in enumerate(parsed_bars):
        if ts >= deadline:
            break
        # This is the same continuous one-minute path invariant enforced by
        # research.outcomes.resolve; sparse bars cannot become a cheaper,
        # shorter completed trade.
        if ts != expected_ts:
            return {"status": "no_data", "net_pct": None,
                    "reason": "episode bars are not a complete 1m path",
                    "exit_semantics_hash": content_hash(
                        program.exit_profile.as_dict())}
        held += 1
        stop_hit = low <= stop if direction == "long" else high >= stop
        if stop_hit:
            result, exit_price, exit_ts = "stop", stop, close_ts
            break
        if path_supplied and index >= len(values):
            return {"status": "no_data", "net_pct": None,
                    "reason": "episode observable path is incomplete",
                    "exit_semantics_hash": content_hash(
                        program.exit_profile.as_dict())}
        value = values[index] if path_supplied else program.observable.evaluate(
            bar, [item[0] for item in parsed_bars[:index]])
        if path_supplied and value is None:
            return {"status": "no_data", "net_pct": None,
                    "reason": "episode observable is missing",
                    "exit_semantics_hash": content_hash(
                        program.exit_profile.as_dict())}
        if value is not None and abs(value) <= program.exit_profile.normalization_threshold:
            result, exit_price, exit_ts = "normalized", close, close_ts
            break
        expected_ts = ts + 60_000
    if held == 0:
        return {"status": "no_data", "net_pct": None,
                "reason": "no complete post-entry bars",
                "exit_semantics_hash": content_hash(
                    program.exit_profile.as_dict())}
    if result == "timeout":
        # Timeout exits at the last processed bar's close, exactly as the
        # production resolver does (never at its open timestamp).
        last_processed = parsed_bars[held - 1]
        exit_price, exit_ts = last_processed[4], last_processed[5]
    # Production funding is charged from bars held, not wall-clock timestamp
    # differences.  This matters when the first bar is offset or the funding
    # schedule is shorter than the usual 8-hour interval.
    hours = held / 60.0
    gross = ((exit_price - entry_price) if direction == "long"
             else (entry_price - exit_price)) / entry_price * 100.0
    realized = costs.realised(plan, result, hours)
    return {"status": result, "net_pct": round(gross - realized["total_pct"], 8),
            "gross_pct": round(gross, 8), "costs": realized,
            "exit_ts": exit_ts, "exit_price": exit_price,
            "bars_held": held, "hours_held": hours,
            "exit_semantics_hash": content_hash(program.exit_profile.as_dict()),
            "funding_provenance": provenance or {"complete": True}}


def seed_forced_flow_pressure() -> DiscoveryProgram:
    """The sole shipped seed; this is a hypothesis, never an edge claim."""
    observable = ObservableSpec(
        name="forced_flow_pressure.v1",
        expression={"op": "product", "args": [
            {"op": "abs", "arg": {"op": "source", "field": "oi_change_4h_pct"}},
            {"op": "product", "args": [
                {"op": "source", "field": "relative_volume_1h"},
                {"op": "abs", "arg": {"op": "source", "field": "imbalance_5bps"}},
            ]},
        ]})
    return DiscoveryProgram(
        program_id="forced_flow_pressure.v1",
        observable=observable,
        causal_claim=CausalClaim(
            payer="leveraged traders forced to reduce positions",
            mechanism="forced deleveraging/absorption can create temporary pressure",
            prediction="large OI change with unusual relative volume and depth imbalance should precede reversion",
            falsifier="pressure and reversion are unchanged from matched rows without the observable, or persist through the fixed horizon",
            confounders_or_limitations=("OI change also includes voluntary closes", "depth imbalance is observational and point-in-time", "no causal edge is asserted")),
        entry_rule=EntryRule(direction="both", threshold=0.0, operator="abs>"),
        exit_profile=ExitProfile(),
        counterfactual=CounterfactualSpec(),
    )


def _candidate_programs() -> tuple[DiscoveryProgram, ...]:
    seed = seed_forced_flow_pressure()
    common = {"causal_claim": seed.causal_claim,
              "entry_rule": seed.entry_rule,
              "exit_profile": seed.exit_profile,
              "counterfactual": seed.counterfactual}
    oi = {"op": "abs", "arg": {"op": "source", "field": "oi_change_4h_pct"}}
    volume = {"op": "source", "field": "relative_volume_1h"}
    depth = {"op": "abs", "arg": {"op": "book_imbalance"}}
    return (seed,
            DiscoveryProgram(
                program_id="forced_flow_pressure.oi_volume.v1",
                observable=ObservableSpec("forced_flow_pressure.oi_volume.v1",
                                           {"op": "product", "args": [oi, volume]}),
                **common),
            DiscoveryProgram(
                program_id="forced_flow_pressure.volume_depth.v1",
                observable=ObservableSpec("forced_flow_pressure.volume_depth.v1",
                                           {"op": "product", "args": [volume, depth]}),
                **common))


def program_identity_hash(program: DiscoveryProgram,
                         contract_identity: Mapping[str, Any] | None = None) -> str:
    return content_hash({"program_hash": program.content_hash,
                         "contract_identity": dict(contract_identity or {})})


def choose_program(previous_hashes: Sequence[str] = (),
                   contract_identity: Mapping[str, Any] | None = None) -> DiscoveryProgram | None:
    """Select one deterministic, genuinely distinct bounded candidate."""
    seen = {str(value) for value in previous_hashes}
    for candidate in _candidate_programs():
        identity = program_identity_hash(candidate, contract_identity)
        if identity not in seen and (contract_identity is not None
                                     or candidate.content_hash not in seen):
            return candidate
    return None


def _expr_source(expr: Mapping[str, Any]) -> str:
    op = expr["op"]
    if op == "source": return f"_finite(row.get({expr['field']!r}))"
    if op == "constant": return repr(float(expr["value"]))
    if op == "book_imbalance": return "_book_imbalance(row)"
    if op == "negate": return f"_unary(-1.0, {_expr_source(expr['arg'])})"
    if op == "abs": return f"_abs({_expr_source(expr['arg'])})"
    if op == "difference": return f"_binary('-', {_expr_source(expr['args'][0])}, {_expr_source(expr['args'][1])})"
    if op == "product": return f"_binary('*', {_expr_source(expr['args'][0])}, {_expr_source(expr['args'][1])})"
    if op == "ratio":
        left, right = _expr_source(expr["args"][0]), _expr_source(expr["args"][1])
        return f"(None if ({right}) in (None, 0) else _finite(({left}) / ({right})))"
    if op == "clip": return f"_clip({_expr_source(expr['arg'])}, {expr['lo']!r}, {expr['hi']!r})"
    # History-aware operations receive a bounded immutable history list on the
    # row under ``_history``; there is no module/global state.
    arg = _expr_source(expr["arg"])
    return f"_window({op!r}, {arg}, row.get('_history'), {int(expr['window'])})"


def generate_source(program: DiscoveryProgram) -> str:
    """Generate a static evaluator; the expression is data, not executable code."""
    expression = canonical_json(_thaw(program.observable.expression))
    return f'''# Generated by research.discovery; research-only, deterministic.
import math

_EXPRESSION = {expression}

def _finite(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None

def _eval(node, row, history):
    op = node.get("op")
    if op == "source":
        field = node.get("field")
        value = row.get(field)
        if value is None and field == "imbalance_5bps":
            value = row.get("book_imbalance")
            if value is None:
                bid = _finite(row.get("book_bid_depth_usd", row.get("bid_depth_usd")))
                ask = _finite(row.get("book_ask_depth_usd", row.get("ask_depth_usd")))
                if bid is None or ask is None:
                    bid = _finite(row.get("bid_depth_5bps"))
                    ask = _finite(row.get("ask_depth_5bps"))
                if bid is not None and ask is not None and bid + ask > 0:
                    value = (bid - ask) / (bid + ask)
        return _finite(value)
    if op == "constant":
        return _finite(node.get("value"))
    if op == "book_imbalance":
        direct = _finite(row.get("book_imbalance"))
        if direct is not None: return direct
        bid = _finite(row.get("book_bid_depth_usd", row.get("bid_depth_usd")))
        ask = _finite(row.get("book_ask_depth_usd", row.get("ask_depth_usd")))
        return None if bid is None or ask is None or bid + ask <= 0 else _finite((bid - ask) / (bid + ask))
    if op in ("negate", "abs"):
        value = _eval(node.get("arg"), row, history)
        return None if value is None else _finite(-value if op == "negate" else abs(value))
    if op in ("difference", "ratio", "product"):
        left = _eval(node.get("args")[0], row, history)
        right = _eval(node.get("args")[1], row, history)
        if left is None or right is None: return None
        if op == "difference": return _finite(left - right)
        if op == "product": return _finite(left * right)
        return None if right == 0 else _finite(left / right)
    if op == "clip":
        value = _eval(node.get("arg"), row, history)
        return None if value is None else _finite(min(node.get("hi"), max(node.get("lo"), value)))
    window = int(node.get("window", 1))
    if not isinstance(history, (list, tuple)):
        return None
    if op == "lag":
        if len(history) < window: return None
        return _eval(node.get("arg"), history[-window], history[:-window])
    values = []
    if len(history) < max(0, window - 1): return None
    for item in history[-max(0, window - 1):]:
        value = _eval(node.get("arg"), item, history[:-1])
        if value is None: return None
        values.append(value)
    current = _eval(node.get("arg"), row, history)
    if current is None or len(values) + 1 < window: return None
    sample = values + [current]
    mean = sum(sample) / window
    if op == "rolling_mean": return _finite(mean)
    std = (sum((value - mean) ** 2 for value in sample) / window) ** 0.5
    if op == "rolling_std": return _finite(std)
    return None if std == 0 else _finite((current - mean) / std)

def evaluate(row):
    if not isinstance(row, dict): return None
    return _finite(_eval(_EXPRESSION, row, row.get("_history")))
'''


_ALLOWED_AST = {
    ast.Module, ast.Import, ast.alias, ast.FunctionDef, ast.arguments,
    ast.arg, ast.Return, ast.Assign, ast.Expr, ast.If, ast.For, ast.Try,
    ast.ExceptHandler, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp,
    ast.Call, ast.Name, ast.Load, ast.Store, ast.Constant, ast.Dict, ast.List,
    ast.Tuple, ast.Subscript, ast.Attribute, ast.Index, ast.Slice, ast.And,
    ast.Or, ast.Not, ast.USub, ast.Add, ast.Sub, ast.Mult, ast.Div,
    ast.Pow, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is,
    ast.IsNot, ast.In, ast.keyword, ast.comprehension, ast.ListComp,
    ast.GeneratorExp,
}


def validate_generated_source(source: str) -> ast.Module:
    tree = ast.parse(source, mode="exec")
    allowed_calls = {"_finite", "_eval", "evaluate", "float", "int", "len",
                     "max", "min", "sum", "isinstance", "range", "abs"}
    allowed_attributes = {"get", "isfinite", "append"}
    for node in ast.walk(tree):
        if type(node) not in _ALLOWED_AST:
            raise ValueError(f"generated artifact AST node is not allowed: {type(node).__name__}")
        if isinstance(node, ast.Import):
            if any(alias.name != "math" for alias in node.names):
                raise ValueError("generated artifact may import only math")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError("dunder attributes are not allowed")
        if isinstance(node, ast.Attribute) and node.attr not in allowed_attributes:
            raise ValueError(f"generated artifact attribute is not allowed: {node.attr}")
        if isinstance(node, ast.Call):
            target = node.func.id if isinstance(node.func, ast.Name) else None
            if target is None and isinstance(node.func, ast.Attribute):
                target = node.func.attr
            if target not in allowed_calls and target not in allowed_attributes:
                raise ValueError(f"generated artifact call is not allowed: {target}")
        if isinstance(node, ast.Name) and node.id in {
                "open", "eval", "exec", "compile", "__import__", "os",
                "subprocess", "socket", "pathlib", "importlib"}:
            raise ValueError(f"generated artifact name is not allowed: {node.id}")
    names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    if "evaluate" not in names:
        raise ValueError("generated artifact must define evaluate(row)")
    return tree


@dataclass(frozen=True)
class DiscoveryArtifact:
    artifact_hash: str
    path: str
    manifest: Mapping[str, Any]


def write_artifact(program: DiscoveryProgram,
                   root: str | Path = "research/results/discovery-artifacts",
                   test_vectors: Sequence[Mapping[str, Any]] | None = None,
                   contract_identity: Mapping[str, Any] | None = None,
                   evidence_context: Mapping[str, Any] | None = None) -> DiscoveryArtifact:
    if (not isinstance(contract_identity, Mapping)
            or not contract_identity.get("semantic_hash")
            or not all(contract_identity.get(key)
                       for key in ("strategy_id", "strategy_version", "variant_id"))):
        raise ValueError("canonical contract identity is required for discovery artifacts")
    vectors = list(test_vectors or [])
    source = generate_source(program)
    validate_generated_source(source)
    expected = [{"row": _safe(dict(vector)), "value": program.observable.evaluate(vector)} for vector in vectors]
    manifest = {"schema": ARTIFACT_SCHEMA, "program": program.as_dict(),
                "contract_identity": dict(contract_identity or {}),
                "source_hash": content_hash(source), "test_vectors": expected}
    if evidence_context is not None:
        if not isinstance(evidence_context, Mapping):
            raise ValueError("evidence_context must be a mapping")
        manifest["evidence_context"] = _safe(dict(evidence_context))
    digest = content_hash({"manifest": manifest, "source": source})
    destination = Path(root) / digest
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "evaluate.py").write_text(source, encoding="utf-8")
    (destination / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    return DiscoveryArtifact(digest, str(destination), MappingProxyType(manifest))


def verify_artifact(artifact: DiscoveryArtifact | str | Path,
                    contract_identity: Mapping[str, Any] | None = None,
                    evidence_context: Mapping[str, Any] | None = None) -> dict:
    path = Path(artifact.path if isinstance(artifact, DiscoveryArtifact) else artifact)
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != ARTIFACT_SCHEMA:
        raise ValueError("discovery artifact schema mismatch")
    recorded_contract = manifest.get("contract_identity")
    if not isinstance(recorded_contract, Mapping) or not recorded_contract.get("semantic_hash"):
        raise ValueError("discovery artifact canonical contract identity is missing")
    if (not isinstance(recorded_contract.get("semantic_hash"), str)
            or len(recorded_contract["semantic_hash"]) != 64
            or any(char not in "0123456789abcdef"
                   for char in recorded_contract["semantic_hash"])):
        raise ValueError("discovery artifact contract semantic hash is invalid")
    source = (path / "evaluate.py").read_text(encoding="utf-8")
    validate_generated_source(source)
    program_payload = manifest.get("program")
    if not isinstance(program_payload, Mapping):
        raise ValueError("discovery artifact program manifest is missing")
    program = DiscoveryProgram.from_dict(program_payload)
    if program.content_hash != program_payload.get("content_hash"):
        raise ValueError("discovery artifact program identity mismatch")
    if contract_identity is None:
        raise ValueError("expected canonical contract identity is required")
    if canonical_json(recorded_contract) != canonical_json(dict(contract_identity)):
        raise ValueError("discovery artifact contract identity drift")
    if evidence_context is not None:
        recorded_evidence = manifest.get("evidence_context")
        if (not isinstance(recorded_evidence, Mapping)
                or canonical_json(recorded_evidence)
                != canonical_json(dict(evidence_context))):
            raise ValueError("discovery artifact event evidence drift")
    canonical_source = generate_source(program)
    if source != canonical_source:
        raise ValueError("discovery artifact source does not match typed IR")
    if content_hash(source) != manifest.get("source_hash"):
        raise ValueError("discovery artifact source hash mismatch")
    digest = content_hash({"manifest": manifest, "source": source})
    if path.name != digest:
        raise ValueError("discovery artifact content hash mismatch")
    return manifest


def load_artifact(artifact: DiscoveryArtifact | str | Path,
                  contract_identity: Mapping[str, Any],
                  evidence_context: Mapping[str, Any] | None = None) -> Callable[[Mapping[str, Any]], float | None]:
    path = Path(artifact.path if isinstance(artifact, DiscoveryArtifact) else artifact)
    verify_artifact(path, contract_identity, evidence_context)
    source = (path / "evaluate.py").read_text(encoding="utf-8")
    module = types.ModuleType("research_discovery_artifact")
    def _import(name, globals=None, locals=None, fromlist=(), level=0):
        if name != "math":
            raise ImportError("discovery artifacts may import only math")
        return __import__(name, globals, locals, fromlist, level)
    builtins = __builtins__ if isinstance(__builtins__, dict) else __builtins__.__dict__
    safe_builtins = {name: builtins[name] for name in ("abs", "bool", "float", "int", "len", "max", "min", "isinstance", "sum", "dict", "tuple", "list", "range", "TypeError", "ValueError", "OverflowError") if name in builtins}
    safe_builtins["__import__"] = _import
    module.__dict__.update({"__builtins__": safe_builtins})
    exec(compile(source, str(path / "evaluate.py"), "exec"), module.__dict__)  # isolated research loader
    return module.__dict__["evaluate"]


def _flatten_event_row(row: Mapping[str, Any]) -> dict:
    """Flatten one event-plane row without accepting future/private state."""
    result = {str(key): value for key, value in row.items()
              if str(key) in _SOURCE_FIELDS
              or str(key) in _ROW_METADATA_FIELDS
              or str(key) in _EVENT_PROVENANCE_FIELDS}
    raw = row.get("payload_json")
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            payload = {}
        if isinstance(payload, Mapping):
            for key, value in payload.items():
                if str(key) in _SOURCE_FIELDS or str(key) in _ROW_METADATA_FIELDS:
                    result[str(key)] = value
    return result


def _event_reference(row: Mapping[str, Any], role: str) -> dict[str, Any]:
    """Return the immutable identity/provenance needed to replay one source."""
    reference = {"role": str(role)}
    for field_name in sorted(_EVENT_PROVENANCE_FIELDS):
        if field_name in row:
            reference[field_name] = row.get(field_name)
    # A source event without an ID is still represented deterministically so
    # offline callers receive an explicit missing-evidence digest instead of
    # silently losing the join binding.
    reference.setdefault("event_id", None)
    reference.setdefault("revision_id", None)
    reference.setdefault("payload_hash", None)
    return reference


def _event_join_context(source_rows: Sequence[tuple[str, Mapping[str, Any]]]) -> dict:
    """Build a canonical per-row event evidence context and digest."""
    sources = [_event_reference(row, role) for role, row in source_rows]
    digest_payload = [{
        "role": source.get("role"),
        "event_id": source.get("event_id"),
        "revision_id": source.get("revision_id"),
        "payload_hash": source.get("payload_hash"),
    } for source in sources]
    return {
        "event_sources": sources,
        "event_join_digest": content_hash(digest_payload),
        "source_event_ids": [source.get("event_id") for source in sources],
        "source_snapshot_ids": sorted({str(source["snapshot_id"])
                                         for source in sources
                                         if source.get("snapshot_id") is not None}),
        "source_feeds": sorted({str(source["feed"]) for source in sources
                                 if source.get("feed") is not None}),
        "source_schemas": sorted({str(source["schema"]) for source in sources
                                   if source.get("schema") is not None}),
    }


def _as_bool(value: Any) -> bool | None:
    """Parse recorder boolean fields without treating arbitrary strings as true."""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "closed", "complete"}:
        return True
    if text in {"0", "false", "no", "n", "open", "incomplete"}:
        return False
    return None


def _is_closed_execution_bar(row: Mapping[str, Any]) -> bool:
    """Return whether a recorder bar is a closed, usable one-minute candle.

    The execution-bar recorder writes ``closed`` (and may also write the
    exchange ``confirm`` flag).  An explicit open/false marker is never
    guessed closed.  Omitted markers
    are also rejected for EventPlane data: legacy CSVs cannot prove that a
    candle was closed.  Offline callers can still construct episode bars
    directly for ``resolve_episode`` where no recorder claim is made.
    """
    for field in ("closed", "confirm"):
        value = _as_bool(row.get(field))
        if value is False:
            return False
        if value is True:
            return True
    status = str(row.get("bar_status") or row.get("status") or "").lower()
    if status in {"open", "forming", "incomplete"}:
        return False
    return False


def _funding_rate_percent(row: Mapping[str, Any]) -> float | None:
    """Read a realized funding rate and normalize exchange fractions to pct."""
    value = row.get("funding_rate_pct")
    if value is None:
        value = row.get("realized_rate", row.get("funding_rate"))
    result = _finite(value)
    if result is None:
        return None
    # OKX's funding endpoints return a decimal fraction (0.0002 = 0.02%).
    # Discovery's CostModel intentionally stores rates as percentage points.
    if abs(result) <= 1.0 and row.get("funding_rate_pct") is None:
        result *= 100.0
    return result


def _funding_provenance(
        events: Sequence[Mapping[str, Any]], *, first_ts: int,
        last_ts: int, interval_hours: float) -> tuple[bool, dict, float | None]:
    """Verify realized funding coverage for one bounded episode.

    Forecast rows are never evidence of a settled charge.  A short episode
    with no settlement due is explicitly verified instead of silently
    converted to a zero-cost sample.  For a longer episode every expected
    funding interval must have a realized event in the event plane.
    """
    realized = []
    for event in events:
        status = str(event.get("status") or "").lower()
        rate = _funding_rate_percent(event)
        settlement = _finite(event.get("settlement_time", event.get("event_ts_ms")))
        if status not in {"realized", "verified_realized", "settled"}:
            continue
        if rate is None or settlement is None:
            continue
        if first_ts < settlement <= last_ts:
            realized.append((int(settlement), rate, event))
    realized.sort(key=lambda item: (item[0], str(item[2].get("event_id") or "")))
    # A recorder may retain a corrected revision for the same settlement.  It
    # proves one settlement, not two intervals; keep the deterministic first
    # revision while retaining its event identity in the audit context.
    unique_realized: list[tuple[int, float, Mapping[str, Any]]] = []
    seen_settlements: set[int] = set()
    for item in realized:
        if item[0] in seen_settlements:
            continue
        seen_settlements.add(item[0])
        unique_realized.append(item)
    realized = unique_realized
    interval_ms = max(1, int(float(interval_hours) * 3_600_000))
    duration = max(0, int(last_ts) - int(first_ts))
    # Funding is charged by completed holding intervals.  The first interval
    # does not settle until its boundary, so a path shorter than one interval
    # is complete with an explicit no-settlement proof.
    expected = duration // interval_ms
    if expected <= 0:
        provenance = {
            "complete": True, "status": "verified_no_settlement_due",
            "event_ids": [], "settlements": [],
            "covered_from_ts": int(first_ts), "covered_to_ts": int(last_ts),
        }
        return True, provenance, None
    if len(realized) < expected:
        provenance = {
            "complete": False, "status": "funding_incomplete",
            "event_ids": [item[2].get("event_id") for item in realized],
            "settlements": [item[0] for item in realized],
            "expected_settlements": int(expected),
            "covered_from_ts": int(first_ts), "covered_to_ts": int(last_ts),
        }
        return False, provenance, None
    selected = realized[:expected]
    provenance = {
        "complete": True, "status": "verified_realized",
        "event_ids": [item[2].get("event_id") for item in selected],
        "settlements": [item[0] for item in selected],
        "expected_settlements": int(expected),
        "covered_from_ts": int(first_ts), "covered_to_ts": int(last_ts),
        "rates_pct": [item[1] for item in selected],
    }
    # CostModel takes a percentage rate and applies it per completed interval.
    # The mean keeps that contract deterministic when an episode spans more
    # than one settlement while retaining every exact rate in provenance.
    return True, provenance, sum(item[1] for item in selected) / len(selected)


def _aggregate_event_context(rows: Sequence[Mapping[str, Any]]) -> dict:
    """Aggregate exact source context for a discovery result/artifact."""
    row_digests = [row.get("event_join_digest") for row in rows
                   if row.get("event_join_digest")]
    source_snapshots = sorted({str(value)
                               for row in rows
                               for value in (row.get("source_snapshot_ids") or [])
                               if value is not None})
    source_feeds = sorted({str(value)
                           for row in rows
                           for value in (row.get("source_feeds") or [])
                           if value is not None})
    source_schemas = sorted({str(value)
                             for row in rows
                             for value in (row.get("source_schemas") or [])
                             if value is not None})
    return {
        "event_join_digest": content_hash(row_digests),
        "source_snapshot_ids": source_snapshots,
        "source_feeds": source_feeds,
        "source_schemas": source_schemas,
        "joined_rows": len(row_digests),
    }


def load_exact_asof_rows(event_plane: Any, *, series: str = "snapshot",
                         instrument: str | None = None,
                         decision_time_ms: int,
                         cutoff_event_ts_ms: int | None = None,
                         limit: int = MAX_ROWS) -> list[dict]:
    """Read only event-plane rows available at the decision timestamp."""
    rows = event_plane.query(series=series, instrument=instrument,
                             decision_time_ms=decision_time_ms,
                             cutoff_event_ts_ms=cutoff_event_ts_ms,
                             limit=min(int(limit), MAX_ROWS, 10_000))
    flattened = [_flatten_event_row(row) for row in rows]
    return sorted(flattened, key=lambda row: (
        int(_finite(row.get("event_ts_ms")) or 0),
        int(_finite(row.get("available_at_ms")) or 0),
        int(_finite(row.get("observed_at_ms")) or 0),
        str(row.get("event_id") or "")))


def load_discovery_rows(event_plane: Any, *, decision_time_ms: int,
                        instrument: str | None = None,
                        cutoff_event_ts_ms: int | None = None,
                        limit: int = 1_000) -> list[dict]:
    """Join actual recorder series into exact-as-of observable rows.

    The recorder writes ``order_book``, ``open_interest`` and
    ``taker_volume`` series.  No synthetic ``snapshot`` series or file-time
    availability is assumed; every input is selected by EventPlane's strict
    availability/event cutoffs.
    """
    book = load_exact_asof_rows(event_plane, series="order_book",
                                instrument=instrument,
                                decision_time_ms=decision_time_ms,
                                cutoff_event_ts_ms=cutoff_event_ts_ms,
                                limit=limit)
    if not book:
        return []
    oi = load_exact_asof_rows(event_plane, series="open_interest",
                              instrument=instrument,
                              decision_time_ms=decision_time_ms,
                              cutoff_event_ts_ms=cutoff_event_ts_ms,
                              limit=limit)
    currency = (str(instrument).split("-")[0] if instrument else None)
    taker = load_exact_asof_rows(event_plane, series="taker_volume",
                                 instrument=currency,
                                 decision_time_ms=decision_time_ms,
                                 cutoff_event_ts_ms=cutoff_event_ts_ms,
                                 limit=limit)
    oi = sorted(oi, key=lambda row: int(_finite(row.get("event_ts_ms")) or 0))
    taker = sorted(taker, key=lambda row: int(_finite(row.get("event_ts_ms")) or 0))
    result = []
    for current in sorted(book, key=lambda row: int(_finite(row.get("event_ts_ms")) or 0)):
        ts = int(_finite(current.get("event_ts_ms")) or 0)
        book_available = _finite(current.get("available_at_ms"))
        if book_available is None:
            continue
        current_instrument = current.get("inst_id") or instrument
        oi_prior = [row for row in oi
                    if (current_instrument is None
                        or row.get("inst_id") in (None, current_instrument))
                    and _finite(row.get("available_at_ms")) is not None
                    and _finite(row.get("available_at_ms")) <= book_available
                    and int(_finite(row.get("event_ts_ms")) or 0) <= ts]
        current_currency = (str(current_instrument).split("-")[0]
                            if current_instrument else currency)
        taker_prior = [row for row in taker
                       if (current_currency is None
                           or row.get("ccy") in (None, current_currency))
                       and _finite(row.get("available_at_ms")) is not None
                       and _finite(row.get("available_at_ms")) <= book_available
                       and int(_finite(row.get("event_ts_ms")) or 0) <= ts]
        if not oi_prior or not taker_prior:
            continue
        oi_current = _finite(oi_prior[-1].get("oi_usd"))
        target_ts = ts - 4 * 3_600_000
        prior_candidates = [row for row in oi_prior
                            if int(_finite(row.get("event_ts_ms")) or 0) <= target_ts]
        if not prior_candidates:
            continue
        prior_row = prior_candidates[-1]
        prior_ts = int(_finite(prior_row.get("event_ts_ms")) or 0)
        if target_ts - prior_ts > 90 * 60_000:
            continue
        oi_previous = _finite(prior_row.get("oi_usd"))
        buy = _finite(taker_prior[-1].get("buy_vol"))
        sell = _finite(taker_prior[-1].get("sell_vol"))
        prior_volumes = []
        for row in taker_prior[:-1][-6:]:
            b, s = _finite(row.get("buy_vol")), _finite(row.get("sell_vol"))
            if b is not None and s is not None:
                prior_volumes.append(b + s)
        current_volume = None if buy is None or sell is None else buy + sell
        normal_volume = (sum(prior_volumes) / len(prior_volumes)
                         if prior_volumes else None)
        if oi_current is None or oi_previous in (None, 0) \
                or current_volume is None or normal_volume in (None, 0):
            continue
        source_rows: list[tuple[str, Mapping[str, Any]]] = [
            ("order_book", current),
            ("open_interest_current", oi_prior[-1]),
            ("open_interest_baseline", prior_row),
            ("taker_volume_current", taker_prior[-1]),
        ]
        source_rows.extend(
            (f"taker_volume_baseline_{index}", row)
            for index, row in enumerate(taker_prior[:-1][-6:]))
        event_context = _event_join_context(source_rows)
        row = {**current,
               "oi_change_4h_pct": (oi_current / oi_previous - 1.0) * 100.0,
               "relative_volume_1h": current_volume / normal_volume,
               "imbalance_5bps": current.get("imbalance_5bps"),
               "buy_vol": buy, "sell_vol": sell,
               "taker_imbalance": ((buy - sell) / current_volume
                                   if current_volume else None),
               # The reversion side is evidence-derived: aggressive selling
               # creates a long fade, while aggressive buying creates a short
               # fade.  Ties are retained as unusable rows, never guessed.
               "direction": ("short" if buy > sell else
                             "long" if sell > buy else None)}
        row.update(event_context)
        if _finite(row.get("imbalance_5bps")) is None:
            continue
        result.append(row)
    return result[:int(limit)]


def load_discovery_episodes(
        event_plane: Any, *, decision_time_ms: int,
        signal_rows: Sequence[Mapping[str, Any]] | None = None,
        rows: Sequence[Mapping[str, Any]] | None = None,
        instrument: str | None = None,
        cutoff_event_ts_ms: int | None = None,
        episode_decision_time_ms: int | None = None,
        episode_cutoff_event_ts_ms: int | None = None,
        limit: int = 1_000,
        max_horizon_bars: int | None = None,
        stop_pct: float = 1.0,
        bar_series: str = "execution_bar_1m",
        funding_series: str = "funding",
        funding_interval_hours: float | None = None,
        program: DiscoveryProgram | None = None) -> list[dict]:
    """Assemble exact-as-of signal rows into bounded evaluation episodes.

    Signal features are selected at ``decision_time_ms`` and the supplied
    event cutoff.  Outcome bars/funding are selected independently at the
    (usually later) episode decision time, so the feature row cannot see its
    own future.  Bars must be closed, one-minute, and contiguous from the
    first visible post-entry timestamp; sparse paths remain explicit rows with
    ``episode_status=missing_bars`` and are never silently shortened.

    The recorder labels these bars ``execution_bar_1m``.  The loader accepts a
    caller-supplied series for offline fixtures, but never fetches a network
    source or manufactures prices.  Funding is accepted only from realized
    event rows, or as an explicit ``verified_no_settlement_due`` proof for a
    path shorter than one funding interval.
    """
    if isinstance(limit, bool) or not 1 <= int(limit) <= MAX_ROWS:
        raise ValueError("limit is outside discovery bounds")
    program = program or seed_forced_flow_pressure()
    profile = program.exit_profile
    horizon = int(max_horizon_bars or profile.max_horizon_bars)
    if isinstance(horizon, bool) or not 1 <= horizon <= MAX_WINDOW:
        raise ValueError("max_horizon_bars is outside discovery bounds")
    if isinstance(stop_pct, bool) or _finite(stop_pct) is None or float(stop_pct) <= 0:
        raise ValueError("stop_pct must be positive and finite")
    interval_hours = (_finite(funding_interval_hours)
                      if funding_interval_hours is not None
                      else float(profile.funding_interval_hours))
    if interval_hours is None or interval_hours <= 0:
        raise ValueError("funding_interval_hours must be positive")

    if signal_rows is not None and rows is not None:
        raise ValueError("provide only one of signal_rows or rows")
    signals = (list(signal_rows if signal_rows is not None else rows)[:int(limit)]
               if signal_rows is not None or rows is not None else
               load_discovery_rows(
                   event_plane, instrument=instrument,
                   decision_time_ms=decision_time_ms,
                   cutoff_event_ts_ms=cutoff_event_ts_ms, limit=int(limit)))
    if not signals:
        return []
    if signal_rows is not None or rows is not None:
        signal_cutoff = (cutoff_event_ts_ms if cutoff_event_ts_ms is not None
                         else decision_time_ms)
        for signal in signals:
            available = _finite(signal.get("available_at_ms"))
            event_ts = _finite(signal.get("event_ts_ms", signal.get("signal_ts")))
            if available is not None and available > int(decision_time_ms):
                raise ValueError("signal row is unavailable at decision time")
            if event_ts is not None and event_ts > int(signal_cutoff):
                raise ValueError("signal row exceeds point-in-time cutoff")
    episode_decision = (int(episode_decision_time_ms)
                        if episode_decision_time_ms is not None
                        else int(decision_time_ms))
    episode_cutoff = (episode_cutoff_event_ts_ms
                      if episode_cutoff_event_ts_ms is not None
                      else episode_decision)
    # Keep the query bounded even when many signals share one instrument.  A
    # full horizon is needed to distinguish a terminal timeout from missing
    # bars; MAX_QUERY_ROWS is enforced by EventPlane itself.
    query_limit = min(10_000, max(1, int(limit) * horizon * 2))
    bar_events = event_plane.query(
        series=str(bar_series), instrument=instrument,
        decision_time_ms=episode_decision,
        cutoff_event_ts_ms=episode_cutoff, limit=query_limit)
    if not bar_events and instrument:
        bar_events = event_plane.query(
            series=str(bar_series), instrument=str(instrument).split("-")[0],
            decision_time_ms=episode_decision,
            cutoff_event_ts_ms=episode_cutoff, limit=query_limit)
    funding_events = event_plane.query(
        series=str(funding_series), instrument=instrument,
        decision_time_ms=episode_decision,
        cutoff_event_ts_ms=episode_cutoff,
        limit=min(10_000, max(1, int(limit) * horizon)))
    if not funding_events and instrument:
        # A few legacy recorder exports identify funding by currency rather
        # than swap instrument.  This fallback remains exact-as-of and is
        # only used when the instrument-keyed query has no evidence.
        funding_events = event_plane.query(
            series=str(funding_series),
            instrument=str(instrument).split("-")[0],
            decision_time_ms=episode_decision,
            cutoff_event_ts_ms=episode_cutoff,
            limit=min(10_000, max(1, int(limit) * horizon)))
    # Outcome normalization is evaluated on the same persisted composite
    # observations as the signal, never on OHLC fields that do not contain
    # OI/volume/book pressure.  One bounded later join serves every episode;
    # per-bar database queries would both be slow and make provenance harder
    # to audit.
    later_composites = load_discovery_rows(
        event_plane, instrument=instrument,
        decision_time_ms=episode_decision,
        cutoff_event_ts_ms=episode_cutoff,
        limit=min(int(limit) * horizon, 10_000))
    composites_by_instrument: dict[str | None, list[dict]] = {}
    for composite in later_composites:
        key = composite.get("inst_id") or instrument
        composites_by_instrument.setdefault(key, []).append(dict(composite))
    for values in composites_by_instrument.values():
        values.sort(key=lambda item: (
            int(_finite(item.get("event_ts_ms")) or 0),
            int(_finite(item.get("available_at_ms")) or 0),
            str(item.get("event_join_digest") or "")))
        latest: dict[int, dict] = {}
        for value in values:
            ts = int(_finite(value.get("event_ts_ms")) or 0)
            prior = latest.get(ts)
            if prior is None or (
                    int(_finite(value.get("available_at_ms")) or 0),
                    str(value.get("event_join_digest") or "")) > (
                    int(_finite(prior.get("available_at_ms")) or 0),
                    str(prior.get("event_join_digest") or "")):
                latest[ts] = value
        values[:] = [latest[ts] for ts in sorted(latest)]
        history: list[Mapping[str, Any]] = []
        for composite in values:
            composite["__observable_value__"] = program.observable.evaluate(
                composite, history)
            history.append(composite)
    bars_by_instrument: dict[str | None, list[dict]] = {}
    for event in bar_events:
        flattened = _flatten_event_row(event)
        if not _is_closed_execution_bar(flattened):
            continue
        key = event.get("instrument") or flattened.get("inst_id") or instrument
        bars_by_instrument.setdefault(key, []).append({
            **flattened, "event_ts_ms": event.get("event_ts_ms"),
            "available_at_ms": event.get("available_at_ms"),
            "event_id": event.get("event_id"),
        })
    for values in bars_by_instrument.values():
        values.sort(key=lambda item: (int(_finite(item.get("event_ts_ms")) or 0),
                                      int(_finite(item.get("available_at_ms")) or 0),
                                      str(item.get("event_id") or "")))
    # EventPlane intentionally preserves revisions.  For a replay path choose
    # the latest available revision at each bar timestamp, rather than
    # silently taking the first historical payload and skipping its revision.
    for key, values in list(bars_by_instrument.items()):
        by_ts: dict[int, dict] = {}
        for value in values:
            ts = int(_finite(value.get("event_ts_ms")) or 0)
            prior = by_ts.get(ts)
            if prior is None or (
                    int(_finite(value.get("available_at_ms")) or 0),
                    str(value.get("event_id") or "")) > (
                    int(_finite(prior.get("available_at_ms")) or 0),
                    str(prior.get("event_id") or "")):
                by_ts[ts] = value
        bars_by_instrument[key] = [by_ts[ts] for ts in sorted(by_ts)]
    funding_by_instrument: dict[str | None, list[dict]] = {}
    for event in funding_events:
        flattened = _flatten_event_row(event)
        key = event.get("instrument") or flattened.get("inst_id") or instrument
        funding_by_instrument.setdefault(key, []).append({
            **flattened, "event_ts_ms": event.get("event_ts_ms"),
            "available_at_ms": event.get("available_at_ms"),
            "event_id": event.get("event_id"),
        })
    episodes: list[dict] = []
    for signal in signals[:int(limit)]:
        current = dict(signal)
        signal_ts_value = _finite(current.get("signal_ts", current.get("event_ts_ms")))
        if signal_ts_value is None:
            current.update({"bars": [], "episode_bars": [],
                            "episode_status": "missing_bars",
                            "funding_complete": False})
            episodes.append(current)
            continue
        signal_ts = int(signal_ts_value)
        entry_ts_value = _finite(current.get("entry_ts"))
        signal_timeframe = str(current.get("signal_timeframe") or "1m")
        try:
            from research.outcomes import TIMEFRAME_MS
            signal_close = signal_ts + TIMEFRAME_MS[signal_timeframe]
        except (ImportError, KeyError, TypeError, ValueError):
            signal_timeframe = "1m"
            signal_close = signal_ts + 60_000
        floor_ts = max(signal_close, int(entry_ts_value) if entry_ts_value is not None else signal_close)
        key = current.get("inst_id") or instrument
        candidates = bars_by_instrument.get(key, [])
        # Some fixtures omit instrument on the bar event.  Such a fallback is
        # safe only when this loader was asked for one instrument.
        if not candidates and instrument is not None:
            candidates = (bars_by_instrument.get(None, []) or
                          bars_by_instrument.get(str(instrument).split("-")[0], []))
        expected = floor_ts
        episode_bars: list[dict] = []
        episode_status = "complete"
        for event in candidates:
            ts_value = _finite(event.get("ts", event.get("event_ts_ms")))
            if ts_value is None:
                continue
            ts = int(ts_value)
            if ts < expected:
                continue
            if ts >= floor_ts + horizon * 60_000:
                break
            # A missing minute is an integrity failure, not a reason to score
            # a shorter path.  Do not append later bars after the gap.
            if ts != expected:
                episode_status = "missing_bars"
                break
            high, low, close = (_finite(event.get(name))
                                for name in ("high", "low", "close"))
            interval = _finite(event.get("bar_interval_ms"))
            if (high is None or low is None or close is None or high < low
                    or (interval is not None and interval != 60_000)):
                episode_status = "missing_bars"
                break
            bar = dict(event)
            bar.update({"ts": ts, "high": high, "low": low, "close": close,
                        "close_ts": int(_finite(event.get("close_ts")) or ts + 60_000),
                        "bar_interval_ms": 60_000})
            episode_bars.append(bar)
            expected = ts + 60_000
        if len(episode_bars) < horizon and episode_status == "complete":
            # A terminal stop/normalization may legitimately end before the
            # configured horizon.  We probe below; until then retain an
            # explicit partial marker instead of claiming a complete timeout.
            episode_status = "partial_bars"
        composite_candidates = composites_by_instrument.get(key, [])
        if not composite_candidates and instrument is not None:
            composite_candidates = (composites_by_instrument.get(None, []) or
                                    composites_by_instrument.get(
                                        str(instrument).split("-")[0], []))
        observable_values: list[float | None] = []
        observable_sources: list[tuple[int, Mapping[str, Any]]] = []
        for bar in episode_bars:
            close_ts = int(_finite(bar.get("close_ts"))
                           or int(_finite(bar.get("ts")) or 0) + 60_000)
            eligible = [composite for composite in composite_candidates
                        if _finite(composite.get("event_ts_ms")) is not None
                        and int(_finite(composite.get("event_ts_ms"))) <= close_ts
                        and _finite(composite.get("available_at_ms")) is not None
                        and int(_finite(composite.get("available_at_ms"))) <= close_ts]
            if not eligible:
                observable_values.append(None)
                continue
            composite = eligible[-1]
            observable_values.append(_finite(composite.get("__observable_value__")))
            observable_sources.append((len(observable_values) - 1, composite))
        if episode_bars and any(value is None for value in observable_values):
            episode_status = "missing_observable"
        current.update({
            "signal_ts": signal_ts, "entry_ts": int(entry_ts_value)
            if entry_ts_value is not None else signal_close,
            "signal_timeframe": signal_timeframe,
            "entry_price": (_finite(current.get("entry_price"))
                             or _finite(current.get("price"))
                             or _finite(current.get("mid"))),
            "stop_pct": _finite(current.get("stop_pct")) or float(stop_pct),
            "bar_interval_ms": 60_000,
            "bars": episode_bars, "episode_bars": episode_bars,
            "observable_values": observable_values,
            "observable_path_status": (
                "complete" if episode_bars and all(
                    value is not None for value in observable_values)
                else "missing"),
            "episode_status": episode_status,
        })
        direction_valid = current.get("direction") in {"long", "short"}
        if not direction_valid:
            # A recorder-derived episode cannot assume long.  Equal or
            # missing aggressive flow has no evidence for a reversion side.
            current["episode_status"] = "missing_direction"
        # Probe terminal state without funding.  This is used only to know
        # how many bars were actually held before funding coverage is checked;
        # the probe result itself is never persisted as an outcome.
        probe = None
        if direction_valid and episode_bars and current.get("entry_price") is not None:
            probe = resolve_episode(
                program,
                {**current, "funding_complete": True,
                 "funding_provenance": {"complete": True}},
                observable_values)
        if (probe and probe.get("status") in {"stop", "normalized", "timeout"}
                and (probe.get("status") != "timeout" or len(episode_bars) >= horizon)):
            terminal = str(probe["status"])
            held = int(probe.get("bars_held") or 0)
            first_bar_ts = int(_finite(episode_bars[0].get("ts")) or floor_ts)
            final_ts = first_bar_ts + held * 60_000
            per_instrument = funding_by_instrument.get(key, [])
            if not per_instrument and instrument is not None:
                per_instrument = (funding_by_instrument.get(None, []) or
                                  funding_by_instrument.get(
                                      str(instrument).split("-")[0], []))
            relevant = [event for event in per_instrument
                        if _finite(event.get("event_ts_ms")) is not None
                        and int(_finite(event.get("event_ts_ms"))) <= final_ts]
            complete, provenance, rate = _funding_provenance(
                relevant, first_ts=first_bar_ts, last_ts=final_ts,
                interval_hours=float(interval_hours))
            current["funding_complete"] = complete
            current["funding_provenance"] = provenance
            current["funding_status"] = provenance.get("status")
            if rate is not None:
                current["funding_rate_pct"] = rate
            if complete:
                current["state"] = terminal
                current["terminal_state"] = terminal
                current["episode_status"] = "complete"
                resolved = resolve_episode(
                    program, current, observable_values)
                current["terminal_state"] = resolved.get("status")
                current["state"] = resolved.get("status")
                current["outcome"] = resolved
            else:
                current.pop("state", None)
                current.pop("terminal_state", None)
                current["episode_status"] = "funding_incomplete"
        else:
            current["funding_complete"] = False
            current["funding_provenance"] = {
                "complete": False,
                "status": ("missing_direction" if not direction_valid
                            else "missing_bars"),
                "event_ids": [],
            }
            current["funding_status"] = current["funding_provenance"]["status"]

        source_rows: list[tuple[str, Mapping[str, Any]]] = []
        for source in current.get("event_sources", []):
            if isinstance(source, Mapping):
                source_rows.append((str(source.get("role") or "signal"), source))
        if not source_rows:
            source_rows.extend(
                (f"signal_{index}", {"event_id": event_id})
                for index, event_id in enumerate(current.get("source_event_ids", [])))
        source_rows.extend((f"bar_{index}", event)
                           for index, event in enumerate(episode_bars))
        source_rows.extend((f"bar_observable_{index}_{role}", source)
                           for index, composite in observable_sources
                           for role, source in (
                               [(str(item.get("role") or "composite"), item)
                                for item in composite.get("event_sources", [])]
                               if composite.get("event_sources") else
                               [("composite", composite)]))
        for index, event in enumerate(current.get("funding_provenance", {}).get("event_ids", [])):
            source_rows.append((f"funding_{index}", {"event_id": event}))
        if source_rows:
            context = _event_join_context(source_rows)
            current.update(context)
        else:
            current.update({"event_join_digest": content_hash([]),
                            "source_event_ids": []})
        episodes.append(current)
    return episodes[:int(limit)]


def _counts_as_tried(payload: Mapping[str, Any]) -> bool:
    """Return whether a persisted result consumed candidate novelty.

    Empty/partial recorder runs are deliberately retained as audit artifacts,
    but they must not exhaust the finite candidate neighborhood.  ``COMPLETE``
    is the lifecycle state emitted only after observable coverage, outcomes,
    and a fitted state model are all available; all other states remain
    retryable when more event-plane evidence arrives.
    """
    return str(payload.get("status") or "").upper() == "COMPLETE"


def prior_program_hashes(findings_store: Any | None = None,
                         analysis_root: str | Path | None = None) -> tuple[str, ...]:
    """Load only completed discovery identities without mutating state."""
    hashes: set[str] = set()
    path = getattr(findings_store, "path", None)
    if path is not None and Path(path).exists():
        import sqlite3
        try:
            with sqlite3.connect(path) as conn:
                rows = conn.execute(
                    "SELECT payload_json FROM analysis_runs WHERE kind='discovery'"
                ).fetchall()
            for (raw,) in rows:
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, Mapping) and _counts_as_tried(payload):
                    identity = payload.get("program_identity_hash")
                    if identity:
                        hashes.add(str(identity))
                    elif payload.get("program_hash"):
                        hashes.add(str(payload["program_hash"]))
        except sqlite3.Error:
            pass
    if analysis_root is not None:
        root = Path(analysis_root)
        if root.is_dir():
            for path in sorted(root.glob("*/result.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(payload, Mapping) or not _counts_as_tried(payload):
                    continue
                if payload.get("program_identity_hash"):
                    hashes.add(str(payload["program_identity_hash"]))
                elif payload.get("program_hash"):
                    hashes.add(str(payload["program_hash"]))
    return tuple(sorted(hashes))


def _persist_result(result: dict, analysis_root: str | Path,
                    findings_store: Any | None = None) -> dict:
    digest = content_hash(result)
    destination = Path(analysis_root) / digest
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "result.json").write_text(
        canonical_json(result) + "\n", encoding="utf-8")
    if findings_store is not None and hasattr(findings_store, "record_analysis"):
        findings_store.record_analysis("discovery",
                                       str(result.get("program_hash") or "idle"),
                                       result)
    return result | {"result_path": str(destination), "content_hash": digest}


def run_discovery(rows: Sequence[Mapping[str, Any]] | None = None, *,
                  result_root: str | Path = "research/results/discovery-artifacts",
                  analysis_root: str | Path = "research/results/discovery",
                  program: DiscoveryProgram | None = None,
                  findings_store: Any | None = None,
                  as_of: Mapping[str, Any] | None = None,
                  previous_hashes: Sequence[str] = (),
                  enabled: bool = True,
                  max_rows: int = MAX_ROWS,
                  max_nodes: int = MAX_NODES,
                  max_depth: int = MAX_DEPTH,
                  max_window: int = MAX_WINDOW,
                  contract_identity: Mapping[str, Any] | None = None) -> dict:
    """Perform one bounded candidate and persist a non-authorizing result.

    ``rows`` must already be exact-as-of event-plane rows.  The lifecycle does
    not fetch data, mutate a registry, or infer success when data is absent.
    Re-running the same content simply returns the existing result path.
    """
    if (not isinstance(contract_identity, Mapping)
            or not contract_identity.get("semantic_hash")
            or not all(contract_identity.get(key)
                       for key in ("strategy_id", "strategy_version", "variant_id"))):
        raise ValueError("validated canonical contract_identity is required")
    program = program or choose_program(previous_hashes, contract_identity)
    if program is None:
        idle_context = _aggregate_event_context(list(rows or [])[:MAX_ROWS])
        return _persist_result({
            "schema": SCHEMA, "status": "IDLE", "verdict": "INCONCLUSIVE",
            "program_hash": None, "reason": "bounded candidate neighborhood exhausted",
            "program_identity_hash": None,
            "authorizes": False,
            "contract_identity": dict(contract_identity),
            "event_evidence": idle_context,
            "event_join_digest": idle_context["event_join_digest"],
            "source_snapshot_ids": idle_context["source_snapshot_ids"],
            "source_feeds": idle_context["source_feeds"],
            "source_schemas": idle_context["source_schemas"],
            "as_of": dict(as_of or {}),
            "handoff": {"status": "RESEARCH_ONLY", "authorizes": False},
        }, analysis_root, findings_store)
    if not enabled:
        disabled_context = _aggregate_event_context(list(rows or [])[:int(max_rows)])
        result = {"schema": SCHEMA, "status": "DISABLED",
                  "verdict": "INCONCLUSIVE", "program_hash": program.content_hash,
                  "program_identity_hash": program_identity_hash(program, contract_identity),
                  "reason": "research.discovery.enabled is false",
                  "authorizes": False, "event_evidence": disabled_context,
                  "event_join_digest": disabled_context["event_join_digest"],
                  "source_snapshot_ids": disabled_context["source_snapshot_ids"],
                  "source_feeds": disabled_context["source_feeds"],
                  "source_schemas": disabled_context["source_schemas"],
                  "contract_identity": dict(contract_identity),
                  "as_of": dict(as_of or {}),
                  "limits": {"max_rows": max_rows, "max_nodes": max_nodes,
                             "max_depth": max_depth, "max_window": max_window},
                  "handoff": {"status": "RESEARCH_ONLY", "authorizes": False}}
        return _persist_result(result, analysis_root, findings_store)
    limits = {"max_rows": max_rows, "max_nodes": max_nodes,
              "max_depth": max_depth, "max_window": max_window}
    for name, value, ceiling in (("max_rows", max_rows, MAX_ROWS),
                                 ("max_nodes", max_nodes, MAX_NODES),
                                 ("max_depth", max_depth, MAX_DEPTH),
                                 ("max_window", max_window, MAX_WINDOW)):
        if isinstance(value, bool) or not 1 <= int(value) <= ceiling:
            raise ValueError(f"{name} is outside discovery bounds")
    if program.observable.node_count > int(max_nodes):
        raise ValueError("program exceeds configured max_nodes")
    if program.observable.depth > int(max_depth):
        raise ValueError("program exceeds configured max_depth")
    if _max_node_window(_thaw(program.observable.expression)) > int(max_window):
        raise ValueError("program exceeds configured max_window")
    bounded_rows = list(rows or [])[:int(max_rows)]
    if any(row.get("event_ts_ms") is not None for row in bounded_rows):
        bounded_rows.sort(key=lambda row: (
            int(_finite(row.get("event_ts_ms")) or 0),
            int(_finite(row.get("available_at_ms")) or 0)))
    decision_bound = (as_of or {}).get("decision_time_ms")
    cutoff_bound = (as_of or {}).get("cutoff_event_ts_ms", decision_bound)
    if decision_bound is not None:
        for row in bounded_rows:
            available = row.get("available_at_ms")
            event_ts = row.get("event_ts_ms")
            if available is not None and _finite(available) is not None \
                    and float(available) > float(decision_bound):
                raise ValueError("discovery row is unavailable at decision time")
            if cutoff_bound is not None and event_ts is not None \
                    and _finite(event_ts) is not None \
                    and float(event_ts) > float(cutoff_bound):
                raise ValueError("discovery row exceeds point-in-time cutoff")
    event_context = _aggregate_event_context(bounded_rows)
    if not bounded_rows:
        result = {"schema": SCHEMA, "status": "NO_DATA", "verdict": "INCONCLUSIVE",
                  "program_hash": program.content_hash,
                  "program_identity_hash": program_identity_hash(program, contract_identity),
                  "reason": "no exact-as-of event-plane rows", "authorizes": False,
                  "limits": limits, "contract_identity": dict(contract_identity or {}),
                  "event_evidence": event_context,
                  "event_join_digest": event_context["event_join_digest"],
                  "source_snapshot_ids": event_context["source_snapshot_ids"],
                  "source_feeds": event_context["source_feeds"],
                  "source_schemas": event_context["source_schemas"],
                  "handoff": {"status": "RESEARCH_ONLY", "authorizes": False}}
        return _persist_result(result, analysis_root, findings_store)
    artifact = write_artifact(program, result_root,
                              test_vectors=bounded_rows[:64],
                              contract_identity=contract_identity,
                              evidence_context=event_context)
    values = [program.observable.evaluate(row, bounded_rows[:index])
              for index, row in enumerate(bounded_rows)]
    covered = [index for index, value in enumerate(values) if value is not None]
    if len(covered) < 2:
        status, verdict = "INSUFFICIENT_COVERAGE", "INCONCLUSIVE"
        counterfactual = None
        world = None
    else:
        outcome_values = []
        episode_rows = [row.get("bars", row.get("episode_bars"))
                        for row in bounded_rows]
        # Scalar ``outcome_value``/``net_pct`` rows belong to older discovery
        # exports and may use different units or exit semantics.  They are
        # retained as audit input, but can never produce a paired result or a
        # COMPLETE lifecycle state.  Mixed episode/scalar batches fail closed
        # for the same reason.
        all_episode_rows = all(isinstance(bars, (list, tuple)) and bool(bars)
                               for bars in episode_rows)
        if not all_episode_rows:
            status = ("NO_STATE_DATA" if not any(row.get("state")
                                                  for row in bounded_rows)
                      else "NO_EPISODE_DATA")
            verdict, counterfactual, world = "INCONCLUSIVE", None, None
        else:
            resolved_rows: list[dict | None] = []
            for row, bars in zip(bounded_rows, episode_rows):
                episode_values = (row.get("observable_values")
                                  if row.get("observable_values") is not None
                                  else [program.observable.evaluate(
                                      bar, bars[:index])
                                        for index, bar in enumerate(bars)])
                resolved = resolve_episode(program, row, episode_values)
                resolved_rows.append(resolved if resolved.get("net_pct") is not None
                                      else None)
            outcome_values = resolved_rows
        if all_episode_rows and sum(value is not None for value in outcome_values) < 2:
            status, verdict = "NO_OUTCOME_DATA", "INCONCLUSIVE"
            counterfactual, world = None, None
        elif all_episode_rows:
            from research.discovery_counterfactual import run_counterfactual
            from research.discovery_world_model import fit_world_model
            counterfactual = run_counterfactual(program, bounded_rows, outcome_values)
            model_rows = []
            for index, (row, resolved) in enumerate(zip(bounded_rows, resolved_rows)):
                if resolved is None:
                    continue
                terminal = str(resolved.get("status") or "")
                # State is derived from the same resolver used for the outcome
                # and cannot be supplied by a scalar row or a lookahead field.
                model_rows.append({**row, "observable": values[index],
                                   "state": terminal})
            world, diagnostics = fit_world_model(model_rows)
            # A valid outcome series is not enough to claim a complete
            # discovery: the empirical world model also needs at least one
            # recognized terminal state in its fit window.  Without that
            # state evidence the model has no learned transitions (and its
            # predictions are only the uniform fallback), so surface the
            # absence explicitly rather than reporting a misleading
            # COMPLETE result.
            if not world.transitions:
                status = "NO_STATE_DATA"
            elif counterfactual.status != "COMPLETE":
                # A world model alone cannot certify an intervention: COMPLETE
                # requires paired episode outcomes and a deterministic
                # counterfactual result under the same exit/funding contract.
                status = "NO_COUNTERFACTUAL_DATA"
            else:
                status = "COMPLETE"
            # A discovery result can only be falsified/inconclusive; it never
            # becomes an edge or promotion authorization.
            verdict = ("FALSIFIED" if counterfactual.effect is not None
                       and counterfactual.effect <= 0 else "INCONCLUSIVE")
    result = {"schema": SCHEMA, "status": status, "verdict": verdict,
              "program": program.as_dict(), "program_hash": program.content_hash,
              "program_identity_hash": program_identity_hash(program, contract_identity),
              "artifact_hash": artifact.artifact_hash, "coverage": len(covered),
              "rows": len(bounded_rows), "counterfactual": counterfactual.as_dict() if counterfactual else None,
              "world_model": world.as_dict() if world else None,
              "authorizes": False, "limits": limits,
              "contract_identity": dict(contract_identity or {}),
              "event_evidence": event_context,
              "event_join_digest": event_context["event_join_digest"],
              "source_snapshot_ids": event_context["source_snapshot_ids"],
              "source_feeds": event_context["source_feeds"],
              "source_schemas": event_context["source_schemas"],
              "handoff": {"status": "RESEARCH_ONLY", "authorizes": False,
                           "promotion": "human_review_required"},
              "as_of": dict(as_of or {})}
    if world:
        result["world_model_diagnostics"] = diagnostics
    return _persist_result(result, analysis_root, findings_store)


__all__ = ["SCHEMA", "ARTIFACT_SCHEMA", "ObservableSpec", "CausalClaim",
           "EntryRule", "ExitProfile", "CounterfactualSpec", "DiscoveryProgram",
           "DiscoveryArtifact", "seed_forced_flow_pressure", "choose_program",
           "program_identity_hash", "evaluate_expression",
           "generate_source", "validate_generated_source", "write_artifact",
           "verify_artifact", "load_artifact", "load_exact_asof_rows",
           "load_discovery_rows", "load_discovery_episodes",
           "resolve_episode", "run_discovery", "content_hash"]
