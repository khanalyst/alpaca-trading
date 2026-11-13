"""Preregister equal-budget session-signal comparisons, without making provider calls.

Every candidate slot, including invalid, duplicate and failed proposals, consumes
the same budget. Select one rule per lane using development data, freeze every
selection, then attach paired returns on the exact preregistered session grid.
These reports are diagnostic only and cannot produce an edge proof or an order.

CLI inputs are JSON files. ``plan`` writes a ledger; ``attempt``, ``freeze`` and
``outcomes`` append bounded evidence to that ledger. ``report`` never mutates it.
Use ``python -m research.session_model_experiment --help`` for the interface.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from research.llm_strategy import content_hash
from studies.session_edges import grammar, propose, stats

PLAN_SCHEMA = "session-model-experiment-plan.v1"
LEDGER_SCHEMA = "session-model-experiment.v1"
REPORT_SCHEMA = "session-model-experiment-report.v1"
STATUSES = ("proposed", "duplicate", "invalid", "provider_failed")


def _digest(value: str, name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None:
        raise ValueError(f"{name} must be a full hexadecimal content or commit hash")
    return value


def _date(value: str) -> str:
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("session dates must use YYYY-MM-DD")
    return value


def create_plan(*, lanes: Sequence[Mapping[str, Any]], candidate_budget: int,
                design_end: str, oos_sessions: Sequence[str], data_hash: str,
                code_hash: str, experiment_id: str | None = None) -> dict[str, Any]:
    """Declare equal candidate slots and the shared sealed evaluation scope.

    ``design_end`` is exclusive. ``data_hash`` must identify the shared data
    snapshot, including the sealed OOS partition; hashes are supplied by the
    caller, not inferred from or asserted to describe the trading corpus.
    """
    if isinstance(candidate_budget, bool) or not isinstance(candidate_budget, int) or not 1 <= candidate_budget <= 10_000:
        raise ValueError("candidate_budget must be an integer between 1 and 10000")
    design_end = _date(design_end)
    sessions = [_date(value) for value in oos_sessions]
    if not sessions or sessions != sorted(set(sessions)) or sessions[0] < design_end:
        raise ValueError("OOS sessions must be unique, ordered and disjoint from design")
    normalized_lanes = []
    for lane in lanes:
        if set(lane) - {"name", "kind", "provider", "model"}:
            raise ValueError("unknown lane fields")
        name, kind = lane.get("name"), lane.get("kind")
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name) is None:
            raise ValueError("lane names must be short stable identifiers")
        if kind not in {"deterministic", "model"}:
            raise ValueError("lane kind must be deterministic or model")
        normalized = {"name": name, "kind": kind, "candidate_budget": candidate_budget}
        if kind == "model":
            for key in ("provider", "model"):
                if not isinstance(lane.get(key), str) or not lane[key].strip():
                    raise ValueError(f"a model lane requires {key}")
                normalized[key] = lane[key]
        elif set(lane) & {"provider", "model"}:
            raise ValueError("deterministic lanes cannot claim a provider or model")
        normalized_lanes.append(normalized)
    if len({row["name"] for row in normalized_lanes}) != len(normalized_lanes):
        raise ValueError("lane names must be unique")
    if sum(row["kind"] == "deterministic" for row in normalized_lanes) != 1 or not any(
            row["kind"] == "model" for row in normalized_lanes):
        raise ValueError("declare exactly one deterministic lane and at least one model lane")
    plan = {"schema": PLAN_SCHEMA, "authorizing": False, "lanes": normalized_lanes,
            "candidate_budget": candidate_budget, "design_end": design_end, "oos_sessions": sessions,
            "data_hash": _digest(data_hash, "data_hash"), "code_hash": _digest(code_hash, "code_hash"),
            "grammar_hash": content_hash(grammar.describe()),
            "budget_unit": "candidate slot, including rejection, duplicate, or provider failure",
            "selection_rule": "one candidate per lane selected on design data before all OOS outcomes",
            "comparison_rule": "paired net returns in bps on every declared session, including flat sessions"}
    plan["experiment_id"] = experiment_id or content_hash(plan)[:24]
    plan["plan_hash"] = content_hash(plan)
    return plan


def _verify_plan(plan: Mapping[str, Any]) -> None:
    body = dict(plan)
    digest = body.pop("plan_hash", None)
    if body.get("schema") != PLAN_SCHEMA or body.get("authorizing") is not False or digest != content_hash(body):
        raise ValueError("experiment plan integrity mismatch")
    if plan.get("grammar_hash") != content_hash(grammar.describe()):
        raise ValueError("experiment grammar changed; preregister a new plan")


def _seal(ledger: dict[str, Any]) -> dict[str, Any]:
    ledger.pop("ledger_hash", None)
    ledger["ledger_hash"] = content_hash(ledger)
    return ledger


def _verified(ledger: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(ledger))
    digest = result.pop("ledger_hash", None)
    if result.get("schema") != LEDGER_SCHEMA or result.get("authorizing") is not False or digest != content_hash(result):
        raise ValueError("experiment ledger integrity mismatch")
    _verify_plan(result["plan"])
    return result


def new_ledger(plan: Mapping[str, Any]) -> dict[str, Any]:
    _verify_plan(plan)
    return _seal({"schema": LEDGER_SCHEMA, "authorizing": False, "plan": copy.deepcopy(dict(plan)),
                  "attempts": [], "selection": None, "outcomes": {}})


def _lane(ledger: Mapping[str, Any], lane_name: str) -> dict[str, Any]:
    for lane in ledger["plan"]["lanes"]:
        if lane["name"] == lane_name:
            return lane
    raise ValueError(f"unknown lane {lane_name!r}")


def record_attempt(ledger: Mapping[str, Any], *, lane_name: str, attempt_id: str,
                   status: str, spec: Mapping[str, Any] | None = None,
                   envelope: Mapping[str, Any] | None = None, hypothesis_index: int = 0) -> dict[str, Any]:
    """Retain every budgeted slot; model candidates require their exact receipt.

    Invalid and failed candidates consume a slot, so a lane cannot retry until
    lucky without exhausting the same preregistered candidate budget. Duplicate
    claims remain attributable, but never count as independent discoveries.
    """
    result = _verified(ledger)
    lane = _lane(result, lane_name)
    if result["selection"] is not None:
        raise ValueError("all candidate lanes are frozen")
    if status not in STATUSES:
        raise ValueError(f"attempt status must be one of {STATUSES}")
    if not isinstance(attempt_id, str) or not attempt_id.strip() or len(attempt_id) > 128:
        raise ValueError("a bounded attempt_id is required")
    if any(row["attempt_id"] == attempt_id for row in result["attempts"]):
        raise ValueError("attempt_id already recorded")
    if sum(row["lane"] == lane_name for row in result["attempts"]) >= lane["candidate_budget"]:
        raise ValueError("lane candidate budget exhausted")
    attribution = None
    receipt_row_kind, receipt_row_index = None, None
    if lane["kind"] == "model":
        if envelope is None:
            raise ValueError("model attempts require a proposal envelope, including failures")
        rows = propose.proposal_rows(envelope)
        provenance = envelope["provenance"]
        if any(provenance[key] != lane[key] for key in ("provider", "model")):
            raise ValueError("proposal provider/model differs from the preregistered lane")
        if provenance["grammar_hash"] != result["plan"]["grammar_hash"]:
            raise ValueError("proposal grammar differs from the experiment")
        if status == "provider_failed":
            if envelope.get("status") != "provider_failed":
                raise ValueError("provider failure requires a failed-call receipt")
            receipt_row_kind, receipt_row_index = "failed", 0
        elif status == "invalid":
            if isinstance(hypothesis_index, bool) or not isinstance(hypothesis_index, int) or not 0 <= hypothesis_index < len(envelope["rejected"]):
                raise ValueError("invalid status must identify a retained rejected proposal")
            receipt_row_kind, receipt_row_index = "rejected", hypothesis_index
        elif status in {"proposed", "duplicate"}:
            if isinstance(hypothesis_index, bool) or not isinstance(hypothesis_index, int) or not 0 <= hypothesis_index < len(rows):
                raise ValueError("hypothesis_index does not identify a valid model proposal")
            chosen = rows[hypothesis_index]
            receipt_row_kind, receipt_row_index = "hypothesis", hypothesis_index
            attribution = chosen["proposal_attribution"]
            chosen_spec = {key: chosen[key] for key in ("family", "symbol", "params")}
            if spec is not None and grammar.spec_id(spec) != grammar.spec_id(chosen_spec):
                raise ValueError("candidate spec differs from the retained model response")
            spec = chosen_spec
        for previous in result["attempts"]:
            receipt = previous["proposal_envelope"]
            if previous["lane"] == lane_name and receipt is not None and receipt["attempt_id"] == envelope["attempt_id"]:
                if receipt["envelope_hash"] != envelope["envelope_hash"]:
                    raise ValueError("provider attempt_id already identifies a different receipt")
                if (previous["receipt_row_kind"], previous["receipt_row_index"]) == (receipt_row_kind, receipt_row_index):
                    raise ValueError("proposal receipt row already spent a candidate slot")
    elif envelope is not None:
        raise ValueError("deterministic attempts cannot claim a provider envelope")
    if status in {"invalid", "provider_failed"}:
        if spec is not None:
            raise ValueError("failed attempts cannot carry an admitted candidate")
        canonical, executable_id = None, None
    else:
        if spec is None:
            raise ValueError("an admitted attempt requires a candidate")
        canonical, executable_id = grammar.normalise(spec), grammar.spec_id(spec)
    duplicate = next((row["attempt_id"] for row in result["attempts"]
                      if executable_id is not None and row["spec_id"] == executable_id), None)
    if status == "duplicate" and duplicate is None:
        raise ValueError("duplicate status requires an already recorded executable candidate")
    if duplicate is not None:
        status = "duplicate"
    result["attempts"].append({"attempt_id": attempt_id, "lane": lane_name, "status": status,
                               "spec": canonical, "spec_id": executable_id, "duplicate_of": duplicate,
                               "proposal_attribution": attribution,
                               "receipt_row_kind": receipt_row_kind, "receipt_row_index": receipt_row_index,
                               "proposal_envelope": copy.deepcopy(dict(envelope)) if envelope is not None else None})
    return _seal(result)


def freeze_selection(ledger: Mapping[str, Any], selections: Mapping[str, str]) -> dict[str, Any]:
    """Freeze all development selections together, after every lane spends its budget."""
    result = _verified(ledger)
    if result["selection"] is not None:
        raise ValueError("selections already frozen")
    expected = {row["name"] for row in result["plan"]["lanes"]}
    if set(selections) != expected:
        raise ValueError("select exactly one attempt per lane")
    for lane in result["plan"]["lanes"]:
        attempts = [row for row in result["attempts"] if row["lane"] == lane["name"]]
        if len(attempts) != lane["candidate_budget"]:
            raise ValueError("all lanes must spend the same candidate budget before selection")
        # A 25-row response is 25 searched hypotheses, even if only one is
        # selected. Require rejected and unused rows too, rather than allowing
        # a model lane to hide a larger search behind its successful slots.
        receipts = {row["proposal_envelope"]["attempt_id"]: row["proposal_envelope"] for row in attempts
                    if row["proposal_envelope"] is not None}
        for receipt_id, receipt in receipts.items():
            expected_rows = ({("failed", 0)} if receipt["status"] == "provider_failed" else
                             {("hypothesis", index) for index in range(len(receipt["hypotheses"]))} |
                             {("rejected", index) for index in range(len(receipt["rejected"]))})
            recorded_rows = {(row["receipt_row_kind"], row["receipt_row_index"]) for row in attempts
                             if row["proposal_envelope"] is not None and row["proposal_envelope"]["attempt_id"] == receipt_id}
            if recorded_rows != expected_rows:
                raise ValueError("every hypothesis and rejection in each model response must spend a candidate slot")
        if not any(row["attempt_id"] == selections[lane["name"]] and row["spec_id"] is not None for row in attempts):
            raise ValueError("selection must identify an admitted candidate in its own lane")
    result["selection"] = {"attempts": dict(selections), "plan_hash": result["plan"]["plan_hash"],
                           "attempts_hash": content_hash(result["attempts"])}
    result["selection"]["selection_hash"] = content_hash(result["selection"])
    return _seal(result)


def record_outcomes(ledger: Mapping[str, Any], *, lane_name: str,
                    net_bps_by_session: Mapping[str, float], data_hash: str,
                    code_hash: str) -> dict[str, Any]:
    """Attach each frozen rule's complete OOS grid, including zero-return sessions."""
    result = _verified(ledger)
    _lane(result, lane_name)
    if result["selection"] is None:
        raise ValueError("freeze every lane before observing OOS results")
    if lane_name in result["outcomes"]:
        raise ValueError("OOS evidence is immutable; start a new experiment for new evidence")
    if data_hash != result["plan"]["data_hash"] or code_hash != result["plan"]["code_hash"]:
        raise ValueError("OOS source/code identity differs from the preregistered experiment")
    if set(net_bps_by_session) != set(result["plan"]["oos_sessions"]):
        raise ValueError("OOS returns must cover exactly every declared session, including flat sessions")
    returns = {}
    for session in result["plan"]["oos_sessions"]:
        value = net_bps_by_session[session]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("OOS returns must be finite net bps")
        returns[session] = float(value)
    selected_attempt = result["selection"]["attempts"][lane_name]
    selected_id = next(row["spec_id"] for row in result["attempts"] if row["attempt_id"] == selected_attempt)
    for previous_lane, previous_outcomes in result["outcomes"].items():
        previous_attempt = result["selection"]["attempts"][previous_lane]
        previous_id = next(row["spec_id"] for row in result["attempts"] if row["attempt_id"] == previous_attempt)
        if previous_id == selected_id and previous_outcomes["net_bps_by_session"] != returns:
            raise ValueError("the same executable rule on shared data/code must have identical OOS returns")
    result["outcomes"][lane_name] = {"net_bps_by_session": returns, "data_hash": data_hash,
                                       "code_hash": code_hash,
                                       "selected_attempt_id": result["selection"]["attempts"][lane_name],
                                       "selection_hash": result["selection"]["selection_hash"]}
    return _seal(result)


def report(ledger: Mapping[str, Any]) -> dict[str, Any]:
    """Compare paired OOS returns without issuing a proof, winner, or significance claim."""
    result = _verified(ledger)
    plan = result["plan"]
    complete = len(result["outcomes"]) == len(plan["lanes"])
    baseline = next(row["name"] for row in plan["lanes"] if row["kind"] == "deterministic")
    lane_reports = []
    for lane in plan["lanes"]:
        attempts = [row for row in result["attempts"] if row["lane"] == lane["name"]]
        receipts = {row["proposal_envelope"]["attempt_id"]: row["proposal_envelope"] for row in attempts
                    if row["proposal_envelope"] is not None}
        evidence = {"lane": lane["name"], "kind": lane["kind"], "budget": lane["candidate_budget"],
                    "attempt_ids": [row["attempt_id"] for row in attempts], "slots_spent": len(attempts),
                    "unique_executable_specs": len({row["spec_id"] for row in attempts if row["spec_id"]}),
                    "statuses": {status: sum(row["status"] == status for row in attempts) for status in STATUSES},
                    "provider_attempt_ids": sorted(receipts),
                    "observed_successful_provider_calls": sum(
                        receipt["provenance"]["provider_call_observed"] for receipt in receipts.values())}
        if lane["kind"] == "model":
            evidence.update({key: lane[key] for key in ("provider", "model")})
        if complete:
            current = result["outcomes"][lane["name"]]["net_bps_by_session"]
            control = result["outcomes"][baseline]["net_bps_by_session"]
            deltas = [current[session] - control[session] for session in plan["oos_sessions"]]
            estimate = stats.newey_west_t(deltas)
            evidence["paired_oos"] = {"sessions": len(deltas), "mean_delta_net_bps": math.fsum(deltas) / len(deltas),
                                      "hac_t_diagnostic": estimate, "baseline": baseline,
                                      "session_deltas_bps": dict(zip(plan["oos_sessions"], deltas))}
        lane_reports.append(evidence)
    return {"schema": REPORT_SCHEMA, "authorizing": False, "diagnostic_only": True,
            "experiment_id": plan["experiment_id"], "plan_hash": plan["plan_hash"],
            "complete_paired_oos": complete, "model_performance_claim": False,
            "limitations": ["No provider calls are made by this experiment recorder.",
                            "Supplied source hashes and returns require independent replay verification.",
                            "Paired estimates are diagnostic; no multiple-test correction or qualification is performed.",
                            "Candidate budgets are equal; API and compute costs are not assumed equal.",
                            "A local call receipt is not a provider attestation."],
            "lanes": lane_reports}


def _read(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path: str, payload: Mapping[str, Any]) -> None:
    destination = Path(path)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, indent=1, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(destination)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--lanes", required=True)
    plan.add_argument("--candidate-budget", type=int, required=True)
    plan.add_argument("--design-end", required=True)
    plan.add_argument("--oos-sessions", required=True)
    plan.add_argument("--data-hash", required=True)
    plan.add_argument("--code-hash", required=True)
    plan.add_argument("--out", required=True)
    for command in ("attempt", "freeze", "outcomes", "report"):
        child = sub.add_parser(command)
        child.add_argument("ledger")
        child.add_argument("--out")
        if command in {"attempt", "outcomes"}:
            child.add_argument("--lane", required=True)
        if command == "attempt":
            child.add_argument("--attempt-id", required=True)
            child.add_argument("--status", choices=STATUSES, required=True)
            child.add_argument("--spec")
            child.add_argument("--envelope")
            child.add_argument("--hypothesis-index", type=int, default=0)
        elif command == "freeze":
            child.add_argument("--selections", required=True)
        elif command == "outcomes":
            child.add_argument("--returns", required=True)
            child.add_argument("--data-hash", required=True)
            child.add_argument("--code-hash", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            output = new_ledger(create_plan(lanes=_read(args.lanes), candidate_budget=args.candidate_budget,
                                            design_end=args.design_end, oos_sessions=_read(args.oos_sessions),
                                            data_hash=args.data_hash, code_hash=args.code_hash))
        else:
            ledger = _read(args.ledger)
            if args.command == "attempt":
                output = record_attempt(ledger, lane_name=args.lane, attempt_id=args.attempt_id, status=args.status,
                                        spec=_read(args.spec) if args.spec else None,
                                        envelope=_read(args.envelope) if args.envelope else None,
                                        hypothesis_index=args.hypothesis_index)
            elif args.command == "freeze":
                output = freeze_selection(ledger, _read(args.selections))
            elif args.command == "outcomes":
                output = record_outcomes(ledger, lane_name=args.lane, net_bps_by_session=_read(args.returns),
                                         data_hash=args.data_hash, code_hash=args.code_hash)
            else:
                output = report(ledger)
        if args.out or args.command != "report":
            _write(args.out or args.ledger, output)
        else:
            print(json.dumps(output, sort_keys=True, indent=1, allow_nan=False))
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
