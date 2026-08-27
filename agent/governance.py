"""Operator-declared promotions, and an audit trail for configuration changes.

Two facts belong to the operator alone and must not move on their own.

*Which edge trades.*  Research can prove an edge; only a person decides that a
proved edge should be given money.  ``strategy.pinned`` is that decision written
down: a list of entries, each carrying an id the operator assigns, naming an
exact ``variant_id`` and vehicle.  Pinning selects; it never authorizes — a
pinned entry still has to resolve to a ``validated``/``champion`` ledger record
with a re-verified passing proof, so writing an id into a file cannot conjure
an edge that never earned one.  Pinning also cannot override hard lifecycle
stops: sequential drift and trial failures demote the edge and record the
promotion context so runtime selection fails closed.  Rolling-R remains
advisory telemetry.  Pinning only prevents an
automatic selector from silently substituting a different proved edge.

*What the configuration said.*  Every distinct configuration the runtime loads
is content-addressed and recorded append-only with its diff against the
previous one, so any changed value can be traced to a version id, a time, and
the exact fields that moved.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping


CONFIG_AUDIT_SCHEMA = "config-audit.v1"
PROMOTION_SCHEMA = "strategy-promotion.v1"
# An operator-assigned id.  Deliberately human-authored rather than generated:
# it is the handle used in the config file, in the audit trail, and in
# conversation about a promotion, so it has to be readable and stable.
_PROMOTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")
# Secrets can legitimately appear in a loaded config.  The audit trail records
# that they changed, never what they changed to.
_REDACTED_KEYS = frozenset((
    "api_key", "secret_key", "apikey", "secret", "token", "password",
    "webhook_url", "endpoint",
))
REDACTED = "<redacted>"


class GovernanceError(ValueError):
    """Raised when a promotion or audit record cannot be trusted as written."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _utc() -> float:
    return datetime.now(timezone.utc).timestamp()


def redact(value: Any) -> Any:
    """Return a copy safe to store and display, with secret values removed."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower())
            if normalized in _REDACTED_KEYS and item not in (None, "", [], {}):
                result[str(key)] = REDACTED
            else:
                result[str(key)] = redact(item)
        return result
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten a config into dotted paths so a diff names the field that moved."""
    if isinstance(value, Mapping):
        flat: dict[str, Any] = {}
        for key, item in value.items():
            flat.update(_flatten(item, f"{prefix}.{key}" if prefix else str(key)))
        return flat
    if isinstance(value, (list, tuple)):
        # Lists are compared whole: a reordered universe is a change, and
        # per-index diffs of an eight-symbol list are noise, not information.
        return {prefix: list(value)}
    return {prefix: value}


def config_diff(before: Mapping[str, Any] | None,
                after: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Name every field that changed between two configurations."""
    old = _flatten(redact(before)) if before is not None else {}
    new = _flatten(redact(after))
    changes = []
    for path in sorted(set(old) | set(new)):
        was, now = old.get(path, None), new.get(path, None)
        if path in old and path in new and was == now:
            continue
        if path not in old:
            changes.append({"path": path, "change": "added", "to": now})
        elif path not in new:
            changes.append({"path": path, "change": "removed", "from": was})
        else:
            changes.append({"path": path, "change": "changed",
                            "from": was, "to": now})
    return changes


def validate_promotions(value: Any, *, path: str = "strategy.pinned"
                        ) -> list[dict[str, Any]]:
    """Validate the operator's pinned promotions.

    Every entry carries its own id because the id is the audit handle: it is
    what an operator quotes when asking why something is trading, and what the
    audit trail names when the entry is added, edited, or removed.
    """
    if value in (None, ""):
        return []
    if not isinstance(value, (list, tuple)):
        raise GovernanceError(f"{path} must be a list of promotion entries")
    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_variants: set[tuple[str, str]] = set()
    for index, raw in enumerate(value):
        where = f"{path}[{index}]"
        if not isinstance(raw, Mapping):
            raise GovernanceError(f"{where} must be a mapping")
        unknown = sorted(set(raw) - {"id", "variant_id", "vehicle", "note",
                                     "strategy_id", "promoted_at"})
        if unknown:
            raise GovernanceError(
                f"{where} has unknown field(s): {', '.join(unknown)}")
        promotion_id = str(raw.get("id") or "").strip()
        if not _PROMOTION_ID.match(promotion_id):
            raise GovernanceError(
                f"{where}.id must be 3-64 characters of letters, digits, "
                "dot, dash or underscore")
        if promotion_id in seen_ids:
            raise GovernanceError(f"{where}.id duplicates {promotion_id!r}")
        variant_id = str(raw.get("variant_id") or "").strip()
        if not variant_id:
            raise GovernanceError(f"{where}.variant_id is required")
        vehicle = str(raw.get("vehicle") or "equity").strip().lower()
        if vehicle not in {"equity", "option"}:
            raise GovernanceError(f"{where}.vehicle must be equity or option")
        if (variant_id, vehicle) in seen_variants:
            raise GovernanceError(
                f"{where} pins {variant_id!r} for {vehicle} twice")
        strategy_id = str(raw.get("strategy_id") or "rule").strip()
        if not strategy_id:
            raise GovernanceError(f"{where}.strategy_id must not be empty")
        note = str(raw.get("note") or "").strip()
        if len(note) > 500:
            raise GovernanceError(f"{where}.note exceeds 500 characters")
        promoted_at = str(raw.get("promoted_at") or "").strip()
        seen_ids.add(promotion_id)
        seen_variants.add((variant_id, vehicle))
        entries.append({"id": promotion_id, "variant_id": variant_id,
                        "vehicle": vehicle, "strategy_id": strategy_id,
                        "note": note, "promoted_at": promoted_at})
    return entries


def pinned_variant_ids(config: Mapping[str, Any]) -> set[tuple[str, str]]:
    """The ``(variant_id, vehicle)`` pairs the operator pinned, if any."""
    strategy = config.get("strategy") if isinstance(config, Mapping) else {}
    entries = (strategy or {}).get("pinned") or []
    return {(str(item.get("variant_id")), str(item.get("vehicle")))
            for item in entries if isinstance(item, Mapping)}


def promotion_snippet(variant_id: str, vehicle: str, *,
                      promotion_id: str | None = None,
                      note: str = "") -> str:
    """Render the exact config a promotion needs, ready to paste.

    Reading a variant id off a report and retyping it is where a promotion
    goes wrong, so the report hands over the block rather than the id.
    """
    suffix = str(variant_id).split(".")[-1][:8] or "edge"
    entry = {
        "id": promotion_id or f"pin-{vehicle}-{suffix}",
        "variant_id": str(variant_id),
        "vehicle": str(vehicle),
        "strategy_id": "rule",
        "promoted_at": datetime.now(timezone.utc).date().isoformat(),
        "note": note or "promoted on positive live paper return",
    }
    return json.dumps({"strategy": {"selection_mode": "pinned",
                                    "pinned": [entry]}}, indent=2)


_AUDIT_TABLE = """CREATE TABLE IF NOT EXISTS config_versions (
    config_version_id TEXT PRIMARY KEY,
    config_hash TEXT NOT NULL UNIQUE,
    previous_version_id TEXT,
    mode TEXT NOT NULL,
    source TEXT NOT NULL,
    actor TEXT NOT NULL,
    config_json TEXT NOT NULL,
    diff_json TEXT NOT NULL,
    created_at REAL NOT NULL
)"""
_AUDIT_TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS config_versions_no_update
       BEFORE UPDATE ON config_versions BEGIN
       SELECT RAISE(ABORT, 'config versions are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS config_versions_no_delete
       BEFORE DELETE ON config_versions BEGIN
       SELECT RAISE(ABORT, 'config versions are immutable'); END""",
)


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    return db


def _version_id(config_hash: str) -> str:
    return f"cfg-{config_hash[:16]}"


def record_config_version(journal_path: str | Path, config: Mapping[str, Any], *,
                          source: str = "config.yaml", actor: str = "operator",
                          ) -> dict[str, Any]:
    """Record this configuration if it differs from the last one recorded.

    The id is derived from the content, so the same configuration loaded a
    thousand times is one version, and any changed value is a new one.  That
    makes the id answer "which settings were in force" rather than "how many
    times did the process start".
    """
    path = Path(journal_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = redact(config)
    config_hash = content_hash(safe)
    version_id = _version_id(config_hash)
    with closing(_connect(path)) as db, db:
        db.execute(_AUDIT_TABLE)
        for trigger in _AUDIT_TRIGGERS:
            db.execute(trigger)
        existing = db.execute(
            "SELECT * FROM config_versions WHERE config_hash=?",
            (config_hash,)).fetchone()
        if existing is not None:
            return {**dict(existing), "recorded": False,
                    "diff": json.loads(existing["diff_json"])}
        previous = db.execute(
            "SELECT config_version_id, config_json FROM config_versions "
            "ORDER BY created_at DESC, config_version_id DESC LIMIT 1").fetchone()
        before = json.loads(previous["config_json"]) if previous else None
        diff = config_diff(before, safe)
        db.execute("INSERT INTO config_versions VALUES(?,?,?,?,?,?,?,?,?)", (
            version_id, config_hash,
            previous["config_version_id"] if previous else None,
            str(config.get("mode") or "paper"), str(source), str(actor),
            canonical_json(safe), canonical_json(diff), _utc(),
        ))
    return {"config_version_id": version_id, "config_hash": config_hash,
            "previous_version_id": previous["config_version_id"] if previous else None,
            "mode": str(config.get("mode") or "paper"), "source": str(source),
            "actor": str(actor), "diff": diff, "recorded": True}


def config_history(journal_path: str | Path, *, limit: int = 50
                   ) -> list[dict[str, Any]]:
    """Read the audit trail back, newest first."""
    path = Path(journal_path)
    if not path.is_file():
        return []
    try:
        with closing(_connect(path)) as db:
            tables = {str(row[0]) for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "config_versions" not in tables:
                return []
            rows = db.execute(
                """SELECT config_version_id, config_hash, previous_version_id,
                          mode, source, actor, diff_json, created_at
                   FROM config_versions
                   ORDER BY created_at DESC, config_version_id DESC LIMIT ?""",
                (max(1, int(limit)),)).fetchall()
    except sqlite3.Error:
        return []
    history = []
    for row in rows:
        item = dict(row)
        item["diff"] = json.loads(item.pop("diff_json")) or []
        item["changed_paths"] = [entry["path"] for entry in item["diff"]]
        history.append(item)
    return history


def config_version_at(journal_path: str | Path, config: Mapping[str, Any]
                      ) -> str:
    """The version id this configuration has, without writing anything."""
    return _version_id(content_hash(redact(config)))


__all__ = [
    "CONFIG_AUDIT_SCHEMA", "PROMOTION_SCHEMA", "REDACTED", "GovernanceError",
    "canonical_json", "config_diff", "config_history", "config_version_at",
    "content_hash", "pinned_variant_ids", "promotion_snippet",
    "record_config_version", "redact", "validate_promotions",
]
