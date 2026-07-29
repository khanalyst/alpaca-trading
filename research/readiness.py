"""Which gates are open, which are blocked, and what would unblock them.

Two things in this programme are waiting on data rather than on engineering,
and both fail in the same quiet way: someone eventually decides they have
waited long enough. Gate G2 in particular reproduces 100% of nothing on an
empty corpus, and B7.5's passive-entry path is a live experiment whose
validation cannot be simulated.

So the question "is it ready yet?" gets a mechanical answer rather than a
judgement. Every check here reads the journal and reports one of:

    PASS        the gate is satisfied, on evidence
    READY       enough data exists; the command has simply not been run
    COLLECTING  not enough data yet, with how much is missing
    BLOCKED     something upstream must happen first
    FAILED      it ran and did not pass. This is a stop, not a delay.

Nothing here estimates or extrapolates. "Collecting" means a count was taken
and fell short, and the shortfall is quoted.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from agent import state

from . import corpus, replay


PASS = "PASS"
READY = "READY"
COLLECTING = "COLLECTING"
BLOCKED = "BLOCKED"
FAILED = "FAILED"

# The 99% threshold is a ratio, so it is meaningless on a handful of events:
# at n=10 a single mismatch reads as 90% and fails a replay that is fine.
MIN_PROPOSALS_FOR_G2 = 100

# H-G and H-H condition on cells, and the plan asks for 150 observations per
# cell before concluding rather than extending collection.
MIN_OBSERVATIONS_PER_CELL = 150

# Enough passive attempts to see a cancel race if one is going to happen.
MIN_MAKER_ATTEMPTS = 20


@dataclass
class Gate:
    name: str
    title: str
    status: str
    detail: str
    next_step: str = ""
    blocks: list = field(default_factory=list)

    @property
    def symbol(self) -> str:
        return {PASS: "PASS", READY: "READY", COLLECTING: "....",
                BLOCKED: "BLKD", FAILED: "FAIL"}[self.status]


def _count(db, kind: str) -> int:
    try:
        with sqlite3.connect(f"file:{Path(db)}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM events WHERE kind=?", (kind,)).fetchone()
        return int(row[0]) if row else 0
    except Exception:                                      # noqa: BLE001
        return 0


def _has_kind(db, kind: str) -> bool:
    return _count(db, kind) > 0


def _proposal_snapshot(db) -> tuple[int, float]:
    try:
        with sqlite3.connect(f"file:{Path(db)}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(ts), 0) FROM events "
                "WHERE kind='setup_proposed'").fetchone()
        return int(row[0]), float(row[1])
    except Exception:                                      # noqa: BLE001
        return 0, 0.0


def _latest_g2_result(db) -> dict | None:
    try:
        with sqlite3.connect(f"file:{Path(db)}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT payload FROM events "
                "WHERE kind='research_gate_result' "
                "ORDER BY ts DESC, rowid DESC LIMIT 1").fetchone()
        payload = json.loads(row[0]) if row else None
        if isinstance(payload, dict) and payload.get("gate") == "G2":
            return payload
    except Exception:                                      # noqa: BLE001
        pass
    return None


def gate_g1() -> Gate:
    """Enrichment records without changing a decision.

    Held by a test rather than by data, so it is answered by the suite and is
    reported here only so the list is complete.
    """
    return Gate(
        "G1", "B0.5 enrichment changes no decision", PASS,
        "held by tests/research/test_enrichment_isolation.py: decisions "
        "byte-identical, prompt_version pinned by hand",
        "python -m pytest tests/research/test_enrichment_isolation.py")


def gate_g2(db, stats: dict, cfg: dict) -> Gate:
    """The keystone. Nothing downstream means anything until this passes."""
    proposals, max_proposal_ts = _proposal_snapshot(db)
    result = _latest_g2_result(db)
    result_is_current = bool(
        result
        and int(result.get("proposal_count", -1)) == proposals
        and float(result.get("max_proposal_ts", -1)) == max_proposal_ts
        and result.get("strategy_config_version")
        == state.strategy_fingerprint(cfg)
        and result.get("fidelity_code_version")
        == replay.fidelity_code_fingerprint())
    if result_is_current and result.get("status") == PASS:
        return Gate(
            "G2", "Replay reproduces the agent's own decisions", PASS,
            f"{result.get('matched', 0)}/{result.get('recorded', 0)} "
            "recorded proposals reproduced; result persisted in the journal",
            "re-run after code/config changes or after collecting new proposals")
    if result_is_current and result.get("status") == FAILED:
        return Gate(
            "G2", "Replay reproduces the agent's own decisions", FAILED,
            f"latest persisted run reproduced {result.get('matched', 0)}/"
            f"{result.get('recorded', 0)} recorded proposals",
            "explain every mismatch, then rerun replay --check-fidelity")
    if proposals == 0:
        return Gate(
            "G2", "Replay reproduces the agent's own decisions", COLLECTING,
            "no setup_proposed events recorded yet, so a fidelity check "
            "would reproduce 100% of nothing",
            "run the agent until it proposes setups", blocks=["B7.5", "G4"])
    if proposals < MIN_PROPOSALS_FOR_G2:
        return Gate(
            "G2", "Replay reproduces the agent's own decisions", COLLECTING,
            f"{proposals} recorded proposals; ~{MIN_PROPOSALS_FOR_G2} are "
            "needed before a 99% ratio is meaningful (at this n a single "
            f"mismatch reads as {(proposals - 1) / proposals:.0%})",
            "keep the agent running", blocks=["B7.5"])
    stale = (
        " A prior G2 result is stale because the corpus has changed."
        if result else "")
    return Gate(
        "G2", "Replay reproduces the agent's own decisions", READY,
        f"{proposals} recorded proposals: enough for the threshold to mean "
        f"something. It has not passed on this exact corpus.{stale}",
        "python research.py replay --check-fidelity")


def gate_g4(db, stats: dict) -> Gate:
    """The funnel decides whether any parameter sweep is worth running."""
    if stats["cycles"] == 0:
        return Gate(
            "G4", "Funnel published; dominant veto identified", COLLECTING,
            "no cycles recorded", "run the agent")
    return Gate(
        "G4", "Funnel published; dominant veto identified", READY,
        f"{stats['cycles']:,} cycles available",
        "python research.py funnel")


def gate_g5(db, stats: dict) -> Gate:
    """Formerly blocked 6.4. Dissolved by making the discriminator a parameter.

    Neither answer is baked into the contract, so neither filters the
    population the other needs. What remains is the ranking, which is an
    ordinary parameter sweep subject to the ordinary sample requirement.
    """
    trips = stats["matched_round_trips"]
    needed = 3 * MIN_PROPOSALS_FOR_G2
    if trips < needed:
        return Gate(
            "G5", "Breakout discriminator chosen on evidence", COLLECTING,
            f"{trips} matched round trips; {needed} needed for a 3-point "
            "axis. The contract default is 'none', so nothing is baked in "
            "while this waits",
            "keep collecting; conditioning axes need no extra sample")
    return Gate(
        "G5", "Breakout discriminator chosen on evidence", READY,
        f"{trips} matched round trips: the 3-point axis is powered",
        "python research.py sweep research/sweeps/"
        "breakout_discriminator.yaml")


def gate_g6(db, stats: dict) -> Gate:
    """H-G and H-H: months of collection, and no way to shorten it."""
    books = stats["book_state_events"]
    enrichment = stats["enrichment_events"]
    if books == 0 and enrichment == 0:
        return Gate(
            "G6", "H-G / H-H sample sufficiency", COLLECTING,
            "no book_state or snapshot_enrichment events yet. This is the "
            "clock B0.5 started and it cannot be shortened",
            "run the agent; these accrue every cycle")
    # A conditioning cell needs observations, and the plan's bar is per cell.
    return Gate(
        "G6", "H-G / H-H sample sufficiency", COLLECTING,
        f"{books:,} book_state and {enrichment:,} enrichment observations. "
        f"The bar is {MIN_OBSERVATIONS_PER_CELL} per conditioning cell, and "
        "cascades are infrequent by construction - expect roughly three "
        "months",
        "nothing to do but keep running")


def gate_b75(db, cfg: dict, g2: Gate) -> Gate:
    """Maker-first entry: a live experiment, so live evidence or nothing."""
    enabled = bool((cfg.get("execution") or {}).get("maker_first_enabled"))
    attempts = corpus.load_events(db, "maker_attempt") if db else []
    resting = [a for a in attempts if a.get("resting")]
    filled = [a for a in attempts if (a.get("filled_contracts") or 0) > 0]

    if resting:
        return Gate(
            "B7.5", "Passive entry validated on a live account", FAILED,
            f"{len(resting)} attempt(s) could not be cancelled and may have "
            "rested. That is the one failure mode the design forbids",
            "investigate before re-enabling; see research/plan/"
            "B7.5-record.md")

    if g2.status != PASS:
        return Gate(
            "B7.5", "Passive entry validated on a live account", BLOCKED,
            "gate G2 must pass first, so a working replay can detect any "
            "change in decision behaviour the entry path causes",
            "see G2 above")

    if not enabled:
        return Gate(
            "B7.5", "Passive entry validated on a live account", READY,
            "G2 is reachable and nothing is resting. The flag is off, which "
            "is correct until it has been exercised on demo",
            "set execution.maker_first_enabled: true in demo, then re-check")

    if len(attempts) < MIN_MAKER_ATTEMPTS:
        return Gate(
            "B7.5", "Passive entry validated on a live account", COLLECTING,
            f"{len(attempts)} of {MIN_MAKER_ATTEMPTS} passive attempts "
            "recorded, none resting so far",
            "keep running on demo")

    return Gate(
        "B7.5", "Passive entry validated on a live account", PASS,
        f"{len(attempts)} attempts, {len(filled)} filled, none left resting",
        "measure fill rate against the IOC counterfactual")


def report(db: str | Path | None, cfg: dict) -> list:
    """Every gate, in dependency order."""
    stats = (corpus.stats(db) if db and Path(db).exists()
             else {"cycles": 0, "matched_round_trips": 0,
                   "book_state_events": 0, "enrichment_events": 0,
                   "from_ts": 0.0, "to_ts": 0.0})
    g2 = gate_g2(db, stats, cfg) if db and Path(db).exists() else Gate(
        "G2", "Replay reproduces the agent's own decisions", COLLECTING,
        "no journal yet", "run the agent", blocks=["B7.5"])
    return [
        gate_g1(),
        g2,
        gate_g4(db, stats) if db and Path(db).exists() else Gate(
            "G4", "Funnel published; dominant veto identified", COLLECTING,
            "no journal yet", "run the agent"),
        gate_g5(db, stats) if db and Path(db).exists() else Gate(
            "G5", "Breakout discriminator chosen on evidence", COLLECTING,
            "no journal yet", "run the agent"),
        gate_g6(db, stats) if db and Path(db).exists() else Gate(
            "G6", "H-G / H-H sample sufficiency", COLLECTING,
            "no journal yet", "run the agent"),
        gate_b75(db if db and Path(db).exists() else None, cfg, g2),
    ], stats


def format_report(gates: list, stats: dict, db) -> str:
    lines = [f"readiness — {db}", ""]
    if stats["cycles"]:
        span_days = max(
            0.0, (stats["to_ts"] - stats["from_ts"]) / 86400)
        lines.append(
            f"  corpus       {stats['cycles']:,} cycles over "
            f"{span_days:.1f} days")
        lines.append(
            f"  round trips  {stats['matched_round_trips']:,}")
    else:
        lines.append("  corpus       empty — the agent has not run yet")
    lines.append("")
    lines.append(f"  {'GATE':<6} {'STATUS':<11} WHAT IT MEANS")
    lines.append(f"  {'-' * 6} {'-' * 11} {'-' * 48}")
    for gate in gates:
        lines.append(f"  {gate.name:<6} {gate.symbol:<11} {gate.title}")
        lines.append(f"  {'':<6} {'':<11} {gate.detail}")
        if gate.next_step:
            lines.append(f"  {'':<6} {'':<11} -> {gate.next_step}")
        lines.append("")

    failed = [g for g in gates if g.status == FAILED]
    if failed:
        lines.append("  ONE OR MORE GATES FAILED. A failure is a stop, not a "
                     "delay: something\n  is wrong and every number "
                     "downstream of it is suspect.")
    elif all(g.status in (PASS, READY) for g in gates):
        lines.append("  Every gate is satisfied or ready to run.")
    else:
        waiting = [g.name for g in gates if g.status == COLLECTING]
        lines.append(
            f"  Waiting on data: {', '.join(waiting)}. Nothing to build - "
            "these need\n  calendar time, and shortening them is the one "
            "thing that cannot be done.")
    return "\n".join(lines)
