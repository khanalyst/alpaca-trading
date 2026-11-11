# Strategy audit and independent research, 11 November 2026

Scope: why no strategy in this repository has a positive edge, whether the code and the
documentation make sense for the goal, what the evidence says should be traded instead, and what
has to happen before paper and live trading. The code was read before any Markdown document; the
documents were then compared with it. Every number below was either computed from files in this
repository or produced by tests run for this audit and reproducible with
[`studies/session_edges`](../../studies/session_edges/README.md).

No performance is guaranteed by anything here. A strategy with a real edge still has losing
months and losing years; what can be controlled is the quality of the evidence and the size of
the bets.

## 1. Verdict

1. **The existing strategies are negative because they contain no gross edge, not because of a
   bug.** Across the 94 registered arms the mean gross return is -0.43 bps per trade, 42 of 92
   evaluable arms are positive before costs (a coin flip), the best clustered t-statistic is 1.30,
   and 94 independent arms of pure noise would be expected to produce a best t near 3. Measured costs of
   about 2.9 bps per round trip then turn a zero into a loss.
2. **The search cannot find an edge, and the test cannot recognise one.** The LLM may only
   re-tune twelve fixed one-minute indicator rules on 24 correlated ETFs, and the evidence floor
   of about 30 sessions can only detect an edge of roughly 4.7 bps per trade, several times
   larger than any documented intraday edge in these instruments.
3. **Edges do exist in the same instruments when the rule class changes.** Two published rules,
   run exactly as specified over 16 to 34 years, pass a registered decision rule net of costs:
   buying index ETFs on a weak close (t between 4.5 and 5.7 on five ETFs since the 1990s, stronger
   since 2020), and intraday momentum outside a time-of-day noise band on the Nasdaq-100
   (t 3.6 over 16 years, weaker since 2024). Together: Sharpe about 1.1, 16 of 17 positive years.
4. **There is now a search that finds them, and a runner that trades them.** A discovery engine
   tested 2,803 session-level hypotheses, took 114 to an untouched holdout and confirmed 9 under
   a correction for everything it promoted (section 12). A forward runner takes the resulting
   edge book through shadow, paper orders and live orders. 75 tests cover both. Neither has been
   run against the broker or the VM: this session had no Alpaca or Azure credentials.

## 2. Why every strategy is still negative

Evidence file: [`all-94-arms.csv`](../deep-audit-2026-11-10/all-94-arms.csv).

| cause | evidence |
| --- | --- |
| No gross edge in the rule class | Mean gross -0.43 bps per trade; 42 of 92 arms positive before costs; best t 1.30; 3 of 92 positive after costs. |
| The search space is closed | `agent/contracts/rule.py` defines 12 families (opening range, momentum, mean reversion, VWAP, volume and range breakouts) on one-minute bars. The discovery prompt in `research/llm_strategy.py` states that the grammar supplies no news, prior-day levels or hedge. The tuning prompt allows one field to move by at most 20% per attempt, at temperature 0, with at most 64 calls. The model is a hill-climber on a surface that is flat. |
| The instruments are not where intraday edges live | 24 broad and sector ETFs, 19 of them US equity funds that move together. These are the most efficiently priced instruments in the market at a one-minute horizon, and none is "in play" on a typical day. |
| The sample cannot detect a realistic edge | Median clustered standard error is 1.9 bps per trade, so the minimum detectable net effect at the tested sample is 4.7 bps per trade (80% power). Detecting 1 bp needs about 640 sessions; 2 bps needs about 160. The floors are 30 sessions. |
| Trades per day are not independent | At most one trade per symbol per session on symbols that share one market move: 720 trades is 30 observations. The code already clusters by session; the consequence is that only more sessions add evidence. |
| The exit geometry does not match the instrument | The stop floor is 30 bps (`MIN_STOP_DISTANCE_BPS`) while a one-minute ATR in these ETFs is about 3 bps, and the default target is 2R. Median share of trades that reach a target: 6%. Median share ended by the clock: 66%. Stops and targets are mostly decoration; the result is the drift over 30 to 90 minutes, which is zero. |
| The cost assumptions are 6 to 30 times reality | Static model 17 bps per round trip and a 25 bps stress veto (`COST_STRESS_REQUIRED_BPS`), against 2.85 bps measured on this universe and under 1 bp in SPY and QQQ. The relative stress of protocol 2 only applies when an expected cost is supplied, and the previous audit records that its producer is not wired, so the 25 bps veto is what runs. A 25 bps veto would also reject the weak-close rule, whose gross edge is 23 to 27 bps per trade. |
| Volume features use 2 to 3% of the tape | The IEX feed drives the volume, relative-volume and VWAP families. |
| The forward lane has produced nothing to judge | [`current-findings.md`](../current-findings.md): zero accepted sessions from 23 complete partitions, all 24 acceptance reports rejected, paper account paused. Readiness needs 210 recorded sessions. |

The earlier reviews fixed measurement defects one at a time. That was correct work, and it could
not change the answer, because the quantity being measured is zero.

## 3. Does the code make sense?

Read closely: `main.py`, `agent/strategy.py`, the signal evaluator and families in
`agent/contracts/rule.py`, the entry path of the factory simulator (`research/factory_core.py`),
the constants and structure of `research/gates.py` and `research/costs.py`, the LLM prompts and
bounds, the provider and order model, the configuration, compose file and CI. The remaining
modules were outlined, not re-audited line by line; three earlier audits did that.

**Sound.** One evaluator is shared by research and runtime. Signals read completed bars and
enter on the next bar. Orders are idempotent by client order id. Paper mode is enforced at the
provider. Statistics are clustered by session and corrected for false discovery. None of this
needs to change.

**Not sound for the goal.**

1. *The system optimises proof, not discovery.* `agent`, `research` and `deploy` hold about
   107,000 lines; the signal logic is about 4,500 (`rule.py`, `ibr.py`). `gates.py` is 5,678
   lines and `live_shadow.py` 9,289. Each audit added controls; none added a hypothesis with a
   reason to work.
2. *The LLM has no discretion that matters.* It cannot see data, cannot add a feature and cannot
   change the instrument, horizon or universe. What it can do, moving one parameter by 20% in
   response to a 30-session fit, is curve-fitting to noise with an audit trail.
3. *Evidence is collected forward only.* The factory learns from the recorder's corpus (17 to 30
   sessions). Alpaca serves ten years of SIP one-minute bars free once they are 15 minutes old,
   and `studies/liquid_reversion/fetch_sip.py` already downloads them, but no decision in the
   pipeline uses them. The one study designed for long history was never run.
4. *The cost and stop defaults assume a different market.* See section 2.
5. *Guardrails block the wrong thing.* The gates would stop a real edge of the size that exists
   in these instruments (2 to 4 bps per trade intraday, about 25 bps per swing trade) while the
   search keeps spending its budget on rules that cannot have one.

**Defect fixed in this change.** `.env.example` set `ALPACA_FACTORY_STRATEGIES=11` and described
"eleven" families while `factory_core.DEFAULT_STRATEGIES`, `compose.yaml` and
`deploy/research-cycle.sh` all use 12. Copying the example silently dropped the twelfth family.

## 4. Documentation versus code

The active documents (`README.md`, `ARCHITECTURE.md`, `OPERATIONS.md`, `SETUP.md`,
`research/README.md`, `research/protocol.md`, `deploy/README.md`, `docs/*.md`) were checked
against the code for the facts that drive behaviour: family count, universe, feed, stop floor,
stress scenarios, evidence floors, trial floors, polling interval, LLM provider and bounds, and
test counts.

* They agree with the code on every one of those, apart from the `.env.example` count above.
  The 2,801-test claim matches discovery (2,876 with the 75 new tests).
* They are accurate and not usable as a guide. About 400 KB of prose describes mechanisms; no
  page says in plain words that the search has found nothing and why. `current-findings.md` and
  the 10 November audit come closest.
* "Edge discovery" in `README.md` describes a process that, on the evidence, cannot discover.
  That is a design problem, not a documentation error, so the text was left alone and this
  report is linked from the catalogue instead.

## 5. What profitable intraday traders actually do

Base rates first. In the complete Taiwanese record, fewer than 1% of day traders were reliably
profitable after fees ([Barber, Lee, Liu and Odean, 2014](https://faculty.haas.berkeley.edu/odean/papers/day%20traders/Day%20Trading%20Skill%20110523.pdf)).
In Brazil almost all individuals who persisted lost money
([Chague, De-Losso and Giovannetti, 2020](https://ideas.repec.org/p/fgv/eesptd/525.html)).
The default outcome of intraday trading is a loss equal to costs.

The documented exceptions share four features, and the twelve families share none of them:

1. **They trade forced or concentrated flow.** Index-level intraday momentum is tied to hedging
   by option dealers and leveraged ETFs
   ([Baltussen, Da, Lammers and Martens, 2021](https://econpapers.repec.org/RePEc:eee:jfinec:v:142:y:2021:i:1:p:377-403)),
   which is largest in the Nasdaq-100. Stock day traders restrict themselves to names with a
   catalyst and abnormal volume
   ([Zarattini, Barbon and Aziz, 2024](https://ideas.repec.org/p/chf/rpseri/rp2498.html)).
2. **They decide a few times a day at fixed clock times**, against a time-of-day baseline, not on
   every minute bar ([Zarattini, Aziz and Barbon, 2024](https://ideas.repec.org/p/chf/rpseri/rp2497.html);
   [Zarattini and Aziz, 2023](https://papers.ssrn.com/abstract=4416622)).
3. **They hold winners to the close** with a small fixed risk, instead of a fixed 2R target.
4. **Their costs are a fraction of a basis point to a few basis points**, in instruments with
   one-cent spreads.

Separately, the largest and most persistent short-horizon effects in index ETFs are not intraday
at all: returns accrue overnight ([Lou, Polk and Skouras, 2019](https://researchonline.lse.ac.uk/id/eprint/119010)),
and a weak close tends to be followed by a recovery
([arXiv 2306.12434](https://arxiv.org/pdf/2306.12434), after Pagonidis, 2013). A platform that is
forced flat at 15:50 every day cannot earn either.

Published results are claims until reproduced. Section 6 reproduces them; several fail.

## 6. Independent tests run for this audit

Data: Nasdaq-100 and S&P 500 index one-minute archives 2010 to 2026; one-minute trades for VTI,
AAPL, AMZN (2004 to 2026) and IVE, IBM, WDC, OIH (2004 to 2026); adjusted daily bars from 1993;
hourly ETF bars for two years; five-minute bars for about 570 stocks over 28 sessions. Costs:
1 bp per round trip for SPY, QQQ and VTI, 2 to 4 bps for single stocks. Design period before
2020; holdout from 2020. Rules were run as published; parameter variations were used to check
robustness, never to select.

| # | hypothesis | result (annualised Sharpe, t) | holdout from 2020 | verdict |
| --- | --- | --- | --- | --- |
| 1 | Noise-band intraday momentum, Nasdaq-100 | 0.81, t 3.62; 14 of 17 years positive | 0.77 (0.43 since 2024) | **paper candidate** |
| 2 | Same, S&P 500 | 0.37, t 1.58 | 0.35 (-0.40 since 2024) | rejected |
| 3 | Same, VTI trades 2006 to 2026 | 0.65, t 2.95 | 0.08 | decayed |
| 4 | Same, AAPL and AMZN | 0.64 and 0.14 | 0.10 and -0.10 | decayed |
| 5 | Same, IBM, WDC, OIH, IVE | t -3.9, -2.9, -2.4, -0.7 | negative | rejected: single names reverse |
| 6 | Rule 1 on real hourly ETF bars, last two years | QQQ +3.2 bps per day (0.80); SPY -0.7; IWM -2.1; DIA -0.8; SMH +2.3; XLK +2.8. Daily profit correlation with the index proxy 0.95 | | confirms 1 and 2 and the proxy |
| 7 | Opening range breakout, 5 minutes, Nasdaq-100 | 0.58, t 2.40 | 0.54 | shadow only |
| 8 | Opening range breakout, S&P 500; 15 and 30 minute ranges | 0.23 or less | about zero | rejected |
| 9 | Opening range breakout, VTI, AAPL, AMZN | 0.51, 0.40, 0.46 | -0.23, -0.19, -0.05 | decayed |
| 10 | First half hour predicts last half hour | -0.68 and -0.95 after costs; gross about zero | negative | rejected |
| 11 | VWAP trend following | VTI -1.58 at one-minute switching, 0.40 at 30 minutes; IVE, IBM, WDC negative | 0.14 or less | rejected |
| 12 | Top-20 relative-volume opening range breakout, about 570 stocks, 28 sessions | -0.21R to -0.37R per trade; random selection about zero | | not supported; too short to conclude |
| 13 | Weak-close reversion decided at 15:50, one-minute data | SPY 0.77 (t 3.21); QQQ 0.76 (t 3.22); VTI trades 0.75 (t 3.67) | 0.92, 0.82, 1.08 | **paper candidate** |
| 14 | Same on daily bars at the close | SPY 0.86 (t 5.49, from 1993); QQQ 0.96 (5.70); DIA 0.84 (5.05); IWM 0.79 (4.48); VTI 0.91 (4.72) | 1.21, 1.26, 0.97, 0.68, 1.11 | supports 13 |
| 15 | Shorting a strong close | about zero | about zero | rejected |
| 16 | Overnight long, close to open | SPY 0.72 (t 4.55); QQQ 0.80 (t 4.57); drawdowns -41 to -43% | 0.55, 0.64 | real, but it is market risk held at night; not adopted as its own leg |
| 17 | Turn of the month | SPY 0.61 (t 3.63); QQQ 0.45; IWM 0.31 | 0.74, 0.55, 0.46 | weak; not adopted |

Reading the table:

* **Intraday momentum survives only at the Nasdaq-100 index level**, which fits the hedging-flow
  explanation. Everywhere else it worked until about 2019 and then stopped. It is the weaker of
  the two candidates, it breaks even near 4 bps per round trip, and it should carry less capital.
* **Weak-close reversion is the strongest and most stable result**: five ETFs, three data
  sources, three decades, stronger after 2020, and insensitive to costs (Sharpe about 0.63 at
  5 bps). Its weakness is that it is long only and holds overnight. It must be decided late:
  moving the decision from 15:50 to 15:30 cuts the S&P 500 result by a third and the VTI result
  by half.
* **Most published intraday results did not survive.** That is the expected fate of a mined
  effect and the reason for the holdout.
* **Multiple testing.** About ten rule families on about eight instruments were examined. Against
  a hurdle of t = 3.3 for 100 trials, rows 1 and 14 pass and row 13 passes with the support of
  row 14.

Combined book of rows 1 and 13 (momentum on QQQ at 100% of equity intraday, reversion split
between QQQ and SPY), 2010 to 2026, unlevered, net: annual return 14.5%, volatility 13.3%, Sharpe
1.09 (t 4.66), maximum drawdown -21.9%, 16 of 17 years positive, holdout Sharpe 1.11, bootstrap
95% interval 0.70 to 1.48, correlation between the two rules 0.10. The same period's S&P 500
buy-and-hold Sharpe was 0.79 with a -38.8% drawdown.

## 7. The strategies to run

| leg | instrument | rule | capital | status |
| --- | --- | --- | --- | --- |
| A | SPY | Weak-close reversion: at 15:50 buy if the price is in the bottom 20% of the day's range; sell at 15:50 on a later day once it is in the top half, or after 5 sessions | 25% of equity | trade on paper after confirmation |
| B | QQQ | Noise-band momentum: decisions every 30 minutes from 10:00 to 15:30, flat at 15:58 | 25% of equity | trade on paper after confirmation |
| C | QQQ | Weak-close reversion | none | shadow: enable as a traded leg in a symbol leg B does not use |
| D | QQQ | 5-minute opening range breakout | none | shadow only |

Section 12 replaces this table with the edge book the discovery engine writes; these four legs
remain the runner's defaults when no book is given.

Expected character on paper: leg A trades about 33 times a year and is in the market about a
fifth of the time; leg B trades about 0.8 times a day. Neither uses the 30 bps stop floor, the 2R
target or the stress veto, because none of those fits these rules; risk is controlled by position
size, the daily loss limit and the end-of-day exit.

Options are not part of this. The repository has only an indicative options feed, so no options
rule can be tested, and an untested options rule should not be traded.

## 8. Tests and data that were missing, and what replaces them

| missing | consequence | now |
| --- | --- | --- |
| Long-history test before forward recording | 30-session samples decide everything | `studies/session_edges/run.py`: 16 years of minute sessions, 34 years of daily bars |
| A design-stage power check | Experiments that could not succeed were run | `sessions_to_confirm_forward` in every result; the forward stage is defined as a fidelity test |
| Design and holdout split by date | No way to see decay | Fixed split at 2020 plus a since-2024 column and per-year results |
| Cost ladder in real units | 9 to 50 bps scenarios hide the 0.5 to 5 bps range that matters | Sharpe at 0, 0.5, 1, 2, 3 and 5 bps for every rule |
| Parameter neighbourhood | Mutation search finds lucky points | One-at-a-time neighbours must be positive in the holdout |
| Selection-aware significance | Best-of-many read at face value | t of at least 3, deflated Sharpe ratio, sign-flip p, block bootstrap interval |
| Live-versus-replay fidelity | Paper results cannot be compared with research | The runner stores the quote-based shadow result and the bar replay for every session |
| Review bands for a forward run | No rule for "is this normal?" | 1%, 5% and median cumulative result for 20, 60, 120 and 250 sessions from the backtest itself |
| Portfolio view | Arms judged alone | Correlations and the combined book |

Still needed, in order:

1. **SIP confirmation** of QQQ and SPY from 2016 (commands in the study README). If a verdict
   changes on SIP bars, the SIP result wins.
2. **Forward shadow** for at least 20 full sessions, then paper orders, under the gates in the
   study README.
3. **A full-universe SIP study** before any "stocks in play" rule is considered: one-minute bars
   for several thousand symbols from 2016, with a real-time scanner. The free IEX feed cannot
   support it live.
4. **An OPRA feed** before any options research.

## 9. What changed in the repository

| path | change |
| --- | --- |
| `studies/session_edges/` | Registered-rule study (`run.py`), discovery engine (`grammar.py`, `discover.py`, `propose.py`), forward runner with shadow, paper and live orders (`live.py`), public-data fetcher, README, committed results |
| `tests/research/test_session_edges_study.py` | 75 offline tests, including live-versus-simulator decision parity, order confirmation, reconciliation, halt and flatten, live-flag guards and the search protocol |
| `deploy/compose.session-edges.yaml` | Optional service for the shadow runner; validated with `docker compose config` against the base file |
| `.env.example` | Family count corrected from 11 to 12 |
| `docs/README.md` | This report and the study added to the catalogue |
| `README.md`, `ARCHITECTURE.md` | The no-overnight statements now say they are the trader's rule, and point to the separate study runner |

Nothing in `agent/`, `research/` or the existing deployment was modified. The existing trader,
its trial and its evidence are untouched.

The trader's `README.md` and `ARCHITECTURE.md` say that the session-edge runner is a separate
process that can hold a position across sessions when its orders are enabled.

## 10. Recommended decisions

1. Stop the autonomous factory on the twelve families. It is spending compute and LLM calls on a
   search with no gross edge, and its 17-session reruns cannot change that.
2. Run the SIP confirmation and start the shadow service. Both are read-only.
3. Keep the trader's execution, reconciliation, journal and watchdog. They are the valuable part
   of the existing code and the right home for legs A and B once they have passed the paper gate.
4. Recalibrate the cost model before anything is routed through the gates again: per-symbol
   measured costs with a 2x stress, not 17 and 25 bps.
5. Treat the LLM as a research assistant that proposes hypotheses with a mechanism and a
   citation, each tested on long history under the registered rule, rather than as a parameter
   tuner.

## 11. Limits of this audit

* No Alpaca or Azure credentials were present in this session. The VM was not inspected, no SIP
  bars were downloaded, and the runner has only been exercised against a simulated broker.
* The minute evidence for legs A and B is on index proxies and, for VTI, on real trades. The
  hourly cross-check supports the proxy; the SIP rerun is still required.
* Exploratory runs for rows 3 to 6, 9, 11, 12 and 15 to 17 used the same rule code on data that
  is not redistributed here; rows 1, 2, 7, 8, 10, 13 and 14 are reproduced by the committed study.
* Past performance of a rule does not bind its future. Leg B in particular shows decay.

## 12. Addendum: the discovery engine and the path to live

Added the same day, in response to the request for a system that can find a positive edge and
for live trading within two days.

**The engine** ([study README](../../studies/session_edges/README.md)). Eleven families of
session-level hypotheses, each with a stated mechanism and bounded parameters. Every hypothesis
is screened on the design period only (before 2020); one champion per family and instrument is
taken to the holdout; confirmation uses Holm at 5% across every champion ever promoted, a cost
stress and a positive result since 2024. A ledger keeps the count, so searching again raises the
bar. A language model can propose hypotheses inside the same grammar; it sees design statistics
only and its proposals join the same correction.

**What it found** on public data (47 ETFs of daily bars from the 1990s, two index minute
proxies from 2010): 2,803 hypotheses tested, 114 promoted, 9 confirmed, 12 provisional, 93 not
confirmed. All nine confirmed results are the same effect, buying a US equity index after
short-term weakness: close-weakness reversion on SPY, XLK, IVW, SSO and SOXL, down-streak
reversion on SPY, QQQ and IVW, and RSI reversion on IVW, with holdout t between 3.3 and 3.9.
Nothing outside equities was confirmed. No intraday rule survived the correction; QQQ momentum
enters the edge book only as a registered rule.

**What that means.** The system can find a positive edge, and on this evidence there is one
robust edge with several expressions, plus one weaker, uncorrelated intraday edge. That is a
narrower book than "a few strategies" suggests: in a sharp sell-off every reversion leg is long
at once. Position size, not the number of legs, is the risk control.

**The edge book** from public data trades close-weakness reversion on SPY, XLK and IVW at
16.7% of equity each and QQQ momentum at 25%; leveraged funds and duplicate underlyings are
recorded only.

**Live in two days.** The runner now has a live order mode behind four independent switches
(`--mode live`, `--live-orders --confirm LIVE`, a `--max-notional` cap, and the provider's
`ALPACA_LIVE_ENABLE=true` guard), a read-only preflight, broker reconciliation on every poll,
fills confirmed by order lookup, a daily loss limit, and `halt` and `flatten` controls. The
two-day sequence is in the study README: SIP confirmation, one full paper session, then live.

My assessment of that timetable, stated plainly:

* The statistical case does not depend on the two days. No forward window of practical length
  can add to or subtract from 30 years of evidence.
* The operational case does. The runner has been tested against a simulated broker only. One
  paper session exercises a handful of real orders. That is thin for real money, so the first
  live weeks should use the reversion legs only, a notional cap of about a tenth of the target
  size, and the feed-appropriate symbols (SPY and QQQ on IEX).
* If the SIP rerun does not reproduce the edge book, or the paper session leaves an unexplained
  problem event, the correct decision is to wait, whatever the calendar says.
