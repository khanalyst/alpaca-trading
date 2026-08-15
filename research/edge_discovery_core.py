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

from agent.contracts.rule import hold_deadline
from .costs import (BAR, QUOTE, CostModel, ReplayPolicy, SQLiteQuoteIndex,
                    SQLiteQuoteIndexDescriptor, index_quotes, quote_fill)
from .market_data import OptionSnapshot, QuoteSnapshot, UnderlyingBar
from .stats import stable_seed


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


def deterministic_placebo_deltas(*args, **kwargs):
    return _facade_dependency("deterministic_placebo_deltas")(*args, **kwargs)


def falsification_gate(*args, **kwargs):
    return _facade_dependency("falsification_gate")(*args, **kwargs)


def max_drawdown_of(*args, **kwargs):
    return _facade_dependency("max_drawdown_of")(*args, **kwargs)


def sample_counts(*args, **kwargs):
    return _facade_dependency("sample_counts")(*args, **kwargs)


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


def _normalize_corpus(rows, *, keep=None, quote_index: SQLiteQuoteIndex | None = None) -> tuple[
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
        provider = str(row.get("provider") or "alpaca")
        feed = str(row.get("feed") or ("opra" if "option" in kind else "sip"))
        try:
            if kind in {"bar", "underlying", "underlying_bar"}:
                bar = normalize_underlying_bar(row, provider=provider, feed=feed)
                if keep is None or keep(_bar_session(bar)):
                    bars.append(bar)
            elif kind in {"option", "option_snapshot", "option_quote"}:
                contract = row.get("contract")
                if isinstance(contract, Mapping):
                    flattened = dict(contract)
                    flattened.update({key: value for key, value in row.items()
                                      if key != "contract"})
                    row = flattened
                snap = normalize_option_snapshot(row, provider=provider, feed=feed)
                if keep is None or keep(snap.session_date.isoformat()):
                    snapshots[f"{snap.timestamp.isoformat()}|{snap.contract.symbol}"] = snap
            elif kind in {"quote", "equity_quote", "underlying_quote"}:
                # An equity quote is the executable price at its instant.  It
                # is used only where a fill lands on that instant; everything
                # else still falls back to the bar and says so.
                quote = normalize_quote(row, provider=provider, feed=feed)
                if keep is None or keep(quote.session_date.isoformat()):
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
                         force_quote_index: bool = False) -> tuple[
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
            bars, snapshots, quotes = _normalize_corpus(raw_rows)
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
                hasher_source, quote_index=quote_index)
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
                    raw_rows, quote_index=quote_index)
            except Exception:
                quote_index.close()
                raise
        else:
            bars, snapshots, quotes = _normalize_corpus(raw_rows)
    if not bars:
        raise DiscoveryError("discovery corpus contains no underlying bars")
    return raw_rows, bars, snapshots, quotes


def validate_worker_projection(source: str | Path, *,
                               bars: Sequence[UnderlyingBar],
                               snapshots: Mapping[str, OptionSnapshot]) -> str:
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
        projection_rows())
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
                 expected_digest: str | None = None) -> tuple[
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
    supplied = quote_descriptor
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
            rows(), keep=keep, quote_index=quote_index)
        if (digest is not None and
                digest.hexdigest() != str(expected_digest)):
            raise DiscoveryError("worker_data changed after parent validation")
    except Exception:
        if owns_quote_index and quote_index is not None:
            quote_index.close()
        raise
    return bars, list(snapshots.values()), quotes


def _effective_ibr_config(base: Mapping | None, overrides: Mapping,
                          *, close_confirmed: bool = True,
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
    # supplying it.
    costs = CostModel.from_config(source)
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
    effective["costs"] = costs.as_dict()
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
              reason: str | None = None) -> dict:
    row = {"vehicle": vehicle, "symbol": symbol, "session_date": day,
           "opportunity_id": opportunity, "net_pnl": 0.0, "return_value": 0.0,
           "no_trade": True}
    if reason:
        row["reject_reason"] = reason
    return row


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
        if reference.get("no_trade") is True or len(session_bars) < 3:
            rows.append(_null_row(symbol, day, opportunity, vehicle))
            continue
        try:
            entry_underlying_ref = float(reference["underlying_entry"])
            distance = abs(entry_underlying_ref - float(reference["stop_price"]))
            direction = str(reference["direction"])
        except (KeyError, TypeError, ValueError):
            rows.append(_null_row(symbol, day, opportunity, vehicle,
                                  "reference trade lacks null-control geometry"))
            continue
        if not math.isfinite(distance) or distance <= 0 or direction not in {"long", "short"}:
            rows.append(_null_row(symbol, day, opportunity, vehicle,
                                  "reference trade lacks null-control geometry"))
            continue
        entry_index = rng.randrange(1, len(session_bars) - 1)
        entry_bar = session_bars[entry_index]
        # The entry bar's open is the boundary observation; its completed
        # record must be available by the bar end, just as the primary replay
        # lane models it.  Strict policy still requires a fresh executable
        # quote below, while the bar's later OHLC never prices the entry.
        if not _visible(entry_bar, entry_bar.end):
            rows.append(_null_row(symbol, day, opportunity, vehicle))
            continue
        if (policy.latest_entry_time is not None and
                entry_bar.timestamp.astimezone(ZoneInfo("America/New_York")).time() >
                policy.latest_entry_time):
            rows.append(_null_row(symbol, day, opportunity, vehicle,
                                  "latest entry boundary"))
            continue
        entry_underlying = float(entry_bar.open)
        stop = (entry_underlying - distance if direction == "long" else
                entry_underlying + distance)
        target = (entry_underlying + distance * float(spec["target_r"])
                  if direction == "long" else
                  entry_underlying - distance * float(spec["target_r"]))
        deadline = hold_deadline(entry_bar.timestamp, spec)
        if policy.force_flat_time is not None:
            force_flat = entry_bar.timestamp.astimezone(
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
        exit_ref = float(exit_bar.close)
        exit_at = exit_bar.end
        boundary_exit = True
        for bar in session_bars[entry_index + 1:last_index + 1]:
            if not _visible(bar, bar.end):
                continue
            if direction == "long":
                gap_stop, gap_target = bar.open <= stop, bar.open >= target
                hit_stop, hit_target = bar.low <= stop, bar.high >= target
            else:
                gap_stop, gap_target = bar.open >= stop, bar.open <= target
                hit_stop, hit_target = bar.high >= stop, bar.low <= target
            # The null shares the candidate's exit rules, gap-through included.
            # Giving chance entries an exit realism the candidate does not have
            # would bias every delta measured against them.
            if gap_stop or gap_target:
                exit_ref, exit_bar, exit_at = float(bar.open), bar, bar.timestamp
                break
            if hit_stop or hit_target:
                exit_ref = stop if hit_stop else target
                exit_bar, exit_at, boundary_exit = bar, bar.end, False
                break
        entry_ref = entry_underlying
        entry_source = exit_source = BAR
        multiplier = 1
        risk_per_unit = distance
        if vehicle == "equity":
            side = "buy" if direction == "long" else "sell"
            quoted = quote_fill(
                quote_index, symbol=symbol, at=entry_bar.timestamp, side=side,
                max_age_seconds=policy.max_market_data_age_seconds,
                session_date=entry_bar.session_date)
            if quoted is not None:
                entry_ref, entry_source = quoted, QUOTE
            elif policy.strict_market_data:
                rows.append(_null_row(symbol, day, opportunity, vehicle,
                                      "no fresh equity quote at entry"))
                continue
            quoted_exit = (quote_fill(
                           quote_index, symbol=symbol, at=exit_at,
                           side="sell" if direction == "long" else "buy",
                           max_age_seconds=policy.max_market_data_age_seconds,
                           session_date=entry_bar.session_date)
                       if boundary_exit else None)
            if quoted_exit is not None:
                exit_ref, exit_source = quoted_exit, QUOTE
            elif policy.strict_market_data and boundary_exit:
                rows.append(_null_row(symbol, day, opportunity, vehicle,
                                      "no fresh equity quote at exit"))
                continue
        if vehicle == "option":
            entry_snap = _option_at(snapshots, symbol=symbol, day=entry_bar.session_date,
                                    direction=direction, cutoff=entry_bar.timestamp,
                                    policy=policy)
            exit_snap = (None if entry_snap is None else
                         _option_at(snapshots, symbol=symbol, day=entry_bar.session_date,
                                    direction=direction, cutoff=exit_at,
                                    contract_symbol=entry_snap.contract.symbol,
                                    policy=policy))
            if entry_snap is None or exit_snap is None:
                rows.append(_null_row(symbol, day, opportunity, vehicle))
                continue
            entry_ref = entry_snap.ask
            exit_ref = exit_snap.bid
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
        rows.append({"vehicle": vehicle, "symbol": symbol, "session_date": day,
                     "opportunity_id": opportunity, "direction": direction,
                     "entry_timestamp": entry_bar.timestamp.isoformat(),
                     "exit_timestamp": exit_bar.end.isoformat(),
                     "quantity": quantity, "entry_price": entry,
                     "exit_price": exit_price, "gross_pnl": gross, "costs": fees,
                     "net_pnl": net,
                     "return_value": net / before if before > 0 else 0.0,
                     "no_trade": False})
    executed = [row for row in rows if row.get("no_trade") is not True]
    return {"account_id": account_id, "starting_cash": float(starting_cash),
            "ending_equity": cash, "realized_pnl": cash - float(starting_cash),
            "max_drawdown": drawdown, "trades": len(executed), "rows": rows}


def _null_reference_rows(result, bars: Sequence[UnderlyingBar], vehicle: str) -> list[dict]:
    """Describe each replayed opportunity to the randomized-entry null control.

    The null needs the underlying anchor the stop was measured from, which an
    option trade's ``entry_reference`` is not: that is the contract's ask.  The
    anchor is the entry bar's open, which is what the replay entered on, so it
    is read back from the bars rather than re-derived from the trade.
    """
    zone = ZoneInfo("America/New_York")
    opens = {(bar.symbol, bar.timestamp): float(bar.open) for bar in bars}
    sessions = sorted({(bar.symbol, bar.timestamp.astimezone(zone).date()) for bar in bars})
    by_session = {(trade.symbol, trade.session_date): trade for trade in result.trades}
    rows: list[dict] = []
    for symbol, day in sessions:
        trade = by_session.get((symbol, day))
        row = {"vehicle": vehicle, "symbol": symbol, "session_date": day.isoformat(),
               "no_trade": trade is None}
        anchor = None if trade is None else opens.get((trade.symbol, trade.entry_timestamp))
        if trade is not None and anchor is not None:
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
                   test_iterations: int = 20_000) -> dict:
    """Evaluate one chronological backtest or a genuinely new shadow sample.

    A backtest is split into fit/held-out partitions.  A shadow evaluation is
    already supplied as a later corpus, so every row is held out and there is
    deliberately no in-sample fit partition to accidentally reuse for a
    lifecycle transition.
    """
    ordered = sorted(candidate, key=lambda row: (str(row.get("session_date", "")),
                                                  str(row.get("entry_timestamp", ""))))
    base_ordered = sorted(baseline, key=lambda row: (str(row.get("session_date", "")),
                                                      str(row.get("entry_timestamp", ""))))
    if shadow:
        fit, heldout = [], ordered
        base_fit, base_heldout = [], base_ordered
    else:
        fit, heldout = chronological_split(ordered, fit_fraction=.7)
        fit_sessions = {str(row.get("session_date") or "") for row in fit}
        held_sessions = {str(row.get("session_date") or "") for row in heldout}
        base_fit = [row for row in base_ordered
                    if str(row.get("session_date") or "") in fit_sessions]
        base_heldout = [row for row in base_ordered
                       if str(row.get("session_date") or "") in held_sessions]
    fit_floor = structural_floor(
        fit, vehicle=vehicle, min_trades=min_trades, min_sessions=min_sessions,
        required=not shadow)
    held_floor = structural_floor(
        heldout, vehicle=vehicle, min_trades=min_trades, min_sessions=min_sessions)
    overall_floor = structural_floor(
        ordered, vehicle=vehicle, min_trades=min_trades, min_sessions=min_sessions)
    separation = (heldout_separation(fit, heldout) if not shadow else
                  {"fit": 0, "heldout": len(heldout), "overlap_sessions": [],
                   "passes": bool(heldout), "mode": "new_data"})
    delta_all = paired_delta(ordered, baseline, vehicle=vehicle)
    delta_fit = (matched_cluster_test(fit, base_fit, vehicle=vehicle) if not shadow else
                 {"available": True, "actual_control": True, "matched": 0,
                  "mean_delta": None, "p_value": 1.0, "mode": "prior_backtest"})
    delta_held = matched_cluster_test(
        heldout, base_heldout, vehicle=vehicle, iterations=test_iterations)
    delta_fit["actual_control"] = bool(actual_control)
    delta_held["actual_control"] = bool(actual_control)
    placebo = deterministic_placebo_deltas(
        heldout, base_heldout, vehicle=vehicle)
    falsification = {
        **falsification_gate(placebo["observed"], placebo["placebo"], alpha=alpha),
        "method": placebo["method"],
        "assignments_hash": placebo["assignments_hash"],
        "observations": len(placebo["observed"]),
        # The draw count, seed and cluster count are what make the recorded
        # null reproducible from the stored deltas alone; the factory lane has
        # always persisted them and re-verification reads them.
        "draws": int(placebo["draws"]), "seed": int(placebo["seed"]),
        "clusters": int(placebo["cluster_count"]),
    }
    # A randomized-entry null asks "did this beat chance", which the matched
    # baseline above cannot: the baseline only asks "did this beat the config
    # it was derived from".  An edge that is really directional drift beats
    # its baseline and loses to chance entries on its own sessions.
    heldout_sessions = {str(row.get("session_date") or "") for row in heldout}
    null_heldout = [row for row in null_rows
                    if str(row.get("session_date") or "") in heldout_sessions]
    null_test = matched_cluster_test(
        heldout, null_heldout, vehicle=vehicle, iterations=test_iterations)
    null_control = {**null_test, "kind": "randomized_entry_null",
                    "available": bool(null_test["available"]),
                    "p_value": float(null_test["p_value"])}
    final = dict(qualification or {
        "available": False, "sessions": [], "net_positive": False,
        "delta_positive": False,
        "post_selection": {"preselected": False, "candidate_id": None},
    })
    candidate_p = float(delta_held.get("p_value", 1.0))
    checks = {
        "fit_structurally_adequate": bool(fit_floor["adequate"]),
        "heldout_structurally_adequate": bool(held_floor["adequate"]),
        "separated": bool(separation["passes"]),
        "actual_control_available": bool(delta_held.get("available") and
                                         delta_held.get("actual_control")),
        "fit_delta_positive": bool(shadow or (
            delta_fit.get("mean_delta") is not None and float(delta_fit["mean_delta"]) > 0)),
        "heldout_delta_positive": bool(delta_held.get("mean_delta") is not None and
                                        float(delta_held["mean_delta"]) > 0),
        "heldout_p_significant": candidate_p <= float(alpha),
        "falsification": bool(falsification["passes"]),
        "null_control_available": bool(null_control["available"]),
        "null_control_delta_positive": bool(
            null_control["mean_delta"] is not None and
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
            "fit_trades": sample_counts(fit, vehicle=vehicle)["trades"],
            "heldout_trades": sample_counts(heldout, vehicle=vehicle)["trades"],
            "fit_sessions": len({row.get("session_date") for row in fit}),
            "heldout_sessions": len({row.get("session_date") for row in heldout}),
            "_fit_rows": fit, "_heldout_rows": heldout,
            "_fit_baseline_rows": base_fit,
            "_heldout_baseline_rows": base_heldout,
            "_null_rows": null_heldout}


def _finalize_gate(gate: dict, *, lane: str, family: Mapping,
                   online_fdr: Mapping | None = None,
                   global_fdr: Mapping | None = None,
                   provenance: Mapping | None = None,
                   candidate_id: str | None = None) -> dict:
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
                              "method": "benjamini_hochberg"}
    gate["passes"] = passes
    gate["cumulative_multiple_tests"] = online
    fit = gate.pop("_fit_rows")
    heldout = gate.pop("_heldout_rows")
    fit_baseline = gate.pop("_fit_baseline_rows", [])
    heldout_baseline = gate.pop("_heldout_baseline_rows", [])
    null_source = gate.pop("_null_rows", [])
    # Only report what this gate actually computed: an absent statistic must
    # stay absent rather than be persisted as a null the proof then has to
    # reproduce.
    absolute = gate.get("heldout_performance") or {}
    performance = {"heldout_delta": gate["heldout_paired_baseline"].get("mean_delta"),
                   "max_drawdown": gate["max_drawdown"]}
    if gate.get("heldout_delta_lcb") is not None:
        performance["heldout_delta_lcb"] = gate["heldout_delta_lcb"]
    for key in ("net_pnl", "expectancy"):
        if key in absolute:
            performance[f"heldout_{key}"] = absolute[key]
    envelope = verified_gate_envelope(
        lane=lane, vehicle=gate["vehicle"], fit=fit, heldout=heldout,
        fit_baseline=fit_baseline, heldout_baseline=heldout_baseline,
        null_source=null_source,
        fit_floor=gate["fit_floor"], heldout_floor=gate["heldout_floor"],
        fit_control=gate["fit_paired_baseline"], control=gate["control"],
        p_value=gate["candidate_p_raw"],
        q_value=float(global_data.get("p_adjusted",
                                     family.get("p_adjusted", 1.0))),
        family_q_value=float(family.get("p_adjusted", 1.0)),
        alpha=gate.get("alpha", 0.05),
        falsification=gate["falsification"], separation=gate["heldout_separation"],
        checks=checks, passes=passes,
        walk_forward=gate.get("walk_forward"),
        qualification=gate.get("qualification"),
        null_control=gate.get("null_control"),
        online_fdr=online, provenance=provenance,
        candidate_id=candidate_id, performance=performance)
    gate["passes"] = bool(envelope["passes"])
    gate["verified_gate"] = envelope
    gate["gate_hash"] = envelope["content_hash"]
    gate["failed_checks"] = sorted(
        key for key, value in envelope["checks"].items() if not value)
    return gate


__all__ = ["DiscoveryError", "corpus_partitions", "corpus_slice",
           "_read_discovery_rows", "_effective_ibr_config",
           "_opportunity_rows", "_null_reference_rows", "_discover_gate",
           "_finalize_gate"]
