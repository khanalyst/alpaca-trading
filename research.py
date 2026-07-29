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

from research import (corpus, findings as findings_mod,         # noqa: E402
                      prices as prices_mod, protocol,
                      replay as replay_mod, score, stats, sweep)


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
        if report["vacuous"]:
            print("G2 VACUOUS: the corpus contains no recorded decisions, so "
                  "the replay\nreproduced 100% of nothing. That is not "
                  "evidence of fidelity. Run the agent\nuntil it has "
                  "proposed setups, then re-check before trusting any "
                  "number\ndownstream of this replay.", file=sys.stderr)
            return 4
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


def cmd_funnel(args: argparse.Namespace) -> int:
    """Publish the veto distribution. Gate G4, and it runs before any sweep.

    If the binding veto sits downstream of the strategy contract, then no
    setting of any contract parameter can increase the trade count, and a
    sweep would consume weeks to discover that the funnel narrows somewhere
    else. Measuring first is the cheapest week in the programme.
    """
    db = Path(args.db) if args.db else default_db(args.mode)
    if not db.exists():
        print(f"no journal at {db}", file=sys.stderr)
        return 1
    cfg = _load_config()
    cycles, outputs = _corpus_for(db)
    result = replay_mod.Replay(
        cfg, mode=args.replay_mode, price_cache=_price_cache(args)).run(
            cycles, outputs)
    funnel = score.funnel_from_replay(result)

    print(f"funnel over {result.cycles:,} cycles, mode {result.mode}\n")
    print(score.format_funnel(funnel))

    reason, share = score.dominant_veto(funnel)
    if reason:
        print(f"\ndominant veto: {reason} ({share:.1f}% of all vetoes)")
        if share > 30.0:
            print(
                "\nGate G4: one veto accounts for more than 30% of "
                "rejections.\nIf it sits downstream of the strategy "
                "contract, no contract parameter can\nincrease the trade "
                "count. Sweeping them would be wasted calendar time -\n"
                "fix the funnel first.")
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    spec = sweep.load_spec(args.spec)
    db = Path(args.db) if args.db else default_db(args.mode)
    if not db.exists():
        print(f"no journal at {db}", file=sys.stderr)
        return 1

    trips = corpus.stats(db)["matched_round_trips"]
    outlook = sweep.forecast(spec, trips)
    print(f"sweep: {spec.name}")
    print(f"hypothesis: {spec.hypothesis}\n")
    print(f"  {outlook['reason']}")
    if not outlook["should_run"] and not args.force:
        print("\nRefusing to run. Reporting the shortfall once is cheaper "
              "than reporting\nINSUFFICIENT_SAMPLE at every grid point, and "
              "each repetition of that\nverdict makes relaxing the rule "
              "more tempting.\n\nPrefer a conditioning axis, which reuses "
              "every trade instead of dividing\nthem. Use --force only if "
              "you intend to record a null result.", file=sys.stderr)
        return 3

    cfg = _load_config()
    cache = _price_cache(args)
    cycles, outputs = _corpus_for(db)

    if spec.is_conditioning():
        axis = spec.condition_axis
        result = replay_mod.Replay(cfg, mode=args.replay_mode,
                                   price_cache=cache).run(cycles, outputs)

        def value_of(decision):
            return (decision.enrichment or {}).get(axis.variable)

        buckets = sweep.partition(result.executed(), axis, value_of)
        scored = sweep.score_partition(buckets)
        # The family-wise correction is applied here rather than left to
        # whoever writes the summary. Several cells against a few hundred
        # round trips guarantees something looks significant.
        corrected = protocol.correct_family(scored)

        print(f"\nconditioning on {axis.variable}, "
              f"pre-registered buckets:\n")
        for name in [b.get("name") for b in axis.buckets]:
            row = corrected.get(name) or {
                "n": 0, "expectancy_r": 0.0, "mde_r": float("inf"),
                "verdict": stats.INSUFFICIENT_SAMPLE, "p_adjusted": 1.0}
            print(f"  {name:<20} n={row['n']:<5} "
                  f"expectancy {row['expectancy_r']:+.4f}R  "
                  f"MDE {row['mde_r']:.4f}R  "
                  f"p_adj {row.get('p_adjusted', 1.0):.3f}  {row['verdict']}")

        # Out-of-sample split per bucket, with the regime profile of both
        # windows beside it: a corpus spanning a volatility change makes the
        # split a regime test rather than a robustness test.
        print("\nout-of-sample (70/30 by time):\n")
        for name in [b.get("name") for b in axis.buckets]:
            rows = buckets.get(name) or []
            if len(rows) < 2:
                print(f"  {name:<20} too few observations to split")
                continue
            split = protocol.out_of_sample(rows)
            comparable = split["fit_regime"].get("comparable")
            note = ("" if comparable is not False
                    else "  REGIME SHIFT: this is a regime test, not a "
                         "robustness test")
            print(f"  {name:<20} survives={split['survives']}  "
                  f"fit n={split['fit']['n']} confirm n={split['confirm']['n']}"
                  f"{note}")

        print("\nOnly the corrected p is quoted. Every bucket is reported, "
              "including the\nempty ones: a programme that records only "
              "positives records only noise.")
        return 0

    from agent import variants as variant_mod
    registry = variant_mod.load_registry(REPO / "research" / "variants.yaml")
    print()
    settings = []
    for variant in sweep.expand(spec, registry):
        variant_cfg = variant_mod.apply(variant, cfg)
        result = replay_mod.Replay(
            variant_cfg, variant_id=variant.variant_id,
            mode=args.replay_mode, price_cache=cache).run(cycles, outputs)
        executed = result.executed()
        settings.append((variant.variant_id, executed))
        returns = [d.outcome["r_multiple"] for d in executed if d.outcome]
        row = score.score_returns(returns, label=variant.variant_id)
        print(f"  {variant.variant_id:<40} n={row['n']:<5} "
              f"expectancy {row['expectancy_r']:+.4f}R  {row['verdict']}")

    # The axis is the unit of decision, not the individual setting: a
    # hypothesis is never rejected on one parameter value.
    base_variant = registry.get(spec.base) or variant_mod.baseline(
        "momentum", "phase1-v2")
    baseline_result = replay_mod.Replay(
        variant_mod.apply(base_variant, cfg),
        variant_id=base_variant.variant_id, mode=args.replay_mode,
        price_cache=cache).run(cycles, outputs)
    verdict = protocol.evaluate_axis(settings, baseline_result.executed())
    print(f"\naxis verdict: {verdict.verdict}")
    print(f"  governing criterion: {verdict.governing_criterion}")
    print(f"  {verdict.detail}")
    if verdict.verdict == stats.INSUFFICIENT_SAMPLE:
        print("\nThe question is open, not answered in the negative. That "
              "distinction is\nthe one most likely to erode: see "
              "research/protocol.md.")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Regenerate every scorecard from the store, deterministically."""
    from agent import variants as variant_mod

    store = findings_mod.FindingsStore(
        args.store or findings_mod.DEFAULT_STORE)
    # Registered-but-unrun variants still get a card: "no sample yet" is a
    # state worth being able to see.
    for variant in variant_mod.load_registry(
            REPO / "research" / "variants.yaml").values():
        store.register(variant)

    written = findings_mod.write_scorecards(
        store, args.out or (REPO / "findings"))
    print(f"regenerated {len(written)} files under "
          f"{args.out or (REPO / 'findings')}")
    for path in written:
        print(f"  {path}")
    return 0


def cmd_cadence(args: argparse.Namespace) -> int:
    """B9.2 evidence: how much LLM spend buys a re-evaluation of nothing?

    The plan requires this published before the cadence split merges, and
    reframed per H-M: if expectancy also decays with latency from the bar
    close, aligning the decision cadence is an alpha fix and the cost saving
    is incidental.
    """
    db = Path(args.db) if args.db else default_db(args.mode)
    if not db.exists():
        print(f"no journal at {db}", file=sys.stderr)
        return 1
    cycles, _ = _corpus_for(db)
    if not cycles:
        print("no cycles in the corpus", file=sys.stderr)
        return 1

    seen: set = set()
    fresh_cycles = repeat_cycles = 0
    for cycle in cycles:
        fresh = False
        for symbol in cycle.symbols():
            signal_ts = cycle.snapshot[symbol].get("signal_ts")
            if signal_ts is None:
                continue
            key = (symbol, int(signal_ts))
            if key not in seen:
                seen.add(key)
                fresh = True
        fresh_cycles += 1 if fresh else 0
        repeat_cycles += 0 if fresh else 1

    total = fresh_cycles + repeat_cycles
    share = repeat_cycles / total if total else 0.0
    print(f"decision cadence, over {total:,} cycles\n")
    print(f"  produced a fresh signal bar   {fresh_cycles:>7,}  "
          f"({fresh_cycles / total:.1%})")
    print(f"  saw only evaluated bars       {repeat_cycles:>7,}  "
          f"({share:.1%})")
    print()
    print("An LLM call on a repeat cycle cannot produce a fresh evaluation "
          "for a symbol\nalready evaluated this bar - "
          "strategy.evaluated_signal blocks it.")
    if share > 0.5:
        saving = 1.0 / max(1e-9, 1.0 - share)
        print(f"\nAligning cycle.decision_interval_seconds to the signal bar "
              f"cuts roughly\n{share:.0%} of LLM calls (~{saving:.1f}x). "
              "Housekeeping, both circuit breakers,\nreconciliation and the "
              "max-hold force close keep running at\ncycle.interval_seconds.")
    else:
        print("\nBelow half. The plan's own advice applies: if the answer is "
              "not 'more than\na handful', do not make the change.")
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

    funnel = sub.add_parser(
        "funnel",
        help="publish the veto distribution (gate G4; run before any sweep)")
    funnel.add_argument("--db", default=None)
    funnel.add_argument("--mode", default="demo", choices=["demo", "live"])
    funnel.add_argument("--prices", default=None)
    funnel.add_argument("--replay-mode", default="recorded_llm",
                        choices=replay_mod.MODES)
    funnel.set_defaults(func=cmd_funnel)

    sweep_parser = sub.add_parser(
        "sweep", help="run a registered parameter or conditioning axis")
    sweep_parser.add_argument("spec", help="path to a research/sweeps/*.yaml")
    sweep_parser.add_argument("--db", default=None)
    sweep_parser.add_argument("--mode", default="demo",
                              choices=["demo", "live"])
    sweep_parser.add_argument("--prices", default=None)
    sweep_parser.add_argument("--replay-mode", default="recorded_llm",
                              choices=replay_mod.MODES)
    sweep_parser.add_argument(
        "--force", action="store_true",
        help="run an underpowered grid anyway, to record a null result")
    sweep_parser.set_defaults(func=cmd_sweep)

    report_parser = sub.add_parser(
        "report", help="regenerate the committed scorecards")
    report_parser.add_argument("--store", default=None)
    report_parser.add_argument("--out", default=None)
    report_parser.set_defaults(func=cmd_report)

    cadence = sub.add_parser(
        "cadence",
        help="B9.2 evidence: share of cycles that re-observe evaluated bars")
    cadence.add_argument("--db", default=None)
    cadence.add_argument("--mode", default="demo", choices=["demo", "live"])
    cadence.set_defaults(func=cmd_cadence)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
