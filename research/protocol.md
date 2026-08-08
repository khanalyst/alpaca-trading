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
- equity and option books have separate samples, costs, and P&L.

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
