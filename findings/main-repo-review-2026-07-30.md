# Main repository review — 2026-07-30

Reviewed at `4791dda` against four requirements:

1. finding an edge, automatically documenting it, and locking it as a strategy;
2. no overfitting;
3. no over-engineering;
4. different hypotheses, and different variants of each.

Every claim below was checked against the code or reproduced. The test suite
was run before anything was changed and passed clean — **832 passed, 1 skipped,
56 subtests** on Python 3.12 — and passes at **839 passed, 1 skipped, 105
subtests** with the fixes below. Items marked **Fixed** were repaired in this
branch and carry a regression test that was confirmed to fail without the fix;
items marked **Recommended** are not implemented here, and the reason is given.

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

**The anti-overfitting machinery guards the path with no data and not the path
that produced every published verdict.** `research/edge_lab.py` and its eight
dependents have zero test coverage. Every committed tier — including three
`T0_REJECTED` verdicts — rests on that engine.

**Variants exist for exactly one strategy.** All 16 entries in
`research/variants.yaml` are `strategy_id: momentum`. The other six hypotheses
are each scored at one hard-coded parameterisation, and the tournament will
award `T0_REJECTED` from it. This contradicts the repository's own rule —
`MIN_AXIS_SETTINGS = 3`, "the rule that stops a good idea being killed by one
badly chosen value" — which governs only the momentum path. `agent/registry.py`
states the consequence out loud for `flush-fade`: *"The mechanism remains
plausible; this parameterisation of it does not survive."* The framework knows
the distinction and the scoring path cannot make it. This is the highest-value
change available and it is requirement 4's core.

---

## 1. Finding an edge, documenting it, locking it as a strategy

### What is built

The pipeline exists end to end and is unusually well sequenced:

| Stage | Mechanism |
| --- | --- |
| Fidelity gate | `research.py replay --check-fidelity` (G2) must reproduce the agent's own recorded decisions; result journalled as `research_gate_result`; `sweep`, `funnel` and `three-arm` all refuse without a current PASS |
| Discovery | `protocol.evaluate_axis` — selection on the fit window only, paired proposal identity, cluster-block bootstrap, verdict names its governing criterion |
| Documentation | `findings.db`, append-only with history triggers, schema 7; `write_scorecards` regenerates one markdown card per variant into `findings/` |
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

### R1-04 — the packet-to-register lock is not machine-checkable · **Fixed**

`StrategySpec.evidence` is a free-form tuple of paths. Nothing requires a spec
at `T3_VALIDATED` or above to cite the SHA-256 of the packet that authorised
it, so the content-addressed packet and the tier it exists to justify are
joined only by a reviewer's diligence. `test_nothing_is_live_eligible_yet` is
the sole tripwire, and someone raising a tier edits it in the same commit.

Recommended: require any spec at `≥ T3_VALIDATED` to carry a
`t3-packet:<sha256>` reference in `evidence`, validated in `__post_init__`, with
a test resolving it against `t3_evidence_packets`. Precedent exists —
`test_every_validated_model_cites_evidence_that_exists` already does this for
forward models. Not implemented here because it defines what counts as
authority for live capital, which is the repository owner's call, not a
reviewer's.

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

Recommended: record the axis count in the qualification payload, apply Holm
across the axes evaluated in one `forward-qualify` run, and state in
`protocol.md` that criterion 5 is a selection screen while criterion 9 is the
test. Not implemented here: `protocol.md` is explicit that the promotion rule
is fixed in advance and applied mechanically, so changing it is a deliberate
reviewed act and not something a review should do to itself.

### R2-02 — the exploratory engine that produced every verdict is untested · **Recommended**

Test references by research module: `replay` 30, `corpus` 30, `findings` 12,
`gates` 12, `score` 12, `stats` 12 — and **`edge_lab` 0**, alongside
`edge_report` 0, `signal_lab` 0, `find_edge` 0, `deep_edge` 0,
`validate_candidate` 0, `unbiased_recheck` 0, `portfolio_sim` 0,
`validate_features` 0.

`tests/test_gates.py` is explicit that "the gates themselves are
integration-tested by running the tournament against real data", and that data
lives under gitignored `runtime/`. So nothing in the 839-test suite exercises
the 1,170-line feature/signal/simulation engine on which
`research/results/edge-audit-2024-2026`, the tournament leaderboard, and three
`T0_REJECTED` tiers all rest. `validate_features.py` is the only cross-check and
it also cannot run in CI, so its clean result survives as prose in
`research/README.md` rather than as a check.

The asymmetry is exactly backwards from where the risk sits. The authoritative
path — which has no corpus yet — has `test_no_lookahead.py`,
`test_replay_determinism.py`, `test_replay_fidelity.py` and a hard G2 gate. The
exploratory path, which has two years of data and has already issued every
published verdict, has none. `agent/registry.py` argues the bias runs against
the strategy so `T0_REJECTED` is the conservative reading; that argument
protects against wrongly *granting* capital and says nothing about a silent
index shift wrongly rejecting a mechanism, which is the failure requirement 4
also cares about.

Recommended: commit a small fixture dataset — 2-3 symbols, a few hundred bars,
a few KB — and add two tests against it: no-lookahead (every feature at bar *i*
uses only bars ≤ *i*) and reproduction (a frozen expected summary for one
contract). Mirror `tests/research/test_no_lookahead.py`. This is a few hundred
lines and it is the cheapest large reduction in risk available in this
repository.

### R2-03 — exploratory evidence changed the shipped configuration · **Recommended**

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

None of this is hidden and each value has a stated reason, so this is a
consistency gap rather than a defect. Recommended: extend the reconciliation
rule to configuration in one paragraph — exploratory evidence may set a shipped
default, and when it does the baseline is a fitted point and the fit window is
recorded beside it — or hold the defaults at their unfitted values until the
journal path confirms them. The first option is probably right; the point is
that the choice should be written down where the tier rule is.

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
reports are already committed and whose conclusions are settled: `find_edge`,
`deep_edge`, `unbiased_recheck`, `maker_study`, `selection_study`,
`analyse_flow`, `make_legacy_dataset`, `phase1_v2_backtest`, `signal_lab`,
`portfolio_sim`. They all import `edge_lab`, so each is a surface that has to
keep importing correctly forever, and nothing distinguishes them from
`replay.py` or `protocol.py`, which must keep working.

`RECONCILIATION.md` already weighed deleting them and kept them for good
reasons — a deleted rejection is how a bad idea comes back. The cheaper third
option: move them under `research/studies/`, or give each a header line naming
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

### R4-01 — six of seven hypotheses have no variants at all · **Recommended (highest value)**

Every entry in `research/variants.yaml` is `strategy_id: momentum`. For every
other hypothesis, `tournament.py` constructs the contract at one hard-coded
point:

```python
CONTRACTS = {
    "flush-fade":     lambda cfg: FlushFadeContract(),
    "trend-multiday": lambda cfg: TrendMultidayContract(),
    "funding-carry":  lambda cfg: FundingCarryContract(),
    ...
}
```

Those are dataclass defaults — `FlushFadeContract` fires at `min_move_atr=1.5`,
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
than that one guess was. Until it exists, requirement 4 is met for momentum and
for the hypothesis *layer*, and not met for the variant layer of any hypothesis
that is not momentum — which is where the search for an edge actually is, since
momentum is retained explicitly as the benchmark null.

Not implemented here because it changes what a published tier means, needs the
downloaded dataset to re-score, and would rewrite committed results — a change
to make deliberately, with the re-scored reports reviewed.

### R4-02 — the in-prompt hypotheses cannot be swept · **Recommended**

`agent/hypotheses.py` hard-codes its thresholds inside the contract functions:
`relative_volume < 1.5`, `oi_change_4h_pct < -1.0`, basis `±0.05` with funding
percentile 80/20. They are absent from `variants.yaml`, so they inherit R4-01's
single-point problem and additionally cannot be varied at all. Each falsifier
is phrased "…does not exceed the unconditional expectancy by more than the MDE
at n >= 100", which is a detectability bar, not a second setting.

Recommended: move those constants into `contract_params`, the mechanism
`agent/registry.py` already uses to give a non-active strategy a stated
parameter set under shadow, so each hypothesis can carry 2-3 settings on the
same footing as a momentum axis.

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
| R1-04 | 1 | P2 | Recommended — tie a `≥T3` tier to a packet hash |
| R2-01 | 2 | P1 | Recommended — Holm across axes in `forward-qualify` |
| R2-02 | 2 | P1 | Recommended — fixture dataset + lookahead test for `edge_lab` |
| R2-03 | 2 | P2 | Recommended — extend the reconciliation rule to config values |
| R3-01 | 3 | P2 | Recommended — separate one-shot studies from infrastructure |
| R3-02 | 3 | P2 | Recommended — split `engine.py` when next touched |
| R3-03 | 3 | P3 | Recommended — stop forcing README/SETUP duplication |
| R4-01 | 4 | P0 | Recommended — per-hypothesis setting axes in the tournament |
| R4-02 | 4 | P2 | Recommended — move in-prompt thresholds into `contract_params` |
| R4-03 | 4 | — | Observation, no action |

Suggested order: R4-01 and R2-02 together, because the first re-scores every
non-momentum hypothesis and the second is what makes those scores trustworthy.
R1-04 next, since it is small and it closes the last unenforced link in the
"lock it as a strategy" chain.
