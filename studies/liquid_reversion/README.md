# Liquid-ETF VWAP reversion study

Status on 20 October 2026: the study is built and unit-tested, and has been run only as two
**smoke runs on small public samples. Nothing in this folder is yet evidence for or against the
hypothesis.** The decision will rest on a walk-forward over ten years of Alpaca SIP one-minute
bars, which has not been run.

This is diagnostic research, kept apart from the paper-trial code: nothing in `agent/` or
`research/` imports it, and it imports neither. It needs only the Python standard library; the
optional fetcher also uses `requests` (already pinned in `requirements.lock.txt`).

## Hypothesis

> On the five most liquid US equity ETFs (SPY, QQQ, IWM, DIA, VTI), on days classified as range days from the first 30 minutes alone, a fade of a volatility-scaled deviation from the session VWAP, entered after 10:00 and targeting the VWAP, earns positive expectancy net of realistic per-symbol costs.

## Files

| file | role |
| --- | --- |
| `bars.py` | bar type, New York sessions, loaders for fetcher CSVs and the public JSON (parsed with `csv`/`json` only) |
| `daytype.py` | range-day classification at 10:00 |
| `strategy.py` | signals, entries, exits and the one-pass grid simulation |
| `costs.py` | round-trip cost model |
| `walkforward.py` | rolling windows, selection, sign-flip test, deflated Sharpe, the decision rule |
| `power.py` | sample-size and minimum-detectable-effect calculations |
| `run.py` | command line: simulate, select or smoke-test, write `report.md` and `results.json` |
| `fetch_sip.py` | command line: download Alpaca SIP bars into one gzip CSV per symbol and month |
| `results/smoke-1m/`, `results/smoke-5m/` | the two smoke runs (not evidence) |
| `tests/research/test_liquid_reversion_study.py` | 52 offline unit tests (in the repository's test tree) |

## Causal rules

Everything below is fixed in advance; only the five grid parameters in the next section are searched.

1. A bar's timestamp is its start. A bar is usable only from start + interval (`bars.py:66`,
   `return self.ts + timedelta(seconds=self.interval_seconds)`). Only regular-hours bars,
   09:30 to 16:00 New York, are kept (`bars.py:95`).
2. **Session VWAP** is cumulative typical price x volume over volume from 09:30, over completed
   regular-session bars (`strategy.py:165`,
   `price_volume += (bar.high + bar.low + bar.close) / 3.0 * bar.volume`).
3. **Volatility** is 1.4826 x the median absolute one-bar log return over the current session's
   completed bars plus the prior five sessions, in bps per minute (five-minute returns are divided by
   the square root of 5).
4. **Score**: z = (close - VWAP) / VWAP x 1e4 / (sigma x sqrt(60)).
5. **Day type** is known at 10:00. Drive = |close at 10:00 / session open - 1| and gap =
   |session open / prior close - 1|, each divided by its median over the previous 20 sessions (at least
   10 needed). Only bars that have ended by 10:00 are read (`daytype.py:74`,
   `if bar.end > cutoff:`), plus completed prior sessions. A range day has both ratios below
   `k_drive` and `k_gap`.
6. **Signals** come only from completed bars ending in [10:00, 15:00) New York
   (`strategy.py:225` and `strategy.py:227`, `if finished < first:` / `if finished >= last:`).
   A signal needs a range day, |z| >= `k_entry`, and a move to VWAP of at least `cost_multiple` x the
   round-trip cost. Short above VWAP, long below.
7. **One trade per symbol-session.** The first qualifying signal consumes the session even if its
   entry is then refused (`strategy.py:356`,
   `out[key] = Refusal(symbol, day, signal.signal_ts, planned, signal.z)`).
8. **Entry** is at the next bar's open, only if that bar starts before 15:00 (`strategy.py:260`,
   `if entry_bar.ts >= ny_instant(day, rules.entry_end):`). It is also refused if that open has
   already crossed the target (`strategy.py:264`).
9. **Target** is the VWAP frozen at the signal (`strategy.py:319`). **Stop** = entry +/- `stop_mult`
   x |entry - target| (`strategy.py:320`,
   `stop = entry - direction * stop_mult * abs(entry - target)`).
10. **Exits** are read from bar highs and lows, from the entry bar onwards. A bar that opens beyond a
    level fills at its open (`strategy.py:291`). Otherwise the stop is checked before the target, so
    the stop wins a same-bar tie (`strategy.py:294`). The time stop exits at the close of the bar
    ending 60 minutes after entry. The position is flat at the close of the bar ending 15:50
    (`strategy.py:270` to `strategy.py:272`).

Sessions with an early close are fed to the history but never traded, provided a calendar is
supplied (`--calendar`).

### Searched grid (108 points)

`k_drive` in {0.6, 0.8, 1.0}, `k_gap` in {1.0, 1.5, inf}, `k_entry` in {1.0, 1.5, 2.0},
`cost_multiple` in {1.5, 2.0}, `stop_mult` in {1.0, 1.5} (`daytype.py:29` and `daytype.py:30`,
`strategy.py:33` to `strategy.py:35`). Smoke runs use the middle point, fixed in advance
(`strategy.py:85`): `k_drive` 0.8, `k_gap` 1.5, `k_entry` 1.5, `cost_multiple` 1.5, `stop_mult` 1.5.

## Cost model

Round trip, in bps of entry notional ($25,000 per trade by default):

    max(one tick, measured spread) + 2 x slippage + SEC fee (sell leg only) + FINRA TAF (sell leg only)

There is no commission term. A round trip has exactly one sell leg: the exit of a long, or the
entry of a short. One tick is 1e4 x $0.01 / price bps; for SPY at $778 that is 0.1285 bps.

| component | value used | source and status |
| --- | --- | --- |
| spread | max($0.01 tick, measured quoted spread), one full spread per round trip (`costs.py:87`) | Measured spreads are optional (`--spreads`). Without them, one tick is used. The SEC's half-penny increment for tightly quoted stocks (amended Rule 612) is not yet in force: Release 34-105656 of 11 June 2026 moved its compliance date to the first business day of November 2027 ([Federal Register](https://public-inspection.federalregister.gov/2026-11997.pdf)). So $0.01 is the right floor for the whole data window. After that date it would overstate the cost by half a tick, about 0.06 to 0.18 bps per round trip on these ETFs, which works against the hypothesis. |
| slippage | 0.5 bps per side, so 1 bp per round trip (`costs.py:28`) | assumption, not measured |
| SEC Section 31 fee | $20.60 per million of sell notional, sell leg only (`costs.py:24`) | [SEC Fee Rate Advisory 2026-2](https://www.sec.gov/rules-regulations/fee-rate-advisories/2026-2). Read on 20 October 2026: "will be set at $20.60 per million dollars", "starting on April 4, 2026". The same page gives $0.00 per million before that date. |
| FINRA Trading Activity Fee | $0.000195 per share sold, capped at $9.79 per trade (`costs.py:25` and `costs.py:26`) | The 2026 rate. FINRA paused the fee for transactions from 1 October to 31 December 2026 ([SR-FINRA-2026-021](https://www.finra.org/sites/default/files/2026-09/SR-FINRA-2026-021.pdf)). It is 0.0025 to 0.0069 bps per round trip at the smoke-run prices, so it is immaterial either way. |
| commission | none | Alpaca API equity trading is commission-free (from the study brief, not re-verified here) |
| market data | SIP consolidated tape, raw (unadjusted) bars | [Alpaca Market Data API](https://docs.alpaca.markets/docs/about-market-data-api). On the free plan, only the latest 15 minutes of SIP data are restricted. |

Today's fee schedule is applied to every year of history on purpose: the question is whether the
edge survives the costs a trade would pay now. The SEC fee in fact moved between $0.00 and
$27.80 per million over the decade, so any one trade's fee is misstated by at most about 0.2 bps,
and the decade's average by a few hundredths of a bp. The cost stress multiplies the whole round
trip by 2.

Round trip at the median closes of the one-minute smoke sample, with no measured spreads:

| symbol | median close | spread | slippage | SEC fee | FINRA TAF | round trip (bps) |
| --- | --- | --- | --- | --- | --- | --- |
| SPY | 768.73 | 0.1301 | 1.0000 | 0.2060 | 0.0025 | 1.3386 |
| QQQ | 741.75 | 0.1348 | 1.0000 | 0.2060 | 0.0026 | 1.3434 |
| IWM | 281.56 | 0.3552 | 1.0000 | 0.2060 | 0.0069 | 1.5681 |
| DIA | 514.85 | 0.1942 | 1.0000 | 0.2060 | 0.0038 | 1.4040 |
| VTI | 378.15 | 0.2644 | 1.0000 | 0.2060 | 0.0052 | 1.4756 |

## Fetching ten years of SIP one-minute bars on the VM

Run from the repository root on the VM. Set the keys in the environment without putting them in a
file or in shell history:

    read -rs ALPACA_API_KEY && export ALPACA_API_KEY
    read -rs ALPACA_SECRET_KEY && export ALPACA_SECRET_KEY

    python -I studies/liquid_reversion/fetch_sip.py --out /data/sip-1min \
        --start 2016-10-01 --end 2026-09-30 --symbols SPY,QQQ,IWM,DIA,VTI --timeframe 1Min --calendar

What the fetcher does:

* It calls `GET https://data.alpaca.markets/v2/stocks/bars` with `feed=sip`, `adjustment=raw` and
  `limit=10000`, following `next_page_token` until it is null.
* It writes one gzip CSV per symbol and New York calendar month, for example
  `SPY_1Min_2016-10.csv.gz`. The files are atomic and byte-for-byte reproducible.
* It ends every request 16 minutes before now (`fetch_sip.py:57`). The free plan restricts only the
  latest 15 minutes of SIP data, so older history is available on the free plan.
* It paces requests to at most 200 per minute. HTTP 429, 5xx and network errors are retried with
  bounded exponential back-off, honouring `Retry-After`.
* The keys are sent only over HTTPS to `data.alpaca.markets`, `paper-api.alpaca.markets` or
  `api.alpaca.markets`, and a redirect is refused rather than followed, because `requests` would
  forward the `APCA-*` headers to the new host.
* The keys travel only in the `APCA-API-KEY-ID` and `APCA-API-SECRET-KEY` request headers
  (`fetch_sip.py:121`). They are never put in the query, printed, logged or written to disk. Error
  bodies are scrubbed of both keys before they reach a message (`fetch_sip.py:163`).
* A month is written as final only when it has ended and was requested in full. Anything else gets a
  `.partial` suffix and is fetched again on the next run; final months already on disk are skipped
  unless you pass `--force`. Start on the first day of a month and end on the last day of a
  completed month, and every file is final.
* `--calendar` also writes `calendar.json` from the Trading API calendar
  (`https://paper-api.alpaca.markets/v2/calendar`, same keys). The study uses it to skip early-close
  sessions.

The fetcher is tested only against a fake HTTP session (pagination, 429 retry, key handling). No
call to Alpaca was made while building this study.

## Running the walk-forward

    python -I studies/liquid_reversion/run.py --data /data/sip-1min --format alpaca --timeframe 1Min \
        --calendar /data/sip-1min/calendar.json --out studies/liquid_reversion/results/walkforward-1m

`--calendar` is required for fetcher data. The fetcher keeps all hours, so without the calendar the
bars after an early close (13:00 on the day after Thanksgiving, for example) would be read as regular
hours and traded. The run also stops if the calendar misses any session in the data, so fetch the
calendar for the whole data range.

Optional arguments:

* `--spreads spreads.json`: measured quoted spreads in bps per symbol, for example
  `{"SPY": 0.15, "QQQ": 0.16}`.
* `--train`, `--test`, `--min-trades`, `--permutations`, `--seed`: these change the preregistered
  design. Any departure is listed under `decision.design_deviations` in `results.json`, and the
  verdict is then never "deploy to paper".

Use the script path as shown. Under `-I`, `python -I -m studies.liquid_reversion.run` cannot find
the package from the working directory. The script inserts the repository root itself
(`run.py:18` to `run.py:21`).

With at least 630 sessions (one training block plus one test block), `run.py` runs the
walk-forward:

* rolling windows of 504 training sessions followed by 126 test sessions, stepping by 126, so each
  later session is tested exactly once (`walkforward.py:24` and `walkforward.py:25`);
* on each training block, the grid point with the highest session-clustered t among those with at
  least 60 training trades is selected. Only trades whose session lies in the training block are read
  (`walkforward.py:187`). The choice is applied unchanged to the following test block;
* the concatenated test blocks form one out-of-sample record. It is evaluated at 1x and 2x cost, with
  a one-sided sign-flip test over whole sessions (10,000 draws, seed 20261019);
* the deflated Sharpe ratio of each window's selection is reported as a diagnostic.

With fewer sessions, the run is a smoke run: no selection, the fixed middle grid point, and a
report headed "NOT EVIDENCE". The report and `results.json` stay under 200 KB each. The per-trade
list is included only up to 250 trades and only while the file stays under that size
(`run.py:50` and `run.py:51`).

From the smoke timing (22 sessions of one-minute bars in about 1.5 seconds, including parsing a
46 MB file), a ten-year run should take minutes rather than hours. That is an estimate; it has not
been measured on the full data.

## Preregistered decision rule

**Deploy to paper only if the out-of-sample session-level mean net bps > 0 with sign-flip
p < 0.05 AND the 2x cost stress stays > 0.**

`walkforward.decision` implements the three checks and publishes the unrounded numbers they used
under `decision.inputs`. `run.py` then applies the rule only to the preregistered design: a
walk-forward run on Alpaca one-minute data for the five symbols, with the default windows, minimum
training trades, permutation draws and seed, no partial month files, and a calendar covering every
session (`run.PREREGISTERED_DESIGN`). Otherwise `decision.applicable` is false, the departures are
listed in `decision.design_deviations`, and `deploy_to_paper` is false whatever the checks read. A
smoke run is never applicable.

The rule, the fixed rules above, the grid, the window lengths, the selection criterion and the cost
model are fixed before any full-data run. Changing any of them after seeing the out-of-sample record
would void the test.

What the third check means: the session mean at twice the cost is the net session mean minus one
more cost per trade. It is above zero only when the net effect per trade exceeds the mean round-trip
cost (about 1.43 bps here). The rule therefore tests "net expectancy above the cost", that is gross
expectancy above twice the cost, not merely "net expectancy above zero". That is deliberate: it is
the margin that protects against costs being underestimated. The power section below is computed
for the whole rule.

## Power

The table below comes from the session-level sd of net P&L measured in the two smoke runs.
Session P&L is the sum of that day's net bps across the five ETFs, one position's notional per
trade. It gives two sets of figures:

* **test only**: the one-sided significance test of the net session mean on its own;
* **whole rule**: all three checks together. Passing needs the net session mean above both the test's
  critical value and one cost per trade, so a true net effect at or below the mean cost (about
  1.43 bps per trade) never passes, at any sample size (`power.rule_required_sessions`).

* **Measured** on the smoke samples, at the middle grid point: session-level sd, trades per traded
  session and the share of sessions that trade.
* **Assumed**: one-sided alpha 0.05, 80% power, the normal approximation, independent sessions, the
  true effects listed (0.5, 1, 2 and 3 bps per trade), and 252 sessions per year for the year
  column.

A true effect of e bps per trade is a session effect of e x (trades per traded session).
Calendar sessions = traded sessions / traded share.

| | one-minute smoke | five-minute smoke |
| --- | --- | --- |
| traded sessions behind the sd (measured) | 7 of 11 | 18 of 31 |
| session-level sd, bps (measured) | 42.94 | 19.18 |
| 90% interval for that sd (chi-square) | 29.64 to 82.25 | 15.06 to 26.85 |
| trades per traded session (measured) | 2.14 | 1.72 |
| share of sessions that trade (measured) | 0.636 | 0.581 |
| mean cost per trade, bps (measured) | 1.43 | 1.43 |
| 0.5 bps net per trade, calendar sessions: test only / whole rule | 15,606 (about 62 years) / never | 5,283 (about 21 years) / never |
| 1 bp net per trade: test only / whole rule | 3,902 (about 15.5 years) / never | 1,321 (about 5.2 years) / never |
| 2 bps net per trade: test only / whole rule | 976 (about 3.9 years) / 1,380 (about 5.5 years) | 331 (about 1.3 years) / 465 (about 1.8 years) |
| 3 bps net per trade: test only / whole rule | 434 (about 1.7 years) / 434 (about 1.7 years) | 149 (about 0.6 years) / 149 (about 0.6 years) |
| minimum detectable effect, 2,500 calendar sessions: test only / whole rule | 1.25 / 1.85 bps per trade | 0.73 / 1.68 bps per trade |
| whole rule at 2,500 calendar sessions, across the 90% sd interval | 1.72 to 2.39 bps per trade | 1.62 to 1.77 bps per trade |
| minimum detectable effect, 1,996 out-of-sample sessions: test only / whole rule | 1.40 / 1.90 bps per trade | 0.81 / 1.70 bps per trade |

How to read it:

* Ten years of data give about 1,996 out-of-sample sessions, because the first 504 only train. On
  them the whole rule detects a true net effect of about 1.7 to 1.9 bps per trade with 80% power, and
  up to about 2.7 bps at the top of the one-minute sd interval.
* The significance test alone could see smaller effects (0.8 to 1.4 bps), but the 2x-cost check
  binds first: any true net effect below the mean cost of about 1.43 bps per trade fails it, however
  much data there is. An earlier version of this table showed the test-only figures as if they were
  the rule's; a review corrected that.
* In gross terms the rule needs about 3.1 to 3.3 bps per trade (net effect plus cost) to pass
  reliably.
* The sd rests on 7 and 18 traded sessions, so required sample sizes are uncertain by a factor of two
  to four (they scale with the square of the sd). The two samples also overlap in time.
* The full study uses one-minute bars, so the one-minute column is the more relevant design, but it
  rests on fewer sessions.

## Smoke results (NOT EVIDENCE)

**These are smoke runs. They check that the pipeline runs end to end on real bars. They are far too
small to support or reject the hypothesis, they are not out of sample, and no parameters were
selected.** The data are public consolidated-tape chart bars from the 15 October 2026 review, not
Alpaca SIP. The two samples overlap in time.

| | one-minute ([report](results/smoke-1m/report.md)) | five-minute ([report](results/smoke-5m/report.md)) |
| --- | --- | --- |
| sessions (known day type) | 22 (11), 2026-09-14 to 2026-10-13 | 42 (31), 2026-08-17 to 2026-10-14 |
| trades / traded sessions | 15 / 7 | 31 / 18 |
| net bps per trade, mean / median | -9.17 / -4.12 | 0.40 / -3.58 |
| gross / cost bps per trade (mean) | -7.74 / 1.43 | 1.83 / 1.43 |
| session-level net bps, mean / sd | -19.65 / 42.94 | 0.69 / 19.18 |
| session-clustered t | -1.364 | 0.152 |
| sign-flip p (one-sided) | 0.8227 | 0.4475 |
| session mean at 2x cost | -22.71 | -1.77 |
| exits | time 13, stop 2 | time 28, target 3 |

The decision rule does not apply to a smoke run: `results.json` records `decision.applicable:
false` and `deploy_to_paper: false`. For reference only, neither sample would pass it: both fail the
sign-flip check and the 2x cost check.

Regenerate them with:

    python -I studies/liquid_reversion/run.py --data PATH/bars_1m.json --format public \
        --out studies/liquid_reversion/results/smoke-1m
    python -I studies/liquid_reversion/run.py --data PATH/bars_5m.json --format public \
        --out studies/liquid_reversion/results/smoke-5m

## Tests

    python -I -m unittest discover -s tests/research -t . -p test_liquid_reversion_study.py -v

The 52 tests are offline and take about a second. They cover:

* the power arithmetic: 2 bps against an sd of 20 bps needs 619 sessions for the test alone, and
  873 for the whole rule at a cost of 1.43 bps; an effect at or below the cost never passes;
* the decision rule: unrounded inputs are published, and a smoke run or any departure from the
  preregistered design never yields "deploy to paper";
* early closes: fetcher data needs a calendar that covers every session, and post-close bars are not
  regular hours;
* tick and fee arithmetic, including the sell-only SEC fee;
* VWAP on a tiny session;
* the 10:00 day type, proved by corrupting later bars;
* no look-ahead at entry: corrupting bars after the signal, and checking every prefix of a session;
* one trade per symbol-session, including a refused first signal;
* stop-first ties, gap fills at the open, the 60-minute time stop and the 15:50 flat;
* walk-forward windows, and a selection that ignores poisoned test data;
* the fetcher's pagination, 429 retry and key handling, against a fake session, including refusing
  plain HTTP, non-Alpaca hosts and redirects.

`python -I -m unittest tests.research.test_liquid_reversion_study` does not work, because `-I`
removes the working directory from `sys.path`. `discover -t .` puts it back. Without `-I`, the
module form works from the repository root.

## Known limitations

* VWAP uses the typical price (high + low + close) / 3 for every bar, because the public data carries
  no per-bar VWAP. The fetcher saves Alpaca's per-bar `vwap` column, but the loader does not use it.
* If a minute bar is missing after a signal, the entry is taken at the next available bar's open,
  which is later than the signal. This is rare on these ETFs.
* Public chart files hold regular hours only and carry no calendar; on them, early-close sessions are
  traded and close at their last bar (`data_end`). Fetcher data cannot be run without a covering
  calendar.
* Slippage is assumed, not measured, and spreads default to one tick unless measured spreads are
  supplied.
* The bars are unadjusted (`adjustment=raw`), which is right for intraday P&L, VWAP and the tick
  floor. But the overnight gap on an ex-dividend morning includes the dividend, so a quiet
  ex-dividend day can be classified as a gap day and skipped. That is about 2% of symbol-sessions
  (about 5% for DIA, which pays monthly). It removes sessions; it does not bias the trades taken.
* Partial month files (the current month) are read if present, but a run that uses any is not the
  preregistered design, so its verdict is never "deploy to paper". End the fetch on the last day of
  a completed month.
