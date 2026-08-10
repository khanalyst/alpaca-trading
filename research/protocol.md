# Research protocol

The research boundary is normalized, point-in-time market data for US-listed
equities/ETFs and listed OCC options only. Provider
payloads are converted to `research.market_data` records before feature
calculation or replay. An event is eligible only when its `as_of` timestamp is
no later than its observation timestamp and no later than the decision cutoff.
Records retain provider/feed identity and the New York session date used for
grouping.

## Replay gates

Every replay must establish the following invariants:

- range features use completed bars only;
- entries occur on the next bar, never on the signal bar;
- an early, reversed, or duplicate bar fails closed;
- same-bar stop/target ties resolve to the stop;
- a gap through a level, on entry or exit, fills at the gap open;
- a fill landing on a bar boundary uses a recorded quote at that instant when
  one exists, and otherwise records that it fell back to the bar;
- spread, slippage, and both-side fees are charged from one shared model;
- positions are force-flat before the session close;
- equity and single-leg long-option books have separate samples, costs, and
  P&L; multi-leg and short option structures are outside the protocol.

`research/costs.py` owns the single expected-cost model and the fill
arithmetic every lane spends it through; no lane carries its own
spread/slippage/fee numbers. Its parameters come from one `costs` config
block. The runtime's `execution.max_slippage_bps` and `max_spread_bps` are
rejection caps, not expectations: they bound the model, and a model expecting
a cost the runtime would refuse to submit fails closed. `research/calibration.py`
scores that model against the entry fills recorded in the runtime journal and
names an optimistic model rather than absorbing it.

The IBR implementation in `research/ibr.py` provides these invariants. A
missing or partial opening range is `no trade`, not an imputed range. A missing
immediate next bar is also `no trade`; stale signals are never carried across
an outage.

## Evidence

An evidence package should include the normalized input digest, event count,
provider/feed/schema identities, timezone/session policy, as-of cutoff,
configuration, code fingerprint, and deterministic fixture result. Results
without this provenance are descriptive only and cannot pass a qualification
gate. Walk-forward and held-out checks must be chronological. Paired baseline,
placebo, and acceptance-floor checks are evaluated independently for each
vehicle and may not pool option and underlying returns.

## Autonomous edge lane

The bounded registry in `research/variants.yaml` remains the complete proposal
surface for the explicit IBR baseline. Autonomous multi-strategy research uses
the finite, validated data-only grammar in `agent/contracts/rule.py`; generated
variant ids are content hashes of those specifications. Arbitrary source code
or unbounded fields are rejected. `agent.edge` resolves only a SQLite candidate
whose status is `validated` or `champion` for the configured strategy/vehicle.
Paper `selection_mode: all_proved` may run one strongest passing variant per
independent family under one global risk book; live mode must pin one named
variant with `selection_mode: specific`.

Backtests and forward-shadow runs are persisted with immutable hashes and
trade/evidence rows. The autonomous lane first evaluates the initial corpus as
a backtest, then accepts only a later, unseen session tail for shadow
evidence. Passing gates advance `candidate` -> `backtest_passed` -> `shadow` ->
`validated`, after which champion selection is automatic; runtime entries stay
blocked until a validated/champion record exists for the selected vehicle.
A candidate cannot skip the lifecycle or silently move backwards. Paper
outcomes are append-only and may demote a champion. Normal operation needs no
manual promotion. Explicit `edge promote` is supported only as an audited
control subject to lifecycle/evidence rules. Backward rollback is rejected;
explicit demotion is the operator safety action.

Factory mutations may inspect only the chronological fit partition. Held-out
and later-forward sessions must not influence hypothesis or parameter
generation. Each variant is evaluated in an isolated simulated account.
Multiple-test correction covers every variant evaluated in the cycle. A
replacement hypothesis may be generated only after the root family has an
adequate trade/session sample and no variant passes; underpowered data must not
cause autonomous hypothesis churn.

Each transition requires a chronological fit/held-out boundary, fit and
held-out structural floors for trades/sessions/clusters, matched baseline
deltas, cluster-level sign randomisation, and both family-local and
cycle-global false-discovery correction. Selection compares candidates across
families, so the q-value that authorizes a champion is the global one.

Beating a control is necessary but never sufficient. A variant must also show
absolute after-cost profitability on unseen data (positive net P&L and
positive per-trade expectancy), a positive lower confidence bound on the mean
held-out delta, a positive delta against a randomized-entry null control that
shares the candidate's session/symbol/direction distribution and exit rules,
and a majority of positive rolling-origin walk-forward folds.

The falsification check is a seeded permutation test: at least ten thousand
cluster-level sign-flip draws form an explicit null distribution, and the
decision is the empirical one-sided p-value against it. The draw count and
seed are derived from the matched evidence and persisted, so the distribution
is reproducible.

The last sessions of every evaluation corpus are sealed into a final
qualification window before any worker is scheduled. Selection, mutation and
diagnosis never receive them; the window is opened exactly once, by the
orchestrator, for the last go/no-go, and refuses to be copied or serialized.
Sealed sessions are scored, never split, so they enter no run, trade row or
family correction, and the forward-only boundary clears them afterwards.

Both research lanes are held to this standard. The explicit IBR lane and the
autonomous factory lane share one randomized-entry null control and one sealed
final window rather than each carrying its own; a corpus too thin to seal a
window or to support rolling-origin folds is underpowered, not failed.

The complete gate is durably persisted and re-verified before validation or
champion selection. Re-verification recomputes the analysis — matched deltas,
p-value, lower bound, falsification, absolute profitability — from the stored
source rows and compares it against the recorded decision, rather than only
re-checking hashes. Champions are ranked by the lower confidence bound, not
the raw held-out delta.
Underpowered data is not failure. Retirement is permitted only after every
intended variant is adequately tested and fails; an enabled LLM lane must first
register a valid bounded replacement. Drawdown is persisted and used to rank
otherwise qualified champions conservatively.

The checked research config enables the bounded strategy LLM with model
`gpt-5`. It reads only the optional `ALPACA_RESEARCH_LLM_SECRETS_FILE`; missing
or invalid credentials/output leave a pending replacement and cannot trigger
premature retirement. Good edges produce deterministic content-addressed
edge proof reports under `research/results/edges/`, with an optional HTTPS
webhook notification. Scheduled cycles report
`completed`, `completed_no_edge`, `no_data`, or `failed`; no status bypasses the
runtime edge gate.
