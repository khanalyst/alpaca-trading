# Reconciling this branch with the planning documents

> Current implementation note: hypothesis variants, adaptive proposal history
> and locks, first-class `FindingsStore` metadata, forward-qualification
> linkage, and bounded isolated shadow workers are now present. The remaining
> change to strategy/variant identity and any promotion is manual and reviewed.
> This document governs evidence authority and G2/safety rules; it does not
> prescribe an import or VM handoff as a production/default execution path.

This branch began as `codex/main-hardening-v2`, which had already built a
substantial research programme. The four planning documents in this directory
describe a different one. They agree on far more than they disagree on, but
where they disagree it is not a detail — it is a disagreement about what
counts as evidence.

This document records what was kept, what had its authority removed, and what
is still open. It exists so the decision is legible later rather than being
rediscovered as a surprise.

---

## The disagreement, stated plainly

**The codex programme measures a strategy by recomputing it over downloaded
OHLCV.** `research/edge_lab.py` is an independently written feature, signal
and simulation engine; `research/download_okx_history.py` fetches two years
of candles, funding and open interest; `research/validate_features.py` exists
to prove the reimplementation reproduces the live modules. On that basis
`momentum` was measured across 115,929 signals and registered as
`T0_REJECTED`.

**The planning documents forbid exactly this.** `batched-implementation.md`
lists "a general backtester over arbitrary historical OHLCV" under *Explicitly
not doing*, and `findings.md` §9.2 gives the reason: some snapshot fields —
`range_pos_pct`, `chg_24h_pct`, `vol_24h_musd` — come from the live 24h
ticker and cannot be reconstructed after the fact. A replay that re-derives
indicators from a later fetch silently mixes revised data with the original.
The rule it draws is absolute: *replay reads `llm_input` and nothing else*.

`batched-implementation.md` §3.2 states the same principle about code rather
than data: the replay "must call the production code, never a
reimplementation — a reimplemented strategy tests the reimplementation."
`edge_lab.py` is, by its own docstring, an independently written engine.

## The ruling

**Journal replay is the authoritative path. The OHLCV work is exploratory
evidence and keeps that status.**

This is not a judgement that the codex work is wrong. It is a judgement about
what each method can support:

- A recomputed backtest can tell you a strategy is *probably* bad, and it can
  do so across two years of data the journal does not cover. That is real,
  and it is more history than the journal will hold for a long time.
- Only a replay from the recorded snapshot can tell you what *this agent*
  would have done, because only the recorded snapshot is what this agent saw.

So the two are not interchangeable, and the asymmetry has a direction:

> A tier may be **lowered** on exploratory evidence and may only be **raised**
> on journal-replay evidence.

Withholding capital on suspicion is the right way to be wrong. Granting it on
suspicion is not.

Configuration follows the same reconciliation rule. Exploratory evidence may
set a shipped default when the choice is explicit and useful, but that default
is then a fitted point rather than an unfitted baseline. The fit window, corpus
provenance, selection rule, and configuration/code fingerprints must be
recorded beside the value and carried into the baseline evidence. This records
what was fitted without granting exploratory evidence authority to raise a
tier; journal replay and forward confirmation remain the only path to a higher
tier.

## Live corpus handoff checklist

- [ ] Keep `research/nightly.sh` running on the VM that owns the live journal.
- [ ] Export or mount its journal, price cache, and corpus manifest into the
      research checkout before running authoritative replay or qualification.
- [ ] Record the VM corpus path, time window, code/config fingerprints, and
      nightly completion status in the run output.
- [ ] Treat an absent `runtime/` directory in another checkout as a missing
      local mount, not as evidence that the VM corpus was not collected.

## What was kept

| Kept | Why |
| --- | --- |
| `agent/registry.py`, the tier ladder, `LIVE_MIN_TIER` | The plan asks for exactly this discipline — pre-registered claims, computed confidence, a gate that config cannot edit around |
| `research/gates.py`, `research/tournament.py` | Statistical-honesty machinery. The plan's promotion protocol wants the same guards |
| `agent/contracts/`, the multi-strategy dispatch | The plan's batch 8 asks for a strategy protocol and a registry. Codex already built it |
| `agent/market.py` open-interest deltas | Directly implements B0.5.1. The planning documents claimed OI "is not collected" and were wrong |
| Codex's per-strategy shadow recording in `engine.py` | Complementary to B7's variant shadow rather than in conflict: one shadows *strategies*, the other *parameter variants* |
| The whole existing test suite | Untouched, per `findings.md` §8. It is the safety net that makes moving fast on the research layer defensible |

## What lost its authority

**The `T0_REJECTED` verdict on `momentum` keeps the tier and loses the claim
to be settled.** `agent/registry.py` now records where that evidence came
from and what it cannot support. The gate is unchanged — momentum still
cannot reach live capital — but the module no longer reads as though the
question is closed.

It is not closed. The faithful test has not been run: gate G2 (does baseline
replay reproduce the agent's own recorded decisions?) and the three-arm H-E
comparison both require a real journal, and this container has none.

## What was NOT deleted, and the case for deleting it

The exploratory modules are still present:

```
research/edge_lab.py              research/edge_report.py
research/phase1_v2_backtest.py    research/signal_lab.py
research/download_okx_history.py  research/find_edge.py
research/make_legacy_dataset.py   research/deep_edge.py
research/validate_features.py     research/unbiased_recheck.py
research/portfolio_sim.py         research/selection_study.py
```

That is roughly 6,000 lines, plus `research/results/` containing the reports
they produced.

**The case for deleting them** is the one the planning documents make: a
second evidence path that reaches different conclusions by different means
will eventually be quoted as though it were the first, particularly when it
is the one with two years of data behind it and the journal holds three
weeks.

**The case for keeping them** is that they answer a question the journal
cannot answer for years — what happened before the agent started running —
and that deleting a measured negative result is how a rejected idea comes
back. `findings.md` itself argues a rejection must remain visible.

**They were kept, and their authority was removed instead.** Deleting six
thousand lines of measured work on a documentary conflict seemed the more
expensive mistake of the two, and it is the harder one to undo. If the
preference is to delete them, they are isolated under `research/` with no
imports from `agent/`, so removal is a clean operation — say so and it is one
commit.

## Still open

- **A real-journal G2 result is still required for promotion.** Local fixture
  tests do not replace `research.py replay --check-fidelity`; a failed G2 is a
  hard stop. Do not read the historical exploratory reports as current G2
  evidence.
- **Batch 6.4 is blocked** on H-I, which is blocked on that same journal.
- **H-G and H-H are blocked on calendar time.** B0.5 started their clock; the
  sample does not exist for roughly three months. That was always the plan.
