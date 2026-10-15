# Review handoff: B_execution_broker

Source: workflow structured result (reviewer could not write files; this file is generated from the journal).

## Files read

| file | lines | read to end |
|---|---|---|
| agent/__init__.py | 0 | yes |
| agent/execution_lifecycle.py | 2964 | yes |
| agent/engine.py | 310 | yes |
| agent/engine_cycle.py | 983 | yes |
| agent/order_timing.py | 34 | yes |
| agent/market_entry_risk.py | 880 | yes |
| agent/risk.py | 867 | yes |
| agent/risk_inputs.py | 272 | yes |
| agent/allocation.py | 256 | yes |
| agent/alpaca_domain.py | 542 | yes |
| agent/alpaca_market_data.py | 580 | yes |
| agent/alpaca_provider.py | 707 | yes |
| agent/alpaca_sdk.py | 191 | yes |
| agent/alpaca_session.py | 218 | yes |
| agent/config.py | 669 | yes |
| config.yaml | 131 | yes |
| main.py | 475 | yes |
| .env.example | 99 | yes |
| deploy/agent.env.example | 9 | yes |

## What this part does (as implemented)

ONE FULL CYCLE, AS IMPLEMENTED (path:line)
1. main.py:250-266 cmd_run builds Engine(cfg) and calls engine.run. Engine.__init__ (agent/engine.py:47-261) resolves the validated edge (107-168), builds AlpacaProvider (169), MarketData with SessionPolicy (175) and RiskEngine on the base config (177). For a non-light engine it then runs preflight, reconcile and _enforce_intraday_cleanup(..., reason="startup_reconciliation", force=True) (engine.py:241-254). Because force=True, this flattens the book on every start.
2. The run loop is in runtime_control.py:603-700 (cross-group). It sleeps to the next wall-clock multiple of cycle.interval_seconds=60 (_aligned_cycle_delay, runtime_control.py:45-53), so cycles start at about hh:mm:00. On any AlpacaError it stops and its finally block flattens (653-670).
3. run_once (engine_cycle.py:161-184) calls _run_once_impl (390-983) in this order:
   - readiness (391-398);
   - full calendar and clock fetch (399-407), then clock validation (408-411, market_entry_risk.py:383-395);
   - REST reconcile of positions and the last 500 orders (412-417; execution_lifecycle.py:1978-2735; alpaca_provider.py:705-707);
   - out-of-session cleanup, which flattens (425-445);
   - session force-flat at close minus 10 minutes via flatten_all("before_close") (446-449);
   - when positions exist, market collection (454-465): one-minute IEX bars from the session open plus quotes for the last 2 minutes, with a 30 s quote freshness limit (market_entry_risk.py:196-339, 213-216, 299-300);
   - position monitor (466; execution_lifecycle.py:1577-1859);
   - account fetch and daily-loss baseline (473-495; market_entry_risk.py:445-500);
   - edge refresh, entry window and latest_entry_time (515-543);
   - durable open-risk and pending-order accounting (560-647).
4. Signal: for each edge and symbol (656-798), _rule_runtime_bars (50-134) requires the following. Every bar must be complete. The newest bar must be from today and have closed no more than 30 s ago (96-98). Bars must be contiguous one-minute bars over the feature window, which is the whole session for opening- or VWAP-anchored families (113-129). generate_rule_signal must fire on the newest completed bar (768-784). The signal is persisted as consumed for that symbol and session BEFORE sizing (786-798).
5. Plan and risk: build_setup_plan (858; agent/strategy.py:131-213) keeps the stop and target anchored to the signal-bar close (agent/contracts/rule.py:2681-2686, 2717). _risk_order (market_entry_risk.py:551-854) then:
   - rejects rule-strategy.v6/v7 (570-575);
   - overwrites entry_price with the current IEX ask or bid (597-605);
   - checks shortable only (615-621);
   - calls RiskEngine.vet_open (risk.py:609-841). vet_open tick-rounds the bracket (686-690), vetoes any stop whose distance from the ask falls below the 30 bps stress floor (713-741), sizes with size_shares (108-149) and applies the open-risk and gross caps (779-786);
   - checks buying power (759-762);
   - always converts the configured market entry into a DAY limit at the quote (811-831);
   - builds a bracket OrderRequest (844-848).
   The engine then re-checks max_concurrent_positions, open risk and gross exposure including pending orders (engine_cycle.py:921-942).
6. Submit: alpaca_provider.submit_order (531-594) does a get_order_by_client_id idempotency lookup (546-550), then posts a LimitOrderRequest with order_class=BRACKET, TakeProfitRequest(limit) and StopLossRequest(stop) (556-567). Any error is wrapped as AlpacaError and re-raised by the cycle (engine_cycle.py:977-978). _record_open_order (execution_lifecycle.py:385-518) persists the order and the broker leg ids (468).
7. Fill tracking: there is no websocket. Each cycle's reconcile updates cumulative fills (2071-2185), activates trades with protective legs (520-927) and refreshes leg statuses (2027-2069). A filled leg becomes the exit (2043-2064).
8. Exits: the broker-resident bracket legs (stop-market and limit target) handle stop and target. The local monitor handles deadlines (max_hold, exit_before, before_close; 111-141, 1605-1608, 1741-1742) and v3-v5 completed-bar transitions with stop replacement (1638-1706, 1419-1575). Each local exit first cancels the legs and confirms them terminal (1098-1252), then sends a market close (1046-1096). Force flat runs flatten_all (runtime_control.py:56-290), which cancels legs per symbol and closes with deterministic "flatten" client ids (197-199).

SIZING (verified by running RiskEngine.vet_open on the shipped config)
- size_shares: shares = min(floor(risk_budget/stop_distance), floor(equity*25%/entry)) (risk.py:123-138).
- With the coordinator's 30 bps stop: raw size is 0.5%/0.3% = 167% of equity, so the 25% notional cap always binds. Planned risk is 25% x 0.30% = 0.075% of equity. Output on $100k: shares=250, risk_usd=75.00, against a configured budget of $500.
- The cap binds for any stop narrower than 200 bps, which includes the v7 100 bps cohorts (0.25% risk).
- max_gross_exposure_pct 50 admits at most two full-size positions. A third is always rejected, and a second is rejected once the first position's market value has drifted above its slack (gross 25,100 gives REJECT). The engine never downsizes to fit.
- max_concurrent_positions 3 is therefore unreachable at full size. max_open_risk_pct 2.0 (maximum about 0.15% in use) and daily_loss_limit_pct 2.0 (about 27 full stop-outs) are effectively non-binding.

ALPACA CORRECTNESS (alpaca-py 0.43.5)
- Order classes: simple (closes, flattens, smoke test) and bracket (entries) only. No OCO or OTO. Stop and target rest at the broker as bracket child legs; they are not simulated, except for the unreachable options profile.
- Time in force: DAY only (alpaca_domain.py:436-439; alpaca_provider.py:452-453). Brackets with DAY are valid, but the legs expire at the close.
- Prices: rounded to $0.01 at or above $1 and $0.0001 below (contracts/risk_geometry.py:110-132). Stops round away from entry and targets toward it.
- client_order_id: capped at 48 characters (alpaca_domain.py:502-503) although Alpaca allows 128. Entry ids are unique per signal. Close and flatten ids are NOT unique (B-01).
- Fractional shares: never used; quantities are integer floors.
- Short selling: shortable is checked; easy_to_borrow is not.
- PDT, day-trade count and day-trading buying power: not handled (B-14).
- Partial fills: tracked with cumulative-average arithmetic (execution_lifecycle.py:548-583).
- Replace and cancel races: handled carefully (1098-1252, 1509-1575).
- 429 and 504: only the alpaca-py defaults apply, 3 retries x 3 s (alpaca/common/constants.py:11-13). A POST retried after a 504 can produce a duplicate client_order_id error.
- 403 and 422: every error becomes AlpacaError and stops the run (B-04).
- Clock: fetched every cycle with zero tolerance for a future server timestamp (B-03).
- Calendar: the full calendar is fetched every cycle (B-13). Early closes come from the calendar close, and DST is handled by ZoneInfo.
- Halts and LULD: no detection, only the 30 s stale-quote filter.
- Opening auction: entries are allowed from 09:30:00 (alpaca_session.py:73), but signals need completed bars.

MARKET DATA
- REST polling only, IEX feed (config.yaml:7).
- Each cycle refetches all session bars for 24 symbols plus 2 minutes of quotes.
- The in-progress bar is excluded (market_entry_risk.py:269, 397-401).
- One fetch per minute, about 0-3 s after the bar closes. A bar that the REST endpoint publishes late is never actionable, because only the newest bar may signal and there is no retry inside the minute.
- strategy.stale_minutes 0.5 is consumed only by the IBR contract (agent/contracts/ibr.py:546-549). The effective freshness limit for rule strategies is execution.max_market_data_age_seconds = 30.
- Spread gates: strategy.max_spread_bps 25 (strategy.py:169-171) and execution.max_spread_bps 100 (risk.py:645-646). These compare against IEX's own BBO, not the NBBO.

## Findings

### B-01 Close and flatten client_order_ids are deterministic per symbol and reason, so the second close of a symbol never reaches the broker
- Severity: CRITICAL  |  Category: broken  |  Confidence: VERIFIED
- Location: `agent/market_entry_risk.py:856-880; agent/execution_lifecycle.py:1051-1052, 1776-1779; agent/runtime_control.py:197-199 (cross-group); agent/alpaca_provider.py:538-550, 503-506`
- Evidence: market_entry_risk.py:860  setup = signal.get("setup_id") or signal.get("signal_ts")
execution_lifecycle.py:1051-1052  client_order_id = self._client_id(
            "close", {"symbol": symbol, "setup_id": reason}, attempt)
alpaca_provider.py:546-550  existing = self.orders(client_order_id=cid)
        if existing:
            self._verify_existing_order(request, existing[0])
            self._submitted_orders[cid] = existing[0]
            return existing[0]
- Why it matters: Close ids are built from the symbol plus a fixed reason string, for example edge-close-spy-max_hold or edge-flatten-spy-before_close. A new trade starts at attempt 0 (closing_attempt is not carried into the new trade row), so the second time any trade on a symbol exits through the same path the id is reused. With 30 bps stops on one-minute bars, max_hold is the dominant exit.
- If the old order had the SAME quantity, the provider returns the old filled order and submits nothing. The monitor then marks the trade as closing on that stale order, and reconcile journals a close (and later the edge outcome) at the old order's price while the position is still open. This corrupts paper evidence and delays the real close by a cycle.
- If the quantity DIFFERS, IdempotencyConflict is raised on every cycle (the monitor retries attempt 0 forever).
- On the force-flat path the conflict escapes flatten_all, stops the run, and falls back to the run_exit and startup_reconciliation ids, which themselves collide on their second use.
- Once those fallbacks are exhausted, the position stays open after its DAY bracket legs expire, giving overnight exposure.
- Suggested fix: Make every close and flatten client_order_id unique per exposure: include the entry order id or setup_id, the session date and the attempt counter (Alpaca allows 128 characters). Keep determinism only within one exposure's retries. Add a test that closes the same symbol twice on different days with the same reason.
- How verified by reviewer: Ran python (locked requirements) -I verification/b_exec/close_id_check.py: an AlpacaProvider whose fake trading client returns yesterday's filled order for the reused id. Output: 'close id: edge-close-spy-max_hold | flatten id: edge-flatten-spy-before_close'; 'held=10: returned order id=old-order-1 status=filled (new submits: 0)'; 'held=12: IdempotencyConflict: client_order_id belongs to a different symbol or quantity (new submits: 0)'. The downstream chains (reconcile journaling the stale fill at execution_lifecycle.py:2353-2412 and 2444-2531, and the overnight cascade) were traced by reading, so those consequences are LIKELY.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### B-02 Bracket stop and target stay anchored to the signal-bar close while entry is re-priced to the live ask: asymmetric veto and collapsing reward-to-risk
- Severity: HIGH  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `agent/market_entry_risk.py:597-605, 811-828; agent/risk.py:686-741; agent/contracts/rule.py:2681-2686, 2717 (cross-group); agent/engine_cycle.py:786-798`
- Evidence: market_entry_risk.py:598-605  entry_reference = _equity_entry_reference(decision, row)
            ...
            decision["entry_price"] = entry_reference
risk.py:740-741  if binding:
                    return None, "stressed_cost_risk_limit"
- Why it matters: The rule computes stop = close - distance and target = close + 2*distance (rule.py:2681-2686). The runtime replaces entry with the current IEX ask but keeps those absolute leg prices.
- For a long whose ask is below the signal close, the stop distance falls under the 30 bps floor and the trade is vetoed.
- For an ask above the close, risk grows and reward shrinks. The max_slippage_bps=50 gate (measured against the close) admits trades whose R falls to about 0.12.
- The rule signal for that symbol and session is consumed before sizing (engine_cycle.py:786-798), so a vetoed signal is lost for the day.
The live sample is therefore a price-path-filtered subset with different R geometry from replay. Paper outcomes cannot validate replay R, and when price has continued, live trades carry systematically worse reward-to-risk than replay.
- Suggested fix: Choose one anchor and use it in both replay and runtime. Either re-anchor stop and target to the executable entry using the authored distance and target_r, or keep close anchors but reject when |ask-close| exceeds a small fraction of R, and model that rejection in replay. Do not consume the symbol-session signal until an order is actually submitted, or model consumption identically in replay.
- How verified by reviewer: Ran verification/b_exec/sizing_check.py (RiskEngine.vet_open, shipped config, close 100.00, stop 99.70, target 100.60). Output: 'ask=99.98: REJECT stressed_cost_risk_limit'; 'ask=100.0: ... R=2.00'; 'ask=100.02: ... R=1.81'; 'ask=100.25: ... R=0.64'; 'ask=100.5: ... stop_dist=0.80 reward=0.10 R=0.12'.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### B-03 Any transient REST read failure, or a millisecond of negative clock skew, liquidates the whole book
- Severity: HIGH  |  Category: wrong-assumption  |  Confidence: VERIFIED
- Location: `agent/engine_cycle.py:399-417, 473-480; agent/market_entry_risk.py:383-395, 502-549`
- Evidence: engine_cycle.py:402-407  except Exception as exc:  # noqa: BLE001
            ...
            return self._fail_closed("calendar_or_clock_unavailable", exc)
market_entry_risk.py:514-515  try:
            complete = self.flatten_all(reason)
market_entry_risk.py:391-392  if age < 0:
            raise AlpacaError("broker clock timestamp is in the future")
- Why it matters: _fail_closed always calls flatten_all, which closes every position at market. It is reached on:
- calendar or clock fetch errors;
- a broker clock timestamp ahead of local time by any amount (the local clock is read after the response, so NTP skew larger than the one-way latency triggers it);
- reconcile errors, account errors and positions errors.
There are about 6 reads per minute over 390 minutes, so even rare transient errors liquidate positions that are already protected by broker-resident bracket legs. Each flatten pays spread and slippage, and the closes are booked as paper outcomes with infrastructure exit reasons (execution_lifecycle.py:2497-2509 feeds _record_edge_outcome). That contaminates the edge ledger with exits the strategy never made.
- Suggested fix: Separate entry blocking from liquidation: on read failures, block new entries and rely on the resting bracket legs, and flatten only after N consecutive failures or close to the force-flat time. Allow a small negative clock tolerance (for example 2 s). Tag infrastructure-forced exits and exclude them from edge evidence.
- How verified by reviewer: Read the call chain end to end: engine_cycle.py:399-417 and 473-480 call _fail_closed, which calls flatten_all at market_entry_risk.py:515. How often it happens in production is not measured.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### B-04 A single broker rejection or ambiguous submit on one entry stops the trader and flattens every other position
- Severity: HIGH  |  Category: incorrect  |  Confidence: VERIFIED
- Location: `agent/engine_cycle.py:947-978; agent/alpaca_provider.py:587-592; agent/runtime_control.py:653-670 (cross-group)`
- Evidence: engine_cycle.py:952  order = self.provider.submit_order(request)
engine_cycle.py:977-978  except AlpacaError:
                    raise
runtime_control.py:670  complete = self._flatten_all_impl(exit_reason)
- Why it matters: submit_order wraps every SDK error as AlpacaError (alpaca_provider.py:591-592). The cycle re-raises it, and run() logs, stops, and its finally block flattens all positions with reason run_exit. Routine Alpaca responses therefore liquidate unrelated, protected positions and halt trading:
- 403 insufficient buying power, PDT protection or a non-ETB short;
- 422 bracket price validation;
- a duplicate client_order_id after alpaca-py retries a POST on 504 (3 retries x 3 s by default).
This compounds B-01, since run_exit flatten ids also collide.
- Suggested fix: Classify submit failures. A definite 4xx rejection should be journalled, should block that symbol or signal, and the cycle should continue. An ambiguous outcome (timeout or 5xx) should be resolved through get_order_by_client_id before deciding anything. Never flatten unrelated positions because one entry failed.
- How verified by reviewer: Read engine_cycle.py:947-978, alpaca_provider.py:531-594, and runtime_control.py:648-700 for the loop and its finally block. The alpaca-py retry defaults were read in alpaca/common/constants.py:11-13 and common/rest.py:127-134 and 200-202.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### B-05 The runtime stressed-cost veto is set to exactly the 30 bps grammar floor, below the configured expected round-trip cost
- Severity: HIGH  |  Category: wrong-assumption  |  Confidence: VERIFIED
- Location: `config.yaml:52-53, 66-69; agent/risk.py:250-259, 713-741; research/costs.py:677-683, 1660 (cross-group); .env.example:86-90`
- Evidence: config.yaml:52-53  "stressed_cost_scenario_bps": 9.0,
    "max_stressed_cost_to_risk_ratio": 0.30,
research/costs.py:1660  stressed = abs(notional) * scenario / 10_000.0
.env.example:89-90  # 9/15/25/50 bps; 25 bps is the authorization scenario.
- Why it matters: The stress charge is 9 bps of ENTRY notional only, and 9/0.30 = 30 bps is exactly MIN_STOP_DISTANCE_BPS. The gate therefore admits every 30 bps stop (ratio 0.300) and adds nothing. The configured expected round trip is 2 x (4/2 + 6) + 2 x 0.5 = 17 bps (costs.py:677-683, 698-700), which is 0.57R on a 30 bps stop, nearly double the 0.30 ratio the gate claims to enforce. Under the 25 bps scenario that .env.example calls the authorisation scenario (and the DEFAULT_CONFIG default, config.py:86), every 30 bps stop is vetoed.
- Suggested fix: Charge the stress on the round trip (entry plus exit) and at least the configured expected cost. Pick one documented authorisation scenario. If 25 bps is the real one, the 30 bps-stop strategies are not tradeable and should not be paper-traded.
- How verified by reviewer: Ran verification/b_exec/stress_check.py. Output: 'shipped scenario/ratio: 9.0 0.3 -> floor bps 30.0'; 'scenario 9.0 bps: ... OK ratio=0.300'; 'scenario 15.0 bps: floor 50.0 bps -> stressed_cost_risk_limit'; 'scenario 25.0 bps: floor 83.3 bps -> stressed_cost_risk_limit'.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### B-06 The notional cap makes realised risk about 0.075% of equity, not 0.5%; the gross cap makes 3 concurrent positions unreachable and the other risk limits non-binding
- Severity: MEDIUM  |  Category: wrong-assumption  |  Confidence: VERIFIED
- Location: `agent/risk.py:123-149, 779-786; agent/engine_cycle.py:921-942; config.yaml:43-48`
- Evidence: risk.py:123-126  raw = math.floor(budget / distance)
        cap_pct = _num(self.r.get("max_position_notional_pct"), 100.0) or 100.0
        notional_cap = equity * cap_pct / 100.0
        cap_shares = math.floor(notional_cap / entry)
risk.py:785-786  if gross_cap_pct is not None and gross + float(plan["notional"]) > equity * gross_cap_pct / 100.0 + 1e-9:
            return None, "max gross exposure cap reached"
- Why it matters: With the 30 bps stop (coordinator fact 2), the uncapped size is 167% of equity, so every trade is capped at 25% notional and planned risk is 0.075% of equity. The cap binds for every stop narrower than 200 bps, including the v7 100 bps cohorts.
- max_gross_exposure_pct 50 allows at most two positions. The third is always rejected. The second is rejected as soon as the first has gained more than its rounding slack, because the engine rejects rather than downsizes.
- max_concurrent_positions 3, max_open_risk_pct 2.0 (at most 0.15% in use) and daily_loss_limit_pct 2.0 (about 27 stop-outs) therefore never bind.
- Any documentation or evidence that speaks of '0.5% risk per trade' overstates risk by about 6.7x.
- Concurrency rejections consume the symbol-session signal (B-02), so the live sample shrinks relative to replay.
- Suggested fix: Make the risk block internally consistent: either raise the notional and gross caps, or state risk_per_trade as effectively min(0.5%, 25% x stop%). Consider downsizing to the remaining gross capacity instead of rejecting. Have replay apply the same caps when counting trades.
- How verified by reviewer: verification/b_exec/sizing_check.py output: 'ask=100.0: shares=250 notional=25000 risk_usd=75.00 (0.075% eq) budget=500'; 'gross=24990.0: OK shares=249'; 'gross=25100.0: REJECT max gross exposure cap reached'; 'gross=49980.0: REJECT max gross exposure cap reached'.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### B-07 Every process start, including a restart mid-position, flattens the whole book
- Severity: MEDIUM  |  Category: wrong-assumption  |  Confidence: VERIFIED
- Location: `agent/engine.py:252-254; agent/startup_edge_policy.py:474-479, 492-500 (cross-group)`
- Evidence: engine.py:252-254  self._enforce_intraday_cleanup(
                        self._preflight.get("clock") if self._preflight else None,
                        reason="startup_reconciliation", force=True)
startup_edge_policy.py:495  if self._inside_regular_session(clock) and not force:
- Why it matters: With force=True the regular-session short-circuit is skipped, so a supervisor restart, crash recovery or deploy at 11:00 liquidates protected positions at market. That costs spread and slippage and truncates each trade's evidence. Combined with B-01, the repeated startup_reconciliation flatten id fails on its second use for a symbol. Cleanup then marks itself incomplete and sets operator_pause, leaving positions unmanaged once the DAY bracket legs expire.
- Suggested fix: During the regular session, re-adopt reconciled positions that still have live broker legs, and flatten only unknown or unprotected exposure. Keep forced cleanup for out-of-session starts.
- How verified by reviewer: Read engine.py:233-261 and startup_edge_policy.py:429-516.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### B-08 Unfilled entry limits never expire before force flat, so stale or adversely selected fills can arrive hours later
- Severity: MEDIUM  |  Category: missing  |  Confidence: VERIFIED
- Location: `agent/market_entry_risk.py:821-828; agent/execution_lifecycle.py:703-723; agent/engine_cycle.py:609-635`
- Evidence: market_entry_risk.py:821-828  if order_type == "market":
                executable = self._number(
                ...
                order_type, limit = "limit", Decimal(str(executable))
- Why it matters: Every entry is a DAY limit at an IEX quote that can be up to 30 s old. When price has moved away, which is most likely for momentum and breakout winners, the order rests. No code path cancels an unfilled entry: grep finds cancel_order only for protective legs (execution_lifecycle.py:1110), and the order is cleared only by the session flatten. A late fill happens only when price returns to the limit, which is adverse selection. For v3-v5 schemas the hold deadline is re-anchored to the fill time (703-723), so a signal from hours earlier is held for a full max_hold window. Replay presumably assumes immediate fills (cross-group question).
- Suggested fix: Give entries a time-to-live (for example 1 bar): cancel if not filled by signal_ts plus 120 s, and confirm the cancel through the broker. Alternatively use IOC-style marketable limits (Alpaca brackets require DAY or GTC, so cancel explicitly). Model non-fills in replay and shadow.
- How verified by reviewer: grep -rn 'cancel_order(' agent/*.py returns only execution_lifecycle.py:1110 (leg cancellation). The run-loop paths in engine_cycle and runtime_control were read in full. The adverse-selection impact is LIKELY.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### B-09 One non-DAY, non-standard or crypto order in the last 500 permanently breaks reconcile; fail-closed flatten then also fails and the trader pauses with positions open
- Severity: MEDIUM  |  Category: broken  |  Confidence: VERIFIED
- Location: `agent/alpaca_provider.py:371, 409-414, 446-453; agent/execution_lifecycle.py:2003-2007`
- Evidence: alpaca_provider.py:452-453  if tif != "day":
            raise AlpacaError("broker order time_in_force must be day")
- Why it matters: orders(status="all") normalises every one of the last 500 orders, terminal ones included, through _order. That function raises on any time_in_force other than day, any type outside market, limit, stop or stop_limit (for example trailing_stop), or any crypto row. A single manual GTC test order in the paper UI therefore makes every reconcile fail. That leads to _fail_closed, whose flatten_all calls provider.orders() and fails the same way, which leads to operator_pause. Positions are left with DAY legs that expire at the close. The condition persists until 500 newer orders push the bad row out.
- Suggested fix: Skip or quarantine terminal historical orders that the runtime did not create (match by client id prefix). Only fail closed on non-conforming WORKING orders for universe symbols.
- How verified by reviewer: Ran verification/b_exec/history_check.py with a fake trading client returning one historical filled GTC order. Output: 'AlpacaError broker order time_in_force must be day' from AlpacaProvider.reconcile().
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### B-10 No HTTP timeout on any broker or data call, so a hung socket freezes the loop, including force flat
- Severity: MEDIUM  |  Category: missing  |  Confidence: LIKELY
- Location: `agent/alpaca_session.py:157, 168, 179; site-packages/alpaca/common/rest.py (alpaca-py 0.43.5):69, 195`
- Evidence: alpaca_session.py:157  self._trading = TradingClient(self.api_key, self.secret_key, paper=self.paper)
alpaca/common/rest.py:195  response = self._session.request(method, url, **opts)
- Why it matters: alpaca-py uses a bare requests.Session with no timeout, and the agent configures none (grep for 'timeout' in alpaca_session.py, alpaca_provider.py and alpaca_market_data.py finds no client timeout). A half-open TCP connection blocks run_once indefinitely, so the 15:50 force flat does not run. The watchdog stays inert while the hung trader holds the run lock (deploy/watchdog.py:11-20). Recovery depends entirely on deploy/trader_supervisor.py (cross-group, not verified).
- Suggested fix: Mount a requests adapter with connect and read timeouts on the SDK sessions (for example 5 s and 15 s), or wrap calls in a deadline. Make the force-flat deadline independent of the main cycle.
- How verified by reviewer: Read the alpaca-py 0.43.5 RESTClient source (rest.py:69, 127-134, 195) and the agent session construction. The supervisor's mitigation was not traced.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### B-11 Runtime refuses rule-strategy.v6 and v7, so the cost-compatible 100 bps cohorts can never be paper-traded
- Severity: MEDIUM  |  Category: incomplete  |  Confidence: VERIFIED
- Location: `agent/market_entry_risk.py:570-575`
- Evidence: if (str(decision.get("rule_schema") or "") in {RULE_SCHEMA_V6, RULE_SCHEMA_V7}
                or str((strategy.get("rule_spec") or {}).get("schema") or "")
                in {RULE_SCHEMA_V6, RULE_SCHEMA_V7}):
            self._event("risk_reject", {
                "symbol": symbol, "reason": "diagnostic_rule_not_executable"})
- Why it matters: Coordinator fact 3: the mechanism cost, deadline and refresh cohorts use v7 with 100 bps stops, the only geometry where costs are a small fraction of R. Every v6 or v7 signal is rejected after it has been consumed for the symbol-session. Any positive v7 research result therefore cannot reach the paper lane without code changes. By contrast, the 24 paper-trial catalog arms are all rule-strategy.v4 (checked with agent.paper_trial._logical_arms).
- Suggested fix: Either add a v7 execution path (bracket geometry, deadlines and the stress gate are already distance-generic), or make the research and promotion pipeline refuse to promote v6 or v7 so results are not mistaken for deployable edges.
- How verified by reviewer: Read market_entry_risk.py:570-575. Ran python -c over agent.paper_trial._logical_arms(), which printed 24 arms, all 'rule-strategy.v4'.
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### B-12 Runtime signal gating needs gap-free IEX minute bars (whole session for anchored families) and the newest bar fetched once per minute; one missing minute disables a symbol
- Severity: MEDIUM  |  Category: wrong-assumption  |  Confidence: LIKELY
- Location: `agent/engine_cycle.py:96-98, 113-129, 774-784; agent/contracts/rule.py:376-379 (cross-group); agent/runtime_control.py:45-53, 661`
- Evidence: engine_cycle.py:113  feature_rows = session_rows if window is None else session_rows[-int(window):]
engine_cycle.py:127-129  if any(right[0] - left[0] != timedelta(minutes=1)
           for left, right in zip(feature_rows, feature_rows[1:])):
        return None
- Why it matters: For opening-anchored and session-VWAP families, feature_window_bars returns None (rule.py:376-379), so the whole session prefix must be contiguous. On the IEX feed, any minute without an IEX print has no bar, and that symbol cannot signal for the rest of the day. Separately, cycles fire at about hh:mm:00 and only the newest completed bar (closed no more than 30 s ago) may signal. If the REST endpoint publishes a bar after the cycle's fetch, that bar is never evaluated. Together these filter which sessions and symbols can ever trade, toward the most liquid IEX names and the minutes where data arrives promptly.
- Suggested fix: Measure IEX bar coverage per symbol from the recorder corpus. Consider the SIP feed, or treat zero-volume minutes as flat bars in both replay and runtime. Re-poll within the minute when the expected newest bar is missing.
- How verified by reviewer: The code path was read end to end (VERIFIED). How common IEX gaps and REST publication lag are is UNVERIFIED: this container has no corpus (coordinator fact 6).
- Independent verification: the two-verifier stage was not run (usage limits). CRITICAL and HIGH findings were re-verified by the coordinator (README, section 2.4).

### B-13 The full market calendar (all years) is downloaded every cycle
- Severity: LOW  |  Category: incorrect  |  Confidence: LIKELY
- Location: `agent/engine_cycle.py:400; agent/alpaca_provider.py:229-237`
- Evidence: engine_cycle.py:400  calendar = self.market.refresh_calendar()
alpaca_provider.py:232-235  kwargs = {}
            if start is not None: kwargs["start"] = start
            if end is not None: kwargs["end"] = end
            request = GetCalendarRequest(**kwargs)
- Why it matters: With no start or end, GET /v2/calendar returns the whole multi-decade calendar, each minute. This adds latency before data collection (worsening B-12) and enlarges the failure surface that triggers B-03.
- Suggested fix: Fetch today plus or minus a few days once per session and cache it.
- How verified by reviewer: Read the call chain (MarketData.refresh_calendar with no arguments, agent/market.py:102-104). The full-calendar behaviour of the Alpaca endpoint was not checked against the network.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### B-14 PDT, day-trade count, day-trading buying power and easy_to_borrow are never consulted
- Severity: LOW  |  Category: missing  |  Confidence: VERIFIED
- Location: `agent/alpaca_provider.py:278-294; agent/market_entry_risk.py:615-621, 759-762`
- Evidence: alpaca_provider.py:278  pattern_day_trader = _value(value, "pattern_day_trader", None)
market_entry_risk.py:616-618  if (decision["execution_profile"] == "shares" and
                decision.get("direction") == "short" and asset is not None and
                not bool(_value(asset, "shortable", False))):
- Why it matters: Every trade is a day trade. On a live account under $25k, Alpaca's PDT protection rejects orders after 3 day trades in 5 days. Non-ETB shorts are also rejected. Through B-04, such a rejection flattens the book. The pattern_day_trader field is parsed but unused, and day-trading buying power is ignored (only buying_power is checked).
- Suggested fix: Read daytrade_count and daytrading_buying_power. Refuse entries when the account is under $25k and the day-trade count is 3 or more, and require easy_to_borrow for shorts.
- How verified by reviewer: grep -rn 'pattern_day_trader|daytrade|easy_to_borrow' agent/*.py finds no consumer outside parsing. The consequence is SPECULATIVE for paper accounts at $100k.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### B-15 'stopped' and 'suspended' are treated as terminal, so a close may be resubmitted while the first is still live
- Severity: LOW  |  Category: incorrect  |  Confidence: LIKELY
- Location: `agent/execution_lifecycle.py:31-34, 1597-1598`
- Evidence: _TERMINAL_ORDER_STATUSES = {
    "filled", "canceled", "cancelled", "expired", "rejected", "replaced", "stopped",
    "suspended", "failed", "not_found",
}
- Why it matters: Alpaca's 'stopped' status means a fill is guaranteed but has not occurred. The monitor only skips a live close when saved_status is non-terminal (1597-1598), so it would send a second close, risking an accidental short. This is rare.
- Suggested fix: Treat stopped, suspended, pending_cancel and pending_replace as working states.
- How verified by reviewer: Read the status set and the monitor retry logic. The status is rare in practice.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### B-16 Config keys that are validated but have no runtime effect under the shipped rule strategy
- Severity: LOW  |  Category: dead-code  |  Confidence: VERIFIED
- Location: `config.yaml:16, 21, 31-38, 58, 64; agent/config.py:238, 290-307; agent/alpaca_session.py:44-48`
- Evidence: config.yaml:21  "min_price": 1.0,
config.yaml:38  "stale_minutes": 0.5,
config.yaml:58  "order_type": "market",
- Why it matters: These keys suggest controls that do nothing:
- universe.min_price is never read (grep finds only config.py).
- strategy.range_minutes, breakout_buffer_bps, min_relative_volume, target_r, max_entry_extension_r, min/max_ibr_width_atr and stale_minutes are consumed only by the IBR path (agent/contracts/ibr.py:546-549, agent/strategy.py:268-301), not by strategy.id=rule.
- execution.order_type 'market' is always converted to a limit (market_entry_risk.py:821-828).
- session.reject_new_entries_minutes_before_close (5) is dominated by the force flat at 10 minutes, which runs first (engine_cycle.py:446-449).
- session.require_exact_calendar is documented as a compatibility-only field (alpaca_session.py:44-48).
- execution.strict_market_data is not read by the runtime.
The brief's 'entry is market order' is doc drift.
- Suggested fix: Remove inert keys or reject them when strategy.id=rule. Rename order_type to reflect marketable-limit semantics.
- How verified by reviewer: grep over agent, research, deploy and main.py for each key (outputs reviewed). Engine and strategy call paths were read.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

### B-17 Tests assert close and flatten id determinism but never exercise a second exposure reusing the same id
- Severity: LOW  |  Category: test-gap  |  Confidence: VERIFIED
- Location: `tests/test_runtime_safety.py:1057-1069`
- Evidence: first = engine._client_id("flatten", signal, attempt=0)
        ...
        self.assertEqual(first, engine._client_id("flatten", signal, attempt=0))
- Why it matters: The suite (coordinator fact 1: runtime 994 tests OK) encodes the determinism that causes B-01 but has no scenario of two trades on one symbol across sessions, so the CRITICAL defect passes CI.
- Suggested fix: Add an engine-level test: open, max_hold close, next-day open and max_hold close on the same symbol with a fake broker that keeps order history and enforces client_order_id uniqueness.
- How verified by reviewer: grep for close and flatten id patterns in tests; read tests/test_runtime_safety.py:1057-1069.
- Independent verification: not run for LOW findings (reviewer confidence label applies).

## Over-engineering

Code that is unreachable or serves no decision under the shipped config, quantified:
1) Options execution stack, about 1,450 lines. Unreachable because strategy.execution_mode=shares, options_feed=indicative, and _risk_order rejects non-OPRA options (market_entry_risk.py:622-633). It comprises:
   - risk.py:151-607 (select_option_contract, size_options, about 457 lines);
   - all of risk_inputs.py (272 lines);
   - alpaca_market_data.py:134-197 and 299-580 (about 345 lines);
   - alpaca_domain.py OptionContract, OptionSnapshot and parse_occ_symbol (about 190 lines);
   - execution_lifecycle.py:929-1044 (option take-profit, about 116 lines);
   - market_entry_risk.py:622-655 and 770-805 (about 70 lines).
2) Multi-edge allocation, about 460 lines: allocation.py (256 lines, opens two SQLite ledgers per cycle) plus engine_cycle.py:186-388 (_allocate_edges, _edge_runtime_opportunity, _prefilter_edge_opportunities). Only used for selection_mode=all_proved; the shipped value is specific (config.yaml:29). _edge_runtime_opportunity (250-319) duplicates the main rule pipeline (676-798).
3) Provenance plumbing, about 400 lines of repetitive field copying. The same 30 or more feed, provider, age and risk-ratio fields are copied five times: execution_lifecycle.py:425-511, 724-800, 874-919, 1907-1975, 2608-2676 and 2785-2841. A single dataclass would remove most of it.
4) Compatibility shims for fakes and old SDKs, about 120 lines:
   - TypeError and ImportError fallbacks in alpaca_provider.calendar, orders, assets and submit (236-249, 265-269, 376-397, 577-585) and execution_lifecycle._close_position (1058-1062);
   - facade forwarders in alpaca_market_data.py:41-102 and engine_cycle.py:137-152, kept only as monkeypatch seams.
5) Two spread gates (strategy.max_spread_bps 25 and execution.max_spread_bps 100). The looser one never binds for rule plans.
6) Per-cycle network waste: the full calendar every minute (B-13); the session's full 24-symbol bar history every minute instead of incremental bars; a get_order_by_client_id round trip before every submit.

Positive notes:
- the leg-cancel race handling (execution_lifecycle.py:1098-1252);
- the stop-replacement crash recovery (1509-1575);
- cumulative partial-fill accounting (548-583).
These are careful and appropriate.

## Cross-group questions

- A_signals / F1_factory_core: does rule replay enter at the signal-bar close, the next-bar open, or a quote? The runtime keeps the stop and target anchored to the close (agent/contracts/rule.py:2681-2686) but enters at the live ask about 1-3 s later and vetoes whenever ask < close for longs (agent/risk.py:713-741). Does replay model that veto and the reward-to-risk shrink (B-02)? Does replay consume a symbol-session signal even when it is not traded, as the runtime does at agent/engine_cycle.py:786-798?
- E_costs_data: research/costs.py stressed_cost_usd (line 1660) charges entry notional only. config.yaml uses 9 bps while .env.example:89-90 says 25 bps is the authorisation scenario and agent/config.py:86 defaults to 25. Which value do research/gates.py cost_stress_report and the evidence epochs actually use? At 25 bps every 30 bps stop is vetoed at runtime (B-05).
- C_state_governance_trial (agent/runtime_control.py:197-199, agent/startup_edge_policy.py:492-500): please confirm the flatten client_order_id collision (B-01) and that startup always flattens (B-07). Also check deploy/watchdog.py:221-222: flatten_all('supervisor_terminated_unresponsive_trader') reuses the same id scheme and collides on its second action for a symbol.
- H2_ops_infra: does deploy/trader_supervisor.py detect and kill a trader hung inside an alpaca-py request (no HTTP timeout, B-10) early enough to make the 15:50 force flat? How does it behave across the restart-flatten loop (B-07) and the operator_pause set by failed flattens?
- H1_recorder_data: from the recorder corpus, what fraction of regular-session minutes have no IEX bar for each of the 24 ETFs? Runtime requires contiguous bars over the whole session for opening- and VWAP-anchored families (agent/engine_cycle.py:113-129), so this decides how many symbol-sessions can ever signal (B-12).
- A_signals / F1 / G1_live_shadow: do replay and shadow apply max_position_notional_pct 25, max_gross_exposure_pct 50 and max_concurrent_positions 3 when counting trades and computing R? The runtime rejects rather than downsizes (agent/engine_cycle.py:921-942), so the paper trade sample will be smaller than replay's (B-06).
- G1_live_shadow / E: is a non-fill of the marketable DAY limit at a quote up to 30 s old modelled anywhere? The runtime never cancels unfilled entries before force flat (B-08).
- F2/F3 edge ledger: paper outcomes ingested through _drain_edge_outbox (agent/execution_lifecycle.py:2900-2964) include exits forced by infrastructure fail-closed flattens (B-03) and possibly stale-price closes from reused client ids (B-01). Does the ledger or trial review filter by exit_reason or close_evidence?
- J1a/J1b tests: add an engine-level test of two exposures on the same symbol across sessions with a broker fake that keeps order history and enforces client_order_id uniqueness (B-17). Add a test that one rejected entry does not flatten other positions (B-04).

## Summary

- Report was NOT written to disk: the harness Write tool refused report files from this subagent, and this structured object is the complete report. Findings by severity: CRITICAL 1, HIGH 4, MEDIUM 7, LOW 5 (17 total).
- B-01 CRITICAL (VERIFIED by fake-broker run): close and flatten client_order_ids are deterministic per symbol and reason (edge-close-spy-max_hold, edge-flatten-spy-before_close). The second exposure on a symbol either gets yesterday's filled order back with nothing submitted (same quantity, so a stale fill is journalled and the position stays open) or raises IdempotencyConflict forever (different quantity). The fallback ids collide too, ending in overnight exposure once the DAY bracket legs expire.
- B-02 HIGH (VERIFIED): stop and target stay anchored to the signal-bar close while entry is re-priced to the live IEX ask. Longs with ask < close are vetoed; ask > close shrinks reward-to-risk to as low as 0.12 within the 50 bps slippage gate. The symbol-session signal is consumed before sizing, so the live sample is a filtered, geometrically different subset of replay.
- B-03 / B-04 HIGH (VERIFIED by reading): any transient calendar, clock, reconcile or account read failure, or a broker clock a few ms ahead of local time, calls _fail_closed and flattens the whole book. Any single submit rejection (403, 422, or a 504-retry duplicate id) stops the run, and its finally block flattens every position. Both cost money and inject infrastructure exits into edge evidence.
- B-05 HIGH (VERIFIED): the runtime stress veto uses 9 bps on entry notional only, which is exactly the 30 bps floor (ratio 0.300), while the configured expected round trip is 17 bps, about 0.57R. At the documented 25 bps authorisation scenario every 30 bps stop would be vetoed.
- B-06 MEDIUM (VERIFIED): the 25% notional cap makes realised risk 0.075% of equity instead of 0.5% (shares=250, risk $75 on $100k). The 50% gross cap allows at most 2 positions and often only 1 (reject, not downsize). max_concurrent_positions 3, max_open_risk 2% and the 2% daily loss limit are effectively unreachable.
- Alpaca integration otherwise: true broker-resident bracket legs (stop-market plus limit target, DAY TIF), cent rounding, integer shares, careful cancel and replace race handling. Gaps: no HTTP timeouts (B-10), no stale-entry expiry (B-08), no PDT, day-trade buying power or ETB checks (B-14), the full calendar fetched every minute (B-13), and a historical non-DAY order permanently breaking reconcile (B-09, VERIFIED).
- Market data is REST polling of IEX once per wall-clock minute. Signals act only on the newest completed bar if it is no more than 30 s old. Session-anchored families need a gap-free IEX session, so one missing minute disables a symbol (B-12). strategy.stale_minutes and the other IBR keys, universe.min_price and order_type=market have no effect on the rule runtime (B-16).
- rule-strategy.v6 and v7 (the 100 bps cost-compatible cohorts) are rejected at runtime as diagnostic_rule_not_executable, so those research results cannot be paper-traded (B-11). Paper-trial arms are all v4.
- About 1,450 lines of options code and about 460 lines of all_proved allocation are unreachable under the shipped config, and about 400 lines are repetitive provenance plumbing.
