# Main repository review — 2026-07-30

> **ARCHIVED / FROZEN REVIEW SNAPSHOT.** This document preserves the code and
> evidence state observed on July 30, 2026. Review labels, gate names,
> schema/feed claims, and follow-up items below are not a current checklist.
> Use `README.md`, `OPERATIONS.md`, and
> `research/AUTONOMOUS_RESEARCH.md` for current behavior.

> Preserved detailed review record. Current implementation status and authority
> boundaries are maintained in
> [`../research/AUTONOMOUS_RESEARCH.md`](../research/AUTONOMOUS_RESEARCH.md);
> this document retains the historical findings and reasoning.

Reviewed at `4791dda` against four requirements. The status notes below also
include subsequent changes now present on `main`.

1. finding an edge, automatically documenting it, and locking it as a strategy;
2. no overfitting;
3. no over-engineering;
4. different hypotheses, and different variants of each.

Every claim below was checked against the code or reproduced. The numeric test
counts in the original review are historical snapshots, not current status;
they are intentionally not repeated because later uncommitted implementation
and test changes are present in this worktree. Items marked **Fixed** were
repaired in this branch and carry a regression test; items marked
**Recommended** are not implemented here, and the reason is given.

### Current implementation reconciliation

Later implementation now connects hypothesis variants to the prompt/parser,
materializes first-class `hypothesis_id` and parameter metadata, and supports
adaptive proposal selection. `FindingsStore` persists proposal history,
setting locks, variant metadata, and forward-qualification linkage. Shadow
evaluation uses bounded workers and isolated per-variant paper state, and is
opt-in through `research:` configuration. The remaining manual boundary is
deliberate: final strategy/variant-ID changes and reviewed promotion are still
required; evidence does not edit the live registry or make a VM import path a
production/default path.

---

## Executive result

The evidence machinery is the strongest part of this repository and it is
better than most things of its kind. `research/protocol.py` and
`research/gates.py` encode failure modes that are usually discovered the
expensive way: a clustered bootstrap whose width collapses to zero on one
market episode, a placebo that scores 41% of the "real" result because pooled
decile thresholds were selecting bullish moments, an out-of-sample split that
is a regime test wearing a robustness label. The register refuses to let a
strategy reach capital on the evidence path that cannot support it, and
`funding-carry` was rejected while posting **+2.008% per trade** because
attribution showed the carry mechanism contributed 2% of it. Throwing away a
positive result for the right reason is the hardest thing on this list, and it
is already in the history.

Against that, three gaps matter.

**The automatic documentation loop was broken, not merely stale.** An in-place
edit to a registered variant's hypothesis text made `register()` raise, which
aborts `research.py report`, `forward-qualify` and `t3-packet` on any host that
already held the old row — and under `nightly.sh`'s `set -e` it killed the
whole nightly cycle at the scorecard step, while `forward-qualify`'s error
handler reported the failure as "no promotable edge". Reproduced, fixed, and
now caught at review time.

**The anti-overfitting machinery has two evidence paths with different
authority.** The exploratory `research/edge_lab.py` path now has focused
fixture coverage; the authoritative journal-replay path remains the source
for promotion. Published exploratory verdicts remain historical evidence, not
automatic promotion authority.

**Variant coverage is now connected at the runtime boundary.** In-prompt
hypotheses materialize deterministic, named variants and adaptive proposals
select only registered bounded settings. The old one-strategy description is
historical context for the original review, not current implementation status.

---

## 1. Finding an edge, documenting it, locking it as a strategy

### What is built

The pipeline exists end to end and is unusually well sequenced:

| Stage | Mechanism |
| --- | --- |
| Fidelity gate | `research.py replay --check-fidelity` (G2) must reproduce the agent's own recorded decisions; result journalled as `research_gate_result`; `sweep`, `funnel` and `three-arm` all refuse without a current PASS |
| Discovery | `protocol.evaluate_axis` — selection on the fit window only, paired proposal identity, cluster-block bootstrap, verdict names its governing criterion |
| Documentation | At review time, `findings.db` used the then-current findings schema and `write_scorecards` regenerated one markdown card per variant into `findings/` |
| Forward confirmation | `forward-qualify` re-derives the verdict from the immutable decision ledger, proving one strategy version, one declared axis, and identical non-axis config |
| Paper | `qualify_variant` → `PAPER_PENDING` while a shadow position is open → clean rebased `PAPER` account when flat |
| Lock | `t3-packet` builds a SHA-256 content-addressed packet, `DRAFT_REVIEW_REQUIRED` unless the full checklist passes **and** a reviewer plus a registry-change reference are supplied |
| Automation | `research/nightly.sh` drives all of it, authoritative path first, G2 as a hard stop |

Design decisions worth keeping exactly as they are: a null result is a row with
the same weight as a positive one; `INSUFFICIENT_SAMPLE` means the question is
open rather than answered; a veto is recorded as an explicit 0R paired action,
without which no confidence or exposure axis could ever demonstrate anything;
and paper success does not edit the register — live authority stays a
deliberate reviewed code change. That last one is the correct reading of "lock
it as a strategy". Automatic promotion to live capital would be the wrong
feature.

### R1-01 — the documentation loop aborted on an edited claim · **Fixed**

`FindingsStore._variant_identity` includes `hypothesis`, so a variant's claim is
part of its immutable experiment identity. `research/variants.yaml` had the
hypothesis text of `momentum.rr.fixed_2_5` and `momentum.rr.fixed_3_0` rewritten
in place in `f87db6d`. Reproduced against a store holding the pre-`f87db6d`
rows:

```
CRASH on momentum.rr.fixed_2_5 -> registered experiment identity is immutable;
         use a new variant_id for changes to strategy, version, overrides, or hypothesis
CRASH on momentum.rr.fixed_3_0 -> registered experiment identity is immutable; ...
```

`cmd_report`, `cmd_forward_qualify` and `cmd_t3_packet` each loop
`store.register(variant)` over the whole registry with no handling, so all three
abort. In `nightly.sh` the consequences differ and both are bad: `research.py
report` is unguarded under `set -e`, so the run dies at "regenerating
scorecards" and the entire exploratory path below it never executes;
`forward-qualify` is wrapped in `|| echo "(collecting, unscoped, or no
promotable edge; see above)"`, so **the edge-qualification step reports a
plausible non-result while actually being broken**. That is the worst available
failure shape for requirement 1.

Fixed by restoring both registered claims and recording the correction as a
YAML comment, which is not part of identity. `momentum.rr.fixed_3_0` keeps its
`superseded` status — status is the one mutable field, and the reason it was
superseded belongs in a comment, not in a rewritten claim.

The rule itself is right and was not weakened. Restating a claim means a new
`variant_id`; a claim that can be reworded after the fact cannot be told apart
from one retro-fitted to the result.

### R1-02 — the nightly report deleted the link to the previous audit · **Fixed**

`index()` rewrote `findings/README.md` from the store alone. The hand-written
`## Repository audits` section — the only link to
`findings/orchestrated-audit-2026-07-29.md` — was therefore destroyed by the
first `research.py report` run after it was added. The file survived on disk
and became unreachable, and nothing errored.

`index()` now discovers documents from disk, so the section is generated and
cannot be orphaned. A generator may overwrite what it wrote; it may not
silently orphan what it did not. This review document is linked by that
mechanism.

### R1-03 — the committed scorecards had drifted · **Fixed**

8 of 16 registered variants had no committed scorecard and 2 were stale, which
is the visible symptom of R1-01. All 16 are now committed, with registration
dates recovered from the commit that introduced each entry so the index reports
when each variant was actually registered.

Three tests now hold the line at review time, where fixing this is free: every
registered variant has a committed card; each card's `Hypothesis`, `Status` and
`Overrides` match the registry; the index links every card and every document
beside it. All were confirmed to fail when the defects are reintroduced.

### R1-04 — the packet-to-register lock is not machine-checkable · **Partially implemented**

`StrategySpec.evidence` is a free-form tuple of paths. Nothing requires a spec
at `T3_VALIDATED` or above to cite the SHA-256 of the packet that authorised
it, so the content-addressed packet and the tier it exists to justify are
joined only by a reviewer's diligence. `test_nothing_is_live_eligible_yet` is
the sole tripwire, and someone raising a tier edits it in the same commit.

Current `main` now requires the `t3-packet:<sha256>` shape for tiers at or above
`T3_VALIDATED`, and the qualification/T3 path validates persisted family
evidence. The remaining gap is that the registry guard does not resolve the
cited hash against `t3_evidence_packets` and confirm that the packet belongs to
the strategy.

---

## 2. No overfitting

This is the requirement the repository serves best. Recorded so it is not lost
in the findings below:

- **Selection and confirmation are separated properly.** One chronological
  cutoff derived from the baseline proposal calendar and applied to every arm,
  so a dense or delayed candidate cannot move the date and draw a different
  regime (`protocol.py`, `common_time_cutoff`).
- **The cluster count, not the trade count, governs precision.**
  `MIN_BOOTSTRAP_CLUSTERS = 8` with the reasoning written down: a hundred pairs
  packed into one afternoon resample to themselves, the percentiles collapse
  onto the point estimate, and a zero-width interval clears any "entirely
  positive" test. A collapsed interval is rejected outright rather than read as
  certainty. Few systems get this right.
- **The noise floor is measured, not assumed.** `gates.py` refuses to test any
  t-statistic because a placebo reached t=2.60 on pure noise on this data.
- **Purged walk-forward**, `PURGE_DAYS = 3`, so a position opened in-sample
  cannot close out-of-sample.
- **Underpowered grids are refused before they run**, so the verdict is stated
  once instead of once per grid point — which is what makes the threshold
  survivable.
- **Provenance is machine-checkable.** `funding-unwind` carries
  `in_sample_by_construction: true` and the tournament caps it at T1 however
  clean the gates come back.
- **`funding-carry` was rejected at +2.008% per trade** because attribution
  showed funding contributed +0.039% and price +1.969%. The residual was sent
  back for its own pre-registration instead of being folded in.

### R2-01 — no family-wise correction in the path that qualifies an edge · **Fixed**

`protocol.correct_family` is applied in exactly one place: the conditioning
branch of `cmd_sweep`. `cmd_forward_qualify` iterates axes independently, and
each axis can reach `PROMOTE` and call `qualify_variant` on its own. Five
parameter axes are registered today (reward/risk, stop width, net direction,
confidence floor, breakout discriminator) and the count grows with the
registry. `protocol.md` states that Holm–Bonferroni is applied "across each
batch" and that "the corrected figure is the only one any recommendation may
quote" — the promotion path quotes none.

The effective false-positive rate is currently small, and the reason is worth
being precise about rather than reassured by: promotion requires the fit
interval *and* the held-out confirmation interval to be entirely positive, and
the confirmation window carries almost all of that protection. The fit-window
criterion is evaluated on the same data used to select the winning setting
(`max` over settings by fit expectancy, then "the entire fit interval must
clear zero"), so it is inflated by the selection and is a screen rather than a
test. Nothing is wrong with using it as a screen; the documentation should stop
implying it is independent evidence.

Current `main` applies the correction across all axes evaluated by
`forward-qualify`, persists a `forward_axis_family` record, and requires that
complete persisted family to survive again at qualification and T3 packet
creation. The promotion rule remains manual and review-gated.

### R2-02 — exploratory evidence needs continuing fixture coverage · **Partially implemented**

The current research suite includes focused deterministic coverage for
`edge_lab`, hypothesis variants, findings-store metadata, and bounded shadow
evaluation. The remaining exploratory studies are still largely report-driven
and should not be mistaken for continuously maintained production modules.

`tests/test_gates.py` is explicit that "the gates themselves are
integration-tested by running the tournament against real data", and that data
lives under gitignored `runtime/`. The 1,170-line
feature/signal/simulation engine on which
`research/results/edge-audit-2024-2026`, the tournament leaderboard, and three
`T0_REJECTED` tiers all rest. `validate_features.py` is the only cross-check and
it also cannot run in CI, so its clean result survives as prose in
`research/README.md` rather than as a check.

The authoritative path has `test_no_lookahead.py`,
`test_replay_determinism.py`, `test_replay_fidelity.py` and a hard G2 gate. The
exploratory reports remain historical and are not a substitute for journal
replay. `agent/registry.py` argues the bias runs against
the strategy so `T0_REJECTED` is the conservative reading; that argument
protects against wrongly *granting* capital and says nothing about a silent
index shift wrongly rejecting a mechanism, which is the failure requirement 4
also cares about.

The remaining recommendation is to extend that fixture coverage only to the
highest-risk exploratory modules that materially feed published verdicts; do
not turn every one-shot study into a permanent test surface.

### R2-03 — exploratory evidence changed the shipped configuration · **Implemented**

`RECONCILIATION.md` draws its rule about **tiers**: lowered on exploratory
evidence, raised only on journal replay. Configuration is not covered, and
three shipped values were selected by the exploratory search:
`fixed_reward_risk: 3.0`, `max_hold_hours: 48` (README: "a 48h maximum hold beat
24h in 8/8 matched walk-forward cells, and a 3R target beat 2R in 4/4 at that
hold. Both are applied in `config.yaml`"), and
`hard_max_entry_extension_atr: 1.2` from the 64-rule selector study.

So exploratory evidence cannot raise a tier but can change what the agent does.
The consequence is already visible in the registry: `momentum.baseline` — the
comparison floor for every variant, and the arm G2 must reproduce — is itself a
fitted point, and `momentum.rr.fixed_3_0` had to be marked `superseded` because
the search moved the baseline onto it.

None of this is hidden and each value has a stated reason. The reconciliation
rule now covers configuration as well as tiers: exploratory evidence may set a
shipped default, and when it does the baseline is explicitly a fitted point.
The fit window, corpus provenance, selection rule, and configuration/code
fingerprints must be recorded beside it. This still does not allow exploratory
evidence to raise a tier; journal replay and forward confirmation remain
authoritative for promotion.

---

## 3. No over-engineering

Roughly 27,900 lines of Python outside tests, 13,200 lines of tests, and 8,700
lines of markdown, for a system that holds no live capital and has found no
edge. That ratio invites the question, and mostly it answers it well: the
safety machinery (transfer identity, position reconciliation, liquidation-stop
checks, circuit breakers) is what makes an autonomous demo agent trustworthy
enough to be worth collecting data from, and the research machinery is not
overhead on the product — it *is* the product. Two-thirds of `research/` earns
its keep on that basis, and the deliberate two-engine design (`edge_lab`
independently written, `validate_features` proving it reproduces the live
modules) is a legitimate use of duplication rather than an accident.

Three excesses are real.

### R3-01 — permanent infrastructure and one-shot studies are indistinguishable · **Recommended**

`research/` holds 30 modules. Roughly half are single-use lab notebooks whose
reports are already committed and whose conclusions are settled. The retained
cluster is now explicitly named under `research/legacy/`: `find_edge`,
`deep_edge`, `edge_report`, `fetch_flow_data`,
`make_legacy_dataset`, `phase1_v2_backtest`, `portfolio_sim`,
`selection_study`, `unbiased_recheck`, and `validate_candidate`; the
load-bearing `maker_study` and shared `signal_lab` remain at their original
paths. The legacy programs import `edge_lab`, so each is a surface that has to
keep importing correctly forever, and nothing distinguishes them from
`replay.py` or `protocol.py`, which must keep working.

`RECONCILIATION.md` already weighed deleting them and kept them for good
reasons — a deleted rejection is how a bad idea comes back. The cheaper third
option: move them under `research/legacy/`, or give each a header line naming
the report it produced and the date it was last meaningful. A reader can then
tell what must keep working from what already ran.

### R3-02 — `agent/engine.py` is the one file with nowhere to put the next feature · **Recommended**

2,688 lines and about 40 methods spanning cycle orchestration, position
reconciliation, maker-first execution, entry settlement, shadow dispatch,
journalling and portfolio construction. `_reconcile_positions` runs ~295 lines
and `_settle_entry` ~250. It is well tested and I found no defect in it; the
cost is that the next change has no obvious home and every change to it touches
the file that also owns order execution.

Recommended: extract execution-and-settlement and reconciliation as the two
natural seams, **when they are next touched anyway**. A standalone refactor of
working, well-tested trading code is not worth the risk it introduces.

### R3-03 — README and SETUP must repeat each other to pass their own tests · **Recommended (low)**

`test_docs_are_current.py` asserts several facts against `README` *and* `SETUP`
separately (shipped LLM provider and model, the readiness command, G2, B7.5),
so those facts must be stated twice in 63 KB + 46 KB of overlapping
documentation. The drift protection is valuable and worth its cost; the
duplication it forces is not. Recommended: keep the per-document assertions
only where a reader of that document alone would be stuck without the fact,
and let the rest be checked against `BOTH`.

---

## 4. Different hypotheses, and different variants of each

### Hypotheses: genuinely broad, and each states its payer

Seven registered strategies, each with a mechanism naming who loses the money
and why they cannot stop, and a falsification agreed before the data:

| Strategy | Return source | Tier |
| --- | --- | --- |
| `momentum` | none established; retained as the benchmark null | T0_REJECTED |
| `flush-fade` | forced liquidation flow is price-insensitive and overshoots | T0_REJECTED |
| `funding-carry` | the carry itself | T0_REJECTED (mechanism falsified) |
| `funding-unwind` | crowded positioning unwinding; carry incidental | T1_HYPOTHESIS |
| `trend-multiday` | multi-week trend persistence, cost falls to ~1% of the move | T1_HYPOTHESIS |
| `ls-ratio-fade` | retail long/short positioning, per-instrument demeaned | T1_HYPOTHESIS |
| `scalp-maker` | spread capture; no direction forecast required | T1_HYPOTHESIS |

Plus three registered in-prompt experimental hypotheses replacing the
unlabelled `other` bucket (`volume-thrust`, `oi-divergence`, `basis-stretch`),
and four pre-registered conditioning axes under `research/sweeps/`. The
economic sources are actually different from one another — liquidation flow,
carry, positioning, horizon economics, spread capture — rather than five
variations on trend. `funding-carry` versus `funding-unwind` sharing one entry
rule under opposite mechanism claims, with `hypotheses_tested: 2` recording
that the entry has been looked at twice, is a good pattern.

The best instance of requirement 4 done right is `momentum.discriminator.*`:
two registered variants for an argument about which variable separates breakout
from continuation, so the corpus decides instead of the argument.

### R4-01 — at review time, most registered hypotheses had no variants · **Partially implemented then**

> Historical finding: this conclusion describes the July 28 pre-settings
> tournament output; the checked-in July 28 report and leaderboard are
> historical/pre-settings evidence, not current results.

Current `main` now declares three pre-registered settings for each of the five
backtestable non-momentum strategies and the tournament generator scores each
setting. The historical tournament output has not been regenerated, so the
existing T0/T1/T2 conclusions remain single-point historical evidence until a
current corpus is scored.

Every entry in `research/variants.yaml` is still `strategy_id: momentum`; the
non-momentum settings live in the hypothesis pre-registrations rather than in
that shadow-variant registry. Before the current changes, `tournament.py`
constructed each non-momentum contract at one hard-coded point:

```python
CONTRACTS = {
    "flush-fade":     lambda cfg: FlushFadeContract(),
    "trend-multiday": lambda cfg: TrendMultidayContract(),
    "funding-carry":  lambda cfg: FundingCarryContract(),
    ...
}
```

The historical report therefore reflects dataclass defaults — `FlushFadeContract` fires at `min_move_atr=1.5`,
`min_oi_drop_pct=1.0`, `min_relative_volume=1.2` and nothing else is ever
tried. `score_strategy` then passes the gates to `tier_from_gates`, where a
failed `beat_nulls` returns `T0_REJECTED` directly. `flush-fade` and
`funding-carry` were each rejected from a single point in their parameter
space.

The repository's own rule forbids this. `protocol.md` criterion 1 requires "at
least three settings including the explicit baseline… the rule that stops a
good idea being killed by one badly chosen value", and `MIN_AXIS_SETTINGS = 3`
enforces it — in `protocol.evaluate_axis`, which only the momentum journal
path reaches. The registry note for `flush-fade` describes the exact damage:
*"The mechanism remains plausible; this parameterisation of it does not
survive."* One sentence later it records that OKX serves only ~60 days of open
interest, so this is an underpowered test of one guess at three thresholds,
and its verdict is stored as a tier.

Recommended, and cheap because the pieces already exist:

1. Let `research/hypotheses/<id>.yaml` declare a `settings:` list — 2-3
   alternatives for each sensitive threshold. The file already lists every
   threshold and already carries `hypotheses_tested` for multiplicity
   accounting, so this adds a pre-registration field rather than a mechanism.
2. Have `tournament.py` build one contract per setting (they are dataclasses,
   so a setting is a kwargs dict) and score each.
3. Route the per-setting summaries through the existing
   `protocol.evaluate_axis` and `correct_family`, and let `hypotheses_tested`
   absorb the added multiplicity.

Then `T0_REJECTED` for a non-momentum hypothesis means the axis is bad rather
than that one guess was. The connected variant layer now satisfies the
implementation requirement; qualification and promotion still depend on
evidence and review for each variant.

The remaining work is the deliberate re-score and review of the committed
results; no current tier should be inferred from the new settings until that
run exists.

The authoritative corpus is being collected on the user's VM by
`research/nightly.sh`. This checkout does not contain that VM's ignored
`runtime/` data, so the local absence is a mount/location issue rather than a
claim that collection has not started. Before the re-score, export or mount
the VM journal, price cache, and corpus manifest and record their window and
fingerprints in the run output.

### R4-02 — the in-prompt hypotheses cannot be swept · **Implemented**

Previously, `agent/hypotheses.py` hard-coded its thresholds inside the contract
functions: `relative_volume < 1.5`, `oi_change_4h_pct < -1.0`, basis `±0.05`
with funding percentile 80/20. They were absent from the setting mechanism, so
they inherited R4-01's single-point problem and could not be varied at all.
Each falsifier is phrased as a detectability bar, not as a second setting.

The three hypothesis contracts now carry explicit `contract_params` for their
registered point and three pre-registered settings each. The contract evaluator
accepts a setting override, so shadow/research callers can test the same named
hypothesis at multiple thresholds without changing the production baseline.

### R4-03 — no axis tests an entry threshold · **Observation, no action**

All five registered parameter axes are exit, risk or policy axes; none moves a
contract entry threshold. Given the funnel finding that the binding veto sits
downstream of the strategy contract — so no contract parameter can change the
trade count — that ordering is deliberate and correct under action-plan C5.
Recorded here so it does not read as an oversight to the next reviewer.

---

## Disposition summary

| ID | Requirement | Severity | Status |
| --- | --- | --- | --- |
| R1-01 | 1 | P0 | **Fixed** — registered claims restored; identity-drift tests added |
| R1-02 | 1 | P1 | **Fixed** — the index can no longer orphan a document |
| R1-03 | 1 | P1 | **Fixed** — all 16 scorecards committed and enforced |
| R1-04 | 1 | P2 | **Partially implemented** — packet hash shape is enforced; packet resolution remains |
| R2-01 | 2 | P1 | **Fixed** — family correction is applied and enforced at qualification/T3 |
| R2-02 | 2 | P1 | **Partially implemented** — `edge_lab` fixture coverage exists; other studies remain |
| R2-03 | 2 | P2 | **Implemented** — exploratory configuration is recorded as a fitted baseline |
| R3-01 | 3 | P2 | Recommended — separate one-shot studies from infrastructure |
| R3-02 | 3 | P2 | Recommended — split `engine.py` when next touched |
| R3-03 | 3 | P3 | Recommended — stop forcing README/SETUP duplication |
| R4-01 | 4 | P0 | **Partially implemented** — settings are registered and scored by code; results need re-scoring |
| R4-02 | 4 | P2 | **Implemented** — hypothesis thresholds have contract params and settings |
| R4-03 | 4 | — | Observation, no action |

Suggested order: R4-01 and R2-02 together, because the first re-scores every
non-momentum hypothesis and the second is what makes those scores trustworthy.
R1-04 next, since it is small and it closes the last unenforced link in the
"lock it as a strategy" chain.
