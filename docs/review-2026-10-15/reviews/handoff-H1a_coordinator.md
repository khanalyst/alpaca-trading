# Review handoff: H1a market-data recorder (coordinator review)

Method: done directly by the coordinator, with targeted reading only (not line by line).

- `deploy/recorder.py` (3,028 lines): header, constants (111 to 149), the forward-lag and gap policies (2289 to 2380), bar continuity (1945 to 2045) and the forward-capture window (2620 to 2660).
- `deploy/recorder_market.py` (531 lines): request shapes (83 to 106) and bar projection (412 to 500).
- The Compose interval setting.

## Findings

### H1a-01 Recorder timestamps are causal and honest (positive, VERIFIED)

- **In-progress bars.** Bars still forming are skipped: `if bar_complete > now: continue`, with the comment "Never freeze an in-progress OHLC row" (`deploy/recorder_market.py:458-462`).
- **`as_of`.** Each bar's `as_of` is its completed minute boundary (`:468`).
- **`observed_at`.** It is the actual receipt time of each source, kept separate for bars and quotes, so a slow paginated quote response does not make bars look late (`:433-443`, `:487-492`).
- **Row checks.** Every row's feed and symbol are asserted against the request (`:449-451`).
- **Late observations.** A row first observed more than 15 minutes after it became available marks its partition as historical, not forward-observed (`deploy/recorder.py:2289-2330`; `DEFAULT_FORWARD_OBSERVATION_MAX_LAG_MINUTES = 15`). A row "observed before it became available" (more than 5 seconds early) is a hard error.
- **Polling.** The authorising interval is capped at 30 seconds (`:143`, `:2881`). Compose uses 30 seconds.

### H1a-02 The recorder tolerates IEX gaps, but session acceptance downstream does not (MEDIUM, cross-group, VERIFIED)

The default bar-gap policy is `observe`. A missing IEX minute is recorded as coverage evidence, with gap counts and the longest gap per symbol, and is fatal only when `ALPACA_RECORDER_STRICT_BAR_FEEDS` names the feed (`deploy/recorder.py:2347-2372`, `:1973-2042`; gap threshold 5 minutes).

This is the right behaviour for IEX, where thinner ETFs often have no prints in a minute. But session acceptance rejects a session for any missing sample (C2-02), so the evidence the recorder carefully keeps is then discarded at session level. The two components disagree about what a usable session is.

Suggested fix: define one session-quality rule, for example at most N missing minutes per symbol and no gap over 5 minutes in the decision window. Apply that same rule in the recorder, in acceptance and in shadow ingestion (G1c-01).

### H1a-03 Bar revisions are only observed within about one poll of completion (LOW, VERIFIED)

In forward-only mode, the request starts at `now - window`, where the window is 1 minute (`DEFAULT_FETCH_WINDOW_MINUTES = 1`), and the bar request is rounded back to the start of the minute (`deploy/recorder.py:2632-2636`; `deploy/recorder_market.py:419-424`).

With 30-second polling, each completed bar is fetched about twice, within about 65 seconds of completion. A correction that Alpaca applies later is never seen. For a single-venue IEX feed, late corrections are rare, so the impact is small. Revisions that are seen are kept as separate observations (`deploy/recorder.py:2406-2411`).

### H1a-04 REST polling forces a large amount of bespoke state machinery (MEDIUM, over-engineering, VERIFIED by structure)

The recorder maintains:

- a JSON index sidecar with a watermark;
- a SQLite recent-key deduplication index (15-minute horizon);
- partition calendar and partition source markers;
- pending and deferred quote windows with catch-up intervals;
- a storage preflight;
- header migration and provenance repair commands.

That is 89 functions and classes in `recorder.py` alone. Most of this exists to make overlapping 30-second REST polls of paginated quote history idempotent and causal.

Two alternatives:

- **Alpaca's real-time WebSocket stream.** It pushes completed minute bars, updated bars and quotes as they happen. The free plan's IEX stream allows up to 30 symbol subscriptions, which covers the 24-ETF universe (Alpaca market data documentation).
- **Free historical consolidated (SIP) bars** for research (H1b-05). With these, the recorder would only need to capture what history cannot provide, namely the quotes actually visible at decision time.

## Summary

- The recorder is careful about time and provenance, which is the part that matters most.
- Its complexity follows from the polling design, and its tolerance of gaps is overridden by a stricter downstream acceptance rule.
