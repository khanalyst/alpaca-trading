"""Frozen paper-primary/shadow research epochs.

This module is deliberately independent from both the runtime journal and the
edge-research ledger.  A paper epoch is operational evidence: it can stop a
run, explain paper/shadow differences, and inform the *next* confirmation
epoch.  It is never alpha evidence and has no promotion authority.

The writer API is intentionally narrow.  Epochs and their complete cohorts are
created atomically, observations are appended as complete paired batches, and
all lifecycle changes are append-only audit events.  SQLite triggers prevent
ordinary mutation while :meth:`PaperEpochStore.verify_integrity` detects
missing guards, changed rows, incomplete batches, and a broken audit chain.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence
from uuid import uuid4


SCHEMA_VERSION = 1
DEFAULT_DB_PATH = Path(os.getenv(
    "ALPACA_PAPER_EPOCH_DB",
    str(Path(__file__).resolve().parents[1] /
        "runtime" / "research" / "paper_epochs.sqlite3"),
))

PAPER_PRIMARY = "paper_primary"
SHADOW = "shadow"
FILL = "fill"
REJECTION = "rejection"
OPERATIONAL_FAILURE = "operational_failure"
GENESIS_HASH = "0" * 64

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_FINGERPRINT_RE = re.compile(
    r"^(?P<scope>[a-z][a-z0-9._-]{1,62}):sha256:(?P<digest>[0-9a-f]{64})$")
_ALPACA_PAPER_RE = re.compile(r"^alpaca-paper-[0-9a-f]{20,64}$")
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")


class PaperEpochError(RuntimeError):
    """Base error for the frozen paper epoch control plane."""


class EpochValidationError(PaperEpochError, ValueError):
    """An epoch manifest, cohort, or observation is invalid."""


class EpochStateError(PaperEpochError):
    """The requested append is not valid in the epoch's current state."""


class IdentityError(PaperEpochError):
    """A supplied identity is secret-shaped, non-paper, or not frozen."""


class RuntimeAdaptationError(PaperEpochError):
    """Runtime LLM adaptation was not explicitly disabled."""


class OutcomeConflict(PaperEpochError):
    """An immutable opportunity was submitted with changed content."""


class IsolationError(PaperEpochError):
    """The configured SQLite path is not an isolated paper-epoch store."""


class IntegrityViolation(PaperEpochError):
    """Stored evidence failed structural or cryptographic verification."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise EpochValidationError("value must be finite JSON") from exc


def content_digest(value: Any) -> str:
    """Return a deterministic SHA-256 digest for a finite JSON value."""
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def identity_fingerprint(scope: str, public_identifier: str) -> str:
    """Create a non-secret identity fingerprint without persisting its input.

    ``public_identifier`` may be an account or process identifier held by the
    caller.  Only the domain-separated digest returned by this function should
    be passed to the store.
    """
    normalized_scope = str(scope).strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9._-]{1,62}", normalized_scope):
        raise IdentityError("identity scope must be a short safe token")
    if not isinstance(public_identifier, str) or not public_identifier:
        raise IdentityError("identity source is required")
    digest = hashlib.sha256(
        f"paper-epoch-identity\0{normalized_scope}\0{public_identifier}".encode(
            "utf-8")
    ).hexdigest()
    return f"{normalized_scope}:sha256:{digest}"


def _require_digest(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise EpochValidationError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _require_token(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_TOKEN_RE.fullmatch(value):
        raise EpochValidationError(f"{name} must be a non-empty safe token")
    return value


def _fingerprint_scope(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise IdentityError(f"{name} must be a non-secret fingerprint")
    match = _FINGERPRINT_RE.fullmatch(value)
    if match:
        return match.group("scope")
    if _ALPACA_PAPER_RE.fullmatch(value):
        return "alpaca-paper"
    raise IdentityError(
        f"{name} must be a scoped SHA-256 fingerprint, not a raw identifier")


def _finite(name: str, value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise EpochValidationError(f"{name} must be finite")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise EpochValidationError(f"{name} must be finite") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        qualifier = "positive and finite" if positive else "finite"
        raise EpochValidationError(f"{name} must be {qualifier}")
    return number


@dataclass(frozen=True)
class FrozenEpoch:
    """Inputs shared unchanged by every member of one realtime cohort."""

    realtime_stream_digest: str
    data_window_digest: str
    config_digest: str
    code_digest: str
    cost_digest: str
    risk_digest: str
    runtime_llm_adaptation: bool = False
    halt_on_operational_failure: bool = True

    def __post_init__(self) -> None:
        for name in ("realtime_stream_digest", "data_window_digest",
                     "config_digest", "code_digest", "cost_digest",
                     "risk_digest"):
            _require_digest(name, getattr(self, name))
        if self.runtime_llm_adaptation is not False:
            raise RuntimeAdaptationError(
                "runtime LLM adaptation must be explicitly disabled")
        if self.halt_on_operational_failure is not True:
            raise EpochValidationError(
                "operational failures must retain fail-closed halt authority")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "paper-frozen-epoch.v1",
            "realtime_stream_digest": self.realtime_stream_digest,
            "data_window_digest": self.data_window_digest,
            "config_digest": self.config_digest,
            "code_digest": self.code_digest,
            "cost_digest": self.cost_digest,
            "risk_digest": self.risk_digest,
            "runtime_llm_adaptation": False,
            "halt_on_operational_failure": True,
        }


# A descriptive alias for callers that use "manifest" terminology.
EpochManifest = FrozenEpoch


@dataclass(frozen=True)
class PaperRuntimeIdentity:
    """Non-secret paper account and process fingerprints."""

    account_fingerprint: str
    runtime_fingerprint: str
    runtime_mode: str = "paper"
    endpoint_kind: str = "paper"

    def __post_init__(self) -> None:
        account_scope = _fingerprint_scope(
            "account_fingerprint", self.account_fingerprint)
        runtime_scope = _fingerprint_scope(
            "runtime_fingerprint", self.runtime_fingerprint)
        if "paper" not in account_scope:
            raise IdentityError("the account fingerprint is not paper-scoped")
        if "paper" not in runtime_scope:
            raise IdentityError("the runtime fingerprint is not paper-scoped")
        if self.runtime_mode != "paper" or self.endpoint_kind != "paper":
            raise IdentityError("paper mode and paper endpoint are required")
        if self.account_fingerprint == self.runtime_fingerprint:
            raise IdentityError("account and runtime identities must be separate")

    def as_dict(self) -> dict[str, str]:
        return {
            "account_fingerprint": self.account_fingerprint,
            "runtime_fingerprint": self.runtime_fingerprint,
            "runtime_mode": "paper",
            "endpoint_kind": "paper",
        }


@dataclass(frozen=True)
class CohortMember:
    """One immutable paper-primary or broker-free shadow sibling."""

    member_id: str
    role: str
    runtime_fingerprint: str
    account_fingerprint: str | None = None
    runtime_mode: str | None = None

    def __post_init__(self) -> None:
        _require_token("member_id", self.member_id)
        scope = _fingerprint_scope(
            "runtime_fingerprint", self.runtime_fingerprint)
        if self.role == PAPER_PRIMARY:
            if self.account_fingerprint is None:
                raise IdentityError("paper primary requires an account fingerprint")
            PaperRuntimeIdentity(
                self.account_fingerprint, self.runtime_fingerprint,
                self.runtime_mode or "paper", "paper")
            if "paper" not in scope:
                raise IdentityError("primary runtime must be paper-scoped")
        elif self.role == SHADOW:
            if self.account_fingerprint is not None:
                raise IdentityError("shadow siblings must be broker-free")
            if (self.runtime_mode or "shadow") != "shadow" or "shadow" not in scope:
                raise IdentityError("shadow runtime must be shadow-scoped")
        else:
            raise EpochValidationError("role must be paper_primary or shadow")

    @classmethod
    def paper_primary(cls, member_id: str,
                      identity: PaperRuntimeIdentity) -> "CohortMember":
        return cls(member_id, PAPER_PRIMARY, identity.runtime_fingerprint,
                   identity.account_fingerprint, "paper")

    @classmethod
    def shadow_sibling(cls, member_id: str,
                       runtime_fingerprint: str) -> "CohortMember":
        return cls(member_id, SHADOW, runtime_fingerprint, None, "shadow")

    def as_dict(self) -> dict[str, Any]:
        return {
            "member_id": self.member_id,
            "role": self.role,
            "runtime_mode": "paper" if self.role == PAPER_PRIMARY else "shadow",
            "runtime_fingerprint": self.runtime_fingerprint,
            "account_fingerprint": self.account_fingerprint,
        }


@dataclass(frozen=True)
class RuntimeStartAttestation:
    """Observed runtime bindings checked immediately before an epoch starts."""

    primary_identity: PaperRuntimeIdentity
    member_runtime_fingerprints: Mapping[str, str]
    member_manifest_digests: Mapping[str, str]
    runtime_llm_adaptation: bool

    def __post_init__(self) -> None:
        if self.runtime_llm_adaptation is not False:
            raise RuntimeAdaptationError(
                "runtime LLM adaptation must be explicitly disabled at start")
        runtimes = dict(self.member_runtime_fingerprints)
        manifests = dict(self.member_manifest_digests)
        for member_id, fingerprint in runtimes.items():
            _require_token("member_id", member_id)
            _fingerprint_scope("runtime_fingerprint", fingerprint)
        for member_id, digest in manifests.items():
            _require_token("member_id", member_id)
            _require_digest("manifest_digest", digest)
        object.__setattr__(self, "member_runtime_fingerprints",
                           MappingProxyType(runtimes))
        object.__setattr__(self, "member_manifest_digests",
                           MappingProxyType(manifests))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "paper-runtime-start-attestation.v1",
            "primary_identity": self.primary_identity.as_dict(),
            "member_runtime_fingerprints": dict(
                self.member_runtime_fingerprints),
            "member_manifest_digests": dict(self.member_manifest_digests),
            "runtime_llm_adaptation": False,
        }


@dataclass(frozen=True)
class ConfirmationRestart:
    """Attestation that a successor restarts on a new, unseen data window."""

    predecessor_epoch_id: str
    restart_fingerprint: str
    unseen_data_digest: str
    unseen_data_confirmed: bool
    runtime_restarted: bool
    prior_epoch_data_excluded: bool

    def __post_init__(self) -> None:
        _require_token("predecessor_epoch_id", self.predecessor_epoch_id)
        scope = _fingerprint_scope(
            "restart_fingerprint", self.restart_fingerprint)
        if "restart" not in scope:
            raise EpochValidationError(
                "restart fingerprint must be restart-scoped")
        _require_digest("unseen_data_digest", self.unseen_data_digest)
        if (self.unseen_data_confirmed is not True or
                self.runtime_restarted is not True or
                self.prior_epoch_data_excluded is not True):
            raise EpochValidationError(
                "successor requires confirmed unseen data and a clean restart")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "paper-confirmation-restart.v1",
            "predecessor_epoch_id": self.predecessor_epoch_id,
            "restart_fingerprint": self.restart_fingerprint,
            "unseen_data_digest": self.unseen_data_digest,
            "unseen_data_confirmed": True,
            "runtime_restarted": True,
            "prior_epoch_data_excluded": True,
        }


UnseenDataConfirmation = ConfirmationRestart


@dataclass(frozen=True)
class ExecutionObservation:
    """One member's immutable result for a shared realtime opportunity."""

    member_id: str
    disposition: str
    operational_ok: bool = True
    quantity: float | None = None
    reference_price: float | None = None
    fill_price: float | None = None
    slippage_bps: float | None = None
    rejection_code: str | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        _require_token("member_id", self.member_id)
        if self.operational_ok is not True and self.operational_ok is not False:
            raise EpochValidationError("operational_ok must be boolean")
        if self.disposition == FILL:
            if self.operational_ok is not True:
                raise EpochValidationError(
                    "a fill cannot also be an operational failure")
            for name in ("quantity", "reference_price", "fill_price"):
                _finite(name, getattr(self, name), positive=True)
            _finite("slippage_bps", self.slippage_bps)
            if self.rejection_code is not None or self.failure_code is not None:
                raise EpochValidationError(
                    "fill observations cannot carry rejection/failure codes")
        elif self.disposition == REJECTION:
            if self.operational_ok is not True:
                raise EpochValidationError(
                    "use operational_failure for an unhealthy runtime")
            _require_token("rejection_code", self.rejection_code)
            if any(value is not None for value in (
                    self.quantity, self.reference_price, self.fill_price,
                    self.slippage_bps, self.failure_code)):
                raise EpochValidationError(
                    "rejections cannot carry fill or failure fields")
        elif self.disposition == OPERATIONAL_FAILURE:
            if self.operational_ok is not False:
                raise EpochValidationError(
                    "operational_failure must set operational_ok=False")
            _require_token("failure_code", self.failure_code)
            if any(value is not None for value in (
                    self.quantity, self.reference_price, self.fill_price,
                    self.slippage_bps, self.rejection_code)):
                raise EpochValidationError(
                    "operational failures cannot carry execution fields")
        else:
            raise EpochValidationError(
                "disposition must be fill, rejection, or operational_failure")

    def as_dict(self) -> dict[str, Any]:
        return {
            "member_id": self.member_id,
            "disposition": self.disposition,
            "operational_ok": self.operational_ok,
            "quantity": (float(self.quantity)
                         if self.quantity is not None else None),
            "reference_price": (float(self.reference_price)
                                if self.reference_price is not None else None),
            "fill_price": (float(self.fill_price)
                           if self.fill_price is not None else None),
            "slippage_bps": (float(self.slippage_bps)
                             if self.slippage_bps is not None else None),
            "rejection_code": self.rejection_code,
            "failure_code": self.failure_code,
        }


@dataclass(frozen=True)
class SealedLesson:
    """A bounded lesson eligible for exactly one successor epoch."""

    category: str
    statement: str

    def __post_init__(self) -> None:
        _require_token("lesson category", self.category)
        if (not isinstance(self.statement, str) or
                not self.statement.strip() or len(self.statement) > 8192):
            raise EpochValidationError(
                "lesson statement must contain 1..8192 characters")

    def as_dict(self) -> dict[str, str]:
        return {"category": self.category,
                "statement": self.statement.strip()}


_TABLES = frozenset({
    "paper_epoch_meta", "paper_epochs", "paper_cohort", "outcome_batches",
    "execution_observations", "operational_parity", "lesson_batches",
    "sealed_lessons", "lesson_visibility", "paper_epoch_audit",
})

_IMMUTABLE_TABLES = tuple(sorted(_TABLES))
_REQUIRED_TRIGGERS = frozenset(
    {f"{table}_no_update" for table in _IMMUTABLE_TABLES} |
    {f"{table}_no_delete" for table in _IMMUTABLE_TABLES} |
    {"paper_cohort_no_late_insert", "observations_no_late_insert",
     "parity_no_late_insert", "lessons_no_late_insert",
     "audit_sequence_is_contiguous"}
)


def _coerce_frozen(value: FrozenEpoch | Mapping[str, Any]) -> FrozenEpoch:
    return value if isinstance(value, FrozenEpoch) else FrozenEpoch(**dict(value))


def _coerce_member(value: CohortMember | Mapping[str, Any]) -> CohortMember:
    return value if isinstance(value, CohortMember) else CohortMember(**dict(value))


def _coerce_identity(
        value: PaperRuntimeIdentity | Mapping[str, Any]) -> PaperRuntimeIdentity:
    return (value if isinstance(value, PaperRuntimeIdentity)
            else PaperRuntimeIdentity(**dict(value)))


def _coerce_attestation(
        value: RuntimeStartAttestation | Mapping[str, Any]
) -> RuntimeStartAttestation:
    if isinstance(value, RuntimeStartAttestation):
        return value
    raw = dict(value)
    schema = raw.pop("schema", "paper-runtime-start-attestation.v1")
    if schema != "paper-runtime-start-attestation.v1":
        raise EpochValidationError("runtime start attestation schema is invalid")
    raw["primary_identity"] = _coerce_identity(raw["primary_identity"])
    return RuntimeStartAttestation(**raw)


def _coerce_confirmation(
        value: ConfirmationRestart | Mapping[str, Any]) -> ConfirmationRestart:
    if isinstance(value, ConfirmationRestart):
        return value
    raw = dict(value)
    schema = raw.pop("schema", "paper-confirmation-restart.v1")
    if schema != "paper-confirmation-restart.v1":
        raise EpochValidationError("confirmation restart schema is invalid")
    return ConfirmationRestart(**raw)


def _coerce_observation(
        value: ExecutionObservation | Mapping[str, Any]) -> ExecutionObservation:
    return (value if isinstance(value, ExecutionObservation)
            else ExecutionObservation(**dict(value)))


def _coerce_lesson(value: SealedLesson | Mapping[str, Any] | str) -> SealedLesson:
    if isinstance(value, SealedLesson):
        return value
    if isinstance(value, str):
        return SealedLesson("observation", value)
    return SealedLesson(**dict(value))


class PaperEpochStore:
    """Own an isolated, append-only paper epoch SQLite database."""

    def __init__(self, path: str | Path = DEFAULT_DB_PATH, *,
                 readonly: bool = False,
                 clock: Callable[[], float] | None = None):
        self.path = Path(path)
        self.readonly = bool(readonly)
        self._clock = clock or time.time
        self._check_namespace()
        if self.readonly:
            if not self.path.is_file():
                raise IsolationError(f"paper epoch store is unavailable: {self.path}")
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._init()
            try:
                self.path.chmod(0o600)
            except OSError as exc:
                raise IsolationError(
                    f"cannot secure paper epoch store: {self.path}") from exc

    def _check_namespace(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        try:
            with closing(sqlite3.connect(str(self.path), timeout=5)) as db:
                names = {str(row[0]) for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'")}
        except (OSError, sqlite3.Error) as exc:
            raise IsolationError(
                f"paper epoch store is not readable: {self.path}") from exc
        unknown = names - _TABLES
        if unknown:
            raise IsolationError(
                "paper epoch store must be separate; found foreign tables: " +
                ", ".join(sorted(unknown)))

    def _connect(self) -> sqlite3.Connection:
        if self.readonly:
            uri = f"file:{self.path.resolve()}?mode=ro"
            db = sqlite3.connect(uri, uri=True, timeout=30)
        else:
            db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        if not self.readonly:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def _write(self):
        if self.readonly:
            raise EpochStateError("paper epoch store is read-only")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _init(self) -> None:
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS paper_epoch_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_epochs (
                    epoch_id TEXT PRIMARY KEY,
                    ordinal INTEGER NOT NULL UNIQUE CHECK (ordinal > 0),
                    predecessor_epoch_id TEXT UNIQUE
                        REFERENCES paper_epochs(epoch_id),
                    realtime_stream_digest TEXT NOT NULL,
                    data_window_digest TEXT NOT NULL UNIQUE,
                    config_digest TEXT NOT NULL,
                    code_digest TEXT NOT NULL,
                    cost_digest TEXT NOT NULL,
                    risk_digest TEXT NOT NULL,
                    runtime_llm_adaptation INTEGER NOT NULL
                        CHECK (runtime_llm_adaptation = 0),
                    halt_on_operational_failure INTEGER NOT NULL
                        CHECK (halt_on_operational_failure = 1),
                    manifest_digest TEXT NOT NULL,
                    cohort_digest TEXT NOT NULL,
                    expected_members INTEGER NOT NULL CHECK (expected_members >= 2),
                    expected_shadows INTEGER NOT NULL CHECK (expected_shadows >= 1),
                    deployed_trader_account_fingerprint TEXT NOT NULL,
                    deployed_trader_runtime_fingerprint TEXT NOT NULL,
                    identity_attestation_digest TEXT NOT NULL,
                    restart_fingerprint TEXT UNIQUE,
                    confirmation_json TEXT,
                    confirmation_digest TEXT,
                    created_at REAL NOT NULL,
                    row_digest TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_cohort (
                    epoch_id TEXT NOT NULL REFERENCES paper_epochs(epoch_id),
                    member_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('paper_primary','shadow')),
                    runtime_mode TEXT NOT NULL CHECK (runtime_mode IN ('paper','shadow')),
                    runtime_fingerprint TEXT NOT NULL,
                    account_fingerprint TEXT,
                    manifest_digest TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    row_digest TEXT NOT NULL,
                    PRIMARY KEY(epoch_id, member_id),
                    UNIQUE(epoch_id, runtime_fingerprint)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS paper_cohort_one_primary
                    ON paper_cohort(epoch_id) WHERE role='paper_primary';
                CREATE TABLE IF NOT EXISTS outcome_batches (
                    batch_id TEXT PRIMARY KEY,
                    epoch_id TEXT NOT NULL REFERENCES paper_epochs(epoch_id),
                    opportunity_id TEXT NOT NULL,
                    stream_event_id TEXT NOT NULL,
                    observation_count INTEGER NOT NULL CHECK (observation_count >= 2),
                    batch_digest TEXT NOT NULL,
                    operational_parity INTEGER NOT NULL CHECK (operational_parity IN (0,1)),
                    operational_failure INTEGER NOT NULL CHECK (operational_failure IN (0,1)),
                    created_at REAL NOT NULL,
                    row_digest TEXT NOT NULL,
                    UNIQUE(epoch_id, opportunity_id)
                );
                CREATE TABLE IF NOT EXISTS execution_observations (
                    batch_id TEXT NOT NULL REFERENCES outcome_batches(batch_id),
                    epoch_id TEXT NOT NULL REFERENCES paper_epochs(epoch_id),
                    member_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('paper_primary','shadow')),
                    disposition TEXT NOT NULL CHECK (
                        disposition IN ('fill','rejection','operational_failure')),
                    operational_ok INTEGER NOT NULL CHECK (operational_ok IN (0,1)),
                    quantity REAL,
                    reference_price REAL,
                    fill_price REAL,
                    slippage_bps REAL,
                    rejection_code TEXT,
                    failure_code TEXT,
                    created_at REAL NOT NULL,
                    row_digest TEXT NOT NULL,
                    PRIMARY KEY(batch_id, member_id),
                    FOREIGN KEY(epoch_id, member_id)
                        REFERENCES paper_cohort(epoch_id, member_id)
                );
                CREATE TABLE IF NOT EXISTS operational_parity (
                    batch_id TEXT NOT NULL REFERENCES outcome_batches(batch_id),
                    epoch_id TEXT NOT NULL REFERENCES paper_epochs(epoch_id),
                    shadow_member_id TEXT NOT NULL,
                    disposition_match INTEGER NOT NULL CHECK (disposition_match IN (0,1)),
                    rejection_match INTEGER,
                    quantity_match INTEGER,
                    slippage_delta_bps REAL,
                    operational_match INTEGER NOT NULL CHECK (operational_match IN (0,1)),
                    created_at REAL NOT NULL,
                    row_digest TEXT NOT NULL,
                    PRIMARY KEY(batch_id, shadow_member_id),
                    FOREIGN KEY(epoch_id, shadow_member_id)
                        REFERENCES paper_cohort(epoch_id, member_id)
                );
                CREATE TABLE IF NOT EXISTS lesson_batches (
                    lesson_batch_id TEXT PRIMARY KEY,
                    source_epoch_id TEXT NOT NULL UNIQUE
                        REFERENCES paper_epochs(epoch_id),
                    lesson_count INTEGER NOT NULL CHECK (lesson_count >= 0),
                    batch_digest TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    row_digest TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sealed_lessons (
                    lesson_id TEXT PRIMARY KEY,
                    lesson_batch_id TEXT NOT NULL
                        REFERENCES lesson_batches(lesson_batch_id),
                    source_epoch_id TEXT NOT NULL REFERENCES paper_epochs(epoch_id),
                    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
                    category TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    row_digest TEXT NOT NULL,
                    UNIQUE(lesson_batch_id, ordinal)
                );
                CREATE TABLE IF NOT EXISTS lesson_visibility (
                    target_epoch_id TEXT NOT NULL REFERENCES paper_epochs(epoch_id),
                    source_epoch_id TEXT NOT NULL REFERENCES paper_epochs(epoch_id),
                    lesson_id TEXT NOT NULL UNIQUE REFERENCES sealed_lessons(lesson_id),
                    lesson_batch_id TEXT NOT NULL
                        REFERENCES lesson_batches(lesson_batch_id),
                    created_at REAL NOT NULL,
                    row_digest TEXT NOT NULL,
                    PRIMARY KEY(target_epoch_id, lesson_id)
                );
                CREATE TABLE IF NOT EXISTS paper_epoch_audit (
                    sequence INTEGER PRIMARY KEY CHECK (sequence > 0),
                    event_id TEXT NOT NULL UNIQUE,
                    epoch_id TEXT NOT NULL REFERENCES paper_epochs(epoch_id),
                    event_type TEXT NOT NULL CHECK (event_type IN (
                        'epoch_created','epoch_started','outcome_recorded',
                        'operational_stop','epoch_stopped','epoch_completed',
                        'lessons_sealed')),
                    payload_json TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS paper_audit_epoch
                    ON paper_epoch_audit(epoch_id, sequence);
                CREATE TRIGGER IF NOT EXISTS paper_cohort_no_late_insert
                    BEFORE INSERT ON paper_cohort
                    WHEN EXISTS (
                        SELECT 1 FROM paper_epoch_audit
                        WHERE epoch_id=NEW.epoch_id AND event_type='epoch_created')
                    BEGIN SELECT RAISE(ABORT, 'paper cohort is frozen'); END;
                CREATE TRIGGER IF NOT EXISTS observations_no_late_insert
                    BEFORE INSERT ON execution_observations
                    WHEN EXISTS (
                        SELECT 1 FROM paper_epoch_audit
                        WHERE event_type='outcome_recorded'
                          AND json_extract(payload_json, '$.batch_id')=NEW.batch_id)
                    BEGIN SELECT RAISE(ABORT, 'outcome batch is frozen'); END;
                CREATE TRIGGER IF NOT EXISTS parity_no_late_insert
                    BEFORE INSERT ON operational_parity
                    WHEN EXISTS (
                        SELECT 1 FROM paper_epoch_audit
                        WHERE event_type='outcome_recorded'
                          AND json_extract(payload_json, '$.batch_id')=NEW.batch_id)
                    BEGIN SELECT RAISE(ABORT, 'parity batch is frozen'); END;
                CREATE TRIGGER IF NOT EXISTS lessons_no_late_insert
                    BEFORE INSERT ON sealed_lessons
                    WHEN EXISTS (
                        SELECT 1 FROM paper_epoch_audit
                        WHERE epoch_id=NEW.source_epoch_id
                          AND event_type='lessons_sealed')
                    BEGIN SELECT RAISE(ABORT, 'lesson batch is sealed'); END;
                CREATE TRIGGER IF NOT EXISTS audit_sequence_is_contiguous
                    BEFORE INSERT ON paper_epoch_audit
                    WHEN NEW.sequence != (
                        SELECT COALESCE(MAX(sequence), 0) + 1
                        FROM paper_epoch_audit)
                    BEGIN SELECT RAISE(ABORT, 'audit sequence must be contiguous'); END;
            """)
            for table in _IMMUTABLE_TABLES:
                db.executescript(f"""
                    CREATE TRIGGER IF NOT EXISTS {table}_no_update
                    BEFORE UPDATE ON {table}
                    BEGIN SELECT RAISE(ABORT, '{table} rows are immutable'); END;
                    CREATE TRIGGER IF NOT EXISTS {table}_no_delete
                    BEFORE DELETE ON {table}
                    BEGIN SELECT RAISE(ABORT, '{table} rows are immutable'); END;
                """)
            rows = list(db.execute("SELECT key,value FROM paper_epoch_meta"))
            if not rows:
                db.execute(
                    "INSERT INTO paper_epoch_meta(key,value) VALUES(?,?)",
                    ("schema_version", str(SCHEMA_VERSION)))
            elif {row["key"]: row["value"] for row in rows} != {
                    "schema_version": str(SCHEMA_VERSION)}:
                raise IntegrityViolation("paper epoch schema metadata is invalid")

    @staticmethod
    def _row_payload(row: sqlite3.Row, fields: Sequence[str]) -> dict[str, Any]:
        return {field: row[field] for field in fields}

    def _append_audit(self, db: sqlite3.Connection, epoch_id: str,
                      event_type: str, payload: Mapping[str, Any],
                      created_at: float) -> dict[str, Any]:
        head = db.execute(
            "SELECT sequence,event_hash FROM paper_epoch_audit "
            "ORDER BY sequence DESC LIMIT 1").fetchone()
        sequence = 1 if head is None else int(head["sequence"]) + 1
        previous_hash = GENESIS_HASH if head is None else str(head["event_hash"])
        event_id = f"paper-event-{uuid4().hex}"
        payload_json = _canonical_json(dict(payload))
        semantic = {
            "sequence": sequence, "event_id": event_id,
            "epoch_id": epoch_id, "event_type": event_type,
            "payload_json": payload_json, "previous_hash": previous_hash,
            "created_at": created_at,
        }
        event_hash = content_digest(semantic)
        db.execute("""INSERT INTO paper_epoch_audit
            (sequence,event_id,epoch_id,event_type,payload_json,previous_hash,
             event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (sequence, event_id, epoch_id, event_type, payload_json,
             previous_hash, event_hash, created_at))
        return {**semantic, "event_hash": event_hash}

    @staticmethod
    def _members(db: sqlite3.Connection, epoch_id: str) -> list[sqlite3.Row]:
        return list(db.execute(
            "SELECT * FROM paper_cohort WHERE epoch_id=? ORDER BY member_id",
            (epoch_id,)))

    @staticmethod
    def _state(db: sqlite3.Connection, epoch_id: str) -> str:
        events = list(db.execute(
            "SELECT event_type FROM paper_epoch_audit WHERE epoch_id=? "
            "ORDER BY sequence", (epoch_id,)))
        if not events or events[0]["event_type"] != "epoch_created":
            raise IntegrityViolation("epoch has no valid creation event")
        state = "frozen"
        terminal = False
        lessons_seen = False
        for index, row in enumerate(events[1:], start=1):
            kind = str(row["event_type"])
            if kind == "epoch_started":
                if state != "frozen" or terminal:
                    raise IntegrityViolation("invalid epoch start sequence")
                state = "running"
            elif kind == "outcome_recorded":
                if state != "running" or terminal:
                    raise IntegrityViolation("outcome recorded outside running epoch")
            elif kind in {"operational_stop", "epoch_stopped"}:
                if state not in {"frozen", "running"} or terminal:
                    raise IntegrityViolation("invalid epoch stop sequence")
                state = "stopped"
                terminal = True
            elif kind == "epoch_completed":
                if state != "running" or terminal:
                    raise IntegrityViolation("invalid epoch completion sequence")
                state = "completed"
                terminal = True
            elif kind == "lessons_sealed":
                if not terminal or lessons_seen or index != len(events) - 1:
                    raise IntegrityViolation("invalid lesson sealing sequence")
                lessons_seen = True
            else:
                raise IntegrityViolation(f"unknown epoch event: {kind}")
        return state

    def create_epoch(
        self,
        frozen: FrozenEpoch | Mapping[str, Any],
        primary: CohortMember | Mapping[str, Any],
        shadows: Sequence[CohortMember | Mapping[str, Any]],
        *,
        predecessor_epoch_id: str | None = None,
        confirmation: ConfirmationRestart | Mapping[str, Any] | None = None,
        epoch_id: str | None = None,
        trader_account_fingerprint: str | None = None,
        trader_runtime_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Atomically freeze a complete one-primary/many-shadow cohort."""
        manifest = _coerce_frozen(frozen)
        paper = _coerce_member(primary)
        siblings = [_coerce_member(item) for item in shadows]
        if paper.role != PAPER_PRIMARY:
            raise EpochValidationError("primary must have paper_primary role")
        if not siblings or any(member.role != SHADOW for member in siblings):
            raise EpochValidationError("at least one shadow sibling is required")
        members = [paper, *siblings]
        member_ids = [member.member_id for member in members]
        runtime_ids = [member.runtime_fingerprint for member in members]
        if len(set(member_ids)) != len(member_ids):
            raise EpochValidationError("cohort member ids must be unique")
        if len(set(runtime_ids)) != len(runtime_ids):
            raise IdentityError("cohort runtime identities must be separate")
        if trader_account_fingerprint is None or trader_runtime_fingerprint is None:
            raise IdentityError(
                "deployed trader account and runtime fingerprints are required")
        _fingerprint_scope("trader_account_fingerprint",
                           trader_account_fingerprint)
        _fingerprint_scope("trader_runtime_fingerprint",
                           trader_runtime_fingerprint)
        if paper.account_fingerprint == trader_account_fingerprint:
            raise IdentityError(
                "paper-research and deployed-trader accounts must be separate")
        if paper.runtime_fingerprint == trader_runtime_fingerprint:
            raise IdentityError(
                "paper-research and deployed-trader runtimes must be separate")

        manifest_body = manifest.as_dict()
        manifest_digest = content_digest(manifest_body)
        member_bodies = [member.as_dict() for member in members]
        member_bodies.sort(key=lambda row: row["member_id"])
        cohort_digest = content_digest({
            "manifest_digest": manifest_digest, "members": member_bodies})
        identity_attestation_digest = content_digest({
            "paper_identity": {
                "account_fingerprint": paper.account_fingerprint,
                "runtime_fingerprint": paper.runtime_fingerprint,
                "runtime_mode": "paper", "endpoint_kind": "paper"},
            "member_runtime_fingerprints": sorted(runtime_ids),
            "deployed_trader_account_fingerprint": trader_account_fingerprint,
            "deployed_trader_runtime_fingerprint": trader_runtime_fingerprint,
            "raw_secrets_stored": False,
        })
        selected_id = epoch_id or f"paper-epoch-{uuid4().hex}"
        _require_token("epoch_id", selected_id)

        self.verify_integrity()
        created_at = float(self._clock())
        with self._write() as db:
            latest = db.execute(
                "SELECT * FROM paper_epochs ORDER BY ordinal DESC LIMIT 1"
            ).fetchone()
            restart: ConfirmationRestart | None = None
            if latest is None:
                if predecessor_epoch_id is not None or confirmation is not None:
                    raise EpochStateError(
                        "the first epoch cannot claim a predecessor")
                ordinal = 1
            else:
                latest_id = str(latest["epoch_id"])
                if predecessor_epoch_id != latest_id:
                    raise EpochStateError(
                        "a successor must name the immediately preceding epoch")
                if self._state(db, latest_id) not in {"stopped", "completed"}:
                    raise EpochStateError(
                        "the preceding epoch must be terminal")
                lesson_batch = db.execute(
                    "SELECT * FROM lesson_batches WHERE source_epoch_id=?",
                    (latest_id,)).fetchone()
                if lesson_batch is None:
                    raise EpochStateError(
                        "the preceding epoch must seal its lessons first")
                if confirmation is None:
                    raise EpochStateError(
                        "a successor requires an unseen-data confirmation restart")
                restart = _coerce_confirmation(confirmation)
                if restart.predecessor_epoch_id != latest_id:
                    raise EpochValidationError(
                        "restart predecessor does not match the epoch chain")
                if restart.unseen_data_digest != manifest.data_window_digest:
                    raise EpochValidationError(
                        "restart unseen data must match the frozen data window")
                if restart.unseen_data_digest == latest["data_window_digest"]:
                    raise EpochValidationError(
                        "confirmation must use a new unseen data window")
                ordinal = int(latest["ordinal"]) + 1

            confirmation_body = restart.as_dict() if restart else None
            confirmation_digest = (content_digest(confirmation_body)
                                   if confirmation_body else None)
            semantic_epoch = {
                "epoch_id": selected_id, "ordinal": ordinal,
                "predecessor_epoch_id": predecessor_epoch_id,
                **{key: manifest_body[key] for key in (
                    "realtime_stream_digest", "data_window_digest",
                    "config_digest", "code_digest", "cost_digest",
                    "risk_digest")},
                "runtime_llm_adaptation": 0,
                "halt_on_operational_failure": 1,
                "manifest_digest": manifest_digest,
                "cohort_digest": cohort_digest,
                "expected_members": len(members),
                "expected_shadows": len(siblings),
                "deployed_trader_account_fingerprint":
                    trader_account_fingerprint,
                "deployed_trader_runtime_fingerprint":
                    trader_runtime_fingerprint,
                "identity_attestation_digest": identity_attestation_digest,
                "restart_fingerprint": (restart.restart_fingerprint
                                        if restart else None),
                "confirmation_json": (_canonical_json(confirmation_body)
                                      if confirmation_body else None),
                "confirmation_digest": confirmation_digest,
                "created_at": created_at,
            }
            epoch_row_digest = content_digest(semantic_epoch)
            db.execute("""INSERT INTO paper_epochs
                (epoch_id,ordinal,predecessor_epoch_id,realtime_stream_digest,
                 data_window_digest,config_digest,code_digest,cost_digest,
                 risk_digest,runtime_llm_adaptation,halt_on_operational_failure,
                 manifest_digest,cohort_digest,expected_members,expected_shadows,
                 deployed_trader_account_fingerprint,
                 deployed_trader_runtime_fingerprint,
                 identity_attestation_digest,restart_fingerprint,
                 confirmation_json,confirmation_digest,created_at,row_digest)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (*semantic_epoch.values(), epoch_row_digest))

            for member in members:
                body = member.as_dict()
                semantic_member = {
                    "epoch_id": selected_id,
                    **body,
                    "manifest_digest": manifest_digest,
                    "created_at": created_at,
                }
                db.execute("""INSERT INTO paper_cohort
                    (epoch_id,member_id,role,runtime_mode,runtime_fingerprint,
                     account_fingerprint,manifest_digest,created_at,row_digest)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (*semantic_member.values(), content_digest(semantic_member)))

            visible_batch_digest = None
            if predecessor_epoch_id is not None:
                batch = db.execute(
                    "SELECT * FROM lesson_batches WHERE source_epoch_id=?",
                    (predecessor_epoch_id,)).fetchone()
                assert batch is not None
                visible_batch_digest = str(batch["batch_digest"])
                lessons = list(db.execute(
                    "SELECT * FROM sealed_lessons WHERE lesson_batch_id=? "
                    "ORDER BY ordinal", (batch["lesson_batch_id"],)))
                for lesson in lessons:
                    semantic_visibility = {
                        "target_epoch_id": selected_id,
                        "source_epoch_id": predecessor_epoch_id,
                        "lesson_id": str(lesson["lesson_id"]),
                        "lesson_batch_id": str(batch["lesson_batch_id"]),
                        "created_at": created_at,
                    }
                    db.execute("""INSERT INTO lesson_visibility
                        (target_epoch_id,source_epoch_id,lesson_id,
                         lesson_batch_id,created_at,row_digest)
                        VALUES(?,?,?,?,?,?)""",
                        (*semantic_visibility.values(),
                         content_digest(semantic_visibility)))

            self._append_audit(db, selected_id, "epoch_created", {
                "epoch_row_digest": epoch_row_digest,
                "manifest_digest": manifest_digest,
                "cohort_digest": cohort_digest,
                "member_count": len(members),
                "shadow_count": len(siblings),
                "predecessor_epoch_id": predecessor_epoch_id,
                "confirmation_digest": confirmation_digest,
                "visible_lesson_batch_digest": visible_batch_digest,
                "runtime_llm_adaptation": False,
                "paper_evidence_policy": "operational_only_no_alpha_no_promotion",
            }, created_at)
        return self.epoch(selected_id)

    def start_epoch(
        self,
        epoch_id: str,
        attestation: RuntimeStartAttestation | Mapping[str, Any],
    ) -> dict[str, Any]:
        """Start only after every frozen runtime and manifest is re-attested."""
        observed = _coerce_attestation(attestation)
        self.verify_integrity()
        created_at = float(self._clock())
        with self._write() as db:
            epoch = db.execute(
                "SELECT * FROM paper_epochs WHERE epoch_id=?", (epoch_id,)
            ).fetchone()
            if epoch is None:
                raise KeyError(epoch_id)
            if self._state(db, epoch_id) != "frozen":
                raise EpochStateError("only a frozen epoch can start")
            if int(epoch["runtime_llm_adaptation"]) != 0:
                raise RuntimeAdaptationError(
                    "stored epoch does not disable runtime adaptation")
            members = self._members(db, epoch_id)
            expected_runtimes = {
                str(row["member_id"]): str(row["runtime_fingerprint"])
                for row in members}
            expected_manifests = {
                str(row["member_id"]): str(row["manifest_digest"])
                for row in members}
            if dict(observed.member_runtime_fingerprints) != expected_runtimes:
                raise IdentityError("active runtime cohort differs from frozen cohort")
            if dict(observed.member_manifest_digests) != expected_manifests:
                raise EpochValidationError(
                    "one or more runtimes did not load the frozen manifest")
            primary = next(row for row in members
                           if row["role"] == PAPER_PRIMARY)
            if (observed.primary_identity.account_fingerprint !=
                    primary["account_fingerprint"] or
                    observed.primary_identity.runtime_fingerprint !=
                    primary["runtime_fingerprint"]):
                raise IdentityError(
                    "observed paper account/runtime identity is not frozen primary")
            body = observed.as_dict()
            self._append_audit(db, epoch_id, "epoch_started", {
                "attestation": body,
                "attestation_digest": content_digest(body),
                "runtime_llm_adaptation": False,
            }, created_at)
        return self.epoch(epoch_id)

    @staticmethod
    def _parity_body(paper: ExecutionObservation,
                     shadow: ExecutionObservation) -> dict[str, Any]:
        disposition_match = paper.disposition == shadow.disposition
        rejection_match: bool | None = None
        quantity_match: bool | None = None
        slippage_delta: float | None = None
        if paper.disposition == REJECTION and shadow.disposition == REJECTION:
            rejection_match = paper.rejection_code == shadow.rejection_code
        if paper.disposition == FILL and shadow.disposition == FILL:
            quantity_match = math.isclose(
                float(paper.quantity), float(shadow.quantity),
                rel_tol=1e-12, abs_tol=1e-12)
            slippage_delta = (float(paper.slippage_bps) -
                              float(shadow.slippage_bps))
        semantic_match = disposition_match
        if rejection_match is not None:
            semantic_match = semantic_match and rejection_match
        if quantity_match is not None:
            semantic_match = semantic_match and quantity_match
        operational_match = bool(
            paper.operational_ok and shadow.operational_ok and semantic_match)
        return {
            "disposition_match": disposition_match,
            "rejection_match": rejection_match,
            "quantity_match": quantity_match,
            "slippage_delta_bps": slippage_delta,
            "operational_match": operational_match,
        }

    def record_outcome(
        self,
        epoch_id: str,
        opportunity_id: str,
        stream_event_id: str,
        observations: Sequence[ExecutionObservation | Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Append one complete paper-versus-shadow operational comparison.

        Slippage is descriptive only.  A matched fill may have different paper
        and shadow slippage without failing operational parity; a missing fill,
        changed rejection, quantity mismatch, or unhealthy runtime does fail
        parity and automatically stops the epoch.
        """
        _require_token("opportunity_id", opportunity_id)
        _require_token("stream_event_id", stream_event_id)
        normalized = [_coerce_observation(item) for item in observations]
        ids = [item.member_id for item in normalized]
        if len(set(ids)) != len(ids):
            raise EpochValidationError(
                "each cohort member needs exactly one observation")
        observation_bodies = [item.as_dict() for item in normalized]
        observation_bodies.sort(key=lambda row: row["member_id"])
        batch_digest = content_digest({
            "epoch_id": epoch_id, "opportunity_id": opportunity_id,
            "stream_event_id": stream_event_id,
            "observations": observation_bodies,
        })

        self.verify_integrity()
        created_at = float(self._clock())
        batch_id: str
        with self._write() as db:
            epoch = db.execute(
                "SELECT * FROM paper_epochs WHERE epoch_id=?", (epoch_id,)
            ).fetchone()
            if epoch is None:
                raise KeyError(epoch_id)
            existing = db.execute(
                "SELECT * FROM outcome_batches WHERE epoch_id=? "
                "AND opportunity_id=?", (epoch_id, opportunity_id)).fetchone()
            if existing is not None:
                if existing["batch_digest"] != batch_digest:
                    raise OutcomeConflict(
                        "opportunity already has different immutable content")
                batch_id = str(existing["batch_id"])
                return self._outcome_from_connection(db, batch_id)
            if self._state(db, epoch_id) != "running":
                raise EpochStateError("outcomes require a running epoch")

            members = self._members(db, epoch_id)
            roles = {str(row["member_id"]): str(row["role"])
                     for row in members}
            if set(ids) != set(roles):
                raise EpochValidationError(
                    "outcome batch must cover the immutable cohort exactly")
            by_id = {item.member_id: item for item in normalized}
            paper_id = next(member_id for member_id, role in roles.items()
                            if role == PAPER_PRIMARY)
            paper = by_id[paper_id]
            parity_by_shadow = {
                member_id: self._parity_body(paper, by_id[member_id])
                for member_id, role in roles.items() if role == SHADOW}
            operational_parity = all(
                row["operational_match"] for row in parity_by_shadow.values())
            operational_failure = bool(
                not operational_parity or
                any(not item.operational_ok for item in normalized))
            batch_id = f"paper-outcome-{uuid4().hex}"
            semantic_batch = {
                "batch_id": batch_id, "epoch_id": epoch_id,
                "opportunity_id": opportunity_id,
                "stream_event_id": stream_event_id,
                "observation_count": len(normalized),
                "batch_digest": batch_digest,
                "operational_parity": int(operational_parity),
                "operational_failure": int(operational_failure),
                "created_at": created_at,
            }
            db.execute("""INSERT INTO outcome_batches
                (batch_id,epoch_id,opportunity_id,stream_event_id,
                 observation_count,batch_digest,operational_parity,
                 operational_failure,created_at,row_digest)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (*semantic_batch.values(), content_digest(semantic_batch)))

            for item in normalized:
                body = item.as_dict()
                semantic_observation = {
                    "batch_id": batch_id, "epoch_id": epoch_id,
                    "member_id": item.member_id,
                    "role": roles[item.member_id],
                    "disposition": body["disposition"],
                    "operational_ok": int(body["operational_ok"]),
                    "quantity": body["quantity"],
                    "reference_price": body["reference_price"],
                    "fill_price": body["fill_price"],
                    "slippage_bps": body["slippage_bps"],
                    "rejection_code": body["rejection_code"],
                    "failure_code": body["failure_code"],
                    "created_at": created_at,
                }
                db.execute("""INSERT INTO execution_observations
                    (batch_id,epoch_id,member_id,role,disposition,
                     operational_ok,quantity,reference_price,fill_price,
                     slippage_bps,rejection_code,failure_code,created_at,row_digest)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (*semantic_observation.values(),
                     content_digest(semantic_observation)))

            for shadow_id, parity in sorted(parity_by_shadow.items()):
                semantic_parity = {
                    "batch_id": batch_id, "epoch_id": epoch_id,
                    "shadow_member_id": shadow_id,
                    "disposition_match": int(parity["disposition_match"]),
                    "rejection_match": (None if parity["rejection_match"] is None
                                        else int(parity["rejection_match"])),
                    "quantity_match": (None if parity["quantity_match"] is None
                                       else int(parity["quantity_match"])),
                    "slippage_delta_bps": parity["slippage_delta_bps"],
                    "operational_match": int(parity["operational_match"]),
                    "created_at": created_at,
                }
                db.execute("""INSERT INTO operational_parity
                    (batch_id,epoch_id,shadow_member_id,disposition_match,
                     rejection_match,quantity_match,slippage_delta_bps,
                     operational_match,created_at,row_digest)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (*semantic_parity.values(), content_digest(semantic_parity)))

            self._append_audit(db, epoch_id, "outcome_recorded", {
                "batch_id": batch_id, "batch_digest": batch_digest,
                "opportunity_id": opportunity_id,
                "operational_parity": operational_parity,
                "operational_failure": operational_failure,
                "alpha_evidence_count": 0,
                "promotion_authority": False,
            }, created_at)
            if operational_failure and bool(
                    epoch["halt_on_operational_failure"]):
                self._append_audit(db, epoch_id, "operational_stop", {
                    "batch_id": batch_id,
                    "reason_code": "paper_shadow_operational_parity_failure",
                    "alpha_evidence_count": 0,
                    "promotion_authority": False,
                }, created_at)
            return self._outcome_from_connection(db, batch_id)

    def stop_epoch(self, epoch_id: str, reason_code: str) -> dict[str, Any]:
        """Append an operator/operational stop without editing prior evidence."""
        _require_token("reason_code", reason_code)
        self.verify_integrity()
        created_at = float(self._clock())
        with self._write() as db:
            if db.execute("SELECT 1 FROM paper_epochs WHERE epoch_id=?",
                          (epoch_id,)).fetchone() is None:
                raise KeyError(epoch_id)
            if self._state(db, epoch_id) not in {"frozen", "running"}:
                raise EpochStateError("epoch is already terminal")
            self._append_audit(db, epoch_id, "epoch_stopped", {
                "reason_code": reason_code,
                "alpha_evidence_count": 0,
                "promotion_authority": False,
            }, created_at)
        return self.epoch(epoch_id)

    def complete_epoch(self, epoch_id: str) -> dict[str, Any]:
        """Complete operational observation without creating alpha evidence."""
        self.verify_integrity()
        created_at = float(self._clock())
        with self._write() as db:
            if db.execute("SELECT 1 FROM paper_epochs WHERE epoch_id=?",
                          (epoch_id,)).fetchone() is None:
                raise KeyError(epoch_id)
            if self._state(db, epoch_id) != "running":
                raise EpochStateError("only a running epoch can complete")
            self._append_audit(db, epoch_id, "epoch_completed", {
                "operational_observation_complete": True,
                "paper_success_can_promote": False,
                "alpha_evidence_count": 0,
                "promotion_authority": False,
            }, created_at)
        return self.epoch(epoch_id)

    def seal_lessons(
        self,
        epoch_id: str,
        lessons: Sequence[SealedLesson | Mapping[str, Any] | str],
    ) -> dict[str, Any]:
        """Seal lessons after termination; expose them only to one successor."""
        normalized = [_coerce_lesson(item) for item in lessons]
        lesson_bodies = [item.as_dict() for item in normalized]
        batch_digest = content_digest({
            "source_epoch_id": epoch_id, "lessons": lesson_bodies})
        self.verify_integrity()
        created_at = float(self._clock())
        with self._write() as db:
            if db.execute("SELECT 1 FROM paper_epochs WHERE epoch_id=?",
                          (epoch_id,)).fetchone() is None:
                raise KeyError(epoch_id)
            if self._state(db, epoch_id) not in {"stopped", "completed"}:
                raise EpochStateError("lessons can be sealed only after termination")
            if db.execute(
                    "SELECT 1 FROM lesson_batches WHERE source_epoch_id=?",
                    (epoch_id,)).fetchone() is not None:
                raise EpochStateError("epoch lessons are already sealed")
            batch_id = f"paper-lessons-{uuid4().hex}"
            semantic_batch = {
                "lesson_batch_id": batch_id, "source_epoch_id": epoch_id,
                "lesson_count": len(normalized), "batch_digest": batch_digest,
                "created_at": created_at,
            }
            db.execute("""INSERT INTO lesson_batches
                (lesson_batch_id,source_epoch_id,lesson_count,batch_digest,
                 created_at,row_digest) VALUES(?,?,?,?,?,?)""",
                (*semantic_batch.values(), content_digest(semantic_batch)))
            for index, lesson in enumerate(normalized):
                lesson_id = f"paper-lesson-{uuid4().hex}"
                body = lesson.as_dict()
                semantic_lesson = {
                    "lesson_id": lesson_id, "lesson_batch_id": batch_id,
                    "source_epoch_id": epoch_id, "ordinal": index,
                    "category": body["category"],
                    "statement": body["statement"], "created_at": created_at,
                }
                db.execute("""INSERT INTO sealed_lessons
                    (lesson_id,lesson_batch_id,source_epoch_id,ordinal,category,
                     statement,created_at,row_digest) VALUES(?,?,?,?,?,?,?,?)""",
                    (*semantic_lesson.values(), content_digest(semantic_lesson)))
            self._append_audit(db, epoch_id, "lessons_sealed", {
                "lesson_batch_id": batch_id,
                "lesson_count": len(normalized),
                "batch_digest": batch_digest,
                "visible_in_source_epoch": False,
                "visibility_policy": "immediate_successor_only_after_unseen_restart",
            }, created_at)
        return {
            "schema": "paper-sealed-lessons.v1",
            "source_epoch_id": epoch_id,
            "lesson_batch_id": batch_id,
            "lesson_count": len(normalized),
            "batch_digest": batch_digest,
            "visible_in_source_epoch": False,
        }

    def epoch(self, epoch_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM paper_epochs WHERE epoch_id=?",
                             (epoch_id,)).fetchone()
            if row is None:
                raise KeyError(epoch_id)
            members = self._members(db, epoch_id)
            status = self._state(db, epoch_id)
            return {
                "schema": "paper-epoch.v1",
                "epoch_id": epoch_id, "ordinal": int(row["ordinal"]),
                "predecessor_epoch_id": row["predecessor_epoch_id"],
                "status": status,
                "manifest": {
                    "realtime_stream_digest": row["realtime_stream_digest"],
                    "data_window_digest": row["data_window_digest"],
                    "config_digest": row["config_digest"],
                    "code_digest": row["code_digest"],
                    "cost_digest": row["cost_digest"],
                    "risk_digest": row["risk_digest"],
                    "runtime_llm_adaptation": False,
                    "halt_on_operational_failure": True,
                },
                "manifest_digest": row["manifest_digest"],
                "cohort_digest": row["cohort_digest"],
                "cohort": [dict(member) for member in members],
                "isolation": {
                    "deployed_trader_account_fingerprint":
                        row["deployed_trader_account_fingerprint"],
                    "deployed_trader_runtime_fingerprint":
                        row["deployed_trader_runtime_fingerprint"],
                    "separate_account": True,
                    "separate_runtime": True,
                    "separate_outcome_store": str(self.path),
                },
                "policy": {
                    "paper_success_can_promote": False,
                    "paper_counts_as_alpha": False,
                    "alpha_evidence_count": 0,
                    "promotion_authority": False,
                },
            }

    def epochs(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            ids = [str(row[0]) for row in db.execute(
                "SELECT epoch_id FROM paper_epochs ORDER BY ordinal")]
        return [self.epoch(epoch_id) for epoch_id in ids]

    @staticmethod
    def _outcome_from_connection(db: sqlite3.Connection,
                                 batch_id: str) -> dict[str, Any]:
        batch = db.execute(
            "SELECT * FROM outcome_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise KeyError(batch_id)
        observations = [dict(row) for row in db.execute(
            "SELECT * FROM execution_observations WHERE batch_id=? "
            "ORDER BY role,member_id", (batch_id,))]
        parity = [dict(row) for row in db.execute(
            "SELECT * FROM operational_parity WHERE batch_id=? "
            "ORDER BY shadow_member_id", (batch_id,))]
        return {
            "schema": "paper-shadow-outcome.v1",
            "batch_id": batch_id, "epoch_id": batch["epoch_id"],
            "opportunity_id": batch["opportunity_id"],
            "stream_event_id": batch["stream_event_id"],
            "batch_digest": batch["batch_digest"],
            "operational_parity": bool(batch["operational_parity"]),
            "operational_failure": bool(batch["operational_failure"]),
            "observations": observations, "shadow_parity": parity,
            "alpha_evidence_count": 0,
            "promotion_authority": False,
        }

    def outcome(self, epoch_id: str, opportunity_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT batch_id FROM outcome_batches WHERE epoch_id=? "
                "AND opportunity_id=?", (epoch_id, opportunity_id)).fetchone()
            if row is None:
                raise KeyError((epoch_id, opportunity_id))
            return self._outcome_from_connection(db, str(row["batch_id"]))

    def outcomes(self, epoch_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            ids = [str(row[0]) for row in db.execute(
                "SELECT batch_id FROM outcome_batches WHERE epoch_id=? "
                "ORDER BY created_at,batch_id", (epoch_id,))]
            return [self._outcome_from_connection(db, batch_id)
                    for batch_id in ids]

    def operational_summary(self, epoch_id: str) -> dict[str, Any]:
        """Report paper execution quality without alpha/promotion semantics."""
        with closing(self._connect()) as db:
            if db.execute("SELECT 1 FROM paper_epochs WHERE epoch_id=?",
                          (epoch_id,)).fetchone() is None:
                raise KeyError(epoch_id)
            primary = db.execute(
                "SELECT member_id FROM paper_cohort WHERE epoch_id=? "
                "AND role='paper_primary'", (epoch_id,)).fetchone()
            assert primary is not None
            rows = list(db.execute("""SELECT o.* FROM execution_observations o
                JOIN outcome_batches b ON b.batch_id=o.batch_id
                WHERE b.epoch_id=? AND o.member_id=?""",
                (epoch_id, primary["member_id"])))
            batch_totals = db.execute("""SELECT count(*) AS total,
                COALESCE(sum(CASE WHEN operational_parity=0 THEN 1 ELSE 0 END),0)
                    AS parity_failures,
                COALESCE(sum(operational_failure),0) AS failures
                FROM outcome_batches WHERE epoch_id=?""", (epoch_id,)).fetchone()
            slippages = [float(row["slippage_bps"]) for row in rows
                         if row["disposition"] == FILL]
            return {
                "schema": "paper-operational-summary.v1",
                "epoch_id": epoch_id, "status": self._state(db, epoch_id),
                "outcomes": int(batch_totals["total"]),
                "paper_fills": sum(row["disposition"] == FILL for row in rows),
                "paper_rejections": sum(
                    row["disposition"] == REJECTION for row in rows),
                "mean_paper_slippage_bps": (
                    sum(slippages) / len(slippages) if slippages else None),
                "parity_failures": int(batch_totals["parity_failures"]),
                "operational_failures": int(batch_totals["failures"]),
                "paper_success_can_promote": False,
                "paper_counts_as_alpha": False,
                "alpha_evidence_count": 0,
                "alpha_contribution": 0.0,
                "promotion_authority": False,
            }

    def lessons_for_epoch(self, epoch_id: str) -> list[dict[str, Any]]:
        """Return only lessons explicitly bound to this immediate successor."""
        with closing(self._connect()) as db:
            if db.execute("SELECT 1 FROM paper_epochs WHERE epoch_id=?",
                          (epoch_id,)).fetchone() is None:
                raise KeyError(epoch_id)
            return [dict(row) for row in db.execute("""
                SELECT l.lesson_id,l.source_epoch_id,l.category,l.statement,
                       v.target_epoch_id,b.batch_digest
                FROM lesson_visibility v
                JOIN sealed_lessons l ON l.lesson_id=v.lesson_id
                JOIN lesson_batches b ON b.lesson_batch_id=v.lesson_batch_id
                WHERE v.target_epoch_id=? ORDER BY l.ordinal
            """, (epoch_id,))]

    @staticmethod
    def _verify_row_digest(row: sqlite3.Row, fields: Sequence[str],
                           label: str) -> None:
        semantic = {field: row[field] for field in fields}
        if content_digest(semantic) != row["row_digest"]:
            raise IntegrityViolation(f"{label} row digest mismatch")

    def verify_integrity(self) -> dict[str, Any]:
        """Recompute the ledger invariants and global append-only hash chain."""
        try:
            with closing(self._connect()) as db:
                tables = {str(row[0]) for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'")}
                if tables != _TABLES:
                    raise IntegrityViolation(
                        "paper epoch table namespace changed")
                triggers = {str(row[0]) for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'")}
                missing = _REQUIRED_TRIGGERS - triggers
                if missing:
                    raise IntegrityViolation(
                        "paper epoch immutability guards are missing: " +
                        ", ".join(sorted(missing)))
                meta = {str(row[0]): str(row[1]) for row in db.execute(
                    "SELECT key,value FROM paper_epoch_meta")}
                if meta != {"schema_version": str(SCHEMA_VERSION)}:
                    raise IntegrityViolation("paper epoch schema metadata changed")

                audit = list(db.execute(
                    "SELECT * FROM paper_epoch_audit ORDER BY sequence"))
                previous_hash = GENESIS_HASH
                for expected_sequence, event in enumerate(audit, start=1):
                    if int(event["sequence"]) != expected_sequence:
                        raise IntegrityViolation("audit sequence has a gap")
                    if event["previous_hash"] != previous_hash:
                        raise IntegrityViolation("audit chain predecessor mismatch")
                    try:
                        json.loads(event["payload_json"])
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise IntegrityViolation("audit payload is invalid JSON") from exc
                    semantic = {
                        "sequence": int(event["sequence"]),
                        "event_id": event["event_id"],
                        "epoch_id": event["epoch_id"],
                        "event_type": event["event_type"],
                        "payload_json": event["payload_json"],
                        "previous_hash": event["previous_hash"],
                        "created_at": event["created_at"],
                    }
                    if content_digest(semantic) != event["event_hash"]:
                        raise IntegrityViolation("audit event hash mismatch")
                    previous_hash = str(event["event_hash"])

                epochs = list(db.execute(
                    "SELECT * FROM paper_epochs ORDER BY ordinal"))
                if bool(epochs) != bool(audit):
                    raise IntegrityViolation("epochs and audit events disagree")
                prior_id: str | None = None
                data_windows: set[str] = set()
                outcome_count = 0
                lesson_count = 0
                visibility_count = 0
                for ordinal, epoch in enumerate(epochs, start=1):
                    epoch_id = str(epoch["epoch_id"])
                    if int(epoch["ordinal"]) != ordinal:
                        raise IntegrityViolation("epoch ordinals are not contiguous")
                    if epoch["predecessor_epoch_id"] != prior_id:
                        raise IntegrityViolation("epoch predecessor chain changed")
                    if epoch["data_window_digest"] in data_windows:
                        raise IntegrityViolation("an epoch reused a data window")
                    data_windows.add(str(epoch["data_window_digest"]))
                    if int(epoch["runtime_llm_adaptation"]) != 0:
                        raise RuntimeAdaptationError(
                            "stored epoch enables runtime LLM adaptation")
                    if int(epoch["halt_on_operational_failure"]) != 1:
                        raise IntegrityViolation(
                            "stored epoch removed operational halt authority")
                    epoch_fields = (
                        "epoch_id", "ordinal", "predecessor_epoch_id",
                        "realtime_stream_digest", "data_window_digest",
                        "config_digest", "code_digest", "cost_digest",
                        "risk_digest", "runtime_llm_adaptation",
                        "halt_on_operational_failure", "manifest_digest",
                        "cohort_digest", "expected_members", "expected_shadows",
                        "deployed_trader_account_fingerprint",
                        "deployed_trader_runtime_fingerprint",
                        "identity_attestation_digest", "restart_fingerprint",
                        "confirmation_json", "confirmation_digest", "created_at")
                    self._verify_row_digest(epoch, epoch_fields, "epoch")
                    frozen = FrozenEpoch(
                        epoch["realtime_stream_digest"],
                        epoch["data_window_digest"], epoch["config_digest"],
                        epoch["code_digest"], epoch["cost_digest"],
                        epoch["risk_digest"], False, True)
                    if content_digest(frozen.as_dict()) != epoch["manifest_digest"]:
                        raise IntegrityViolation("epoch manifest digest mismatch")

                    members = self._members(db, epoch_id)
                    if len(members) != int(epoch["expected_members"]):
                        raise IntegrityViolation("cohort member count changed")
                    primaries = [row for row in members
                                 if row["role"] == PAPER_PRIMARY]
                    shadows = [row for row in members if row["role"] == SHADOW]
                    if len(primaries) != 1 or len(shadows) < 1 or len(shadows) != int(
                            epoch["expected_shadows"]):
                        raise IntegrityViolation(
                            "cohort must contain one primary and shadow siblings")
                    member_bodies: list[dict[str, Any]] = []
                    for member in members:
                        member_fields = (
                            "epoch_id", "member_id", "role", "runtime_mode",
                            "runtime_fingerprint", "account_fingerprint",
                            "manifest_digest", "created_at")
                        self._verify_row_digest(member, member_fields, "cohort")
                        body = {
                            "member_id": member["member_id"],
                            "role": member["role"],
                            "runtime_mode": member["runtime_mode"],
                            "runtime_fingerprint": member["runtime_fingerprint"],
                            "account_fingerprint": member["account_fingerprint"],
                        }
                        _coerce_member(body)
                        if member["manifest_digest"] != epoch["manifest_digest"]:
                            raise IntegrityViolation(
                                "cohort member manifest is not frozen")
                        member_bodies.append(body)
                    member_bodies.sort(key=lambda row: row["member_id"])
                    if content_digest({
                            "manifest_digest": epoch["manifest_digest"],
                            "members": member_bodies}) != epoch["cohort_digest"]:
                        raise IntegrityViolation("cohort digest mismatch")
                    expected_identity_digest = content_digest({
                        "paper_identity": {
                            "account_fingerprint":
                                primaries[0]["account_fingerprint"],
                            "runtime_fingerprint":
                                primaries[0]["runtime_fingerprint"],
                            "runtime_mode": "paper", "endpoint_kind": "paper"},
                        "member_runtime_fingerprints": sorted(
                            str(row["runtime_fingerprint"]) for row in members),
                        "deployed_trader_account_fingerprint":
                            epoch["deployed_trader_account_fingerprint"],
                        "deployed_trader_runtime_fingerprint":
                            epoch["deployed_trader_runtime_fingerprint"],
                        "raw_secrets_stored": False,
                    })
                    if expected_identity_digest != epoch[
                            "identity_attestation_digest"]:
                        raise IntegrityViolation(
                            "paper account/runtime identity attestation changed")

                    creation_events = [event for event in audit
                                       if event["epoch_id"] == epoch_id and
                                       event["event_type"] == "epoch_created"]
                    if len(creation_events) != 1:
                        raise IntegrityViolation(
                            "epoch must have one creation audit event")
                    creation = json.loads(creation_events[0]["payload_json"])
                    if (creation.get("epoch_row_digest") != epoch["row_digest"] or
                            creation.get("manifest_digest") !=
                            epoch["manifest_digest"] or
                            creation.get("cohort_digest") != epoch["cohort_digest"] or
                            creation.get("member_count") != len(members) or
                            creation.get("shadow_count") != len(shadows) or
                            creation.get("predecessor_epoch_id") != prior_id or
                            creation.get("confirmation_digest") !=
                            epoch["confirmation_digest"] or
                            creation.get("runtime_llm_adaptation") is not False or
                            creation.get("paper_evidence_policy") !=
                            "operational_only_no_alpha_no_promotion"):
                        raise IntegrityViolation("epoch creation audit mismatch")

                    epoch_events = [event for event in audit
                                    if event["epoch_id"] == epoch_id]
                    state = self._state(db, epoch_id)
                    starts = [event for event in epoch_events
                              if event["event_type"] == "epoch_started"]
                    if len(starts) > 1:
                        raise IntegrityViolation("epoch has multiple starts")
                    if starts:
                        payload = json.loads(starts[0]["payload_json"])
                        attestation_body = payload.get("attestation")
                        if (not isinstance(attestation_body, dict) or
                                content_digest(attestation_body) !=
                                payload.get("attestation_digest") or
                                payload.get("runtime_llm_adaptation") is not False):
                            raise IntegrityViolation("start attestation changed")
                        attestation = _coerce_attestation(attestation_body)
                        expected_runtimes = {
                            str(row["member_id"]): str(row["runtime_fingerprint"])
                            for row in members}
                        expected_manifests = {
                            str(row["member_id"]): str(row["manifest_digest"])
                            for row in members}
                        if (dict(attestation.member_runtime_fingerprints) !=
                                expected_runtimes or
                                dict(attestation.member_manifest_digests) !=
                                expected_manifests):
                            raise IntegrityViolation(
                                "start attestation does not match cohort")
                        if (attestation.primary_identity.account_fingerprint !=
                                primaries[0]["account_fingerprint"] or
                                attestation.primary_identity.runtime_fingerprint !=
                                primaries[0]["runtime_fingerprint"]):
                            raise IntegrityViolation(
                                "start paper identity does not match primary")

                    batches = list(db.execute(
                        "SELECT * FROM outcome_batches WHERE epoch_id=?",
                        (epoch_id,)))
                    outcome_events = [event for event in epoch_events
                                      if event["event_type"] ==
                                      "outcome_recorded"]
                    if len(outcome_events) != len(batches):
                        raise IntegrityViolation(
                            "outcome audit count differs from stored batches")
                    expected_operational_stops: set[str] = set()
                    for batch in batches:
                        outcome_count += 1
                        batch_fields = (
                            "batch_id", "epoch_id", "opportunity_id",
                            "stream_event_id", "observation_count",
                            "batch_digest", "operational_parity",
                            "operational_failure", "created_at")
                        self._verify_row_digest(batch, batch_fields,
                                                "outcome batch")
                        observations = list(db.execute(
                            "SELECT * FROM execution_observations "
                            "WHERE batch_id=? ORDER BY member_id",
                            (batch["batch_id"],)))
                        if (len(observations) != len(members) or
                                len(observations) != batch["observation_count"] or
                                {row["member_id"] for row in observations} !=
                                {row["member_id"] for row in members}):
                            raise IntegrityViolation(
                                "outcome does not cover immutable cohort")
                        observation_bodies = []
                        observation_objects: dict[str, ExecutionObservation] = {}
                        for observation in observations:
                            observation_fields = (
                                "batch_id", "epoch_id", "member_id", "role",
                                "disposition", "operational_ok", "quantity",
                                "reference_price", "fill_price", "slippage_bps",
                                "rejection_code", "failure_code", "created_at")
                            self._verify_row_digest(
                                observation, observation_fields, "observation")
                            body = {
                                "member_id": observation["member_id"],
                                "disposition": observation["disposition"],
                                "operational_ok": bool(
                                    observation["operational_ok"]),
                                "quantity": observation["quantity"],
                                "reference_price": observation["reference_price"],
                                "fill_price": observation["fill_price"],
                                "slippage_bps": observation["slippage_bps"],
                                "rejection_code": observation["rejection_code"],
                                "failure_code": observation["failure_code"],
                            }
                            item = _coerce_observation(body)
                            observation_objects[item.member_id] = item
                            observation_bodies.append(item.as_dict())
                        observation_bodies.sort(
                            key=lambda row: row["member_id"])
                        expected_batch_digest = content_digest({
                            "epoch_id": epoch_id,
                            "opportunity_id": batch["opportunity_id"],
                            "stream_event_id": batch["stream_event_id"],
                            "observations": observation_bodies,
                        })
                        if expected_batch_digest != batch["batch_digest"]:
                            raise IntegrityViolation("outcome batch digest mismatch")
                        paper_id = str(primaries[0]["member_id"])
                        parity_rows = list(db.execute(
                            "SELECT * FROM operational_parity WHERE batch_id=?",
                            (batch["batch_id"],)))
                        if {row["shadow_member_id"] for row in parity_rows} != {
                                row["member_id"] for row in shadows}:
                            raise IntegrityViolation(
                                "shadow parity rows do not match cohort")
                        parity_matches = []
                        for parity in parity_rows:
                            parity_fields = (
                                "batch_id", "epoch_id", "shadow_member_id",
                                "disposition_match", "rejection_match",
                                "quantity_match", "slippage_delta_bps",
                                "operational_match", "created_at")
                            self._verify_row_digest(parity, parity_fields,
                                                    "parity")
                            expected = self._parity_body(
                                observation_objects[paper_id],
                                observation_objects[
                                    str(parity["shadow_member_id"])])
                            actual = {
                                "disposition_match": bool(
                                    parity["disposition_match"]),
                                "rejection_match": (
                                    None if parity["rejection_match"] is None
                                    else bool(parity["rejection_match"])),
                                "quantity_match": (
                                    None if parity["quantity_match"] is None
                                    else bool(parity["quantity_match"])),
                                "slippage_delta_bps": parity[
                                    "slippage_delta_bps"],
                                "operational_match": bool(
                                    parity["operational_match"]),
                            }
                            if actual != expected:
                                raise IntegrityViolation(
                                    "operational parity calculation changed")
                            parity_matches.append(expected["operational_match"])
                        overall = all(parity_matches)
                        failure = (not overall or any(
                            not item.operational_ok
                            for item in observation_objects.values()))
                        if (bool(batch["operational_parity"]) != overall or
                                bool(batch["operational_failure"]) != failure):
                            raise IntegrityViolation(
                                "outcome operational classification changed")
                        events = [event for event in epoch_events
                                  if event["event_type"] == "outcome_recorded" and
                                  json.loads(event["payload_json"]).get("batch_id") ==
                                  batch["batch_id"]]
                        if len(events) != 1:
                            raise IntegrityViolation(
                                "outcome must have one audit event")
                        payload = json.loads(events[0]["payload_json"])
                        if (payload.get("batch_digest") != batch["batch_digest"] or
                                payload.get("alpha_evidence_count") != 0 or
                                payload.get("promotion_authority") is not False):
                            raise IntegrityViolation(
                                "outcome audit policy changed")
                        if failure:
                            expected_operational_stops.add(str(batch["batch_id"]))
                            stops = [event for event in epoch_events
                                     if event["event_type"] == "operational_stop" and
                                     json.loads(event["payload_json"]).get(
                                         "batch_id") == batch["batch_id"]]
                            if len(stops) != 1:
                                raise IntegrityViolation(
                                    "operational failure did not stop epoch")
                    actual_operational_stops = {
                        str(json.loads(event["payload_json"]).get("batch_id"))
                        for event in epoch_events
                        if event["event_type"] == "operational_stop"}
                    if actual_operational_stops != expected_operational_stops:
                        raise IntegrityViolation(
                            "operational stop does not match a failed batch")

                    lesson_batch = db.execute(
                        "SELECT * FROM lesson_batches WHERE source_epoch_id=?",
                        (epoch_id,)).fetchone()
                    lessons: list[sqlite3.Row] = []
                    if lesson_batch is not None:
                        batch_fields = (
                            "lesson_batch_id", "source_epoch_id",
                            "lesson_count", "batch_digest", "created_at")
                        self._verify_row_digest(lesson_batch, batch_fields,
                                                "lesson batch")
                        lessons = list(db.execute(
                            "SELECT * FROM sealed_lessons "
                            "WHERE lesson_batch_id=? ORDER BY ordinal",
                            (lesson_batch["lesson_batch_id"],)))
                        if len(lessons) != int(lesson_batch["lesson_count"]):
                            raise IntegrityViolation("lesson count changed")
                        bodies = []
                        for index, lesson in enumerate(lessons):
                            lesson_count += 1
                            lesson_fields = (
                                "lesson_id", "lesson_batch_id",
                                "source_epoch_id", "ordinal", "category",
                                "statement", "created_at")
                            self._verify_row_digest(lesson, lesson_fields,
                                                    "lesson")
                            if int(lesson["ordinal"]) != index:
                                raise IntegrityViolation(
                                    "lesson ordinals are not contiguous")
                            bodies.append({
                                "category": lesson["category"],
                                "statement": lesson["statement"]})
                        expected = content_digest({
                            "source_epoch_id": epoch_id, "lessons": bodies})
                        if expected != lesson_batch["batch_digest"]:
                            raise IntegrityViolation("lesson batch digest mismatch")
                        seal_events = [event for event in epoch_events
                                       if event["event_type"] == "lessons_sealed"]
                        if len(seal_events) != 1:
                            raise IntegrityViolation(
                                "lesson batch must have one seal event")
                        seal_payload = json.loads(
                            seal_events[0]["payload_json"])
                        if (seal_payload.get("batch_digest") != expected or
                                seal_payload.get("visible_in_source_epoch") is not
                                False):
                            raise IntegrityViolation(
                                "lesson seal audit mismatch")
                    elif any(event["event_type"] == "lessons_sealed"
                             for event in epoch_events):
                        raise IntegrityViolation(
                            "lesson seal event has no immutable lesson batch")
                    if ordinal < len(epochs) and lesson_batch is None:
                        raise IntegrityViolation(
                            "successor exists before predecessor lessons sealed")

                    if prior_id is None:
                        if (epoch["confirmation_json"] is not None or
                                epoch["confirmation_digest"] is not None or
                                epoch["restart_fingerprint"] is not None):
                            raise IntegrityViolation(
                                "first epoch has a restart confirmation")
                        if db.execute(
                                "SELECT 1 FROM lesson_visibility "
                                "WHERE target_epoch_id=? LIMIT 1",
                                (epoch_id,)).fetchone() is not None:
                            raise IntegrityViolation(
                                "lessons cannot be visible in the first epoch")
                    else:
                        try:
                            confirmation_body = json.loads(
                                epoch["confirmation_json"])
                            confirmation = _coerce_confirmation(
                                confirmation_body)
                        except (TypeError, json.JSONDecodeError,
                                PaperEpochError) as exc:
                            raise IntegrityViolation(
                                "successor restart confirmation is invalid") from exc
                        if (confirmation.predecessor_epoch_id != prior_id or
                                confirmation.unseen_data_digest !=
                                epoch["data_window_digest"] or
                                content_digest(confirmation_body) !=
                                epoch["confirmation_digest"] or
                                confirmation.restart_fingerprint !=
                                epoch["restart_fingerprint"]):
                            raise IntegrityViolation(
                                "successor confirmation does not match epoch")
                        source_batch = db.execute(
                            "SELECT * FROM lesson_batches WHERE source_epoch_id=?",
                            (prior_id,)).fetchone()
                        assert source_batch is not None
                        visible = list(db.execute(
                            "SELECT * FROM lesson_visibility "
                            "WHERE target_epoch_id=? ORDER BY lesson_id",
                            (epoch_id,)))
                        visibility_count += len(visible)
                        source_lessons = list(db.execute(
                            "SELECT lesson_id FROM sealed_lessons "
                            "WHERE lesson_batch_id=? ORDER BY lesson_id",
                            (source_batch["lesson_batch_id"],)))
                        if ({row["lesson_id"] for row in visible} !=
                                {row["lesson_id"] for row in source_lessons}):
                            raise IntegrityViolation(
                                "successor lesson visibility is incomplete")
                        for visibility in visible:
                            visibility_fields = (
                                "target_epoch_id", "source_epoch_id",
                                "lesson_id", "lesson_batch_id", "created_at")
                            self._verify_row_digest(
                                visibility, visibility_fields, "lesson visibility")
                            if (visibility["source_epoch_id"] != prior_id or
                                    visibility["lesson_batch_id"] !=
                                    source_batch["lesson_batch_id"]):
                                raise IntegrityViolation(
                                    "lesson escaped its immediate successor")
                    prior_id = epoch_id

                orphan_counts = {
                    "outcome_batches": db.execute(
                        "SELECT count(*) FROM outcome_batches").fetchone()[0],
                    "lesson_batches": db.execute(
                        "SELECT count(*) FROM sealed_lessons").fetchone()[0],
                    "lesson_visibility": db.execute(
                        "SELECT count(*) FROM lesson_visibility").fetchone()[0],
                }
                if int(orphan_counts["outcome_batches"]) != outcome_count:
                    raise IntegrityViolation("orphan outcome batches exist")
                if int(orphan_counts["lesson_batches"]) != lesson_count:
                    raise IntegrityViolation("orphan lessons exist")
                if int(orphan_counts["lesson_visibility"]) != visibility_count:
                    raise IntegrityViolation("orphan lesson visibility rows exist")
                return {
                    "schema": "paper-epoch-integrity.v1", "ok": True,
                    "schema_version": SCHEMA_VERSION,
                    "epochs": len(epochs), "audit_events": len(audit),
                    "outcomes": outcome_count, "lessons": lesson_count,
                    "audit_head": previous_hash,
                }
        except IntegrityViolation:
            raise
        except (sqlite3.Error, KeyError, TypeError, ValueError,
                StopIteration) as exc:
            raise IntegrityViolation(
                "paper epoch store failed integrity verification") from exc

    # Concise aliases for callers that use ledger/audit terminology.
    audit = verify_integrity


PaperEpochLedger = PaperEpochStore


__all__ = [
    "SCHEMA_VERSION", "DEFAULT_DB_PATH", "PAPER_PRIMARY", "SHADOW", "FILL",
    "REJECTION", "OPERATIONAL_FAILURE", "PaperEpochError",
    "EpochValidationError", "EpochStateError", "IdentityError",
    "RuntimeAdaptationError", "OutcomeConflict", "IsolationError",
    "IntegrityViolation", "content_digest", "identity_fingerprint",
    "FrozenEpoch", "EpochManifest", "PaperRuntimeIdentity", "CohortMember",
    "RuntimeStartAttestation", "ConfirmationRestart",
    "UnseenDataConfirmation", "ExecutionObservation", "SealedLesson",
    "PaperEpochStore", "PaperEpochLedger",
]
