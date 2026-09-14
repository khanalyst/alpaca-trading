# Pending work — September 14, 2026

This is the active pending-only list. The [strategy review](strategy-variant-review.md)
covers all 43 registered equity arms; [release verification](release-verification-2026-09-14.json)
records software checks separately from market evidence.

## Release and paper activation

The local release is prepared but has not been pushed, deployed, or activated.
GitHub access is blocked by an approval-service HTTP 404 before
execution, not a verified GitHub CI failure. Restore approved Git access, verify
CI for the resulting main commit, and complete the existing backup/image rollout.
The September 14 read-only host check still found release `c9368e2` and an
operator-paused trader.

The one selected paper experiment is the opening-range-breakout baseline
`rule.opening-range-breakout.0eb200d3136d80ee`, using the explicit
[paper profile](../deploy/paper-orb.config.json) and
[activation procedure](paper-shadow-trials.md). It is an interpretable baseline,
not a proven profitable strategy or a historical winner selection.
The default configuration remains disabled; risk and market-data limits are
unchanged.

The paper account was active with zero broker positions/orders at 07:54 UTC;
the local book was flat and paused at 08:05 UTC. These read-only observations
are not a fresh locked reconciliation or activation. After rollout, verify
the paper endpoint, account scope, reconciled local/broker flatness and no pending
orders immediately before activating the named trial. No live-money release is
authorized.

## Forward-session and execution evidence

- The September 14 host check reported **0 accepted / 4 rejected** complete
  forward partitions. Preserve those rejected reports. After rollout, freeze the
  code/configuration/feed/cohort, record warmup separately, and capture complete
  same-epoch sessions with every required symbol and arm.
- Verify IEX coverage, quote freshness, completed-bar availability, capture
  cadence and all-arm cursor progress during market hours. Healthy containers
  and closed-market heartbeats do not establish these conditions.
- The named paper trial requires 20 accepted sessions and 20 closed outcomes;
  60 accepted sessions is its review limit, not permission to promote.
  Research readiness still requires 30 accepted sessions, with separate
  qualification/proof thresholds. Paper results do not confer live eligibility.
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
