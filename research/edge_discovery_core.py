"""Deterministic discovery helpers shared by the discovery facades.

The helper implementations in this module intentionally remain independent of
the lifecycle/ledger orchestration in :mod:`research.edge_lab`.  Dependency
proxies resolve through that facade at call time, preserving the historical
patch seams while keeping this module import-safe on its own.
"""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import random
from typing import Any, Mapping, Sequence

from agent.contracts.risk_geometry import (
    RiskGeometryError, effective_stop_distance, equity_price_increment,
    quantize_equity_bracket,
)
from agent.contracts.rule import (
    MIN_STOP_DISTANCE_BPS, RULE_SCHEMA_V4, completed_bar_exit_transition,
    frozen_target_reference, hold_deadline, initialize_exit_state,
    rule_vehicle_executable, thesis_exit_deadline, validate_rule_spec,
)
from .costs import (BAR, QUOTE, RESTING_BRACKET,
                    RESTING_BRACKET_FILL_SCHEMA, STRESSED_COST_BASIS,
                    STRESSED_COST_SCHEMA, CostError, CostModel,
                    ReplayPolicy, SQLiteQuoteIndex,
                    SQLiteQuoteIndexDescriptor, check_stressed_cost_plan,
                    check_entry_slippage, index_quotes, quote_fill,
                    quote_fill_record, resting_bracket_fill_claim,
                    stressed_cost_usd)
from .market_data import (OptionSnapshot, QuoteSnapshot, UnderlyingBar,
                          historical_backfill_record, replay_available_at,
                          replay_open_is_available,
                          replay_record_is_available)
from .stats import stable_seed
from .gates import (
    ACTUAL_CONTROL_MIN_COVERAGE, ACTUAL_CONTROL_MIN_MATCHED,
    FALSIFICATION_INDEPENDENT_METHOD,
)

MIN_PROMOTION_CLUSTERS = 30
# A randomized-entry null is only useful when it covers nearly all of the
# candidate opportunities.  Keep this local to the discovery/null boundary;
# fit-only diagnostics must not be able to lower an authorizing control floor.
MIN_NULL_CONTROL_MATCHED = 30
MIN_NULL_CONTROL_COVERAGE = 0.80


def _facade_dependency(name: str):
    from . import edge_lab
    return getattr(edge_lab, name)


def _simulation_dependency(name: str):
    # `factory_core` reaches this module through the ledger facades, so its
    # shared replay primitives are resolved at call time rather than imported.
    from . import factory_core
    return getattr(factory_core, name)


def _visible(*args, **kwargs):
    return _simulation_dependency("_visible")(*args, **kwargs)


def _option_at(*args, **kwargs):
    return _simulation_dependency("_option_at")(*args, **kwargs)


def normalize_underlying_bar(*args, **kwargs):
    return _facade_dependency("normalize_underlying_bar")(*args, **kwargs)


def normalize_option_snapshot(*args, **kwargs):
    return _facade_dependency("normalize_option_snapshot")(*args, **kwargs)


def normalize_quote(*args, **kwargs):
    return _facade_dependency("normalize_quote")(*args, **kwargs)


def IBRConfig(*args, **kwargs):
    return _facade_dependency("IBRConfig")(*args, **kwargs)


def ZoneInfo(*args, **kwargs):
    return _facade_dependency("ZoneInfo")(*args, **kwargs)


def chronological_split(*args, **kwargs):
    return _facade_dependency("chronological_split")(*args, **kwargs)


def structural_floor(*args, **kwargs):
    return _facade_dependency("structural_floor")(*args, **kwargs)


def heldout_separation(*args, **kwargs):
    return _facade_dependency("heldout_separation")(*args, **kwargs)


def paired_delta(*args, **kwargs):
    return _facade_dependency("paired_delta")(*args, **kwargs)


def matched_cluster_test(*args, **kwargs):
    return _facade_dependency("matched_cluster_test")(*args, **kwargs)


def paired_control_adequacy(*args, **kwargs):
    return _facade_dependency("paired_control_adequacy")(*args, **kwargs)


def deterministic_placebo_deltas(*args, **kwargs):
    return _facade_dependency("deterministic_placebo_deltas")(*args, **kwargs)


def falsification_gate(*args, **kwargs):
    return _facade_dependency("falsification_gate")(*args, **kwargs)


def max_drawdown_of(*args, **kwargs):
    return _facade_dependency("max_drawdown_of")(*args, **kwargs)


def sample_counts(*args, **kwargs):
    return _facade_dependency("sample_counts")(*args, **kwargs)


def authorization_projection(*args, **kwargs):
    return _facade_dependency("authorization_projection")(*args, **kwargs)


def arm_evidence_report(*args, **kwargs):
    return _facade_dependency("arm_evidence_report")(*args, **kwargs)


def _projection_summary(projection: Mapping[str, Any]) -> dict:
    return {key: projection.get(key) for key in (
        "schema", "vehicle", "equity_feed", "strict", "counts", "reasons",
        "excluded")}


def verified_gate_envelope(*args, **kwargs):
    return _facade_dependency("verified_gate_envelope")(*args, **kwargs)


class DiscoveryError(ValueError):
    """Raised when a discovery corpus cannot be evaluated safely."""


SESSION_WINDOW_ENV = "ALPACA_RESEARCH_SESSION_WINDOW"
QUOTE_INDEX_MIN_BYTES = 32 * 1024 * 1024


def _canonical_json(value: Any) -> str:
    """Match the ledger's canonical JSON encoding without importing SQLite."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str)


class _StreamingRawRows:
    """List-compatible raw rows whose canonical digest was built while reading."""

    def __init__(self, source: Path, count: int, digest: str):
        self.source = source
        self.count = int(count)
        self.digest = str(digest)

    def __iter__(self):
        return _stream_rows(self.source)

    def __len__(self) -> int:
        return self.count

    def __getitem__(self, key):
        return list(iter(self))[key]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (list, tuple, _StreamingRawRows)):
            return list(self) == list(other)
        return NotImplemented

    def content_hash(self) -> str:
        return self.digest


def _corpus_size(source: Path) -> int:
    """Return the available JSONL bytes without reading any row into memory."""
    paths = (sorted(source.glob("*.jsonl")) if source.is_dir() else [source])
    total = 0
    for path in paths:
        try:
            total += int(path.stat().st_size)
        except OSError as exc:
            raise DiscoveryError(f"unable to stat discovery JSONL {path}: {exc}") from exc
    return total


def corpus_partitions(source: Path, *, window: int | None = None) -> list[Path]:
    """Return the session partitions of a corpus directory, oldest first.

    ``window`` keeps only the most recent partitions, so a replay that needs
    the last few sessions never pays for the whole recorded history.
    """
    partitions = sorted(path for path in source.glob("*.jsonl") if path.is_file())
    if window is None:
        try:
            window = int(os.getenv(SESSION_WINDOW_ENV, "0"))
        except ValueError as exc:
            raise DiscoveryError(f"invalid {SESSION_WINDOW_ENV}: {exc}") from exc
    return partitions[-window:] if window and window > 0 else partitions


def _stream_rows(source: Path):
    """Yield decoded rows one line at a time; never hold the file in memory."""
    for partition in (corpus_partitions(source) if source.is_dir() else [source]):
        try:
            with partition.open(encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        yield json.loads(line)
        except (OSError, json.JSONDecodeError) as exc:
            raise DiscoveryError(f"invalid discovery JSONL {partition}: {exc}") from exc


def _row_provenance(row: Mapping[str, Any], *, kind: str,
                    require: bool = False,
                    expected_equity_feed: str = "iex",
                    expected_provider: str | None = None) -> tuple[str | None, str | None]:
    """Read a row's provenance without manufacturing research metadata.

    Recorder rows carry these fields at write time.  An explicitly supplied
    external corpus must do the same: passing the CLI's provider/feed as
    normalizer overrides must not make an unlabelled or differently sourced
    row look like configured evidence.  Non-strict callers retain the legacy
    diagnostic projection, but strict authorizing callers get a hard error
    before replay can create gates.
    """
    equity = kind in {"bar", "underlying", "underlying_bar",
                      "quote", "quote_snapshot", "equity_quote",
                      "underlying_quote"}
    raw_provider = row.get("provider")
    raw_feed = row.get("feed", row.get("feed_id"))
    provider = None if raw_provider is None else str(raw_provider).strip()
    feed = (None if raw_feed is None else
            str(raw_feed).strip().lower().replace("-", "_"))
    if feed == "delayed":
        feed = "delayed_sip"
    if require and not provider:
        raise DiscoveryError(
            f"{kind} research requires explicit provider provenance")
    if require and equity:
        if not feed:
            raise DiscoveryError(
                f"{kind} research requires explicit feed provenance")
        expected = (str(expected_equity_feed or "iex").strip().lower()
                    .replace("-", "_"))
        if expected == "delayed":
            expected = "delayed_sip"
        if expected not in {"iex", "sip"}:
            raise DiscoveryError(
                "equity research requires a configured real-time feed "
                f"(iex or sip); {expected or '[missing]'} is diagnostic-only")
        if feed == "delayed":
            feed = "delayed_sip"
        if feed != expected:
            qualifier = ("; delayed_sip is diagnostic-only"
                         if feed == "delayed_sip" else "")
            raise DiscoveryError(
                f"{kind} row feed {feed!r} does not match configured "
                f"executable feed {expected!r}{qualifier}")
        if expected_provider and provider != str(expected_provider).strip():
            raise DiscoveryError(
                f"{kind} row provider {provider!r} does not match configured provider")
    return provider, feed


def _normalize_corpus(rows, *, keep=None, quote_index: SQLiteQuoteIndex | None = None,
                      include_quotes: bool = True,
                      require_provenance: bool = False,
                      expected_equity_feed: str = "iex",
                      expected_provider: str | None = None) -> tuple[
        list[UnderlyingBar], dict[str, OptionSnapshot], list[QuoteSnapshot]]:
    """Normalize one row stream into the three replay books.

    ``keep`` is an optional session predicate applied *after* normalization,
    so a re-read of one partition yields exactly the records a slice of the
    whole corpus would have yielded, in the same order.
    """
    bars: list[UnderlyingBar] = []
    snapshots: dict[str, OptionSnapshot] = {}
    quotes: list[QuoteSnapshot] = [] if quote_index is None else quote_index
    for number, source_row in enumerate(rows, 1):
        if not isinstance(source_row, Mapping):
            raise DiscoveryError("discovery rows must be JSON objects")
        row = dict(source_row)
        kind = str(row.get("kind", "bar")).lower()
        try:
            provider, feed = _row_provenance(
                row, kind=kind, require=require_provenance,
                expected_equity_feed=expected_equity_feed,
                expected_provider=expected_provider)
            if kind in {"bar", "underlying", "underlying_bar"}:
                # The permissive projection is diagnostic-only compatibility
                # for old in-memory fixtures.  Authorizing callers always set
                # ``require_provenance`` and therefore never reach defaults.
                bar = normalize_underlying_bar(
                    row, provider=provider or "alpaca", feed=feed or "iex")
                if keep is None or keep(_bar_session(bar)):
                    bars.append(bar)
            elif kind in {"option", "option_snapshot", "option_quote"}:
                contract = row.get("contract")
                if isinstance(contract, Mapping):
                    flattened = dict(contract)
                    flattened.update({key: value for key, value in row.items()
                                      if key != "contract"})
                    row = flattened
                option_provider, option_feed = _row_provenance(
                    row, kind=kind, require=True,
                    expected_provider=expected_provider)
                option_provider = option_provider or "alpaca"
                option_feed = option_feed or ""
                if option_feed != "opra":
                    raise ValueError(
                        "option research requires explicit OPRA feed provenance")
                provider = option_provider
                feed = option_feed
                snap = normalize_option_snapshot(row, provider=provider, feed=feed)
                if keep is None or keep(snap.session_date.isoformat()):
                    snapshots[f"{snap.timestamp.isoformat()}|{snap.contract.symbol}"] = snap
            elif kind in {"quote", "quote_snapshot", "equity_quote",
                          "underlying_quote"}:
                # An equity quote is the executable price at its instant.  It
                # is used only where a fill lands on that instant; everything
                # else still falls back to the bar and says so.
                quote = normalize_quote(
                    row, provider=provider or "alpaca", feed=feed or "iex")
                if (include_quotes and
                        (keep is None or keep(quote.session_date.isoformat()))):
                    if quote_index is None:
                        quotes.append(quote)
                    elif not getattr(quote_index, "_read_only", False):
                        quote_index.add(quote)
            # Other metadata stays in the dataset hash without being fed into
            # an OHLC replay.
        except (TypeError, ValueError) as exc:
            raise DiscoveryError(f"row {number}: {exc}") from exc
    if quote_index is None:
        quotes.sort(key=lambda item: (item.symbol, item.timestamp))
    else:
        quote_index.finalize()
    return bars, snapshots, quotes


def _bar_session(bar: UnderlyingBar) -> str:
    """The New York session a bar belongs to, as the replay lanes group them."""
    return bar.timestamp.astimezone(ZoneInfo("America/New_York")).date().isoformat()


def _read_discovery_rows(data: str | Path | Sequence[Mapping], *,
                         force_quote_index: bool = False,
                         require_provenance: bool = False,
                         expected_equity_feed: str = "iex",
                         expected_provider: str | None = None) -> tuple[
        list[dict], list[UnderlyingBar], dict[str, OptionSnapshot], list[QuoteSnapshot]]:
    """Load one normalized JSONL corpus: bars, option quotes, equity quotes.

    ``data`` is a JSONL file, a directory of session partitions, or an
    in-memory sequence. Files are streamed line by line; what the replay then
    computes is identical either way.
    """
    if isinstance(data, (str, Path)):
        source = Path(data)
        size = _corpus_size(source)
        if size < QUOTE_INDEX_MIN_BYTES and not force_quote_index:
            # Preserve the small-corpus/list contract used by notebooks and
            # callers that keep a temporary source only for the duration of
            # this call.  Large production corpora take the streaming branch
            # below and never retain raw rows in memory.
            raw_rows = list(_stream_rows(source))
            if any(not isinstance(row, Mapping) for row in raw_rows):
                raise DiscoveryError("discovery rows must be JSON objects")
            bars, snapshots, quotes = _normalize_corpus(
                raw_rows, require_provenance=require_provenance,
                expected_equity_feed=expected_equity_feed,
                expected_provider=expected_provider)
            if not bars:
                raise DiscoveryError("discovery corpus contains no underlying bars")
            return raw_rows, bars, snapshots, quotes
        hasher = hashlib.sha256()
        hasher.update(b"[")
        first = True
        count = 0

        def digest_rows():
            nonlocal first, count
            for row in _stream_rows(source):
                if not first:
                    hasher.update(b",")
                hasher.update(_canonical_json(row).encode("utf-8"))
                first = False
                count += 1
                yield row

        hasher_source = digest_rows()
        quote_index = SQLiteQuoteIndex()
        try:
            bars, snapshots, quotes = _normalize_corpus(
                hasher_source, quote_index=quote_index,
                require_provenance=require_provenance,
                expected_equity_feed=expected_equity_feed,
                expected_provider=expected_provider)
        except Exception:
            if quote_index is not None:
                quote_index.close()
            raise
        hasher.update(b"]")
        raw_rows = _StreamingRawRows(source, count, hasher.hexdigest())
        if not bars:
            quote_index.close()
            raise DiscoveryError("discovery corpus contains no underlying bars")
        return raw_rows, bars, snapshots, quotes
    else:
        raw_rows = [dict(row) for row in data]
    if any(not isinstance(row, Mapping) for row in raw_rows):
        raise DiscoveryError("discovery rows must be JSON objects")
    if not isinstance(data, (str, Path)):
        if force_quote_index:
            quote_index = SQLiteQuoteIndex()
            try:
                bars, snapshots, quotes = _normalize_corpus(
                    raw_rows, quote_index=quote_index,
                    require_provenance=require_provenance,
                    expected_equity_feed=expected_equity_feed,
                    expected_provider=expected_provider)
            except Exception:
                quote_index.close()
                raise
        else:
            bars, snapshots, quotes = _normalize_corpus(
                raw_rows, require_provenance=require_provenance,
                expected_equity_feed=expected_equity_feed,
                expected_provider=expected_provider)
    if not bars:
        raise DiscoveryError("discovery corpus contains no underlying bars")
    return raw_rows, bars, snapshots, quotes


def validate_worker_projection(source: str | Path, *,
                               bars: Sequence[UnderlyingBar],
                               snapshots: Mapping[str, OptionSnapshot],
                               expected_equity_feed: str = "iex") -> str:
    """Verify that a compact worker view is exactly the full replay projection.

    The view is an optimization, not a second research dataset.  Validate it
    once in the parent before scheduling workers so an omitted or altered bar
    or option snapshot can never change gates while retaining the full
    corpus's experiment identity.
    """
    allowed = {
        "bar", "underlying", "underlying_bar",
        "option", "option_snapshot", "option_quote",
    }
    hasher = hashlib.sha256()
    hasher.update(b"[")
    first = True

    def projection_rows():
        nonlocal first
        for number, row in enumerate(_stream_rows(Path(source)), 1):
            if not isinstance(row, Mapping):
                raise DiscoveryError("worker_data rows must be JSON objects")
            kind = str(row.get("kind", "bar")).lower()
            if kind not in allowed:
                raise DiscoveryError(
                    f"worker_data row {number} must contain only bars and option snapshots")
            if not first:
                hasher.update(b",")
            hasher.update(_canonical_json(row).encode("utf-8"))
            first = False
            yield row

    projected_bars, projected_snapshots, projected_quotes = _normalize_corpus(
        projection_rows(), require_provenance=True,
        expected_equity_feed=expected_equity_feed)
    if projected_quotes:
        raise DiscoveryError("worker_data must not contain equity quotes")
    if (projected_bars != list(bars) or
            projected_snapshots != dict(snapshots)):
        raise DiscoveryError(
            "worker_data does not match the full corpus replay projection")
    hasher.update(b"]")
    return hasher.hexdigest()


def corpus_slice(source: str | Path, *, after: str | None = None,
                 until: str | None = None,
                 exclude: Sequence[str] = (),
                 quote_descriptor: SQLiteQuoteIndexDescriptor | None = None,
                 include_quotes: bool = True,
                 expected_digest: str | None = None,
                 expected_equity_feed: str = "iex") -> tuple[
                     list[UnderlyingBar], list[OptionSnapshot], list[QuoteSnapshot]]:
    """Re-read one recorded corpus and keep only a session window of it.

    This is the worker-side half of "ship a descriptor, not a corpus": the
    three predicates are exactly the ones an orchestrator would have applied
    to its own in-memory books, so the records — and therefore every trade,
    statistic and content hash computed from them — are identical.  ``until``
    pins the window against an append-only recorder growing underneath a
    running cycle.  The raw rows are never accumulated.
    """
    dropped = {str(value) for value in exclude}

    def keep(session: str) -> bool:
        return ((after is None or session > str(after)) and
                (until is None or session <= str(until)) and
                session not in dropped)

    source_path = Path(source)
    supplied = quote_descriptor if include_quotes else None
    owns_quote_index = False
    if supplied is not None:
        quote_index = SQLiteQuoteIndex.open_read_only(supplied)
        owns_quote_index = True
    else:
        quote_index = (SQLiteQuoteIndex() if _corpus_size(source_path) >=
                       QUOTE_INDEX_MIN_BYTES else None)
        owns_quote_index = quote_index is not None
    digest = hashlib.sha256() if expected_digest is not None else None

    def rows():
        first = True
        if digest is not None:
            digest.update(b"[")
        for row in _stream_rows(source_path):
            if digest is not None:
                if not first:
                    digest.update(b",")
                digest.update(_canonical_json(row).encode("utf-8"))
                first = False
            yield row
        if digest is not None:
            digest.update(b"]")

    try:
        bars, snapshots, quotes = _normalize_corpus(
            rows(), keep=keep, quote_index=quote_index,
            include_quotes=include_quotes,
            require_provenance=True,
            expected_equity_feed=expected_equity_feed)
        if (digest is not None and
                digest.hexdigest() != str(expected_digest)):
            raise DiscoveryError("worker_data changed after parent validation")
    except Exception:
        if owns_quote_index and quote_index is not None:
            quote_index.close()
        raise
    return bars, list(snapshots.values()), quotes


def _effective_ibr_config(base: Mapping | None, overrides: Mapping,
                          *, vehicle: str = "equity",
                          close_confirmed: bool = True,
                          policy: ReplayPolicy | None = None) -> tuple[IBRConfig, dict]:
    """Build the replay config used by every variant from one immutable base."""
    source = dict(base or {})
    strategy = dict(source.get("strategy") or {})
    # The runtime contract's defaults are explicit here rather than inherited
    # from a notebook's replay defaults.
    strategy.setdefault("range_minutes", 15)
    strategy.setdefault("breakout_buffer_bps", 5.0)
    strategy.setdefault("target_r", 2.0)
    strategy.setdefault("range_stop", True)
    strategy.setdefault("stop_pct", .003)
    strategy.setdefault("target_pct", .006)
    for path, value in overrides.items():
        parts = str(path).split(".")
        if parts and parts[0] == "strategy":
            parts = parts[1:]
        node = strategy
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        if parts:
            node[parts[-1]] = value
    # One cost model for every lane.  `execution.max_slippage_bps` is a
    # rejection cap, not an expectation: it bounds the model rather than
    # supplying it.  The vehicle is part of the replay boundary, so an
    # option-only schedule can never silently fall back to the flat model.
    costs = CostModel.from_config(source, vehicle=vehicle)
    cfg = IBRConfig(
        range_minutes=int(strategy.get("range_minutes", 15)),
        stop_pct=float(strategy.get("stop_pct", .003)),
        target_pct=float(strategy.get("target_pct", .006)),
        target_r=float(strategy["target_r"]) if strategy.get("target_r") is not None else None,
        range_stop=bool(strategy.get("range_stop", True)),
        breakout_buffer_bps=float(strategy.get("breakout_buffer_bps", 5.0)),
        costs=costs,
        close_confirmed=bool(close_confirmed),
        timezone=str((source.get("session") or {}).get("timezone", "America/New_York")),
        policy=(ReplayPolicy.from_config(source) if policy is None else policy),
    )
    effective = dict(source)
    effective["strategy"] = strategy
    effective["replay"] = {"close_confirmed": cfg.close_confirmed,
                            "range_stop": cfg.range_stop}
    # Persist the selected model at the top level (the effective replay
    # economics) while retaining the complete normalized schedule.  The
    # latter belongs to candidate identity: changing an option-only override
    # must invalidate option proofs even when an equity replay is unchanged.
    effective_costs = costs.as_dict()
    raw_costs = source.get("costs")
    if isinstance(raw_costs, Mapping) and isinstance(raw_costs.get("vehicles"), Mapping):
        fields = ("spread_bps", "slippage_bps", "fee_bps",
                  "option_fee_per_contract_side", "provenance")
        effective_costs = {key: effective_costs[key]
                           for key in fields if key in effective_costs}
        schedule: dict[str, dict] = {}
        for name in sorted(raw_costs["vehicles"], key=str):
            scheduled = CostModel.from_config(source, vehicle=str(name))
            schedule[str(name)] = {
                key: getattr(scheduled, key)
                for key in fields if hasattr(scheduled, key)
            }
        effective_costs["vehicles"] = schedule
    effective["costs"] = effective_costs
    return cfg, effective


def _opportunity_rows(result, bars: Sequence[UnderlyingBar], vehicle: str) -> list[dict]:
    """Materialize one row per symbol/session, including no-trade zeros."""
    zone = ZoneInfo("America/New_York")
    sessions = sorted({(bar.symbol, bar.timestamp.astimezone(zone).date()) for bar in bars})
    by_session = {(trade.symbol, trade.session_date): trade for trade in result.trades}
    # The replay records why it declined each session.  Carrying that onto the
    # no-trade row is what lets the gates tell a corpus they could not price
    # from one that simply held no edge.
    refused = {(refusal.symbol, refusal.session_date): refusal
               for refusal in getattr(result, "refusals", ())}
    rows: list[dict] = []
    for symbol, day in sessions:
        opportunity_id = f"ibr:{vehicle}:{symbol}:{day.isoformat()}"
        trade = by_session.get((symbol, day))
        if trade is None:
            row = {"vehicle": vehicle, "symbol": symbol,
                   "session_date": day.isoformat(),
                   "opportunity_id": opportunity_id, "net_pnl": 0.0,
                   "return_value": 0.0, "no_trade": True}
            refusal = refused.get((symbol, day))
            if refusal is not None:
                row["reject_reason"] = refusal.reason
                if refusal.detail:
                    row["reject_detail"] = dict(refusal.detail)
            rows.append(row)
            continue
        row = {key: value for key, value in vars(trade).items()}
        row.update({"session_date": trade.session_date.isoformat(),
                    "opportunity_id": opportunity_id, "no_trade": False,
                    "return_value": float(trade.net_pnl)})
        for key, value in list(row.items()):
            if isinstance(value, (datetime, date)):
                row[key] = value.isoformat()
        rows.append(row)
    return rows


def _null_row(symbol: str, day: str, opportunity: str, vehicle: str,
              reason: str | None = None,
              telemetry: Mapping[str, Any] | None = None) -> dict:
    row = {"vehicle": vehicle, "symbol": symbol, "session_date": day,
           "opportunity_id": opportunity, "net_pnl": 0.0, "return_value": 0.0,
           "no_trade": True}
    if reason:
        row["reject_reason"] = reason
    if telemetry:
        row.update(dict(telemetry))
    return row


def _rule_mature_prefix(spec: Mapping[str, Any], window: int | None) -> int:
    """Return the executable rule's causal prefix without inactive fields."""
    required = max(int(spec["lookback"]) + 1,
                   int(spec["atr_period"]) + 1,
                   int(window or 0))
    confirmations = {str(spec.get("confirmation") or "none")}
    confirmations.update(str(item) for item in spec.get("confirmations") or ())
    if "trend" in confirmations:
        required = max(required, int(spec["slow_lookback"]))
    if "volume" in confirmations:
        required = max(required, int(spec["lookback"]) + 1)
    if "volatility" in confirmations:
        required = max(required, int(spec["atr_period"]) + 1)
    return required


def _null_admissible_entry_indices(session_bars: Sequence[Any], spec: Mapping,
                                   *, direction: str, policy: ReplayPolicy,
                                   vehicle: str, snapshots: Sequence[Any],
                                   quote_index: Any) -> list[tuple[int, datetime]]:
    """Return entries the candidate could actually have admitted.

    Sampling a raw bar index gives the null access to bars before the rule's
    mature prefix, outside its entry clock, across feature gaps, or after a
    force-flat boundary.  Those bars are impossible candidate opportunities
    and make the null artificially weak.  This helper mirrors the executable
    rule eligibility checks and is deliberately shared only by the
    authorizing null path, not by fit diagnostics.
    """
    if not session_bars:
        return []
    # ``feature_window_bars`` is the executable rule's dependency declaration.
    # It activates ``slow_lookback`` only for families/confirmations that read
    # it.  Do not make every normalized spec wait for that field, and do not
    # silently fall back to a shorter prefix if a declared dependency cannot
    # be resolved: an underpowered corpus has no admissible controls.
    declared_rule = all(name in spec for name in ("family", "lookback",
                                                  "atr_period"))
    if declared_rule:
        try:
            window = _simulation_dependency("feature_window_bars")(spec)
            minimum_prefix = _rule_mature_prefix(spec, window)
        except (KeyError, TypeError, ValueError):
            return []
    else:
        # Opening-range nulls use a compact spec by design; no rule feature
        # dependency was declared.  The preceding bar is the causal clock
        # anchor and the following bar is the entry; exit eligibility below
        # separately requires a later observable mark.  Starting at one (not
        # two) therefore admits the smallest valid three-bar fixture without
        # inventing a feature maturity requirement.
        window = None
        minimum_prefix = 1
    contiguous = _simulation_dependency("_contiguous")
    available = _simulation_dependency("_available")
    entries: list[tuple[int, datetime]] = []
    for signal_index in range(0, max(0, len(session_bars) - 1)):
        if signal_index + 1 < minimum_prefix:
            continue
        feature_start = (0 if window is None else
                         max(0, signal_index + 1 - int(window)))
        if not contiguous(session_bars, feature_start, signal_index + 1):
            continue
        signal_bar = session_bars[signal_index]
        entry_bar = session_bars[signal_index + 1]
        available_times = [available(item, policy) for item in
                           session_bars[feature_start:signal_index + 1]]
        available_times = [item for item in available_times if item is not None]
        signal_ready = max([signal_bar.end, *available_times], default=None)
        if signal_ready is None:
            continue
        entry_at = signal_bar.end if signal_ready <= signal_bar.end else signal_ready
        if entry_bar.timestamp != signal_bar.end and signal_ready <= signal_bar.end:
            continue
        entry_index = next((probe for probe in range(signal_index + 1,
                                                      len(session_bars))
                            if session_bars[probe].timestamp >= entry_at), None)
        if entry_index is None:
            continue
        entry_bar = session_bars[entry_index]
        local = entry_at.astimezone(ZoneInfo("America/New_York"))
        if (policy.latest_entry_time is not None and
                local.time() > policy.latest_entry_time):
            continue
        if (policy.force_flat_time is not None and
                local.time() >= policy.force_flat_time):
            continue
        if vehicle == "equity":
            side = "buy" if direction == "long" else "sell"
            quoted = quote_fill_record(
                quote_index, symbol=entry_bar.symbol, at=entry_at, side=side,
                max_age_seconds=policy.max_market_data_age_seconds,
                session_date=entry_bar.session_date)
            if quoted is None and policy.strict_market_data:
                continue
            if quoted is None and replay_open_is_available(
                    entry_bar, entry_bar.timestamp,
                    allow_historical_backfill_diagnostics=(
                        policy.allow_historical_backfill_diagnostics)) is False:
                continue
        elif vehicle == "option":
            if _option_at(snapshots, symbol=entry_bar.symbol,
                          day=entry_bar.session_date, direction=direction,
                          cutoff=entry_at, policy=policy) is None:
                continue
        # A null position must have at least one observable mark after entry;
        # otherwise the eventual exit would be right-censored rather than an
        # admissible candidate hold.
        deadline = hold_deadline(entry_at, spec)
        if not any(bar.timestamp > entry_at and
                   bar.end.timestamp() <= deadline and
                   replay_available_at(
                       bar,
                       allow_historical_backfill_diagnostics=(
                           policy.allow_historical_backfill_diagnostics))
                   is not None for bar in session_bars[entry_index + 1:]):
            continue
        entries.append((entry_index, entry_at))
    return entries


def null_control_account(bars: Sequence[Any], snapshots: Sequence[Any],
                         spec: Mapping[str, Any], *, vehicle: str,
                         reference_rows: Sequence[Mapping], account_id: str,
                         starting_cash: float = 100_000.0, risk_pct: float = .5,
                         costs: CostModel | None = None,
                         quotes: Sequence[Any] | None = None,
                         fixed_quantity: float | None = None,
                         policy: ReplayPolicy | Mapping | None = None) -> dict:
    """Replay a chance-entry null with the strategy's own exit and cost rules.

    The null keeps the candidate's session/symbol/direction distribution and
    its stop geometry, but chooses the entry bar at random.  A candidate that
    cannot beat this is timing nothing: comparing only against the parent
    specification measures relative improvement, not edge against chance.

    ``fixed_quantity`` replaces the isolated account's risk sizing for a lane
    whose own replay trades a fixed size.  A delta between books sized
    differently would measure position size, not timing.
    """
    # Preserve whether this is a compact IBR null specification.  Validation
    # fills rule defaults, which would otherwise make the helper invent a
    # momentum predicate that the IBR candidate never used.
    raw_spec = dict(spec)
    spec = validate_rule_spec(spec)
    unsupported_vehicle = not rule_vehicle_executable(spec, vehicle)
    model = costs or CostModel()
    # Omitted policy is the checked runtime policy.  Historical bar fallback
    # is available only through an explicit ReplayPolicy(strict_market_data=False).
    policy = (ReplayPolicy.from_config(policy) if isinstance(policy, Mapping)
              else (ReplayPolicy() if policy is None else policy))
    quote_index = index_quotes(quotes)
    grouped: dict[tuple[str, str], list] = {}
    for bar in sorted(bars, key=lambda item: (item.timestamp, item.symbol)):
        grouped.setdefault((bar.symbol, bar.session_date.isoformat()), []).append(bar)
    references = {(str(row.get("symbol")), str(row.get("session_date"))): row
                  for row in reference_rows}
    rng = random.Random(stable_seed({"account": str(account_id),
                                     "spec": dict(spec),
                                     "sessions": sorted(references)}))
    cash = float(starting_cash)
    peak = cash
    drawdown = 0.0
    rows: list[dict] = []
    for key in sorted(references):
        symbol, day = key
        opportunity = f"null:{account_id}:{symbol}:{day}"
        reference = references[key]
        session_bars = grouped.get(key, [])
        if unsupported_vehicle:
            rows.append(_null_row(
                symbol, day, opportunity, vehicle,
                "rule-strategy.v3 is not executable for options"))
            continue
        if reference.get("no_trade") is True or len(session_bars) < 3:
            rows.append(_null_row(symbol, day, opportunity, vehicle))
            continue
        try:
            entry_underlying_ref = float(reference["underlying_entry"])
            distance = float(reference.get(
                "stop_distance",
                abs(entry_underlying_ref - float(reference["stop_price"]))))
            direction = str(reference["direction"])
        except (KeyError, TypeError, ValueError):
            rows.append(_null_row(symbol, day, opportunity, vehicle,
                                  "reference trade lacks null-control geometry"))
            continue
        if not math.isfinite(distance) or distance <= 0 or direction not in {"long", "short"}:
            rows.append(_null_row(symbol, day, opportunity, vehicle,
                                  "reference trade lacks null-control geometry"))
            continue
        admissible = _null_admissible_entry_indices(
            session_bars, raw_spec, direction=direction, policy=policy,
            vehicle=vehicle, snapshots=snapshots, quote_index=quote_index)
        if not admissible:
            rows.append(_null_row(symbol, day, opportunity, vehicle,
                                  "no_admissible_null_entry"))
            continue
        entry_index, sampled_entry_at = rng.choice(admissible)
        source_entry_bar = session_bars[entry_index]
        source_ready = replay_available_at(
            source_entry_bar,
            allow_historical_backfill_diagnostics=(
                policy.allow_historical_backfill_diagnostics),
        )
        if source_ready is None:
            rows.append(_null_row(symbol, day, opportunity, vehicle,
                                  "entry_bar_not_visible"))
            continue
        entry_at = max(sampled_entry_at, source_entry_bar.timestamp, source_ready)
        entry_index = next((probe for probe in range(entry_index, len(session_bars))
                            if session_bars[probe].timestamp >= entry_at), None)
        if entry_index is None:
            rows.append(_null_row(symbol, day, opportunity, vehicle,
                                  "entry_bar_not_visible"))
            continue
        entry_bar = session_bars[entry_index]
        # A completed recorder bar is normally observed at its end.  A fresh
        # executable boundary quote can authorize a strict entry without
        # consuming that delayed OHLC; permissive bar fallback still requires
        # the opening record itself to be visible at its timestamp.
        entry_bar_visible = replay_open_is_available(
            entry_bar, entry_bar.timestamp,
            allow_historical_backfill_diagnostics=(
                policy.allow_historical_backfill_diagnostics),
        )
        entry_underlying = (float(entry_bar.open)
                            if entry_bar_visible else None)
        # Preserve the pre-quote reference.  An executable quote is the fill,
        # but it must be checked against the boundary reference before it is
        # allowed to replace that anchor.
        entry_reference = entry_underlying
        entry_ref = entry_underlying
        entry_source = BAR
        entry_feed = entry_provider = None
        entry_age = 0.0
        entry_snap = None
        if vehicle == "equity":
            side = "buy" if direction == "long" else "sell"
            quoted = quote_fill_record(
                quote_index, symbol=symbol, at=entry_at, side=side,
                max_age_seconds=policy.max_market_data_age_seconds,
                session_date=entry_bar.session_date)
            if quoted is not None:
                if entry_reference is not None:
                    slippage, slippage_reason = check_entry_slippage(
                        side, entry_reference, quoted.price,
                        model.max_slippage_bps)
                    if slippage_reason is not None:
                        rows.append(_null_row(
                            symbol, day, opportunity, vehicle,
                            slippage_reason, slippage))
                        continue
                entry_underlying = quoted.price
                entry_ref, entry_source = quoted.price, QUOTE
                entry_feed, entry_provider = quoted.feed, quoted.provider
                entry_age = max(0.0, (entry_at -
                                      quoted.timestamp).total_seconds())
            elif policy.strict_market_data:
                rows.append(_null_row(symbol, day, opportunity, vehicle,
                                      "no fresh equity quote at entry"))
                continue
            elif not entry_bar_visible:
                rows.append(_null_row(symbol, day, opportunity, vehicle,
                                      "entry_bar_not_visible"))
                continue
        elif vehicle == "option":
            entry_snap = _option_at(
                snapshots, symbol=symbol, day=entry_bar.session_date,
                direction=direction, cutoff=entry_at, policy=policy)
            if entry_snap is None:
                rows.append(_null_row(symbol, day, opportunity, vehicle))
                continue
            if entry_snap.underlying_price and entry_snap.underlying_price > 0:
                entry_underlying = float(entry_snap.underlying_price)
            elif entry_underlying is None:
                rows.append(_null_row(symbol, day, opportunity, vehicle,
                                      "entry_bar_not_visible"))
                continue
            entry_ref = entry_snap.ask
            entry_source = QUOTE
            entry_feed, entry_provider = (str(entry_snap.identity.feed),
                                          str(entry_snap.identity.provider))
            entry_age = max(0.0, (entry_at -
                                  entry_snap.timestamp).total_seconds())
        if entry_underlying is None:
            rows.append(_null_row(symbol, day, opportunity, vehicle,
                                  "entry_bar_not_visible"))
            continue
        if (policy.latest_entry_time is not None and
                entry_at.astimezone(ZoneInfo("America/New_York")).time() >
                policy.latest_entry_time):
            rows.append(_null_row(symbol, day, opportunity, vehicle,
                                  "latest entry boundary"))
            continue
        authored_distance = distance
        stop_floor_bps = MIN_STOP_DISTANCE_BPS
        stop_floor_binding = False
        stress_scenario, stress_activation_reason = (
            policy.resolve_stress_scenario(
                symbol, entry_at, vehicle=vehicle))
        if (vehicle == "equity" and stress_scenario is not None and
                policy.max_stressed_cost_to_risk_ratio is not None):
            try:
                distance, stop_floor_bps = effective_stop_distance(
                    entry_underlying, authored_distance,
                    base_floor_bps=MIN_STOP_DISTANCE_BPS,
                    scenario_bps=stress_scenario,
                    max_cost_to_risk_ratio=(
                        policy.max_stressed_cost_to_risk_ratio),
                    minimum_increment=equity_price_increment(entry_underlying))
            except RiskGeometryError:
                rows.append(_null_row(symbol, day, opportunity, vehicle,
                                      "stressed_cost_invalid"))
                continue
            stop_floor_binding = distance > authored_distance + 1e-12
        stop = (entry_underlying - distance if direction == "long" else
                entry_underlying + distance)
        target_mode = str(spec.get("target_mode") or "fixed_r")
        target_reference = None
        if target_mode == "fixed_r":
            target = (entry_underlying + distance * float(spec["target_r"])
                      if direction == "long" else
                      entry_underlying - distance * float(spec["target_r"]))
        else:
            target_reference = frozen_target_reference(
                session_bars[:entry_index], spec)
            target = target_reference
            if (target is None or
                    (direction == "long" and target <= entry_underlying) or
                    (direction == "short" and target >= entry_underlying)):
                rows.append(_null_row(
                    symbol, day, opportunity, vehicle,
                    "target_reference_unavailable_or_wrong_side"))
                continue
        if (not math.isfinite(stop) or stop <= 0 or
                not math.isfinite(float(target)) or float(target) <= 0 or
                (direction == "long" and not (stop < entry_underlying < target)) or
                (direction == "short" and not (target < entry_underlying < stop))):
            rows.append(_null_row(symbol, day, opportunity, vehicle,
                                  "stressed_cost_invalid"))
            continue
        if vehicle == "equity":
            try:
                stop, target, distance = quantize_equity_bracket(
                    entry_underlying, stop, target, direction)
            except RiskGeometryError:
                rows.append(_null_row(symbol, day, opportunity, vehicle,
                                      "broker_tick_geometry_invalid"))
                continue
        deadline = hold_deadline(entry_at, spec)
        thesis_deadline = thesis_exit_deadline(entry_at, spec)
        if policy.force_flat_time is not None:
            force_flat = entry_at.astimezone(
                ZoneInfo("America/New_York")).replace(
                    hour=policy.force_flat_time.hour,
                    minute=policy.force_flat_time.minute,
                    second=policy.force_flat_time.second,
                    microsecond=policy.force_flat_time.microsecond)
            deadline = min(deadline, force_flat.timestamp())
        last_index = entry_index
        for probe in range(entry_index + 1, len(session_bars)):
            if session_bars[probe].end.timestamp() > deadline:
                break
            last_index = probe
        exit_bar = session_bars[last_index]
        # If the randomized position has no later bar, its close would be the
        # first completed-bar OHLC consumed as an exit mark.  A delayed entry
        # record is not available even at that close boundary, so do not leak
        # its eventual close into the null account.
        if (exit_bar is entry_bar and
                not replay_record_is_available(
                    entry_bar, entry_bar.end,
                    allow_historical_backfill_diagnostics=(
                        policy.allow_historical_backfill_diagnostics))):
            rows.append(_null_row(symbol, day, opportunity, vehicle,
                                  "entry_bar_not_visible"))
            continue
        exit_ref = float(exit_bar.close)
        exit_at = exit_bar.end
        boundary_exit = True
        exit_reason = ("exit_before" if thesis_deadline is not None and
                       abs(float(deadline) - float(thesis_deadline)) <= 1e-9 and
                       abs(exit_at.timestamp() - float(deadline)) <= 1e-9
                       else "time")
        tie_broken = False
        exit_state = initialize_exit_state(
            direction, entry_underlying, stop, target,
            breakeven_r=spec.get("breakeven_r"),
            trailing_stop_r=spec.get("trailing_stop_r"),
            target_mode=target_mode,
            target_lookback=spec.get("target_lookback"),
            exit_before_ts=thesis_deadline)
        for bar in session_bars[entry_index:last_index + 1]:
            if replay_available_at(
                    bar,
                    allow_historical_backfill_diagnostics=(
                        policy.allow_historical_backfill_diagnostics)) is None:
                # A resting null exit may consume a delayed completed bar once
                # its full record is observed; only bars before entry are
                # excluded by the availability-time entry resolver.
                continue
            transition = completed_bar_exit_transition(exit_state, bar)
            exit_state = transition["state"]
            resolved = transition["exit"]
            if resolved is None:
                continue
            exit_ref = float(resolved["price"])
            exit_reason = str(resolved["reason"])
            tie_broken = bool(resolved.get("tie_broken"))
            exit_bar = bar
            boundary_exit = bool(resolved.get("gapped"))
            exit_at = bar.timestamp if boundary_exit else bar.end
            break
        exit_source = BAR
        exit_fill_schema = None
        exit_fill_claim = None
        exit_feed = exit_provider = None
        exit_age = 0.0
        multiplier = 1
        risk_per_unit = distance
        if vehicle == "equity":
            # Intrabar exits have no observable trigger instant.  Use the
            # exiting bar's opening quote (the last point-in-time observation
            # available before its high/low is inspected), rather than
            # manufacturing a bar fallback or looking past the trigger.
            exit_quote_at = exit_at if boundary_exit else exit_bar.timestamp
            quoted_exit = quote_fill_record(
                quote_index, symbol=symbol, at=exit_quote_at,
                side="sell" if direction == "long" else "buy",
                max_age_seconds=policy.max_market_data_age_seconds,
                session_date=entry_bar.session_date)
            # A non-gap level trigger is the broker-resident bracket fill; a
            # quote at the bar boundary cannot identify its unknown trigger
            # instant and must never replace the planned resting level.
            if (not boundary_exit and exit_reason in {"stop", "target"}):
                exit_source = RESTING_BRACKET
                exit_fill_schema = RESTING_BRACKET_FILL_SCHEMA
                active_stop = float(exit_state.get("active_stop_price", stop))
                exit_fill_claim = resting_bracket_fill_claim(
                    exit_reason=exit_reason, exit_reference=exit_ref,
                    stop_price=active_stop, target_price=target,
                    bar_timestamp=exit_bar.timestamp.isoformat(),
                    bar_feed=exit_bar.feed, bar_provider=exit_bar.provider,
                    tie_broken=tie_broken)
            elif quoted_exit is not None:
                exit_ref, exit_source = quoted_exit.price, QUOTE
                exit_feed, exit_provider = quoted_exit.feed, quoted_exit.provider
                exit_age = max(0.0, (exit_quote_at -
                                    quoted_exit.timestamp).total_seconds())
            elif policy.strict_market_data and (boundary_exit or
                                                exit_reason in {
                                                    "time", "exit_before"}):
                rows.append(_null_row(symbol, day, opportunity, vehicle,
                                      "no fresh equity quote at exit"))
                continue
        if vehicle == "option":
            assert entry_snap is not None
            exit_snap = _option_at(
                         snapshots, symbol=symbol, day=entry_bar.session_date,
                         direction=direction, cutoff=exit_at,
                         contract_symbol=entry_snap.contract.symbol,
                         policy=policy)
            if exit_snap is None:
                rows.append(_null_row(symbol, day, opportunity, vehicle))
                continue
            exit_ref = exit_snap.bid
            exit_feed = str(exit_snap.identity.feed)
            exit_provider = str(exit_snap.identity.provider)
            exit_age = max(0.0, (exit_at -
                                 exit_snap.timestamp).total_seconds())
            multiplier = entry_snap.contract.multiplier
            risk_per_unit = entry_ref * multiplier
        if fixed_quantity is not None:
            quantity = float(fixed_quantity)
        else:
            quantity = math.floor(max(0.0, cash * float(risk_pct) / 100.0) /
                                  max(float(risk_per_unit), 1e-9))
            if vehicle == "equity":
                # A chance entry derives its own stop from its own fill, so the
                # plan anchor and the fill are the same price here.
                quantity = min(quantity, math.floor(
                    max(0.0, cash * _simulation_dependency("NOTIONAL_CAP_PCT") / 100.0) /
                    max(float(entry_underlying), 1e-9)))
        if quantity <= 0:
            rows.append(_null_row(symbol, day, opportunity, vehicle,
                                  "isolated account risk budget cannot fund one unit"))
            continue
        stress_enabled = (
            policy.stressed_cost_scenario_bps is not None or
            policy.max_stressed_cost_to_risk_ratio is not None or
            policy.stressed_cost_calibration_enabled)
        nominal_risk_usd = quantity * float(risk_per_unit)
        entry_notional = ((float(entry_underlying) * quantity)
                          if vehicle == "equity" else
                          (float(entry_ref) * quantity * multiplier))
        stress_telemetry: dict[str, Any] = {}
        if stress_enabled:
            plan = {
                "execution_profile": "options" if vehicle == "option" else "shares",
                "contracts": quantity if vehicle == "option" else None,
                "shares": quantity if vehicle == "equity" else None,
                "notional": entry_notional,
                "risk_usd": nominal_risk_usd,
            }
            checked, stress_reason = check_stressed_cost_plan(
                plan,
                scenario_bps=stress_scenario,
                max_ratio=policy.max_stressed_cost_to_risk_ratio,
                costs=model,
            )
            if stress_reason is not None:
                stress_telemetry = {
                    "stressed_cost_vehicle": vehicle,
                    "stressed_cost_schema": STRESSED_COST_SCHEMA,
                    "stressed_cost_basis": dict(STRESSED_COST_BASIS),
                    "stressed_cost_entry_notional": float(entry_notional),
                    "entry_notional": float(entry_notional),
                    "stressed_cost_scenario_bps": stress_scenario,
                    "stressed_cost_activation_reason": (
                        stress_activation_reason),
                    "max_stressed_cost_to_risk_ratio": (
                        policy.max_stressed_cost_to_risk_ratio),
                    "stressed_cost_risk_usd": float(nominal_risk_usd),
                    "risk_usd": float(nominal_risk_usd),
                }
                try:
                    if (stress_scenario is not None and
                            policy.max_stressed_cost_to_risk_ratio is not None and
                            nominal_risk_usd > 0 and entry_notional > 0):
                        stressed = stressed_cost_usd(
                            entry_notional=entry_notional,
                            scenario_bps=stress_scenario,
                            vehicle=vehicle, quantity=quantity, costs=model)
                        stress_telemetry.update({
                            "stressed_cost_usd": float(stressed),
                            "stressed_cost_to_risk_ratio": float(
                                stressed / nominal_risk_usd),
                        })
                except (CostError, TypeError, ValueError, OverflowError,
                        ZeroDivisionError):
                    pass
                rows.append(_null_row(symbol, day, opportunity, vehicle,
                                      stress_reason, stress_telemetry))
                continue
            if checked is not None:
                stress_telemetry = {
                    key: value for key, value in checked.items()
                    if key.startswith("stressed_cost_") or
                    key == "max_stressed_cost_to_risk_ratio"
                }
        execution_direction = "long" if vehicle == "option" else direction
        executable = vehicle == "option"
        entry = model.execution_price(
            entry_ref, execution_direction, entry=True,
            executable_quote=executable or entry_source == QUOTE)
        exit_price = model.execution_price(
            exit_ref, execution_direction, entry=False,
            executable_quote=executable or exit_source == QUOTE)
        gross = ((exit_price - entry) if execution_direction == "long" else
                 (entry - exit_price)) * quantity * multiplier
        fees = model.fees(entry, exit_price, quantity, multiplier, vehicle=vehicle)
        net = gross - fees
        before = cash
        cash += net
        peak = max(peak, cash)
        drawdown = max(drawdown, peak - cash)
        row = {"vehicle": vehicle, "symbol": symbol, "session_date": day,
                     "opportunity_id": opportunity, "direction": direction,
                     "entry_timestamp": entry_bar.timestamp.isoformat(),
                     "exit_timestamp": exit_bar.end.isoformat(),
                     "quantity": quantity, "entry_price": entry,
                     "exit_price": exit_price, "entry_reference": entry_ref,
                     "exit_reference": exit_ref,
                     "gross_pnl": gross, "costs": fees,
                     "net_pnl": net,
                     "return_value": net / before if before > 0 else 0.0,
                     "no_trade": False,
                     "exit_reason": exit_reason,
                     "tie_broken": tie_broken,
                     "gap_fill": bool(boundary_exit),
                     "entry_gap_fill": False,
                     "exit_gap_fill": bool(boundary_exit),
                     "stop_price": stop,
                     "stop_distance": distance,
                     "authored_stop_distance": authored_distance,
                     "authored_stop_distance_bps": (
                         authored_distance / entry_underlying * 10_000.0),
                     "effective_stop_floor_bps": stop_floor_bps,
                     "stress_floor_binding": stop_floor_binding,
                     "stop_geometry_scenario_bps": stress_scenario,
                     "stop_geometry_max_cost_to_risk_ratio": (
                         policy.max_stressed_cost_to_risk_ratio),
                     "stop_geometry_activation_reason": (
                         stress_activation_reason),
                     "active_stop_price": float(
                         exit_state.get("active_stop_price", stop)),
                     "target_price": target,
                     "target_mode": target_mode,
                     "target_reference": target_reference,
                     "target_lookback": spec.get("target_lookback"),
                     "trailing_stop_r": spec.get("trailing_stop_r"),
                     "exit_before_minutes": spec.get("exit_before_minutes"),
                     "exit_before_ts": thesis_deadline,
                     "rule_schema": spec.get("schema"),
                     "entry_fill_source": entry_source,
                     "exit_fill_source": exit_source,
                     "exit_fill_schema": exit_fill_schema,
                     "exit_fill_claim": exit_fill_claim,
                     "exit_fill_bar_timestamp": (
                         exit_bar.timestamp.isoformat()
                         if exit_source == RESTING_BRACKET else None),
                     "entry_quote_age_seconds": entry_age,
                     "exit_quote_age_seconds": exit_age,
                     "entry_feed": entry_feed,
                     "exit_feed": exit_feed,
                     "entry_provider": entry_provider,
                     "exit_provider": exit_provider,
                     "signal_bar_feed": source_entry_bar.feed,
                     "signal_bar_provider": source_entry_bar.provider,
                     "entry_bar_feed": entry_bar.feed,
                     "entry_bar_provider": entry_bar.provider,
                     "exit_bar_feed": exit_bar.feed,
                     "exit_bar_provider": exit_bar.provider,
                     "evidence_mode": (
                         "diagnostic_historical_backfill"
                         if any(
                             historical_backfill_record(item)
                             for item in (source_entry_bar, entry_bar, exit_bar))
                         else "forward_observed"
                     )}
        if stress_enabled:
            row.update({"quantity": quantity, "risk_usd": nominal_risk_usd,
                        "nominal_risk_usd": nominal_risk_usd,
                        "entry_notional": entry_notional})
            row.update(stress_telemetry)
        if spec.get("breakeven_r") is not None:
            row.update({
                "breakeven_r": spec["breakeven_r"],
                "initial_stop_price": exit_state["initial_stop_price"],
                "active_stop_price": exit_state["active_stop_price"],
                "breakeven_armed_at": exit_state.get("breakeven_armed_at"),
                "breakeven_armed_epoch": exit_state.get("breakeven_armed_epoch"),
            })
        if spec.get("schema") == RULE_SCHEMA_V4:
            row.update({
                "initial_stop_price": exit_state["initial_stop_price"],
                "active_stop_price": exit_state["active_stop_price"],
                "trailing_stop_r": exit_state.get("trailing_stop_r"),
                "target_mode": exit_state.get("target_mode", "fixed_r"),
                "target_lookback": exit_state.get("target_lookback"),
                "exit_before_ts": exit_state.get("exit_before_ts"),
            })
        rows.append(row)
    executed = [row for row in rows if row.get("no_trade") is not True]
    return {"account_id": account_id, "starting_cash": float(starting_cash),
            "ending_equity": cash, "realized_pnl": cash - float(starting_cash),
            "max_drawdown": drawdown, "trades": len(executed), "rows": rows}


def _null_reference_rows(result, bars: Sequence[UnderlyingBar], vehicle: str,
                         *, policy: ReplayPolicy | Mapping | None = None) -> list[dict]:
    """Describe each replayed opportunity to the randomized-entry null control.

    The null needs the underlying anchor the stop was measured from, which an
    option trade's ``entry_reference`` is not: that is the contract's ask.  The
    anchor is the entry bar's open, which is what the replay entered on, so it
    is read back from the bars rather than re-derived from the trade.
    """
    # Source provenance determines the evidence label; replay visibility is a
    # separate policy choice.  A strict replay therefore cannot become
    # historical-bar-visible merely because its source is labelled backfill.
    if isinstance(policy, Mapping):
        allow_backfill = bool(policy.get(
            "allow_historical_backfill_diagnostics", False))
    else:
        allow_backfill = bool(
            getattr(policy, "allow_historical_backfill_diagnostics", False))
    zone = ZoneInfo("America/New_York")
    bars_by_key = {(bar.symbol, bar.timestamp): bar for bar in bars}
    sessions = sorted({(bar.symbol, bar.timestamp.astimezone(zone).date()) for bar in bars})
    by_session = {(trade.symbol, trade.session_date): trade for trade in result.trades}
    rows: list[dict] = []
    for symbol, day in sessions:
        trade = by_session.get((symbol, day))
        entry_bar = None
        if trade is not None:
            entry_at = trade.entry_timestamp
            if not isinstance(entry_at, datetime):
                try:
                    entry_at = datetime.fromisoformat(str(entry_at))
                except (TypeError, ValueError):
                    entry_at = None
            entry_bar = (None if entry_at is None else
                         bars_by_key.get((trade.symbol, entry_at)))
            if entry_bar is None:
                # Delayed signal decisions can occur between bar boundaries;
                # the first full bar at/after the causal entry is the only
                # OHLC record eligible for bar fallback/exit provenance.
                candidates = [bar for (sym, _), bar in bars_by_key.items()
                              if sym == trade.symbol
                              and bar.session_date == trade.session_date
                              and entry_at is not None
                              and bar.timestamp >= entry_at]
                if candidates:
                    entry_bar = min(candidates, key=lambda bar: bar.timestamp)
        diagnostic = (trade is not None and getattr(
            trade, "evidence_mode", "") == "diagnostic_historical_backfill")
        bar_visible = (entry_bar is not None and
                       replay_open_is_available(
                           entry_bar, entry_bar.timestamp,
                           allow_historical_backfill_diagnostics=allow_backfill))
        # Strict replay may have priced a delayed bar from a fresh boundary
        # quote/snapshot.  Prefer the persisted underlying anchor for both
        # vehicles; the option ``entry_reference`` is its premium and cannot
        # serve as the null's underlying stop geometry.
        persisted_anchor = (None if trade is None else
                            getattr(trade, "underlying_entry", None))
        quote_anchor = (trade is not None and vehicle == "equity" and
                        str(getattr(trade, "entry_fill_source", "")) == QUOTE)
        executable = (trade is not None and
                      (bar_visible or quote_anchor or persisted_anchor is not None))
        row = {"vehicle": vehicle, "symbol": symbol, "session_date": day.isoformat(),
               "no_trade": not executable}
        if diagnostic:
            row["evidence_mode"] = "diagnostic_historical_backfill"
        anchor = None
        if executable:
            anchor = (float(persisted_anchor) if persisted_anchor is not None else
                      float(entry_bar.open) if bar_visible else
                      float(trade.entry_reference))
        if executable and anchor is not None:
            row.update({"direction": trade.direction, "underlying_entry": anchor,
                        "stop_price": float(trade.stop_price)})
        rows.append(row)
    return rows


def _discover_gate(candidate: Sequence[Mapping], baseline: Sequence[Mapping], *,
                   vehicle: str, min_trades: int, min_sessions: int,
                   alpha: float, shadow: bool = False,
                   actual_control: bool = True,
                   control_kind: str = "matched_actual_baseline",
                   null_rows: Sequence[Mapping] = (),
                   qualification: Mapping | None = None,
                   test_iterations: int = 20_000,
                   equity_feed: str = "iex") -> dict:
    """Evaluate one chronological backtest or a genuinely new shadow sample.

    A backtest is split into fit/held-out partitions.  A shadow evaluation is
    already supplied as a later corpus, so every row is held out and there is
    deliberately no in-sample fit partition to accidentally reuse for a
    lifecycle transition.
    """
    # Preserve every replay row for the envelope, but perform all authorizing
    # calculations on one strict, quality-audited projection.  Candidate,
    # baseline, and randomized null arms use the exact same boundary.
    candidate_raw = [dict(row) for row in candidate]
    baseline_raw = [dict(row) for row in baseline]
    null_raw = [dict(row) for row in null_rows]
    candidate_projection = authorization_projection(candidate_raw, vehicle=vehicle,
                                                    strict=True,
                                                    equity_feed=equity_feed)
    baseline_projection = authorization_projection(baseline_raw, vehicle=vehicle,
                                                   strict=True,
                                                   equity_feed=equity_feed)
    null_projection = authorization_projection(null_raw, vehicle=vehicle,
                                                strict=True,
                                                equity_feed=equity_feed)
    candidate = candidate_projection["eligible"]
    baseline = baseline_projection["eligible"]
    null_rows = null_projection["eligible"]
    ordered_raw = sorted(candidate_raw, key=lambda row: (
        str(row.get("session_date", "")), str(row.get("entry_timestamp", ""))))
    base_ordered_raw = sorted(baseline_raw, key=lambda row: (
        str(row.get("session_date", "")), str(row.get("entry_timestamp", ""))))
    ordered = sorted(candidate, key=lambda row: (str(row.get("session_date", "")),
                                                  str(row.get("entry_timestamp", ""))))
    base_ordered = sorted(baseline, key=lambda row: (str(row.get("session_date", "")),
                                                      str(row.get("entry_timestamp", ""))))
    if shadow:
        raw_fit, raw_heldout = [], ordered_raw
        raw_base_fit, raw_base_heldout = [], base_ordered_raw
        fit, heldout = [], ordered
        base_fit, base_heldout = [], base_ordered
    else:
        raw_fit, raw_heldout = chronological_split(ordered_raw, fit_fraction=.7)
        raw_fit_sessions = {str(row.get("session_date") or "") for row in raw_fit}
        raw_held_sessions = {str(row.get("session_date") or "") for row in raw_heldout}
        fit = [row for row in ordered
               if str(row.get("session_date") or "") in raw_fit_sessions]
        heldout = [row for row in ordered
                   if str(row.get("session_date") or "") in raw_held_sessions]
        raw_base_fit = [row for row in base_ordered_raw
                        if str(row.get("session_date") or "") in raw_fit_sessions]
        raw_base_heldout = [row for row in base_ordered_raw
                            if str(row.get("session_date") or "") in raw_held_sessions]
        fit_sessions = raw_fit_sessions
        held_sessions = raw_held_sessions
        base_fit = [row for row in base_ordered
                    if str(row.get("session_date") or "") in fit_sessions]
        base_heldout = [row for row in base_ordered
                       if str(row.get("session_date") or "") in held_sessions]
    fit_sessions = {str(row.get("session_date") or "") for row in raw_fit}
    held_sessions = {str(row.get("session_date") or "") for row in raw_heldout}
    fit_floor = structural_floor(
        fit, vehicle=vehicle, min_trades=min_trades, min_sessions=min_sessions,
        min_clusters=MIN_PROMOTION_CLUSTERS, required=not shadow,
        equity_feed=equity_feed)
    held_floor = structural_floor(
        heldout, vehicle=vehicle, min_trades=min_trades, min_sessions=min_sessions,
        min_clusters=MIN_PROMOTION_CLUSTERS, equity_feed=equity_feed)
    overall_floor = structural_floor(
        ordered, vehicle=vehicle, min_trades=min_trades, min_sessions=min_sessions,
        min_clusters=MIN_PROMOTION_CLUSTERS, equity_feed=equity_feed)
    separation = (heldout_separation(fit, heldout) if not shadow else
                  {"fit": 0, "heldout": len(heldout), "overlap_sessions": [],
                   "passes": bool(heldout), "mode": "new_data"})
    delta_all = paired_delta(
        ordered, baseline, vehicle=vehicle, equity_feed=equity_feed)
    delta_fit = (matched_cluster_test(
        fit, base_fit, vehicle=vehicle,
        min_matched=ACTUAL_CONTROL_MIN_MATCHED,
        min_coverage=ACTUAL_CONTROL_MIN_COVERAGE,
        equity_feed=equity_feed) if not shadow else
                 {"available": True, "actual_control": True, "matched": 0,
                  "mean_delta": None, "p_value": 1.0, "mode": "prior_backtest"})
    delta_held = matched_cluster_test(
        heldout, base_heldout, vehicle=vehicle, iterations=test_iterations,
        min_matched=ACTUAL_CONTROL_MIN_MATCHED,
        min_coverage=ACTUAL_CONTROL_MIN_COVERAGE,
        equity_feed=equity_feed)
    delta_fit["actual_control"] = bool(actual_control)
    delta_held["actual_control"] = bool(actual_control)
    fit_adequacy = paired_control_adequacy(
        fit, base_fit, vehicle=vehicle,
        min_matched=ACTUAL_CONTROL_MIN_MATCHED,
        min_coverage=ACTUAL_CONTROL_MIN_COVERAGE,
        equity_feed=equity_feed) if not shadow else {
            "matched": 0, "candidate_count": 0, "control_count": 0,
            "coverage": 0.0, "minimum_matched": ACTUAL_CONTROL_MIN_MATCHED,
            "minimum_coverage": ACTUAL_CONTROL_MIN_COVERAGE,
            "count_adequate": False, "coverage_adequate": False,
            "adequate": False,
        }
    held_adequacy = paired_control_adequacy(
        heldout, base_heldout, vehicle=vehicle,
        min_matched=ACTUAL_CONTROL_MIN_MATCHED,
        min_coverage=ACTUAL_CONTROL_MIN_COVERAGE,
        equity_feed=equity_feed)
    delta_fit["paired_adequacy"] = fit_adequacy
    delta_fit["adequate"] = bool(fit_adequacy["adequate"])
    delta_held["paired_adequacy"] = held_adequacy
    delta_held["adequate"] = bool(held_adequacy["adequate"])
    placebo = deterministic_placebo_deltas(
        heldout, base_heldout, vehicle=vehicle,
        equity_feed=equity_feed)
    candidate_p = float(delta_held.get("p_value", 1.0))
    independent_seed = stable_seed({
        "purpose": "independent_placebo_null_tail.v1",
        "primary_assignments_hash": placebo["assignments_hash"],
        "draws": int(placebo["draws"]),
    })
    if independent_seed == int(placebo["seed"]):
        independent_seed = stable_seed({
            "purpose": "independent_placebo_null_tail.v1.retry",
            "primary_assignments_hash": placebo["assignments_hash"],
            "draws": int(placebo["draws"]),
        })
    independent_placebo = deterministic_placebo_deltas(
        heldout, base_heldout, vehicle=vehicle,
        draws=int(placebo["draws"]), seed=independent_seed,
        equity_feed=equity_feed)
    independent_falsification = falsification_gate(
        independent_placebo["observed"], independent_placebo["placebo"],
        alpha=alpha)
    falsification = {
        **falsification_gate(
            placebo["observed"], placebo["placebo"], alpha=alpha,
            preregistered_p_value=candidate_p,
            independent_p_value=independent_falsification["p_value"],
            independent_method=FALSIFICATION_INDEPENDENT_METHOD,
            independent_result_hash=independent_placebo["assignments_hash"],
            require_independent=True),
        "method": placebo["method"],
        "assignments_hash": placebo["assignments_hash"],
        "observations": len(placebo["observed"]),
        # The draw count, seed and cluster count are what make the recorded
        # null reproducible from the stored deltas alone; the factory lane has
        # always persisted them and re-verification reads them.
        "draws": int(placebo["draws"]), "seed": int(placebo["seed"]),
        "clusters": int(placebo["cluster_count"]),
        "primary_p_value": candidate_p,
        "independent_method": FALSIFICATION_INDEPENDENT_METHOD,
        "independent_result_hash": independent_placebo["assignments_hash"],
        "independent_assignments_hash": independent_placebo["assignments_hash"],
        "independent_draws": int(independent_placebo["draws"]),
        "independent_seed": int(independent_placebo["seed"]),
    }
    # A randomized-entry null asks "did this beat chance", which the matched
    # baseline above cannot: the baseline only asks "did this beat the config
    # it was derived from".  An edge that is really directional drift beats
    # its baseline and loses to chance entries on its own sessions.
    heldout_sessions = {str(row.get("session_date") or "") for row in heldout}
    null_heldout = [row for row in null_rows
                    if str(row.get("session_date") or "") in heldout_sessions]
    null_test = matched_cluster_test(
        heldout, null_heldout, vehicle=vehicle, iterations=test_iterations,
        equity_feed=equity_feed)
    null_adequacy = paired_control_adequacy(
        heldout, null_heldout, vehicle=vehicle,
        # A caller may ask for a stricter local trade floor, but cannot reduce
        # the protocol's absolute randomized-null evidence requirement.
        min_matched=max(MIN_NULL_CONTROL_MATCHED, int(min_trades)),
        min_coverage=MIN_NULL_CONTROL_COVERAGE,
        equity_feed=equity_feed)
    null_control = {**null_test, "kind": "randomized_entry_null",
                    "available": bool(null_test["available"] and
                                      null_adequacy["adequate"]),
                    "raw_available": bool(null_test["available"]),
                    "adequate": bool(null_adequacy["adequate"]),
                    "paired_adequacy": null_adequacy,
                    "minimum_matched": null_adequacy["minimum_matched"],
                    "minimum_coverage": null_adequacy["minimum_coverage"],
                    "p_value": float(null_test["p_value"])}
    final = dict(qualification or {
        "available": False, "sessions": [], "net_positive": False,
        "delta_positive": False,
        "post_selection": {"preselected": False, "candidate_id": None},
    })
    checks = {
        "fit_structurally_adequate": bool(fit_floor["adequate"]),
        "heldout_structurally_adequate": bool(held_floor["adequate"]),
        "separated": bool(separation["passes"]),
        "actual_control_available": bool(delta_held.get("available") and
                                         delta_held.get("actual_control") and
                                         held_adequacy["adequate"]),
        "actual_control_adequate": bool(held_adequacy["adequate"]),
        "fit_delta_positive": bool(shadow or (
            fit_adequacy["adequate"] and
            delta_fit.get("mean_delta") is not None and
            float(delta_fit["mean_delta"]) > 0)),
        "heldout_delta_positive": bool(delta_held.get("mean_delta") is not None and
                                        held_adequacy["adequate"] and
                                        float(delta_held["mean_delta"]) > 0),
        "heldout_p_significant": candidate_p <= float(alpha),
        "falsification": bool(falsification["passes"]),
        "null_control_available": bool(null_control["available"]),
        "null_control_delta_positive": bool(
            null_control["available"] and null_control["mean_delta"] is not None and
            float(null_control["mean_delta"]) > 0 and
            float(null_control["p_value"]) <= float(alpha)),
        "qualification_net_positive": bool(final.get("available") and
                                           final.get("net_positive")),
        "qualification_delta_positive": bool(final.get("available") and
                                             final.get("delta_positive")),
    }
    development_checks = {
        name: value for name, value in checks.items()
        if name not in {"qualification_net_positive",
                        "qualification_delta_positive"}
    }
    development_passes = bool(
        all(development_checks.values()) and delta_all.get("mean_delta") is not None and
        float(delta_all["mean_delta"]) > 0)
    passes_without_family = bool(
        all(checks.values()) and delta_all.get("mean_delta") is not None and
        float(delta_all["mean_delta"]) > 0)
    fit_null_raw = [row for row in null_raw
                    if str(row.get("session_date") or "") in fit_sessions]
    heldout_null_raw = [row for row in null_raw
                        if str(row.get("session_date") or "") in heldout_sessions]
    fit_null_projection = (authorization_projection(
        fit_null_raw, vehicle=vehicle, strict=True, equity_feed=equity_feed)
        if fit_null_raw else {"eligible": [], "excluded": [], "reasons": {}})
    heldout_null_projection = (authorization_projection(
        heldout_null_raw, vehicle=vehicle, strict=True,
        equity_feed=equity_feed)
        if heldout_null_raw else {"eligible": [], "excluded": [], "reasons": {}})
    arm_diagnostics = {
        "fit": arm_evidence_report(
            candidate=raw_fit, baseline=raw_base_fit, null=fit_null_raw,
            vehicle=vehicle, equity_feed=equity_feed,
            projections={"candidate": authorization_projection(
                             raw_fit, vehicle=vehicle, strict=True,
                             equity_feed=equity_feed),
                         "baseline": authorization_projection(
                             raw_base_fit, vehicle=vehicle, strict=True,
                             equity_feed=equity_feed),
                         "null": fit_null_projection}),
        "heldout": arm_evidence_report(
            candidate=raw_heldout, baseline=raw_base_heldout,
            null=heldout_null_raw, vehicle=vehicle,
            equity_feed=equity_feed,
            projections={"candidate": authorization_projection(
                             raw_heldout, vehicle=vehicle, strict=True,
                             equity_feed=equity_feed),
                         "baseline": authorization_projection(
                             raw_base_heldout, vehicle=vehicle, strict=True,
                             equity_feed=equity_feed),
                         "null": heldout_null_projection}),
        "all": arm_evidence_report(
            candidate=ordered_raw, baseline=base_ordered_raw,
            null=null_raw, vehicle=vehicle, equity_feed=equity_feed,
            projections={"candidate": authorization_projection(
                             ordered_raw, vehicle=vehicle, strict=True,
                             equity_feed=equity_feed),
                         "baseline": authorization_projection(
                             base_ordered_raw, vehicle=vehicle, strict=True,
                             equity_feed=equity_feed),
                         "null": null_projection}),
    }
    return {"vehicle": vehicle, "shadow": shadow,
            "alpha": float(alpha),
            "passes_without_family": passes_without_family,
            "development_passes_without_family": development_passes,
            "candidate_p_raw": candidate_p,
            "floor": overall_floor, "fit_floor": fit_floor, "heldout_floor": held_floor,
            "heldout_separation": separation, "paired_baseline": delta_all,
            "fit_paired_baseline": delta_fit,
            "heldout_paired_baseline": delta_held,
            "control": {**delta_held, "kind": control_kind},
            "null_control": null_control, "qualification": final,
            "falsification": falsification,
            "checks_without_family": checks,
            "max_drawdown": max_drawdown_of(ordered),
            "fit_trades": sample_counts(
                fit, vehicle=vehicle, equity_feed=equity_feed)["trades"],
            "heldout_trades": sample_counts(
                heldout, vehicle=vehicle, equity_feed=equity_feed)["trades"],
            "fit_sessions": len({row.get("session_date") for row in fit}),
            "heldout_sessions": len({row.get("session_date") for row in heldout}),
            "_fit_rows": fit, "_heldout_rows": heldout,
            "_fit_baseline_rows": base_fit,
            "_heldout_baseline_rows": base_heldout,
            "_null_rows": null_heldout,
            # Raw rows are retained for diagnostics/proof payloads; the
            # underscored rows above are the only authorizing projection.
            "_fit_raw_rows": [dict(row) for row in raw_fit],
            "_heldout_raw_rows": [dict(row) for row in raw_heldout],
            "_fit_baseline_raw_rows": [dict(row) for row in raw_base_fit],
            "_heldout_baseline_raw_rows": [dict(row) for row in raw_base_heldout],
            "_null_raw_rows": [dict(row) for row in null_raw
                               if str(row.get("session_date") or "") in held_sessions],
            "authorization_projection": {
                "candidate": _projection_summary(candidate_projection),
                "baseline": _projection_summary(baseline_projection),
                "null": _projection_summary(null_projection),
            },
            "arm_diagnostics": arm_diagnostics,
            }


def _finalize_gate(gate: dict, *, lane: str, family: Mapping,
                   online_fdr: Mapping | None = None,
                   global_fdr: Mapping | None = None,
                   fdr_batch: Mapping | None = None,
                   provenance: Mapping | None = None,
                   candidate_id: str | None = None,
                   costs: CostModel | None = None,
                   equity_feed: str = "iex") -> dict:
    online = dict(online_fdr or {})
    global_data = dict(global_fdr or family)
    cumulative_passes = bool(
        online.get("decision") is True or
        (online.get("required") is False and
         online.get("status") == "deferred_to_live_shadow" and
         online.get("tested") is False))
    checks = {**gate["checks_without_family"],
              "family_fdr_significant": bool(family.get("significant", False)),
              "global_fdr_significant": bool(global_data.get("significant", False)),
              "cumulative_fdr_significant": cumulative_passes}
    passes = bool(gate["passes_without_family"] and all(checks.values()))
    gate["multiple_tests"] = {"candidate": dict(family),
                              "global": dict(global_data),
                              "method": "benjamini_yekutieli"}
    gate["passes"] = passes
    gate["cumulative_multiple_tests"] = online
    fit = gate.pop("_fit_rows")
    heldout = gate.pop("_heldout_rows")
    fit_baseline = gate.pop("_fit_baseline_rows", [])
    heldout_baseline = gate.pop("_heldout_baseline_rows", [])
    null_source = gate.pop("_null_rows", [])
    fit_raw = gate.pop("_fit_raw_rows", fit)
    heldout_raw = gate.pop("_heldout_raw_rows", heldout)
    fit_baseline_raw = gate.pop("_fit_baseline_raw_rows", fit_baseline)
    heldout_baseline_raw = gate.pop("_heldout_baseline_raw_rows", heldout_baseline)
    null_raw = gate.pop("_null_raw_rows", null_source)
    # Only report what this gate actually computed: an absent statistic must
    # stay absent rather than be persisted as a null the proof then has to
    # reproduce.
    absolute = gate.get("heldout_performance") or {}
    performance = {"heldout_delta": gate["heldout_paired_baseline"].get("mean_delta"),
                   "max_drawdown": gate["max_drawdown"]}
    if gate["heldout_paired_baseline"].get("mean_r_delta") is not None:
        performance["heldout_r_delta"] = gate["heldout_paired_baseline"].get(
            "mean_r_delta")
    if gate.get("heldout_delta_lcb") is not None:
        performance["heldout_delta_lcb"] = gate["heldout_delta_lcb"]
    for key in ("net_pnl", "expectancy"):
        if key in absolute:
            performance[f"heldout_{key}"] = absolute[key]
    envelope = verified_gate_envelope(
        lane=lane, vehicle=gate["vehicle"], fit=fit, heldout=heldout,
        fit_baseline=fit_baseline, heldout_baseline=heldout_baseline,
        null_source=null_source,
        fit_raw=fit_raw, heldout_raw=heldout_raw,
        fit_baseline_raw=fit_baseline_raw,
        heldout_baseline_raw=heldout_baseline_raw,
        null_raw=null_raw,
        fit_floor=gate["fit_floor"], heldout_floor=gate["heldout_floor"],
        fit_control=gate["fit_paired_baseline"], control=gate["control"],
        p_value=gate["candidate_p_raw"],
        q_value=float(global_data.get("p_adjusted",
                                     family.get("p_adjusted", 1.0))),
        family_q_value=float(family.get("p_adjusted", 1.0)),
        fdr_batch=fdr_batch,
        alpha=gate.get("alpha", 0.05),
        falsification=gate["falsification"], separation=gate["heldout_separation"],
        checks=checks, passes=passes,
        walk_forward=gate.get("walk_forward"),
        retirement=gate.get("retirement_evidence"),
        qualification=gate.get("qualification"),
        null_control=gate.get("null_control"),
        online_fdr=online, provenance=provenance,
        candidate_id=candidate_id, performance=performance, costs=costs,
        equity_feed=equity_feed)
    gate["passes"] = bool(envelope["passes"])
    gate["verified_gate"] = envelope
    gate["gate_hash"] = envelope["content_hash"]
    gate["failed_checks"] = sorted(
        key for key, value in envelope["checks"].items() if not value)
    return gate


__all__ = ["DiscoveryError", "corpus_partitions", "corpus_slice",
           "_read_discovery_rows", "_effective_ibr_config",
           "_opportunity_rows", "_null_reference_rows", "_discover_gate",
           "_finalize_gate", "authorization_projection", "arm_evidence_report",
           "MIN_PROMOTION_CLUSTERS"]
