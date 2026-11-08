# edgefinder: automated edge search and an auction trader

`edgefinder` searches a catalogue of documented daily strategy hypotheses on 25 years of public data, decides which (if any) is an edge by a protocol fixed in code, and trades the result through Alpaca at the opening and closing auctions.

It is separate from the intraday system. Nothing in `agent/`, `research/` or `deploy/` imports it, so it cannot change the preregistered v2 instrument digest or the paper-trial identity. The findings it produced are summarised in [docs/findings.md](../docs/findings.md); the latest full report is [results/report.md](results/report.md).

## What is in the package

| module | role |
| --- | --- |
| `data.py` | daily bars since inception and hourly bars for two years (public chart endpoint), or Alpaca SIP daily bars; cached under `runtime/edgefinder/cache`; every session split into a dividend-aware overnight leg and an intraday leg, with the 13-week bill rate split by calendar time |
| `costs.py` | the repository's `per_symbol_v2` round trip: one full spread, 0.5 bps slippage a side, SEC and FINRA fees on the sell leg (SPY 2.18 bps) |
| `catalogue.py` | 11 daily families, 443 candidates, each with its mechanism and references |
| `backtest.py` | one engine for every candidate; costs charged at each auction where the weight changes |
| `stats.py` | Newey-West alpha, deflated Sharpe, PBO by CSCV, Hansen SPA and White's reality check, Holm and Benjamini-Yekutieli |
| `discover.py` | the protocol and the scan |
| `report.py` | `results/report.md` and the frozen pick, `results/strategy.json` |
| `auctions.py` | re-prices the pick at Alpaca's official auction prints |
| `trader.py`, `broker.py` | the executor and a dependency-free Alpaca REST adapter |

## The protocol

Fixed in `discover.py` before the first run; no constant was changed after it.

1. Every candidate is back-tested over each fund's whole history. Statistics start on 1 June 2001, after decimal ticks, so today's cost schedule applies to every evaluated day.
2. **Edge** means alpha, not return: the information ratio of the intercept from regressing the candidate's net excess return on equal-weight buy and hold of its own funds, with Newey-West errors. Holding the market is not an edge.
3. Discovery runs to 31 December 2015. The whole catalogue is tested jointly there (SPA, reality check, deflated Sharpe, PBO, Holm and BY).
4. The ten best discovery candidates (at least five years of history each) are confirmed **once** on the untouched holdout, 2016 to the last bar, by five tests:
   - holdout alpha significant after Holm across the ten (p below 0.05);
   - holdout Sharpe above zero;
   - holdout gross edge at least twice the modelled cost;
   - discovery deflated Sharpe of at least 0.95 for the full number of trials;
   - holdout alpha still positive with its best year removed.
5. **Confirmed** (all five) may trade live. Otherwise the candidate passing the most tests (ties by discovery rank) is frozen as a **paper-only** trial; that tie-break rule was added after the first scan was seen. With neither, `strategy.json` has no candidate and the trader idles.
6. A walk-forward re-runs the selection every January on past data only and holds the pick for the year. Its concatenated returns estimate what the automatic search itself earns.

### Causality

A market-on-close order must reach Alpaca before 15:50, before the close it trades at exists. Every decision on day `t` therefore uses closes through `t-1` only (plus the published exchange calendar). `tests/test_edgefinder.py` perturbs and deletes every future bar for every family and checks that no past decision moves. Injecting a one-day look-ahead into any of five families makes that test fail.

## Run the scan

```sh
python -m edgefinder.discover --out edgefinder/results            # cached data
python -m edgefinder.discover --out edgefinder/results --refresh  # refetch
```

About 20 seconds. Writes `scan.json` (every candidate, every window), `report.md` and `strategy.json`.

## Settle the auction question first

The bars report the first valid trade as the open. A market-on-open order fills at the primary exchange's opening auction, which can differ, and Alpaca's paper trading simulates opening orders at the opening quote. So before trading the overnight pick, re-price it at the official auction prints:

```sh
export ALPACA_API_KEY=... ALPACA_SECRET_KEY=...    # any Alpaca account; free plan is enough
python -m edgefinder.auctions --spec edgefinder/results/strategy.json --out edgefinder/results/auction-check.json
```

It keeps the decisions on the bars and prices the legs at the largest auction print of each session since 2016. Verdict `survives` needs the auction-priced alpha positive with t of at least 1.96.

## Trade it

The trader must have **its own Alpaca paper account**. The intraday trader force-flats at 15:50 and its watchdog flattens any position it does not know, so the two must never share an account; the trader refuses to run beside positions in funds its strategy does not trade.

```sh
# preview without keys: a flat 100,000 account on the next session
python -m edgefinder.trader plan --offline-equity 100000 --phase close

# with the dedicated account's keys in a dotenv file
export EDGEFINDER_SECRETS_FILE=/path/to/edgefinder.env   # ALPACA_API_KEY=..., ALPACA_SECRET_KEY=...
python -m edgefinder.trader plan --phase close --ignore-window   # what it would do now
python -m edgefinder.trader run --phase close --submit           # one phase, inside its window
python -m edgefinder.trader loop --submit                        # the daily scheduler
python -m edgefinder.trader status                               # positions, fills, shortfall
python -m edgefinder.trader halt                                 # stop all orders
python -m edgefinder.trader resume
```

Each trading day:

| phase | when (New York) | orders |
| --- | --- | --- |
| `open` | 08:00 to 09:27 | market-on-open (`opg`) to reach the session weights; Alpaca rejects `opg` from 09:28 |
| `close` | 25 to 12 minutes before the close (15:35 to 15:48, earlier on half days) | market-on-close (`cls`) to reach the overnight weights; Alpaca rejects `cls` from 15:50 |
| `catchup` | after 09:35 if the open phase was missed | ordinary market orders to the session weights |

Checks, all fail closed:

- paper unless `--live`; live also needs a `confirmed` spec and `ALPACA_LIVE_ENABLE=true`;
- a dedicated account (no positions in other funds), an active and unblocked account;
- data that ends at the previous session, and a full look-back;
- a `HALT` file in the state directory, or equity more than 20% below its recorded peak (which writes the `HALT` file);
- whole shares only (auction orders cannot be fractional), and no single order above 105% of the allocated capital;
- deterministic client order ids per strategy, session, phase, fund and side, looked up before every submit, so a retry or a restart cannot double an order.

Every decision, order, refusal and error is appended to `runtime/edgefinder/journal.jsonl`.

### What the paper account can and cannot tell you

Paper proves the mechanics: the windows, sizing, idempotency, the journal and the halt paths. It cannot prove the edge. At the holdout's nightly noise (about 53 bps) a t of 2 on the measured 3.7 bps a night needs roughly 800 nights, and paper fills of auction orders are simulated. `status` reports the fill shortfall against the official auction prints when Alpaca serves them, and an execution verdict (`pending` until 120 legs; `pass` when the mean round-trip shortfall is at most 1 bps with its upper 95% bound at most 2 bps; `stop` when the lower bound exceeds 2 bps). That verdict is meaningful on live fills only.

### Going live

The trader refuses live mode unless `strategy.json` says `"live_allowed": true`, which the scan writes only for a `confirmed` verdict. Changing that by hand is a deliberate, reviewable act; do it only after the auction check survives, and start with a small allocation (`--capital-fraction`).

## Deploy

Compose (separate secret file and volume; nothing in `compose.yaml` changes):

```sh
export EDGEFINDER_SECRET_FILE=/etc/alpaca-agent-trading/edgefinder.env
docker compose -f compose.yaml -f deploy/compose.edgefinder.yaml up -d edgefinder-trader
```

systemd: [`deploy/edgefinder-trader.service`](../deploy/edgefinder-trader.service).

## Tests

```sh
python -m unittest tests.test_edgefinder
```

54 tests: data legs and dividends, cost parity with `research/costs.py`, look-ahead perturbation for every family, the engine's arithmetic, the statistics against known cases, an end-to-end scan that finds a planted edge, the selection rule, the auction re-pricing, every trader check, phase and failure path against an in-memory broker, and two checks that edgefinder stays out of every frozen identity of the intraday system.
