# Trading decision examples

These examples are reconstructed from the agent's own journal on
2026-07-23. They show the difference between universe selection, LLM
reasoning, deterministic risk checks, exchange execution, and account-level
circuit breakers.

The percentages below are price distances unless explicitly described as a
percentage of account equity. A proposed take-profit is not guaranteed to
fill, and actual net P&L also includes fees, funding, spread, and slippage.

## Decision pipeline

Every new position follows this sequence:

1. Rank eligible instruments by normalized 24-hour quote turnover.
2. Build a completed-candle snapshot for every selected instrument.
3. Ask the LLM for zero or more `open`, `close`, or `hold` decisions.
4. Process closes first and sort opens by descending confidence.
5. Validate each open against current positions, costs, cooldowns, and
   exposure limits.
6. Check the live order book and submit an IOC entry with attached
   exchange-side stop-loss and take-profit.
7. Verify the actual fill and protective orders.

## Example 1: KAITO long opened successfully

At the decision time, the KAITO snapshot contained:

- Price: 0.9976 USDT.
- 24-hour change: +3.74%.
- 24-hour quote turnover: 1,772.8 million USDT.
- 15m, 1h, and 4h trends: all up.
- 1h RSI: 55.6.
- 1h ATR: 1.96% of price.
- Current ATR versus its recent median: 0.90.
- One-hour momentum: +0.89%.
- Relative one-hour volume: 1.16.
- 30-hour correlation with BTC returns: -0.26.
- Recent swing low: 1.88% below price.
- Position in the 24-hour range: 81%.
- BTC market context: transition regime with -0.54% one-hour momentum.

The LLM proposed:

```json
{
  "action": "open",
  "symbol": "KAITO/USDT:USDT",
  "direction": "long",
  "confidence": 0.82,
  "size_pct_equity": 0.0,
  "leverage": 3.0,
  "stop_loss_pct": 2.15,
  "take_profit_pct": 4.6,
  "reasoning": "All three timeframes trend up with positive momentum and above-average participation, while its low BTC correlation supports an independent continuation long; stop sits beyond the 20-period swing low and ATR."
}
```

Why it passed:

- The proposed stop was wider than ATR and beyond the recent swing low.
- The take-profit was approximately 2.14 times the stop distance.
- Confidence exceeded the configured 0.65 floor.
- The deterministic cost, exposure, spread, and order-book checks passed.
- The exchange verified the fill and protective orders.

The actual opened notional was approximately 28,910 USDT. The estimated
all-in loss at the stop was approximately 2.78% of that notional after
including the price stop, round-trip fees, spread, expected stop slippage,
the reserved entry-price boundary, and adverse funding.

That corresponds to approximately 803 USDT, or 1.11% of the then-current
72,232 USDT account equity. The configured per-trade ceiling was 1.5%, but
the 40%-of-equity notional cap reduced the actual planned risk.

If the 4.60% target were filled exactly, gross price P&L would be
approximately 1,330 USDT, or 1.84% of account equity before exit fees,
funding, and execution effects.

## Example 2: CL long proposed but rejected by OKX

The CL snapshot contained:

- 15m, 1h, and 4h trends: all up.
- 1h RSI: approximately 72, indicating an extended market.
- One-hour momentum: +0.77%.
- Relative one-hour volume: 10.5.
- Negative funding, which favored a long position.
- Price approximately 1.85% above the 1h EMA20.

The LLM proposed:

```json
{
  "action": "open",
  "symbol": "CL/USDT:USDT",
  "direction": "long",
  "confidence": 0.75,
  "size_pct_equity": 12.0,
  "leverage": 2.0,
  "stop_loss_pct": 2.05,
  "take_profit_pct": 4.4,
  "reasoning": "Strong three-timeframe uptrend, exceptional relative volume, positive momentum and negative funding support continuation, though the extended RSI and EMA distance warrant reduced size."
}
```

The LLM deliberately requested less than the risk engine's full computed
size because the instrument was extended. Its take-profit was approximately
2.15 times its stop distance.

OKX rejected the entry with its required attached stop-loss and take-profit.
The agent correctly did not send an unprotected fallback order, so no CL
position was opened.

This example also exposed two issues addressed by Phase 1:

- CL is a commodity instrument and should never have entered a crypto-only
  universe.
- The old error wrapper hid the underlying OKX rejection code and did not
  create persistent backoff, allowing CL to be proposed again later.

## Example 3: AAVE pullback proposed but not opened

The LLM identified:

- Aligned 1h and 4h uptrends.
- A flat 15m pullback near the 1h EMA20.
- Positive one-hour momentum.
- Elevated relative volume.

It proposed:

```json
{
  "action": "open",
  "symbol": "AAVE/USDT:USDT",
  "direction": "long",
  "confidence": 0.76,
  "size_pct_equity": 0.0,
  "leverage": 3.0,
  "stop_loss_pct": 1.1,
  "take_profit_pct": 2.4,
  "reasoning": "1h and 4h uptrends align with a flat 15m pullback at the 1h EMA20, positive momentum and elevated volume; stop clears ATR and recent swing low."
}
```

The proposed gross price reward was approximately 2.18 times the stop
distance. OKX rejected the required attached entry, so no position was
opened. As with CL, the old visible error did not retain enough information
to identify the exact exchange cause.

## Example 4: daily-loss and drawdown protection

The strategy has no fixed daily profit target. It instead sizes each trade
from current equity and applies account-level loss controls:

- Maximum planned all-in loss per trade: 1.5% of current USDT equity.
- Daily loss limit: 5% from the UTC-day starting equity.
- Maximum drawdown: 15% from the high-water mark.

If live equity falls 5% from the UTC-day benchmark, the agent enters
`DAY_STOPPED` and opens no new positions until the next UTC day. Existing
positions continue to be protected and managed. With the current
`flatten_on_daily_stop: false` setting, they are not automatically closed
solely because the daily stop was reached.

If equity falls 15% from the high-water mark, the agent durably enters
`KILLED`, closes positions, cancels remaining orders, and stops.

These thresholds limit loss; they do not guarantee that a gap, exchange
outage, liquidation event, or severe slippage cannot exceed the planned
amount.
