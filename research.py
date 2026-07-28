#!/usr/bin/env python3
"""The research CLI: offline analysis of the corpus the agent already writes.

Nothing here touches the trading path. Every command is read-only against
``journal.db`` and, from batch 2 onward, a local price cache. No command
places an order, calls an LLM, or writes to the runtime state.

    python research.py corpus stats
    python research.py corpus stats --db runtime/demo/journal.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from research import corpus                                    # noqa: E402


def default_db(mode: str = "demo") -> Path:
    return REPO / "runtime" / mode / "journal.db"


def cmd_corpus_stats(args: argparse.Namespace) -> int:
    db = Path(args.db) if args.db else default_db(args.mode)
    if not db.exists():
        print(f"no journal at {db}", file=sys.stderr)
        print("The corpus is written by the running agent. If the agent has "
              "not run in this mode yet, there is nothing to read.",
              file=sys.stderr)
        return 1
    print(corpus.format_stats(corpus.stats(db)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="group", required=True)

    corpus_parser = sub.add_parser(
        "corpus", help="read the recorded model inputs and outcomes")
    corpus_sub = corpus_parser.add_subparsers(dest="command", required=True)

    stats = corpus_sub.add_parser(
        "stats",
        help="how much data is there, and is it enough to reject anything")
    stats.add_argument("--db", default=None, help="path to journal.db")
    stats.add_argument("--mode", default="demo", choices=["demo", "live"],
                       help="which runtime's journal to read (default: demo)")
    stats.set_defaults(func=cmd_corpus_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
