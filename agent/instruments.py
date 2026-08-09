"""Fail-closed instrument policy for the trading runtime.

Only US-listed equities/ETFs and standard OCC option symbols are accepted.
Crypto identifiers are rejected explicitly so a missing or renamed provider
asset-class field cannot broaden the runtime's trading scope.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Any


_EQUITY_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,11}$")
_OPTION_RE = re.compile(
    r"^(?P<root>[A-Z0-9.]{1,8})(?P<expiry>\d{6})(?P<right>[CP])(?P<strike>\d{8})$")
_CRYPTO_ASSET_CLASSES = {
    "crypto", "cryptocurrency", "digital_asset", "digitalasset",
    "us_crypto", "spot_crypto",
}
_CRYPTO_BASES = {
    "AAVE", "AVAX", "BAT", "BCH", "BTC", "CRV", "DOGE", "DOT",
    "ETH", "GRT", "LINK", "LTC", "MKR", "SHIB", "SOL", "SUSHI",
    "UNI", "USDC", "USDT", "XLM", "XRP", "XTZ", "YFI",
}
_CRYPTO_QUOTES = {"USD", "USDC", "USDT", "BTC", "ETH"}


def _text(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip()


def reject_crypto(value: Any, field: str = "instrument") -> None:
    """Raise when *value* identifies a crypto asset, pair, class, or kind."""
    if isinstance(value, Mapping):
        for name in ("asset_class", "class", "kind", "asset_type", "symbol"):
            if name in value:
                reject_crypto(value[name], f"{field}.{name}")
        return
    raw = _text(value)
    lowered = raw.lower().replace("-", "_").replace(" ", "_")
    if lowered in _CRYPTO_ASSET_CLASSES or "crypto" in lowered:
        raise ValueError(f"{field} must not be crypto")
    upper = raw.upper().replace(" ", "")
    if "/" in upper:
        raise ValueError(f"{field} must not be a slash pair")
    compact = upper.replace("-", "").replace("_", "")
    if any(compact == base + quote for base in _CRYPTO_BASES
           for quote in _CRYPTO_QUOTES):
        raise ValueError(f"{field} must not be a crypto pair")


def validate_asset_class(value: Any) -> str:
    """Return the sole accepted provider asset-class names."""
    reject_crypto(value, "asset_class")
    normalized = _text(value).lower()
    if normalized not in {"us_equity", "us_option"}:
        raise ValueError(f"unsupported asset class {value!r}")
    return normalized


def validate_equity_symbol(symbol: Any) -> str:
    """Normalize a US equity/ETF symbol and reject non-equity identifiers."""
    reject_crypto(symbol, "equity symbol")
    normalized = _text(symbol).upper()
    if _OPTION_RE.fullmatch(normalized):
        raise ValueError("equity symbol must not be an OCC option symbol")
    if not _EQUITY_RE.fullmatch(normalized):
        raise ValueError(f"invalid US equity symbol {symbol!r}")
    return normalized


def validate_option_symbol(symbol: Any, underlying: Any = None, *,
                           expiration: Any = None, strike: Any = None) -> str:
    """Normalize OCC identity and optionally verify underlying/expiry/strike."""
    reject_crypto(symbol, "option symbol")
    normalized = _text(symbol).upper().replace(" ", "")
    match = _OPTION_RE.fullmatch(normalized)
    if match is None:
        raise ValueError(f"invalid OCC option symbol {symbol!r}")
    root = validate_equity_symbol(match.group("root"))
    if underlying is not None and root != validate_equity_symbol(underlying):
        raise ValueError("option symbol does not match its underlying")
    raw_expiry = match.group("expiry")
    try:
        parsed_expiry = date(2000 + int(raw_expiry[:2]), int(raw_expiry[2:4]),
                             int(raw_expiry[4:]))
    except ValueError as exc:
        raise ValueError("option symbol has an invalid expiration") from exc
    strike_units = int(match.group("strike"))
    if strike_units <= 0:
        raise ValueError("option symbol strike must be positive")
    if expiration is not None:
        expected_expiry = (expiration.date() if isinstance(expiration, datetime)
                           else expiration if isinstance(expiration, date) else
                           date.fromisoformat(str(expiration)[:10]))
        if parsed_expiry != expected_expiry:
            raise ValueError("option symbol does not match its expiration")
    if strike is not None:
        try:
            expected_strike = Decimal(str(strike))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("option strike metadata is invalid") from exc
        if (not expected_strike.is_finite() or
                expected_strike != Decimal(strike_units) / Decimal("1000")):
            raise ValueError("option symbol does not match its strike")
    return normalized


def validate_instrument(symbol: Any, asset_class: Any = None) -> str:
    """Validate a symbol with an explicit class, or infer only standard OCC."""
    if asset_class is not None:
        normalized_class = validate_asset_class(asset_class)
        return (validate_option_symbol(symbol) if normalized_class == "us_option"
                else validate_equity_symbol(symbol))
    raw = _text(symbol).upper().replace(" ", "")
    return validate_option_symbol(raw) if _OPTION_RE.fullmatch(raw) else \
        validate_equity_symbol(raw)


__all__ = [
    "reject_crypto", "validate_asset_class", "validate_equity_symbol",
    "validate_instrument", "validate_option_symbol",
]
