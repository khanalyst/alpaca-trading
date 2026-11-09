"""A readable, append-only record of every LLM research call and what became of it.

The factory's ledgers keep hashes of each provider exchange, which prove what
happened but do not show it.  This log shows it: for every ``discover``,
``tune`` and ``propose`` call, what the model was given (the vehicle, the root
rule, the refinement phase, the aggregate diagnosis and the graded lessons),
what it returned (the rule, the fields it changed against the root, its thesis
or reasons), and then what the factory did with each proposal (kept and tested,
or dropped and why).  Variant ids tie each row to the factory report's gate
results.

Nothing here can influence research: the log is written after each decision
and never read by the factory.  A write failure is logged and ignored.

The path is ``ALPACA_LLM_AUDIT_PATH``, by default
``<runtime root>/research/llm-audit.jsonl``; ``off`` disables it.

    python -m research.llm_audit --log runtime/research/llm-audit.jsonl --out llm-audit.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA = "llm-audit.v1"
MAX_FIELD_BYTES = 8_192
MAX_LESSONS = 24
log = logging.getLogger("research.llm_audit")


def audit_path() -> Path | None:
    value = os.getenv("ALPACA_LLM_AUDIT_PATH", "").strip()
    if value.lower() == "off":
        return None
    if value:
        return Path(value)
    root = os.getenv("ALPACA_AGENT_RUNTIME_ROOT") or "runtime"
    return Path(root) / "research" / "llm-audit.jsonl"


def _bounded(value: Any, limit: int = MAX_FIELD_BYTES) -> Any:
    """JSON-safe copy; anything larger than ``limit`` bytes becomes a marked summary."""
    try:
        text = json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = json.dumps(str(value))
    if len(text.encode()) <= limit:
        return json.loads(text)
    keys = sorted(value) if isinstance(value, Mapping) else None
    return {"_truncated": True, "bytes": len(text.encode()), "keys": keys,
            "head": text[: limit // 2]}


def spec_changes(root: Mapping[str, Any] | None,
                 proposed: Mapping[str, Any] | None) -> dict[str, list[Any]]:
    """Fields whose values differ between two rule specs, as ``{field: [root, proposed]}``."""
    if not isinstance(root, Mapping) or not isinstance(proposed, Mapping):
        return {}
    keys = sorted(set(root) | set(proposed))
    return {k: [root.get(k), proposed.get(k)] for k in keys if root.get(k) != proposed.get(k)}


def _write(record: Mapping[str, Any], path: Path | None = None) -> None:
    target = path or audit_path()
    if target is None:
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"schema": SCHEMA,
                           "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                           **record}, sort_keys=True, default=str)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception as exc:  # noqa: BLE001 - the audit must never stop research
        log.warning("llm audit write failed: %r", exc)


def record_call(kind: str, *, provider: str, model: str,
                arguments: Mapping[str, Any], result: Any,
                path: Path | None = None) -> None:
    """One provider call: its inputs, its validated outputs and any error."""
    root = (arguments.get("rule_spec") or arguments.get("prior_validated_rule_spec"))
    lessons = arguments.get("lessons") or ()
    if not isinstance(lessons, (list, tuple)):
        lessons = [lessons]
    request = {
        "vehicle": arguments.get("vehicle"),
        "slot": arguments.get("slot"),
        "generation": arguments.get("generation"),
        "count": arguments.get("count"),
        "root_rule_spec": _bounded(root) if root is not None else None,
        "diagnosis": _bounded(arguments.get("diagnosis")) if "diagnosis" in arguments else None,
        "context": _bounded(arguments.get("context")) if "context" in arguments else None,
        "lessons": _bounded(list(lessons)[:MAX_LESSONS]),
        "lessons_given": len(lessons),
    }
    evidence = getattr(result, "evidence", None) or {}
    response: dict[str, Any] = {
        "success": bool(getattr(result, "success", False)),
        "error": getattr(result, "error", None),
        "attempts": evidence.get("attempts"),
        "attempt_errors": evidence.get("attempt_errors"),
        "prompt_hash": evidence.get("prompt_hash"),
        "request_hash": evidence.get("request_hash"),
        "response_hash": evidence.get("raw_response_hash"),
    }
    spec = getattr(result, "rule_spec", None)
    if spec is not None:
        response.update(rule_spec=_bounded(spec), variant_id=getattr(result, "variant_id", None),
                        family=spec.get("family") if isinstance(spec, Mapping) else None,
                        changed=spec_changes(root, spec),
                        thesis=getattr(result, "thesis", None))
    variants = getattr(result, "variants", ()) or ()
    if variants:
        response["variants"] = [{
            "variant_id": v.get("variant_id"),
            "reason": v.get("reason"),
            "builds_on": v.get("builds_on"),
            "changed": spec_changes(root, v.get("rule_spec")),
        } for v in variants if isinstance(v, Mapping)]
    _write({"event": "call", "kind": kind, "provider": provider, "model": model,
            "request": request, "response": response}, path)


def record_decision(kind: str, *, accepted: bool, provider_success: bool | None,
                    error: str | None, evidence: Mapping[str, Any] | None,
                    kept: Sequence[Mapping[str, Any]] = (),
                    path: Path | None = None) -> None:
    """What the factory did with a call's output (and, for tuning, which variants it kept)."""
    evidence = evidence or {}
    _write({"event": "decision", "kind": kind, "accepted": bool(accepted),
            "provider_success": provider_success, "error": error,
            "rejected_because": evidence.get("rejected"),
            "variant_id": evidence.get("variant_id"),
            "kept": [dict(k) for k in kept]}, path)


# --------------------------------------------------------------------------
# report


def read(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _changes(changed: Mapping[str, Any] | None) -> str:
    if not changed:
        return ""
    return "; ".join(f"{k}: {v[0]} -> {v[1]}" for k, v in changed.items() if k != "schema")


def render(rows: Iterable[Mapping[str, Any]]) -> str:
    rows = list(rows)
    calls = [r for r in rows if r.get("event") == "call"]
    decisions = [r for r in rows if r.get("event") == "decision"]
    kinds = sorted({r.get("kind") for r in calls})
    lines = ["# LLM research calls", "",
             f"{len(calls)} calls and {len(decisions)} factory decisions in the log.", "",
             "| kind | calls | provider answered | kept by the factory |",
             "| --- | --- | --- | --- |"]
    for kind in kinds:
        k_calls = [r for r in calls if r.get("kind") == kind]
        k_dec = [r for r in decisions if r.get("kind") == kind]
        lines.append(f"| {kind} | {len(k_calls)} | "
                     f"{sum(bool(r['response'].get('success')) for r in k_calls)} | "
                     f"{sum(bool(r.get('accepted')) for r in k_dec)} |")
    lines += ["", "## Every call", "",
              "| time (UTC) | kind | model | given | returned | changed against the root | "
              "thesis or reasons |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in calls:
        req, res = r.get("request", {}), r.get("response", {})
        root = req.get("root_rule_spec") or {}
        given = []
        if isinstance(root, Mapping) and root.get("family"):
            given.append(f"root {root.get('family')}")
        if req.get("lessons_given"):
            given.append(f"{req['lessons_given']} lessons")
        if req.get("count"):
            given.append(f"asked for {req['count']}")
        if not res.get("success"):
            returned = f"error: {str(res.get('error'))[:120]}"
            changed = why = ""
        elif res.get("variants"):
            returned = f"{len(res['variants'])} variants"
            changed = "<br>".join(_changes(v.get("changed")) for v in res["variants"])
            why = "<br>".join(str(v.get("reason") or "") for v in res["variants"])
        else:
            returned = f"{res.get('family')} `{res.get('variant_id')}`"
            changed = _changes(res.get("changed"))
            why = str(res.get("thesis") or "")
        lines.append(f"| {str(r.get('ts'))[:19]} | {r.get('kind')} | {r.get('model')} | "
                     f"{', '.join(given)} | {returned} | {changed} | {why} |")
    lines += ["", "## Factory decisions", "",
              "| time (UTC) | kind | kept | rejected because | variants tested |",
              "| --- | --- | --- | --- | --- |"]
    for r in decisions:
        kept = ", ".join(f"`{k.get('variant_id')}` ({k.get('source')})" for k in r.get("kept") or [])
        lines.append(f"| {str(r.get('ts'))[:19]} | {r.get('kind')} | "
                     f"{'yes' if r.get('accepted') else 'no'} | "
                     f"{r.get('rejected_because') or r.get('error') or ''} | {kept} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the LLM research audit log.")
    parser.add_argument("--log", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    path = args.log or audit_path()
    if path is None or not path.exists():
        print(f"no audit log at {path}", file=sys.stderr)
        return 1
    text = render(read(path))
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
