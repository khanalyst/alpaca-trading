# Orchestrated strategy audit — 2026-07-29

> **ARCHIVED / FROZEN REVIEW SNAPSHOT.** This audit records the repository as
> reviewed on July 29, 2026. Its finding IDs, schema/feed descriptions, and
> remediation states are historical evidence, not current instructions. Use
> `README.md`, `OPERATIONS.md`, and `research/AUTONOMOUS_RESEARCH.md` for
> current behavior.

This is the missing consolidated record of the repository review performed on
the isolated branch `codex/orchestrated-strategy-audit` in
`/private/tmp/okx-agent-crypto-orchestrated`. The original checkout was not
modified.

The review used four specialist passes, run one at a time:

1. hypothesis testing, replay, and parameter-learning integrity;
2. reporting, persistence, and auditability;
3. end-to-end strategy coherence and safety;
4. synthesis of the README against the behavior of the code.

The first implementation commit produced from those reviews is `7a78790`
(`Harden strategy research and reporting`). On 2026-07-30 the remaining eight
items were implemented on the same isolated branch, then subjected to separate
validation and strategy review. This document records the final disposition;
an item marked **Open** is still not claimed as solved.

## Executive result

The branch materially strengthens research validity and reporting durability:
variant evidence is recomputed under each parameter set, outcome windows no
longer begin before the decision, winner selection is separated from
confirmation data, G2 is a durable enforced prerequisite, research evaluations
are persisted atomically, reports preserve compatibility dimensions, and
parameter-shadow events are separated from cross-strategy-shadow events.

The follow-up adds independent persisted variant accounts, durable fair
scheduling, paired/dependence-aware inference, richer replay state feedback,
forward-only findings migrations, transfer-ID deduplication with explicit
reconciliation when an ID is absent, strategy-specific outcome contracts, and
content-addressed review-gated T3 evidence packets. The then-current schema persisted
every accepted or vetoed proposal atomically, treats vetoes as explicit
zero-return paired actions, freezes qualification at a common pre-paper
evidence boundary, and invalidates windows containing operational failures.
Schema migration 7 introduced the complete decision ledger and legacy
watermark.
Exchange/SQLite commits still cannot be one distributed transaction, and replay
still cannot reproduce exchange-only state such as fill races or
liquidation/margin details.

## 1. Hypothesis testing and edge-refinement findings

| ID | Original priority | Finding | Disposition | Evidence / result |
| --- | --- | --- | --- | --- |
| HYP-01 | P0 | Replay and shadow variants reused cached `setup_evidence` created under the live configuration. Parameter variants therefore did not necessarily test their own hypothesis. | **Fixed** | `research/replay.py` and `agent/shadow.py` recompute evidence using each variant's effective configuration. Regression coverage was added in `tests/research/test_replay_determinism.py` and `tests/research/test_shadow_isolation.py`. |
| HYP-02 | P0 | Outcome construction could include bars before the actual decision, creating look-ahead contamination. | **Fixed** | `research/outcomes.py` now carries `entry_ts` and excludes pre-decision/pre-entry bars. Covered by `tests/research/test_no_lookahead.py`. |
| HYP-03 | P0 | G2 replay fidelity was described as a prerequisite but `three-arm`, `funnel`, and `sweep` could run without enforcing it. | **Fixed** | The three research paths now enforce a current G2 PASS. Every check journals a durable `research_gate_result`. |
| HYP-04 | P0 | Out-of-sample confirmation data influenced winner selection, weakening the independence of the claimed confirmation. | **Fixed** | `research/protocol.py` selects on the 70% fit partition and confirms the selected candidate only on the held-out 30%. Confirmation must have a positive delta. |
| HYP-05 | P1 | Shadow variants evaluate against portfolio state affected by live decisions, rather than a fully independent simulated account for every variant. | **Fixed** | Every variant now persists its own cash, equity, positions, exposure, cooldowns, circuit breakers, proposal history and trade history in `findings.db`; the live portfolio input is ignored. |
| HYP-06 | P1 | Fixed evaluation order plus a wall-clock budget can systematically under-sample later variants. | **Fixed** | A durable least-observed-first scheduler records evaluations/skips and moves under-observed variants to the front across cycles and restarts. |
| HYP-07 | P1 | Axis rejection could be based on an adequate aggregate sample while individual settings remained under-sampled. | **Fixed** | Rejection now requires at least 100 observations for each evaluated setting. |
| HYP-08 | P1 | Replay does not fully model equity feedback, loss cooldowns, universe-selection changes, and every circuit breaker. | **Substantially fixed; exchange-only gap remains** | Replay now feeds resolved PnL into equity, closes positions, starts loss cooldowns, tracks universe changes and applies daily-loss/max-drawdown transitions. Diagnostics enumerate modelled transitions, recorded mismatches and remaining exchange-only fields. |
| HYP-09 | P1 | Three-arm comparison does not provide strict paired matching for all proposals/outcomes, and its bootstrap can overstate certainty when samples are structurally dependent. | **Fixed** | Comparisons match exact stable proposal identities, report mismatch/coverage diagnostics, require ≥80% coverage with no duplicates, and use a paired six-hour cluster/block bootstrap. |
| HYP-10 | P2 | Research records could be compared across incompatible strategy, prompt, configuration, or code versions without a hard compatibility boundary. | **Mitigated** | G2 is tied to proposal-corpus state plus strategy/configuration and fidelity-code fingerprints. Reports group by account, strategy/version, prompt/config/code, parameter variant, and strategy configuration. A universal compatibility policy across every historical tool is still not enforced. |
| HYP-11 | P0 | Policy-changing variants could persist only accepted trades, leaving no honest population for confidence, exposure, or discriminator vetoes. | **Fixed at review time** | The then-current store persisted accepted/vetoed decisions atomically with portfolio and trade state. Vetoes were explicit zero-return paired actions; qualification used one common pre-paper window and rejected operational failures inside it. |

## 2. Reporting, storage, and auditability findings

| ID | Original priority | Finding | Disposition | Evidence / result |
| --- | --- | --- | --- | --- |
| REP-01 | P0 | Research commands could calculate results without persisting the run, metrics, and findings to `findings.db`. | **Fixed** | Sweeps and successful evaluations now append atomic run/result/finding records through `research/findings.py`. `research.py report` also maintains a backup. |
| REP-02 | P0 | JSON reporting pooled incompatible accounts, strategy versions, variants, and code/configuration contexts. | **Fixed** | `report.py` emits schema version 2 and groups by runtime account, strategy/version, prompt/config/code, parameter variant, and strategy configuration. Covered by `tests/test_report.py`. |
| REP-03 | P0 | Variant identity was discarded in part of the reporting path. | **Fixed** | Reports now retain `variant_id` and `strategy_config_version`. |
| REP-04 | P1 | An exchange fill and the subsequent local journal write cannot be one atomic transaction. A database failure after a fill can leave incomplete attribution. | **Open external-transaction risk** | Existing fail-closed/emergency reconciliation behavior remains, but no local change can make an exchange and SQLite commit atomic. Operational reconciliation is still required. |
| REP-05 | P1 | Transfer events lack a durable exchange transfer identifier, so identical legitimate transfers cannot be safely distinguished from duplicates. | **Fixed where OKX supplies identity; explicit reconciliation otherwise** | Bill/transfer IDs are persisted and deduplicated exactly. Rows without identity are never heuristically collapsed; they enter `transfer_reconciliation_required`. |
| REP-06 | P1 | Findings were append-only by convention rather than protected against update/delete through the application database. | **Fixed within the SQLite store** | New immutability triggers protect runs, results, and findings; uniqueness and foreign-key protections were strengthened. Direct filesystem replacement by an operator remains outside the database's threat model. |
| REP-07 | P1 | There was no automatic backup of the research findings database. | **Fixed at review time; later superseded** | The reviewed version maintained one local findings snapshot. Schema 14 now uses versioned verified backups and never treats that historical snapshot as off-host protection. |
| REP-08 | P2 | Schema-version handling could regress, and existing databases cannot acquire every new foreign-key constraint without a table rebuild. | **Fixed** | The findings store has numbered forward-only transactional migrations, rebuilds legacy core tables, records migration history, validates the current schema and refuses newer unsupported versions. |
| REP-09 | P0 | Reporting could not show the full action population because vetoes were not durable evidence. | **Fixed** | Scorecards now include accept/veto counts, decision-ledger watermarks and content-addressed forward evidence linked to exact accepted trade outcomes. Legacy executed-trades-only qualifications are revoked rather than blended with invented veto history. |

## 3. Strategy-coherence and safety findings

| ID | Finding | Disposition | Evidence / result |
| --- | --- | --- | --- |
| STR-01 | Multiple deterministic strategy contracts existed, but the LLM prompt/schema was momentum-specific, making the other contracts appear more runnable than they were. | **Fixed as a safety boundary** | Registry metadata now distinguishes `analyst_ready`; only momentum is runnable. Other contracts remain shadow-only until the analyst can emit their setup types correctly. |
| STR-02 | Active and shadow parameter evaluation did not reliably apply the same effective parameter contract. | **Fixed** | Each parameter variant now recomputes evidence under its own effective configuration. |
| STR-03 | Cross-strategy shadow decisions used incompatible timeframes/horizons for outcome and expectancy interpretation. | **Fixed as an enforcement boundary; evidence validation remains per strategy** | Every strategy now binds an explicit timeframe, entry, stop, target, costs and holding contract. Only momentum's model is validated; all others are programmatically prohibited from expectancy/promotion until their own evidence is supplied. |
| STR-04 | A G2 state of READY could unblock B7.5 even though READY means the gate still needs to run. | **Fixed** | B7.5 requires a persisted, current G2 PASS. New proposals or strategy/configuration/fidelity-code changes stale the result back to READY. |
| STR-05 | Exploratory research gates could award `T3_VALIDATED`, allowing non-authoritative evidence to reach a live-capable tier. | **Fixed** | Exploratory tournament output is capped at `T2_CANDIDATE`. T3/T4 remain deliberate reviewed registry changes backed by authoritative evidence. |
| STR-06 | Momentum-specific exits and cost assumptions were applied to other strategy mechanisms. | **Fixed by withholding unsupported scoring** | Non-momentum cross-strategy observations remain raw signals until their own forward model is validated. |
| STR-07 | Parameter shadows and cross-strategy shadows shared one event shape, making incompatible populations easy to mix. | **Fixed** | Events are split into `variant_shadow_decision`, `strategy_shadow_decision`, and `strategy_shadow_summary`. Legacy reads are filtered defensively. |
| STR-08 | The repository has no authoritative automated pathway that promotes a strategy to T3 after evidence passes. | **Fixed as a reviewed workflow; registry mutation remains manual by design** | `research.py t3-packet` creates an immutable content-addressed packet linked to current G2, forward analysis, paper evidence and provenance. It is `REVIEWED` only with a complete checklist, reviewer and registry-change reference; no command edits the tier. |

## 4. README-to-code synthesis findings

The synthesizer's initial verdict was that the README was directionally useful
but overstated several guarantees. The implementation commit revised those
sections. The current disposition is:

| ID | Documentation mismatch found | Disposition |
| --- | --- | --- |
| DOC-01 | Replay was described too much like an exact, complete simulation. | **Corrected** — the README describes journal replay as authoritative only after G2 passes and states the remaining fidelity limits. |
| DOC-02 | G2 was called a full stop even though callers could bypass it. | **Code and documentation aligned** — relevant research commands enforce it and persist the result. |
| DOC-03 | Exploratory evidence appeared able to produce a live-capable tier. | **Code and documentation aligned** — exploratory output is capped at T2. |
| DOC-04 | Research commands appeared to rewrite authoritative registry tiers automatically. | **Corrected** — tier changes are documented as deliberate reviewed code changes. |
| DOC-05 | Persistence language implied all scorecards/results were durably recorded. | **Corrected and implemented** — successful evaluations append atomic findings records and maintain a backup. |
| DOC-06 | JSON report isolation was stronger in prose than in the actual grouping logic. | **Corrected and implemented** — compatibility dimensions and variant identity are retained. |
| DOC-07 | Non-momentum contracts appeared runnable through the momentum-specific analyst. | **Corrected and guarded** — they are documented and enforced as shadow-only/raw-signal paths. |
| DOC-08 | One shadow event schema appeared to cover two semantically different populations. | **Corrected and implemented** — parameter and strategy shadow events are separate. |
| DOC-09 | The README hard-coded an obsolete test count. | **Corrected in this audit record commit** — the command now says `full suite` rather than embedding a count that drifts. |

The README now describes independent local variant portfolios, fair scheduling,
paired qualification, clean paper-stage rebasing, migration behavior, transfer
reconciliation and the manual T3 boundary. “Paper” here is deliberately local
simulation, never an exchange order or automatic live promotion.

## 5. Implementation map

The main implementation areas changed by `7a78790` were:

- evidence and outcome integrity: `research/outcomes.py`,
  `research/replay.py`, `agent/shadow.py`;
- selection and confirmation protocol: `research/protocol.py`;
- durable G2 enforcement and readiness: `research/gates.py`,
  `research/readiness.py`, `research.py`, `research/export_live.py`;
- findings persistence and backup: `research/findings.py`;
- report compatibility boundaries: `report.py`;
- strategy runnability and forward-model boundaries: `agent/registry.py`,
  `agent/config.py`, `agent/engine.py`;
- event-schema separation: `agent/shadow.py`, `agent/engine.py`;
- exploratory tier cap: `research/tournament.py`;
- documentation alignment: `README.md`.

## 6. Verification

After the implementation changes, the full test suite completed successfully:

- **809 tests run**
- **1 skipped**
- **0 failures or errors**

New regression coverage includes no-look-ahead outcome windows, replay
determinism, shadow evidence isolation, G2 persistence/staleness, findings-store
immutability/atomicity, readiness enforcement, report grouping, registry safety
boundaries, separated shadow event types, complete decision-ledger migration,
accepted/vetoed pairing, common prequalification cutoffs, and operational
failure invalidation.

Passing tests establish that the implemented contracts behave as asserted by
the suite. They do not remove the explicitly open external transaction boundary
or make replay reproduce exchange-only state.

## 7. Remaining operational work

1. Run the real-time shadow system long enough to collect the required matched
   action population for each axis; the protocol deliberately refuses to claim
   an edge before those samples exist.
2. Let qualified winners build a clean post-qualification local `PAPER` sample,
   then review the content-addressed T3 packet before any manual registry edit.
3. Add and validate a strategy-specific forward model before permitting any
   non-momentum expectancy or promotion claim.
4. Keep operator reconciliation for the irreducible exchange/SQLite transaction
   boundary and for exchange-only state that deterministic replay cannot know.
