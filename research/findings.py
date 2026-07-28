"""The findings store: append-only, so a rejection stays legible afterwards.

Intention #5 - every learning and recommendation persisted, per strategy and
per variant, reviewable later. ``report.py`` printed to stdout and forgot,
which meant the reasoning behind a decision survived only as long as the
terminal scrollback.

Two design commitments, both about what happens to negative results.

**Findings are append-only.** A rejection is a row, not a deletion. Six
months from now the question that matters is not "which variants are alive"
but "why was this rejected, and on what sample" - and if the answer was
deleted the same idea comes back, gets tested again, and consumes the same
calendar time a second time.

**Null results are recorded with the same weight as positive ones.** A
programme that only writes down what worked is a programme that only writes
down noise: at these sample sizes, filtering for positives is filtering for
the largest random numbers. ``INSUFFICIENT_SAMPLE`` is a finding, and often
the most useful one, because it says the question is still open rather than
answered in the negative.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


DEFAULT_STORE = Path(__file__).resolve().parent / "cache" / "findings.db"

KINDS = ("observation", "recommendation", "decision")


def _connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS variants (
            variant_id TEXT PRIMARY KEY, strategy_id TEXT,
            base_version TEXT, overrides_json TEXT, hypothesis TEXT,
            status TEXT, created_ts REAL, updated_ts REAL);
        CREATE TABLE IF NOT EXISTS variant_runs (
            run_id TEXT PRIMARY KEY, variant_id TEXT, corpus_from_ts REAL,
            corpus_to_ts REAL, corpus_cycles INTEGER, mode TEXT,
            code_version TEXT, scorer_version TEXT, ts REAL);
        CREATE TABLE IF NOT EXISTS variant_results (
            run_id TEXT, metric TEXT, value REAL, ci_low REAL,
            ci_high REAL, n INTEGER);
        CREATE TABLE IF NOT EXISTS findings (
            finding_id INTEGER PRIMARY KEY AUTOINCREMENT,
            variant_id TEXT, ts REAL, author TEXT, kind TEXT, text TEXT,
            run_id TEXT);
        CREATE INDEX IF NOT EXISTS findings_variant
            ON findings (variant_id, ts);
    """)
    return conn


class FindingsStore:
    def __init__(self, path: str | Path = DEFAULT_STORE) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------- variants

    def register(self, variant) -> None:
        """Idempotent: re-registering unchanged content touches nothing.

        ``updated_ts`` reaches the committed index, so bumping it on every
        run would make ``research.py report`` produce a diff every time it
        was invoked - and a diff that always appears is a diff nobody reads.
        """
        now = time.time()
        overrides = json.dumps(variant.overrides, sort_keys=True)
        with _connect(self.path) as conn:
            existing = conn.execute(
                "SELECT * FROM variants WHERE variant_id=?",
                (variant.variant_id,)).fetchone()
            if existing is not None:
                unchanged = (
                    existing["strategy_id"] == variant.strategy_id
                    and existing["base_version"] == variant.base_version
                    and existing["overrides_json"] == overrides
                    and existing["hypothesis"] == variant.hypothesis
                    and existing["status"] == variant.status)
                if unchanged:
                    return
            conn.execute(
                "INSERT OR REPLACE INTO variants (variant_id, strategy_id, "
                "base_version, overrides_json, hypothesis, status, "
                "created_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?)",
                (variant.variant_id, variant.strategy_id,
                 variant.base_version, overrides,
                 variant.hypothesis, variant.status,
                 existing["created_ts"] if existing else now, now))

    def set_status(self, variant_id: str, status: str) -> None:
        with _connect(self.path) as conn:
            conn.execute(
                "UPDATE variants SET status=?, updated_ts=? "
                "WHERE variant_id=?", (status, time.time(), variant_id))

    def variants(self) -> list:
        with _connect(self.path) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM variants ORDER BY variant_id")]

    def variant(self, variant_id: str) -> dict | None:
        with _connect(self.path) as conn:
            row = conn.execute(
                "SELECT * FROM variants WHERE variant_id=?",
                (variant_id,)).fetchone()
        return dict(row) if row else None

    # ----------------------------------------------------------------- runs

    def record_run(self, run_id: str, variant_id: str, result,
                   scorer_version: str = "1", code_version: str = "") -> None:
        with _connect(self.path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO variant_runs (run_id, variant_id, "
                "corpus_from_ts, corpus_to_ts, corpus_cycles, mode, "
                "code_version, scorer_version, ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, variant_id, result.corpus_from_ts,
                 result.corpus_to_ts, result.cycles, result.mode,
                 code_version, scorer_version, time.time()))

    def record_metrics(self, run_id: str, scored: dict) -> None:
        rows = []
        for metric, value in scored.items():
            if metric in ("label", "verdict"):
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            rows.append((run_id, metric, numeric,
                         float(scored.get("ci_low") or 0.0),
                         float(scored.get("ci_high") or 0.0),
                         int(scored.get("n") or 0)))
        with _connect(self.path) as conn:
            conn.execute("DELETE FROM variant_results WHERE run_id=?",
                         (run_id,))
            conn.executemany(
                "INSERT INTO variant_results (run_id, metric, value, "
                "ci_low, ci_high, n) VALUES (?,?,?,?,?,?)", rows)

    def runs_for(self, variant_id: str) -> list:
        with _connect(self.path) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM variant_runs WHERE variant_id=? "
                "ORDER BY ts DESC", (variant_id,))]

    def metrics_for(self, run_id: str) -> dict:
        with _connect(self.path) as conn:
            return {r["metric"]: dict(r) for r in conn.execute(
                "SELECT * FROM variant_results WHERE run_id=?", (run_id,))}

    # ------------------------------------------------------------- findings

    def add_finding(self, variant_id: str, kind: str, text: str,
                    author: str = "research", run_id: str = "",
                    ts: float | None = None) -> int:
        """Append a finding. Nothing in this class ever deletes one."""
        if kind not in KINDS:
            raise ValueError(
                f"kind must be one of {', '.join(KINDS)}, got {kind!r}")
        with _connect(self.path) as conn:
            cursor = conn.execute(
                "INSERT INTO findings (variant_id, ts, author, kind, text, "
                "run_id) VALUES (?,?,?,?,?,?)",
                (variant_id, ts if ts is not None else time.time(),
                 author, kind, text, run_id))
            return int(cursor.lastrowid)

    def findings_for(self, variant_id: str) -> list:
        with _connect(self.path) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM findings WHERE variant_id=? "
                "ORDER BY ts ASC, finding_id ASC", (variant_id,))]


# --------------------------------------------------------------- scorecards

def _fmt(value, spec: str = "+.4f") -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if number == float("inf"):
        return "inf"
    return format(number, spec)


def scorecard(store: FindingsStore, variant_id: str,
              baseline_id: str = "momentum.baseline") -> str:
    """One markdown file per variant, regenerated deterministically.

    Deterministic matters because these are committed: a generator whose
    output moved on every run would produce a diff on every run, and a diff
    that always appears is a diff nobody reads.

    Nothing here is derived from ``time.time()`` for that reason - the
    timestamps come from the stored rows.
    """
    variant = store.variant(variant_id)
    if variant is None:
        return f"# {variant_id}\n\nNot registered.\n"

    runs = store.runs_for(variant_id)
    latest = runs[0] if runs else None
    metrics = store.metrics_for(latest["run_id"]) if latest else {}
    overrides = json.loads(variant["overrides_json"] or "{}")

    lines = [f"# {variant_id}", ""]
    lines.append(f"Status: {variant['status']}")
    lines.append(f"Hypothesis: {variant['hypothesis']}")
    if overrides:
        rendered = ", ".join(f"{k} = {v}" for k, v in sorted(overrides.items()))
        lines.append(f"Overrides: {rendered}")
    else:
        lines.append("Overrides: none (this is the comparison floor)")
    lines.append("")

    lines.append("## Sample")
    if latest is None:
        lines += ["", "Registered but never run. No sample, and therefore no "
                      "result to report.", ""]
    else:
        n = int((metrics.get("n") or {}).get("n")
                or (metrics.get("expectancy_r") or {}).get("n") or 0)
        mde = (metrics.get("mde_r") or {}).get("value")
        lines += [
            "",
            f"corpus {int(latest['corpus_cycles'] or 0):,} cycles | "
            f"mode {latest['mode']} | {n} round trips",
            f"MDE at n={n}: {_fmt(mde, '.4f')}R "
            f"-- effects below this are undetectable",
            "",
        ]
        lines.append("## Results")
        lines.append("")
        lines.append("| metric | value | 95% interval |")
        lines.append("| --- | --- | --- |")
        for name in ("expectancy_r", "win_rate", "profit_factor", "total_r",
                     "max_drawdown_r"):
            row = metrics.get(name)
            if not row:
                continue
            interval = (f"[{_fmt(row['ci_low'])}, {_fmt(row['ci_high'])}]"
                        if name == "expectancy_r" else "")
            lines.append(f"| {name} | {_fmt(row['value'])} | {interval} |")
        lines.append("")

    log = store.findings_for(variant_id)
    lines.append("## Findings log")
    lines.append("")
    if not log:
        lines.append("No findings recorded yet.")
    else:
        for entry in log:
            stamp = time.strftime("%Y-%m-%d",
                                  time.gmtime(float(entry["ts"] or 0)))
            lines.append(f"- **{stamp}  {entry['kind']}** — {entry['text']}")
    lines.append("")
    return "\n".join(lines)


def index(store: FindingsStore) -> str:
    """``findings/README.md``: every variant, status, sample, last updated."""
    lines = [
        "# Findings index",
        "",
        "Every registered variant, including the rejected ones. A rejection "
        "is a row here, never a deletion: the question six months from now "
        "is not which variants are alive but why this one was rejected and "
        "on what sample.",
        "",
        "| variant | status | round trips | expectancy | last updated |",
        "| --- | --- | --- | --- | --- |",
    ]
    for variant in store.variants():
        runs = store.runs_for(variant["variant_id"])
        metrics = store.metrics_for(runs[0]["run_id"]) if runs else {}
        expectancy = metrics.get("expectancy_r")
        n = int((expectancy or {}).get("n") or 0)
        updated = time.strftime(
            "%Y-%m-%d", time.gmtime(float(variant["updated_ts"] or 0)))
        lines.append(
            f"| [{variant['variant_id']}]"
            f"({variant['strategy_id']}/{variant['variant_id']}.md) "
            f"| {variant['status']} | {n} "
            f"| {_fmt((expectancy or {}).get('value'))} | {updated} |")
    lines.append("")
    return "\n".join(lines)


def write_scorecards(store: FindingsStore, root: str | Path) -> list:
    """Regenerate every scorecard. Running twice with no new data is a no-op."""
    root = Path(root)
    written = []
    for variant in store.variants():
        directory = root / variant["strategy_id"]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{variant['variant_id']}.md"
        path.write_text(scorecard(store, variant["variant_id"]),
                        encoding="utf-8")
        written.append(path)
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text(index(store), encoding="utf-8")
    written.append(root / "README.md")
    return written
