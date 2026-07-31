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
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
import yaml  # noqa: E402

from agent import state  # noqa: E402
from agent.config import ConfigError, validate_config  # noqa: E402
from agent.state import DAY_STOPPED, KILLED, PAUSED, RUNNING  # noqa: E402


def setup_logging() -> None:
    state.RUNTIME.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                state.RUNTIME / "agent.log", maxBytes=10 * 1024 * 1024,
                backupCount=5),
        ],
    )


ENV_FILE = ROOT / ".env"
SECRETS_FILE_ENV = "OKX_AGENT_SECRETS_FILE"
SECRET_VARS = ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE",
               "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ALERT_WEBHOOK_URL")
# Credentials that were already in the process environment before .env was
# read — i.e. coming from somewhere other than .env. Reported by `check`.
SHELL_SOURCED: list[str] = []


def secrets_file() -> Path:
    """Resolve the one credential file without exposing its contents."""
    configured = os.getenv(SECRETS_FILE_ENV)
    return Path(configured).expanduser() if configured else ENV_FILE


def load_secrets(mode: str = "demo") -> None:
    """Load every credential from one explicit file.

    ``.env`` remains the local default. Containers may set
    ``OKX_AGENT_SECRETS_FILE`` to a read-only Docker secret containing the
    same dotenv syntax. ``override=True`` and the explicit environment clear
    ensure a stale shell export cannot silently point a run at another account.
    """
    source = secrets_file()
    if not source.is_file():
        raise FileNotFoundError(
            f"No credential file at {source}. Copy .env.example to .env or "
            f"set {SECRETS_FILE_ENV} to a Docker/configured secret file."
        )
    file_mode = source.stat().st_mode
    if file_mode & 0o077:
        # Docker Compose implementations commonly expose a container secret
        # as root-owned 0444 even when the compose target requests 0400. It is
        # still isolated to this non-root, single-purpose container and mounted
        # read-only. Ordinary host files retain the stricter check.
        docker_secret = source.is_absolute() and source.parent == Path("/run/secrets")
        message = (f"{source} is readable by other users on this host. "
                   f"Run: chmod 600 {source}")
        if mode == "live" and not docker_secret:
            raise PermissionError(message)
        if not docker_secret:
            print(f"WARNING: {message}", file=sys.stderr)
    SHELL_SOURCED[:] = [v for v in SECRET_VARS if os.getenv(v)]
    for variable in SECRET_VARS:
        os.environ.pop(variable, None)
    load_dotenv(source, override=True)


class _ShutdownSignals:
    """Translate container termination into the engine's safe pause path."""

    def __init__(self) -> None:
        self.reason: str | None = None
        self.engine = None
        self.previous: dict[int, object] = {}

    def install(self) -> None:
        for signum in (signal.SIGTERM, signal.SIGINT):
            self.previous[signum] = signal.getsignal(signum)
            signal.signal(signum, self._handle)

    def attach(self, engine) -> None:
        self.engine = engine
        if self.reason:
            engine.request_shutdown(self.reason)

    def _handle(self, signum, _frame) -> None:
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        self.reason = name
        logging.getLogger("main").warning(
            "%s received; requesting a safe pause", name)
        if self.engine is not None:
            self.engine.request_shutdown(name)

    def restore(self) -> None:
        for signum, handler in self.previous.items():
            signal.signal(signum, handler)


def load_cfg(path: str) -> dict:
    with open(path) as f:
        return validate_config(yaml.safe_load(f))


def load_exchange_cfg(path: str) -> dict:
    cfg = load_cfg(path)
    state.configure_runtime(cfg["mode"])
    load_secrets(cfg["mode"])
    state.bind_runtime_identity(
        cfg["mode"], os.getenv("OKX_API_KEY") or "")
    return cfg


def _print_configuration_error(exc: Exception) -> None:
    print(f"Configuration error: {exc}", file=sys.stderr)


def _dispatch(args, cfg) -> int:
    try:
        return args.fn(args, cfg)
    except Exception as exc:
        logging.getLogger("main").exception(
            "Command %s failed", args.command)
        print(f"{args.command} failed: {exc}", file=sys.stderr)
        return 1


def _light_engine(cfg):
    from agent.engine import Engine
    return Engine(cfg, light=True)


def _flatten(cfg, reason: str) -> bool:
    try:
        ok = _light_engine(cfg).flatten_all(reason)
    except Exception as e:
        print(f"Flatten FAILED: {e}")
        print("Close positions manually in the OKX app/web if any remain.")
        return False
    if ok:
        print("All orders cancelled and all positions closed.")
    else:
        print("Flatten was incomplete: positions or orders remain unverified. "
              f"See {state.RUNTIME / 'agent.log'} and inspect OKX manually.")
    return ok


# ------------------------------------------------------------------ commands

def cmd_run(args, cfg) -> int:
    run_lock = state.acquire_run_lock()
    if run_lock is None:
        pid = state.read_pid()
        print(f"An agent loop already appears to be running (pid {pid}). "
              "Refusing to start a second instance.")
        return 1
    try:
        st = state.load_state()
        if st["state"] == KILLED and not args.acknowledge_kill:
            print(f"Agent is KILLED (reason: {st.get('kill_reason')}).")
            print(f"Review {state.RUNTIME / 'agent.log'} and the journal, "
                  "then restart with:")
            print("  python main.py run --acknowledge-kill")
            return 1
        if st["state"] == KILLED:
            st["state"] = PAUSED
            st["kill_reason"] = None
            st["flatten_on_kill"] = True
            st["operator_pause"] = False
            state.save_state(st)
        shutdown = _ShutdownSignals()
        shutdown.install()
        try:
            from agent.engine import Engine
            engine = Engine(cfg)
            shutdown.attach(engine)
            engine.run(run_lock)
        finally:
            shutdown.restore()
        return 0
    finally:
        state.release_run_lock(run_lock)


def cmd_pause(args, cfg) -> int:
    state.set_state(PAUSED, operator_pause=True)
    print("State set to PAUSED. No new positions will be opened. Open "
          "positions keep their exchange-side stop-loss and take-profit "
          "orders and the loop still enforces max-hold and margin guards.")
    print("This pause survives crashes and restarts; use 'python main.py "
          "resume' to re-enable trading.")
    if args.flatten:
        try:
            cfg = cfg or load_exchange_cfg(args.config)
        except (OSError, yaml.YAMLError, ConfigError) as exc:
            _print_configuration_error(exc)
            print("PAUSED was saved, but flatten could not connect to OKX.")
            return 1
        return 0 if _flatten(cfg, "pause --flatten") else 1
    return 0


def cmd_resume(args, cfg) -> int:
    st = state.load_state()
    if st["state"] == KILLED:
        print("Agent is KILLED. Use: python main.py run --acknowledge-kill")
        return 1
    if st["state"] == DAY_STOPPED:
        print("Note: overriding today's daily loss stop.")
    state.set_state(RUNNING, operator_pause=False)
    pid = state.read_pid()
    if pid and state.pid_alive(pid):
        print("State set to RUNNING. The live loop will resume on its next "
              "cycle.")
    else:
        print("State set to RUNNING, but no loop is running. Start it with: "
              "python main.py run")
    return 0


def cmd_kill(args, cfg) -> int:
    state.set_state(
        KILLED, reason="manual kill", operator_pause=True,
        flatten_on_kill=not args.keep_positions)
    if args.keep_positions:
        print("KILL flag set with --keep-positions. The loop will exit without "
              "touching positions or protective orders.")
        return 0
    # The advisory lock, not a potentially stale/reused PID, is authoritative.
    # If it is free, hold it for the direct flatten so a new loop cannot race
    # the emergency command.
    emergency_lock = state.acquire_run_lock()
    if emergency_lock is None:
        print("KILL flag set. The running loop will flatten and exit.")
        return 0
    try:
        print("KILL flag set. No loop is running; flattening directly.")
        try:
            cfg = cfg or load_exchange_cfg(args.config)
        except (OSError, yaml.YAMLError, ConfigError) as exc:
            _print_configuration_error(exc)
            print("KILLED was saved, but positions could not be flattened. "
                  "Close them manually in OKX.")
            return 1
        ok = _flatten(cfg, "manual kill")
        return 0 if ok else 1
    finally:
        state.release_run_lock(emergency_lock)


def cmd_flatten(args, cfg) -> int:
    ok = _flatten(cfg, "manual flatten")
    state.set_state(PAUSED, operator_pause=True)
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
        basis_current = st.get("equity_basis") == state.EQUITY_BASIS
        if basis_current and st.get("day_start_equity"):
            day = (equity - st["day_start_equity"]) / st["day_start_equity"] * 100
            line += f"   Day PnL: {day:+.2f}%"
        if basis_current and st.get("high_water_mark"):
            dd = (st["high_water_mark"] - equity) / st["high_water_mark"] * 100
            line += (f"   High-water mark: {st['high_water_mark']:,.2f}"
                     f"   Drawdown: {dd:.2f}%")
        print(line)
        if not basis_current:
            print("Benchmarks: pending one-time USDT-only rebase on the next "
                  "agent cycle")
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
    print(f"Secrets: {secrets_file()}")
    if SHELL_SOURCED:
        print(f"  NOTE {', '.join(SHELL_SOURCED)} also set in the shell "
              "environment; .env takes precedence. Unset the shell copies so "
              "there is one source of truth.")
    for key in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"):
        if not os.getenv(key):
            print(f"  MISSING {key} in .env")
            ok = False
    provider = cfg["llm"]["provider"]
    need = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
    if not os.getenv(need):
        print(f"  MISSING {need} in .env")
        ok = False
    alert_cfg = cfg["alerts"]
    alert_env = alert_cfg["webhook_url_env"]
    if cfg["mode"] == "live" and not alert_cfg["enabled"]:
        print("  LIVE BLOCKED: alerts.enabled must be true")
        ok = False
    if alert_cfg["enabled"] and not os.getenv(alert_env):
        print(f"  MISSING {alert_env} in .env (alerts.enabled is true)")
        ok = False
    if ok:
        try:
            from agent.exchange import Exchange
            from agent import market
            ex = Exchange(cfg)          # also verifies the clock
            equity = ex.equity_usdt()
            universe, universe_audit = market.select_universe(ex, cfg)
            print(f"  OKX connection OK ({'DEMO' if ex.demo else 'LIVE'}). "
                  f"Equity: {equity:,.2f} USDT")
            print(f"  Clock drift vs OKX: {ex.clock_drift_ms() / 1000:+.2f}s "
                  "(must stay within 30s)")
            print(f"  Eligible crypto universe: {len(universe)} "
                  f"instrument(s)")
            print(f"  Universe head: {', '.join(universe[:5]) or 'none'}")
            skipped = [
                row for row in universe_audit["candidates"]
                if (not row["selected"]
                    and row["reason"] != "ranked_below_top_n")
            ][:5]
            for row in skipped:
                print(f"    skipped {row['symbol']}: {row['reason']}")
        except Exception as e:
            print(f"  OKX check FAILED: {e}")
            ok = False
        else:
            # OKX's read-only account-config endpoint identifies this key's
            # permissions, mode and IP binding without changing leverage,
            # position mode or placing a probe order.
            try:
                account = ex.verify_trade_permission()
                print("  Account safety OK (read-only check): "
                      f"posMode={account['position_mode']} "
                      f"permissions={','.join(account['permissions'])} "
                      f"ip_bound={account['ip_bound']}"
                      + (f" non_usdt={account['non_usdt_collateral_pct']:.2f}%"
                         if account["non_usdt_collateral_pct"] is not None
                         else ""))
            except Exception as e:
                print(f"  TRADE PERMISSION FAILED: {e}")
                print("  The key can read the account but not trade. In OKX "
                      "API management, edit the key and enable 'Trade' "
                      "(never enable 'Withdraw').")
                ok = False
            if ok and cfg["mode"] == "live":
                try:
                    from agent.alerts import AlertManager
                    AlertManager(cfg).require_live_ready(probe=True)
                    print("  Live alert delivery OK")
                except Exception as e:
                    print(f"  LIVE ALERT CHECK FAILED: {e}")
                    ok = False
            if ok:
                try:
                    from agent.brain import LLM
                    llm = LLM(cfg)
                    model = llm.preflight()
                    # Showing the endpoint makes a mis-set OPENAI_BASE_URL
                    # obvious instead of silently routing to the wrong host.
                    print(f"  LLM access OK ({model}) via {llm.endpoint()}")
                except Exception as e:
                    print(f"  LLM CHECK FAILED: {e}")
                    ok = False
    print("All checks passed. You can start with: python main.py run"
          if ok else
          "Fix the issues above, then re-run: python main.py check")
    return 0 if ok else 1


def cmd_strategies(args, cfg) -> int:
    """Print the strategy register: what may run, and on what evidence."""
    from agent.registry import LIVE_MIN_TIER, REGISTRY

    active = str(cfg["strategy"]["id"])
    order = sorted(REGISTRY.values(),
                   key=lambda s: (-s.tier_rank(), s.id))
    print(f"Strategy register  (active: {active}, mode: {cfg['mode']})")
    print(f"Live requires {LIVE_MIN_TIER} or better.\n")
    for spec in order:
        marks = []
        if spec.id == active:
            marks.append("ACTIVE")
        marks.append(
            "runnable" if spec.implemented and spec.analyst_ready
            else "shadow-only" if spec.implemented
            else "research-only")
        if spec.meets(LIVE_MIN_TIER):
            marks.append("live-eligible")
        print(f"  {spec.id}/{spec.version}  [{spec.tier}]  "
              f"({', '.join(marks)})")
        print(f"      timeframe {spec.signal_timeframe}, hold <= "
              f"{spec.max_hold_hours_ceiling:g}h, {spec.execution_style} "
              f"execution")
        if args.verbose:
            print(f"      mechanism:     {spec.mechanism}")
            print(f"      falsified by:  {spec.falsification}")
            if spec.notes:
                print(f"      notes:         {spec.notes}")
            for source in spec.evidence:
                print(f"      evidence:      {source}")
        print()
    if not args.verbose:
        print("Use --verbose for each strategy's mechanism and "
              "falsification criterion.")
    print("Only entries marked runnable may be configured for the order path; "
          "shadow-only/research-only entries remain isolated research. Any "
          "strategy change is a deliberate reviewed config change followed "
          "by a restart, never a research-side automatic switch.")
    return 0


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

    p = sub.add_parser("strategies",
                       help="list registered strategies and their tiers")
    p.add_argument("--verbose", action="store_true",
                   help="also print each mechanism and falsification test")
    p.set_defaults(fn=cmd_strategies)

    args = parser.parse_args()
    # Runtime selection must happen before logging, state, PID or journal
    # access. Demo and live therefore cannot share operational files.
    try:
        cfg = load_cfg(args.config)
        state.configure_runtime(cfg["mode"])
    except (OSError, yaml.YAMLError, ConfigError,
            state.RuntimeIdentityError) as exc:
        _print_configuration_error(exc)
        return 2
    # These controls write only local durable state. They must remain usable
    # during credential loss, .env rotation or an exchange outage. Commands
    # that also flatten load credentials only after PAUSED/KILLED is saved.
    # "strategies" only reads the register and the local config, so it must
    # work before .env exists - it is the command that tells a new operator
    # what may be configured in the first place.
    credential_free = args.command in {"pause", "resume", "kill", "strategies"}
    try:
        if not credential_free:
            load_secrets(cfg["mode"])
            state.bind_runtime_identity(
                cfg["mode"], os.getenv("OKX_API_KEY") or "")
        setup_logging()
    except OSError as exc:
        if not credential_free:
            print(f"Logging startup failed: {exc}", file=sys.stderr)
            return 2
        print(f"WARNING: logging is unavailable: {exc}", file=sys.stderr)
    except (state.RuntimeIdentityError, KeyError) as exc:
        _print_configuration_error(exc)
        return 2
    if credential_free:
        # pause/resume/kill act on local state alone and are deliberately
        # given no config, so a malformed block cannot block a stop command.
        # "strategies" is the exception: it reports on the config itself.
        return _dispatch(args, cfg if args.command == "strategies" else None)
    return _dispatch(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
