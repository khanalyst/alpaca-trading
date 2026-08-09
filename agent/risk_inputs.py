"""Stdlib-only normalization helpers for risk option inputs.

Importing this module has no dependency on the risk engine or provider layers.
Nested helper calls resolve ``agent.risk`` lazily so existing callers and
monkeypatch paths remain valid without introducing an import cycle.
"""

from __future__ import annotations

import math
from numbers import Number
import re
import time
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timezone
from collections.abc import Mapping


def _facade_helper(name: str):
    # Deliberately import only at call time so importing this stdlib-only
    # module does not pull in the risk engine or its provider dependencies.
    from . import risk
    return getattr(risk, name)


_OCC_OPTION_RE = re.compile(
    r"^(?P<root>[A-Z0-9.]{1,8})(?P<expiry>\d{6})(?P<right>[CP])(?P<strike>\d{8})$")


def _object_mapping(value):
    """Return a shallow, safe mapping for provider rows and dataclass models."""
    if isinstance(value, Mapping):
        try:
            return dict(value)
        except (TypeError, ValueError, RuntimeError):
            return {}
    if is_dataclass(value) and not isinstance(value, type):
        result = {}
        for field in fields(value):
            try:
                result[field.name] = getattr(value, field.name)
            except Exception:  # noqa: BLE001 - malformed model rows fail closed
                continue
        return result
    try:
        data = vars(value)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, Mapping):
        return dict(data)
    # SDK models can expose slots/properties without a __dict__.  Read only
    # the fields used by risk; arbitrary attribute traversal is intentionally
    # avoided so malformed provider objects fail closed.
    names = (
        "symbol", "contract", "underlying_symbol", "underlying",
        "expiration", "expiration_date", "expiry", "strike",
        "strike_price", "type", "right", "option_type", "multiplier",
        "contract_multiplier", "contract_size", "size", "dte", "bid",
        "ask", "bid_price", "ask_price", "last", "last_price",
        "bid_size", "ask_size", "volume", "day_volume", "open_interest", "oi",
        "latest_quote", "quote", "timestamp", "quote_ts",
        "quote_timestamp", "quote_age_seconds", "stale", "quote_stale",
        "debit", "net_debit", "side", "strategy", "structure",
        "position_intent", "moneyness_distance", "underlying_price",
        "underlying_last", "spot",
    )
    result = {}
    for name in names:
        try:
            result[name] = getattr(value, name)
        except AttributeError:
            continue
        except Exception:  # noqa: BLE001 - a provider property must not abort selection
            continue
    return result


def _normalize_option_candidate(value):
    """Flatten a mapping/dataclass-like option row and nested quote/contract."""
    option = _facade_helper("_object_mapping")(value)
    if not option:
        return None
    # OptionSnapshot carries identity on ``contract`` and quote fields on the
    # outer object.  Fill missing fields from nested records without allowing
    # them to overwrite explicit outer values.
    nested_contract = _facade_helper("_object_mapping")(option.get("contract"))
    outer_symbol = option.get("symbol")
    nested_symbol = nested_contract.get("symbol")
    if (outer_symbol is not None and nested_symbol is not None and
            str(getattr(outer_symbol, "value", outer_symbol)).strip().upper() !=
            str(getattr(nested_symbol, "value", nested_symbol)).strip().upper()):
        option["_nested_identity_conflict"] = True
    for outer_name in ("underlying_symbol", "underlying"):
        outer_value = option.get(outer_name)
        if outer_value is None:
            continue
        for nested_name in ("underlying_symbol", "underlying"):
            nested_value = nested_contract.get(nested_name)
            if (nested_value is not None and
                    str(getattr(outer_value, "value", outer_value)).strip().upper() !=
                    str(getattr(nested_value, "value", nested_value)).strip().upper()):
                option["_nested_identity_conflict"] = True
    identity_aliases = (
        (("expiration", "expiration_date", "expiry"),
         _facade_helper("_expiration_date")),
        (("strike", "strike_price"), _facade_helper("_num")),
        (("type", "right", "option_type"),
         _facade_helper("_normalized_option_kind")),
        (("multiplier", "contract_multiplier", "contract_size", "size"),
         _facade_helper("_num")),
    )
    for aliases, normalizer in identity_aliases:
        if _facade_helper("_identity_alias_conflict")(
                option, nested_contract, aliases, normalizer):
            option["_nested_identity_conflict"] = True
    for nested_name in ("contract", "latest_quote", "quote"):
        nested = _facade_helper("_object_mapping")(option.get(nested_name))
        for key, nested_value in nested.items():
            option.setdefault(key, nested_value)
    for canonical, aliases in {
        "bid": ("bid_price",), "ask": ("ask_price",),
        "last": ("last_price",), "volume": ("day_volume",),
        "quote_ts": ("quote_timestamp", "timestamp"),
    }.items():
        if canonical not in option:
            for alias in aliases:
                if alias in option:
                    option[canonical] = option[alias]
                    break
    return option


def _candidate_sequence(candidates):
    """Treat one mapping/model as one row while retaining iterable callers."""
    if candidates is None:
        return ()
    if isinstance(candidates, (Mapping, str, bytes)):
        return (candidates,)
    try:
        return iter(candidates)
    except (TypeError, ValueError, RuntimeError):
        return (candidates,)


def _num(value, default=None):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _timestamp(value):
    """Normalize quote timestamps expressed as epoch or ISO text."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, Number):
        number = float(value)
        if not math.isfinite(number):
            return None
        if abs(number) > 100_000_000_000:
            number /= 1000.0
        return number
    else:
        raw = str(value).strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # A naive provider timestamp has no safe point-in-time meaning.  Do
        # not silently interpret it as UTC: callers may otherwise pair it
        # with a fabricated fresh quote age and bypass the freshness gate.
        return None
    return parsed.timestamp()


def _evaluation_timestamp(value):
    """Normalize an evaluation clock to a finite epoch timestamp.

    Risk checks accept an aware ``datetime`` or a numeric epoch.  A missing
    clock means "evaluate now" for the public selectors, while booleans,
    strings, naive datetimes, and non-finite values are malformed clocks.
    """
    if value is None:
        value = time.time()
    if isinstance(value, bool):
        raise ValueError("evaluation timestamp is invalid")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evaluation timestamp must be timezone-aware")
        try:
            timestamp = value.timestamp()
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError("evaluation timestamp is invalid") from exc
    elif isinstance(value, (int, float)):
        try:
            timestamp = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("evaluation timestamp is invalid") from exc
    else:
        raise ValueError("evaluation timestamp is invalid")
    if not math.isfinite(timestamp):
        raise ValueError("evaluation timestamp is invalid")
    # datetime.fromtimestamp is used for expiry/DTE checks.  Reject finite
    # epochs outside the platform's representable datetime range here rather
    # than allowing a provider row to trigger an uncaught OverflowError.
    try:
        datetime.fromtimestamp(timestamp, timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("evaluation timestamp is invalid") from exc
    return timestamp


def _expiration_date(value):
    """Normalize option expiry metadata to a calendar date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(getattr(value, "value", value)).strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except (TypeError, ValueError):
        return None


def _normalized_option_kind(value):
    kind = _facade_helper("_option_kind")(value)
    if kind in {"c", "call"}:
        return "call"
    if kind in {"p", "put"}:
        return "put"
    return None


def _option_kind(value) -> str:
    return str(getattr(value, "value", value or "")).lower().split(".")[-1].strip()


def _identity_alias_conflict(outer: Mapping, nested: Mapping,
                             aliases: tuple[str, ...], normalizer) -> bool:
    """Return true when explicit outer/nested identity aliases disagree."""
    outer_values = [outer[name] for name in aliases if name in outer]
    nested_values = [nested[name] for name in aliases if name in nested]
    def normalize(value):
        try:
            return normalizer(value)
        except Exception:  # noqa: BLE001 - malformed identity fails closed
            return None

    outer_normalized = [normalize(value) for value in outer_values]
    nested_normalized = [normalize(value) for value in nested_values]
    if any(value is None for value in outer_normalized + nested_normalized):
        return bool(outer_values or nested_values)
    all_values = outer_normalized + nested_normalized
    for first in all_values:
        for second in all_values:
            if isinstance(first, (int, float)) and isinstance(second, (int, float)):
                if abs(first - second) > 1e-9:
                    return True
            elif first != second:
                return True
    return False
