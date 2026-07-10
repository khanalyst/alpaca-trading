"""Thin, defensive wrapper around ccxt's OKX client.

Everything that touches the exchange lives here. All order placement uses
cross margin on USDT-settled perpetual swaps, in one-way (net) position mode.
Stop-loss and take-profit orders are placed ON THE EXCHANGE, so positions
stay protected even if this process dies.
"""

import logging
import os
import time

import ccxt

log = logging.getLogger("exchange")


class Exchange:
    def __init__(self, cfg: dict):
        key = os.getenv("OKX_API_KEY")
        secret = os.getenv("OKX_API_SECRET")
        passphrase = os.getenv("OKX_API_PASSPHRASE")
        if not (key and secret and passphrase):
            raise RuntimeError(
                "Missing OKX credentials. Copy .env.example to .env and fill in "
                "OKX_API_KEY, OKX_API_SECRET and OKX_API_PASSPHRASE."
            )
        self.demo = cfg.get("mode", "demo") == "demo"
        self.x = ccxt.okx({
            "apiKey": key,
            "secret": secret,
            "password": passphrase,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        })
        if self.demo:
            # OKX demo trading = same endpoints + a simulated-trading header.
            try:
                self.x.set_sandbox_mode(True)
            except Exception:
                pass
            headers = dict(self.x.headers or {})
            headers["x-simulated-trading"] = "1"
            self.x.headers = headers
        self.x.load_markets()
        try:
            # One-way (net) position mode keeps order handling simple.
            self.x.set_position_mode(False)
        except Exception as e:
            log.debug("set_position_mode skipped: %s", e)

    # ------------------------------------------------------------- helpers

    def retry(self, fn, *a, **kw):
        last = None
        for i in range(3):
            try:
                return fn(*a, **kw)
            except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
                last = e
                time.sleep(1.5 * (i + 1))
        raise last

    def price(self, symbol: str) -> float:
        t = self.retry(self.x.fetch_ticker, symbol)
        return float(t.get("last") or t.get("close") or 0)

    # ------------------------------------------------------------- account

    def equity_usdt(self) -> float:
        bal = self.retry(self.x.fetch_balance)
        data = (bal.get("info") or {}).get("data") or []
        if data and data[0].get("totalEq") not in (None, ""):
            return float(data[0]["totalEq"])
        return float((bal.get("USDT") or {}).get("total") or 0)

    def margin_usage_pct(self) -> float | None:
        try:
            bal = self.retry(self.x.fetch_balance)
            u = bal.get("USDT") or {}
            used = float(u.get("used") or 0)
            total = float(u.get("total") or 0)
            if total <= 0:
                return None
            return used / total * 100
        except Exception:
            return None

    def transfers_since(self, since_ms: int) -> tuple[float, int]:
        """Net USDT transferred in/out of the trading account since since_ms.

        Used to rebase the drawdown and daily-loss benchmarks so a deposit is
        not counted as profit and a withdrawal is not counted as a crash.
        """
        latest = since_ms
        net = 0.0
        try:
            entries = self.retry(self.x.fetch_ledger, "USDT", since_ms, 100)
        except Exception as e:
            log.debug("fetch_ledger unavailable: %s", e)
            return 0.0, int(time.time() * 1000)
        for e in entries or []:
            ts = e.get("timestamp") or 0
            latest = max(latest, ts)
            okx_type = str((e.get("info") or {}).get("type", ""))
            is_transfer = e.get("type") == "transfer" or okx_type == "1"
            if not is_transfer:
                continue
            amt = abs(float(e.get("amount") or 0))
            direction = e.get("direction")
            if direction == "in":
                net += amt
            elif direction == "out":
                net -= amt
        return net, latest

    def positions(self) -> list[dict]:
        out = []
        for p in self.retry(self.x.fetch_positions) or []:
            if abs(float(p.get("contracts") or 0)) > 0:
                out.append(p)
        return out

    # ------------------------------------------------------------- sizing

    def contracts_for_notional(self, symbol: str, notional_usd: float,
                               price: float) -> float:
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

    # ------------------------------------------------------------- orders

    def open_position(self, symbol: str, side: str, contracts: float,
                      leverage: float, sl_price: float, tp_price: float):
        try:
            self.retry(self.x.set_leverage, int(leverage), symbol,
                       {"mgnMode": "cross"})
        except Exception as e:
            log.warning("set_leverage %s failed: %s", symbol, e)

        sl = self.x.price_to_precision(symbol, sl_price)
        tp = self.x.price_to_precision(symbol, tp_price)
        params = {
            "tdMode": "cross",
            "stopLoss": {"triggerPrice": sl},
            "takeProfit": {"triggerPrice": tp},
        }
        try:
            # Preferred path: entry with SL/TP attached server-side.
            return self.retry(self.x.create_order, symbol, "market", side,
                              contracts, None, params)
        except ccxt.ExchangeError as e:
            log.warning("attached SL/TP rejected (%s); using separate stop "
                        "orders", e)
        order = self.retry(self.x.create_order, symbol, "market", side,
                           contracts, None, {"tdMode": "cross"})
        opposite = "sell" if side == "buy" else "buy"
        try:
            self.retry(self.x.create_order, symbol, "market", opposite,
                       contracts, None,
                       {"tdMode": "cross", "reduceOnly": True,
                        "stopLossPrice": sl})
        except Exception as e:
            log.error("stop-loss placement failed for %s: %s", symbol, e)
        try:
            self.retry(self.x.create_order, symbol, "market", opposite,
                       contracts, None,
                       {"tdMode": "cross", "reduceOnly": True,
                        "takeProfitPrice": tp})
        except Exception as e:
            log.warning("take-profit placement failed for %s: %s", symbol, e)
        return order

    def close_position(self, pos: dict):
        symbol = pos["symbol"]
        contracts = abs(float(pos.get("contracts") or 0))
        if contracts <= 0:
            return None
        side = "sell" if pos.get("side") == "long" else "buy"
        self.cancel_symbol(symbol)
        return self.retry(self.x.create_order, symbol, "market", side,
                          contracts, None,
                          {"tdMode": "cross", "reduceOnly": True})

    # --------------------------------------------------------- cancellation

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
        for flag in ({"trigger": True}, {"stop": True}):
            try:
                algos = self.x.fetch_open_orders(symbol, None, None, flag) or []
                for o in algos:
                    try:
                        self.x.cancel_order(o["id"], symbol, flag)
                    except Exception as e:
                        log.warning("cancel algo %s: %s", o.get("id"), e)
                break
            except Exception:
                continue

    def cancel_everything(self) -> None:
        """Kill-switch helper: cancel all orders across all symbols."""
        try:
            for o in self.retry(self.x.fetch_open_orders) or []:
                try:
                    self.x.cancel_order(o["id"], o.get("symbol"))
                except Exception as e:
                    log.warning("cancel %s: %s", o.get("id"), e)
        except Exception as e:
            log.debug("fetch_open_orders(all): %s", e)
        for flag in ({"trigger": True}, {"stop": True}):
            try:
                algos = self.x.fetch_open_orders(None, None, None, flag) or []
                for o in algos:
                    try:
                        self.x.cancel_order(o["id"], o.get("symbol"), flag)
                    except Exception as e:
                        log.warning("cancel algo %s: %s", o.get("id"), e)
                break
            except Exception:
                continue
        # Belt and braces: clear per-symbol orders on anything still open.
        for p in self.positions():
            self.cancel_symbol(p["symbol"])
