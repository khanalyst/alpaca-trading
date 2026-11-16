# Rule strategy v3–v7 exit protocol

This filename is retained for existing links. The executable grammar and
transition implementation are `agent/contracts/rule.py`; this page summarizes
the versioned contract and does not define a second strategy language.

## Schema versions

The validator accepts `rule-strategy.v1` through `rule-strategy.v7`. Each
version is explicit in the normalized specification and contributes to its
content identity, while no-op extensions may share a semantic identity:

* V1 is the original bounded signal grammar.
* V2 adds bounded entry predicates: extra confirmations, entry windows, and
  ATR bands. It does not change sizing, exits, or order placement.
* V3 adds nullable `breakeven_r`. A completed close reaching that multiple of
  the initial fill-to-stop risk arms a move to the broker-valid actual fill
  price for the next bar. `null` preserves fixed-stop behavior.
* V4 adds equity-only exit policy fields: `target_mode` (`fixed_r`,
  `session_vwap`, or `rolling_mean`), bounded `target_lookback`, nullable
  `trailing_stop_r`, and nullable `exit_before_minutes`.
* V5 adds opt-in regime and reclaim entry context (`regime_mode`, timeframe,
  lookback, efficiency threshold, `entry_trigger`, and `pullback_bars`).
* V6 adds family-scoped mechanism fields: ATR/range compression, opening hold,
  range-expansion body/close geometry, VWAP slope mode, and relative-volume
  mode. V6 is diagnostic grammar only: `rule_vehicle_executable` rejects V6
  and V7 for promotion or runtime execution.
* V7 adds the authored `min_stop_distance_bps` floor and opt-in correction
  fields (`signal_event`, `stop_mode`, `residual_mode`,
  `volume_confirmation_mode`, `session_gap_mode`, `range_multiplier`, and
  `residual_beta_sessions`). V7 is also diagnostic grammar only; a corrected
  specification is a new registration and identity.

Options remain on the executable v1/v2 path. V3–V5 exit/context extensions are
not executable for options; V6/V7 are not executable for any vehicle. A
diagnostic replay of a non-executable schema must stay labelled
`authorizing: false` and cannot produce a deployable proof.

All families, fields, numeric bounds, confirmations, sides, and family-scoped
extensions are closed sets in the validator. Unknown fields and cross-family
mechanism fields fail closed. A tuned child preserves its root family and
schema; changing either is a new discovery decision.

## Frozen exit target

For V4 and later, non-fixed targets are computed once from the completed signal
prefix. `session_vwap` resets on the bars' New York session date;
`rolling_mean` uses exactly the configured completed-close lookback. The raw
reference is persisted as `target_reference`, and the broker-valid rounded
level as `target_price`. Neither research nor runtime recalculates the target
after entry.

A missing, non-finite, non-positive, or wrong-side reference rejects the signal
as `target_reference_unavailable_or_wrong_side`. There is no fallback to an R
target. If an entry gap lands beyond the frozen target, ordinary bracket
geometry rejects the trade rather than moving the target.

The initial risk unit is always derived from the actual fill and initial stop.
Breakeven and trailing amendments do not rewrite that risk unit. A trailing
stop is derived from a completed close, rounded toward the entry to a valid
equity tick, ratchets monotonically, never crosses the frozen target, and takes
effect only on the next bar.

## Deterministic completed-bar transition

`completed_bar_exit_transition` is pure and shared by research and runtime. For
each completed OHLC bar it:

1. resolves an entry fill already beyond the stop or target;
2. resolves an opening gap at that open;
3. resolves intrabar touches, with the stop winning an unknowable two-sided
   path;
4. if still open, arms breakeven and/or a monotone trailing amendment from the
   completed close for the next bar; and
5. leaves deadline precedence to the shared contract: session force-flat, then
   thesis deadline, then maximum hold when timestamps tie.

Duplicate completed bars are idempotent; out-of-order bars and invalid OHLC or
geometry fail closed. State retains entry, initial and active stops, frozen
target, target mode/reference, amendment epochs, and the last completed bar.

The canonical cross-lane exit causes are:

`stop`, `target`, `max_hold`, `thesis_deadline`, `session_force_flat`,
`data_discontinuity`, and `unknown`.

Runtime may retain aliases such as `before_close`, `max_hold`, or
`exit_before`; research may retain historical `time` and `exit_before`. Those
aliases are compatibility fields, not comparison keys.

## Entry and session boundaries

Signals use completed bars only and enter at the causal next-bar boundary. The
causal maturity and continuity windows are family-specific. Session-anchored
families (`vwap_reversion`, `vwap_trend`, and opening families) cannot treat a
late or missing early bar as a continuous prefix. V7's explicit
`session_gap_mode: zero_volume` narrows the adjacency requirement only as
defined by the validator; it does not permit a session to begin after the
bell.

The safety force-flat timestamp is the exact calendar close minus the
configured `force_flat_minutes_before_close` offset (ten minutes by default).
`exit_before_minutes` is measured before that session close, not after the
open. A thesis deadline is separate
from the safety force-flat timestamp even when they coincide. Missing strict
exit quotes refuse the observation; permissive historical diagnostic lanes may
use a bar reference but retain the diagnostic provenance.

## Broker amendment and recovery

For protected equity positions, runtime persists amendment intent before asking
the broker to replace a live stop. It advances the durable active stop only
after validating the successor response. Reconciliation follows replacement
links after a process failure. If a stop fills during the replacement race,
that broker leg is the close and runtime does not submit a duplicate market
order.

## Authority

An exit protocol match is necessary for replay parity, not sufficient for an
entry. Diagnostic geometry, target reachability, and paper-trial observations
cannot authorize an entry or override an operator pause. Authorizing evidence
still requires source validation, current replay/gate epochs, executable quote
provenance, qualification, parity-matched shadow evidence, and explicit
deployment selection.
