"""Fail-closed source provenance checks shared by research write boundaries."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .market_data import NormalizationError, parse_timestamp


MAX_OBSERVED_AT_FUTURE_SKEW_SECONDS = 5 * 60
# Recorder provenance classifies rows first observed materially after their
# market event as historical.  Recheck that invariant at every research write
# boundary so an old/unlabelled corpus cannot be mistaken for forward evidence.
MAX_FORWARD_OBSERVATION_LAG_SECONDS = 15 * 60
SOURCE_MODES = frozenset({"forward_observed", "historical_backfill"})
SOURCE_KINDS = frozenset({
    "bar", "underlying", "underlying_bar",
    "quote", "quote_snapshot", "equity_quote", "underlying_quote",
    "option", "option_snapshot", "option_quote",
})


class SourceValidationError(ValueError):
    """Raised when a research source cannot prove the requested evidence mode."""

    def __init__(self, message: str, *, report: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.report = dict(report or {})


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str)


def source_content_hash(rows: Iterable[Mapping[str, Any]]) -> str:
    """Hash an ordered row stream with the same canonical list framing as replay."""
    hasher = hashlib.sha256()
    hasher.update(b"[")
    first = True
    for row in rows:
        if not first:
            hasher.update(b",")
        hasher.update(_canonical_json(row).encode("utf-8"))
        first = False
    hasher.update(b"]")
    return hasher.hexdigest()


def _clock(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        raise TypeError("now must be a timezone-aware datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise TypeError("now must be timezone-aware")
    return value.astimezone(timezone.utc)


def _mode(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_") or "forward_observed"


def source_paths(source: str | Path) -> list[Path]:
    """Return the exact ordered partitions consumed by research replay."""
    source = Path(source)
    if source.is_file():
        return [source]
    if source.is_dir():
        paths = sorted(path for path in source.glob("*.jsonl") if path.is_file())
        raw_window = os.getenv("ALPACA_RESEARCH_SESSION_WINDOW", "0")
        try:
            window = int(raw_window)
        except ValueError as exc:
            raise OSError(
                "invalid ALPACA_RESEARCH_SESSION_WINDOW") from exc
        return paths[-window:] if window > 0 else paths
    raise OSError(f"source does not exist: {source}")


def _rows(data: str | Path | Iterable[Mapping[str, Any]]):
    if isinstance(data, (str, Path)):
        if str(data) == "-":
            raise OSError(
                "stdin must be materialized and validated before research replay")
        offset = 0
        for path in source_paths(data):
            rows_seen = 0
            with path.open(encoding="utf-8") as stream:
                for number, line in enumerate(stream, offset + 1):
                    rows_seen += 1
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError as exc:
                        yield number, None, f"row {number}: invalid JSON: {exc}"
                        continue
                    if not isinstance(row, Mapping):
                        yield number, None, f"row {number}: expected an object"
                        continue
                    yield number, row, None
            offset += rows_seen
        return
    for number, row in enumerate(data, 1):
        if not isinstance(row, Mapping):
            yield number, None, f"row {number}: expected an object"
            continue
        yield number, row, None


def scan_source(
        data: str | Path | Iterable[Mapping[str, Any]], *,
        now: datetime | None = None) -> dict[str, Any]:
    """Return bounded provenance facts without applying evidence-mode policy."""
    clock = _clock(now)
    counts: dict[str, int] = {}
    errors: list[str] = []
    hasher = hashlib.sha256()
    hasher.update(b"[")
    first_hashed_row = True
    rows = 0
    future = 0
    late_forward = 0
    implicit_modes = 0
    latest: datetime | None = None
    providers: set[str] = set()
    feeds: set[str] = set()
    kinds: set[str] = set()
    try:
        iterator = _rows(data)
        for number, row, error in iterator:
            rows += 1
            if error:
                errors.append(error)
                continue
            assert row is not None
            if not first_hashed_row:
                hasher.update(b",")
            hasher.update(_canonical_json(row).encode("utf-8"))
            first_hashed_row = False
            raw_mode = row.get("source_mode")
            mode_valid = True
            if (raw_mode is None or
                    (isinstance(raw_mode, str) and not raw_mode.strip())):
                implicit_modes += 1
            elif not isinstance(raw_mode, str):
                errors.append(
                    f"row {number}: source_mode must be a string when present")
                mode_valid = False
            mode = _mode(raw_mode)
            if mode_valid:
                counts[mode] = counts.get(mode, 0) + 1
            provider = str(row.get("provider") or "").strip()
            feed = str(row.get("feed", row.get("feed_id")) or "").strip()
            if provider:
                providers.add(provider)
            if feed:
                feeds.add(feed)
            raw_kind = row.get("kind")
            if raw_kind not in (None, "") and not isinstance(raw_kind, str):
                errors.append(f"row {number}: kind must be a string when present")
                kind = ""
            else:
                kind = str(raw_kind or "bar").strip().lower()
            if kind:
                kinds.add(kind)
            if kind not in SOURCE_KINDS:
                errors.append(f"row {number}: unsupported kind {kind!r}")
            if mode_valid and mode not in SOURCE_MODES:
                errors.append(f"row {number}: unsupported source_mode {mode!r}")
            observed_raw = row.get("observed_at") or row.get("as_of") or row.get("timestamp")
            if observed_raw in (None, ""):
                continue
            try:
                observed = parse_timestamp(observed_raw, name="observed_at")
            except (NormalizationError, TypeError, ValueError) as exc:
                errors.append(f"row {number}: invalid observed_at: {exc}")
                continue
            if latest is None or observed > latest:
                latest = observed
            if (observed - clock).total_seconds() > MAX_OBSERVED_AT_FUTURE_SKEW_SECONDS:
                future += 1
            event_raw = row.get("timestamp")
            if event_raw not in (None, ""):
                try:
                    event_at = parse_timestamp(event_raw, name="timestamp")
                except (NormalizationError, TypeError, ValueError) as exc:
                    errors.append(f"row {number}: invalid timestamp: {exc}")
                else:
                    if (mode_valid and mode != "historical_backfill" and
                            (observed - event_at).total_seconds() >
                            MAX_FORWARD_OBSERVATION_LAG_SECONDS):
                        late_forward += 1
    except (OSError, UnicodeError) as exc:
        errors.append(f"unable to read source: {exc}")
    hasher.update(b"]")
    return {
        "rows": rows,
        "source_mode_counts": dict(sorted(counts.items())),
        "implicit_source_mode_rows": implicit_modes,
        "future_observed_rows": future,
        "late_forward_observation_rows": late_forward,
        "latest_observed_at": None if latest is None else latest.isoformat(),
        "providers": sorted(providers),
        "feeds": sorted(feeds),
        "kinds": sorted(kinds),
        "content_hash": hasher.hexdigest(),
        "errors": errors,
    }


def validate_source(
        data: str | Path | Iterable[Mapping[str, Any]], *,
        diagnostic_only: bool = False,
        now: datetime | None = None,
        expected_content_hash: str | None = None) -> dict[str, Any]:
    """Validate source identity before any research result or ledger mutation.

    Historical and mixed modes are permitted only for an explicit diagnostic
    run. Authorizing inputs must also carry an explicit source_mode on every
    row. Structural corruption, unsupported labels, future observation times,
    and a changed source snapshot fail closed in every mode.
    """
    if not isinstance(diagnostic_only, bool):
        raise TypeError("diagnostic_only must be boolean")
    report = scan_source(data, now=now)
    errors = list(report.get("errors") or ())
    counts = dict(report.get("source_mode_counts") or {})
    modes = {mode for mode, count in counts.items() if count}
    if int(report.get("future_observed_rows") or 0):
        errors.append(
            "observed_at is unreasonably in the future of wall clock "
            f"(>{MAX_OBSERVED_AT_FUTURE_SKEW_SECONDS}s skew)")
    if int(report.get("late_forward_observation_rows") or 0):
        errors.append(
            "rows first observed more than "
            f"{MAX_FORWARD_OBSERVATION_LAG_SECONDS}s after market timestamp "
            "must be labelled historical_backfill")
    if expected_content_hash is not None and str(report.get("content_hash")) != str(
            expected_content_hash):
        errors.append("research source changed after provenance validation")
    if not diagnostic_only:
        if int(report.get("implicit_source_mode_rows") or 0):
            errors.append(
                "authorizing provenance preflight requires explicit source_mode; "
                "use diagnostic-only for unlabelled compatibility data")
        if counts.get("historical_backfill", 0):
            errors.append(
                "historical_backfill source_mode is diagnostic-only; "
                "use an explicit diagnostic-only run")
        if len(modes) > 1:
            errors.append("mixed source_mode values cannot authorize evidence")
    report.update({
        "errors": errors,
        "authorizing": not diagnostic_only,
        "diagnostic_only": diagnostic_only,
    })
    if errors:
        raise SourceValidationError(
            "research source preflight failed: " + "; ".join(errors[:8]),
            report=report)
    return report


__all__ = [
    "MAX_FORWARD_OBSERVATION_LAG_SECONDS",
    "MAX_OBSERVED_AT_FUTURE_SKEW_SECONDS", "SOURCE_KINDS", "SOURCE_MODES",
    "SourceValidationError", "scan_source", "source_content_hash", "source_paths",
    "validate_source",
]
