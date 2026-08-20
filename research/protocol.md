# Research protocol

The research boundary is normalized, point-in-time market data for US-listed
equities/ETFs and listed OCC options only. Provider
payloads are converted to `research.market_data` records before feature
calculation or replay. Required records become actionable only at the maximum
of their market event timestamp, `as_of`, and `observed_at` timestamps. A
delayed recorder bar can therefore signal when observed; execution enters at
that decision/observation time using fresh SIP (equity) or OPRA (option)
evidence. Delayed full OHLC never backfills an earlier entry, and partial
pre-entry bar ranges are excluded. `as_of` may never be later than
`observed_at`. Records retain provider/feed identity and the New York session
date used for grouping.

The shipped paper deployment requires SIP for equities and OPRA for options.
Those are the defaults and the minimum entitlements for autonomous research
and executable option evidence; a partial or non-executable feed cannot satisfy a
research proof. The trader remains paper-only with live trading disabled and
uses the `shares` runtime execution profile. Scheduled research follows the
equity lane by default (`ALPACA_RESEARCH_VEHICLES=equity`); `option` or `all` is
an explicit operator selection, with calibration and authorization evidence
kept per vehicle. Option research is evidence generation only and does not
authorize options live execution.
Selecting the separate `options` execution profile is an explicit paper
runtime decision after OPRA evidence and controls are reviewed.

The immutable authorizing floors are: 100 trades plus 30 complete
sessions/clusters for backtest/factory windows; 100 trades plus 30 complete
sessions/clusters for the sealed qualification window; and 150 trades plus 30
complete sessions for the parity-matched live-shadow tail. Replay epoch 5
retains the epoch-4 point-in-time, executable-row, vehicle-cost,
raw-confirmatory-p, and stressed-cost boundaries and additionally seals paired
synthetic root-control shadow decisions/replays, diagnostic-only historical-
backfill provenance with exact calendar metadata, durable live-shadow FDR
allocation binding, chronological paired inference, finite BH input
validation, and conservative broker-tick equity rounding. Epoch-4 proofs remain
readable for audit but are quarantined and cannot validate, champion, or
authorize the paper trader until re-derived under epoch 5.

Production replay requires exact Alpaca calendar metadata for every session,
including early closes. Missing metadata is a refusal; no fixed 16:00 close is
promoted as a fallback.

Historical backfill preserves that exact calendar metadata in the recorder
sidecar and labels rows `source_mode: historical_backfill`. Its truthful
fetch-time `observed_at` is retained. Only an explicit diagnostic replay policy
may inspect those rows at their provider `as_of` boundary; resulting evidence is
marked `diagnostic_historical_backfill`, excluded from authorizing statistics,
and cannot authorize a proof or live deployment.

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
- a fill landing on a bar boundary uses fresh executable quote evidence at the
  causal decision/observation time when strict; a delayed full OHLC record never
  backfills an earlier entry, and any bar fallback is explicitly diagnostic;
- spread, slippage, and both-side fees are charged from one shared model;
- an option leg is priced only from a quote no older than the strict 30-second
  freshness bound at the instant being priced; a signal whose contract has no such quote
  is recorded as an explicit unpriced row, never dropped and never filled from
  the contract's last quote of the morning;
- authorizing equity fills require SIP quote provenance on both entry and exit
  legs; authorizing option fills require OPRA quote provenance on both legs.
  Provider, feed, quote age, and fill source are retained for each leg, and any
  leg older than 30 seconds, bar-only, partial-feed, or missing remains diagnostic;
- positions are force-flat before the session close;
- a bounded rule position also carries a `max_hold_bars` time exit, computed by
  the one helper the runtime uses (`agent/contracts/rule.py::hold_deadline`)
  and clamped to the force-flat time;
- equity and single-leg long-option books have separate samples, costs, and
  P&L; multi-leg and short option structures are outside the protocol.

`research/costs.py` owns the single expected-cost model and the fill
arithmetic every lane spends it through; no lane carries its own
spread/slippage/fee numbers. `cost_model_for_vehicle` selects an explicit
`costs.vehicles.equity` or `.option` override when configured and records its
provenance; otherwise the shipped schedule is 4 bps spread, 6 bps adverse
slippage, 0.5 bps per-side notional fee, and a 0.65 currency-unit option fee
per contract per side. The runtime's `execution.max_slippage_bps`
and `max_spread_bps` are rejection caps, not expectations: they bound the
model, and a model expecting a cost the runtime would refuse to submit fails
closed. Preregistered all-in stress scenarios are 9, 15, 25, and 50 bps; 25
bps is the authorization requirement and the others remain diagnostics.
Stress bps are charged against entry notional, with listed-option round-trip
fees added for both per-contract sides; they are not per-side bps. The shipped
`max_stressed_cost_to_risk_ratio` is `0.30`, so a 30-bps-floor trade's 25-bps
entry-notional stress is about `0.833` of risk and is vetoed before option fees.
For equity orders, bracket legs and limit prices are rounded toward the entry
to Alpaca's valid price ticks (`$0.01` at or above `$1`, `$0.0001` below) before
risk sizing and submission; a rounded bracket that no longer straddles entry is
rejected.
`research/calibration.py` is a read-only authorization check stratified by
runtime mode, vehicle, execution profile, and both entry and exit when
references are present. Partial fills use plan/reference fields. Missing,
stale, or insufficient calibration, an optimistic cost verdict, a terminal fill
below 80% of requested quantity, or a partial-cancel rate above 20% returns a
veto and non-zero status. Offline diagnostics may still run, but shadow
authorization remains blocked. In-flight orders are excluded, and the model is
never adjusted automatically.

`ReplayPolicy.from_config` is the runtime policy source for replay. The shipped
`execution.strict_market_data` default is `true`; direct replay APIs are strict
as well. The policy carries
the strict 30-second market-data age, option DTE (default 7–60), option spread
and liquidity checks, latest-entry and force-flat times, and portfolio limits
(concurrent positions, position notional, gross exposure, open risk, and daily
loss). Research cannot relax these option, timing, or risk constraints while
simulating.

Serial inference is deterministic: day/session-cluster deltas use a seeded
moving-block cluster bootstrap with persisted draw count, seed, and block
length. Effective breadth is persisted and re-verified as a matched
symbol-by-session correlation/eigenvalue diagnostic only; it never increases N
or replaces independent session clusters.

The IBR implementation in `research/ibr.py` provides these invariants. A
missing or partial opening range is `no trade`, not an imputed range. When the
signal is actionable at the next-bar boundary, a missing immediate next bar is
`no trade`; when recorder delay makes the signal actionable later, entry may
move to the first complete bar at/after that decision time and requires fresh
quote evidence. Stale signals are never carried across an outage.

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
variant. A pinned candidate is not exempt from automatic lifecycle stops: the
rolling-R guard, sequential drift test, and trial review still evaluate it and a
breach parks or demotes it. The operator-assigned pin identity and promotion
fields are carried into the transition as audit context, while runtime risk
limits remain independent safety controls.

No automatic process may add, remove, or rewrite the operator's configuration
pin. Lifecycle guards may nevertheless demote a pinned candidate and remove it
from runtime eligibility; the pin entry and its context remain audit metadata.

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
Candidate registration and every proof run share one canonical assumptions
hash covering feeds, session/calendar, strategy, risk, execution, and costs.
The runtime reapplies the candidate and recomputes that hash before selection;
legacy evidence or any assumption drift fails closed and must be re-researched.
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
creates exact-session candidate, paired synthetic root-control, and
randomized-null replays, and quarantines mismatch/incomplete rows. A tuned rule
descendant's root-control arm consumes the same event stream in its own virtual
book and remains isolated from EdgeLedger lifecycle state. Research-side
`edge ingest-shadow` opens
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

Before variant replay, the factory records compact fit-only diagnostics: the
eligible-prefix and first-signal rates, ATR-in-basis-points and 30-bps-floor
binding, signal-anchored planned stop/target/hold distributions, configured
and stressed cost-to-risk (9/15/25/50 bps) summaries, delivered versus intended
risk, realized exit reasons/ties/gaps, and a diagnostic clustered MDE/power
report whose effect and cluster units are explicit. It also records
provider/feed provenance, entry-pricing source, configured limits, and
pass/fail/unknown row counts. The current fixed exit grammar is machine-readable
as `fixed-atr-floor-bracket-r-target-bar-cap.v1`; its only supported modes are
the parity-verified ATR-floor bracket, configured R target, and configured bar
cap. `executable_exit_templates_added` remains `false` and
`requires_operator_review` remains `true`. An attempted trailing, partial, or
other unsupported exit field is flagged as
`rejected_unsupported_exit_grammar` before research and cannot be treated as a
research result. Planned signal/exit geometry may be counted even when
executable quote pricing is absent, but is marked quote-required and remains
non-authorizing.

The standalone `research.py factory run`/`factory-run` preflight requires
explicit row provenance and SIP for the default equity lane. `--diagnostic-only`
is an explicit non-authorizing mode for incomplete or non-SIP input and emits
no proofs.

The first-signal planned vectors also produce deterministic entry and full
behavior fingerprints. When the fit contains a signal, full behavioral aliases
produce a deterministic canonicalization proposal for operator review; all
intended variants remain in worker replay and BH until that review. Zero-signal
aliases remain visible, and the proposed alias/parameter-collapse summary is
persisted without raw rows. These are diagnostics only and cannot authorize a
candidate or read held-out/sealed sessions.

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
new run. LORD receives the raw confirmatory p-value; the family/global BH
q-values select the candidate but are not spent as online p-values. Formal
LORD validity still requires that confirmatory p-value to be based on evidence
not reused for the adaptive selection step (the in-process replay remains
diagnostic unless that independence boundary is established).

Live-shadow ingestion establishes that boundary chronologically. The unchanged
`shadow-confirmation-v4` scope handles each complete new tail by splitting it
deterministically into an older selection window and a newer, disjoint
confirmatory window; both windows must independently meet the configured
trade/session floors before any online allocation is spent. Family and global BH
use only the selection-window raw p-values. At most the selected, preflight-ready
candidate is recomputed on the confirmatory window, and only that gate's raw p is
sent to LORD. The source and provenance persist both session lists, their
digests, the disjointness marker, and the confirmatory p-value source. Legacy
same-tail v3 records remain auditable but cannot authorize under epoch 5. The
persisted live proof must match the durable FDR allocation for its vehicle scope
and test id; caller-supplied p/alpha/allocation fields are not authority.

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

Each verified gate envelope also carries per-arm candidate, baseline, and
randomized-null evidence: raw/executed/eligible counts, fill sources,
entry/exit quote-age summaries, gross/cost/net economics, matched and dropped
match keys, and directional/pair coverage. Quote density may change the
eligible or paired null/control evidence even when the candidate count is
unchanged.

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
and never accepted from a caller. The current generation is **epoch 5**. Epoch
5 retains the epoch-4 point-in-time availability, executable-row-only
statistics, vehicle-specific cost provenance, raw confirmatory p accounting,
and stressed-cost abstention boundary. It additionally seals paired synthetic
root-control shadow decisions/replays, marks historical backfill as diagnostic
non-authorizing evidence with exact calendar metadata, binds live-shadow
authorization to the durable FDR allocation, orders paired inference
chronologically, validates finite BH inputs (alpha in `(0,1]`, p-values in
`[0,1]`), and rounds broker-bound equity prices conservatively to valid ticks
before sizing.

A run from a superseded generation cannot authorize `validated`, `champion`, or
runtime eligibility, and `EdgeLedger.eligibility` names that quarantine rather
than reporting a bare ineligibility. This is deliberately not a digest check:
evidence measured under a replay engine that has since been corrected still
re-hashes and still recomputes, because the recorded rows are exactly what that
engine produced. Quarantine is not deletion — the rows stay readable and the
lifecycle history stays intact — so epoch-4 proofs remain audit-readable but
must be re-derived under epoch 5 to authorize. Authorization requires exact
equality with current epoch 5; future as well as stale epochs remain audit-only.
Each current-epoch run seals one immutable verified gate proof; re-derivation
appends a new proof rather than rewriting history. The constant is raised
whenever a replay or gate change invalidates evidence recorded before it.
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
paired synthetic root-control, and randomized-null replays; mismatched/incomplete rows are
quarantined. `edge ingest-shadow` is the sole authorization boundary: it opens
the WAL read-only, requires strictly newer complete parity-matched rows, prior
qualification, source/config/code/provenance/replay/gate hashes, family/global
BH, and durable online FDR before appending immutable `lane=shadow` proof and
the live-ingestion marker.

The live-shadow Compose service is broker-free and starts with the plain
supported deployment: it reads recorder rows
and EdgeLedger candidates through read-only connections, evaluates isolated
virtual books, and writes only its own WAL SQLite database. It has no broker
credentials or broker/runtime mutation path. The scheduled research cycle
invokes `edge ingest-shadow` by default when enabled; a missing shadow DB is a
no-op. It compares runtime shadow semantic signatures with factory/IBR replay
signatures for parity; only the research consumer can append the authorization
marker.

The checked research config enables the bounded strategy LLM with model
`gpt-5`. Compose requires the host override
`ALPACA_RESEARCH_LLM_SECRET_FILE` and mounts that separate readable provider
dotenv as `ALPACA_RESEARCH_LLM_SECRETS_FILE`; missing, unreadable, or keyless
credentials fail the cycle closed before discovery. Invalid model output leaves
a pending replacement and cannot trigger premature retirement. Good edges
produce deterministic content-addressed edge proof reports under
`research/results/edges/`, with an optional HTTPS webhook notification.
Scheduled cycles report
`completed`, `completed_no_edge`, `no_data`, or `failed`; no status bypasses the
runtime edge gate.
