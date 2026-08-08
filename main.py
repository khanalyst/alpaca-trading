#!/usr/bin/env python3
"""Command line control for the Alpaca paper-trading runtime."""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agent.config import ConfigError, validate_config
from agent.engine import Engine


def load_cfg(path: str | Path = ROOT / "config.yaml") -> dict:
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError("PyYAML is required to load YAML configuration") from exc
    with open(path, encoding="utf-8") as handle:
        try:
            raw = yaml.safe_load(handle) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML: {exc}") from exc
    return validate_config(raw)


def _plain(value):
    """Convert provider dataclasses/enums into CLI-safe plain mappings."""
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
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError("PyYAML is required for CLI output") from exc
    return yaml.safe_dump(_plain(value), sort_keys=False,
                         default_flow_style=False).rstrip()


def _engine(cfg, light=False):
    return Engine(cfg, light=light)


def cmd_check(args, cfg) -> int:
    engine = _engine(cfg, light=True)
    try:
        result = engine.check(authenticated=bool(args.authenticated))
    except Exception as exc:  # noqa: BLE001
        print(f"Authenticated check failed: {exc}", file=sys.stderr)
        return 1
    print(_dump_yaml(result))
    return 0


def cmd_status(args, cfg) -> int:
    result = _engine(cfg, light=True).status()
    print(_dump_yaml(result))
    return 0


def cmd_run(args, cfg) -> int:
    engine = _engine(cfg)
    try:
        engine.run(max_cycles=args.cycles)
    except Exception as exc:  # noqa: BLE001
        print(f"run failed: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_flatten(args, cfg) -> int:
    try:
        _engine(cfg, light=True).flatten_all(args.reason)
    except Exception as exc:  # noqa: BLE001
        print(f"flatten failed: {exc}", file=sys.stderr)
        return 1
    print("Flatten requested")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Alpaca paper trading agent")
    p.add_argument("--config", default=str(ROOT / "config.yaml"))
    p.add_argument("--env-file", default=None)
    sub = p.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="validate config; no network by default")
    check.add_argument("--authenticated", action="store_true", help="query account and broker clock")
    check.set_defaults(fn=cmd_check)
    status = sub.add_parser("status")
    status.set_defaults(fn=cmd_status)
    run = sub.add_parser("run")
    run.add_argument("--cycles", type=int, default=None)
    run.set_defaults(fn=cmd_run)
    flatten = sub.add_parser("flatten")
    flatten.add_argument("--reason", default="operator")
    flatten.set_defaults(fn=cmd_flatten)
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
