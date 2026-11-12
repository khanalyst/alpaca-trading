# Session edges: discovery, evidence and the forward runner

Status on 12 November 2026. This folder holds three things that work together:

1. **A registered-rule study** (`run.py`): published session-level rules, parameters fixed in
   advance, judged on 16 years of minute data and up to 34 years of daily data.
2. **A discovery engine** (`grammar.py`, `discover.py`, `propose.py`): a bounded search over
   session-level hypotheses that screens on a design period, confirms on an untouched holdout
   with a correction for everything it ever promoted, and writes an edge book.
3. **A forward runner** (`live.py`): takes the edge book through shadow, paper orders and live
   orders with the same decision code the simulators use.

The engine searches 16 edges (signals) with 204 variants (parameter settings) per instrument:
seven from the day-trading desk, six swing and three from the options desk
([EDGES.md](EDGES.md) lists the premise of each). On public data it tested 3,861 hypotheses,
has taken 124 to the holdout and confirms 9. The edge book that results trades four legs.
Everything here has passed 88 offline tests, and the runner's preflight has passed against the
Alpaca paper account. **Nothing has yet been confirmed on SIP bars**; that is the first step
below.

No result here is a promise. An edge with a Sharpe ratio near 1 has losing months and losing
years; the numbers that matter for sizing are the drawdowns and the review bands, not the mean.

## Why this exists

The one-minute rule grammar in `agent/contracts/rule.py` was tested on 94 arms over about 30
sessions: mean gross return -0.43 bps per trade, best t-statistic 1.30
(`docs/deep-audit-2026-11-10/all-94-arms.csv`). The search had no edge to find and the sample
could not have recognised one (see `docs/strategy-audit-2026-11-11/README.md`). This folder
changes both: the hypotheses have a reason to work, and the evidence is years long.

## The discovery engine

A hypothesis is data: `{"family": ..., "symbol": ..., "params": {...}}`. An **edge** (a family
in `grammar.py`) is one signal with a published or structural reason to exist; a **variant** is
that signal with different values. Every edge is traded in shares of the instrument, including
the ones that read the options market.

| edge | desk | data | signal | variants | the runner can trade it |
| --- | --- | --- | --- | ---: | --- |
| `noise_band_momentum` | day trader | minute | price outside its usual move for the time of day, checked every 30 or 60 minutes | 36 | yes |
| `midday_trend` | day trader | minute | one midday check of the same band, held to the end of day | 12 | yes |
| `opening_range_breakout` | day trader | minute | direction of the first 5, 15 or 30 minutes, stop at the far side | 12 | recorded only |
| `prior_day_break` | day trader | minute | first close beyond yesterday's high or low, followed or faded, optionally only after a narrow day | 12 | no |
| `gap_open` | day trader | minute | follow or fade a large overnight gap | 12 | no |
| `vwap_trend` | day trader | minute, needs volume | long above the session VWAP, short below | 6 | no |
| `last_hour` | day trader | minute | follow or fade the day's move into the close | 4 | no |
| `close_weakness_reversion` | swing | minute | buy a close near the day's low at 15:50, sell the recovery | 30 | yes |
| `close_weakness_reversion_daily` | swing | daily | the same, decided at the close on daily bars | 30 | yes, as the 15:50 rule |
| `rsi_reversion_daily` | swing | daily | buy a 2- or 3-day oversold reading | 12 | no |
| `down_streak_daily` | swing | daily | buy after 2, 3 or 4 lower closes | 9 | no |
| `overnight_hold` | swing | minute | hold from 15:58 to 09:31 | 3 | no |
| `turn_of_month_daily` | swing | daily | hold across the month end | 4 | no |
| `vix_spike_daily` | options | daily, needs VIX | buy after implied volatility jumps above its 10-day average | 12 | no |
| `vix_term_structure_daily` | options | daily, needs VIX and VIX3M | buy an inverted volatility curve, or hold only while it slopes up | 6 | no |
| `expiration_week_daily` | options | daily | long through the week of the monthly option expiration | 4 | no |

[EDGES.md](EDGES.md) gives the premise and the exact values of every variant; it is generated
from the code (`python -m studies.session_edges.grammar`) and a test keeps it in step.

The search may combine edges, parameters and instruments freely. What it may conclude is
fixed in `discover.py`:

1. **Screen on the design period only** (before 2020-01-01): Newey-West t of at least 2.0 net
   of costs, positive at twice the cost, at least 40 round trips, at least 70% of one-step
   parameter neighbours positive, and an **alpha t of at least 2.0** against the instrument's
   own daily return. The last check removes exposure: a rule that is simply long the market on
   most days earns the market's drift and is not an edge.
2. **One champion per edge and instrument** goes forward: the screened hypothesis with the
   highest design t. Nothing else ever touches the holdout.
3. **Confirm on the holdout** (2020 onwards). `confirmed` needs Holm at 5% across every champion
   ever promoted, a positive holdout at twice the cost, a positive result since 2024 and a
   holdout alpha t of at least 2.0. `provisional` needs Benjamini-Yekutieli at 10%, the cost
   stress and a positive holdout alpha.
4. **The ledger remembers.** Every promoted hypothesis stays in the correction, so searching
   again, adding an edge, or adding proposals raises the bar.

Costs are per round trip and per instrument (1 bp for SPY, QQQ and IWM; 1.5 for DIA and VTI;
3 for anything else unless `--cost-bps` says otherwise). An edge whose context series is not
loaded (`--context VIX,VIX3M`), or which needs traded volume on data that has none, is skipped
and named in the report.

### What it found on public data

Full record: [results/discovery-public-2026-11-12/discovery.md](results/discovery-public-2026-11-12/discovery.md)
(47 ETFs of daily history, two index minute archives from 2010, VIX and VIX3M). The ledger of
that run is committed beside it; pass it with `--ledger` so that a later run keeps counting.

| edge | tested | passed the design screen | champions this run | confirmed |
| --- | ---: | ---: | ---: | ---: |
| close_weakness_reversion_daily (47 ETFs) | 1,410 | 367 | 25 | 5 |
| down_streak_daily | 423 | 28 | 10 | 3 |
| rsi_reversion_daily | 564 | 42 | 14 | 1 |
| vix_term_structure_daily | 282 | 11 | 7 | 0 |
| noise_band_momentum (QQQ, SPY minute proxies) | 72 | 18 | 2 | 0 |
| turn_of_month_daily | 188 | 2 | 1 | 0 |
| vix_spike_daily | 564 | 1 | 1 | 0 |
| expiration_week_daily | 188 | 1 | 1 | 0 |
| close_weakness_reversion (minute proxies) | 60 | 0 | 0 | 0 |
| prior_day_break, midday_trend, gap_open, opening_range_breakout, last_hour, overnight_hold | 110 | 0 | 0 | 0 |
| vwap_trend | not searched: the index proxies carry no volume | | | |

The nine confirmed hypotheses (holdout 2020 to 2026, net of costs). Beta is the rule's exposure
to the instrument; the alpha t tests what is left after that exposure is removed:

| edge | instrument | holdout t | holdout Sharpe | bps per session | beta | alpha t |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| close_weakness_reversion_daily | SOXL | 3.88 | 1.34 | 37.6 | 0.37 | 2.93 |
| close_weakness_reversion_daily | SPY | 3.76 | 1.30 | 5.5 | 0.28 | 2.60 |
| close_weakness_reversion_daily | XLK | 3.70 | 1.25 | 7.2 | 0.27 | 2.47 |
| close_weakness_reversion_daily | SSO | 3.69 | 1.33 | 13.1 | 0.39 | 2.50 |
| rsi_reversion_daily | IVW | 3.66 | 1.27 | 6.3 | 0.28 | 2.48 |
| down_streak_daily | SPY | 3.65 | 1.18 | 3.5 | 0.13 | 2.68 |
| down_streak_daily | QQQ | 3.61 | 1.33 | 3.4 | 0.07 | 3.03 |
| close_weakness_reversion_daily | IVW | 3.53 | 1.33 | 7.0 | 0.32 | 2.54 |
| down_streak_daily | IVW | 3.34 | 1.20 | 3.2 | 0.08 | 2.70 |

Read this carefully:

* **It is one idea, found nine times.** Every confirmed hypothesis buys a US equity index after
  short-term weakness. That is strong evidence the effect is real and no evidence of
  diversification: the legs fall together in a crash.
* **It is not market exposure in disguise.** The confirmed rules carry a beta of 0.07 to 0.39
  and keep an alpha t of 2.5 to 3.0 once that exposure is removed. Roughly a third of each
  rule's return is the market's drift; the rest is timing.
* **The options-desk edges did not confirm.** Holding only while the volatility curve slopes up
  reached the holdout on seven instruments with a holdout t of 1.4 to 2.2, but its beta is 0.55
  to 0.77 and its alpha t is below 1: it is mostly the market. Buying a VIX spike and holding
  through expiration week each produced one champion, and both lost money in the holdout.
* **The new day-trading edges did not pass the design screen.** Breaks of yesterday's range,
  followed or faded, with or without a narrow-day setup, show nothing on the index proxies.
* **Nothing confirmed outside equities.** Treasury and corporate bond funds reached the holdout
  and failed; gold, silver, oil and high-yield bonds did not pass the design screen. That is what
  a real, equity-specific effect looks like.
* **Intraday rules do not survive the search correction.** QQQ momentum has a holdout p of
  about 0.02; among 124 champions that is not enough. It is in the edge book only as a
  registered rule (below), and it is the weaker leg.
* **The 15:50 form is weaker than the daily form.** Daily rules decide at the close; the runner
  decides at 15:50. Where both forms were measured, deciding ten minutes early cost between a
  tenth and a third of the Sharpe ratio (S&P 500: 0.87 to 0.77; VTI: 0.96 to 0.75; Nasdaq-100:
  1.10 to 0.76), and on the ten design years of the minute proxies the 15:50 form does not
  reach an alpha t of 2 by itself. The daily history is what carries the evidence.

### The edge book

`discover.py` writes `edge_book.json`. A leg trades when it is `confirmed`, or a paper candidate
of the registered-rule study (`--registered`); `provisional` legs are recorded only. One traded
leg owns one symbol, one traded leg stands for each underlying index, and a leveraged fund is
never traded (it repeats its index's edge with more risk per dollar). The swing sleeve may use
half the account across at most four legs; the intraday sleeve one leg at 25%.

The book from public data ([edge_book.json](results/discovery-public-2026-11-12/edge_book.json)), unchanged by the wider search:

| trade | weight | tier | rule | symbol | parameters |
| --- | ---: | --- | --- | --- | --- |
| yes | 16.7% | confirmed | close_weakness_reversion | SPY | enter below 0.10, exit above 0.60, at most 5 sessions |
| yes | 16.7% | confirmed | close_weakness_reversion | XLK | enter below 0.10, exit above 0.60, at most 5 sessions |
| yes | 16.7% | confirmed | close_weakness_reversion | IVW | enter below 0.20, exit above 0.60, at most 3 sessions |
| yes | 25.0% | registered | noise_band_momentum | QQQ | published parameters, both sides |
| record only | | confirmed | close_weakness_reversion | SOXL, SSO | leveraged funds |
| record only | | registered or provisional | close_weakness_reversion | QQQ, SMH, DIA, VTI, VB, EEM, GDX and three leveraged funds | |

### Letting a model propose hypotheses

```bash
python -m studies.session_edges.propose prompt --discovery OUT/discovery.json --symbols QQQ,SPY,IWM,XLK
```

```bash
python -m studies.session_edges.propose ask --model MODEL --discovery OUT/discovery.json --symbols QQQ,SPY,IWM,XLK --out proposals.json
```

```bash
python -I studies/session_edges/discover.py --alpaca-dir /data/sip --symbols QQQ,SPY,IWM,XLK --proposals proposals.json --out OUT
```

The model may choose any family, instrument and parameter value inside the grammar and must give
a one-sentence mechanism. It sees design-period statistics only, never a holdout number. Its
proposals are screened like grid points, and each one that reaches the holdout joins the
correction. `ask` uses the OpenAI-compatible provider named by `OPENAI_API_KEY`; `prompt` and
`check` let any model be used by hand. A new *family* is a code change with its own simulator
and tests, by design.

## The registered-rule study

Fixed published rules, no search. Decision rule in `run.py`: t of at least 3.0 over all sessions,
holdout t of at least 2.0, positive at twice the cost in both periods, at least 70% of parameter
neighbours positive in the holdout, deflated Sharpe ratio of at least 0.95.
Results on index minute proxies, 2010-11-15 to 2026-11-06
([report](results/public-2026-11-10/report.md)):

| instrument | rule | Sharpe | t | holdout Sharpe | since 2024 | positive years | max drawdown | verdict |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| QQQ | noise_band_momentum | 0.81 | 3.62 | 0.77 | 0.43 | 14/17 | -9.7% | paper candidate |
| QQQ | close_weakness_reversion | 0.76 | 3.22 | 0.82 | 0.91 | 13/17 | -24.0% | paper candidate |
| SPY | close_weakness_reversion | 0.77 | 3.21 | 0.92 | 0.80 | 15/17 | -16.5% | paper candidate |
| QQQ | opening_range_breakout | 0.58 | 2.40 | 0.54 | 0.26 | 13/17 | -14.0% | shadow only |
| SPY | noise_band_momentum, opening_range_breakout | 0.37, 0.23 | 1.58, 0.96 | 0.35, 0.08 | negative | | | rejected |
| QQQ, SPY | first_last_half_hour | -0.68, -0.95 | | negative | negative | | | rejected |

The three paper candidates together: annual return 14.5%, volatility 13.3%, Sharpe 1.09
(t 4.66), maximum drawdown -21.9%, 16 of 17 years positive. Momentum and reversion are nearly
uncorrelated (0.10). Sources: Zarattini, Aziz and Barbon (2024),
[Beat the Market](https://ideas.repec.org/p/chf/rpseri/rp2497.html); Pagonidis (2013), The IBS
Effect, corroborated in [arXiv 2306.12434](https://arxiv.org/pdf/2306.12434); Zarattini and Aziz
(2023), [Can Day Trading Really Be Profitable?](https://papers.ssrn.com/abstract=4416622);
Gao, Han, Li and Zhou (2018),
[Market intraday momentum](https://econpapers.repec.org/RePEc:eee:jfinec:v:129:y:2018:i:2:p:394-414).

Limits of this evidence: the minute data are index CFD quotes, not ETF trades (a check against
real QQQ and SPY hourly bars gave a daily profit correlation of 0.95); momentum breaks even near
4 bps per round trip and has faded everywhere except the Nasdaq-100; the reversion rule is long
only and its QQQ daily history includes a -44% additive drawdown in 2000 to 2002.

## The forward runner

```bash
python -m studies.session_edges.live run --book OUT/edge_book.json
```

The runner polls the broker clock, takes each decision once from completed one-minute bars, and
records what a marketable order would pay at the live quote. After the close it replays the
day's bars through the simulator and stores both numbers. A restart resumes the session; a
decision more than four minutes late is skipped, never acted on stale; half days are not traded.

With orders enabled it follows the shadow book through a small state machine:

* one order at a time per leg, written to disk before it is sent, and looked up by its client
  order id until the broker reports a final state. Shares change only on a confirmed fill;
* the broker's positions are read every poll. If a leg holds fewer shares than the runner
  believes, it adopts the smaller number. If its symbol holds shares the runner did not buy, the
  leg stops opening positions and never sells them;
* a position never crosses zero in one order, never exceeds the leg's share of equity or
  `--max-notional`, and is never opened after the daily loss limit (2% by default), a `HALT`
  file or a `FLATTEN` request;
* an order that fails is retried three times to open and five times to close, then the leg
  stops and says so in the journal;
* intraday legs are closed at 15:58, and again at the next open if anything was left behind;
* a new edge book that no longer trades a leg which still holds shares is refused at start-up,
  so a position is never orphaned. Flatten with the previous book first.

Operator controls, none of which needs broker access:

```bash
python -m studies.session_edges.live halt
```

```bash
python -m studies.session_edges.live flatten
```

```bash
python -m studies.session_edges.live resume
```

`halt` forbids new positions. `flatten` makes the running runner close every position it holds
at its next poll and then halts. `resume` removes both. `report` prints forward statistics,
broker fill slippage against the decision quote, and a count of every problem event.

## Two days to live

This is the shortest path that still tests what can be tested. Each step has a stop condition.
A forward window of any practical length cannot prove these edges (the rules need about 2,400
sessions for that); what two days can establish is that the evidence holds on real ETF bars and
that the order plumbing works against the real broker.

**Day 0: confirm on SIP bars.** Alpaca serves historical SIP bars on the free plan once they are
15 minutes old. On the VM, open a shell in the trader's container definition, where `/app/runtime`
is the persistent volume, and put the keys in the environment for the fetcher:

```bash
docker compose run --rm --no-deps trader sh
```

```bash
set -a; . /run/secrets/agent_credentials; set +a
```

```bash
python -I studies/liquid_reversion/fetch_sip.py --out /app/runtime/session_edges/sip --start 2016-01-01 --end 2026-10-31 --symbols QQQ,SPY,XLK,IVW --calendar
```

```bash
python -I studies/session_edges/fetch_public.py --out /app/runtime/session_edges/public --pairs "" --daily SPY,QQQ,IWM,DIA,VTI,XLK,IVW,SMH --context VIX,VIX3M
```

```bash
python -I studies/session_edges/run.py --alpaca-dir /app/runtime/session_edges/sip --symbols QQQ,SPY --calendar /app/runtime/session_edges/sip/calendar.json --out /app/runtime/session_edges/registered
```

```bash
python -I studies/session_edges/discover.py --alpaca-dir /app/runtime/session_edges/sip --symbols QQQ,SPY,XLK,IVW --calendar /app/runtime/session_edges/sip/calendar.json --daily-dir /app/runtime/session_edges/public/daily --daily SPY,QQQ,IWM,DIA,VTI,XLK,IVW,SMH --context VIX,VIX3M --registered /app/runtime/session_edges/registered/results.json --out /app/runtime/session_edges/discovery
```

*Stop if* the legs you intend to trade are not in the new
`/app/runtime/session_edges/discovery/edge_book.json` with `trade: true`, or if the minute form of
the reversion rule (`close_weakness_reversion` on the SIP symbols in `discovery.md`) has a
negative holdout. The SIP result wins over the public one.

**Day 1: one full session on paper.** Stop the trader first; both take the same runtime lock.

```bash
docker compose stop trader
```

```bash
docker compose run --rm --no-deps trader python -m studies.session_edges.live preflight --paper-orders --book /app/runtime/session_edges/discovery/edge_book.json --dir /app/runtime/session_edges/paper
```

```bash
docker compose run -d --no-deps --name session-edges-paper trader python -m studies.session_edges.live run --paper-orders --book /app/runtime/session_edges/discovery/edge_book.json --dir /app/runtime/session_edges/paper
```

After the close:

```bash
docker compose run --rm --no-deps trader python -m studies.session_edges.live report --dir /app/runtime/session_edges/paper
```

*Stop if* `problem_events` is not empty and you cannot explain each entry, if any `fill` is
missing for a submitted order, if the momentum leg's `shadow_gross` and `replay_gross` differ by
more than a few basis points, or if the account is not flat in the intraday symbol after 16:00.
The momentum leg trades on most days, so it exercises the order path even when no reversion
entry occurs.

**Day 2: live, small, reversion first.** Use live keys in their own secrets file and a separate
state directory. Stop the paper runner first (`docker stop session-edges-paper`).

```bash
ALPACA_AGENT_SECRET_FILE=/path/to/live.env docker compose run --rm --no-deps -e ALPACA_PAPER=false -e ALPACA_LIVE_ENABLE=true trader python -m studies.session_edges.live preflight --mode live --live-orders --confirm LIVE --max-notional 1000 --trade-only close_weakness_reversion --book /app/runtime/session_edges/discovery/edge_book.json --dir /app/runtime/session_edges/live
```

```bash
ALPACA_AGENT_SECRET_FILE=/path/to/live.env docker compose run -d --no-deps --name session-edges-live -e ALPACA_PAPER=false -e ALPACA_LIVE_ENABLE=true trader python -m studies.session_edges.live run --mode live --live-orders --confirm LIVE --max-notional 1000 --trade-only close_weakness_reversion --book /app/runtime/session_edges/discovery/edge_book.json --dir /app/runtime/session_edges/live
```

The operator controls take the same `--dir`, for example:

```bash
docker compose run --rm --no-deps trader python -m studies.session_edges.live flatten --dir /app/runtime/session_edges/live
```

Recommendations for the first live weeks, in order of importance:

1. **Trade the reversion legs only** (`--trade-only close_weakness_reversion`). They carry the
   strongest evidence, send one order in and one order out, and never short. Keep QQQ momentum on
   paper or in shadow until 20 sessions show its shadow and replay results agree.
2. **Keep `--max-notional` small**, a tenth of what the weights would allow, and raise it only
   after 20 sessions with an empty `problem_events`.
3. **On the IEX feed, trade SPY and QQQ only.** The 15:50 rule needs the day's true high and
   low; IEX carries 2 to 3% of the tape, which is adequate for the two most traded ETFs and
   thin for XLK and IVW. Use `--trade-only close_weakness_reversion:SPY`, or `--feed sip`.
4. **Use an account that holds none of the traded symbols.** The runner refuses to open a
   position in a symbol that already holds shares it did not buy.
5. **Look at the journal after every session** (`report`). Live keeps running through the
   night because the reversion legs hold for up to five sessions.

What two days do not give you: a statistical forward test, a soak of the order path beyond a
handful of orders, or protection by the existing watchdog. The watchdog watches the trader's
runtime and configuration, not this runner; on a restart of the runner in paper mode it can
flatten the account while the lock is free, so stop it for planned restarts
(`docker compose stop watchdog`). In live mode the runner's own controls are the loss limit,
reconciliation, `halt` and `flatten`.

Short selling (the momentum leg) needs a margin account with shorting enabled; the preflight
checks both. FINRA replaced the pattern day trader rule with intraday margin standards on
4 June 2026 ([Regulatory Notice 26-10](https://www.finra.org/rules-guidance/notices/26-10)), so
the old 25,000 dollar minimum no longer applies; brokers may phase the change in until
October 2027, so check what your account shows.

## Review bands and the longer path

A forward run is judged against the backtest's own distribution of results over the same number
of sessions (`forward_bands_pct` in `results.json`, and the table in the registered report). For
60 sessions: QQQ momentum 1% / 5% / median = -4.9% / -3.2% / +1.1%; SPY reversion
-8.3% / -3.9% / +1.6%. A result below the 1% band, or a drawdown beyond 1.5 times the backtest
maximum, is the signal to halt and review. Scale size only while results stay inside the bands.

## Files

| file | role |
| --- | --- |
| `data.py` | session matrices; loaders for SIP month files, public index archives and adjusted daily bars |
| `strategies.py` | the rules, their decision functions and simulators |
| `grammar.py` | the hypothesis grammar: edges, parameter bounds, grids, neighbours, ids |
| `EDGES.md` | the catalogue of every edge, its premise and its variants (generated from `grammar.py`) |
| `discover.py` | the search protocol, the ledger, the edge book and the discovery report |
| `propose.py` | prompt, provider call and validation for model-proposed hypotheses |
| `stats.py` | per-session statistics: Newey-West t, sign-flip p, block bootstrap, deflated Sharpe, power, review bands |
| `run.py` | the registered-rule study |
| `fetch_public.py` | download the public diagnostic data |
| `live.py` | the forward runner: shadow, paper orders, live orders, preflight, controls, report |
| `results/` | committed runs on public data |
| `tests/research/test_session_edges_study.py` | 88 offline tests (in the repository's test tree) |

Nothing in `agent/` or `research/` imports this folder. The study and the engine need only the
Python standard library; the runner reuses `agent.alpaca_provider` and the runtime lock.
