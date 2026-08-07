"""Small command line boundary for evidence verification and replay import."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .evidence_package import (
    EvidenceValidationError,
    canonical_json,
    import_replay_fixture,
    replay_fixture,
    verify_evidence_package,
    verify_golden_replay,
)


def _json_object(path: str | None, description: str) -> dict | None:
    if path is None:
        return None
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceValidationError(
            f"{description} is not readable JSON: {path}") from exc
    if not isinstance(value, dict):
        raise EvidenceValidationError(f"{description} must be a JSON object")
    return value


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="python -m research.evidence_cli",
        description="Research evidence only; no promotion or trading actions.")
    commands = root.add_subparsers(dest="command", required=True)

    imported = commands.add_parser(
        "import-replay", help="sanitize a journal or existing replay fixture")
    imported.add_argument("source")
    imported.add_argument("destination")
    imported.add_argument(
        "--classification", choices=("synthetic", "real_market"),
        default="synthetic")
    imported.add_argument(
        "--provenance-manifest",
        help="required hash-bound JSON attestation for real_market journals")
    imported.add_argument(
        "--config-json", help="exact replay config (JSON; secrets are removed)")
    imported.add_argument("--variant-id", default="UNSET")
    imported.add_argument(
        "--mode", choices=(
            "deterministic", "recorded_llm", "deterministic_vetoed"),
        default="recorded_llm")

    verified = commands.add_parser(
        "verify-package", help="verify a content-addressed evidence package")
    verified.add_argument("package")
    verified.add_argument("--code-root")
    verified.add_argument("--config")

    run = commands.add_parser(
        "run-replay", help="run a sanitized fixture through production replay")
    run.add_argument("fixture")

    golden = commands.add_parser(
        "verify-golden", help="compare replay with checked expected output")
    golden.add_argument("fixture")
    golden.add_argument("expected")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "import-replay":
            provenance = _json_object(
                args.provenance_manifest, "provenance manifest")
            config = _json_object(args.config_json, "replay config")
            output = import_replay_fixture(
                args.source, args.destination,
                classification=args.classification,
                provenance=provenance, config=config,
                variant_id=args.variant_id, mode=args.mode)
            print(output)
        elif args.command == "verify-package":
            manifest = verify_evidence_package(
                args.package, code_root=args.code_root,
                config_path=args.config)
            print(manifest["evidence_id"])
        elif args.command == "run-replay":
            print(canonical_json(replay_fixture(args.fixture)))
        else:
            print(canonical_json(
                verify_golden_replay(args.fixture, args.expected)))
    except EvidenceValidationError as exc:
        print(f"evidence error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
