"""Readable narrative of what autonomous research actually did.

The factory and edge ledgers already record everything: each hypothesis, every
variant's full gate with its named checks, the diagnosis behind each mutation,
why a family was retired and after how many variants, and the provider/prompt
hashes behind an LLM proposal.  None of it was readable — ``factory status``
returned hypothesis rows and three counts, and the rest sat inside JSON blobs
no command opened.

This module is the reader.  It is strictly derived: it opens the ledgers
read-only, never writes, and computes nothing it cannot show the evidence for.
Where a number came from a gate, the gate's own hash is reported beside it, so
a claim in this report can always be traced back to the immutable row it came
from.
"""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from .edge_ledger_store import DEFAULT_DB_PATH, VEHICLES

REPORT_SCHEMA = "factory-report.v1"

# Origins where the model actually authored the hypothesis.  Everything else
# is deterministic, even when a provider was asked first and refused.
_LLM_ORIGINS = frozenset(("llm_discovery", "llm_replacement"))

# Ordered so the narrative reads as a lifecycle rather than an alphabet.
_CHECK_LABELS = {
    "fit_structurally_adequate": "fit sample big enough",
    "heldout_structurally_adequate": "held-out sample big enough",
    "separated": "fit and held-out do not overlap",
    "actual_control_available": "a real matched baseline existed",
    "fit_delta_positive": "beat the baseline in fit",
    "heldout_delta_positive": "beat the baseline held-out",
    "heldout_delta_lcb_positive": "held-out edge survives its lower bound",
    "heldout_p_significant": "held-out result is significant",
    "falsification": "beat a placebo/sign-flipped null",
    "heldout_net_pnl_positive": "made money held-out",
    "heldout_expectancy_positive": "positive expectancy per trade",
    "null_control_available": "a randomized-entry null existed",
    "null_control_delta_positive": "beat randomized entry timing",
    "walk_forward_majority_positive": "positive in most walk-forward folds",
    "qualification_net_positive": "made money in the sealed final window",
    "qualification_delta_positive": "beat the baseline in the sealed window",
    "family_fdr_significant": "survives multiple-testing within its family",
    "global_fdr_significant": "survives multiple-testing across the cycle",
}


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _loads(value: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, (bool, str, bytes)) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _failed_checks(gate: Mapping[str, Any]) -> list[str]:
    """Named reasons a variant did not pass, in lifecycle order."""
    envelope = gate.get("verified_gate")
    checks = (envelope.get("checks") if isinstance(envelope, Mapping) else None)
    if not isinstance(checks, Mapping):
        checks = gate.get("checks_without_family")
    if not isinstance(checks, Mapping):
        return []
    return [_CHECK_LABELS.get(name, name)
            for name in _CHECK_LABELS if checks.get(name) is False]


def _variant_row(record: Mapping[str, Any]) -> dict:
    """One variant's performance, without dragging its trade rows along."""
    gate = record.get("gate") if isinstance(record.get("gate"), Mapping) else {}
    account = (record.get("account")
               if isinstance(record.get("account"), Mapping) else {})
    diagnosis = (record.get("diagnostic")
                 if isinstance(record.get("diagnostic"), Mapping) else {})
    statistics = gate.get("global_multiple_tests")
    q_value = (_number(statistics.get("p_adjusted"))
               if isinstance(statistics, Mapping) else None)
    spec = record.get("rule_spec") if isinstance(record.get("rule_spec"), Mapping) else {}
    return {
        "variant_id": record.get("variant_id"),
        "family": spec.get("family"),
        "rule_schema": spec.get("schema"),
        "lane": record.get("mode"),
        "evaluated": {"start": record.get("evaluation_start"),
                      "end": record.get("evaluation_end")},
        "trades": account.get("trades"),
        "net_pnl": _number(account.get("realized_pnl")),
        "max_drawdown": _number(account.get("max_drawdown")),
        "fit_trades": gate.get("fit_trades"),
        "heldout_trades": gate.get("heldout_trades"),
        "heldout_net_pnl": _number(gate.get("heldout_net_pnl")),
        "heldout_expectancy": _number(gate.get("heldout_expectancy")),
        "heldout_delta": _number((gate.get("test") or {}).get("mean_delta")
                                 if isinstance(gate.get("test"), Mapping) else None),
        "heldout_delta_lcb": _number(gate.get("heldout_delta_lcb")),
        "p_raw": _number(gate.get("p_raw")),
        "q_value": q_value,
        "confidence": _number(gate.get("confidence")),
        "passes": bool(gate.get("passes")),
        "underpowered": not (gate.get("sample_adequate") and
                             gate.get("heldout_sample_adequate")),
        "failed_checks": _failed_checks(gate),
        "primary_failure": diagnosis.get("primary_failure"),
        "win_rate": _number(diagnosis.get("win_rate")),
        "profit_factor": _number(diagnosis.get("profit_factor")),
        "gate_hash": gate.get("gate_hash"),
        # Why this variant was tried, and who decided to try it.  Fixed before
        # the gate beside it was computed, so the pair reads as a prediction
        # and its result rather than as a summary written afterwards.
        "reason": record.get("reason"),
        "proposed_by": record.get("proposed_by"),
    }


def _origin(events: Sequence[Mapping[str, Any]],
            parent_events: Sequence[Mapping[str, Any]],
            hypothesis_id: str, generation: int) -> dict:
    """Where this hypothesis came from, and who proposed it.

    A genesis slot has no ancestor, so its seeding decision is recorded on the
    hypothesis itself; every other origin is recorded on the ancestor that was
    replaced. Own events are checked first so genesis is never mistaken for a
    template when a model actually proposed it.
    """
    for event in reversed(events):
        payload = event.get("payload") or {}
        if not isinstance(payload, Mapping) or payload.get("seeded") is not True:
            continue
        origin: dict[str, Any] = {"kind": str(payload.get("source") or "template"),
                                  "reason": event.get("reason")}
        evidence = payload.get("llm_evidence")
        if isinstance(evidence, Mapping):
            origin["llm"] = {
                "provider": evidence.get("provider"),
                "model": evidence.get("model"),
                "attempts": evidence.get("attempts"),
                "request_hash": evidence.get("request_hash"),
                "response_hash": evidence.get("raw_response_hash"),
                "prompt_hash": evidence.get("system_prompt_hash"),
            }
        if payload.get("llm_error"):
            origin["llm_error"] = payload["llm_error"]
        return origin
    for event in reversed(parent_events):
        payload = event.get("payload") or {}
        if not isinstance(payload, Mapping):
            continue
        successor = (payload.get("replacement_hypothesis_id") or
                     payload.get("successor_hypothesis_id"))
        if successor != hypothesis_id:
            continue
        evidence = payload.get("llm_evidence")
        source = payload.get("source")
        if payload.get("rotation") is True:
            kind = "rotation"
        elif payload.get("reseed") is True:
            kind = "reseed_after_proof"
        elif isinstance(evidence, Mapping) and evidence.get("kind") != "discovery":
            kind = "llm_replacement"
        else:
            kind = "replacement"
        origin: dict[str, Any] = {"kind": kind, "reason": event.get("reason")}
        if source:
            origin["source"] = source
        if isinstance(evidence, Mapping):
            origin["llm"] = {
                "provider": evidence.get("provider"),
                "model": evidence.get("model"),
                "attempts": evidence.get("attempts"),
                "request_hash": evidence.get("request_hash"),
                "response_hash": evidence.get("raw_response_hash"),
                "prompt_hash": evidence.get("system_prompt_hash"),
            }
        if payload.get("llm_error"):
            origin["llm_error"] = payload["llm_error"]
        return origin
    return {"kind": "template" if generation == 0 else "seed"}


def _outcome(events: Sequence[Mapping[str, Any]], status: str) -> dict:
    """What finally happened to this hypothesis, and on what evidence."""
    for event in reversed(events):
        payload = event.get("payload") or {}
        if not isinstance(payload, Mapping):
            payload = {}
        if event.get("status") == "retired":
            diagnosis = payload.get("diagnostic") or {}
            return {
                "kind": "rotated" if payload.get("rotation") else "retired",
                "reason": event.get("reason"),
                "variants_tested": payload.get("tested_variants"),
                "variants_intended": payload.get("expected_variants"),
                "primary_failure": (diagnosis.get("primary_failure")
                                    if isinstance(diagnosis, Mapping) else None),
                "replacement_hypothesis_id": payload.get("replacement_hypothesis_id"),
                "replacement_variant_id": payload.get("replacement_variant_id"),
                # Retirement is only legal against adequately powered failed
                # gates; their hashes are the proof it was.
                "failed_gate_hashes": payload.get("verified_gate_hashes") or [],
            }
        if event.get("status") == "validated" and payload.get("passing"):
            return {"kind": "proved", "reason": event.get("reason"),
                    "proved_variants": payload.get("passing"),
                    "successor_hypothesis_id": payload.get("successor_hypothesis_id"),
                    "successor_source": payload.get("source")}
        if event.get("status") in {"pending_generation_limit",
                                   "pending_llm_replacement"}:
            return {"kind": event["status"], "reason": event.get("reason"),
                    "failure": payload.get("failure"),
                    "llm_error": payload.get("error"),
                    "max_generations": payload.get("max_generations"),
                    "rotations_used": payload.get("rotations_used")}
    return {"kind": status or "active"}


def build_report(db_path: str | Path = DEFAULT_DB_PATH, *,
                 vehicle: str | None = None, slot: int | None = None) -> dict:
    """Assemble the full discovery narrative from the two ledgers."""
    path = Path(db_path)
    if vehicle is not None and vehicle not in VEHICLES:
        raise ValueError("vehicle must be equity or option")
    if not path.is_file():
        return {"schema": REPORT_SCHEMA, "available": False,
                "reason": "ledger not created", "db_path": str(path),
                "vehicles": []}
    with closing(_connect(path)) as db:
        tables = {str(row[0]) for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "factory_hypotheses" not in tables:
            return {"schema": REPORT_SCHEMA, "available": False,
                    "reason": "no factory lineage recorded", "db_path": str(path),
                    "vehicles": []}
        hypotheses = [dict(row) for row in db.execute(
            """SELECT h.*, (SELECT status FROM factory_events e
                            WHERE e.hypothesis_id=h.hypothesis_id
                            ORDER BY e.created_at DESC, e.event_id DESC LIMIT 1)
               AS status FROM factory_hypotheses h
               ORDER BY h.vehicle, h.slot, h.generation""")]
        events: dict[str, list[dict]] = {}
        for row in db.execute(
                "SELECT * FROM factory_events ORDER BY created_at, event_id"):
            item = dict(row)
            item["payload"] = _loads(item.pop("payload_json")) or {}
            events.setdefault(str(item["hypothesis_id"]), []).append(item)
        accounts: dict[str, list[dict]] = {}
        for row in db.execute(
                """SELECT hypothesis_id, result_json, created_at
                   FROM factory_accounts ORDER BY created_at, account_id"""):
            record = _loads(row["result_json"])
            if isinstance(record, Mapping):
                accounts.setdefault(str(row["hypothesis_id"]), []).append(
                    _variant_row(record))
        deployed: dict[tuple[str, str], str] = {}
        if {"candidates", "candidate_state"}.issubset(tables):
            for row in db.execute(
                    """SELECT c.variant_id, c.vehicle, s.status
                       FROM candidates c JOIN candidate_state s
                         ON s.candidate_id=c.candidate_id"""):
                deployed[(str(row["variant_id"]), str(row["vehicle"]))] = str(
                    row["status"])
        cycles = int(db.execute(
            "SELECT COUNT(*) FROM factory_cycles").fetchone()[0]) \
            if "factory_cycles" in tables else 0
        # The graded reason history: what was tried, why, and what the gates
        # then said.  Read here so the narrative can show the learning loop
        # rather than only the current state.
        lessons: dict[str, list[dict]] = {}
        if {"factory_lessons", "factory_lesson_outcomes"}.issubset(tables):
            parent_columns = {str(item["name"]) for item in
                              db.execute("PRAGMA table_info(factory_lessons)")}
            parent = ("l.parent_lesson_id" if "parent_lesson_id" in parent_columns
                      else "NULL AS parent_lesson_id")
            reasons: dict[str, str] = {}
            for row in db.execute(
                    f"""SELECT l.lesson_id, {parent}, l.vehicle, l.family,
                              l.kind, l.source, l.reason,
                              l.changed_json, l.variant_id, l.created_at,
                              o.passed, o.underpowered, o.heldout_delta,
                              o.q_value, o.failed_checks_json, o.outcome_id
                       FROM factory_lessons l
                       LEFT JOIN factory_lesson_outcomes o
                         ON o.lesson_id=l.lesson_id
                       ORDER BY l.created_at DESC, l.lesson_id DESC"""):
                graded = row["outcome_id"] is not None
                reasons[str(row["lesson_id"])] = str(row["reason"])
                lessons.setdefault(str(row["vehicle"]), []).append({
                    "lesson_id": row["lesson_id"],
                    # The lesson this proposal reasoned from.  An unbroken
                    # chain is the difference between a search that learns and
                    # one that restarts every cycle.
                    "parent_lesson_id": row["parent_lesson_id"],
                    "family": row["family"], "kind": row["kind"],
                    "proposed_by": row["source"], "reason": row["reason"],
                    "variant_id": row["variant_id"],
                    "changed": _loads(row["changed_json"]) or {},
                    "graded": graded,
                    "verdict": (None if not graded else
                                "passed" if row["passed"] else
                                "underpowered" if row["underpowered"] else
                                "failed"),
                    "heldout_delta": _number(row["heldout_delta"]),
                    "q_value": _number(row["q_value"]),
                    "failed_checks": [
                        _CHECK_LABELS.get(name, name) for name in
                        (_loads(row["failed_checks_json"]) or [])],
                })
            # Resolved after the sweep so a parent recorded in the same cycle
            # is available regardless of row order.
            for rows in lessons.values():
                for item in rows:
                    item["built_on"] = reasons.get(str(item["parent_lesson_id"]))

    report_vehicles = []
    for name in VEHICLES:
        if vehicle is not None and name != vehicle:
            continue
        local = [item for item in hypotheses if item["vehicle"] == name
                 and (slot is None or int(item["slot"]) == int(slot))]
        if not local:
            continue
        slots: dict[int, list[dict]] = {}
        for item in local:
            hypothesis_id = str(item["hypothesis_id"])
            own = events.get(hypothesis_id, [])
            parent = events.get(str(item["parent_hypothesis_id"] or ""), [])
            variants = accounts.get(hypothesis_id, [])
            for row in variants:
                row["ledger_status"] = deployed.get(
                    (str(row["variant_id"]), name))
            slots.setdefault(int(item["slot"]), []).append({
                "hypothesis_id": hypothesis_id,
                "generation": int(item["generation"]),
                "family": item["family"],
                "status": item["status"],
                "rule_schema": (_loads(item["spec_json"]) or {}).get("schema"),
                "rule_spec": _loads(item["spec_json"]),
                # For an LLM-discovered hypothesis this is the model's own
                # one-sentence rationale; otherwise it is generated from the
                # spec. Either way it is text, never an instruction.
                "thesis": item["thesis"],
                "falsification": item["falsification"],
                "parent_hypothesis_id": item["parent_hypothesis_id"],
                "not_before": item["not_before"],
                "origin": _origin(own, parent, hypothesis_id,
                                  int(item["generation"])),
                "variants": variants,
                "variants_tested": len(variants),
                "outcome": _outcome(own, str(item["status"] or "")),
            })
        active = sum(1 for item in local if item["status"] in {
            "queued", "testing", "backtest_passed", "pending_generation_limit",
            "pending_llm_replacement"})
        proved = [row for rows in slots.values() for item in rows
                  for row in item["variants"]
                  if row["ledger_status"] in {"validated", "champion"}]
        local_lessons = [item for item in lessons.get(name, [])
                         if slot is None or any(
                             item["variant_id"] == row["variant_id"]
                             for rows in slots.values() for entry in rows
                             for row in entry["variants"])]
        graded = [item for item in local_lessons if item["graded"]]
        report_vehicles.append({
            "vehicle": name,
            "summary": {
                "slots": len(slots),
                "active_slots": active,
                "hypotheses": len(local),
                "variants_tested": sum(len(item["variants"])
                                       for rows in slots.values()
                                       for item in rows),
                "families_explored": sorted({str(item["family"])
                                             for item in local}),
                "proved_variants": [row["variant_id"] for row in proved],
                "retired_hypotheses": sum(
                    1 for rows in slots.values() for item in rows
                    if item["outcome"]["kind"] in {"retired", "rotated"}),
                # Authored by the model, not merely asked of it.  A rejected
                # proposal still records provider evidence, so counting the
                # presence of that evidence reported a deterministic template
                # as an LLM-seeded hypothesis.
                "llm_seeded_hypotheses": sum(
                    1 for rows in slots.values() for item in rows
                    if item["origin"]["kind"] in _LLM_ORIGINS),
                "llm_proposals_rejected": sum(
                    1 for rows in slots.values() for item in rows
                    if item["origin"].get("llm_error")),
                "reasons_recorded": len(local_lessons),
                "reasons_graded": len(graded),
                "llm_reasons_that_passed": sum(
                    1 for item in graded
                    if item["proposed_by"] == "llm" and item["verdict"] == "passed"),
                "llm_tuned_variants": sum(
                    1 for item in local_lessons
                    if item["proposed_by"] == "llm" and item["kind"] == "tuning"),
                # Proposals that named the earlier result they reasoned from,
                # rather than starting from nothing.
                "reasons_built_on_a_prior_lesson": sum(
                    1 for item in local_lessons if item.get("parent_lesson_id")),
            },
            "lessons": local_lessons,
            "slots": [{"slot": key, "generations": slots[key]}
                      for key in sorted(slots)],
        })
    return {"schema": REPORT_SCHEMA, "available": True, "db_path": str(path),
            "cycles": cycles, "vehicles": report_vehicles}


# How many graded reasons the rendered narrative shows before it becomes a log
# rather than a report.  ``build_report`` returns all of them either way.
_LESSON_ROWS = 25


def _changed_phrase(changed: Mapping[str, Any]) -> str:
    """Render a recorded parameter delta the way the ledger stored it."""
    parts = []
    for key, value in sorted(changed.items()):
        if isinstance(value, Mapping) and {"from", "to"} <= set(value):
            parts.append(f"{key} {value['from']}→{value['to']}")
        else:
            parts.append(f"{key}={value}")
    return "; ".join(parts)


def _fmt(value: Any, digits: int = 4) -> str:
    number = _number(value)
    if number is None:
        return "—" if value is None else str(value)
    return f"{number:,.{digits}f}".rstrip("0").rstrip(".") or "0"


def render_text(report: Mapping[str, Any]) -> str:
    """Render the report as a plain-text narrative."""
    if not report.get("available"):
        return (f"No research lineage yet ({report.get('reason')}).\n"
                f"Ledger: {report.get('db_path')}\n")
    out: list[str] = []
    add = out.append
    add(f"Autonomous research report  ({report['db_path']})")
    add(f"cycles run: {report.get('cycles', 0)}")
    for vehicle in report["vehicles"]:
        summary = vehicle["summary"]
        add("")
        add("=" * 78)
        add(f"VEHICLE: {vehicle['vehicle']}")
        add("=" * 78)
        add(f"  slots {summary['slots']} ({summary['active_slots']} still searching)"
            f" | hypotheses {summary['hypotheses']}"
            f" | variants tested {summary['variants_tested']}")
        add(f"  families explored: {', '.join(summary['families_explored'])}")
        add(f"  hypotheses proposed by the LLM: {summary['llm_seeded_hypotheses']}"
            f" ({summary['llm_proposals_rejected']} proposal(s) refused)")
        add(f"  hypotheses retired/rotated: {summary['retired_hypotheses']}")
        add(f"  variants the model tuned: {summary['llm_tuned_variants']}"
            f" | reasons recorded {summary['reasons_recorded']}"
            f" ({summary['reasons_graded']} graded,"
            f" {summary['reasons_built_on_a_prior_lesson']} built on an"
            f" earlier lesson)")
        proved = summary["proved_variants"]
        add(f"  PROVED EDGES: {', '.join(proved) if proved else 'none yet'}")
        if vehicle.get("lessons"):
            add("")
            add("  WHAT IT TRIED, WHY, AND WHAT HAPPENED")
            for item in vehicle["lessons"][:_LESSON_ROWS]:
                verdict = item["verdict"] or "not yet graded"
                add(f"    [{verdict}] {item['kind']} by {item['proposed_by']}"
                    f" ({item['family']})")
                add(f"      reason: {item['reason']}")
                if item.get("built_on"):
                    add(f"      built on: {item['built_on']}")
                if item["changed"]:
                    add(f"      changed: {_changed_phrase(item['changed'])}")
                if item["verdict"] is not None:
                    add(f"      held-out delta {_fmt(item['heldout_delta'])}"
                        f"  q {_fmt(item['q_value'])}")
                if item["failed_checks"]:
                    add("      failed: " + "; ".join(item["failed_checks"][:4]))
        for entry in vehicle["slots"]:
            add("")
            add(f"  {'-' * 74}")
            add(f"  SLOT {entry['slot']}")
            for item in entry["generations"]:
                add("")
                origin = item["origin"]
                label = origin["kind"].replace("_", " ")
                add(f"    gen {item['generation']}  {item['family']}"
                    f"  [{item['status']}]  via {label}")
                add(f"      id      {item['hypothesis_id']}")
                add(f"      grammar {item['rule_schema']}")
                add(f"      thesis  {item['thesis']}")
                add(f"      refuted if: {item['falsification']}")
                llm = origin.get("llm")
                if llm:
                    add(f"      proposed by {llm.get('provider')}/{llm.get('model')}"
                        f" in {llm.get('attempts')} attempt(s)")
                    add(f"        prompt {str(llm.get('prompt_hash'))[:16]}…"
                        f"  request {str(llm.get('request_hash'))[:16]}…"
                        f"  response {str(llm.get('response_hash'))[:16]}…")
                if origin.get("llm_error"):
                    add(f"      LLM proposal rejected: {origin['llm_error']}")
                if item["variants"]:
                    add(f"      variants tested: {item['variants_tested']}")
                for variant in item["variants"]:
                    verdict = ("PASS" if variant["passes"] else
                               "underpowered" if variant["underpowered"] else "fail")
                    add(f"        - {variant['variant_id']}  [{verdict}]"
                        f"  lane={variant['lane']}")
                    add(f"            trades {variant['trades']}"
                        f" (fit {variant['fit_trades']} / held-out {variant['heldout_trades']})"
                        f"  net {_fmt(variant['net_pnl'], 2)}"
                        f"  maxDD {_fmt(variant['max_drawdown'], 2)}")
                    add(f"            held-out delta {_fmt(variant['heldout_delta'])}"
                        f"  lcb {_fmt(variant['heldout_delta_lcb'])}"
                        f"  q {_fmt(variant['q_value'])}"
                        f"  win {_fmt(variant['win_rate'])}")
                    if variant["reason"]:
                        add(f"            tried because ({variant['proposed_by']}):"
                            f" {variant['reason']}")
                    if variant["ledger_status"]:
                        add(f"            ledger status: {variant['ledger_status']}")
                    if variant["primary_failure"] and variant["primary_failure"] != "none":
                        add(f"            diagnosis: {variant['primary_failure']}")
                    if variant["failed_checks"]:
                        add("            failed: " +
                            "; ".join(variant["failed_checks"]))
                outcome = item["outcome"]
                add(f"      outcome: {outcome['kind'].replace('_', ' ')}"
                    f" — {outcome.get('reason') or ''}".rstrip(" —"))
                if outcome.get("variants_tested") is not None:
                    add(f"        after {outcome['variants_tested']} of"
                        f" {outcome.get('variants_intended')} intended variants"
                        f" failed adequately powered gates")
                if outcome.get("primary_failure"):
                    add(f"        dominant failure mode: {outcome['primary_failure']}")
                if outcome.get("replacement_hypothesis_id"):
                    add(f"        replaced by {outcome['replacement_hypothesis_id']}")
                if outcome.get("proved_variants"):
                    add(f"        proved: {', '.join(outcome['proved_variants'])}")
                if outcome.get("successor_hypothesis_id"):
                    add(f"        slot reseeded with {outcome['successor_hypothesis_id']}"
                        f" ({outcome.get('successor_source')})")
                if outcome.get("llm_error"):
                    add(f"        waiting on a valid LLM proposal: {outcome['llm_error']}")
    add("")
    return "\n".join(out) + "\n"


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render the report as Markdown for sharing or archiving."""
    if not report.get("available"):
        return f"# Research report\n\nNo lineage yet ({report.get('reason')}).\n"
    out = [f"# Autonomous research report", "",
           f"- Ledger: `{report['db_path']}`", f"- Cycles run: {report.get('cycles', 0)}"]
    for vehicle in report["vehicles"]:
        summary = vehicle["summary"]
        out += ["", f"## {vehicle['vehicle']}", "",
                f"- Slots: {summary['slots']} ({summary['active_slots']} still searching)",
                f"- Hypotheses: {summary['hypotheses']}, variants tested: {summary['variants_tested']}",
                f"- Families explored: {', '.join(summary['families_explored'])}",
                f"- Proposed by the LLM: {summary['llm_seeded_hypotheses']}"
                f" ({summary['llm_proposals_rejected']} refused)",
                f"- Retired or rotated: {summary['retired_hypotheses']}",
                f"- Variants the model tuned: {summary['llm_tuned_variants']}",
                f"- Reasons recorded: {summary['reasons_recorded']}"
                f" ({summary['reasons_graded']} graded)",
                f"- Proved edges: {', '.join(summary['proved_variants']) or 'none yet'}"]
        if vehicle.get("lessons"):
            out += ["", "#### What it tried, why, and what happened", "",
                    "| verdict | kind | by | reason | built on | changed |"
                    " held-out Δ |",
                    "| --- | --- | --- | --- | --- | --- | --- |"]
            for item in vehicle["lessons"][:_LESSON_ROWS]:
                out.append(
                    f"| {item['verdict'] or 'ungraded'} | {item['kind']} |"
                    f" {item['proposed_by']} | {item['reason']} |"
                    f" {item.get('built_on') or '—'} |"
                    f" {_changed_phrase(item['changed'])} |"
                    f" {_fmt(item['heldout_delta'])} |")
        for entry in vehicle["slots"]:
            out += ["", f"### Slot {entry['slot']}"]
            for item in entry["generations"]:
                out += ["", f"#### gen {item['generation']} — {item['family']} "
                            f"(`{item['status']}`, via {item['origin']['kind']})", "",
                        f"> {item['thesis']}", ""]
                if item["variants"]:
                    out += ["| variant | lane | trades | held-out Δ | lcb | q | verdict |",
                            "| --- | --- | --- | --- | --- | --- | --- |"]
                    for variant in item["variants"]:
                        verdict = ("**pass**" if variant["passes"] else
                                   "underpowered" if variant["underpowered"] else "fail")
                        out.append(
                            f"| `{variant['variant_id']}` | {variant['lane']} |"
                            f" {variant['trades']} | {_fmt(variant['heldout_delta'])} |"
                            f" {_fmt(variant['heldout_delta_lcb'])} |"
                            f" {_fmt(variant['q_value'])} | {verdict} |")
                outcome = item["outcome"]
                out += ["", f"**Outcome:** {outcome['kind'].replace('_', ' ')}"
                            f" — {outcome.get('reason') or ''}"]
                if outcome.get("variants_tested") is not None:
                    out.append(f" (after {outcome['variants_tested']} variants)")
    return "\n".join(out) + "\n"


DEFAULT_REPORT_ROOT = Path("research/results/factory")


def write_report(db_path: str | Path = DEFAULT_DB_PATH, *,
                 vehicle: str | None = None,
                 output_root: str | Path = DEFAULT_REPORT_ROOT) -> Path | None:
    """Archive the narrative as Markdown, and return where it landed.

    The report was previously reachable only by running a command by hand.  On
    the documented headless topology nobody ever does, so the discovery
    narrative — the one artifact that explains what research has been doing —
    was written and never read.  Writing it under ``research/results`` puts it
    exactly where the read-only dashboard already lists Markdown reports.

    The path is stable per vehicle so a cycle overwrites its own predecessor
    rather than accumulating one file per night; the ledger, not this file, is
    the durable record.
    """
    report = build_report(db_path, vehicle=vehicle)
    if not report.get("available"):
        return None
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{vehicle or 'all'}.md"
    # Write-then-rename so the dashboard never serves a half-written report.
    staging = target.with_suffix(".md.tmp")
    staging.write_text(render_markdown(report), encoding="utf-8")
    staging.replace(target)
    return target


__all__ = ["DEFAULT_REPORT_ROOT", "REPORT_SCHEMA", "build_report",
           "render_markdown", "render_text", "write_report"]
