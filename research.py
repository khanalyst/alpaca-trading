#!/usr/bin/env python3
"""The research CLI: offline analysis of the corpus the agent already writes.

Nothing here touches the trading path. Commands read ``journal.db`` and, from
batch 2 onward, a local price cache. A G2 check appends its pass/fail audit
record to that journal unless ``--no-persist`` is supplied; research evidence
is persisted in schema 16. The
dedicated ``research-loop`` command may call the LLM with a research-only
prompt, and ``backup`` creates a new verified versioned snapshot. No command
places an order or mutates trading state.

    python research.py corpus stats
    python research.py corpus stats --db runtime/demo/journal.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from research import (backup as backup_mod, corpus,             # noqa: E402
                      artifact as artifact_mod, findings as findings_mod,
                      prices as prices_mod, protocol,
                      readiness as readiness_mod,
                      replay as replay_mod, review as review_mod,
                      score, stats, sweep)


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
    baseline_id = variant_mod.baseline_variant_id(
        str(base["strategy"]["id"]))
    if variant_id in (None, "", "live", baseline_id):
        return base
    registry = variant_mod.load_registry(REPO / "research" / "variants.yaml")
    if variant_id not in registry:
        raise SystemExit(
            f"unknown variant {variant_id!r}. Registered: "
            f"{', '.join(sorted(registry)) or 'none'}")
    return variant_mod.apply(registry[variant_id], base)


def _load_experiment_registry(cfg: dict, store) -> dict:
    """Load file, registered hypothesis, and persisted adaptive variants."""
    from agent import variants as variant_mod

    registry = variant_mod.load_registry(REPO / "research" / "variants.yaml")
    strategy = cfg.get("strategy") or {}
    strategy_id = str(strategy.get("id") or "")
    base_version = str(strategy.get("version") or "")
    if strategy_id and base_version:
        # Six of the seven registered strategy ids contain a hyphen, which is
        # not legal in a variant id. The prefix helper is the one place that
        # conversion lives; keying this by the raw id would file the baseline
        # under a name nothing else ever looks up.
        registry.setdefault(
            variant_mod.baseline_variant_id(strategy_id),
            variant_mod.baseline(strategy_id, base_version),
        )
        for variant in variant_mod.hypothesis_variants(
                strategy_id, base_version):
            registry.setdefault(variant.variant_id, variant)

    # The store is authoritative for identities that have already run: this
    # preserves a mutable result status and rehydrates exact adaptive values
    # that are not present in the hand-edited YAML registry.
    for row in store.variants():
        try:
            variant = variant_mod.Variant(
                variant_id=str(row["variant_id"]),
                strategy_id=str(row["strategy_id"]),
                base_version=str(row["base_version"]),
                overrides=json.loads(row["overrides_json"] or "{}"),
                hypothesis=str(row["hypothesis"] or ""),
                status=str(row["status"]),
                hypothesis_id=row.get("hypothesis_id"),
                hypothesis_params=json.loads(
                    row.get("hypothesis_params_json") or "{}"),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            # Invalid legacy rows cannot safely participate in a new run.
            continue
        registry[variant.variant_id] = variant
    return registry


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


DEFAULT_STAGING_PATH = "research/cache/staging.db"
DEFAULT_RESEARCH_REVIEW_CAP = 8


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _configured_path(
        cfg: dict, key: str, requested: str | Path | None,
        fallback: str | Path) -> Path:
    """Resolve one research path from an explicit override or config."""
    configured = requested or (cfg.get("research") or {}).get(key) or fallback
    path = Path(configured)
    return path if path.is_absolute() else REPO / path


def _configured_staging(cfg: dict,
                       requested: str | Path | None = None) -> Path:
    return _configured_path(cfg, "staging_store", requested,
                            DEFAULT_STAGING_PATH)


def _corpus_for(db: Path):
    cycles, report = corpus.load_cycles(db)
    outputs, _ = corpus.load_model_outputs(db)
    if report.skipped:
        print(f"note: skipped {report.skipped} unparseable llm_input rows "
              f"({dict(report.reasons)})", file=sys.stderr)
    return cycles, outputs


def _g2_replay_window(db: Path, cycles: list, outputs: list) -> tuple[list, list]:
    """Restrict a strict G2 replay to cycles at/after the journal boundary."""
    rows, _ = corpus.load_g2_proposals(db)
    evidence = corpus.g2_evidence_window(rows)
    allowed = corpus.g2_post_boundary_cycle_ids(db, evidence)
    if allowed is None:
        return cycles, outputs
    return (
        [cycle for cycle in cycles if cycle.cycle_id in allowed],
        [output for output in outputs if output.cycle_id in allowed],
    )


def _g2_captured_corpus(
        db: Path) -> tuple[list, list, dict, list[str]]:
    """Capture the G2 window before loading the exact replay inputs."""
    captured = replay_mod.g2_capture_metadata(db)
    allowed = captured.pop("_allowed_cycle_ids", None)
    cycles, outputs = _corpus_for(db)
    if allowed is not None:
        cycles = [cycle for cycle in cycles if cycle.cycle_id in allowed]
        outputs = [output for output in outputs if output.cycle_id in allowed]
    current = replay_mod.g2_evidence_metadata(db)
    changed = replay_mod.g2_metadata_changes(captured, current)
    return cycles, outputs, captured, changed


def _proposal_snapshot(db: Path) -> tuple[int, float]:
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(ts), 0) FROM events "
            "WHERE kind='setup_proposed'").fetchone()
    return int(row[0]), float(row[1])


def _latest_g2_payload(db: Path) -> dict | None:
    if not db.exists():
        return None
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT payload FROM events WHERE kind='research_gate_result' "
            "ORDER BY ts DESC").fetchall()
    for (raw,) in rows:
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("gate") == "G2":
            return payload
    return None


def _paper_decisions(rows: list) -> list:
    return protocol.paper_trade_decisions(rows)


def _record_g2_result(
        db: Path, status: str, report: dict, result, cfg: dict,
        captured_metadata: dict | None = None) -> bool:
    """Append a durable G2 audit record to the journal being checked."""
    from agent import state

    proposals, max_proposal_ts = _proposal_snapshot(db)
    evidence_metadata = replay_mod.g2_evidence_metadata(db)
    if captured_metadata is None:
        captured_metadata = {
            key: report[key]
            for key in replay_mod.G2_METADATA_FIELDS if key in report
        }
    captured_metadata = dict(captured_metadata or evidence_metadata)
    changed_fields = replay_mod.g2_metadata_changes(
        captured_metadata, evidence_metadata)
    capture_stale = bool(changed_fields)
    forced_failed = capture_stale and status == "PASS"
    capture_stale_reason = (
        "authoritative G2 replay corpus changed after replay capture: "
        + ", ".join(changed_fields)
        if capture_stale else report.get("capture_stale_reason", ""))
    if capture_stale:
        report["capture_stale"] = True
        report["capture_stale_reason"] = capture_stale_reason
    if forced_failed:
        status = "FAILED"
    malformed_reasons = dict(report.get("malformed_reasons", {}))
    if capture_stale:
        malformed_reasons["authoritative_corpus_changed_after_replay"] = 1
    payload = json.dumps({
        "gate": "G2",
        "status": status,
        "proposal_count": proposals,
        "max_proposal_ts": max_proposal_ts,
        "matched": report["matched"],
        "recorded": report["recorded"],
        "reproduction_rate": report["reproduction_rate"],
        "missing_count": report.get("missing_count", 0),
        "extra_count": report.get("extra_count", 0),
        "malformed_count": report.get("malformed_count", 0),
        "duplicate_count": report.get("duplicate_count", 0),
        "unresolved_count": report.get("unresolved_count", 0),
        "malformed_reasons": malformed_reasons,
        "capture_stale": capture_stale or bool(
            report.get("capture_stale", False)),
        "capture_stale_reason": capture_stale_reason,
        "vacuous": bool(report.get(
            "vacuous", int(report.get("recorded", 0) or 0) == 0)),
        "legacy_identity": bool(captured_metadata.get(
            "legacy_identity", evidence_metadata["legacy_identity"])),
        "modern_evidence_count": int(
            captured_metadata.get("modern_evidence_count",
                                 evidence_metadata["modern_evidence_count"])
            or 0),
        "legacy_excluded_count": int(
            captured_metadata.get("legacy_excluded_count",
                                 evidence_metadata["legacy_excluded_count"])
            or 0),
        "legacy_after_boundary_count": int(
            captured_metadata.get("legacy_after_boundary_count",
                                 evidence_metadata["legacy_after_boundary_count"])
            or 0),
        "modern_boundary_ts": captured_metadata.get(
            "modern_boundary_ts", evidence_metadata["modern_boundary_ts"]),
        "modern_boundary_rowid": captured_metadata.get(
            "modern_boundary_rowid", evidence_metadata["modern_boundary_rowid"]),
        "modern_boundary_cycle_id": captured_metadata.get(
            "modern_boundary_cycle_id",
            evidence_metadata["modern_boundary_cycle_id"]),
        "corpus_digest": captured_metadata.get(
            "corpus_digest", report.get(
                "corpus_digest", replay_mod.g2_corpus_digest(db))),
        "replay_corpus_digest": captured_metadata.get(
            "replay_corpus_digest", evidence_metadata["replay_corpus_digest"]),
        "replay_corpus_count": int(report.get(
            "replay_corpus_count",
            captured_metadata.get("replay_corpus_count",
                                 evidence_metadata["replay_corpus_count"]))
            or 0),
        "replay_corpus_max_rowid": int(report.get(
            "replay_corpus_max_rowid",
            captured_metadata.get("replay_corpus_max_rowid",
                                 evidence_metadata["replay_corpus_max_rowid"]))
            or 0),
        "replay_digest": result.digest(),
        "variant_id": result.variant_id,
        "replay_mode": result.mode,
        "strategy_config_version": state.strategy_fingerprint(cfg),
        "fidelity_code_version": replay_mod.fidelity_code_fingerprint(),
    }, sort_keys=True)
    try:
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO events (ts, kind, payload) VALUES (?,?,?)",
                (time.time(), "research_gate_result", payload))
    except sqlite3.Error as exc:
        print(f"could not persist G2 result to {db}: {exc}", file=sys.stderr)
        return False
    # The audit row is retained as FAILED evidence, but a caller that had
    # believed this was a PASS must stop downstream work after the race.
    return not forced_failed


def _require_g2(db: Path, cfg: dict, cycles: list, outputs: list) -> int:
    """Refuse downstream research unless baseline replay fidelity is real."""
    loaded_cycles, loaded_outputs, captured_metadata, changed = (
        _g2_captured_corpus(db))
    # Replace the caller's lists so every downstream arm uses the exact rows
    # that were loaded after the pre-capture check.
    cycles[:] = loaded_cycles
    outputs[:] = loaded_outputs
    if changed:
        reason = ("authoritative G2 replay corpus changed while loading "
                  "the replay window: " + ", ".join(changed))
        print(f"G2 capture stale: {reason}", file=sys.stderr)
        return 2
    from agent.variants import baseline_variant_id

    baseline_id = baseline_variant_id(str(cfg["strategy"]["id"]))
    baseline = replay_mod.Replay(
        cfg, variant_id=baseline_id, mode="recorded_llm").run(
        cycles, outputs)
    baseline.g2_metadata = captured_metadata
    report = replay_mod.fidelity(
        baseline, db, captured_metadata=captured_metadata)
    print(f"G2 fidelity: {report['reproduction_rate']:.4%} "
          f"({report['matched']}/{report['recorded']})")
    if report["vacuous"]:
        _record_g2_result(
            db, "VACUOUS", report, baseline, cfg, captured_metadata)
        print("G2 VACUOUS: no recorded setup proposals exist. Downstream "
              "research is blocked until fidelity can be measured.",
              file=sys.stderr)
        return 4
    if report["recorded"] < readiness_mod.MIN_PROPOSALS_FOR_G2:
        _record_g2_result(
            db, "INSUFFICIENT_SAMPLE", report, baseline, cfg,
            captured_metadata)
        print(
            f"G2 COLLECTING: {report['recorded']} recorded proposals; "
            f"{readiness_mod.MIN_PROPOSALS_FOR_G2} are required before the "
            "fidelity ratio can unlock downstream research.",
            file=sys.stderr)
        return 4
    if not report["passes_g2"]:
        _record_g2_result(
            db, "FAILED", report, baseline, cfg, captured_metadata)
        print("G2 FAILED: downstream research is blocked until every "
              "mismatch is explained.", file=sys.stderr)
        return 2
    if not _record_g2_result(
            db, "PASS", report, baseline, cfg, captured_metadata):
        if report.get("capture_stale_reason"):
            print(f"G2 capture stale: {report['capture_stale_reason']}",
                  file=sys.stderr)
        print("G2 passed but its audit record was not stored; downstream "
              "research remains blocked.", file=sys.stderr)
        return 5
    return 0


def _run_id(subject: str, result) -> str:
    return f"{subject}:{result.digest()}:{uuid.uuid4().hex}"


def cmd_replay(args: argparse.Namespace) -> int:
    db = Path(args.db) if args.db else default_db(args.mode)
    if not db.exists():
        print(f"no journal at {db}", file=sys.stderr)
        return 1
    cfg = _load_config()
    from agent.variants import baseline_variant_id

    variant_id = args.variant or baseline_variant_id(
        str(cfg["strategy"]["id"]))
    cfg = _resolve_cfg(variant_id)
    captured_metadata = None
    if args.check_fidelity:
        cycles, outputs, captured_metadata, changed = _g2_captured_corpus(db)
        if changed:
            reason = ("authoritative G2 replay corpus changed while loading "
                      "the replay window: " + ", ".join(changed))
            print(f"G2 capture stale: {reason}", file=sys.stderr)
            return 2
    else:
        cycles, outputs = _corpus_for(db)
    result = replay_mod.Replay(
        cfg, variant_id=variant_id, mode=args.replay_mode,
        price_cache=_price_cache(args)).run(cycles, outputs)
    result.g2_metadata = captured_metadata

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
        persist_fidelity = not bool(getattr(args, "no_persist", False))
        baseline_id = baseline_variant_id(str(cfg["strategy"]["id"]))
        if (variant_id not in ("live", baseline_id)
                or args.replay_mode != "recorded_llm"):
            print(f"G2 must use {baseline_id} in recorded_llm mode.",
                  file=sys.stderr)
            return 2
        report = replay_mod.fidelity(
            result, db, captured_metadata=captured_metadata)
        print(f"\nG2 fidelity: {report['reproduction_rate']:.4%} "
              f"({report['matched']}/{report['recorded']} recorded "
              f"decisions reproduced)")
        if not persist_fidelity:
            print("G2 audit persistence disabled (--no-persist).")
        if report["vacuous"]:
            if persist_fidelity:
                _record_g2_result(
                    db, "VACUOUS", report, result, cfg, captured_metadata)
            print("G2 VACUOUS: the corpus contains no recorded decisions, so "
                  "the replay\nreproduced 100% of nothing. That is not "
                  "evidence of fidelity. Run the agent\nuntil it has "
                  "proposed setups, then re-check before trusting any "
                  "number\ndownstream of this replay.", file=sys.stderr)
            return 4
        if report["recorded"] < readiness_mod.MIN_PROPOSALS_FOR_G2:
            if persist_fidelity:
                _record_g2_result(
                    db, "INSUFFICIENT_SAMPLE", report, result, cfg,
                    captured_metadata)
            print(
                f"G2 COLLECTING: {report['recorded']} recorded proposals; "
                f"{readiness_mod.MIN_PROPOSALS_FOR_G2} are required before "
                "this ratio can pass the gate.", file=sys.stderr)
            return 4
        if not report["passes_g2"]:
            if persist_fidelity:
                _record_g2_result(
                    db, "FAILED", report, result, cfg, captured_metadata)
            print("G2 FAILED. Every number downstream of this replay is "
                  "worthless until it is explained. This is a full stop, "
                  "not a debugging task to work around.", file=sys.stderr)
            return 2
        if (persist_fidelity
                and not _record_g2_result(
                    db, "PASS", report, result, cfg, captured_metadata)):
            if report.get("capture_stale_reason"):
                print(f"G2 capture stale: {report['capture_stale_reason']}",
                      file=sys.stderr)
            return 5
    return 0


def cmd_three_arm(args: argparse.Namespace) -> int:
    """H-E: is the LLM's value in selection, in rejection, or absent?"""
    db = Path(args.db) if args.db else default_db(args.mode)
    if not db.exists():
        print(f"no journal at {db}", file=sys.stderr)
        return 1
    cfg = _load_config()
    cycles, outputs = _corpus_for(db)
    gate = _require_g2(db, cfg, cycles, outputs)
    if gate:
        return gate
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

    comparisons = {}
    for left, right in (("recorded_llm", "deterministic"),
                        ("deterministic_vetoed", "deterministic"),
                        ("deterministic_vetoed", "recorded_llm")):
        comparison = protocol.paired_arm_comparison(
            arms[left]["result"].decisions,
            arms[right]["result"].decisions)
        diff = comparison["interval"]
        comparisons[f"{left}__minus__{right}"] = {
            **{key: value for key, value in comparison.items()
               if key != "interval"},
            "interval": {
                "point": diff.point, "low": diff.low,
                "high": diff.high, "n": diff.n,
            },
        }
        verdict = ("differs" if diff.excludes_zero()
                   else stats.INSUFFICIENT_SAMPLE)
        print(f"  {left} - {right}: {diff}   {verdict}")
        print(f"      paired {comparison['paired_n']:,}/"
              f"{comparison['proposal_union_n']:,} proposals "
              f"({comparison['pair_coverage_pct']:.1f}%); "
              f"left-only {comparison['left_only_proposals']:,}, "
              f"right-only {comparison['right_only_proposals']:,}, "
              f"duplicates {comparison['left_duplicates'] + comparison['right_duplicates']:,}")

    store = findings_mod.FindingsStore(_configured_store(cfg, args.store))
    analysis_id = store.record_analysis(
        "three_arm", str(cfg["strategy"]["id"]), {
            "journal": str(db),
            "fidelity_code_version": replay_mod.fidelity_code_fingerprint(),
            "arms": {
                mode: {"executed": data["result"].funnel["executed"],
                       "resolved": len(data["returns"])}
                for mode, data in arms.items()
            },
            "comparisons": comparisons,
        })
    print(f"\npersisted paired three-arm analysis {analysis_id} to {store.path}")

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
    gate = _require_g2(db, cfg, cycles, outputs)
    if gate:
        return gate
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
    gate = _require_g2(db, cfg, cycles, outputs)
    if gate:
        return gate

    from agent import variants as variant_mod
    registry = variant_mod.load_registry(REPO / "research" / "variants.yaml")
    store = findings_mod.FindingsStore(_configured_store(cfg, args.store))

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
        base_variant = registry.get(spec.base) or variant_mod.baseline(
            str(cfg["strategy"]["id"]), str(cfg["strategy"]["version"]))
        subject = variant_mod.Variant(
            variant_id=f"{base_variant.strategy_id}.condition.{spec.name}",
            strategy_id=base_variant.strategy_id,
            base_version=base_variant.base_version,
            overrides={}, hypothesis=spec.hypothesis, status="candidate")
        store.register(subject)
        pooled = score.score_returns(
            [d.outcome["r_multiple"] for d in result.executed()
             if d.outcome and d.outcome.get("r_multiple") is not None],
            label=subject.variant_id)
        store.record_evaluation(
            _run_id(subject.variant_id, result), subject.variant_id,
            result, pooled,
            json.dumps({
                "sweep": spec.name, "axis": axis.variable,
                "verdict": "CONDITIONING_RESULT",
                "buckets": corrected,
            }, sort_keys=True, default=str),
            kind="observation")
        print(f"\npersisted conditioning evidence to {store.path}")
        return 0

    print()
    settings = []
    evaluations = []
    for variant in sweep.expand(spec, registry):
        store.register(variant)
        variant_cfg = variant_mod.apply(variant, cfg)
        result = replay_mod.Replay(
            variant_cfg, variant_id=variant.variant_id,
            mode=args.replay_mode, price_cache=cache).run(cycles, outputs)
        executed = result.executed()
        settings.append((variant.variant_id, executed))
        returns = [d.outcome["r_multiple"] for d in executed if d.outcome]
        row = score.score_returns(returns, label=variant.variant_id)
        evaluations.append((variant, result, row))
        print(f"  {variant.variant_id:<40} n={row['n']:<5} "
              f"expectancy {row['expectancy_r']:+.4f}R  {row['verdict']}")

    # The axis is the unit of decision, not the individual setting: a
    # hypothesis is never rejected on one parameter value.
    base_variant = registry.get(spec.base) or variant_mod.baseline(
        str(cfg["strategy"]["id"]), str(cfg["strategy"]["version"]))
    baseline_result = replay_mod.Replay(
        variant_mod.apply(base_variant, cfg),
        variant_id=base_variant.variant_id, mode=args.replay_mode,
        price_cache=cache).run(cycles, outputs)
    verdict = protocol.evaluate_axis(
        settings, baseline_result.executed(),
        strategy_id=base_variant.strategy_id)
    print(f"\naxis verdict: {verdict.verdict}")
    print(f"  governing criterion: {verdict.governing_criterion}")
    print(f"  {verdict.detail}")
    if verdict.verdict == stats.INSUFFICIENT_SAMPLE:
        print("\nThe question is open, not answered in the negative. That "
              "distinction is\nthe one most likely to erode: see "
              "research/protocol.md.")
    for variant, result, row in evaluations:
        store.record_evaluation(
            _run_id(variant.variant_id, result), variant.variant_id,
            result, row,
            f"{spec.name}: {verdict.verdict} — "
            f"{verdict.governing_criterion}. {verdict.detail}")
    analysis_id = store.record_analysis("parameter_axis", spec.name, {
        "journal": str(db), "verdict": verdict.verdict,
        "governing_criterion": verdict.governing_criterion,
        "detail": verdict.detail, "evidence": verdict.evidence,
        "g2_fidelity_code_version": replay_mod.fidelity_code_fingerprint(),
        "strategy_id": base_variant.strategy_id,
    })
    if verdict.verdict == protocol.PROMOTE:
        print(f"{verdict.evidence['best']} is a replay candidate only; "
              "real-time paired forward evidence is required before paper")
    print(f"\npersisted {len(evaluations)} variant result(s) to {store.path}")
    return 0


def cmd_forward_qualify(args: argparse.Namespace) -> int:
    """Select an edge from paired real-time shadow outcomes, then paper it."""
    cfg = _load_config()
    store = findings_mod.FindingsStore(_configured_store(cfg, args.store))
    registry = _load_experiment_registry(cfg, store)
    for variant in registry.values():
        store.register(variant)
    scope = args.scope
    if not scope:
        scopes = store.paper_scopes()
        if len(scopes) != 1:
            print("--scope is required when findings.db contains zero or "
                  "multiple paper scopes", file=sys.stderr)
            return 2
        scope = scopes[0]
    # The analyst lane is deliberately not comparable to deterministic
    # forward evidence. Reject it even when supplied explicitly (for
    # example by the nightly FORWARD_SCOPE override), so an operator cannot
    # accidentally qualify the sibling ``:llm`` account.
    if str(scope).endswith(":llm"):
        print("--scope must identify a deterministic forward lane; the "
              "analyst :llm scope is not eligible", file=sys.stderr)
        return 2
    from agent.variants import baseline_variant_id, strategy_variant_prefix

    strategy_id = str(getattr(args, "strategy", None)
                      or cfg["strategy"]["id"])
    # A strategy id and a variant id are different alphabets: hyphens are
    # legal in the first and not in the second.
    variant_prefix = strategy_variant_prefix(strategy_id)
    baseline_id = baseline_variant_id(strategy_id)
    axes: dict[str, dict] = {}
    hypothesis_groups: dict[str, list] = {}
    for variant_id, variant in sorted(registry.items()):
        if (variant.strategy_id != strategy_id
                or variant.status not in {"candidate", "testing"}):
            continue
        if variant.hypothesis_id and variant_id.startswith(
                f"{variant_prefix}.hyp."):
            hypothesis_groups.setdefault(str(variant.hypothesis_id), []).append(
                variant)
            continue
        if variant_id == baseline_id:
            continue
        axis = tuple(sorted(str(path) for path in variant.overrides))
        if not axis:
            continue
        axis_id = "+".join(axis)
        axes.setdefault(axis_id, {
            "axis": list(axis),
            "baseline_id": baseline_id,
            "variants": [],
        })["variants"].append(variant)

    # Metadata-backed hypothesis contracts are real experiment axes even
    # though they do not map to executable config paths. The registered
    # setting is the baseline; every other materialized setting (including an
    # adaptive value) is an arm in the same hypothesis family.
    for hypothesis_id, variants_for_hypothesis in sorted(
            hypothesis_groups.items()):
        baseline = next((variant for variant in variants_for_hypothesis
                         if variant.variant_id.endswith(".registered")), None)
        settings = [variant for variant in variants_for_hypothesis
                    if variant is not baseline]
        if baseline is None or not settings:
            continue
        keys = sorted({str(key)
                       for variant in variants_for_hypothesis
                       for key in variant.hypothesis_params})
        if not keys:
            continue
        axis = [f"hypothesis.{hypothesis_id}.{key}" for key in keys]
        axes["+".join(axis)] = {
            "axis": axis,
            "baseline_id": baseline.variant_id,
            "variants": settings,
        }
    if not axes:
        print(f"no active parameter axes for {strategy_id}", file=sys.stderr)
        return 2
        
    # Every axis is evaluated and persisted before any of them may qualify.
    # Criterion 10 corrects across the axes tested in one run, and a family
    # size cannot be known while still walking the family.
    evaluated: dict[str, dict] = {}
    for axis_id, axis_record in sorted(axes.items()):
        axis = axis_record["axis"]
        setting_ids = [variant.variant_id
                       for variant in axis_record["variants"]]
        try:
            analysis_id, verdict = store.record_forward_analysis(
                scope, strategy_id, list(axis),
                axis_record["baseline_id"], setting_ids)
        except ValueError as exc:
            print(f"forward axis {axis_id}: evidence invalid: {exc}",
                  file=sys.stderr)
            continue
        analysis = store.analysis(analysis_id) or {}
        evaluated[axis_id] = {
            "axis": list(axis),
            "analysis_id": analysis_id,
            "verdict": verdict,
            "portfolios": (analysis.get("payload") or {}).get(
                "portfolio_statuses") or {},
        }
    if not evaluated:
        print("no axis produced valid forward evidence", file=sys.stderr)
        return 2

    corrected = protocol.apply_axis_family_correction(
        {axis_id: row["verdict"] for axis_id, row in evaluated.items()})
    family_analysis_id = store.record_analysis(
        "forward_axis_family", f"{strategy_id}:{scope}", {
            "strategy_id": strategy_id, "scope_key": scope,
            "axes": {axis_id: {
                "analysis_id": row["analysis_id"],
                "verdict_before_correction": row["verdict"].verdict,
                "verdict": corrected[axis_id].verdict,
                "governing_criterion": corrected[axis_id].governing_criterion,
                "family": corrected[axis_id].evidence.get("family"),
            } for axis_id, row in sorted(evaluated.items())},
        })
    print(f"family correction across {len(evaluated)} axis/axes "
          f"(analysis {family_analysis_id})\n")

    for axis_id, row in sorted(evaluated.items()):
        verdict = corrected[axis_id]
        family = verdict.evidence.get("family") or {}
        
        print(f"forward axis {axis_id}: {verdict.verdict}")
        print(f"  {verdict.governing_criterion}: {verdict.detail}")
        print(f"  family: {family.get('family_n')} axes, p_adj "
              f"{float(family.get('p_adjusted', 1.0)):.3f}")
        if verdict.verdict != protocol.PROMOTE:
            continue
        best = str(verdict.evidence["best"])
        if row["portfolios"].get(best) == "REVOKED":
            print(f"  {best} cannot enter paper: its shadow portfolio is "
                  "revoked and requires reconciliation", file=sys.stderr)
            continue
        event_id = store.qualify_variant(
            best, {
                "source": "paired_real_time_forward",
                "analysis_id": row["analysis_id"],
                "family_analysis_id": family_analysis_id,
                "axis": row["axis"],
                "verdict": verdict.evidence,
                "family": family,
                "automatic_action": "start isolated local paper when flat",
                "live_promotion": False,
            }, source_analysis_id=row["analysis_id"], scope_key=scope)
        
        print(f"qualified {best} for isolated paper in {scope} "
              f"(event {event_id}); live registry unchanged")
    return 0


def _forward_assignment_ids(analysis: dict | None, variant_id: str) -> set[str]:
    payload = (analysis or {}).get("payload") or {}
    source = payload.get("source_evidence") or {}
    setting = (source.get("settings") or {}).get(variant_id) or {}
    return {
        str(item.get("assignment_id") or "")
        for item in setting.get("assignments") or []
        if isinstance(item, dict) and item.get("assignment_id")}


def _build_t3_payload(
        cfg: dict, store: findings_mod.FindingsStore, variant_id: str,
        scope: str, db: Path, *, manual_registry_review: bool,
        created_from: str) -> dict:
    """Build one review payload entirely from persisted, recomputable state."""
    from agent import brain, state
    from agent import variants as variant_mod
    from agent.forward_models import require_complete_contract

    # The artifact is for the exact executable variant, not merely the base
    # config that the CLI loaded.  Rehydrate the immutable registry identity
    # from the store, apply its overrides through the same validator used by
    # the shadow runtime, and hash that validated result in the neutral
    # artifact module.
    variant_row = store.variant(variant_id)
    if variant_row is None:
        raise ValueError(f"unknown variant {variant_id!r}")
    try:
        registered_variant = variant_mod.Variant(
            variant_id=str(variant_row["variant_id"]),
            strategy_id=str(variant_row["strategy_id"]),
            base_version=str(variant_row["base_version"]),
            overrides=json.loads(variant_row.get("overrides_json") or "{}"),
            hypothesis=str(variant_row.get("hypothesis") or ""),
            status=str(variant_row.get("status") or "candidate"),
            hypothesis_id=variant_row.get("hypothesis_id"),
            hypothesis_params=json.loads(
                variant_row.get("hypothesis_params_json") or "{}"),
        )
        artifact_cfg = variant_mod.apply(
            registered_variant, cfg, allow_shadow_strategy=True)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"{variant_id}: cannot build artifact from registered variant") from exc
    variant_definition = {
        "variant_id": str(registered_variant.variant_id),
        "strategy_id": str(registered_variant.strategy_id),
        "base_version": str(registered_variant.base_version),
        "overrides": dict(registered_variant.overrides),
        "hypothesis": str(registered_variant.hypothesis),
        "hypothesis_id": str(registered_variant.hypothesis_id or ""),
        "hypothesis_params": dict(registered_variant.hypothesis_params or {}),
    }
    model = require_complete_contract(registered_variant.strategy_id)
    prompt_catalog = variant_mod.research_selection_catalog()
    prompt_text = brain.build_system(artifact_cfg, catalog=prompt_catalog)
    prompt_hash = artifact_mod.sha256_text(prompt_text)
    prompt_inputs_hash = artifact_mod.sha256_json({
        "catalog": prompt_catalog,
        "strategy": artifact_cfg.get("strategy") or {},
    })

    qualification = store.qualification_status(variant_id, scope)
    analysis = store.analysis(
        str((qualification or {}).get("source_analysis_id") or ""))
    paper = store.paper_summary(scope, variant_id, paper_only=True)
    g2 = _latest_g2_payload(db)
    analysis_payload = (analysis or {}).get("payload") or {}
    evidence = analysis_payload.get("evidence") or {}
    eligibility = (
        (analysis_payload.get("source_evidence") or {}).get("eligibility") or {})
    candidate_provenance = (
        (eligibility.get("setting_provenances") or {}).get(variant_id))
    manifest_config = (candidate_provenance.get("experiment_config")
                       if isinstance(candidate_provenance, dict)
                       else artifact_cfg)
    manifest_strategy_config_version = (
        candidate_provenance.get("strategy_config_version")
        if isinstance(candidate_provenance, dict)
        and candidate_provenance.get("strategy_config_version")
        else state.strategy_fingerprint(artifact_cfg))
    manifest_model_id = (
        candidate_provenance.get("forward_model_id")
        if isinstance(candidate_provenance, dict)
        and candidate_provenance.get("forward_model_id")
        else model.model_id)
    manifest_model_hash = (
        candidate_provenance.get("forward_model_assumptions_hash")
        if isinstance(candidate_provenance, dict)
        and candidate_provenance.get("forward_model_assumptions_hash")
        else findings_mod._content_hash(model.as_dict()))
    manifest, artifact_hash = artifact_mod.build_manifest(
        strategy_id=registered_variant.strategy_id,
        strategy_version=registered_variant.base_version,
        variant_id=registered_variant.variant_id,
        variant_definition_hash=findings_mod.variant_identity_hash(
            registered_variant),
        variant_definition=variant_definition,
        config=manifest_config,
        strategy_config_version=manifest_strategy_config_version,
        forward_model_id=manifest_model_id,
        forward_model_assumptions_hash=manifest_model_hash,
        prompt_hash=prompt_hash,
        llm_provider=str((artifact_cfg.get("llm") or {}).get("provider") or ""),
        llm_model=str((artifact_cfg.get("llm") or {}).get("model") or ""),
        prompt_inputs_hash=prompt_inputs_hash,
        deployment_config_hash=state.deployment_config_hash(manifest_config),
        diagnostic_model_id=str((artifact_cfg.get("llm") or {}).get("model") or "")
        or None,
    )
    experiment_provenances = [
        eligibility.get("baseline_provenance"),
        *list((eligibility.get("setting_provenances") or {}).values()),
    ]
    min_paper = int((cfg.get("research") or {}).get(
        "paper_min_closed_trades") or 100)
    g2_current = bool(
        db.exists()
        and g2 and g2.get("status") == "PASS"
        and replay_mod.validate_persisted_g2(g2, db, cfg)[0])
    assignment_ids = _forward_assignment_ids(analysis, variant_id)
    edge_candidates = [
        item for item in store.edge_candidates_for(
            variant_id=variant_id, scope_key=scope)
        if str(item["assignment_id"]) in assignment_ids]
    checklist = {
        "current_g2_pass": g2_current,
        "held_out_confirmation": bool(
            analysis_payload.get("verdict") == protocol.PROMOTE
            and evidence.get("selection_window") == "fit"
            and evidence.get("confirm_delta")),
        "corpus_provenance": all(
            analysis_payload.get(key) is not None for key in
            ("corpus_from_ts", "corpus_to_ts", "resolved_outcomes")),
        "config_provenance": bool(
            experiment_provenances
            and all(isinstance(item, dict)
                    and item.get("strategy_config_version")
                    for item in experiment_provenances)),
        "code_provenance": bool(analysis_payload.get("code_version")),
        "forward_sample": bool(
            qualification and qualification.get("status") == "QUALIFIED"
            and analysis_payload.get("source")
            == "real_time_shadow_portfolios"),
        "axis_family_corrected": bool(
            ((qualification or {}).get("detail") or {})
            .get("family", {}).get("significant")),
        "saved_edge_candidate": bool(edge_candidates),
        "paper_sample": int(paper.get("closed_trades") or 0) >= min_paper,
        "paper_positive": bool(
            paper.get("status") == "PAPER"
            and paper.get("expectancy_r") is not None
            and float(paper["expectancy_r"]) > 0
            and not paper.get("revoked_reason")),
        findings_mod.T3_MANUAL_CHECK: bool(manual_registry_review),
    }
    return {
        "variant_id": variant_id,
        "scope_key": scope,
        "created_from": created_from,
        "checklist": checklist,
        "g2": g2,
        "edge_candidates": edge_candidates,
        "qualification": qualification,
        "forward_analysis": analysis,
        "paper_result": paper,
        "current_provenance": {
            # Preserve the established G2/current-replay provenance field:
            # G2 replays the baseline config.  The artifact carries the exact
            # candidate config and has its own explicit binding below.
            "strategy_config_version": state.strategy_fingerprint(cfg),
            "artifact_strategy_config_version": manifest[
                "strategy_config_version"],
            "code_version": state.code_fingerprint(),
            "fidelity_code_version": replay_mod.fidelity_code_fingerprint(),
            "validated_config_hash": manifest["validated_config_hash"],
            "runtime_source_hash": manifest["runtime_source_hash"],
            "variant_definition_hash": manifest["variant_definition_hash"],
            "forward_model_id": manifest["forward_model_id"],
            "forward_model_assumptions_hash": (
                manifest["forward_model_assumptions_hash"]),
            "deployment_config_hash": manifest["deployment_config_hash"],
            "llm_endpoint": manifest["llm_endpoint"],
            "llm_endpoint_hash": manifest["llm_endpoint_hash"],
        },
        # The manifest and its full digest are additive.  Legacy payloads do
        # not gain an inferred identity and consequently remain unbound.
        "artifact_manifest": manifest,
        "artifact_hash": artifact_hash,
    }


def cmd_t3_packet(args: argparse.Namespace) -> int:
    """Create a content-addressed, review-gated T3 evidence packet."""
    if bool(args.reviewed_by) != bool(args.registry_change_ref):
        print("--reviewed-by and --registry-change-ref must be supplied "
              "together", file=sys.stderr)
        return 2
    cfg = _load_config()
    store = findings_mod.FindingsStore(_configured_store(cfg, args.store))
    registry = _load_experiment_registry(cfg, store)
    if args.variant not in registry:
        print(f"unknown variant {args.variant}", file=sys.stderr)
        return 2
    store.register(registry[args.variant])
    scopes = store.paper_scopes()
    scope = args.scope or (scopes[0] if len(scopes) == 1 else None)
    if not scope:
        print("--scope is required when findings.db contains zero or multiple "
              "paper scopes", file=sys.stderr)
        return 2
    db = Path(args.db) if args.db else default_db(args.mode)
    payload = _build_t3_payload(
        cfg, store, args.variant, scope, db,
        manual_registry_review=bool(
            args.reviewed_by and args.registry_change_ref),
        created_from="research.py t3-packet")
    packet_id = store.create_t3_packet(
        args.variant, payload, reviewed_by=args.reviewed_by or "",
        registry_change_ref=args.registry_change_ref or "")
    packet = next(item for item in store.t3_packets_for(args.variant)
                  if item["packet_id"] == packet_id)
    print(f"T3 packet {packet_id}: {packet['review_status']}")
    print(f"  SHA-256 {packet['payload_hash']}")
    print(f"  registry evidence reference: t3-packet:{packet['payload_hash']}")
    for name, passed in payload["checklist"].items():
        print(f"  {'PASS' if passed else 'BLOCK'} {name}")
    print("No registry tier or live configuration was changed.")
    return 0


def cmd_prepare_review_artifacts(args: argparse.Namespace) -> int:
    """Idempotently persist a draft T3 artifact for each ready evidence state."""
    cfg = _load_config()
    store = findings_mod.FindingsStore(_configured_store(cfg, args.store))
    db = Path(args.db) if args.db else default_db(args.mode)
    scopes = [args.scope] if args.scope else sorted(store.paper_scopes())
    non_manual = findings_mod.T3_REQUIRED_CHECKS - {
        findings_mod.T3_MANUAL_CHECK}
    created = existing = collecting = 0
    for scope in scopes:
        for variant_id in store.qualified_variant_ids(scope):
            payload = _build_t3_payload(
                cfg, store, variant_id, scope, db,
                manual_registry_review=False,
                created_from="research.py prepare-review-artifacts")
            checklist = payload["checklist"]
            blocked = sorted(
                name for name in non_manual if not bool(checklist.get(name)))
            if blocked:
                collecting += 1
                print(f"review artifact collecting for {variant_id} in "
                      f"{scope}: {', '.join(blocked)}")
                continue
            if not store.t3_evidence_is_backed(variant_id, payload):
                collecting += 1
                print(f"review artifact collecting for {variant_id} in "
                      f"{scope}: persisted evidence validation failed")
                continue
            prior_ids = {
                packet["packet_id"]
                for packet in store.t3_packets_for(variant_id)}
            packet_id = store.create_t3_packet(variant_id, payload)
            packet = next(
                item for item in store.t3_packets_for(variant_id)
                if item["packet_id"] == packet_id)
            if packet_id in prior_ids:
                existing += 1
                print(f"review artifact already exists for {variant_id} in "
                      f"{scope}: {packet_id}")
            else:
                created += 1
                print(f"draft T3 review artifact {packet_id} for {variant_id} "
                      f"in {scope}")
            print(f"  SHA-256 {packet['payload_hash']}")
    print(json.dumps({
        "created": created,
        "existing": existing,
        "collecting": collecting,
        "live_promotion": False,
    }, sort_keys=True))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Regenerate every scorecard from the store, deterministically."""
    cfg = _load_config()
    store = findings_mod.FindingsStore(_configured_store(cfg, args.store))
    # Registered-but-unrun variants still get a card: "no sample yet" is a
    # state worth being able to see. Persisted adaptive variants are included
    # so the report cannot hide the exact values the live path tried.
    for variant in _load_experiment_registry(cfg, store).values():
        store.register(variant)

    written = findings_mod.write_scorecards(
        store, args.out or (REPO / "findings"))
    backup = store.backup()
    print(f"regenerated {len(written)} files under "
          f"{args.out or (REPO / 'findings')}")
    print(f"backed up findings store to {backup}")
    for path in written:
        print(f"  {path}")
    return 0


def _configured_store(cfg: dict, requested: str | Path | None) -> Path:
    return _configured_path(
        cfg, "findings_store", requested, findings_mod.DEFAULT_STORE)


def _latest_verified_external_backup_readonly(
        store_path: str | Path) -> dict | None:
    """Inspect backup evidence without creating or migrating a findings DB."""
    path = Path(store_path)
    if not path.is_file():
        return None
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        tables = {
            str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not {"backup_runs", "backup_verifications"}.issubset(tables):
            return None
        rows = conn.execute(
            "SELECT run.*, verification.backup_path, "
            "verification.verified_ts, verification.detail_json "
            "FROM backup_runs AS run JOIN backup_verifications AS "
            "verification ON verification.verification_id=("
            "SELECT latest.verification_id FROM backup_verifications AS "
            "latest WHERE latest.backup_id=run.backup_id "
            "ORDER BY latest.verified_ts DESC, latest.rowid DESC LIMIT 1) "
            "WHERE run.target_kind='external_mounted' "
            "AND verification.status='VERIFIED' "
            "ORDER BY verification.verified_ts DESC").fetchall()
    for row in rows:
        item = dict(row)
        try:
            target_evidence = json.loads(
                item.get("target_evidence_json") or "{}")
            detail = json.loads(item.get("detail_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if findings_mod._positive_external_target_evidence(target_evidence):
            item["target_evidence"] = target_evidence
            item["detail"] = detail
            return item
    return None


def _configured_backup_target(
        cfg: dict, requested: str | Path | None) -> tuple[Path | None, bool]:
    configured = requested or (cfg.get("research") or {}).get("backup_target")
    if not configured:
        return None, False
    path = Path(configured)
    return (path if path.is_absolute() else REPO / path), True


def cmd_backup(args: argparse.Namespace) -> int:
    """Create one versioned, immediately verified research backup."""
    cfg = _load_config()
    store_path = _configured_store(cfg, args.store)
    target, target_configured = _configured_backup_target(cfg, args.target)
    journal = Path(args.journal) if args.journal else default_db(args.mode)
    options = {
        key: str(value) if isinstance(value, Path) else value
        for key, value in vars(args).items() if not key.startswith("_")
    }
    try:
        result = backup_mod.create_backup(
            store_path=store_path,
            target=target,
            target_configured=target_configured,
            journal_path=journal,
            runtime_research_root=Path(args.runtime_research),
            results_root=Path(args.results),
            require_external=args.require_external,
            command=getattr(args, "_command", []),
            options=options)
    except backup_mod.BackupError as exc:
        print(f"backup FAILED: {exc}", file=sys.stderr)
        for error in exc.result.get("errors") or []:
            print(f"  {error}", file=sys.stderr)
        return 1
    print(f"backup VERIFIED: {result['backup_path']}")
    print(f"  files: {result['files_verified']}")
    print(f"  content: {result['content_id']}")
    print(f"  target: {result['target_kind']}")
    if result["target_kind"] == "external_mounted":
        print("  EXTERNAL MOUNT VERIFIED: target st_dev differs from all "
              "repository/source devices.")
    elif result["target_kind"] == "configured_local":
        print("  CONFIGURED LOCAL: this explicit target shares a source "
              "filesystem/device and does not make VM deletion safe.")
    else:
        print("  LOCAL DEFAULT: this backup does not make VM deletion safe.")
    return 0


def cmd_verify_backup(args: argparse.Namespace) -> int:
    """Re-verify a backup and append the result to FindingsStore."""
    cfg = _load_config()
    store_path = _configured_store(cfg, args.store)
    result = backup_mod.verify_and_record(args.path, store_path=store_path)
    label = "VERIFIED" if result["ok"] else "FAILED"
    print(f"backup verification {label}: {args.path}")
    print(f"  files: {result['files_verified']}")
    for error in result["errors"]:
        print(f"  {error}", file=sys.stderr)
    if result.get("target_kind") != "external_mounted":
        print("  LOCAL ONLY: no verified different-device mounted copy is "
              "proven; off-host retention is a separate operator check.")
    else:
        print("  EXTERNAL MOUNT VERIFIED: manifest records different-device "
              "classification evidence.")
    return 0 if result["ok"] else 1


def cmd_cadence(args: argparse.Namespace) -> int:
    """B9.2 evidence: how much LLM spend buys a re-evaluation of nothing?

    The plan requires this published before the cadence split merges, and
    reframed as signal-decay evidence: if expectancy also decays with latency from the bar
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


def cmd_readiness(args: argparse.Namespace) -> int:
    """Answer "is it ready yet?" from the journal rather than from memory.

    Two items in this programme wait on data rather than on engineering, and
    both fail the same quiet way: someone decides they have waited long
    enough. This makes the answer mechanical.
    """
    db = Path(args.db) if args.db else default_db(args.mode)
    cfg = _load_config()
    gates, stats = readiness_mod.report(db if db.exists() else None, cfg)
    print(readiness_mod.format_report(
        gates, stats, db if db.exists() else f"{db} (absent)"))
    store_path = _configured_store(cfg, getattr(args, "store", None))
    external = _latest_verified_external_backup_readonly(store_path)
    if external:
        print("\nexternal backup  PASS       latest different-device mounted "
              f"backup verified at {external['verified_ts']:.0f}")
    else:
        print("\nexternal backup  BLOCKED    no verified different-device "
              "mounted backup is recorded")
        print("                         default and configured-local backups "
              "do not make this VM safe to delete")

    if external is None or any(
            g.status == readiness_mod.FAILED for g in gates):
        return 2
    return 0


def cmd_research_loop(args: argparse.Namespace) -> int:
    """Backfill outcomes and review a bounded queue of pending results."""
    cfg = _load_config()
    store = findings_mod.FindingsStore(_configured_store(cfg, args.store))
    if args.no_review:
        result = {
            "status": "OUTCOMES_ONLY",
            "outcomes": store.ensure_terminal_experiment_outcomes(),
        }
    else:
        result = review_mod.process_pending_reviews(
            store, cfg,
            max_reviews=getattr(
                args, "max_reviews", DEFAULT_RESEARCH_REVIEW_CAP))
    print(json.dumps(result, sort_keys=True, default=str))
    # Provider and parse failures are persisted and deliberately nonfatal so
    # a nightly invocation can retry without losing the deterministic result.
    return 0


def cmd_author(args: argparse.Namespace) -> int:
    """Ask for new candidate mechanisms and stage the ones that validate.

    Deliberately not gated on a terminal outcome. The reviewer needs one to
    have something to explain; generation is what a loop with nothing left to
    nominate needs most, and that is exactly the state with no terminal
    outcome in it.
    """
    from agent.staging import StagingStore
    from research import authoring

    cfg = _load_config()
    store = StagingStore(_configured_staging(cfg, args.staging))
    history = {}
    if not args.no_history:
        findings_store = findings_mod.FindingsStore(
            _configured_store(cfg, args.store))
        try:
            context = findings_store.research_history_context()
        except Exception as exc:  # noqa: BLE001 - history is an input, not a gate
            print(f"history unavailable, proposing without it: {exc}",
                  file=sys.stderr)
            context = {}
        completed = list((context or {}).get("completed_outcomes", []))
        failed = list((context or {}).get("failed_exact_settings", []))
        retryable = list((context or {}).get(
            "retryable_inconclusive_assignments", []))
        # ``completed_outcomes`` is the durable compatibility surface; the
        # narrower keys are preferred when present, with verdict filtering as
        # a safe fallback for stores written before those projections existed.
        if not failed:
            failed = [item for item in completed
                      if str(item.get("verdict") or "").upper() == "FAILED"]
        if not retryable:
            retryable = [item for item in completed
                         if str(item.get("verdict") or "").upper()
                         == "INCONCLUSIVE"]
        history = {
            "falsified": failed,
            "inconclusive": retryable,
            "completed_outcomes": completed,
        }
        # What previous generations of authored mechanisms learned. Without
        # this the proposer sees which of its own claims are staged but not
        # what happened to them, and restates a dead one at a slightly
        # different threshold - the same claim wearing a different number.
        try:
            from research import staged_review

            staged_history = staged_review.authoring_history(
                store, findings_store)
            history["falsified"].extend(staged_history["falsified"])
            history["inconclusive"].extend(staged_history["inconclusive"])
        except Exception as exc:  # noqa: BLE001
            print(f"staged history unavailable: {exc}", file=sys.stderr)
    if args.dry_run:
        request = authoring.build_request(
            store, history=history, max_proposals=args.max_proposals)
        print(json.dumps(request, indent=2, sort_keys=True, default=str))
        return 0
    result = authoring.author_generation(
        store, cfg, history=history, max_proposals=args.max_proposals)
    print(json.dumps(result, sort_keys=True, default=str))
    # A failed provider call is recorded and retried on the next cadence
    # rather than failing the nightly run around it.
    return 0


def cmd_stage_seed(args: argparse.Namespace) -> int:
    """Register the version-controlled pre-registrations into staging.

    Idempotent: a claim already in the store is reported and skipped, so this
    can run on every deploy without a human deciding whether the second run's
    failure mattered.
    """
    from agent.staging import StagingStore
    from research import staging_seed

    cfg = _load_config()
    store = StagingStore(_configured_staging(cfg, args.staging))
    try:
        entries = staging_seed.load(args.file)
    except staging_seed.SeedError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.dry_run:
        print(json.dumps(
            [{"contract_id": entry.get("contract_id"),
              "status": entry.get("status")} for entry in entries],
            indent=2, sort_keys=True))
        return 0
    result = staging_seed.seed(store, entries, generation=args.generation)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    # A rejected pre-registration is a broken claim in version control, not a
    # transient failure, so unlike the authoring path this one fails loudly.
    return 1 if result["rejected"] else 0


def cmd_staged(args: argparse.Namespace) -> int:
    """List staged mechanisms and what each one claims."""
    from agent.staging import StagingStore

    cfg = _load_config()
    store = StagingStore(_configured_staging(cfg, args.staging))
    contracts = store.active()
    if not contracts:
        print("no staged mechanisms")
        return 0
    for contract in contracts:
        print(f"{contract.contract_id}  [{contract.direction}]")
        print(f"  mechanism: {contract.mechanism}")
        print(f"  payer:     {contract.payer}")
        print(f"  falsifier: {contract.falsifier}")
        print(f"  fires when {contract.describe()}")
        print()
    summary = store.summary()
    print(f"{summary['active']} active, {summary['retired']} retired, "
          f"tiers {', '.join(summary['tiers'])}")
    return 0


def cmd_shortlist(args: argparse.Namespace) -> int:
    """Rank what the evidence supports, and say how far it goes.

    Reports every measured arm including the ones with nothing to show, so a
    mechanism that was starved of data is visible as untested rather than
    silently absent from the ranking.
    """
    from research import shortlist as shortlist_mod

    cfg = _load_config()
    store = findings_mod.FindingsStore(_configured_store(cfg, args.store))
    staging = None
    staging_path = _configured_staging(cfg, args.staging)
    if Path(staging_path).is_file():
        from agent.staging import StagingStore

        staging = StagingStore(staging_path)
    candidates = shortlist_mod.from_store(
        store, staging, scope_key=args.scope)
    if args.json:
        import dataclasses

        print(json.dumps(
            [dataclasses.asdict(item)
             for item in shortlist_mod.rank(candidates)],
            indent=2, sort_keys=True, default=str))
        return 0
    report = shortlist_mod.render(candidates)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(report, end="")
    return 0


def cmd_review_staged(args: argparse.Namespace) -> int:
    """Adjudicate staged mechanisms and retire the ones that are finished.

    Retirement frees a lane; the claim and the reason stay recorded so the
    next generation can see what has already been tried.
    """
    from agent.staging import StagingStore
    from research import staged_review

    cfg = _load_config()
    staging_path = _configured_staging(cfg, args.staging)
    if not Path(staging_path).is_file():
        print("no staged mechanisms")
        return 0
    store = findings_mod.FindingsStore(_configured_store(cfg, args.store))
    result = staged_review.review(
        StagingStore(staging_path), store, scope_key=args.scope,
        retire=not args.dry_run)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


def cmd_qualify_staged(args: argparse.Namespace) -> int:
    """Start isolated local PAPER for supported staged mechanisms only."""
    from agent.staging import StagingStore
    from research import staged_review

    cfg = _load_config()
    staging_path = _configured_staging(cfg, args.staging)
    if not staging_path.is_file():
        print(json.dumps({"status": "NO_STAGING_STORE"}, sort_keys=True))
        return 0

    store = findings_mod.FindingsStore(_configured_store(cfg, args.store))
    staging = StagingStore(staging_path)
    requested_scope = str(args.scope or "").strip() or None
    if requested_scope is not None and not requested_scope.endswith(":staged"):
        print("--scope must identify a staged shadow lane", file=sys.stderr)
        return 2

    if requested_scope is not None:
        scopes = [requested_scope]
    else:
        scopes = sorted(
            scope for scope in store.paper_scopes()
            if str(scope).endswith(":staged"))

    qualified, skipped, errors = [], [], []
    for scope in scopes:
        review = staged_review.review(
            staging, store, scope_key=scope, retire=False)
        for entry in review.get("verdicts", []):
            if entry.get("code") != staged_review.SUPPORTED:
                skipped.append({
                    "scope_key": scope,
                    "variant_id": entry.get("variant_id"),
                    "code": entry.get("code"),
                })
                continue
            variant_id = str(entry.get("variant_id") or "")
            if not variant_id:
                errors.append({"scope_key": scope, "error": "missing variant_id"})
                continue
            try:
                event_id = store.qualify_staged_variant(
                    variant_id,
                    detail={"review": entry},
                    scope_key=scope,
                )
            except Exception as exc:                       # noqa: BLE001
                errors.append({
                    "scope_key": scope,
                    "variant_id": variant_id,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                continue
            qualified.append({
                "scope_key": scope,
                "variant_id": variant_id,
                "event_id": event_id,
                "paper_mode": "isolated_local",
                "live_promotion": False,
            })

    result = {
        "status": "QUALIFIED" if qualified else "NO_SUPPORTED_STAGED_EDGES",
        "scopes": scopes,
        "qualified": qualified,
        "skipped": skipped,
        "errors": errors,
    }
    print(json.dumps(result, sort_keys=True, default=str))
    return 2 if errors else 0


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
        "--variant", default=None,
        help="registered variant id; defaults to config.strategy.id baseline")
    replay_parser.add_argument(
        "--replay-mode", default="recorded_llm", choices=replay_mod.MODES,
        help="proposer mode")
    replay_parser.add_argument(
        "--prices", default=None,
        help="path to a 1m price cache; without it no outcome is resolved")
    replay_parser.add_argument(
        "--check-fidelity", action="store_true",
        help="gate G2: assert the baseline reproduces recorded decisions")
    replay_parser.add_argument(
        "--no-persist", action="store_true",
        help="run a fidelity check without appending its G2 audit event")
    replay_parser.set_defaults(func=cmd_replay)

    three = sub.add_parser(
        "three-arm",
        help="H-E: does the LLM select, veto, or neither")
    three.add_argument("--db", default=None)
    three.add_argument("--mode", default="demo", choices=["demo", "live"])
    three.add_argument(
        "--prices", default=None,
        help="path to a 1m price cache; required to score any arm")
    three.add_argument("--store", default=None,
                       help="path to findings.db")
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
    sweep_parser.add_argument(
        "--store", default=None,
        help="findings.db path; defaults to research/cache/findings.db")
    sweep_parser.add_argument("--replay-mode", default="recorded_llm",
                              choices=replay_mod.MODES)
    sweep_parser.add_argument(
        "--force", action="store_true",
        help="run an underpowered grid anyway, to record a null result")
    sweep_parser.set_defaults(func=cmd_sweep)

    forward = sub.add_parser(
        "forward-qualify",
        help="select an edge from paired real-time shadow outcomes")
    forward.add_argument(
        "--strategy", default=None,
        help="strategy id; defaults to validated config.strategy.id")
    forward.add_argument("--scope", default=None,
                         help="runtime/account scope; auto-detected if unique")
    forward.add_argument("--store", default=None,
                         help="findings.db path")
    forward.set_defaults(func=cmd_forward_qualify)

    prepare = sub.add_parser(
        "prepare-review-artifacts",
        help="create draft T3 artifacts for fully ready paper evidence")
    prepare.add_argument("--scope", default=None,
                         help="runtime/account scope; all scopes by default")
    prepare.add_argument("--store", default=None,
                         help="findings.db path")
    prepare.add_argument("--db", default=None,
                         help="journal.db path used for current G2 evidence")
    prepare.add_argument("--mode", default="demo", choices=["demo", "live"])
    prepare.set_defaults(func=cmd_prepare_review_artifacts)

    t3 = sub.add_parser(
        "t3-packet",
        help="build a content-addressed reviewed T3 evidence packet")
    t3.add_argument("--variant", required=True)
    t3.add_argument("--scope", default=None,
                    help="runtime/account scope; auto-detected if unique")
    t3.add_argument("--store", default=None)
    t3.add_argument("--db", default=None)
    t3.add_argument("--mode", default="demo", choices=["demo", "live"])
    t3.add_argument("--reviewed-by", default=None)
    t3.add_argument("--registry-change-ref", default=None)
    t3.set_defaults(func=cmd_t3_packet)

    report_parser = sub.add_parser(
        "report", help="regenerate the committed scorecards")
    report_parser.add_argument("--store", default=None)
    report_parser.add_argument("--out", default=None)
    report_parser.set_defaults(func=cmd_report)

    backup_parser = sub.add_parser(
        "backup", help="create a versioned, verified research backup")
    backup_parser.add_argument("--store", default=None,
                               help="findings.db path")
    backup_parser.add_argument("--journal", default=None,
                               help="journal.db path")
    backup_parser.add_argument("--mode", default="demo",
                               choices=["demo", "live"])
    backup_parser.add_argument(
        "--target", default=None,
        help="pre-existing configured destination; a different st_dev is "
             "required for external classification")
    backup_parser.add_argument(
        "--require-external", action="store_true",
        help="fail unless the target is proven on a different mounted device")
    backup_parser.add_argument(
        "--runtime-research", default=str(backup_mod.DEFAULT_RUNTIME_RESEARCH),
        help="recorder/manifests root")
    backup_parser.add_argument(
        "--results", default=str(backup_mod.DEFAULT_RESULTS),
        help="durable research result root")
    backup_parser.set_defaults(func=cmd_backup)

    verify_backup_parser = sub.add_parser(
        "verify-backup", help="verify a backup and append the result")
    verify_backup_parser.add_argument("path", help="backup directory")
    verify_backup_parser.add_argument("--store", default=None,
                                      help="findings.db path")
    verify_backup_parser.set_defaults(func=cmd_verify_backup)

    cadence = sub.add_parser(
        "cadence",
        help="B9.2 evidence: share of cycles that re-observe evaluated bars")
    cadence.add_argument("--db", default=None)
    cadence.add_argument("--mode", default="demo", choices=["demo", "live"])
    cadence.set_defaults(func=cmd_cadence)

    ready = sub.add_parser(
        "readiness",
        help="which gates are open, blocked, or still collecting")
    ready.add_argument("--db", default=None)
    ready.add_argument(
        "--store", default=None,
        help="findings.db to inspect read-only; absent stores are not created")
    ready.add_argument("--mode", default="demo", choices=["demo", "live"])
    ready.set_defaults(func=cmd_readiness)

    review_staged = sub.add_parser(
        "review-staged",
        help="verdict every staged mechanism and retire the finished ones")
    review_staged.add_argument("--store", default=None, help="findings.db path")
    review_staged.add_argument("--staging", default=None, help="staging.db path")
    review_staged.add_argument("--scope", default=None, help="limit to one scope")
    review_staged.add_argument("--dry-run", action="store_true",
                               help="report verdicts without retiring")
    review_staged.set_defaults(func=cmd_review_staged)

    qualify_staged = sub.add_parser(
        "qualify-staged",
        help="start isolated local PAPER for supported staged mechanisms")
    qualify_staged.add_argument("--store", default=None,
                                help="findings.db path")
    qualify_staged.add_argument("--staging", default=None,
                                help="staging.db path")
    qualify_staged.add_argument(
        "--scope", default=None,
        help="one deterministic staged scope; otherwise process all staged scopes")
    qualify_staged.set_defaults(func=cmd_qualify_staged)

    shortlist_parser = sub.add_parser(
        "shortlist", help="rank measured candidates and state the evidence")
    shortlist_parser.add_argument("--store", default=None,
                                  help="findings.db path")
    shortlist_parser.add_argument("--staging", default=None,
                                  help="staging.db path")
    shortlist_parser.add_argument("--scope", default=None,
                                  help="limit to one scope key")
    shortlist_parser.add_argument("--out", default=None,
                                  help="write the report to this path")
    shortlist_parser.add_argument("--json", action="store_true",
                                  help="emit structured rows instead of prose")
    shortlist_parser.set_defaults(func=cmd_shortlist)

    author = sub.add_parser(
        "author",
        help="propose new candidate mechanisms and stage the valid ones")
    author.add_argument("--staging", default=None, help="staging.db path")
    author.add_argument("--store", default=None, help="findings.db path")
    author.add_argument("--max-proposals", type=int, default=4)
    author.add_argument("--no-history", action="store_true",
                        help="propose without prior falsification context")
    author.add_argument("--dry-run", action="store_true",
                        help="print the request without calling a provider")
    author.set_defaults(func=cmd_author)

    stage_seed = sub.add_parser(
        "stage-seed",
        help="register the version-controlled pre-registrations into staging")
    stage_seed.add_argument("--staging", default=None, help="staging.db path")
    stage_seed.add_argument("--file", default=None,
                            help="seed YAML; defaults to "
                                 "research/staged/pre-registered.yaml")
    stage_seed.add_argument("--generation", type=int, default=0,
                            help="generation label for these claims; 0 marks "
                                 "them as founding rather than loop-produced")
    stage_seed.add_argument("--dry-run", action="store_true",
                            help="list what the file contains without writing")
    stage_seed.set_defaults(func=cmd_stage_seed)

    staged = sub.add_parser("staged", help="list staged candidate mechanisms")
    staged.add_argument("--staging", default=None, help="staging.db path")
    staged.set_defaults(func=cmd_staged)

    learning = sub.add_parser(
        "research-loop",
        help="backfill terminal outcomes and review a bounded pending queue")
    learning.add_argument("--store", default=None,
                          help="findings.db path")
    learning.add_argument(
        "--max-reviews", type=_positive_int,
        default=DEFAULT_RESEARCH_REVIEW_CAP,
        help=("maximum terminal experiment reviews per invocation "
              f"(default: {DEFAULT_RESEARCH_REVIEW_CAP})"))
    learning.add_argument(
        "--no-review", action="store_true",
        help="only create missing deterministic outcomes; do not call an LLM")
    learning.set_defaults(func=cmd_research_loop)

    return parser


def main(argv: list[str] | None = None) -> int:
    cli_args = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(cli_args)
    args._command = [sys.executable, str(Path(__file__).resolve()), *cli_args]
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
