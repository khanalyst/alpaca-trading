# Current hypotheses and variants

This is the authoritative documentation map for what is being tested and
where each identity is stored. It separates strategy identity, runtime
hypothesis settings, named shadow variants, and exploratory tournament
settings.

## Four layers of identity

| Layer | Source of truth | Purpose |
| --- | --- | --- |
| Strategy authority | `agent/registry.py` | Strategy mechanism, falsification, tier, implementation readiness, forward-model readiness, and live eligibility. |
| Runtime hypotheses | `agent/hypotheses.py` | Three prompt-visible hypotheses with bounded `contract_params` and pre-registered settings. |
| Runtime variants | `agent/variants.py` and `research/variants.yaml` | Named deterministic shadow variants, including the momentum parameter axes. |
| Tournament hypotheses | `research/hypotheses/*.yaml` and `research/tournament.py` | Pre-registered OHLCV strategies and their settings. These are exploratory, not live configuration. |

Persisted adaptive variants live in `research/cache/findings.db` on the active
host. The store records the hypothesis ID, setting ID, exact numeric values,
proposal reasoning, run, lock window, observations, and later evidence links.
Committed momentum scorecards in `findings/momentum/` are the reviewable Git
snapshot of the named registry variants.

## Registered strategies

| Strategy | Version | Current tier/status | Current use |
| --- | --- | --- | --- |
| `momentum` | `phase1-v3` runtime; `phase1-v2` tournament benchmark | `T0_REJECTED` / benchmark null | Current demo runtime and comparison floor; not live eligible. |
| `flush-fade` | `v1` | `T0_REJECTED` | Exploratory result; mechanism remains a retest question only with better OI data or a new pre-registration. |
| `funding-carry` | `v1` | `T0_REJECTED` | Mechanism falsified by attribution: price, not carry, produced the result. |
| `funding-unwind` | `v1` | `T1_HYPOTHESIS` | In-sample-generated hypothesis; needs out-of-sample or forward shadow evidence. |
| `trend-multiday` | `v1` | `T1_HYPOTHESIS` | Exploratory multi-day candidate; settings are pre-registered. |
| `ls-ratio-fade` | `v1` | `T1_HYPOTHESIS` / shadow-only | Forward-only because the input series is not in the historical corpus. |
| `scalp-maker` | `v1` | `T1_HYPOTHESIS` / blocked on data | Needs sustained recorded book/order-flow data. |

No strategy is currently live eligible. The runtime `momentum/phase1-v3` is a
benchmark and safety-rehearsal path, not a profitable-edge claim.

## Runtime hypotheses and settings

The LLM may propose only registered hypotheses and bounded numeric settings.
The current contract points and alternatives are:

| Hypothesis | Registered point | Alternatives |
| --- | --- | --- |
| `volume-thrust` | `min_relative_volume=1.5` | `1.2` (`lower_participation`), `2.0` (`higher_participation`) |
| `oi-divergence` | `max_oi_change_4h_pct=-1.0` | `-0.5` (`milder_decline`), `-2.0` (`deeper_decline`) |
| `basis-stretch` | `basis_threshold_pct=0.05`, funding tails `80/20`, `min_funding_samples=10` | basis `0.03` (`narrower_stretch`), `0.10` (`wider_stretch`) |

The baseline contract parameters remain the production research baseline. A
setting is a research variant; it does not silently alter the active strategy
configuration.

## Named momentum variants

`research/variants.yaml` currently records the baseline plus reward/risk, stop,
net-direction, confidence-floor, and breakout-discriminator axes. The committed
cards under `findings/momentum/` are:

- baseline;
- reward/risk: `1.5`, `2.0`, `2.5`, and superseded `3.0`;
- stop ATR: `1.25`, `1.5`, `2.0`;
- net direction: `60`, `80`, `120`;
- confidence floor: `0.50`, `0.55`, `0.60`;
- discriminator: `trend_alignment`, `volatility_regime`.

The exact hypothesis text, status, and overrides in each scorecard must match
the registry. A changed claim or override is a new variant identity, not an
in-place edit.

## Adaptive parameter search

The adaptive path is bounded and history-first:

1. The LLM receives the registered hypothesis/settings and proposes a numeric
   value only within the declared range, with reasoning.
2. The parser rejects unknown identities, non-numeric/non-finite values,
   out-of-range values, duplicate or locked proposals, and insufficient
   reasoning.
3. The proposal, exact value, lock window, run ID, and reasoning are persisted
   before evaluation. A previous failed value remains in history and is not
   silently retried.
4. The engine materializes the proposal as a first-class variant and evaluates
   it through the same isolated shadow path as the named variants.
5. At most one adaptive setting is scheduled per strategy in a cycle; bounded
   workers evaluate eligible variants in parallel, while durable writes remain
   serialized.
6. If the evidence stays open or fails, the value remains a recorded outcome;
   after the lock expires, the LLM may choose a different registered value.

This is parameter search and evidence collection, not automatic promotion. A
qualified result is linked to its exact variant and evidence window, then
stored as an immutable qualification event. It can start local PAPER when the
account is flat; it cannot edit the strategy registry or place live orders.

## Tournament settings versus runtime settings

The tournament's `settings:` lists in `research/hypotheses/*.yaml` are static,
pre-registered alternatives for an exploratory corpus run. Runtime hypothesis
settings in `agent/hypotheses.py` are prompt-visible bounded choices evaluated
from live/shadow snapshots. They share the idea of first-class variants but
are not the same evidence population and must not be pooled.

See [`../MAIN_REPO_REVIEW_PLAN.md`](../MAIN_REPO_REVIEW_PLAN.md) for the
remaining status and [`../OPERATIONS.md`](../OPERATIONS.md) for the commands
that collect, score, and report them.

