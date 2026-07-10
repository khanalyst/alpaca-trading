#!/usr/bin/env python3
"""OKX AI Trading Agent - command line control.

  python main.py check                 validate config, keys and connectivity
  python main.py run                   start the trading loop (foreground)
  python main.py run --acknowledge-kill  restart after a self-kill event
  python main.py pause                 stop opening new positions
  python main.py pause --flatten       pause AND close everything now
  python main.py resume                re-enable trading
  python main.py flatten               close all positions, cancel all orders
  python main.py kill                  KILL SWITCH: flatten everything, stop
  python main.py kill --keep-positions stop the loop but leave positions open
  python main.py status                equity, positions, state
"""

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
import yaml  # noqa: E402

from agent import state  # noqa: E402
from agent.state import DAY_STOPPED, KILLED, PAUSED, RUNNING  # noqa: E402


def setup_logging() -> None:
    state.RUNTIME.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(state.RUNTIME / "agent.log"),
        ],
    )


def load_cfg(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _light_engine(cfg):
    from agent.engine import Engine
    return Engine(cfg, light=True)


def _flatten(cfg, reason: str) -> bool:
    try:
        _light_engine(cfg).flatten_all(reason)
        print("All orders cancelled and all positions closed.")
        return True
    except Exception as e:
        print(f"Flatten FAILED: {e}")
        print("Close positions manually in the OKX app/web if any remain.")
        return False


# ------------------------------------------------------------------ commands

def cmd_run(args, cfg) -> int:
    pid = state.read_pid()
    if pid and state.pid_alive(pid):
        print(f"An agent loop already appears to be running (pid {pid}). "
              "Refusing to start a second instance.")
        return 1
    st = state.load_state()
    if st["state"] == KILLED and not args.acknowledge_kill:
        print(f"Agent is KILLED (reason: {st.get('kill_reason')}).")
        print("Review runtime/agent.log and the journal, then restart with:")
        print("  python main.py run --acknowledge-kill")
        return 1
    if st["state"] == KILLED:
        st["state"] = PAUSED
        st["kill_reason"] = None
        state.save_state(st)
    from agent.engine import Engine
    engine = Engine(cfg)
    engine.run()
    return 0


def cmd_pause(args, cfg) -> int:
    state.set_state(PAUSED)
    print("State set to PAUSED. No new positions will be opened. Open "
          "positions keep their exchange-side stop-loss and take-profit "
          "orders and the loop still enforces max-hold and margin guards.")
    if args.flatten:
        _flatten(cfg, "pause --flatten")
    return 0


def cmd_resume(args, cfg) -> int:
    st = state.load_state()
    if st["state"] == KILLED:
        print("Agent is KILLED. Use: python main.py run --acknowledge-kill")
        return 1
    if st["state"] == DAY_STOPPED:
        print("Note: overriding today's daily loss stop.")
    state.set_state(RUNNING)
    pid = state.read_pid()
    if pid and state.pid_alive(pid):
        print("State set to RUNNING. The live loop will resume on its next "
              "cycle.")
    else:
        print("State set to RUNNING, but no loop is running. Start it with: "
              "python main.py run")
    return 0


def cmd_kill(args, cfg) -> int:
    state.set_state(KILLED, reason="manual kill")
    print("KILL flag set. A running loop will flatten and exit on its next "
          "tick.")
    if args.keep_positions:
        print("--keep-positions: leaving positions open. Their stop-loss and "
              "take-profit orders remain live on OKX servers.")
        return 0
    ok = _flatten(cfg, "manual kill")
    return 0 if ok else 1


def cmd_flatten(args, cfg) -> int:
    ok = _flatten(cfg, "manual flatten")
    state.set_state(PAUSED)
    print("State set to PAUSED.")
    return 0 if ok else 1


def cmd_status(args, cfg) -> int:
    st = state.load_state()
    pid = state.read_pid()
    alive = bool(pid and state.pid_alive(pid))
    loop = f"running (pid {pid})" if alive else "not running"
    print(f"State: {st['state']}   Loop: {loop}   Mode: {cfg['mode']}   "
          f"LLM: {cfg['llm']['provider']}/{cfg['llm']['model']}")
    if st.get("kill_reason"):
        print(f"Last kill reason: {st['kill_reason']}")
    try:
        from agent.exchange import Exchange
        ex = Exchange(cfg)
        equity = ex.equity_usdt()
        line = f"Equity: {equity:,.2f} USDT"
        if st.get("day_start_equity"):
            day = (equity - st["day_start_equity"]) / st["day_start_equity"] * 100
            line += f"   Day PnL: {day:+.2f}%"
        if st.get("high_water_mark"):
            dd = (st["high_water_mark"] - equity) / st["high_water_mark"] * 100
            line += (f"   High-water mark: {st['high_water_mark']:,.2f}"
                     f"   Drawdown: {dd:.2f}%")
        print(line)
        positions = ex.positions()
        if not positions:
            print("Open positions: none")
        else:
            print("Open positions:")
            for p in positions:
                print(f"  {p['symbol']:<22} {str(p.get('side')):<6} "
                      f"contracts={p.get('contracts')} "
                      f"entry={p.get('entryPrice')} "
                      f"mark={p.get('markPrice')} "
                      f"uPnL={float(p.get('percentage') or 0):+.2f}% "
                      f"lev={p.get('leverage')}")
    except Exception as e:
        print(f"(Could not query OKX: {e})")
    return 0


def cmd_check(args, cfg) -> int:
    ok = True
    print(f"Mode: {cfg['mode']}")
    print(f"LLM:  {cfg['llm']['provider']} / {cfg['llm']['model']}")
    for key in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"):
        if not os.getenv(key):
            print(f"  MISSING {key} in .env")
            ok = False
    provider = cfg["llm"]["provider"]
    need = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
    if not os.getenv(need):
        print(f"  MISSING {need} in .env")
        ok = False
    if ok:
        try:
            from agent.exchange import Exchange
            from agent import market
            ex = Exchange(cfg)
            equity = ex.equity_usdt()
            universe = market.build_universe(ex, cfg)
            print(f"  OKX connection OK ({'DEMO' if ex.demo else 'LIVE'}). "
                  f"Equity: {equity:,.2f} USDT")
            print(f"  Universe head: {', '.join(universe[:5])}")
        except Exception as e:
            print(f"  OKX check FAILED: {e}")
            ok = False
    print("All checks passed. You can start with: python main.py run"
          if ok else
          "Fix the issues above, then re-run: python main.py check")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="OKX AI Trading Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", default=str(ROOT / "config.yaml"),
                        help="path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="start the trading loop")
    p.add_argument("--acknowledge-kill", action="store_true",
                   help="restart after a self-kill event")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("pause", help="stop opening new positions")
    p.add_argument("--flatten", action="store_true",
                   help="also close all open positions now")
    p.set_defaults(fn=cmd_pause)

    p = sub.add_parser("resume", help="re-enable trading")
    p.set_defaults(fn=cmd_resume)

    p = sub.add_parser("kill", help="kill switch: flatten and stop")
    p.add_argument("--keep-positions", action="store_true",
                   help="stop the loop but leave positions open")
    p.set_defaults(fn=cmd_kill)

    p = sub.add_parser("flatten", help="close everything, then pause")
    p.set_defaults(fn=cmd_flatten)

    p = sub.add_parser("status", help="show state, equity and positions")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("check", help="validate config, keys and connectivity")
    p.set_defaults(fn=cmd_check)

    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    setup_logging()
    cfg = load_cfg(args.config)
    return args.fn(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
