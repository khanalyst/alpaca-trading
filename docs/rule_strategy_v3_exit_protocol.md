# Rule strategy v3/v4 exit protocol

This filename is retained for existing links. The current bounded equity exit
contract spans `rule-strategy.v3` and `rule-strategy.v4`.

V3 adds nullable `breakeven_r`. A completed close that reaches the configured
multiple of initial fill-to-stop risk arms a move to the actual fill price for
the next bar. `null` preserves the original fixed-stop behavior.

V4 retains v2 entry predicates and v3 breakeven behavior, then adds:

- `target_mode`: `fixed_r`, `session_vwap`, or `rolling_mean`;
- `target_lookback`: the bounded completed-bar lookback for `rolling_mean`;
- `trailing_stop_r`: a nullable completed-close trailing distance;
- `exit_before_minutes`: a nullable thesis deadline before the New York close.

Options remain on executable v1/v2 schemas. V3/v4 are equity-share only because
the supported option path has no parity-safe stop-amendment lifecycle.

## Frozen target contract

Non-fixed targets are computed once from the completed signal prefix. Session
VWAP resets on the bars' New York session date; rolling mean uses exactly the
configured completed-close lookback. The raw reference is persisted as
`target_reference`; the broker-valid rounded level is `target_price`. Neither
research nor runtime recalculates the target after entry.

A missing, non-finite, non-positive, or wrong-side reference rejects the signal
with `target_reference_unavailable_or_wrong_side`. It never falls back to an R
target. Entry gaps that place the executable fill beyond the frozen target fail
the ordinary bracket-geometry check rather than silently moving the target.

## Deterministic transition

Research replay, randomized controls, and runtime use the same pure completed-
bar transition:

1. Resolve an entry fill already beyond the stop or target at the fill anchor.
2. Resolve a later opening gap at that open.
3. Resolve intrabar touches, with the stop winning an unknowable two-sided path.
4. If still open, arm breakeven and trailing amendments from the completed close;
   those stop changes apply only to the next bar and ratchet monotonically.
5. If no protective exit fired, apply deadline precedence: session force-flat,
   thesis deadline, then maximum hold when timestamps tie.

The initial risk unit and frozen target never change. Durable state retains the
initial and active stops, target policy/reference, amendment epochs, and last
completed bar.

## Canonical reasons and compatibility

Research and runtime publish `canonical_exit_reason` from one vocabulary:
`stop`, `target`, `max_hold`, `thesis_deadline`, `session_force_flat`, and
`data_discontinuity`; unrecognized operational causes map to `unknown` rather
than extending the canonical vocabulary. Runtime keeps operational `reason`
aliases such as
`before_close`, `max_hold`, and `exit_before`; research keeps the historical
`exit_reason` values `time` and `exit_before`. Those compatibility fields are
not the cross-lane comparison key.

Force-flat replay uses the exact session boundary and, in strict equity lanes,
requires a fresh executable exit quote. Missing quotes refuse the observation;
permissive diagnostic lanes may use the bar reference and still apply the shared
cost model. P&L is always calculated from the selected executable entry and exit
prices, quantity, multiplier, and fees.

## Family hypotheses and bounded search

New equity roots retain neutral v4 defaults and their stable content identities.
The coordinate pool promotes one auditable family-specific hypothesis early:
breakout/trend families try a completed-close trailing ratchet, while reversion
families try a frozen `rolling_mean` or `session_vwap` target. The neutral root
and other fixed-R candidates remain in the first batch, so an unavailable fair-
value reference cannot block the whole batch. Family-aware target and hold
ladders remain bounded, and discovery keeps its existing attempt cap.

## Broker amendment and recovery

For protected equity positions, runtime persists amendment intent before asking
the broker to replace the live stop. It advances the active stop only after the
successor response is validated. Reconciliation follows replacement links after
a process failure. If either stop fills during the race, that broker leg is the
close; runtime must not submit a duplicate market order.
