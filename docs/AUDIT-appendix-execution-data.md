# Audit 03 — Execution Correctness & Market Data
Branch: claude/trading-strategy-audit-zveg57
Status: IN PROGRESS (append-only)

## F1 [HIGH] CONFIRMED — Option take-profit limit is priced in premium-R, effectively unreachable
`agent/execution_lifecycle.py:526-550` `_option_take_profit_price`:
```
distance = abs(underlying - stop); reward = abs(target - underlying)
price = debit * (1 + reward/distance)   # ROUND_CEILING to cent
```
The plan's reward/risk multiple (target_r, default 2.0, config.yaml strategy.target_r=2.0) is applied to the
FULL PREMIUM. So the resting sell_to_close limit sits at 3x the debit paid.
The docstring at :552-560 explicitly says this leg "is the only protection Alpaca lets an option position keep
when this process dies". A ~30-60bp underlying move (the strategy's actual stop/target scale) moves an ATM
premium by tens of percent, not 200%. => the only broker-resident option protection is a limit that in practice
never fills. Options positions are, for all realistic paths, entirely unprotected at the broker.

## F2 [MEDIUM] CONFIRMED — option risk unit vs open-risk cap
`agent/risk.py:530-535`: option plan sets `risk_usd = option["max_loss"] = contracts*debit*multiplier` and
`notional` = the same number. So for options, notional == risk_usd exactly.
Consequences:
- `max_gross_exposure_pct` and `max_open_risk_pct` (risk.py:589-596) are compared against the same quantity.
- `check_stressed_cost` (risk.py:132-180) computes `ratio = stressed_cost_usd(notional)/risk_usd`, i.e. the
  cost as a pure fraction of premium; with limit 1.0 it can only fire if per-trade cost exceeded 100% of premium.
  Inert for options as well as equities.

## F3 [CRITICAL] CONFIRMED — Bracket legs are never rounded to a tick; Alpaca rejects sub-penny prices
Chain (no quantization anywhere):
- `agent/contracts/rule.py:773-776` — `distance = max(atr*stop_atr, close*MIN_STOP_DISTANCE_FRACTION)`;
  `stop = close ± distance`, `target = close ∓ distance*target_r` — raw float arithmetic.
- `agent/risk.py:646-653` — plan carries `stop_price`/`target_price` unmodified.
- `agent/market_entry_risk.py:494-505` — `OrderRequest(..., order_class="bracket",
  take_profit=Decimal(str(take_profit)), stop_loss=Decimal(str(stop_loss)))`.
- `agent/alpaca_domain.py:458-492` — validates sign/ordering only; NO tick quantization.
- `agent/alpaca_provider.py:552-553` — `TakeProfitRequest(limit_price=float(request.take_profit))`,
  `StopLossRequest(stop_price=float(request.stop_loss))`.

Executed against the real functions (short SPY, close 612.37, atr 0.41, stop_atr 1.0, target_r 2.0):
```
stop_loss  = 614.2071100000001
take_profit= 608.69578
```
Alpaca rejects limit/stop prices finer than $0.01 for instruments >= $1.00
("sub-penny increment does not fulfill minimum pricing criteria"). Every equity bracket entry is
therefore expected to be rejected at the broker.
Failure mode is not silent: `agent/engine_cycle.py:619-622` re-raises `AlpacaError` from
`provider.submit_order`, which `run_once` (`engine_cycle.py:151-160`) turns into `self.close()` +
re-raise. So a submit rejection tears down the runtime rather than skipping the symbol.
Compare `_option_take_profit_price` (execution_lifecycle.py:549-550) which *does* quantize to
`Decimal("0.01")` — the equity bracket path simply omits it.
NOTE: because no rounding exists, the "rounding inverts a tight stop" question is moot; but it also
means the stop is not on a tradable increment.

## F4 [CONFIRMED, informational] Short-side sign arithmetic is correct
`agent/risk.py:549-551` enforces `stop>entry>target` for short / `stop<entry<target` for long BEFORE
sizing, and `alpaca_domain.py:485-490` re-checks `take_profit < limit_price < stop_loss` for sell
brackets. `contracts/rule.py:775-776` signs are correct. Constructed short case asserts
stop(614.207) > entry(612.37) > target(608.696). No sign inversion found.
Residual: `market_entry_risk.py:490-492` maps `side = "buy" if direction=="long" else "sell"`;
`_entry_execution` uses bid for sell / ask for buy — correct.

## F5 [MEDIUM] CONFIRMED — stressed-cost veto is inert by 17%, and shows the strategy's real economics
`ratio = stressed_cost_usd(notional)/risk_usd` = stress_bps/stop_bps, independent of size.
scenario 25bps, stop floor 30bps (`MIN_STOP_DISTANCE_FRACTION`) => ratio caps at 0.8333 vs a limit of 1.0.
Measured on the constructed trade: `stressed_cost_to_risk_ratio: 0.8333333333333335`.
The gate can only ever fire if a spec produced a stop tighter than 25bps, which the 30bps floor forbids.

## F6 [CRITICAL] CONFIRMED — "no overnight book" is not guaranteed; a trader death in the last 5 minutes always leaves a position overnight
Facts:
- Equity protection = Alpaca bracket legs with `time_in_force="day"` (`market_entry_risk.py:496-501`,
  `alpaca_domain.py:436-439` hard-rejects anything but `day`). Unfilled legs die at the close; the
  position does not.
- Options have no broker stop at all (see F1).
- Force-flat is only ever executed by the trader loop: `engine_cycle.py:256-259` (10 min before close)
  and `runtime_control.py:653-665` (process-exit `finally`).
- The watchdog is the only out-of-process backstop. `deploy/watchdog.py:47-48` +
  `deploy/alpaca-watchdog.service:19-20` + `compose.yaml` watchdog command: `--max-heartbeat-age 300`,
  `--interval 30`. It acts only after the heartbeat is >=300s stale, and only if the trader has released
  the run lock (`watchdog.py:154-166`).
=> Worst case latency from trader death to watchdog flatten is 300s + 30s = **330 seconds**.
Force-flat begins at close-10min. Any trader death after roughly close-5.5min cannot be covered by the
watchdog before the close. The position (equity or option) survives into the next session, unprotected,
with no broker stop.
Additionally `watchdog.decide()` (:75-93) checks only heartbeat FRESHNESS, never status: a trader alive
but permanently returning `{"action":"hold"}` keeps writing a fresh heartbeat and the watchdog stays
inert forever. The docstring at :16-18 acknowledges the hung-trader case; the last-5-minutes case is not
acknowledged anywhere.

## F7 [HIGH] CONFIRMED — force-flat silently disabled whenever the session lookup returns None
`agent/alpaca_session.py:78-82`:
```
def should_force_flat(self, now, session):
    if session is None:
        return False
```
`agent/market.py:138-139` additionally ANDs with `self._calendar_loaded`.
Both are fail-OPEN on the exit path: a calendar that loads but does not contain today's date
(`session_for`, alpaca_session.py:206-212 matches on exact local date) yields `session=None` and
force-flat NEVER fires. Contrast `entry_allowed` (:66-73) which fails CLOSED on `session is None`.
The engine partly masks this by failing closed if `refresh_calendar()` raises
(`engine_cycle.py:212-218`), but a successful-but-wrong calendar response is unguarded.
Same defect reaches `_monitor_positions` (`execution_lifecycle.py:880`).

## F8 [MEDIUM] CONFIRMED — full market calendar is re-downloaded every 60s cycle
`engine_cycle.py:213` calls `self.market.refresh_calendar()` with no arguments;
`agent/alpaca_provider.py:228-234` then builds `GetCalendarRequest()` with no start/end, i.e. Alpaca's
default (entire calendar history). `session_for` (alpaca_session.py:206-212) then re-normalizes every
returned row on every lookup, and `session()` is called several times per cycle. Wasteful, and it burns
the shared 200 req/min budget on the same path that must stay responsive during force-flat.

## F9 [MEDIUM] CONFIRMED — option position risk is expressed in a different unit than the proof
`agent/risk.py:504-535`: `contracts = floor(risk_budget / (debit*multiplier))`, so realized loss at the
UNDERLYING stop is only ~delta * (stop_bps) * spot * qty, not the budgeted premium. With the 30bps
minimum stop (`contracts/rule.py:774`, MIN_STOP_DISTANCE_FRACTION) and a ~0.5-delta contract, hitting the
stop costs roughly 40-50% of the premium, i.e. the option lane delivers ~2-3x the equity lane's realized
risk per trade for the same nominal budget while both are validated against the same underlying-R proof.
`_option_take_profit_price` compounds this by defining reward in premium-R (F1).

## F10 [HIGH] CONFIRMED — equity protective legs are never quantity-reconciled against the filled position
`agent/execution_lifecycle.py:76-91` `_protective_legs` persists only `order_id`, `role`, `status`,
`price` for bracket children — **no `qty`**. (The option take-profit path at :621 does store `qty`, and
:601-603 does compare it to the trade qty; the equity path has no equivalent.)
`_broker_protected` (:104-107) therefore answers "is a live stop+target pair recorded", never
"does that pair cover the quantity I actually hold".
Consequences on a partial entry fill (or on any later position-size change):
- `_activate_filled_trade` (:308-518) correctly re-derives risk/notional from `filled_qty` and rewrites
  `active_trades[symbol]["qty"]`, but leaves `protective_legs` untouched.
- `_monitor_positions` (:900-905) sees `protected == True` and disables the local stop poller entirely
  ("The broker owns the stop and target exits"), regardless of leg size.
A leg sized to the wrong quantity is thus indistinguishable from correct protection, and it suppresses
the only fallback. No test or runtime assertion covers leg qty vs position qty for equities.

## F11 [MEDIUM] CONFIRMED — any non-`day` order anywhere on the account hard-fails the cycle into a flatten
`agent/execution_lifecycle.py:1012-1020`: reconcile raises `AlpacaError("broker reconciliation found a
non-day order")` for ANY order in the account snapshot with `time_in_force != "day"`, including orders
this system did not create. `engine_cycle.py:224-227` turns that into
`_fail_closed("cycle_reconciliation_failed")` which flattens the whole book
(`market_entry_risk.py:317-340`). A single manual GTC order on the account force-liquidates the strategy.

# DOMAIN B — DATA / LOOK-AHEAD

## F12 [CONFIRMED, no defect] Bar timestamp convention is consistent end to end; entry is NEXT-bar
- Recorder: `deploy/recorder_market.py:374-395` — "Alpaca timestamps one-minute bars at their open";
  writes `timestamp` = bar OPEN, `as_of` = open+1min (bar CLOSE), and SKIPS a bar whose
  `bar_complete > now` so an in-progress candle is never frozen.
- Backfill: `deploy/backfill.py:151-153` writes the same convention.
- Replay availability: `research/market_data.py:203-227` `record_available_at = max(timestamp, as_of,
  observed_at)`.
- Replay execution: `research/factory_core.py:299-333` — signal is evaluated on `session_bars[:index+1]`
  and the entry bar is `session_bars[index+1]`, filled at that bar's OPEN or at a fresh boundary quote at
  `entry_at = signal_bar.end`. This is a genuine next-bar model. No 1-bar look-ahead.
- Runtime equivalent: `agent/engine_cycle.py:48-115` `_rule_runtime_bars` requires `stamp + 1min <= now`
  before a bar is visible, and the market order is sent after that boundary.
- Stop/target are deliberately anchored to the SIGNAL bar's close in both lanes
  (`factory_core.py:415-421` comment; `market_entry_risk.py:492-495`), so entry gap shows up as R error.
  This is internally consistent.
CAVEAT: `agent/contracts/rule.py:788` still labels the signal-bar close as `entry_price`, and that is what
`agent/market_entry_risk.py:241-256` `_entry_execution` uses as the slippage reference. With
`max_slippage_bps=50` the reference being one bar stale is inert, but the field name misdescribes it.

## F13 [MEDIUM] CONFIRMED — no corporate-action handling anywhere; bars are requested unadjusted
`agent/alpaca_market_data.py:191-197` builds `StockBarsRequest` with no `adjustment` argument, so Alpaca's
default (`raw`) applies; `deploy/backfill.py:131-132` uses the same provider method. Repo-wide grep for
`adjust|split|dividend|corporate` returns zero handling code.
Intra-session replay is unaffected (a split is an overnight event and all levels are bps-relative), so this
is not the catastrophe it would be for a multi-day model — but there is no detection at all, so an ETF
split silently invalidates any cross-session statistic computed on price levels, and OCC contracts adjusted
by a split (non-100 multiplier) are accepted by `agent/risk.py:427-433` on the multiplier the provider
reports with no sanity check that it is 100.

## F14 [HIGH] CONFIRMED — the recorder never rejects a bar gap in the shipped deployment
`deploy/recorder.py:817-819`:
```
def _bar_gap_policy(feed): strict = _strict_bar_feeds(); return "strict" if "*" in strict or feed in strict else "observe"
```
`_strict_bar_feeds()` (:795-815) returns an empty frozenset when `ALPACA_RECORDER_STRICT_BAR_FEEDS` is
unset/empty, and `compose.yaml` passes exactly that (`ALPACA_RECORDER_STRICT_BAR_FEEDS: "${...:-}"`).
So `_verify_bar_continuity` (:528-591) NEVER raises; gaps are only counted into the sidecar
`bar_coverage`. Additionally `DEFAULT_BAR_GAP_MINUTES` = 5 (`compose.yaml` ALPACA_RECORDER_BAR_GAP_MINUTES
default 5), so a hole of up to 4 consecutive missing one-minute bars is not even counted as a gap.
Downstream, grep shows `bar_coverage` is read ONLY by `deploy/health.py:107-148` (a status field).
Nothing in `research/`, `deploy/research-cycle.sh`, or the factory consults it. A session with observed
gaps is admitted to research indistinguishably from a clean one.

## F15 [HIGH] CONFIRMED — "session count" for the 30-session floor is distinct trade dates, not data coverage
`research/gates.py:405-470` `floor_feasibility`:
```
sessions = {str(_session_key(row)) for row in materialized if _session_key(row)}
... structural_pass = observed_trades >= min_trades and len(sessions) >= min_sessions ...
```
A "session" is simply a distinct `session_date` appearing on a replayed trade row. Nothing checks how many
bars that session actually contained, whether it had a halt, whether the recorder was down for most of it,
or whether `bar_coverage` flagged gaps (see F14 — nothing reads it). A session with 20 recorded minutes and
one trade counts identically to a full 390-bar session.
`research/factory_core.py:237-257` `_session_bars_valid` only enforces 60s interval + strictly increasing
unique timestamps; there is no minimum bar count and no whole-session contiguity requirement.

## F16 [HIGH] CONFIRMED — a data gap resolves a replay position but not a live one
`research/factory_core.py:422-435`: the hold loop breaks on the first non-adjacent bar, so a position is
force-resolved at the close of the last contiguous bar with `reason="time"`, at that bar's close price.
Runtime does the opposite: `_monitor_positions` (`execution_lifecycle.py:876-1000`) keeps the position
across any bar gap — the broker bracket is live and only stop/target/hold-deadline/force-flat close it.
So every gap in the corpus buys the backtest a free, path-blind exit at a known price that live trading
never gets. Given F14 (gaps are never rejected and sub-5-minute holes are not even counted), this bias is
unbounded and unmeasured.

## F17 [MEDIUM] CONFIRMED — v2 entry window and max_hold_bars are calendar-blind (half days)
Executed against the real functions:
- `_session_minutes` (`contracts/rule.py:504-511`) is DST-correct — it anchors to 09:30 local via ZoneInfo
  and returns 0.0 / 389.0 identically on 2026-03-08, 2026-11-01 and ordinary days. No DST defect.
- `entry_before_minutes` defaults to SESSION_MINUTES=390 (`rule.py:92-110`). On the 2026-11-27 half day
  (13:00 close) `_within_entry_window` returns **True at 13:30**, i.e. 30 minutes after the market closed.
  The parameter's meaning silently changes with session length; a spec fitted on full days is unconstrained
  on half days.
- `hold_deadline(10:00 on 2026-11-27, max_hold_bars=390)` returns **16:31 ET** — 3.5 hours past that day's
  13:00 close.
Both are normally clamped: replay by `replay_policy_for_session` (`research/costs.py:216-256`, derived from
the bar's exact session_close) and runtime by force-flat. They become live only when the clamp is missing —
which is exactly F7/F18.

## F18 [HIGH] CONFIRMED — the runtime's force-flat fallback hardcodes a 16:00 close
`agent/strategy.py:64-72` `_default_force_flat`:
```
return (datetime.combine(local.date(), dt_time(16, 0), tzinfo=zone) - timedelta(minutes=minutes)).isoformat()
```
This fallback is taken whenever `agent/engine_cycle.py:270-278` could not derive `force_flat_at`, i.e.
whenever `self.market.session(now)` returned None — the same condition that makes
`should_force_flat` return False (F7). On an early-close day whose calendar row is missing, the plan's
`force_flat_ts` is 15:50 (2h50m after the real 13:00 close), the hold deadline is clamped to that, and
`should_force_flat` never fires. Every guard fails in the same direction on the same input.
Note it also uses `force_flat_minutes_before_close` defaulting to **5** here versus 10 everywhere else
(config.yaml session/strategy both say 10).

## F19 [MEDIUM] CONFIRMED — execution caps are inert, and the self-consistency check passes trivially
`research/costs.py:360-370` raises only if `spread_bps > max_spread_bps` (4 vs 100) or
`entry_cost_bps > max_slippage_bps` (4/2+6 = 8 vs 50). Neither can fire with shipped values.
Runtime side: `agent/risk.py:553-555` rejects when `market.spread_bps > execution.max_spread_bps` = 100 bps
(1%) on ETFs quoting ~0.5-1 bp; `agent/market_entry_risk.py:250-256` rejects when quoted entry slippage
exceeds 50 bps measured against the SIGNAL BAR CLOSE (so it also absorbs a full bar of price movement
before it can fire). The tighter one is the rule path's `strategy.max_spread_bps` default of 25 bps
(`agent/strategy.py:114`), which is not set in config.yaml and is still ~25x the real spread.
No cap is tighter than expected cost; none can reject the strategy's own economics.

## !! WORKING-TREE CONTAMINATION (not a repo defect) !!
`git status` was CLEAN when this audit started. Mid-audit, a concurrent process modified the working tree:
```
agent/risk.py:546
-  if spread is not None and spread > max_spread: return None, "spread is too wide"
+  if spread is not None and spread < max_spread: return None, "spread is too wide"
```
mtime 2026-08-19 09:36:41 (every other file 2026-08-18). This is an injected mutation from another
concurrent session, NOT the committed code. HEAD has the correct `>`. All findings above were derived
against the committed version. I did not revert it (instructed not to modify the tree) — the owner should
`git checkout -- agent/risk.py`.

## F20 [HIGH] CONFIRMED (numerically) — the option lane cannot buy a near-ATM contract at the shipped equity
Ran the real `RiskEngine.vet_open` on the options profile, $100k equity, risk_per_trade_pct 0.5 ($500 budget),
SPY at 612.37:
```
debit $2.05 -> contracts=2, risk_usd=$410, TP limit $6.15 = 3.00x debit
             premium implied by the underlying TARGET (delta .5) ~ $3.89  -> TP never fills
             loss at the underlying STOP (delta .5) ~ $184 vs "risked" $410
debit $6.05 -> REJECT "option debit exceeds risk budget"
debit $12.00-> REJECT "option debit exceeds risk budget"
```
`agent/risk.py:513-520`: `contracts = floor(risk_usd / (debit*multiplier))`, and `select_option_contract`
(:454-470) ranks by minimum |strike-spot| FIRST, so it always picks the near-ATM contract, whose 7-60 DTE
premium on a $600 underlying is $6-12. Result: the option lane rejects essentially every candidate it
selects. Combined with F1, when it does trade (cheap OTM only) its only broker protection is unreachable.
Worst case realized loss is bounded at the full premium, 0.41% equity/position, 1.23% at
max_concurrent_positions=3 — bounded, but ~5x the equity lane's delivered risk and unrelated to the
underlying-R the proof was gated on.
Also `agent/market_entry_risk.py:466-470` passes `reference = option["debit"]` and side "buy" into
`_entry_execution`, where `executable = option["ask"] == debit`, so `adverse == 0` always: the option entry
slippage cap is a structural no-op.

## F21 [HIGH] CONFIRMED — no symbol-level correlation limit; 3 concurrent positions can be one bet
Universe (config.yaml) is SPY, QQQ, IWM, DIA, XLF, XLK, XLE, XLV — eight highly correlated US index/sector
ETFs. `max_concurrent_positions = 3`. The only duplication guards are
`agent/risk.py:517` ("already holding this symbol") and `agent/market_entry_risk.py:440-443`
("already holding this underlying"). `agent/allocation.py:153-202` limits correlated *edges* (via held-out
per-session R, `CORRELATION_THRESHOLD=0.5`), never correlated *instruments*, and it is skipped entirely
when `self.mode == "live"` (`agent/engine_cycle.py:172-176`).
`max_open_risk_pct = 2.0` therefore sums three ~0.9-correlated positions as if independent. Realized
portfolio risk on a coordinated index move is ~3x the intended per-trade risk, not sqrt(3)x.

## F22 [LOW] Observations
- `agent/strategy.py:114` uses `strategy.max_spread_bps` default 25.0 — a key not present in config.yaml,
  so the rule path's effective spread cap silently differs from `execution.max_spread_bps` (100).
- `agent/startup_edge_policy.py:298-309` `_latest_entry_allowed` compares against a fixed wall clock
  ("15:00") with no relation to the session close; harmless only because `can_enter` also applies the
  calendar-relative cutoff.
- `agent/execution_lifecycle.py:1004-1005`: `reconcile()` falls back to `provider.positions()` +
  `provider.orders()` as two separate un-atomic calls when the provider has no `reconcile`, so a fill
  between them is invisible for that cycle.
