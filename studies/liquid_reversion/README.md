# Liquid-ETF VWAP reversion study

This standalone simulator studies a fade of a volatility-scaled deviation from
session VWAP after a range-day classification. It writes diagnostic outputs;
it does not submit orders or activate the main paper trial.

## Implementation

| Module | Responsibility |
| --- | --- |
| [bars.py](bars.py) | Bar parsing, completed-bar timing, New York sessions and calendars |
| [daytype.py](daytype.py) | Day classification from the opening window and prior sessions |
| [strategy.py](strategy.py) | Parameter grid, signals, next-bar entries and simulated exits |
| [costs.py](costs.py) | Fixed cost-model constants and measured-spread input |
| [walkforward.py](walkforward.py) | Training selection and disjoint test-window evaluation |
| [power.py](power.py) | Session aggregation and sample-size diagnostics |
| [run.py](run.py) | CLI, design checks and bounded JSON/Markdown output |
| [fetch_sip.py](fetch_sip.py) | Historical SIP bars and calendar retrieval |

Signals use completed regular-session bars. The day classifier reads the opening
window and completed prior sessions; a signal enters at the following bar's open.
The first qualifying signal consumes the symbol-session even when that entry is
refused. The target is VWAP frozen at the signal. Simulated stops win same-bar
stop/target ties, and a gap beyond a level fills at the bar open. `Rules` in
`strategy.py` defines the entry window, holding horizon and flatten time.

## Input retrieval

Examples use the project's `.venv/bin/python` from the repository root. Inside
the application image, use its `python` executable instead.

The fetcher reads `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` from the environment.
It does not load a secret file automatically. Configure credentials using the
project's [setup guidance](../../SETUP.md), without printing them or adding them
to version control.

```bash
.venv/bin/python -m studies.liquid_reversion.fetch_sip --help
.venv/bin/python -m studies.liquid_reversion.fetch_sip \
  --out DATA/sip --start START_DATE --end END_DATE \
  --symbols SPY,QQQ,IWM,DIA,VTI --timeframe 1Min --calendar
```

Replace `START_DATE` and `END_DATE` with ISO dates. Request complete, ended calendar
months for final month files. The fetcher writes `<SYMBOL>_1Min_YYYY-MM.csv.gz`;
incomplete months receive `.partial.csv.gz`. Existing final files are skipped
unless `--force` is supplied. Pagination and bounded retry handling are in the
fetcher. Requests use `feed=sip` and `adjustment=raw`; account entitlement or a
request failure is not silently converted to another feed.

`--calendar` writes the exchange calendar for the requested range. A completed
month file is a download boundary, not proof that every regular-session minute
was observed. Preserve raw files and inspect coverage before interpreting results.

## Running the study

```bash
.venv/bin/python -m studies.liquid_reversion.run \
  --data DATA/sip --format alpaca --timeframe 1Min \
  --calendar DATA/sip/calendar.json --out RUNS/liquid-reversion
```

Alpaca-format runs require `--calendar`; the calendar must cover the data, and
early-close sessions are excluded from trading. Public chart JSON is accepted
with `--format public`; it remains a distinct diagnostic source.

Use a new output directory for each experiment and retain the input fingerprint.
`results.json` records coverage, source, rule settings, grid, selection, costs,
design deviations and evaluation. `report.md` renders that record. Trade lists
may be omitted to keep outputs bounded.

With enough sessions, the default design uses 504 training sessions followed by
126 test sessions. Training selects the grid point with the highest clustered
training statistic among those meeting the training-trade floor; each choice
is applied unchanged to its following test window. With insufficient sessions,
the CLI uses the fixed smoke parameters and labels the run as lacking
out-of-sample evidence.

`--train`, `--test`, `--min-trades`, `--permutations`, `--seed`, `--symbols`, and
input properties affect the recorded design. `PREREGISTERED_DESIGN` in `run.py`
determines whether the study's local decision rule is applicable. Its
`decision.deploy_to_paper` field is a research verdict; it does not change the
main system's proof, trial or order authorization.

## Costs and interpretation

`costs.py` applies a tick/spread floor, slippage on both legs, and fixed SEC/TAF
fee constants on the sell leg. These are model assumptions, not a live fee
schedule. `--spreads` accepts measured per-symbol quoted spreads in basis points.
`--spreads` is a path to a JSON object, not inline JSON. Review the constants
and intended execution before interpreting net results.
Cost stress multiplies the modeled round-trip cost.

The evaluation includes session-based statistics, a sign-flip test, cost stress,
and power diagnostics. A historical split does not establish an untouched or
source-independent holdout. Main-system qualification still requires its own
supported runtime contract and real execution evidence.

## Validation

```bash
.venv/bin/python -m unittest tests.research.test_liquid_reversion_study
```

Use the [research guide](../../research/README.md) for the main research pipeline.
