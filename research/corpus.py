"""Read the corpus the agent has been writing and nobody has been reading.

Every cycle, the engine journals an ``llm_input`` event containing the exact
per-symbol snapshot dict and portfolio state that were fed to the model. That
is precisely the input ``strategy.setup_evidence``, ``strategy.build_setup_plan``
and ``RiskEngine.vet_open`` consume, and all three are pure functions of
``(snapshot, cfg)``. So an arbitrary number of parameter variants can be
replayed against exactly the data the live agent saw - offline, with no
exchange risk, no extra market-data calls and no LLM spend.

Until this module existed, ``grep -rn "llm_input"`` returned the writer and
its own unit tests. The richest data in the system had no consumer.

Three properties matter more than convenience here:

**Never raise on a malformed row.** A corpus loader that dies on one
truncated payload is a corpus loader that cannot read a real journal. Bad
rows are counted and skipped, and the count is reported, because a silent
skip and a clean parse must not look the same.

**Take the first observation per signal bar.** With a 300s cycle and a 900s
signal bar there are roughly three observations of each bar. The agent acted
on the first one; the second and third are the same bar re-observed after the
decision was already made. Averaging them would blend a decision with its own
aftermath.

**Report the gaps.** A corpus with holes in it must not look continuous. An
uptime gap is the difference between "the strategy took no trades" and "the
agent was not running", and those have opposite meanings.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


SNAPSHOT_MARKER = "MARKET SNAPSHOT (liquid USDT perpetual swaps on OKX):"
PORTFOLIO_MARKER = "PORTFOLIO STATE:"
MAX_NEW_MARKER = "You may propose at most "


@dataclass
class CycleRecord:
    """One cycle's model input, exactly as the model received it."""

    ts: float
    run_id: str | None
    cycle_id: str | None
    snapshot: dict
    portfolio: dict
    max_new: int
    provider: str
    variant_id: str | None = None
    strategy_config_version: str | None = None
    journal_rowid: int | None = None
    # Enrichment is joined from the separate ``snapshot_enrichment`` event,
    # never from ``llm_input``; conditioning axes read this private field.
    enrichment: dict = field(default_factory=dict)

    def symbols(self) -> list[str]:
        return [s for s, v in self.snapshot.items()
                if not s.startswith("_") and isinstance(v, dict)]


@dataclass
class ModelOutput:
    """What the model said, and how much trouble it had saying it."""

    ts: float
    cycle_id: str | None
    response_id: str | None
    raw_text: str
    parsed_decisions: list
    request_attempts: int


@dataclass
class LoadReport:
    """What was read, and what could not be."""

    rows: int = 0
    parsed: int = 0
    skipped: int = 0
    reasons: dict = field(default_factory=lambda: defaultdict(int))

    def skip(self, reason: str) -> None:
        self.skipped += 1
        self.reasons[reason] += 1


@dataclass
class G2ProposalReport:
    """Strict proposal-corpus diagnostics used only by gate G2.

    The generic event loader intentionally tolerates malformed rows for
    exploratory corpus statistics.  G2 cannot: a skipped proposal changes the
    denominator and can turn a broken journal into an apparent pass.
    """

    rows: int = 0
    parsed: int = 0
    malformed: int = 0
    duplicates: int = 0
    reasons: dict = field(default_factory=lambda: defaultdict(int))

    def reject(self, reason: str) -> None:
        self.malformed += 1
        self.reasons[reason] += 1


@dataclass
class G2EvidenceWindow:
    """The authoritative proposal rows for one strict G2 evaluation.

    Journals written before rich proposal identity existed remain readable as
    legacy-only corpora.  Once the first modern row appears, only modern rows
    from that deterministic journal position onward are evidence; old rows
    are excluded from the denominator and a legacy row after that boundary is
    a fail-closed journal violation.
    """

    rows: list[dict]
    legacy_mode: bool
    modern_boundary_ts: float | None = None
    modern_boundary_rowid: int | None = None
    modern_boundary_cycle_id: str | None = None
    legacy_excluded_count: int = 0
    legacy_after_boundary_count: int = 0


_G2_MODERN_FIELDS = (
    # Strategy context columns were added before the rich proposal payload;
    # they alone do not establish a modern identity boundary.
    "signal_ts", "setup_key", "setup_type",
)


def is_modern_g2_row(row: dict) -> bool:
    """Whether a loaded row carries any rich-identity evidence field."""
    return any(row.get(field) is not None for field in _G2_MODERN_FIELDS)


def g2_evidence_window(rows: list[dict]) -> G2EvidenceWindow:
    """Select strict G2 evidence without weakening legacy-only journals."""
    # SQLite rowid is the append position.  Timestamps can arrive late or be
    # backfilled, so they cannot define the migration boundary.
    ordered = sorted(rows, key=lambda row: (
        int(row.get("_g2_rowid") or 0), float(row.get("_g2_ts") or 0.0)))
    first_modern = next(
        (index for index, row in enumerate(ordered)
         if is_modern_g2_row(row)), None)
    if first_modern is None:
        return G2EvidenceWindow(ordered, legacy_mode=True)

    boundary_row = ordered[first_modern]
    boundary_ts = boundary_row.get("_g2_ts")
    boundary_rowid = boundary_row.get("_g2_rowid")
    boundary_cycle_id = boundary_row.get("cycle_id")
    evaluated = []
    legacy_before = 0
    legacy_after = 0
    for index, row in enumerate(ordered):
        if is_modern_g2_row(row):
            evaluated.append(row)
        elif index < first_modern:
            legacy_before += 1
        else:
            legacy_after += 1
    return G2EvidenceWindow(
        evaluated, legacy_mode=False,
        modern_boundary_ts=(float(boundary_ts)
                            if boundary_ts is not None else None),
        modern_boundary_rowid=(int(boundary_rowid)
                               if boundary_rowid is not None else None),
        modern_boundary_cycle_id=(str(boundary_cycle_id)
                                  if boundary_cycle_id is not None else None),
        legacy_excluded_count=legacy_before,
        legacy_after_boundary_count=legacy_after,
    )


def g2_post_boundary_cycle_ids(
        db: str | Path, evidence: G2EvidenceWindow) -> set[str] | None:
    """Return all model-input cycles in the authoritative G2 window.

    ``None`` means the journal has no model-input rows (as in small unit
    fixtures), so callers should retain their existing comparison behavior.
    The boundary cycle itself is included because the engine may append its
    ``llm_input`` before the proposal event that establishes the boundary.
    """
    if evidence.legacy_mode or evidence.modern_boundary_rowid is None:
        return None
    try:
        with closing(_connect(db)) as conn:
            columns = _columns(conn, "events")
            cycle_column = "cycle_id" if "cycle_id" in columns \
                else "NULL AS cycle_id"
            rows = conn.execute(
                "SELECT rowid, " + cycle_column
                + " FROM events WHERE kind='llm_input' "
                "ORDER BY rowid ASC").fetchall()
    except (sqlite3.Error, OSError):
        return None
    if not rows:
        return None
    cycle_ids = {
        str(row["cycle_id"])
        for row in rows
        if row["cycle_id"] is not None
        and int(row["rowid"]) >= int(evidence.modern_boundary_rowid)
    }
    if evidence.modern_boundary_cycle_id:
        cycle_ids.add(evidence.modern_boundary_cycle_id)
    return cycle_ids


def g2_replay_corpus_metadata(
        db: str | Path, evidence: G2EvidenceWindow | None = None) -> dict:
    """Digest the ordered model-input/output corpus in the G2 window."""
    if evidence is None:
        rows, _ = load_g2_proposals(db)
        evidence = g2_evidence_window(rows)
    try:
        with closing(_connect(db)) as conn:
            columns = _columns(conn, "events")
            if not {"ts", "kind", "payload"}.issubset(columns):
                raise sqlite3.OperationalError("events schema incomplete")
            cycle_column = "cycle_id" if "cycle_id" in columns \
                else "NULL AS cycle_id"
            events = conn.execute(
                "SELECT rowid, ts, kind, " + cycle_column + ", payload FROM events "
                "WHERE kind IN ('llm_input', 'llm_output') "
                "ORDER BY rowid ASC").fetchall()
    except (sqlite3.Error, OSError):
        events = []
    selected = []
    for event in events:
        rowid = int(event["rowid"])
        if evidence.legacy_mode or evidence.modern_boundary_rowid is None:
            in_window = True
        else:
            in_window = (
                rowid >= evidence.modern_boundary_rowid
                or (evidence.modern_boundary_cycle_id is not None
                    and event["cycle_id"] == evidence.modern_boundary_cycle_id))
        if not in_window:
            continue
        raw_payload = event["payload"]
        try:
            payload = json.loads(raw_payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = raw_payload
        selected.append({
            "rowid": rowid,
            "ts": float(event["ts"] or 0.0),
            "kind": str(event["kind"]),
            "cycle_id": event["cycle_id"],
            "payload": payload,
        })
    canonical = {
        "boundary_ts": evidence.modern_boundary_ts,
        "boundary_rowid": evidence.modern_boundary_rowid,
        "boundary_cycle_id": evidence.modern_boundary_cycle_id,
        "events": selected,
    }
    digest = hashlib.sha256(json.dumps(
        canonical, sort_keys=True, separators=(",", ":"),
        default=str, allow_nan=True).encode("utf-8")).hexdigest()
    return {
        "replay_corpus_digest": digest,
        "replay_corpus_count": len(selected),
        "replay_corpus_max_rowid": (selected[-1]["rowid"] if selected else 0),
    }


def _connect(db: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(db)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def user_message_of(request: dict, provider: str) -> str | None:
    """Pull the user message out of a recorded provider request.

    The snapshot lives inside the request body and the body's shape differs
    by provider: Anthropic carries the system prompt in its own field so the
    user message is ``messages[0]``, while OpenAI puts the system prompt in
    ``messages[0]`` and the user message in ``messages[1]``.

    Rather than trusting the index, find the message whose role is ``user``.
    A journal that spans a provider switch is otherwise silently mis-parsed
    for every row on one side of it.
    """
    messages = (request or {}).get("messages")
    if not isinstance(messages, list):
        return None
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            # Anthropic accepts a content-block list as well as plain text.
            if isinstance(content, list):
                parts = [b.get("text", "") for b in content
                         if isinstance(b, dict)]
                return "".join(parts) or None
    return None


def parse_user_message(text: str) -> tuple[dict, dict, int] | None:
    """Split the user message back into snapshot, portfolio and max_new.

    Returns ``None`` rather than raising on anything unexpected. This runs
    over thousands of rows written across months and code versions, and one
    unparseable row must cost one row.
    """
    if not isinstance(text, str) or SNAPSHOT_MARKER not in text:
        return None
    try:
        after_snapshot = text.split(SNAPSHOT_MARKER, 1)[1]
        snapshot_text, rest = after_snapshot.split(PORTFOLIO_MARKER, 1)
        snapshot = json.loads(snapshot_text.strip())

        portfolio_text = rest
        max_new = 0
        if MAX_NEW_MARKER in rest:
            portfolio_text, tail = rest.split(MAX_NEW_MARKER, 1)
            digits = ""
            for char in tail.strip():
                if char.isdigit():
                    digits += char
                else:
                    break
            max_new = int(digits) if digits else 0
        portfolio = json.loads(portfolio_text.strip())
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(snapshot, dict) or not isinstance(portfolio, dict):
        return None
    return snapshot, portfolio, max_new


def load_enrichment(db: str | Path) -> dict:
    """``cycle_id -> {"market": {...}, "symbols": {symbol: {...}}}``."""
    out: dict = {}
    for event in load_events(db, "snapshot_enrichment"):
        cycle_id = event.get("cycle_id")
        if not cycle_id:
            continue
        out[cycle_id] = {
            "market": event.get("market") or {},
            "symbols": event.get("symbols") or {},
        }
    return out


def load_cycles(db: str | Path) -> tuple[list[CycleRecord], LoadReport]:
    """Every recorded model input, oldest first, with enrichment joined on."""
    report = LoadReport()
    out: list[CycleRecord] = []
    enrichment = load_enrichment(db)
    with closing(_connect(db)) as conn:
        available = _columns(conn, "events")
        optional = [c for c in ("variant_id", "strategy_config_version")
                    if c in available]
        columns = ", ".join(["ts", "payload", "run_id", "cycle_id"] + optional)
        for row in conn.execute(
                f"SELECT rowid AS journal_rowid, {columns} "
                "FROM events WHERE kind='llm_input' ORDER BY ts ASC"):
            report.rows += 1
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                report.skip("payload is not json")
                continue
            provider = str(payload.get("provider") or "unknown")
            message = user_message_of(payload.get("request") or {}, provider)
            if message is None:
                report.skip("no user message in request")
                continue
            parsed = parse_user_message(message)
            if parsed is None:
                report.skip("user message did not parse")
                continue
            snapshot, portfolio, max_new = parsed
            report.parsed += 1
            out.append(CycleRecord(
                ts=float(row["ts"] or 0.0),
                run_id=row["run_id"], cycle_id=row["cycle_id"],
                snapshot=snapshot, portfolio=portfolio, max_new=max_new,
                provider=provider,
                variant_id=(row["variant_id"]
                            if "variant_id" in optional else None),
                strategy_config_version=(
                    row["strategy_config_version"]
                    if "strategy_config_version" in optional else None),
                journal_rowid=int(row["journal_rowid"]),
                enrichment=enrichment.get(row["cycle_id"]) or {},
            ))
    return out, report


def load_model_outputs(db: str | Path) -> tuple[list[ModelOutput], LoadReport]:
    """Every recorded model response, including the ones that failed to parse.

    ``parse_decisions`` drops malformed opens silently, so the model's
    malformed-output rate is invisible from the ``decisions`` event alone.
    Reading the raw response back is the only way to see it.
    """
    report = LoadReport()
    out: list[ModelOutput] = []
    with closing(_connect(db)) as conn:
        for row in conn.execute(
                "SELECT ts, payload, cycle_id FROM events "
                "WHERE kind='llm_output' ORDER BY ts ASC"):
            report.rows += 1
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                report.skip("payload is not json")
                continue
            response = payload.get("response") or {}
            raw_text = str(response.get("raw_text") or "")
            parsed: list = []
            if raw_text:
                try:
                    from agent.brain import parse_decisions
                    parsed = parse_decisions(raw_text)
                except Exception:                          # noqa: BLE001
                    report.skip("decisions did not parse")
                    parsed = []
            report.parsed += 1
            out.append(ModelOutput(
                ts=float(row["ts"] or 0.0), cycle_id=row["cycle_id"],
                response_id=response.get("id"), raw_text=raw_text,
                parsed_decisions=parsed,
                request_attempts=len(payload.get("request_attempts") or []),
            ))
    return out, report


def load_events(db: str | Path, kind: str) -> list[dict]:
    """Any journalled event kind, decoded, oldest first. Bad rows skipped."""
    out: list[dict] = []
    with closing(_connect(db)) as conn:
        for row in conn.execute(
                "SELECT ts, payload, cycle_id, setup_id FROM events "
                "WHERE kind=? ORDER BY ts ASC", (kind,)):
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                payload = {"value": payload}
            out.append({
                "ts": float(row["ts"] or 0.0), "cycle_id": row["cycle_id"],
                "setup_id": row["setup_id"], **payload,
            })
    return out


def load_g2_proposals(db: str | Path) -> tuple[list[dict], G2ProposalReport]:
    """Load ``setup_proposed`` rows without changing generic corpus behavior.

    Columns were added to the journal over time, so the query is assembled
    from the columns actually present.  Payload fields remain authoritative
    for values written by older builds; journal columns are used as a
    fallback.  Every row is returned with a private ``_g2_rowid`` marker so
    diagnostics can identify duplicate/malformed input without exposing the
    SQLite implementation to callers.
    """
    report = G2ProposalReport()
    out: list[dict] = []
    try:
        conn = _connect(db)
    except (sqlite3.Error, OSError):
        report.reject("journal_unreadable")
        return out, report
    # ``sqlite3.Connection.__exit__`` commits/rolls back but does not close
    # the handle.  This loader is called repeatedly by readiness and replay,
    # so close it explicitly even on the early-return diagnostics below.
    with closing(conn):
        columns = _columns(conn, "events")
        if not columns:
            report.reject("events_table_missing")
            return out, report
        if not {"ts", "payload", "kind"}.issubset(columns):
            report.reject("events_schema_missing")
            return out, report
        optional = [
            name for name in (
                "strategy_id", "strategy_version", "variant_id",
                "strategy_config_version", "prompt_version", "setup_key",
                "setup_type", "signal_ts", "symbol", "direction",
            ) if name in columns
        ]
        # ``rowid`` is an implicit SQLite column and is not reported by
        # PRAGMA table_info, but selecting it is safe for the journal
        # tables we support and gives deterministic duplicate diagnostics.
        selected = ["rowid", "ts", "payload"]
        selected.extend(name for name in ("cycle_id", "setup_id")
                        if name in columns)
        selected.extend(optional)
        try:
            rows = conn.execute(
                "SELECT " + ", ".join(selected) +
                " FROM events WHERE kind='setup_proposed' "
                "ORDER BY rowid ASC").fetchall()
        except sqlite3.Error:
            report.reject("journal_query_failed")
            return out, report
        for row in rows:
            report.rows += 1
            raw = row["payload"]
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                report.reject("invalid_json")
                continue
            if not isinstance(payload, dict):
                report.reject("payload_not_object")
                continue
            data = {key: row[key] for key in selected if key in row.keys()}
            # Payload wins: a context column can be stale relative to the
            # explicit event body in journals written during a migration.
            data.update(payload)
            data["_g2_rowid"] = int(row["rowid"])
            data["_g2_ts"] = float(row["ts"] or 0.0)
            data["_g2_cycle_id"] = (
                row["cycle_id"] if "cycle_id" in row.keys() else None)
            data["_g2_setup_id"] = (
                row["setup_id"] if "setup_id" in row.keys() else None)
            # A proposal must identify the cycle, symbol, and direction.  The
            # remaining identity fields are optional for legacy journals and
            # are handled by replay.canonical_proposal_identity.
            if not isinstance(data.get("cycle_id") or data.get("_g2_cycle_id"),
                              str) or not str(data.get("cycle_id") or
                                               data.get("_g2_cycle_id")):
                report.reject("missing_cycle_id")
                continue
            if not isinstance(data.get("symbol"), str) or not str(
                    data.get("symbol")).strip():
                report.reject("missing_symbol")
                continue
            if data.get("direction") not in {"long", "short"}:
                report.reject("invalid_direction")
                continue
            data["cycle_id"] = str(data.get("cycle_id") or
                                    data.get("_g2_cycle_id"))
            data["symbol"] = str(data["symbol"]).strip()
            data["direction"] = str(data["direction"])
            report.parsed += 1
            out.append(data)
    return out, report


def g2_proposal_digest(rows: list[dict]) -> str:
    """Content digest for a canonical proposal corpus.

    Sorting makes the digest independent of SQLite insertion order while
    retaining duplicates (which are themselves a fail-closed diagnostic).
    Private row markers are excluded so a backup/restore does not alter the
    content identity.
    """
    canonical = []
    for row in rows:
        normalized = {}
        for key in sorted(row):
            if key.startswith("_") or key == "rowid":
                continue
            value = row.get(key)
            if key == "payload" and isinstance(value, str):
                try:
                    value = json.loads(value)
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            normalized[key] = value
        canonical.append(normalized)
    canonical.sort(key=lambda row: json.dumps(
        row, sort_keys=True, separators=(",", ":"), default=str))
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                         default=str, allow_nan=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_outcomes(db: str | Path) -> dict:
    """Trades and the lifecycle events that explain what happened to setups."""
    with closing(_connect(db)) as conn:
        trades = [dict(row) for row in conn.execute(
            "SELECT * FROM trades ORDER BY ts ASC")]
    return {
        "trades": trades,
        "setup_status": load_events(db, "setup_status"),
        "setup_proposed": load_events(db, "setup_proposed"),
        "rejected": load_events(db, "rejected"),
        "order_execution": load_events(db, "order_execution"),
        "entry_liquidity_rejected": load_events(
            db, "entry_liquidity_rejected"),
    }


def index_by_signal_bar(cycles: list[CycleRecord]) -> dict:
    """``(symbol, signal_ts) -> the first snapshot seen for that bar``.

    The first, deliberately. With a 300s cycle against a 900s signal bar the
    same bar is observed about three times, and the agent decided on the
    first observation. Later ones are the same bar seen after the decision
    was taken, so treating them as independent would both inflate the sample
    and blend a decision with its own aftermath.
    """
    out: dict = {}
    for cycle in cycles:
        for symbol in cycle.symbols():
            data = cycle.snapshot[symbol]
            signal_ts = data.get("signal_ts")
            if signal_ts is None:
                continue
            key = (symbol, int(signal_ts))
            if key not in out:
                out[key] = {"cycle": cycle, "snapshot": data}
    return out


def uptime_gaps(cycles: list[CycleRecord],
                threshold_seconds: float = 3600.0) -> list[dict]:
    """Stretches where the agent was not running, so holes are visible."""
    gaps = []
    for previous, current in zip(cycles, cycles[1:]):
        delta = current.ts - previous.ts
        if delta > threshold_seconds:
            gaps.append({
                "from_ts": previous.ts, "to_ts": current.ts,
                "seconds": delta,
                "crossed_run": previous.run_id != current.run_id,
            })
    return gaps


def _utc(ts: float) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M")


def stats(db: str | Path) -> dict:
    """Answer the question nobody could answer before: how much data is there?

    And the question that follows it, which is the one that decides whether
    this programme can conclude anything at all: is it enough to reject a
    hypothesis?
    """
    cycles, cycle_report = load_cycles(db)
    outputs, output_report = load_model_outputs(db)
    outcomes = load_outcomes(db)
    bars = index_by_signal_bar(cycles)
    gaps = uptime_gaps(cycles)

    proposals = outcomes["setup_proposed"]
    rejected = outcomes["rejected"]
    trades = outcomes["trades"]
    opens = [t for t in trades if str(t.get("action")) == "open"]
    closes = [t for t in trades if str(t.get("action")) == "close"]
    open_ids = {t.get("trade_id") for t in opens if t.get("trade_id")}
    matched = len([t for t in closes if t.get("trade_id") in open_ids])

    return {
        "db": str(db),
        "cycles": len(cycles),
        "cycles_skipped": cycle_report.skipped,
        "cycle_skip_reasons": dict(cycle_report.reasons),
        "from_ts": cycles[0].ts if cycles else 0.0,
        "to_ts": cycles[-1].ts if cycles else 0.0,
        "runs": len({c.run_id for c in cycles if c.run_id}),
        "gaps": gaps,
        "gap_seconds": sum(g["seconds"] for g in gaps),
        "unique_symbols": len({s for c in cycles for s in c.symbols()}),
        "signal_bars": len(bars),
        "model_outputs": len(outputs),
        "output_parse_failures": output_report.skipped,
        "proposals": len(proposals),
        "rejections": len(rejected),
        "opens": len(opens),
        "matched_round_trips": matched,
        "book_state_events": len(load_events(db, "book_state")),
        "enrichment_events": len(load_events(db, "snapshot_enrichment")),
        "providers": sorted({c.provider for c in cycles}),
    }


def format_stats(s: dict) -> str:
    """The output that decides whether anything downstream is worth running."""
    span = f"{_utc(s['from_ts'])} -> {_utc(s['to_ts'])} UTC"
    hours = s["gap_seconds"] / 3600
    lines = [
        f"corpus: {s['db']}",
        f"  cycles           {s['cycles']:>8,}   {span}",
    ]
    if s["cycles_skipped"]:
        reasons = ", ".join(f"{k}: {v}" for k, v
                            in sorted(s["cycle_skip_reasons"].items()))
        lines.append(f"  unparseable      {s['cycles_skipped']:>8,}   "
                     f"({reasons})")
    lines += [
        f"  runs             {s['runs']:>8,}   gaps > 1h: "
        f"{len(s['gaps'])} (total {hours:.1f}h downtime)",
        f"  unique symbols   {s['unique_symbols']:>8,}",
        f"  signal bars      {s['signal_bars']:>8,}   "
        f"(symbol x 15m bar observations)",
        f"  model outputs    {s['model_outputs']:>8,}   parse failures: "
        f"{s['output_parse_failures']}",
        f"  proposals        {s['proposals']:>8,}   rejections: "
        f"{s['rejections']}",
        f"  opens            {s['opens']:>8,}",
        f"  round trips      {s['matched_round_trips']:>8,}",
        "",
        f"  book_state       {s['book_state_events']:>8,}   "
        f"enrichment: {s['enrichment_events']:,}",
    ]
    if s["providers"]:
        lines.append(f"  providers        {', '.join(s['providers']):>8}")

    # Promotion requires 100 matched round trips per setting and at least
    # three settings on an axis, so a hypothesis needs 300 matched trips.
    trips = s["matched_round_trips"]
    lines += ["", "  " + (
        f"{trips} round trips. A single parameter axis needs ~300 "
        f"(3 settings x 100). Conditioning axes reuse all {trips}."
        if trips < 300 else
        f"{trips} round trips: a three-point parameter axis is now "
        "in reach.")]
    return "\n".join(lines)
