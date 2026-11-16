# Session-edge research

The session study screens bounded minute, daily and volatility-context
hypotheses upstream of the main validation system. Historical results,
registered-rule reports and generated books are diagnostic. They do not enable
paper or live entries.

The [catalogue](EDGES.md) is generated from [grammar.py](grammar.py). Its runner
support flag describes signal implementation, while trading authority remains
subject to the main proof/runtime bridge.

## Inputs and causal contracts

- Minute input uses SIP month files from the
  [historical fetcher](../liquid_reversion/fetch_sip.py), named
  `<SYMBOL>_1Min_YYYY-MM.csv.gz`. Supply the fetched calendar to exclude
  early-close sessions correctly.
- Daily input uses adjusted chart files from [fetch_public.py](fetch_public.py).
  Context series such as VIX and VIX3M are supplied separately by name.
- The turn-of-month family needs an explicit ordered exchange calendar through
  `--exchange-calendar`. Cover all loaded dates and the calendar rule's required
  surrounding dates; do not infer future month ends from price observations.
- Minute statistical screens require observed bars for complete sessions.
  Missing signal/fill/exit bars cannot become executable prices. A completed
  download does not guarantee complete session coverage; the screen refuses
  incomplete sessions rather than assigning unavailable returns zero.
- Completed daily close/RSI/streak signals execute at the next observed open.
  The daily close rule and the minute 15:50 rule retain separate identities.
- Intraday noise-band simulations flatten at the runner's configured 15:58
  boundary. Missing sessions cannot silently erase an owned swing position.
- Costs are scenario assumptions, charged on executed sides. Calibrate them for
  the intended instrument, time, size and execution before interpretation.

Historical comparison windows and correction thresholds are defined in
[discover.py](discover.py) and [stats.py](stats.py). Their labels make no claim
that the historical windows are untouched holdouts. Discovery tests positive
net return and exposure-adjusted alpha, and retains unavailable evidence in the
cumulative correction rather than treating it as success.

## Historical workflow

Examples use `.venv/bin/python` from the repository root; inside the built
application image, use `python` instead. Use a frozen
input corpus, a new output directory and a shared ledger outside individual
run directories. An isolated application image can mount inputs read-only and
only the new research destination writable. The study does not need access to
active paper, proof or trial stores.

Inspect the retrieval CLIs before fetching data:

```bash
.venv/bin/python -m studies.liquid_reversion.fetch_sip --help
.venv/bin/python -m studies.session_edges.fetch_public --help
```

Run the registered-rule study with minute input:

```bash
.venv/bin/python -m studies.session_edges.run \
  --alpaca-dir DATA/sip --symbols QQQ,SPY \
  --calendar DATA/sip/calendar.json --out RUNS/registered
```

`run.py` requires minute input and can also accept daily files. For a discovery
experiment, initialize a shared ledger explicitly once:

```bash
.venv/bin/python -m studies.session_edges.discover \
  --alpaca-dir DATA/sip --symbols QQQ,SPY \
  --calendar DATA/sip/calendar.json \
  --ledger HISTORY/ledger.json --initialize-ledger \
  --experiment sip-causal --out RUNS/discovery
```

For a daily-only experiment, use `--daily-dir DATA/public/daily` and `--daily`
instead of the minute arguments. Add `--context VIX=VIX,VIX3M=VIX3M` when those
files are available, and `--exchange-calendar PATH` for the month-end family.
Missing required context is reported by family; malformed supplied inputs can
refuse the run.

The ledger binds each experiment name to data, context, source code, simulation
version, costs and protocol. Reusing the name requires identical bound inputs.
Changed inputs require a new name and `--new-experiment`, retaining the same
shared ledger. `--migrate-ledger PATH` with `--initialize-ledger` retains a
legacy ledger as diagnostic history without upgrading its claims. Do not reset
history to discard failed or previously admitted trials.

Outputs include `discovery.json`, `discovery.md`, `edge_book.json` and
`main_candidate_review.json`. Preserve raw inputs, failures, manifests and all
experiment outputs. No report label or book field grants execution authority.

## Main-candidate review

```bash
.venv/bin/python -m research.session_candidate_review \
  --discovery RUNS/discovery/discovery.json --executions executions.json \
  --out RUNS/main-candidate-review.json
```

Executions must bind the exact candidate request, specification, kernel, real
fills and matched controls. Session-return summaries cannot substitute for
trade records. The reviewer rejects mismatched rules and reused or overlapping
qualification sessions. Unsupported runtime/lifecycle, incomplete main gates
or missing strictly newer parity-shadow evidence remains a blocker. The main
system supplies trading authority through its existing pipeline.

## Proposal experiments

```bash
.venv/bin/python -m studies.session_edges.propose prompt \
  --discovery RUNS/discovery/discovery.json --symbols QQQ,SPY
.venv/bin/python -m studies.session_edges.propose ask \
  --model MODEL --discovery RUNS/discovery/discovery.json \
  --symbols QQQ,SPY --out RUNS/proposals.json
.venv/bin/python -m research.session_model_experiment --help
```

Configure the provider before using `ask`. Receipts retain provider/model,
prompt, request, response, configuration identity and failed calls. Discovery
retains attributed, rejected and duplicate proposals. Equal-budget experiments
freeze selections before paired outcomes; they remain diagnostic comparisons.

## Forward shadow and recovery

```bash
.venv/bin/python -m studies.session_edges.live run \
  --book RUNS/discovery/edge_book.json --dir RUNS/shadow
```

Default shadow records observations and decisions without sending orders. The
optional [Compose override](../../deploy/compose.session-edges.yaml) provides a
shadow service; the ordinary deployment helper does not enable it.

`--paper-orders` and `--live-orders` are permitted only with `--recover-owned`;
new entries remain disabled pending the main proof/runtime bridge. For previously saved owned exposure, explicit
`--recover-owned` mode uses the original directory, saved ownership and pending
client IDs to reconcile and reduce exposure. It cannot open new positions or
use a replacement book. Review the account-scoped preflight and recovery flags:

```bash
.venv/bin/python -m studies.session_edges.live preflight --help
```

The main account's operator pause, frozen trial and authorization are separate
from this study. Research or deployment must preserve them.

## Validation

```bash
.venv/bin/python deploy/test_suite.py --shard research
```

The complete test-shard commands are in the [project guide](../../README.md).
