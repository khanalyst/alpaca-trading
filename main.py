#!/usr/bin/env python3
"""Command line control for the Alpaca trading runtime."""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from decimal import Decimal
import json
import logging
import os
import signal
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agent.config import ConfigError, load_config
from agent.alpaca_domain import OrderRequest
from agent.alpaca_provider import AlpacaProvider
from agent.engine import Engine
from agent.instruments import validate_equity_symbol
from agent import state


_FAILED_ORDER_STATUSES = {
    "canceled", "cancelled", "expired", "rejected", "done", "closed",
    "done_for_day", "replaced", "stopped", "suspended", "failed",
    "not_found",
}


def load_cfg(path: str | Path = ROOT / "config.yaml") -> dict:
    return load_config(path)


def _plain(value):
    """Convert provider dataclasses/enums into CLI-safe plain mappings."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {_plain(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(item) for item in value]
    if hasattr(value, "value"):
        return _plain(value.value)
    return value


def _dump_yaml(value) -> str:
    plain = _plain(value)
    try:
        import yaml
    except ImportError:
        # JSON is an adequate presentation fallback in a recovery shell.
        return json.dumps(plain, indent=2, sort_keys=False, default=str)
    return yaml.safe_dump(plain, sort_keys=False,
                         default_flow_style=False).rstrip()


def _engine(cfg, light=False):
    return Engine(cfg, light=light)


def _provider(cfg):
    return AlpacaProvider(cfg)


def _wait_for_order(provider, client_order_id: str, qty: Decimal,
                    timeout: float):
    deadline = time.monotonic() + timeout
    last_status = "not_found"
    while True:
        rows = provider.orders(client_order_id=client_order_id)
        if len(rows) > 1:
            raise RuntimeError(
                f"broker returned multiple orders for {client_order_id}")
        if rows:
            order = rows[0]
            last_status = order.status
            if order.status == "filled":
                if order.filled_qty != qty:
                    raise RuntimeError(
                        f"filled order quantity {order.filled_qty} does not match {qty}")
                return order
            if order.status in _FAILED_ORDER_STATUSES:
                raise RuntimeError(
                    f"order {client_order_id} ended with status {order.status}")
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"order {client_order_id} did not fill within {timeout:g}s; "
                f"last status {last_status}")
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))


def _wait_for_position(provider, symbol: str, qty: Decimal | None,
                       timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        positions = provider.positions()
        held = next((position for position in positions
                     if position.symbol == symbol), None)
        if qty is None and held is None:
            return positions
        if qty is not None and held is not None and held.qty == qty:
            return positions
        if time.monotonic() >= deadline:
            wanted = "flat" if qty is None else f"quantity {qty}"
            actual = "flat" if held is None else f"quantity {held.qty}"
            raise TimeoutError(
                f"position {symbol} did not reach {wanted} within {timeout:g}s; "
                f"last state {actual}")
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))


def _paper_smoke_cleanup(provider, symbol: str, timeout: float) -> dict:
    result = {"cancel_requested": False, "close_requested": False,
              "flat": False, "open_orders": None, "errors": []}
    try:
        provider.cancel_all_orders()
        result["cancel_requested"] = True
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"cancel failed: {exc}")
    try:
        held = next((position for position in provider.positions()
                     if position.symbol == symbol), None)
        if held is not None:
            recovery_id = f"smoke-recovery-{uuid.uuid4().hex[:20]}"
            order = provider.close_position(
                symbol, qty=held.qty, client_order_id=recovery_id)
            result["close_requested"] = order is not None
            if order is not None:
                _wait_for_order(provider, recovery_id, held.qty, timeout)
        positions = _wait_for_position(provider, symbol, None, timeout)
        open_orders = provider.orders(status="open")
        result["open_orders"] = len(open_orders)
        result["flat"] = not positions and not open_orders
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"recovery failed: {exc}")
    return result


def cmd_check(args, cfg) -> int:
    if not bool(args.authenticated):
        print(_dump_yaml({
            "mode": cfg.get("mode", "paper"),
            "paper": cfg.get("mode", "paper") == "paper",
            "authenticated": False,
            "local_config_valid": True,
            "edge_checked": False,
        }))
        return 0
    engine = _engine(cfg, light=True)
    try:
        result = engine.check(authenticated=bool(args.authenticated))
    except Exception as exc:  # noqa: BLE001
        print(f"Authenticated check failed: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.close()
    print(_dump_yaml(result))
    return 1 if result.get("edge_required") and not result.get("edge_ready") else 0


def cmd_status(args, cfg) -> int:
    engine = _engine(cfg, light=True)
    try:
        result = engine.status()
    finally:
        engine.close()
    print(_dump_yaml(result))
    return 1 if (result.get("auth_error") or
                 (result.get("edge_required") and not result.get("edge_ready"))) else 0


def cmd_run(args, cfg) -> int:
    engine = _engine(cfg)
    previous = {}
    def _stop(signum, _frame):
        engine.request_shutdown(signal.Signals(signum).name)
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, _stop)
    try:
        engine.run(max_cycles=args.cycles)
    except Exception as exc:  # noqa: BLE001
        print(f"run failed: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


def cmd_flatten(args, cfg) -> int:
    engine = _engine(cfg, light=True)
    try:
        complete = engine.flatten_all(args.reason)
    except Exception as exc:  # noqa: BLE001
        print(f"flatten failed: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.close()
    if not complete:
        print(f"flatten incomplete: residual {cfg.get('mode', 'paper')} positions remain",
              file=sys.stderr)
        return 1
    print("Flatten requested")
    return 0


def cmd_resume(args, cfg) -> int:
    """Authenticate and authorize a paused, broker-confirmed flat runtime."""
    try:
        engine = _engine(cfg, light=True)
        result = engine.resume()
    except Exception as exc:  # noqa: BLE001
        print(f"resume failed: {exc}", file=sys.stderr)
        return 1
    print(_dump_yaml(result))
    return 0


def cmd_paper_smoke(args, cfg) -> int:
    """Open and close one paper equity share, then prove broker-flat state."""
    if args.confirm != "PAPER":
        print("paper smoke refused: --confirm PAPER is required", file=sys.stderr)
        return 2
    broker = cfg.get("broker") if isinstance(cfg.get("broker"), dict) else {}
    if (cfg.get("mode", "paper") != "paper" or
            broker.get("paper") is not True or
            broker.get("allow_live", False) is not False):
        print("paper smoke refused: configuration is not pinned to paper mode",
              file=sys.stderr)
        return 2
    try:
        symbol = validate_equity_symbol(args.symbol)
    except ValueError as exc:
        print(f"paper smoke refused: {exc}", file=sys.stderr)
        return 2
    if not isinstance(args.timeout, (int, float)) or args.timeout <= 0:
        print("paper smoke refused: timeout must be positive", file=sys.stderr)
        return 2

    provider = None
    cleanup_allowed = False
    lock_handle = None
    try:
        state.configure_runtime("paper")
        lock_handle = state.acquire_run_lock()
        if lock_handle is None:
            raise RuntimeError(
                "paper runtime lock is held; stop the trader before this test")
        provider = _provider(cfg)
        if provider.mode != "paper" or provider.paper is not True:
            raise RuntimeError("provider is not connected to the paper endpoint")
        account = provider.account()
        if account.status != "active":
            raise RuntimeError(f"paper account status is {account.status}, not active")
        clock = provider.clock()
        if not clock.is_open:
            raise RuntimeError(
                f"market is closed; next open is {clock.next_open}")
        if clock.next_close is None:
            raise RuntimeError("broker clock did not report the market close")
        seconds_to_close = (clock.next_close - clock.timestamp).total_seconds()
        if seconds_to_close < 300:
            raise RuntimeError(
                "paper smoke requires at least 5 minutes before market close")
        positions = provider.positions()
        open_orders = provider.orders(status="open")
        if positions or open_orders:
            raise RuntimeError(
                "paper account must start flat with no open orders; "
                f"found {len(positions)} positions and {len(open_orders)} open orders")
        cleanup_allowed = True

        qty = Decimal("1")
        entry_id = f"smoke-open-{uuid.uuid4().hex[:20]}"
        provider.submit_order(OrderRequest(
            symbol=symbol, qty=qty, side="buy", type="market",
            time_in_force="day", client_order_id=entry_id))
        entry = _wait_for_order(provider, entry_id, qty, args.timeout)
        _wait_for_position(provider, symbol, qty, args.timeout)

        exit_id = f"smoke-close-{uuid.uuid4().hex[:20]}"
        submitted_exit = provider.close_position(
            symbol, qty=qty, client_order_id=exit_id)
        if submitted_exit is None:
            raise RuntimeError("broker position disappeared before close submission")
        exit_order = _wait_for_order(provider, exit_id, qty, args.timeout)
        final_positions = _wait_for_position(
            provider, symbol, None, args.timeout)
        final_orders = provider.orders(status="open")
        if final_positions or final_orders:
            raise RuntimeError(
                "paper smoke reconciliation found residual broker state")
        print(_dump_yaml({
            "schema": "paper-order-smoke.v1",
            "status": "ok",
            "mode": provider.mode,
            "paper": provider.paper,
            "account_status": account.status,
            "market_timestamp": clock.timestamp,
            "symbol": symbol,
            "qty": qty,
            "entry": entry,
            "exit": exit_order,
            "flat": True,
            "open_orders": 0,
        }))
        return 0
    except Exception as exc:  # noqa: BLE001
        result = {"schema": "paper-order-smoke.v1", "status": "failed",
                  "error": str(exc), "symbol": symbol}
        if provider is not None and cleanup_allowed:
            result["cleanup"] = _paper_smoke_cleanup(
                provider, symbol, args.timeout)
        print(_dump_yaml(result), file=sys.stderr)
        return 1
    finally:
        state.release_run_lock(lock_handle)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Alpaca trading agent")
    p.add_argument("--config", default=str(ROOT / "config.yaml"))
    p.add_argument("--env-file", default=None)
    sub = p.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="validate config and authenticated broker connectivity")
    auth = check.add_mutually_exclusive_group()
    auth.add_argument("--authenticated", dest="authenticated", action="store_true",
                      help="query account, broker clock, calendar and configured feeds (default)")
    auth.add_argument("--offline", dest="authenticated", action="store_false",
                      help="validate local configuration only; never use as a trading preflight")
    check.set_defaults(authenticated=True)
    check.set_defaults(fn=cmd_check)
    status = sub.add_parser("status")
    status.set_defaults(fn=cmd_status)
    run = sub.add_parser("run")
    run.add_argument("--cycles", type=int, default=None)
    run.set_defaults(fn=cmd_run)
    flatten = sub.add_parser("flatten")
    flatten.add_argument("--reason", default="operator")
    flatten.set_defaults(fn=cmd_flatten)
    resume = sub.add_parser(
        "resume",
        help="authenticated flat-only authorization for a paused runtime",
        description=("Authenticate, reconcile once, and clear operator pause "
                     "only when the broker and durable journal prove flat; "
                     "this command never submits, cancels, or flattens orders."),
    )
    resume.set_defaults(fn=cmd_resume)
    smoke = sub.add_parser(
        "paper-smoke",
        help="place and close exactly one paper equity share",
        description=("Authenticated paper-only broker smoke test. The account "
                     "must begin flat with no open orders. The command buys "
                     "one share, closes it, and verifies the account is flat."),
    )
    smoke.add_argument("--symbol", default="SPY")
    smoke.add_argument("--timeout", type=float, default=90.0)
    smoke.add_argument("--confirm", choices=["PAPER"], required=True)
    smoke.set_defaults(fn=cmd_paper_smoke)
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    env_file = args.env_file or os.getenv("ALPACA_AGENT_SECRETS_FILE") or os.getenv("ALPACA_AGENT_SECRET_FILE")
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None
    if env_file:
        if load_dotenv is not None:
            load_dotenv(env_file, override=False)
    elif (ROOT / ".env").is_file() and load_dotenv is not None:
        load_dotenv(ROOT / ".env", override=False)
    try:
        cfg = load_cfg(args.config)
    except (OSError, ConfigError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    return args.fn(args, cfg)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
