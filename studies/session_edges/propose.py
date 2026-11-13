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
import math
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from research.llm_strategy import content_hash
from studies.session_edges import grammar

PROPOSAL_SCHEMA = "session-edge-proposals.v1"
MAX_PROPOSALS = 25
MAX_THESIS_CHARS = 240
MAX_RESPONSE_BYTES = 65_536
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
        promoted.append({"spec": _prompt_spec(row["spec"]), "design_t": _finite_stat((row.get("design") or {}).get("t"))})
    # Project each row as well as the outer record. A caller-supplied leaderboard
    # must not smuggle held-out numbers or raw observations into a future prompt.
    leaderboard = []
    for row in (record or {}).get("design_leaderboard") or []:
        leaderboard.append({"spec": _prompt_spec(row["spec"]),
                            **{key: _finite_stat(row[key]) for key in
                               ("design_t", "design_net_bps_per_session") if key in row}})
    payload = {"grammar": grammar.describe(), "symbols": [str(symbol) for symbol in symbols],
               "design_leaderboard": leaderboard,
               "already_promoted": promoted,
               "wanted": max(1, min(int(count), MAX_PROPOSALS))}
    return {"system": SYSTEM_PROMPT, "user": json.dumps(payload, sort_keys=True)}


def _finite_stat(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("design statistics must be finite numbers")
    return float(value)


def _prompt_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    grammar.normalise({key: spec[key] for key in ("family", "symbol", "params") if key in spec})
    return {key: spec[key] for key in ("family", "symbol", "params") if key in spec}


def parse_reply(text: str, *, symbols: Sequence[str] | None = None
                ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Valid hypotheses and rejected ones (each with the reason), from a model's reply."""
    if len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
        return [], [{"spec": None, "reason": "reply exceeds the response byte limit"}]
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


def _provider_request(model: str, prompt: Mapping[str, str], config: Mapping[str, Any]) -> dict[str, Any]:
    request: dict[str, Any] = {"model": model, "messages": [
        {"role": "system", "content": prompt["system"]},
        {"role": "user", "content": prompt["user"]}]}
    if "timeout_seconds" in config:
        request["timeout"] = config["timeout_seconds"]
    for key in ("temperature", "seed", "max_completion_tokens"):
        if config.get(key) is not None:
            request[key] = config[key]
    return request


def _call(model: str, prompt: Mapping[str, str], *, client: Any = None,
          config: Mapping[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    if client is None:
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set; use the prompt command and send it by hand")
        from openai import OpenAI
        client = OpenAI(max_retries=0)
    response = client.chat.completions.create(**_provider_request(model, prompt, config or {}))
    metadata = {key: getattr(response, key, None) for key in ("id", "model", "system_fingerprint")}
    dump = getattr(response, "model_dump", None)
    metadata["response_payload"] = dump(mode="json") if callable(dump) else None
    return str(response.choices[0].message.content or ""), metadata


def ask(model: str, prompt: Mapping[str, str], *, client: Any = None) -> str:
    """One chat completion from an OpenAI-compatible provider; the key comes from the environment."""
    return _call(model, prompt, client=client)[0]


def _config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    config = dict(config or {})
    unknown = set(config) - {"timeout_seconds", "temperature", "seed", "max_completion_tokens"}
    if unknown:
        raise ValueError(f"unknown provider configuration fields: {sorted(unknown)}")
    timeout = config.get("timeout_seconds", 30.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise ValueError("timeout_seconds must be finite and between 0 and 120")
    config["timeout_seconds"] = float(timeout)
    temperature = config.get("temperature")
    if temperature is not None and (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
                                    or not math.isfinite(temperature) or not 0 <= temperature <= 2):
        raise ValueError("temperature must be null or a finite number between 0 and 2")
    for key, low, high in (("seed", 0, 2**31 - 1), ("max_completion_tokens", 1, 32_768)):
        value = config.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high):
            raise ValueError(f"{key} is outside its integer bounds")
    return config


def endpoint_identity(base_url: str | None) -> dict[str, str | None]:
    """Identify the endpoint without writing credentials or signed URL values."""
    if not base_url:
        return {"origin": "https://api.openai.com", "url_hash": content_hash("https://api.openai.com/v1")}
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("provider endpoint must be an HTTP(S) URL")
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    return {"origin": f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port else ""),
            "url_hash": content_hash(base_url)}


def proposal_envelope(response_text: str | None, *, provider: str, model: str,
                      prompt: Mapping[str, str] | None = None, symbols: Sequence[str] | None = None,
                      config: Mapping[str, Any] | None = None, attempt_id: str | None = None,
                      transport_metadata: Mapping[str, Any] | None = None,
                      endpoint: Mapping[str, Any] | None = None, provider_call_observed: bool = False,
                      error_type: str | None = None) -> dict[str, Any]:
    """Seal an exact non-secret research request and reply, including failed calls.

    Hashes bind reproducible local evidence; they are not provider signatures or
    proof that a call occurred. Manual checks explicitly retain missing context.
    """
    if not provider.strip() or not model.strip():
        raise ValueError("provider and model identity are required")
    configuration = _config(config)
    safe_prompt = None if prompt is None else {key: str(prompt[key]) for key in ("system", "user")}
    request = None if safe_prompt is None else _provider_request(model, safe_prompt, configuration)
    hypotheses, rejected = parse_reply(response_text, symbols=symbols) if response_text is not None else ([], [])
    if response_text is not None and len(response_text.encode("utf-8")) > MAX_RESPONSE_BYTES:
        # Do not retain unbounded provider payloads; their exact digest survives.
        raise ValueError("reply exceeds the response byte limit")
    metadata = {key: (transport_metadata or {}).get(key) for key in ("id", "model", "system_fingerprint")}
    provider_response = (transport_metadata or {}).get("response_payload")
    captured_response = {"text": response_text, "payload": provider_response}
    endpoint = dict(endpoint or {"origin": None, "url_hash": None})
    configuration_identity = {"request_config": configuration, "endpoint": endpoint, "max_retries": 0}
    provenance = {"provider": provider, "model": model, "prompt": safe_prompt, "request": request,
                  "response_text": response_text, "provider_response": provider_response,
                  "response_capture": "full" if provider_response is not None else "text_only",
                  "config": configuration_identity, "endpoint": endpoint,
                  "transport_metadata": metadata, "provider_call_observed": bool(provider_call_observed),
                  "attribution_complete": safe_prompt is not None,
                  "grammar_hash": content_hash(grammar.describe()),
                  "prompt_hash": content_hash(safe_prompt), "request_hash": content_hash(request),
                  "response_hash": content_hash(captured_response), "config_hash": content_hash(configuration_identity)}
    envelope = {"schema": PROPOSAL_SCHEMA, "authorizing": False,
                "attempt_id": attempt_id or uuid.uuid4().hex,
                "status": "provider_failed" if error_type else "completed",
                "error_type": error_type, "symbols": list(symbols) if symbols is not None else None,
                "provenance": provenance, "hypotheses": hypotheses, "rejected": rejected}
    envelope["envelope_hash"] = content_hash(envelope)
    return envelope


def ask_envelope(model: str, prompt: Mapping[str, str], *, symbols: Sequence[str],
                 provider: str = "openai-compatible", config: Mapping[str, Any] | None = None,
                 client: Any = None, attempt_id: str | None = None) -> dict[str, Any]:
    """Make one explicitly requested call; failures still produce an attempt receipt."""
    configuration = _config(config)
    endpoint = endpoint_identity(str(getattr(client, "base_url", "")) if client is not None
                                 else os.getenv("OPENAI_BASE_URL"))
    try:
        reply, metadata = _call(model, prompt, client=client, config=configuration)
        return proposal_envelope(reply, provider=provider, model=model, prompt=prompt, symbols=symbols,
                                 config=configuration, attempt_id=attempt_id, transport_metadata=metadata,
                                 endpoint=endpoint, provider_call_observed=True)
    except Exception as error:  # a failure is recorded without response/credential-bearing error text
        return proposal_envelope(None, provider=provider, model=model, prompt=prompt, symbols=symbols,
                                 config=configuration, attempt_id=attempt_id, endpoint=endpoint,
                                 error_type=type(error).__name__)


def proposal_rows(payload: Any) -> list[dict[str, Any]]:
    """Read an attributed envelope or a compatible legacy list, failing on edits."""
    if isinstance(payload, list):
        rows = []
        for index, row in enumerate(payload):
            if not isinstance(row, Mapping):
                raise ValueError("proposal list entries must be objects")
            rows.append({**row, "proposal_attribution": {"status": "legacy_unattributed",
                                                        "thesis": row.get("thesis"), "index": index}})
        return rows
    if not isinstance(payload, Mapping) or payload.get("schema") != PROPOSAL_SCHEMA:
        raise ValueError("proposals must be a legacy list or a session-edge-proposals.v1 envelope")
    envelope = dict(payload)
    digest = envelope.pop("envelope_hash", None)
    if payload.get("authorizing") is not False or digest != content_hash(envelope):
        raise ValueError("proposal envelope integrity mismatch")
    provenance = payload["provenance"]
    for name in ("prompt", "request", "response", "config"):
        source = ({"text": provenance.get("response_text"), "payload": provenance.get("provider_response")}
                  if name == "response" else provenance.get(name))
        if provenance.get(f"{name}_hash") != content_hash(source):
            raise ValueError(f"proposal {name} identity mismatch")
    if provenance.get("grammar_hash") != content_hash(grammar.describe()):
        raise ValueError("proposal grammar changed; generate a new proposal receipt")
    prompt = provenance.get("prompt")
    expected_request = None if prompt is None else _provider_request(
        provenance["model"], prompt, _config(provenance["config"]["request_config"]))
    if expected_request != provenance.get("request"):
        raise ValueError("proposal request differs from prompt/model/config identity")
    response = provenance.get("response_text")
    expected, rejected = parse_reply(response, symbols=payload.get("symbols")) if response is not None else ([], [])
    if expected != payload.get("hypotheses") or rejected != payload.get("rejected"):
        raise ValueError("proposal hypotheses do not match the retained response")
    attribution = {key: provenance[key] for key in (
        "provider", "model", "prompt_hash", "request_hash", "response_hash", "config_hash", "grammar_hash",
        "provider_call_observed", "attribution_complete")}
    attribution.update({"attempt_id": payload["attempt_id"], "envelope_hash": digest,
                        "reported_model": provenance["transport_metadata"].get("model")})
    return [{**row, "proposal_attribution": {**attribution, "thesis": row["thesis"], "index": index}}
            for index, row in enumerate(expected)]


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


def _write_envelope(envelope: Mapping[str, Any], out: str | None) -> int:
    text = json.dumps(envelope, indent=1, sort_keys=True, allow_nan=False) + "\n"
    if out:
        Path(out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    print(f"attempt {envelope['attempt_id']}: {len(envelope['hypotheses'])} valid, "
          f"{len(envelope['rejected'])} rejected; {envelope['status']}", file=sys.stderr)
    return 2 if envelope["status"] == "provider_failed" else (0 if envelope["hypotheses"] else 1)


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
            command.add_argument("--provider", default="openai-compatible")
            command.add_argument("--attempt-id")
            command.add_argument("--timeout-seconds", type=float, default=30)
            command.add_argument("--temperature", type=float)
            command.add_argument("--seed", type=int)
            command.add_argument("--out", help="write the valid proposals here")
    check = sub.add_parser("check")
    check.add_argument("reply", help="file holding the model's reply")
    check.add_argument("--symbols", help="restrict proposals to these instruments")
    check.add_argument("--out", help="write the valid proposals here")
    check.add_argument("--provider", default="manual")
    check.add_argument("--model", default="unknown")
    check.add_argument("--prompt", help="optional exact prompt JSON from the prompt command")
    check.add_argument("--attempt-id")
    args = parser.parse_args(argv)
    symbols = [item.strip() for item in (args.symbols or "").split(",") if item.strip()]
    if args.command == "check":
        envelope = proposal_envelope(Path(args.reply).read_text(encoding="utf-8"), provider=args.provider,
                                     model=args.model, symbols=symbols or None,
                                     prompt=_record(args.prompt), attempt_id=args.attempt_id)
        return _write_envelope(envelope, args.out)
    prompt = build_prompt(_record(args.discovery), symbols, count=args.count)
    if args.command == "prompt":
        print(json.dumps(prompt, indent=1))
        return 0
    envelope = ask_envelope(args.model, prompt, provider=args.provider, symbols=symbols,
                            config={"timeout_seconds": args.timeout_seconds,
                                    "temperature": args.temperature, "seed": args.seed},
                            attempt_id=args.attempt_id)
    return _write_envelope(envelope, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
