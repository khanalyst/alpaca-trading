"""Append-only persistence for autonomous strategy-factory lineage."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence
import uuid

from .edge_lab import DEFAULT_DB_PATH, EdgeLedger, canonical_json, content_hash
from .gates import verify_gate_envelope
from .stats import (DEPENDENCE_CORRELATION_THRESHOLD,
                     DEPENDENCE_MIN_COMPLETE_SESSIONS,
                     DEPENDENCE_MIN_PRIOR_CYCLES,
                     DEPENDENCE_POLICY_SCHEMA,
                     deterministic_dependence_map)
from agent.contracts.rule import rule_variant_id


FACTORY_SCHEMA = "strategy-factory.v1"
FACTORY_IDENTITY_SCHEMA = "strategy-experiment.v1"
ACTIVE_HYPOTHESIS_STATES = {
    "queued", "testing", "backtest_passed", "pending_generation_limit",
    "pending_llm_replacement",
}
FACTORY_STATUSES = ACTIVE_HYPOTHESIS_STATES | {
    "validated", "retired", "bounded_space_exhausted",
}
# Variant closures are deliberately distinct from hypothesis retirement.  A
# bounded search can close one exact parameter point while leaving siblings
# retryable (or while recentering the hypothesis onto the best fit child).
VARIANT_CLOSURE_MODES = {"scientific", "budget", "recenter"}
# What a recorded reason was given for.  ``tuning`` changes the numbers of one
# hypothesis; the rest change which hypothesis a slot holds.
LESSON_KINDS = {"tuning", "tuning_retry", "discovery", "replacement", "rotation", "reseed",
                # What a live paper trial taught, which is the only lesson kind
                # produced by real fills rather than a replay.
                "trial"}
LESSON_SOURCES = {"llm", "deterministic", "live_paper"}
# v6 is the active raw-p confirmatory method.  It is a new durable scope and
# therefore gets its own LORD++ sequence and initial-wealth preregistration.
# v5 remains fully readable/replayable: its rows retain the original W0=alpha
# allocation and are never reinterpreted as v6 after a restart.  The prior
# v2/v3/v4 scopes likewise retain their historical methods.
CONFIRMATORY_SCOPE_VERSION = "shadow-confirmation-v6"
CONFIRMATORY_SCOPE_VERSION_V5 = "shadow-confirmation-v5"
FDR_METHOD_V5 = "lord_plus_plus_balanced_raw_p_v5"
FDR_METHOD_V6 = "lord_plus_plus_balanced_raw_p_v6"
# FDR_METHOD is the active method for new callers.  Keep the explicit v5
# symbol above for audit/replay consumers that must select the old semantics.
FDR_METHOD = FDR_METHOD_V6
LEGACY_FDR_METHOD = "lord_balanced_v2"
LEGACY_RAW_FDR_METHOD = "lord_balanced_raw_p_v3"
FDR_METHOD_VERSION_V5 = "v5"
FDR_METHOD_VERSION_V6 = "v6"
FDR_METHOD_VERSION = FDR_METHOD_VERSION_V6
# v5 spent the full nominal alpha before its first discovery.  v6 is the new
# preregistration: half of alpha is initial wealth, leaving alpha/2 for the
# first-discovery reward.  The unsuffixed constant is the active v6 value;
# callers replaying v5 should use the explicit v5 constant.
FDR_INITIAL_WEALTH_FRACTION_V5 = 1.0
FDR_INITIAL_WEALTH_FRACTION_V6 = 0.5
FDR_INITIAL_WEALTH_FRACTION = FDR_INITIAL_WEALTH_FRACTION_V6
FDR_GAMMA_METHOD = "balanced_telescoping"


def _paired_session_deltas(result: Mapping[str, Any], *, vehicle: str) -> list[dict[str, Any]]:
    """Return candidate-minus-baseline deltas keyed by tested session.

    This intentionally mirrors the gate match key (comparison id, then
    symbol/session, then opportunity/timestamp) without using aggregate P&L.
    Ambiguous duplicate keys are dropped.  The caller can therefore average
    repeated variants deterministically while preserving the exact paired
    opportunities that the gate tested.
    """
    gate = result.get("gate") if isinstance(result, Mapping) else None
    envelope = gate.get("verified_gate") if isinstance(gate, Mapping) else None
    if not isinstance(envelope, Mapping):
        return []
    candidate = envelope.get("heldout_source")
    baseline = envelope.get("heldout_baseline_source")
    if not isinstance(candidate, (list, tuple)) or not isinstance(baseline, (list, tuple)):
        return []

    def key(row: Mapping) -> str:
        if row.get("comparison_id"):
            return str(row["comparison_id"])
        symbol, session = row.get("symbol"), row.get("session_date")
        if symbol and session:
            return f"{row.get('vehicle', vehicle)}:{symbol}:{session}"
        return str(row.get("opportunity_id") or row.get("entry_timestamp") or "")

    def indexed(rows: list | tuple) -> dict[str, Mapping]:
        values: dict[str, Mapping] = {}
        duplicates: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping) or row.get("no_trade") is True:
                continue
            if row.get("vehicle", vehicle) != vehicle:
                continue
            match = key(row)
            if not match or match in values:
                duplicates.add(match)
            else:
                values[match] = row
        for match in duplicates:
            values.pop(match, None)
        return values

    left, right = indexed(candidate), indexed(baseline)
    paired: list[dict[str, Any]] = []
    for match in sorted(set(left) & set(right)):
        try:
            delta = float(left[match].get("net_pnl", 0.0)) - float(
                right[match].get("net_pnl", 0.0))
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(delta):
            continue
        session = str(left[match].get("session_date") or "").strip()
        if session:
            paired.append({"session": session, "delta": delta})
    return paired


def dependence_policy_digest(policy: Mapping[str, Any]) -> str:
    """Hash semantic dependence policy inputs, excluding audit timestamps/ids.

    ``policy_hash`` authenticates the exact persisted freeze (including target
    cycle and cutoff).  Authorizing gate/provenance hashes must instead be
    stable across equivalent memory/process/path replays, so they use this
    digest over version, thresholds, source cycles, cluster assignments, and
    deterministic evidence only.
    """
    evidence = policy.get("evidence") if isinstance(policy.get("evidence"), Mapping) else {}
    semantic = {
        "schema": str(policy.get("schema") or DEPENDENCE_POLICY_SCHEMA),
        "version": int(policy.get("version", 1)),
        "vehicle": str(policy.get("vehicle") or ""),
        "source_cycles": sorted(str(item) for item in (policy.get("source_cycles") or ())),
        "cluster_map": {str(key): str(value) for key, value in sorted(
            (policy.get("cluster_map") or {}).items(), key=lambda item: str(item[0]))},
        "evidence": evidence,
    }
    return content_hash(semantic)


def _fdr_semantics(scope: str) -> tuple[str, str]:
    """Return the method and p-value kind encoded by a durable scope.

    A scope is the durable method-version boundary.  The v2 rows spent a
    selection q-value; v3 and v4 rows spent the old balanced raw-p sequence.
    v5 and v6 are separate LORD++ sequences with different preregistered
    initial wealth.  An unversioned/custom scope retains the pre-v6 raw-p
    semantics so an old caller cannot silently opt into the new rule by
    omitting a version.
    """
    value = str(scope)
    if value.startswith("shadow-confirmation-v2:"):
        return LEGACY_FDR_METHOD, "legacy_q"
    if value.startswith(("shadow-confirmation-v3:",
                         "shadow-confirmation-v4:")):
        return LEGACY_RAW_FDR_METHOD, "raw_confirmatory"
    if value.startswith(f"{CONFIRMATORY_SCOPE_VERSION_V5}:"):
        return FDR_METHOD_V5, "raw_confirmatory"
    if value.startswith(f"{CONFIRMATORY_SCOPE_VERSION}:"):
        return FDR_METHOD_V6, "raw_confirmatory"
    if value.startswith("shadow-confirmation-v"):
        raise FactoryError(f"unsupported confirmatory FDR scope: {value}")
    # Unversioned/custom scopes predate the v6 boundary.  Keep their old
    # raw-p LORD++ semantics rather than silently opting them into W0=alpha/2.
    return FDR_METHOD_V5, "raw_confirmatory"


class FactoryError(ValueError):
    """Raised when a factory operation cannot preserve research boundaries."""


def _fdr_method_version(method: str) -> str:
    if method == FDR_METHOD_V5:
        return FDR_METHOD_VERSION_V5
    if method == FDR_METHOD_V6:
        return FDR_METHOD_VERSION_V6
    if method == LEGACY_RAW_FDR_METHOD:
        return "v3"
    if method == LEGACY_FDR_METHOD:
        return "v2"
    raise FactoryError(f"unsupported durable FDR method: {method}")


def _fdr_p_value_kind(method: str) -> str:
    if method == LEGACY_FDR_METHOD:
        return "legacy_q"
    if method in {FDR_METHOD_V5, FDR_METHOD_V6, LEGACY_RAW_FDR_METHOD}:
        return "raw_confirmatory"
    raise FactoryError(f"unsupported durable FDR method: {method}")


def _resolved_fdr_semantics(
        scope: str, rows: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    """Resolve a scope without reinterpreting pre-versioned durable rows.

    New callers use the method encoded by the scope.  Rows written before the
    method columns existed may have an unversioned/custom scope, however; once
    migration records their historical method, that stored identity is the
    authority for the existing sequence.  Explicit ``shadow-confirmation-vN``
    scopes must always agree with their declared version.
    """
    expected_method, expected_kind = _fdr_semantics(scope)
    stored_methods = {
        str(row["method"])
        for row in rows
        if "method" in row.keys() and str(row["method"] or "")
    }
    if len(stored_methods) > 1:
        raise FactoryError("durable FDR scope contains mixed methods")
    if not stored_methods:
        return expected_method, expected_kind
    stored_method = next(iter(stored_methods))
    stored_kind = _fdr_p_value_kind(stored_method)
    if str(scope).startswith("shadow-confirmation-v") and stored_method != expected_method:
        raise FactoryError(
            f"FDR scope contains method {stored_method}, expected {expected_method}")
    expected_version = _fdr_method_version(stored_method)
    stored_versions = {
        str(row["method_version"])
        for row in rows
        if "method_version" in row.keys() and str(row["method_version"] or "")
    }
    if len(stored_versions) > 1 or (
            stored_versions and stored_versions != {expected_version}):
        raise FactoryError("durable FDR scope contains inconsistent method versions")
    return stored_method, stored_kind


def _fdr_gamma(index: int) -> float:
    """Return the balanced LORD weight ``gamma_i = 1/(i(i+1))``."""
    if index <= 0:
        return 0.0
    return 1.0 / (int(index) * (int(index) + 1))


def _legacy_fdr_allocation(decisions: list[Mapping[str, Any]],
                           alpha: float) -> tuple[int, float]:
    """Return the pre-v5 balanced LORD-style allocation for old scopes."""
    test_index = len(decisions) + 1
    allocated = float(alpha) * _fdr_gamma(test_index)
    for discovery_index, row in enumerate(decisions, start=1):
        if bool(row["decision"]):
            allocated += float(alpha) * _fdr_gamma(test_index - discovery_index)
    return test_index, min(float(alpha), allocated)


def _lord_plus_plus_allocation(
        decisions: list[Mapping[str, Any]], alpha: float,
        initial_wealth_fraction: float) -> tuple[int, float]:
    """Return a LORD++ allocation for one preregistered initial wealth.

    Let ``tau_j`` be the test index of the j-th prior discovery and let
    ``gamma_i = 1/(i(i+1))``.  For an explicitly preregistered initial wealth
    ``0 < W0 <= alpha``, LORD++ allocates at test ``i``

    ``W0*gamma_i + (alpha-W0)*gamma_(i-tau_1) +
    alpha*sum(gamma_(i-tau_j), j >= 2)``

    over discoveries strictly before ``i``.  ``initial_wealth_fraction`` is
    part of the durable method identity; changing it without a new scope
    would reinterpret every prior allocation.
    indices with no prior discovery contribute nothing.  The balanced gamma
    sequence sums to one, so the base and each reward stream are separately
    bounded as required by LORD++.
    """
    test_index = len(decisions) + 1
    fraction = float(initial_wealth_fraction)
    if not math.isfinite(fraction) or not 0.0 < fraction <= 1.0:
        raise FactoryError("initial wealth fraction must be in (0,1]")
    initial_wealth = float(alpha) * fraction
    allocated = initial_wealth * _fdr_gamma(test_index)
    discoveries = [index for index, row in enumerate(decisions, start=1)
                   if bool(row["decision"])]
    if discoveries:
        first = discoveries[0]
        if test_index > first:
            allocated += (float(alpha) - initial_wealth) * _fdr_gamma(
                test_index - first)
        for discovery in discoveries[1:]:
            if test_index > discovery:
                allocated += float(alpha) * _fdr_gamma(test_index - discovery)
    return test_index, min(float(alpha), allocated)


def _fdr_allocation(decisions: list[Mapping[str, Any]],
                    alpha: float) -> tuple[int, float]:
    """Return the v5 LORD++ allocation (the historical W0=alpha rule)."""
    return _lord_plus_plus_allocation(
        decisions, alpha, FDR_INITIAL_WEALTH_FRACTION_V5)


def _fdr_allocation_v6(decisions: list[Mapping[str, Any]],
                       alpha: float) -> tuple[int, float]:
    """Return the v6 LORD++ allocation (the new preregistered W0=alpha/2)."""
    return _lord_plus_plus_allocation(
        decisions, alpha, FDR_INITIAL_WEALTH_FRACTION_V6)


def _fdr_allocation_for_method(decisions: list[Mapping[str, Any]], alpha: float,
                               method: str) -> tuple[int, float]:
    """Dispatch allocation while preserving the old scope semantics."""
    if method in {LEGACY_FDR_METHOD, LEGACY_RAW_FDR_METHOD}:
        return _legacy_fdr_allocation(decisions, alpha)
    if method == FDR_METHOD_V5:
        return _fdr_allocation(decisions, alpha)
    if method == FDR_METHOD_V6:
        return _fdr_allocation_v6(decisions, alpha)
    raise FactoryError(f"unsupported durable FDR method: {method}")


def _fdr_metadata(method: str, p_value_kind: str, alpha: float) -> dict[str, Any]:
    """Describe the durable method without conflating old and current rules."""
    metadata = {"method": str(method), "p_value_kind": str(p_value_kind)}
    if method in {FDR_METHOD_V5, FDR_METHOD_V6}:
        fraction = (FDR_INITIAL_WEALTH_FRACTION_V5
                    if method == FDR_METHOD_V5
                    else FDR_INITIAL_WEALTH_FRACTION_V6)
        initial_wealth = float(alpha) * fraction
        metadata.update({
            "method_version": _fdr_method_version(method),
            "algorithm": "LORD++",
            "gamma_method": FDR_GAMMA_METHOD,
            "gamma_formula": "1/(i*(i+1))",
            "gamma_sum": 1.0,
            "initial_wealth": initial_wealth,
            "initial_wealth_fraction": fraction,
            "first_discovery_reward": float(alpha) - initial_wealth,
            "subsequent_discovery_reward": float(alpha),
            "reference": (
                "Ramdas-Yang-Wainwright-Jordan-2017-"
                "online-FDR-with-decaying-memory"),
            "reference_url": (
                "https://proceedings.neurips.cc/paper_files/paper/2017/hash/"
                "7f018eb7b301a66658931cb8a93fd6e8-Abstract.html"),
            "guarantee": {
                "mFDR": "conditional_super_uniform_null_p_values",
                "FDR": "independent_null_p_values_and_monotone_predictable_levels",
                "confirmatory_design_must_establish_assumptions": True,
            },
        })
    elif method == LEGACY_RAW_FDR_METHOD:
        metadata.update({"method_version": _fdr_method_version(method),
                         "algorithm": "legacy_balanced_lord"})
    else:
        metadata.update({"method_version": _fdr_method_version(method),
                         "algorithm": "legacy_balanced_lord"})
    return metadata


def _locked_scope_alpha(rows: list[Mapping[str, Any]], requested: float) -> float:
    """Keep one preregistered alpha for the lifetime of an FDR scope."""
    if not rows:
        return float(requested)
    values = [_real(row["alpha"] if "alpha" in row.keys() else None)
              for row in rows]
    if any(value is None for value in values):
        raise FactoryError("durable FDR scope has an invalid alpha")
    locked = float(values[0])
    if any(not math.isclose(float(value), locked, rel_tol=0.0, abs_tol=1e-12)
           for value in values[1:]):
        raise FactoryError("durable FDR scope contains inconsistent alpha values")
    if not math.isclose(float(requested), locked, rel_tol=0.0, abs_tol=1e-12):
        raise FactoryError(
            f"FDR scope alpha is immutable at {locked:g}; requested {requested:g}")
    return locked


def deferred_fdr(scope: str, test_id: str) -> dict[str, Any]:
    """Describe an offline proof whose cumulative test is reserved for live shadow."""
    method, p_kind = _fdr_semantics(scope)
    deferred_method = {
        FDR_METHOD_V5: "deferred_confirmatory_raw_p_v5",
        FDR_METHOD_V6: "deferred_confirmatory_raw_p_v6",
        LEGACY_RAW_FDR_METHOD: "deferred_confirmatory_raw_p_v3",
        LEGACY_FDR_METHOD: "deferred_confirmatory_legacy_q_v2",
    }.get(method, "deferred_confirmatory_legacy")
    return {"scope": str(scope), "test_id": str(test_id), "required": False,
            "tested": False, "status": "deferred_to_live_shadow",
            "decision": False, "cumulative": True,
            "method": deferred_method,
            "p_value_kind": p_kind,
            **({"method_version": _fdr_method_version(method),
                "online_method": method}
               if method in {FDR_METHOD_V5, FDR_METHOD_V6} else {})}


def _real(value: Any) -> float | None:
    """Coerce a metric to a finite float, or ``None`` rather than a guess."""
    if isinstance(value, (bool, str, bytes, bytearray)) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    return db


def experiment_identity(*, dataset_hash: str, vehicle: str,
                        code_hash: str | None = None,
                        config_hash: str | None = None,
                        cost: Mapping | None = None,
                        risk: Mapping | None = None,
                        gate: Mapping | None = None,
                        provenance: Mapping | None = None) -> dict[str, Any]:
    """Build the deterministic identity that distinguishes factory runs.

    The returned body is safe to persist and its ``identity_hash`` is stable
    across process/cycle UUIDs.  Legacy callers can omit optional fields; the
    dataset/vehicle identity remains backward-compatible while new callers
    should bind every supplied assumption.
    """
    body = {"schema": FACTORY_IDENTITY_SCHEMA, "dataset_hash": str(dataset_hash),
            "vehicle": str(vehicle), "code_hash": code_hash,
            "config_hash": config_hash, "cost": dict(cost or {}),
            "risk": dict(risk or {}), "gate": dict(gate or {}),
            "provenance": dict(provenance or {})}
    body["identity_hash"] = content_hash(body)
    return body


def experiment_provenance(*, dataset: Any = None, config: Any = None,
                          code: Any = None, cost: Mapping | None = None,
                          risk: Mapping | None = None,
                          gate: Mapping | None = None) -> dict[str, str]:
    """Hash the assumptions that materially affect a factory result."""
    return {
        "dataset_hash": content_hash(dataset if dataset is not None else {}),
        "config_hash": content_hash(config if config is not None else {}),
        "code_hash": content_hash(code if code is not None else {}),
        "cost_hash": content_hash(dict(cost or {})),
        "risk_hash": content_hash(dict(risk or {})),
        "gate_hash": content_hash(dict(gate or {})),
    }


def _migrate_cycle_identity(db: sqlite3.Connection) -> None:
    """Migrate the old dataset/vehicle uniqueness without losing rows."""
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='factory_cycles'").fetchone()
    if not exists:
        return
    columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(factory_cycles)")}
    if "identity_hash" in columns:
        if "identity_json" not in columns:
            db.execute("DROP TRIGGER IF EXISTS factory_cycles_no_update")
            db.execute("DROP TRIGGER IF EXISTS factory_cycles_no_delete")
            db.execute("ALTER TABLE factory_cycles ADD COLUMN identity_json TEXT NOT NULL DEFAULT '{}'")
            rows = db.execute("SELECT cycle_id,dataset_hash,vehicle,identity_hash FROM factory_cycles").fetchall()
            for row in rows:
                identity = experiment_identity(dataset_hash=row["dataset_hash"], vehicle=row["vehicle"])
                db.execute("UPDATE factory_cycles SET identity_json=? WHERE cycle_id=?",
                           (canonical_json(identity), row["cycle_id"]))
        return
    db.execute("DROP TRIGGER IF EXISTS factory_cycles_no_update")
    db.execute("DROP TRIGGER IF EXISTS factory_cycles_no_delete")
    db.execute("ALTER TABLE factory_cycles RENAME TO factory_cycles_legacy")
    db.execute("""CREATE TABLE factory_cycles (
        cycle_id TEXT PRIMARY KEY, dataset_hash TEXT NOT NULL,
        vehicle TEXT NOT NULL, identity_hash TEXT NOT NULL,
        identity_json TEXT NOT NULL, workers INTEGER NOT NULL,
        strategies INTEGER NOT NULL, variants INTEGER NOT NULL,
        result_json TEXT NOT NULL, created_at REAL NOT NULL)""")
    rows = db.execute("SELECT * FROM factory_cycles_legacy").fetchall()
    for row in rows:
        identity = experiment_identity(dataset_hash=row["dataset_hash"], vehicle=row["vehicle"])
        db.execute("""INSERT INTO factory_cycles
            (cycle_id,dataset_hash,vehicle,identity_hash,identity_json,workers,
             strategies,variants,result_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""", (
                row["cycle_id"], row["dataset_hash"], row["vehicle"],
                identity["identity_hash"], canonical_json(identity), row["workers"],
                row["strategies"], row["variants"], row["result_json"], row["created_at"]))
    db.execute("DROP TABLE factory_cycles_legacy")


class FactoryLedger:
    """Store immutable hypotheses, accounts, events, and completed cycles."""

    def __init__(self, path: str | Path = DEFAULT_DB_PATH):
        self.path = Path(path)
        EdgeLedger(self.path)
        with closing(_connect(self.path)) as db, db:
            _migrate_cycle_identity(db)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS factory_hypotheses (
                    hypothesis_id TEXT PRIMARY KEY,
                    slot INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    vehicle TEXT NOT NULL CHECK(vehicle IN ('equity','option')),
                    parent_hypothesis_id TEXT,
                    family TEXT NOT NULL,
                    thesis TEXT NOT NULL,
                    falsification TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    not_before TEXT,
                    created_at REAL NOT NULL,
                    UNIQUE(vehicle,slot,generation)
                );
                CREATE TABLE IF NOT EXISTS factory_events (
                    event_id TEXT PRIMARY KEY,
                    hypothesis_id TEXT NOT NULL REFERENCES factory_hypotheses(hypothesis_id),
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS factory_accounts (
                    account_id TEXT PRIMARY KEY,
                    cycle_id TEXT NOT NULL,
                    hypothesis_id TEXT NOT NULL REFERENCES factory_hypotheses(hypothesis_id),
                    variant_id TEXT NOT NULL,
                    vehicle TEXT NOT NULL,
                    starting_cash REAL NOT NULL,
                    ending_equity REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    max_drawdown REAL NOT NULL,
                    trades INTEGER NOT NULL,
                    worker_pid INTEGER NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(cycle_id,variant_id,vehicle)
                );
                CREATE TABLE IF NOT EXISTS factory_variant_closures (
                    closure_id TEXT PRIMARY KEY,
                    hypothesis_id TEXT NOT NULL REFERENCES factory_hypotheses(hypothesis_id),
                    vehicle TEXT NOT NULL CHECK(vehicle IN ('equity','option')),
                    variant_id TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('scientific','budget','recenter')),
                    reason TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    evidence_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(hypothesis_id,variant_id)
                );
                CREATE INDEX IF NOT EXISTS factory_variant_closures_vehicle
                    ON factory_variant_closures(vehicle,variant_id);
                CREATE TABLE IF NOT EXISTS factory_cycles (
                    cycle_id TEXT PRIMARY KEY,
                    dataset_hash TEXT NOT NULL,
                    vehicle TEXT NOT NULL,
                    identity_hash TEXT NOT NULL,
                    identity_json TEXT NOT NULL,
                    workers INTEGER NOT NULL,
                    strategies INTEGER NOT NULL,
                    variants INTEGER NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                -- A policy is frozen before a cycle evaluates any current
                -- data.  It is immutable and becomes runtime-visible only
                -- once its target cycle has completed in factory_cycles.
                CREATE TABLE IF NOT EXISTS factory_dependence_policies (
                    policy_id TEXT PRIMARY KEY,
                    target_cycle_id TEXT NOT NULL,
                    vehicle TEXT NOT NULL CHECK(vehicle IN ('equity','option')),
                    schema TEXT NOT NULL,
                    cutoff REAL NOT NULL,
                    source_cycles_json TEXT NOT NULL,
                    cluster_map_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    policy_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(target_cycle_id, vehicle)
                );
                CREATE INDEX IF NOT EXISTS factory_dependence_policies_latest
                    ON factory_dependence_policies(vehicle, created_at);
                -- Why something was tried, and what happened when it was.
                -- Split in two because the two facts are learned at different
                -- times: the reason exists when the variant is proposed, the
                -- grade only after its gate is computed.  Keeping them as two
                -- append-only rows preserves the ledger's no-update rule
                -- instead of carving an exception into it.
                CREATE TABLE IF NOT EXISTS factory_lessons (
                    lesson_id TEXT PRIMARY KEY,
                    hypothesis_id TEXT NOT NULL REFERENCES factory_hypotheses(hypothesis_id),
                    vehicle TEXT NOT NULL CHECK(vehicle IN ('equity','option')),
                    family TEXT NOT NULL,
                    variant_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    changed_json TEXT NOT NULL,
                    diagnosis_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    -- The graded lesson this proposal reasoned from, so the
                    -- chain of learning is a durable edge in the ledger rather
                    -- than an assertion in a prompt.
                    parent_lesson_id TEXT REFERENCES factory_lessons(lesson_id),
                    UNIQUE(hypothesis_id,variant_id,kind)
                );
                CREATE TABLE IF NOT EXISTS factory_lesson_outcomes (
                    outcome_id TEXT PRIMARY KEY,
                    lesson_id TEXT NOT NULL REFERENCES factory_lessons(lesson_id),
                    passed INTEGER NOT NULL,
                    underpowered INTEGER NOT NULL,
                    classification TEXT NOT NULL,
                    fit_delta REAL,
                    heldout_delta REAL,
                    heldout_net_pnl REAL,
                    q_value REAL,
                    failed_checks_json TEXT NOT NULL,
                    gate_hash TEXT,
                    created_at REAL NOT NULL,
                    UNIQUE(lesson_id)
                );
                CREATE INDEX IF NOT EXISTS factory_lessons_family
                    ON factory_lessons(vehicle,family,created_at);
                CREATE TRIGGER IF NOT EXISTS factory_lessons_no_update
                    BEFORE UPDATE ON factory_lessons BEGIN
                    SELECT RAISE(ABORT, 'factory lessons are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_lessons_no_delete
                    BEFORE DELETE ON factory_lessons BEGIN
                    SELECT RAISE(ABORT, 'factory lessons are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_lesson_outcomes_no_update
                    BEFORE UPDATE ON factory_lesson_outcomes BEGIN
                    SELECT RAISE(ABORT, 'factory lesson outcomes are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_lesson_outcomes_no_delete
                    BEFORE DELETE ON factory_lesson_outcomes BEGIN
                    SELECT RAISE(ABORT, 'factory lesson outcomes are immutable');
                END;
                CREATE INDEX IF NOT EXISTS factory_lessons_parent
                    ON factory_lessons(parent_lesson_id);
                CREATE TRIGGER IF NOT EXISTS factory_hypotheses_no_update
                    BEFORE UPDATE ON factory_hypotheses BEGIN
                    SELECT RAISE(ABORT, 'factory hypotheses are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_hypotheses_no_delete
                    BEFORE DELETE ON factory_hypotheses BEGIN
                    SELECT RAISE(ABORT, 'factory hypotheses are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_accounts_no_update
                    BEFORE UPDATE ON factory_accounts BEGIN
                    SELECT RAISE(ABORT, 'factory accounts are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_accounts_no_delete
                    BEFORE DELETE ON factory_accounts BEGIN
                    SELECT RAISE(ABORT, 'factory accounts are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_variant_closures_no_update
                    BEFORE UPDATE ON factory_variant_closures BEGIN
                    SELECT RAISE(ABORT, 'factory variant closures are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_variant_closures_no_delete
                    BEFORE DELETE ON factory_variant_closures BEGIN
                    SELECT RAISE(ABORT, 'factory variant closures are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_events_no_update
                    BEFORE UPDATE ON factory_events BEGIN
                    SELECT RAISE(ABORT, 'factory events are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_events_no_delete
                    BEFORE DELETE ON factory_events BEGIN
                    SELECT RAISE(ABORT, 'factory events are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_cycles_no_update
                    BEFORE UPDATE ON factory_cycles BEGIN
                    SELECT RAISE(ABORT, 'factory cycles are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_cycles_no_delete
                    BEFORE DELETE ON factory_cycles BEGIN
                    SELECT RAISE(ABORT, 'factory cycles are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_dependence_policies_no_update
                    BEFORE UPDATE ON factory_dependence_policies BEGIN
                    SELECT RAISE(ABORT, 'dependence policies are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS factory_dependence_policies_no_delete
                    BEFORE DELETE ON factory_dependence_policies BEGIN
                    SELECT RAISE(ABORT, 'dependence policies are immutable');
                END;
            """)
            # ``CREATE TABLE IF NOT EXISTS`` leaves an already-created table
            # alone, so a ledger written before the learning chain existed
            # needs the column added rather than assumed.
            columns = {str(row["name"]) for row in
                       db.execute("PRAGMA table_info(factory_lessons)")}
            if columns and "parent_lesson_id" not in columns:
                db.execute("ALTER TABLE factory_lessons "
                           "ADD COLUMN parent_lesson_id TEXT")
            outcome_columns = {str(row["name"]) for row in
                               db.execute("PRAGMA table_info(factory_lesson_outcomes)")}
            if outcome_columns and "fit_delta" not in outcome_columns:
                db.execute("ALTER TABLE factory_lesson_outcomes ADD COLUMN fit_delta REAL")
                outcome_columns.add("fit_delta")
            if outcome_columns and "classification" not in outcome_columns:
                # Historical adequate non-passes did not carry the upper-bound
                # evidence needed to distinguish rejection from uncertainty.
                # Retest them instead of upgrading them into terminal failures.
                db.execute(
                    "ALTER TABLE factory_lesson_outcomes ADD COLUMN "
                    "classification TEXT NOT NULL DEFAULT 'legacy_unclassified'")
            db.execute("""CREATE TABLE IF NOT EXISTS factory_fdr (
                decision_id TEXT PRIMARY KEY, scope TEXT NOT NULL,
                test_id TEXT NOT NULL, p_value REAL NOT NULL,
                alpha REAL NOT NULL, allocated_alpha REAL NOT NULL,
                decision INTEGER NOT NULL, created_at REAL NOT NULL,
                method TEXT NOT NULL DEFAULT '',
                method_version TEXT NOT NULL DEFAULT '',
                UNIQUE(scope,test_id)
            )""")
            # Persist the method identity with each immutable decision.  The
            # original table had no method columns; its scope prefix is the
            # only available version discriminator, so backfill v2/v3/v4
            # rows from that prefix before the append-only triggers are
            # recreated.  This prevents a v4 row from being reinterpreted as
            # a newer LORD++ sequence after restart.  Scope prefixes are
            # resolved individually so existing v5 rows never become v6.
            fdr_columns = {str(row["name"]) for row in
                           db.execute("PRAGMA table_info(factory_fdr)")}
            if "method" not in fdr_columns or "method_version" not in fdr_columns:
                db.execute("DROP TRIGGER IF EXISTS factory_fdr_no_update")
                if "method" not in fdr_columns:
                    db.execute("ALTER TABLE factory_fdr ADD COLUMN method TEXT NOT NULL DEFAULT ''")
                if "method_version" not in fdr_columns:
                    db.execute("ALTER TABLE factory_fdr ADD COLUMN method_version TEXT NOT NULL DEFAULT ''")
                fdr_columns = {str(row["name"]) for row in
                               db.execute("PRAGMA table_info(factory_fdr)")}
                # Existing decisions were written under the method selected
                # by the pre-v5 implementation. Backfilling is a schema
                # migration, not a scientific rewrite: every existing raw-p
                # row (including an unversioned/custom scope) used the legacy
                # allocation, while only v2 used the older q-value contract.
                # A v5 decision cannot predate these columns.
                rows = db.execute("SELECT decision_id,scope FROM factory_fdr").fetchall()
                for row in rows:
                    scope = str(row["scope"])
                    if scope.startswith("shadow-confirmation-v2:"):
                        method = LEGACY_FDR_METHOD
                    elif scope.startswith(f"{CONFIRMATORY_SCOPE_VERSION_V5}:"):
                        method = FDR_METHOD_V5
                    elif scope.startswith(f"{CONFIRMATORY_SCOPE_VERSION}:"):
                        method = FDR_METHOD_V6
                    else:
                        method = LEGACY_RAW_FDR_METHOD
                    version = _fdr_method_version(method)
                    db.execute("UPDATE factory_fdr SET method=?,method_version=? "
                               "WHERE decision_id=?", (method, version, row["decision_id"]))
            else:
                # A database created by this version can still contain rows
                # from an interrupted migration with blank metadata.
                rows = db.execute("SELECT decision_id,scope FROM factory_fdr "
                                  "WHERE method='' OR method_version='' ").fetchall()
                if rows:
                    db.execute("DROP TRIGGER IF EXISTS factory_fdr_no_update")
                    for row in rows:
                        scope = str(row["scope"])
                        if scope.startswith(f"{CONFIRMATORY_SCOPE_VERSION_V5}:"):
                            method = FDR_METHOD_V5
                        elif scope.startswith(f"{CONFIRMATORY_SCOPE_VERSION}:"):
                            method = FDR_METHOD_V6
                        elif scope.startswith("shadow-confirmation-v2:"):
                            method = LEGACY_FDR_METHOD
                        else:
                            # Blank metadata is itself legacy/partial state;
                            # do not upgrade an ambiguous row into LORD++.
                            method = LEGACY_RAW_FDR_METHOD
                        version = _fdr_method_version(method)
                        db.execute("UPDATE factory_fdr SET method=?,method_version=? "
                                   "WHERE decision_id=?", (method, version, row["decision_id"]))
            db.execute("CREATE INDEX IF NOT EXISTS factory_fdr_scope ON factory_fdr(scope,created_at)")
            db.execute("""CREATE TRIGGER IF NOT EXISTS factory_fdr_no_update
                BEFORE UPDATE ON factory_fdr BEGIN
                SELECT RAISE(ABORT, 'factory FDR decisions are immutable'); END;""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS factory_fdr_no_delete
                BEFORE DELETE ON factory_fdr BEGIN
                SELECT RAISE(ABORT, 'factory FDR decisions are immutable'); END;""")
            # Confirmatory evidence is a consumable resource.  Keep the
            # session set at the factory boundary so a process crash between
            # releasing a sealed window and writing its proof cannot make the
            # same sessions look new on the next cycle.
            db.execute("""CREATE TABLE IF NOT EXISTS factory_evidence_claims (
                claim_id TEXT PRIMARY KEY,
                cycle_id TEXT NOT NULL,
                hypothesis_id TEXT NOT NULL REFERENCES factory_hypotheses(hypothesis_id),
                vehicle TEXT NOT NULL CHECK(vehicle IN ('equity','option')),
                variant_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('heldout','qualification')),
                sessions_json TEXT NOT NULL,
                session_digest TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(cycle_id,hypothesis_id,variant_id,kind)
            )""")
            db.execute("""CREATE INDEX IF NOT EXISTS factory_evidence_vehicle
                ON factory_evidence_claims(vehicle,kind,created_at)""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS factory_evidence_no_update
                BEFORE UPDATE ON factory_evidence_claims BEGIN
                SELECT RAISE(ABORT, 'factory evidence claims are immutable'); END;""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS factory_evidence_no_delete
                BEFORE DELETE ON factory_evidence_claims BEGIN
                SELECT RAISE(ABORT, 'factory evidence claims are immutable'); END;""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS factory_cycles_no_update
                BEFORE UPDATE ON factory_cycles BEGIN
                SELECT RAISE(ABORT, 'factory cycles are immutable'); END;""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS factory_cycles_no_delete
                BEFORE DELETE ON factory_cycles BEGIN
                SELECT RAISE(ABORT, 'factory cycles are immutable'); END;""")

    def register(self, hypothesis: Any) -> dict:
        now = datetime.now().timestamp()
        with closing(_connect(self.path)) as db, db:
            row = db.execute(
                "SELECT * FROM factory_hypotheses WHERE hypothesis_id=?",
                (hypothesis.hypothesis_id,),
            ).fetchone()
            if row is None:
                db.execute("INSERT INTO factory_hypotheses VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                    hypothesis.hypothesis_id, hypothesis.slot, hypothesis.generation,
                    hypothesis.vehicle, hypothesis.parent_hypothesis_id,
                    hypothesis.family, hypothesis.thesis,
                    hypothesis.falsification, canonical_json(hypothesis.rule_spec),
                    hypothesis.not_before, now,
                ))
                db.execute("INSERT INTO factory_events VALUES(?,?,?,?,?,?)", (
                    uuid.uuid4().hex, hypothesis.hypothesis_id, "queued",
                    "autonomous hypothesis registered", canonical_json({}), now,
                ))
            return self.hypothesis(hypothesis.hypothesis_id) or {}

    def event(self, hypothesis_id: str, status: str, reason: str,
              payload: Mapping | None = None) -> None:
        if not reason.strip():
            raise FactoryError("factory event reason is required")
        if status not in FACTORY_STATUSES:
            raise FactoryError(f"unknown factory status: {status}")
        if status == "retired":
            raise FactoryError("retirement requires retire_hypothesis evidence verification")
        if self.hypothesis(hypothesis_id) is None:
            raise KeyError(hypothesis_id)
        self._append_event(hypothesis_id, status, reason, payload)

    def _append_event(self, hypothesis_id: str, status: str, reason: str,
                      payload: Mapping | None = None) -> None:
        with closing(_connect(self.path)) as db, db:
            db.execute("INSERT INTO factory_events VALUES(?,?,?,?,?,?)", (
                uuid.uuid4().hex, hypothesis_id, status, reason,
                canonical_json(dict(payload or {})), datetime.now().timestamp(),
            ))

    def events(self, hypothesis_id: str) -> list[dict]:
        if self.hypothesis(hypothesis_id) is None:
            raise KeyError(hypothesis_id)
        with closing(_connect(self.path)) as db:
            rows = db.execute("""SELECT * FROM factory_events
                WHERE hypothesis_id=? ORDER BY created_at,event_id""",
                (hypothesis_id,)).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            output.append(item)
        return output

    def retire_hypothesis(self, hypothesis_id: str, *, cycle_id: str,
                          expected_variants: int, reason: str,
                          payload: Mapping | None = None,
                          mode: str = "scientific") -> None:
        """Close a hypothesis after mode-specific durable verification.

        ``scientific`` preserves the historical strict retirement guard: every
        intended variant needs an adequate, verified terminal rejection.  A
        ``budget`` close instead requires every intended exact variant to have
        an immutable closure row (scientific or budget), while ``recenter``
        requires a same-family successor and a fit-only child selection.  The
        latter two modes never relabel underpowered evidence as a statistical
        rejection.
        """
        if not reason.strip():
            raise FactoryError("factory retirement reason is required")
        mode = str(mode)
        if mode not in {"scientific", "budget", "recenter"}:
            raise FactoryError("unknown hypothesis retirement mode")
        with closing(_connect(self.path)) as db:
            child = db.execute("""SELECT * FROM factory_hypotheses
                WHERE parent_hypothesis_id=? ORDER BY created_at DESC LIMIT 1""",
                (hypothesis_id,)).fetchone()
            rows = db.execute("""SELECT result_json FROM factory_accounts
                WHERE cycle_id=? AND hypothesis_id=? ORDER BY variant_id""",
                (cycle_id, hypothesis_id)).fetchall()
        if child is None:
            raise FactoryError("hypothesis cannot retire before its replacement is registered")
        if len(rows) != int(expected_variants) or int(expected_variants) < 1:
            raise FactoryError("hypothesis retirement requires every intended variant account")
        if mode == "budget":
            with closing(_connect(self.path)) as db:
                closures = db.execute(
                    "SELECT variant_id,mode FROM factory_variant_closures "
                    "WHERE hypothesis_id=?", (hypothesis_id,)).fetchall()
            closed = {str(row["variant_id"]): str(row["mode"]) for row in closures}
            account_ids = set()
            for row in rows:
                try:
                    account_ids.add(str(json.loads(row["result_json"])["variant_id"]))
                except (KeyError, TypeError, json.JSONDecodeError) as exc:
                    raise FactoryError("hypothesis budget closure evidence is incomplete") from exc
            if not account_ids.issubset(closed) or len(account_ids) != int(expected_variants):
                raise FactoryError("hypothesis budget retirement requires every variant closure")
            detail = {**dict(payload or {}), "mode": mode, "cycle_id": cycle_id,
                      "expected_variants": int(expected_variants),
                      "closed_variant_ids": sorted(account_ids),
                      "closure_modes": {key: closed[key] for key in sorted(account_ids)}}
            self._append_event(hypothesis_id, "retired", reason, detail)
            return
        if mode == "recenter":
            detail_payload = dict(payload or {})
            parent = self.hypothesis(hypothesis_id) or {}
            child_item = dict(child)
            if str(child_item.get("family")) != str(parent.get("family")):
                raise FactoryError("recenter successor must remain in the same family")
            from_variant = str(detail_payload.get("from_variant_id") or "")
            to_variant = str(detail_payload.get("to_variant_id") or child_item.get("hypothesis_id"))
            fit_source = str(detail_payload.get("fit_score_source") or "")
            fit_score = _real(detail_payload.get("fit_score"))
            if not from_variant or fit_source != "fit_test.mean_delta" or fit_score is None:
                raise FactoryError("recenter retirement requires immutable fit-only child evidence")
            try:
                source_ids = {str(json.loads(row["result_json"])["variant_id"]) for row in rows}
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise FactoryError("recenter evidence is incomplete") from exc
            if from_variant not in source_ids:
                raise FactoryError("recenter source variant was not evaluated")
            try:
                child_spec = json.loads(child_item["spec_json"])
                child_variant = str(rule_variant_id(child_spec))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise FactoryError("recenter successor rule spec is invalid") from exc
            if to_variant != child_variant:
                raise FactoryError("recenter target variant does not match successor spec")
            detail = {**detail_payload, "mode": mode, "cycle_id": cycle_id,
                      "expected_variants": int(expected_variants),
                      "from_variant_id": from_variant,
                      "to_variant_id": to_variant}
            self._append_event(hypothesis_id, "retired", reason, detail)
            return
        gate_hashes = []
        for row in rows:
            try:
                result = json.loads(row["result_json"])
                gate = result["gate"]
                envelope = gate["verified_gate"]
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise FactoryError("hypothesis retirement evidence is incomplete") from exc
            if (gate.get("sample_adequate") is not True or
                    gate.get("heldout_sample_adequate") is not True or
                    gate.get("passes") is not False or
                    not verify_gate_envelope(envelope) or
                    envelope.get("passes") is not False):
                raise FactoryError("hypothesis retirement requires adequate failed verified gates")
            performance = envelope.get("performance")
            try:
                net = float(performance.get("heldout_net_pnl"))
                expectancy = float(performance.get("heldout_expectancy"))
            except (AttributeError, TypeError, ValueError):
                raise FactoryError(
                    "hypothesis retirement requires terminal negative performance evidence")
            if not (net <= 0.0 and expectancy <= 0.0):
                raise FactoryError(
                    "hypothesis retirement requires terminal negative performance evidence")
            retirement = envelope.get("retirement") or {}
            if (retirement.get("rejects_minimum_useful_edge") is not True or
                    retirement.get("multi_window_negative") is not True):
                raise FactoryError(
                    "hypothesis retirement requires a powered upper-bound "
                    "rejection across multiple negative windows")
            gate_hashes.append(str(envelope["content_hash"]))
        detail = {**dict(payload or {}), "cycle_id": cycle_id,
                  "expected_variants": int(expected_variants),
                  "verified_gate_hashes": sorted(gate_hashes)}
        self._append_event(hypothesis_id, "retired", reason, detail)

    def hypothesis(self, hypothesis_id: str) -> dict | None:
        with closing(_connect(self.path)) as db:
            row = db.execute("""SELECT h.*, (
                SELECT status FROM factory_events e WHERE e.hypothesis_id=h.hypothesis_id
                ORDER BY e.created_at DESC,e.event_id DESC LIMIT 1) AS status
                FROM factory_hypotheses h WHERE h.hypothesis_id=?""",
                (hypothesis_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["rule_spec"] = json.loads(item.pop("spec_json"))
        return item

    def hypotheses(self, *, vehicle: str | None = None) -> list[dict]:
        where = " WHERE h.vehicle=?" if vehicle else ""
        parameters = (vehicle,) if vehicle else ()
        with closing(_connect(self.path)) as db:
            rows = db.execute("""SELECT h.*, (
                SELECT status FROM factory_events e WHERE e.hypothesis_id=h.hypothesis_id
                ORDER BY e.created_at DESC,e.event_id DESC LIMIT 1) AS status
                FROM factory_hypotheses h""" + where +
                " ORDER BY h.vehicle,h.slot,h.generation", parameters).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["rule_spec"] = json.loads(item.pop("spec_json"))
            output.append(item)
        return output

    def active(self, vehicle: str) -> list[dict]:
        latest: dict[int, dict] = {}
        for item in self.hypotheses(vehicle=vehicle):
            if item.get("status") in ACTIVE_HYPOTHESIS_STATES:
                latest[int(item["slot"])] = item
        return [latest[key] for key in sorted(latest)]

    def slot_latest(self, vehicle: str) -> dict[int, dict]:
        """Return the highest-generation hypothesis in each occupied slot."""
        latest: dict[int, dict] = {}
        for item in self.hypotheses(vehicle=vehicle):
            slot = int(item["slot"])
            current = latest.get(slot)
            if current is None or int(item["generation"]) >= int(current["generation"]):
                latest[slot] = item
        return latest

    def next_generation(self, vehicle: str, slot: int) -> int:
        """Return the next free generation number in a slot.

        ``factory_hypotheses`` is unique on ``(vehicle, slot, generation)``, so
        a successor registered into an occupied slot has to continue that
        slot's numbering rather than restart it.
        """
        generations = [int(item["generation"])
                       for item in self.hypotheses(vehicle=vehicle)
                       if int(item["slot"]) == int(slot)]
        return max(generations) + 1 if generations else 0

    def slot_families(self, vehicle: str, slot: int) -> set[str]:
        return {str(item["family"]) for item in self.hypotheses(vehicle=vehicle)
                if int(item["slot"]) == int(slot)}

    def novel_tuning_values(self, *, hypothesis_id: str, vehicle: str,
                            family: str) -> set[str]:
        """Return model-authored novel values already spent in this lineage.

        Prompt briefs are intentionally short, so counting only their latest
        rows lets a restarted cycle spend the same novelty allowance again.
        Walk the immutable parent chain and aggregate every marked lesson for
        this family.  The returned immutable lesson/value tokens make the cap
        durable across restarts and prompt truncation; each marked attempt
        consumes one allowance unit.
        """
        lineage: set[str] = set()
        current = str(hypothesis_id)
        while current and current not in lineage:
            lineage.add(current)
            item = self.hypothesis(current)
            if item is None:
                break
            parent = item.get("parent_hypothesis_id")
            current = str(parent) if parent else ""
        if not lineage:
            return set()
        placeholders = ",".join("?" for _ in lineage)
        parameters: list[Any] = [str(vehicle), str(family), *sorted(lineage)]
        values: set[str] = set()
        with closing(_connect(self.path)) as db:
            rows = db.execute(
                "SELECT lesson_id,changed_json,evidence_json FROM factory_lessons "
                "WHERE vehicle=? AND family=? AND hypothesis_id IN (" +
                placeholders + ")", parameters).fetchall()
        for row in rows:
            try:
                evidence = json.loads(row["evidence_json"] or "{}")
                changed = json.loads(row["changed_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(evidence, Mapping) or evidence.get("novel_tuning") is not True:
                continue
            if not isinstance(changed, Mapping):
                continue
            for name, change in changed.items():
                if isinstance(change, Mapping) and "to" in change:
                    # Keep the immutable lesson id in the token: every
                    # authored novel attempt spends one durable budget unit,
                    # even if a provider repeats the same value after a
                    # restart.  The value payload remains available for
                    # duplicate suppression in the caller.
                    values.add(str(row["lesson_id"]) + ":" + canonical_json({
                        "parameter": str(name), "value": change.get("to")}))
        return values

    def slot_event_count(self, vehicle: str, slot: int, *, status: str,
                         flag: str) -> int:
        """Count a slot's events carrying ``payload[flag] is True``."""
        total = 0
        for item in self.hypotheses(vehicle=vehicle):
            if int(item["slot"]) != int(slot):
                continue
            for event in self.events(str(item["hypothesis_id"])):
                payload = event.get("payload")
                if (event.get("status") == status and
                        isinstance(payload, Mapping) and payload.get(flag) is True):
                    total += 1
        return total

    def record_lesson(self, hypothesis_id: str, *, vehicle: str, family: str,
                      variant_id: str, kind: str, source: str, reason: str,
                      changed: Mapping | None = None,
                      diagnosis: Mapping | None = None,
                      evidence: Mapping | None = None,
                      parent_lesson_id: str | None = None) -> str:
        """Record why something was tried, before anyone knows if it worked.

        Writing the reason at proposal time is what makes it evidence rather
        than a story told afterwards: it is fixed before the gate that will
        judge it has been computed.
        """
        if kind not in LESSON_KINDS:
            raise FactoryError(f"unknown lesson kind: {kind}")
        if source not in LESSON_SOURCES:
            raise FactoryError(f"unknown lesson source: {source}")
        if not str(reason).strip():
            raise FactoryError("a lesson requires a stated reason")
        if vehicle not in {"equity", "option"}:
            raise FactoryError("vehicle must be equity or option")
        if self.hypothesis(hypothesis_id) is None:
            raise KeyError(hypothesis_id)
        with closing(_connect(self.path)) as db, db:
            existing = db.execute(
                """SELECT lesson_id FROM factory_lessons
                   WHERE hypothesis_id=? AND variant_id=? AND kind=?""",
                (hypothesis_id, str(variant_id), kind)).fetchone()
            if existing is not None:
                # Replaying an exact underpowered/inconclusive point is
                # scientifically retryable, but the immutable first lesson
                # cannot be relinked.  Preserve the new citation as a
                # separate append-only retry lesson; account attempts remain
                # the authoritative confirmatory budget.
                if parent_lesson_id and kind == "tuning":
                    retry_kind = "tuning_retry"
                    retry = db.execute(
                        "SELECT lesson_id FROM factory_lessons WHERE "
                        "hypothesis_id=? AND variant_id=? AND kind=?",
                        (hypothesis_id, str(variant_id), retry_kind)).fetchone()
                    if retry is not None:
                        return str(retry["lesson_id"])
                    kind = retry_kind
                else:
                    return str(existing["lesson_id"])
            lesson_id = uuid.uuid4().hex
            db.execute(
                """INSERT INTO factory_lessons
                   (lesson_id,hypothesis_id,vehicle,family,variant_id,kind,
                    source,reason,changed_json,diagnosis_json,evidence_json,
                    created_at,parent_lesson_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    lesson_id, hypothesis_id, vehicle, str(family),
                    str(variant_id), kind, source,
                    " ".join(str(reason).split()),
                    canonical_json(dict(changed or {})),
                    canonical_json(dict(diagnosis or {})),
                    canonical_json(dict(evidence or {})),
                    datetime.now().timestamp(),
                    str(parent_lesson_id) if parent_lesson_id else None,
                ))
            return lesson_id

    def resolve_lesson_ref(self, ref: str) -> str | None:
        """Map a short citation back to the lesson it names, or ``None``.

        Proposals cite the truncated id the brief carried, so the citation has
        to be resolved before it can be stored as a foreign key.  An
        unresolvable reference is dropped rather than stored, which keeps a
        fabricated citation out of the chain.
        """
        text = str(ref or "").strip().lower()
        if not text:
            return None
        with closing(_connect(self.path)) as db:
            rows = db.execute(
                "SELECT lesson_id FROM factory_lessons WHERE lesson_id LIKE ? || '%'"
                " LIMIT 2", (text,)).fetchall()
        return str(rows[0]["lesson_id"]) if len(rows) == 1 else None

    def failed_variant_ids(self, *, vehicle: str,
                           family: str | None = None) -> set[str]:
        """Variants whose powered evidence rejected a useful edge.

        A merely adequate non-pass can still be positive or statistically
        inconclusive. Only an explicit upper-bound rejection closes a parameter
        point; legacy, underpowered and inconclusive results remain retestable.
        """
        parameters: list[Any] = [vehicle]
        clause = ""
        if family is not None:
            clause = " AND l.family=?"
            parameters.append(family)
        with closing(_connect(self.path)) as db:
            rows = db.execute(
                """SELECT DISTINCT l.variant_id FROM factory_lessons l
                   JOIN factory_lesson_outcomes o ON o.lesson_id=l.lesson_id
                   WHERE l.vehicle=?
                     AND o.classification='adequate_negative_rejection'"""
                + clause, parameters).fetchall()
            # Budget-closed exact variants are also terminal for selection,
            # but remain distinguishable in the durable closure table/report.
            closed = db.execute(
                "SELECT DISTINCT c.variant_id FROM factory_variant_closures c "
                "JOIN factory_hypotheses h ON h.hypothesis_id=c.hypothesis_id "
                "WHERE c.vehicle=? AND c.mode IN ('budget','scientific')" +
                (" AND h.family=?" if family is not None else ""),
                parameters).fetchall()
        return ({str(row["variant_id"]) for row in rows} |
                {str(row["variant_id"]) for row in closed})

    def account_attempts(self, hypothesis_id: str, variant_id: str) -> int:
        """Count all durable account rows for one exact variant."""
        with closing(_connect(self.path)) as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM factory_accounts "
                "WHERE hypothesis_id=? AND variant_id=?",
                (str(hypothesis_id), str(variant_id))).fetchone()
        return int(row["n"] if row else 0)

    def variant_attempts(self, hypothesis_id: str, variant_id: str) -> int:
        """Count eligible confirmatory attempts for one exact variant.

        Underpowered accounts are diagnostic observations and do not spend the
        finite confirmatory budget.  Likewise a passing account is not a
        non-passing attempt that can be retired by budget.  The gate is read
        from each immutable account result, so a restart cannot lose or
        inflate the eligible prefix.
        """
        with closing(_connect(self.path)) as db:
            rows = db.execute(
                "SELECT result_json FROM factory_accounts "
                "WHERE hypothesis_id=? AND variant_id=?",
                (str(hypothesis_id), str(variant_id))).fetchall()
        eligible = 0
        for row in rows:
            try:
                result = json.loads(row["result_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            gate = result.get("gate") if isinstance(result, Mapping) else None
            if (not isinstance(gate, Mapping) or
                    gate.get("sample_adequate") is not True or
                    gate.get("heldout_sample_adequate") is not True or
                    gate.get("passes") is True):
                continue
            eligible += 1
        return eligible

    def close_variant(self, hypothesis_id: str, variant_id: str, *,
                      vehicle: str, mode: str, reason: str,
                      attempts: int | None = None,
                      evidence: Mapping | None = None) -> dict[str, Any]:
        """Persist one exact variant's terminal closure idempotently.

        Closure is append-only and intentionally separate from lessons: a
        lesson can be inconclusive or underpowered, while this row means the
        exact variant is no longer eligible for scheduling.
        """
        if mode not in VARIANT_CLOSURE_MODES:
            raise FactoryError("unknown variant closure mode")
        if vehicle not in {"equity", "option"}:
            raise FactoryError("vehicle must be equity or option")
        if not str(reason).strip():
            raise FactoryError("variant closure reason is required")
        if self.hypothesis(hypothesis_id) is None:
            raise KeyError(hypothesis_id)
        eligible_count = self.variant_attempts(hypothesis_id, variant_id)
        if attempts is not None:
            try:
                requested_count = int(attempts)
            except (TypeError, ValueError, OverflowError) as exc:
                raise FactoryError("variant attempts must be an integer") from exc
            if requested_count != eligible_count:
                raise FactoryError(
                    "variant closure attempts must match durable eligible "
                    f"confirmatory count ({eligible_count})")
        count = eligible_count
        total_count = self.account_attempts(hypothesis_id, variant_id)
        with closing(_connect(self.path)) as db, db:
            existing = db.execute(
                "SELECT * FROM factory_variant_closures WHERE hypothesis_id=? AND variant_id=?",
                (str(hypothesis_id), str(variant_id))).fetchone()
            if existing is not None:
                return dict(existing) | {"evidence": json.loads(existing["evidence_json"]),
                                         "account_attempts_total": total_count}
            closure_id = uuid.uuid4().hex
            db.execute(
                """INSERT INTO factory_variant_closures
                   (closure_id,hypothesis_id,vehicle,variant_id,mode,reason,
                    attempts,evidence_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (closure_id, str(hypothesis_id), str(vehicle), str(variant_id),
                 str(mode), " ".join(str(reason).split()), count,
                    canonical_json({**dict(evidence or {}),
                                    "eligible_confirmatory_attempts": count,
                                    "account_attempts_total": total_count}),
                 datetime.now().timestamp()))
            return {"closure_id": closure_id, "hypothesis_id": str(hypothesis_id),
                    "vehicle": str(vehicle), "variant_id": str(variant_id),
                    "mode": str(mode), "reason": " ".join(str(reason).split()),
                    "attempts": count,
                    "account_attempts_total": total_count,
                    "evidence": {**dict(evidence or {}),
                                  "eligible_confirmatory_attempts": count,
                                  "account_attempts_total": total_count}}

    def variant_closures(self, *, vehicle: str | None = None,
                         hypothesis_id: str | None = None) -> list[dict[str, Any]]:
        where, params = [], []
        if vehicle is not None:
            where.append("vehicle=?"); params.append(str(vehicle))
        if hypothesis_id is not None:
            where.append("hypothesis_id=?"); params.append(str(hypothesis_id))
        clause = " WHERE " + " AND ".join(where) if where else ""
        with closing(_connect(self.path)) as db:
            rows = db.execute("SELECT * FROM factory_variant_closures" + clause +
                              " ORDER BY created_at,closure_id", params).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json"))
            output.append(item)
        return output

    def closed_variant_ids(self, *, vehicle: str,
                           hypothesis_id: str | None = None) -> set[str]:
        return {str(item["variant_id"]) for item in self.variant_closures(
            vehicle=vehicle, hypothesis_id=hypothesis_id)}

    def grade_lesson(self, hypothesis_id: str, variant_id: str, *, kind: str,
                     outcome: Mapping) -> str | None:
        """Attach the gate's verdict to the reason that predicted it.

        Returns ``None`` when nothing proposed this variant with a reason, so
        grading a deterministic path that predates lessons is a no-op rather
        than an error.
        """
        with closing(_connect(self.path)) as db, db:
            lesson = db.execute(
                """SELECT lesson_id FROM factory_lessons
                   WHERE hypothesis_id=? AND variant_id=? AND kind=?""",
                (hypothesis_id, str(variant_id), kind)).fetchone()
            if lesson is None and kind == "tuning":
                lesson = db.execute(
                    """SELECT lesson_id FROM factory_lessons
                       WHERE hypothesis_id=? AND variant_id=? AND kind='tuning_retry'
                       ORDER BY created_at DESC LIMIT 1""",
                    (hypothesis_id, str(variant_id))).fetchone()
            if lesson is None:
                return None
            lesson_id = str(lesson["lesson_id"])
            if db.execute("SELECT 1 FROM factory_lesson_outcomes WHERE lesson_id=?",
                          (lesson_id,)).fetchone() is not None:
                return lesson_id
            classification = str(outcome.get("classification") or (
                "proved" if outcome.get("passed") else
                "underpowered" if outcome.get("underpowered") else
                "adequate_inconclusive"))
            db.execute("""INSERT INTO factory_lesson_outcomes (
                    outcome_id,lesson_id,passed,underpowered,classification,
                    fit_delta,heldout_delta,heldout_net_pnl,q_value,failed_checks_json,
                    gate_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (
                uuid.uuid4().hex, lesson_id,
                1 if outcome.get("passed") else 0,
                1 if outcome.get("underpowered") else 0,
                classification, _real(outcome.get("fit_delta")),
                _real(outcome.get("heldout_delta")),
                _real(outcome.get("heldout_net_pnl")),
                _real(outcome.get("q_value")),
                canonical_json(list(outcome.get("failed_checks") or [])),
                str(outcome["gate_hash"]) if outcome.get("gate_hash") else None,
                datetime.now().timestamp(),
            ))
            return lesson_id

    def lessons(self, *, vehicle: str | None = None, family: str | None = None,
                hypothesis_id: str | None = None, graded_only: bool = False,
                limit: int = 50) -> list[dict]:
        """Return graded proposal reasons, most recent first."""
        where = []
        parameters: list[Any] = []
        for column, value in (("l.vehicle", vehicle), ("l.family", family),
                              ("l.hypothesis_id", hypothesis_id)):
            if value is not None:
                where.append(f"{column}=?")
                parameters.append(value)
        if graded_only:
            where.append("o.outcome_id IS NOT NULL")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        parameters.append(max(1, int(limit)))
        with closing(_connect(self.path)) as db:
            rows = db.execute(
                """SELECT l.*, o.passed, o.underpowered, o.classification,
                          o.fit_delta, o.heldout_delta,
                          o.heldout_net_pnl, o.q_value, o.failed_checks_json,
                          o.gate_hash, o.outcome_id
                   FROM factory_lessons l
                   LEFT JOIN factory_lesson_outcomes o ON o.lesson_id=l.lesson_id"""
                + clause + " ORDER BY l.created_at DESC, l.lesson_id DESC LIMIT ?",
                parameters).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["changed"] = json.loads(item.pop("changed_json"))
            item["diagnosis"] = json.loads(item.pop("diagnosis_json"))
            item["evidence"] = json.loads(item.pop("evidence_json"))
            failed = item.pop("failed_checks_json")
            graded = item.pop("outcome_id") is not None
            item["outcome"] = ({
                "passed": bool(item["passed"]),
                "underpowered": bool(item["underpowered"]),
                "classification": item["classification"],
                "fit_delta": item["fit_delta"],
                "heldout_delta": item["heldout_delta"],
                "heldout_net_pnl": item["heldout_net_pnl"],
                "q_value": item["q_value"],
                "failed_checks": json.loads(failed) if failed else [],
                "gate_hash": item["gate_hash"],
            } if graded else None)
            for key in ("passed", "underpowered", "classification", "fit_delta", "heldout_delta",
                        "heldout_net_pnl", "q_value", "gate_hash"):
                item.pop(key, None)
            output.append(item)
        return output

    def add_account(self, cycle_id: str, hypothesis_id: str, result: Mapping) -> None:
        account = result["account"]
        with closing(_connect(self.path)) as db, db:
            db.execute("INSERT INTO factory_accounts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                account["account_id"], cycle_id, hypothesis_id, result["variant_id"],
                result["vehicle"], account["starting_cash"], account["ending_equity"],
                account["realized_pnl"], account["max_drawdown"], account["trades"],
                result["worker_pid"], canonical_json(dict(result)), datetime.now().timestamp(),
            ))
            gate = result.get("gate") if isinstance(result, Mapping) else None
            envelope = gate.get("verified_gate") if isinstance(gate, Mapping) else None
            heldout = (envelope.get("heldout_source")
                       if isinstance(envelope, Mapping) else
                       (gate.get("heldout_source") or gate.get("_heldout_rows")
                        if isinstance(gate, Mapping) else ()))
            # Underpowered siblings are diagnostic only.  They must not claim
            # held-out evidence (or advance a boundary) merely by being
            # persisted as an account.  Adequacy is checked independently for
            # every variant, so one thin sibling cannot veto a qualified pass.
            eligible = bool(isinstance(gate, Mapping) and
                            gate.get("sample_adequate") is True and
                            gate.get("heldout_sample_adequate") is True)
            if eligible:
                try:
                    self._claim_evidence_db(
                        db, cycle_id=cycle_id, hypothesis_id=hypothesis_id,
                        vehicle=str(result["vehicle"]), variant_id=str(result["variant_id"]),
                        kind="heldout", sessions=self._sessions_from_rows(heldout))
                except FactoryError:
                    # Synthetic/replayed worker rows can be produced after a
                    # previous claim (for example during crash-recovery retry).
                    # Preserve the immutable account for audit; the scheduler
                    # filters consumed sessions before a new evaluation.
                    pass

    @staticmethod
    def _sessions_from_rows(rows: Any) -> tuple[str, ...]:
        if not isinstance(rows, (list, tuple)):
            return ()
        values = {str(row.get("session_date") or "") for row in rows
                  if isinstance(row, Mapping) and row.get("session_date")}
        return tuple(sorted(item for item in values if item))

    @staticmethod
    def _claim_evidence_db(db: sqlite3.Connection, *, cycle_id: str,
                           hypothesis_id: str, vehicle: str,
                           variant_id: str, kind: str,
                           sessions: tuple[str, ...] | list[str]) -> dict[str, Any] | None:
        """Atomically reserve a session set for one confirmatory claim.

        Claims from the same cycle are idempotent and may share a held-out
        partition across variants. Any overlap with a prior cycle is rejected,
        regardless of whether that prior claim was held-out or qualification.
        """
        normalized = tuple(sorted({str(item) for item in (sessions or ())
                                   if str(item)}))
        if not normalized:
            return None
        if kind not in {"heldout", "qualification"}:
            raise FactoryError(f"unknown evidence kind: {kind}")
        existing = db.execute(
            """SELECT * FROM factory_evidence_claims
               WHERE cycle_id=? AND hypothesis_id=? AND variant_id=? AND kind=?""",
            (str(cycle_id), str(hypothesis_id), str(variant_id), str(kind))).fetchone()
        if existing is not None:
            return dict(existing)
        rows = db.execute(
            "SELECT cycle_id,kind,sessions_json FROM factory_evidence_claims WHERE vehicle=?",
            (str(vehicle),)).fetchall()
        requested = set(normalized)
        for row in rows:
            # Same-cycle variants may share one held-out partition. A
            # qualification claim must still be disjoint from held-out rows
            # in that cycle, since those are different evidence roles.
            if (str(row["cycle_id"]) == str(cycle_id) and
                    str(row["kind"]) == str(kind)):
                continue
            try:
                prior = set(json.loads(row["sessions_json"]))
            except (TypeError, json.JSONDecodeError):
                prior = set()
            overlap = sorted(requested & {str(item) for item in prior})
            if overlap:
                raise FactoryError(
                    "confirmatory evidence session already consumed: "
                    + ", ".join(overlap[:3]))
        digest = content_hash({"vehicle": str(vehicle), "kind": str(kind),
                               "sessions": normalized})
        claim_id = uuid.uuid4().hex
        db.execute(
            """INSERT INTO factory_evidence_claims
               (claim_id,cycle_id,hypothesis_id,vehicle,variant_id,kind,
                sessions_json,session_digest,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (claim_id, str(cycle_id), str(hypothesis_id), str(vehicle),
             str(variant_id), str(kind), canonical_json(list(normalized)),
             digest, datetime.now().timestamp()))
        return {"claim_id": claim_id, "cycle_id": str(cycle_id),
                "hypothesis_id": str(hypothesis_id), "vehicle": str(vehicle),
                "variant_id": str(variant_id), "kind": str(kind),
                "sessions": list(normalized), "session_digest": digest}

    def claim_qualification(self, cycle_id: str, hypothesis_id: str, *,
                            vehicle: str, variant_id: str,
                            sessions: tuple[str, ...] | list[str]) -> dict[str, Any] | None:
        """Reserve a sealed qualification window before it is released.

        The insert is the crash-safe boundary: once it commits, a restarted
        process cannot spend that session set as a fresh qualification window.
        """
        with closing(_connect(self.path)) as db, db:
            return self._claim_evidence_db(
                db, cycle_id=cycle_id, hypothesis_id=hypothesis_id,
                vehicle=vehicle, variant_id=variant_id, kind="qualification",
                sessions=sessions)

    def evidence_sessions(self, vehicle: str, *, kind: str | None = None) -> set[str]:
        """Return all session dates consumed by prior confirmatory claims."""
        clause = " AND kind=?" if kind is not None else ""
        parameters: tuple[Any, ...] = (str(vehicle),) + (
            (str(kind),) if kind is not None else ())
        with closing(_connect(self.path)) as db:
            rows = db.execute(
                "SELECT sessions_json FROM factory_evidence_claims "
                "WHERE vehicle=?" + clause, parameters).fetchall()
        output: set[str] = set()
        for row in rows:
            try:
                values = json.loads(row["sessions_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(values, list):
                output.update(str(item) for item in values if str(item))
        return output

    def last_boundary(self, hypothesis_id: str, vehicle: str) -> str | None:
        # A factory account is diagnostic evidence, not a consumed forward
        # boundary: it is written before the gate knows whether all intended
        # shadow variants were adequately powered.  Read boundaries only from
        # durable EdgeLedger proof runs, so an underpowered sibling cannot make
        # the next cycle skip unseen observations.
        with closing(_connect(self.path)) as db:
            try:
                rows = db.execute("""SELECT r.run_id, r.candidate_id,
                        r.heldout_end, r.fit_end, r.metrics_json, c.axes_json,
                        e.payload_json,
                        e.evidence_hash
                    FROM runs r JOIN candidates c ON c.candidate_id=r.candidate_id
                    JOIN evidence e ON e.run_id=r.run_id AND e.kind='verified_gate'
                    WHERE r.vehicle=? AND r.lane='shadow'""",
                    (vehicle,)).fetchall()
            except sqlite3.Error:
                return None
        proof_ledger = EdgeLedger(self.path)
        values = []
        for row in rows:
            try:
                axes = json.loads(row["axes_json"] or "{}")
                if axes.get("hypothesis_id") != hypothesis_id:
                    continue
                verified = proof_ledger.verified_run(str(row["run_id"]))
                if (verified is None or verified[0].get("candidate_id") !=
                        row["candidate_id"] or verified[0].get("lane") != "shadow" or
                        verified[0].get("vehicle") != vehicle or
                        verified[1].get("passes") is not True):
                    continue
                payload = json.loads(row["payload_json"] or "{}")
                if (row["evidence_hash"] != content_hash(payload) or
                        not isinstance(payload, Mapping)):
                    continue
                gate = payload.get("gate")
                if (not isinstance(gate, Mapping) or
                        payload.get("gate_hash") != gate.get("content_hash") or
                        not verify_gate_envelope(gate)):
                    continue
                for value in (row["heldout_end"], row["fit_end"]):
                    if value:
                        values.append(str(value))
                metrics = json.loads(row["metrics_json"] or "{}")
                run_gate = metrics.get("gate") if isinstance(metrics, Mapping) else None
                qualification = (run_gate.get("qualification")
                                 if isinstance(run_gate, Mapping) else None)
                if isinstance(qualification, Mapping):
                    values.extend(str(item) for item in
                                   (qualification.get("sessions") or ()) if item)
            except json.JSONDecodeError:
                continue
        return max(values) if values else None

    def existing_cycle(self, dataset_hash: str, vehicle: str,
                       identity: Mapping | str | None = None) -> dict | None:
        identity_hash = (str(identity.get("identity_hash")) if isinstance(identity, Mapping)
                         and identity.get("identity_hash") else
                         str(identity) if isinstance(identity, str) else None)
        with closing(_connect(self.path)) as db:
            if identity_hash:
                row = db.execute(
                    "SELECT result_json FROM factory_cycles WHERE identity_hash=?",
                    (identity_hash,)).fetchone()
            else:
                row = db.execute(
                    "SELECT result_json FROM factory_cycles WHERE dataset_hash=? AND vehicle=? "
                    "ORDER BY created_at DESC LIMIT 1", (dataset_hash, vehicle)).fetchone()
        return json.loads(row["result_json"]) if row else None

    def add_cycle(self, cycle_id: str, dataset_hash: str, vehicle: str,
                  workers: int, strategies: int, variants: int,
                  result: Mapping, *, identity: Mapping | str | None = None,
                  provenance: Mapping | None = None) -> None:
        if identity is None:
            identity = experiment_identity(
                dataset_hash=dataset_hash, vehicle=vehicle,
                provenance=provenance)
        elif isinstance(identity, str):
            identity = {"schema": FACTORY_IDENTITY_SCHEMA,
                        "dataset_hash": str(dataset_hash), "vehicle": str(vehicle),
                        "identity_hash": identity}
        else:
            identity = dict(identity)
            identity.setdefault("identity_hash", content_hash(identity))
        recorded = dict(result)
        recorded.setdefault("experiment_identity", identity)
        with closing(_connect(self.path)) as db, db:
            db.execute("""INSERT INTO factory_cycles
                (cycle_id,dataset_hash,vehicle,identity_hash,identity_json,workers,
                 strategies,variants,result_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (
                cycle_id, dataset_hash, vehicle, identity["identity_hash"],
                canonical_json(identity), workers, strategies, variants,
                canonical_json(recorded), datetime.now().timestamp(),
            ))

    def freeze_dependence_policy(self, cycle_id: str, *, vehicle: str,
                                 cutoff: float | None = None) -> dict[str, Any]:
        """Freeze prior-cycle dependence before the current cycle is tested.

        Only account rows belonging to completed ``factory_cycles`` whose
        completion timestamp is strictly before ``cutoff`` are considered.
        The append-only policy row records the exact source cycle ids,
        thresholds, cluster map, and content hash used by runtime allocation.
        """
        if vehicle not in {"equity", "option"}:
            raise FactoryError("vehicle must be equity or option")
        resolved_cutoff = (datetime.now().timestamp() if cutoff is None else float(cutoff))
        if not math.isfinite(resolved_cutoff):
            raise FactoryError("dependence policy cutoff must be finite")
        with closing(_connect(self.path)) as db:
            existing = db.execute(
                "SELECT * FROM factory_dependence_policies WHERE target_cycle_id=? AND vehicle=?",
                (str(cycle_id), str(vehicle))).fetchone()
            if existing is not None:
                return self._decode_dependence_policy(existing)
            rows = db.execute(
                """SELECT a.cycle_id,a.result_json,c.created_at
                   FROM factory_accounts a JOIN factory_cycles c
                     ON c.cycle_id=a.cycle_id
                   WHERE a.vehicle=? AND c.created_at < ?
                   ORDER BY c.created_at,c.cycle_id,a.account_id""",
                (str(vehicle), resolved_cutoff)).fetchall()
        observations: list[dict[str, Any]] = []
        for row in rows:
            try:
                result = json.loads(row["result_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(result, Mapping):
                continue
            spec = result.get("rule_spec")
            gate = result.get("gate")
            family = (spec.get("family") if isinstance(spec, Mapping) else
                      result.get("family"))
            if not family or not isinstance(gate, Mapping):
                continue
            for paired in _paired_session_deltas(result, vehicle=vehicle):
                observations.append({"cycle_id": str(row["cycle_id"]),
                                     "family": str(family), **paired})
        policy = deterministic_dependence_map(
            observations, cutoff=resolved_cutoff,
            min_sessions=DEPENDENCE_MIN_COMPLETE_SESSIONS,
            min_cycles=DEPENDENCE_MIN_PRIOR_CYCLES,
            threshold=DEPENDENCE_CORRELATION_THRESHOLD)
        source_cycles = list(policy.get("source_cycles") or ())
        cluster_map = dict(policy.get("cluster_map") or {})
        evidence = {key: value for key, value in policy.items()
                    if key not in {"schema", "cutoff", "cluster_map"}}
        body = {"schema": DEPENDENCE_POLICY_SCHEMA, "version": 1,
                "target_cycle_id": str(cycle_id), "vehicle": str(vehicle),
                "cutoff": resolved_cutoff, "source_cycles": source_cycles,
                "cluster_map": cluster_map, "evidence": evidence}
        policy_hash = content_hash(body)
        policy_id = uuid.uuid4().hex
        with closing(_connect(self.path)) as db, db:
            db.execute(
                """INSERT INTO factory_dependence_policies
                   (policy_id,target_cycle_id,vehicle,schema,cutoff,
                    source_cycles_json,cluster_map_json,evidence_json,
                    policy_hash,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (policy_id, str(cycle_id), str(vehicle), DEPENDENCE_POLICY_SCHEMA,
                 resolved_cutoff, canonical_json(source_cycles),
                 canonical_json(cluster_map), canonical_json(evidence),
                 policy_hash, datetime.now().timestamp()))
        return {**body, "policy_id": policy_id, "policy_hash": policy_hash,
                "policy_digest": dependence_policy_digest(body),
                "verified_persisted": True}

    @staticmethod
    def _decode_dependence_policy(row: Mapping[str, Any]) -> dict[str, Any]:
        try:
            source_cycles = json.loads(row["source_cycles_json"] or "[]")
            cluster_map = json.loads(row["cluster_map_json"] or "{}")
            evidence = json.loads(row["evidence_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return {"verified_persisted": False, "reason": "malformed_policy"}
        body = {"schema": str(row["schema"]), "version": 1,
                "target_cycle_id": str(row["target_cycle_id"]),
                "vehicle": str(row["vehicle"]), "cutoff": float(row["cutoff"]),
                "source_cycles": source_cycles, "cluster_map": cluster_map,
                "evidence": evidence}
        expected = content_hash(body)
        verified = expected == str(row["policy_hash"] or "")
        return {**body, "policy_id": str(row["policy_id"]),
                "policy_hash": str(row["policy_hash"]),
                "policy_digest": dependence_policy_digest(body),
                "verified_persisted": bool(verified)}

    def latest_dependence_policy(self, *, vehicle: str) -> dict[str, Any] | None:
        """Return the newest hash-verified policy for a completed target cycle."""
        if vehicle not in {"equity", "option"}:
            return None
        with closing(_connect(self.path)) as db:
            rows = db.execute(
                "SELECT * FROM factory_dependence_policies WHERE vehicle=? "
                "ORDER BY created_at DESC,policy_id DESC", (str(vehicle),)).fetchall()
            for row in rows:
                policy = self._decode_dependence_policy(row)
                if not policy.get("verified_persisted"):
                    continue
                completed = db.execute(
                    "SELECT 1 FROM factory_cycles WHERE cycle_id=?",
                    (policy.get("target_cycle_id"),)).fetchone()
                if completed is None:
                    # A crashed/in-progress cycle's freeze is audit evidence,
                    # not a runtime authorization.
                    continue
                return policy
        return None

    def dependence_policies(self, *, vehicle: str | None = None) -> list[dict[str, Any]]:
        """Read all persisted policies for report/audit consumers."""
        clause = " WHERE vehicle=?" if vehicle else ""
        args = (str(vehicle),) if vehicle else ()
        with closing(_connect(self.path)) as db:
            rows = db.execute("SELECT * FROM factory_dependence_policies" + clause +
                              " ORDER BY created_at,policy_id", args).fetchall()
        return [self._decode_dependence_policy(row) for row in rows]

    def next_fdr_allocation(self, scope: str, *, alpha: float = .05) -> dict[str, Any]:
        """Preview the next durable allocation without spending it."""
        nominal = float(alpha)
        if not math.isfinite(nominal) or not 0 < nominal <= 1:
            raise FactoryError("alpha must be in (0,1]")
        resolved_scope = str(scope)
        with closing(_connect(self.path)) as db:
            rows = db.execute(
                "SELECT * FROM factory_fdr WHERE scope=? "
                "ORDER BY created_at,decision_id", (resolved_scope,)).fetchall()
        nominal = _locked_scope_alpha(list(rows), nominal)
        method, p_kind = _resolved_fdr_semantics(resolved_scope, list(rows))
        test_index, allocated = _fdr_allocation_for_method(
            list(rows), nominal, method)
        return {"scope": resolved_scope, "alpha": nominal,
                "allocated_alpha": allocated, "tests": test_index,
                "cumulative": True, "preview": True,
                **_fdr_metadata(method, p_kind, nominal)}

    def record_fdr_decision(self, scope: str, test_id: str, p_value: float,
                            *, alpha: float = .05) -> dict[str, Any]:
        """Persist one deterministic cumulative online-FDR decision.

        For v5 and the active v6 live-shadow scopes, ``p_value`` is the raw
        confirmatory statistic and the allocation is standard LORD++ under
        that scope's preregistered initial wealth. v2/v3/v4 scopes retain
        their legacy allocation for audit compatibility.
        Family/global batch-adjusted q-values are candidate-selection summaries and must
        not be passed to a raw-p scope.

        Callers claiming FDR control must establish independent null p-values;
        conditional super-uniformity alone supports the weaker mFDR result in
        the cited LORD++ theorem. Development and offline-forward screens
        deliberately defer cumulative testing to the strictly newer
        live-shadow boundary.
        """
        if isinstance(p_value, bool) or isinstance(alpha, bool):
            raise FactoryError("p_value must be in [0,1] and alpha in (0,1]")
        p = float(p_value); nominal = float(alpha)
        if (not math.isfinite(p) or not 0 <= p <= 1 or
                not math.isfinite(nominal) or not 0 < nominal <= 1):
            raise FactoryError("p_value must be in [0,1] and alpha in (0,1]")
        scope, test_id = str(scope), str(test_id)
        # Validate an explicit scope version before entering the write
        # transaction. Existing custom/unversioned rows are resolved from
        # their durable method identity once loaded below.
        _fdr_semantics(scope)
        with closing(_connect(self.path)) as db, db:
            # Allocation and insertion are one serialized state transition.
            # A deferred transaction lets concurrent workers both read the
            # same prefix and spend the same LORD allocation before either
            # INSERT becomes visible.
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM factory_fdr WHERE scope=? "
                "ORDER BY created_at,decision_id", (scope,)).fetchall()
            nominal = _locked_scope_alpha(list(rows), nominal)
            method, p_kind = _resolved_fdr_semantics(scope, list(rows))
            for existing_index, existing in enumerate(rows, start=1):
                if str(existing["test_id"]) == test_id:
                    return dict(existing) | {
                        "decision": bool(existing["decision"]),
                        "tests": existing_index, "cumulative": True,
                        **_fdr_metadata(method, p_kind, nominal),
                    }
            test_index, allocated = _fdr_allocation_for_method(
                list(rows), nominal, method)
            decision = bool(p <= allocated)
            db.execute("""INSERT INTO factory_fdr
                (decision_id,scope,test_id,p_value,alpha,allocated_alpha,
                 decision,created_at,method,method_version)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (
                uuid.uuid4().hex, scope, test_id, p, nominal, allocated,
                int(decision), datetime.now().timestamp(), method,
                _fdr_metadata(method, p_kind, nominal)["method_version"]))
        return {"scope": scope, "test_id": test_id, "p_value": p,
                "alpha": nominal, "allocated_alpha": allocated,
                "decision": decision, "tests": test_index,
                "cumulative": True,
                **_fdr_metadata(method, p_kind, nominal)}

    # Explicit aliases are the integration seam for strategy_factory callers.
    online_fdr = record_fdr_decision
    cumulative_fdr = record_fdr_decision

    def fdr_state(self, scope: str = "global", *, alpha: float = .05) -> dict[str, Any]:
        """Return an auditable snapshot of a cumulative FDR scope.

        ``alpha`` is the preregistered nominal level, not a spendable LORD
        wealth balance.  Once a scope has a durable decision its value is
        immutable and a conflicting requested value is rejected, just as it
        is by :meth:`next_fdr_allocation` and
        :meth:`record_fdr_decision`.  The preview is computed from the current
        decision prefix and does not append a row or reserve an allocation.

        The explicit depletion/resolution fields are intentionally diagnostic:
        they describe whether a finite next allocation can be resolved by a
        caller, and never turn ``alpha - alpha_spent`` into an authorization
        quantity.
        """
        nominal = float(alpha)
        if not math.isfinite(nominal) or not 0 < nominal <= 1:
            raise FactoryError("alpha must be in (0,1]")
        resolved_scope = str(scope)
        with closing(_connect(self.path)) as db:
            rows = db.execute(
                "SELECT * FROM factory_fdr WHERE scope=? "
                "ORDER BY created_at,decision_id", (resolved_scope,)).fetchall()
        nominal = _locked_scope_alpha(list(rows), nominal)
        decisions = [dict(row) for row in rows]
        method, p_kind = _resolved_fdr_semantics(resolved_scope, list(rows))
        test_index, next_allocated = _fdr_allocation_for_method(
            list(rows), nominal, method)
        # Floating point underflow is the only practical way this telescoping
        # allocation reaches zero.  Keep the threshold explicit so readers do
        # not mistake ``alpha_spent`` for remaining online-FDR wealth.
        resolution_epsilon = float(math.ulp(max(1.0, nominal)))
        resolution_exhausted = bool(next_allocated <= resolution_epsilon)
        preview = {
            "scope": resolved_scope,
            "alpha": nominal,
            "allocated_alpha": next_allocated,
            "next_allocated_alpha": next_allocated,
            "tests": test_index,
            "cumulative": True,
            **_fdr_metadata(method, p_kind, nominal),
            "preview": True,
        }
        return {
            "scope": resolved_scope,
            "cumulative": True,
            "alpha": nominal,
            "alpha_locked": bool(rows),
            "alpha_immutable": True,
            "alpha_source": "durable" if rows else "default_preview",
            "alpha_spent": sum(float(row["allocated_alpha"]) for row in rows),
            "tests": len(rows),
            "discoveries": sum(int(row["decision"]) for row in rows),
            "next_test": test_index,
            "next_allocated_alpha": next_allocated,
            "next_preview": preview,
            "next_allocation": preview,
            "next_allocation_preview": preview,
            "preview": preview,
            "resolution_epsilon": resolution_epsilon,
            "depleted": bool(next_allocated <= 0.0),
            "allocation_depleted": bool(next_allocated <= 0.0),
            "is_depleted": bool(next_allocated <= 0.0),
            "resolution_exhausted": resolution_exhausted,
            "resolution_available": not resolution_exhausted,
            "has_resolution": not resolution_exhausted,
            "resolution_reason": ("next_allocation_available"
                                  if not resolution_exhausted
                                  else "next_allocation_underflow"),
            "resolution_status": "exhausted" if resolution_exhausted else "available",
            **_fdr_metadata(method, p_kind, nominal),
            "decisions": decisions,
        }

    online_fdr_state = fdr_state

    def status(self) -> dict:
        hypotheses = self.hypotheses()
        with closing(_connect(self.path)) as db:
            accounts = db.execute(
                "SELECT COUNT(*) AS n FROM factory_accounts").fetchone()["n"]
            cycles = db.execute(
                "SELECT COUNT(*) AS n FROM factory_cycles").fetchone()["n"]
            closures = db.execute(
                "SELECT COUNT(*) AS n FROM factory_variant_closures").fetchone()["n"]
        return {"schema": FACTORY_SCHEMA, "hypotheses": hypotheses,
                "accounts": int(accounts), "cycles": int(cycles),
                "variant_closures": int(closures)}


__all__ = [
    "ACTIVE_HYPOTHESIS_STATES", "FACTORY_SCHEMA", "FACTORY_IDENTITY_SCHEMA", "FACTORY_STATUSES",
    "VARIANT_CLOSURE_MODES",
    "CONFIRMATORY_SCOPE_VERSION", "CONFIRMATORY_SCOPE_VERSION_V5",
    "FDR_METHOD", "FDR_METHOD_V5", "FDR_METHOD_V6",
    "LEGACY_FDR_METHOD", "LEGACY_RAW_FDR_METHOD",
    "FDR_METHOD_VERSION", "FDR_METHOD_VERSION_V5", "FDR_METHOD_VERSION_V6",
    "FDR_INITIAL_WEALTH_FRACTION", "FDR_INITIAL_WEALTH_FRACTION_V5",
    "FDR_INITIAL_WEALTH_FRACTION_V6", "FDR_GAMMA_METHOD", "LESSON_KINDS",
    "LESSON_SOURCES", "FactoryError", "FactoryLedger",
    "deferred_fdr",
    "experiment_identity",
    "experiment_provenance",
]
