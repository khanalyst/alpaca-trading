"""Account-level simulation driven by the repository's real RiskEngine.

The event study measures the signal. This measures the *agent*: concurrent
position limits, exposure and beta caps, planned-risk budget, per-symbol and
per-setup cooldowns, failed-thesis re-entry rules, the daily loss stop and
the drawdown self-kill - all evaluated against the point-in-time universe.

No LLM is replayed. Every admissible setup is taken in priority order, which
is the *most active* version of the agent. A selector can only subtract
trades from this stream, so this is the ceiling on trade count, not on
profitability.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research.edge_lab import (  # noqa: E402
    BAR_MS, Contract, Costs, derive_levels, evidence_masks, research_gate,
)

INITIAL_EQUITY = 10_000.0


def _load_cfg() -> dict:
    from agent.config import validate_config
    repo = Path(__file__).resolve().parents[1]
    return validate_config(yaml.safe_load((repo / "config.yaml").read_text()))


def build_candidate_index(frames, membership, contract: Contract,
                          exit_policy: str, setups) -> dict:
    """Map bar timestamp -> list of admissible setups at that bar."""
    by_ts = defaultdict(list)
    for symbol, frame in frames.items():
        df = frame.df
        masks = evidence_masks(df, contract)
        in_universe = membership[symbol] & (
            df["trend_4h"].to_numpy(object) != None)  # noqa: E711
        for setup in setups:
            for direction in ("long", "short"):
                mask = masks[f"{setup}_{direction}"] & in_universe
                ext = masks[f"extension_{direction}"]
                mask &= ~np.isnan(ext) & (
                    ext <= contract.hard_max_entry_extension_atr)
                mask &= research_gate(df, contract, direction)
                stop, take = derive_levels(df, contract, direction, exit_policy)
                mask &= ~np.isnan(stop) & (stop >= contract.min_stop_pct) \
                    & (stop <= contract.max_stop_pct) & (take <= 50.0)
                for index in np.where(mask)[0]:
                    by_ts[int(frame.ts[index])].append({
                        "symbol": symbol, "direction": direction,
                        "setup_type": setup, "index": int(index),
                        "stop_pct": float(stop[index]),
                        "take_pct": float(take[index]),
                        "signal_1h_ts": float(df["signal_1h_ts"].iloc[index]),
                        "relative_volume": float(
                            np.nan_to_num(df["relative_volume_1h"].iloc[index])),
                        "momentum_abs": abs(float(
                            np.nan_to_num(df["mom_1h_pct"].iloc[index]))),
                        "setup_key": f"{symbol}|{direction}|{setup}",
                    })
    return by_ts


def run_portfolio(frames, membership, contract: Contract, costs: Costs,
                  *, exit_policy: str = "fixed_rr",
                  setups=("trend_continuation", "range_breakout"),
                  enforce_kill: bool = True,
                  cfg: dict | None = None) -> dict:
    from agent.risk import RiskEngine

    cfg = cfg or _load_cfg()
    risk_cfg = cfg["risk"]
    engine = RiskEngine(cfg)
    by_ts = build_candidate_index(
        frames, membership, contract, exit_policy, setups)

    # A single ordered timeline across every instrument.
    timeline = sorted({int(t) for frame in frames.values() for t in frame.ts})
    bar_pos = {
        symbol: {int(t): i for i, t in enumerate(frame.ts)}
        for symbol, frame in frames.items()
    }

    cash = INITIAL_EQUITY
    positions: dict[str, dict] = {}
    trades: list[dict] = []
    equity_curve: list[dict] = []
    rejections: dict[str, int] = defaultdict(int)
    symbol_cooldown: dict[str, int] = defaultdict(int)
    setup_cooldown: dict[str, int] = defaultdict(int)
    loss_memory: dict[str, dict] = {}
    high_water = INITIAL_EQUITY
    day = None
    day_start_equity = INITIAL_EQUITY
    day_stopped = False
    killed = False
    kill_ts = None
    pending: list[dict] = []

    def price_at(symbol: str, ts: int, field: str) -> float | None:
        pos = bar_pos[symbol].get(ts)
        if pos is None:
            return None
        return float(frames[symbol].df[field].iloc[pos])

    def mark_equity(ts: int, field: str) -> float:
        total = cash
        for symbol, position in positions.items():
            price = price_at(symbol, ts, field)
            if price is None:
                price = position["last_mark"]
            else:
                position["last_mark"] = price
            sign = 1.0 if position["direction"] == "long" else -1.0
            total += (price - position["entry_price"]) * position["qty"] * sign
        return total

    for ts in timeline:
        today = pd.Timestamp(ts, unit="ms", tz="UTC").date().isoformat()
        if today != day:
            day = today
            day_start_equity = mark_equity(ts, "open")
            day_stopped = False

        # ---- 1. execute entries queued by the previous bar's signals
        if pending and not killed and not day_stopped:
            pending.sort(key=lambda row: (
                0 if row["setup_type"] == "range_breakout" else 1,
                -row["relative_volume"], -row["momentum_abs"], row["symbol"]))
            seen = set()
            for candidate in pending:
                symbol = candidate["symbol"]
                if symbol in seen:
                    rejections["same_bar_lower_priority"] += 1
                    continue
                seen.add(symbol)
                if symbol in positions:
                    rejections["already_holding"] += 1
                    continue
                if ts < symbol_cooldown[symbol]:
                    rejections["post_loss_cooldown"] += 1
                    continue
                if ts < setup_cooldown[candidate["setup_key"]]:
                    rejections["setup_cooldown"] += 1
                    continue
                previous = loss_memory.get(candidate["setup_key"])
                if previous:
                    if ts < previous["closed_ts"] + 3_600_000:
                        rejections["failed_thesis_time"] += 1
                        continue
                    if candidate["signal_1h_ts"] <= previous["signal_1h_ts"]:
                        rejections["failed_thesis_fresh_hour"] += 1
                        continue
                entry_open = price_at(symbol, ts, "open")
                if entry_open is None:
                    rejections["no_price"] += 1
                    continue

                equity = mark_equity(ts, "open")
                snapshot = {}
                held_view = []
                active = {}
                gross = 0.0
                for held_symbol, held in positions.items():
                    mark = price_at(held_symbol, ts, "open") or held["last_mark"]
                    notional = abs(mark * held["qty"])
                    gross += notional
                    held_view.append({"symbol": held_symbol,
                                      "side": held["direction"],
                                      "notional": notional})
                    active[held_symbol] = {"risk_usd": held["risk_usd"]}
                    snapshot[held_symbol] = _snap(frames, held_symbol, ts,
                                                  bar_pos, cfg)
                snapshot[symbol] = _snap(frames, symbol, ts, bar_pos, cfg)
                decision = {
                    "symbol": symbol, "direction": candidate["direction"],
                    "confidence": 1.0,
                    "stop_loss_pct": candidate["stop_pct"],
                    "take_profit_pct": candidate["take_pct"],
                    "setup_type": candidate["setup_type"],
                    "execution_choice": "normal",
                }
                plan, why = engine.vet_open(
                    decision, equity, held_view, snapshot, {}, gross,
                    active_trades=active)
                if plan is None:
                    rejections[f"risk:{why}"] += 1
                    continue
                sign = 1.0 if candidate["direction"] == "long" else -1.0
                entry_price = entry_open * (
                    1 + sign * costs.entry_slippage / 100)
                notional = float(plan["notional"])
                qty = notional / entry_price
                fee = notional * costs.fee_per_side / 100
                cash -= fee
                positions[symbol] = {
                    "direction": candidate["direction"],
                    "setup_type": candidate["setup_type"],
                    "setup_key": candidate["setup_key"],
                    "signal_1h_ts": candidate["signal_1h_ts"],
                    "entry_ts": ts, "entry_price": entry_price,
                    "last_mark": entry_price, "qty": qty,
                    "notional": notional, "entry_fee": fee,
                    "risk_usd": float(plan["risk_usd"]),
                    "stop": entry_price * (1 - sign * float(plan["sl_pct"]) / 100),
                    "take": entry_price * (1 + sign * float(plan["tp_pct"]) / 100),
                    "funding_usd": 0.0, "bars": 0,
                }
        pending = []

        # ---- 2. funding settlement
        if costs.include_funding:
            for symbol, position in positions.items():
                frame = frames[symbol]
                hit = np.where(frame.funding_ts == ts)[0]
                if len(hit):
                    rate = float(frame.funding_rates[int(hit[0])])
                    mark = price_at(symbol, ts, "open") or position["last_mark"]
                    sign = 1.0 if position["direction"] == "long" else -1.0
                    paid = -sign * abs(mark * position["qty"]) * rate
                    cash += paid
                    position["funding_usd"] += paid

        # ---- 3. exits: stop first on an ambiguous bar, then target, then age
        for symbol in list(positions):
            position = positions[symbol]
            pos = bar_pos[symbol].get(ts)
            if pos is None:
                continue
            position["bars"] += 1
            row = frames[symbol].df.iloc[pos]
            direction = position["direction"]
            sign = 1.0 if direction == "long" else -1.0
            if direction == "long":
                stop_hit = float(row["low"]) <= position["stop"]
                take_hit = float(row["high"]) >= position["take"]
            else:
                stop_hit = float(row["high"]) >= position["stop"]
                take_hit = float(row["low"]) <= position["take"]
            reason = raw = None
            slip = costs.exit_slippage
            if stop_hit:
                reason, slip = "stop", costs.stop_slippage
                raw = (min(position["stop"], float(row["open"]))
                       if direction == "long"
                       else max(position["stop"], float(row["open"])))
            elif take_hit:
                reason, raw = "take_profit", position["take"]
            elif position["bars"] >= contract.max_hold_bars:
                reason, raw = "max_hold", float(row["close"])
            if reason is None:
                continue
            exit_price = raw * (1 - sign * slip / 100)
            exit_fee = abs(exit_price * position["qty"]) * costs.fee_per_side / 100
            gross = (exit_price - position["entry_price"]) * position["qty"] * sign
            cash += gross - exit_fee
            pnl = (gross - position["entry_fee"] - exit_fee
                   + position["funding_usd"])
            trades.append({
                "symbol": symbol, "direction": direction,
                "setup_type": position["setup_type"],
                "entry_ts": position["entry_ts"], "exit_ts": ts + BAR_MS,
                "exit_reason": reason, "notional": position["notional"],
                "risk_usd": position["risk_usd"], "net_pnl_usd": pnl,
                "fees_usd": position["entry_fee"] + exit_fee,
                "funding_usd": position["funding_usd"],
                "r_multiple": pnl / position["risk_usd"],
                "bars_held": position["bars"],
            })
            setup_cooldown[position["setup_key"]] = ts + 45 * 60_000
            if pnl < 0:
                symbol_cooldown[symbol] = ts + 45 * 60_000
                loss_memory[position["setup_key"]] = {
                    "closed_ts": ts + BAR_MS,
                    "signal_1h_ts": position["signal_1h_ts"]}
            del positions[symbol]

        # ---- 4. account-level breakers
        equity_close = mark_equity(ts, "close")
        high_water = max(high_water, equity_close)
        drawdown = (high_water - equity_close) / high_water * 100
        day_pnl = ((equity_close - day_start_equity) / day_start_equity * 100
                   if day_start_equity > 0 else 0.0)
        if day_pnl <= -float(risk_cfg["daily_loss_limit_pct"]):
            day_stopped = True
        if (enforce_kill and not killed
                and drawdown >= float(risk_cfg["max_drawdown_pct"])):
            for symbol in list(positions):
                position = positions[symbol]
                sign = 1.0 if position["direction"] == "long" else -1.0
                close_price = price_at(symbol, ts, "close") or position["last_mark"]
                exit_price = close_price * (1 - sign * costs.exit_slippage / 100)
                exit_fee = abs(exit_price * position["qty"]) * costs.fee_per_side / 100
                gross = (exit_price - position["entry_price"]) * position["qty"] * sign
                cash += gross - exit_fee
                pnl = (gross - position["entry_fee"] - exit_fee
                       + position["funding_usd"])
                trades.append({
                    "symbol": symbol, "direction": position["direction"],
                    "setup_type": position["setup_type"],
                    "entry_ts": position["entry_ts"], "exit_ts": ts + BAR_MS,
                    "exit_reason": "drawdown_kill",
                    "notional": position["notional"],
                    "risk_usd": position["risk_usd"], "net_pnl_usd": pnl,
                    "fees_usd": position["entry_fee"] + exit_fee,
                    "funding_usd": position["funding_usd"],
                    "r_multiple": pnl / position["risk_usd"],
                    "bars_held": position["bars"]})
            positions.clear()
            killed = True
            kill_ts = ts
            equity_close = cash

        equity_curve.append({"timestamp_ms": ts + BAR_MS,
                             "equity": equity_close,
                             "drawdown_pct": drawdown,
                             "open_positions": len(positions),
                             "killed": killed})
        if not killed and not day_stopped:
            pending = list(by_ts.get(ts, []))

    return _metrics(trades, equity_curve, killed, dict(rejections), kill_ts)


def _snap(frames, symbol, ts, bar_pos, cfg) -> dict:
    """The subset of the live snapshot the RiskEngine actually reads."""
    pos = bar_pos[symbol].get(ts)
    df = frames[symbol].df
    if pos is None:
        pos = 0
    row = df.iloc[pos]

    def num(value, default=None):
        value = float(value) if value is not None else None
        if value is None or value != value:
            return default
        return value

    return {
        "price": float(row["open"]),
        # Historical top-of-book is unavailable; the spread assumption is
        # carried by the cost scenario instead of being invented here.
        "spread_pct": 0.02,
        "funding_rate_pct": num(row["funding_rate_pct"], 0.0),
        "funding_interval_hours": num(row["funding_interval_hours"], 8.0),
        "next_funding_minutes": num(row["next_funding_minutes"], 240.0),
        "taker_fee_pct_per_side": float(
            cfg["trading_costs"]["taker_fee_pct_per_side"]),
        "fee_rate_source": "configured_backtest",
        "beta_btc_1h_72": num(row["beta_btc_1h_72"]),
        "corr_btc_samples": int(row["corr_btc_samples"]),
    }


def _metrics(trades, equity_curve, killed, rejections, kill_ts=None) -> dict:
    frame = pd.DataFrame(trades)
    curve = pd.DataFrame(equity_curve)
    if curve.empty:
        return {"trades": 0}
    final = float(curve.iloc[-1]["equity"])
    peak = curve["equity"].cummax()
    drawdown = float(((peak - curve["equity"]) / peak).max() * 100)
    daily = curve.copy()
    daily["date"] = pd.to_datetime(
        daily["timestamp_ms"], unit="ms", utc=True).dt.date
    daily_equity = daily.groupby("date")["equity"].last()
    returns = daily_equity.pct_change().dropna()
    days = max(1.0, (curve.iloc[-1]["timestamp_ms"]
                     - curve.iloc[0]["timestamp_ms"]) / 86_400_000)
    out = {
        "initial_equity": INITIAL_EQUITY,
        "final_equity": round(final, 2),
        "total_return_pct": round(final / INITIAL_EQUITY * 100 - 100, 2),
        "cagr_pct": round(
            ((final / INITIAL_EQUITY) ** (365.25 / days) - 1) * 100, 2)
        if final > 0 else -100.0,
        "max_drawdown_pct": round(drawdown, 2),
        "sharpe_daily_annualized": round(
            float(returns.mean() / returns.std() * np.sqrt(365)), 2)
        if len(returns) > 5 and returns.std() > 0 else None,
        "killed_by_drawdown": bool(killed),
        "killed_at": (str(pd.Timestamp(kill_ts, unit="ms", tz="UTC").date())
                      if kill_ts else None),
        "traded_days": round(days, 1),
        "trades": int(len(frame)),
        "top_rejections": dict(sorted(
            rejections.items(), key=lambda kv: -kv[1])[:8]),
    }
    if not frame.empty:
        pnl = frame["net_pnl_usd"].to_numpy(float)
        wins, losses = pnl[pnl > 0], pnl[pnl < 0]
        out.update({
            "win_rate_pct": round(float((pnl > 0).mean() * 100), 2),
            "expectancy_r": round(float(frame["r_multiple"].mean()), 4),
            "expectancy_usd": round(float(pnl.mean()), 2),
            "profit_factor": round(float(wins.sum() / -losses.sum()), 3)
            if len(losses) else None,
            "total_fees_usd": round(float(frame["fees_usd"].sum()), 2),
            "total_funding_usd": round(float(frame["funding_usd"].sum()), 2),
            "avg_hold_hours": round(float(frame["bars_held"].mean() * 0.25), 2),
            "exit_mix_pct": {
                k: round(v * 100, 1) for k, v in
                frame["exit_reason"].value_counts(normalize=True).items()},
        })
    return out
