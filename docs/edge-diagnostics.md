# Edge diagnostics without trading authorization

Research measurement must remain usable when forward evidence is insufficient.
Neither the inventory survey nor cost calibration opens a broker account,
creates a proof, promotes a candidate, or changes runtime risk configuration.

## Complete existing inventory

Run from the repository root against a frozen, normalized JSONL corpus:

```sh
.venv/bin/python -m research.diagnostic_suite \
  --data /absolute/path/to/frozen/market.jsonl \
  --agent-config config.yaml \
  --out /absolute/path/to/new/inventory.json \
  --diagnostic-only --workers 2
```

Exit **2** means diagnostic completion, not trading eligibility. The survey
resolves the existing 24 diagnostic-shadow arms, 12 mechanism arms, and seven
equity IBR registry arms. It does not invent or tune strategies. Each output
and its `.manifest.json` must be new; the manifest freezes identities before
replay, and publication rechecks source and code hashes.

The default measures complete strategy replays and admission outcomes. Add
`--full-fit-diagnostics` only for a deliberately budgeted all-prefix predicate
audit; that additional scan can be much more expensive than replay. A skipped
prefix audit is explicitly marked unavailable, not a negative result.

Read `admission_preflight` before interpreting returns. The fixed stress-policy
minimum stop is scenario basis points divided by the allowed cost/risk ratio.
That is an admission constraint, not expected transaction costs. The survey
never widens an authored stop to satisfy it.

`execution_blocked`, `missing_data`, `no_signal`, and `underpowered` are not
negative expectancy. No-trade expectancy remains null. Accounts are isolated;
adding their dollar P&L does not produce a portfolio return.

### IBR runtime evidence and legacy comparisons

The seven registered IBR arms reuse the event-driven shadow worker, shared live
signal/setup/risk code and isolated, risk-sized diagnostic accounts. The adapter
does not open a shadow database, EdgeLedger or broker connection. It preserves
one-signal-per-symbol/session state before setup/risk rejection, the signal-close
authored bracket, executable quote admission, and force-flat deadlines.

The runtime lane requires explicitly forward-observed bars and contemporaneous
quotes with matching feed/provider and consistent session calendars. Historical,
mixed, missing-quote or invalid-calendar sources return `unavailable` with null
diagnostics. Incomplete or unpriced opportunities remain missing-data rows;
only closed modeled positions contribute realized P&L. Positive synthetic test
fixtures verify arithmetic, not an economic edge.

Each IBR result retains `legacy_comparison` from the existing fixed-share replay,
including its partial-runtime-parity warning. Top-level `outcome_counts` describe
the shared-runtime lane; `legacy_comparison_outcome_counts` are separate. An old
positive legacy comparison is neither erased nor promoted into runtime evidence.

`--ibr-max-events` defaults to 100,000 source events and is frozen in the manifest.
Exceeding the budget makes the runtime lane unavailable; input is never silently
truncated. Raise it only for a deliberately sized, immutable source. The inventory
still contains exactly 43 unique arms, not 43 plus seven duplicate IBR entries.

## Before the forward-session gate passes

The normal research scheduler still waits for accepted full sessions. An
operator can run a separate bounded diagnostic using these environment settings:

```text
ALPACA_RESEARCH_PREACCEPTANCE_DIAGNOSTIC_ONCE=1
ALPACA_RESEARCH_SNAPSHOT_ROOT=/absolute/path/to/sealed-snapshot
ALPACA_RESEARCH_ACCEPTANCE_ROOT=/absolute/path/to/acceptance-reports
ALPACA_RESEARCH_SESSION_WINDOW=3
ALPACA_RESEARCH_MAX_SOURCE_BYTES=100000000
ALPACA_RESEARCH_DIAGNOSTIC_OUTPUT=/absolute/path/outside-snapshot/new-inventory.json
```

Invoke `deploy/research-cycle.sh` with those settings. Size/window values are
explicit operating budgets, not evidence thresholds. The snapshot must verify,
the output must be new and outside it, and the census must be underpowered.
The mode skips provider/LLM preflight and every authorizing research stage. It
runs the survey once, accepts its diagnostic exit, and returns the original
`waiting_for_forward_sessions` status. It does not manufacture accepted sessions.

## Quote-cost calibration and publication

```sh
.venv/bin/python -m research.cost_rerun \
  --calibration-only --corpus /absolute/path/to/frozen/quotes.jsonl \
  --config config.yaml --min-quotes-per-cell 500 \
  --publish-latest /absolute/path/to/calibration-latest.json
```

Calibration accepts canonical quote-only input; replay still requires bars.
Source/feed/future-observation validation remains mandatory. Missing quotes or
insufficient fit/held-out coverage means unavailable calibration, not zero cost.
Quote-derived estimates are not measurements of actual broker fills or impact.

Publication preserves content-addressed artifacts in
`calibration-latest.artifacts/<content_hash>.json` and atomically refreshes a
nonactivating latest view. It retains the previous artifact, rejects tampering
and symlinked targets, and leaves the original `--out` writer immutable. Runtime
activation requires a separate explicit review of an immutable artifact path.

## Live verification after deployment

The Compose shadow service explicitly sets `ALPACA_SHADOW_INCLUDE_IBR=1`: 24
fixed rule arms plus seven registered IBR arms, each with its own modeled account.
The library and standalone deployment CLI retain a 24-arm default unless opted
in through this setting or `--diagnostic-include-ibr`. Setting the environment
value to `0` opts out; invalid values and authorizing-mode combinations fail
closed. The service has no broker-secret mounts and keeps live trading disabled.
These are simulated shadow accounts, not additional Alpaca broker accounts.

Changing the cohort or code creates a new evidence epoch. Do not merge its
outcomes or session acceptance with the older 24-arm epoch. Shortability, broker
buying power, pending orders and actual fills remain unobserved broker-only
conditions, even when the signal/risk contracts match.

Recorder health separates start-to-start cadence, cycle duration/overrun,
provider fetches, local processing, market timestamps, and response receipt.
Diagnostic shadow shares poll-local preparation and batches durable event
inserts; account changes and diagnostic progress remain atomic. Retry tests
cover the separate event and source-offset commits.

Require fresh quotes/bars, progress from every arm, no unresolved replay errors,
and a complete accepted session before claiming operational success. A local
benchmark is not a production-cadence result. Keep broker trading paused until
the existing validation, risk, and explicit activation requirements are met.
