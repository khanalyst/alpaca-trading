# Rule strategy v3 exit protocol

`rule-strategy.v3` is an opt-in extension of v2. It adds one field:
`breakeven_r`, either `null` or a finite number from `0` through `10`, strictly
below `target_r`. `null` is the default and preserves fixed-stop behavior.
The canonical forms and content hashes of v1 and v2 are unchanged.

New deterministic equity factory roots use v3 with `breakeven_r: null`, then
their bounded coordinate neighborhood and discovery ladder traverse finite
breakeven triggers below `target_r`. Option roots and discovery remain on v2;
the v3 exit is reachable without relying on an LLM-authored proposal.

## Executable vehicle boundary

V3 is executable for equity shares only. A non-null `breakeven_r` requires a
provider capable of replacing the live broker stop leg. Options are rejected
by replay, null controls, and runtime risk before contract selection because
Alpaca's supported option order path does not provide a parity-safe stop
amendment.

## Deterministic transition

Research replay, randomized null controls, and runtime call the same pure
completed-bar transition. Its order is fixed:

1. On the entry bar, resolve an entry fill already beyond the initial stop or
   target at the fill anchor.
2. Resolve a later bar opening beyond the active stop or target at that open.
3. Resolve intrabar stop/target touches; when both occur, the stop wins.
4. If still open, compare the completed close with the breakeven trigger. An
   arm recorded at that close changes the active stop only for the next bar.

The risk unit is always the absolute distance from the actual entry fill to
the initial stop. The initial stop is immutable. The active stop, arm time,
arm epoch, and last completed bar are durable state.

## Broker amendment and recovery

For protected equity positions, runtime persists replacement intent and arm
state before asking Alpaca to replace the live stop. It attaches the returned
successor and advances the active stop only after the broker response is
validated. Reconciliation follows `replaced_by`/`replaces` links to recover a
successor after a process failure between broker acceptance and local attach.

A replacement error triggers broker reconciliation before retry or local
close. If the old or successor stop filled during the race, that broker leg is
the close and runtime must not submit a duplicate market order. An unresolved
`replaced` order without its successor is a reconciliation error, not evidence
that protection may be widened locally.
