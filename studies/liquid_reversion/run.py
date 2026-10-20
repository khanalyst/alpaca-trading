"""Run the study: simulate the grid, select out of sample (or smoke-test), write the report.

Usage, from the repository root::

    python -I studies/liquid_reversion/run.py --data PATH --format alpaca|public --out DIR \\
        [--train N --test N --timeframe 1Min|5Min]

``python -m studies.liquid_reversion.run`` takes the same arguments. With fewer
sessions than one training plus one test block the run is a SMOKE run: no
selection, the middle of the grid, and a report that says it is not evidence.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    # ``python -I path/to/run.py`` puts neither the working directory nor the
    # script's folder on sys.path, so make the repository root importable.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import argparse  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
from collections.abc import Iterable, Iterator, Mapping, Sequence  # noqa: E402
from datetime import date, time  # noqa: E402
from statistics import median  # noqa: E402

from studies.liquid_reversion import bars as bar_io  # noqa: E402
from studies.liquid_reversion.costs import (FINRA_TAF_CAP, FINRA_TAF_PER_SHARE,  # noqa: E402
                                            SEC_FEE_PER_MILLION, cost_breakdown)
from studies.liquid_reversion.power import power_table  # noqa: E402
from studies.liquid_reversion.strategy import (GRID, RULES, SMOKE_PARAMS, GridResult, Params,  # noqa: E402
                                               Trade, simulate_grid)
from studies.liquid_reversion.walkforward import (MIN_TRAIN_TRADES, PERMUTATION_DRAWS,  # noqa: E402
                                                  PERMUTATION_SEED, TEST_SESSIONS, TRAIN_SESSIONS,
                                                  WalkForward, evaluate, walk_forward)

SCHEMA = "liquid-reversion-study.v1"
SYMBOLS = ("SPY", "QQQ", "IWM", "DIA", "VTI")
HYPOTHESIS = ("On the five most liquid US equity ETFs (SPY, QQQ, IWM, DIA, VTI), on days classified "
              "as range days from the first 30 minutes alone, a fade of a volatility-scaled deviation "
              "from the session VWAP, entered after 10:00 and targeting the VWAP, earns positive "
              "expectancy net of realistic per-symbol costs.")
SMOKE_BANNER = ("SMOKE RUN: NOT EVIDENCE. There are fewer sessions than one training block plus one "
                "test block, so no parameters were selected and nothing here was tested out of sample. "
                "It checks that the pipeline runs end to end on real bars; the numbers are far too few "
                "to support or reject the hypothesis.")
MAX_TRADES_IN_JSON = 1000


class DataStats:
    """Per-symbol coverage counters, filled while sessions stream past."""

    def __init__(self, interval_seconds: int, early_closes: Mapping[date, time]) -> None:
        self.interval_seconds = interval_seconds
        self.early_closes = early_closes
        self.symbols: dict[str, dict[str, object]] = {}

    def watch(self, symbol: str, sessions: Iterable[tuple[date, list[bar_io.Bar]]]
              ) -> Iterator[tuple[date, list[bar_io.Bar]]]:
        """Pass sessions through unchanged while counting bars, short sessions and zero volume."""
        stats = self.symbols.setdefault(symbol, {"sessions": 0, "bars": 0, "bars_per_session": [],
                                                 "zero_volume_bars": 0, "short_sessions": 0,
                                                 "first": None, "last": None, "prices": []})
        for day, rows in sessions:
            stats["sessions"] += 1
            stats["bars"] += len(rows)
            stats["bars_per_session"].append(len(rows))
            stats["zero_volume_bars"] += sum(1 for bar in rows if bar.volume == 0)
            if len(rows) < self._expected(day):
                stats["short_sessions"] += 1
            stats["first"] = stats["first"] or day.isoformat()
            stats["last"] = day.isoformat()
            stats["prices"].append(rows[-1].close)
            yield day, rows

    def _expected(self, day: date) -> int:
        return self._bars_until(self.early_closes.get(day, bar_io.REGULAR_CLOSE))

    def _bars_until(self, close: time) -> int:
        opening = bar_io.REGULAR_OPEN
        minutes = close.hour * 60 + close.minute - (opening.hour * 60 + opening.minute)
        return minutes * 60 // self.interval_seconds

    def summary(self) -> dict[str, dict[str, object]]:
        out: dict[str, dict[str, object]] = {}
        for symbol, stats in self.symbols.items():
            counts = stats["bars_per_session"]
            out[symbol] = {"sessions": stats["sessions"], "bars": stats["bars"],
                           "bars_per_session": {"min": min(counts), "median": median(counts),
                                                "max": max(counts)} if counts else None,
                           "expected_bars_per_full_session": self._bars_until(bar_io.REGULAR_CLOSE),
                           "short_sessions": stats["short_sessions"],
                           "zero_volume_bars": stats["zero_volume_bars"],
                           "first_session": stats["first"], "last_session": stats["last"],
                           "median_close": round(median(stats["prices"]), 4) if stats["prices"] else None}
        return out


def fingerprint(paths: Sequence[Path]) -> str:
    """SHA-256 over each input file's name and content, in name order."""
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.name):
        digest.update(path.name.encode("utf-8") + b"\0")
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    return digest.hexdigest()


def load_public(path: Path, symbols: Sequence[str], timeframe: str | None,
                early_closes: Mapping[date, time]) -> tuple[list, DataStats, dict[str, object]]:
    """Streams for each symbol from a public bar file, plus coverage counters."""
    loaded = bar_io.load_public_chart_json(path, symbols=symbols)
    if not loaded:
        raise SystemExit("the public bar file holds none of the requested symbols")
    interval = loaded[0].interval_seconds
    if timeframe is not None and bar_io.TIMEFRAME_SECONDS[timeframe] != interval:
        raise SystemExit(f"--timeframe {timeframe} does not match the file's {interval // 60}-minute bars")
    grouped = bar_io.session_bars(loaded, early_closes=early_closes)
    stats = DataStats(interval, early_closes)
    streams = [(symbol, stats.watch(symbol, grouped[symbol].items())) for symbol in symbols if symbol in grouped]
    info = {"format": "public", "path_name": path.name, "interval_seconds": interval,
            "files": 1, "sha256": fingerprint([path]),
            "source": ("public consolidated-tape chart bars as used by the 2026-10-15 review "
                       "(diagnostic only, not Alpaca SIP)")}
    return streams, stats, info


def load_alpaca(path: Path, symbols: Sequence[str], timeframe: str,
                early_closes: Mapping[date, time]) -> tuple[list, DataStats, dict[str, object]]:
    """Streams for each symbol from fetcher output (a directory or one month file)."""
    interval = bar_io.TIMEFRAME_SECONDS[timeframe]
    if path.is_dir():
        files = bar_io.alpaca_month_files(path, timeframe, symbols)
    else:
        bars = bar_io.load_alpaca_csv(path, interval_seconds=interval)
        files = {bars[0].symbol: [path]} if bars else {}
    if not files:
        raise SystemExit(f"no {timeframe} month files for {', '.join(symbols)} under {path}")
    stats = DataStats(interval, early_closes)
    streams = [(symbol, stats.watch(symbol, bar_io.iter_alpaca_sessions(
        files[symbol], interval_seconds=interval, early_closes=early_closes)))
        for symbol in symbols if symbol in files]
    every = [item for symbol in files for item in files[symbol]]
    info = {"format": "alpaca", "path_name": path.name, "interval_seconds": interval,
            "files": len(every), "partial_files": sum(item.name.endswith(".partial.csv.gz") for item in every),
            "sha256": fingerprint(every), "source": "Alpaca Market Data v2 historical bars, feed=sip, raw"}
    return streams, stats, info


def cost_table(prices: Mapping[str, float], spreads: Mapping[str, float]) -> dict[str, dict[str, float]]:
    """Round-trip components per symbol at its median close, with the default notional."""
    out: dict[str, dict[str, float]] = {}
    for symbol, price in prices.items():
        parts = cost_breakdown(price, notional=RULES.notional, spread_bps=spreads.get(symbol),
                               slippage_bps_per_side=RULES.slippage_bps_per_side)
        out[symbol] = {"price": round(price, 2), "spread_bps": round(parts.spread_bps, 4),
                       "slippage_bps": round(parts.slippage_bps, 4), "sec_fee_bps": round(parts.sec_fee_bps, 4),
                       "taf_bps": round(parts.taf_bps, 4), "round_trip_bps": round(parts.total_bps, 4)}
    return out


def run_study(result: GridResult, *, train: int, test: int, min_trades: int, draws: int,
              seed: int) -> dict[str, object]:
    """Choose the mode, select or fix parameters and evaluate the resulting record."""
    sessions = result.sessions()
    if len(sessions) < train + test:
        params = SMOKE_PARAMS
        known = result.known_sessions()
        evaluation = evaluate(result.trades[params], sessions=known, refusals=result.refusals[params],
                              draws=draws, seed=seed)
        return {"mode": "smoke", "out_of_sample": False, "banner": SMOKE_BANNER,
                "selection": {"kind": "none (smoke run)", "params": params.as_dict(),
                              "combinations_tried": 1, "sessions": len(sessions),
                              "sessions_with_known_day_type": len(known),
                              "sessions_needed_for_walk_forward": train + test},
                "evaluation": evaluation, "trades_record": result.trades[params],
                "deflated_sharpe": {"note": "not computed: no selection took place in a smoke run"}}
    wf = walk_forward(result.trades, result.refusals, sessions, grid=result.grid, train=train, test=test,
                      min_trades=min_trades)
    evaluation = evaluate(wf.trades, sessions=wf.test_sessions, refusals=wf.refusals, draws=draws, seed=seed)
    return {"mode": "walk_forward", "out_of_sample": True, "banner": None,
            "selection": _selection_summary(wf, len(result.grid), train, test, min_trades),
            "evaluation": evaluation, "trades_record": list(wf.trades),
            "deflated_sharpe": _deflated_summary(wf)}


def _selection_summary(wf: WalkForward, grid_size: int, train: int, test: int,
                       min_trades: int) -> dict[str, object]:
    rows = []
    chosen: dict[str, dict[str, int]] = {}
    for selection in wf.selections:
        window = selection.window
        tested = frozenset(window.test)
        params = selection.params.as_dict() if selection.params else None
        rows.append({"window": window.index, "train": [window.train[0].isoformat(), window.train[-1].isoformat()],
                     "test": [window.test[0].isoformat(), window.test[-1].isoformat()],
                     "test_sessions": len(window.test), "params": params,
                     "train_t": None if selection.train_t is None else round(selection.train_t, 3),
                     "train_trades": selection.train_trades, "eligible_combinations": selection.eligible,
                     "test_trades": sum(1 for trade in wf.trades if trade.session in tested),
                     "deflated_sharpe": None if selection.deflated_sharpe is None
                     else round(selection.deflated_sharpe, 4)})
        for name, value in (params or {}).items():
            counts = chosen.setdefault(name, {})
            counts[str(value)] = counts.get(str(value), 0) + 1
    return {"kind": "highest session-clustered training t with at least "
                    f"{min_trades} training trades", "train_sessions": train, "test_sessions": test,
            "windows": len(wf.windows), "grid_size": grid_size,
            "combinations_tried": grid_size * len(wf.windows),
            "windows_without_selection": sum(1 for s in wf.selections if s.params is None),
            "per_window": rows, "parameter_counts": chosen}


def _deflated_summary(wf: WalkForward) -> dict[str, object]:
    values = [s.deflated_sharpe for s in wf.selections if s.deflated_sharpe is not None]
    return {"method": "Bailey and Lopez de Prado (2014), per training window, trials = grid points "
                      "with a defined training Sharpe ratio; correlated trials make it conservative",
            "per_window": [None if s.deflated_sharpe is None else round(s.deflated_sharpe, 4)
                           for s in wf.selections],
            "median": round(median(values), 4) if values else None,
            "note": "the out-of-sample record, not this number, is the main protection against selection"}


def _fmt(value: object, places: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        if math.isinf(value):
            return "inf"
        return f"{value:.{places}f}"
    return str(value)


def _table(header: Sequence[str], rows: Iterable[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(" --- " for _ in header) + "|"]
    lines.extend("| " + " | ".join(_fmt(cell) for cell in row) + " |" for row in rows)
    return lines


def render_report(results: Mapping[str, object]) -> str:
    """The Markdown report (British English)."""
    data = results["data"]
    evaluation = results["evaluation"]
    selection = results["selection"]
    smoke = results["mode"] == "smoke"
    lines = ["# Liquid-ETF VWAP reversion study: " + ("smoke run" if smoke else "walk-forward result"), ""]
    if smoke:
        lines += [f"> **{results['banner']}**", ""]
    lines += ["## Hypothesis", "", f"> {results['hypothesis']}", ""]
    lines += ["## Data", "",
              f"- Source: {data['source']}.",
              f"- Input: `{data['path_name']}` ({data['files']} file{'' if data['files'] == 1 else 's'}), "
              f"{data['interval_seconds'] // 60}-minute bars, "
              f"SHA-256 `{data['sha256'][:16]}...`.",
              f"- Sessions: {data['sessions']} in total, "
              f"{data['first_session']} to {data['last_session']}; regular hours only (09:30 to 16:00 New York).",
              f"- Early closes supplied: {data['early_closes']} (sessions with an early close feed the history "
              "but are not traded).", ""]
    coverage = data["coverage"]
    lines += _table(["symbol", "sessions", "bars", "min bars", "max bars", "short sessions", "zero-volume bars",
                     "median close"],
                    [[symbol, row["sessions"], row["bars"], (row["bars_per_session"] or {}).get("min"),
                      (row["bars_per_session"] or {}).get("max"), row["short_sessions"], row["zero_volume_bars"],
                      row["median_close"]] for symbol, row in coverage.items()])
    lines += ["", "Zero-volume bars add nothing to VWAP; a short session simply has fewer candidate bars.", ""]
    lines += ["## Rules (fixed, preregistered)", ""]
    lines += [f"- {rule}" for rule in results["rules"]["text"]]
    lines += ["", "## Costs", "", results["cost_model"]["text"], ""]
    lines += _table(["symbol", "median close", "spread", "slippage", "SEC fee", "FINRA TAF", "round trip (bps)"],
                    [[symbol, row["price"], *(f"{row[key]:.4f}" for key in ("spread_bps", "slippage_bps",
                                                                            "sec_fee_bps", "taf_bps",
                                                                            "round_trip_bps"))]
                     for symbol, row in results["cost_model"]["per_symbol_at_median_close"].items()])
    lines += ["", "## Parameters", ""]
    if smoke:
        params = selection["params"]
        lines += ["No selection: one fixed grid point, the middle of the grid "
                  f"(k_drive {params['k_drive']}, k_gap {params['k_gap']}, k_entry {params['k_entry']}, "
                  f"cost_multiple {params['cost_multiple']}, stop_mult {params['stop_mult']}). "
                  f"Combinations tried: {selection['combinations_tried']}. A walk-forward needs "
                  f"{selection['sessions_needed_for_walk_forward']} sessions; this input has "
                  f"{selection['sessions']}, of which {selection['sessions_with_known_day_type']} have a "
                  "known day type.", ""]
    else:
        lines += [f"Selection rule: {selection['kind']}. Grid of {selection['grid_size']} combinations "
                  f"evaluated in each of {selection['windows']} windows ({selection['combinations_tried']} "
                  f"evaluations in all); training {selection['train_sessions']} sessions, testing "
                  f"{selection['test_sessions']}, step equal to the test length. Windows without an "
                  f"eligible combination: {selection['windows_without_selection']}.", ""]
        lines += _table(["window", "train", "test", "k_drive", "k_gap", "k_entry", "cost x", "stop x",
                         "train t", "train trades", "test trades", "DSR"],
                        [[row["window"], f"{row['train'][0]} to {row['train'][1]}",
                          f"{row['test'][0]} to {row['test'][1]}",
                          *((row["params"] or {}).get(name) for name in
                            ("k_drive", "k_gap", "k_entry", "cost_multiple", "stop_mult")),
                          _fmt(row["train_t"], 3), row["train_trades"], row["test_trades"],
                          _fmt(row["deflated_sharpe"], 3)]
                         for row in selection["per_window"]])
        lines += [""]
    title = "Results (smoke sample, in sample)" if smoke else "Results (out of sample)"
    net = evaluation["net_bps_per_trade"]
    session = evaluation["session_level"]
    lines += [f"## {title}", ""]
    lines += _table(["measure", "value"], [
        ["trades", evaluation["trades"]],
        ["sessions available / traded", f"{evaluation['sessions_available']} / {evaluation['sessions_traded']}"],
        ["trades per traded session", evaluation["trades_per_traded_session"]],
        ["net bps per trade: mean / median", f"{_fmt(net['mean'])} / {_fmt(net['median'])}"],
        ["gross bps per trade (mean)", evaluation["gross_bps_per_trade_mean"]],
        ["cost bps per trade (mean)", evaluation["cost_bps_per_trade_mean"]],
        ["session-level net bps: mean / sd", f"{_fmt(session['mean'])} / {_fmt(session['sd'])}"],
        ["session-clustered t", _fmt(evaluation["clustered_t"], 3)],
        ["sign-flip p (one-sided, sessions, "
         f"{evaluation['permutation']['draws']:,} draws, seed {evaluation['permutation']['seed']})",
         _fmt(evaluation["sign_flip_p_one_sided"], 4)],
        ["win rate", evaluation["win_rate"]],
        ["sides", ", ".join(f"{k} {v}" for k, v in evaluation["sides"].items()) or "none"],
        ["exit mix", ", ".join(f"{k} {v}" for k, v in evaluation["exit_mix"].items()) or "none"],
        ["refused entries (session consumed)",
         ", ".join(f"{k} {v}" for k, v in evaluation["refused_entries"].items()) or "none"],
    ])
    lines += ["", "### By symbol", ""]
    lines += _table(["symbol", "trades", "sessions", "net mean", "gross mean", "win rate", "t"],
                    [[name, row["trades"], row["sessions"], row["net_bps_mean"], row["gross_bps_mean"],
                      row["win_rate"], _fmt(row["clustered_t"], 3)] for name, row in evaluation["by_symbol"].items()])
    lines += ["", "### By year", ""]
    lines += _table(["year", "trades", "sessions", "net mean", "gross mean", "win rate", "t"],
                    [[name, row["trades"], row["sessions"], row["net_bps_mean"], row["gross_bps_mean"],
                      row["win_rate"], _fmt(row["clustered_t"], 3)] for name, row in evaluation["by_year"].items()])
    lines += ["", "### Cost stress", "",
              "The same trades with every cost component multiplied by the stress factor.", ""]
    lines += _table(["stress", "net bps per trade", "session mean", "t", "sign-flip p"],
                    [[name, row["net_bps_per_trade_mean"], row["session_mean"], _fmt(row["clustered_t"], 3),
                      _fmt(row["sign_flip_p_one_sided"], 4)] for name, row in evaluation["cost_stress"].items()])
    lines += ["", "### Power", ""]
    power = evaluation["power"]
    if "sd_bps" in power:
        lines += [f"Measured here: session-level sd {_fmt(power['sd_bps'])} bps, "
                  f"{_fmt(power['trades_per_session'])} trades per traded session, "
                  f"{_fmt(power['traded_share'])} of available sessions traded. Assumed: one-sided "
                  "alpha 0.05 and 80% power, normal approximation.", ""]
        lines += [f"- Minimum detectable effect for the {session['count']} traded sessions here: "
                  f"{_fmt(power['mde_per_trade_bps'])} bps per trade."]
        if power.get("traded_sessions_needed") is not None:
            lines += [f"- Sessions needed to detect the observed {_fmt(power['observed_effect_bps_per_trade'])} "
                      f"bps per trade: {power['traded_sessions_needed']} traded "
                      f"({power['calendar_sessions_needed']} calendar) sessions."]
        else:
            lines += [f"- Observed effect {_fmt(power.get('observed_effect_bps_per_trade'))} bps per trade: "
                      "not positive, so no sample size would detect it."]
        table = results.get("power_table")
        if table:
            lines += ["", "Sessions needed for a given true effect per trade, at the sd and trade rate measured "
                      "here:", ""]
            lines += _table(["effect (bps per trade)", "traded sessions", "calendar sessions"],
                            [[row["effect_bps_per_trade"], row["traded_sessions"], row["calendar_sessions"]]
                             for row in table["rows"]])
            mde = table["mde"]
            lines += ["", f"MDE with {mde['calendar_sessions']:,} calendar sessions (about ten years): "
                      f"{_fmt(mde['per_trade_bps'])} bps per trade ({_fmt(mde['session_bps'])} bps per traded "
                      "session)."]
    else:
        lines += [power.get("note", "not available")]
    lines += ["", "### Selection bias", ""]
    deflated = results["deflated_sharpe"]
    if "median" in deflated:
        lines += [f"Deflated Sharpe ratio of the in-sample selection ({deflated['method']}): median "
                  f"{_fmt(deflated['median'], 3)} across windows. {deflated['note'].capitalize()}."]
    else:
        lines += [deflated["note"].capitalize() + "."]
    rule = results["decision"]
    lines += ["", "## Preregistered decision rule", "",
              "Deploy to paper only if the out-of-sample session-level mean net bps is above zero with "
              "sign-flip p < 0.05 AND the 2x cost stress stays above zero.", ""]
    if smoke:
        lines += ["Not applicable: a smoke run is not an out-of-sample test. For reference only, the checks "
                  "on this sample would read: " + _checks(rule["checks"]) + "."]
    else:
        lines += _table(["check", "passed"], [[CHECK_LABELS.get(name, name), passed]
                                              for name, passed in rule["checks"].items()])
        lines += ["", f"**Decision: {'deploy to paper' if rule['deploy_to_paper'] else 'do not deploy'}.**"]
    lines += [""]
    return "\n".join(lines)


CHECK_LABELS = {"session_mean_above_zero": "session-level mean net bps above zero",
                "sign_flip_p_below_0_05": "sign-flip p below 0.05",
                "session_mean_at_2x_cost_above_zero": "session-level mean above zero at 2x cost"}


def _checks(checks: Mapping[str, bool]) -> str:
    return "; ".join(f"{CHECK_LABELS.get(name, name)}: {'yes' if passed else 'no'}" for name, passed in checks.items())


RULE_TEXT = (
    "Session VWAP: cumulative typical price x volume over volume from 09:30, completed bars only.",
    "Volatility: 1.4826 x median absolute bar log return over the current session's completed bars and the "
    "prior 5 sessions, in bps per minute (five-minute returns divided by the square root of 5).",
    "Score: z = (close - VWAP) / VWAP x 1e4 / (sigma x sqrt(60)).",
    "Day type at 10:00: drive and gap, each over its median for the previous 20 sessions (at least 10 "
    "needed); range day if both ratios are below k_drive and k_gap.",
    "Signal on a completed bar ending in [10:00, 15:00): range day, |z| >= k_entry, and the move to VWAP "
    "at least cost_multiple x round-trip cost. Short above VWAP, long below.",
    "One trade per symbol-session: the first qualifying signal consumes it, even if the entry is refused.",
    "Entry at the next bar's open, only if that bar starts before 15:00 and the open has not already "
    "crossed the frozen target.",
    "Target: VWAP frozen at the signal. Stop: entry +/- stop_mult x |entry - target|.",
    "Exit on bar highs and lows from the entry bar: stop first on a tie, a gap beyond a level fills at the "
    "open; time stop at the close of the bar ending 60 minutes after entry; flat at the bar ending 15:50.",
)


def _cost_text(spreads: Mapping[str, float]) -> str:
    measured = (", ".join(f"{symbol} {value:g}" for symbol, value in sorted(spreads.items()))
                if spreads else "none supplied, so one tick is used")
    return (f"Round trip in bps of entry notional (${RULES.notional:,.0f} per trade, Alpaca charges no "
            f"commission): one full quoted spread, max(one tick, measured spread; measured: {measured}); "
            f"slippage {RULES.slippage_bps_per_side:g} bps per side; SEC Section 31 fee on the sell leg only "
            f"(${SEC_FEE_PER_MILLION:.2f} per million); FINRA TAF ${FINRA_TAF_PER_SHARE} per share sold, "
            f"capped at ${FINRA_TAF_CAP} per trade. Cost stress multiplies the whole round trip.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Liquid-ETF VWAP reversion study (diagnostic research).")
    parser.add_argument("--data", required=True, type=Path, help="fetcher directory or file, or public JSON")
    parser.add_argument("--format", required=True, choices=("alpaca", "public"))
    parser.add_argument("--out", required=True, type=Path, help="directory for report.md and results.json")
    parser.add_argument("--train", type=int, default=TRAIN_SESSIONS)
    parser.add_argument("--test", type=int, default=TEST_SESSIONS)
    parser.add_argument("--timeframe", choices=tuple(bar_io.TIMEFRAME_SECONDS), default=None,
                        help="bar size; Alpaca data defaults to 1Min, public files declare their own")
    parser.add_argument("--symbols", default=",".join(SYMBOLS))
    parser.add_argument("--spreads", type=Path, default=None,
                        help="optional JSON object of measured quoted spreads in bps per symbol")
    parser.add_argument("--calendar", type=Path, default=None,
                        help="optional calendar.json from the fetcher; early closes are not traded")
    parser.add_argument("--min-trades", type=int, default=MIN_TRAIN_TRADES)
    parser.add_argument("--permutations", type=int, default=PERMUTATION_DRAWS)
    parser.add_argument("--seed", type=int, default=PERMUTATION_SEED)
    return parser


def _read_spreads(path: Path | None) -> dict[str, float]:
    if path is None:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit("--spreads must hold a JSON object of symbol to bps")
    out: dict[str, float] = {}
    for symbol, value in raw.items():
        if (not isinstance(symbol, str) or isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0):
            raise SystemExit("--spreads values must be finite non-negative numbers")
        out[symbol] = float(value)
    return out


def _json_ready(value: object) -> object:
    if isinstance(value, float):
        return "inf" if math.isinf(value) else (None if math.isnan(value) else value)
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    symbols = tuple(item.strip().upper() for item in args.symbols.split(",") if item.strip())
    if not symbols or any(not bar_io.SYMBOL_PATTERN.fullmatch(symbol) for symbol in symbols):
        raise SystemExit("--symbols must be a comma-separated list of tickers")
    if args.train <= 0 or args.test <= 0 or args.min_trades < 1 or args.permutations < 1:
        raise SystemExit("--train, --test, --min-trades and --permutations must be positive")
    early_closes = bar_io.load_early_closes(args.calendar) if args.calendar else {}
    spreads = _read_spreads(args.spreads)
    if args.format == "public":
        streams, stats, info = load_public(args.data, symbols, args.timeframe, early_closes)
    else:
        streams, stats, info = load_alpaca(args.data, symbols, args.timeframe or "1Min", early_closes)
    result = simulate_grid(streams, GRID, rules=RULES, spreads=spreads,
                           skip_sessions=frozenset(early_closes))
    study = run_study(result, train=args.train, test=args.test, min_trades=args.min_trades,
                      draws=args.permutations, seed=args.seed)
    coverage = stats.summary()
    sessions = result.sessions()
    info.update({"symbols": [symbol for symbol in symbols if symbol in coverage],
                 "sessions": len(sessions),
                 "first_session": sessions[0].isoformat() if sessions else None,
                 "last_session": sessions[-1].isoformat() if sessions else None,
                 "early_closes": len(early_closes), "coverage": coverage})
    prices = {symbol: row["median_close"] for symbol, row in coverage.items() if row["median_close"]}
    trades: list[Trade] = study.pop("trades_record")
    evaluation = study["evaluation"]
    power = evaluation["power"]
    table = None
    if power.get("sd_bps") and power.get("trades_per_session") and power.get("traded_share"):
        table = power_table(sd_bps=power["sd_bps"], trades_per_session=power["trades_per_session"],
                            traded_share=power["traded_share"])
    results: dict[str, object] = {
        "schema": SCHEMA, **{key: study[key] for key in ("mode", "out_of_sample", "banner")},
        "hypothesis": HYPOTHESIS, "data": info,
        "rules": {"text": list(RULE_TEXT), "horizon_minutes": RULES.horizon_minutes,
                  "entry_window": [RULES.entry_start.isoformat("minutes"), RULES.entry_end.isoformat("minutes")],
                  "flat_time": RULES.flat_time.isoformat("minutes"), "sigma_sessions": RULES.sigma_sessions,
                  "day_type_lookback": RULES.lookback, "day_type_min_history": RULES.min_history},
        "cost_model": {"text": _cost_text(spreads), "notional": RULES.notional,
                       "slippage_bps_per_side": RULES.slippage_bps_per_side, "measured_spreads_bps": spreads,
                       "per_symbol_at_median_close": cost_table(prices, spreads)},
        "grid": {"size": len(GRID), "values": {
            "k_drive": [0.6, 0.8, 1.0], "k_gap": [1.0, 1.5, "inf"], "k_entry": [1.0, 1.5, 2.0],
            "cost_multiple": [1.5, 2.0], "stop_mult": [1.0, 1.5]}},
        "selection": study["selection"], "evaluation": evaluation, "power_table": table,
        "deflated_sharpe": study["deflated_sharpe"], "decision": evaluation["decision"],
        "trades": ([trade.as_dict() for trade in trades] if len(trades) <= MAX_TRADES_IN_JSON
                   else f"omitted: {len(trades)} trades exceed the {MAX_TRADES_IN_JSON}-trade limit"),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "results.json").write_text(json.dumps(_json_ready(results), indent=1) + "\n", encoding="utf-8")
    (args.out / "report.md").write_text(render_report(results), encoding="utf-8")
    mode = "SMOKE (not evidence)" if study["mode"] == "smoke" else "walk-forward"
    print(f"{mode}: {evaluation['trades']} trades, net {_fmt(evaluation['net_bps_per_trade']['mean'])} bps per "
          f"trade, wrote {args.out / 'report.md'} and {args.out / 'results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
