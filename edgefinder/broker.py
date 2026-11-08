"""A small Alpaca REST adapter, and an in-memory broker for tests.

Only the calls the trader needs: account, positions, orders, submit, clock,
calendar and the latest trade.  Requests go only to the Alpaca hosts over
HTTPS and never follow a redirect, so the key headers cannot leak.  No SDK is
needed, so the trader runs wherever the standard library does.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

PAPER_HOST = "paper-api.alpaca.markets"
LIVE_HOST = "api.alpaca.markets"
DATA_HOST = "data.alpaca.markets"
ALLOWED_HOSTS = {PAPER_HOST, LIVE_HOST, DATA_HOST}


class BrokerError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status, self.body = status, body


class Broker(Protocol):
    paper: bool

    def account(self) -> dict[str, Any]: ...
    def positions(self) -> dict[str, float]: ...
    def orders(self, after: dt.datetime) -> list[dict[str, Any]]: ...
    def order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None: ...
    def submit(self, *, symbol: str, qty: int, side: str, time_in_force: str,
               client_order_id: str) -> dict[str, Any]: ...
    def clock(self) -> dict[str, Any]: ...
    def calendar(self, start: dt.date, end: dt.date) -> list[dict[str, Any]]: ...
    def latest_price(self, symbol: str) -> float | None: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class AlpacaBroker:
    """Alpaca trading API v2 over HTTPS."""

    def __init__(self, *, paper: bool = True, key: str | None = None, secret: str | None = None,
                 timeout: float = 15.0, data_feed: str = "iex"):
        self.paper = paper
        self.key = key or os.getenv("ALPACA_API_KEY") or ""
        self.secret = secret or os.getenv("ALPACA_SECRET_KEY") or ""
        if not self.key or not self.secret:
            raise BrokerError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set")
        self.host = PAPER_HOST if paper else LIVE_HOST
        self.timeout = timeout
        self.data_feed = data_feed
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _request(self, method: str, host: str, path: str, *, query: Mapping[str, Any] | None = None,
                 body: Mapping[str, Any] | None = None) -> Any:
        if host not in ALLOWED_HOSTS:
            raise BrokerError(f"host {host} is not an Alpaca host")
        url = f"https://{host}{path}"
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers={
            "APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret,
            "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                text = response.read().decode()
        except urllib.error.HTTPError as error:
            raise BrokerError(f"{method} {path}: HTTP {error.code}", error.code,
                              error.read().decode(errors="replace")) from error
        return json.loads(text) if text else None

    def account(self) -> dict[str, Any]:
        return self._request("GET", self.host, "/v2/account")

    def positions(self) -> dict[str, float]:
        return {p["symbol"]: float(p["qty"]) for p in self._request("GET", self.host, "/v2/positions")}

    def orders(self, after: dt.datetime) -> list[dict[str, Any]]:
        return self._request("GET", self.host, "/v2/orders", query={
            "status": "all", "limit": 500, "direction": "desc", "nested": "false",
            "after": after.astimezone(dt.timezone.utc).isoformat()})

    def order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        try:
            return self._request("GET", self.host, "/v2/orders:by_client_order_id",
                                 query={"client_order_id": client_order_id})
        except BrokerError as error:
            if error.status == 404:
                return None
            raise

    def submit(self, *, symbol: str, qty: int, side: str, time_in_force: str,
               client_order_id: str) -> dict[str, Any]:
        return self._request("POST", self.host, "/v2/orders", body={
            "symbol": symbol, "qty": str(int(qty)), "side": side, "type": "market",
            "time_in_force": time_in_force, "client_order_id": client_order_id})

    def clock(self) -> dict[str, Any]:
        return self._request("GET", self.host, "/v2/clock")

    def calendar(self, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
        return self._request("GET", self.host, "/v2/calendar",
                             query={"start": start.isoformat(), "end": end.isoformat()})

    def latest_price(self, symbol: str) -> float | None:
        try:
            payload = self._request("GET", DATA_HOST,
                                    f"/v2/stocks/{urllib.parse.quote(symbol)}/trades/latest",
                                    query={"feed": self.data_feed})
            return float(payload["trade"]["p"])
        except (BrokerError, KeyError, TypeError, ValueError):
            return None


@dataclass
class FakeBroker:
    """Deterministic broker for tests: orders fill at ``fill_prices`` on demand."""

    equity: float = 100_000.0
    paper: bool = True
    now: dt.datetime = field(default_factory=lambda: dt.datetime(2026, 11, 9, 15, 40,
                                                                tzinfo=dt.timezone.utc))
    trading_days: list[dt.date] = field(default_factory=list)
    early_closes: dict[dt.date, str] = field(default_factory=dict)
    prices: dict[str, float] = field(default_factory=dict)
    held: dict[str, float] = field(default_factory=dict)
    submitted: list[dict[str, Any]] = field(default_factory=list)
    blocked: bool = False
    fail_submit: bool = False

    def account(self) -> dict[str, Any]:
        return {"equity": str(self.equity), "cash": str(self.equity), "status": "ACTIVE",
                "trading_blocked": self.blocked, "account_blocked": False, "currency": "USD"}

    def positions(self) -> dict[str, float]:
        return {k: v for k, v in self.held.items() if v}

    def orders(self, after: dt.datetime) -> list[dict[str, Any]]:
        return [o for o in self.submitted if o["submitted_at"] >= after.isoformat()]

    def order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        return next((o for o in self.submitted if o["client_order_id"] == client_order_id), None)

    def submit(self, *, symbol: str, qty: int, side: str, time_in_force: str,
               client_order_id: str) -> dict[str, Any]:
        if self.fail_submit:
            raise BrokerError("submit failed", 500)
        if self.order_by_client_id(client_order_id):
            raise BrokerError("client_order_id must be unique", 422)
        order = {"id": f"o{len(self.submitted) + 1}", "client_order_id": client_order_id,
                 "symbol": symbol, "qty": str(qty), "side": side, "type": "market",
                 "time_in_force": time_in_force, "status": "accepted", "filled_qty": "0",
                 "filled_avg_price": None, "submitted_at": self.now.isoformat()}
        self.submitted.append(order)
        return order

    def fill_all(self, prices: Mapping[str, float] | None = None) -> None:
        for o in self.submitted:
            if o["status"] != "accepted":
                continue
            price = (prices or self.prices)[o["symbol"]]
            signed = float(o["qty"]) * (1 if o["side"] == "buy" else -1)
            self.held[o["symbol"]] = self.held.get(o["symbol"], 0.0) + signed
            o.update(status="filled", filled_qty=o["qty"], filled_avg_price=str(price),
                     filled_at=self.now.isoformat())

    def clock(self) -> dict[str, Any]:
        return {"timestamp": self.now.isoformat(), "is_open": False}

    def calendar(self, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
        return [{"date": d.isoformat(), "open": "09:30", "close": self.early_closes.get(d, "16:00")}
                for d in self.trading_days if start <= d <= end]

    def latest_price(self, symbol: str) -> float | None:
        return self.prices.get(symbol)
