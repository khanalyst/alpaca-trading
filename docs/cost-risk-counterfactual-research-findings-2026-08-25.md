# Cost-risk counterfactual and research-learning findings

> Subsequent independent validation and the bounded remediation are recorded in
> [research-findings-validation-and-remediation-2026-08-26.md](research-findings-validation-and-remediation-2026-08-26.md).
> That note corrects unsupported cost/random-null/rolling-guard interpretations
> without changing this report's historical artifact measurements.

- **Report date:** 2026-08-25
- **Vehicle/feed:** US equity / Alpaca IEX
- **Measurement code commit:** `5644388c7ad0ca57583a438df0761457a5e0ad5e`
- **Experiment status:** measured, diagnostic-only, non-authorizing
- **Production decision:** retain `risk.max_stressed_cost_to_risk_ratio = 0.30`
- **Edge decision:** no strategy, variant, or threshold was validated or promoted
- **Terra source cutoff/as-of:** `2026-08-17T13:50:11.599048+00:00`
- **Counterfactual generated:** `2026-08-25T13:47:53.381259+00:00`
- **Latest live VM recheck:** `2026-08-26T13:14:33+00:00`

This document consolidates the VM end-to-end validation, the fresh Terra
strategy-factory run, the `0.30` versus `0.60` stressed-cost counterfactual,
the Section 05 measurement, the evidence-funnel status, the strategy-by-strategy
outcomes, and the persistence audit for future LLM learning and edge formation.

## Executive findings

1. `0.30` and `0.60` are cost-to-risk veto thresholds. They are not risk
   allocations, return targets, stop distances, basis-point assumptions, or
   LLM learning modes.
2. The LLM generated one frozen set of 44 variants across 11 families. The
   counterfactual replayed that same set twice and did not call or update the
   LLM in either arm.
3. At `0.30`, all 1,775 strategy signal opportunities were rejected by the
   stressed-cost gate. There were no trades and therefore no measured R
   expectancy.
4. At `0.60`, 513 trades were executed. Their pooled mean was `-0.541550R`,
   sample sigma was `0.865725R`, and descriptive net P&L was
   `-$33,039.82`. Sixteen variants traded; every one of the sixteen had
   negative mean R. No variant was profitable.
5. `0.60` made the strategy cohort reachable, but the trades it admitted were
   materially losing on this historical diagnostic corpus. This supports
   retaining `0.30`; it does not prove that `0.30` has positive expectancy,
   because `0.30` produced no trades.
6. The Section 05 fixed `0.38R` dead-band assumption is not supported. The
   measured `0.60` per-arm interval width is `0.209460R`, but this is outcome
   dispersion for a pooled diagnostic arm, not a clean counterfactual effect
   interval and not a deployable candidate's dead band.
7. The five-window evidence funnel remains unmeasured. The whole-corpus replay
   has no sealed fit, held-out, qualification, shadow-selection, or
   shadow-confirmation assignments.
8. The source hypotheses, exact variants, account diagnostics, call hashes,
   and counterfactual summaries are durably saved for audit. They are not
   written to the active authorizing learning ledger and are not automatically
   supplied to future LLM discovery or tuning.
9. The VM production/default edge database is empty. There is no validated or
   champion edge. The trader remains paused and production remains configured
   at `0.30`; the original validation reported `validated_edge_required`,
   while the latest state file left its top-level reason unset.
10. The counterfactual artifact is internally well-formed and provenance-bound,
    but it honestly fails direct causal isolation because admitted trades
    changed later account state and generated path-dependent differences.

## Direct answers to the requested questions

### What exactly are `0.30` and `0.60`?

The gate computes:

```text
stressed_cost_to_risk_ratio = stressed_estimated_cost / intended_trade_risk
```

The trade is refused when that ratio exceeds the configured maximum. The
shipped stress scenario remains 25 bps of entry notional in both arms. Only
the maximum ratio changes. The implementation is in
[`agent/risk.py`](../agent/risk.py#L120-L182) and
[`research/costs.py`](../research/costs.py#L757-L865).

For a trade with `$100` of intended risk:

- `0.30` allows at most `$30` of stressed estimated cost;
- `0.60` allows at most `$60` of stressed estimated cost.

A trade with `$45` of stressed cost and `$100` of intended risk is rejected at
`0.30` and admitted at `0.60`, assuming every other risk and execution gate
passes. Neither value changes `risk_per_trade_pct`, the stop, the target, the
position size, the cost scenario, or the strategy definition.

### Is the LLM recording or learning separately in both arms?

No. The counterfactual has no per-arm LLM loop.

The sequence was:

1. `gpt-5.6-terra` performed bounded discovery and tuning once.
2. That source run produced 11 family hypotheses and 44 frozen variants.
3. The exact same variants and corpus were simulated at `0.30`.
4. The exact same variants and corpus were simulated at `0.60`.
5. Aggregate arm results and pairing diagnostics were written to one
   diagnostic JSON artifact.

The counterfactual does not import or instantiate `FactoryLedger`,
`EdgeLedger`, or the LLM proposal client. It emits no proof and performs no
promotion. See
[`research/cost_counterfactual.py`](../research/cost_counterfactual.py#L807-L851)
and the explicit diagnostic factory boundary in
[`research/strategy_factory.py`](../research/strategy_factory.py#L2440-L2455).

The Terra source report records `calls_used = 22`, the model/deployment,
request and response hashes, grammar and normalized-spec hashes, exact rule
specifications, theses, falsification statements, selection reasons, and
aggregate diagnostics. It does not store provider responses verbatim. In the
fresh diagnostic report, tuning records show `lessons_supplied = 0`,
`lessons_cited = []`, and the variants do not build on prior graded lessons.

### What is the base of the study?

The empirical base is a frozen historical IEX equity corpus and one frozen
strategy cohort:

| Study component | Frozen value |
| --- | --- |
| Provider/feed | Alpaca / IEX |
| Vehicle | Equity |
| Source cutoff/as-of | `2026-08-17T13:50:11.599048+00:00` |
| Derived sessions replayed | 126 |
| Source factory `raw_rows` | 19,332,347 |
| Preprocessing manifest kept rows | 19,386,686 |
| Replay rows in paired experiment | 44,352 |
| Strategy families | 11 |
| Variants | 44, four per family |
| Isolated diagnostic accounts | 44 |
| Dataset hash | `301af2d63ca64cba283868fe05ebcd4fdf44fc716b5bcc0ce8844101356c3aa8` |
| Frozen cohort hash | `81cf67196cea99f15fdf2c66132d85aa683e5aec358b69752241549889b50cc7` |
| Changed configuration path | `risk.max_stressed_cost_to_risk_ratio` |

The two row counts describe successive views and reconcile exactly. The cache
retained 19,386,686 normalized rows: 364,861 bars, 18,967,486 quotes, and
54,339 option snapshots. The equity vehicle filter then excluded those 54,339
option rows without changing the source, leaving 19,332,347 bars-plus-quotes
as the factory's `raw_rows`. The 419,200 replay rows are a separately derived
view, not additional normalized source rows. The counterfactual's inferential
pairing denominator is 44,352 terminal variant/opportunity rows.

The corpus is historical-backfill diagnostic evidence. The authorizing
protocol forbids it from creating a proof or validating a deployment. The
point-in-time, feed, calendar, cost, and replay constraints are documented in
[`research/protocol.md`](../research/protocol.md).

### What should form future edge and variant decisions?

The intended authorizing loop is:

```text
fit-only evidence
  -> immutable FactoryLedger proposal reason and parameter change
  -> graded fit lesson and family/shared-learning summary
  -> next LLM or deterministic proposal
  -> chronological held-out and qualification gates
  -> parity-matched shadow-selection and shadow-confirmation
  -> immutable EdgeLedger evidence and proof
  -> validated/champion candidate
  -> paper trial and operator-visible promotion decision
```

Future proposal prompts are designed to receive compact, fit-derived lesson
fields such as lesson ID, family, changed parameters, reason, fit verdict, and
fit delta. Raw market rows and post-selection held-out, qualification, and
shadow results are deliberately excluded from proposal prompts to prevent
leakage. The normal immutable lesson/outcome schema is in
[`research/factory_ledger.py`](../research/factory_ledger.py#L349-L435).

This diagnostic counterfactual must not be copied into that authorizing chain
as if it were fit evidence. It used the whole historical corpus and did not
seal the five protocol windows. Its safe future use is as a separately tagged,
non-authorizing experiment record:

- retain `0.30` as the production safety policy;
- record that `0.60` admitted a negative cohort and is rejected for this
  frozen experiment;
- do not tune the same variants against their whole-corpus `0.60` outcomes;
- require new variant identities and unseen, preregistered windows before a
  result may affect edge status;
- provide only fit-window summaries to future proposal generation;
- retain held-out, qualification, and shadow outcomes for audit and gates,
  never as same-epoch tuning input.

### What has been properly recorded and what has not?

| Layer | What is saved | Is it active future-learning input? | Finding |
| --- | --- | --- | --- |
| Raw recorder | Session CSVs, recorder index, source sidecars, watermark/status, dedup database | Corpus input after validation | Saved; recorder status still needs operational follow-up |
| Derived corpus | IEX/RTH-derived sessions and provenance markers | Replay input | Saved and content-addressed |
| Preprocessing cache | Validated data, quotes, replay, bars, reports, member hashes | Reproducible acceleration layer | Saved; not runtime authority |
| Terra diagnostic report | Hypotheses, theses, falsification, exact rule specs, 44 variants, account diagnostics, call evidence/hashes | No | Audit-complete but non-authorizing |
| Counterfactual report | Arm policies, summaries, R distributions, Section 05, pairing classifications, hashes | No | Audit-complete but non-authorizing |
| Counterfactual raw arm rows | Constructed in memory | No | Not persisted; artifact retains aggregates and digests |
| Counterfactual config contents | Replay-policy projection and hashes | No | Full contents explicitly not persisted (`config_contents_persisted=false`) |
| Active `FactoryLedger` | Nothing beyond schema metadata | Yes, if populated | Currently empty: no cycles, hypotheses, lessons, or outcomes |
| Alternate Terra/derived ledgers | 51 candidates, 44 accounts, 1 cycle, 11 hypotheses, 55 lessons/outcomes; all outcomes underpowered | No | Saved in inactive databases; not production/default memory |
| Active `EdgeLedger` | Nothing authorizing | Yes, if populated | Zero candidates, runs, evidence, trades, or proofs |
| Paper outcomes | None for an authorizing edge | Failed trials can become lessons | No active edge exists to enter trial review |
| Markdown proofs | None | Proof reader only | Correct: diagnostic evidence may not emit proofs |

The central persistence conclusion is therefore:

> The audit trail is largely durable and reproducible, but the fresh diagnostic
> findings are not integrated into the active canonical learning memory.

That separation is scientifically safe, because it prevents historical
whole-corpus leakage. It also means the LLM will not automatically remember
this experiment unless a dedicated diagnostic experiment registry or a
carefully scoped fit-only import path is added.

## System synchronization and end-to-end validation

The research execution was validated while the following three locations all
resolved to measurement commit
`5644388c7ad0ca57583a438df0761457a5e0ad5e`:

- local `main`;
- GitHub `origin/main`;
- VM repository `/opt/alpaca-agent-trading` on `okx-agent`.

The detached VM counterfactual container
`alpaca-cost-counterfactual-v2-5644388` completed after approximately four
hours and twelve minutes with exit code `0`. The output was then validated
end-to-end:

- strict finite JSON parsing passed;
- the repository content-hash algorithm recomputed the exact stored hash;
- dataset, source-report, cohort, code, runtime-config, and run-settings
  bindings were present and matched;
- the source report was confirmed diagnostic-only, non-authorizing, and to
  contain an explicit empty proof list;
- the only changed configuration path was exact;
- pairing was complete and free of duplicate or malformed keys;
- production mutation and promotion were false;
- the production trader remained on `0.30`; during the original validation it
  was paused for `validated_edge_required`.

The report added by this document is documentation-only and does not alter the
experiment's code, configuration, corpus, or content identity.

### Live VM recheck on 2026-08-26

The following was read directly from `okx-agent`, not inferred from the local
copies:

| Check | Live result |
| --- | --- |
| VM repository | Clean tracked worktree at `5644388c7ad0ca57583a438df0761457a5e0ad5e` |
| GitHub `origin/main` | `5644388c7ad0ca57583a438df0761457a5e0ad5e` |
| Counterfactual container | Exited `0`; started `2026-08-25T09:35:29.966550Z`, finished `2026-08-25T13:47:53.619641Z` |
| Terra artifact | 36,914,450 bytes; literal SHA-256 `843d711b37a19653b03fdf07de4747d91fcc1b7dbaeb67501eba8000f5ebc3e7` |
| Counterfactual artifact | 758,780 bytes; literal SHA-256 `b0543c6bc9240cf3bbdf01ee4c0691a496c6f33ec79a868c3ab30727f93585e5` |
| Production policy | Ratio `0.30`, stress `25` bps, selection mode `all_proved`, validated variant required |
| Trader container | Running, Docker-healthy, zero restarts, state `PAUSED`, `operator_pause=false`; top-level reason unset in the current state file |
| Active ledger | Still zero candidates, lessons, runs, evidence, and paper outcomes |
| Alternate Terra/derived-v3 ledgers | Counts unchanged at 51 candidates, 44 accounts, 55 lessons/outcomes each; no runs/evidence/paper outcomes |

The canonical counterfactual JSON still reports the same content, dataset, and
cohort hashes; 1,775/0 baseline signals/trades; 1,775/513 alternative
signals/trades; `-0.5415496294R`; 44,352 paired rows; 840 path-dependent
transitions; `authorizing=false`; `production_mutation=false`; and zero proofs.

#### Recorder status nuance

Docker reports `alpaca-recorder` running and healthy with zero restarts, but
the recorder's status file retains a retryable failure from
`2026-08-26T03:42:48Z`:

```text
OperationalError: database or disk is full
```

The VM and runtime volume are not capacity-full:

- host root: 51 GB available, 17% used;
- runtime volume: 74 GB available, 38% used;
- runtime-volume inodes: 1% used;
- recorder corpus: approximately 26 GB;
- recent-key SQLite file: approximately 434 MB;
- rollback journal observed during the live transaction: approximately 53 MB.

During the recheck, the recorder process was active and the index watermark
advanced from `2026-08-24T18:20:59.976429Z` to
`2026-08-24T18:22:59.978634Z`. A read-only SQLite query encountered
`database is locked`, consistent with the active exclusive update. Thus the
old `database or disk is full` status is not evidence of current host disk
exhaustion, and recording was progressing during observation. However, the
status/health mismatch and the roughly 43-hour watermark lag remain operational
debt. Forward readiness must remain pending until the recorder catches up and
emits an unambiguous current success/health record.

## Artifact inventory and provenance

### Canonical VM artifacts

| Artifact | VM path | Role |
| --- | --- | --- |
| Fresh Terra source report | `/app/runtime/research/diagnostics/terra-20260824-fresh-3a64ffc-equity.json` | Frozen hypotheses, variants, account diagnostics, and LLM call evidence |
| Cost counterfactual | `/app/runtime/research/diagnostics/cost-counterfactual-terra-20260824-fresh-3a64ffc-equity.json` | Measured `0.30`/`0.60` result |
| Raw recorder corpus | `/app/runtime/research/recorded/` | Source session files and provenance |
| Derived equity corpus | `/app/runtime/research/recorded-iex-rth-derived-v2-20260824/` | Frozen IEX/RTH research view |
| Preprocessing cache | `/app/research/cache/preprocessing/entries/d6d9f16461f76702b30548cb1e5b23529d1a25dd6672b71c1885894dcf5567a7/` | Immutable normalized/replay artifacts |
| Active/default ledger | `/app/runtime/research/edge_lab.sqlite3` | Production research authority; currently empty |
| Inactive Terra ledger | `/app/runtime/research/edge_lab.terra-20260823.sqlite3` | Prior/alternate underpowered research history |
| Inactive derived-v3 ledger | `/app/runtime/research/edge_lab.iex-rth-derived-v3.sqlite3` | Prior/alternate underpowered research history |

Read-only local audit copies of the two JSON reports were placed at:

- `/private/tmp/terra-20260824-fresh-3a64ffc-equity.json`;
- `/private/tmp/cost-counterfactual-terra-20260824-fresh-3a64ffc-equity.json`.

### Counterfactual identity hashes

| Identity | SHA-256 |
| --- | --- |
| Final content | `9a65c7f8455bba729ee51da2a95ba6a6f0d7be2f12233a3e82244f25ef4c7e32` |
| Source report | `4542e7c71dc3a6088fca75320671e5ea88059f58591d2e983396675abd6af3c9` |
| Dataset | `301af2d63ca64cba283868fe05ebcd4fdf44fc716b5bcc0ce8844101356c3aa8` |
| Frozen cohort | `81cf67196cea99f15fdf2c66132d85aa683e5aec358b69752241549889b50cc7` |
| Measurement code | `f11da2a6f0cc67e88b05bfc7c7bb5cf91507cb10e555052285b90bca99eccf1d` |
| Run settings | `dc632c2abf06456c068a76369a20622d1519cde41524cef39a3798678ddd25f5` |
| Runtime/baseline config | `baf989318e194c5185072f593f8f5f98e89a2f2741c202b9b286833bb095bac7` |
| Alternative config | `34319fcc1542bb4aca38d8154f8e383b33d21f8a3e7ee2439166aea31059fb7c` |

The artifact's canonical repository `content_hash` is a hash over the
normalized payload with the content-hash field handled by the repository
algorithm. It is distinct from the SHA-256 of the literal JSON file bytes. The
local audit copies had these byte hashes:

| File | Literal-file SHA-256 |
| --- | --- |
| Counterfactual JSON | `b0543c6bc9240cf3bbdf01ee4c0691a496c6f33ec79a868c3ab30727f93585e5` |
| Terra source JSON | `843d711b37a19653b03fdf07de4747d91fcc1b7dbaeb67501eba8000f5ebc3e7` |

### Preprocessing evidence

The immutable cache manifest binds these members by size and content hash:

| Member | Bytes |
| --- | ---: |
| Validated/normalized data | 7,889,042,323 |
| Quotes | 5,833,903,351 |
| Replay | 153,727,820 |
| Bars | 114,345,510 |

The preprocessing stage scanned 19,415,521 source rows and quarantined 28,835
legacy option snapshots for `as_of_after_observed_at`, leaving 19,386,686
normalized rows. Its view counts were 364,861 bars, 18,967,486 quotes, 54,339
remaining option snapshots, and 419,200 separately derived replay rows. The
equity vehicle filter excluded all 54,339 option rows from this experiment,
which yields the source factory's exact 19,332,347-row equity input.

## Experiment design

### Frozen cohort

The Terra source report has schema `strategy-factory.v1`, status
`diagnostic_only`, `authorizing=false`, and `proofs=[]`. It produced:

- 11 LLM-discovered root hypotheses;
- four frozen variants per family;
- 44 isolated accounts;
- 44 frozen variant IDs;
- zero authorizing results and zero proofs.

The source report records 22 LLM calls used from a budget of 64 and no provider
or authentication circuit failure. Diagnostic mode intentionally allows model
discovery and bounded tuning while skipping both ledgers, FDR mutation,
qualification/shadow boundaries, proof generation, trial review, and
production state.

### Counterfactual arms

| Property | Baseline | Alternative |
| --- | ---: | ---: |
| `max_stressed_cost_to_risk_ratio` | `0.30` | `0.60` |
| `stressed_cost_scenario_bps` | `25` | `25` |
| Risk per trade | `0.5%` | `0.5%` |
| Max open risk | `2.0%` | `2.0%` |
| Max concurrent positions | `3` | `3` |
| Dataset | Frozen, identical | Frozen, identical |
| Variant cohort | 44, identical | 44, identical |
| Promotion authority | None | None |
| Production mutation | False | False |

`config_evidence.changed_paths` contains exactly:

```text
risk.max_stressed_cost_to_risk_ratio
```

No unexpected configuration path changed.

### Opportunity pairing and isolation

Pairing used the exact key `variant_id + opportunity_id`.

| Pairing result | Count |
| --- | ---: |
| Matched | 44,352 |
| Baseline-only | 0 |
| Alternative-only | 0 |
| Duplicate keys | 0 |
| Malformed rows | 0 |
| Pairing coverage | 100% |

The configuration experiment was exact, but its row-level outcome path was not
causally isolated:

| Transition classification | Count |
| --- | ---: |
| Unchanged | 42,999 |
| Direct cost-gate admission | 513 |
| Downstream transition after gate change | 20 |
| Path-dependent output change | 820 |
| Unexpected or path-dependent total | 840 |

The 20 downstream transitions were cost-blocked rows that later encountered
the three-position capacity limit after earlier `0.60` trades changed account
state. The 820 path-dependent output changes are later output differences
following a changed execution path.

Consequently:

- `configuration_change_verified = true`;
- `pairing_complete = true`;
- `direct_causal_interpretation_isolated = false`;
- `controlled_change_verified = false`;
- invariant failure: `direct_causal_interpretation_not_isolated`.

The correct interpretation is an end-to-end replay of two policies over the
same frozen cohort, not a clean local causal effect for every paired row.

## Aggregate empirical results

| Metric | `0.30` baseline | `0.60` alternative |
| --- | ---: | ---: |
| Terminal replay rows | 44,352 | 44,352 |
| Signal opportunities | 1,775 | 1,775 |
| Stressed-cost refusals | 1,775 | 1,242 |
| Cost admissibility | 0.0000% | 30.0282% |
| Execution admission | 0.0000% | 28.9014% |
| Executed trades | 0 | 513 |
| Mean R | unavailable | -0.541550R |
| Median R | unavailable | -0.607264R |
| Sample sigma | unavailable | 0.865725R |
| Minimum / maximum R | unavailable | -1.661396R / 1.694398R |
| Descriptive net P&L | $0.00, no trades | -$33,039.82 |
| Target reached | 0/0 | 70/513, 13.6452% |
| Sessions replayed | 126 | 126 |
| Sessions with trades | 0 | 68 |
| Trades/session over all sessions | 0.000 | 4.071 |
| Trades/session over active sessions | unavailable | 7.544 |
| Maximum pooled variant-trades/session | 0 | 29 |

`0.60` reduced stressed-cost refusals by 533, but only 513 became executions;
the other 20 reached the downstream concurrent-position limit. The admitted
trade fills were diagnostic bar fills: 513 bar entries and 513 bar exits. No
entry-slippage observations were available. This is another reason the P&L
must remain descriptive rather than an execution proof.

### Dispositions and refusal reasons

| Disposition/reason | `0.30` | `0.60` |
| --- | ---: | ---: |
| `executed` | 0 | 513 |
| `no_signal` | 10,920 | 10,920 |
| `refused` | 33,432 | 32,919 |
| `no_contiguous_feature_window` | 31,657 | 31,657 |
| `stressed_cost_risk_limit` | 1,775 | 1,242 |
| `max concurrent positions reached` | 0 | 20 |

For the `0.60` executed trades, ordinary modeled costs averaged `$2.451125`
per variant-trade, with median `$2.452126`, minimum `$2.356489`, maximum
`$2.504367`, and sample sigma `$0.028558`. Mean P&L per executed variant-trade
was `-$64.405102`.

The baseline stressed-cost-to-risk observations had mean `0.662637`, median
`0.677727`, interquartile range `[0.584063, 0.751006]`, minimum `0.326605`,
maximum `0.833333`, and sample sigma `0.112271`. These values explain why a
`0.30` limit rejected every signal. The alternative's observed ratio
distribution is nearly the same, but the limit admits the lower part of that
distribution; it has 1,755 finite ratio observations because 20 later rows
transitioned into the concurrent-position refusal after earlier admissions.

### Distinguishing the two admissibility measurements

Two different measurements now exist and must not be combined:

| Measurement | `0.30` | `0.60` | Denominator |
| --- | ---: | ---: | --- |
| Prior geometric ATR-window admissibility | 13.92% | 52.08% | Measured market windows under stop/ATR geometry |
| End-to-end strategy-signal cost admissibility | 0.00% | 30.03% | 1,775 actual signal opportunities from the frozen cohort |

The `13.92% -> 52.08%` result remains useful evidence that the geometry is
restrictive but not empty. The end-to-end replay asks a narrower question:
whether these particular strategies, with all their other constraints and
account-state paths, generated cost-admissible signal opportunities. Different
denominators explain the different percentages.

## Section 05: empirical dispersion, dead band, MDE, and power

### What was measured

The prior assumptions were approximately `1.3R` per-trade sigma and 3.3 trades
per session cluster. The counterfactual measured the following pooled
variant-trade outcomes for `0.60`:

| Quantity | Measurement |
| --- | ---: |
| Finite R observations | 513 |
| Sessions with trades / bootstrap clusters | 68 |
| Per-trade sample sigma | 0.865725R |
| Trades per all 126 sessions | 4.071 |
| Trades per 68 active sessions | 7.544 |
| Moving-block bootstrap draws | 4,000 |
| Block length | 5 sessions |
| Observed mean | -0.541550R |
| 95% interval | [-0.654285R, -0.444825R] |
| Interval width | 0.209460R |
| Clustered MDE at alpha 0.05 | 0.159105R |
| Bootstrap standard error | 0.063988R |
| Target economic effect | 0.05R |
| Estimated power at 0.05R | 0.19225 |
| Target power | 0.80 |

For `0.30`, Section 05 is unavailable because it produced zero finite R
observations.

### What the measurement means

- The assumed `1.3R` dispersion was too high for this pooled `0.60` sample;
  the measured value is `0.865725R`.
- The assumed 3.3 trades per session is not reproduced. The result is 4.071
  pooled variant-trades over every session or 7.544 over sessions with at least
  one trade.
- These are pooled observations from 44 isolated variant accounts, not the
  trade rate or sigma of one deployable strategy.
- The interval width is `0.209460R`, not `0.38R`.
- The observed interval is wholly negative and does not contain the positive
  `0.05R` economic floor.
- Power for detecting a `0.05R` effect is only 19.225%, far below the 80%
  target.
- This is explicitly a per-arm empirical outcome-dispersion measurement, not
  a counterfactual effect estimate.

Therefore a fixed `0.38R` dead band should not be retained. The baseline arm
cannot supply its own interval, and the alternative interval cannot be
relabelled as a causal threshold-effect interval.

## Evidence-funnel status

The corrected protocol contains five windows, not four:

| Window | Trade floor | Session/cluster floor | Measured here? |
| --- | ---: | ---: | --- |
| Fit | 100 | 30 | No |
| Held-out | 100 | 30 | No |
| Qualification | 100 | 30 | No |
| Shadow selection | 150 | 30 | No |
| Shadow confirmation | 150 | 30 | No |
| **Nominal total** | **600** | — | **No** |

Every window is marked unavailable with reason:

```text
whole_corpus_counterfactual_has_no_sealed_or_live_window_assignments
```

The readiness context is 150 offline sessions plus 60 shadow sessions, or 210
sessions in total. The counterfactual artifact does not measure current
forward-session progress. An earlier handoff reported one forward-observed
session, while the later VM recorder audit found the indexed source entries
marked historical backfill and a retryable recorder request failure. Forward
readiness must therefore be recomputed from the repaired recorder provenance;
it should not presently be treated as a verified `1/210` or as complete.

## Strategy-family findings

The table covers all 11 strategy families and four variants per family. Signal
opportunities are the common frozen-cohort opportunities. Every `0.30` trade
count is zero.

| Strategy family | Signal opportunities | `0.30` trades | `0.60` trades | `0.60` mean R | Descriptive `0.60` P&L | Target hits | Variant outcome |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Mean reversion | 0 | 0 | 0 | — | $0.00 | 0/0 | 4/4 no-trade |
| Momentum continuation | 0 | 0 | 0 | — | $0.00 | 0/0 | 4/4 no-trade |
| Opening drive | 0 | 0 | 0 | — | $0.00 | 0/0 | 4/4 no-trade |
| Opening-range breakout | 335 | 0 | 106 | -0.550231R | -$7,066.60 | 12/106 | 4/4 traded; 4/4 negative |
| Opening-range fade | 0 | 0 | 0 | — | $0.00 | 0/0 | 4/4 no-trade |
| Range expansion | 290 | 0 | 168 | -0.630566R | -$12,684.49 | 28/168 | 4/4 traded; 4/4 negative |
| Trend pullback | 2 | 0 | 0 | — | $0.00 | 0/0 | 4/4 no-trade; both signals remain cost-blocked |
| Volatility breakout | 0 | 0 | 0 | — | $0.00 | 0/0 | 4/4 no-trade |
| Volume breakout | 96 | 0 | 0 | — | $0.00 | 0/0 | 4/4 no-trade; all 96 signals remain cost-blocked |
| VWAP reversion | 934 | 0 | 226 | -0.440751R | -$11,739.88 | 29/226 | 4/4 traded; 4/4 negative |
| VWAP trend | 118 | 0 | 13 | -1.072738R | -$1,548.85 | 1/13 | 4/4 traded; 4/4 negative |
| **Total** | **1,775** | **0** | **513** | **-0.541550R** | **-$33,039.82** | **70/513** | **16 negative trading variants; 28 no-trade** |

### Family-level interpretation

#### Mean reversion

The hypothesis proposed that late-morning, volatility-and-volume overshoots
could mean-revert after opening price discovery. None of its four variants
produced a signal in this replay. The result is untested/no-opportunity, not a
negative expectancy estimate.

#### Momentum continuation

The hypothesis required aligned trend, expanding volume, and sufficient ATR.
None of its four variants produced a signal. The thesis is untested on this
cohort.

#### Opening drive

The hypothesis expected persistent high-volume opening drives to attract
follow-through. None of its four variants produced a signal. The thesis is
untested on this cohort.

#### Opening-range breakout

This family produced 335 signal opportunities. `0.30` blocked all of them.
`0.60` executed 106 trades, but all four variants were negative. The family
mean was `-0.550231R`; descriptive P&L was `-$7,066.60`. The relaxed cost gate
made the family reachable but did not reveal positive edge.

#### Opening-range fade

The hypothesis expected failed opening excursions to reverse after liquidity
replenishment. None of its four variants produced a signal. The thesis is
untested on this cohort.

#### Range expansion

This family produced 290 signal opportunities. `0.60` executed 168 trades,
the most of any family except VWAP reversion. All four variants were negative,
with family mean `-0.630566R` and descriptive P&L `-$12,684.49`, the largest
family loss in the experiment.

#### Trend pullback

One variant produced two signals. Both were rejected at `0.30` and remained
rejected at `0.60`. No R outcome exists; the result is cost-unreachable for
this cohort.

#### Volatility breakout

None of the four variants produced a signal. This family is untested in the
current replay despite its designed intent to use wider ATR risk to avoid the
cost floor.

#### Volume breakout

The family produced 96 signal opportunities. Every one remained above the
cost-to-risk limit even at `0.60`, so no trades executed. This is stronger than
a zero-signal result: the strategies found setups, but the proposed risk/cost
geometry remained unreachable.

#### VWAP reversion

This family supplied 934 of the 1,775 signal opportunities and 226 of the 513
executions. It was the least negative trading family, but all four variants
still lost. The family mean was `-0.440751R`; descriptive P&L was
`-$11,739.88`. The least-negative individual variant in the complete cohort was
`rule.vwap-reversion.a73b8c754617e615` at `-0.360884R` over 78 trades. It is
not a promotion candidate.

#### VWAP trend

This family produced 118 signals and only 13 executions. All four variants
were negative. Its pooled mean of `-1.072738R` was the worst family result,
though the sample is sparse.

## Complete 44-variant outcome table

`Signals` and `0.30 cost vetoes` are from the baseline arm. `0.60 cost vetoes`
and all outcome fields are from the alternative arm. A zero-trade row has no
R estimate; `$0` is an accounting result, not evidence of flat expectancy.

| Family | Variant ID | Signals | `0.30` cost vetoes | `0.30` trades | `0.60` cost vetoes | `0.60` trades | `0.60` mean R | `0.60` P&L | Target hits |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| mean-reversion | `rule.mean-reversion.5492362abed14e28` | 0 | 0 | 0 | 0 | 0 | — | $0.00 | 0/0 |
| mean-reversion | `rule.mean-reversion.56b555b670acaf79` | 0 | 0 | 0 | 0 | 0 | — | $0.00 | 0/0 |
| mean-reversion | `rule.mean-reversion.ce1dc46d4af4e3b1` | 0 | 0 | 0 | 0 | 0 | — | $0.00 | 0/0 |
| mean-reversion | `rule.mean-reversion.db8f6edb149b3eac` | 0 | 0 | 0 | 0 | 0 | — | $0.00 | 0/0 |
| momentum-continuation | `rule.momentum-continuation.1dc036c7628080d3` | 0 | 0 | 0 | 0 | 0 | — | $0.00 | 0/0 |
| momentum-continuation | `rule.momentum-continuation.2bf9acfcf87792f8` | 0 | 0 | 0 | 0 | 0 | — | $0.00 | 0/0 |
| momentum-continuation | `rule.momentum-continuation.955caf5e5f857365` | 0 | 0 | 0 | 0 | 0 | — | $0.00 | 0/0 |
| momentum-continuation | `rule.momentum-continuation.bfa229bd1b021325` | 0 | 0 | 0 | 0 | 0 | — | $0.00 | 0/0 |
| opening-drive | `rule.opening-drive.3203e3bd72906f14` | 0 | 0 | 0 | 0 | 0 | — | $0.00 | 0/0 |
| opening-drive | `rule.opening-drive.3606be107797593c` | 0 | 0 | 0 | 0 | 0 | — | $0.00 | 0/0 |
| opening-drive | `rule.opening-drive.5f190d7f939f34f6` | 0 | 0 | 0 | 0 | 0 | — | $0.00 | 0/0 |
| opening-drive | `rule.opening-drive.66b4829e5aef120e` | 0 | 0 | 0 | 0 | 0 | — | $0.00 | 0/0 |
| opening-range-breakout | `rule.opening-range-breakout.51d9a4810a471ae3` | 66 | 66 | 0 | 0 | 64 | -0.413449R | -$3,115.75 | 9/64 |
| opening-range-breakout | `rule.opening-range-breakout.5e1a6a2559bd27aa` | 131 | 131 | 0 | 118 | 13 | -0.739377R | -$1,196.84 | 1/13 |
| opening-range-breakout | `rule.opening-range-breakout.be80f07787d32391` | 66 | 66 | 0 | 53 | 13 | -0.739377R | -$1,196.84 | 1/13 |
| opening-range-breakout | `rule.opening-range-breakout.d7d2db3a3b60406f` | 72 | 72 | 0 | 56 | 16 | -0.789993R | -$1,557.17 | 1/16 |
| opening-range-fade | `rule.opening-range-fade.6dca427e07fd0c5a` | 0 | 0 | 0 | 0 | 0 | — | $0.00 | 0/0 |
| opening-range-fade | `rule.opening-range-fade.b5b20c365332a3d2` | 0 | 0 | 0 | 0 | 0 | — | $0.00 | 0/0 |
| opening-range-fade | `rule.opening-range-fade.bc10822608da69e5` | 0 | 0 | 0 | 0 | 0 | — | $0.00 | 0/0 |
| opening-range-fade | `rule.opening-range-fade.c8bbcde81a8f9035` | 0 | 0 | 0 | 0 | 0 | — | $0.00 | 0/0 |
| range-expansion | `rule.range-expansion.3b79fdabc6a383b5` | 80 | 80 | 0 | 30 | 50 | -0.586424R | -$3,673.01 | 8/50 |
| range-expansion | `rule.range-expansion.76ef30386fde6af3` | 80 | 80 | 0 | 38 | 42 | -0.686141R | -$3,344.37 | 6/42 |
| range-expansion | `rule.range-expansion.abd1589544fe305c` | 65 | 65 | 0 | 26 | 38 | -0.628894R | -$2,833.55 | 7/38 |
| range-expansion | `rule.range-expansion.eecfaa6487102dbb` | 65 | 65 | 0 | 26 | 38 | -0.628894R | -$2,833.55 | 7/38 |
| trend-pullback | `rule.trend-pullback.25119a2a0230869d` | 0 | 0 | 0 | 0 | 0 | — | $0.00 | 0/0 |
| trend-pullback | `rule.trend-pullback.6285a5578e7be43b` | 0 | 0 | 0 | 0 | 0 | — | $0.00 | 0/0 |
| trend-pullback | `rule.trend-pullback.87ab54bbe471174c` | 0 | 0 | 0 | 0 | 0 | — | $0.00 | 0/0 |
| trend-pullback | `rule.trend-pullback.f326cb992e9160e5` | 2 | 2 | 0 | 2 | 0 | — | $0.00 | 0/0 |
| volatility-breakout | `rule.volatility-breakout.082b10b3539741c8` | 0 | 0 | 0 | 0 | 0 | — | $0.00 | 0/0 |
| volatility-breakout | `rule.volatility-breakout.166d1398b6045d22` | 0 | 0 | 0 | 0 | 0 | — | $0.00 | 0/0 |
| volatility-breakout | `rule.volatility-breakout.68e15478879806da` | 0 | 0 | 0 | 0 | 0 | — | $0.00 | 0/0 |
| volatility-breakout | `rule.volatility-breakout.9ff9d6c91dc8182a` | 0 | 0 | 0 | 0 | 0 | — | $0.00 | 0/0 |
| volume-breakout | `rule.volume-breakout.92a667fa86797f91` | 20 | 20 | 0 | 20 | 0 | — | $0.00 | 0/0 |
| volume-breakout | `rule.volume-breakout.b146288f295b2288` | 25 | 25 | 0 | 25 | 0 | — | $0.00 | 0/0 |
| volume-breakout | `rule.volume-breakout.b93d55fd1aa5693f` | 36 | 36 | 0 | 36 | 0 | — | $0.00 | 0/0 |
| volume-breakout | `rule.volume-breakout.ed8849c3813b5e1a` | 15 | 15 | 0 | 15 | 0 | — | $0.00 | 0/0 |
| vwap-reversion | `rule.vwap-reversion.69523b66ecd26980` | 208 | 208 | 0 | 152 | 51 | -0.473764R | -$2,806.42 | 5/51 |
| vwap-reversion | `rule.vwap-reversion.a73b8c754617e615` | 286 | 286 | 0 | 203 | 78 | -0.360884R | -$3,334.07 | 14/78 |
| vwap-reversion | `rule.vwap-reversion.dff3b616eb7aeae5` | 217 | 217 | 0 | 164 | 49 | -0.490126R | -$2,841.80 | 5/49 |
| vwap-reversion | `rule.vwap-reversion.e9ea4409038ae8e5` | 223 | 223 | 0 | 173 | 48 | -0.485057R | -$2,757.60 | 5/48 |
| vwap-trend | `rule.vwap-trend.aac34669f24789e1` | 24 | 24 | 0 | 22 | 2 | -1.476856R | -$319.21 | 0/2 |
| vwap-trend | `rule.vwap-trend.be551c0c563b9eb4` | 33 | 33 | 0 | 31 | 2 | -1.133687R | -$243.24 | 0/2 |
| vwap-trend | `rule.vwap-trend.cdbf7267aa788a42` | 29 | 29 | 0 | 26 | 3 | -1.429217R | -$480.40 | 0/3 |
| vwap-trend | `rule.vwap-trend.da2f39d3ca2d2244` | 32 | 32 | 0 | 26 | 6 | -0.739475R | -$505.99 | 1/6 |

## Learning and ledger audit

### What the normal factory records

The authorizing factory is designed to persist an immutable chain:

1. a proposal reason before the outcome is known;
2. the exact changed parameter values and source (`llm`, `deterministic`, or
   live paper);
3. the hypothesis and parent lesson lineage;
4. fit classification and fit delta;
5. held-out delta/P&L, q-value, failed checks, and verified gate hash for audit;
6. the complete account result and cycle provenance;
7. later paper outcomes and lifecycle events in `EdgeLedger`.

Future LLM proposal prompts use only compact allowed projections, primarily
fit-derived lessons. Held-out and qualification evidence can be retained in the
ledger while remaining unavailable to proposal generation. This is the right
anti-leakage boundary.

### What happened in this diagnostic run

Diagnostic factory mode deliberately did not construct either ledger. It wrote
one source JSON report and returned non-authorizing status. The counterfactual
then wrote one additional JSON report and likewise did not construct either
ledger.

Therefore none of these `0.30` or `0.60` facts currently become automated
future memory:

- which families generated no signals;
- which families remained cost-unreachable at `0.60`;
- which sixteen variants traded and lost;
- the measured `0.865725R` dispersion;
- the measured session-cluster rates;
- the negative `0.60` interval;
- the path-dependence failure.

They remain available to operators and future tooling through the diagnostic
artifact, but the next normal factory cycle will not retrieve them from the
active `FactoryLedger`.

### Active versus alternate databases

The VM production/default database
`/app/runtime/research/edge_lab.sqlite3` was audited with these effective
counts:

| Table/domain | Count |
| --- | ---: |
| Candidates | 0 |
| Factory cycles | 0 |
| Factory hypotheses | 0 |
| Factory lessons | 0 |
| Factory lesson outcomes | 0 |
| Runs | 0 |
| Evidence | 0 |
| Paper outcomes | 0 |

The inactive Terra and derived-v3 databases each retain prior research with 51
candidate rows, 44 factory accounts, one cycle, 11 hypotheses, and 55 graded
lessons/outcomes. All 55 outcomes are underpowered; all 51 candidates remain in
candidate state; neither database contains an authorizing run, evidence, or
proof. Their variant identities differ from the fresh diagnostic cohort. They
must not be silently merged into the production ledger.

The repository checkout also contains a separate local file at
`runtime/research/edge_lab.sqlite3`. It is not the VM production authority and
is not synchronized as runtime evidence. At documentation time it contained
seven rows, all in `candidate` state, and zero runs, evidence, factory lessons,
or lesson outcomes. Statements in this report that the active/default ledger
is empty refer specifically to the VM production path under `/app/runtime`.

### Positive and negative live lessons

The normal paper-trial path persists paper outcomes. A failed trial demotes the
candidate and creates a failed live-paper lesson for future tuning. A passed
trial becomes operator-visible as `promotable` but does not currently create a
positive live-paper lesson. This means the durable outcome exists, but positive
trial learning is not symmetrically projected into factory memory.

## Status of the expert-review control items

| Item | Current verified state | Research conclusion |
| --- | --- | --- |
| `max_stressed_cost_to_risk_ratio` | Production `0.30`; `0.60` diagnostic only | Keep `0.30`; `0.60` admitted a losing cohort and has no authority |
| `RETIREMENT_MIN_USEFUL_R` | `0.05R` | Still configured; fixed `0.38R` width rejected; baseline dead-band unavailable |
| `_fdr_gamma` | `1 / (i * (i + 1))` telescoping allocation | Ratchet remains; no alpha floor or replacement proposed |
| `near_duplicate_distance` | `0.001` | Exact semantic aliases are removed, but the continuous near-duplicate guard remains very narrow |
| Cross-family behavioral alias | Exact fit-behavior aliases can now be collapsed across families before held-out replay | The measured 99.8711% near-alias is not exact; near-alias/dependence risk remains unresolved |
| `DEPLOYED_STATUSES` | `(VALIDATED, CHAMPION)` | Trial review only sees already proved/deployed candidates; active ledger currently has none |
| Effective shipped trial floor | 20 sessions, 20 trades, mean R > 0, total R > 0 | Current configuration differs from stale 30-session/100-trade prose and module-only fallback defaults |
| Paper export payload | Operational fields only | Cannot replay alpha: lacks symbol, side, session/signal timing, brackets, quote provenance, exits, and R |
| Corpus readiness | Protocol requires 150 offline + 60 shadow sessions | Current forward-ready count must be re-audited after provenance repair; historical backfill is non-authorizing |

### Family alias detail

The current factory has a fit-only behavioral canonicalization pass that can
collapse exact executable aliases even when the variants were authored under
different families. It operates cycle-globally before held-out replay and BH,
and deliberately retains zero-signal or incomplete-probe variants rather than
silently excluding them.

Proposal-generation semantic deduplication is still family-local. The later
fit-only realized-behavior pass is the part that can collapse exact aliases
across families. These are separate controls and should not be described as one
universal deduplication mechanism.

That resolves exact cross-family behavioral duplicates. It does not establish
that two strategies with 99.8711% directional agreement are the same exact
fingerprint. The previously measured volatility-breakout/volume-breakout pair
had 447 divergences across 346,772 real 15-bar windows. That severe near-alias
should still be handled by dependence clustering, a preregistered near-behavior
rule, or operator review rather than being counted as independent evidence.

### Trial-lane detail

`DEPLOYED_STATUSES` is intentionally limited to `validated` and `champion`.
Only those candidates are reviewed by the ordinary paper-trial evaluator. A
failed trial is parked and becomes a lesson; a passed unpinned trial becomes
operator-visible as promotable. No trial can currently run because the active
ledger contains no validated/champion candidate. Whether an earlier,
strictly-isolated research trial lane should exist is a product/protocol
decision, not evidence supplied by this counterfactual.

The effective shipped trial configuration is currently 20 sessions and 20
trades with strictly positive mean and total R required to pass. Older prose
and the module fallback used when no configuration is supplied state 30
sessions and 100 trades. The configured 20/20 policy is what actually governs
the shipped system and the documentation mismatch should be corrected before
trial activation.

### Paper-export detail

`ExecutionObservation` currently records member ID, disposition, operational
health, quantity, reference/fill price, slippage, and rejection/failure codes.
The export is intentionally an operational handoff rather than a replayable
observer/signal feed. It lacks the symbol, direction/side, session, signal and
entry timestamps, stop/target brackets, quote age/provider/feed provenance,
exit details, risk reference, and R multiple needed to replay or learn alpha.

This is safe because the export cannot accidentally claim alpha authority. It
also means it cannot satisfy the requested full learning record without a
separate, provenance-complete observation schema.

## What the experiment does and does not establish

### Established

- The `0.30` production gate is highly restrictive for this cohort.
- Relaxing only the configuration threshold to `0.60` creates materially more
  admissible signals and executions.
- The newly admitted cohort had strongly negative ordinary diagnostic replay
  outcomes.
- All 16 variants that traded were negative.
- The old per-trade sigma and trades/session assumptions do not match this
  pooled sample.
- The fixed `0.38R` interval/dead-band assumption is invalid.
- The evidence protocol contains five windows and a nominal 600-trade burden.
- The fresh diagnostic artifacts are reproducible and non-authorizing.
- The active authorizing learning and evidence ledgers are empty.

### Not established

- `0.30` does not have positive expectancy; it produced no trades.
- `0.60` is not proven universally inferior across all strategies or future
  data; it failed for this frozen cohort and corpus.
- The report is not a clean row-level causal estimate because execution paths
  diverged after admissions.
- No strategy passed fit, held-out, qualification, shadow-selection, or
  shadow-confirmation here.
- No strategy has authorizing quote-fill evidence; all 513 entries and exits
  in the alternative arm were diagnostic bar fills.
- No five-window funnel count was measured.
- No reliable baseline Section 05 interval, MDE, or dead band exists.
- The diagnostic findings are not active LLM memory.
- The current corpus does not satisfy the required forward/shadow readiness.

## Decision record

### Immediate decision

1. Keep production at `0.30`.
2. Do not promote `0.60` or any of the 44 variants.
3. Record the `0.60` arm as a rejected diagnostic policy experiment for this
   exact dataset/cohort/code identity.
4. Do not call `0.30` the winning strategy. It is the safer current gate, not a
   measured positive-return arm.
5. Do not reuse the whole-corpus outcomes as same-data LLM tuning targets.

### Recommended research/persistence work

1. Add a diagnostic experiment registry separate from the authorizing
   `FactoryLedger` and `EdgeLedger`.
2. Store source-report, dataset, cohort, code, config, and run hashes; exact
   variant IDs; hypothesis and selection rationale; per-arm outcomes; pairing
   flags; and explicit `diagnostic_only/non_authorizing` labels.
3. Make future proposal retrieval expose only preregistered fit-window lessons.
   Keep held-out, qualification, and shadow evidence sealed from proposal
   generation.
4. Preserve the counterfactual's negative result as a policy-level guardrail,
   not as alpha proof and not as permission to auto-select a threshold.
5. Re-audit recorder provenance and forward-session readiness after the
   retryable request failure; historical-backfill rows must remain excluded
   from authorization.
6. Run future candidate identities through the complete five-window funnel on
   unseen chronological data with quote-provenance-complete fills.
7. Add or define a near-behavior/dependence treatment for the 99.8711%
   cross-family alias rather than relying only on exact deduplication and a
   `0.001` semantic distance.
8. If paper epochs are expected to support replay and strategy learning, add a
   separate immutable observation record containing symbol, side, session,
   timestamps, brackets, quote provenance, exit, risk reference, and R. Keep
   the existing public operational export non-authorizing.
9. Decide explicitly whether passed live trials should create positive lessons
   as well as durable paper outcomes; preserve the anti-leakage and
   next-epoch-only boundaries.

## Validation record

The implementation and artifact were checked with:

- `python3 -m unittest -q tests.research.test_cost_counterfactual` — 10 tests
  passed;
- strict warnings on the focused test module — passed;
- Python compile checks — passed;
- `git diff --check` — passed;
- strict finite JSON parse — passed;
- recomputed final `content_hash` — exact match;
- source-report contract and dataset binding — passed;
- exact two-arm/config-path check — passed;
- 44,352-row complete pairing — passed;
- production mutation/proof/promotion checks — passed;
- direct causal isolation — failed honestly due path dependence.

## Final conclusion

The study answered the reachability question and rejected the proposed
production relaxation for this cohort. `0.60` admitted 513 trades, but every
variant that traded had negative mean R and the pooled result was
`-0.541550R`. `0.30` remains the correct production setting because it is the
current conservative authorization policy and because the proposed relaxation
failed—not because `0.30` demonstrated a profitable edge.

The more important system finding is architectural: the research artifacts are
saved with strong provenance, but the active future-learning ledger is empty.
The next step is not to teach the LLM directly from this whole-corpus result.
It is to preserve the result in a non-authorizing diagnostic registry, repair
forward-corpus readiness, and let only properly partitioned fit lessons plus
unseen qualification/shadow evidence participate in future edge formation.
