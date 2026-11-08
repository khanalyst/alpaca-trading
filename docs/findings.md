# Findings and research results

Updated 8 November 2026, data to 6 November 2026. This is the single findings document for the repository. It replaces the dated findings reports, which are listed with permanent links in [section 10](#10-removed-documents).

## 1. Bottom line

1. **No strategy in the repository is a confirmed, tradeable edge.**
2. **The intraday catalogue is dead after costs.** Every one of its 82 registered arms (12 rule families and IBR, across all cohorts) was replayed on fresh data in October 2026. None is significantly positive net of realistic costs, and the corrected `intraday-mechanisms.v3` cohort changed nothing ([section 7](#7-earlier-findings-consolidated)).
3. **A new automated search** (`edgefinder/`) scanned 443 daily candidates in 11 documented families on 24 ETFs, from 2001 to 2026, under a protocol fixed in code before the first run.
   - No candidate passed all five holdout confirmation tests.
   - The best-supported is **overnight VTI with a 200-day trend filter**: buy at the closing auction, sell at the next opening auction, only while VTI is above its 200-day average.
   - Over the 2016 to 2026 holdout it earned 7.6% a year over bills, Sharpe 1.00, alpha 5.6% a year (t 2.63), maximum drawdown -13.5%. It passes four of the five tests and fails only the deflated Sharpe ratio across 443 trials.
4. **About 1.6 bps of its 5.4 bps a night is a print-location effect, not extra return.** VTI's overnight leg beats SPY's by that much (t 7.1) and its intraday leg gives the same amount back, with no close-to-close difference. That part pays only if auction orders receive the printed open and close, and Alpaca staff confirm that the printed open (the first valid trade) and the opening auction price can differ ([section 5](#5-the-best-supported-candidate-in-detail)).
5. **The automatic selection itself works out of sample.** Re-run every January on past data only, from 2007 to 2026, it earned alpha of 4.5% a year (t 2.78), Sharpe 0.71 against 0.54 for SPY on the same days. Because it is out of the market for most of each day, its compound return was lower: 7.4% a year against 10.7%.
6. **An automated trader for the pick now exists.** It is paper only, by rule, until the pick is confirmed. The decisive next test is cheap and needs only an Alpaca key: re-price the strategy at the official auction prints (`python -m edgefinder.auctions`).

## 2. The gap between the repository and the goal

The goal: find the best edge automatically, see its results and caveats, and trade it automatically and profitably.

| part of the goal | state before this change | the gap |
| --- | --- | --- |
| a profitable strategy | 82 intraday arms; none profitable after realistic costs | the search space: intraday trades on liquid ETFs, where the gross structure found (2 to 3 bps) is about the size of a round trip |
| the one premium the research had found | overnight drift (SPY +3.57 bps a night, t 3.39 over 16 years) | the runtime is flat by 15:50 and its watchdog flattens anything unknown, so it can never hold overnight |
| find edges automatically | the factory searches a closed intraday grammar on self-recorded IEX sessions | a first proof needed 100 or more recorded sessions (mid-January 2027 at the earliest); the gates were built on a 17 bps cost model that needs about 25 bps of gross edge to pass; the live-shadow lane had several independent ways to stall |
| see results and caveats | six dated reports, about 150 files of findings and evidence | no single current view; the latest conclusions were spread over three documents that each corrected the last |
| trade it automatically | the runtime trades only ledger records with a verified proof | no proof exists, and the paper account was paused and flat at the last verified check (11 October) |

Root causes, in order of weight:

1. **Where the search looked.** Every hypothesis was intraday, flat by the close, on the most efficient instruments there are. The holding period was too short for any premium to exceed costs.
2. **How the evidence was gathered.** Research waited for self-recorded sessions instead of using decades of public history, so each question cost months.
3. **What was built first.** About 200,000 lines of governance (ledgers, proofs, shadow parity, online false-discovery accounting) were built ahead of any edge for them to govern.

What this change does about it:

- **New search space.** A daily catalogue that can hold overnight, across sessions and across months, where the edge-to-cost ratio is favourable.
- **New evidence base.** 25 years of public daily data, plus two years of hourly bars, used at once.
- **Honest statistics.** Selection on discovery data only, a one-shot holdout, joint multiple-testing control, and a walk-forward of the selection procedure itself.
- **A trader built for it.** It uses the same decision code as the research, on a separate account, so parity holds by construction.

What it does not change:

- the intraday runtime, the research factory and the preregistered v2 tests, which are untouched and keep their identity hashes;
- the size of the system, whose slimming is the open proposal in [core-design.md](core-design.md).

## 3. How the automated search works

Full detail is in [edgefinder/README.md](../edgefinder/README.md). In short:

| step | what happens |
| --- | --- |
| data | public daily bars since each fund's inception (SPY from 1993), split into an overnight leg (close to next open, dividend included) and an intraday leg (open to close); the 13-week bill rate per leg. Rebuilt close-to-close returns match the provider's adjusted closes to a median 0.002 bps |
| costs | `per_symbol_v2`: one full spread, 0.5 bps slippage a side, sell-side SEC and FINRA fees. SPY 2.18 bps, VTI 1.99 bps a round trip |
| catalogue | overnight, intraday (long and short), weekday overnight, 50 and 200-day trend, 6 and 12-month time-series momentum, cross-sectional momentum (18 variants), dual momentum, RSI(2) reversal, turn of the month, pre-holiday, volatility targeting: 443 candidates |
| edge measure | alpha information ratio against buy and hold of the candidate's own funds, net of costs and bills, Newey-West errors |
| discovery | 1 June 2001 to 31 December 2015, with SPA, reality check, deflated Sharpe, PBO and Holm/BY over the whole catalogue |
| confirmation | the top ten by discovery rank, once, on 2016 to November 2026, five tests |
| walk-forward | re-select every January with past data only, hold for the year |

Every decision on day `t` uses data through the close of `t-1` only, because a closing-auction order must be sent before 15:50. A test perturbs and deletes all future data for every family; injecting a one-day look-ahead into any of five families makes it fail.

## 4. Results

Full report: [edgefinder/results/report.md](../edgefinder/results/report.md). Every number is in `edgefinder/results/scan.json`.

### 4.1 Multiple testing over the whole catalogue (discovery window)

| test | result | reading |
| --- | --- | --- |
| Hansen SPA, null: no candidate has alpha | p 0.001 | something in the catalogue beats buy and hold, after accounting for everything tried |
| White reality check, same null | p 0.10 | the older, less powerful test does not reject |
| probability of backtest overfitting (CSCV) | 0.34 | a third of the time the discovery winner lands in the bottom half out of sample |
| significant after Holm, 5% | 5 of 443 | all overnight or intraday legs of VTI, VO, VB and HYG |
| rank correlation, discovery against holdout | 0.27 | weakly persistent |

### 4.2 The ten best discovery candidates on the holdout

| rank | candidate | holdout alpha a year (t) | holdout Sharpe | gross edge / cost | tests passed | verdict |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | overnight HYG, trend filter | -5.1% (-4.58) | -1.23 | 0.1x | 0 | rejected |
| 2 | overnight VO, trend filter | 4.3% (2.04) | 0.79 | 2.2x | 3 | weak |
| 3 | overnight HYG | -7.6% (-5.22) | -1.00 | 0.0x | 0 | rejected |
| 4 | short HYG intraday | -8.1% (-5.48) | -1.66 | negative | 0 | rejected |
| 5 | **overnight VTI, trend filter** | **5.6% (2.63)** | **1.00** | **2.9x** | **4** | **weak, the pick** |
| 6 | overnight VB | 7.9% (2.68) | 0.93 | 3.7x | 4 | weak |
| 7 | short VB intraday | 7.6% (2.60) | 0.08 | 1.3x | 3 | weak |
| 8 | overnight VB, trend filter | 8.6% (3.63) | 1.22 | 4.0x | 4 | weak |
| 9 | overnight XLU, trend filter | -5.6% (-2.64) | -0.77 | 0.3x | 0 | rejected |
| 10 | overnight VO | 2.0% (0.77) | 0.53 | 2.0x | 2 | rejected |

The discovery winner, overnight HYG, reversed sign out of sample (alpha t -4.6). That is the confirmation stage doing its job, and the reason no single backtest should be traded on its own say-so.

### 4.3 Families

| family | median holdout alpha IR | members positive in holdout | reading |
| --- | --- | --- | --- |
| pre-holiday | 0.37 | 23 of 23 | the most consistent effect, but it holds about 9 sessions a year; median alpha about 1% a year |
| turn of the month | 0.09 | 76% | consistent sign, small |
| 200 and 50-day trend | 0.09 | 67% | mostly risk reduction, little alpha |
| RSI(2) reversal | 0.06 | 58% | weak once entries are causal (next open, not the signal close) |
| volatility targeting | 0.06 | 63% | better Sharpe than buy and hold on SPY (0.78 against 0.72), alpha t about 1 |
| cross-sectional momentum | 0.06 | 61% | no reliable alpha on 24 ETFs |
| weekday overnight | 0.03 | 53% | noise |
| time-series momentum | 0.02 | 54% | noise |
| dual momentum | -0.06 | 0 of 2 | negative since 2016 |
| overnight | -0.23 | 39% | strong for VTI, VB and VO; weak or negative after costs for SPY, QQQ and most sectors |
| intraday | -0.83 | 13% | the intraday leg loses money net of costs almost everywhere |

### 4.4 Hourly last-half-hour momentum (two years)

All 50 candidates (the return to 15:30, or the first hour's return, predicting the last half hour) were negative in the holdout year after costs. The best discovery candidate has t 0.70 and a BY-adjusted p of 1.0. The 30-minute hold cannot carry a 2 to 3 bps round trip.

## 5. The best-supported candidate in detail

`overnight:VTI:filter=sma200`. Each session, if VTI's total-return index closed above its 200-day average the day before, buy at the closing auction and sell at the next opening auction. It holds about 207 nights a year.

| window | years | excess over bills | volatility | Sharpe | alpha (t) | beta | max drawdown |
| --- | --- | --- | --- | --- | --- | --- | --- |
| discovery 2001 to 2015 | 13.7 | 7.7% | 6.3% | 1.22 | 7.0% (4.47) | 0.10 | -13.0% |
| holdout 2016 to 2026 | 10.8 | 7.6% | 7.6% | 1.00 | 5.6% (2.63) | 0.16 | -13.5% |
| VTI buy and hold, holdout | 10.8 | 12.4% | | 0.69 | | 1 | -35.4% |

Per held night in the holdout:

- gross overnight return 6.42 bps;
- modelled cost 1.98 bps;
- net of cost and bills 3.69 bps.

The edge covers 2.9 times its modelled cost. At twice the modelled cost, holdout alpha falls to 1.5% a year.

### 5.1 Why part of it may be real

The overnight premium is one of the best documented regularities in equities. Cliff, Cooper and Gulen found US stock and ETF returns accrue overnight, driven in part by high opening prices that fall back in the first hour. Lou, Polk and Skouras show the split persists for years and links it to a tug of war between clienteles.

Berkman, Koch, Tuttle and Zhang tie the pattern to retail buying at the open: the opening price is high relative to the rest of the day, especially when retail attention is high. Selling into the opening auction is then a form of liquidity provision that is paid for. On SPY alone, overnight returns were +3.88 bps a session in the holdout against +2.07 intraday, and the 200-day filter keeps the nights that average +4.45 bps and drops those that average +0.97.

### 5.2 Why part of it may not be

VTI and SPY own largely the same stocks and have the same daily return. Yet VTI's overnight leg beats SPY's and its intraday leg gives the lead back:

| period | VTI minus SPY, overnight | VTI minus SPY, intraday | VTI minus SPY, close to close |
| --- | --- | --- | --- |
| 2002 to 2005 | +7.31 bps | -6.65 bps | +0.64 bps |
| 2006 to 2010 | +3.47 bps | -3.10 bps | +0.29 bps |
| 2011 to 2015 | +2.17 bps | -2.24 bps | -0.08 bps |
| 2016 to 2020 | +1.32 bps | -1.17 bps | +0.14 bps |
| 2021 to 2026 | +1.77 bps | -2.11 bps | -0.34 bps |

That is a statement about where VTI's open and close are printed, not about what it earns. It has shrunk as ETF markets tightened. It is not caused by stale opens: excluding the 30 holdout nights whose open equals the prior close moves the net edge from 3.69 to 3.78 bps.

Whether it can be captured depends on execution, and here the risk is concrete. According to an Alpaca staff reply on the Alpaca forum:

- a market-on-open order fills at the primary listing exchange's opening auction;
- the daily bar's open is the first valid trade of the day, which can be a small print on another venue (their examples differ by 17 to 22 cents);
- Alpaca's paper trading simulates an opening order as a market order at the opening quote.

**So a paper trial cannot test this. Historical auction prints can.** `python -m edgefinder.auctions` keeps the strategy's decisions on the bars and re-prices its legs at the largest official auction print of each session since 2016. If the auction-priced alpha is still positive with t of at least 1.96, the print question is settled in the strategy's favour.

### 5.3 What the paper trial can and cannot show

At the holdout's nightly noise (52.8 bps) the return cannot be confirmed quickly:

| held nights | standard error | t at the holdout mean |
| --- | --- | --- |
| 60 | 6.8 bps | 0.54 |
| 250 | 3.3 bps | 1.11 |
| 1,000 | 1.7 bps | 2.21 |

A t of 2 needs about 818 nights, roughly four years. The trial is therefore an execution check, not a return test. The trader's `status` reports fill shortfall against the official auction prints and a pre-declared verdict:

- `pending` until 120 legs;
- `pass` if the mean round-trip shortfall is at most 1 bps with its upper 95% bound at most 2 bps;
- `stop` if the lower bound exceeds 2 bps.

The verdict tests the auction assumption only on live fills; on paper it tests the trader's mechanics and Alpaca's simulator.

## 6. Caveats

Specific to the pick:

- It fails the deflated Sharpe test: with 443 trials whose Sharpe ratios are this dispersed, the best discovery result is not distinguishable from the luckiest null. It passes the holdout tests, which are cleaner, but the selection used the discovery window of a catalogue that I chose.
- One round trip on every held session (207 a year): any execution shortfall repeats daily, and every gain is short term for tax.
- Gap risk while the exchange is shut cannot be capped by a stop. The trend filter limited the worst drawdown to 13.5%, and 2022 and 2023 were both losing years (-6.3% and -8.5%).
- It returns less than holding VTI in a rising market (7.6% against 12.4% a year over bills in the holdout) with far less risk. Matching the index's return would need leverage, which brings margin costs not modelled here.

General:

- The data are public consolidated prices from an unofficial endpoint, not the auction prints.
- One cost schedule (September 2026 spreads at 2026 prices) is applied to every year; spreads were wider in the 2000s, which flatters early years for high-turnover families.
- The 24-fund universe was chosen in 2026, and all its funds survived.
- Bills are assumed to be earned on idle cash. If the broker pays nothing on it, the pick's holdout compound return is 9.3% a year rather than 10.1%.
- Every family and grid was written before the first run, and no protocol constant changed after it. The tie-break that picks among `weak` candidates (most tests passed, then discovery rank) was added after the first run was seen.

## 7. Earlier findings, consolidated

**September 2026 audit (24 rule arms, 19 public sessions).**

- The 17 bps shipped cost model was about six times a realistic round trip (2.8 bps).
- Re-picking exit geometry was 85% overfitting: +2.68 bps in sample became +0.48 held out.
- Opening-range fade and VWAP reversion led on control-adjusted signal quality. They were preregistered as `*-control-adjusted.v1` on 22 and 23 September.

**15 October review (82 arms, 22 fresh sessions).**

- No arm significantly profitable; 47 significantly negative; the average arm -4.4 bps a trade. The best (opening-range fade, t 1.59) is what chance produces from 82 tries.
- Runtime defects that could lose money or leave positions open were found: reused close order ids (B-01), unbooked flatten closes (C1-01), unprotected positions after a failed flatten (C1-02), a paused trader skipping the 15:50 flat (C2-01), whole-book liquidation on one transient error or rejection (B-03, B-04), and bracket legs anchored away from the fill (B-02).
- The first six were fixed with tests between 19 and 24 October. Two further adversarial rounds then found and fixed 15 more defects, five of them introduced by the fixes. B-02, B-07 (a restart flattens the whole book) and B-08 (entry limits that never expire) are not in that list.
- The research machinery was judged unable to confirm an edge as built, and the system (about 202,000 lines) far larger than the 10,000 to 15,000 line core it needs.

**26 October refresh (30 sessions, 8 of them unseen).**

- Still no edge. The arms' ranking inverted in the fresh window (rank correlation -0.35), which is what regime noise looks like.
- Over long samples the overnight premium was the one robust result: SPY +3.57 bps a night (t 3.39), QQQ +5.04 (t 4.12) and IWM +5.19 (t 4.02) over 16 years.
- Market intraday momentum, fading large opening gaps and cross-sectional first-hour reversal showed nothing.
- Intraday mean reversion weakened, with its 60-minute variance-ratio interval now including 1.

**28 October gap closure.**

- The review's signal, cost, statistics and gate findings were addressed as opt-in versions: `per_symbol_v2` costs, gate protocol 2, `rule-strategy.v7` corrections and preregistered v2. Several are only partly closed. An adversarial review of that work found 21 defects in the new code, all fixed.
- The corrected cohort `intraday-mechanisms.v3` still has no positive arm over 30 sessions; three are significantly negative.
- Open items are listed in section 9.

**Preregistered hypotheses.**

- The v1 hypotheses are compromised: the decision rule changed after sealing.
- `vwap-reversion-control-adjusted.v2` and `opening-range-fade-control-adjusted.v2` are sealed from 29 October and accruing. Look 1 needs 30 complete IEX sessions, so mid-December 2026 at the earliest. See [preregistered-hypotheses.md](preregistered-hypotheses.md).

**Liquid-ETF reversion study.**

- Built and tested, with two smoke runs only (-10.3 and -2.5 bps a trade). The decision needs the ten-year SIP walk-forward described in [its README](../studies/liquid_reversion/README.md).

**Deployment.**

- At the last verified check (11 October 2026) the paper account was paused and flat, with no strategy eligible.
- I don't know the account's state since then; it lives on the VM.

## 8. Automated trading: what runs and how

The commands, phases, checks and deployment are in [edgefinder/README.md](../edgefinder/README.md). In short:

- Use a **dedicated** Alpaca paper account, never the intraday trader's: that runtime force-flats at 15:50 and its watchdog flattens unknown positions.
- `python -m edgefinder.trader loop --submit` runs two phases each trading day:
  - market-on-open orders between 08:00 and 09:27 New York time;
  - market-on-close orders 25 to 12 minutes before the close.
- It recomputes the frozen candidate's weights with the research's own code. It refuses on stale data, foreign positions, a halt file or a 20% drawdown, and its deterministic order ids make a retry harmless.
- Live mode needs a `confirmed` spec. The current pick is `weak`, so the trader refuses live for it.

## 9. What to do next, in order

1. **Run the auction check** (one command, any Alpaca key, minutes):
   - `survives`: the pick stands on the prices an order would get.
   - `weakened` or `fails`: the print-location share was the edge. Drop to the SPY-like core of about 2 bps a night, or move on.
2. **Run the trader on a dedicated paper account.** This proves the mechanics, not the edge.
3. **If the check survives, run a small live pilot** (a few thousand dollars, `--capital-fraction`). This is the only way to measure real auction fills. It needs a deliberate edit of `live_allowed` in the spec, which should be a reviewed commit, and it should stop on the trader's `stop` verdict.
4. **Re-run the scan as data accrue** (`--refresh`); the holdout grows by about 250 sessions a year.

Options worth weighing:

- **Lower turnover, smaller edge.** Pre-holiday holds were positive in the holdout for all 23 funds tested, at about 9 sessions a year, so costs barely matter; the alpha is about 1% a year.
- **Return target.** The overnight pick earns the market's premium with a sixth of its beta. Leverage could raise the return, but margin interest and gap risk scale with it.
- **The intraday stack.** The evidence argues for the reduction in [core-design.md](core-design.md) rather than more cohorts. The preregistered v2 looks are the one intraday test still worth waiting for.

Open items carried from the 28 October gap closure:

- the V7 corrections are research-only;
- `run_factory` cannot dispatch the v3 cohort;
- factory replay has no prior-session context;
- producers do not stamp `expected_round_trip_bps` or `comparison_id`;
- the factory and gates default to `static_v1`;
- LORD++ v7 does not unblock the first confirmatory tests;
- the runtime does not journal the decision-time mid;
- `research/ibr.py` compares a raw stop with a cent-rounded floor.

## 10. Removed documents

Removed on 8 November 2026 because they recorded findings that this page now summarises. Each link opens the file at commit `21d9729`, the last that holds it.

| removed | what it held |
| --- | --- |
| [docs/current-findings.md](https://github.com/khanalyst/alpaca-trading/blob/21d97293455b42ac2ef7298bf814b09b68680200/docs/current-findings.md) | the 11 October deployment snapshot |
| [docs/audit-2026-09-21/](https://github.com/khanalyst/alpaca-trading/tree/21d97293455b42ac2ef7298bf814b09b68680200/docs/audit-2026-09-21) | the September audit, its scripts and JSON results |
| [docs/review-2026-10-15/](https://github.com/khanalyst/alpaca-trading/tree/21d97293455b42ac2ef7298bf814b09b68680200/docs/review-2026-10-15) | the 15 October review, 27 handoffs, verification scripts and the review's research |
| [docs/research-2026-10-26/](https://github.com/khanalyst/alpaca-trading/tree/21d97293455b42ac2ef7298bf814b09b68680200/docs/research-2026-10-26) | the 26 October performance refresh |
| [docs/research-2026-10-28/](https://github.com/khanalyst/alpaca-trading/tree/21d97293455b42ac2ef7298bf814b09b68680200/docs/research-2026-10-28) | the 28 October gap closure and v3 cohort replay |
| [studies/liquid_reversion/results/](https://github.com/khanalyst/alpaca-trading/tree/21d97293455b42ac2ef7298bf814b09b68680200/studies/liquid_reversion/results) | the two smoke runs |

Two files were moved rather than removed:

- `docs/audit-2026-09-21/spread-reference.json` is now `tests/research/fixtures/spread-reference.json`, because a cost test pins the reference spreads to it;
- `docs/review-2026-10-15/core-design.md` is now [docs/core-design.md](core-design.md), because it is a design proposal, not a finding.

Frozen strings inside registered research modules still name the old paths. They are left as they are because editing those modules would change the preregistered instrument digest; the links above resolve them.

## 11. Sources

- Alpaca, [Orders at Alpaca](https://docs.alpaca.markets/docs/orders-at-alpaca): `opg` orders are rejected from 09:28 to 19:00 New York time, and `cls` orders from 15:50 to 19:00.
- Alpaca, [historical auctions endpoint](https://docs.alpaca.markets/reference/stockauctions-1).
- Alpaca forum, [OPG orders not filled at open price (live trading)](https://forum.alpaca.markets/t/opg-orders-not-filled-at-open-price-live-trading/14672): opening orders fill at the primary exchange's auction; the bar open is the first valid trade; paper simulates opening orders at the opening quote.
- Alpaca forum, [daily bar prices against auction prices](https://forum.alpaca.markets/t/open-close-daily-bar-prices-vs-open-close-auction-prices-on-primary-exchange/14227).
- Cliff, Cooper and Gulen, "Return differences between trading and non-trading hours: like night and day" (2008); summary at [CXO Advisory](https://www.cxoadvisory.com/calendar-effects/buy-at-the-close-and-sell-at-the-open).
- Lou, Polk and Skouras, "A tug of war: overnight versus intraday expected returns", Journal of Financial Economics 134(1), 2019 ([LSE accepted version](https://personal.lse.ac.uk/polk/research/TugOfWar.pdf)).
- Berkman, Koch, Tuttle and Zhang, "Paying attention: overnight returns and the hidden cost of buying at the open", Journal of Financial and Quantitative Analysis 47(4), 2012 ([Cambridge](https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/paying-attention-overnight-returns-and-the-hidden-cost-of-buying-at-the-open/F9AAD159B512C651F09D5D52011D88E0)).
- Bailey and Lopez de Prado, "The deflated Sharpe ratio", Journal of Portfolio Management 40(5), 2014 ([SSRN](https://papers.ssrn.com/abstract=2460551)).
- Bailey, Borwein, Lopez de Prado and Zhu, "The probability of backtest overfitting", Journal of Computational Finance ([SSRN](https://papers.ssrn.com/abstract=2326253)).
- Hansen, "A test for superior predictive ability", Journal of Business and Economic Statistics 23(4), 2005 ([doi:10.1198/073500105000000063](https://doi.org/10.1198/073500105000000063)).
- White, "A reality check for data snooping", Econometrica 68(5), 2000.
- Politis and Romano, "The stationary bootstrap", Journal of the American Statistical Association 89, 1994.
- Newey and West, "A simple, positive semi-definite, heteroskedasticity and autocorrelation consistent covariance matrix", Econometrica 55(3), 1987.
- The family references (Moskowitz, Ooi and Pedersen 2012; Moreira and Muir 2017; Lakonishok and Smidt 1988; Ariel 1990; Connors and Alvarez 2008; and others) are listed beside each family in the report.
