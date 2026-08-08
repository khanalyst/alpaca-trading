"""Thin, defensive wrapper around ccxt's OKX client.

Everything that touches the exchange lives here. All order placement uses
cross margin on USDT-settled perpetual swaps, in one-way (net) position mode.
Stop-loss and take-profit orders are placed ON THE EXCHANGE, so positions
stay protected even if this process dies.
"""

import json
import logging
import math
import os
import re
import time
import uuid
from dataclasses import dataclass

import ccxt
from ccxt.base.types import Entry

from .state import JournalError

log = logging.getLogger("exchange")
PROTECTIVE_ALGO_TYPES = ("conditional", "oco", "trigger")
ALL_ALGO_TYPES = PROTECTIVE_ALGO_TYPES + (
    "move_order_stop", "iceberg", "twap")

# OKX rejects any signed request whose OK-ACCESS-TIMESTAMP is more than 30s
# from server time (error 50102). We refuse to start well inside that window
# so a slow request cannot push an already-marginal clock over the edge.
CLOCK_SKEW_FATAL_MS = 15_000
CLOCK_SKEW_WARN_MS = 3_000
CLOCK_RECHECK_SECONDS = 900
ACCOUNT_RECHECK_SECONDS = 900
OKX_CRYPTO_INSTRUMENT_CATEGORY = "1"


@dataclass(frozen=True)
class TransferRecord:
    """One OKX ledger transfer with explicit identity quality."""

    transfer_id: str | None
    ts_ms: int
    net_usdt: float
    reconciliation_required: bool
    reconciliation_key: str

    def as_event(self) -> dict:
        return {
            "transfer_id": self.transfer_id,
            "ledger_ts_ms": self.ts_ms,
            "net_usdt": self.net_usdt,
            "identity_status": (
                "identified" if self.transfer_id
                else "reconciliation_required"),
            "reconciliation_required": self.reconciliation_required,
            "reconciliation_key": self.reconciliation_key,
        }


@dataclass(frozen=True)
class TransferBatch:
    net_usdt: float
    next_since_ms: int
    records: tuple[TransferRecord, ...]

    def __iter__(self):
        """Preserve the historical two-value unpacking contract."""
        yield self.net_usdt
        yield self.next_since_ms


class CredentialError(RuntimeError):
    """A permanent auth/clock problem. Retrying cannot fix it.

    Raised for OKX 50102 (timestamp outside the signing window) and for every
    ccxt AuthenticationError (bad key, bad passphrase, bad signature, missing
    Trade permission, IP not whitelisted, key expired). The engine treats this
    differently from a transient network fault: it stops the loop instead of
    spinning forever against credentials that will never start working.
    """


class EntryLiquidityRejected(RuntimeError):
    """A safe entry could not fit inside the configured price boundary.

    Structured details let the engine give the next LLM cycle useful
    execution feedback without parsing a human-readable log message.
    """

    def __init__(self, message: str, details: dict):
        super().__init__(message)
        self.details = details


class EntryOrderRejected(RuntimeError):
    """OKX refused an entry before a verified position was opened.

    The structured fields are safe to persist and let the engine distinguish
    a temporary exchange failure from an instrument/account incompatibility.
    They deliberately omit request headers, credentials and full responses.
    """

    def __init__(self, message: str, details: dict):
        super().__init__(message)
        self.details = details


class OrderSubmissionAmbiguousError(RuntimeError):
    """An order may exist but its single-shot submission was not recovered."""

    def __init__(self, message: str, audit: dict | None = None):
        super().__init__(message)
        details = dict(audit or {})
        details.setdefault("outcome", "ambiguous_unrecovered")
        self._order_audit = details


class MakerFirstPreSubmitError(RuntimeError):
    """A maker attempt stopped before any order submission was possible."""

    def __init__(self, message: str, audit: dict | None = None):
        super().__init__(message)
        details = dict(audit or {})
        details.setdefault("outcome", "pre_submit_failed")
        self._order_audit = details


class MakerFirstAmbiguousError(RuntimeError):
    """A maker-first attempt cannot prove a safe fallback boundary.

    Maker-first is allowed to hand the setup to the crossing path only after
    a structured zero-fill result and an explicit terminal cancellation
    status.  Every malformed or exceptional boundary is therefore marked as
    ambiguous so callers can fail closed without parsing exception text.
    """

    def __init__(self, message: str, audit: dict | None = None):
        super().__init__(message)
        details = dict(audit or {})
        details.setdefault("outcome", "ambiguous_maker_first")
        self._order_audit = details


class Exchange:
    def __init__(self, cfg: dict, alerts=None,
                 validate_account: bool = True):
        self.cfg = cfg
        self.alerts = alerts
        key = os.getenv("OKX_API_KEY")
        secret = os.getenv("OKX_API_SECRET")
        passphrase = os.getenv("OKX_API_PASSPHRASE")
        if not (key and secret and passphrase):
            raise RuntimeError(
                "Missing OKX credentials. Copy .env.example to .env and fill in "
                "OKX_API_KEY, OKX_API_SECRET and OKX_API_PASSPHRASE."
            )
        mode = cfg.get("mode")
        if mode not in {"demo", "live"}:
            raise ValueError("mode must be exactly 'demo' or 'live'")
        self.demo = mode == "demo"
        self.x = ccxt.okx({
            "apiKey": key,
            "secret": secret,
            "password": passphrase,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })
        # OKX publishes filled liquidation orders, but ccxt does not map the
        # path: its implicit endpoints are class-level Entry descriptors from
        # a static table (ccxt/abstract/okx.py) and the runtime
        # define_rest_api that once extended it is gone. Bind one more Entry
        # to THIS client through the same descriptor protocol ccxt uses, so
        # public_call finds an ordinary implicit method and ccxt - not this
        # file - still builds the URL. See public_call for why hand-building
        # one fails, and fails silently.
        self.x.publicGetPublicLiquidationOrders = Entry(
            "public/liquidation-orders", "public", "GET", {"cost": 1},
        ).__get__(self.x, type(self.x))
        if self.demo:
            # OKX demo trading = same endpoints + a simulated-trading header.
            try:
                self.x.set_sandbox_mode(True)
            except Exception:
                pass
            headers = dict(self.x.headers or {})
            headers["x-simulated-trading"] = "1"
            self.x.headers = headers
        # Before anything signed goes out, prove the clock can produce a valid
        # OK-ACCESS-TIMESTAMP. A drifted clock fails every private call, so
        # finding out here beats finding out mid-entry.
        self.check_clock()
        self.x.load_markets()
        # Account settings are never changed implicitly.  Read the current
        # configuration and refuse to operate unless the one-way position
        # model assumed everywhere else in this engine is already active.
        if validate_account:
            self._account_config = self.account_config(refresh=True)
            self.verify_account_safety(require_trade=False, refresh=False)

    def _alert(self, level: str, event: str, message: str,
               details: dict | None = None) -> None:
        if self.alerts:
            self.alerts.send(level, event, message, details)

    def retry(self, fn, *a, **kw):
        last = None
        for i in range(3):
            try:
                return fn(*a, **kw)
            except ccxt.InvalidNonce as e:
                # OKX 50102. ccxt files this under NetworkError, but a clock
                # outside the 30s signing window will not heal itself between
                # retries — re-measure the drift and report it plainly.
                raise CredentialError(self._clock_error(e)) from e
            except ccxt.AuthenticationError as e:
                raise CredentialError(
                    f"OKX rejected the API credentials: {e}. Check that "
                    "OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE in .env "
                    "are correct and unquoted-safe, that the key carries Read "
                    "+ Trade permission, that any IP binding matches this "
                    f"host, and that mode: {self.cfg.get('mode')} matches the "
                    "kind of key (demo keys work only in demo mode)."
                ) from e
            except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
                last = e
                time.sleep(1.5 * (i + 1))
        raise last

    def public_call(self, method: str, params: dict) -> list:
        """Call one of ccxt's generated public OKX endpoints, return `data`.

        Used for the statistics endpoints (open-interest history, long/short
        ratio) that back enrichment fields. Public and unsigned, so it cannot
        touch the account.

        Goes through ccxt's implicit method rather than a hand-built URL.
        That is not stylistic: ``urls["api"]["rest"]`` is the unexpanded
        template ``https://{hostname}``, so assembling a URL from it produces
        a request that always fails - and because these fields degrade to
        None by design, it would fail *silently*, leaving the strategies that
        depend on them never firing with nothing in the logs.

        Returns an empty list on any failure: no order depends on these
        fields, so a statistics outage must never be able to stop trading.
        """
        fetcher = getattr(self.x, method, None)
        if not callable(fetcher):
            log.warning("ccxt has no public endpoint %s; "
                        "enrichment field unavailable", method)
            return []
        try:
            response = self.retry(fetcher, params)
        except Exception:
            return []
        if not isinstance(response, dict) or str(response.get("code")) != "0":
            return []
        data = response.get("data")
        return data if isinstance(data, list) else []

    @staticmethod
    def _safe_exchange_error_text(value: object) -> str:
        """Return a bounded error string with configured secrets removed."""
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        for name in ("OKX_API_KEY", "OKX_API_SECRET",
                     "OKX_API_PASSPHRASE"):
            secret = os.getenv(name)
            if secret:
                text = text.replace(secret, "<redacted>")
        return text[:500] or "OKX rejected the request"

    @classmethod
    def _okx_error_details(cls, exc: Exception) -> dict:
        """Extract OKX's code/message without persisting a raw HTTP response."""
        raw = str(exc or "")
        text = cls._safe_exchange_error_text(raw)
        payload = None
        start, end = raw.find("{"), raw.rfind("}")
        if 0 <= start < end:
            try:
                candidate = json.loads(raw[start:end + 1])
                if isinstance(candidate, dict):
                    payload = candidate
            except json.JSONDecodeError:
                pass

        code = None
        message = None
        result_rows = []
        if payload:
            rows = payload.get("data") or []
            row = rows[0] if rows and isinstance(rows[0], dict) else {}
            code = row.get("sCode") or payload.get("code")
            message = row.get("sMsg") or payload.get("msg")
            for item in rows:
                if not isinstance(item, dict):
                    continue
                result_rows.append({
                    "code": str(item.get("sCode"))
                    if item.get("sCode") not in (None, "") else None,
                    "sub_code": str(item.get("subCode"))
                    if item.get("subCode") not in (None, "") else None,
                    "message": cls._safe_exchange_error_text(
                        item.get("sMsg") or item.get("msg")),
                    "order_id": str(item.get("ordId"))
                    if item.get("ordId") not in (None, "") else None,
                    "client_order_id": str(item.get("clOrdId"))
                    if item.get("clOrdId") not in (None, "") else None,
                    "in_time": str(item.get("inTime"))
                    if item.get("inTime") not in (None, "") else None,
                    "out_time": str(item.get("outTime"))
                    if item.get("outTime") not in (None, "") else None,
                })
        if code in (None, ""):
            match = re.search(
                r"(?:sCode|code)[\"'=:\s]+([A-Za-z0-9_-]+)", text)
            code = match.group(1) if match else None
        message = cls._safe_exchange_error_text(message or text)
        lowered = message.lower()
        permanent_hints = (
            "instrument does not exist",
            "instrument is not available",
            "instrument not available",
            "invalid instid",
            "invalid instrument",
            "not available for trading",
            "not supported",
            "unsupported",
            "has been suspended",
            "is suspended",
            "has been delisted",
            "is delisted",
        )
        classification = (
            "permanent"
            if any(hint in lowered for hint in permanent_hints)
            else "transient"
        )
        response = getattr(exc, "response", None)
        http_status = (
            getattr(response, "status_code", None)
            or getattr(exc, "status_code", None)
        )
        details = {
            "error_code": str(code) if code not in (None, "") else None,
            "error_message": message,
            "classification": classification,
            "http_status": int(http_status)
            if (isinstance(http_status, (int, float))
                and math.isfinite(float(http_status))) else None,
            "result_rows": result_rows,
        }
        order_audit = getattr(exc, "_order_audit", None)
        if isinstance(order_audit, dict):
            details["order_audit"] = order_audit
        return details

    @classmethod
    def _entry_order_rejection(
            cls, symbol: str, stage: str,
            exc: Exception) -> EntryOrderRejected:
        details = {
            "symbol": symbol,
            "stage": stage,
            **cls._okx_error_details(exc),
        }
        code = (f" code {details['error_code']}"
                if details["error_code"] else "")
        return EntryOrderRejected(
            f"{symbol} entry rejected during {stage}; OKX{code}: "
            f"{details['error_message']}; no unprotected fallback order "
            "was sent",
            details,
        )

    def clock_drift_ms(self) -> float:
        """Local clock minus OKX server clock, in milliseconds.

        Measured against GET /api/v5/public/time (unsigned), with the round
        trip halved out so network latency is not counted as drift.
        """
        sent = time.time() * 1000
        server = float(self.x.fetch_time())
        received = time.time() * 1000
        return (sent + received) / 2 - server

    def _clock_error(self, exc: Exception | None = None) -> str:
        try:
            drift = self.clock_drift_ms()
            measured = f"local clock is {drift / 1000:+.1f}s from OKX server time"
        except Exception:
            measured = "could not reach OKX to measure the drift"
        detail = f" ({exc})" if exc else ""
        return (
            f"OKX rejected the request timestamp{detail}; {measured}. Signed "
            "requests must be within 30s of server time. Enable NTP on this "
            "host (macOS: System Settings > General > Date & Time > Set "
            "automatically; Linux: `sudo timedatectl set-ntp true`) and "
            "restart the agent."
        )

    def check_clock(self, fatal: bool = True) -> float:
        """Verify the clock is inside OKX's signing window. Returns drift ms."""
        try:
            drift = self.clock_drift_ms()
        except Exception as e:
            # A public endpoint being unreachable is a network problem, not a
            # clock problem; let the normal retry paths deal with it.
            log.warning("Could not verify clock against OKX: %s", e)
            return 0.0
        self._clock_checked_at = time.time()
        if abs(drift) >= CLOCK_SKEW_FATAL_MS:
            msg = (f"Clock is {drift / 1000:+.1f}s from OKX server time, past "
                   f"the safe limit of {CLOCK_SKEW_FATAL_MS / 1000:.0f}s. "
                   "Every signed request will be rejected (50102). Enable NTP "
                   "on this host and restart.")
            self._alert("critical", "clock_drift", msg, {"drift_ms": drift})
            if fatal:
                raise CredentialError(msg)
            log.error(msg)
        elif abs(drift) >= CLOCK_SKEW_WARN_MS:
            log.warning("Clock is %+.1fs from OKX server time (limit 30s); "
                        "check NTP on this host.", drift / 1000)
        else:
            log.debug("Clock drift vs OKX: %+.0f ms", drift)
        return drift

    def recheck_clock_if_due(self) -> None:
        """Periodic re-check for hosts that drift after a clean startup."""
        last = getattr(self, "_clock_checked_at", 0.0)
        if time.time() - last >= CLOCK_RECHECK_SECONDS:
            self.check_clock(fatal=True)

    def account_config(self, refresh: bool = False) -> dict:
        """Return OKX account configuration through its read-only endpoint."""
        cached = getattr(self, "_account_config", None)
        if cached is not None and not refresh:
            return cached
        getter = getattr(self.x, "private_get_account_config", None)
        if not callable(getter):
            raise RuntimeError(
                "installed CCXT does not expose OKX account configuration")
        response = self.retry(getter)
        if str((response or {}).get("code", "0")) != "0":
            raise RuntimeError(
                f"OKX account configuration failed: "
                f"{(response or {}).get('msg') or response}")
        rows = (response or {}).get("data") or []
        if not rows or not isinstance(rows[0], dict):
            raise RuntimeError("OKX returned no account configuration")
        self._account_config = rows[0]
        return self._account_config

    def account_swap_instruments(self, refresh: bool = False) -> dict[str, dict]:
        """Return SWAP instruments enabled for this exact OKX account.

        Public market metadata includes products that can be unavailable in a
        user's region or demo environment.  The private, read-only account
        endpoint is authoritative and also carries ``instCategory``:
        ``1`` is crypto; stocks, commodities, forex and bonds use other values.
        """
        cached = getattr(self, "_account_swap_instruments", None)
        if cached is not None and not refresh:
            return cached
        getter = getattr(self.x, "private_get_account_instruments", None)
        if not callable(getter):
            raise RuntimeError(
                "installed CCXT does not expose OKX account instruments")
        response = self.retry(getter, {"instType": "SWAP"})
        if str((response or {}).get("code", "0")) != "0":
            raise RuntimeError(
                "OKX account instruments failed: "
                f"{(response or {}).get('msg') or response}")
        rows = (response or {}).get("data") or []
        if not rows:
            raise RuntimeError(
                "OKX returned no SWAP instruments for this account")
        by_id = {
            str(market.get("id") or ""): (symbol, market)
            for symbol, market in self.x.markets.items()
            if market.get("swap")
        }
        mapped: dict[str, dict] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            inst_id = str(row.get("instId") or "")
            item = by_id.get(inst_id)
            if not item:
                continue
            symbol, _ = item
            mapped[symbol] = dict(row)
        if not mapped:
            raise RuntimeError(
                "OKX account SWAP instruments did not match loaded markets")
        self._account_swap_instruments = mapped
        return mapped

    def taker_fee_pct(self, symbol: str, refresh: bool = False) -> float:
        """Return this account's actual taker fee rate as a positive percent."""
        cache = getattr(self, "_taker_fee_pct_cache", {})
        if symbol in cache and not refresh:
            return float(cache[symbol])
        fetcher = getattr(self.x, "fetch_trading_fee", None)
        if not callable(fetcher):
            raise RuntimeError(
                "installed CCXT cannot read the account taker fee")
        row = self.retry(fetcher, symbol)
        if not isinstance(row, dict) or row.get("taker") in (None, ""):
            raise RuntimeError(f"OKX returned no taker fee for {symbol}")
        try:
            rate = float(row["taker"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError(
                f"OKX returned an invalid taker fee for {symbol}") from exc
        if not math.isfinite(rate):
            raise RuntimeError(
                f"OKX returned an invalid taker fee for {symbol}")
        percent = abs(rate) * 100
        if not math.isfinite(percent) or percent < 0 or percent > 1:
            raise RuntimeError(
                f"OKX returned an implausible taker fee for {symbol}")
        cache = dict(cache)
        cache[symbol] = percent
        self._taker_fee_pct_cache = cache
        return percent

    @staticmethod
    def _permissions(config: dict) -> set[str]:
        raw = config.get("perm") or ""
        if isinstance(raw, list):
            values = raw
        else:
            values = str(raw).replace(";", ",").split(",")
        return {str(value).strip().lower() for value in values
                if str(value).strip()}

    def verify_account_safety(self, require_trade: bool = True,
                              refresh: bool = True) -> dict:
        """Fail closed on incompatible or over-privileged account settings.

        This is intentionally read-only. Position mode and API permissions
        must be configured in OKX before starting the agent.
        """
        config = self.account_config(refresh=refresh)
        pos_mode = str(config.get("posMode") or "")
        if pos_mode != "net_mode":
            raise CredentialError(
                "OKX position mode must be net_mode (one-way), but the "
                f"account reports {pos_mode or 'unknown'}. Change it in OKX "
                "while flat, then rerun check.")
        account_level = str(config.get("acctLv") or "")
        if account_level and account_level not in {"2", "3", "4"}:
            raise CredentialError(
                f"OKX account mode {account_level} does not support this "
                "cross-margin swap agent")

        permissions = self._permissions(config)
        if "withdraw" in permissions:
            raise CredentialError(
                "Refusing an API key with Withdraw permission. Create an "
                "IP-bound key with Read + Trade only.")
        if require_trade and "trade" not in permissions:
            raise CredentialError(
                "The API key is read-only; Read + Trade permission is required")
        bound_ips = str(config.get("ip") or "").strip()
        live = not getattr(self, "demo", self.cfg.get("mode") == "demo")
        if require_trade and live and not bound_ips:
            raise CredentialError(
                "Live trading requires an IP-bound OKX API key")
        try:
            if not require_trade:
                collateral = None
            elif live:
                collateral = self.verify_usdt_collateral()
            else:
                # Demo measures the same ratio and warns. Refusing to start
                # here would block the environment that exists for finding
                # this out, but staying silent lets mixed collateral quietly
                # invalidate every paper result.
                collateral = self.measure_usdt_collateral()
        except RuntimeError as exc:
            raise CredentialError(str(exc)) from exc
        result = {
            "position_mode": pos_mode,
            "account_level": account_level or None,
            "permissions": sorted(permissions),
            "ip_bound": bool(bound_ips),
            "non_usdt_collateral_pct": collateral,
        }
        if require_trade:
            self._account_checked_at = time.time()
        return result

    def verify_trade_permission(self) -> dict:
        """Verify Trade scope without changing leverage or placing an order."""
        return self.verify_account_safety(require_trade=True, refresh=True)

    def recheck_account_safety_if_due(self) -> None:
        last = getattr(self, "_account_checked_at", 0.0)
        if time.time() - last >= ACCOUNT_RECHECK_SECONDS:
            self.verify_account_safety(require_trade=True, refresh=True)

    # Keep a minimal inert pending-entry shim for maker-first compatibility.
    # Core execution does not carry observer/order-group metadata.
    def _clear_pending_entry(self) -> None:
        self._pending_entry = None

    @staticmethod
    def _client_order_id(prefix: str) -> str:
        return (prefix + uuid.uuid4().hex)[:32]

    @staticmethod
    def _trimmed_identity(value: object) -> str | None:
        if value in (None, "") or isinstance(value, bool):
            return None
        try:
            identity = str(value).strip()
        except Exception:
            return None
        return identity or None

    def _order_identity_matches(self, order: object, symbol: str,
                                client_order_id: str,
                                *, require_client_echo: bool = True) -> bool:
        """Prove one order belongs to the exact symbol and client request."""
        target_client = self._trimmed_identity(client_order_id)
        target_symbol = self._trimmed_identity(symbol)
        if (not isinstance(order, dict) or not target_client
                or not target_symbol):
            return False
        info = order.get("info")
        if info not in (None, "") and not isinstance(info, dict):
            return False
        info = info or {}

        normalized_symbol = self._trimmed_identity(order.get("symbol"))
        instrument_id = self._trimmed_identity(info.get("instId"))
        if not normalized_symbol and not instrument_id:
            return False
        expected_instrument = None
        try:
            market = self.x.market(target_symbol)
            if isinstance(market, dict):
                expected_instrument = self._trimmed_identity(market.get("id"))
        except (CredentialError, ccxt.AuthenticationError, JournalError,
                OrderSubmissionAmbiguousError):
            raise
        except Exception:
            # A normalized top-level symbol remains sufficient evidence. Raw
            # instId-only responses require market metadata to map safely.
            expected_instrument = None
        if normalized_symbol and normalized_symbol != target_symbol:
            return False
        if instrument_id and instrument_id not in {
                target_symbol, expected_instrument}:
            return False
        if instrument_id and expected_instrument is None \
                and instrument_id != target_symbol:
            return False

        identities = {
            self._trimmed_identity(order.get("clientOrderId")),
            self._trimmed_identity(info.get("clOrdId")),
            self._trimmed_identity(info.get("algoClOrdId")),
        }
        identities.discard(None)
        if identities:
            return identities == {target_client}
        # OKX/CCXT does not consistently echo clOrdId on ordinary order reads.
        # Recovery is stricter because the client ID is the lookup proof; a
        # normal fill read is already anchored by the exact exchange order ID.
        return not require_client_echo

    def _recover_order(self, symbol: str, client_order_id: str):
        """Recover an ambiguously acknowledged order without resubmitting it."""
        target = self._trimmed_identity(client_order_id)
        if not target:
            return None

        def identity_matches(order: object) -> bool:
            return (
                isinstance(order, dict)
                and self._trimmed_identity(order.get("id")) is not None
                and self._order_identity_matches(order, symbol, target)
            )

        for _ in range(4):
            for params in (
                    {"clientOrderId": target},
                    {"clientOrderId": target, "trigger": True}):
                try:
                    order = self.x.fetch_order(None, symbol, params)
                    if identity_matches(order):
                        return order
                except (CredentialError, ccxt.AuthenticationError):
                    raise
                except Exception:
                    continue
            # Some OKX/CCXT versions expose a client ID on an algo order as
            # clOrdId and others as algoClOrdId. Scan pending algo orders as
            # a final read-only recovery path before declaring ambiguity.
            for order_type in PROTECTIVE_ALGO_TYPES:
                try:
                    orders = self.x.fetch_open_orders(
                        symbol, None, None, {"ordType": order_type}) or []
                except (CredentialError, ccxt.AuthenticationError):
                    raise
                except Exception:
                    continue
                for order in orders:
                    if identity_matches(order):
                        return order
            time.sleep(0.75)
        return None

    def _create_order_once(self, symbol: str, order_type: str, side: str,
                           amount: float, price, params: dict, prefix: str,
                           ):
        """Place an order once; a timeout is reconciled, never blindly retried."""
        request = dict(params)
        client_id = self._client_order_id(prefix)
        request["clientOrderId"] = client_id
        started = time.monotonic()
        audit = {
            "client_order_id": client_id,
            "symbol": symbol,
            "order_type": order_type,
            "side": side,
            "amount": float(amount),
            "price": price,
            "params": {
                key: value for key, value in request.items()
                if key not in {"apiKey", "secret", "password"}
            },
            "submission_count": 1,
            "recovery_attempted": False,
        }
        try:
            order = self.x.create_order(
                symbol, order_type, side, amount, price, request)
            audit["latency_ms"] = round(
                (time.monotonic() - started) * 1000, 1)
            audit["outcome"] = "acknowledged"
            if isinstance(order, dict):
                order["_submission_audit"] = audit
            return order
        except ccxt.InvalidNonce as exc:
            audit["latency_ms"] = round(
                (time.monotonic() - started) * 1000, 1)
            audit["outcome"] = "clock_rejected"
            setattr(exc, "_order_audit", audit)
            raise CredentialError(self._clock_error(exc)) from exc
        except ccxt.AuthenticationError as exc:
            audit["latency_ms"] = round(
                (time.monotonic() - started) * 1000, 1)
            audit["outcome"] = "authentication_rejected"
            setattr(exc, "_order_audit", audit)
            raise CredentialError(
                "OKX rejected the API credentials while placing an order; "
                "stop the agent and re-run `python main.py check`"
            ) from exc
        except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
            audit["recovery_attempted"] = True
            recovered = self._recover_order(symbol, client_id)
            if recovered:
                audit["latency_ms"] = round(
                    (time.monotonic() - started) * 1000, 1)
                audit["outcome"] = "recovered_after_ambiguous_response"
                if isinstance(recovered, dict):
                    recovered["_submission_audit"] = audit
                log.warning("Recovered %s after ambiguous network response",
                            client_id)
                return recovered
            audit["latency_ms"] = round(
                (time.monotonic() - started) * 1000, 1)
            audit["outcome"] = "ambiguous_unrecovered"
            error = OrderSubmissionAmbiguousError(
                f"ambiguous order result for {client_id}; order was not retried",
                audit,
            )
            raise error from exc
        except Exception as exc:
            audit["latency_ms"] = round(
                (time.monotonic() - started) * 1000, 1)
            audit["outcome"] = "rejected"
            setattr(exc, "_order_audit", audit)
            raise

    @staticmethod
    def _fee_usd(order: dict) -> float:
        # CCXT may expose the same charge through both `fee` and `fees`.
        # Prefer the detailed list and only fall back to the singleton.
        fees = list(order.get("fees") or [])
        if not fees and order.get("fee"):
            fees = [order["fee"]]
        total = 0.0
        for fee in fees:
            if not isinstance(fee, dict):
                continue
            cost = float(fee.get("cost") or 0)
            # CCXT normally exposes fee cost as positive; OKX raw values are
            # negative when charged. Store costs as positive numbers.
            total += abs(cost)
        if total == 0:
            total = abs(float((order.get("info") or {}).get("fee") or 0))
        return total

    def verify_fill(self, order: dict, symbol: str, requested: float,
                    expected_price: float | None = None,
                    side: str | None = None) -> dict:
        """Wait for a terminal order state and return actual execution data."""
        full_terminal = {"filled", "closed"}
        canceled_terminal = {"canceled", "cancelled", "expired", "rejected"}
        terminal = full_terminal | canceled_terminal

        original = order if isinstance(order, dict) else {}
        raw_submission_audit = original.get("_submission_audit")
        submission_audit = (
            dict(raw_submission_audit)
            if isinstance(raw_submission_audit, dict) else {})

        def ambiguous(message: str, *, current=None):
            audit = dict(submission_audit)
            if isinstance(current, dict):
                try:
                    last_status = str(
                        current.get("status") or "unknown").lower()
                except Exception:
                    last_status = "malformed"
                audit.update({
                    "order_id": current.get("id") or original.get("id"),
                    "last_fill_status": last_status,
                })
            else:
                audit.update({
                    "order_id": original.get("id"),
                    "last_fill_status": "unknown",
                })
            audit["outcome"] = "fill_ambiguous"
            raise OrderSubmissionAmbiguousError(message, audit)

        def normalized_status(response: dict) -> str:
            raw = response.get("status")
            if raw in (None, ""):
                return "unknown"
            if not isinstance(raw, str):
                ambiguous("order status is malformed", current=response)
            return raw.strip().lower() or "unknown"

        if isinstance(requested, bool):
            ambiguous("requested order quantity is malformed")
        try:
            requested_value = float(requested)
        except (TypeError, ValueError, OverflowError):
            ambiguous("requested order quantity is malformed")
        if not math.isfinite(requested_value) or requested_value <= 0:
            ambiguous("requested order quantity is not positive and finite")
        if not isinstance(order, dict):
            ambiguous("order acknowledgement is not structured")
        if raw_submission_audit not in (None, "") \
                and not isinstance(raw_submission_audit, dict):
            ambiguous("order submission audit is not structured")
        current = order
        order_id = current.get("id")
        if order_id in (None, ""):
            ambiguous("order acknowledgement has no exchange order id",
                      current=current)
        try:
            expected_order_id = str(order_id).strip()
        except Exception:
            ambiguous("order acknowledgement exchange id is malformed",
                      current=current)
        if not expected_order_id:
            ambiguous("order acknowledgement has no exchange order id",
                      current=current)
        expected_client_id = self._trimmed_identity(
            submission_audit.get("client_order_id"))
        if not expected_client_id:
            ambiguous("order acknowledgement has no requested client order id",
                      current=current)
        try:
            initial_identity_matches = self._order_identity_matches(
                current, symbol, expected_client_id,
                require_client_echo=False)
        except (CredentialError, ccxt.AuthenticationError, JournalError,
                OrderSubmissionAmbiguousError):
            raise
        except Exception as exc:
            ambiguous(f"order identity could not be verified: {exc}",
                      current=current)
        if not initial_identity_matches:
            ambiguous(
                "order acknowledgement symbol/client identity does not match "
                "the request", current=current)

        def checked_response(response, stage: str) -> dict:
            if not isinstance(response, dict):
                ambiguous(f"{stage} order response is not structured",
                          current=response)
            response_id = response.get("id")
            if response_id in (None, ""):
                ambiguous(f"{stage} order response has no exchange order id",
                          current=response)
            try:
                response_order_id = str(response_id).strip()
            except Exception:
                ambiguous(f"{stage} order response id is malformed",
                          current=response)
            if not response_order_id:
                ambiguous(f"{stage} order response has no exchange order id",
                          current=response)
            if response_order_id != expected_order_id:
                ambiguous(f"{stage} order response id does not match request",
                          current=response)
            try:
                identity_matches = self._order_identity_matches(
                    response, symbol, expected_client_id,
                    require_client_echo=False)
            except (CredentialError, ccxt.AuthenticationError, JournalError,
                    OrderSubmissionAmbiguousError):
                raise
            except Exception as exc:
                ambiguous(f"{stage} order identity is malformed: {exc}",
                          current=response)
            if not identity_matches:
                ambiguous(
                    f"{stage} order symbol/client identity does not match "
                    "the request", current=response)
            return response

        deadline = time.monotonic() + float(
            self.cfg["execution"]["fill_timeout_seconds"])
        while time.monotonic() < deadline:
            status = normalized_status(current)
            if status in terminal:
                break
            try:
                fetched = self.retry(
                    self.x.fetch_order, order_id, symbol)
            except (CredentialError, ccxt.AuthenticationError, JournalError):
                raise
            except Exception as exc:
                ambiguous(
                    f"could not verify order {expected_order_id}: {exc}",
                    current=current)
            current = checked_response(fetched, "polled")
            time.sleep(0.25)

        status = normalized_status(current)
        if status not in terminal:
            try:
                self.x.cancel_order(order_id, symbol)
            except (CredentialError, ccxt.AuthenticationError, JournalError):
                raise
            except Exception as exc:
                ambiguous(
                    f"could not confirm cancellation of order "
                    f"{expected_order_id}: {exc}", current=current)
            try:
                fetched = self.retry(
                    self.x.fetch_order, order_id, symbol)
            except (CredentialError, ccxt.AuthenticationError, JournalError):
                raise
            except Exception as exc:
                ambiguous(
                    f"could not read order {expected_order_id} after cancel: "
                    f"{exc}", current=current)
            current = checked_response(fetched, "post-cancel")
            status = normalized_status(current)
        if status not in terminal:
            ambiguous(
                f"order {expected_order_id} has no verified terminal state",
                current=current)

        info_raw = current.get("info")
        if info_raw not in (None, "") and not isinstance(info_raw, dict):
            ambiguous("terminal order info is not structured", current=current)
        info = info_raw or {}

        def explicit_value(top_key: str, info_key: str):
            top = current.get(top_key)
            if top not in (None, ""):
                return top, True
            raw = info.get(info_key)
            if raw not in (None, ""):
                return raw, True
            return None, False

        filled_raw, filled_explicit = explicit_value("filled", "accFillSz")
        remaining_raw, remaining_explicit = explicit_value(
            "remaining", "remainSz")
        if not filled_explicit or isinstance(filled_raw, bool):
            ambiguous("terminal order has no explicit cumulative fill quantity",
                      current=current)
        try:
            filled = float(filled_raw)
        except (TypeError, ValueError, OverflowError):
            ambiguous("terminal cumulative fill quantity is malformed",
                      current=current)
        if (not math.isfinite(filled) or filled < 0
                or filled > requested_value):
            ambiguous("terminal cumulative fill quantity is invalid",
                      current=current)
        if remaining_explicit:
            if isinstance(remaining_raw, bool):
                ambiguous("terminal remaining quantity is malformed",
                          current=current)
            try:
                remaining = float(remaining_raw)
            except (TypeError, ValueError, OverflowError):
                ambiguous("terminal remaining quantity is malformed",
                          current=current)
            if (not math.isfinite(remaining) or remaining < 0
                    or remaining > requested_value):
                ambiguous("terminal remaining quantity is invalid",
                          current=current)
        else:
            remaining = requested_value - filled

        tolerance = max(1e-12, requested_value * 1e-9)
        if remaining_explicit and abs(
                filled + remaining - requested_value) > tolerance:
            ambiguous("terminal fill and remaining quantities disagree",
                      current=current)
        if status in full_terminal:
            if (abs(filled - requested_value) > tolerance
                    or (remaining_explicit and abs(remaining) > tolerance)):
                ambiguous("full terminal status does not prove a full fill",
                          current=current)
            filled = requested_value
            remaining = 0.0
        elif filled > 0:
            if filled >= requested_value - tolerance:
                ambiguous("canceled terminal status conflicts with a full fill",
                          current=current)
            if not remaining_explicit:
                ambiguous(
                    "partial fill lacks explicit remaining quantity",
                    current=current)

        average_raw, average_explicit = explicit_value("average", "avgPx")
        if filled > 0:
            if not average_explicit or isinstance(average_raw, bool):
                ambiguous("verified fill has no exchange average price",
                          current=current)
            try:
                average = float(average_raw)
            except (TypeError, ValueError, OverflowError):
                ambiguous("verified fill average price is malformed",
                          current=current)
            if not math.isfinite(average) or average <= 0:
                ambiguous("verified fill average price is invalid",
                          current=current)
        else:
            average = 0.0

        submission_audit = dict(current.get("_submission_audit")
                                or submission_audit)
        submission_audit.update({
            "order_id": expected_order_id,
            "last_fill_status": status,
        })
        if filled <= 0:
            submission_audit["outcome"] = "fill_unverified"
            error = RuntimeError(
                f"order {expected_order_id} has no verified fill")
            setattr(error, "_order_audit", submission_audit)
            raise error

        try:
            contract_size = float(
                self.x.market(symbol).get("contractSize") or 1)
        except (CredentialError, ccxt.AuthenticationError, JournalError,
                OrderSubmissionAmbiguousError):
            raise
        except Exception as exc:
            ambiguous(f"contract metadata is malformed: {exc}", current=current)
        if not math.isfinite(contract_size) or contract_size <= 0:
            ambiguous("contract size is not positive and finite", current=current)
        slippage = 0.0
        if expected_price and expected_price > 0:
            execution_side = str(
                side or current.get("side") or info.get("side") or "").lower()
            if execution_side == "buy":
                price_shortfall = average - expected_price
            elif execution_side == "sell":
                price_shortfall = expected_price - average
            else:
                price_shortfall = abs(average - expected_price)
            slippage = price_shortfall * filled * contract_size
        if not math.isfinite(slippage):
            ambiguous("fill slippage is not finite", current=current)
        realized_includes_costs = info.get("realizedPnl") not in (None, "")
        realized_raw = info.get("realizedPnl")
        if realized_raw in (None, ""):
            realized_raw = info.get("pnl")
        if realized_raw in (None, ""):
            realized = None
        else:
            if isinstance(realized_raw, bool):
                ambiguous("realized PnL is malformed", current=current)
            try:
                realized = float(realized_raw)
            except (TypeError, ValueError, OverflowError):
                ambiguous("realized PnL is malformed", current=current)
            if not math.isfinite(realized):
                ambiguous("realized PnL is not finite", current=current)
        try:
            fee_usd = self._fee_usd(current)
        except (CredentialError, ccxt.AuthenticationError, JournalError,
                OrderSubmissionAmbiguousError):
            raise
        except Exception as exc:
            ambiguous(f"fill fee metadata is malformed: {exc}", current=current)
        return {
            "symbol": str(symbol).strip(),
            "order_id": expected_order_id,
            "status": status,
            "requested": requested_value,
            "filled": filled,
            "average": average,
            "partial": filled + tolerance < requested_value,
            "fee_usd": fee_usd,
            "realized_pnl_usd": realized,
            "realized_includes_costs": realized_includes_costs,
            "slippage_usd": slippage,
            "adverse_slippage_usd": max(0.0, slippage),
            "client_order_id": expected_client_id,
            "submission_audit": (
                submission_audit or None),
            "order": current,
        }

    def price(self, symbol: str) -> float:
        t = self.retry(self.x.fetch_ticker, symbol)
        value = float(t.get("last") or t.get("close") or 0)
        if not math.isfinite(value) or value <= 0:
            raise RuntimeError(f"OKX returned an invalid price for {symbol}")
        return value

    @staticmethod
    def _usdt_equity_from_balance(balance: dict) -> float:
        """Return USDT-denominated equity without valuing other currencies.

        OKX ``totalEq`` is an account-wide USD conversion and therefore
        includes assets such as the 100 virtual OKB issued to demo accounts.
        This agent trades USDT-settled swaps, so sizing and circuit breakers
        must use the USDT currency row's ``eq`` field instead.
        """
        rows = ((balance.get("info") or {}).get("data") or [])
        if rows:
            details = rows[0].get("details") or []
            usdt = next((detail for detail in details
                         if str(detail.get("ccy") or "").upper() == "USDT"),
                        None)
            if usdt is None:
                raise RuntimeError("OKX returned no USDT currency equity")
            raw = usdt.get("eq")
            if raw in (None, ""):
                raise RuntimeError("OKX returned no USDT equity value")
            value = float(raw)
        else:
            # Compatibility fallback for a CCXT response without raw OKX
            # account data. This remains currency-specific; never fall back
            # to account-wide totalEq.
            value = float((balance.get("USDT") or {}).get("total") or 0)
        if not math.isfinite(value) or value <= 0:
            raise RuntimeError(
                "OKX USDT equity is not a positive finite value")
        return value

    def equity_usdt(self) -> float:
        balance = self.retry(self.x.fetch_balance)
        value = self._usdt_equity_from_balance(balance)
        return value

    def collateral_breakdown(self) -> tuple[float, float]:
        """Return (enabled non-USDT USD, USDT-plus-enabled USD equity).

        Split out from verify_usdt_collateral so demo can measure the same
        number without inheriting live's refusal to start.
        """
        bal = self.retry(self.x.fetch_balance)
        rows = (bal.get("info") or {}).get("data") or []
        if not rows:
            raise RuntimeError("OKX collateral breakdown is unavailable")
        account = rows[0]
        details = account.get("details") or []
        if not details:
            raise RuntimeError("OKX returned no collateral currency details")
        non_usdt = 0.0
        usdt_usd = None
        for detail in details:
            if str(detail.get("ccy") or "").upper() == "USDT":
                raw = detail.get("eqUsd")
                if raw in (None, ""):
                    raw = detail.get("eq")
                usdt_usd = float(raw or 0)
                continue
            # Disabled assets still appear in totalEq, but cannot support or
            # share risk with cross-margin positions. Missing flags are
            # treated conservatively as enabled for backwards compatibility.
            enabled = detail.get("collateralEnabled")
            if enabled is False or str(enabled).strip().lower() in {
                    "false", "0"}:
                continue
            value = float(detail.get("eqUsd") or 0)
            if not math.isfinite(value):
                raise RuntimeError("OKX collateral breakdown is not finite")
            non_usdt += abs(value)
        if (usdt_usd is None or not math.isfinite(usdt_usd)
                or usdt_usd <= 0):
            raise RuntimeError("OKX USDT collateral equity is not positive")
        return non_usdt, usdt_usd + non_usdt

    @staticmethod
    def _collateral_is_mixed(non_usdt: float, total: float) -> bool:
        """True when enabled non-USDT collateral is material to account risk."""
        return non_usdt > max(1.0, total * 0.01)

    def verify_usdt_collateral(self) -> float:
        """Require a live account whose equity is effectively all USDT."""
        non_usdt, total = self.collateral_breakdown()
        pct = non_usdt / total * 100
        if self._collateral_is_mixed(non_usdt, total):
            raise RuntimeError(
                f"enabled non-USDT collateral is {pct:.2f}% of trading "
                "equity; disable it, move it out, or use a dedicated "
                "USDT-only sub-account")
        return pct

    def measure_usdt_collateral(self) -> float:
        """Demo counterpart: report the same ratio, but warn instead of stop.

        Sizing uses USDT currency equity only. Enabled non-USDT collateral is
        still reported because it shares liquidation risk in cross margin;
        disabled demo assets such as OKX's virtual OKB are ignored entirely.
        """
        non_usdt, total = self.collateral_breakdown()
        pct = non_usdt / total * 100
        if self._collateral_is_mixed(non_usdt, total):
            log.warning(
                "enabled non-USDT collateral is %.1f%% of demo equity "
                "(%.0f of %.0f USD). It is excluded from position sizing but "
                "still shares cross-margin liquidation risk; disable it so "
                "demo matches the agent's USDT-only risk model.",
                pct, non_usdt, total)
        return pct

    def account_risk_metrics(self) -> dict:
        """Return conservative account-level IMR and MMR measurements.

        OKX defines ``mgnRatio`` as adjusted equity divided by maintenance
        margin, with forced liquidation beginning at 1.0.  This agent excludes
        non-USDT collateral from sizing, so use the smaller of adjusted equity
        and USDT currency equity for both ratios.  That can only make the guard
        more conservative than OKX's account-wide calculation.
        """
        bal = self.retry(self.x.fetch_balance)
        rows = (bal.get("info") or {}).get("data") or []
        if not rows:
            raise RuntimeError("OKX account risk fields are unavailable")
        account = rows[0]
        details = account.get("details") or []
        usdt_detail = next(
            (detail for detail in details
             if str(detail.get("ccy") or "").upper() == "USDT"),
            None,
        )
        usdt_equity = self._usdt_equity_from_balance(bal)
        equity_raw = account.get("adjEq")
        if equity_raw in (None, ""):
            equity_raw = account.get("totalEq")
        imr_raw = account.get("imr")
        if equity_raw not in (None, "") and imr_raw not in (None, ""):
            # Multi-currency and portfolio modes expose account-level USD risk.
            # Never let ignored collateral make the denominator look safer.
            equity = min(float(equity_raw), usdt_equity)
            mmr_raw = account.get("mmr")
            raw_ratio = account.get("mgnRatio")
            scope = "account_usdt_capped"
        elif usdt_detail is not None and usdt_detail.get("imr") not in (
                None, ""):
            # Single-currency/Futures mode exposes the same fields inside the
            # USDT detail row rather than at account level.
            equity = usdt_equity
            imr_raw = usdt_detail.get("imr")
            mmr_raw = usdt_detail.get("mmr")
            raw_ratio = usdt_detail.get("mgnRatio")
            scope = "usdt_currency"
        else:
            raise RuntimeError(
                "OKX returned no adjusted-equity/initial-margin measurement")
        initial_margin = float(imr_raw)
        if not math.isfinite(equity) or equity <= 0:
            raise RuntimeError("OKX adjusted equity is not positive")
        if not math.isfinite(initial_margin) or initial_margin < 0:
            raise RuntimeError("OKX initial margin is not a finite value")

        if mmr_raw in (None, ""):
            if initial_margin > 0:
                raise RuntimeError(
                    "OKX returned no maintenance-margin measurement")
            maintenance_margin = 0.0
        else:
            maintenance_margin = float(mmr_raw)
        if (not math.isfinite(maintenance_margin)
                or maintenance_margin < 0):
            raise RuntimeError(
                "OKX maintenance margin is not a finite value")
        maintenance_ratio = (
            equity / maintenance_margin if maintenance_margin > 0 else None
        )
        if raw_ratio in (None, ""):
            okx_ratio = None
        else:
            okx_ratio = float(raw_ratio)
            if not math.isfinite(okx_ratio) or okx_ratio < 0:
                raise RuntimeError(
                    "OKX margin ratio is not a finite value")
        return {
            "equity_usdt_basis": equity,
            "initial_margin_usd": initial_margin,
            "maintenance_margin_usd": maintenance_margin,
            "initial_margin_usage_pct": initial_margin / equity * 100,
            "maintenance_margin_ratio": maintenance_ratio,
            "okx_margin_ratio": okx_ratio,
            "risk_scope": scope,
        }

    def margin_usage_pct(self) -> float:
        """Compatibility wrapper for callers needing only initial-margin use."""
        return float(
            self.account_risk_metrics()["initial_margin_usage_pct"])

    def transfers_since(
            self, since_ms: int,
            seen_ids: set[str] | None = None) -> TransferBatch:
        """Net USDT transferred in/out of the trading account since since_ms.

        Used to rebase the drawdown and daily-loss benchmarks so a deposit is
        not counted as profit and a withdrawal is not counted as a crash.

        Returns a TransferBatch that still unpacks as
        ``(net_usdt, next_since_ms)``. fetch_ledger treats `since` as
        inclusive, so the cursor advances one past the newest entry counted --
        otherwise the same transfer is re-counted every cycle until a newer
        ledger entry appears. On error the cursor stays put so transfers that
        happened during an outage are picked up on recovery.
        """
        try:
            entries = self.retry(self.x.fetch_ledger, "USDT", since_ms, 100)
        except Exception as e:
            log.debug("fetch_ledger unavailable: %s", e)
            return TransferBatch(0.0, since_ms, ())
        already_seen = set(seen_ids or ())
        net = 0.0
        latest = since_ms - 1
        records = []
        for index, e in enumerate(entries or []):
            ts = int(e.get("timestamp") or 0)
            if ts < since_ms:
                continue
            latest = max(latest, ts)
            okx_type = str((e.get("info") or {}).get("type", ""))
            is_transfer = e.get("type") == "transfer" or okx_type == "1"
            if not is_transfer:
                continue
            amt = abs(float(e.get("amount") or 0))
            direction = e.get("direction")
            if direction == "in":
                signed = amt
            elif direction == "out":
                signed = -amt
            else:
                continue
            info = e.get("info") or {}
            raw_id = next((value for value in (
                e.get("id"), info.get("billId"), info.get("id"),
                info.get("tradeId"), info.get("ordId"), info.get("txId"))
                if value not in (None, "")), None)
            transfer_id = f"okx:{raw_id}" if raw_id is not None else None
            # This key is for an operator's reconciliation queue only. It is
            # deliberately not used as an idempotency key: equal legitimate
            # transfers can share timestamp, direction and amount.
            reconciliation_key = (
                f"ledger:{ts}:{direction}:{amt:.12g}:{index}")
            if transfer_id and transfer_id in already_seen:
                continue
            if transfer_id:
                already_seen.add(transfer_id)
            net += signed
            records.append(TransferRecord(
                transfer_id=transfer_id, ts_ms=ts, net_usdt=signed,
                reconciliation_required=transfer_id is None,
                reconciliation_key=reconciliation_key))
        return TransferBatch(net, latest + 1, tuple(records))

    def funding_since(self, symbol: str, since_ms: int) -> float | None:
        """Return signed funding paid/received, or None if it cannot be read."""
        try:
            rows = self.retry(
                self.x.fetch_funding_history, symbol, since_ms, 100) or []
        except (CredentialError, ccxt.AuthenticationError, JournalError,
                OrderSubmissionAmbiguousError):
            raise
        except Exception as exc:
            log.warning("funding history unavailable for %s: %s", symbol, exc)
            return None
        # An empty history is not proof of zero funding.  Returning 0.0 here
        # caused the close path to label an unobserved settlement as
        # ``available`` and let a forecast enter realized PnL.  Preserve a
        # real zero amount when the exchange returned an explicit row, but
        # fail closed when no usable funding ledger entry exists.
        amounts = []
        for row in rows:
            try:
                amount = float(row.get("amount"))
            except (AttributeError, TypeError, ValueError):
                continue
            if math.isfinite(amount):
                amounts.append(amount)
        return sum(amounts) if amounts else None

    @staticmethod
    def _seconds_timestamp(value: object) -> float | None:
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(timestamp) or timestamp <= 0:
            return None
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0
        now = time.time()
        if timestamp < 1_230_768_000 or timestamp > now + 60:
            return None
        return timestamp

    def position_opened_at(self, pos: dict) -> float | None:
        """Recover the start of the current continuous net position.

        OKX normally returns ``cTime`` on a position. If an adapter omits it,
        reverse the account's fills until the current net position crosses
        flat. Returning ``None`` is intentional: the engine pauses rather
        than inventing a fresh timestamp and silently defeating max-hold.
        """
        info = pos.get("info") or {}
        for value in (
                info.get("cTime"), info.get("createdTime"),
                info.get("openTime"), info.get("openTimestamp"),
                pos.get("created"), pos.get("datetime")):
            if isinstance(value, str) and not value.replace(".", "", 1).isdigit():
                continue
            recovered = self._seconds_timestamp(value)
            if recovered is not None:
                return recovered

        symbol = pos.get("symbol")
        contracts = abs(float(pos.get("contracts") or 0))
        if not symbol or not math.isfinite(contracts) or contracts <= 0:
            return None
        side = str(pos.get("side") or info.get("posSide") or "").lower()
        if side not in {"long", "short"}:
            raw = float(info.get("pos") or 0)
            side = "long" if raw >= 0 else "short"
        current = contracts if side == "long" else -contracts
        lookback_hours = max(
            24.0 * 30,
            float(self.cfg.get("risk", {}).get("max_hold_hours") or 24) * 2,
        )
        since_ms = int((time.time() - lookback_hours * 3600) * 1000)
        try:
            fills = self.retry(
                self.x.fetch_my_trades, symbol, since_ms, 100) or []
        except (CredentialError, ccxt.AuthenticationError, JournalError,
                OrderSubmissionAmbiguousError):
            raise
        except Exception as exc:
            log.warning("position age fill history unavailable for %s: %s",
                        symbol, exc)
            return None
        for fill in sorted(
                fills,
                key=lambda row: int(row.get("timestamp") or 0),
                reverse=True):
            fill_side = str(fill.get("side") or "").lower()
            try:
                amount = abs(float(fill.get("amount") or 0))
            except (TypeError, ValueError):
                continue
            if fill_side not in {"buy", "sell"} \
                    or not math.isfinite(amount) or amount <= 0:
                continue
            signed = amount if fill_side == "buy" else -amount
            previous = current - signed
            if current != 0 and (
                    abs(previous) <= max(1e-12, contracts * 1e-9)
                    or previous * current < 0):
                recovered = self._seconds_timestamp(fill.get("timestamp"))
                if recovered is not None:
                    return recovered
            current = previous
        return None

    def positions(self) -> list[dict]:
        out = []
        for p in self.retry(self.x.fetch_positions) or []:
            contracts = float(p.get("contracts") or 0)
            if not math.isfinite(contracts):
                raise RuntimeError(
                    f"OKX returned a non-finite position size for "
                    f"{p.get('symbol') or 'unknown symbol'}")
            if abs(contracts) > 0:
                out.append(p)
        return out

    def position(self, symbol: str, side: str | None = None) -> dict | None:
        for position in self.positions():
            if position.get("symbol") != symbol:
                continue
            if side is None:
                return position
            position_side = str(
                position.get("side")
                or (position.get("info") or {}).get("posSide") or "").lower()
            if position_side not in {"long", "short"}:
                raw = float((position.get("info") or {}).get("pos") or 0)
                position_side = "long" if raw >= 0 else "short"
            if position_side == side:
                return position
        return None

    def contracts_for_notional(self, symbol: str, notional_usd: float,
                               price: float) -> float:
        if (not math.isfinite(float(notional_usd))
                or not math.isfinite(float(price))
                or float(notional_usd) <= 0 or float(price) <= 0):
            return 0.0
        m = self.x.market(symbol)
        contract_size = float(m.get("contractSize") or 1)
        raw = notional_usd / (contract_size * price)
        try:
            amt = float(self.x.amount_to_precision(symbol, raw))
        except Exception:
            return 0.0
        min_amt = ((m.get("limits") or {}).get("amount") or {}).get("min") or 0
        if amt <= 0 or amt < float(min_amt or 0):
            return 0.0
        return amt

    def book_state(self, symbol: str, band_pct: float = 0.35,
                   max_age_seconds: float | None = None) -> dict:
        """Observe the order book without judging it.

        ``guarded_entry_limit`` reads the same book but only journals when it
        rejects, so every passing observation is discarded. That is precisely
        backwards for the depth-restoration hypothesis, which claims the
        tradeable moment in a liquidation
        cascade is the depth *restoration* rather than the impulse: spread
        spikes then normalises, top-of-book depth collapses then refills, and
        price is still near the extreme when the refill happens. That
        signature is only visible if the ordinary readings were kept too.

        This raises nothing. A caller journalling market observations must
        never be able to interrupt trading, so every failure becomes a row of
        nulls carrying the reason.
        """
        blank = {
            "symbol": symbol, "mid": None, "best_bid": None,
            "best_ask": None, "spread_pct": None,
            "bid_depth_usd": None, "ask_depth_usd": None,
            "top_bid_size": None, "top_ask_size": None,
            "bid_levels": [], "ask_levels": [], "contract_size": None,
            "band_pct": float(band_pct), "book_ts": None,
            "age_seconds": None, "error": None,
        }
        try:
            book = self.retry(self.x.fetch_order_book, symbol, 50)
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            if not bids or not asks:
                blank["error"] = "no two-sided depth"
                return blank
            best_bid = float(bids[0][0])
            best_ask = float(asks[0][0])
            if not (math.isfinite(best_bid) and math.isfinite(best_ask)
                    and best_ask >= best_bid > 0):
                blank["error"] = "invalid top of book"
                return blank

            mid = (best_bid + best_ask) / 2
            contract_size = float(
                self.x.market(symbol).get("contractSize") or 1)
            if not math.isfinite(contract_size) or contract_size <= 0:
                blank["error"] = "invalid contract size"
                return blank
            floor = mid * (1 - float(band_pct) / 100)
            ceiling = mid * (1 + float(band_pct) / 100)

            def depth(levels, inside) -> float:
                total = 0.0
                for level in levels:
                    price = float(level[0])
                    amount = float(level[1])
                    if (not math.isfinite(price) or not math.isfinite(amount)
                            or price <= 0 or amount < 0):
                        raise ValueError("invalid order-book level")
                    if not inside(price):
                        break
                    total += price * amount * contract_size
                return round(total, 2)

            timestamp = book.get("timestamp")
            age_seconds = None
            if timestamp not in (None, ""):
                timestamp = float(timestamp)
                if math.isfinite(timestamp) and timestamp > 0:
                    if timestamp > time.time() * 1000 + 5_000:
                        blank["error"] = "order book timestamp is in the future"
                        return blank
                    age_seconds = max(
                        0.0, (time.time() * 1000 - timestamp) / 1000)
            blank.update({
                "mid": round(mid, 10),
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread_pct": round((best_ask - best_bid) / mid * 100, 6),
                "bid_depth_usd": depth(bids, lambda p: p >= floor),
                "ask_depth_usd": depth(asks, lambda p: p <= ceiling),
                "top_bid_size": float(bids[0][1]),
                "top_ask_size": float(asks[0][1]),
                # Preserve only the executable depth the simulator actually
                # consumes. Amounts are contracts, matching OKX/CCXT.
                "bid_levels": [[float(level[0]), float(level[1])]
                               for level in bids[:50]],
                "ask_levels": [[float(level[0]), float(level[1])]
                               for level in asks[:50]],
                "contract_size": contract_size,
                "book_ts": timestamp,
                "age_seconds": age_seconds,
            })
            if timestamp in (None, "") or age_seconds is None:
                blank["error"] = "order book has no valid exchange timestamp"
            elif (max_age_seconds is not None
                  and age_seconds > float(max_age_seconds)):
                blank["error"] = (
                    f"order book is {age_seconds:.1f}s old; limit is "
                    f"{float(max_age_seconds):.1f}s")
            return blank
        except Exception as e:                      # observation, never a gate
            blank["error"] = f"{type(e).__name__}: {e}"
            return blank

    def guarded_entry_limit(self, symbol: str, side: str, contracts: float,
                            max_spread_pct: float,
                            max_slippage_pct: float,
                            max_age_seconds: float) -> dict:
        """Build a marketable IOC limit only when displayed depth is enough.

        The order-book check rejects wide spreads and insufficient size. The
        returned limit is the hard exchange-side price boundary, so a sudden
        move after this read produces no/partial fill instead of unlimited
        market-order slippage.
        """
        book = self.retry(self.x.fetch_order_book, symbol, 50)
        timestamp = book.get("timestamp")
        if timestamp in (None, ""):
            raise RuntimeError(f"{symbol} order book has no exchange timestamp")
        timestamp = float(timestamp)
        if not math.isfinite(timestamp) or timestamp <= 0:
            raise RuntimeError(f"{symbol} order book timestamp is invalid")
        if timestamp > time.time() * 1000 + 5_000:
            raise RuntimeError(f"{symbol} order book timestamp is in the future")
        age_seconds = max(0.0, (time.time() * 1000 - timestamp) / 1000)
        if age_seconds > float(max_age_seconds):
            raise RuntimeError(
                f"{symbol} order book is {age_seconds:.1f}s old; limit is "
                f"{float(max_age_seconds):.1f}s")
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            raise RuntimeError(f"{symbol} order book has no two-sided depth")
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        if (not math.isfinite(best_bid) or not math.isfinite(best_ask)
                or best_bid <= 0 or best_ask < best_bid):
            raise RuntimeError(f"{symbol} order book is invalid")
        mid = (best_bid + best_ask) / 2
        spread_pct = (best_ask - best_bid) / mid * 100
        if spread_pct > float(max_spread_pct):
            raise RuntimeError(
                f"{symbol} spread {spread_pct:.4f}% exceeds "
                f"{float(max_spread_pct):.4f}%")

        buying = side == "buy"
        if not buying and side != "sell":
            raise ValueError(f"invalid entry side: {side}")
        boundary = mid * (1 + float(max_slippage_pct) / 100) if buying \
            else mid * (1 - float(max_slippage_pct) / 100)
        levels = asks if buying else bids
        remaining = float(contracts)
        filled = cost = 0.0
        for level in levels:
            price = float(level[0])
            amount = float(level[1])
            if (not math.isfinite(price) or not math.isfinite(amount)
                    or price <= 0 or amount < 0):
                raise RuntimeError(
                    f"{symbol} order book contains non-finite depth")
            inside = price <= boundary if buying else price >= boundary
            if not inside:
                break
            take = min(remaining, amount)
            cost += take * price
            filled += take
            remaining -= take
            if remaining <= max(1e-12, contracts * 1e-9):
                break
        if remaining > max(1e-12, contracts * 1e-9):
            contract_size = float(
                self.x.market(symbol).get("contractSize") or 1)
            requested_notional = contracts * mid * contract_size
            available_notional = cost * contract_size
            raise EntryLiquidityRejected(
                f"{symbol} has only {filled:g} of {contracts:g} contracts "
                f"inside the {float(max_slippage_pct):.4f}% entry cap",
                {
                    "symbol": symbol,
                    "reason": "insufficient_depth",
                    "requested_contracts": float(contracts),
                    "available_contracts": float(filled),
                    "requested_notional_usdt": requested_notional,
                    "available_notional_usdt": available_notional,
                    "max_slippage_pct": float(max_slippage_pct),
                },
            )
        vwap = cost / filled
        limit = float(self.x.price_to_precision(symbol, boundary))
        if not math.isfinite(limit) or limit <= 0:
            raise RuntimeError(
                f"{symbol} price precision returned an invalid IOC limit")
        rounded_past_cap = ((buying and limit > boundary)
                            or (not buying and limit < boundary))
        if rounded_past_cap:
            market = self.x.market(symbol)
            tick = (market.get("precision") or {}).get("price")
            if (getattr(self.x, "precisionMode", None) == ccxt.TICK_SIZE
                    and tick and float(tick) > 0):
                limit += -float(tick) if buying else float(tick)
                limit = float(self.x.price_to_precision(symbol, limit))
            if ((buying and limit > boundary)
                    or (not buying and limit < boundary)):
                raise RuntimeError(
                    f"{symbol} price precision cannot honor the slippage cap")
        return {
            "limit_price": limit,
            "mid": mid,
            "spread_pct": spread_pct,
            "estimated_vwap": vwap,
            "estimated_slippage_pct": abs(vwap - mid) / mid * 100,
            "age_seconds": age_seconds,
        }

    def protective_orders(self, symbol: str) -> list[dict]:
        """Return regular and conditional orders, deduplicated by order ID."""
        found = []
        calls = [None] + [{"ordType": name}
                          for name in PROTECTIVE_ALGO_TYPES]
        for params in calls:
            try:
                orders = (self.retry(self.x.fetch_open_orders, symbol)
                          if params is None else
                          self.retry(self.x.fetch_open_orders, symbol, None,
                                     None, params))
                found.extend(orders or [])
            except (CredentialError, ccxt.AuthenticationError, JournalError,
                    OrderSubmissionAmbiguousError):
                raise
            except Exception:
                continue
        deduped = {}
        for order in found:
            key = str(order.get("id") or id(order))
            deduped[key] = order
        return list(deduped.values())

    @staticmethod
    def _protection_size(order: dict) -> float:
        info = order.get("info") or {}
        if str(info.get("closeFraction") or "") == "1":
            return float("inf")
        return abs(float(order.get("remaining") or order.get("amount")
                         or info.get("sz") or 0))

    @staticmethod
    def _protection_matches_position(order: dict, position_side: str) -> bool:
        """Only count orders that can reduce the position being checked."""
        info = order.get("info") or {}
        expected_side = "sell" if position_side == "long" else "buy"
        order_side = str(order.get("side") or info.get("side") or "").lower()
        if order_side != expected_side:
            return False
        reduce_raw = order.get("reduceOnly")
        if reduce_raw is None:
            reduce_raw = info.get("reduceOnly")
        reducing = (reduce_raw is True
                    or str(reduce_raw).strip().lower() in {"1", "true"}
                    or str(info.get("closeFraction") or "") == "1")
        if not reducing:
            return False
        pos_side = str(info.get("posSide") or "net").lower()
        return pos_side in {"", "net", position_side}

    def protection_status(self, symbol: str, contracts: float, side: str,
                          mark_price: float) -> dict:
        stop_size = take_size = 0.0
        stop_prices = []
        take_prices = []
        orders = self.protective_orders(symbol)
        for order in orders:
            if not self._protection_matches_position(order, side):
                continue
            info = order.get("info") or {}
            size = self._protection_size(order)
            stop_price = (order.get("stopLossPrice") or info.get("slTriggerPx"))
            take_price = (order.get("takeProfitPrice") or info.get("tpTriggerPx"))
            trigger = order.get("triggerPrice") or info.get("triggerPx")
            if stop_price not in (None, "", "0"):
                stop_size += size
                stop_prices.append(float(stop_price))
            if take_price not in (None, "", "0"):
                take_size += size
                take_prices.append(float(take_price))
            if (stop_price in (None, "", "0")
                    and take_price in (None, "", "0") and trigger):
                trigger = float(trigger)
                is_stop = ((side == "long" and trigger < mark_price)
                           or (side == "short" and trigger > mark_price))
                if is_stop:
                    stop_size += size
                    stop_prices.append(trigger)
                else:
                    take_size += size
                    take_prices.append(trigger)
        tolerance = max(1e-12, abs(contracts) * 1e-6)
        return {
            "stop_loss": stop_size + tolerance >= abs(contracts),
            "take_profit": take_size + tolerance >= abs(contracts),
            "stop_covered": stop_size,
            "take_covered": take_size,
            "stop_price": stop_prices[0] if stop_prices else None,
            "take_price": take_prices[0] if take_prices else None,
            "orders": orders,
        }

    def ensure_protection(self, symbol: str, side: str, contracts: float,
                          sl_price: float, tp_price: float,
                          mark_price: float) -> dict:
        """Create any missing reduce-only protection, then verify coverage."""
        status = self.protection_status(symbol, contracts, side, mark_price)
        opposite = "sell" if side == "long" else "buy"
        if not status["stop_loss"]:
            self._create_order_once(
                symbol, "market", opposite, contracts, None,
                {"tdMode": "cross", "reduceOnly": True,
                 "stopLossPrice": self.x.price_to_precision(symbol, sl_price)},
                "okxsl",
            )
        if not status["take_profit"]:
            try:
                self._create_order_once(
                    symbol, "market", opposite, contracts, None,
                    {"tdMode": "cross", "reduceOnly": True,
                     "takeProfitPrice": self.x.price_to_precision(symbol, tp_price)},
                    "okxtp",
                )
            except (CredentialError, ccxt.AuthenticationError, JournalError,
                    OrderSubmissionAmbiguousError):
                raise
            except Exception as exc:
                log.error("take-profit placement failed for %s: %s", symbol, exc)
                self._alert("error", "take_profit_missing",
                            f"{symbol} has no take-profit", {"error": str(exc)})
        time.sleep(0.5)
        return self.protection_status(symbol, contracts, side, mark_price)

    def settle_fill(self, fill: dict, symbol: str, side: str,
                    contracts: float, sl_price: float,
                    tp_price: float) -> dict:
        """Verify the position and its protection after ANY entry fills.

        Extracted so every path capable of creating a position runs the same
        verification: read the live position, confirm the exchange-side stop
        actually exists, alert loudly if it does not, and attach the mark and
        liquidation prices the engine needs to judge whether that stop sits
        inside the liquidation distance.

        B7.5 is why this is shared rather than inlined. A maker-first fill
        creates a position exactly as an IOC fill does, and one that skipped
        this would be a position the engine believes is protected without
        anything having checked.
        """
        position_side = "long" if side == "buy" else "short"

        def mark_unsettled(exc: Exception) -> None:
            raw_audit = (getattr(exc, "_order_audit", None)
                         or (fill.get("submission_audit")
                             if isinstance(fill, dict) else None))
            audit = dict(raw_audit) if isinstance(raw_audit, dict) else {}
            audit.update({
                "order_id": fill.get("order_id"),
                "last_fill_status": fill.get("status"),
                "outcome": "post_fill_unsettled",
            })
            setattr(exc, "_order_audit", audit)
            setattr(exc, "_post_fill_unsettled", True)

        def ambiguous(message: str, cause: Exception | None = None):
            raw_audit = (fill.get("submission_audit")
                         if isinstance(fill, dict) else None)
            audit = dict(raw_audit) if isinstance(raw_audit, dict) else {}
            audit.update({
                "order_id": fill.get("order_id")
                if isinstance(fill, dict) else None,
                "last_fill_status": fill.get("status")
                if isinstance(fill, dict) else None,
                "outcome": "post_fill_unsettled",
            })
            error = OrderSubmissionAmbiguousError(message, audit)
            mark_unsettled(error)
            if cause is None:
                raise error
            raise error from cause

        if not isinstance(fill, dict):
            ambiguous("verified entry fill is not structured")
        raw_fill_audit = fill.get("submission_audit")
        if raw_fill_audit not in (None, "") \
                and not isinstance(raw_fill_audit, dict):
            ambiguous("verified entry submission audit is not structured")
        fill_symbol = self._trimmed_identity(fill.get("symbol"))
        order_id = self._trimmed_identity(fill.get("order_id"))
        client_id = self._trimmed_identity(fill.get("client_order_id"))
        position_symbol = self._trimmed_identity(symbol)
        if (not fill_symbol or fill_symbol != position_symbol
                or not order_id or not client_id):
            ambiguous("verified entry fill identity is missing or mismatched")
        for key in ("filled", "average"):
            raw = fill.get(key)
            if isinstance(raw, bool):
                ambiguous(f"verified entry {key} is malformed")
            try:
                value = float(raw)
            except (TypeError, ValueError, OverflowError) as exc:
                ambiguous(f"verified entry {key} is malformed", exc)
            if not math.isfinite(value) or value <= 0:
                ambiguous(f"verified entry {key} is not positive and finite")
            fill[key] = value
        if not isinstance(fill.get("partial"), bool):
            ambiguous("verified entry partial flag is not boolean")

        try:
            live_position = self.position(symbol)
        except (CredentialError, ccxt.AuthenticationError, JournalError,
                OrderSubmissionAmbiguousError) as exc:
            mark_unsettled(exc)
            raise
        except Exception as exc:
            ambiguous(
                f"verified fill exists but the live position read failed: {exc}",
                exc)
        if not isinstance(live_position, dict):
            ambiguous("verified fill exists but no structured live position "
                      "was returned")
        live_symbol = self._trimmed_identity(live_position.get("symbol"))
        live_info = live_position.get("info")
        if live_info not in (None, "") and not isinstance(live_info, dict):
            ambiguous("live position metadata is not structured")
        live_info = live_info or {}
        live_inst = self._trimmed_identity(live_info.get("instId"))
        if live_symbol and live_symbol != position_symbol:
            ambiguous("live position symbol does not match the filled entry")
        if live_inst:
            expected_inst = None
            try:
                market = self.x.market(symbol)
                if isinstance(market, dict):
                    expected_inst = self._trimmed_identity(market.get("id"))
            except (CredentialError, ccxt.AuthenticationError, JournalError,
                    OrderSubmissionAmbiguousError) as exc:
                mark_unsettled(exc)
                raise
            except Exception as exc:
                ambiguous(f"position instrument metadata is unavailable: {exc}",
                          exc)
            if live_inst not in {position_symbol, expected_inst}:
                ambiguous("live position instrument does not match the filled "
                          "entry")
        raw_contracts = live_position.get("contracts")
        if isinstance(raw_contracts, bool):
            ambiguous("live position contracts are malformed")
        try:
            live_contracts = abs(float(raw_contracts))
        except (TypeError, ValueError, OverflowError) as exc:
            ambiguous("live position contracts are malformed", exc)
        if not math.isfinite(live_contracts) or live_contracts <= 0:
            ambiguous("live position contracts are not positive and finite")
        tolerance = max(1e-12, fill["filled"] * 1e-6)
        if abs(live_contracts - fill["filled"]) > tolerance:
            ambiguous("live position contracts do not match the verified fill")
        raw_mark = live_position.get("markPrice")
        if isinstance(raw_mark, bool):
            ambiguous("live position mark price is malformed")
        try:
            mark = float(raw_mark)
        except (TypeError, ValueError, OverflowError) as exc:
            ambiguous("live position mark price is malformed", exc)
        if not math.isfinite(mark) or mark <= 0:
            ambiguous("live position mark price is not positive and finite")
        position_id = self._trimmed_identity(
            live_position.get("id") or live_info.get("posId"))
        if not position_id:
            ambiguous("live position has no position identity")
        # Attached algo orders can be eventually consistent on the read API.
        time.sleep(0.4)
        try:
            protection = self.ensure_protection(
                symbol, position_side, live_contracts, sl_price, tp_price, mark)
        except (CredentialError, ccxt.AuthenticationError, JournalError,
                OrderSubmissionAmbiguousError) as exc:
            mark_unsettled(exc)
            raise
        except Exception as e:
            ambiguous(
                f"verified fill exists but protection verification failed: {e}",
                e)
        if not isinstance(protection, dict):
            ambiguous("entry protection status is not structured")
        if (not isinstance(protection.get("stop_loss"), bool)
                or not isinstance(protection.get("take_profit"), bool)):
            ambiguous("entry protection flags are not exact booleans")

        if not protection.get("stop_loss"):
            try:
                self._alert(
                    "critical", "unprotected_position",
                    f"{symbol} entry filled without a verified stop-loss",
                    {"contracts": live_contracts})
            except (CredentialError, ccxt.AuthenticationError, JournalError,
                    OrderSubmissionAmbiguousError) as exc:
                mark_unsettled(exc)
                raise
            except Exception as exc:
                ambiguous(
                    f"verified fill exists but the unprotected-position "
                    f"alert failed: {exc}", exc)
        if fill["partial"]:
            log.warning("PARTIAL ENTRY %s: requested %s, filled %s", symbol,
                        contracts, fill["filled"])
            try:
                self._alert(
                    "warning", "partial_entry",
                    f"{symbol} entry partially filled",
                    {"requested": contracts, "filled": fill["filled"]})
            except (CredentialError, ccxt.AuthenticationError, JournalError,
                    OrderSubmissionAmbiguousError) as exc:
                mark_unsettled(exc)
                raise
            except Exception as exc:
                ambiguous(
                    f"verified partial fill exists but its alert failed: {exc}",
                    exc)
        fill["protection"] = protection
        fill["position_contracts"] = live_contracts
        fill["position_id"] = position_id
        liquidation_raw = (
            live_position.get("liquidationPrice") or live_info.get("liqPx")
        )
        if liquidation_raw in (None, "", "0", 0):
            liquidation_price = None
        else:
            try:
                liquidation_price = float(liquidation_raw)
            except (TypeError, ValueError, OverflowError) as exc:
                ambiguous("live position liquidation price is malformed", exc)
            if not math.isfinite(liquidation_price) or liquidation_price <= 0:
                ambiguous("live position liquidation price is invalid")
        fill["liquidation_price"] = liquidation_price
        fill["mark_price"] = mark
        return fill

    def maker_first_entry(self, symbol: str, side: str, contracts: float,
                          leverage: float, sl_price: float, tp_price: float,
                          wait_seconds: float,
                          reference_price: float) -> dict:
        """Post passively for ``wait_seconds``, then get out of the way.

        Maker-first entry study. Every IOC entry crosses the spread and accepts adverse
        selection: you buy at the moment a seller wants to sell to you. At a
        2% stop, round-trip friction is roughly 10% of the risk unit, so
        converting the filled fraction from taker to maker moves expectancy
        by more than most of the parameter axes queued for sweeping - and it
        does so without requiring any signal to have edge.

        Fill rate is not knowable from history, because the passive order was
        never there. This is a live experiment and nothing else.

        **The order is never left resting.** Every exit from this method
        either returns a filled position or has cancelled the order. If the
        cancel fails, the fill state is re-read and reported rather than
        assumed, because an abandoned resting order is an unmanaged position
        waiting to happen.

        Protection is unchanged: stop-loss and take-profit are attached to
        the order server-side exactly as the IOC path attaches them, so a
        fill arrives already protected.
        """
        # A prior maker attempt may have failed before its crossing fallback
        # consumed the correlation. Never let that old group attach to this
        # fresh symbol/attempt; a successfully submitted maker order below
        # installs its own group for the immediate fallback path.
        self._clear_pending_entry()

        def block(message: str, *, outcome: str,
                  cause: Exception | None = None,
                  audit: dict | None = None):
            """Clear the hand-off and raise a classified blocking error."""
            self._clear_pending_entry()
            details = dict(audit or {}) if isinstance(audit, dict) else {}
            details.update({"outcome": outcome, "symbol": symbol})
            error = MakerFirstAmbiguousError(
                message, details)
            if cause is None:
                raise error
            raise error from cause

        def pre_submit_block(message: str, *, outcome: str,
                             cause: Exception | None = None):
            """Reject this attempt without implying an order may exist."""
            self._clear_pending_entry()
            error = MakerFirstPreSubmitError(
                message, {"outcome": outcome, "symbol": symbol})
            if cause is None:
                raise error
            raise error from cause

        try:
            requested = float(contracts)
        except (TypeError, ValueError, OverflowError) as exc:
            pre_submit_block(
                "maker-first requested contracts are malformed",
                outcome="invalid_requested_contracts", cause=exc)
        if not math.isfinite(requested) or requested <= 0:
            pre_submit_block(
                "maker-first requested contracts are non-positive or non-finite",
                outcome="invalid_requested_contracts")

        # Nothing has been submitted yet, but pre-submit errors are still a
        # hard stop for this attempt.  The engine must not turn an unknown
        # maker-path failure into an immediate crossing order.
        try:
            self.retry(self.x.set_leverage, int(leverage), symbol,
                       {"mgnMode": "cross"})
            book = self.retry(self.x.fetch_order_book, symbol, 5)
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            if not bids or not asks:
                raise RuntimeError(
                    f"{symbol} order book has no two-sided depth")
        except (CredentialError, ccxt.AuthenticationError):
            raise
        except Exception as exc:                       # noqa: BLE001
            pre_submit_block(
                "maker-first pre-submit failure blocks this attempt",
                outcome="pre_submit_exchange_failure", cause=exc)

        # Join the near touch rather than improving it: improving would cross
        # into the spread we are trying to capture.
        try:
            passive = (float(bids[0][0]) if side == "buy"
                       else float(asks[0][0]))
            limit = float(self.x.price_to_precision(symbol, passive))
            if not math.isfinite(limit) or limit <= 0:
                raise ValueError("maker-first passive price is invalid")
        except Exception as exc:                       # noqa: BLE001
            pre_submit_block(
                "maker-first passive price is malformed",
                outcome="invalid_passive_price", cause=exc)

        try:
            params = {
                "tdMode": "cross",
                "stopLoss": {"triggerPrice":
                             self.x.price_to_precision(symbol, sl_price)},
                "takeProfit": {"triggerPrice":
                               self.x.price_to_precision(symbol, tp_price)},
                # Post-only: if it would cross, it is rejected rather than
                # silently becoming the taker order this method exists to avoid.
                "postOnly": True,
            }
        except (CredentialError, ccxt.AuthenticationError):
            raise
        except Exception as exc:                       # noqa: BLE001
            pre_submit_block(
                "maker-first protection prices are malformed",
                outcome="invalid_protection_price", cause=exc)
        # Order creation is deliberately single-shot. A generic retry can
        # submit the same passive order twice after an ambiguous timeout,
        # leaving duplicate exposure when the crossing fallback runs.
        try:
            order = self._create_order_once(
                symbol, "limit", side, contracts, limit, params, "okxmk",
                )
        except (CredentialError, ccxt.AuthenticationError):
            self._clear_pending_entry()
            raise
        except Exception as exc:                       # noqa: BLE001
            block("maker-first order submission failed; fallback blocked",
                  outcome="ambiguous_submit", cause=exc,
                  audit=getattr(exc, "_order_audit", None))
        if not isinstance(order, dict):
            block("maker-first order acknowledgement is not a mapping",
                  outcome="ambiguous_ack")
        order_id = order.get("id")
        if not order_id:
            raw_audit = order.get("_submission_audit")
            if raw_audit not in (None, "") and not isinstance(raw_audit, dict):
                block("maker-first order audit is malformed",
                      outcome="ambiguous_ack")
            audit = dict(raw_audit or {})
            try:
                recovered = self._recover_order(
                    symbol, str(audit.get("client_order_id") or ""))
            except Exception as exc:                   # noqa: BLE001
                block("maker-first order recovery failed; fallback blocked",
                      outcome="ambiguous_recovery", cause=exc,
                      audit=getattr(exc, "_order_audit", None))
            if recovered:
                if not isinstance(recovered, dict):
                    block("maker-first recovered acknowledgement is malformed",
                          outcome="ambiguous_recovery")
                recovered["_submission_audit"] = {
                    **audit, "outcome": "recovered_missing_order_id",
                    "recovery_attempted": True,
                }
                order = recovered
                order_id = order.get("id")
        if not order_id:
            audit = dict(order.get("_submission_audit") or {})
            audit.update({
                "outcome": "ambiguous_unrecovered",
                "recovery_attempted": True,
            })
            self._clear_pending_entry()
            raise MakerFirstAmbiguousError(
                "maker-first order acknowledgement has no exchange order id",
                audit)
        try:
            order_id = str(order_id).strip()
        except Exception as exc:                           # noqa: BLE001
            block("maker-first order id is malformed",
                  outcome="ambiguous_ack", cause=exc)
        if not order_id:
            block("maker-first order acknowledgement has an empty order id",
                  outcome="ambiguous_ack")
        maker_audit = order.get("_submission_audit")
        if maker_audit not in (None, "") and not isinstance(maker_audit, dict):
            block("maker-first order audit is malformed",
                  outcome="ambiguous_ack")
        maker_client_id = self._trimmed_identity(
            (maker_audit or {}).get("client_order_id"))

        try:
            wait_value = float(wait_seconds)
            if not math.isfinite(wait_value) or wait_value < 0:
                raise ValueError("maker-first wait duration is invalid")
            reference_value = float(reference_price)
            if not math.isfinite(reference_value) or reference_value <= 0:
                raise ValueError("maker-first reference price is invalid")
            deadline = time.time() + wait_value
        except (TypeError, ValueError, OverflowError) as exc:
            block("maker-first wait duration is malformed",
                  outcome="ambiguous_post_ack", cause=exc)

        def read_number(value, *, label: str, context: str) -> float:
            if isinstance(value, bool):
                block(f"maker-first {context} {label} is malformed",
                      outcome="ambiguous_quantity")
            try:
                result = float(value)
            except (TypeError, ValueError, OverflowError) as exc:
                block(f"maker-first {context} {label} is malformed",
                      outcome="ambiguous_quantity", cause=exc)
            if not math.isfinite(result):
                block(f"maker-first {context} {label} is not finite",
                      outcome="ambiguous_quantity")
            return result

        def order_evidence(value: object, *, context: str,
                           allow_missing_filled: bool = False):
            """Return strict cumulative fill evidence from one order read."""
            if not isinstance(value, dict):
                block(f"maker-first {context} order state is not a mapping",
                      outcome="ambiguous_quantity")
            info = value.get("info")
            info = info if isinstance(info, dict) else {}

            top_raw = value.get("filled")
            acc_raw = info.get("accFillSz")
            last_raw = info.get("fillSz")
            if top_raw not in (None, ""):
                filled_value = read_number(
                    top_raw, label="filled quantity", context=context)
                fill_source = "filled"
                # accFillSz is cumulative exchange evidence. If CCXT's
                # projection disagrees with it, neither is safe to use for a
                # crossing decision.
                if acc_raw not in (None, ""):
                    accumulated = read_number(
                        acc_raw, label="accumulated fill quantity",
                        context=context)
                    if accumulated != filled_value:
                        block(
                            f"maker-first {context} fill quantities disagree",
                            outcome="ambiguous_quantity")
            elif acc_raw not in (None, ""):
                filled_value = read_number(
                    acc_raw, label="accumulated fill quantity",
                    context=context)
                fill_source = "info.accFillSz"
            elif last_raw not in (None, ""):
                filled_value = read_number(
                    last_raw, label="fill quantity", context=context)
                fill_source = "info.fillSz"
            elif allow_missing_filled:
                # Only the initial create acknowledgement may provisionally
                # omit quantity. No terminal/cancel decision uses this zero.
                filled_value = 0.0
                fill_source = None
            else:
                block(
                    f"maker-first {context} has no explicit fill quantity",
                    outcome="ambiguous_quantity")

            if filled_value < 0 or filled_value > requested:
                block(f"maker-first {context} fill state is invalid",
                      outcome="ambiguous_quantity")

            remaining_raw = value.get("remaining")
            if remaining_raw in (None, ""):
                remaining_raw = info.get("remainSz")
            if remaining_raw not in (None, ""):
                remaining = read_number(
                    remaining_raw, label="remaining quantity",
                    context=context)
                if remaining < 0 or remaining > requested:
                    block(
                        f"maker-first {context} remaining state is invalid",
                        outcome="ambiguous_quantity")
                tolerance = max(1e-12, abs(requested) * 1e-12)
                if not math.isclose(
                        filled_value + remaining, requested,
                        rel_tol=0.0, abs_tol=tolerance):
                    block(
                        f"maker-first {context} fill and remaining disagree",
                        outcome="ambiguous_quantity")

            average_raw = value.get("average")
            if average_raw in (None, ""):
                average_raw = info.get("avgPx")
            if average_raw in (None, ""):
                average_raw = info.get("fillPx")
            average_value = None
            if filled_value > 0:
                if average_raw in (None, ""):
                    block(
                        f"maker-first {context} has no explicit fill average",
                        outcome="ambiguous_quantity")
                average_value = read_number(
                    average_raw, label="fill average", context=context)
                if average_value <= 0:
                    block(f"maker-first {context} average is invalid",
                          outcome="ambiguous_quantity")
            return filled_value, average_value, fill_source

        def verify_read_identity(value: dict, *, context: str) -> None:
            candidate = value.get("id")
            if candidate in (None, ""):
                block(f"maker-first {context} has no order id",
                      outcome="ambiguous_order_identity")
            try:
                candidate = str(candidate).strip()
            except Exception as exc:                       # noqa: BLE001
                block(f"maker-first {context} order id is malformed",
                      outcome="ambiguous_order_identity", cause=exc)
            if not candidate or candidate != order_id:
                block(f"maker-first {context} order id does not match",
                      outcome="ambiguous_order_identity")

        filled, average, quantity_source = order_evidence(
            order, context="ack", allow_missing_filled=True)
        while time.time() < deadline and filled < requested:
            time.sleep(min(1.0, max(0.05, deadline - time.time())))
            try:
                polled = self.retry(self.x.fetch_order, order_id, symbol)
            except (CredentialError, ccxt.AuthenticationError):
                self._clear_pending_entry()
                raise
            except Exception as exc:                       # noqa: BLE001
                block("maker-first fill poll failed; fallback blocked",
                      outcome="ambiguous_post_ack", cause=exc)
            if not isinstance(polled, dict):
                block("maker-first fill poll returned a non-mapping",
                      outcome="ambiguous_post_ack")
            verify_read_identity(polled, context="poll")
            order = polled
            filled, average, quantity_source = order_evidence(
                order, context="poll")

        # A full fill is terminal independently of cancellation. For zero or
        # partial fills, a successful cancel response is only a request; the
        # post-cancel read must expose an explicit terminal non-live status.
        cancelled = False
        cancel_confirmed = False
        status = ""
        if filled < requested:
            try:
                self.retry(self.x.cancel_order, order_id, symbol)
            except (CredentialError, ccxt.AuthenticationError):
                self._clear_pending_entry()
                raise
            except Exception as exc:                       # noqa: BLE001
                log.warning("maker-first cancel failed for %s: %s",
                            symbol, exc)
            try:
                post_cancel = self.retry(self.x.fetch_order, order_id, symbol)
            except (CredentialError, ccxt.AuthenticationError):
                self._clear_pending_entry()
                raise
            except Exception as exc:                       # noqa: BLE001
                block("maker-first post-cancel read failed; fallback blocked",
                      outcome="ambiguous_cancel_confirmation", cause=exc)
            if not isinstance(post_cancel, dict):
                block("maker-first post-cancel read returned a non-mapping",
                      outcome="ambiguous_cancel_confirmation")
            verify_read_identity(post_cancel, context="post-cancel")
            order = post_cancel
            filled, average, quantity_source = order_evidence(
                order, context="post-cancel")
            try:
                info = order.get("info")
                info = info if isinstance(info, dict) else {}
                status = str(order.get("status") or info.get("state")
                             or "").strip().lower()
            except Exception as exc:                       # noqa: BLE001
                block("maker-first cancellation status is malformed",
                      outcome="ambiguous_cancel_confirmation", cause=exc)
            terminal = {"canceled", "cancelled", "expired", "rejected"}
            cancel_confirmed = bool(status in terminal)
            cancelled = bool(filled < requested and cancel_confirmed)
        if filled >= requested:
            # A race can turn a partial/zero state into a full fill while the
            # cancel is in flight. Full fill remains terminal and is settled.
            filled = requested
            cancelled = False
            cancel_confirmed = False

        # A passive fill is a position, so it goes through exactly the same
        # verification an IOC fill does. Settlement failures are an ambiguity
        # boundary and can never leak a fallback hand-off.
        settled = None
        if filled > 0 and (filled >= requested or cancelled):
            self._clear_pending_entry()
            if not maker_client_id:
                block("maker-first filled request has no client order identity",
                      outcome="ambiguous_settlement")
            try:
                settled = self.settle_fill(
                    {"symbol": symbol,
                     "filled": float(filled),
                     "average": average,
                     "requested": requested,
                     "partial": float(filled) < requested,
                     "order_id": order_id,
                     "client_order_id": (order.get("clientOrderId")
                                         or ((order.get("info") or {}).get(
                                             "clOrdId")
                                             if isinstance(order.get("info"),
                                                           dict) else None)
                                         or maker_client_id),
                     "status": order.get("status"),
                     "fee_usd": 0.0, "slippage_usd": 0.0,
                     "submission_audit": {"path": "maker_first"}},
                    symbol, side, requested, sl_price, tp_price)
            except (CredentialError, ccxt.AuthenticationError) as exc:
                setattr(exc, "_order_audit", {
                    "outcome": "ambiguous_settlement",
                    "symbol": symbol,
                    "order_id": order_id,
                })
                raise
            except (JournalError, OrderSubmissionAmbiguousError):
                raise
            except Exception as exc:                       # noqa: BLE001
                block("maker-first settlement failed; fallback blocked",
                      outcome="ambiguous_settlement", cause=exc)
            if not isinstance(settled, dict):
                block("maker-first settlement returned a non-mapping",
                      outcome="ambiguous_settlement")
        elif filled > 0:
            # A partial order whose cancellation is not proven may still be
            # live or may race into another fill. Do not treat it as a normal
            # completed entry; reconciliation owns any later position read.
            self._clear_pending_entry()

        # The hand-off is deliberately installed at the last possible point:
        # only an explicit zero-fill and an explicit terminal cancellation can
        # authorize the crossing fallback. Partial and full fills always clear
        # it, even when a partial was confirmed cancelled.
        self._clear_pending_entry()

        slippage_saved_pct = None
        if average is not None and reference_price:
            try:
                direction = 1.0 if side == "buy" else -1.0
                # Positive means the passive fill beat the reference.
                slippage_saved_pct = round(
                    direction * (reference_value - average)
                    / reference_value * 100, 6)
            except Exception as exc:                       # noqa: BLE001
                block("maker-first counterfactual is malformed",
                      outcome="ambiguous_post_ack", cause=exc)

        return {
            "symbol": symbol,
            "order_id": order_id,
            "requested_contracts": requested,
            "filled_contracts": float(filled),
            "fill_rate": (float(filled) / requested if requested else 0.0),
            "limit_price": limit,
            "average_price": average,
            "reference_price": reference_value,
            "ioc_counterfactual_pct": slippage_saved_pct,
            "wait_seconds": wait_value,
            "cancelled": cancelled,
            "resting": bool(filled < requested and not cancelled),
            "blocking": bool(filled < requested and not cancelled),
            "cancellation_status": status or None,
            "cancellation_confirmed": bool(cancel_confirmed),
            "cancellation_order_id": (
                order_id if cancel_confirmed else None),
            "quantity_evidence": quantity_source,
            "blocking_reason": (
                None if (filled >= requested or cancelled)
                else "cancel_confirmation_uncertain"),
            "submission_audit": (
                None if (filled >= requested or cancelled)
                else {"outcome": "ambiguous_cancel_confirmation",
                      "symbol": symbol}),
            # The settled fill, shaped exactly like open_position's return so
            # the engine's own post-fill bookkeeping can consume it unchanged.
            # None when nothing filled, which is the ordinary outcome.
            "execution": settled,
        }

    def open_position(self, symbol: str, side: str, contracts: float,
                      leverage: float, sl_price: float, tp_price: float,
                      expected_price: float | None = None,
                      entry_limit_price: float | None = None) -> dict:
        # Consume the maker hand-off at absolute method entry.  Leverage,
        # precision and request construction can all raise; none of those
        # failures may let a one-shot group leak into a later symbol/attempt.
        self._clear_pending_entry()
        try:
            self.retry(self.x.set_leverage, int(leverage), symbol,
                       {"mgnMode": "cross"})
        except CredentialError:
            raise
        except ccxt.ExchangeError as e:
            raise self._entry_order_rejection(
                symbol, "set_leverage", e) from e
        except Exception as e:
            raise RuntimeError(f"set_leverage failed for {symbol}: {e}") from e

        sl = self.x.price_to_precision(symbol, sl_price)
        tp = self.x.price_to_precision(symbol, tp_price)
        params = {
            "tdMode": "cross",
            "stopLoss": {"triggerPrice": sl},
            "takeProfit": {"triggerPrice": tp},
        }
        order_type = "limit" if entry_limit_price is not None else "market"
        order_price = (self.x.price_to_precision(symbol, entry_limit_price)
                       if entry_limit_price is not None else None)
        if entry_limit_price is not None:
            params["timeInForce"] = "IOC"
        try:
            # Preferred path: entry with SL/TP attached server-side.
            order = self._create_order_once(
                symbol, order_type, side, contracts, order_price, params,
                "okxent")
        except ccxt.ExchangeError as e:
            raise self._entry_order_rejection(
                symbol, "attached_entry", e) from e

        fill = self.verify_fill(
            order, symbol, contracts, expected_price, side=side)
        return self.settle_fill(
            fill, symbol, side, contracts, sl_price, tp_price)

    @classmethod
    def _validated_close_execution(cls, execution: object,
                                   requested: object,
                                   symbol: str) -> dict:
        """Return a close result only when its quantity proof is exact."""
        audit = {}
        if isinstance(execution, dict):
            raw_audit = execution.get("submission_audit")
            if isinstance(raw_audit, dict):
                audit.update(raw_audit)
            audit.update({
                "order_id": execution.get("order_id"),
                "last_fill_status": execution.get("status"),
                "outcome": "close_result_ambiguous",
            })

        def fail(message: str):
            raise OrderSubmissionAmbiguousError(
                f"close result for {symbol} is ambiguous: {message}", audit)

        if not isinstance(execution, dict):
            fail("result is not structured")
        for key in ("order_id", "client_order_id"):
            identity = cls._trimmed_identity(execution.get(key))
            if not identity:
                fail(f"{key} is missing or malformed")
            execution[key] = identity
        status = execution.get("status")
        if not isinstance(status, str) or not status.strip():
            fail("status is missing or malformed")
        execution["status"] = status.strip().lower()
        if isinstance(requested, bool):
            fail("requested contracts are malformed")
        try:
            requested_value = float(requested)
        except (TypeError, ValueError, OverflowError):
            fail("requested contracts are malformed")
        if not math.isfinite(requested_value) or requested_value <= 0:
            fail("requested contracts are not positive and finite")
        for key in ("filled", "average", "remaining_contracts"):
            raw = execution.get(key)
            if raw in (None, "") or isinstance(raw, bool):
                fail(f"{key} is missing or malformed")
            try:
                value = float(raw)
            except (TypeError, ValueError, OverflowError):
                fail(f"{key} is malformed")
            if not math.isfinite(value):
                fail(f"{key} is not finite")
            execution[key] = value
        filled = execution["filled"]
        average = execution["average"]
        remaining = execution["remaining_contracts"]
        tolerance = max(1e-12, requested_value * 1e-6)
        if filled <= 0 or filled > requested_value + tolerance:
            fail("filled contracts are outside the requested close")
        if average <= 0:
            fail("average price is not positive")
        if remaining < 0 or remaining > requested_value + tolerance:
            fail("remaining contracts are outside the original position")
        if abs(filled + remaining - requested_value) > tolerance:
            fail("filled and remaining contracts disagree with the request")
        fully_closed = execution.get("fully_closed")
        if not isinstance(fully_closed, bool):
            fail("fully_closed is not an exact boolean")
        expected_closed = remaining <= tolerance
        if fully_closed is not expected_closed:
            fail("fully_closed conflicts with remaining contracts")
        for key in ("fee_usd", "slippage_usd", "adverse_slippage_usd"):
            raw = execution.get(key)
            if raw in (None, "") or isinstance(raw, bool):
                fail(f"{key} is missing or malformed")
            try:
                value = float(raw)
            except (TypeError, ValueError, OverflowError):
                fail(f"{key} is malformed")
            if not math.isfinite(value):
                fail(f"{key} is not finite")
            execution[key] = value
        execution["requested"] = requested_value
        return execution

    def close_position(self, pos: dict) -> dict:
        symbol = pos["symbol"]
        raw_contracts = pos.get("contracts")
        if isinstance(raw_contracts, bool):
            raise OrderSubmissionAmbiguousError(
                f"close position contracts for {symbol} are malformed")
        try:
            contracts = abs(float(raw_contracts))
        except (TypeError, ValueError, OverflowError) as exc:
            raise OrderSubmissionAmbiguousError(
                f"close position contracts for {symbol} are malformed"
            ) from exc
        if not math.isfinite(contracts) or contracts <= 0:
            raise OrderSubmissionAmbiguousError(
                f"close position contracts for {symbol} are not positive "
                "and finite")
        position_side = str(pos.get("side") or "").lower()
        if position_side not in {"long", "short"}:
            raw = float((pos.get("info") or {}).get("pos") or 0)
            position_side = "long" if raw >= 0 else "short"
        side = "sell" if position_side == "long" else "buy"
        # Close FIRST, cancel protection after. If the close order fails the
        # position still has its stop-loss; and leftover reduce-only SL/TP
        # orders on a now-flat position can never open new exposure (they
        # simply fail if triggered), so late cancellation is harmless.
        params = {"tdMode": "cross", "reduceOnly": True}
        raw_pos_side = str((pos.get("info") or {}).get("posSide") or "").lower()
        if raw_pos_side in {"long", "short"}:
            # Emergency flatten remains available if an operator changed the
            # account to hedge mode after startup.
            params["posSide"] = raw_pos_side
        order = self._create_order_once(
            symbol, "market", side, contracts, None,
            params, "okxcls")
        result = self.verify_fill(
            order, symbol, contracts,
            float(pos.get("markPrice") or pos.get("last") or 0) or None,
            side=side)
        try:
            remaining = self.position(symbol, position_side)
        except (CredentialError, ccxt.AuthenticationError, JournalError,
                OrderSubmissionAmbiguousError):
            raise
        except Exception as exc:
            audit = dict(result.get("submission_audit") or {})
            audit.update({
                "order_id": result.get("order_id"),
                "last_fill_status": result.get("status"),
                "outcome": "close_position_read_ambiguous",
            })
            raise OrderSubmissionAmbiguousError(
                f"close fill for {symbol} was verified but the remaining "
                f"position could not be read: {exc}", audit) from exc
        if remaining is not None and not isinstance(remaining, dict):
            audit = dict(result.get("submission_audit") or {})
            audit.update({
                "order_id": result.get("order_id"),
                "last_fill_status": result.get("status"),
                "outcome": "close_position_read_ambiguous",
            })
            raise OrderSubmissionAmbiguousError(
                f"close fill for {symbol} was verified but the remaining "
                "position response was malformed", audit)
        if remaining is None:
            remaining_raw = 0
        elif "contracts" not in remaining:
            remaining_raw = float("nan")
        else:
            remaining_symbol = self._trimmed_identity(remaining.get("symbol"))
            if remaining_symbol and remaining_symbol != symbol:
                remaining_raw = float("nan")
            else:
                remaining_raw = remaining.get("contracts")
        if isinstance(remaining_raw, bool):
            remaining_raw = float("nan")
        try:
            remaining_contracts = abs(float(remaining_raw))
        except (TypeError, ValueError, OverflowError):
            remaining_contracts = float("nan")
        if not math.isfinite(remaining_contracts):
            audit = dict(result.get("submission_audit") or {})
            audit.update({
                "order_id": result.get("order_id"),
                "last_fill_status": result.get("status"),
                "outcome": "close_position_read_ambiguous",
            })
            raise OrderSubmissionAmbiguousError(
                f"close fill for {symbol} was verified but remaining "
                "contracts were malformed", audit)
        result["fully_closed"] = remaining_contracts <= max(1e-12, contracts * 1e-6)
        result["remaining_contracts"] = remaining_contracts
        result = self._validated_close_execution(result, contracts, symbol)
        if result["fully_closed"]:
            try:
                positions = self.positions()
            except (CredentialError, ccxt.AuthenticationError, JournalError,
                    OrderSubmissionAmbiguousError):
                raise
            except Exception as exc:
                audit = dict(result.get("submission_audit") or {})
                audit.update({
                    "order_id": result.get("order_id"),
                    "last_fill_status": result.get("status"),
                    "outcome": "close_account_read_ambiguous",
                })
                raise OrderSubmissionAmbiguousError(
                    f"close fill for {symbol} was verified but account "
                    f"positions could not be reconciled: {exc}", audit) from exc
            if not any(p.get("symbol") == symbol for p in positions):
                self.cancel_symbol(symbol)
        else:
            log.error("close for %s left %s contracts; protection retained",
                      symbol, remaining_contracts)
            self._alert("error", "partial_close",
                        f"{symbol} close left an open remainder",
                        {"remaining_contracts": remaining_contracts})
        return result

    def cancel_symbol(self, symbol: str) -> None:
        """Cancel every regular and conditional (algo) order on one symbol."""
        try:
            for o in self.retry(self.x.fetch_open_orders, symbol) or []:
                try:
                    self.x.cancel_order(o["id"], symbol)
                except (CredentialError, ccxt.AuthenticationError,
                        JournalError, OrderSubmissionAmbiguousError):
                    raise
                except Exception as e:
                    log.warning("cancel %s: %s", o.get("id"), e)
        except (CredentialError, ccxt.AuthenticationError, JournalError,
                OrderSubmissionAmbiguousError):
            raise
        except Exception as e:
            log.debug("fetch_open_orders(%s): %s", symbol, e)
        for order_type in ALL_ALGO_TYPES:
            query = {"ordType": order_type}
            cancel_params = {"trigger": True}
            try:
                algos = self.x.fetch_open_orders(
                    symbol, None, None, query) or []
                for o in algos:
                    try:
                        self.x.cancel_order(o["id"], symbol, cancel_params)
                    except (CredentialError, ccxt.AuthenticationError,
                            JournalError, OrderSubmissionAmbiguousError):
                        raise
                    except Exception as e:
                        log.warning("cancel algo %s: %s", o.get("id"), e)
            except (CredentialError, ccxt.AuthenticationError, JournalError,
                    OrderSubmissionAmbiguousError):
                raise
            except Exception:
                continue

    def cancel_everything(self) -> None:
        """Cancel and then verify every regular and algo order is gone."""
        failures = []
        try:
            for o in self.retry(self.x.fetch_open_orders) or []:
                try:
                    self.x.cancel_order(o["id"], o.get("symbol"))
                except (CredentialError, ccxt.AuthenticationError,
                        JournalError, OrderSubmissionAmbiguousError):
                    raise
                except Exception as e:
                    log.warning("cancel %s: %s", o.get("id"), e)
                    failures.append(f"{o.get('id')}: {e}")
        except (CredentialError, ccxt.AuthenticationError, JournalError,
                OrderSubmissionAmbiguousError):
            raise
        except Exception as e:
            failures.append(f"regular-order query: {e}")
        for order_type in ALL_ALGO_TYPES:
            query = {"ordType": order_type}
            cancel_params = {"trigger": True}
            try:
                algos = self.x.fetch_open_orders(
                    None, None, None, query) or []
                for o in algos:
                    try:
                        self.x.cancel_order(
                            o["id"], o.get("symbol"), cancel_params)
                    except (CredentialError, ccxt.AuthenticationError,
                            JournalError, OrderSubmissionAmbiguousError):
                        raise
                    except Exception as e:
                        log.warning("cancel algo %s: %s", o.get("id"), e)
                        failures.append(f"algo {o.get('id')}: {e}")
            except (CredentialError, ccxt.AuthenticationError, JournalError,
                    OrderSubmissionAmbiguousError):
                raise
            except Exception as exc:
                failures.append(f"{order_type}-order query: {exc}")

        time.sleep(0.5)
        remaining = []
        try:
            remaining.extend(self.retry(self.x.fetch_open_orders) or [])
        except (CredentialError, ccxt.AuthenticationError, JournalError,
                OrderSubmissionAmbiguousError):
            raise
        except Exception as exc:
            failures.append(f"regular-order verification: {exc}")
        for order_type in ALL_ALGO_TYPES:
            try:
                rows = self.x.fetch_open_orders(
                    None, None, None, {"ordType": order_type}) or []
                remaining.extend(rows)
            except (CredentialError, ccxt.AuthenticationError, JournalError,
                    OrderSubmissionAmbiguousError):
                raise
            except Exception as exc:
                failures.append(f"{order_type}-order verification: {exc}")
        remaining_ids = sorted({str(order.get("id") or "unknown")
                                for order in remaining})
        if failures or remaining_ids:
            details = "; ".join(failures)
            if remaining_ids:
                details += ("; " if details else "") + (
                    "still open: " + ", ".join(remaining_ids))
            raise RuntimeError(f"order cancellation could not be verified: {details}")

    def closed_position_summary(self, symbol: str, since_ms: int,
                                direction: str, entry_price: float,
                                quantity: float) -> dict:
        """Recover actual close price, fees, funding and PnL after SL/TP exit."""
        def finite_number(value: object, *, positive: bool = False):
            if value in (None, "") or isinstance(value, bool):
                return None
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            if not math.isfinite(number) or (positive and number <= 0):
                return None
            return number

        if self.x.has.get("fetchPositionsHistory"):
            try:
                rows = self.retry(self.x.fetch_positions_history,
                                  [symbol], since_ms, 100) or []
                valid_rows = []
                for row in rows:
                    if not isinstance(row, dict) or row.get("symbol") != symbol:
                        continue
                    info = row.get("info")
                    if info not in (None, "") and not isinstance(info, dict):
                        continue
                    info = info or {}
                    price = finite_number(
                        row.get("exitPrice")
                        if row.get("exitPrice") not in (None, "")
                        else info.get("closeAvgPx")
                        if info.get("closeAvgPx") not in (None, "")
                        else info.get("avgPx"), positive=True)
                    qty = finite_number(info.get("closeTotalPos"), positive=True)
                    if price is None or qty is None:
                        continue
                    realized = finite_number(
                        row.get("realizedPnl")
                        if row.get("realizedPnl") not in (None, "")
                        else info.get("realizedPnl"))
                    fee_raw = finite_number(info.get("fee"))
                    funding = finite_number(info.get("fundingFee"))
                    if realized is None:
                        realized = 0.0
                    if fee_raw is None:
                        fee_raw = 0.0
                    if funding is None:
                        funding = 0.0
                    timestamp = finite_number(row.get("timestamp")) or 0.0
                    valid_rows.append((timestamp, price, qty, realized,
                                       fee_raw, funding))
                if valid_rows:
                    _, price, qty, realized, fee_raw, funding = max(
                        valid_rows, key=lambda item: item[0])
                    return {
                        "symbol": symbol,
                        "price": price,
                        "qty": abs(qty),
                        "fee_usd": abs(fee_raw),
                        "funding_usd": funding,
                        "realized_pnl_usd": realized,
                        "status": "position_history",
                    }
            except (CredentialError, ccxt.AuthenticationError, JournalError,
                    OrderSubmissionAmbiguousError):
                raise
            except Exception as exc:
                log.warning("position history reconciliation failed for %s: %s",
                            symbol, exc)

        try:
            fills = self.retry(self.x.fetch_my_trades, symbol, since_ms, 100) or []
        except (CredentialError, ccxt.AuthenticationError, JournalError,
                OrderSubmissionAmbiguousError):
            raise
        except Exception as exc:
            raise RuntimeError(f"could not fetch closing fills for {symbol}: {exc}")
        close_side = "sell" if direction == "long" else "buy"
        closing = []
        for fill in fills:
            if (not isinstance(fill, dict) or fill.get("symbol") != symbol
                    or fill.get("side") != close_side):
                continue
            amount = finite_number(fill.get("amount"), positive=True)
            price = finite_number(fill.get("price"), positive=True)
            info = fill.get("info")
            if info not in (None, "") and not isinstance(info, dict):
                continue
            if amount is None or price is None:
                continue
            closing.append((fill, amount, price, info or {}))
        if not closing:
            raise OrderSubmissionAmbiguousError(
                f"no valid closing fills found yet for {symbol}",
                {"symbol": symbol, "outcome": "close_history_unresolved"})
        qty = sum(amount for _, amount, _, _ in closing)
        cost = sum(amount * fill_price
                   for _, amount, fill_price, _ in closing)
        if not math.isfinite(qty) or qty <= 0 \
                or not math.isfinite(cost) or cost <= 0:
            raise OrderSubmissionAmbiguousError(
                f"closing fill quantity/price is invalid for {symbol}",
                {"symbol": symbol, "outcome": "close_history_unresolved"})
        price = cost / qty
        try:
            fee = sum(self._fee_usd(fill)
                      for fill, _, _, _ in closing)
        except (CredentialError, ccxt.AuthenticationError, JournalError,
                OrderSubmissionAmbiguousError):
            raise
        except Exception as exc:
            raise OrderSubmissionAmbiguousError(
                f"closing fill fee metadata is invalid for {symbol}",
                {"symbol": symbol, "outcome": "close_history_unresolved"}
            ) from exc
        pnl = 0.0
        for _, _, _, info in closing:
            raw_pnl = info.get("fillPnl")
            if raw_pnl in (None, ""):
                continue
            value = finite_number(raw_pnl)
            if value is None:
                raise OrderSubmissionAmbiguousError(
                    f"closing fill PnL metadata is invalid for {symbol}",
                    {"symbol": symbol,
                     "outcome": "close_history_unresolved"})
            pnl += value
        if pnl == 0 and entry_price > 0:
            contract_size = float(self.x.market(symbol).get("contractSize") or 1)
            move = price - entry_price
            pnl = move * qty * contract_size * (1 if direction == "long" else -1)
        # OKX fillPnl excludes transaction fees and funding. Return net
        # closing-fill PnL; the engine separately deducts the opening fee
        # retained in state.
        pnl -= fee
        funding = self.funding_since(symbol, since_ms)
        if funding is not None:
            pnl += funding
        return {
            "symbol": symbol,
            "price": price,
            "qty": qty,
            "fee_usd": fee,
            "funding_usd": funding or 0.0,
            "realized_pnl_usd": pnl,
            "status": ("fill_history" if funding is not None
                       else "fill_history_funding_unavailable"),
        }
