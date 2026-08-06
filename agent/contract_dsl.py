"""Bounded declarative contracts for research/staging lanes.

Legacy contracts are finite comparisons over fields declared by a complete
forward model.  A small set of deterministic primitives is also accepted,
but only when its source is already persisted in the snapshot: completed 1m
execution bars, realized funding events, or the observed order book.  The
compiler never fetches data or keeps process history; an absent source vetoes
the condition with a reason.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Callable

from . import registry as strategy_registry
from .forward_models import require_complete_contract

MIN_CLAIM_CHARS = 40
MAX_CONDITIONS = 6
MAX_COMPLEXITY = 12
MAX_SERIES_WINDOW = 30
MAX_EVENT_SEQUENCE = 4
OPERATORS: dict[str, Callable[[float, float], bool]] = {
    ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
}
DIRECTIONS = ("long", "short", "both")
BAR_FIELDS = frozenset({"open", "high", "low", "close", "volume"})
PERCENTILE_FIELDS = frozenset({
    "funding_percentile_30", "long_short_percentile_30",
})
SUPPORTED_PRIMITIVES = (
    "lagged_value", "rolling_change", "percentile_rank",
    "volatility_filter", "regime_filter", "event_sequence",
    "feature_interaction", "order_book_imbalance", "liquidity_state",
)


class ContractProposalError(ValueError):
    """The proposal is not executable as written."""


def _observable_fields() -> dict[str, None]:
    names: dict[str, None] = {}
    for strategy_id in sorted(strategy_registry.REGISTRY):
        try:
            model = require_complete_contract(strategy_id)
        except Exception:  # noqa: BLE001 - incomplete models add no inputs
            continue
        for name in model.required_fields:
            if not name.startswith("_enrichment."):
                names[name] = None
    return names


OBSERVABLE_FIELDS = _observable_fields()
FIELD_BOUNDS: dict[str, tuple[float, float]] = {
    "funding_percentile_30": (0.0, 100.0),
    "long_short_percentile_30": (0.0, 100.0),
    "range_pos_pct": (0.0, 100.0),
    "trend_1h": (-1.0, 1.0), "trend_4h": (-1.0, 1.0),
    "relative_volume_1h": (0.0, 100.0), "atr_1h_ratio": (0.0, 100.0),
    "spread_pct": (0.0, 100.0),
}


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple((str(k), _freeze(value[k])) for k in sorted(value, key=str))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise ContractProposalError(f"primitive argument {value!r} is unsupported")


def _thaw(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(v, (list, tuple)) and len(v) == 2
                         and isinstance(v[0], str) for v in value):
            return {v[0]: _thaw(v[1]) for v in value}
        return [_thaw(v) for v in value]
    return value


def _args(raw: object) -> dict[str, Any]:
    value = _thaw(raw)
    if value in (None, (), []):
        return {}
    if isinstance(value, dict):
        return dict(value)
    raise ContractProposalError("primitive args must be an object")


@dataclass(frozen=True)
class Condition:
    field: str | None
    op: str | None
    value: float | None
    when_direction: str | None = None
    kind: str = "comparison"
    args: tuple[tuple[str, Any], ...] = ()

    def describe(self) -> str:
        scope = f" ({self.when_direction} only)" if self.when_direction else ""
        if self.kind == "comparison":
            return f"{self.field} {self.op} {self.value:g}{scope}"
        return f"{self.kind} {_thaw(self.args)}{scope}"


@dataclass(frozen=True)
class ProposedContract:
    contract_id: str
    mechanism: str
    payer: str
    falsifier: str
    direction: str
    conditions: tuple[Condition, ...]
    author: str = "llm"
    notes: str = ""
    _fields: tuple[str, ...] = dataclass_field(default=())

    def describe(self) -> str:
        return "; ".join(c.describe() for c in self.conditions)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractProposalError(f"{name} is required")
    value = " ".join(value.split())
    if len(value) < MIN_CLAIM_CHARS:
        raise ContractProposalError(
            f"{name} is {len(value)} characters; a claim under "
            f"{MIN_CLAIM_CHARS} cannot state a cause and a test")
    return value


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractProposalError(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise ContractProposalError(f"{name} must be finite")
    return value


def _integer(value: object, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ContractProposalError(f"{name} must be an integer between {low} and {high}")
    return value


def _op(raw: dict, index: int) -> str:
    value = raw.get("op")
    if value not in OPERATORS:
        raise ContractProposalError(
            f"condition {index} uses operator {value!r}; allowed: "
            f"{', '.join(sorted(OPERATORS))}")
    return str(value)


def _when(raw: dict, index: int) -> str | None:
    value = raw.get("when_direction")
    if value is not None and value not in ("long", "short"):
        raise ContractProposalError(
            f"condition {index} when_direction must be long, short, or absent")
    return value


def _field(raw: dict, index: int) -> str:
    name = raw.get("field")
    if name not in OBSERVABLE_FIELDS:
        raise ContractProposalError(
            f"condition {index} names {name!r}, which no validated contract "
            f"declares. Proposable fields: {', '.join(sorted(OBSERVABLE_FIELDS))}")
    return str(name)


def _bar_field(raw: dict, index: int) -> str:
    field = raw.get("field")
    if field not in BAR_FIELDS:
        raise ContractProposalError(
            f"condition {index} execution_bars field must be one of "
            f"{', '.join(sorted(BAR_FIELDS))}")
    return str(field)


def _value(raw: dict, index: int, bounds: tuple[float, float] | None = None,
           key: str = "value") -> float:
    value = _number(raw.get(key), f"condition {index} {key}")
    if bounds is not None and not bounds[0] <= value <= bounds[1]:
        raise ContractProposalError(
            f"condition {index} threshold {value:g} is outside the observed "
            f"range ({bounds[0]:g}..{bounds[1]:g})")
    return value


def _primitive(raw: dict, index: int) -> Condition:
    kind = str(raw.get("primitive", raw.get("kind", ""))).strip().lower()
    aliases = {"lagged": "lagged_value", "rolling": "rolling_change",
               "volatility_regime": "volatility_filter",
               "regime": "regime_filter",
               "book_imbalance": "order_book_imbalance"}
    kind = aliases.get(kind, kind)
    when = _when(raw, index)
    if kind == "cross_sectional_rank":
        raise ContractProposalError(
            "cross_sectional_rank requires full-universe runtime context; "
            "the staged evaluator supplies one symbol row, so it is unsupported")

    if kind in {"lagged_value", "rolling_change"}:
        source = str(raw.get("source", "execution_bars"))
        if source not in {"execution_bars", "1m_execution_bars"}:
            raise ContractProposalError(
                f"condition {index} {kind} requires persisted execution_bars; "
                "no other history source exists")
        field = _bar_field(raw, index)
        op, value = _op(raw, index), _value(raw, index)
        if kind == "lagged_value":
            args = {"source": "execution_bars", "field": field,
                    "lag": _integer(raw.get("lag"), f"condition {index} lag", 1, MAX_SERIES_WINDOW),
                    "op": op, "value": value}
        else:
            mode = str(raw.get("mode", "pct")).lower()
            if mode not in {"pct", "absolute"}:
                raise ContractProposalError(
                    f"condition {index} rolling_change mode must be pct or absolute")
            args = {"source": "execution_bars", "field": field,
                    "window": _integer(raw.get("window"), f"condition {index} window", 1, MAX_SERIES_WINDOW),
                    "mode": mode, "op": op, "value": value}
        return Condition(field, op, value, when, kind, _freeze(args))

    if kind == "percentile_rank":
        field = raw.get("field")
        if field not in PERCENTILE_FIELDS:
            raise ContractProposalError(
                f"condition {index} percentile_rank requires a persisted "
                "funding or long/short percentile field")
        op, value = _op(raw, index), _value(raw, index, (0.0, 100.0))
        return Condition(str(field), op, value, when, kind,
                         _freeze({"field": str(field), "op": op, "value": value}))

    if kind == "volatility_filter":
        if raw.get("field", "atr_1h_ratio") != "atr_1h_ratio":
            raise ContractProposalError(
                f"condition {index} volatility_filter only supports the "
                "persisted atr_1h_ratio field")
        op, value = _op(raw, index), _value(raw, index, (0.0, 100.0))
        return Condition("atr_1h_ratio", op, value, when, kind,
                         _freeze({"field": "atr_1h_ratio", "op": op, "value": value}))

    if kind == "regime_filter":
        state = str(raw.get("state", "")).strip()
        if state not in {"trend_up", "trend_down", "high_volatility",
                         "choppy", "transition"}:
            raise ContractProposalError(
                f"condition {index} regime_filter state is unsupported")
        return Condition("regime", None, None, when, kind,
                         _freeze({"field": "regime", "state": state}))

    if kind == "event_sequence":
        source = str(raw.get("source", "realized_funding_events"))
        if source != "realized_funding_events":
            raise ContractProposalError(
                f"condition {index} event_sequence requires persisted "
                "realized_funding_events; no generic event stream exists")
        events = raw.get("events")
        if not isinstance(events, (list, tuple)) or not 2 <= len(events) <= MAX_EVENT_SEQUENCE:
            raise ContractProposalError(
                f"condition {index} event_sequence requires 2-{MAX_EVENT_SEQUENCE} events")
        patterns = []
        for event in events:
            if isinstance(event, str) and event in {"funding_positive", "funding_negative"}:
                patterns.append({"name": event})
                continue
            if not isinstance(event, dict) or event.get("field", "rate_pct") != "rate_pct":
                raise ContractProposalError(
                    f"condition {index} events must compare persisted rate_pct")
            event_op = event.get("op")
            if event_op not in OPERATORS:
                raise ContractProposalError(
                    f"condition {index} event operator {event_op!r} is unsupported")
            patterns.append({"field": "rate_pct", "op": str(event_op),
                             "value": _number(event.get("value"), f"condition {index} event value")})
        window = _integer(raw.get("window", len(patterns)),
                          f"condition {index} event window", len(patterns), MAX_SERIES_WINDOW)
        return Condition(None, None, None, when, kind,
                         _freeze({"source": source, "events": patterns, "window": window}))

    if kind == "feature_interaction":
        fields = raw.get("fields")
        if not isinstance(fields, (list, tuple)) or len(fields) != 2:
            raise ContractProposalError(
                f"condition {index} feature_interaction requires exactly two fields")
        fields = [_field({"field": name}, index) for name in fields]
        operation = str(raw.get("operation", "multiply"))
        if operation not in {"multiply", "difference", "sum", "ratio"}:
            raise ContractProposalError(
                f"condition {index} feature_interaction operation is unsupported")
        op, value = _op(raw, index), _value(raw, index)
        return Condition(None, op, value, when, kind,
                         _freeze({"fields": fields, "operation": operation,
                                  "op": op, "value": value}))

    if kind == "order_book_imbalance":
        metric = str(raw.get("metric", "depth"))
        if metric not in {"depth", "top"}:
            raise ContractProposalError(
                f"condition {index} order_book_imbalance metric must be depth or top")
        op, value = _op(raw, index), _value(raw, index, (-1.0, 1.0))
        return Condition(None, op, value, when, kind,
                         _freeze({"metric": metric, "op": op, "value": value}))

    if kind == "liquidity_state":
        state = str(raw.get("state", ""))
        if state not in {"available", "thin", "deep", "wide_spread", "stale"}:
            raise ContractProposalError(
                f"condition {index} liquidity_state is unsupported")
        limits = {}
        for key, bounds in {
                "max_depth_usd": (0.0, 1e12), "min_depth_usd": (0.0, 1e12),
                "max_spread_pct": (0.0, 100.0), "max_age_seconds": (0.0, 86400.0),
        }.items():
            if raw.get(key) is not None:
                limits[key] = _value(raw, index, bounds, key)
        needed = {"thin": {"max_depth_usd", "max_spread_pct"},
                  "deep": {"min_depth_usd"}, "wide_spread": {"max_spread_pct"},
                  "stale": {"max_age_seconds"}}.get(state, set())
        if needed and not needed.intersection(limits):
            raise ContractProposalError(
                f"condition {index} liquidity_state {state} needs a persisted threshold")
        return Condition(None, None, None, when, kind,
                         _freeze({"state": state, **limits}))

    raise ContractProposalError(
        f"condition {index} uses unsupported primitive {kind!r}; "
        "supported: lagged_value, rolling_change, percentile_rank, "
        "volatility_filter, event_sequence, feature_interaction, "
        "regime_filter, order_book_imbalance, liquidity_state")


def _condition(raw: object, index: int) -> Condition:
    if not isinstance(raw, dict):
        raise ContractProposalError(f"condition {index} must be an object")
    kind = raw.get("primitive", raw.get("kind"))
    if kind is not None and str(kind).lower() != "comparison":
        # Stored primitive conditions carry canonical arguments in ``args``.
        merged = dict(raw)
        persisted = _args(raw.get("args"))
        for key, value in persisted.items():
            merged.setdefault(key, value)
        merged["primitive"] = kind
        return _primitive(merged, index)
    name = _field(raw, index)
    op, value = _op(raw, index), _value(raw, index)
    low, high = FIELD_BOUNDS.get(name, (None, None))
    if low is not None and not low <= value <= high:
        raise ContractProposalError(
            f"condition {index} threshold {value:g} is outside the observed "
            f"range of {name} ({low:g}..{high:g}), so it fires always or never")
    return Condition(name, op, value, _when(raw, index))


def _complexity(condition: Condition) -> int:
    args = _thaw(condition.args)
    if condition.kind == "feature_interaction":
        return 2
    if condition.kind == "event_sequence":
        return len(args.get("events", ()))
    return 1


def validate(proposal: object) -> ProposedContract:
    if not isinstance(proposal, dict):
        raise ContractProposalError("a contract proposal must be an object")
    contract_id = proposal.get("contract_id")
    if not isinstance(contract_id, str) or not contract_id.strip():
        raise ContractProposalError("contract_id is required")
    contract_id = contract_id.strip()
    if not all(part.isalnum() or part in "-_." for part in contract_id):
        raise ContractProposalError("contract_id may contain only alphanumerics, '-', '_' and '.'")
    direction = proposal.get("direction", "both")
    if direction not in DIRECTIONS:
        raise ContractProposalError(f"direction must be one of {', '.join(DIRECTIONS)}")
    raw_conditions = proposal.get("conditions", ())
    raw_primitives = proposal.get("primitives", ())
    if not isinstance(raw_conditions, (list, tuple)):
        raise ContractProposalError("conditions must be a list")
    if not isinstance(raw_primitives, (list, tuple)):
        raise ContractProposalError("primitives must be a list")
    conditions = list(raw_conditions)
    primitives = list(raw_primitives)
    raw = conditions + primitives
    if not raw:
        raise ContractProposalError("at least one condition or primitive is required")
    if len(raw) > MAX_CONDITIONS:
        raise ContractProposalError(
            f"{len(raw)} conditions exceeds the {MAX_CONDITIONS} allowed; a contract that needs more is fitting the sample")
    parsed = tuple(_condition(value, index) for index, value in enumerate(raw))
    complexity = sum(_complexity(value) for value in parsed)
    if complexity > MAX_COMPLEXITY:
        raise ContractProposalError(f"contract complexity {complexity} exceeds the {MAX_COMPLEXITY} allowed")
    scoped = {value.when_direction for value in parsed if value.when_direction}
    # An unscoped condition applies to both directions, so a mixed contract
    # (one directional guard plus one shared guard) is still valid for both.
    if (direction == "both" and parsed
            and all(value.when_direction for value in parsed)
            and len(scoped) == 1):
        raise ContractProposalError(f"direction is 'both' but every condition is {scoped.pop()}-only")
    if direction in {"long", "short"} and scoped - {direction}:
        raise ContractProposalError(
            f"direction is {direction} but a condition is scoped to {', '.join(sorted(scoped - {direction}))}")
    return ProposedContract(
        contract_id=contract_id, mechanism=_text(proposal.get("mechanism"), "mechanism"),
        payer=_text(proposal.get("payer"), "payer"), falsifier=_text(proposal.get("falsifier"), "falsifier"),
        direction=direction, conditions=parsed,
        author=str(proposal.get("author") or "llm"),
        notes=" ".join(str(proposal.get("notes") or "").split()),
        _fields=tuple(sorted({value.field for value in parsed if value.field})),
    )


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _path(row: dict, path: str) -> Any:
    value: Any = row
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _bars(row: dict) -> list[dict] | None:
    values = _path(row, "_enrichment.execution_bars")
    if not isinstance(values, list):
        return None
    out = [value for value in values if isinstance(value, dict)
           and value.get("complete") is True and _finite(value.get("timestamp_ms")) is not None]
    return sorted(out, key=lambda value: float(value["timestamp_ms"]))


def _compare(value: float, op: str, threshold: float) -> bool:
    return OPERATORS[op](value, threshold)


def _event_match(event: dict, pattern: dict) -> bool:
    rate = _finite(event.get("rate_pct"))
    if pattern.get("name") == "funding_positive":
        return rate is not None and rate > 0
    if pattern.get("name") == "funding_negative":
        return rate is not None and rate < 0
    value = _finite(pattern.get("value"))
    return rate is not None and value is not None and _compare(rate, pattern["op"], value)


def _evaluate(condition: Condition, row: dict | None,
              overrides: dict | None = None) -> tuple[bool, str | None]:
    if row is None:
        return False, f"{condition.kind} unavailable"
    args = _thaw(condition.args)
    kind = condition.kind
    if kind == "comparison":
        value = _finite(row.get(condition.field))
        if value is None:
            return False, f"{condition.field} unavailable"
        threshold = condition.value
        if isinstance(overrides, dict) and condition.field in overrides:
            candidate = _finite(overrides.get(condition.field))
            if candidate is None:
                return False, f"{condition.field} override is not a number"
            threshold = candidate
        passed = _compare(value, condition.op, threshold)
        return (passed,
                None if passed
                else f"{condition.field} {value:g} fails {condition.op} {threshold:g}")
    if kind in {"lagged_value", "rolling_change"}:
        bars = _bars(row)
        if bars is None:
            return False, f"{kind} unavailable: execution_bars not persisted"
        field = args["field"]
        if kind == "lagged_value":
            lag = args["lag"]
            if len(bars) <= lag:
                return False, f"{kind} unavailable: insufficient complete bars"
            value = _finite(bars[-1 - lag].get(field))
        else:
            window = args["window"]
            if len(bars) <= window:
                return False, f"{kind} unavailable: insufficient complete bars"
            current, prior = _finite(bars[-1].get(field)), _finite(bars[-1 - window].get(field))
            if current is None or prior is None or (args["mode"] == "pct" and prior == 0):
                value = None
            else:
                value = current - prior if args["mode"] == "absolute" else (current / prior - 1) * 100
        if value is None:
            return False, f"{kind} unavailable"
        passed = _compare(value, args["op"], args["value"])
        return passed, None if passed else f"{kind} {value:g} fails {args['op']} {args['value']:g}"
    if kind == "regime_filter":
        value = row.get("regime")
        expected = args["state"]
        passed = isinstance(value, str) and value == expected
        return passed, None if passed else f"regime is not {expected}"
    if kind in {"percentile_rank", "volatility_filter"}:
        value = _finite(row.get(args["field"]))
        if value is None:
            return False, f"{kind} unavailable"
        passed = _compare(value, args["op"], args["value"])
        return passed, None if passed else f"{kind} {value:g} fails {args['op']} {args['value']:g}"
    if kind == "event_sequence":
        events = _path(row, "_enrichment.realized_funding_events")
        if not isinstance(events, list):
            return False, "event_sequence unavailable: realized_funding_events not persisted"
        events = [event for event in events if isinstance(event, dict)]
        patterns, window = args["events"], args["window"]
        if len(events) < len(patterns):
            return False, "event_sequence unavailable: insufficient events"
        start_at = max(0, len(events) - window)
        for start in range(start_at, len(events) - len(patterns) + 1):
            if all(_event_match(events[start + offset], pattern)
                   for offset, pattern in enumerate(patterns)):
                return True, None
        return False, "event_sequence did not match"
    if kind == "feature_interaction":
        values = [_finite(row.get(field)) for field in args["fields"]]
        if any(value is None for value in values):
            return False, "feature_interaction unavailable"
        left, right = values
        if args["operation"] == "multiply":
            value = left * right
        elif args["operation"] == "difference":
            value = left - right
        elif args["operation"] == "sum":
            value = left + right
        elif right != 0:
            value = left / right
        else:
            return False, "feature_interaction unavailable: zero denominator"
        passed = math.isfinite(value) and _compare(value, args["op"], args["value"])
        return passed, None if passed else f"feature_interaction {value:g} fails {args['op']} {args['value']:g}"
    book = _path(row, "_enrichment")
    if not isinstance(book, dict) or str(book.get("book_observation_error") or "").strip():
        return False, f"{kind} unavailable: book observation invalid"
    bid_depth, ask_depth = _finite(book.get("book_bid_depth_usd")), _finite(book.get("book_ask_depth_usd"))
    bid_top, ask_top = _finite(book.get("book_top_bid_size")), _finite(book.get("book_top_ask_size"))
    spread, age = _finite(book.get("book_spread_pct")), _finite(book.get("book_age_seconds"))
    if kind == "order_book_imbalance":
        bid, ask = (bid_depth, ask_depth) if args["metric"] == "depth" else (bid_top, ask_top)
        if bid is None or ask is None or bid + ask <= 0:
            return False, "order_book_imbalance unavailable"
        value = (bid - ask) / (bid + ask)
        passed = _compare(value, args["op"], args["value"])
        return passed, None if passed else f"order_book_imbalance {value:g} fails {args['op']} {args['value']:g}"
    state = args["state"]
    if state == "available":
        passed = spread is not None and bid_depth is not None and ask_depth is not None
    elif state == "thin":
        passed = ((args.get("max_depth_usd") is not None and
                   (bid_depth is None or ask_depth is None or min(bid_depth, ask_depth) < args["max_depth_usd"])) or
                  (args.get("max_spread_pct") is not None and spread is not None and spread > args["max_spread_pct"]))
    elif state == "deep":
        passed = args.get("min_depth_usd") is not None and bid_depth is not None and ask_depth is not None and min(bid_depth, ask_depth) >= args["min_depth_usd"]
    elif state == "wide_spread":
        passed = args.get("max_spread_pct") is not None and spread is not None and spread > args["max_spread_pct"]
    else:
        passed = args.get("max_age_seconds") is not None and age is not None and age > args["max_age_seconds"]
    return passed, None if passed else f"liquidity state is not {state}"


def compile_contract(contract: ProposedContract) -> Callable:
    def evaluate(snapshot: dict, direction: str, cfg: dict,
                 params: dict) -> tuple[bool, str | None]:
        del cfg
        overrides = params if isinstance(params, dict) else {}
        if contract.direction != "both" and direction != contract.direction:
            return False, f"{contract.contract_id} is {contract.direction}-only"
        # Staged lanes pass one symbol row. A full universe is not silently
        # interpreted as a row, which is why cross-sectional rank is rejected.
        row = snapshot if isinstance(snapshot, dict) and "_enrichment" in snapshot else snapshot
        for condition in contract.conditions:
            if condition.when_direction and condition.when_direction != direction:
                continue
            fired, reason = _evaluate(
                condition, row if isinstance(row, dict) else None, overrides)
            if not fired:
                return False, reason
        return True, None
    evaluate.__name__ = f"proposed_{contract.contract_id.replace('.', '_')}"
    evaluate.__doc__ = contract.mechanism
    return evaluate


def build(proposal: object) -> tuple[ProposedContract, Callable]:
    contract = validate(proposal)
    return contract, compile_contract(contract)
