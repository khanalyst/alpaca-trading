# Paper incumbent and persistent diagnostic shadows

This is an experiment workflow, not evidence that a profitable strategy exists.
It adds no services and no broker accounts. The existing trader manages one
Alpaca paper incumbent; the existing shadow service maintains independent
modeled books in its SQLite database.

## One frozen paper incumbent

`research.paper_trial` is disabled in the shipped configuration. Enabling it
requires an explicit `trial_id` and one exact `variant_id` from the fixed
diagnostic catalog. `auto`, arbitrary specs, runtime LLM decisions, options,
delayed feeds, and live mode cannot use this exception. The normal validated
edge gate must remain enabled.

The minimal configuration shape is:

```json
{
  "research": {
    "trial": {
      "enabled": true,
      "min_sessions": 20,
      "min_trades": 20,
      "min_mean_r": 0.0,
      "min_total_r": 0.0
    },
    "paper_trial": {
      "enabled": false,
      "trial_id": "",
      "variant_id": "",
      "accepted_session_report_root": "/app/shadow/session-acceptance",
      "max_review_sessions": 60
    }
  }
}
```

Keep `enabled: false` until an exact arm is selected and the account-scoped
preflight and flat-book checks pass. Do not reset the paper account or delete
runtime state to change variants. List the exact catalog identities locally:

```bash
python -c 'from research.diagnostic_shadow import _logical_arms; print("\n".join(a["variant_id"] for a in _logical_arms()))'
```

The incumbent identity freezes the rule, universe, feed/provider, risk,
execution/cost/session policy, stopping thresholds, and code identity. Restart
does not choose a new leaderboard winner. An active identity/configuration
change is refused. First activation requires an account-bound, reconciled flat
broker and local book. Replacement requires a terminal trial and another
confirmed flat book, including no pending orders. Before replacement, the
existing account-scoped journal receives a `paper_trial_terminal_snapshot`
containing the old identity, verdict, accepted sessions, and outcomes. A failed
audit write blocks replacement; the old experiment is not silently discarded.

The default minimum is **20 accepted market sessions and 20 closed parent
outcomes**. A session is counted only after a matching full-session operational
report exists. Activation-day warmup, partial days, rejected/missing reports,
and historical backfills do not count. A quiet but healthy day can advance
session tenure; it does not manufacture trades or confidence observations.

After the minimum sample:

- Positive point estimates and a session-cluster lower bound above the mean-R
  floor produce `passed`. The incumbent may continue; this does not grant proof,
  live permission, or automatic promotion.
- Nonpositive point estimates and an upper bound below the mean-R floor
  produce `failed`.
- Missing, malformed, underpowered, or overlapping uncertainty remains
  `inconclusive`, including a small negative point estimate.
- At 60 accepted sessions, the trial pauses as `review_required` if it has not
  already failed. A review pause is not a negative strategy verdict.

The interval requires at least 95% session-cluster confidence and the configured
usable observation/day-cluster floors. It is a pointwise bootstrap interval,
not an anytime-valid profitability guarantee. Market dependence and repeated
research remain reasons not to treat a paper pass as live authorization.

Daily loss, exposure, spread, stressed cost, fresh data, exact calendar,
position protection, shutdown, and operator kill controls remain independent.
No tenure rule forces an entry or prevents a safety exit. Experimental parent
outcomes are persisted atomically with closes in the existing paper state;
they never enter the authorizing EdgeLedger/FDR outbox.

## Parallel shadow books

Each arm of the fixed 24-arm diagnostic cohort has its own persistent cash,
positions, modeled orders, fills, and net P&L. State and the consumed-event cursor
commit together, so retries cannot credit a close twice. Cash carries across
sessions and restarts. The activation session remains warmup-only.

Entries and market exits use causal, exact-feed/provider forward quotes:
buys use the ask, sells use the bid, with modeled slippage and fees. Resting
stop/target events use the shared completed-bar exit rules; the entry signal
bar cannot retrospectively close its new position. An entirely post-deadline
bar cannot earn a target fill. Missing executable deadline quotes leave the
position open, unpriced, and flagged until the first later executable quote.

The exact broker calendar supplies the force-flat deadline, including early
closes. The strategy's `force_flat_minutes_before_close` overrides the session
default, matching the broker runtime. Both deadline representations survive
setup and risk sizing so the persistent book clamps its maximum hold to the
same session boundary. Missing required calendar metadata prevents entry.

`diagnostic_shadow.forward_accounts` exposes the summary and `by_candidate`
rows, including `last_event_at`. Unknown marks stay unknown. Each row is an
independent simulated account, not a portion of an actual Alpaca account;
pooling their balances or returns is misleading. All broker/qualification/FDR
authority remains false. Existing authorizing replay and parity gates are
unchanged.

## Full-session operational evidence

The existing shadow loop samples after publishing its atomic health heartbeat.
It writes bounded append-only samples and atomic
`session-YYYY-MM-DD.report.json` reports under
`/app/shadow/session-acceptance`. No separate monitoring daemon is added.

Acceptance requires full exact-calendar coverage, stable deployment/code/cohort/
activation identities, all 24 arms, genuine post-activation event progress,
and current symbol observations. An unchanged heartbeat or cursor is not a
completed day. Warmup must precede the counted session.

Quote age and shadow source lag remain capped at 30 seconds. Completed one-minute
bars are checked against their next publication deadline with at most 30 seconds
of publication tolerance, rather than an impossible continuous 30-second raw bar
age. Before the first session bar is due, a premarket bar is not required. The
immutable bar completion timestamp drives this check: re-fetching an old bar
cannot make it current. Raw bar age remains visible, and execution freshness
checks are unchanged.

Recorder and shadow examples both poll every 30 seconds. Poll-gap tolerance
follows the configured interval; it does not relax market-observation limits.
Missing samples, identity changes, stale symbols, or missing boundaries cannot
silently become a complete accepted day. Sparse quotes can still prevent a day
from qualifying; this change does not manufacture feed coverage.

`ALPACA_RESEARCH_ACCEPTANCE_ROOT` makes scheduled recorder research run a
metadata-only census before expensive preprocessing or provider research calls.
Insufficient accepted history yields `waiting_for_forward_sessions`, not a
failed strategy. Explicit external datasets retain precedence and their existing
validation path. Once history is sufficient, input-size/storage/snapshot gates
still apply. This does not solve the future capacity requirement of replaying
many multi-gigabyte sessions; the limits are not raised to hide that issue.

## Recorder cache and rollout

The recent-key cache uses an ordinary rowid table while retaining exact text
keys/timestamps, a 64 MiB cache, and DELETE/FULL durability. A verified v1 cache
is migrated by streaming only the recent window into an atomic replacement.
Corrupt caches and interrupted publication use the existing authoritative-corpus
recovery path. Market partitions and provenance are never rewritten by this
migration. Local synthetic timing is not a production-latency guarantee.

Deploy one tested image/config identity, verify account scope and readiness,
then collect complete future sessions. Do not relabel earlier incomplete days
as accepted evidence. The dashboard separates the frozen paper experiment,
persistent modeled shadow books/refusals, and qualified-edge proof reviews.
