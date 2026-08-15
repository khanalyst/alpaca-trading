"""One executable-cost and fill model for every research lane.

Three lanes used to carry their own spread/slippage/fee numbers and their own
arithmetic, so a change in one silently disagreed with the others and none of
them agreed with the deployed runtime.  This module owns both: the expected
cost parameters and the formulas that spend them.  A lane may choose a
``CostModel``; it may not re-implement one.

Expected cost is not a rejection cap.  ``execution.max_slippage_bps`` is the
worst quoted slippage the runtime will *accept* before refusing to submit;
simulating at that number prices every fill as if it were the worst tolerable
one, and simulating without it prices every fill as if the cap did not exist.
The expected values below are the cost of a normal marketable fill in the
configured liquid US ETF universe; the caps are carried alongside only so a
model that expects a cost the runtime would reject fails closed here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
import math
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping, Sequence

# A quoted spread of ~2 bps covers a one- to two-cent book on the configured
# ETF universe including its less liquid members, not only the tightest name.
DEFAULT_SPREAD_BPS = 2.0
# A marketable entry and a broker-resident stop leg both execute through the
# book at whatever is resting when they arrive; a triggered stop in a moving
# market pays materially more than half the quoted spread.
DEFAULT_SLIPPAGE_BPS = 3.0
# Regulatory and exchange fees on notional, charged on both sides.
DEFAULT_FEE_BPS = 0.5
# Conservative listed-option broker/exchange fee floor per contract per side.
# Configuration may override this, but a default zero would systematically
# overstate option expectancy relative to equity.
DEFAULT_OPTION_FEE_PER_CONTRACT_SIDE = 0.05
# Mirrors the checked `execution` block; these are caps, never expectations.
RUNTIME_MAX_SPREAD_BPS = 100.0
RUNTIME_MAX_SLIPPAGE_BPS = 50.0

CONFIG_BLOCK = "costs"


class CostError(ValueError):
    """Raised for a malformed or internally inconsistent cost model."""


@dataclass(frozen=True)
class ReplayPolicy:
    """Point-in-time and portfolio limits shared by replay lanes.

    The runtime owns these values.  A research caller can pass the validated
    runtime config (or this value object) through the optional ``policy`` hook;
    omitted policy retains the historical fixture behaviour for compatibility.
    """

    max_market_data_age_seconds: float = 30.0
    options_min_dte: int = 7
    options_max_dte: int = 60
    options_max_spread_pct: float = 10.0
    risk_per_trade_pct: float = 0.5
    latest_entry_time: time | None = None
    force_flat_time: time | None = None
    max_concurrent_positions: int | None = None
    max_position_notional_pct: float | None = None
    max_gross_exposure_pct: float | None = None
    max_open_risk_pct: float | None = None
    daily_loss_limit_pct: float | None = None
    strict_market_data: bool = True

    def __post_init__(self) -> None:
        age = float(self.max_market_data_age_seconds)
        if not math.isfinite(age) or age < 0:
            raise CostError("max_market_data_age_seconds must be finite and non-negative")
        if int(self.options_min_dte) != self.options_min_dte or self.options_min_dte < 0:
            raise CostError("options_min_dte must be a non-negative integer")
        if int(self.options_max_dte) != self.options_max_dte or self.options_max_dte < self.options_min_dte:
            raise CostError("options_max_dte must be an integer >= options_min_dte")
        spread = float(self.options_max_spread_pct)
        if not math.isfinite(spread) or spread < 0:
            raise CostError("options_max_spread_pct must be finite and non-negative")
        for name in ("max_concurrent_positions",):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or int(value) != value or int(value) < 1):
                raise CostError(f"{name} must be a positive integer when supplied")
        for name in ("risk_per_trade_pct", "max_position_notional_pct", "max_gross_exposure_pct",
                     "max_open_risk_pct", "daily_loss_limit_pct"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(float(value)) or float(value) < 0):
                raise CostError(f"{name} must be finite and non-negative when supplied")
        if not isinstance(self.strict_market_data, bool):
            raise CostError("strict_market_data must be true or false")

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_market_data_age_seconds": float(self.max_market_data_age_seconds),
            "options_min_dte": int(self.options_min_dte),
            "options_max_dte": int(self.options_max_dte),
            "options_max_spread_pct": float(self.options_max_spread_pct),
            "risk_per_trade_pct": float(self.risk_per_trade_pct),
            "latest_entry_time": (None if self.latest_entry_time is None else
                                   self.latest_entry_time.isoformat()),
            "force_flat_time": (None if self.force_flat_time is None else
                                 self.force_flat_time.isoformat()),
            "max_concurrent_positions": self.max_concurrent_positions,
            "max_position_notional_pct": self.max_position_notional_pct,
            "max_gross_exposure_pct": self.max_gross_exposure_pct,
            "max_open_risk_pct": self.max_open_risk_pct,
            "daily_loss_limit_pct": self.daily_loss_limit_pct,
            "strict_market_data": self.strict_market_data,
        }

    @classmethod
    def from_config(cls, config: Mapping | None) -> "ReplayPolicy":
        """Read limits from the same validated runtime config blocks."""
        source = dict(config or {})
        execution = source.get("execution") or {}
        risk = source.get("risk") or {}
        strategy = source.get("strategy") or {}
        session = source.get("session") or {}
        if not all(isinstance(block, Mapping) for block in (execution, risk, strategy, session)):
            raise CostError("runtime policy blocks must be mappings")
        latest = strategy.get("latest_entry_time")
        if latest is not None and not isinstance(latest, time):
            try:
                latest = time.fromisoformat(str(latest))
            except ValueError as exc:
                raise CostError("strategy.latest_entry_time must be HH:MM") from exc
        force = strategy.get("force_flat_time")
        if force is None:
            minutes = session.get("force_flat_minutes_before_close")
            # The runtime session close is 16:00 ET; callers that provide only
            # the minute offset get the same force-flat wall clock.
            if minutes is not None:
                try:
                    force = (datetime.combine(date.today(), time(16, 0)) -
                             timedelta(minutes=int(minutes))).time()
                except (TypeError, ValueError):
                    raise CostError("session.force_flat_minutes_before_close must be an integer")
        elif not isinstance(force, time):
            try:
                force = time.fromisoformat(str(force))
            except ValueError as exc:
                raise CostError("force_flat_time must be HH:MM") from exc
        return cls(
            max_market_data_age_seconds=float(execution.get("max_market_data_age_seconds", 30.0)),
            options_min_dte=int(risk.get("options_min_dte", 7)),
            options_max_dte=int(risk.get("options_max_dte", 60)),
            options_max_spread_pct=float(risk.get("options_max_spread_pct", 10.0)),
            risk_per_trade_pct=float(risk.get("risk_per_trade_pct", 0.5)),
            latest_entry_time=latest,
            force_flat_time=force,
            max_concurrent_positions=(None if risk.get("max_concurrent_positions") is None else int(risk["max_concurrent_positions"])),
            max_position_notional_pct=(None if risk.get("max_position_notional_pct") is None else float(risk["max_position_notional_pct"])),
            max_gross_exposure_pct=(None if risk.get("max_gross_exposure_pct") is None else float(risk["max_gross_exposure_pct"])),
            max_open_risk_pct=(None if risk.get("max_total_open_risk_pct",
                                                risk.get("max_open_risk_pct")) is None else
                               float(risk.get("max_total_open_risk_pct",
                                              risk.get("max_open_risk_pct")))),
            daily_loss_limit_pct=(None if risk.get("daily_loss_limit_pct") is None else float(risk["daily_loss_limit_pct"])),
            # Every other policy field is read here; omitting this one pinned it
            # at the strict default with no way to change it from configuration.
            # A bars-only corpus (a backfill without ``--quotes``) then prices
            # nothing at all, which is a data-shape mismatch rather than a
            # research result -- see ``fill_source_summary``.
            strict_market_data=_strict_market_data(execution),
        )


def replay_policy_for_mode(policy: ReplayPolicy, mode: str, *,
                           backtest_bar_fallback: bool = False) -> ReplayPolicy:
    """Derive the policy allowed for one offline research lane.

    Backtests may explicitly replay historical bars without executable quotes;
    shadow replay is always point-in-time strict because it is the evidence
    that gates live-shadow authorization.  Direct replay callers retain their
    existing policy and do not pass through this helper.
    """
    if not isinstance(policy, ReplayPolicy):
        raise CostError("policy must be a ReplayPolicy")
    lane = str(mode).strip().lower()
    if lane not in {"backtest", "shadow"}:
        raise CostError("mode must be backtest or shadow")
    if not isinstance(backtest_bar_fallback, bool):
        raise CostError("backtest_bar_fallback must be true or false")
    strict = lane == "shadow" or not (lane == "backtest" and backtest_bar_fallback)
    return replace(policy, strict_market_data=strict)


def _strict_market_data(execution: Mapping) -> bool:
    """Resolve ``execution.strict_market_data``, defaulting to strict.

    Strict replay refuses to price a fill it has no recorded quote for, which
    is right when a quote *should* exist and did not.  A corpus that never
    carried quotes at all is a different situation: nothing can price, and the
    honest response is to say so rather than to report an edgeless run.
    """
    value = execution.get("strict_market_data", True)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise CostError("execution.strict_market_data must be true or false")


def _bps(value: Any, name: str) -> float:
    # Booleans are not numbers and a numeric string is not a measurement; both
    # are configuration mistakes that must surface rather than be coerced.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CostError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise CostError(f"{name} must be finite and non-negative")
    return number


@dataclass(frozen=True)
class CostModel:
    """Expected per-fill cost, validated against the runtime's rejection caps."""

    spread_bps: float = DEFAULT_SPREAD_BPS
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS
    fee_bps: float = DEFAULT_FEE_BPS
    max_spread_bps: float = RUNTIME_MAX_SPREAD_BPS
    max_slippage_bps: float = RUNTIME_MAX_SLIPPAGE_BPS
    # Optional listed-option fee charged once per contract per side.  Kept
    # after the original fields so positional CostModel(...) callers remain
    # backward-compatible.
    option_fee_per_contract_side: float = DEFAULT_OPTION_FEE_PER_CONTRACT_SIDE
    # Alias accepted for broker schedules that call the per-side amount simply
    # ``option_fee_per_contract``.
    option_fee_per_contract: float | None = None
    provenance: str = "default"

    def __post_init__(self) -> None:
        for name in ("spread_bps", "slippage_bps", "fee_bps",
                     "option_fee_per_contract_side",
                     "max_spread_bps", "max_slippage_bps"):
            object.__setattr__(self, name, _bps(getattr(self, name), name))
        if self.option_fee_per_contract is not None:
            object.__setattr__(self, "option_fee_per_contract",
                               _bps(self.option_fee_per_contract,
                                    "option_fee_per_contract"))
            object.__setattr__(self, "option_fee_per_contract_side",
                               self.option_fee_per_contract)
        if not isinstance(self.provenance, str) or not self.provenance.strip():
            raise CostError("provenance must be a non-empty string")
        if self.spread_bps > self.max_spread_bps:
            raise CostError(
                f"expected spread {self.spread_bps} bps exceeds the runtime's "
                f"{self.max_spread_bps} bps rejection cap")
        # The runtime measures slippage against its own reference price and
        # refuses to submit past the cap.  A research model that expects more
        # than that is simulating fills the runtime would never take.
        if self.entry_cost_bps > self.max_slippage_bps:
            raise CostError(
                f"expected entry cost {self.entry_cost_bps} bps exceeds the "
                f"runtime's {self.max_slippage_bps} bps slippage cap")

    @property
    def entry_cost_bps(self) -> float:
        """Half the quoted spread plus adverse slippage, in basis points."""
        return self.spread_bps / 2.0 + self.slippage_bps

    def per_side_bps(self, *, executable_quote: bool = False) -> float:
        """Cost of one execution; an executable quote already includes spread."""
        return self.slippage_bps if executable_quote else self.entry_cost_bps

    def execution_price(self, reference: float, direction: str, *, entry: bool,
                        executable_quote: bool = False) -> float:
        """Move an execution reference adversely by one side's cost."""
        # Long buys at the ask and sells at the bid; short mirrors it.  An
        # option ``ask``/``bid`` or an equity quote is already executable, so
        # charging a modelled half-spread on top would bill the spread twice.
        sign = 1.0 if ((direction == "long") == entry) else -1.0
        rate = self.per_side_bps(executable_quote=executable_quote) / 10_000.0
        return float(reference) * (1.0 + sign * rate)

    def fees(self, entry_price: float, exit_price: float, quantity: float,
             multiplier: float = 1.0, *, vehicle: str = "equity") -> float:
        """Both-side notional fees plus optional per-contract option fees."""
        notional = (abs(float(entry_price)) + abs(float(exit_price))) * \
            float(quantity) * float(multiplier)
        total = notional * self.fee_bps / 10_000.0
        if vehicle == "option":
            total += float(quantity) * 2.0 * self.option_fee_per_contract_side
        return total

    def as_dict(self) -> dict:
        return {"spread_bps": self.spread_bps, "slippage_bps": self.slippage_bps,
                "fee_bps": self.fee_bps,
                "option_fee_per_contract_side": self.option_fee_per_contract_side,
                "option_fee_per_contract": self.option_fee_per_contract_side,
                "entry_cost_bps": self.entry_cost_bps,
                "max_spread_bps": self.max_spread_bps,
                "max_slippage_bps": self.max_slippage_bps,
                "provenance": self.provenance}

    @classmethod
    def from_config(cls, config: Mapping | None) -> CostModel:
        """Build from the single ``costs`` block, capped by ``execution``.

        The caps are read from the same ``execution`` block the trader
        validates, so tightening the runtime's tolerance is immediately a
        research constraint rather than a number somebody remembers to copy.
        """
        source = dict(config or {})
        block = source.get(CONFIG_BLOCK) or {}
        if not isinstance(block, Mapping):
            raise CostError(f"{CONFIG_BLOCK} must be a mapping")
        unknown = sorted(set(block) - {"spread_bps", "slippage_bps", "fee_bps",
                                       "option_fee_per_contract_side",
                                       "option_fee_per_contract", "provenance"})
        if unknown:
            raise CostError(f"{CONFIG_BLOCK} has unknown field(s): {', '.join(unknown)}")
        execution = source.get("execution") or {}
        if not isinstance(execution, Mapping):
            raise CostError("execution must be a mapping")
        return cls(
            spread_bps=block.get("spread_bps", DEFAULT_SPREAD_BPS),
            slippage_bps=block.get("slippage_bps", DEFAULT_SLIPPAGE_BPS),
            fee_bps=block.get("fee_bps", DEFAULT_FEE_BPS),
            option_fee_per_contract_side=block.get(
                "option_fee_per_contract_side", DEFAULT_OPTION_FEE_PER_CONTRACT_SIDE),
            option_fee_per_contract=block.get("option_fee_per_contract"),
            max_spread_bps=execution.get("max_spread_bps", RUNTIME_MAX_SPREAD_BPS),
            max_slippage_bps=execution.get("max_slippage_bps", RUNTIME_MAX_SLIPPAGE_BPS),
            provenance=str(block.get("provenance", "default" if not block else "config")),
        )


BAR = "bar"
QUOTE = "quote"


@dataclass(frozen=True)
class SQLiteQuoteIndexDescriptor:
    """Serializable, read-only handle for a finalized quote index.

    The descriptor intentionally carries the symbol-id map and summary
    metadata in addition to the SQLite path.  A worker can therefore resolve
    quotes without rebuilding an index or depending on mutable parent state.
    The owning :class:`SQLiteQuoteIndex` remains responsible for the temporary
    directory lifetime until all children have closed their handles.
    """

    path: str
    symbols: tuple[tuple[str, int], ...]
    count: int
    max_session_date: str | None


class SQLiteQuoteIndex:
    """Disk-backed quote resolver for large recorded corpora.

    A research cycle only asks for the latest visible quote at a fill boundary;
    retaining millions of normalized quote objects in a Python list is both
    wasteful and the source of the backtest OOM.  This index stores the small
    set of fields needed by :func:`quote_fill` in a temporary SQLite database
    and keeps only the symbol-id map and a write batch in memory.
    """

    _BATCH_SIZE = 10_000

    def __init__(self, directory: str | Path | None = None):
        import sqlite3

        self._temporary: tempfile.TemporaryDirectory | None = None
        if directory is None:
            self._temporary = tempfile.TemporaryDirectory(prefix="alpaca-quotes-")
            root = Path(self._temporary.name)
        else:
            root = Path(directory)
            root.mkdir(parents=True, exist_ok=True)
        self.path = root / "quotes.sqlite3"
        self._db = sqlite3.connect(str(self.path), timeout=30)
        self._read_only = False
        self._db.execute("PRAGMA journal_mode=OFF")
        self._db.execute("PRAGMA synchronous=OFF")
        self._db.execute("PRAGMA temp_store=FILE")
        self._db.execute("""
            CREATE TABLE quotes (
                symbol_id INTEGER NOT NULL,
                timestamp REAL NOT NULL,
                as_of REAL NOT NULL,
                bid REAL NOT NULL,
                ask REAL NOT NULL,
                session_day INTEGER NOT NULL,
                sequence INTEGER NOT NULL,
                PRIMARY KEY (symbol_id, timestamp, sequence)
            ) WITHOUT ROWID
        """)
        self._symbols: dict[str, int] = {}
        self._pending: list[tuple[int, float, float, float, float, int, int]] = []
        self._sequence = 0
        self._count = 0
        self._max_session_day: int | None = None
        self._closed = False

    @classmethod
    def open_read_only(cls, descriptor: SQLiteQuoteIndexDescriptor):
        """Open a finalized index from a serializable descriptor.

        The returned object owns only a read-only SQLite connection.  Closing
        it never removes the descriptor's file or its parent temporary
        directory.
        """
        import sqlite3

        if not isinstance(descriptor, SQLiteQuoteIndexDescriptor):
            raise TypeError("descriptor must be a SQLiteQuoteIndexDescriptor")
        path = Path(descriptor.path)
        if not path.is_file():
            raise FileNotFoundError(path)
        obj = cls.__new__(cls)
        obj._temporary = None
        obj.path = path
        obj._db = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
        obj._read_only = True
        obj._symbols = dict(descriptor.symbols)
        obj._pending = []
        obj._sequence = 0
        obj._count = int(descriptor.count)
        max_session = descriptor.max_session_date
        obj._max_session_day = (None if max_session is None
                                else date.fromisoformat(max_session).toordinal())
        obj._closed = False
        return obj


    def descriptor(self) -> SQLiteQuoteIndexDescriptor:
        """Return a serializable descriptor for this finalized index."""
        if self._closed:
            raise RuntimeError("quote index is closed")
        self.finalize()
        return SQLiteQuoteIndexDescriptor(
            path=str(self.path),
            symbols=tuple(sorted(self._symbols.items())),
            count=self._count,
            max_session_date=(None if self.max_session_date is None
                              else self.max_session_date.isoformat()),
        )

    def add(self, quote: Any) -> None:
        if self._closed:
            raise RuntimeError("quote index is closed")
        if self._read_only:
            raise RuntimeError("read-only quote index cannot write")
        symbol = str(quote.symbol).upper()
        symbol_id = self._symbols.get(symbol)
        if symbol_id is None:
            symbol_id = len(self._symbols) + 1
            self._symbols[symbol] = symbol_id
        timestamp = float(quote.timestamp.timestamp())
        as_of = float(quote.identity.as_of.timestamp())
        session_day = int(quote.session_date.toordinal())
        self._pending.append((
            symbol_id, timestamp, as_of, float(quote.bid), float(quote.ask),
            session_day, self._sequence,
        ))
        self._sequence += 1
        self._count += 1
        if self._max_session_day is None or session_day > self._max_session_day:
            self._max_session_day = session_day
        if len(self._pending) >= self._BATCH_SIZE:
            self._flush()

    def _flush(self) -> None:
        if not self._pending:
            return
        if self._read_only:
            raise RuntimeError("read-only quote index cannot write")
        self._db.executemany(
            "INSERT INTO quotes VALUES (?,?,?,?,?,?,?)", self._pending)
        self._db.commit()
        self._pending.clear()

    def finalize(self) -> "SQLiteQuoteIndex":
        if not self._closed and not self._read_only:
            self._flush()
        return self

    @property
    def count(self) -> int:
        return self._count

    @property
    def max_session_date(self) -> date | None:
        return (date.fromordinal(self._max_session_day)
                if self._max_session_day is not None else None)

    def quote_fill(self, *, symbol: str, at: datetime, side: str,
                   max_age_seconds: float | None = 30.0,
                   session_date: date | None = None) -> float | None:
        """Resolve the same latest-visible quote as the in-memory index."""
        if self._closed or self._count == 0:
            return None
        if not self._read_only:
            self._flush()
        symbol_id = self._symbols.get(str(symbol).upper())
        if symbol_id is None:
            return None
        at_ts = float(at.timestamp())
        limit = 30.0 if max_age_seconds is None else float(max_age_seconds)
        session_day = (None if session_date is None else
                       int(session_date.toordinal()))
        cursor = self._db.execute(
            """SELECT timestamp, as_of, bid, ask, session_day
                 FROM quotes
                WHERE symbol_id=? AND timestamp<=?
                ORDER BY timestamp DESC, sequence DESC""",
            (symbol_id, at_ts),
        )
        for timestamp, as_of, bid, ask, row_session_day in cursor:
            age = at_ts - float(timestamp)
            if age > limit:
                # Rows are newest first, so all remaining rows are stale.
                break
            if session_day is not None and int(row_session_day) != session_day:
                continue
            if float(as_of) > at_ts:
                continue
            price = float(ask if side == "buy" else bid)
            return price if math.isfinite(price) and price > 0 else None
        return None

    def close(self) -> None:
        if self._closed:
            return
        try:
            if not self._read_only:
                self._flush()
            self._db.close()
        finally:
            self._closed = True
            if self._temporary is not None:
                self._temporary.cleanup()
                self._temporary = None

    def __bool__(self) -> bool:
        return self._count > 0 and not self._closed

    def __del__(self):  # pragma: no cover - interpreter shutdown cleanup
        try:
            self.close()
        except Exception:
            pass


def index_quotes(quotes: Iterable[Any] | None) -> dict[str, list] | SQLiteQuoteIndex:
    """Group quote snapshots by symbol in chronological order."""
    if quotes is not None and callable(getattr(quotes, "quote_fill", None)):
        return quotes
    grouped: dict[str, list] = {}
    for quote in quotes or ():
        grouped.setdefault(str(quote.symbol).upper(), []).append(quote)
    for rows in grouped.values():
        rows.sort(key=lambda item: item.timestamp)
    return grouped


def quote_fill(indexed: Mapping[str, Sequence[Any]] | SQLiteQuoteIndex | None, *, symbol: str,
               at: datetime, side: str, max_age_seconds: float | None = 30.0,
               session_date: date | None = None) -> float | None:
    """Return the executable side of the last quote visible at a fill instant.

    ``None`` means no quote was recorded for that instant; the caller must
    fall back to the bar and say so rather than inventing a price.
    """
    if indexed is None:
        return None
    resolver = getattr(indexed, "quote_fill", None)
    if callable(resolver):
        return resolver(symbol=symbol, at=at, side=side,
                        max_age_seconds=max_age_seconds,
                        session_date=session_date)
    if not indexed:
        return None
    rows = indexed.get(str(symbol).upper())
    if not rows:
        return None
    best = None
    for quote in rows:
        if quote.timestamp > at:
            break
        identity = getattr(quote, "identity", None)
        if identity is None or identity.as_of > at:
            continue
        if session_date is not None and getattr(quote, "session_date", None) != session_date:
            continue
        best = quote
    if best is None:
        return None
    age = (at - best.timestamp).total_seconds()
    limit = 30.0 if max_age_seconds is None else float(max_age_seconds)
    if age < 0 or age > limit:
        return None
    price = float(best.ask if side == "buy" else best.bid)
    return price if math.isfinite(price) and price > 0 else None


__all__ = [
    "BAR", "CONFIG_BLOCK", "CostError", "CostModel", "DEFAULT_FEE_BPS",
    "DEFAULT_OPTION_FEE_PER_CONTRACT_SIDE", "DEFAULT_SLIPPAGE_BPS",
    "DEFAULT_SPREAD_BPS", "QUOTE",
    "RUNTIME_MAX_SLIPPAGE_BPS", "RUNTIME_MAX_SPREAD_BPS", "ReplayPolicy",
    "replay_policy_for_mode",
    "SQLiteQuoteIndex", "SQLiteQuoteIndexDescriptor", "index_quotes", "quote_fill",
]
