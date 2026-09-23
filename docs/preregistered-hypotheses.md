# Preregistered hypotheses

Everything else in this repository searches. Search finds hypotheses, and it
is also why a searched result cannot confirm itself: the sessions that
suggested an idea are the ones that would have to test it. A preregistered
hypothesis is fixed in code before its test data exist, and is decided by
rules written down in advance.

The source of truth is `research/preregistered.py`. This page explains it;
it does not override it.

## `vwap-reversion-control-adjusted.v1`

Registered 2026-09-22. Manifest hash
`d2e56d80bdb0e367bcceebbe4792bf4a00aee815eb442e62f043ee2e40b7c48e`, pinned by
`tests/research/test_preregistered.py`.

**Claim.** On the configured 24-ETF universe, bars on which the frozen
session-VWAP reversion rule fires are followed over the next 60 minutes by a
return in the rule's own direction that beats a clock-matched control by at
least 3.0 bps on average.

**Why this one.** It led five in-sample measurements in the September 2026
audit (`docs/audit-2026-09-21/SIGNAL-VALUE.md`), and its mirror, `vwap_trend`,
was the worst arm in the catalogue. It was chosen *because* of those results,
which is exactly why it may only be confirmed on sessions they did not use.

| item | fixed value |
| --- | --- |
| subject | `rule.vwap-reversion.ab97bfe87ed566de` (diagnostic-shadow slot 7 baseline) |
| primary endpoint | 60-minute control-adjusted forward return, `research.signal_quality` |
| inference unit | the session: CR1 clustered t (Student-t, G-1 df) and a cluster sign-flip |
| sealed window | sessions strictly after **2026-09-21** |
| decision data | Alpaca IEX, explicitly `forward_observed` (the deployment's own feed) |
| look 1 | exactly the first 30 sealed sessions, one-sided alpha 0.025 |
| look 2 | exactly the first 60 sealed sessions, one-sided alpha 0.025 |
| economic hurdle | 3.0 bps, the kill threshold written at commit `dc156ca` |

**Decision at each look.**

- **pass**: clustered t test *and* sign-flip test both below alpha, *and* the
  mean is at least 3.0 bps. Significance alone never passes: a real but
  sub-hurdle effect is not worth trading.
- **futility**: the mean is credibly below 3.0 bps. Retire the hypothesis.
- **inconclusive**: neither. Continue to look 2; at look 2 the claim is not
  established and the hypothesis is retired. There is no third look.
- Fewer than 30 matched controls, or coverage below 80%, is inconclusive.
- Before look 1: descriptive only. No decision and no early stop.

**What a pass buys.** Entry to the existing proof pipeline: held-out replay,
controls, false-discovery correction, live-shadow parity and the paper trial.
It never authorizes trading, sizing or a configuration change.

**No new runtime arm is needed.** The subject is already in the deployed
diagnostic shadow cohort, and a test fails if it ever leaves it, so the
recorder corpus on the VM is already accumulating the data this needs.

## `opening-range-fade-control-adjusted.v1`

Registered 2026-09-23, while that day's session was trading and before any of
its bars were fetched. Manifest hash
`a0ae21f5ffd765142f2f4b92c47e33f652e0fb3213f1fd8d5a6d82c3915c2b9e`, pinned by
`tests/research/test_preregistered.py`.

**Claim.** Bars on which the frozen opening-range fade rule fires are followed
over the next 60 minutes by a return in the rule's own direction that beats a
clock-matched control by at least 3.0 bps on average.

**Why this one.** It was the only catalogue arm still positive after
realistic costs in the audit's bracket replay (+10.2 bps per trade over 59
trades, session-clustered t +2.57), with a +19.5 bps control-adjusted delta at
60 minutes (clustered t +2.95). It also lost on 2026-09-22. All of that is
in-sample, which is why it needs its own sealed test.

| item | fixed value |
| --- | --- |
| subject | `rule.opening-range-fade.d5785d9e70b56def` (diagnostic-shadow baseline) |
| mirror | `rule.opening-range-breakout.0eb200d3136d80ee`, expected negative |
| sealed window | sessions strictly after **2026-09-23** |
| everything else | identical to the VWAP hypothesis above: endpoint, data, looks, alpha, hurdle, decision rule |

It is also the incumbent of the paper profile `deploy/paper-orf.config.json`
(trial `paper-orf-baseline-20260923-v1`). The paper trial's real fills are
trade-level evidence under the trial's own floors; they never enter this
decision, and this decision never grants the trial anything.

Each hypothesis spends its own alpha and is reported separately.

## Running it

On the VM, against the recorder's forward corpus (a JSONL file or a directory
of session partitions):

```sh
python -m research.preregistered \
  --data /app/runtime/research/recorded-forward-2026-09-16 \
  --out /app/runtime/research/preregistered/vwap-reversion-$(date -u +%F).json
python -m research.preregistered \
  --hypothesis opening-range-fade-control-adjusted.v1 \
  --data /app/runtime/research/recorded-forward-2026-09-16 \
  --out /app/runtime/research/preregistered/opening-range-fade-$(date -u +%F).json
```

A report is never replaced; each run writes a new file. The run is
decision-bearing only if `research.source_validation.validate_source` accepts
the corpus as authorizing *and* every bar carries the Alpaca IEX feed. Any
other corpus is evaluated and labelled `diagnostic_only`, with the reasons, and
`rule_outcome_if_eligible` shows what the rule would have said.

A bar that does not state its `source_mode` is normalized as
`forward_observed` by default. The evaluator never trusts that default: it
takes eligibility from the source preflight only.

## Changing a hypothesis

Don't. Any edit changes the manifest hash and fails the suite. Register a new
version (`...v2`) with its own sealed window starting after the last session
anyone has examined, and leave `v1` and its reports intact.
