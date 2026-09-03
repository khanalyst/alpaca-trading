# Alpaca intraday research

This directory contains offline replay and bounded research utilities for
US-listed equities/ETFs and listed OCC options; crypto is outside scope.
Options are single-leg long calls or puts only; multi-leg, naked, and short
structures are not supported. Data adapters
normalize their payloads through `research.market_data` before any analysis.
Every normalized event
records its provider, feed, schema, session date/timezone, observation time,
and the as-of timestamp used by the analysis. Availability is bounded by the
latest of event timestamp, `as_of`, and `observed_at`; `as_of > observed_at`,
naive timestamps, future values, invalid quotes, and malformed OHLC values fail
closed. Feed provenance is request-bound: retain an explicit provider-row feed
label when present, otherwise use the configured/requested feed label; it is
not an independent venue attestation.

The shipped paper deployment defaults to the free Basic IEX equity feed and an
equity-only universe; it does not acquire options. `indicative` is the safe
options-feed default and is non-executable. Authorizing equity evidence must
use the exact IEX or SIP feed; `delayed_sip` is diagnostic only. The trader's
runtime execution profile remains `shares`, with live
trading disabled. Scheduled research follows the deployed equity/shares lane by
default (`ALPACA_RESEARCH_VEHICLES=equity`). Set the variable to `option` or
`all` only after explicitly adding an option universe and configuring OPRA;
option research is evidence generation, not permission to execute options.
Compose starts the recorder, scheduled research, broker-free shadow, watchdog,
trader, and dashboard with one plain `docker compose up -d` command; systemd
uses the same defaults.

The recorder samples on a fixed 30-second cadence and keeps durable per-symbol
quote and completed-bar watermarks. Research readiness requires both watermarks
to be no older than 30 seconds for each required symbol; a quote watermark
cannot stand in for a bar watermark. Exact Alpaca calendar metadata records
holidays and early closes. Scheduler liveness is reported separately from
research evidence/readiness, so an alive scheduler is not evidence of a ready
corpus or a validated edge.

## Normalized data

`UnderlyingBar` is a one-minute OHLCV record. `QuoteSnapshot` is a timestamped
bid/ask record. `OptionContract` and `OptionSnapshot` carry the contract
identity and multiplier separately from the quote. Use
`normalize_underlying_bar`, `normalize_quote`, and
`normalize_option_snapshot` at provider boundaries; replay code does not
accept raw provider dictionaries.

The deployment recorder writes bars, quotes, and option snapshots to one mixed
corpus, appended into one partition per New York session date under
`sessions/` with a sidecar index. `deploy/research-cycle.sh` concatenates those
partitions in session order (`ALPACA_RESEARCH_SESSION_WINDOW` limits it to the
most recent N), validates the result, and routes the vehicle-local discovery
lanes from it.
It also invokes `research.strategy_factory`, which evaluates twelve logical
strategy slots over the finite catalog of twelve rule families by default.
Slot count and worker capacity are independent controls. Each generated
variant owns an isolated simulated account processed by one bounded worker; no
capital or P&L is shared between arms.
Paper runtime selection can then use `selection_mode: all_proved`, which keeps
one strongest proven variant per verified frozen prior-cycle dependence cluster
under one global risk book. Families without a verified cluster assignment use
the held-out correlation-safe fallback; symbol breadth is not an independence
license.

Historical `deploy/backfill.py` partitions retain the recorder schema but carry
`source_mode: historical_backfill` and exact Alpaca session open/close metadata,
including early closes. Their fetch-time `observed_at` is retained rather than
backdated. An explicit diagnostic replay policy may inspect these rows and
labels resulting trades `diagnostic_historical_backfill`; they are excluded from
authorizing statistics and can never authorize a live-shadow proof.

The authorizing evidence floors are immutable: backtest/factory windows require
100 trades, 30 complete sessions, and 30 session clusters; qualification
requires 100 trades and 30 complete sessions/clusters; the parity-matched
live-shadow tail requires 150 trades and 30 complete sessions. These are
evidence floors, not tuning knobs. The shipped default universe is 24 liquid
ETFs spanning broad-market, size, sector, international, rates/credit, metals,
and semiconductor exposures (the exact list is in `config.yaml`), improving
opportunity capacity, but real signal rates still require sufficient history.
Replay allows at most one trade per symbol-session; floor feasibility fails
closed when a required floor cannot be supported. Widen history and/or
`universe.symbols`, never lower a floor.

Qualification is powered at a minimum of 100 trades, 30 complete sessions, and
30 session-level clusters. Replay epoch 6 retains the epoch-5 point-in-time,
executable-row, vehicle-cost, raw-confirmatory-p, and stressed-cost boundaries,
and additionally seals paired synthetic root-control shadow decisions/replays,
diagnostic-only historical-backfill provenance with exact calendar metadata,
durable live-shadow FDR allocation binding, chronological paired inference,
finite BY input validation, and conservative broker-tick equity rounding.
Epoch-5 proofs remain readable for audit but are quarantined and cannot validate,
champion, or authorize the paper trader until re-derived under epoch 6.
Authorization requires exact epoch equality with current epoch 6; future epochs
are audit-only as well. A verified current-epoch gate is sealed immutably per
run, and re-derivation appends a new proof instead of rewriting prior evidence.

The default session timezone is `America/New_York`. Production replay requires
the exact Alpaca calendar session bounds for every session, including early
closes; it never promotes a missing calendar day to a fixed 16:00 close.
Session dates are derived after timezone conversion, so the daylight-saving
transitions in March and November do not shift a bar into a neighboring session.
A source `as_of` timestamp may not be later than its observation/ingestion
timestamp.

## IBR replay

`research.ibr.replay_ibr` implements the initial-balance-range path:

1. Require a complete, contiguous range of completed one-minute bars from the
   configured US session open.
2. Detect a breakout only after a range bar has closed.
3. Make the signal actionable at the maximum availability time of the required
   records (`timestamp`, `as_of`, and `observed_at`). A delayed recorder bar
   may signal when observed; enter at that decision/observation time using a
   fresh exact IEX or SIP quote (equity) or OPRA quote (option). Delayed full OHLC never backfills an earlier entry, and
   partial pre-entry ranges are excluded.
4. Apply gap-aware fills and stop-first ties when a candle touches both stop
   and target. A bar that opens beyond the stop or target fills at that open,
   on exit as well as on entry, not at a level the market never traded again.
5. Price a fill landing on a bar boundary from a recorded quote at that
   instant when one exists, and record on the trade whether the quote or the
   bar was used. A level triggered inside a bar has no such instant and keeps
   the level.
6. Apply spread, adverse slippage, and per-side fees to both executions
   through the one shared model in `research.costs`.
7. Force-flat at the configured pre-close boundary; no position crosses a
   session boundary.

Equity and single-leg long-option vehicles are independent result books. Call
`replay_ibr_vehicles` for a mapping of separate results; it intentionally has
no pooled P&L field. Option replay requires timestamped option snapshots and
uses their contract multiplier.

Market-data strictness is lane-specific. The shipped
`execution.strict_market_data` default is `true`; direct replay APIs default to
strict `ReplayPolicy` behavior and require a fresh executable equity quote at each
boundary that needs one. Historical backtest lanes use the validated,
research-only `research.backtest_bar_fallback` setting (default `true`): when a
boundary has no equity quote, the bar may supply the reference, the shared
conservative spread/slippage/fee model still prices the fill, and the result
records whether `quote` or `bar` supplied it. Forward-shadow, broker-free
live-shadow, and paper remain strict and require fresh executable quotes.
Backtest evidence never authorizes paper.

Authorizing fill-quality evidence is stricter than a diagnostic backtest:
equity entry and exit legs must carry exact IEX or SIP quote provenance, and
option entry and exit legs must carry OPRA quote provenance. `delayed_sip` is
diagnostic only. Every leg records provider, feed, quote timestamp/age, and
fill source; both legs must be executable and no older than 30 seconds.
Bar-only, missing, partial-feed, or stale legs remain
diagnostic and cannot authorize a proof. Fit diagnostics may count planned
signal/exit geometry as a quote-required, non-authorizing measurement when
executable pricing is absent.

The command-line surface is intentionally small:

```bash
python research.py validate-data bars.jsonl --provider alpaca --feed iex
python research.py backtest-ibr bars.jsonl --vehicle equity
python research.py backtest-ibr bars.jsonl --vehicle equity --strict-market-data
python3 research.py llm-preflight --agent-config config.yaml
```

`backtest-ibr` defaults to historical bar fallback; `--strict-market-data`
opts into strict diagnostics. `validate-data` and `backtest-ibr` are offline and
read JSONL only. They never download data, call an exchange, place orders, or
modify trading state. `llm-preflight` is the provider-network exception described
below; it does not load market data or touch trading state.

### Provider preflight

`llm-preflight` makes one bounded, non-authorizing provider call and does not
load a dataset or write standalone artifacts. Run it after changing the
research endpoint or model and before an expensive cycle. The scheduled
wrapper persists the same bounded, redacted result in the terminal cycle JSON,
status heartbeat, and operational history (including `degraded` transient
outages), while still allowing deterministic fallback. `research.strategy_llm.model` accepts a
provider model ID for generic endpoints. For Azure OpenAI-compatible base URLs,
set the optional `research.strategy_llm.deployment` to the resource-local
deployment alias; the preflight fails clearly before corpus work when that
alias is absent, while retaining `model` as catalog/evidence metadata.
Authentication/configuration failures and HTTP 404 `DeploymentNotFound` are
fatal and stop the scheduled cycle. Timeouts, 429s, and 5xx responses are
transient: the cycle records degraded provider evidence and keeps its
deterministic fallback. If every runtime LLM call later fails, the cycle ends
with explicit `llm_provider_failure` unless an authorizing proof already exists.

## Costs and fill calibration

`research.costs.CostModel` is the single expected-cost model: every lane --
IBR replay, edge discovery, and the strategy factory -- prices its fills
through it, and none carries its own spread/slippage/fee numbers or its own
arithmetic. `cost_model_for_vehicle` selects optional `costs.vehicles.equity`
or `.option` overrides and persists their provenance; absent an override, the
shipped defaults are 4 bps spread, 6 bps adverse slippage, 0.5 bps per-side
notional fee, and a 0.65 currency-unit option fee per contract per side. These
are expected costs, not rejection caps.

Every proof also persists the preregistered all-in stress scenarios of 9, 15,
25, and 50 bps. The 25 bps scenario is the authorization requirement; the
other scenarios remain diagnostics. A stress result that is missing, negative,
or not positive at 25 bps cannot authorize a candidate.

Stress semantics are explicit: each scenario charges its bps against entry
notional, then listed options add round-trip fees for both per-contract sides;
the stress bps are not per-side bps. The shipped
`max_stressed_cost_to_risk_ratio` is `0.30`; a 30-bps-floor trade has about
`0.833` cost-to-risk at the 25-bps stress and is therefore vetoed before any
option fees.

The runtime's stressed-cost risk check abstains before order submission when
the configured 25 bps scenario (or another preregistered 9/15/50 bps choice)
exceeds its allowed cost-to-risk ratio, and persists the scenario, cost, ratio,
and vehicle telemetry. The runtime's `execution.max_slippage_bps` and
`max_spread_bps` are rejection caps, not expectations. They are read into the
same model and bound it: a model expecting a cost the runtime would refuse to
submit fails closed rather
than simulating fills that could never happen. The pure entry-slippage check
applies the same quote-versus-reference cap to runtime, factory, explicit IBR,
and randomized-null quote entries; malformed inputs use
`entry_slippage_invalid`, while over-cap quotes use
`entry_slippage_exceeds_limit`. Sourcing an expected slippage from the cap is
as wrong as ignoring the cap.

Quote-driven fills need the quote rows to reach a strict lane. `research.edge_lab`
and `research.strategy_factory` are handed the complete mixed corpus by
`deploy/research-cycle.sh`, so a scheduled cycle prices a boundary fill from a
recorded quote where one exists and records the source on the trade. Historical
backtests may use the research-only bar fallback described above; the
bar-only, quote-only and option-only views the script derives alongside it are
used to decide which vehicle lanes to run and to feed the standalone
`backtest-ibr` invocation, which receives the quotes explicitly.

```bash
python research.py calibrate runtime/paper/journal.db --vehicle equity
# Use --vehicle option for the independent option calibration lane.
```

`research.calibration` reads the runtime journal read-only and reports adverse
cost in basis points, model bias, runtime-cap overruns, and a verdict of
`conservative`, `optimistic`, or `insufficient_data`. Results are stratified by
runtime mode, vehicle, execution profile, and by both entry and exit when
references are present; partial fills use the plan/reference fields rather than
their realized notional, and missing references remain unreferenced. Equity and
options are never pooled. Pass `--vehicle` (or the API's `vehicle=` argument)
for an explicit per-vehicle report; the report records its filter and available
journal vehicles. Scheduled shadow authorization always computes and checks the
matching vehicle report, so a mixed journal cannot authorize the other lane.
Calibration is an authorization gate: missing, stale,
or insufficient evidence, an optimistic cost verdict, a terminal material
underfill (<80% of requested quantity), or a partial-cancel rate above 20%
returns a veto and non-zero status. The scheduled cycle still records offline
discovery/factory diagnostics, but it does not ingest shadow authorization until
calibration is fresh and authorized. In-flight orders are excluded; calibration
never adjusts the model automatically.
The scheduled calibration-only pass is per symbol/session on the fixed
9/15/25/50-bps ladder and is disabled by default. Activation requires an
explicit operator path and a content-addressed artifact with exact
provider/feed identity, disjoint chronological held-out sessions, sufficient
coverage, and one artifact-wide effective-after boundary. An unusable cell
falls back to the configured scalar stress; the artifact cannot authorize
itself.

## Evidence and provenance

Research artifacts require row-level provider/feed identity; command-line flags
cannot manufacture missing IEX/OPRA provenance. They retain the normalized input
digest, configuration, code version, cost model, risk/`ReplayPolicy`, and
gate assumptions alongside results. The experiment identity binds dataset,
configuration, code, cost, risk, gates, and provenance hashes. Any feature or
label must be computed from events at or before its as-of timestamp.  A
completed-bar fixture and a no-look-ahead test are required for every new
replay path.  Fixed-rule rolling-origin forward-stability, paired-baseline, placebo/falsification, and
acceptance-floor checks are mandatory gates, with fit/held-out structural
floors, family-local, frozen-dependence-cluster, and cycle-global false-discovery
correction, sealed
qualification source binding, and a durable verified gate. A family-local pass
with a cycle-global failure is a normal marginal result; only a candidate that
also clears the frozen-cluster veto can authorize cross-family selection. The
gates are applied per vehicle and per
session rather than to a pooled equity/options series. Rolling-origin uses the
same fixed rule in every fold. The cumulative online-FDR allocation is durable
per vehicle scope and persists across cycles. The active
`shadow-confirmation-v6` scope implements the LORD++ construction of
[Ramdas, Yang, Wainwright, and Jordan (2017)](https://proceedings.neurips.cc/paper_files/paper/2017/hash/7f018eb7b301a66658931cb8a93fd6e8-Abstract.html): with
`gamma_k=1/(k*(k+1))`, explicit `W0=alpha/2`, and discovery indices `tau_j`, the
allocation at test `t` is `W0*gamma_t + (alpha-W0)*gamma_(t-tau_1) +
alpha*sum_(j>=2, tau_j<t) gamma_(t-tau_j)`. Thus pre-discovery spending is
`(alpha/2)*gamma_t`, the first-discovery reward is the explicitly recorded
`alpha-W0=alpha/2`, and subsequent rewards are `alpha`. v5 rows retain their
prior `W0=alpha` semantics and are not replayed into v6. The cited result controls
mFDR under conditionally super-uniform null p-values; its full FDR guarantee
additionally assumes independent null p-values and predictable test levels
that are monotone in prior discoveries. The implementation fixes that
predictable monotone schedule, but only the confirmatory design can justify
the p-value and dependence assumptions. Disjoint chronological tails prevent
selection-row reuse; they do not establish either assumption. The sealed
qualification window is released once by
one preselected candidate alone, while other variants remain diagnostic.

Serial inference is deterministic: paired deltas are grouped by chronological
session/day clusters and the persisted lower bound uses a seeded moving-block
cluster bootstrap (with its draw count, seed, and block length). The persisted
effective-breadth report is a correlation/eigenvalue participation-ratio
diagnostic over matched symbol-by-session deltas. Breadth is re-derived and
verified with the proof, but it never counts as additional independent sample
size; independence still comes from session clusters. Authorizing dependence is
separate: before each factory cycle, completed prior-cycle family deltas are
frozen into a hash-verified map. Strong clusters receive an additional
cluster-level BY veto, and runtime allocation admits at most the strongest edge
per verified frozen cluster; an unavailable map never grants independence.

Remaining extension boundaries are explicit. Universe expansion requires an
operator-approved exact symbol list, recorder coverage for that list, and a new
identity/proof. Event conditioning requires a point-in-time event source with
provider, `as_of`, and observation provenance. Prior-session, true
multi-timeframe, and cross-sectional features require explicit replay context;
missing or ambiguous context fails closed. Shadow quarantine is not an auto-skip:
an unresolved session blocks watermark/FDR advancement until source correction
and a bounded parity replay complete.

## Edge laboratory

`research.edge_lab` stores pre-registered candidates, immutable
backtest/shadow runs, trades, evidence, paper outcomes, and lifecycle events
in SQLite. Every run carries dataset, configuration, code, and provenance
SHA-256 hashes. The default database is `runtime/research/edge_lab.sqlite3`
(override with `ALPACA_EDGE_DB`).

The lifecycle is forward-only: an initial corpus backtest moves a candidate to
`backtest_passed`; a later corpus must contain sessions strictly after the
persisted boundary, and passing offline forward-shadow gates may move it to
`shadow` only. Offline historical/forward replay never authorizes runtime.
Only broker-free ShadowRunner output consumed by research-side
`edge ingest-shadow` can append the parity-matched live marker and move a
candidate to `validated`/`champion`. A shadow boundary advances only from a
durable re-verifying passing proof; an underpowered worker persists no shadow
result, so the same tail can be reconsidered later. Paper outcomes
are append-only evidence scoped to the exact shadow proof that authorized the
entry and may demote a deployed edge. A demoted candidate may re-prove on a
newer shadow run; re-proving begins a new trial epoch instead of aggregating
lifetime outcomes. Candidates are scored
separately for `equity` and `option` vehicles. Gates require chronological
held-out data, fit/held-out trade and session structural floors, matched
controls, cluster-aware randomisation, family-local, frozen-dependence-cluster,
and cycle-global FDR,
sealed qualification observations/digests, and placebo/falsification.
The v3 proof stores the complete family/global/cluster raw p-value batches and
recomputes BY during verification; scalar q-values alone cannot authorize.
Its second placebo seed is an integrity replication over the same held-out
sample, not independent market evidence or another significance hurdle.
Underpowered or inconclusive data is not failure. Retirement is allowed only
after all bounded coordinate/interaction points and the final confirmation
carry a powered upper-bound rejection across multiple negative windows; a
valid bounded LLM replacement must be registered first when that lane is enabled.
Drawdown is
measured and used in conservative champion ranking. Normal operation needs no
manual promotion; the `edge promote` CLI is only for explicit, audited controls
and cannot bypass the live marker. Legacy validated/champion rows without that
marker may be evaluated/migrated but remain ineligible until a new authorized
live proof. Backward rollback is rejected; explicit demotion is the operator
safety action.

## Broker-free live shadow

For a supported paper deployment, create two files outside Git: an Alpaca
paper broker secret (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`,
`ALPACA_PAPER=true`) and a separate readable research-provider dotenv file
containing `OPENAI_API_KEY` for the checked provider (or the matching
configured provider key). Set the host path in
`ALPACA_RESEARCH_LLM_SECRET_FILE`, validate, and start every lane together:

```bash
docker compose config --quiet
docker compose up -d
docker compose ps
```

Compose starts scheduled research and the broker-free shadow lane in the default
startup. It refuses to render the research service when the separate provider
secret path is missing or unreadable. The plain Compose `shadow` service reads recorder rows and EdgeLedger
candidates through read-only connections, evaluates eligible candidates in
isolated virtual books, and writes only its own WAL SQLite database. For each
complete session it creates candidate, paired synthetic root-control, and
randomized-entry-null replays; mismatch or incomplete rows are quarantined.
It uses the deterministic runtime signal/setup/risk path and compares semantic
shadow signatures with factory/IBR replay for parity. Tuned rule descendants
also receive a paired synthetic root-control arm: it consumes the same events
in its own virtual book and writes first-class shadow replay evidence, while
remaining outside EdgeLedger lifecycle state. It has no broker
credentials, order path, or broker/runtime state authority. The scheduled
research cycle runs `edge ingest-shadow` by default when enabled; absent shadow
DB is a no-op. Ingestion opens the WAL read-only and requires strictly newer,
complete parity-matched rows, prior qualification, source/config/code/
provenance/replay/gate hashes, family/global BY plus the frozen-dependence-cluster
veto, and durable online FDR before
appending the immutable `lane=shadow` proof and live marker. Semantic or
mid-tail incompleteness fails closed but is repairable: correct the source and
run the bounded complete parity replay before retrying ingestion. There is no
unsafe auto-skip: unresolved quarantine blocks the watermark and FDR boundary.
The `shadow-confirmation-v6` scope splits each tail into older
chronological selection sessions and a newer disjoint confirmatory window; BY
uses the selection p-values, and only the selected candidate's raw confirmatory
p-value reaches LORD++. Same-tail v3 scopes remain audit-only and cannot
authorize. Legacy v2/v3/v4 sequences (`lord_balanced_v2` and
`lord_balanced_raw_p_v3`) and v5 LORD++ rows remain audit-readable and isolated
from v6. Under epoch 6, the persisted live proof must match the durable FDR
allocation (scope/test id, method/version, p-value, alpha, allocation, and
decision), not merely repeat those fields in a caller-supplied envelope.

## Autonomous strategy factory

`research.strategy_factory` owns safe hypothesis generation. Its proposal
language is the finite grammar in `agent.contracts.rule`: twelve signal
primitives — opening-range breakout/fade, momentum continuation, mean
reversion, trend pullback, volatility breakout, volume breakout, VWAP
reversion, VWAP trend, range expansion, opening drive, and the shares-only
`cross_sectional_residual` family against SPY with synchronized one-minute
context — with bounded confirmations and exit parameters. It never generates
or imports source code.

Session-anchored families re-derive the current session from the bars' own New
York dates, so a longer history can never contaminate a session statistic.
`cross_sectional_residual` is a separate shares-only synchronized-context path.
The identifier is retained for compatibility, while the implemented thesis is
SPY-relative directional momentum rather than a beta-neutral residual or paired
hedge. SPY self-reference and symbols outside the bounded comparable-equity ETF
eligibility set fail closed and remain visible in per-symbol diagnostics.
Research replays one session at a time and the runtime fetches from the session
open, so the two see the same window either way.

The grammar has four versions. `rule-strategy.v1` is the original field set and
is unchanged, so every candidate already in a ledger keeps its exact
`variant_id`. `rule-strategy.v2` is a strict superset reached only by naming it
explicitly, and adds four *entry-side* predicates: `confirmations` (a list of
additional trend/volume/volatility filters, all of which must hold),
`entry_after_minutes`/`entry_before_minutes` (the minutes-from-09:30-New-York
window a signal may fire in), and `min_atr_bps`/`max_atr_bps` (the volatility
regime the rule may trade). `rule-strategy.v3` keeps those entry predicates and
adds nullable numeric `breakeven_r` for equity shares; `null` preserves the
fixed-stop behavior. `rule-strategy.v4` adds bounded equity-only exits: a
frozen session VWAP or rolling-mean target, a monotone trailing stop, and an
`exit-before` deadline. Options remain on executable v1/v2 schemas. Together
the extensions let a hypothesis express a *conditional* edge or bounded
equity exit behavior without changing the signal evaluator. V2 entry
predicates stay outside sizing/execution; v3/v4 affect only the shared bounded
exit-state path and cannot author arbitrary orders. A v2 spec that admits a
signal produces exactly the v1 plan, while v3/v4 specs add only their declared
equity exit state.

### Slots are capacity, not licences

The factory runs a fixed number of logical slots. A slot's hypothesis leaves
the active set permanently once it proves an edge — the deployed variant is
frozen and must never be re-tuned — so the slot is immediately reseeded with a
*new* hypothesis in the same cycle. Without that reseed the factory would lose
one worker per success and eventually have nothing left to search. Reseeding
prefers a family the slot has not tried, at that family's own template, and
then continues into the conditional grammar ladder (v2 entry predicates and
v3/v4 equity exits): a slot that has run out of
families has not run out of hypotheses. Each reseed grants one further
`max_generations` mutation budget and never consumes the separate
failure-recovery rotation budget.

`run_factory` gives every configured slot an active hypothesis at the start of
every cycle, which is one code path for three situations: genesis on a fresh
ledger, a slot added by raising `--strategies`, and a slot that lost its
hypothesis (a ledger written before reseeding existed). The cycle result
reports `seeded`, `revived`, `reseeds`, and `active_slots` separately —
`revived` is the one that says something had gone wrong — so idle capacity is
visible rather than silent.

Research defaults to the deployed vehicle (`equity`). `research.py vehicles`
reports the selected set, and `ALPACA_RESEARCH_VEHICLES` may explicitly select
`equity`, `option`, `all`, or a comma-separated subset; the shipped Compose and
systemd paths set it to `equity`. The trader still runs one runtime execution
profile, `shares`, so proving an option edge is useful
research evidence but never switches the trader to options or authorizes an
options order. Select the `options` execution profile separately, on paper,
only after its OPRA evidence and controls have been reviewed.

On a fresh corpus, each worker diagnoses its baseline only from the
chronological fit partition, creates bounded variants based on the observed
failure mode, and evaluates those variants on untouched held-out sessions.
Every variant has a separate simulated cash/equity account. Coordinate
refinement changes exactly one executable field at a time. After all such
points are measured, at most two measured fields are combined in a bounded
interaction phase, followed by one unchanged confirmation. A family is retired
only when every point explicitly rejects a useful edge with adequate powered
evidence; if LLM replacement is enabled, a valid bounded proposal is registered first. For
v2/v3 roots with measured `min_atr_bps` and `stop_atr` coordinate lessons, an
`execution_blocked` fit diagnosis makes coordinate exhaustion schedule exactly
one bounded measured interaction. It uses configured stress geometry when
available, never invents missing values, changes no risk constants, and does not
claim the pair will trade. A
missing or invalid LLM proposal leaves the family pending replacement, not
retired. Insufficient data is not treated as failure. Backtest winners must
still pass strictly later forward data before runtime can select them.

Before replay, the fit-only signal screen supplies the report's forward-return
and control rows. Unavailable comparisons are represented by explicit `p=1`
placeholders, and a terminal current-hypothesis no-edge outcome is reported
instead of silently treating a corpus as proof; a changed corpus reseeds the
hypothesis. `research.path_telemetry` measures target/hold reachability from
the actual entry through the bounded hold, while lower-target and hold proposals
are limited to preregistered ladders and remain non-authorizing.

Before full variant replay, `research.fit_diagnostics` records fit-only
eligible-prefix and first-signal rates, ATR and 30-bps-floor binding, planned
stop/target/hold distributions, gross/net/fees/slippage and configured/stressed
cost-to-risk, configured pre-cap versus fill-delivered risk, exit reason/tie/gap
counts, and clustered MDE/power, provider/feed provenance for each leg,
entry-pricing sources, configured limits,
pass/fail/unknown row counts, behavioral alias fingerprints, and an execution-
rejection section that passes only aggregate fit-partition counts/reasons to
proposal generation. The exit grammar stays fixed
(ATR-floor bracket, configured R target, and bar cap); these measurements are
operator-review diagnostics only and never authorize or expand a candidate.
When every fit opportunity is explicitly rejected at execution, the fit is
classified as `execution_blocked`, distinct from sparse/underpowered data. It
may close after the bounded attempt cap only to progress and observe search
exhaustion; it is not a powered negative edge conclusion.

Each factory result also persists one `bar-coverage.v1` record for the input
corpus, with per-symbol/session observed minutes, exact-calendar expected
minutes when those boundaries are present, early-close caveats, internal gaps,
and bounded gap samples. It is not repeated in every account or variant. A
time-like trade exit records `internal_gap` or `observed_data_end` separately
from normal `time_expiry`. These fields describe censoring only: neither a gap
nor a coverage ratio is assigned a positive or negative R effect without a
separate matched analysis.

A worker is given a corpus descriptor — the session window it needs — rather
than a copy of the corpus, and re-reads that window itself. The predicates are
the orchestrator's own, so the trades, statistics and content hashes are the
same either way; an in-memory corpus has nothing to re-read and still travels
with the task.

### What the LLM does, and what it cannot do

`research.llm_strategy` serves three distinct requests, all bounded by the same
output contract:

- `llm-rule-proposal.v1` — **repair.** Asked for a replacement `rule_spec` only
  after every intended variant of a family has failed with an adequate sample.
- `llm-edge-discovery.v1` — **discovery.** Asked for a genuinely new hypothesis
  whenever a slot needs one: at genesis on a fresh ledger, when a slot proves
  an edge and is reseeded, when a generation budget runs out, and when an idle
  slot is revived. The request carries a small aggregate brief — the families
  this slot has tried, the families already carrying a deployed edge, the last
  diagnosis, the slots already seeded earlier *in this same cycle*, and the
  graded history of earlier reasons — and the reply must include a
  one-sentence `thesis` of at most 240 characters, which is recorded as
  evidence and displayed but never interpreted as an instruction.
- `llm-variant-tuning.v1` — **tuning.** Asked for the parameter variants of one
  hypothesis, given its root spec, the fit-partition diagnosis, and the graded
  lessons. Each returned variant must carry a `reason` of at most 240
  characters naming the parameter it changed and the diagnosed problem it
  should fix, and a `builds_on` citing the lesson it reasoned from.

### The model tunes values; it cannot invent a signal

The signal primitives are fixed code. `RULE_FAMILIES` names twelve of them,
`CONFIRMATIONS` and `SIDES` are closed enums, the permitted field set is fixed
per grammar version, and every numeric field has hard bounds — so
`validate_rule_spec` rejects an unknown family, an unknown filter, an invented
indicator field, or a new data source before anything reaches an evaluator.
How each signal is computed from the bars lives in
`agent/contracts/rule.py::evaluate_rule_signal`, which is code the research
process never generates, imports, or rewrites. That has always been true of
every lane; tuning does not weaken it.

Tuning is narrower still. A tuned spec must keep its root's `family` *and* its
root's `schema`:

- **family** — changing which idea is under test is discovery's job, not
  tuning's.
- **schema** — `rule-strategy.v2` unlocks whole categories of predicate
  (`confirmations`, an entry-time window, an ATR regime band) that the root was
  never expressed in, while v3 adds the nullable equity `breakeven_r` exit
  field and v4 adds bounded target, trailing-stop, and deadline fields.
  Reaching a wider schema is adding structure, not tuning values, so every
  root remains on its declared schema while its existing fields are tuned
  (including when `breakeven_r` changes from `null` to a finite value).
  Options remain on v1/v2.

What is left is exactly what "tuning the parameters" means: the values of the
fields the root already has, inside the bounds the grammar already enforces.

### Why something was tried, and how that turned out

Parameter search used to be a fixed table: three hand-written responses per
diagnosed failure mode, with an arithmetic sweep filling anything past that.
It worked, but nothing it learned in one cycle reached the next one.

All three replies use strict full-schema structured JSON (`additionalProperties:
false`, including the complete rule specification), are size-capped,
fence-rejecting, and validated against the rule grammar before anything is
stored; keys that look like source, credentials, or market rows are refused
outright. The adapter enforces a per-run total-call budget, bounded attempts,
timeouts, and response bytes, and opens an authentication circuit after an auth
failure. Result evidence records schema/grammar hashes, call counts, circuit
state, and each attempt's hashes/errors. A discovered hypothesis is
registered `queued` with no run, no gate, and no candidate — it must earn
`backtest_passed` and then a strictly later offline forward-shadow pass through
exactly the same gates as a deterministic one. That pass may leave the
candidate at `shadow`; only live parity ingestion can authorize runtime. A
tuned variant gets its own isolated
simulated account and faces every gate a mutated variant faces. **The LLM
chooses what to try next; it can never shorten the evidence path or authorize
trading.** The shipped lane requires its separate readable provider secret;
missing or unreadable credentials fail closed before discovery rather than
downgrading the run to an unauthenticated or silently different mode.

Every proposal records a **reason** before the gate that will judge it exists,
and that reason is **graded** against fit-derived evidence afterwards. The pair
lives in two append-only tables — `factory_lessons` for the reason,
`factory_lesson_outcomes` for the verdict — because the two facts are known at
different times, and writing the reason first is what makes it a prediction
rather than a summary. Proposal verdicts and shared learning are fit-derived;
underpowered attempts, including `execution_blocked`, do not count as family or
parameter successes or failures. Held-out, sealed, and qualification evidence
remains audit metadata and is unavailable to proposal generation. Deterministic
mutations record reasons in the same shape, including an explicit "no diagnosis
behind it" marker on the sweep fill, so a tuned reason can be compared against
the fixed table rather than only against other tuned reasons.

The graded pairs are read back into the next tuning and discovery request,
oldest-first-trimmed to stay inside the prompt's aggregate bound.

### A proposal has to build on a learning, not arrive from nowhere

Handing the model its history is necessary but not sufficient — nothing in a
prompt makes a model use what it was given. Three rules make the loop a
property of the system instead:

1. **Cite the lesson.** Each brief entry carries a short id. When any lesson is
   supplied, every returned variant must set `builds_on` to one of those ids,
   and its `reason` must say what that lesson showed. An uncited proposal is
   refused; so is one citing an id that was never supplied, which would be a
   fabricated citation.
2. **Do not re-run a settled experiment.** A variant whose parameters a graded
   lesson already recorded as a powered negative rejection is dropped, and the
   deterministic table tops the slot back up. *Underpowered* is not a failure,
   so a thin sample never closes a door.
3. **Keep the chain.** The citation is resolved to a real lesson id and stored
   as `parent_lesson_id`, so "B was tried because A failed this way" is a
   durable edge in the ledger. `factory report` renders it as `built on:` and
   counts how many proposals stood on an earlier result.

The model may add at most eight novel numeric tuning values per lineage/family.
The factory ledger records marked novel attempts across parent hypotheses and
cycles, so the allowance survives prompt trimming and restarts; deterministic
grid values do not consume it.

That is the whole loop: propose with a reason and a citation, evaluate under
unchanged gates, grade the reason, and hand the grade forward.

Across all discovery lanes, executable variants are de-duplicated by a
family-specific semantic signature, including v1/v2/v3 no-op aliases. Continuous
numeric axes use relative/local scaling for semantic novelty, while
integer/topology axes use their grammar spans; exact signatures and validation
are unchanged. Only a
graded `adequate_negative_rejection` suppresses its exact variant id;
underpowered and adequate-but-inconclusive results remain eligible.

### Learning shared across every strategy

A per-family brief can only say what happened to one idea. Some of what
research learns is not about one idea at all — that lowering a target tends to
help across families, that live trials keep disagreeing with replays, that one
direction of change fails everywhere it is tried. `shared_learning` aggregates
exactly that and attaches it to every tuning and discovery request, so a fresh
slot with no history of its own still has evidence to reason from.

It is an aggregate of *outcomes*, never of market data: each entry is a
parameter name, a direction, how many graded attempts changed it that way, and
how many passed. Underpowered attempts are excluded — they say something about
the sample, not the parameter — and a direction needs at least three graded
attempts before it is reported, because a "trend" drawn from one is worse than
no trend. Nothing in the digest is derived from a held-out, sealed, or
later-forward observation, so sharing it adds no information about unseen data.

## The isolated paper-research epoch

`research.paper_epoch` is a separate operational experiment from both the
authorizing EdgeLedger and the existing paper-account trial below. It freezes
exactly one primary connected to a separately fingerprinted Alpaca paper
account/runtime and at least one broker-free shadow sibling. Every member must
attest the same realtime-stream, data-window, config, code, cost, risk, and
manifest digests before the epoch starts. Runtime LLM adaptation is required to
be false both in the manifest and at start.

The epoch writes only its dedicated SQLite store (default
`runtime/research/paper_epochs.sqlite3`). A complete observation batch contains
the primary and every sibling for the same stream event. It records paper
fills, slippage, rejections, and paper/shadow operational parity. A mismatch or
runtime failure may stop the epoch; a favorable paper outcome contributes zero
alpha evidence and has no promotion authority. The database is append-only,
uses immutable triggers, row digests, and an audit hash chain, and refuses to
share a namespace with an EdgeLedger or runtime journal.

Lifecycle operations use bounded JSON documents and never accept or persist
broker secrets:

```bash
python research.py paper-epoch create --db runtime/research/paper_epochs.sqlite3 --input create.json
python research.py paper-epoch start --db runtime/research/paper_epochs.sqlite3 --epoch EPOCH --input attestation.json
python research.py paper-epoch record --db runtime/research/paper_epochs.sqlite3 --epoch EPOCH --input paired-outcome.json
python research.py paper-epoch complete --db runtime/research/paper_epochs.sqlite3 --epoch EPOCH
python research.py paper-epoch seal --db runtime/research/paper_epochs.sqlite3 --epoch EPOCH --input lessons.json
python research.py paper-epoch verify --db runtime/research/paper_epochs.sqlite3
```

An immutable, public-safe operational snapshot can be emitted without opening
the store for writes:

```bash
python research.py paper-epoch export \
  --db runtime/research/paper_epochs.sqlite3 --epoch EPOCH \
  --output-root research/results/paper-epochs
```

The writer verifies the audit chain, then produces deterministic canonical
JSONL with an epoch manifest/cohort digest, current status and policy,
operational summary, one record for each outcome currently stored, and the
integrity/audit head. The filename contains the full content SHA-256 digest;
creation is atomic and repeats are byte-identity checked. Account/runtime
fingerprints and the local SQLite path are intentionally omitted. This is an
operational export, not a complete observer or signal feed: it does not fill
in session dates, signal times, symbols, sides, brackets, quote provenance,
exits, or R values. It is descriptive instrumentation with zero alpha or
promotion authority. An optional HTTPS metadata notification is available via
`--webhook-url`; delivery is best effort and never changes the artifact.

Lessons are invisible to their source epoch. A successor may consume only the
immediately preceding sealed batch, and only after attesting a clean runtime
restart and a different unseen data-window digest. It therefore restarts
confirmation rather than tuning the running cohort. Broker order wiring must be
hosted in its own paper-only process with its own credentials; the control
plane deliberately cannot fall back to the trader's account or credentials.

## The paper-account trial lane

`research.trial` closes the loop between the book and the search.

A proved edge that is not pinned trades the same Alpaca paper account, and its real fills
accumulate as `paper_outcomes`, each carrying the exact passing shadow
`proof_run_id` that authorized entry. After the trial window — the standalone
module fallback is 30 sessions/100 trades, while the shipped validated runtime
configuration supplies 20 sessions/20 trades — that proof epoch is judged
against an explicit floor (total R and mean R both positive by default):

- **Clears it** → the edge keeps trading and becomes *promotable*. Nothing is
  promoted; `edge promotable` hands the operator the config block.
- **Misses it** → the edge is parked, and the reason is written into the lesson
  ledger as a `trial` lesson from `live_paper`, graded immediately. The next
  tuning request reads it, so the parameters proposed next are answering the
  book rather than only the replay. This is the point of the lane.
- **Window still open, or outcomes carry no usable R** → nothing happens.
  Underpowered is not failure here either.

A pinned edge is still judged. The sequential drift stop and trial review run
for pinned identities just as they do for automatic selections; an authoritative
failure parks or demotes the edge and records the pin context for audit. The
20-outcome rolling-R calculation remains visible as advisory telemetry but does
not itself change lifecycle state. Pinning selects an identity and prevents
silent substitution, but never bypasses authorization or a hard lifecycle stop.

```bash
python research.py edge trials --dry-run   # verdicts, changing nothing
python research.py edge trials             # park the failures (exit 3)
python research.py edge promotable         # what has earned a promotion
```

The scheduled cycle runs the review *before* discovery and tuning, so a trial
that just finished below its floor is already a recorded lesson by the time
this cycle proposes anything. `ALPACA_TRIAL_REVIEW_ENABLED=0` turns it off.

If the same immutable candidate is later re-proved, the newer passing shadow
run starts a fresh trial epoch. Earlier outcomes remain in the append-only
ledger but are excluded from `paper_performance`, drift, and the new verdict.
A failed or unverifiable latest shadow proof quarantines history rather than
falling back to lifetime aggregation.

Parking an edge never promotes anything in its place. The replacement still has
to earn `backtest_passed`, a strictly later offline shadow pass, and every gate,
then obtain a new parity-matched live proof before runtime eligibility.

```bash
python research.py factory report --format markdown   # includes the graded reasons
```

The unmutated root is always variant zero and is never proposed away. It is
compared with an independent randomized-entry null that preserves the
candidate's session/symbol/direction distribution and exit rules; it is never
compared with itself. The root remains a real candidate in the family and
cycle-global Benjamini–Yekutieli denominators, so it consumes multiplicity like
any mutation.

Verified gate envelopes carry explainable arm evidence for candidate, baseline,
and randomized-null rows: raw/executed/eligible counts, fill sources, quote-age
summaries, gross/cost/net economics, matched and dropped match keys, and
directional/pair coverage. Quote density can legitimately change null/control
evidence even when the candidate count is unchanged.

The checked config enables model-assisted discovery, replacement, and tuning
with OpenAI `gpt-5.6-terra`. Azure-compatible endpoints also require the exact
resource-local alias in `research.strategy_llm.deployment`; preflight rejects
an unset alias before loading the corpus. Compose uses the host override
`ALPACA_RESEARCH_LLM_SECRET_FILE`; the scheduler reads the mounted path through
`ALPACA_RESEARCH_LLM_SECRETS_FILE`. Provider keys are read only from that
separate readable file, never from the broker secret. An enabled adapter
without its provider key fails before discovery; invalid or rejected model
output records a pending replacement and cannot retire a family prematurely.
Successful proof produces a content-addressed finding. `research.proof.webhook_url` may send that finding
to an HTTPS webhook without changing the durable artifact.

`OPENAI_BASE_URL` and `ANTHROPIC_BASE_URL` are trust boundaries: a configured
endpoint receives the provider key and the bounded aggregate prompt. Use only a
trusted HTTPS service; there is no application host allowlist. Prompt, request,
schema, configuration (including the effective sampling setting), and
raw-response hashes prove what the cycle consumed, and make the evidence
reproducible for that exact invocation, but they do not guarantee bit-for-bit
identical model output later. The OpenAI path uses the Responses API
structured-output request and sends `temperature: 0` when supported; when the
configured model or deployment is exactly `gpt-5.6-terra`, the adapter omits
that unsupported parameter and records `temperature: null` in configuration
evidence. Anthropic requests keep `temperature` at `0`; the hashes preserve the
actual request and response. The adapter's call budget is a per-run call-count
guard rather than spend accounting; provider-side quotas remain an operational
control.

The scheduled cycle reports `completed`, `completed_no_edge`, `no_data`, or
`failed`. `completed_no_edge` means the input was valid but no candidate was
proved; the report separates `adequate_negative_rejection`,
`adequate_negative_inconclusive`, `adequate_inconclusive`, `underpowered`, and
families not yet tested. `no_data` means the input was unavailable or empty. Neither status
permits bypassing the runtime edge gate.

Before validation, the scheduled cycle builds temporary normalized views and
emits `research-cycle-quarantine.v1`. Recorder rows from the legacy observation
timestamp bug (`as_of > observed_at`) are excluded from those views without
modifying the append-only source. Rows proven outside an authoritative Alpaca
session, including an authoritative closed day, are likewise excluded and
reported as `research-cycle-calendar-filter.v1`; missing, malformed, or
conflicting exact-calendar metadata remains a hard validation failure.

```bash
python research.py factory run --data market.jsonl --strategies 12 --variants 4 --workers 2
python research.py factory status
python research.py factory report [--slot N] [--format text|markdown|json] [--write]
```

The standalone factory preflight requires readable normalized JSONL with
explicit provider/feed provenance; the equity lane requires exact IEX or SIP.
`delayed_sip` and other partial/non-exact feeds are diagnostic-only, mark the
result diagnostic, and emit no proofs. In this mode
the configured strategy model may still perform bounded discovery and tuning;
its hypotheses, variants, call evidence, and aggregate refusal diagnostics are
written to `ALPACA_FACTORY_DIAGNOSTIC_REPORT`, never to either authorizing
ledger. Scheduled diagnostic cycles skip trial review and shadow ingestion.

For a preregistered reachability check, freeze the exact diagnostic report and
run the same specifications and data under two cost-risk limits:

```bash
python -m research.cost_counterfactual \
  --data market.jsonl --specs diagnostic-report.json \
  --agent-config config.yaml --baseline 0.30 --alternative 0.60 \
  --output cost-counterfactual.json
```

Only that one risk-policy field changes. The output measures refusal rate,
executed trades, and ordinary diagnostic replay P&L, is explicitly
non-authorizing, and cannot select or update the production threshold from its
own result. It does not claim a separate stressed-expectancy estimator.

The v2 contract verifies that the only changed config path is exactly
`risk.max_stressed_cost_to_risk_ratio` and persists baseline, alternative, and
runtime config hashes. Pairing is exact on `variant_id + opportunity_id`;
malformed terminal/numeric/identity rows and duplicate keys are excluded from
pairing, and identity/duplicate exclusions also apply to the empirical
summaries. Incomplete, ambiguous, or path-dependent pairs fail the top-level
controlled-change invariant and cannot support a direct causal interpretation.
Each arm reports empirical `r_multiple` (R) distributions,
including mean and sample sigma, trades per session, target definition/hit
counts, gross/fee/net economics, modeled execution-drag decomposition when the
references permit it, stop geometry, exit reasons, stressed-cost-to-risk values,
entry slippage, and fill sources. Compact signal-opportunity evidence is sorted,
JSON-safe, content-addressed per row, and bound by a collection hash. This makes
the replay reproducible without making its stateful account path a causal or
authorizing result.
The diagnostic Section 0.5 measurement includes a 95% moving-block
session-cluster bootstrap interval and clustered MDE/power at alpha .05 for a
`.05R` effect; uncertainty is data-derived and no fixed `0.38R` width is
assumed.

Provenance records the source-report, measurement-code, dataset, frozen-cohort,
run-settings, and final result content digests (`source_report_hash`,
`measurement_code_hash`, `dataset_hash`, `frozen_cohort_hash`,
`run_settings_hash`, and `content_hash`). A bound source report must also carry
its dataset hash, strict diagnostic/non-authorizing flags, and an explicit
empty proof list; omissions fail the controlled-change invariant. The evidence
funnel lists five
protocol windows—`fit`, `heldout`, `qualification`, `shadow_selection`, and
`shadow_confirmation`—with nominal floors of 100, 100, 100, 150, and 150
trades (30 sessions/clusters each; 600 trades in total); each is marked
`measurement_available: false` until sealed/live window assignments exist. Its
readiness context is 150 offline sessions plus 60 shadow sessions (210 total).
These measurements remain diagnostic-only: P&L is descriptive, and the result
cannot select a threshold or promote an edge.

`factory run` archives the Markdown narrative under `research/results/factory/`
on every cycle, including a cycle that proved nothing, so the read-only
dashboard lists it without anyone running a command. `--write` does the same
on demand; `ALPACA_RESEARCH_REPORT_DIR` overrides the destination.

`research.factory_report` is the reader for everything the two ledgers already
record but nothing previously opened: per slot, the lineage of hypotheses it
has held; per hypothesis, its origin (template, deterministic mutation,
rotation, LLM discovery, LLM replacement) with the model and the prompt,
request and response content hashes where one applies, plus its thesis and its
falsification condition; per variant, trade counts split fit/held-out, net P&L,
drawdown, held-out delta and lower bound, q-value, and the named gates it
missed; and per outcome, why a hypothesis was retired, after how many of its
intended variants, the dominant failure mode, and what replaced it. It opens
both ledgers read-only, derives every number on read, and reports the gate hash
beside anything a gate produced.

```bash
python research.py edge init
python research.py edge discover --data market.jsonl --vehicle equity --lane auto
python research.py edge status --vehicle equity
python research.py edge ingest CANDIDATE_ID paper-outcome.json
python research.py edge paper --vehicle equity --deployed
```

`edge status` reports lifecycle state; `edge paper` reports how each edge is
actually doing on live paper outcomes — trade and session counts, total and
mean R, win rate, net P&L, the advisory rolling-R monitor with its floor, and
the authoritative sequential drift statistic against the held-out distribution the edge was
validated on. The live view is limited to the latest passing shadow proof epoch,
so “what it has done since” means since the proof currently authorizing it, not
the candidate's lifetime. Both matter: the first is the evidence an edge was
promoted with, the second is what it has done since. Neither can change a
lifecycle state; they are read-only views of append-only data.
