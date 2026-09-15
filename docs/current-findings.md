# Pending work — September 15, 2026

This is the active pending-only list. The [strategy review](strategy-variant-review.md)
covers all 43 registered equity arms. The dated [release verification](release-verification-2026-09-14.json)
records pre-release observations; the [paper activation receipt](paper-activation-2026-09-15.json)
records the current deployment verification.

## Verified release context

Code release `c7f996a269b34b4eae8a2408ed298aa62fc3e3b6` was pushed to GitHub
main and all five release CI jobs succeeded. At the September 15 verification,
the pinned `alpaca-agent-trading:paper-orb-c7f996a` image had all six services
healthy. Authenticated flat-only resume succeeded,
and the exact opening-range-breakout baseline
`rule.opening-range-breakout.0eb200d3136d80ee` is activated, running, and
account-bound on the actual September 15 date. `operator_pause` is false,
live mode and runtime LLM are false, risk is unchanged, and the diagnostic
shadow has 31 modeled accounts (24 rule plus seven IBR) with matching code and
cohort identities and no candidate errors. Verification occurred while the
market was closed. The full backup is retained on a verified off-host copy.

September 14 was not collected during the backup approval delay; its session
report is rejected, not accepted evidence. September 15 is activation-day warmup.
At verification, accepted trial sessions and closed trial outcomes were both
zero.
The baseline is not a profitability verdict or live-money authorization.

## Forward-session and execution evidence

- Capture complete future market-hour sessions after warmup with every required
  symbol and arm, keeping deployment, code, feed, cohort, and activation
  identities frozen.
- Verify IEX coverage, quote freshness, completed-bar availability, capture
  cadence and all-arm cursor progress during market hours. Healthy containers
  and closed-market heartbeats do not establish these conditions.
- The named paper trial requires 20 accepted sessions and 20 closed outcomes;
  60 accepted sessions is its review limit, and research readiness still
  requires 30 accepted sessions with separate qualification/proof thresholds.
  Warmup, rejected/missing reports, and historical backfills do not count.
- Measure real quote/fill costs and rejection reasons before changing any
  parameter. The unchanged 25 bps stress / 0.30 cost-risk ceiling requires
  about 83.33 bps of stop distance before tick geometry; a 30 bps floor alone
  cannot pass. The paper strategy may correctly submit no orders. Do not widen
  stops, lower costs, or relax gates just to produce trades.
- Use bounded immutable snapshots and predeclared comparisons on untouched
  confirmation data. Historical backfills, modeled shadow fills and actual
  Alpaca paper fills remain separate evidence classes. The latest retained
  [historical results](edge-results-2026-09-12.json) have not been rerun and prove
  no positive edge.

## Separately scoped research work

Point-in-time catalyst/news/corporate-action inputs; factor, sector and duration
exposure; hedged residual execution; later-signal emission comparisons; and
executable markouts, MFE/MAE, matched controls and signal-to-fill attribution
remain research work, not established software defects. New semantics require
new identities and frozen comparisons. Adding more strategies or searching
more variants on the same examined data does not replace forward confirmation.
