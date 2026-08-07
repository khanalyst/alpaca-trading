"""Score every registered strategy through the seven gates, and rank them.

One command answers "what do we currently believe about each strategy, and
why". It reads the register (agent/registry.py) for what exists, the
pre-registration files (research/hypotheses/*.yaml) for what was claimed
before the test ran, and produces a leaderboard plus a written report.

Two rules keep it honest:

**No score without a pre-registration.** A strategy with no hypothesis file
is not scored at all. The file records the parameters and the hypothesis
count before the result exists, which is the only defence against quietly
tuning until something looks good.

**The benchmark must reproduce.** momentum/phase1-v3 is scored on every run
even though its answer is known. If the harness stops reproducing its
measured failure, the harness has broken and every other number in the run
is suspect. That check is the reason this is a tournament and not a search.

    python research/tournament.py --data runtime/research/data
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import json
import signal
import shutil
import sys
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from math import isfinite
from numbers import Integral, Real
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "research"))

import yaml  # noqa: E402

from agent.forward_models import BY_STRATEGY  # noqa: E402
from agent.registry import REGISTRY, TIERS  # noqa: E402
try:  # support both `python -m research.tournament` and legacy imports
    from . import gates as gate_mod  # noqa: E402
    from .findings import FindingsStore, _content_hash, resolve_store_path  # noqa: E402
    from .edge_lab import (DEFAULT_COST_SCENARIO,  # noqa: E402
                           Contract, FlushFadeContract,
                           FundingCarryContract, TrendMultidayContract,
                           load_dataset, universe_membership)
except ImportError:  # tests and direct script execution
    import gates as gate_mod  # noqa: E402
    from findings import FindingsStore, _content_hash, resolve_store_path  # noqa: E402
    from edge_lab import (DEFAULT_COST_SCENARIO,  # noqa: E402
                          Contract, FlushFadeContract,
                          FundingCarryContract, TrendMultidayContract,
                          load_dataset, universe_membership)


HYPOTHESES = REPO / "research" / "hypotheses"
DATA_MANIFEST_SCHEMA = "okx-history-snapshot.v1"


class TournamentFailure(RuntimeError):
    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _strict_json(path: Path) -> object:
    def reject_constant(token: str):
        raise ValueError(f"non-finite JSON constant {token}")

    return json.loads(
        path.read_text(encoding="utf-8"), parse_constant=reject_constant)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _csv_identity(path: Path) -> dict:
    rows = 0
    first = None
    last = None
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "timestamp_ms" not in reader.fieldnames:
            raise ValueError("missing timestamp_ms column")
        for row in reader:
            try:
                timestamp = int(row["timestamp_ms"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("invalid timestamp_ms value") from exc
            rows += 1
            first = timestamp if first is None else min(first, timestamp)
            last = timestamp if last is None else max(last, timestamp)
    return {
        "sha256": _sha256_file(path),
        "rows": rows,
        "first_timestamp_ms": first,
        "last_timestamp_ms": last,
    }


def validate_data_snapshot(root: Path) -> dict:
    """Require an exact, untampered downloader snapshot before scoring."""
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise TournamentFailure(
            f"data snapshot has no manifest.json: {root}", exit_code=2)
    try:
        manifest = _strict_json(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise TournamentFailure(
            f"data manifest is unreadable or non-standard JSON: {exc}",
            exit_code=2) from exc
    if not isinstance(manifest, dict):
        raise TournamentFailure("data manifest must be a JSON object", 2)
    if manifest.get("schema") != DATA_MANIFEST_SCHEMA:
        raise TournamentFailure(
            f"data manifest schema must be {DATA_MANIFEST_SCHEMA!r}; "
            "legacy exports remain preserved but are not provenance-safe "
            "tournament inputs", 2)
    declared_rows = manifest.get("files")
    if not isinstance(declared_rows, list):
        raise TournamentFailure("data manifest files must be a list", 2)

    linked = sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*")
        if path.is_symlink())
    if linked:
        raise TournamentFailure(
            "data snapshot contains symbolic links: " + ", ".join(linked), 2)
    actual = {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file() and path != manifest_path
    }
    declared: dict[str, dict] = {}
    for item in declared_rows:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise TournamentFailure("data manifest has an invalid file entry", 2)
        name = item["path"]
        relative = Path(name)
        if (relative.is_absolute() or ".." in relative.parts
                or relative.as_posix() != name or name == "manifest.json"):
            raise TournamentFailure(
                f"data manifest has an unsafe file path: {name!r}", 2)
        if name in declared:
            raise TournamentFailure(
                f"data manifest declares {name!r} more than once", 2)
        declared[name] = item

    missing = sorted(set(declared) - set(actual))
    extra = sorted(set(actual) - set(declared))
    if missing or extra:
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if extra:
            details.append("extra: " + ", ".join(extra))
        raise TournamentFailure(
            "data snapshot membership mismatch (" + "; ".join(details) + ")",
            2)

    for name, expected in declared.items():
        path = actual[name]
        if path.suffix != ".csv":
            raise TournamentFailure(
                f"data snapshot contains unsupported file {name!r}", 2)
        try:
            observed = _csv_identity(path)
        except (OSError, UnicodeError, csv.Error, ValueError) as exc:
            raise TournamentFailure(
                f"data snapshot file {name!r} is invalid: {exc}", 2) from exc
        for field in ("sha256", "rows", "first_timestamp_ms",
                      "last_timestamp_ms"):
            if expected.get(field) != observed[field]:
                raise TournamentFailure(
                    f"data snapshot {field} mismatch for {name!r}: "
                    f"manifest={expected.get(field)!r}, "
                    f"observed={observed[field]!r}", 2)
    return manifest


def _json_safe(value: object) -> object:
    """Convert non-finite numeric output to JSON null recursively."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        number = float(value)
        return number if isfinite(number) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _identity_name(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _file_identities(paths: list[Path]) -> list[dict]:
    identities = []
    for path in sorted(set(paths), key=lambda item: str(item)):
        item = {"path": _identity_name(path)}
        try:
            stat = path.stat()
            item.update({
                "size_bytes": stat.st_size,
                "sha256": _sha256_file(path),
            })
        except OSError as exc:
            item["error"] = str(exc)
        identities.append(item)
    return identities


def _regular_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        path for path in root.rglob("*")
        if path.is_file() and not path.is_symlink())


def _identity_payload(args: argparse.Namespace) -> dict:
    code_paths = (
        _regular_files(REPO / "agent")
        + [path for path in _regular_files(REPO / "research")
           if path.suffix == ".py"]
        + [REPO / "research.py"])
    code_paths = [path for path in code_paths if path.suffix == ".py"]
    config_paths = [REPO / "config.yaml", REPO / "research" / "variants.yaml"]
    config_paths += sorted(HYPOTHESES.glob("*.yaml"))
    input_paths = _regular_files(args.data)
    if args.forward and args.forward.is_file():
        input_paths.append(args.forward)
    manifest_paths = [
        path for path in input_paths
        if "manifest" in path.name.lower() and path.suffix.lower() == ".json"
    ]
    code_files = _file_identities(code_paths)
    config_files = _file_identities(config_paths)
    manifests = _file_identities(manifest_paths)
    inputs = _file_identities(input_paths)
    return {
        "code_identity": {
            "sha256": _content_hash(code_files),
            "files": code_files,
        },
        "config_identity": {
            "sha256": _content_hash(config_files),
            "files": config_files,
        },
        "data_manifest_identity": {
            "sha256": _content_hash(manifests),
            "files": manifests,
            "present": bool(manifests),
        },
        "input_hashes": {
            "sha256": _content_hash(inputs),
            "files": inputs,
        },
    }


def _json_write_once(path: Path, payload: object) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(_json_safe(payload), handle, indent=2, sort_keys=True,
                  default=str, allow_nan=False)
        handle.write("\n")


def _text_write_once(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)


def _copy_latest(run_directory: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for name in ("REPORT.md", "leaderboard.json"):
        shutil.copyfile(run_directory / name, output / name)


def _failure_report(run: dict, errors: list[dict]) -> str:
    lines = [
        "# Strategy tournament", "",
        f"Run `{run['run_id']}` failed before scoring completed.", "",
        f"- started: {run['started_utc']}",
        f"- data root: `{run['options'].get('data')}`",
        f"- durable run directory: `{run['run_directory']}`", "",
        "## Errors", "",
    ]
    for error in errors:
        lines.append(
            f"- `{error.get('type', 'Error')}`: {error.get('message', '')}")
    lines += ["", "No prior tournament run was overwritten.", ""]
    return "\n".join(lines)

# Which research contract implements each registered strategy. A strategy in
# the register with no entry here is registered but not yet testable, which
# the report states rather than silently skipping.

# Each entry returns the contract at its REGISTERED parameterisation. The
# pre-registration's `settings:` block names the alternatives, and
# `axis_settings` derives them from this one with `dataclasses.replace`, so a
# setting cannot silently change a field the registered point never had.
CONTRACTS = {
    "momentum": lambda cfg: Contract.from_config(cfg) if cfg else Contract(),
    "flush-fade": lambda cfg: FlushFadeContract(),
    "trend-multiday": lambda cfg: TrendMultidayContract(),
    "funding-carry": lambda cfg: FundingCarryContract(),
    # Same entry rule as funding-carry under an opposite mechanism
    # claim. Sharing the contract is deliberate: what differs is the
    # claim about where the money comes from, and the attribution
    # gate is what tells the two apart.
    "funding-unwind": lambda cfg: FundingCarryContract(),
    # ls-ratio-fade is deliberately absent. Its input series is served for
    # ~30 days and is not in the research dataset, so it cannot be
    # backtested honestly - it accumulates forward evidence through shadow
    # evaluation only, and the report says so rather than scoring it on
    # data that does not exist.
}

# Registry names are the live/shadow contract names. Most research contracts
# use the same names; the few aliases below are explicit because silently
# relying on dataclass defaults is how the registered point drifted. Momentum
# is intentionally absent: it remains config-driven so the benchmark keeps
# its existing semantics. Strategies without a research contract are not
# scored here, so no mapping is guessed for them.
REGISTERED_PARAM_ALIASES = {
    "flush-fade": {
        "flush_min_move_atr": "min_move_atr",
        "flush_min_oi_drop_pct": "min_oi_drop_pct",
        "flush_min_relative_volume": "min_relative_volume",
    },
    "funding-carry": {
        "carry_percentile": "percentile",
        "carry_min_samples": "min_samples",
    },
    "funding-unwind": {
        "unwind_percentile": "percentile",
        "unwind_min_samples": "min_samples",
    },
    "trend-multiday": {
        "trend_min_range_pos_pct": "min_range_pos_pct",
        "trend_max_atr_ratio": "max_atr_ratio",
    },
}

# The benchmark's known values, from research/results/edge-audit-2024-2026.
# Reproduction is checked loosely: different data windows legitimately move
# the number, but a sign flip or an order-of-magnitude change means the
# harness is measuring something else.
BENCHMARK_ID = "momentum"
BENCHMARK_EXPECTED_R = -0.096
BENCHMARK_TOLERANCE_R = 0.20


def load_preregistration(strategy_id: str) -> dict | None:
    path = HYPOTHESES / f"{strategy_id}.yaml"
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text())

REGISTERED_SETTING = "registered"


def registered_contract(strategy_id: str, cfg: dict | None):
    """Build the research contract at the registry's stated point."""
    base = CONTRACTS[strategy_id](cfg)
    if strategy_id == BENCHMARK_ID:
        return base

    fields = {field.name for field in dataclasses.fields(base)}
    aliases = REGISTERED_PARAM_ALIASES.get(strategy_id, {})
    params = {}
    for name, value in REGISTRY[strategy_id].contract_params.items():
        target = name if name in fields else aliases.get(name)
        if target not in fields:
            raise ValueError(
                f"{strategy_id}: registered parameter {name!r} has no safe "
                "mapping to its research contract")
        params[target] = value
    return dataclasses.replace(base, **params) if params else base


def axis_settings(strategy_id: str, prereg: dict, cfg: dict | None) -> list[dict]:
    """Every pre-registered parameterisation of one hypothesis.

    A hypothesis used to be scored at exactly one point - the contract's
    dataclass defaults - and the battery would turn that single point into a
    tier. `flush-fade` was rejected at `min_move_atr=1.5` and nothing else was
    ever tried, while the register recorded that the mechanism remained
    plausible. That is the failure `protocol.md` criterion 1 exists to prevent,
    and it only ever governed the journal path.

    The alternatives are declared in the pre-registration file, beside the
    mechanism and the falsifier and before any of them is scored, because a
    parameterisation added after seeing the result is not a robustness check.
    Validation is fail-closed for the same reason `agent/variants.py` refuses
    to invent config structure: a typo that registers cleanly would score the
    registered point three times and report that the parameter made no
    difference.
    """
    base = registered_contract(strategy_id, cfg)
    known = {field.name for field in dataclasses.fields(base)}
    declared = prereg.get("settings")
    if not declared:
        return [{"setting_id": REGISTERED_SETTING, "params": {},
                 "claim": "the pre-registered parameterisation",
                 "contract": base, "registered": True}]
    if not isinstance(declared, list):
        raise ValueError(f"{strategy_id}: settings must be a list")

    out: list[dict] = []
    seen_ids: set[str] = set()
    seen_values: set[tuple] = set()
    for index, entry in enumerate(declared):
        if not isinstance(entry, dict):
            raise ValueError(f"{strategy_id}: setting #{index} is not a mapping")
        unknown_keys = set(entry) - {"id", "params", "claim"}
        if unknown_keys:
            raise ValueError(
                f"{strategy_id}: setting #{index} has unknown field(s): "
                f"{', '.join(sorted(unknown_keys))}")
        setting_id = str(entry.get("id") or "").strip()
        if not setting_id:
            raise ValueError(f"{strategy_id}: setting #{index} has no id")
        if setting_id in seen_ids:
            raise ValueError(
                f"{strategy_id}: duplicate setting id {setting_id!r}")
        params = entry.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError(f"{strategy_id}: {setting_id} params must be a map")
        invalid = sorted(set(params) - known)
        if invalid:
            raise ValueError(
                f"{strategy_id}: {setting_id} sets {', '.join(invalid)}, which "
                f"{base.__class__.__name__} does not have. Available: "
                f"{', '.join(sorted(known))}")
        claim = str(entry.get("claim") or "").strip()
        if len(claim) < 10:
            raise ValueError(
                f"{strategy_id}: {setting_id} must say what it claims, in a "
                "sentence - an unexplained parameterisation is a grid point")
        fingerprint = tuple(sorted(params.items(), key=lambda kv: kv[0]))
        if fingerprint in seen_values:
            raise ValueError(
                f"{strategy_id}: {setting_id} duplicates another setting's "
                "parameters")
        seen_values.add(fingerprint)
        seen_ids.add(setting_id)
        out.append({
            "setting_id": setting_id, "params": dict(params), "claim": claim,
            "contract": dataclasses.replace(base, **params) if params else base,
            "registered": not params,
        })

    if sum(1 for setting in out if setting["registered"]) != 1:
        raise ValueError(
            f"{strategy_id}: exactly one setting must have empty params - the "
            "registered point every other setting is compared against")
    return out


def score_strategy(spec, frames, membership, cfg, cost_name: str,
                   exit_policy: str) -> dict:
    prereg = load_preregistration(spec.id)
    if prereg is None:
        return {
            "strategy_id": spec.id, "version": spec.version,
            "scored": False,
            "reason": (
                f"no pre-registration at research/hypotheses/{spec.id}.yaml; "
                "write the mechanism, parameters and hypothesis count before "
                "running the test"),
            "registered_tier": spec.tier,
        }
    factory = CONTRACTS.get(spec.id)
    if factory is None:
        return {
            "strategy_id": spec.id, "version": spec.version,
            "scored": False,
            "reason": ("registered but no research contract implements it "
                       "yet; see the register's notes for what blocks it"),
            "registered_tier": spec.tier,
            "notes": spec.notes,
        }

    try:
        settings = axis_settings(spec.id, prereg, cfg)
    except ValueError as exc:
        return {
            "strategy_id": spec.id, "version": spec.version,
            "scored": False,
            "reason": f"pre-registered settings are invalid: {exc}",
            "registered_tier": spec.tier,
        }

    effective_exit_policy = exit_policy
    if spec.id in {"funding-carry", "funding-unwind"}:
        model = BY_STRATEGY.get(spec.id)
        if model is None or not model.contract_complete:
            return {
                "strategy_id": spec.id, "version": spec.version,
                "scored": False,
                "reason": ("funding tournament refused: no complete "
                           "authoritative forward exit contract"),
                "registered_tier": spec.tier,
            }
        effective_exit_policy = model.exit_policy

    for setting in settings:
        setting["results"] = gate_mod.run_all(
            spec, frames, membership, setting["contract"],
            cost_name=cost_name, exit_policy=effective_exit_policy,
            prereg=prereg)
        setting["tier"], setting["why"] = gate_mod.tier_from_gates(
            setting["results"])
        setting["headline"] = gate_mod.headline(
            frames, membership, setting["contract"], cost_name,
            effective_exit_policy)

    # The unit of decision is the hypothesis, not the parameterisation that
    # happened to be tried first.
    tier, why, best = gate_mod.tier_from_settings(settings)
    registered = next(setting for setting in settings if setting["registered"])
    contract = registered["contract"]
                   
    # Provenance cap. A hypothesis GENERATED by looking at the data it is
    # now scored on cannot be confirmed by that data, however clean the
    # gates come back - the gates measure the result, and the result is what
    # suggested the hypothesis. Declared in the pre-registration because
    # nothing in the numbers can reveal it, and enforced here because a fact
    # that only lives in a comment is a fact that gets forgotten.
    if prereg.get("in_sample_by_construction") and tier != "T0_REJECTED":
        tier, why = "T1_HYPOTHESIS", (
            "gates return " + tier + ", but this hypothesis was generated "
            "from the data it is scored on, so the result is in-sample by "
            "construction. Needs data that did not suggest it: "
            + str(prereg.get("what_would_change_the_verdict")
                  or "out-of-sample history or forward evidence").strip()
            .replace("\n", " ")[:200])
    # The headline stays the REGISTERED parameterisation's, not the best
    # setting's. benchmark_check compares it against a stored expectation, so
    # letting a better setting supply it would make the harness-reproduction
    # check drift every time the settings list changed - and that check is the
    # reason this is a tournament rather than a search.
    stats = registered["headline"]
    # A tier registered above this battery's ceiling was granted by the
    # authoritative recorded-replay path, which this run cannot reproduce and
    # therefore cannot contradict. Measuring T2 against a registered T3 is not
    # a demotion, it is the battery reaching the end of what it may say - so it
    # must not be reported or alerted as evidence of failure, or the nightly
    # run would file the same false alarm forever.
    beyond_authority = (TIERS.index(spec.tier)
                        > TIERS.index(gate_mod.EXPLORATORY_CEILING))
    return {
        "strategy_id": spec.id,
        "version": spec.version,
        "scored": True,
        "registered_tier": spec.tier,
        "measured_tier": tier,
        "tier_reason": why,
        "exploratory_ceiling": gate_mod.EXPLORATORY_CEILING,
        "beyond_exploratory_authority": beyond_authority,
        "tier_changed": tier != spec.tier and not beyond_authority,
        "hypotheses_tested": prereg.get("hypotheses_tested"),
        "mechanism": spec.mechanism,
        "falsification": spec.falsification,
        "requested_exit_policy": exit_policy,
        "exit_policy": effective_exit_policy,
        "headline": stats,
        # The registered point's gates, so the report's per-strategy gate table
        # keeps describing the parameterisation the register names.
        "gates": [r.as_dict() for r in registered["results"]],
        "settings_tested": len(settings),
        "best_setting": best["setting_id"],
        "settings": [{
            "setting_id": setting["setting_id"],
            "params": setting["params"],
            "claim": setting["claim"],
            "registered": setting["registered"],
            "tier": setting["tier"],
            "why": setting["why"],
            "trades": (setting["headline"] or {}).get("trades", 0),
            "expectancy_pct": (setting["headline"] or {}).get("expectancy_pct"),
            "expectancy_r": (setting["headline"] or {}).get("expectancy_r"),
            "p_adjusted": setting.get("p_adjusted"),
            "significant_corrected": setting.get("significant_corrected"),
            "gates": [r.as_dict() for r in setting["results"]],
        } for setting in settings],
    }


def benchmark_check(rows: list[dict]) -> dict:
    """Confirm the harness still reproduces the known-failing benchmark."""
    row = next((r for r in rows if r["strategy_id"] == BENCHMARK_ID), None)
    if row is None or not row.get("scored"):
        return {"ok": False, "reason": "benchmark was not scored"}
    measured = row["headline"].get("expectancy_r")
    if measured is None:
        return {"ok": False, "reason": "benchmark produced no trades"}
    drift = abs(float(measured) - BENCHMARK_EXPECTED_R)
    ok = float(measured) < 0 and drift <= BENCHMARK_TOLERANCE_R
    return {
        "ok": bool(ok),
        "measured_expectancy_r": float(measured),
        "expected_expectancy_r": BENCHMARK_EXPECTED_R,
        "drift_r": float(drift),
        "tolerance_r": BENCHMARK_TOLERANCE_R,
        "reason": (
            "benchmark reproduces its measured failure"
            if ok else
            "benchmark did NOT reproduce; treat every other number in this "
            "run as unverified until the harness is fixed"),
    }


def _finite_float(value: object) -> float | None:
    """Return a finite float, or None for a missing or malformed value."""
    try:
        number = float(value)                              # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _format_signed(value: object, suffix: str = "") -> str:
    """Format an optional number without letting the report raise.

    A missing expectancy is a normal state - a strategy can have resolved
    trades journalled before any of them has an R multiple - and a nightly
    report that raises on it loses every other strategy's result too.
    """
    number = _finite_float(value)
    if number is None:
        return "n/a"
    return f"{number:+.4f}{suffix}"


def apply_forward_evidence(row: dict, forward: dict) -> None:
    """Attach live/shadow results without promoting an exploratory tier.

    Forward agreement is useful evidence, but this tournament is still the
    recomputed-OHLCV path. T3/T4 promotion belongs to an authoritative,
    G2-valid recorded replay with a persisted decision, not to this report.

    A sign DISAGREEMENT is recorded loudly even when the counts are small.
    It is the earliest available warning that a backtest was fitted, and it
    arrives long before the sample is large enough to prove anything.

    A strategy can have fired signals and shadow summaries before any trade
    has enough future data to resolve. That is a pending state, and comparing
    signs against it would read an absent expectancy as a flat one - which is
    the same sign as the backtest half the time, and means nothing either way.
    """
    if not row.get("scored"):
        return
    entry = (forward.get("strategies") or {}).get(row["strategy_id"])
    if not isinstance(entry, dict) or not entry:
        row["forward"] = {"status": "no forward evidence recorded yet"}
        return

    try:
        observed = max(0, int(entry.get("resolved_trades") or 0))
    except (TypeError, ValueError):
        row["forward"] = {
            "status": ("forward evidence is malformed: resolved_trades is "
                       "not an integer")}
        return

    forward_pct = _finite_float(entry.get("forward_expectancy_pct"))
    backtest_pct = _finite_float((row.get("headline") or {}).get(
        "expectancy_pct"))
    needed = 0
    for gate in row.get("gates") or []:
        if gate.get("gate") == "is_detectable":
            needed = _finite_float(
                (gate.get("numbers") or {}).get("trades_needed")) or 0
            needed = max(0, int(needed))
            break

    row["forward"] = {
        "resolved_trades": observed,
        "trades_needed": needed,
        "forward_expectancy_pct": forward_pct,
        "backtest_expectancy_pct": backtest_pct,
        "signs_agree": None,
        "signals_fired": entry.get("signals_fired"),
        "positions_actually_opened": entry.get("positions_actually_opened"),
        "selectivity_pct": entry.get("selectivity_pct"),
        "progress_pct": (round(observed / needed * 100, 1)
                         if needed else None),
    }

    if not observed:
        row["forward"]["status"] = "no resolved forward trades yet"
        return
    if forward_pct is None:
        row["forward"]["status"] = (
            "resolved trades exist but their expectancy is missing; sign "
            "comparison deferred")
        return
    if backtest_pct is None:
        row["forward"]["status"] = (
            "backtest expectancy is unavailable; sign comparison deferred")
        return

    agrees = (forward_pct >= 0) == (backtest_pct >= 0)
    row["forward"]["signs_agree"] = agrees

    if agrees is False:
        row["forward"]["warning"] = (
            "forward and backtest disagree in sign; treat the backtest as "
            "unconfirmed regardless of how clean it looked")
    if agrees and needed and observed >= needed:
        row["forward"]["next_step"] = (
            "sample target reached; run the authoritative recorded-replay "
            "confirmation before changing a tier")


def recommendation(row: dict) -> str:
    """Deterministic, so nobody has to decide how impressed to be."""
    if not row.get("scored"):
        return "not scored"
    tier = row["measured_tier"]
    if tier == "T0_REJECTED":
        return "REJECT - archive with the finding; do not retest without a new mechanism"
    if tier == "T1_HYPOTHESIS":
        return "HOLD - keep collecting data; nothing testable has passed yet"
    if tier == "T2_CANDIDATE":
        if row.get("beyond_exploratory_authority"):
            return (f"NO CHANGE - already registered "
                    f"{row['registered_tier']} on authoritative evidence this "
                    f"battery cannot reproduce; nothing here revises it")
        return "HOLD - exploratory candidate; authoritative recorded replay is required"
    # Above the ceiling this battery cannot reach. Reachable only if the tier
    # rubric is ever widened, so it states the boundary rather than assuming
    # the tier came from evidence that can authorize anything.
    return (f"HOLD - {tier} is above this battery's "
            f"{row.get('exploratory_ceiling', gate_mod.EXPLORATORY_CEILING)} "
            f"ceiling; only the authoritative recorded-replay path may award "
            f"it")


def alert_on_tier_changes(rows: list[dict], payload: dict) -> None:
    """Notify when a strategy crosses a tier, or the harness breaks.

    Crossing a gate is the only event in this pipeline that should
    interrupt someone. Nobody should have to read a nightly report to find
    out that a strategy was rejected or became eligible - and a broken
    benchmark is more urgent than either, because it invalidates every other
    number in the run.

    Reuses the agent's retried-webhook path, including its local
    failed-delivery queue, so an alert is not lost to a transient outage.
    """
    changes = [r for r in rows
               if r.get("scored") and r.get("tier_changed")]
    check = payload.get("benchmark_check") or {}
    if not changes and check.get("ok", True):
        return
    try:
        import yaml as _yaml

        from agent.alerts import AlertManager
        from agent.config import validate_config
        cfg = validate_config(
            _yaml.safe_load((REPO / "config.yaml").read_text()))
        alerts = AlertManager(cfg)
    except Exception as exc:                               # noqa: BLE001
        print(f"note: tier-change alerting unavailable ({exc})",
              file=sys.stderr)
        return

    if not check.get("ok", True):
        alerts.send(
            "critical", "research_harness_broken",
            "Tournament benchmark did not reproduce; this run's results "
            "are unverified",
            {"reason": check.get("reason"),
             "measured_expectancy_r": check.get("measured_expectancy_r"),
             "expected_expectancy_r": check.get("expected_expectancy_r")})

    for row in changes:
        promoted = (TIERS.index(row["measured_tier"])
                    > TIERS.index(row["registered_tier"]))
        alerts.send(
            "warning",
            "strategy_tier_promoted" if promoted else "strategy_tier_demoted",
            f"{row['strategy_id']}/{row['version']}: "
            f"{row['registered_tier']} -> {row['measured_tier']}",
            {"reason": row["tier_reason"],
             "expectancy_pct": row["headline"].get("expectancy_pct"),
             "trades": row["headline"].get("trades"),
             "note": ("a promotion needs more evidence than a demotion; "
                      "check the run window before acting"
                      if promoted else
                      "evidence of failure - update agent/registry.py")})


def write_report(path: Path, payload: dict) -> None:
    rows = payload["strategies"]
    lines = [
        "# Strategy tournament", "",
        f"Durable run: `{payload.get('run_id', 'legacy')}`.",
        "",
        "This file is immutable inside its run directory. A top-level copy "
        "under `research/results/tournament/` is only the latest view; prior "
        "runs remain under `runs/`.",
        "",
        f"Generated {payload['generated_utc']} from `{payload['data_root']}`.",
        "",
        f"- instruments: {payload['instruments']}",
        f"- bars per instrument (min): {payload['min_bars']}",
        f"- window: {payload['window_start']} to {payload['window_end']}",
        f"- cost scenario: `{payload['cost_scenario']}`, "
        f"exit policy: `{payload['exit_policy']}`",
        "",
        "## Harness check", "",
    ]
    check = payload["benchmark_check"]
    lines += [
        f"**{'PASS' if check['ok'] else 'FAIL'}** - {check['reason']}",
        "",
    ]
    if "measured_expectancy_r" in check:
        lines.append(
            f"Benchmark `{BENCHMARK_ID}` measured "
            f"{_format_signed(check.get('measured_expectancy_r'))} R against "
            f"an expected "
            f"{_format_signed(check.get('expected_expectancy_r'))} R "
            f"(drift {_format_signed(check.get('drift_r'))}, tolerance "
            f"{_format_signed(check.get('tolerance_r'))}).")
        lines.append("")

    lines += ["## Leaderboard", "",
              "| Strategy | Tier | Trades | Expectancy % | Expectancy R | "
              "Hypotheses | Settings | Selected setting | Recommendation |",
              "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |"]
    for row in rows:
        if not row.get("scored"):
            lines.append(
                f"| `{row['strategy_id']}` | {row['registered_tier']} "
                f"(registered) | - | - | - | - | - | - | {row['reason']} |")
            continue
        head = row["headline"]
        lines.append(
            f"| `{row['strategy_id']}/{row['version']}` "
            f"| {row['measured_tier']} "
            f"| {head.get('trades', 0)} "
            f"| {_format_signed(head.get('expectancy_pct'))} "
            f"| {_format_signed(head.get('expectancy_r'))} "
            f"| {row.get('hypotheses_tested', '?')} "
            f"| {row.get('settings_tested', len(row.get('settings') or []))} "
            f"| `{row.get('best_setting', '-')}` "
            f"| {recommendation(row)} |")
    lines.append("")

    for row in rows:
        if not row.get("scored"):
            continue
        lines += [f"## {row['strategy_id']}/{row['version']}", "",
                  f"**Measured tier: {row['measured_tier']}** - "
                  f"{row['tier_reason']}", "",
                  f"Executable exit policy: "
                  f"`{row.get('exit_policy', payload['exit_policy'])}` "
                  f"(run default requested "
                  f"`{row.get('requested_exit_policy', payload['exit_policy'])}`).",
                  ""]
        if row.get("beyond_exploratory_authority"):
            lines += [
                f"> Registered {row['registered_tier']} is above this "
                f"battery's {row['exploratory_ceiling']} ceiling. The "
                f"measured tier below is what exploratory data can show on "
                f"its own; it is not a demotion and it does not revise the "
                f"registered tier. Only the authoritative recorded-replay "
                f"path can change that.", ""]
        elif row["tier_changed"]:
            promoted = (TIERS.index(row["measured_tier"])
                        > TIERS.index(row["registered_tier"]))
            if promoted:
                # An upward move is the direction in which wishful thinking
                # travels, so it gets the sceptical note. A short window can
                # easily flatter a strategy that a longer one condemns.
                lines += [
                    f"> Measured {row['measured_tier']} is ABOVE the "
                    f"registered {row['registered_tier']}. Do not promote on "
                    f"this alone: check that this run's window and instrument "
                    f"count are not thinner than the evidence behind the "
                    f"registered tier. A promotion needs more data than a "
                    f"demotion, not less.", ""]
            else:
                lines += [
                    f"> Measured {row['measured_tier']} is BELOW the "
                    f"registered {row['registered_tier']}. Evidence of "
                    f"failure is worth acting on at a lower bar than "
                    f"evidence of success: update `agent/registry.py` with "
                    f"this report as the record.", ""]
        lines += ["**Mechanism.** " + row["mechanism"].strip(), "",
                  "**Falsified by.** " + row["falsification"].strip(), ""]

        settings = row.get("settings") or []
        if len(settings) > 1:
            setting_count = row.get("settings_tested", len(settings))
            lines += [
                f"**Pre-registered parameterisations ({setting_count}).** The "
                "tier above is a verdict on the hypothesis, not on one "
                "threshold: rejection requires every setting to fail, and a "
                "tier above T1 requires the best setting to survive a Holm "
                "correction across them.", "",
                "| Setting | Parameters | Trades | Expectancy % | Tier | "
                "p_adj | Claim |",
                "| --- | --- | ---: | ---: | --- | ---: | --- |"]
            for setting in settings:
                params = (", ".join(f"`{k}={v}`" for k, v in
                                    sorted(setting["params"].items()))
                          or "*registered*")
                mark = ("**" if setting["setting_id"] == row.get("best_setting")
                        else "")
                p_adj = setting.get("p_adjusted")
                lines.append(
                    f"| {mark}{setting['setting_id']}{mark} | {params} "
                    f"| {setting.get('trades', 0)} "
                    f"| {_format_signed(setting.get('expectancy_pct'), '%')} "
                    f"| {setting['tier']} "
                    f"| {f'{float(p_adj):.3f}' if p_adj is not None else '-'} "
                    f"| {setting['claim']} |")
            lines += ["",
                      f"Selected setting: `{row.get('best_setting')}`. Gates below "
                      "are the registered parameterisation's.", ""]

        lines += ["| Gate | Result | Detail |", "| --- | --- | --- |"]
        for gate in row["gates"]:
            mark = "pass" if gate["passed"] else "FAIL"
            lines.append(
                f"| `{gate['gate']}` | {mark} | {gate['summary']} |")
        lines.append("")

        fwd = row.get("forward") or {}
        if fwd.get("status"):
            lines += [f"*Forward evidence: {fwd['status']}.*", ""]
        elif fwd:
            agree = fwd.get("signs_agree")
            lines += [
                "**Forward (live shadow).** "
                f"{fwd.get('resolved_trades', 0)} resolved trades of the "
                f"{fwd.get('trades_needed') or '?'} this effect size needs"
                + (f" ({fwd['progress_pct']}%)"
                   if fwd.get("progress_pct") is not None else "")
                + ".",
                "",
                "- forward expectancy: "
                f"{_format_signed(fwd.get('forward_expectancy_pct'), '%')} "
                "vs backtest "
                f"{_format_signed(fwd.get('backtest_expectancy_pct'), '%')}",
                f"- signs agree: "
                f"{'yes' if agree else 'NO' if agree is False else 'unknown'}",
            ]
            if fwd.get("selectivity_pct") is not None:
                lines.append(
                    f"- the contract fired {fwd.get('signals_fired')} times "
                    f"and the account opened "
                    f"{fwd.get('positions_actually_opened')} positions "
                    f"({fwd['selectivity_pct']}%) - the gap is the analyst "
                    f"layer's contribution")
            if fwd.get("warning"):
                lines += ["", f"> {fwd['warning']}"]
            lines.append("")

    lines += [
        "## How to read this", "",
        "No gate here tests a t-statistic. On this data a placebo reached "
        "t = 2.60 on deliberately destroyed information, so `t > 2` is not "
        "evidence - the placebo ratio is.", "",
        "A tier is a claim about evidence, not about promise. This OHLCV "
        "tournament is capped at `T2_CANDIDATE`; `T3_VALIDATED` and "
        "`T4_CONFIRMED` require the authoritative recorded-replay path and "
        "persisted confirmation evidence.", "",
    ]
    path.write_text("\n".join(lines) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--out", type=Path,
                        default=REPO / "research" / "results" / "tournament")
    parser.add_argument(
        "--store", type=Path, default=None,
        help="findings.db path; defaults to research.findings_store")
    parser.add_argument("--cost", default=DEFAULT_COST_SCENARIO,
                        help="cost scenario the gates score against; defaults to the account's real taker rate")
    parser.add_argument("--exit-policy", default="fixed_rr")
    parser.add_argument("--min-bars", type=int, default=8000)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument(
        "--workers", type=int, default=2,
        help="bounded concurrent strategy workers (each setting is scored once)")
    parser.add_argument("--only", default="",
                        help="comma-separated strategy ids to score")
    parser.add_argument("--forward", type=Path,
                        default=REPO / "runtime" / "research"
                        / "forward_evidence.json",
                        help="output of research/export_live.py")
    return parser


def main(argv: list[str] | None = None) -> int:
    cli_args = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(cli_args)
    options = {
        key: str(value) if isinstance(value, Path) else value
        for key, value in vars(args).items()
    }
    command = [sys.executable, str(Path(__file__).resolve()), *cli_args]

    cfg = None
    config_error = None
    try:
        from agent.config import validate_config
        cfg = validate_config(
            yaml.safe_load((REPO / "config.yaml").read_text()))
    except Exception as exc:                       # noqa: BLE001
        config_error = str(exc)
        print(f"note: config.yaml not usable for contract parameters ({exc}); "
              f"falling back to research defaults", file=sys.stderr)
    configured_store = (
        (cfg.get("research") or {}).get("findings_store") if cfg else None)
    store_path = args.store or resolve_store_path(configured_store)

    started_ts = time.time()
    started_utc = datetime.fromtimestamp(
        started_ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    identities = _identity_payload(args)
    content_id = _content_hash({
        "command": command,
        "options": options,
        **identities,
    })
    run_id = _content_hash({
        "content_id": content_id,
        "started_ns": time.time_ns(),
        "nonce": uuid.uuid4().hex,
    })
    run_directory = args.out / "runs" / (
        f"{datetime.fromtimestamp(started_ts, timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}"
        f"-{run_id[:16]}")
    run_directory.mkdir(parents=True, exist_ok=False)
    run = {
        "schema": "tournament-run.v1",
        "run_id": run_id,
        "content_id": content_id,
        "started_ts": started_ts,
        "started_utc": started_utc,
        "run_directory": str(run_directory),
        "command": command,
        "options": options,
        **identities,
    }
    _json_write_once(run_directory / "RUN.json", {
        "schema": run["schema"],
        "run_id": run_id,
        "content_id": content_id,
        "started_ts": started_ts,
        "started_utc": started_utc,
        "run_directory": str(run_directory),
        "status": "STARTED",
    })
    _json_write_once(run_directory / "INVOCATION.json", {
        "command": command, "options": options,
    })
    _json_write_once(run_directory / "INPUTS.json", {
        **identities,
        "config_load_error": config_error,
    })

    store = FindingsStore(store_path)
    start_recorded = False
    if argv is None:
        def interrupted(_signum, _frame):
            raise TournamentFailure("tournament interrupted by SIGTERM", 143)

        signal.signal(signal.SIGTERM, interrupted)
    try:
        store.start_tournament_run(run)
        start_recorded = True

        validate_data_snapshot(args.data)
        frames = load_dataset(args.data, min_bars=args.min_bars)
        if not frames:
            raise TournamentFailure(
                f"No instrument in {args.data} has >= {args.min_bars} bars. "
                "Download more history, or lower --min-bars for a smoke test.",
                exit_code=2)
        membership = universe_membership(frames, top_n=args.top_n)

        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        specs = [s for s in REGISTRY.values() if not wanted or s.id in wanted]
        specs.sort(key=lambda s: (-TIERS.index(s.tier), s.id))

        def score_one(spec):
            print(f"scoring {spec.id}/{spec.version} ...", file=sys.stderr)
            return score_strategy(spec, frames, membership, cfg,
                                  args.cost, args.exit_policy)

        worker_count = max(1, min(int(args.workers), len(specs) or 1))
        if worker_count == 1 or len(specs) < 2:
            rows = [score_one(spec) for spec in specs]
        else:
            # Inputs are read-only and each strategy owns its settings list.
            # Result order stays deterministic even though scoring is bounded.
            with ThreadPoolExecutor(max_workers=worker_count,
                                    thread_name_prefix="tournament") as pool:
                rows = list(pool.map(score_one, specs))

        forward = {}
        if args.forward and args.forward.exists():
            try:
                forward = json.loads(args.forward.read_text())
            except (OSError, ValueError) as exc:               # noqa: BLE001
                print(f"note: could not read forward evidence ({exc})",
                      file=sys.stderr)
        for row in rows:
            apply_forward_evidence(row, forward)

        scored = [r for r in rows if r.get("scored")]
        scored.sort(key=lambda r: (-TIERS.index(r["measured_tier"]),
                                   -(r["headline"].get("expectancy_pct") or 0)))
        unscored = [r for r in rows if not r.get("scored")]
        ordered = scored + unscored

        starts = [int(f.ts[0]) for f in frames.values()]
        ends = [int(f.ts[-1]) for f in frames.values()]

        def stamp(ms: int) -> str:
            return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime(
                "%Y-%m-%d")

        payload = {
            "schema": "tournament-leaderboard.v1",
            "run_id": run_id,
            "content_id": content_id,
            "status": "SUCCEEDED",
            "started_utc": started_utc,
            "generated_utc": datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%SZ"),
            "run_directory": str(run_directory),
            "data_root": str(args.data),
            "instruments": len(frames),
            "min_bars": min(len(f.df) for f in frames.values()),
            "window_start": stamp(min(starts)),
            "window_end": stamp(max(ends)),
            "cost_scenario": args.cost,
            "exit_policy": args.exit_policy,
            "strategies": ordered,
        }
        payload["benchmark_check"] = benchmark_check(ordered)

        alert_on_tier_changes(ordered, payload)

        _json_write_once(run_directory / "leaderboard.json", payload)
        write_report(run_directory / "REPORT.md", payload)
        _json_write_once(run_directory / "ERRORS.json", [])
        ended_ts = time.time()
        store.finish_tournament_run(
            run_id, "SUCCEEDED", ended_ts=ended_ts, strategies=ordered,
            detail={
                "benchmark_check": payload["benchmark_check"],
                "leaderboard_sha256": _sha256_file(
                    run_directory / "leaderboard.json"),
                "report_sha256": _sha256_file(run_directory / "REPORT.md"),
            })
        _json_write_once(run_directory / "COMPLETION.json", {
            "run_id": run_id,
            "status": "SUCCEEDED",
            "ended_ts": ended_ts,
            "ended_utc": datetime.fromtimestamp(
                ended_ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "exit_code": 0 if payload["benchmark_check"]["ok"] else 1,
        })
        _copy_latest(run_directory, args.out)

        check = payload["benchmark_check"]
        print(f"\nharness check: {'PASS' if check['ok'] else 'FAIL'} - "
              f"{check['reason']}")
        for row in ordered:
            if row.get("scored"):
                head = row["headline"]
                print(f"  {row['strategy_id']:<16} "
                      f"{row['measured_tier']:<15} "
                      f"{head.get('trades', 0):>6} trades  "
                      f"{_format_signed(head.get('expectancy_pct'), '%')}  "
                      f"{recommendation(row)}")
            else:
                print(f"  {row['strategy_id']:<16} {'NOT SCORED':<15} "
                      f"{row['reason'][:60]}")
        print(f"\nwrote durable run {run_directory}")
        print(f"latest view: {args.out / 'REPORT.md'}")
        return 0 if check["ok"] else 1
    except (Exception, KeyboardInterrupt) as exc:          # noqa: BLE001
        ended_ts = time.time()
        exit_code = (
            exc.exit_code if isinstance(exc, TournamentFailure)
            else 130 if isinstance(exc, KeyboardInterrupt) else 1)
        error = {
            "type": type(exc).__name__,
            "message": str(exc) or type(exc).__name__,
            "traceback": "".join(traceback.format_exception(exc)),
        }
        failure_payload = {
            "schema": "tournament-leaderboard.v1",
            "run_id": run_id,
            "content_id": content_id,
            "status": "FAILED",
            "started_utc": started_utc,
            "generated_utc": datetime.fromtimestamp(
                ended_ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
            "run_directory": str(run_directory),
            "data_root": str(args.data),
            "strategies": [],
            "errors": [error],
            "benchmark_check": {
                "ok": False,
                "reason": "tournament did not complete",
            },
        }
        leaderboard = run_directory / "leaderboard.json"
        report = run_directory / "REPORT.md"
        errors = run_directory / "ERRORS.json"
        if not leaderboard.exists():
            _json_write_once(leaderboard, failure_payload)
        if not report.exists():
            _text_write_once(report, _failure_report(run, [error]))
        if not errors.exists():
            _json_write_once(errors, [error])
        if start_recorded:
            try:
                store.finish_tournament_run(
                    run_id, "FAILED", ended_ts=ended_ts,
                    detail={"exit_code": exit_code, "errors": [error]})
            except ValueError:
                pass
        completion = run_directory / "COMPLETION.json"
        if not completion.exists():
            _json_write_once(completion, {
                "run_id": run_id,
                "status": "FAILED",
                "ended_ts": ended_ts,
                "ended_utc": datetime.fromtimestamp(
                    ended_ts, timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%S.%fZ"),
                "exit_code": exit_code,
            })
        _copy_latest(run_directory, args.out)
        print(error["message"], file=sys.stderr)
        print(f"failure evidence: {run_directory}", file=sys.stderr)
        return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
