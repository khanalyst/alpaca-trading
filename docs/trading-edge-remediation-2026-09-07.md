# Trading and research remediation — September 7, 2026

Implemented in the local `main` working tree against baseline `958da0c`.
The preceding audit is in [the audit report](trading-edge-audit-2026-09-07.md),
with the [historical variant appendix](trading-edge-historical-variants-2026-09-07.md). This record
describes code changes, not a deployment or a claim of positive expectancy.

## Economic conclusion

The historical sample contained 44 variants: 20 traded and all 20 lost money;
24 produced no trades. Across the isolated variant simulations, 650 trades
produced gross P&L of −$28,148.83 and net P&L of −$29,748.81. These are
overlapping strategy experiments, not one portfolio's return. Gross already
uses modeled fill prices. The extra $1,599.98 cost deduction is not the entire
execution penalty. The sample had 90 stops, 46 targets, and 514 time exits.
Relaxing stressed execution constraints increased activity without turning the
sample profitable. More trades and more parameters are therefore not evidence
that the economic problem has been solved.

The fixes make losses, missing evidence, trade accounting and hypotheses more
interpretable. A positive edge still needs future, unseen market observations.
No proof, FDR, held-out, stress, account-risk or promotion requirement was
weakened to create apparent winners.

## Execution and account risk

- Multiple partial/canceled/retried closes now aggregate against the original
  entry. Incremental quantities are journaled once, with weighted exit prices;
  the parent completes only when its filled quantity is accounted for. The
  reproduced ten-share trade now attributes ten shares at the weighted $102
  exit, instead of only six shares at $101. Old close orders cannot attach to
  a subsequent same-symbol entry.
- Known whole-trade costs are apportioned across partial closes. Broker-fill
  P&L subtracts fees once; slippage remains separately labeled telemetry.
  Missing cost evidence does not become a fabricated zero-fee net profit.
- Daily account-loss checks run before proof, entry-cutoff, and position-monitor
  early returns, including a failed individual close. Portfolio flattening can
  still act when another position remains exposed.
- Paper selection checks current opportunities before allocating its limited
  correlation/risk slots. It uses the already collected completed-bar snapshot,
  current signal-session budget and held/pending symbols. Definite no-signal
  candidates cannot crowd out a candidate with a signal. Existing entry and
  sizing checks remain authoritative.
- A new supervisor owns one trader child. It measures advancing child
  heartbeats, terminates only its own hung child, confirms exit, and then hands
  recovery to the exclusive-lock, authenticated watchdog. Recovery persists
  operator pause before broker I/O. Compose, the image command and systemd use
  the supervisor. Broker/network outages still prevent guaranteed flattening.

## Strategies and variants

`rule-strategy.v5` adds opt-in price-path context and structural confirmation.
Existing v1–v4 specifications and IDs keep their old behavior. A neutral v5
specification has v4 semantics, but a new storage identity. No existing proved
strategy is silently upgraded.

Context uses 3–24 complete 5- or 15-minute buckets, aligned to the New York
regular-session open. It rejects missing/duplicate minutes and unfinished
buckets. Efficiency is absolute net price movement divided by the sum of
absolute movements between the first bucket open and subsequent bucket closes.
A trend filter requires direction agreement and efficiency above its threshold;
a range filter requires efficiency below its threshold. This is an explicit
price-path hypothesis, not a calibrated prediction probability or a news regime.
The bounded feature window conservatively reserves up to one extra bucket's
alignment padding. Runtime and research share the evaluator and source digest.

A trend reclaim requires a prior impulse, an actual retracement and a completed
close through the previous bar's high/low. A value reclaim requires extension
from the pre-pullback VWAP or mean, confirmation, and remaining distance to that
anchor. The signal candle cannot create its own prior trend or value anchor.
Targets, trailing stops and deadlines retain the existing bounded v4 semantics.
V5 is equity-only; options retain their existing executable v1/v2 restriction.

The fixed `intraday-mechanisms.v1` cohort compares 12 arms:

| Family | Four frozen arms | Question |
| --- | --- | --- |
| Opening-range breakout | Parent; 5-minute trend; 15-minute trend; 5-minute trend with shorter hold | Does continuation context improve breakouts, and does waiting dilute them? |
| Trend pullback | Legacy parent; reclaim; reclaim plus trend; same with trailing stop | Does structural confirmation help entry quality or only delay entry? |
| VWAP reversion | Parent; reclaim; reclaim plus range; same with shorter hold | Does confirmation in a ranging path improve reversion before value is reached? |

Run `python research.py factory run --data market.jsonl --diagnostic-only
--cohort intraday-mechanisms.v1`. The cohort has no LLM proposals or adaptive
mutations and keeps all losing and zero-trade results. It cannot authorize
entries or write proofs. Freeze a separate confirmation protocol before using
unseen data to evaluate any chosen treatment; repeatedly selecting the best
diagnostic arm would recreate selection bias.

## Research fidelity and parallel evidence

Valid orphan calendar sidecars no longer crash recorder rebuilds, while
malformed metadata still fails. Automatic cache reuse from historical partition markers is disabled: a marker
does not stop subsequent recorder writes. The pre-existing caller-supplied
`ALPACA_RESEARCH_IMMUTABLE_SOURCE_IDENTITY` contract remains available for
externally frozen input. The new historical fingerprint is diagnostic only;
a sealed snapshot protocol remains unfinished.

Each research-cycle process now publishes owner PID/start, job/build identity,
dataset identity, independent lease, actual phase/progress and terminal outcome.
The dashboard shows this separately from scheduler status. An alive process
lease cannot masquerade as advancing research. Scheduler ownership remains
unknown unless explicitly supplied.

Research, recorded observations, shadow decisions, paper outcomes and account
reports remain distinct evidence streams. They can run alongside one another
using the existing recorder/shadow/runtime architecture; they are not all made
equivalent by a common container or database. The new causal context snapshot
can be compared across signal, replay, plan and trade. Historical reconstruction
does not establish what was available at a live decision time.

The audited host had no qualifying forward sessions and no paper outcomes for
the seven registered candidates. Its 137 historical backfill partitions were
diagnostic. Code fixes do not create missing forward sessions or retroactively
grade its 120 lessons. Fresh recorder/shadow observations and later paper fills
must supply that evidence after an operator deploys and resumes the system.

Alpaca documents that paper trading omits several execution effects, including
market impact and latency slippage. It also documents minute-bar updates when
late trades arrive. These are reasons to keep measured fills and causal source
provenance separate from reconstructed history: [paper trading](https://docs.alpaca.markets/us/docs/paper-trading),
[real-time stock data](https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data).

## P&L, confidence and charts

Reports aggregate completed parent trades across the full journal. Net winners
use net P&L even when an R denominator is missing; gross winners remain a
separate statistic. Unknown legacy economics remain unknown. Recent fill pages
do not truncate lifetime totals. A positive trial point estimate now also needs
a 95% session-cluster lower bound above its mean-R floor to become promotable;
positive but uncertain trials remain inconclusive. Mean R × 100 is labeled
explicitly and is never presented as capital return.

The read-only dashboard shows gross/fees/net, account equity and drawdown, net
payoffs, clustered uncertainty, recorded portfolio notional, and frozen entry
context. Paper/live records are separated. Account equity is not adjusted for
cash transfers; ETF buckets are not measured betas or independent bet counts.
Options without delta evidence cannot be included in comparable equity exposure.

## Remaining evidence and scope

There is no verified profitable replacement strategy yet. Prior-session levels,
cross-market breadth, beta estimates, event/news timestamps and calibrated
volume seasonality need explicit data contracts and tests before becoming
authorizing filters. They are not represented by the new intraday context.
The existing shared-book correlation cap remains the risk control; coarse
dashboard exposure labels do not introduce an unvalidated beta-based allocator.

Verification results and logs are recorded in
[the handover](trading-edge-handover-2026-09-08.md). The user subsequently
authorized committing and pushing the completed implementation to main. No
broker orders, deployment or operator resume were performed.
