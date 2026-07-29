# Trading decision examples

These examples explain the current `momentum / phase1-v3` decision contract.

> **Batch 6 changed two things these walkthroughs depend on.** `exit_policy`
> no longer accepts `structure_target` (6.1: it was arithmetically identical
> to `fixed_rr` in both setups it was designed for), and `funding_squeeze`
> now requires a structure invalidation rather than being the one setup
> allowed the tightest ATR stop (6.2 / defect D3). An experimental setup also
> requires a registered `hypothesis_id` (6.3). Any arithmetic below that
> walks through `structure_target` describes the v2 contract.
The KAITO, CL and AAVE observations come from the agent's July 23, 2026
journal. Those trades were originally evaluated by an older interface in
which the model supplied numeric leverage, size, stop and target. Phase 1 no
longer accepts that numeric authority; the examples below show how the same
market observations flow through the current code.

Percentages are price distances unless explicitly described as a percentage
of account equity. Targets are planned exits, not guaranteed fills. Net PnL
also includes actual fees, funding and execution shortfall.

## Current decision pipeline

Every proposed entry follows this sequence:

1. Rank active USDT-settled perpetual swaps by normalized 24-hour quote
   turnover.
2. Admit only account-available OKX category-1 crypto instruments with enough
   completed 15m, 1h and 4h history.
3. Build a snapshot containing trend, momentum, relative volume, ATR history,
   range/structure, funding history, perp/index basis, account fee, open
   interest, BTC regime and robust BTC correlation measurements.
4. Ask the LLM for zero or more semantic decisions. For an open, it chooses
   `setup_type`, direction, confidence, `invalidation_anchor`, `exit_policy`
   and `execution_choice`.
5. Check that the label has its minimum deterministic evidence and that the entry
   is not beyond the hard no-chase boundary.
6. Derive stop, target and leverage in code. Model-supplied numeric size,
   leverage, stop or target fields are ignored.
7. Size from the all-in stop risk, then apply per-position, portfolio,
   total planned stop-risk, net-direction, BTC-beta, margin and liquidation
   constraints.
8. Validate proposed pairs sequentially in confidence order. A fill from the
   first pair immediately reduces the exposure available to the second.
9. Check a fresh order book, submit an IOC entry with required attached
   exchange-side SL/TP, verify actual fills and record the complete outcome.

The current model-side open schema is:

```json
{
  "action": "open",
  "symbol": "BTC/USDT:USDT",
  "direction": "long",
  "setup_type": "trend_continuation",
  "invalidation_anchor": "structure",
  "exit_policy": "fixed_rr",
  "execution_choice": "normal",
  "what_changed_since_last_loss": "",
  "confidence": 0.8,
  "reasoning": "one concise setup-specific sentence"
}
```

## Example 1: KAITO trend-continuation long

The historical KAITO snapshot contained:

- Price: 0.9976 USDT.
- 24-hour change: +3.74%.
- 24-hour quote turnover: 1,772.8 million USDT.
- 15m, 1h and 4h trends: all up.
- 1h RSI: 55.6.
- 1h ATR: 1.96% of price.
- Current ATR versus its recent median: 0.90.
- One-hour momentum: +0.89%.
- Relative one-hour volume: 1.16.
- 30-hour BTC-return correlation: -0.26.
- Recent swing low: 1.88% below price.
- Position in the 24-hour range: 81%.
- BTC context: transition regime with -0.54% one-hour momentum.

A current semantic proposal could be:

```json
{
  "action": "open",
  "symbol": "KAITO/USDT:USDT",
  "direction": "long",
  "setup_type": "trend_continuation",
  "invalidation_anchor": "structure",
  "exit_policy": "fixed_rr",
  "execution_choice": "normal",
  "confidence": 0.82,
  "reasoning": "All three timeframes trend up with positive momentum and participation while the recent swing low provides a clear invalidation."
}
```

With the current default contract, code would derive:

```text
ATR floor       = 1.96% × 1.00                         = 1.96%
structure stop  = 1.88% + (1.96% × 0.15 ATR buffer)  = 2.174%
chosen stop     = max(1.96%, 2.174%)                  ≈ 2.17%
fixed-R target  = 2.174% × 2R                         ≈ 4.35%
entry leverage  = risk.entry_leverage                 = 2x
```

The LLM still makes the important discretionary call: whether this is a
quality continuation at all, which direction is justified, whether structure
is the real invalidation, whether a fixed or extended target fits the regime,
and how confident it is. It cannot move the stop inside structure, choose 10x
leverage or inflate size.

The historical revision actually opened about 28,910 USDT at 3x with a 2.15%
stop and 4.60% target. That fill is useful historical evidence, but it is not
a promise of what the current version would size or execute.

## Example 2: CL never reaches the LLM

CL previously appeared because its volume and trend data looked eligible:

- 15m, 1h and 4h trends were up.
- 1h RSI was about 72.
- One-hour momentum was +0.77%.
- Relative one-hour volume was 10.5.
- Funding was negative.

CL is a commodity instrument, not crypto. The current universe requires OKX
private catalogue category `1`, active linear USDT settlement and sufficient
history. CL is therefore excluded during universe construction and cannot
consume a top-10 slot or reach the model.

The latest universe audit stores the selected list and every exclusion reason.
If an exchange order is rejected for a valid crypto instrument, the journal
also preserves its stage, classification, OKX code/sub-code, safe message,
client order ID and submission/recovery audit. `python report.py` displays
those reasons and codes.

## Example 3: AAVE pullback

The historical AAVE observation had aligned 1h/4h uptrends, a flat 15m
pullback near the 1h EMA20, positive one-hour momentum and elevated relative
volume. A current proposal would describe intent rather than invent numeric
risk:

```json
{
  "action": "open",
  "symbol": "AAVE/USDT:USDT",
  "direction": "long",
  "setup_type": "trend_continuation",
  "invalidation_anchor": "structure",
  "exit_policy": "extended_rr",
  "execution_choice": "normal",
  "confidence": 0.76,
  "reasoning": "The higher-timeframe uptrend remains intact and the 15m pullback is near the 1h EMA with positive participation."
}
```

The evidence contract requires aligned 1h and 4h uptrends plus a positive
completed 15m resumption that is not itself in a downtrend. The LLM must still
decide whether the pullback is genuinely attractive.
Code then places a long stop beyond the recent swing low plus the configured
ATR buffer, never tighter than the ATR floor. An `extended_rr` target uses the
configured 3R multiple. If the snapshot lacks finite ATR or structure, the
proposal is rejected rather than guessed.

## Example 4: insufficient order-book depth

Suppose the strategy approves a KAITO setup and deterministic sizing requests
28,806 contracts, but only 12,672 contracts are visible before the configured
0.35% entry-price boundary. No order is sent.

The rejection is stored as execution feedback. The same symbol cannot be
re-evaluated repeatedly on the same completed 15-minute candle, even if the
model changes direction or relabels the setup. On a later completed candle the
LLM may:

- choose a better pair;
- stay flat; or
- deliberately set `"execution_choice": "retry_smaller"`.

For `retry_smaller`, code—not the model—caps size to the configured fraction
of freshly measured safe depth. Every retry gets a new order book. A repeated
depth failure creates a persisted backoff, so restarting the process does not
erase the safety memory.

## Example 5: partial fill and realized performance

Assume one trade closes in two pieces:

```text
partial-close net realized PnL: +3.00 USDT
final remainder net realized:   +4.70 USDT
matched trade total:            +7.70 USDT
```

The final journal row stores only its incremental +4.70 USDT remainder. The
report sums the two rows once; it does not add a cumulative final value and
double-count the partial. Open/unmatched trades remain outside realized
performance.

Entry and exit records also keep actual fees, funding status, signed
implementation shortfall and adverse-only slippage. A favorable fill is
negative signed shortfall and zero adverse slippage; it is not mislabeled as
an execution cost.

## Setup memory and strategy attribution

Each evaluated idea receives:

- a `setup_key` for strategy version + symbol + direction + setup type;
- a `setup_id` that also includes the completed signal-candle timestamp;
- run, cycle, prompt, config, code and equity-basis identifiers.

A symbol gets one evaluation per completed 15-minute candle. After a setup
finishes, the same semantic setup is blocked for
`strategy.setup_cooldown_minutes`; records remain available for
`strategy.setup_memory_hours`. This is bounded operational memory and
idempotency, not self-modifying trading logic.

After a realized loss, elapsed cooldown alone is not enough. The same
symbol/direction/setup also needs a newer completed 1h candle, a changed
objective evidence fingerprint and a non-empty
`what_changed_since_last_loss` explanation. Code verifies the time/bar/data
conditions; the explanation preserves the model's discretionary reasoning.

Demo-only `other` proposals are not mixed into the primary attribution. They
run as `momentum-experimental`, use the smaller
`experimental_risk_per_trade_pct` budget and report separately.

`python report.py` separates results by strategy/version,
prompt/config/code variant and setup type and shows net realized PnL,
expectancy, profit factor, R-multiples, fees, funding, signed/adverse slippage
and synthetic strategy drawdown. Equity curves are segmented by
valuation-basis ID, so removing demo OKB from the equity basis cannot appear as
a trading loss.

## Account-level loss protection

The strategy has no fixed daily profit target. With the current defaults:

- maximum planned all-in loss per trade: 1.5% of current USDT equity;
- daily loss limit: 5% from the UTC-day starting equity;
- maximum drawdown: 15% from the high-water mark;
- deterministic entry leverage: 2x;
- maximum three concurrent positions, subject to exposure and margin caps.

At the daily loss limit, the agent enters `DAY_STOPPED` and opens no new
positions until the next UTC day. At maximum drawdown it durably enters
`KILLED`, flattens and requires explicit human acknowledgement to restart.
Exchange-side stops, liquidation-distance checks and account IMR/MMR guards
reduce risk, but cannot guarantee a gap, outage or severe slippage will stay
inside the planned amount.
