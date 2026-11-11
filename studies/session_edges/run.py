"""Run the session-edge study and write ``results.json`` and ``report.md``.

Usage, from the repository root::

    # public diagnostic data (index minute archives and adjusted daily bars)
    python -I studies/session_edges/run.py --index-dir DIR --pairs nsxusd=QQQ,spxusd=SPY \\
        --daily-dir DIR --daily SPY,QQQ,IWM,DIA --out studies/session_edges/results/public

    # the deployment's own data: SIP one-minute month files from liquid_reversion.fetch_sip
    python -I studies/session_edges/run.py --alpaca-dir DIR --symbols QQQ,SPY --out DIR

``python -m studies.session_edges.run`` takes the same arguments. The rules
and the decision thresholds are fixed in this file and in ``strategies.py``;
nothing is selected from the data the verdict is computed on.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    # ``python -I path/to/run.py`` puts neither the working directory nor the
    # script's folder on sys.path, so make the repository root importable.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse  # noqa: E402
import json  # noqa: E402
from collections.abc import Callable, Mapping, Sequence  # noqa: E402
from dataclasses import replace  # noqa: E402
from datetime import date  # noqa: E402

from studies.session_edges import stats  # noqa: E402
from studies.session_edges.data import (DailyBar, Session, load_alpaca_sessions,  # noqa: E402
                                        load_daily_chart, load_index_minute_archives)
from studies.session_edges.strategies import (CLOSE_REVERSION, NOISE_BAND, OPENING_RANGE,  # noqa: E402
                                              DayResult, simulate_close_reversion,
                                              simulate_close_reversion_daily, simulate_first_last,
                                              simulate_noise_band, simulate_opening_range,
                                              simulate_overnight)

SCHEMA = "session-edges-study.v1"
# Sessions before this date are the design period the published rules were
# written on; sessions on or after it were never used to choose anything here.
HOLDOUT_START = date(2020, 1, 1)
RECENT_START = date(2024, 1, 1)
# Round-trip cost of a marketable order in the most liquid index ETFs: a one
# cent spread on a 500-600 dollar share is 0.2 bps, plus regulatory fees on the
# sale and a slippage allowance. Every verdict must also hold at twice this.
BASE_COST_BPS = 1.0
STRESS_MULTIPLE = 2.0
COST_LADDER_BPS = (0.0, 0.5, 1.0, 2.0, 3.0, 5.0)
# Forward windows, in sessions, for which the backtest's own distribution of
# cumulative results is published (the shadow and paper review bands).
FORWARD_WINDOWS = (20, 60, 120, 250)

# Fixed decision thresholds (registered with the rules, not tuned).
MIN_T_ALL = 3.0             # Harvey, Liu and Zhu (2016): t > 3 for a mined effect
MIN_T_HOLDOUT = 2.0
MIN_NEIGHBOUR_SHARE = 0.70  # share of one-at-a-time neighbours positive in the holdout
MIN_DEFLATED_SHARPE = 0.95
PAPER, SHADOW, REJECT = "paper_candidate", "shadow_only", "rejected"

Simulator = Callable[[Sequence[Session]], list[DayResult]]


def noise_band_neighbours() -> dict[str, Simulator]:
    """One-at-a-time variations around the registered noise-band rule (robustness, not selection)."""
    out: dict[str, Simulator] = {}
    for lookback in (7, 10, 20, 30):
        out[f"lookback={lookback}"] = lambda s, v=lookback: simulate_noise_band(
            s, replace(NOISE_BAND, lookback_sessions=v))
    for step in (15, 60):
        out[f"step={step}"] = lambda s, v=step: simulate_noise_band(
            s, replace(NOISE_BAND, decision_step_minutes=v, first_decision_minute=max(30, v)))
    for band in (0.75, 1.25, 1.5):
        out[f"band={band}"] = lambda s, v=band: simulate_noise_band(s, replace(NOISE_BAND, band_multiple=v))
    out["no_gap_reference"] = lambda s: simulate_noise_band(s, replace(NOISE_BAND, use_previous_close=False))
    out["long_only"] = lambda s: simulate_noise_band(s, replace(NOISE_BAND, sides=("long",)))
    out["short_only"] = lambda s: simulate_noise_band(s, replace(NOISE_BAND, sides=("short",)))
    out["flat_at_15:58"] = lambda s: simulate_noise_band(s, replace(NOISE_BAND, flat_minute=388))
    return out


def close_reversion_neighbours() -> dict[str, Simulator]:
    """One-at-a-time variations around the registered close-weakness rule."""
    out: dict[str, Simulator] = {}
    for enter in (0.10, 0.15, 0.25, 0.30):
        out[f"enter_below={enter}"] = lambda s, v=enter: simulate_close_reversion(
            s, replace(CLOSE_REVERSION, enter_below=v))
    for leave in (0.40, 0.60):
        out[f"exit_above={leave}"] = lambda s, v=leave: simulate_close_reversion(
            s, replace(CLOSE_REVERSION, exit_above=v))
    for held in (3, 10):
        out[f"max_sessions={held}"] = lambda s, v=held: simulate_close_reversion(
            s, replace(CLOSE_REVERSION, max_sessions=v))
    for minute in (360, 385):
        out[f"decision_minute={minute}"] = lambda s, v=minute: simulate_close_reversion(
            s, replace(CLOSE_REVERSION, decision_minute=v))
    return out


def opening_range_neighbours() -> dict[str, Simulator]:
    out: dict[str, Simulator] = {}
    for minutes in (15, 30):
        out[f"minutes={minutes}"] = lambda s, v=minutes: simulate_opening_range(
            s, replace(OPENING_RANGE, minutes=v))
    out["no_target"] = lambda s: simulate_opening_range(s, replace(OPENING_RANGE, target_r=1e9))
    return out


RULES: dict[str, tuple[Simulator, Callable[[], dict[str, Simulator]]]] = {
    "noise_band_momentum": (simulate_noise_band, noise_band_neighbours),
    "close_weakness_reversion": (simulate_close_reversion, close_reversion_neighbours),
    "opening_range_breakout": (simulate_opening_range, opening_range_neighbours),
    "first_last_half_hour": (simulate_first_last, dict),
}


def verdict(evidence: Mapping[str, object], stressed: Mapping[str, object],
            neighbour_share: float | None) -> tuple[str, list[str]]:
    """Apply the registered thresholds; returns the tier and the checks that failed."""
    failed: list[str] = []
    overall = evidence.get("all") or {}
    holdout = evidence.get("holdout") or {}
    stressed_all = stressed.get("all") or {}
    stressed_holdout = stressed.get("holdout") or {}

    def number(block: Mapping[str, object], key: str) -> float:
        value = block.get(key)
        return float(value) if isinstance(value, (int, float)) else float("nan")

    if not number(overall, "t_newey_west") >= MIN_T_ALL:
        failed.append(f"all-period t below {MIN_T_ALL}")
    if not number(holdout, "t_newey_west") >= MIN_T_HOLDOUT:
        failed.append(f"holdout t below {MIN_T_HOLDOUT}")
    if not (number(stressed_all, "net_bps_per_session") > 0 and
            number(stressed_holdout, "net_bps_per_session") > 0):
        failed.append(f"not positive at {STRESS_MULTIPLE:g}x cost")
    if neighbour_share is None or neighbour_share < MIN_NEIGHBOUR_SHARE:
        failed.append(f"fewer than {MIN_NEIGHBOUR_SHARE:.0%} of neighbours positive in the holdout")
    deflated = evidence.get("deflated_sharpe")
    if not (isinstance(deflated, float) and deflated >= MIN_DEFLATED_SHARPE):
        failed.append(f"deflated Sharpe below {MIN_DEFLATED_SHARPE}")
    if not failed:
        return PAPER, failed
    positive = (number(overall, "net_bps_per_session") > 0 and number(holdout, "net_bps_per_session") > 0
                and number(overall, "t_newey_west") >= MIN_T_HOLDOUT)
    return (SHADOW if positive else REJECT), failed


def study_rule(name: str, sessions: Sequence[Session], *, draws: int) -> dict[str, object]:
    """Registered rule, cost ladder and one-at-a-time neighbours on one instrument."""
    simulate, neighbours = RULES[name]
    base = simulate(sessions)
    variants = {label: run(sessions) for label, run in neighbours().items()}
    trials = [sharpe for sharpe in (stats.per_session_sharpe(result, BASE_COST_BPS)
                                    for result in (base, *variants.values())) if sharpe is not None]
    evidence = stats.evaluate(base, cost_bps=BASE_COST_BPS, split=HOLDOUT_START, recent=RECENT_START,
                              trials=trials, draws=draws)
    stressed = stats.evaluate(base, cost_bps=BASE_COST_BPS * STRESS_MULTIPLE, split=HOLDOUT_START, draws=1)
    ladder = {}
    for cost in COST_LADDER_BPS:
        block = stats.summarise([r.day for r in base], stats.net_series(base, cost))
        ladder[f"{cost:g}"] = None if block is None else {"sharpe": block["sharpe"],
                                                          "net_bps_per_session": block["net_bps_per_session"]}
    neighbour_rows = {}
    positive = 0
    for label, result in variants.items():
        block = stats.evaluate(result, cost_bps=BASE_COST_BPS, split=HOLDOUT_START, draws=1)
        holdout = block.get("holdout") or {}
        overall = block.get("all") or {}
        neighbour_rows[label] = {"sharpe_all": overall.get("sharpe"), "t_all": overall.get("t_newey_west"),
                                 "sharpe_holdout": holdout.get("sharpe"),
                                 "net_bps_holdout": holdout.get("net_bps_per_session")}
        value = holdout.get("net_bps_per_session")
        positive += 1 if isinstance(value, float) and value > 0 else 0
    share = positive / len(variants) if variants else None
    tier, failed = verdict(evidence, stressed, share)
    base_net = stats.net_series(base, BASE_COST_BPS)
    bands = {str(window): stats.rolling_sum_quantiles(base_net, window) for window in FORWARD_WINDOWS}
    return {"rule": name, "evidence": evidence, "stressed_cost": {"all": stressed.get("all"),
                                                                 "holdout": stressed.get("holdout")},
            "cost_ladder": ladder, "neighbours": neighbour_rows, "forward_bands_pct": bands,
            "neighbours_positive_in_holdout": share, "verdict": tier, "failed_checks": failed,
            "_net": stats.net_series(base, BASE_COST_BPS), "_days": [r.day for r in base]}


def study_daily(symbol: str, bars: Sequence[DailyBar], *, draws: int) -> dict[str, object]:
    """Long-history daily forms: the close-weakness rule at the close, and the overnight return."""
    reversion = simulate_close_reversion_daily(bars)
    overnight = simulate_overnight(bars)
    return {
        "symbol": symbol,
        "close_weakness_reversion_at_close": stats.evaluate(
            reversion, cost_bps=BASE_COST_BPS, split=HOLDOUT_START, recent=RECENT_START, draws=draws),
        "overnight_long": stats.evaluate(
            overnight, cost_bps=BASE_COST_BPS, split=HOLDOUT_START, recent=RECENT_START, draws=draws),
    }


def combined_book(minute: Mapping[str, Mapping[str, dict[str, object]]]) -> dict[str, object] | None:
    """Every paper candidate, equally weighted within its rule, on the sessions they share."""
    legs: list[tuple[list[date], list[float], float]] = []
    names: list[str] = []
    by_rule: dict[str, list[tuple[str, dict[str, object]]]] = {}
    for label, rules in minute.items():
        for name, block in rules.items():
            if block["verdict"] == PAPER:
                by_rule.setdefault(name, []).append((label, block))
    for name, members in by_rule.items():
        for label, block in members:
            legs.append((block["_days"], block["_net"], 1.0 / len(members)))
            names.append(f"{name}:{label}")
    if len(legs) < 2:
        return None
    days, net = stats.combine(legs)
    summary = stats.summarise(days, net)
    if summary is None:
        return None
    matrix = {}
    for i, left in enumerate(names):
        for j in range(i + 1, len(names)):
            shared, _ = stats.combine([legs[i], legs[j]])
            a = dict(zip(legs[i][0], legs[i][1]))
            b = dict(zip(legs[j][0], legs[j][1]))
            matrix[f"{left} ~ {names[j]}"] = stats.correlation([a[d] for d in shared], [b[d] for d in shared])
    holdout = [(day, value) for day, value in zip(days, net) if day >= HOLDOUT_START]
    return {"legs": names, "weights": "each rule at 100% of equity, split equally across its instruments",
            "all": summary,
            "holdout": stats.summarise([d for d, _ in holdout], [v for _, v in holdout]) if holdout else None,
            "by_year_pct": stats.by_year(days, net), "correlations": matrix,
            "bootstrap_sharpe": stats.block_bootstrap_sharpe(net)}


def _strip(value: object) -> object:
    if isinstance(value, dict):
        return {key: _strip(item) for key, item in value.items() if not str(key).startswith("_")}
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (list, tuple)):
        return [_strip(item) for item in value]
    return value


def _cell(value: object, places: int = 2) -> str:
    return "n/a" if not isinstance(value, (int, float)) else f"{value:.{places}f}"


def render_report(results: Mapping[str, object]) -> str:
    """A compact Markdown view of ``results``; the JSON is the record."""
    lines = ["# Session-edge study", "",
             f"Source: {results['source']}. Costs: {BASE_COST_BPS:g} bps per round trip "
             f"(stress {BASE_COST_BPS * STRESS_MULTIPLE:g} bps). Design period before "
             f"{HOLDOUT_START.isoformat()}, holdout from that date. Returns are arithmetic, on notional, "
             "unlevered. Statistics are per session.", "",
             "## Registered rules on one-minute sessions", "",
             "| instrument | rule | sessions | Sharpe | t | net bps/session | holdout Sharpe | holdout t | "
             "since 2024 Sharpe | positive years | max drawdown % | deflated Sharpe | verdict |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for label, rules in results["minute"].items():
        for name, block in rules.items():
            evidence = block["evidence"]
            overall, holdout = evidence.get("all") or {}, evidence.get("holdout") or {}
            recent = evidence.get("recent") or {}
            lines.append(
                f"| {label} | {name} | {overall.get('sessions', 'n/a')} | {_cell(overall.get('sharpe'))} | "
                f"{_cell(overall.get('t_newey_west'))} | {_cell(overall.get('net_bps_per_session'))} | "
                f"{_cell(holdout.get('sharpe'))} | {_cell(holdout.get('t_newey_west'))} | "
                f"{_cell(recent.get('sharpe'))} | {evidence.get('positive_years')}/{evidence.get('years')} | "
                f"{_cell(overall.get('max_drawdown_pct'), 1)} | {_cell(evidence.get('deflated_sharpe'))} | "
                f"{block['verdict']} |")
    lines += ["", "Failed checks:", ""]
    for label, rules in results["minute"].items():
        for name, block in rules.items():
            if block["failed_checks"]:
                lines.append(f"- {label} {name}: " + "; ".join(block["failed_checks"]))
    lines += ["", "## Cost sensitivity (annualised Sharpe by round-trip cost in bps)", "",
              "| instrument | rule | " + " | ".join(f"{cost:g}" for cost in COST_LADDER_BPS) + " |",
              "|---|---|" + "---|" * len(COST_LADDER_BPS)]
    for label, rules in results["minute"].items():
        for name, block in rules.items():
            cells = [_cell((block["cost_ladder"].get(f"{cost:g}") or {}).get("sharpe")) for cost in COST_LADDER_BPS]
            lines.append(f"| {label} | {name} | " + " | ".join(cells) + " |")
    lines += ["", "## Forward review bands (cumulative net % over N sessions, from the backtest itself)", "",
              "| instrument | rule | " + " | ".join(f"{w} sessions: 1% / 5% / median" for w in FORWARD_WINDOWS) + " |",
              "|---|---|" + "---|" * len(FORWARD_WINDOWS)]
    for label, rules in results["minute"].items():
        for name, block in rules.items():
            if block["verdict"] == REJECT:
                continue
            cells = []
            for window in FORWARD_WINDOWS:
                band = block["forward_bands_pct"].get(str(window)) or {}
                cells.append(" / ".join(_cell(band.get(key), 1) for key in ("p01", "p05", "p50")))
            lines.append(f"| {label} | {name} | " + " | ".join(cells) + " |")
    if results.get("daily"):
        lines += ["", "## Daily bars (long history)", "",
                  "| symbol | rule | sessions | from | Sharpe | t | holdout Sharpe | since 2024 Sharpe | "
                  "positive years | max drawdown % |", "|---|---|---|---|---|---|---|---|---|---|"]
        for symbol, block in results["daily"].items():
            for name in ("close_weakness_reversion_at_close", "overnight_long"):
                evidence = block[name]
                overall, holdout = evidence.get("all") or {}, evidence.get("holdout") or {}
                recent = evidence.get("recent") or {}
                lines.append(
                    f"| {symbol} | {name} | {overall.get('sessions', 'n/a')} | {overall.get('first', 'n/a')} | "
                    f"{_cell(overall.get('sharpe'))} | {_cell(overall.get('t_newey_west'))} | "
                    f"{_cell(holdout.get('sharpe'))} | {_cell(recent.get('sharpe'))} | "
                    f"{evidence.get('positive_years')}/{evidence.get('years')} | "
                    f"{_cell(overall.get('max_drawdown_pct'), 1)} |")
    book = results.get("combined_book")
    if book:
        overall, holdout = book["all"], book.get("holdout") or {}
        lines += ["", "## Combined book of paper candidates", "",
                  f"Legs: {', '.join(book['legs'])}. {book['weights']}.", "",
                  f"- Annual return {_cell(overall.get('annual_return_pct'))}%, volatility "
                  f"{_cell(overall.get('annual_volatility_pct'))}%, Sharpe {_cell(overall.get('sharpe'))} "
                  f"(t {_cell(overall.get('t_newey_west'))}), max drawdown "
                  f"{_cell(overall.get('max_drawdown_pct'), 1)}%.",
                  f"- Holdout Sharpe {_cell(holdout.get('sharpe'))}.",
                  f"- Bootstrap 95% Sharpe interval: {_cell((book.get('bootstrap_sharpe') or {}).get('low'))} to "
                  f"{_cell((book.get('bootstrap_sharpe') or {}).get('high'))}."]
        for pair, value in book["correlations"].items():
            lines.append(f"- Correlation {pair}: {_cell(value)}")
    lines += ["", "## Reading the verdicts", "",
              f"`{PAPER}` needs every check: all-period t of at least {MIN_T_ALL}, holdout t of at least "
              f"{MIN_T_HOLDOUT}, positive at {STRESS_MULTIPLE:g}x cost in both periods, at least "
              f"{MIN_NEIGHBOUR_SHARE:.0%} of one-at-a-time neighbours positive in the holdout, and a deflated "
              f"Sharpe ratio of at least {MIN_DEFLATED_SHARPE}. `{SHADOW}` is positive in both periods but "
              f"fails a check; it is recorded forward and never traded. `{REJECT}` is everything else.", ""]
    return "\n".join(lines)


def run(*, minute: Mapping[str, Sequence[Session]], daily: Mapping[str, Sequence[DailyBar]],
        source: str, draws: int = stats.SIGN_FLIP_DRAWS) -> dict[str, object]:
    """The whole study on already-loaded data."""
    minute_results = {label: {name: study_rule(name, sessions, draws=draws) for name in RULES}
                      for label, sessions in minute.items()}
    results: dict[str, object] = {
        "schema": SCHEMA, "source": source,
        "registration": {"holdout_start": HOLDOUT_START.isoformat(), "base_cost_bps": BASE_COST_BPS,
                         "stress_multiple": STRESS_MULTIPLE, "min_t_all": MIN_T_ALL,
                         "min_t_holdout": MIN_T_HOLDOUT, "min_neighbour_share": MIN_NEIGHBOUR_SHARE,
                         "min_deflated_sharpe": MIN_DEFLATED_SHARPE},
        "minute": minute_results,
        "daily": {symbol: study_daily(symbol, bars, draws=draws) for symbol, bars in daily.items()},
        "combined_book": combined_book(minute_results),
    }
    return results


def _pairs(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in text.split(","):
        if item.strip():
            key, _, label = item.partition("=")
            out[key.strip()] = (label or key).strip()
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--index-dir", help="directory of public index one-minute archives")
    parser.add_argument("--pairs", default="nsxusd=QQQ,spxusd=SPY",
                        help="archive prefix=label pairs for --index-dir")
    parser.add_argument("--alpaca-dir", help="directory of SIP 1Min month files (liquid_reversion.fetch_sip)")
    parser.add_argument("--symbols", default="QQQ,SPY", help="symbols to read from --alpaca-dir")
    parser.add_argument("--calendar", help="calendar.json written by the SIP fetcher (early closes)")
    parser.add_argument("--daily-dir", help="directory of adjusted daily chart files (fetch_public)")
    parser.add_argument("--daily", default="SPY,QQQ,IWM,DIA", help="symbols to read from --daily-dir")
    parser.add_argument("--out", required=True, help="output directory for results.json and report.md")
    parser.add_argument("--draws", type=int, default=stats.SIGN_FLIP_DRAWS, help="sign-flip permutation draws")
    args = parser.parse_args(argv)
    if not args.index_dir and not args.alpaca_dir:
        parser.error("give --index-dir or --alpaca-dir")
    minute: dict[str, list[Session]] = {}
    sources: list[str] = []
    if args.alpaca_dir:
        for symbol in [item.strip().upper() for item in args.symbols.split(",") if item.strip()]:
            minute[symbol] = load_alpaca_sessions(args.alpaca_dir, symbol, calendar=args.calendar)
        sources.append("Alpaca SIP one-minute bars")
    if args.index_dir:
        for pair, label in _pairs(args.pairs).items():
            minute[f"{label} (index proxy)"] = load_index_minute_archives(args.index_dir, pair)
        sources.append("public index one-minute archives (diagnostic proxy, no volume)")
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
        sources.append("public adjusted daily bars")
    results = run(minute=minute, daily=daily, source="; ".join(sources), draws=args.draws)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(_strip(results), indent=1, sort_keys=True) + "\n",
                                      encoding="utf-8")
    (out / "report.md").write_text(render_report(results), encoding="utf-8")
    for label, rules in results["minute"].items():
        for name, block in rules.items():
            print(f"{label:24s} {name:26s} {block['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
