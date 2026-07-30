# B6 — acting on the rejected hypotheses

The first batch that changes trading behaviour. Everything before it was
additive; this one alters what the agent will and will not do.

## What shipped

**6.1 — `structure_target` deleted.** It computed
`max(stop_pct * fixed_rr, distance_to_recent_extreme)`, and in both setups it
was designed for, the first term always won. A `range_breakout` fires at
`range_pos_pct >= 85`, so price is already at the highs and the remaining
distance is near zero. A `trend_continuation` pullback sits 1–2% from the
swing while `stop_pct * fixed_rr` is around 4%. The policy was therefore
identical to `fixed_rr` precisely where it was supposed to differ.

Deleting is the honest default. An inert choice in the model's option set is
worse than no choice: it makes the decision space look richer than it is, and
it attributes outcomes to a policy that never actually applied.

**6.2 — `funding_squeeze` now requires a structure invalidation (defect
D3).** It was the only setup permitted the ATR anchor, which yields exactly
`min_stop_atr_multiple` — the narrowest stop the system can produce — for a
counter-trend, mean-reversion entry into a crowded book. Fading a crowd needs
more room than following one, not less.

The rest of 6.2 was already in place before this batch: the contract already
thresholds on `funding_percentile_30` rather than a raw rate, already
requires `funding_samples_30 >= 10`, and already demands the price and trend
confirmation the prompt promises (`trend_1h != "up"`,
`price_stabilized_short`). Only the anchor requirement was missing.

**6.3 — `allow_experimental_setups_in_demo: false`, as an interim measure.**
`other` bypasses the evidence contract entirely and pools every distinct
experimental idea under one label, so `report.py` groups them into a single
row and nothing can be learned from any of them. It is also demo-only, which
means demo statistics are contaminated by a population of trades live will
silently refuse.

The full replacement — a registered `hypothesis_id` chosen from an explicit
versioned list, each with its own contract and its own attribution — changes
the prompt, so it belongs with the 6.5 version bump rather than here. Until
then the flag is off, so demo statistics mean something.

## What did not ship, and why

**6.4 is gated on H-I and did not ship.** It proposes separating
`range_breakout` from `trend_continuation` by requiring a breakout to occur
without prior multi-timeframe alignment. The diagnosis is right — the two
contracts overlap and attribution is splitting one phenomenon — but the
proposed discriminator may be the wrong variable. If H-I holds, the real
partition is volatility regime, and trend alignment is a correlated proxy for
it. Shipping 6.4 first would bake the proxy into the contract and filter the
population on a correlated variable, which makes the regime test impossible
to run cleanly afterwards.

Gate G5: 6.4 cannot merge until `research.py sweep
research/sweeps/regime_conditioning.yaml` returns a result or
`INSUFFICIENT_SAMPLE`. The pre-registration is committed.

**6.5's version bump did not ship**, because it waits on 6.4 so the
`strategy.version` attribution forks once rather than twice.

## The attribution fork this batch does cause

`prompt_version` moved `b9a09a9dc3bc59ec -> 8d99182f0dcea1c4`. Removing
`structure_target` from the model's option set and tightening the
invalidation-anchor rule both change the system prompt, and the prompt
version is derived from its text.

This is deliberate and it is recorded here rather than discovered later.
Pre-6.1 and post-6.1 observations describe different decision spaces and
**must never be pooled**. The pinned constant in
`tests/research/test_enrichment_isolation.py` was updated in this batch,
which is the only circumstance in which it may move.

## The gate: replay diff before merging

The batch's own acceptance criterion is to replay both contracts over the
same corpus and publish the difference before merging.

Run against a 400-cycle demonstration corpus — **not a real journal, because
this container has none** — the post-6.1 contract introduces zero new vetoes:

```
post-6.1  fired 954  vetoed 941  executed 13

new vetoes introduced by this batch:
  requires a structure invalidation : 0
  exit policy is not recognised     : 0
```

That result is honest but weak, and its weakness should be stated plainly:
the demonstration corpus's recorded decisions all used `structure` anchors
and `fixed_rr` exits, so neither new rule had anything to bite on. The diff
proves the changes are not gratuitously destructive; it does **not** prove
they are inconsequential on real data, where `atr`-anchored `funding_squeeze`
decisions and `structure_target` exits may well appear.

**Re-run this diff against the real journal before trusting the batch**, with
`research.py replay --check-fidelity` first so gate G2 is satisfied on the
same corpus.

## Tests

`tests/test_strategy.py` gained
`test_funding_squeeze_requires_a_structure_invalidation`. One existing test
was corrected rather than weakened: it built a `funding_squeeze` decision
with `invalidation_anchor="atr"`, which is now itself disqualifying, so the
fixture no longer reached the evidence-contract check it was written to
exercise. The anchor was changed to `structure` and the anchor rule got its
own test.

Full suite: **534 passed, 1 skipped, 44 subtests**.
