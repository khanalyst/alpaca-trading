"""Turn the live journal into forward evidence for every registered strategy.

The trading loop records, every cycle, what each registered contract WOULD
have done - not just the one holding capital. This script resolves those
shadow decisions against real subsequent bars and reports each strategy's
forward expectancy.

Why this exists: a strategy with no capital otherwise accumulates no
out-of-sample evidence at all, so testing all registered strategies would take
longer than testing one. Shadow evaluation costs nothing and runs them
concurrently on identical data.

Outcomes are resolved with ``edge_lab.simulate`` - the same simulator the
backtests use, with the same stop-fills-first ambiguity rule. That is the
whole point: a forward number computed by different code than the backtest
number is not comparable to it, and comparing them is the job.

It also answers a question offline research cannot: the active strategy's
contract is shadowed too, so the gap between "the contract fired" and "the
model took it" is directly observable rather than merely bounded.

    python research/export_live.py --mode demo --data runtime/research/data
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "research"))

from edge_lab import COST_SCENARIOS, load_dataset, simulate  # noqa: E402
from agent.registry import spec_for  # noqa: E402

BAR_MS = 900_000


def read_journal(db_path: Path) -> tuple[pd.DataFrame, pd.DataFrame,
                                         pd.DataFrame]:
    """Return (strategy decisions, strategy summaries, real trades).

    New journals use event names whose schema is explicit. Legacy
    ``shadow_decision`` rows are accepted only when they have strategy
    attribution and either no variant attribution or the legacy ``live``
    sentinel. This deliberately excludes the parameter-variant rows that
    historically shared the same event name.
    """
    if not db_path.exists():
        raise SystemExit(
            f"no journal at {db_path}. Run the agent first, or pass --mode "
            f"live / --journal explicitly.")
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        event_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(events)")
        }
        variant_expr = (
            "variant_id" if "variant_id" in event_columns
            else "NULL AS variant_id"
        )
        events = pd.read_sql_query(
            "SELECT ts, kind, payload, strategy_id, strategy_version, "
            f"{variant_expr} FROM events WHERE kind IN ("
            "'strategy_shadow_decision','strategy_shadow_summary',"
            "'shadow_decision','shadow_summary')",
            conn)
        try:
            trades = pd.read_sql_query(
                "SELECT ts, symbol, side, action, strategy_id, setup_type, "
                "pnl_pct, realized_pnl_usd FROM trades", conn)
        except Exception:                                  # noqa: BLE001
            trades = pd.DataFrame()

    def expand(kinds: set[str], *, decisions: bool = False) -> pd.DataFrame:
        rows = []
        selected = events[events["kind"].isin(kinds)]
        for row in selected.itertuples(index=False):
            if decisions and row.kind == "shadow_decision":
                variant_id = ("" if pd.isna(row.variant_id)
                              else str(row.variant_id).strip())
                if (variant_id not in {"", "live"}
                        or pd.isna(row.strategy_id)):
                    continue
            try:
                payload = json.loads(row.payload)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            payload["ts"] = row.ts
            payload["strategy_id"] = row.strategy_id
            payload["strategy_version"] = row.strategy_version
            rows.append(payload)
        return pd.DataFrame(rows)

    decisions = expand(
        {"strategy_shadow_decision", "shadow_decision"}, decisions=True)
    summaries = expand({"strategy_shadow_summary", "shadow_summary"})
    return decisions, summaries, trades


def derive_levels(row, min_stop_atr: float, buffer_atr: float,
                  reward_risk: float) -> tuple[float, float]:
    """Same stop/target rule the contract uses, applied offline.

    Kept out of the engine deliberately. The engine records the raw evidence
    it saw; the exit policy is a research choice, so deriving levels here
    means an exit rule can be re-examined against recorded signals without
    needing the agent to have guessed right at the time.
    """
    atr = float(row.get("atr_1h_pct") or 0.0)
    if atr <= 0:
        return float("nan"), float("nan")
    structure = row.get("swing_low_pct" if row["direction"] == "long"
                        else "swing_high_pct")
    try:
        structure = float(structure)
    except (TypeError, ValueError):
        structure = 0.0
    stop = max(atr * min_stop_atr, structure + atr * buffer_atr)
    return round(stop, 6), round(stop * reward_risk, 6)


def _registered_horizon_bars(strategy_id: str) -> int:
    """Return the immutable forward model's horizon on the 15-minute grid."""
    hours = float(spec_for(strategy_id).forward_model.horizon_hours)
    return int(math.ceil(hours * 3_600_000 / BAR_MS))


def _registered_horizons(strategy_ids) -> dict[str, int]:
    """Describe known, scoreable strategies without trusting journal IDs."""
    horizons = {}
    for strategy_id in sorted({str(item) for item in strategy_ids
                               if not pd.isna(item)}):
        try:
            if spec_for(strategy_id).forward_model_ready:
                horizons[strategy_id] = _registered_horizon_bars(strategy_id)
        except Exception:  # stale or foreign journal strategy IDs are ignored
            continue
    return horizons


def resolve(decisions: pd.DataFrame, frames: dict, costs,
            max_hold_bars: int | None,
            min_stop_atr: float, buffer_atr: float,
            reward_risk: float) -> pd.DataFrame:
    """Resolve only decisions with their complete registered outcome window.

    ``max_hold_bars`` is retained as a unit-test hook for small synthetic
    frames. Production callers pass ``None`` so every strategy uses the
    horizon bound to its registered forward outcome model.
    """
    resolved = []
    unresolved = defaultdict(int)
    for (strategy_id, symbol, direction), group in decisions.groupby(
            ["strategy_id", "symbol", "direction"]):
        try:
            spec = spec_for(str(strategy_id))
            scoring_ready = spec.forward_model_ready
            strategy_hold_bars = (
                int(max_hold_bars) if max_hold_bars is not None
                else _registered_horizon_bars(str(strategy_id)))
        except Exception:                                  # noqa: BLE001
            scoring_ready = False
            strategy_hold_bars = 0
        if not scoring_ready:
            unresolved[
                f"{strategy_id}: no validated forward outcome model"] += len(
                    group)
            continue
        if strategy_hold_bars <= 0:
            unresolved[f"{strategy_id}: invalid outcome horizon"] += len(group)
            continue
        frame = frames.get(symbol)
        if frame is None:
            unresolved[f"{symbol}: no bar data"] += len(group)
            continue
        idx, stops, takes = [], [], []
        for row in group.to_dict("records"):
            # Signal timestamp is the completed bar the contract fired on.
            signal_ts = row.get("signal_ts")
            try:
                signal_ts = int(float(signal_ts))
            except (TypeError, ValueError):
                signal_ts = int(float(row["ts"]) * 1000)
            position = int(np.searchsorted(frame.ts, signal_ts, side="right")) - 1
            # Needs at least one bar after the signal to enter on, and enough
            # room afterwards for the hold window to have finished.
            if position < 0 or position >= len(frame.ts) - 2:
                unresolved[f"{symbol}: signal outside downloaded bars"] += 1
                continue
            entry_position = position + 1
            if entry_position + strategy_hold_bars > len(frame.ts):
                unresolved[
                    f"{strategy_id}/{symbol}: incomplete "
                    f"{strategy_hold_bars}-bar outcome window"] += 1
                continue
            stop, take = derive_levels(row, min_stop_atr, buffer_atr,
                                       reward_risk)
            if not np.isfinite(stop) or stop <= 0:
                unresolved[f"{symbol}: no usable stop"] += 1
                continue
            idx.append(position)
            stops.append(stop)
            takes.append(take)
        if not idx:
            continue
        chunk = simulate(frame, np.array(idx), direction,
                         np.array(stops), np.array(takes), costs,
                         strategy_hold_bars)
        if chunk.empty:
            continue
        chunk["strategy_id"] = strategy_id
        chunk["symbol"] = symbol
        chunk["direction"] = direction
        resolved.append(chunk)
    out = (pd.concat(resolved, ignore_index=True) if resolved
           else pd.DataFrame())
    out.attrs["unresolved"] = dict(unresolved)
    return out


def summarise(resolved: pd.DataFrame, summaries: pd.DataFrame,
              trades: pd.DataFrame) -> dict:
    per_strategy = {}
    strategy_ids = set()
    if not resolved.empty:
        strategy_ids |= set(resolved["strategy_id"].dropna().unique())
    if not summaries.empty:
        strategy_ids |= set(summaries["strategy_id"].dropna().unique())

    for strategy_id in sorted(strategy_ids):
        entry: dict = {"strategy_id": strategy_id}
        if not summaries.empty:
            rows = summaries[summaries["strategy_id"] == strategy_id]
            entry["cycles_observed"] = int(len(rows))
            entry["signals_fired"] = int(
                pd.to_numeric(rows.get("signals"), errors="coerce")
                .fillna(0).sum())
            entry["was_active"] = bool(
                rows.get("is_active", pd.Series(dtype=bool)).any())
        if not resolved.empty:
            rows = resolved[resolved["strategy_id"] == strategy_id]
            if len(rows):
                net = rows["net_pct"].to_numpy(float)
                r = rows["r_multiple"].to_numpy(float)
                entry.update({
                    "resolved_trades": int(len(rows)),
                    "forward_expectancy_pct": float(np.nanmean(net)),
                    "forward_expectancy_r": float(np.nanmean(r)),
                    "forward_win_rate_pct": float((net > 0).mean() * 100),
                    "forward_sd_r": float(np.nanstd(r, ddof=1))
                    if len(rows) > 1 else None,
                })
        per_strategy[strategy_id] = entry

    # What the contract fired vs what the account actually did. A large gap
    # is the LLM layer's contribution, in whichever direction it runs.
    taken = {}
    if not trades.empty and "action" in trades:
        opens = trades[trades["action"].astype(str).str.lower() == "open"]
        for strategy_id, rows in opens.groupby("strategy_id"):
            taken[str(strategy_id)] = int(len(rows))
    for strategy_id, entry in per_strategy.items():
        entry["positions_actually_opened"] = taken.get(strategy_id, 0)
        fired = entry.get("signals_fired") or 0
        entry["selectivity_pct"] = (
            round(entry["positions_actually_opened"] / fired * 100, 2)
            if fired else None)
    return per_strategy


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="demo",
                        choices=["demo", "live", "test"])
    parser.add_argument("--journal", type=Path,
                        help="explicit path to journal.db")
    parser.add_argument("--data", type=Path,
                        default=REPO / "runtime" / "research" / "data",
                        help="bar data used to resolve shadow outcomes")
    parser.add_argument("--out", type=Path,
                        default=REPO / "runtime" / "research"
                        / "forward_evidence.json")
    parser.add_argument("--cost", default="base")
    parser.add_argument("--min-stop-atr", type=float, default=1.0)
    parser.add_argument("--structure-buffer-atr", type=float, default=0.15)
    parser.add_argument("--reward-risk", type=float, default=3.0)
    parser.add_argument("--min-bars", type=int, default=1000)
    args = parser.parse_args()

    db_path = args.journal or (
        REPO / "runtime" / args.mode / "journal.db")
    decisions, summaries, trades = read_journal(db_path)
    if decisions.empty and summaries.empty:
        print(f"No shadow records in {db_path} yet. The agent writes them "
              f"once per cycle; give it a cycle or two.", file=sys.stderr)

    frames = load_dataset(args.data, min_bars=args.min_bars)
    resolved = pd.DataFrame()
    if not decisions.empty and frames:
        resolved = resolve(decisions, frames, COST_SCENARIOS[args.cost],
                           None, args.min_stop_atr,
                           args.structure_buffer_atr, args.reward_risk)
    elif not frames:
        print(f"No bar data in {args.data}; shadow outcomes cannot be "
              f"resolved. Download history first.", file=sys.stderr)

    payload = {
        "journal": str(db_path),
        "data_root": str(args.data),
        "cost_scenario": args.cost,
        "exit_rule": {
            "min_stop_atr": args.min_stop_atr,
            "structure_buffer_atr": args.structure_buffer_atr,
            "reward_risk": args.reward_risk,
            "holding_model": "registered_per_strategy",
            "max_hold_bars_by_strategy": _registered_horizons(
                decisions.get("strategy_id", pd.Series(dtype=str))),
        },
        "shadow_decisions_recorded": int(len(decisions)),
        "unresolved": resolved.attrs.get("unresolved", {}),
        "strategies": summarise(resolved, summaries, trades),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    print(f"shadow decisions recorded : {payload['shadow_decisions_recorded']}")
    for strategy_id, entry in sorted(payload["strategies"].items()):
        print(f"  {strategy_id:<16} "
              f"cycles={entry.get('cycles_observed', 0):<6} "
              f"fired={entry.get('signals_fired', 0):<6} "
              f"resolved={entry.get('resolved_trades', 0):<6} "
              + (f"fwd={entry['forward_expectancy_pct']:+.4f}%"
                 if "forward_expectancy_pct" in entry else "fwd=n/a"))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
