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

    @staticmethod
    def _client_order_id(prefix: str) -> str:
        return (prefix + uuid.uuid4().hex)[:32]

    def _recover_order(self, symbol: str, client_order_id: str):
        """Recover an ambiguously acknowledged order without resubmitting it."""
        for _ in range(4):
            for params in (
                    {"clientOrderId": client_order_id},
                    {"clientOrderId": client_order_id, "trigger": True}):
                try:
                    order = self.x.fetch_order(None, symbol, params)
                    if order and (order.get("id") or order.get("clientOrderId")):
                        return order
                except Exception:
                    continue
            # Some OKX/CCXT versions expose a client ID on an algo order as
            # clOrdId and others as algoClOrdId. Scan pending algo orders as
            # a final read-only recovery path before declaring ambiguity.
            for order_type in PROTECTIVE_ALGO_TYPES:
                try:
                    orders = self.x.fetch_open_orders(
                        symbol, None, None, {"ordType": order_type}) or []
                except Exception:
                    continue
                for order in orders:
                    info = order.get("info") or {}
                    ids = {
                        str(order.get("clientOrderId") or ""),
                        str(info.get("clOrdId") or ""),
                        str(info.get("algoClOrdId") or ""),
                    }
                    if client_order_id in ids:
                        return order
            time.sleep(0.75)
        return None

    def _create_order_once(self, symbol: str, order_type: str, side: str,
                           amount: float, price, params: dict, prefix: str):
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
            error = RuntimeError(
                f"ambiguous order result for {client_id}; order was not retried"
            )
            setattr(error, "_order_audit", audit)
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
        current = order or {}
        order_id = current.get("id")
        deadline = time.monotonic() + float(
            self.cfg["execution"]["fill_timeout_seconds"])
        while order_id and time.monotonic() < deadline:
            status = str(current.get("status") or "").lower()
            if status in {"closed", "canceled", "rejected", "expired"}:
                break
            try:
                current = self.retry(self.x.fetch_order, order_id, symbol)
            except Exception:
                time.sleep(0.4)
                continue
            time.sleep(0.25)

        status = str(current.get("status") or "unknown").lower()
        if status not in {"closed", "canceled", "rejected", "expired"} and order_id:
            try:
                self.x.cancel_order(order_id, symbol)
                current = self.retry(self.x.fetch_order, order_id, symbol)
                status = str(current.get("status") or status).lower()
            except Exception as exc:
                log.warning("could not cancel non-terminal order %s: %s",
                            order_id, exc)

        info = current.get("info") or {}
        filled = float(current.get("filled") or info.get("accFillSz")
                       or info.get("fillSz") or 0)
        average = float(current.get("average") or info.get("avgPx")
                        or info.get("fillPx") or 0)
        submission_audit = dict(
            current.get("_submission_audit")
            or (order or {}).get("_submission_audit")
            or {}
        )
        submission_audit.update({
            "order_id": order_id,
            "last_fill_status": status,
        })
        if not math.isfinite(filled) or filled <= 0:
            submission_audit["outcome"] = "fill_unverified"
            error = RuntimeError(
                f"order {order_id or '?'} has no verified fill")
            setattr(error, "_order_audit", submission_audit)
            raise error
        if not math.isfinite(average) or average <= 0:
            average = float(expected_price or 0)
        if not math.isfinite(average) or average <= 0:
            submission_audit["outcome"] = "fill_price_unverified"
            error = RuntimeError(
                f"order {order_id or '?'} has no verified fill price")
            setattr(error, "_order_audit", submission_audit)
            raise error

        contract_size = float(self.x.market(symbol).get("contractSize") or 1)
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
        realized_includes_costs = info.get("realizedPnl") not in (None, "")
        realized_raw = info.get("realizedPnl")
        if realized_raw in (None, ""):
            realized_raw = info.get("pnl")
        return {
            "order_id": order_id,
            "status": status,
            "requested": float(requested),
            "filled": filled,
            "average": average,
            "partial": filled + 1e-12 < float(requested),
            "fee_usd": self._fee_usd(current),
            "realized_pnl_usd": (float(realized_raw)
                                 if realized_raw not in (None, "") else None),
            "realized_includes_costs": realized_includes_costs,
            "slippage_usd": slippage,
            "adverse_slippage_usd": max(0.0, slippage),
            "client_order_id": (
                current.get("clientOrderId") or info.get("clOrdId")
                or (order or {}).get("clientOrderId")
                or ((order or {}).get("_submission_audit") or {}).get(
                    "client_order_id")),
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
        return self._usdt_equity_from_balance(
            self.retry(self.x.fetch_balance))

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
        except Exception as exc:
            log.warning("funding history unavailable for %s: %s", symbol, exc)
            return None
        return sum(float(row.get("amount") or 0) for row in rows)

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
        try:
            live_position = self.position(symbol)
        except Exception as exc:
            # The verified fill is enough to protect the position even when
            # the positions endpoint is temporarily unavailable.
            live_position = None
            log.warning("position verification failed after %s fill: %s",
                        symbol, exc)
            self._alert("warning", "position_read_after_fill_failed",
                        f"Using the verified {symbol} fill to place protection",
                        {"error": str(exc)})
        live_contracts = abs(float((live_position or {}).get("contracts")
                                   or fill["filled"]))
        mark = float((live_position or {}).get("markPrice")
                     or fill["average"])
        # Attached algo orders can be eventually consistent on the read API.
        time.sleep(0.4)
        try:
            protection = self.ensure_protection(
                symbol, position_side, live_contracts, sl_price, tp_price, mark)
        except Exception as e:
            protection = {"stop_loss": False, "take_profit": False}
            log.error("protection verification failed for %s: %s", symbol, e)

        if not protection.get("stop_loss"):
            self._alert("critical", "unprotected_position",
                        f"{symbol} entry filled without a verified stop-loss",
                        {"contracts": live_contracts})
        if fill["partial"]:
            log.warning("PARTIAL ENTRY %s: requested %s, filled %s", symbol,
                        contracts, fill["filled"])
            self._alert("warning", "partial_entry",
                        f"{symbol} entry partially filled",
                        {"requested": contracts, "filled": fill["filled"]})
        fill["protection"] = protection
        fill["position_contracts"] = live_contracts
        fill["position_id"] = ((live_position or {}).get("id")
                               or ((live_position or {}).get("info") or {}).get(
                                   "posId"))
        liquidation_raw = (
            (live_position or {}).get("liquidationPrice")
            or ((live_position or {}).get("info") or {}).get("liqPx")
        )
        if liquidation_raw in (None, "", "0", 0):
            liquidation_price = None
        else:
            try:
                liquidation_price = float(liquidation_raw)
            except (TypeError, ValueError):
                # The engine treats an invalid available measurement as unsafe
                # and emergency-closes the already-filled position. Never raise
                # here and accidentally make a real fill look like no fill.
                liquidation_price = liquidation_raw
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
        self.retry(self.x.set_leverage, int(leverage), symbol,
                   {"mgnMode": "cross"})
        book = self.retry(self.x.fetch_order_book, symbol, 5)
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            raise RuntimeError(f"{symbol} order book has no two-sided depth")

        # Join the near touch rather than improving it: improving would cross
        # into the spread we are trying to capture.
        passive = float(bids[0][0]) if side == "buy" else float(asks[0][0])
        limit = float(self.x.price_to_precision(symbol, passive))

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
        # Order creation is deliberately single-shot. A generic retry can
        # submit the same passive order twice after an ambiguous timeout,
        # leaving duplicate exposure when the crossing fallback runs.
        order = self._create_order_once(
            symbol, "limit", side, contracts, limit, params, "okxmk")
        order_id = order.get("id")
        if not order_id:
            audit = dict(order.get("_submission_audit") or {})
            recovered = self._recover_order(
                symbol, str(audit.get("client_order_id") or ""))
            if recovered:
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
            error = RuntimeError(
                "maker-first order acknowledgement has no exchange order id")
            setattr(error, "_order_audit", audit)
            raise error

        deadline = time.time() + max(0.0, float(wait_seconds))
        filled = float(order.get("filled") or 0)
        while time.time() < deadline and filled < contracts:
            time.sleep(min(1.0, max(0.05, deadline - time.time())))
            try:
                order = self.retry(self.x.fetch_order, order_id, symbol)
                filled = float(order.get("filled") or 0)
            except Exception as exc:                       # noqa: BLE001
                log.warning("maker-first fill poll failed for %s: %s",
                            symbol, exc)
                break

        cancelled = True
        if filled < contracts:
            try:
                self.retry(self.x.cancel_order, order_id, symbol)
            except Exception as exc:                       # noqa: BLE001
                # The order may have filled in the race. Re-read rather than
                # assume either way: assuming unfilled would double the
                # position, assuming filled would leave one unmanaged.
                cancelled = False
                log.warning("maker-first cancel failed for %s: %s",
                            symbol, exc)
            try:
                order = self.retry(self.x.fetch_order, order_id, symbol)
                filled = float(order.get("filled") or 0)
            except Exception as exc:                       # noqa: BLE001
                log.warning("maker-first post-cancel read failed for %s: %s",
                            symbol, exc)

        # A passive fill is a position, so it goes through exactly the same
        # verification an IOC fill does - live position read, protection
        # audit, mark and liquidation prices. Skipping it would leave the
        # engine believing a position is protected without anything having
        # checked.
        settled = None
        if filled > 0:
            settled = self.settle_fill(
                {"filled": float(filled),
                 "average": float(order.get("average") or limit),
                 "requested": float(contracts),
                 "partial": float(filled) < float(contracts),
                 "order_id": order_id,
                 "client_order_id": (order.get("clientOrderId")
                                     or (order.get("info") or {}).get(
                                         "clOrdId")),
                 "status": order.get("status"),
                 "fee_usd": 0.0, "slippage_usd": 0.0,
                 "submission_audit": {"path": "maker_first"}},
                symbol, side, contracts, sl_price, tp_price)

        average = order.get("average") or (limit if filled else None)
        slippage_saved_pct = None
        if average and reference_price:
            realised = float(average)
            direction = 1.0 if side == "buy" else -1.0
            # Positive means the passive fill beat the reference.
            slippage_saved_pct = round(
                direction * (reference_price - realised)
                / reference_price * 100, 6)

        return {
            "order_id": order_id,
            "requested_contracts": float(contracts),
            "filled_contracts": float(filled),
            "fill_rate": (float(filled) / float(contracts)
                          if contracts else 0.0),
            "limit_price": limit,
            "average_price": float(average) if average else None,
            "reference_price": float(reference_price),
            "ioc_counterfactual_pct": slippage_saved_pct,
            "wait_seconds": float(wait_seconds),
            "cancelled": cancelled,
            "resting": bool(not cancelled and filled < contracts),
            # The settled fill, shaped exactly like open_position's return so
            # the engine's own post-fill bookkeeping can consume it unchanged.
            # None when nothing filled, which is the ordinary outcome.
            "execution": settled,
        }

    def open_position(self, symbol: str, side: str, contracts: float,
                      leverage: float, sl_price: float, tp_price: float,
                      expected_price: float | None = None,
                      entry_limit_price: float | None = None) -> dict:
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

    def close_position(self, pos: dict) -> dict:
        symbol = pos["symbol"]
        contracts = abs(float(pos.get("contracts") or 0))
        if contracts <= 0:
            return None
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
        remaining = self.position(symbol, position_side)
        remaining_contracts = abs(float((remaining or {}).get("contracts") or 0))
        result["fully_closed"] = remaining_contracts <= max(1e-12, contracts * 1e-6)
        result["remaining_contracts"] = remaining_contracts
        if result["fully_closed"]:
            if not any(p.get("symbol") == symbol for p in self.positions()):
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
                except Exception as e:
                    log.warning("cancel %s: %s", o.get("id"), e)
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
                    except Exception as e:
                        log.warning("cancel algo %s: %s", o.get("id"), e)
            except Exception:
                continue

    def cancel_everything(self) -> None:
        """Cancel and then verify every regular and algo order is gone."""
        failures = []
        try:
            for o in self.retry(self.x.fetch_open_orders) or []:
                try:
                    self.x.cancel_order(o["id"], o.get("symbol"))
                except Exception as e:
                    log.warning("cancel %s: %s", o.get("id"), e)
                    failures.append(f"{o.get('id')}: {e}")
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
                    except Exception as e:
                        log.warning("cancel algo %s: %s", o.get("id"), e)
                        failures.append(f"algo {o.get('id')}: {e}")
            except Exception as exc:
                failures.append(f"{order_type}-order query: {exc}")

        time.sleep(0.5)
        remaining = []
        try:
            remaining.extend(self.retry(self.x.fetch_open_orders) or [])
        except Exception as exc:
            failures.append(f"regular-order verification: {exc}")
        for order_type in ALL_ALGO_TYPES:
            try:
                rows = self.x.fetch_open_orders(
                    None, None, None, {"ordType": order_type}) or []
                remaining.extend(rows)
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
        if self.x.has.get("fetchPositionsHistory"):
            try:
                rows = self.retry(self.x.fetch_positions_history,
                                  [symbol], since_ms, 100) or []
                rows = [r for r in rows if r.get("symbol") == symbol]
                if rows:
                    row = max(rows, key=lambda r: int(r.get("timestamp") or 0))
                    info = row.get("info") or {}
                    realized = float(row.get("realizedPnl")
                                     or info.get("realizedPnl") or 0)
                    fee_raw = float(info.get("fee") or 0)
                    funding = float(info.get("fundingFee") or 0)
                    price = float(row.get("exitPrice") or info.get("closeAvgPx")
                                  or info.get("avgPx") or 0)
                    qty = abs(float(info.get("closeTotalPos") or quantity))
                    return {
                        "price": price,
                        "qty": qty,
                        "fee_usd": abs(fee_raw),
                        "funding_usd": funding,
                        "realized_pnl_usd": realized,
                        "status": "position_history",
                    }
            except Exception as exc:
                log.warning("position history reconciliation failed for %s: %s",
                            symbol, exc)

        try:
            fills = self.retry(self.x.fetch_my_trades, symbol, since_ms, 100) or []
        except Exception as exc:
            raise RuntimeError(f"could not fetch closing fills for {symbol}: {exc}")
        close_side = "sell" if direction == "long" else "buy"
        closing = [f for f in fills if f.get("side") == close_side]
        if not closing:
            raise RuntimeError(f"no closing fills found for {symbol}")
        qty = sum(abs(float(f.get("amount") or 0)) for f in closing)
        cost = sum(abs(float(f.get("amount") or 0))
                   * float(f.get("price") or 0) for f in closing)
        price = cost / qty if qty else 0.0
        fee = sum(self._fee_usd(f) for f in closing)
        pnl = sum(float((f.get("info") or {}).get("fillPnl") or 0)
                  for f in closing)
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
            "price": price,
            "qty": qty,
            "fee_usd": fee,
            "funding_usd": funding or 0.0,
            "realized_pnl_usd": pnl,
            "status": ("fill_history" if funding is not None
                       else "fill_history_funding_unavailable"),
        }
