"""Search for historical hypotheses; never grant execution authority.

Usage, from the repository root::

    python -I studies/session_edges/discover.py --alpaca-dir DIR --symbols QQQ,SPY,IWM,DIA \\
        --daily-dir DIR --daily SPY,QQQ,IWM,DIA,XLK,SMH --out runtime/session_edges/discovery \\
        --ledger runtime/session_edges/global-ledger.json --experiment sip-v2 --initialize-ledger

    # public diagnostic data instead of SIP bars
    python -I studies/session_edges/discover.py --index-dir DIR --pairs nsxusd=QQQ,spxusd=SPY \\
        --daily-dir DIR --daily SPY,QQQ --out OUT --ledger GLOBAL_LEDGER --experiment public-v2

The search is free to try every family, parameter and instrument in
``grammar.py``; what it may conclude is fixed here:

1. **Screen on the design period only** (sessions before the holdout date).
   A hypothesis must have a Newey-West t of at least ``MIN_DESIGN_T`` net of
   costs, stay positive at twice the cost, trade often enough, have most of
   its one-step parameter neighbours positive too, and earn more than its
   exposure to the instrument explains (an alpha t of at least
   ``MIN_ALPHA_T`` against the instrument's own daily return), so that a rule
   which is merely long the market most days is not mistaken for an edge.
2. **One champion per family and instrument** goes forward: the screened
   hypothesis with the highest design t. Nothing else touches the holdout.
3. **Evaluate the historical test period** with a correction for every
   champion ever promoted. The tested p is the maximum of the positive-return
   and exposure-adjusted alpha p-values: both claims must survive correction.
   Holm at 5% makes a hypothesis ``historical_supported``; BY at 10% makes it
   ``historical_provisional``. Neither is prospective qualification.
4. **The ledger remembers.** Every promoted hypothesis stays in the count, so
   searching again, or adding proposals, raises the bar instead of lowering it.

All output is nonauthorizing. Exact-rule main-infrastructure qualification,
causal execution replay and independent new forward confirmation remain
required. A daily signal or index proxy never qualifies another execution rule.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse  # noqa: E402
import copy  # noqa: E402
import fcntl  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402
from collections.abc import Mapping, Sequence  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from dataclasses import asdict, is_dataclass  # noqa: E402
from datetime import date, datetime, timezone  # noqa: E402
from statistics import fmean  # noqa: E402
from typing import Any  # noqa: E402

from studies.session_edges import grammar, stats  # noqa: E402
from studies.session_edges.data import (DailyBar, Session, load_alpaca_sessions,  # noqa: E402
                                        load_daily_chart, load_daily_closes, load_index_minute_archives)
from studies.session_edges.run import HOLDOUT_START, RECENT_START, STRESS_MULTIPLE  # noqa: E402
from studies.session_edges.strategies import DayResult  # noqa: E402

SCHEMA = "session-edges-discovery.v2"
LEGACY_SCHEMA = "session-edges-discovery.v1"
LEDGER_SCHEMA = "session-edges-ledger.v2"
BOOK_SCHEMA = "session-edges-book.v2"
# Round-trip cost in bps for a marketable order of modest size. Anything not
# listed is charged the conservative default; replace these with measured
# quote costs once the recorder has them.
COST_BPS = {"SPY": 1.0, "QQQ": 1.0, "IWM": 1.0, "DIA": 1.5, "VTI": 1.5}
DEFAULT_COST_BPS = 3.0
PROXY_SUFFIX = " (index proxy)"

MIN_DESIGN_SESSIONS = 750
MIN_DESIGN_ROUND_TRIPS = 40
MIN_DESIGN_T = 2.0
MIN_NEIGHBOUR_SHARE = 0.70
MIN_ALPHA_T = 2.0            # t of the return left after removing exposure to the instrument itself
MIN_ALPHA_SESSIONS = 60
MIN_HOLDOUT_SESSIONS = 500
FAMILYWISE_ALPHA = 0.05
FALSE_DISCOVERY_RATE = 0.10
CONFIRMED, PROVISIONAL = "historical_supported", "historical_provisional"
UNCONFIRMED, REGISTERED = "unsupported", "registered_diagnostic"

# Edge book: how much of the account each sleeve may use and how many legs it may hold.
SLEEVE_BUDGET = {"intraday": 0.50, "swing": 0.50}
MAX_LEGS_PER_SLEEVE = {"intraday": 1, "swing": 4}
MAX_LEG_WEIGHT = 0.25
# A leveraged fund repeats its index's edge with more risk per dollar, so it
# is recorded and not traded, and one traded leg stands for each underlying.
UNDERLYING = {"SSO": "SPY", "UPRO": "SPY", "SPXL": "SPY", "IVV": "SPY", "VOO": "SPY", "QLD": "QQQ",
              "TQQQ": "QQQ", "QQQM": "QQQ", "SOXL": "SMH", "TNA": "IWM", "UWM": "IWM"}
LEVERAGED = frozenset({"SSO", "UPRO", "SPXL", "QLD", "TQQQ", "SOXL", "TNA", "UWM"})


def base_symbol(label: str) -> str:
    return label[:-len(PROXY_SUFFIX)] if label.endswith(PROXY_SUFFIX) else label


def cost_for(label: str, overrides: Mapping[str, float] | None = None) -> float:
    symbol = base_symbol(label)
    value = float(overrides[symbol]) if overrides and symbol in overrides else COST_BPS.get(symbol, DEFAULT_COST_BPS)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"invalid round-trip cost for {symbol}: {value}")
    return value


def benchmark_returns(kind: str, data: Sequence[Any]) -> dict[date, float]:
    """What simply holding the instrument earned on each session, close to close."""
    out: dict[date, float] = {}
    previous = 0.0
    for item in data:
        close = item.close[-1] if kind == grammar.MINUTE else item.close
        if previous > 0:
            out[item.day] = close / previous - 1.0
        previous = close
    return out


def beyond_holding(days: Sequence[date], net: Sequence[float],
                   benchmark: Mapping[date, float]) -> dict[str, float | None]:
    """Regress a net daily series on the instrument's own return: its beta and the alpha left over.

    The HAC intercept error includes uncertainty in the fitted beta. This
    single-factor diagnostic is not evidence of causality or proof of skill.
    """
    pairs = [(value, benchmark[day]) for day, value in zip(days, net) if day in benchmark]
    if len(pairs) < MIN_ALPHA_SESSIONS:
        return {"beta": None, "alpha_bps_per_session": None, "alpha_t": None, "alpha_p_one_sided": 1.0}
    return stats.regression_alpha([pair[0] for pair in pairs], [pair[1] for pair in pairs])


def window_stats(results: Sequence[DayResult], cost_bps: float, *, start: date | None = None,
                 end: date | None = None, benchmark: Mapping[date, float] | None = None) -> dict[str, Any]:
    """Net statistics of the sessions in ``[start, end)`` at ``cost_bps`` and at the stressed cost.

    With ``benchmark`` (the instrument's own daily returns) the statistics
    also say how much of the result is left once that exposure is removed.
    """
    chosen = [result for result in results
              if (start is None or result.day >= start) and (end is None or result.day < end)]
    net = stats.net_series(chosen, cost_bps)
    stressed = stats.net_series(chosen, cost_bps * STRESS_MULTIPLE)
    trips = sum(result.round_trips for result in chosen)
    t_value = stats.newey_west_t(net)
    out = {"sessions": len(chosen), "round_trips": trips,
           "net_bps_per_session": fmean(net) * 1e4 if net else None,
           "stressed_bps_per_session": fmean(stressed) * 1e4 if stressed else None,
           "t": t_value, "sharpe": stats.annualised_sharpe(net),
           "p_one_sided": stats.one_sided_p(t_value)}
    if benchmark is not None:
        out.update(beyond_holding([result.day for result in chosen], net, benchmark))
    out["qualification_p_one_sided"] = max(out["p_one_sided"], out.get("alpha_p_one_sided", 1.0))
    return out


def passes_screen(design: Mapping[str, Any], neighbour_share: float | None) -> tuple[bool, list[str]]:
    """The design-period screen; returns the verdict and the reasons it failed."""
    failed: list[str] = []
    if design["sessions"] < MIN_DESIGN_SESSIONS:
        failed.append("too few design sessions")
    if design["round_trips"] < MIN_DESIGN_ROUND_TRIPS:
        failed.append("too few design round trips")
    if design["t"] is None or design["t"] < MIN_DESIGN_T:
        failed.append(f"design t below {MIN_DESIGN_T}")
    if design["stressed_bps_per_session"] is None or design["stressed_bps_per_session"] <= 0:
        failed.append("not positive at the stressed cost")
    if neighbour_share is not None and neighbour_share < MIN_NEIGHBOUR_SHARE:
        failed.append("parameter neighbours are not positive")
    if "alpha_t" in design and (design["alpha_t"] is None or design["alpha_t"] < MIN_ALPHA_T):
        failed.append("no return beyond holding the instrument")
    return not failed, failed


def holm(p_values: Mapping[str, float], alpha: float = FAMILYWISE_ALPHA) -> set[str]:
    """Hypotheses rejected by Holm's step-down procedure (family-wise error ``alpha``)."""
    _validate_p_values(p_values)
    ordered = sorted(p_values.items(), key=lambda item: item[1])
    passed: set[str] = set()
    for rank, (key, p_value) in enumerate(ordered):
        if p_value > alpha / (len(ordered) - rank):
            break
        passed.add(key)
    return passed


def benjamini_yekutieli(p_values: Mapping[str, float], rate: float = FALSE_DISCOVERY_RATE) -> set[str]:
    """Hypotheses rejected at false discovery rate ``rate`` under arbitrary dependence."""
    _validate_p_values(p_values)
    ordered = sorted(p_values.items(), key=lambda item: item[1])
    count = len(ordered)
    if not count:
        return set()
    harmonic = math.fsum(1.0 / index for index in range(1, count + 1))
    cutoff = 0
    for rank, (_, p_value) in enumerate(ordered, start=1):
        if p_value <= rank / (count * harmonic) * rate:
            cutoff = rank
    return {key for key, _ in ordered[:cutoff]}


def _validate_p_values(p_values: Mapping[str, float]) -> None:
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or not 0 <= value <= 1 for value in p_values.values()):
        raise ValueError("p-values must be finite numbers in [0, 1]")


def historical_corrections(promoted: Mapping[str, Mapping[str, Any]], *, prior_trials: int = 0
                           ) -> tuple[dict[str, float], set[str], set[str]]:
    """Correct the intersection of positive net return and positive alpha.

    Missing/underpowered evidence, including historical migrated trials, is
    retained at p=1 rather than removed from the multiple-testing denominator.
    These are fixed historical tests, not an adaptive/online-FDR guarantee.
    """
    p_values: dict[str, float] = {}
    for key, row in promoted.items():
        holdout = row.get("holdout") or {}
        if holdout.get("sessions", 0) < MIN_HOLDOUT_SESSIONS:
            p_values[key] = 1.0
            continue
        p_values[key] = max(stats.one_sided_p(holdout.get("t")),
                            stats.one_sided_p(holdout.get("alpha_t")))
    p_values.update({f"historical-trial:{index}": 1.0 for index in range(prior_trials)})
    return p_values, holm(p_values), benjamini_yekutieli(p_values)


class LedgerError(ValueError):
    """The global research history or experiment identity is incompatible."""


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _canonical(value: Any) -> str:
    return json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _seal_ledger(ledger: dict[str, Any]) -> None:
    ledger["integrity_digest"] = _digest({key: value for key, value in ledger.items() if key != "integrity_digest"})


def _validate_ledger(ledger: Mapping[str, Any]) -> None:
    if ledger.get("schema") != LEDGER_SCHEMA:
        raise LedgerError("incompatible ledger schema; explicitly migrate to a new v2 ledger; never overwrite history")
    if ledger.get("holdout_start") != HOLDOUT_START.isoformat():
        raise LedgerError("ledger historical split differs from this protocol")
    for name in ("experiments", "promoted", "historical"):
        if not isinstance(ledger.get(name), Mapping):
            raise LedgerError(f"ledger {name} must be an object")
    if ledger.get("integrity_digest") != _digest({key: value for key, value in ledger.items()
                                                  if key != "integrity_digest"}):
        raise LedgerError("ledger integrity mismatch; preserve the file and restore verified history")
    for row in ledger["promoted"].values():
        experiment = ledger["experiments"].get(row.get("experiment_name"))
        if not experiment or row.get("experiment_id") != experiment.get("id"):
            raise LedgerError("promoted evidence has an incompatible experiment identity")
        if row.get("evidence_digest") != _evidence_digest(row):
            raise LedgerError("promoted evidence digest mismatch")


def new_ledger() -> dict[str, Any]:
    ledger = {"schema": LEDGER_SCHEMA, "holdout_start": HOLDOUT_START.isoformat(),
              "authorizing": False, "experiments": {}, "promoted": {}, "historical": {}}
    _seal_ledger(ledger)
    return ledger


def load_ledger(path: Path | None, *, create: bool = False) -> dict[str, Any]:
    """Read verified history. Missing/incompatible persisted files never reset it.

    ``None`` creates an explicitly ephemeral ledger for offline API callers;
    the CLI requires a global path and explicit initialization of a new file.
    """
    if path is None:
        return new_ledger()
    path = Path(path)
    if not path.is_file():
        if create:
            return new_ledger()
        raise LedgerError(f"{path}: ledger missing; use --initialize-ledger only for a new shared history")
    if create:
        raise LedgerError(f"{path}: refusing to initialize over an existing ledger")
    ledger = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(ledger, Mapping):
        raise LedgerError("ledger must be a JSON object")
    _validate_ledger(ledger)
    return ledger


def migrate_legacy_ledger(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Retain v1 evidence verbatim as diagnostics, charging every prior trial p=1.

    Migration makes no positive qualification claim and never modifies the
    source artifact. New causal simulations must use a new experiment.
    """
    if payload.get("schema") != LEGACY_SCHEMA or not isinstance(payload.get("promoted"), Mapping):
        raise LedgerError("migration requires a v1 discovery/ledger artifact with promoted history")
    ledger = new_ledger()
    artifact = copy.deepcopy(dict(payload))
    source_digest = _digest(artifact)
    ledger["historical"][source_digest] = {"source_digest": source_digest, "authorizing": False,
        "status": "historical_diagnostic_only", "trials": len(artifact["promoted"]), "artifact": artifact}
    _seal_ledger(ledger)
    return ledger


def experiment_manifest(datasets: Mapping[tuple[str, str], Sequence[Any]], *,
                        context: grammar.Context, costs: Mapping[str, float] | None,
                        source_provenance: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Bind normalized observations, source declarations, code and protocol.

    Dataset hashing streams one session/bar at a time, rather than copying
    the full minute corpus into a second serialized in-memory object.
    """
    inputs: dict[str, str] = {}
    for (kind, symbol), rows in sorted(datasets.items()):
        digest = hashlib.sha256()
        for row in rows:
            digest.update(_canonical(row).encode("utf-8"))
            digest.update(b"\n")
        inputs[f"{kind}:{symbol}"] = digest.hexdigest()
    root = Path(__file__).resolve().parents[2]
    modules = ("data.py", "strategies.py", "grammar.py", "stats.py", "discover.py", "run.py")
    code = {name: hashlib.sha256((root / "studies/session_edges" / name).read_bytes()).hexdigest()
            for name in modules}
    code["liquid_reversion/walkforward.py"] = hashlib.sha256(
        (root / "studies/liquid_reversion/walkforward.py").read_bytes()).hexdigest()
    policy = {"holdout_start": HOLDOUT_START, "recent_start": RECENT_START,
              "min_design_sessions": MIN_DESIGN_SESSIONS, "min_design_trips": MIN_DESIGN_ROUND_TRIPS,
              "min_design_t": MIN_DESIGN_T, "min_neighbour_share": MIN_NEIGHBOUR_SHARE,
              "min_alpha_t": MIN_ALPHA_T, "min_alpha_sessions": MIN_ALPHA_SESSIONS,
              "min_holdout_sessions": MIN_HOLDOUT_SESSIONS, "stress_multiple": STRESS_MULTIPLE,
              "familywise_alpha": FAMILYWISE_ALPHA, "fdr": FALSE_DISCOVERY_RATE,
              "correction": "max_positive_return_and_hac_alpha_p", "grammar": grammar.describe(),
              "simulation_version": getattr(grammar, "SIMULATION_VERSION", "unversioned")}
    effective_costs = {label: cost_for(label, costs) for _, label in datasets}
    return {"schema": "session-edges-experiment.v2", "input_digests": inputs,
            "source_provenance": dict(source_provenance or {"mode": "unspecified_diagnostic"}),
            "dataset_digest": _digest(inputs), "context_digest": _digest(context),
            "code_digest": _digest(code), "code_files": code,
            "cost_digest": _digest(effective_costs), "cost_bps": effective_costs,
            "protocol_digest": _digest(policy)}


def _bind_experiment(ledger: dict[str, Any], name: str, manifest: Mapping[str, Any], *,
                     allow_new: bool) -> str:
    if not name or len(name) > 160:
        raise LedgerError("a stable experiment name is required")
    identity = _digest(manifest)
    existing = ledger["experiments"].get(name)
    if existing and (existing.get("id") != identity or existing.get("manifest") != manifest):
        raise LedgerError(f"experiment {name!r} changed data, source, code, costs or protocol; "
                          "use an explicit new experiment name and retain the shared ledger")
    if not existing:
        if ledger["experiments"] and not allow_new:
            raise LedgerError("adding an experiment requires --new-experiment; prior trials remain counted")
        ledger["experiments"][name] = {"id": identity, "manifest": dict(manifest)}
    return identity


def _evidence_digest(row: Mapping[str, Any]) -> str:
    immutable = ("spec", "source", "cost_bps", "design", "neighbour_share", "holdout", "recent",
                 "data_last_session", "promoted_at", "experiment_name", "experiment_id")
    return _digest({name: row.get(name) for name in immutable})


@contextmanager
def ledger_lock(path: Path):
    """Serialize read/search/write across processes sharing the global ledger."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_ledger(path: Path, ledger: Mapping[str, Any]) -> None:
    """Atomically persist exact evidence without rounding hashed floats."""
    _validate_ledger(ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(ledger, indent=1, sort_keys=True, allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def discover(*, minute: Mapping[str, Sequence[Session]], daily: Mapping[str, Sequence[DailyBar]],
             proposals: Sequence[Mapping[str, Any]] = (), ledger: dict[str, Any] | None = None,
             costs: Mapping[str, float] | None = None, families: Sequence[str] | None = None,
             context: grammar.Context | None = None, progress: Any = None,
             experiment_name: str = "offline-diagnostic", allow_new_experiment: bool = False,
             source_provenance: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Run the whole protocol on loaded data and return the discovery record.

    ``context`` holds the named daily series some families read, such as
    ``{"VIX": {date: close}}``. A family whose series is missing, or which
    needs traded volume on data that has none, is skipped and reported.
    """
    supplied_ledger = ledger
    ledger = copy.deepcopy(ledger) if ledger is not None else new_ledger()
    _validate_ledger(ledger)
    context = context or {}
    datasets: dict[tuple[str, str], Sequence[Any]] = {}
    from studies.session_edges.data import require_complete_sessions
    for label, sessions in minute.items():
        require_complete_sessions(sessions, label=label)
        datasets[(grammar.MINUTE, label)] = sessions
    for symbol, bars in daily.items():
        datasets[(grammar.DAILY, symbol)] = bars
    manifest = experiment_manifest(datasets, context=context, costs=costs, source_provenance=source_provenance)
    experiment_id = _bind_experiment(ledger, experiment_name, manifest, allow_new=allow_new_experiment)
    benchmarks = {slot: benchmark_returns(slot[0], data) for slot, data in datasets.items()}
    wanted = set(families) if families else set(grammar.FAMILIES)
    skipped: dict[str, str] = {}

    def usable(family: grammar.Family, slot: tuple[str, str]) -> bool:
        missing = [name for name in family.needs if name not in context]
        if missing:
            skipped[family.name] = f"context series not loaded: {', '.join(missing)}"
            return False
        if family.volume and not _has_volume(datasets[slot]):
            skipped.setdefault(family.name, "needs traded volume")
            return False
        return True

    # ---- stage 1: every hypothesis, design period only ------------------
    candidates: dict[str, dict[str, Any]] = {}
    rejected_proposals: list[dict[str, Any]] = []

    def evaluate(spec: Mapping[str, Any], source: str) -> dict[str, Any] | None:
        proposed = dict(spec)
        attribution = proposed.pop("proposal_attribution", None)
        canonical = grammar.normalise(proposed)
        family = grammar.FAMILIES[canonical["family"]]
        slot = (family.data, canonical["symbol"])
        if slot not in datasets or not usable(family, slot):
            return None
        key = grammar.spec_id(canonical)
        if key not in candidates:
            results = grammar.simulator(canonical, context)(datasets[slot])
            cost = cost_for(canonical["symbol"], costs)
            candidates[key] = {"id": key, "spec": canonical, "source": source, "cost_bps": cost,
                               "proposal_attributions": [],
                               "design": window_stats(results, cost, end=HOLDOUT_START,
                                                      benchmark=benchmarks[slot])}
        if source == "proposal":
            claim = dict(attribution) if isinstance(attribution, Mapping) else {"status": "unattributed"}
            if claim not in candidates[key]["proposal_attributions"]:
                candidates[key]["proposal_attributions"].append(claim)
        return candidates[key]

    for (kind, label) in datasets:
        for family in grammar.FAMILIES.values():
            if family.data != kind or family.name not in wanted or not usable(family, (kind, label)):
                continue
            for spec in grammar.enumerate_family(family, label):
                evaluate(spec, "grid")
            if progress:
                progress(f"design {label} {family.name}")
    for proposal in proposals:
        try:
            if evaluate(proposal, "proposal") is None:
                rejected_proposals.append({"spec": dict(proposal),
                                           "reason": "no usable data for that family and symbol"})
        except grammar.SpecError as error:
            rejected_proposals.append({"spec": dict(proposal), "reason": str(error)})

    for candidate in list(candidates.values()):
        means = []
        for neighbour in grammar.neighbours(candidate["spec"]):
            other = evaluate(neighbour, "neighbour")
            if other is not None and other["design"]["net_bps_per_session"] is not None:
                means.append(other["design"]["net_bps_per_session"])
        share = sum(1 for value in means if value > 0) / len(means) if means else None
        candidate["neighbour_share"] = share
        candidate["screened"], candidate["screen_failures"] = passes_screen(candidate["design"], share)
    for candidate in candidates.values():
        if "screened" not in candidate:      # reached only as another hypothesis's neighbour
            candidate.update({"neighbour_share": None, "screened": False,
                              "screen_failures": ["evaluated as a neighbour only"]})

    # ---- stage 2: one champion per family and instrument ----------------
    champions: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in candidates.values():
        if not candidate["screened"]:
            continue
        slot = (candidate["spec"]["family"], candidate["spec"]["symbol"])
        best = champions.get(slot)
        if best is None or candidate["design"]["t"] > best["design"]["t"]:
            champions[slot] = candidate

    # ---- stage 3: holdout, once per champion, remembered in the ledger --
    promoted: dict[str, Any] = ledger["promoted"]
    for candidate in champions.values():
        evidence_id = f"{experiment_id}:{candidate['id']}"
        if evidence_id in promoted:
            for claim in candidate["proposal_attributions"]:
                if claim not in promoted[evidence_id]["proposal_attributions"]:
                    promoted[evidence_id]["proposal_attributions"].append(claim)
            continue
        family = grammar.FAMILIES[candidate["spec"]["family"]]
        slot = (family.data, candidate["spec"]["symbol"])
        results = grammar.simulator(candidate["spec"], context)(datasets[slot])
        promoted[evidence_id] = {
            "spec": candidate["spec"], "source": candidate["source"], "cost_bps": candidate["cost_bps"],
            "experiment_name": experiment_name, "experiment_id": experiment_id,
            "proposal_attributions": candidate["proposal_attributions"],
            "design": candidate["design"], "neighbour_share": candidate["neighbour_share"],
            "holdout": window_stats(results, candidate["cost_bps"], start=HOLDOUT_START,
                                    benchmark=benchmarks[slot]),
            "recent": window_stats(results, candidate["cost_bps"], start=RECENT_START,
                                   benchmark=benchmarks[slot]),
            "data_last_session": results[-1].day.isoformat() if results else None,
            "promoted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        promoted[evidence_id]["evidence_digest"] = _evidence_digest(promoted[evidence_id])
        if progress:
            progress(f"holdout {candidate['spec']['symbol']} {candidate['spec']['family']}")

    # Evidence is immutable. A changed causal kernel or source must start an
    # explicitly new experiment; old rows are never enriched or upgraded.
    prior_trials = sum(row["trials"] for row in ledger["historical"].values())
    testable, family_wise, discoveries = historical_corrections(promoted, prior_trials=prior_trials)
    for key, row in promoted.items():
        holdout, recent = row["holdout"], row["recent"]
        survives_cost = (holdout["stressed_bps_per_session"] or 0) > 0
        recent_positive = (recent["net_bps_per_session"] or 0) > 0
        beyond = (holdout.get("alpha_t") or 0) >= MIN_ALPHA_T
        some_alpha = (holdout.get("alpha_bps_per_session") or 0) > 0
        if key in family_wise and survives_cost and recent_positive and beyond:
            row["tier"] = CONFIRMED
        elif key in discoveries and survives_cost and some_alpha:
            row["tier"] = PROVISIONAL
        else:
            row["tier"] = UNCONFIRMED
        row["authorizing"] = False
        row["requires_main_qualification"] = True
        row["checks"] = {"holm": key in family_wise, "false_discovery": key in discoveries,
                         "corrected_hypothesis": "positive_net_return_and_exposure_adjusted_alpha",
                         "positive_at_stressed_cost": survives_cost, "positive_recently": recent_positive,
                         "holdout_long_enough": holdout["sessions"] >= MIN_HOLDOUT_SESSIONS,
                         "beyond_holding_the_instrument": beyond}

    funnel: dict[str, dict[str, int]] = {}
    for candidate in candidates.values():
        row = funnel.setdefault(candidate["spec"]["family"], {"tested": 0, "screened": 0, "champions": 0})
        row["tested"] += 1
        row["screened"] += 1 if candidate["screened"] else 0
    for (family_name, _) in champions:
        funnel[family_name]["champions"] += 1
    _seal_ledger(ledger)
    _validate_ledger(ledger)
    if supplied_ledger is not None:
        supplied_ledger.clear()
        supplied_ledger.update(ledger)
    current = {key: row for key, row in promoted.items() if row["experiment_id"] == experiment_id}
    return {"schema": SCHEMA, "authorizing": False, "requires_main_qualification": True,
            "experiment_name": experiment_name, "experiment_id": experiment_id, "experiment": manifest,
            "experiments": copy.deepcopy(ledger["experiments"]),
            "holdout_start": HOLDOUT_START.isoformat(),
            "recent_start": RECENT_START.isoformat(),
            "thresholds": {"min_design_t": MIN_DESIGN_T, "min_neighbour_share": MIN_NEIGHBOUR_SHARE,
                           "min_alpha_t": MIN_ALPHA_T,
                           "stress_multiple": STRESS_MULTIPLE, "familywise_alpha": FAMILYWISE_ALPHA,
                           "false_discovery_rate": FALSE_DISCOVERY_RATE},
            "datasets": {f"{kind}:{label}": len(data) for (kind, label), data in datasets.items()},
            "hypotheses_tested": len(candidates), "champions_this_run": len(champions),
            "promoted_ever": len(promoted) + prior_trials, "historical_trials": prior_trials,
            "global_correction": {"trials": len(testable), "experiments": len(ledger["experiments"]),
                                  "hypothesis": "positive_net_return_and_exposure_adjusted_alpha",
                                  "method": "fixed_historical_holm_and_by_not_online_fdr"},
            "funnel": funnel, "promoted": current,
            "context_series": sorted(context), "skipped_families": skipped,
            "rejected_proposals": rejected_proposals,
            "design_leaderboard": design_leaderboard(candidates)}


def _has_volume(data: Sequence[Any]) -> bool:
    """Whether minute data carries traded volume (an index proxy does not)."""
    sample = list(data[:20]) + list(data[-20:])
    return any(math.fsum(session.volume) > 0 for session in sample)


def design_leaderboard(candidates: Mapping[str, Mapping[str, Any]], limit: int = 40) -> list[dict[str, Any]]:
    """The best hypotheses by design-period t. Holdout numbers are never included."""
    ranked = sorted((row for row in candidates.values() if row["design"]["t"] is not None),
                    key=lambda row: row["design"]["t"], reverse=True)[:limit]
    return [{"spec": row["spec"], "design_t": row["design"]["t"],
             "design_net_bps_per_session": row["design"]["net_bps_per_session"],
             "screened": row["screened"], "screen_failures": row["screen_failures"]} for row in ranked]


def _sleeve(runner_rule: str) -> str:
    return "swing" if runner_rule == "close_weakness_reversion" else "intraday"


def _runner_params(family: grammar.Family, params: Mapping[str, Any]) -> dict[str, Any]:
    """Parameters in the form the forward runner's rule takes."""
    if family.runner_rule == "close_weakness_reversion":
        rule = grammar.close_reversion_rules(params)
        return {"decision_minute": rule.decision_minute, "enter_below": rule.enter_below,
                "exit_above": rule.exit_above, "max_sessions": rule.max_sessions}
    rule = grammar.midday_trend_rules(params) if family.name == "midday_trend" else grammar.noise_band_rules(params)
    return {"lookback_sessions": rule.lookback_sessions, "decision_step_minutes": rule.decision_step_minutes,
            "first_decision_minute": rule.first_decision_minute,
            "last_decision_minute": rule.last_decision_minute, "band_multiple": rule.band_multiple,
            "sides": list(rule.sides)}


def build_book(record: Mapping[str, Any], registered: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Export diagnostic candidates and optional exact-rule shadow legs.

    There is no study-to-order promotion path. Daily hypotheses remain daily
    hypotheses; index proxies remain proxy evidence. Main's qualification
    infrastructure must establish authority for the exact executable rule.
    """
    if record.get("schema") != SCHEMA or record.get("authorizing") is not False:
        raise LedgerError("only v2 nonauthorizing discovery records can create a diagnostic book")
    legs: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for key, row in record["promoted"].items():
        family = grammar.FAMILIES.get(row["spec"]["family"])
        if family is None:
            continue
        candidates.append({"id": key, "spec": row["spec"], "tier": row["tier"],
                           "experiment_id": row["experiment_id"], "evidence_digest": row["evidence_digest"],
                           "authorizing": False, "requires_main_qualification": True,
                           "data_kind": family.data, "proxy": row["spec"]["symbol"].endswith(PROXY_SUFFIX)})
        # A daily spec is not the runner's 15:50 spec, even if a future grammar
        # accidentally gives them the same runner alias.
        if family.data != grammar.MINUTE or family.runner_rule is None or row["tier"] == UNCONFIRMED:
            continue
        proxy = row["spec"]["symbol"].endswith(PROXY_SUFFIX)
        legs.append({"id": key, "rule": family.runner_rule, "symbol": base_symbol(row["spec"]["symbol"]),
                     "params": _runner_params(family, row["spec"]["params"]), "tier": row["tier"],
                     "family": family.name, "evidence": row["spec"]["symbol"],
                     "experiment_id": row["experiment_id"], "evidence_digest": row["evidence_digest"],
                     "holdout_t": row["holdout"]["t"], "holdout_sharpe": row["holdout"]["sharpe"],
                     "holdout_alpha_t": row["holdout"].get("alpha_t"),
                     "holdout_beta": row["holdout"].get("beta"),
                     "note": "index proxy: diagnostic only; main qualification required" if proxy
                             else "exact-rule forward evidence and main qualification required"})
    if registered and registered.get("schema") not in ("session-edges-study.v1", "session-edges-study.v2"):
        raise LedgerError("registered study schema is incompatible")
    for label, verdicts in ((registered or {}).get("minute") or {}).items():
        for name, block in verdicts.items():
            family = grammar.FAMILIES.get(name)
            if (family is None or family.data != grammar.MINUTE or family.runner_rule is None
                    or block.get("verdict") not in ("historical_support", "paper_candidate")):
                continue
            holdout = (block.get("evidence") or {}).get("holdout") or {}
            legacy = registered.get("schema") == "session-edges-study.v1"
            legs.append({"id": f"registered:{name}:{base_symbol(label)}", "rule": family.runner_rule,
                         "symbol": base_symbol(label), "params": _runner_params(family, family.defaults()),
                         "tier": REGISTERED, "family": name, "evidence": label,
                         "holdout_t": holdout.get("t_newey_west"), "holdout_sharpe": holdout.get("sharpe"),
                         "note": ("legacy study evidence: not requalified; " if legacy else "registered study: ")
                                 + "diagnostic only; global alpha correction and main qualification required"})
    seen: set[tuple[str, str, str]] = set()
    book: list[dict[str, Any]] = []
    for leg in legs:
        slot = (leg["rule"], leg["symbol"], _canonical(leg["params"]))
        if slot in seen:
            continue
        seen.add(slot)
        book.append({**leg, "sleeve": _sleeve(leg["rule"]), "trade": False, "weight": 0.0,
                     "authorizing": False, "requires_main_qualification": True})
    return {"schema": BOOK_SCHEMA, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "authorizing": False, "requires_main_qualification": True,
            "experiment_id": record["experiment_id"],
            "holdout_start": record["holdout_start"], "promoted_ever": record["promoted_ever"],
            "candidates": candidates, "legs": book}


def _cell(value: Any, places: int = 2) -> str:
    return "n/a" if not isinstance(value, (int, float)) else f"{value:.{places}f}"


def render_report(record: Mapping[str, Any], book: Mapping[str, Any]) -> str:
    lines = ["# Historical edge research", "",
             "**Nonauthorizing research. No output from this study permits an order.** Exact-rule main "
             "qualification and independent new forward evidence are required. The fixed historical "
             "split does not establish that a published rule, model, or later research never saw these dates.", "",
             f"{record['hypotheses_tested']} hypotheses tested on the design period (before "
             f"{record['holdout_start']}); {record['promoted_ever']} champions have ever been taken to the "
             f"historical test period and all remain in the global correction. The corrected p-value is "
             f"the larger of the positive-net-return and HAC alpha p-values. Modeled costs are per round "
             f"trip, per instrument; mean net return must remain positive at {STRESS_MULTIPLE:g}x cost.", "",
             f"Experiment: `{record['experiment_name']}` (`{record['experiment_id']}`).", "",
             "## Funnel", "", "| family | tested | passed the design screen | champions |", "|---|---|---|---|"]
    for name, row in sorted(record["funnel"].items()):
        lines.append(f"| {name} | {row['tested']} | {row['screened']} | {row['champions']} |")
    lines += ["", "## Historical test results of this experiment", "",
              "| tier | family | instrument | parameters | design t | holdout t | holdout Sharpe | "
              "holdout bps/session | holdout beta | holdout alpha t | since recent start bps/session |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    order = {CONFIRMED: 0, PROVISIONAL: 1, UNCONFIRMED: 2}
    for row in sorted(record["promoted"].values(),
                      key=lambda item: (order[item["tier"]], -(item["holdout"]["t"] or -99))):
        spec = row["spec"]
        params = ", ".join(f"{key}={value}" for key, value in spec["params"].items())
        lines.append(f"| {row['tier']} | {spec['family']} | {spec['symbol']} | {params} | "
                     f"{_cell(row['design']['t'])} | {_cell(row['holdout']['t'])} | "
                     f"{_cell(row['holdout']['sharpe'])} | {_cell(row['holdout']['net_bps_per_session'])} | "
                     f"{_cell(row['holdout'].get('beta'))} | {_cell(row['holdout'].get('alpha_t'))} | "
                     f"{_cell(row['recent']['net_bps_per_session'])} |")
    lines += ["", "## Diagnostic shadow book", "",
              "| trade | weight | tier | rule | symbol | parameters | evidence | note |",
              "|---|---|---|---|---|---|---|---|"]
    for leg in book["legs"]:
        params = ", ".join(f"{key}={value}" for key, value in leg["params"].items())
        lines.append(f"| {'yes' if leg['trade'] else 'record only'} | {leg['weight']:.1%} | {leg['tier']} | "
                     f"{leg['rule']} | {leg['symbol']} | {params} | {leg['family']} on {leg['evidence']} | "
                     f"{leg['note']} |")
    lines += ["", "`historical_supported` passed Holm at 5% for both positive return and alpha; "
              "`historical_provisional` passed BY at 10%. Cost stress and the stated recent-period "
              "checks also apply. `registered_diagnostic` preserves a fixed-study candidate without "
              "claiming it passed the discovery correction. Every tier is diagnostic and every leg "
              "has `trade=false`. Daily and proxy evidence cannot authorize another execution rule.", "",
              "Alpha removes a fitted linear exposure to the instrument's own daily return, with "
              "a HAC intercept error including beta estimation. It is a single-factor historical "
              "diagnostic, not proof of skill, independence or future profitability.", ""]
    if record.get("skipped_families"):
        lines += ["## Families not searched in this run", ""]
        lines += [f"* `{name}`: {reason}" for name, reason in sorted(record["skipped_families"].items())]
        lines.append("")
    return "\n".join(lines)


def _round(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _round(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round(item) for item in value]
    if isinstance(value, float):
        return round(value, 6)
    return value


def _pairs(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in text.split(","):
        if item.strip():
            key, _, label = item.partition("=")
            out[key.strip()] = (label or key).strip()
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--alpaca-dir", help="SIP 1Min month files (liquid_reversion.fetch_sip)")
    parser.add_argument("--symbols", default="QQQ,SPY", help="symbols to read from --alpaca-dir")
    parser.add_argument("--calendar", help="calendar.json written by the SIP fetcher")
    parser.add_argument("--exchange-calendar", help="full ex-ante trading calendar for daily month-end signals")
    parser.add_argument("--index-dir", help="public index one-minute archives")
    parser.add_argument("--pairs", default="nsxusd=QQQ,spxusd=SPY")
    parser.add_argument("--daily-dir", help="adjusted daily chart files (fetch_public)")
    parser.add_argument("--daily", default="SPY,QQQ,IWM,DIA")
    parser.add_argument("--context-dir", help="daily files of context series; defaults to --daily-dir")
    parser.add_argument("--context", default="",
                        help="context series as NAME=FILE_STEM, e.g. VIX=VIX,VIX3M=VIX3M (fetch_public --context)")
    parser.add_argument("--proposals", help="attributed proposal envelope or legacy JSON list")
    parser.add_argument("--registered", help="results.json of run.py, included as diagnostics only")
    parser.add_argument("--ledger", required=True, help="shared global research ledger; independent of output directory")
    parser.add_argument("--experiment", required=True, help="stable name binding source data, code, costs and protocol")
    parser.add_argument("--initialize-ledger", action="store_true", help="explicitly initialize a new global history")
    parser.add_argument("--new-experiment", action="store_true", help="explicitly add an experiment, retaining all prior trials")
    parser.add_argument("--migrate-ledger", help="preserve v1 source as diagnostics in a newly initialized v2 ledger")
    parser.add_argument("--families", help="comma-separated family names to search; default all")
    parser.add_argument("--cost-bps", default="", help="per-symbol round-trip costs, e.g. XLK=2,SMH=2.5")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if args.migrate_ledger and not args.initialize_ledger:
        parser.error("--migrate-ledger requires --initialize-ledger and a separate new --ledger path")
    minute: dict[str, list[Session]] = {}
    source_files: set[Path] = set()
    if args.alpaca_dir:
        for symbol in [item.strip().upper() for item in args.symbols.split(",") if item.strip()]:
            minute[symbol] = load_alpaca_sessions(args.alpaca_dir, symbol, calendar=args.calendar)
        source_files.update(path for path in Path(args.alpaca_dir).rglob("*") if path.is_file())
        if args.calendar:
            source_files.add(Path(args.calendar))
    if args.index_dir:
        for pair, label in _pairs(args.pairs).items():
            minute[f"{label}{PROXY_SUFFIX}"] = load_index_minute_archives(args.index_dir, pair)
            source_files.update(Path(args.index_dir).glob(f"{pair}_*.zip"))
    daily: dict[str, list[DailyBar]] = {}
    if args.daily_dir:
        for symbol in [item.strip().upper() for item in args.daily.split(",") if item.strip()]:
            for suffix in (".json.gz", ".json"):
                path = Path(args.daily_dir) / f"{symbol}{suffix}"
                if path.is_file():
                    daily[symbol] = load_daily_chart(path)
                    source_files.add(path)
                    break
            else:
                raise FileNotFoundError(f"no daily chart file for {symbol} in {args.daily_dir}")
    context: dict[str, Any] = {}
    if args.exchange_calendar:
        from studies.session_edges.data import load_exchange_calendar
        context["NYSE_CALENDAR"] = load_exchange_calendar(args.exchange_calendar)
        source_files.add(Path(args.exchange_calendar))
    for name, stem in _pairs(args.context).items():
        folder = Path(args.context_dir or args.daily_dir or ".")
        for suffix in (".json.gz", ".json"):
            if (folder / f"{stem}{suffix}").is_file():
                context[name.upper()] = load_daily_closes(folder / f"{stem}{suffix}")
                source_files.add(folder / f"{stem}{suffix}")
                break
        else:
            raise FileNotFoundError(f"no context file for {name} ({stem}) in {folder}")
    if not minute and not daily:
        parser.error("give --alpaca-dir, --index-dir or --daily-dir")
    from studies.session_edges.propose import proposal_rows
    proposals = proposal_rows(json.loads(Path(args.proposals).read_text(encoding="utf-8"))) if args.proposals else []
    registered = json.loads(Path(args.registered).read_text(encoding="utf-8")) if args.registered else None
    costs = {key.upper(): float(value) for key, value in _pairs(args.cost_bps).items()} if args.cost_bps else None
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    provenance = {"minute_sources": {label: "index_proxy" if label.endswith(PROXY_SUFFIX) else "historical_sip"
                                      for label in minute},
                  "daily_source": "public_adjusted_daily", "authorizing": False, "raw_source_digests": []}
    for path in sorted(source_files):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        provenance["raw_source_digests"].append({"name": path.name, "sha256": digest.hexdigest()})
    ledger_path = Path(args.ledger)
    with ledger_lock(ledger_path):
        ledger = load_ledger(ledger_path, create=args.initialize_ledger)
        if args.migrate_ledger:
            ledger = migrate_legacy_ledger(json.loads(Path(args.migrate_ledger).read_text(encoding="utf-8")))
        record = discover(minute=minute, daily=daily, proposals=proposals, ledger=ledger, costs=costs,
                          families=[item.strip() for item in args.families.split(",")] if args.families else None,
                          context=context, progress=lambda text: print(text, flush=True),
                          experiment_name=args.experiment, allow_new_experiment=args.new_experiment,
                          source_provenance=provenance)
        book = build_book(record, registered)
        from research.session_candidate_review import review_discovery
        review = review_discovery(record)
        write_ledger(ledger_path, ledger)
    (out / "discovery.json").write_text(json.dumps(record, indent=1, sort_keys=True, allow_nan=False) + "\n",
                                        encoding="utf-8")
    (out / "edge_book.json").write_text(json.dumps(book, indent=1, sort_keys=True, allow_nan=False) + "\n",
                                        encoding="utf-8")
    (out / "main_candidate_review.json").write_text(json.dumps(review, indent=1, sort_keys=True, allow_nan=False) + "\n",
                                                    encoding="utf-8")
    (out / "discovery.md").write_text(render_report(record, book), encoding="utf-8")
    tiers: dict[str, int] = {}
    for row in record["promoted"].values():
        tiers[row["tier"]] = tiers.get(row["tier"], 0) + 1
    print(f"tested {record['hypotheses_tested']}, promoted {record['promoted_ever']}, tiers {tiers}, "
          f"diagnostic legs {len(book['legs'])}; execution authority: none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
