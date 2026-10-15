# Fresh research, 15 October 2026: how to reproduce

Everything in `results/` was produced by the scripts in `scripts/` from public
data fetched on 14 and 15 October 2026. Nothing here is decision-bearing for the
repository's protocol: the bars are public consolidated-tape bars, labelled
`historical_backfill` on `delayed_sip`, not the deployment's Alpaca IEX
`forward_observed` corpus on the VM (that corpus was not reachable from the review
environment).

## Data (not committed; `data/` is git-ignored)

```sh
cd docs/review-2026-10-15/research
python -I scripts/fetch.py data/bars_1m.json 1m 30 7      # 22 sessions, 2026-09-14..10-13
python -I scripts/fetch.py data/bars_5m.json 5m 59 30     # 42 sessions, 2026-08-17..10-14
python -I scripts/fetch.py data/bars_60m.json 60m 729 120 # 500 sessions, 2024-10-16..2026-10-14
python -I scripts/fetch_daily.py data/bars_1d.json        # 2010-01-04..2026-10-14, with adjusted closes
python -I scripts/dq.py data/bars_1m.json                 # coverage and per-symbol volatility check
```

The public endpoint only serves one-minute bars for the last 30 days, so a re-run
on a later date covers a different window. Results will differ accordingly.

## Repository arms (uses the repository's own evaluators)

```sh
python -I scripts/replay.py results/replay 2               # 82 arms, per-arm JSON (about 71 MB)
python -I scripts/analyze_replay.py results/replay data/bars_1m.json results/analysis.json
python -I scripts/runtime_faithful.py                      # first trade per symbol-session (runtime rule)
python -I scripts/summarize.py
python -I scripts/prereg.py results/prereg.json            # research.preregistered, diagnostic_only
python -I scripts/crosscheck.py                            # independent measure vs repository instrument
python -I scripts/sq.py results/sq.json 2                  # repository instrument for all arms (slow: ~15-50 min per arm)
```

`replay.py` calls `agent.contracts.rule.evaluate_rule_signal`,
`initialize_exit_state` and `completed_bar_exit_transition`, and
`agent.contracts.ibr.generate_ibr_signal`. Runtime rules mirrored: entry at the next
bar's open, no entry at or after 15:00, flat at 15:50, stressed-cost veto (30 bps),
thesis deadline = 16:00 minus `exit_before_minutes`, max hold = entry bar plus
`max_hold_bars`, stop-first ties, first trade per symbol-session for the
runtime-faithful view. Costs per round trip: gross; fees 0.28 bps; realistic =
max(one tick, Corwin-Schultz median from `docs/audit-2026-09-21/spread-reference.json`)
+ 1.0 bps slippage + 0.28 bps fees; shipped model 17 bps.

## Independent hypotheses (own code, no repository imports)

```sh
python -I scripts/hypotheses_long.py data results/hypotheses_long.json  # intraday momentum, gap fade, cross-sectional reversal, overnight drift
python -I scripts/pro_variants.py data results/pro_variants.json        # professional ORF, VWAP reversion and mirror, ORB-5
python -I scripts/variance_ratio.py data > results/variance_ratio.txt   # intraday variance ratios with session bootstrap
```

## Files in `results/`

| file | content |
| --- | --- |
| `summary.txt` | aggregate across 82 arms (sequential re-entry view) |
| `runtime_faithful.txt` / `.json` | first trade per symbol-session, per arm, with the unseen-window split, one-bar-delay sensitivity and portfolio simulation |
| `analysis.txt` / `.json` | per-arm trade economics (sequential view), exit mix, signal quality at 5/15/30/60/120 minutes, portfolio simulation |
| `prereg.json` | `research.preregistered.evaluate` for both hypotheses, `decision_eligible=False` |
| `crosscheck_prereg.txt`, `crosscheck_sq.txt` | independent signal-quality measure against the repository instrument on identical windows |
| `sq_repo_instrument_9_arms.json` | repository `measure_signal_quality` output for the nine arms it completed |
| `hypotheses_long.json`, `pro_variants.json`, `variance_ratio.txt` | independent hypothesis tests |
| `replay.log` | per-arm run log (trades, refusals, seconds) |
