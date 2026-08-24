"""Operator CLI adapter for the isolated paper-primary/shadow epoch store."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from .paper_epoch import DEFAULT_DB_PATH, PaperEpochStore
from .paper_epoch_export import DEFAULT_OUTPUT_ROOT, write_paper_epoch_export


def _document(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"paper epoch input is unreadable: {source}") from exc
    if not isinstance(value, dict):
        raise ValueError("paper epoch input must be one JSON object")
    return value


def _without_schema(value: Any, expected: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{expected} payload must be an object")
    result = dict(value)
    schema = result.pop("schema", expected)
    if schema != expected:
        raise ValueError(f"expected schema {expected}, got {schema!r}")
    return result


def cmd_paper_epoch(args: argparse.Namespace) -> int:
    """Execute one bounded lifecycle operation and print structured JSON."""
    if args.action == "export":
        if not args.epoch:
            raise ValueError("export requires --epoch")
        # Export is explicitly a read path.  The writer also opens a supplied
        # path read-only, but constructing the store here keeps the CLI
        # contract obvious and prevents accidental lifecycle writes.
        store = PaperEpochStore(args.db, readonly=True)
        result = write_paper_epoch_export(
            store,
            args.epoch,
            getattr(args, "output_root", DEFAULT_OUTPUT_ROOT),
            webhook_url=getattr(args, "webhook_url", None),
            webhook_timeout_seconds=getattr(args, "webhook_timeout_seconds", 5.0),
        )
        print(json.dumps(result.as_dict(), sort_keys=True, default=str))
        return 0
    needs_epoch = args.action in {"start", "record", "stop", "complete", "seal"}
    needs_input = args.action in {"create", "start", "record", "seal"}
    if needs_epoch and not args.epoch:
        raise ValueError(f"{args.action} requires --epoch")
    if needs_input and not args.input:
        raise ValueError(f"{args.action} requires --input")
    if args.action == "stop" and not args.reason:
        raise ValueError("stop requires --reason")
    readonly = args.action in {"status", "verify"}
    store = PaperEpochStore(args.db, readonly=readonly)
    if args.action == "create":
        payload = _without_schema(
            _document(args.input), "paper-epoch-create.v1")
        frozen = _without_schema(
            payload.pop("frozen", None), "paper-frozen-epoch.v1")
        primary = payload.pop("primary", None)
        shadows = payload.pop("shadows", None)
        if not isinstance(primary, Mapping):
            raise ValueError("paper epoch create requires one primary object")
        if not isinstance(shadows, list):
            raise ValueError("paper epoch create requires a shadows array")
        allowed = {
            "predecessor_epoch_id", "confirmation", "epoch_id",
            "trader_account_fingerprint", "trader_runtime_fingerprint",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError(
                "unknown paper epoch create fields: " + ", ".join(unknown))
        result = store.create_epoch(
            frozen, dict(primary), shadows,
            predecessor_epoch_id=payload.get("predecessor_epoch_id"),
            confirmation=payload.get("confirmation"),
            epoch_id=payload.get("epoch_id"),
            trader_account_fingerprint=payload.get(
                "trader_account_fingerprint"),
            trader_runtime_fingerprint=payload.get(
                "trader_runtime_fingerprint"))
    elif args.action == "start":
        result = store.start_epoch(args.epoch, _document(args.input))
    elif args.action == "record":
        payload = _without_schema(
            _document(args.input), "paper-shadow-outcome-input.v1")
        allowed = {"opportunity_id", "stream_event_id", "observations"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError(
                "unknown paper outcome fields: " + ", ".join(unknown))
        observations = payload.get("observations")
        if not isinstance(observations, list):
            raise ValueError("paper outcome observations must be an array")
        result = store.record_outcome(
            args.epoch, payload.get("opportunity_id"),
            payload.get("stream_event_id"), observations)
    elif args.action == "stop":
        result = store.stop_epoch(args.epoch, args.reason)
    elif args.action == "complete":
        result = store.complete_epoch(args.epoch)
    elif args.action == "seal":
        payload = _without_schema(
            _document(args.input), "paper-sealed-lessons-input.v1")
        if set(payload) != {"lessons"} or not isinstance(
                payload.get("lessons"), list):
            raise ValueError("lesson input must contain only a lessons array")
        result = store.seal_lessons(args.epoch, payload["lessons"])
    elif args.action == "status":
        result = (store.epoch(args.epoch) if args.epoch else {
            "schema": "paper-epochs.v1",
            "epochs": store.epochs(),
        })
    elif args.action == "verify":
        result = store.verify_integrity()
    else:  # pragma: no cover - argparse constrains this branch.
        raise ValueError(f"unsupported paper epoch action: {args.action}")
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


def add_paper_epoch_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = sub.add_parser(
        "paper-epoch",
        help="manage an isolated frozen paper-primary/shadow research epoch")
    parser.add_argument(
        "action",
        choices=("create", "start", "record", "stop", "complete", "seal",
                 "status", "verify", "export"))
    parser.add_argument(
        "--db", default=str(DEFAULT_DB_PATH),
        help="separate paper-epoch SQLite outcome store")
    parser.add_argument("--epoch", default=None)
    parser.add_argument("--input", default=None, help="bounded JSON input")
    parser.add_argument("--reason", default=None)
    parser.add_argument(
        "--output-root", default=str(DEFAULT_OUTPUT_ROOT),
        help="root directory for immutable canonical JSONL exports")
    parser.add_argument(
        "--webhook-url", default=None,
        help="optional HTTPS metadata notification URL (best effort)")
    parser.add_argument(
        "--webhook-timeout-seconds", type=float, default=5.0,
        help="HTTPS webhook timeout in seconds (default: 5)")
    parser.set_defaults(func=cmd_paper_epoch)
    return parser


__all__ = ["add_paper_epoch_parser", "cmd_paper_epoch"]
