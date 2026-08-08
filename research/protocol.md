# Research protocol

The research boundary is normalized, point-in-time market data. Provider
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
- a gap through a level fills at the gap open;
- spread, slippage, and both-side fees are charged;
- positions are force-flat before the session close;
- equity and single-leg long-option books have separate samples, costs, and
  P&L; multi-leg and short option structures are outside the protocol.

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

Backtests and forward-shadow runs are persisted with immutable hashes and
trade/evidence rows. The autonomous lane first evaluates the initial corpus as
a backtest, then accepts only a later, unseen session tail for shadow
evidence. Passing gates advance `candidate` -> `backtest_passed` -> `shadow` ->
`validated`, after which champion selection is automatic; runtime entries stay
blocked until a validated/champion record exists for the selected vehicle.
A candidate cannot skip the lifecycle or silently move backwards. Paper
outcomes are append-only and may demote a champion. Normal operation needs no
manual promotion; explicit `edge promote`/rollback commands are supported only
as audited controls subject to lifecycle/evidence rules. Demote, retire, and
rollback are operator safety actions.

Factory mutations may inspect only the chronological fit partition. Held-out
and later-forward sessions must not influence hypothesis or parameter
generation. Each variant is evaluated in an isolated simulated account.
Multiple-test correction covers every variant evaluated in the cycle. A
replacement hypothesis may be generated only after the root family has an
adequate trade/session sample and no variant passes; underpowered data must not
cause autonomous hypothesis churn.

Each transition requires a chronological fit/held-out boundary, minimum trades
and sessions in each window, matched baseline deltas, cluster-level sign
randomisation, family-level false-discovery correction, and a
placebo/falsification comparison. Drawdown is persisted and used to rank
otherwise qualified champions conservatively.
