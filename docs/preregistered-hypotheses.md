# Preregistered hypotheses

The source of truth is `research/preregistered.py`. This page records the
frozen contracts and the command surface; it does not publish empirical
results. A registered pass earns entry to the existing proof pipeline. It never
authorizes trading, sizing, a configuration change, or an entry.

## Registry status

The module exposes exactly four ids:

| hypothesis id | schema | contract status |
| --- | --- | --- |
| `vwap-reversion-control-adjusted.v1` | `preregistered-hypothesis.v1` | frozen for reproducibility; code marks it compromised and warns on evaluation |
| `opening-range-fade-control-adjusted.v1` | `preregistered-hypothesis.v1` | frozen for reproducibility; code marks it compromised and warns on evaluation |
| `vwap-reversion-control-adjusted.v2` | `preregistered-hypothesis.v2` | frozen instrument/manifest contract |
| `opening-range-fade-control-adjusted.v2` | `preregistered-hypothesis.v2` | frozen instrument/manifest contract |

The v1 manifests and evaluation behavior are retained byte-for-byte so stored
reports remain reproducible. They are not current confirmatory authority. V2
does not silently edit v1: it registers a new manifest, new instrument
identity, new sealed boundary, and append-only look records.

## Shared v1 contract

Both v1 hypotheses use the configured 24-ETF universe, Alpaca provider, exact
IEX decision feed, and `forward_observed` source mode. The primary endpoint is
the session-clustered, control-adjusted directional return at 60 minutes;
120 minutes is descriptive. The control is the same instrument and session
minute on other sessions in the look window. Inference uses a CR1
session-clustered t statistic and a one-sided whole-session sign-flip test.

Each v1 look uses exactly the first N sealed sessions in date order:

| look | sessions | one-sided alpha |
| --- | ---: | ---: |
| 1 | 30 | 0.025 |
| 2 | 60 | 0.025 |

The economic hurdle is 3.0 bps. A pass requires both one-sided tests below the
look alpha and a mean control-adjusted delta at least the hurdle. A futility
decision requires a one-sided test that the mean is below the hurdle. Fewer
than 30 matched controls or 80% control coverage is inconclusive. Before a
look, only session availability is reported; a later look is not evaluated
after a pass or futility decision.

The frozen v1 subjects are:

* `vwap-reversion-control-adjusted.v1` →
  `rule.vwap-reversion.ab97bfe87ed566de`; and
* `opening-range-fade-control-adjusted.v1` →
  `rule.opening-range-fade.d5785d9e70b56def`.

The v1 sealed boundaries are manifest data (`sessions_after` is
`2026-09-21` for VWAP reversion and `2026-09-23` for opening-range fade). The
v1 manifest hashes are, respectively,
`d2e56d80bdb0e367bcceebbe4792bf4a00aee815eb442e62f043ee2e40b7c48e` and
`a0ae21f5ffd765142f2f4b92c47e33f652e0fb3213f1fd8d5a6d82c3915c2b9e`.

## Shared v2 contract

V2 uses the same two frozen subjects and mirrors, but changes the outcome to
the open of the bar after the completed signal bar (`next_bar_open`). The
primary outcome is a wall-clock 60-minute return; `skip_one_bar_open` is a
registered sensitivity anchor and `signal_close` is diagnostic only. A missing
entry or horizon bar has no outcome; a candidate without a control in the
registered tier counts as unmatched.

The v2 decision claim is a one-sided lower-confidence-bound claim that the
candidate-minus-control delta exceeds the frozen 17.0 bps static round-trip
cost. The 17.0 bps value is the registration-time `static_v1` arithmetic (4
bps spread, 6 bps adverse slippage, and 0.5 bps fee per side). A pass requires
the control-adjusted lower bound above that cost, the sign-flip test of
cost-adjusted deltas below alpha, and a positive candidate own-return CR1
statistic after the same cost. A futility decision is an upper confidence
bound below the cost on a control-adequate look. Underpowered control coverage
is inconclusive regardless of the numerical bound.

Both looks use the first complete eligible sessions in date order:

| look | sessions | one-sided alpha |
| --- | ---: | ---: |
| 1 | 30 | 0.025 |
| 2 | 60 | 0.025 |

The two v2 hypotheses each own their alpha; neither is re-tested on behalf of
the other. A look is decided once. Its look number, session-list digest,
instrument digest, manifest hash, and result are appended to the look-record
JSONL. Every later evaluation must reproduce the record. A duplicate,
unreproducible, malformed, or conflicting record makes the decision ineligible
and reports no sealed-window statistics.

The v2 instrument identity is `preregistered-instrument-identity.v2` with
registered digest
`1dced3ad98f1aa089696c929773d5388e5ecd2993d2821cbd190108eb8325e31`.
`evaluate()` recomputes the exact import-closure source identity and refuses a
decision when the running instrument or manifest differs. The v2 manifest
hashes are:

* VWAP reversion: `d0003f117e8e47a5b15653f645d0fa2ab72463e1d289ef118406c77774eb6b93`;
* opening-range fade: `22b7ca355aa42b72c766c5d56d829ba5f14618a010d29aceb4ce75caeab7c13f`.

The v2 registration block fixes `registered_on: 2026-10-28`,
`sealed_from: 2026-10-29`, and the last examined session before registration.
These are frozen manifest boundaries, not a current data snapshot.

## V2 session definition

A countable session is a complete regular Alpaca-calendar session in
`America/New_York`, closed by the evaluation instant, with exact open/close
metadata. Early-close sessions are excluded. The fixed universe is:

```text
SPY QQQ IWM DIA XLF XLK XLE XLV XLI XLP XLY XLU XLB XLRE
VTI VO VB EFA EEM TLT HYG GLD SLV SMH
```

A symbol is covered when it has bars for at least 50% of calendar minutes. A
session is eligible when at least 18 symbols are covered and at least 95% of
session minutes have a bar for some universe symbol. Other symbols are dropped
before measurement. A session in progress, a future evaluation instant, or an
excluded calendar/session-quality row cannot enter a look or its control pool.

The registered data contract is provider `alpaca`, feed `iex`, and source mode
`forward_observed`. Other feeds may be inspected only as descriptive,
non-decision diagnostics.

## Exact CLI

The module accepts one normalized JSONL corpus or session-partition directory
and refuses to replace an existing report:

```bash
.venv/bin/python -m research.preregistered \
  --data DATA.jsonl \
  --out NEW_REPORT.json \
  --hypothesis vwap-reversion-control-adjusted.v2 \
  --look-records LOOK_RECORDS.jsonl \
  --as-of AS_OF_UTC
```

`--hypothesis` accepts any of the four registered ids. V2 requires
`--look-records`; `--as-of`, when supplied, must be ISO 8601 with a UTC offset
(for example `YYYY-MM-DDTHH:MM:SS+00:00`). The output path is created only
after evaluation. A v1 run emits the source-defined compromise warning. Source
preflight still rejects malformed rows, future observations, and incompatible
provider/feed; a diagnostic fallback is labelled non-authorizing.

The look-record file is append-only and locked across read, evaluate, and
append. A final unterminated line from an interrupted append is safely dropped
under the lock and the deterministic look may be reproduced. No command or
report may turn a v1/v2 decision into a proof without the independent
source-gated evidence lifecycle.

## Relationship to paper and shadow lanes

The registered subjects may also appear in diagnostic shadow or a paper trial.
Those lanes measure runtime/replay availability and actual paper outcomes under
their own controls. They are descriptive companions, not inputs that rewrite a
manifest, and neither lane decides the preregistered hypothesis for the other.
See [`docs/paper-shadow-trials.md`](paper-shadow-trials.md) and
[`research/protocol.md`](../research/protocol.md) for the authority boundary.
