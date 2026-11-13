"""Import exact session-study hypotheses into main's research evidence checks.

This is a diagnostic bridge, not another EdgeLedger writer. Study session
returns are never converted to executed trades. Only explicitly bound,
trade-level execution observations enter the existing main gates. An imported
rule still needs a compatible main runtime/lifecycle, the complete gate and
strictly newer parity-matched live-shadow proof before it can authorize orders.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from . import gates
from .stats import benjamini_yekutieli
from studies.session_edges import grammar


SCHEMA = "session-candidate-review.v1"
REQUEST_SCHEMA = "session-candidate-request.v1"
EVIDENCE_SCHEMA = "session-candidate-executions.v1"
DISCOVERY_SCHEMA = "session-edges-discovery.v2"
_EVIDENCE_FIELDS = ("spec", "source", "cost_bps", "design", "neighbour_share", "holdout", "recent",
                    "data_last_session", "promoted_at", "experiment_name", "experiment_id")
_REQUIRED_DIGESTS = ("dataset_digest", "context_digest", "code_digest", "cost_digest", "protocol_digest")
_HARD_BLOCKERS = ("exact_main_runtime_and_lifecycle_contract_required",
                  "complete_main_gate_and_durable_online_fdr_required",
                  "strictly_newer_parity_matched_live_shadow_proof_required")


class CandidateReviewError(ValueError):
    """An imported candidate or execution observation has incompatible identity."""


def _hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise CandidateReviewError(f"{label} must be a SHA-256 identity")
    return value


def _session(value: Any) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise CandidateReviewError("execution sessions must be ISO dates") from exc
    if parsed.isoformat() != value:
        raise CandidateReviewError("execution sessions must be canonical ISO dates")
    return value


def _timestamp(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise CandidateReviewError("execution timestamps must be timezone-aware") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CandidateReviewError("execution timestamps must be timezone-aware")
    return parsed


def candidate_request(row: Mapping[str, Any], experiment: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze the exact hypothesis, simulation form and source experiment.

    Daily and minute specs retain their distinct family and kernel identities.
    No runner translation, historical tier or trade boolean enters authority.
    """
    if row.get("authorizing") is not False or row.get("requires_main_qualification") is not True:
        raise CandidateReviewError("study candidates must explicitly remain nonauthorizing")
    spec = grammar.normalise(row.get("spec"))
    manifest = experiment.get("manifest")
    if not isinstance(manifest, Mapping) or manifest.get("schema") != "session-edges-experiment.v2":
        raise CandidateReviewError("candidate needs its exact v2 experiment manifest")
    experiment_id = _digest(row.get("experiment_id"), "experiment_id")
    if experiment.get("id") != experiment_id or _hash(manifest) != experiment_id:
        raise CandidateReviewError("candidate experiment identity mismatch")
    if row.get("evidence_digest") != _hash({key: row.get(key) for key in _EVIDENCE_FIELDS}):
        raise CandidateReviewError("candidate source evidence digest mismatch")
    for name in _REQUIRED_DIGESTS:
        _digest(manifest.get(name), name)
    family = grammar.FAMILIES[spec["family"]]
    dataset_key = f"{family.data}:{spec['symbol']}"
    dataset = manifest.get("input_digests") or {}
    if not isinstance(dataset, Mapping) or dataset_key not in dataset:
        raise CandidateReviewError("candidate's exact instrument/data form is absent from the experiment")
    _digest(dataset[dataset_key], "candidate_dataset_digest")
    if _hash(dataset) != manifest["dataset_digest"]:
        raise CandidateReviewError("experiment dataset digest mismatch")
    code_files = manifest.get("code_files")
    if not isinstance(code_files, Mapping) or _hash(code_files) != manifest["code_digest"]:
        raise CandidateReviewError("experiment code digest mismatch")
    kernel = {"family": family.name, "data": family.data,
              "simulation_version": getattr(grammar, "SIMULATION_VERSION", "unversioned"),
              "code_digest": manifest["code_digest"],
              "source_code_files": dict(code_files),
              "execution_form": ("completed-daily-signal-next-session-open" if family.data == grammar.DAILY
                                 else "completed-minute-prefix-next-observed-bar"),
              "runner_translation_permitted": False}
    cutoff = row.get("data_last_session")
    if cutoff is not None:
        _session(cutoff)
    request = {"schema": REQUEST_SCHEMA, "spec": spec, "kernel": kernel,
               "execution_identity": _hash({"spec": spec, "kernel": kernel}),
               "experiment_name": row.get("experiment_name"), "experiment_id": experiment_id,
               "source_evidence_digest": row["evidence_digest"], "source_dataset_key": dataset_key,
               "source_dataset_digest": dataset[dataset_key],
               "source_identities": {name: manifest[name] for name in _REQUIRED_DIGESTS},
               "source_provenance": dict(manifest.get("source_provenance") or {}),
               "research_through": cutoff, "authorizing": False, "requires_main_qualification": True}
    request["request_id"] = _hash(request)
    return request


def _validate_request(request: Mapping[str, Any]) -> None:
    if (request.get("schema") != REQUEST_SCHEMA or request.get("authorizing") is not False or
            request.get("requires_main_qualification") is not True):
        raise CandidateReviewError("not a nonauthorizing exact-candidate request")
    if request.get("request_id") != _hash({k: v for k, v in request.items() if k != "request_id"}):
        raise CandidateReviewError("candidate request identity mismatch")
    spec = grammar.normalise(request.get("spec"))
    kernel = request.get("kernel") or {}
    if (spec != request["spec"] or kernel.get("family") != spec["family"] or
            kernel.get("data") != grammar.FAMILIES[spec["family"]].data or
            kernel.get("runner_translation_permitted") is not False or
            request.get("execution_identity") != _hash({"spec": spec, "kernel": kernel})):
        raise CandidateReviewError("candidate execution identity mismatch")


def _bound_rows(value: Any, request: Mapping[str, Any], *, control_identity: str | None = None) -> list[dict]:
    if not isinstance(value, list) or len(value) > gates.QUALIFICATION_MAX_ROWS:
        raise CandidateReviewError("execution evidence must be a bounded list of trade observations")
    rows: list[dict] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise CandidateReviewError("execution observation must be an object")
        row = dict(raw)
        if row.get("session_candidate_request_id") != request["request_id"]:
            raise CandidateReviewError("execution observation belongs to another candidate")
        name, identity = (("session_control_identity", control_identity) if control_identity else
                          ("session_execution_identity", request["execution_identity"]))
        if row.get(name) != identity:
            raise CandidateReviewError("execution observation has a different rule/control identity")
        if row.get("vehicle") != "equity" or row.get("symbol") != request["spec"]["symbol"]:
            raise CandidateReviewError("execution observation instrument/vehicle mismatch")
        _session(row.get("session_date"))
        key = row.get("opportunity_id")
        if not isinstance(key, str) or not key or key in seen:
            raise CandidateReviewError("execution opportunities must have unique identities")
        seen.add(key)
        if row.get("no_trade") is not True:
            entry, exit_at = _timestamp(row.get("entry_timestamp")), _timestamp(row.get("exit_timestamp"))
            if entry > exit_at:
                raise CandidateReviewError("execution exits before entry")
            for field in ("entry_price", "exit_price", "quantity", "risk_usd", "net_pnl"):
                value = row.get(field)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise CandidateReviewError(f"trade-level execution requires finite {field}")
                if field != "net_pnl" and value <= 0:
                    raise CandidateReviewError(f"trade-level execution requires positive {field}")
        rows.append(row)
    return rows


def review_execution_evidence(request: Mapping[str, Any], evidence: Mapping[str, Any] | None = None,
                              *, used_seals: set[str] | None = None) -> dict[str, Any]:
    """Run main's immutable floors and paired gates on exact bound observations.

    Seal use is exclusive within a review batch; main's durable qualification
    registry and proof remain mandatory across invocations. Caller booleans
    cannot prove preregistration, so ``preselected`` is deliberately false.
    """
    _validate_request(request)
    evidence = evidence or {}
    if evidence and (evidence.get("schema") != EVIDENCE_SCHEMA or
                     evidence.get("request_id") != request["request_id"]):
        raise CandidateReviewError("execution evidence request/schema mismatch")
    control_id = _digest(evidence.get("control_identity"), "control_identity") if evidence else None
    candidate = _bound_rows(evidence.get("candidate_rows", []), request)
    baseline = _bound_rows(evidence.get("baseline_rows", []), request, control_identity=control_id)
    final = _bound_rows(evidence.get("qualification_rows", []), request)
    final_baseline = _bound_rows(evidence.get("qualification_baseline_rows", []), request,
                                 control_identity=control_id)
    sessions = evidence.get("qualification_sessions", [])
    if not isinstance(sessions, list) or sessions != sorted(set(sessions)):
        raise CandidateReviewError("qualification sessions must be unique and chronological")
    for session in sessions:
        _session(session)
    development_sessions = {r["session_date"] for r in [*candidate, *baseline]}
    if final or final_baseline or sessions:
        cutoff = max([request.get("research_through") or "", *development_sessions])
        if not sessions or sessions[0] <= cutoff:
            raise CandidateReviewError("qualification seal reuses historical/development sessions")
        for row in [*candidate, *baseline]:
            if row.get("no_trade") is not True and _timestamp(row["exit_timestamp"]).date().isoformat() >= sessions[0]:
                raise CandidateReviewError("development payoff overlaps the qualification seal")
        if any(r["session_date"] not in sessions for r in [*final, *final_baseline]):
            raise CandidateReviewError("qualification observations are outside the sealed sessions")
        seal = _hash(sessions)
        if used_seals is not None:
            if seal in used_seals:
                raise CandidateReviewError("qualification seal already consumed by another reviewed candidate")
            used_seals.add(seal)
    feed = str(evidence.get("equity_feed") or "sip")
    projection = {name: gates.authorization_projection(rows, vehicle="equity", strict=True,
                                                        equity_feed=feed, equity_provider="alpaca")
                  for name, rows in (("candidate", candidate), ("baseline", baseline),
                                     ("qualification", final), ("qualification_baseline", final_baseline))}
    eligible = {name: p["eligible"] for name, p in projection.items()}
    fit, heldout = gates.chronological_split(eligible["candidate"])
    held_sessions = {row["session_date"] for row in heldout}
    held_base = [row for row in eligible["baseline"] if row["session_date"] in held_sessions]
    minimums = {"min_trades": gates.PROTOCOL_BACKTEST_MIN_TRADES,
                "min_sessions": gates.PROTOCOL_BACKTEST_MIN_SESSIONS,
                "min_clusters": gates.PROTOCOL_BACKTEST_MIN_CLUSTERS}
    common = {"vehicle": "equity", "equity_feed": feed, "equity_provider": "alpaca"}
    floors = {"fit": gates.structural_floor(fit, **common, **minimums),
              "heldout": gates.structural_floor(heldout, **common, **minimums)}
    paired = gates.matched_cluster_test(heldout, held_base, **common)
    rolling = gates.walk_forward_report(eligible["candidate"], eligible["baseline"], **common,
                                       min_test_trades=gates.ACTUAL_CONTROL_MIN_MATCHED,
                                       min_matched=gates.ACTUAL_CONTROL_MIN_MATCHED,
                                       requested_min_sessions=gates.PROTOCOL_BACKTEST_MIN_SESSIONS)
    qualification = gates.qualification_report(
        eligible["qualification"], eligible["qualification_baseline"], **common, sessions=sessions,
        candidate_id=request["request_id"], preselected=False,
        min_trades=gates.PROTOCOL_QUALIFICATION_MIN_TRADES,
        min_sessions=gates.PROTOCOL_QUALIFICATION_MIN_SESSIONS,
        min_clusters=gates.PROTOCOL_QUALIFICATION_MIN_CLUSTERS)
    blockers = list(_HARD_BLOCKERS)
    if not candidate:
        blockers.append("trade_level_execution_observations_required")
    if not all(floor["adequate"] for floor in floors.values()):
        blockers.append("main_fit_or_heldout_floor_not_met")
    if not paired["adequate"]:
        blockers.append("main_matched_control_floor_not_met")
    if not qualification["adequate"]:
        blockers.append("main_sealed_qualification_floor_not_met")
    if "index proxy" in request["spec"]["symbol"].lower():
        blockers.append("proxy_source_requires_exact_tradable_instrument_replay")
    return {"authorizing": False, "qualified": False, "request_id": request["request_id"],
            "scope": "diagnostic_execution_readiness_only", "blockers": blockers,
            "floors": floors, "paired_control": paired, "walk_forward": rolling,
            "qualification": qualification,
            "projection": {name: {k: v for k, v in p.items() if k not in {"raw", "eligible"}}
                           for name, p in projection.items()},
            "heldout_p_value": paired.get("p_value", 1.0)}


def review_discovery(record: Mapping[str, Any], evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build main candidate dossiers without mutating proofs, trials or FDR."""
    if record.get("schema") != DISCOVERY_SCHEMA or record.get("authorizing") is not False:
        raise CandidateReviewError("only nonauthorizing v2 discovery records may be imported")
    experiments = record.get("experiments")
    if not isinstance(experiments, Mapping):
        raise CandidateReviewError("discovery must retain its experiment registry")
    evidence = evidence or {}
    dossiers = []
    used_seals: set[str] = set()
    for source_id, row in sorted((record.get("promoted") or {}).items()):
        if not isinstance(row, Mapping):
            raise CandidateReviewError("promoted candidate must be an object")
        experiment = experiments.get(row.get("experiment_name"))
        if not isinstance(experiment, Mapping):
            raise CandidateReviewError("promoted candidate experiment is absent")
        request = candidate_request(row, experiment)
        checks = review_execution_evidence(request, evidence.get(request["request_id"]), used_seals=used_seals)
        dossiers.append({"source_candidate_id": source_id, "historical_tier": row.get("tier"),
                         "request": request, "main_gate_readiness": checks,
                         "authorizing": False, "requires_main_qualification": True})
    unknown = set(evidence) - {d["request"]["request_id"] for d in dossiers}
    if unknown:
        raise CandidateReviewError("execution evidence contains unknown candidate requests")
    adjusted = benjamini_yekutieli({d["request"]["request_id"]: d["main_gate_readiness"]["heldout_p_value"]
                                  for d in dossiers})
    for dossier in dossiers:
        correction = adjusted[dossier["request"]["request_id"]]
        dossier["main_gate_readiness"]["diagnostic_batch_q_value"] = correction["p_adjusted"]
        dossier["main_gate_readiness"]["diagnostic_batch_fdr"] = correction
    result = {"schema": SCHEMA, "authorizing": False, "requires_main_qualification": True,
              "candidate_count": len(dossiers), "qualified_count": 0, "candidates": dossiers,
              "batch_scope": "diagnostic_only_does_not_spend_main_online_fdr"}
    result["content_digest"] = _hash(result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", required=True)
    parser.add_argument("--executions", help="JSON object keyed by exact request_id; genuine trade observations only")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    record = json.loads(Path(args.discovery).read_text())
    evidence = json.loads(Path(args.executions).read_text()) if args.executions else None
    result = review_discovery(record, evidence)
    Path(args.out).write_text(json.dumps(result, indent=1, sort_keys=True, allow_nan=False) + "\n")
    print(f"reviewed {result['candidate_count']} exact candidates; qualified 0; main proof required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
