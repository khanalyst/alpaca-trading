"""Let a language model propose hypotheses inside the grammar, and check what comes back.

Usage, from the repository root::

    # write the prompt, to send to any model by hand
    python -m studies.session_edges.propose prompt --discovery OUT/discovery.json --symbols QQQ,SPY,IWM

    # or ask the configured OpenAI-compatible provider directly (OPENAI_API_KEY in the environment)
    python -m studies.session_edges.propose ask --model MODEL --discovery OUT/discovery.json \\
        --symbols QQQ,SPY,IWM --out proposals.json

    # validate a reply saved to a file
    python -m studies.session_edges.propose check reply.json --out proposals.json

    # then test the proposals under the same protocol as the grid
    python -I studies/session_edges/discover.py ... --proposals proposals.json

The model has real discretion and hard bounds. It may choose any family,
instrument and parameter value the grammar allows and must say why the edge
should exist. It cannot add a signal, see a holdout result (the prompt carries
design-period statistics only) or lower a threshold: a proposal is screened
on the design period, and every proposal that reaches the holdout is added to
the correction, so asking again makes confirmation harder, not easier.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from studies.session_edges import grammar

MAX_PROPOSALS = 25
MAX_THESIS_CHARS = 240
SYSTEM_PROMPT = """You propose trading hypotheses for a systematic research process on US index and \
sector ETFs. Reply with one JSON object and nothing else:
{"hypotheses": [{"family": "...", "symbol": "...", "params": {...}, "thesis": "..."}]}

Rules:
1. Use only the families and parameters in the grammar you are given, with values inside their \
ranges or choices. You cannot add a signal, an indicator, an instrument outside the list, code, or \
any other field.
2. "thesis" is one sentence, at most 240 characters, naming the mechanism: who is on the other \
side of the trade and why they keep paying. A hypothesis without a mechanism is noise.
3. Prefer ideas that differ in kind from the leaderboard: another family, another instrument \
with the same economic driver, or a parameter region that tests the mechanism rather than a \
neighbouring value.
4. Do not repeat a hypothesis you are shown.
You are shown design-period statistics only. Every proposal is screened on that period and, if it \
reaches the holdout, is added to the multiple-testing correction, so a weak guess costs evidence."""


def build_prompt(record: Mapping[str, Any] | None, symbols: Sequence[str], *, count: int = 10) -> dict[str, str]:
    """The system and user messages. Holdout results are never included."""
    promoted = []
    for row in ((record or {}).get("promoted") or {}).values():
        promoted.append({"spec": row["spec"], "design_t": (row.get("design") or {}).get("t")})
    payload = {"grammar": grammar.describe(), "symbols": [str(symbol) for symbol in symbols],
               "design_leaderboard": list((record or {}).get("design_leaderboard") or []),
               "already_promoted": promoted,
               "wanted": max(1, min(int(count), MAX_PROPOSALS))}
    return {"system": SYSTEM_PROMPT, "user": json.dumps(payload, sort_keys=True)}


def parse_reply(text: str, *, symbols: Sequence[str] | None = None
                ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Valid hypotheses and rejected ones (each with the reason), from a model's reply."""
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`")
        body = body[body.index("\n") + 1:] if "\n" in body else body
    try:
        data = json.loads(body)
    except json.JSONDecodeError as error:
        return [], [{"spec": None, "reason": f"reply is not JSON: {error.msg}"}]
    rows = data.get("hypotheses") if isinstance(data, Mapping) else data
    if not isinstance(rows, list):
        return [], [{"spec": None, "reason": "reply has no list of hypotheses"}]
    allowed = None if symbols is None else {str(symbol) for symbol in symbols}
    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        try:
            canonical = grammar.normalise(row)
            if allowed is not None and canonical["symbol"] not in allowed:
                raise grammar.SpecError(f"symbol {canonical['symbol']!r} is not in the allowed list")
            thesis = str(row.get("thesis") or "").strip()
            if not thesis:
                raise grammar.SpecError("a thesis is required")
            key = grammar.spec_id(canonical)
            if key in seen:
                raise grammar.SpecError("duplicate hypothesis")
            if len(valid) >= MAX_PROPOSALS:
                raise grammar.SpecError(f"more than {MAX_PROPOSALS} proposals")
        except grammar.SpecError as error:
            rejected.append({"spec": row if isinstance(row, Mapping) else None, "reason": str(error)})
            continue
        seen.add(key)
        valid.append({**canonical, "thesis": thesis[:MAX_THESIS_CHARS]})
    return valid, rejected


def ask(model: str, prompt: Mapping[str, str], *, client: Any = None) -> str:
    """One chat completion from an OpenAI-compatible provider; the key comes from the environment."""
    if client is None:
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set; use the prompt command and send it by hand")
        from openai import OpenAI
        client = OpenAI()
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": prompt["system"]}, {"role": "user", "content": prompt["user"]}])
    return str(response.choices[0].message.content or "")


def _record(path: str | None) -> dict[str, Any] | None:
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else None


def _write(valid: Sequence[Mapping[str, Any]], rejected: Sequence[Mapping[str, Any]], out: str | None) -> int:
    text = json.dumps(list(valid), indent=1, sort_keys=True) + "\n"
    if out:
        Path(out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    for row in rejected:
        print(f"rejected: {row['reason']}", file=sys.stderr)
    print(f"{len(valid)} valid, {len(rejected)} rejected", file=sys.stderr)
    return 0 if valid else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prompt", "ask"):
        command = sub.add_parser(name)
        command.add_argument("--discovery", help="discovery.json of an earlier run (design statistics only are used)")
        command.add_argument("--symbols", required=True, help="instruments the model may use")
        command.add_argument("--count", type=int, default=10)
        if name == "ask":
            command.add_argument("--model", required=True)
            command.add_argument("--out", help="write the valid proposals here")
    check = sub.add_parser("check")
    check.add_argument("reply", help="file holding the model's reply")
    check.add_argument("--symbols", help="restrict proposals to these instruments")
    check.add_argument("--out", help="write the valid proposals here")
    args = parser.parse_args(argv)
    symbols = [item.strip() for item in (args.symbols or "").split(",") if item.strip()]
    if args.command == "check":
        valid, rejected = parse_reply(Path(args.reply).read_text(encoding="utf-8"), symbols=symbols or None)
        return _write(valid, rejected, args.out)
    prompt = build_prompt(_record(args.discovery), symbols, count=args.count)
    if args.command == "prompt":
        print(json.dumps(prompt, indent=1))
        return 0
    try:
        reply = ask(args.model, prompt)
    except Exception as exc:  # noqa: BLE001 - a provider failure is reported, never worked around
        print(f"the provider call failed: {str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__}",
              file=sys.stderr)
        return 2
    valid, rejected = parse_reply(reply, symbols=symbols)
    return _write(valid, rejected, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
