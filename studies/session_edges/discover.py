"""Search the hypothesis grammar for edges and write the edge book the runner trades.

Usage, from the repository root::

    python -I studies/session_edges/discover.py --alpaca-dir DIR --symbols QQQ,SPY,IWM,DIA \\
        --daily-dir DIR --daily SPY,QQQ,IWM,DIA,XLK,SMH --out runtime/session_edges/discovery

    # public diagnostic data instead of SIP bars
    python -I studies/session_edges/discover.py --index-dir DIR --pairs nsxusd=QQQ,spxusd=SPY \\
        --daily-dir DIR --daily SPY,QQQ --out OUT

The search is free to try every family, parameter and instrument in
``grammar.py``; what it may conclude is fixed here:

1. **Screen on the design period only** (sessions before the holdout date).
   A hypothesis must have a Newey-West t of at least ``MIN_DESIGN_T`` net of
   costs, stay positive at twice the cost, trade often enough, and have most
   of its one-step parameter neighbours positive too.
2. **One champion per family and instrument** goes forward: the screened
   hypothesis with the highest design t. Nothing else touches the holdout.
3. **Confirm on the holdout** with a correction for every champion ever
   promoted. Holm at 5% makes a hypothesis ``confirmed``; Benjamini-Yekutieli
   at 10% makes it ``provisional``; both also need a positive holdout at twice
   the cost, and ``confirmed`` needs a positive result since ``RECENT_START``.
4. **The ledger remembers.** Every promoted hypothesis stays in the count, so
   searching again, or adding proposals, raises the bar instead of lowering it.

The edge book lists what the forward runner should trade (confirmed, or a
paper candidate of the registered-rule study) and what it should only record.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
from collections.abc import Mapping, Sequence  # noqa: E402
from datetime import date, datetime, timezone  # noqa: E402
from statistics import NormalDist, fmean  # noqa: E402
from typing import Any  # noqa: E402

from studies.session_edges import grammar, stats  # noqa: E402
from studies.session_edges.data import (DailyBar, Session, load_alpaca_sessions,  # noqa: E402
                                        load_daily_chart, load_index_minute_archives)
from studies.session_edges.run import HOLDOUT_START, RECENT_START, STRESS_MULTIPLE  # noqa: E402
from studies.session_edges.strategies import DayResult  # noqa: E402

SCHEMA = "session-edges-discovery.v1"
BOOK_SCHEMA = "session-edges-book.v1"
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
MIN_HOLDOUT_SESSIONS = 500
FAMILYWISE_ALPHA = 0.05
FALSE_DISCOVERY_RATE = 0.10
CONFIRMED, PROVISIONAL, UNCONFIRMED, REGISTERED = "confirmed", "provisional", "unconfirmed", "registered"

# Edge book: how much of the account each sleeve may use and how many legs it may hold.
SLEEVE_BUDGET = {"intraday": 0.50, "swing": 0.50}
MAX_LEGS_PER_SLEEVE = {"intraday": 1, "swing": 4}
MAX_LEG_WEIGHT = 0.25
# A leveraged fund repeats its index's edge with more risk per dollar, so it
# is recorded and not traded, and one traded leg stands for each underlying.
UNDERLYING = {"SSO": "SPY", "UPRO": "SPY", "SPXL": "SPY", "IVV": "SPY", "VOO": "SPY", "QLD": "QQQ",
              "TQQQ": "QQQ", "QQQM": "QQQ", "SOXL": "SMH", "TNA": "IWM", "UWM": "IWM"}
LEVERAGED = frozenset({"SSO", "UPRO", "SPXL", "QLD", "TQQQ", "SOXL", "TNA", "UWM"})
_NORMAL = NormalDist()


def base_symbol(label: str) -> str:
    return label[:-len(PROXY_SUFFIX)] if label.endswith(PROXY_SUFFIX) else label


def cost_for(label: str, overrides: Mapping[str, float] | None = None) -> float:
    symbol = base_symbol(label)
    if overrides and symbol in overrides:
        return float(overrides[symbol])
    return COST_BPS.get(symbol, DEFAULT_COST_BPS)


def window_stats(results: Sequence[DayResult], cost_bps: float, *, start: date | None = None,
                 end: date | None = None) -> dict[str, Any]:
    """Net statistics of the sessions in ``[start, end)`` at ``cost_bps`` and at the stressed cost."""
    chosen = [result for result in results
              if (start is None or result.day >= start) and (end is None or result.day < end)]
    net = stats.net_series(chosen, cost_bps)
    stressed = stats.net_series(chosen, cost_bps * STRESS_MULTIPLE)
    trips = sum(result.round_trips for result in chosen)
    t_value = stats.newey_west_t(net)
    return {"sessions": len(chosen), "round_trips": trips,
            "net_bps_per_session": fmean(net) * 1e4 if net else None,
            "stressed_bps_per_session": fmean(stressed) * 1e4 if stressed else None,
            "t": t_value, "sharpe": stats.annualised_sharpe(net),
            "p_one_sided": None if t_value is None else 1.0 - _NORMAL.cdf(t_value)}


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
    return not failed, failed


def holm(p_values: Mapping[str, float], alpha: float = FAMILYWISE_ALPHA) -> set[str]:
    """Hypotheses rejected by Holm's step-down procedure (family-wise error ``alpha``)."""
    ordered = sorted(p_values.items(), key=lambda item: item[1])
    passed: set[str] = set()
    for rank, (key, p_value) in enumerate(ordered):
        if p_value > alpha / (len(ordered) - rank):
            break
        passed.add(key)
    return passed


def benjamini_yekutieli(p_values: Mapping[str, float], rate: float = FALSE_DISCOVERY_RATE) -> set[str]:
    """Hypotheses rejected at false discovery rate ``rate`` under arbitrary dependence."""
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


def load_ledger(path: Path | None) -> dict[str, Any]:
    if path is not None and path.is_file():
        ledger = json.loads(path.read_text(encoding="utf-8"))
        if ledger.get("schema") == SCHEMA and ledger.get("holdout_start") == HOLDOUT_START.isoformat():
            return ledger
    return {"schema": SCHEMA, "holdout_start": HOLDOUT_START.isoformat(), "promoted": {}}


def discover(*, minute: Mapping[str, Sequence[Session]], daily: Mapping[str, Sequence[DailyBar]],
             proposals: Sequence[Mapping[str, Any]] = (), ledger: dict[str, Any] | None = None,
             costs: Mapping[str, float] | None = None, families: Sequence[str] | None = None,
             progress: Any = None) -> dict[str, Any]:
    """Run the whole protocol on loaded data and return the discovery record."""
    ledger = ledger if ledger is not None else load_ledger(None)
    datasets: dict[tuple[str, str], Sequence[Any]] = {}
    for label, sessions in minute.items():
        datasets[(grammar.MINUTE, label)] = sessions
    for symbol, bars in daily.items():
        datasets[(grammar.DAILY, symbol)] = bars
    wanted = set(families) if families else set(grammar.FAMILIES)

    # ---- stage 1: every hypothesis, design period only ------------------
    candidates: dict[str, dict[str, Any]] = {}
    rejected_proposals: list[dict[str, Any]] = []

    def evaluate(spec: Mapping[str, Any], source: str) -> dict[str, Any] | None:
        canonical = grammar.normalise(spec)
        family = grammar.FAMILIES[canonical["family"]]
        data = datasets.get((family.data, canonical["symbol"]))
        if data is None:
            return None
        key = grammar.spec_id(canonical)
        if key not in candidates:
            results = grammar.simulator(canonical)(data)
            cost = cost_for(canonical["symbol"], costs)
            candidates[key] = {"id": key, "spec": canonical, "source": source, "cost_bps": cost,
                               "design": window_stats(results, cost, end=HOLDOUT_START)}
        return candidates[key]

    for (kind, label) in datasets:
        for family in grammar.FAMILIES.values():
            if family.data != kind or family.name not in wanted:
                continue
            for spec in grammar.enumerate_family(family, label):
                evaluate(spec, "grid")
            if progress:
                progress(f"design {label} {family.name}")
    for proposal in proposals:
        try:
            if evaluate(proposal, "proposal") is None:
                rejected_proposals.append({"spec": dict(proposal), "reason": "no data for that family and symbol"})
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
        if candidate["id"] in promoted:
            continue
        family = grammar.FAMILIES[candidate["spec"]["family"]]
        data = datasets[(family.data, candidate["spec"]["symbol"])]
        results = grammar.simulator(candidate["spec"])(data)
        promoted[candidate["id"]] = {
            "spec": candidate["spec"], "source": candidate["source"], "cost_bps": candidate["cost_bps"],
            "design": candidate["design"], "neighbour_share": candidate["neighbour_share"],
            "holdout": window_stats(results, candidate["cost_bps"], start=HOLDOUT_START),
            "recent": window_stats(results, candidate["cost_bps"], start=RECENT_START),
            "data_last_session": results[-1].day.isoformat() if results else None,
            "promoted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if progress:
            progress(f"holdout {candidate['spec']['symbol']} {candidate['spec']['family']}")

    testable = {key: row["holdout"]["p_one_sided"] for key, row in promoted.items()
                if row["holdout"]["p_one_sided"] is not None
                and row["holdout"]["sessions"] >= MIN_HOLDOUT_SESSIONS}
    family_wise = holm(testable)
    discoveries = benjamini_yekutieli(testable)
    for key, row in promoted.items():
        holdout, recent = row["holdout"], row["recent"]
        survives_cost = (holdout["stressed_bps_per_session"] or 0) > 0
        recent_positive = (recent["net_bps_per_session"] or 0) > 0
        if key in family_wise and survives_cost and recent_positive:
            row["tier"] = CONFIRMED
        elif key in discoveries and survives_cost:
            row["tier"] = PROVISIONAL
        else:
            row["tier"] = UNCONFIRMED
        row["checks"] = {"holm": key in family_wise, "false_discovery": key in discoveries,
                         "positive_at_stressed_cost": survives_cost, "positive_recently": recent_positive,
                         "holdout_long_enough": key in testable}

    funnel: dict[str, dict[str, int]] = {}
    for candidate in candidates.values():
        row = funnel.setdefault(candidate["spec"]["family"], {"tested": 0, "screened": 0, "champions": 0})
        row["tested"] += 1
        row["screened"] += 1 if candidate["screened"] else 0
    for (family_name, _) in champions:
        funnel[family_name]["champions"] += 1
    return {"schema": SCHEMA, "holdout_start": HOLDOUT_START.isoformat(),
            "recent_start": RECENT_START.isoformat(),
            "thresholds": {"min_design_t": MIN_DESIGN_T, "min_neighbour_share": MIN_NEIGHBOUR_SHARE,
                           "stress_multiple": STRESS_MULTIPLE, "familywise_alpha": FAMILYWISE_ALPHA,
                           "false_discovery_rate": FALSE_DISCOVERY_RATE},
            "datasets": {f"{kind}:{label}": len(data) for (kind, label), data in datasets.items()},
            "hypotheses_tested": len(candidates), "champions_this_run": len(champions),
            "promoted_ever": len(promoted), "funnel": funnel, "promoted": promoted,
            "rejected_proposals": rejected_proposals,
            "design_leaderboard": design_leaderboard(candidates)}


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
    """The legs the runner should trade or record, with their share of the account.

    A leg trades when it is ``confirmed`` here or a paper candidate of the
    registered-rule study; ``provisional`` legs are recorded only. One traded
    leg owns one symbol, the strongest evidence first.
    """
    legs: list[dict[str, Any]] = []
    for key, row in record["promoted"].items():
        family = grammar.FAMILIES[row["spec"]["family"]]
        if family.runner_rule is None or row["tier"] == UNCONFIRMED:
            continue
        legs.append({"id": key, "rule": family.runner_rule, "symbol": base_symbol(row["spec"]["symbol"]),
                     "params": _runner_params(family, row["spec"]["params"]), "tier": row["tier"],
                     "family": family.name, "evidence": row["spec"]["symbol"],
                     "holdout_t": row["holdout"]["t"], "holdout_sharpe": row["holdout"]["sharpe"]})
    for label, verdicts in ((registered or {}).get("minute") or {}).items():
        for name, block in verdicts.items():
            family = grammar.FAMILIES.get(name)
            if family is None or family.runner_rule is None or block.get("verdict") != "paper_candidate":
                continue
            holdout = (block.get("evidence") or {}).get("holdout") or {}
            legs.append({"id": f"registered:{name}:{base_symbol(label)}", "rule": family.runner_rule,
                         "symbol": base_symbol(label), "params": _runner_params(family, family.defaults()),
                         "tier": REGISTERED, "family": name, "evidence": label,
                         "holdout_t": holdout.get("t_newey_west"), "holdout_sharpe": holdout.get("sharpe")})
    order = {CONFIRMED: 0, REGISTERED: 1, PROVISIONAL: 2}
    legs.sort(key=lambda leg: (order[leg["tier"]], -(leg["holdout_t"] or 0.0)))
    taken_symbols: set[str] = set()
    taken_underlyings: set[tuple[str, str]] = set()
    seen: set[tuple[str, str]] = set()
    counts = {sleeve: 0 for sleeve in SLEEVE_BUDGET}
    book: list[dict[str, Any]] = []
    for leg in legs:
        slot = (leg["rule"], leg["symbol"])
        if slot in seen:
            continue                      # the same rule on the same symbol: the stronger evidence is kept
        seen.add(slot)
        sleeve = _sleeve(leg["rule"])
        underlying = (sleeve, UNDERLYING.get(leg["symbol"], leg["symbol"]))
        if leg["tier"] not in (CONFIRMED, REGISTERED):
            note = "evidence tier does not trade"
        elif leg["symbol"] in LEVERAGED:
            note = "leveraged fund: recorded only"
        elif leg["symbol"] in taken_symbols:
            note = "symbol already owned by a stronger leg"
        elif underlying in taken_underlyings:
            note = "underlying index already traded in this sleeve"
        elif counts[sleeve] >= MAX_LEGS_PER_SLEEVE[sleeve]:
            note = "sleeve is full"
        else:
            note = ""
        trade = not note
        if trade:
            taken_symbols.add(leg["symbol"])
            taken_underlyings.add(underlying)
            counts[sleeve] += 1
        book.append({**leg, "sleeve": sleeve, "trade": trade, "weight": 0.0, "note": note})
    for leg in book:
        if leg["trade"]:
            leg["weight"] = round(min(MAX_LEG_WEIGHT, SLEEVE_BUDGET[leg["sleeve"]] / counts[leg["sleeve"]]), 4)
    return {"schema": BOOK_SCHEMA, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "holdout_start": record["holdout_start"], "promoted_ever": record["promoted_ever"],
            "legs": book}


def _cell(value: Any, places: int = 2) -> str:
    return "n/a" if not isinstance(value, (int, float)) else f"{value:.{places}f}"


def render_report(record: Mapping[str, Any], book: Mapping[str, Any]) -> str:
    lines = ["# Edge discovery", "",
             f"{record['hypotheses_tested']} hypotheses tested on the design period (before "
             f"{record['holdout_start']}); {record['promoted_ever']} champions have ever been taken to the "
             f"holdout and all of them are in the correction. Costs are per round trip, per instrument, "
             f"and every check is repeated at {STRESS_MULTIPLE:g}x.", "",
             "## Funnel", "", "| family | tested | passed the design screen | champions |", "|---|---|---|---|"]
    for name, row in sorted(record["funnel"].items()):
        lines.append(f"| {name} | {row['tested']} | {row['screened']} | {row['champions']} |")
    lines += ["", "## Holdout results of every promoted hypothesis", "",
              "| tier | family | instrument | parameters | design t | holdout t | holdout Sharpe | "
              "holdout bps/session | since recent start bps/session |", "|---|---|---|---|---|---|---|---|---|"]
    order = {CONFIRMED: 0, PROVISIONAL: 1, UNCONFIRMED: 2}
    for row in sorted(record["promoted"].values(),
                      key=lambda item: (order[item["tier"]], -(item["holdout"]["t"] or -99))):
        spec = row["spec"]
        params = ", ".join(f"{key}={value}" for key, value in spec["params"].items())
        lines.append(f"| {row['tier']} | {spec['family']} | {spec['symbol']} | {params} | "
                     f"{_cell(row['design']['t'])} | {_cell(row['holdout']['t'])} | "
                     f"{_cell(row['holdout']['sharpe'])} | {_cell(row['holdout']['net_bps_per_session'])} | "
                     f"{_cell(row['recent']['net_bps_per_session'])} |")
    lines += ["", "## Edge book", "",
              "| trade | weight | tier | rule | symbol | parameters | evidence | note |",
              "|---|---|---|---|---|---|---|---|"]
    for leg in book["legs"]:
        params = ", ".join(f"{key}={value}" for key, value in leg["params"].items())
        lines.append(f"| {'yes' if leg['trade'] else 'record only'} | {leg['weight']:.1%} | {leg['tier']} | "
                     f"{leg['rule']} | {leg['symbol']} | {params} | {leg['family']} on {leg['evidence']} | "
                     f"{leg['note']} |")
    lines += ["", "`confirmed` passed Holm at 5% across every promoted hypothesis, stayed positive at twice "
              "the cost and has been positive since the recent start. `provisional` passed "
              "Benjamini-Yekutieli at 10% and the cost stress. `registered` is a paper candidate of the "
              "fixed-rule study. Only `confirmed` and `registered` legs trade.", ""]
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
    parser.add_argument("--index-dir", help="public index one-minute archives")
    parser.add_argument("--pairs", default="nsxusd=QQQ,spxusd=SPY")
    parser.add_argument("--daily-dir", help="adjusted daily chart files (fetch_public)")
    parser.add_argument("--daily", default="SPY,QQQ,IWM,DIA")
    parser.add_argument("--proposals", help="JSON file: a list of hypotheses to test alongside the grid")
    parser.add_argument("--registered", help="results.json of run.py, to add its paper candidates to the book")
    parser.add_argument("--ledger", help="ledger file; defaults to OUT/ledger.json")
    parser.add_argument("--families", help="comma-separated family names to search; default all")
    parser.add_argument("--cost-bps", default="", help="per-symbol round-trip costs, e.g. XLK=2,SMH=2.5")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    minute: dict[str, list[Session]] = {}
    if args.alpaca_dir:
        for symbol in [item.strip().upper() for item in args.symbols.split(",") if item.strip()]:
            minute[symbol] = load_alpaca_sessions(args.alpaca_dir, symbol, calendar=args.calendar)
    if args.index_dir:
        for pair, label in _pairs(args.pairs).items():
            minute[f"{label}{PROXY_SUFFIX}"] = load_index_minute_archives(args.index_dir, pair)
    daily: dict[str, list[DailyBar]] = {}
    if args.daily_dir:
        for symbol in [item.strip().upper() for item in args.daily.split(",") if item.strip()]:
            for suffix in (".json.gz", ".json"):
                path = Path(args.daily_dir) / f"{symbol}{suffix}"
                if path.is_file():
                    daily[symbol] = load_daily_chart(path)
                    break
            else:
                raise FileNotFoundError(f"no daily chart file for {symbol} in {args.daily_dir}")
    if not minute and not daily:
        parser.error("give --alpaca-dir, --index-dir or --daily-dir")
    proposals = json.loads(Path(args.proposals).read_text(encoding="utf-8")) if args.proposals else []
    if not isinstance(proposals, list):
        parser.error("--proposals must hold a JSON list")
    registered = json.loads(Path(args.registered).read_text(encoding="utf-8")) if args.registered else None
    costs = {key.upper(): float(value) for key, value in _pairs(args.cost_bps).items()} if args.cost_bps else None
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ledger_path = Path(args.ledger) if args.ledger else out / "ledger.json"
    ledger = load_ledger(ledger_path)
    record = discover(minute=minute, daily=daily, proposals=proposals, ledger=ledger, costs=costs,
                      families=[item.strip() for item in args.families.split(",")] if args.families else None,
                      progress=lambda text: print(text, flush=True))
    book = build_book(record, registered)
    ledger_path.write_text(json.dumps(_round(ledger), indent=1, sort_keys=True) + "\n", encoding="utf-8")
    (out / "discovery.json").write_text(json.dumps(_round(record), indent=1, sort_keys=True) + "\n",
                                        encoding="utf-8")
    (out / "edge_book.json").write_text(json.dumps(_round(book), indent=1, sort_keys=True) + "\n",
                                        encoding="utf-8")
    (out / "discovery.md").write_text(render_report(record, book), encoding="utf-8")
    tiers: dict[str, int] = {}
    for row in record["promoted"].values():
        tiers[row["tier"]] = tiers.get(row["tier"], 0) + 1
    print(f"tested {record['hypotheses_tested']}, promoted {record['promoted_ever']}, tiers {tiers}, "
          f"legs to trade {sum(1 for leg in book['legs'] if leg['trade'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
