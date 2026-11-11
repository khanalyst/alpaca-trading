# Session-edge study

Status on 11 November 2026: built, unit-tested (46 offline tests) and run on 16 years of public
index one-minute data and up to 34 years of adjusted daily data. Three rule/instrument pairs meet
every registered check and are **paper candidates**; one is **shadow only**; the rest are
**rejected**. The evidence is retrospective and on public diagnostic data. It must be reproduced
on SIP bars (one command, below) and then run forward in shadow before any order is sent.

This folder is kept apart from the paper-trial code: nothing in `agent/` or `research/` imports
it. The study needs only the Python standard library. The forward runner reuses
`agent.alpaca_provider` so the paper-only safeguards stay in one place.

## Why this study exists

The one-minute rule grammar (`agent/contracts/rule.py`) was tested on 94 arms over about 30
sessions. Mean gross return was -0.4 bps per trade and no arm reached t = 2
(`docs/deep-audit-2026-11-10/all-94-arms.csv`). Two things follow, and both are measured here:

1. Those rules have no gross edge to tune. A different class of rule is needed: few decisions
   per session, taken from published work, with parameters fixed in advance.
2. Thirty forward sessions cannot confirm a realistic edge. The registered rules below need
   roughly 2,400 to 2,700 sessions to reach 80% power on their own daily returns (see
   `sessions_to_confirm_forward` in `results.json`). The evidence has to come from long history;
   the forward shadow checks implementation and costs, not statistical significance.

## The registered rules

Parameters are fixed in `strategies.py` and are not searched. Each simulator calls the same
decision function the forward runner calls.

| rule | decision | source |
| --- | --- | --- |
| `noise_band_momentum` | Every 30 minutes from 10:00 to 15:30, compare the price with a band around the open: `max(open, prior close) * (1 + s)` and `min(open, prior close) * (1 - s)`, where `s` is the mean absolute move from the open to that time of day over the prior 14 sessions. Outside the band, follow the move; back inside, go flat. Flat at the close. | Zarattini, Aziz and Barbon (2024), [Beat the Market: An Effective Intraday Momentum Strategy for S&P500 ETF (SPY)](https://ideas.repec.org/p/chf/rpseri/rp2497.html) |
| `close_weakness_reversion` | At 15:50, compute the internal bar strength `(price - day low) / (day high - day low)`. Flat and below 0.20: buy. Long and above 0.50, or after 5 sessions: sell. Long only. | Pagonidis (2013), The IBS Effect: Mean Reversion in Equity ETFs; corroborated on later data in [arXiv 2306.12434](https://arxiv.org/pdf/2306.12434) |
| `opening_range_breakout` | Trade in the direction of the first 5-minute candle at the 09:35 open, stop at the candle's far extreme, target 10R, flat at the close. | Zarattini and Aziz (2023), [Can Day Trading Really Be Profitable?](https://papers.ssrn.com/abstract=4416622) |
| `first_last_half_hour` (diagnostic) | Hold the sign of the first half hour over the last half hour. | Gao, Han, Li and Zhou (2018), [Market intraday momentum](https://econpapers.repec.org/RePEc:eee:jfinec:v:129:y:2018:i:2:p:394-414) |

All decisions read completed bars only and fill at the open of the next bar. Sessions with fewer
than 280 real one-minute bars (half days, broken feeds) are excluded from the study and are not
traded by the runner.

## The registered decision rule

Fixed in `run.py` before the results were computed:

* design period: sessions before 2020-01-01; holdout: sessions from that date;
* cost: 1 bp per round trip (a one-cent spread on a 500 to 600 dollar share is about 0.2 bps,
  plus sale fees and a slippage allowance), and every check repeated at 2 bps;
* `paper_candidate` needs all of: Newey-West t of at least 3.0 over all sessions (the hurdle of
  [Harvey, Liu and Zhu, 2016](https://econpapers.repec.org/RePEc:oup:rfinst:v:29:y:2016:i:1:p:5-68.));
  holdout t of at least 2.0; positive at twice the cost in both periods; at least 70% of the
  one-at-a-time parameter neighbours positive in the holdout; deflated Sharpe ratio of at least 0.95;
* `shadow_only`: positive in both periods with t of at least 2.0 but a failed check. Recorded
  forward, never traded;
* `rejected`: everything else.

## Results (public data, 2010-11-15 to 2026-11-06)

Full tables: [results/public-2026-11-10/report.md](results/public-2026-11-10/report.md); the
record is `results.json` beside it. Returns are arithmetic, on notional, unlevered, net of 1 bp
per round trip. "QQQ" and "SPY" are Nasdaq-100 and S&P 500 index one-minute proxies.

| instrument | rule | Sharpe | t | holdout Sharpe | since 2024 | positive years | max drawdown | verdict |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| QQQ | noise_band_momentum | 0.81 | 3.62 | 0.77 | 0.43 | 14/17 | -9.7% | paper candidate |
| QQQ | close_weakness_reversion | 0.76 | 3.22 | 0.82 | 0.91 | 13/17 | -24.0% | paper candidate |
| SPY | close_weakness_reversion | 0.77 | 3.21 | 0.92 | 0.80 | 15/17 | -16.5% | paper candidate |
| QQQ | opening_range_breakout | 0.58 | 2.40 | 0.54 | 0.26 | 13/17 | -14.0% | shadow only |
| SPY | noise_band_momentum | 0.37 | 1.58 | 0.35 | -0.40 | 10/17 | -15.0% | rejected |
| SPY | opening_range_breakout | 0.23 | 0.96 | 0.08 | 0.09 | 12/17 | -14.5% | rejected |
| QQQ, SPY | first_last_half_hour | -0.68, -0.95 | -2.68, -3.61 | negative | negative | 6/17, 2/17 | | rejected |

Daily bars, deciding at the close itself (optimistic by the last ten minutes):

| symbol | from | close_weakness_reversion Sharpe (t) | holdout Sharpe | positive years |
| --- | --- | ---: | ---: | ---: |
| SPY | 1993 | 0.86 (5.49) | 1.21 | 30/34 |
| QQQ | 1999 | 0.96 (5.70) | 1.26 | 26/28 |
| DIA | 1998 | 0.84 (5.05) | 0.97 | 27/29 |
| IWM | 2000 | 0.79 (4.48) | 0.68 | 21/27 |
| VTI | 2001 | 0.91 (4.72) | 1.11 | 24/26 |

The three paper candidates together (momentum at 100% of equity intraday, reversion split evenly
between QQQ and SPY): annual return 14.5%, volatility 13.3%, Sharpe 1.09 (t 4.66), maximum
drawdown -21.9%, holdout Sharpe 1.11, bootstrap 95% interval 0.70 to 1.48. Momentum and reversion
are nearly uncorrelated (0.10).

## What the results do not show

* **Not a guarantee.** A Sharpe ratio near 0.8 means losing years and losing stretches of many
  months. The reversion rule is long only and falls with the market in a crash; its QQQ daily
  history includes a -44% additive drawdown (2000 to 2002).
* **Proxy data.** The minute results are index CFD quotes, not ETF trades. A check against real
  QQQ and SPY hourly bars over the last two years gave a daily profit correlation of 0.95 with the
  proxy, but the SIP rerun below is the test that counts.
* **Cost dependent.** Momentum breaks even near 4 bps per round trip (Sharpe 0.54 at 2 bps, 0.28
  at 3 bps). It is viable only in the most liquid instruments with marketable orders of modest size.
* **Decay.** Momentum on QQQ was weaker after 2024 (Sharpe 0.43) and the same rule has faded to
  nothing on S&P 500 proxies, on VTI trades (holdout Sharpe 0.08) and on AAPL and AMZN (holdout
  near zero). Treat it as the weaker of the two edges and size it accordingly.
* **Multiple testing.** About ten rule families were examined on about eight instruments while
  preparing this study, with one-at-a-time robustness variants of each. The deflated Sharpe ratio
  here deflates only for each rule's own neighbours. Against a stricter hurdle for 100 trials
  (t of about 3.3), QQQ momentum (3.62) and the daily reversion results (4.5 to 5.7) pass; the
  minute-level reversion results (3.2) pass only with the support of the daily history.
* **Effects examined and not adopted** (exploratory runs, not in this folder's results): VWAP trend
  following (negative after costs on VTI, IVE, IBM, WDC; decayed on AAPL, AMZN), intraday
  momentum and opening range breakout on single stocks (IBM, WDC, OIH negative; AAPL, AMZN decayed
  after 2019), top-20 relative-volume opening range breakout on about 570 stocks over 28 recent
  sessions (negative; too short and too coarse to be conclusive), turn of the month (weak),
  overnight long (significant, t 4.5 on SPY and QQQ, but it is the equity risk premium earned at
  night with -40% drawdowns, and the reversion rule already holds overnight selectively).

## Files

| file | role |
| --- | --- |
| `data.py` | session matrices, loaders for SIP month files, public index archives and adjusted daily bars |
| `strategies.py` | the fixed rules, their decision functions and simulators |
| `stats.py` | per-session statistics: Newey-West t, sign-flip p, block bootstrap, deflated Sharpe, power |
| `run.py` | command line: run the study, apply the decision rule, write `report.md` and `results.json` |
| `fetch_public.py` | command line: download the public diagnostic data |
| `live.py` | command line: forward shadow, optional paper orders, forward report |
| `results/public-2026-11-10/` | the run reported above, including the forward review bands |
| `tests/research/test_session_edges_study.py` | 46 offline unit tests (in the repository's test tree) |

## Reproduce

```bash
python -I studies/session_edges/fetch_public.py --out /data/session-edges
```

```bash
python -I studies/session_edges/run.py --index-dir /data/session-edges/index --daily-dir /data/session-edges/daily --out /data/session-edges/out
```

The run takes about four minutes.

## Confirm on SIP bars (do this first)

Alpaca serves historical SIP bars on the free plan when they are more than 15 minutes old.

```bash
python -I studies/liquid_reversion/fetch_sip.py --out /data/sip --start 2016-01-01 --end 2026-10-31 --symbols QQQ,SPY --calendar
```

```bash
python -I studies/session_edges/run.py --alpaca-dir /data/sip --symbols QQQ,SPY --calendar /data/sip/calendar.json --out /data/session-edges/sip
```

Proceed to the forward shadow only if the paper candidates keep their verdicts on SIP bars. If a
rule drops a tier there, the SIP result wins.

## Forward shadow

```bash
python -m studies.session_edges.live run
```

The runner polls the broker clock, takes each decision once from completed one-minute bars, and
records what a marketable order would have paid at the live quote (ask to buy, bid to sell). No
order is sent. After the close it replays the day's bars through the study simulator and stores
both numbers. State and journal live in `runtime/session_edges/`; a restart resumes the session,
and a decision that is more than four minutes late is skipped, never acted on stale.

As a service next to the existing stack:

```bash
docker compose -f compose.yaml -f deploy/compose.session-edges.yaml up -d session-edges
```

Forward statistics:

```bash
python -m studies.session_edges.live report
```

Default legs (`live.DEFAULT_LEGS`): momentum on QQQ and reversion on SPY are the legs that would
trade; reversion on QQQ and the opening range on QQQ are recorded only. One traded leg owns one
symbol, so the broker position is never a mixture of two rules.

## Paper orders

Only after the SIP confirmation and at least 20 clean shadow sessions:

```bash
python -m studies.session_edges.live run --paper-orders
```

This needs a configuration pinned to paper mode and a provider on the paper endpoint. It takes the
same runtime lock as the trader, so stop the trader first; the two never share the account. In
the compose deployment, run it in the trader's own container definition so that it sees the same
runtime volume and therefore the same lock:

```bash
docker compose stop trader
```

```bash
docker compose run --rm --no-deps trader python -m studies.session_edges.live run --paper-orders --dir /app/runtime/session_edges
```

Orders are market orders for whole shares: momentum uses 50% of equity, reversion 25%. Intraday
legs leave at 15:58. No new entry is sent once equity is 2% below its level at the first entry
check of the day. A long-to-short flip is sent as two orders because a broker position cannot
cross zero in one.

Short selling for the momentum leg needs a margin account. FINRA's pattern day trader rule
applies to the intraday leg on a margin account below the regulatory equity minimum; check the
current rule before going live.

## From shadow to paper to live

A forward window cannot prove these edges; it can disprove the implementation. The gates are
therefore about fidelity and cost, with a loss limit:

| step | condition |
| --- | --- |
| shadow to paper orders | SIP rerun keeps the verdict; 20 full shadow sessions; shadow-versus-replay correlation of at least 0.9 on the intraday leg; mean shadow minus replay no worse than -1 bp per session; no skipped decisions caused by the runner itself |
| paper to live (small) | 60 paper sessions; realised fills within 1 bp per round trip of the shadow quotes; cumulative paper result above the 5th percentile of the backtest's 60-session distribution |
| stop and review | cumulative result below the 1st percentile of the backtest distribution for the elapsed sessions, or a drawdown beyond 1.5 times the backtest maximum |

Start live at a quarter of the paper size and scale only after a further 60 sessions inside the
same bands.
