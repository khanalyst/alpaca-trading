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

from research import (corpus, prices as prices_mod,             # noqa: E402
                      replay as replay_mod, stats)


def default_db(mode: str = "demo") -> Path:
    return REPO / "runtime" / mode / "journal.db"


def _load_config() -> dict:
    import yaml
    from agent.config import validate_config
    raw = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    return validate_config(raw)


def _resolve_cfg(variant_id: str) -> dict:
    from agent import variants as variant_mod
    base = _load_config()
    if variant_id in (None, "", "live", "momentum.baseline"):
        return base
    registry = variant_mod.load_registry(REPO / "research" / "variants.yaml")
    if variant_id not in registry:
        raise SystemExit(
            f"unknown variant {variant_id!r}. Registered: "
            f"{', '.join(sorted(registry)) or 'none'}")
    return variant_mod.apply(registry[variant_id], base)


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


def _price_cache(args: argparse.Namespace):
    """A cache, or None. Research is offline unless a path is given."""
    path = getattr(args, "prices", None)
    if not path:
        return None
    return prices_mod.PriceCache(path)


def _corpus_for(db: Path):
    cycles, report = corpus.load_cycles(db)
    outputs, _ = corpus.load_model_outputs(db)
    if report.skipped:
        print(f"note: skipped {report.skipped} unparseable llm_input rows "
              f"({dict(report.reasons)})", file=sys.stderr)
    return cycles, outputs


def cmd_replay(args: argparse.Namespace) -> int:
    db = Path(args.db) if args.db else default_db(args.mode)
    if not db.exists():
        print(f"no journal at {db}", file=sys.stderr)
        return 1
    cfg = _resolve_cfg(args.variant)
    cycles, outputs = _corpus_for(db)
    result = replay_mod.Replay(
        cfg, variant_id=args.variant, mode=args.replay_mode,
        price_cache=_price_cache(args)).run(cycles, outputs)

    print(f"variant {result.variant_id}  mode {result.mode}  "
          f"cycles {result.cycles:,}")
    funnel = result.funnel
    print(f"\ncontract fired   {funnel['fired']:>8,}")
    print(f"  -> proposed    {funnel['proposed']:>8,}")
    print(f"  -> vetoed      {funnel['vetoed']:>8,}")
    print(f"  -> executed    {funnel['executed']:>8,}")
    if funnel["veto_reasons"]:
        print("\nveto reasons:")
        for reason, count in sorted(funnel["veto_reasons"].items(),
                                    key=lambda kv: -kv[1])[:12]:
            print(f"  {count:>6,}  {reason}")

    if args.check_fidelity:
        report = replay_mod.fidelity(result, db)
        print(f"\nG2 fidelity: {report['reproduction_rate']:.4%} "
              f"({report['matched']}/{report['recorded']} recorded "
              f"decisions reproduced)")
        if not report["passes_g2"]:
            print("G2 FAILED. Every number downstream of this replay is "
                  "worthless until it is explained. This is a full stop, "
                  "not a debugging task to work around.", file=sys.stderr)
            return 2
    return 0


def cmd_three_arm(args: argparse.Namespace) -> int:
    """H-E: is the LLM's value in selection, in rejection, or absent?"""
    db = Path(args.db) if args.db else default_db(args.mode)
    if not db.exists():
        print(f"no journal at {db}", file=sys.stderr)
        return 1
    cfg = _load_config()
    cycles, outputs = _corpus_for(db)
    cache = _price_cache(args)
    if cache is None:
        print("note: no --prices cache given, so no outcomes are resolved "
              "and every arm scores on an empty return series.",
              file=sys.stderr)

    arms = {}
    for mode in replay_mod.MODES:
        result = replay_mod.Replay(
            cfg, mode=mode, price_cache=cache).run(cycles, outputs)
        returns = [d.outcome["r_multiple"] for d in result.executed()
                   if d.outcome]
        arms[mode] = {"result": result, "returns": returns}

    print("H-E, three arms. A two-arm test cannot tell a poor selector that "
          "is a good vetoer\nfrom a component that does not earn its keep.\n")
    labels = {
        "deterministic": "A null      (contract fires, take it)",
        "recorded_llm": "B llm       (the model's own decisions)",
        "deterministic_vetoed": "C veto      (contract proposes, model may "
                                "only suppress)",
    }
    for mode, data in arms.items():
        interval = stats.bootstrap_mean(data["returns"])
        mde = stats.minimum_detectable_effect(data["returns"])
        executed = data["result"].funnel["executed"]
        print(f"  {labels[mode]}")
        print(f"      executed {executed:>6,}   mean R {interval}")
        print(f"      MDE at this n: {mde:.4f}R\n")

    for left, right in (("recorded_llm", "deterministic"),
                        ("deterministic_vetoed", "deterministic"),
                        ("deterministic_vetoed", "recorded_llm")):
        diff = stats.bootstrap_difference(
            arms[left]["returns"], arms[right]["returns"])
        verdict = ("differs" if diff.excludes_zero()
                   else stats.INSUFFICIENT_SAMPLE)
        print(f"  {left} - {right}: {diff}   {verdict}")

    print("\nIf all three intervals overlap at the sample available, the "
          "answer is\nINSUFFICIENT_SAMPLE, not 'the LLM is fine'.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="group", required=True)

    corpus_parser = sub.add_parser(
        "corpus", help="read the recorded model inputs and outcomes")
    corpus_sub = corpus_parser.add_subparsers(dest="command", required=True)

    stats_parser = corpus_sub.add_parser(
        "stats",
        help="how much data is there, and is it enough to reject anything")
    stats_parser.add_argument("--db", default=None,
                              help="path to journal.db")
    stats_parser.add_argument(
        "--mode", default="demo", choices=["demo", "live"],
        help="which runtime's journal to read (default: demo)")
    stats_parser.set_defaults(func=cmd_corpus_stats)

    replay_parser = sub.add_parser(
        "replay", help="re-derive what a variant would have decided")
    replay_parser.add_argument("--db", default=None)
    replay_parser.add_argument("--mode", default="demo",
                               choices=["demo", "live"])
    replay_parser.add_argument(
        "--variant", default="momentum.baseline",
        help="registered variant id (see research/variants.yaml)")
    replay_parser.add_argument(
        "--replay-mode", default="recorded_llm", choices=replay_mod.MODES,
        help="proposer mode")
    replay_parser.add_argument(
        "--prices", default=None,
        help="path to a 1m price cache; without it no outcome is resolved")
    replay_parser.add_argument(
        "--check-fidelity", action="store_true",
        help="gate G2: assert the baseline reproduces recorded decisions")
    replay_parser.set_defaults(func=cmd_replay)

    three = sub.add_parser(
        "three-arm",
        help="H-E: does the LLM select, veto, or neither")
    three.add_argument("--db", default=None)
    three.add_argument("--mode", default="demo", choices=["demo", "live"])
    three.add_argument(
        "--prices", default=None,
        help="path to a 1m price cache; required to score any arm")
    three.set_defaults(func=cmd_three_arm)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
