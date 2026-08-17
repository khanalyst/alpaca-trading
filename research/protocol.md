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
- bar adjacency is required exactly where a missing minute changes an outcome,
  and nowhere else: over the bars a signal is computed from
  (`agent/contracts/rule.py::feature_window_bars`, which is the whole session
  prefix for the session-accumulating VWAP families), between a signal and its
  entry, and across a hold — a hold walk stops at a discontinuity and resolves
  the position on the last observed bar rather than treating the next recorded
  minute as adjacent. A gap after a position is resolved does not delete the
  observation: rejecting a whole symbol-session for one missing low-volume
  minute discards a large, non-random share of a real corpus;
- same-bar stop/target ties resolve to the stop;
- a gap through a level, on entry or exit, fills at the gap open;
- a fill landing on a bar boundary uses a recorded quote at that instant when
  one exists, and otherwise records that it fell back to the bar;
- spread, slippage, and both-side fees are charged from one shared model;
- an option leg is priced only from a quote no older than the strict 30-second
  freshness bound at the instant being priced; a signal whose contract has no such quote
  is recorded as an explicit unpriced row, never dropped and never filled from
  the contract's last quote of the morning;
- positions are force-flat before the session close;
- a bounded rule position also carries a `max_hold_bars` time exit, computed by
  the one helper the runtime uses (`agent/contracts/rule.py::hold_deadline`)
  and clamped to the force-flat time;
- equity and single-leg long-option books have separate samples, costs, and
  P&L; multi-leg and short option structures are outside the protocol.

A planned risk unit must be worth at least
`research/gates.py::MIN_RISK_UNIT_COST_RATIO` (3) round trips of the shared
cost model, measured as the median over a run's held-out equity trades by
`risk_unit_report` and enforced as the `risk_unit_adequate` gate check. Below
that multiple the break-even hit rate leaves the range a directional intraday
rule can reach, so a held-out sample that looks profitable is reporting noise
that has not yet paid its costs. An equity result that cannot state a risk unit
at all fails the check rather than skipping it. The option vehicle is not
applicable and does not veto: a long option risks the whole premium, which is
never a handful of basis points. The same invariant is enforced at the signal,
where `agent/contracts/rule.py::MIN_STOP_DISTANCE_BPS` (30 bps) floors every
planned stop regardless of `stop_atr` and `atr_period` — a one-minute ATR is a
few basis points, so without the floor the multiplier alone plans stops smaller
than the spread and slippage paid to take them. The floor binds behaviour, not
identity: `DEFAULT_RULE_SPEC` is the normalization target for omitted fields,
so its values are frozen and a stored spec keeps its content-addressed
`variant_id`.

`research/costs.py` owns the single expected-cost model and the fill
arithmetic every lane spends it through; no lane carries its own
spread/slippage/fee numbers. Its parameters come from one `costs` config
block. The runtime's `execution.max_slippage_bps` and `max_spread_bps` are
rejection caps, not expectations: they bound the model, and a model expecting
a cost the runtime would refuse to submit fails closed. `research/calibration.py`
provides a read-only advisory calibration stratified by runtime mode, vehicle,
execution profile, and entry versus exit when references are present. Thin or
missing strata are `insufficient_data`; no pooled equity/options verdict is
emitted and the model is never adjusted automatically.

`ReplayPolicy.from_config` is the runtime policy source for replay. It carries
the strict 30-second market-data age, option DTE (default 7–60), option spread
and liquidity checks, latest-entry and force-flat times, and portfolio limits
(concurrent positions, position notional, gross exposure, open risk, and daily
loss). Research cannot relax these option, timing, or risk constraints while
simulating.

The IBR implementation in `research/ibr.py` provides these invariants. A
missing or partial opening range is `no trade`, not an imputed range. A missing
immediate next bar is also `no trade`; stale signals are never carried across
an outage.

Market-data strictness is lane-specific. The validated research-only setting
`research.backtest_bar_fallback` defaults to `true`: historical backtest lanes
may price a missing equity quote at a bar boundary from the bar, using the
shared conservative spread/slippage/fee model, and record `quote` versus `bar`
as the fill source. Forward-shadow, live-shadow, and paper lanes remain strict
and require a fresh executable quote. Direct replay APIs default to strict;
only a lane orchestrator or the standalone `backtest-ibr` command opts into
historical fallback. A passing backtest is evidence for the next gate, never
authorization to trade paper.

## Evidence

An evidence package should include the normalized input digest, event count,
provider/feed/schema identities, timezone/session policy, as-of cutoff,
configuration, code fingerprint, cost model, risk/`ReplayPolicy`, gate
assumptions, and deterministic fixture result. The experiment identity binds
dataset, configuration, code, cost, risk, gates, and provenance hashes. Results
without this provenance are descriptive only and cannot pass a qualification
gate. Walk-forward and held-out checks must be chronological. Paired baseline,
placebo, and acceptance-floor checks are evaluated independently for each
vehicle and may not pool option and underlying returns.

## Deployment states

Four states, one of which no automatic process may enter.

*Research* and *proved* are the lanes above: gates decide, and they may move a
candidate in either direction on evidence. Offline historical and forward
replay may persist a passing `lane=shadow` proof, but that status is stability
evidence only and never authorizes runtime entries.

*Trial* is a proved, unpinned edge trading the paper account. Its live outcomes
are append-only evidence attributed to the exact passing shadow proof that
authorized entry. After a configured window of sessions and trades it is
judged against an explicit floor. An edge below the floor is demoted and the
result is recorded as a graded lesson sourced from live paper, which later
proposals may read. An edge above it keeps trading and is reported as
promotable. If a demoted candidate later earns a newer passing shadow proof, it
may re-prove; the new trial begins a new proof epoch and cannot inherit the
earlier epoch's wins or losses.
Underpowered windows and outcomes without a usable risk reference are never
treated as failure; a failed or unverifiable latest shadow proof quarantines
history rather than falling back to lifetime aggregation.

*Pinned* is an operator-declared promotion: an entry in `strategy.pinned`
carrying an operator-assigned id, an exact `variant_id`, and a vehicle.
Pinning is a selection, not an authorization — a pinned entry still resolves
through the same evidence gate and a pin that does not resolve trades nothing
rather than being substituted. It is the preferred live route because the
operator-assigned id makes the promotion explicit and auditable; the legacy
`selection_mode: specific` route remains supported for one exact proved
variant. A pinned candidate is exempt from every automatic lifecycle change:
the rolling-R guard, the sequential drift test, and the trial review all still
evaluate and still record what they found, but they raise a durable alert
instead of transitioning it. Runtime risk limits are unaffected; they are
safety, not lifecycle.

No automatic process may move a candidate into the pinned state, and none may
move one out of it.

## Configuration provenance

Every distinct configuration a runtime operates under is content-addressed and
recorded append-only with a version id, the previous version, and a diff naming
each field that changed. Secret-bearing fields are redacted before hashing, so
the trail records that such a field changed and never what it changed to, and a
change confined to redacted values is therefore not distinguishable as a new
version. Records are immutable. Failure to write this audit row does not stop a
runtime that may own exposure; it produces a sticky degraded heartbeat with
reason `config_audit_unavailable` until a successful audit clears it.

## Autonomous edge lane

The bounded registry in `research/variants.yaml` remains the complete proposal
surface for the explicit IBR baseline. Autonomous multi-strategy research uses
the finite, validated data-only grammar in `agent/contracts/rule.py`; generated
variant ids are content hashes of those specifications. Arbitrary source code
or unbounded fields are rejected. `agent.edge` resolves only a SQLite candidate
whose status is `validated` or `champion` for the configured strategy/vehicle
and whose latest shadow proof carries the parity-matched live-ingestion marker.
Paper `selection_mode: all_proved` may run one strongest passing variant per
independent family under one global risk book. Live mode resolves exactly one
proved record: preferably one `selection_mode: pinned` entry, or one legacy
`selection_mode: specific` variant. It never substitutes a different
candidate when the requested proof/configuration no longer re-verifies.

Backtests and forward-shadow runs are persisted with immutable hashes and
trade/evidence rows. The autonomous lane first evaluates the initial corpus as
a backtest, then accepts only a later, unseen session tail for offline
forward-shadow evidence. Passing gates advance `candidate` ->
`backtest_passed` -> `shadow` only; runtime entries stay blocked because
backtest or offline forward-shadow evidence cannot authorize paper deployment.
The broker-free ShadowRunner
evaluates eligible candidates in isolated virtual books from recorder events,
creates exact-session candidate/root-baseline/randomized-null replays, and
quarantines mismatch/incomplete rows. Research-side `edge ingest-shadow` opens
the shadow WAL read-only, requires strictly newer complete parity-matched rows,
prior qualification, source/config/code/provenance/replay/gate hashes, family
and global BH plus durable online FDR, then appends the immutable `lane=shadow`
proof and live marker; only then can the candidate become `validated`/`champion`.
A candidate cannot skip the lifecycle or silently move backwards. Paper
outcomes are append-only, proof-epoch scoped, and may demote a deployed edge.
Normal operation needs no manual promotion. Explicit `edge promote` is
supported only as an audited control subject to lifecycle/evidence rules and
cannot bypass the live marker. Legacy validated/champion rows without that
marker can be evaluated/migrated but remain ineligible until a new authorized
live proof.
Backward rollback is rejected; explicit demotion is the operator safety
action.

Factory mutations may inspect only the chronological fit partition. Held-out
and later-forward sessions must not influence hypothesis or parameter
generation. This binds the bounded LLM tuning lane exactly as it binds the
deterministic mutation table: a tuning request is given the fit-partition
diagnosis and the graded outcomes of *already completed* proposals, never a
held-out, sealed, or later-forward observation. Each variant is evaluated in an
isolated simulated account.

No lane may extend the signal vocabulary. The families, confirmation filters,
sides, permitted fields, and numeric bounds in `agent/contracts/rule.py` are
closed sets, and the feature computation itself is code that research never
generates or rewrites. A proposal naming an unknown family, filter, field, or
data source is rejected at the boundary. Tuning is bounded further: a tuned
variant must preserve both its root's `family` and its root's grammar
`schema`, so it may change only the values of fields the root already carries.
Selecting a different family, or a wider grammar version, is a discovery
decision and follows the discovery path with its own gates.

The three LLM request contracts use strict full-schema structured output with
`additionalProperties: false`; tuning must echo the complete normalized root
specification. The adapter enforces a per-run total-call budget, bounded
attempts/time/response bytes, and an authentication circuit. Each result keeps
schema/grammar hashes, call counts, circuit state, and per-attempt hashes or
errors as evidence.

Every proposal — deterministic or model-authored — records a stated reason
before the gate that judges it is computed, and that reason is graded against
the resulting gate afterwards. Both records are append-only and the grade is
written once. A model-authored proposal made against a non-empty history must
additionally cite the graded lesson it reasoned from; the citation is resolved
against the ledger and stored, and an unresolvable one is refused. A parameter
set is closed only when a graded lesson records a powered upper-bound rejection
of the minimum useful edge; an underpowered or adequate-but-inconclusive result
is not such a record. The graded pairs may
be fed back into later proposals; because they describe only completed,
already-corrected evaluations, doing so adds no information about unseen data.
Multiple-test correction is what prices the search, so a tuned variant counts
against the same family-local and cycle-global false-discovery budget as a
mutated one.

Variant identity is de-duplicated by a family-specific executable semantic
signature, including the v1/v2 no-op alias. A graded
`adequate_negative_rejection` suppresses only that exact failed variant id;
underpowered and inconclusive outcomes do not suppress a future attempt and do
not close the family.

Refinement follows a fixed sequence. Coordinate phase changes exactly one
executable field per child. Interaction phase begins only after every bounded
coordinate point is closed and combines exactly two of the strongest measured
one-field values. A final unchanged replay confirms the conclusion before a
replacement is allowed. Model tuning is schema-validated against the same
one-field/two-field rule; it cannot hide a bundle of changes inside a reason.
Multiple-test correction covers every variant evaluated in the cycle. A
replacement hypothesis may be generated only after the root family has at
least 100 executed trades in each fit/held-out partition, at least 30 held-out
sessions, a 95% clustered upper bound no greater than the 0.05R minimum useful
edge, and at least two adequate negative forward windows for every bounded
point; underpowered or inconclusive data must not cause autonomous hypothesis
churn.

Each transition requires a chronological fit/held-out boundary, fit and
held-out structural floors for trades/sessions/clusters, matched baseline
deltas, cluster-level sign randomisation, and both family-local and
cycle-global false-discovery correction. Selection compares candidates across
families, so the q-value that authorizes a champion is the global one. Because
Benjamini-Hochberg is monotone in the number of tests, cycle-global q is never
less conservative than family q; a family pass/global fail is therefore a
normal result for a marginal candidate, and proof verification must compare
each decision flag with its own q-value.

The post-selection test also consumes a durable cumulative online-FDR
allocation. Its LORD-style state is stored per scope in the factory ledger and
persists across cycles, so alpha allocation and discoveries are not reset by a
new run.

Beating a control is necessary but never sufficient. A variant must also show
absolute after-cost profitability on unseen data (positive net P&L and
positive per-trade expectancy), a positive lower confidence bound on the mean
held-out delta, a positive delta against a randomized-entry null control that
shares the candidate's session/symbol/direction distribution and exit rules,
and a majority of positive fixed-rule rolling-origin forward-stability folds.
The same rule is evaluated in every fold.

The falsification check is a seeded permutation test: at least ten thousand
cluster-level sign-flip draws form an explicit null distribution, and the
decision is the empirical one-sided p-value against it. The draw count and
seed are derived from the matched evidence and persisted, so the distribution
is reproducible.

The last sessions of every evaluation corpus are sealed into a final
qualification window before any worker is scheduled. Selection, mutation and
diagnosis never receive them. Development evidence is ranked and corrected
first; one preselected candidate alone releases and consumes the sealed window
exactly once for the last go/no-go. Other variants remain diagnostic and cannot
authorize a proof. Sealed sessions are scored, never split,
so they enter no fit/held-out run, trade row, or family correction. The verified
gate does retain a bounded copy of the candidate and baseline qualification
observations, the exact declared session set, and content digests. Verification
recomputes the qualification report and rejects a row from an undeclared
session, a missing declared session, digest tampering, excessive row count, or
an oversized envelope.

Both research lanes are held to this standard. The explicit IBR lane and the
autonomous factory lane share one randomized-entry null control and one sealed
final window rather than each carrying its own; a corpus too thin to seal a
window or to support rolling-origin folds is underpowered, not failed.

The held-out trade floor is evidence, not a tuning knob. The shipped default
universe is eight liquid ETFs (`SPY`, `QQQ`, `IWM`, `DIA`, `XLF`, `XLK`, `XLE`,
`XLV`), improving opportunity capacity, but real signal rates still require
sufficient history. Replay allows at most one trade per symbol-session; floor
feasibility fails closed when 100 held-out trades cannot be supported. Widen
history and/or the universe, never lower the floor.

The complete gate is durably persisted and re-verified before validation or
champion selection. Re-verification recomputes the analysis — matched deltas,
p-value, lower bound, falsification, absolute profitability — from the stored
source rows and compares it against the recorded decision, rather than only
re-checking hashes. Champions are ranked by the lower confidence bound, not
the raw held-out delta.

Every run also records the replay generation it was measured under
(`research/edge_ledger_store.py::REPLAY_ENGINE_EPOCH`), assigned by the ledger
and never accepted from a caller. A run from a superseded generation cannot
authorize `validated`, `champion`, or runtime eligibility, and
`EdgeLedger.eligibility` names that quarantine rather than reporting a bare
ineligibility. This is deliberately not a digest check: evidence measured under
a replay engine that has since been corrected still re-hashes and still
recomputes, because the recorded rows are exactly what that engine produced.
What changed is that those rows describe fills, quote ages, portfolio limits or
multiplicity accounting the current protocol does not accept. Quarantine is not
deletion — the rows stay readable and the lifecycle history stays intact — so
re-deriving under the current engine is the only route back to a deployable
proof. The constant is raised whenever a replay or gate change invalidates
evidence recorded before it.
Underpowered or inconclusive data is not failure. Retirement is permitted only
after every bounded point and the confirmation carry a powered upper-bound
rejection across multiple negative windows; an enabled LLM lane must first
register a valid bounded replacement. A demoted candidate
may re-prove on a newer shadow run, starting a new evidence epoch. Drawdown is
persisted and used to rank otherwise qualified champions conservatively.

The offline forward-shadow boundary advances only from a durable,
re-verifying passing replay proof. Diagnostic account rows never advance it. If
even one intended variant in a worker lacks the required trade/session floors,
that worker persists no shadow proof, transition, or reseed, and the complete
tail remains eligible for reconsideration on the next cycle. Offline shadow
status never authorizes runtime. The live ShadowRunner consumes recorder
events in candidate-isolated virtual books and creates exact-session candidate,
root-baseline, and randomized-null replays; mismatched/incomplete rows are
quarantined. `edge ingest-shadow` is the sole authorization boundary: it opens
the WAL read-only, requires strictly newer complete parity-matched rows, prior
qualification, source/config/code/provenance/replay/gate hashes, family/global
BH, and durable online FDR before appending immutable `lane=shadow` proof and
the live-ingestion marker.

The optional live-shadow Compose profile is broker-free: it reads recorder rows
and EdgeLedger candidates through read-only connections, evaluates isolated
virtual books, and writes only its own WAL SQLite database. It has no broker
credentials or broker/runtime mutation path. The scheduled research cycle
invokes `edge ingest-shadow` by default when enabled; a missing shadow DB is a
no-op. It compares runtime shadow semantic signatures with factory/IBR replay
signatures for parity; only the research consumer can append the authorization
marker.

The checked research config enables the bounded strategy LLM with model
`gpt-5`. Enabling it is an explicit operational contract: `research-cycle.sh`
refuses to start a cycle that claims to be model-assisted without a credential
for it, so a deployment without `OPENAI_API_KEY` fails closed with
`strategy LLM is enabled but OPENAI_API_KEY is unavailable` rather than
silently running deterministically. Set `research.strategy_llm.enabled=false`
to run the deterministic lane instead. Within a cycle the adapter reads only
the optional `ALPACA_RESEARCH_LLM_SECRETS_FILE`; missing or invalid
credentials/output leave a pending replacement and cannot trigger premature
retirement. Good edges produce deterministic content-addressed
edge proof reports under `research/results/edges/`, with an optional HTTPS
webhook notification. Scheduled cycles report
`completed`, `completed_no_edge`, `no_data`, or `failed`; no status bypasses the
runtime edge gate.
