# Autonomous research lifecycle

This is the canonical semantic description of the autonomous research system.
Executable code and `config.yaml` remain the behavioral authority. Installation
belongs in [`../SETUP.md`](../SETUP.md), operations in
[`../OPERATIONS.md`](../OPERATIONS.md), strategy identities in
[`HYPOTHESES_AND_VARIANTS.md`](HYPOTHESES_AND_VARIANTS.md), and statistical
rules in [`protocol.md`](protocol.md).

No current strategy is a proven edge or qualified for live capital. The
shipped account mode remains `demo`, but `strategy.execution_mode` is
`shadow_only`: no order, LLM, or deterministic entry path is active. The
configured `ls-ratio-fade/v1` tuned identity is research-only and remains an
unproven hypothesis.

## The authority chain

The lifecycle is deliberately one-way:

`market snapshot → deterministic shadow decision → isolated paper evidence →
paired qualification → bounded LLM authoring/refinement → human-reviewed
registration handoff → optional reviewed demo run → explicit human live
approval`

Those arrows are authority boundaries, not just processing stages:

1. The engine records one timestamped market snapshot. In shipped
   `shadow_only` mode no strategy reaches the demo exchange; research
   evaluators have no exchange object.
2. Deterministic shadow contracts emit long and short paper proposals before
   risk policy is applied. The registered long/short-ratio base contract uses
   80/20 within-instrument percentile tails, a 3 ATR extension cap, a two-ATR
   minimum structure stop, fixed 2R target, observed costs/funding, and a
   48-hour outer hold. The explicit
   `ls_ratio_fade.tuned_70_30_ext_1_5_stop_1_target_3` research variant uses
   70/30 tails, a 1.5 ATR extension cap, a 1 ATR stop, and a 3R target; it is
   unproven, research-only, and not a promotion. It is a pinned isolated paper
   arm with its own account and is never an adaptive one-axis selector
   candidate.
3. Every research arm owns isolated cash, positions, decisions, trades, risk,
   cooldowns, and circuit breakers. Eligible vetoes are explicit zero-return
   decisions; missing or unresolved observations are not silently converted
   to zero.
4. Qualification compares candidate and baseline on the same immutable
   proposal identities. A research label, qualification event, evidence
   package, or handoff never edits the registry, configuration, account mode,
   risk limits, or capital authority.
5. The authoring model reads a bounded projection of persisted successes,
   nulls, near misses, held-out degradation, missing-data outcomes, and retired
   staged mechanisms. Accepted output is only a deterministic entry-condition
   contract. It cannot author exits, stops, targets, sizing, files, network
   calls, or order authority.
6. A supported staged mechanism produces a content-addressed
   `staged_registration_handoff.v1` artifact for explicit human review. The
   handoff declares `live_eligible: false` and records that neither code nor the
   registry was mutated. A human must decide whether to implement and register
   it before it can enter the registered comparison and demo-review workflow.
7. A registered candidate still needs current qualification, clean local
   PAPER evidence, a content-addressed reviewed evidence packet, and the
   account-bound candidate-demo preflight. Demo success does not authorize
   live capital. Live requires a separate human-reviewed registry/configuration
   change, a qualifying tier and its single reviewed packet citation, then the
   normal live safety checks.

## Decision sources and the LLM boundary

`strategy.execution_mode` has three meanings:

- `deterministic`: the registered forward contract proposes orders; engine
  startup does not construct or preflight an LLM client and no `:llm` research
  lane exists;
- `analyst`: the momentum analyst proposes on the order path and the exact
  analyst decisions may be recorded in a separate `:llm` scope;
- `shadow_only`: there is no order path, while research lanes may continue.

An analyst-created `:llm` scope is planner history only. It is not a
deterministic comparison arm, is never pooled with candidate/baseline evidence,
and cannot qualify a strategy. In particular, deterministic proposals must
never be relabeled as analyst evidence.

The shipped config still names an LLM provider/model because the independent
nightly author and reviewer may use it. That does not make an LLM a runtime
dependency for the shipped `shadow_only` startup. No order or deterministic
entry path is enabled until an operator explicitly changes the mode.

## Current strategy inventory

The executable registry contains seven strategy families. Their canonical
version/status table is in
[`HYPOTHESES_AND_VARIANTS.md`](HYPOTHESES_AND_VARIANTS.md) and is checked
against `agent/registry.py`.

- `momentum`, `flush-fade`, and `funding-carry` are rejected configurations or
  mechanisms; rejection is not evidence for the remaining strategies.
- `funding-unwind`, `trend-multiday`, `ls-ratio-fade`, and `scalp-maker` remain
  hypotheses. `funding-unwind` and `trend-multiday` have long horizons, and
  `funding-carry` is also offline-only despite having an executable forward
  model. The tuned `ls-ratio-fade` identity is configured for shadow research
  by elimination, not promoted to an order path.
- `funding-carry` holds while funding pays and exits when the 30-day funding
  percentile normalizes to 50, or on its stop/ten-day timeout. It has no price
  target. `funding-unwind` is a different directional claim and retains a fixed
  2R price target.

The runtime trading universe is the top 25 qualifying instruments. The
standalone recorder may scan up to 50 instruments, and the nightly historical
download currently caps a fresh snapshot at 26 symbols. These are separate
purposes and must not be described as one universe. Current FindingsStore data
uses schema 17 and current realtime evidence uses `forward_feed_version: 8`;
older feed identities remain historical and are never pooled into the current
population.

## Paired, held-out qualification

The registered forward-axis protocol selects on the chronological first 70%
and confirms on the last 30%, using one common time cutoff for all arms. The
minimum evidence is:

- 100 full candidate/baseline pairs, including at least 70 fit pairs and 30
  held-out confirmation pairs;
- at least 80% candidate/baseline coverage of the proposal union in each
  relevant window, no duplicate proposal identities, and at least 8
  distinct six-hour market episodes for each dependence-aware interval;
- at least 3 settings on the declared axis, identical non-axis executable
  inputs and contemporaneous baselines;
- positive after-cost candidate performance, a positive candidate-minus-
  baseline interval in the full, fit, and confirmation evidence, no worse
  drawdown than baseline, and a held-out result that does not contradict the
  fit result;
- the calibrated one-sided clustered paired sign-flip test on held-out pairs,
  followed by Holm correction across every axis evaluated in that
  qualification family.

The persisted protocol records the sign-exchangeability/symmetric-null
assumption, cluster size, seed or exact-enumeration mode, family membership,
and adjusted result. Missing, malformed, under-covered, duplicate, gappy, or
under-clustered evidence stays collecting/inconclusive. A positive point
estimate never substitutes for a failed evidence gate.

Staged support uses the same matched opportunity principle and held-out
clustered comparison. It additionally requires at least 100 closed trades,
at least 1% firing, 80% resolved and paired opportunity coverage, and
false-discovery control across the screened staged family. A clear negative
can retire early; passing an early screen can never promote.

## Contract and economic fidelity

`StrategyContract` is the canonical composite of registry strategy spec,
forward-model economics/exit, evidence builder, immutable variant identity,
and semantic hash. Startup rejects a named variant whose effective
configuration has drifted. Evidence ingest retains missing or mismatched legacy
rows for audit but quarantines them from inference. Funding is also
fail-closed: only `verified_realized` and `verified_no_settlement_due` outcomes
may enter inference, qualification, or authoring metrics.

Paper evidence is attributable only when the strategy contract, forward
outcome model, executable configuration, code identity, dataset, runtime,
command, and outputs are bound before the outcome is known. Forward models fix
the signal clock, observed executable entry side, stop, exit policy, holding
horizon, required market fields and funding treatment.

The deterministic simulator uses the observed book touch/depth rather than a
mark-price fiction. It records entry and exit fees, observed spread treatment,
depth-derived fill economics, stop slippage, and realized funding settlements.
The R denominator uses the same all-in planned-loss components as runtime risk
sizing. An absent book or required field is a data refusal, not a cheap fill.
For one-minute outcome replay, confirmed `execution_bar_1m` bars must be
strictly ordered and contiguous after the signal/entry boundary. A missing
minute returns `no_data` with zero held duration, costs, funding, and risk; a
direct timeout requires full horizon coverage and cannot be manufactured from
the last partial bar. If a single minute touches stop and target, the stop wins.
The observable normalization path is evaluated from those persisted bars;
long/short direction comes from the evidence-derived episode identity.
Price-cache ranges use an end-exclusive `end_ms`; a bar exactly at the end
boundary belongs to the next request, not this window.

Evidence packages use `research-evidence-package.v1`. The manifest
content-addresses the exact dataset and coverage, provenance class, code tree,
strategy and forward contracts, config identity, fees/slippage/funding,
prompt applicability and provider/model when relevant, runtime, argv, outputs,
timestamps, and parent evidence IDs. Blob, code, config, coverage, or manifest
drift fails verification. `parent_evidence_ids` make derivations recursively
traceable by content ID; verification recursively verifies every referenced
parent package, which must remain a separate package that is retained and
verified rather than trusting a label. Synthetic fixtures cannot be relabeled as
real-market evidence.

Useful verification commands:

```bash
./.venv/bin/python -m research.evidence_cli verify-package \
  <evidence-package> --code-root . --config config.yaml
./.venv/bin/python -m research.evidence_cli verify-golden \
  tests/fixtures/evidence/golden_replay_synthetic.json \
  tests/fixtures/evidence/golden_replay_expected.json
./.venv/bin/python -m research.evidence_cli run-replay \
  tests/fixtures/evidence/golden_replay_synthetic.json
./.venv/bin/python -m research.evidence_cli import-replay \
  <source> <new-fixture> --classification synthetic
```

The checked-in golden replay is explicitly synthetic. It proves deterministic
replay behavior, not market profitability.

## Recorded event plane and bounded discovery

The recorder adds local receipt (`observed_at`/`available_at`), source, feed,
schema, payload revision, and quality metadata without inferring missing legacy
timestamps. Confirmed `execution_bar_1m` rows are joined into episodes only
after the signal feature cutoff, using a later bounded outcome cutoff for bars
and funding. Direction is derived from the persisted episode evidence rather
than a scalar outcome. `research.py ingest-recorded` builds
`runtime/research/market_events.db` as the separate `event-plane.v1` view,
archives content-addressed raw CSV snapshots, applies strict event-time and
availability-time as-of filtering, and quarantines malformed, contradictory,
or legacy rows. The nightly path ingests before discovery. Missing recorder
input is the nonfatal `NO_DATA` state, and recorder health does not gate trader
or research startup. `forward_feed_version` remains 8; it is not the event
plane schema version.

`research.py discover` evaluates a bounded typed IR and generated,
AST-verified evaluator. The initial family uses persisted open-interest,
taker-volume, and book fields for a forced-flow-pressure observable, binds a
causal claim/falsifier and fixed mechanism-aligned `ExitProfile`, runs verified
funding/cost/episode counterfactuals, fits only a small empirical world model,
and records exact source-event digests in content-addressed artifacts. Candidate
progression and Findings analysis are append-only and non-authorizing.
`IDLE`, `NO_DATA`, and `NO_STATE_DATA` cannot change registry, configuration,
tier, or order state. A `COMPLETE` result remains research-only: scalar or
mixed scalar/non-episode rows cannot complete a counterfactual, and no
discovery result grants registry/config/demo/live authority.

The protocol and shortlist share the policy-neutral primitives in
`research/evidence_primitives.py` for canonical opportunity identity,
duplicate-safe indexing, chronological splitting, and pair/union coverage.
Their lane policies remain distinct, but neither may silently change the
opportunity identity or resurrect a duplicate.

## Bounded authoring and refinement

Authoring is backpressured by one shared staging capacity. The shipped limit is
32 active configurations. A command requests four proposals by default and
the executable cap is 8. Each novel root initially receives at most two
one-parameter neighbors; it is never expanded as a Cartesian grid.

Every authoring attempt is append-only in the staging SQLite database,
including provider, model, prompt version, full request, bounded context and
evidence hashes, raw response, parse/validation status, failures, returned and
accepted contract IDs, rejection reasons, and result. Provider failures and
malformed or non-novel output therefore remain evidence rather than
disappearing.

The shipped refinement policy allows 1 later attempt, at most 2 variants per
attempt, and at most 5 configurations across a mechanism family.
Refinement is permitted only after a persisted coded retirement such as
negative expectancy, held-out failure, or never firing. It changes one scalar
threshold toward a bounded neighbor, records parent configuration and attempt,
and is idempotent across scheduler retries. Inactive/starved lanes expire to
release capacity without claiming that the market mechanism was falsified.
Initial staged family registration publishes the root and all bounded
configurations atomically; a failed child validation leaves no partial family.

Prepare a supported staged handoff with:

```bash
./.venv/bin/python research.py prepare-handoff \
  --store research/cache/findings.db \
  --staging research/cache/staging.db
```

Artifacts default to `research/results/staged-handoffs/sha256-<digest>.json`.
Rerunning unchanged evidence returns the existing artifact. Preparation reads
a consistent findings snapshot and does not mutate findings, source, registry,
or configuration.

For recorded discovery, `research.py prepare-discovery-handoff` is a separate
human checkpoint. It verifies the persisted `COMPLETE` result, typed artifact,
contract identity, event evidence, and content hashes, then writes an
idempotent packet under `research/results/discovery-handoffs/`. The packet is
content-addressed and carries `HUMAN_DECISION_REQUIRED`; registry, config,
code, demo, and live authority/mutation fields are all false. Missing or
ineligible discovery evidence is a nonfatal nightly state.

## Scheduler and health truth

The Compose scheduler runs one UTC-daily job (03:00 by default), immediately
runs a missed job once per UTC date, and binds mode and data paths to validated
configuration. It writes an atomic status heartbeat every 30 seconds while a
child is running, applies a four-hour default wall-clock deadline, terminates
the whole child process group on timeout, and retains bounded 32,000-character
stdout/stderr tails plus truncation counts.

Authoring/review JSON failures are recognized while streaming. A masked
provider failure, retry-pending review, or partial review makes the effective
job status nonzero even if a wrapper returned zero. Status becomes `waiting`,
`running`, `completed`, `failed`, `timed_out`, or `stopped`; job identity,
deadline, exit code and structured failures persist in
`runtime/health/research.json`, with bounded redacted append-only history in
`runtime/health/research.history.jsonl`. Trader heartbeat writes the same
latest-value/history pair through `heartbeat.json` and
`heartbeat.history.jsonl`.

The research health probe is green only for a fresh `waiting`, `running`, or
`completed` heartbeat with no nonzero last exit and no run past its deadline.
Trader health also fails when research is expected but unavailable or when any
per-strategy shadow evaluator reports an error. Research failure is visible but
does not restart or stop the separate trader service.

An automatic nightly run with no eligible discovery result (including no
persisted COMPLETE result to hand off) is nonfatal and remains an explicit
research status. Parser, artifact-integrity, or content-address failures are
different: they fail closed and make the child nonzero.

Verified backups include findings/journal/event-plane SQLite snapshots, raw
recorder data and archives, completed immutable market snapshots, runtime
identity, heartbeat/status/failed-alert histories, discovery artifacts, and
research results. Verification checks payload hashes, JSON/JSONL parsing,
SQLite integrity/foreign keys, and secret exclusions.

## Human checkpoints

`research.py prepare-handoff` is the handoff from supported staged evidence to
a human implementation/registration decision. `research.py
prepare-review-artifacts` prepares non-authorizing drafts for already
registered qualified evidence. `research.py t3-packet` binds reviewer and
registry-change references into a content-addressed review packet, but it
still places no order and changes no tier.

A reviewed candidate may be exercised on the explicitly account-bound OKX
demo path only through `main.py run --candidate-demo` with its qualified
variant, scope, reviewed packet reference, and expected demo account
fingerprint. Moving from that rehearsal to live remains an explicit human
approval and configuration deployment. Nothing in the scheduler, authoring
loop, handoff generator, evidence package, or demo run performs that move.

The maker-first passive-entry primitive is a demo-only measurement boundary,
distinct from the `scalp-maker` shadow strategy. Its fill/cancel/timeout
evidence must be collected and reviewed before any live-mode consideration;
configuration rejects maker-first in live mode.
