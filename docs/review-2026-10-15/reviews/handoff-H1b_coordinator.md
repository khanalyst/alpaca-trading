# Review handoff: H1b backfill, feed study, provenance, research dataset, snapshot and cache (coordinator review)

Method: done directly by the coordinator, with targeted reading, not line by line.

- Function inventories of `deploy/research_cache.py`, `deploy/research_dataset.py` and `deploy/research_snapshot.py`.
- Pattern search and reading for feed, adjustment and source labelling in `deploy/backfill.py`.
- `deploy/feed_study.py` lines 1 to 45.
- Cache-key identity in `deploy/research_cache.py`.
- The Alpaca stock-bars API reference, for the `adjustment` and `feed` defaults.

`deploy/market_observations.py`, `deploy/profile_recorder_keys.py` and `deploy/provenance.py` were not read beyond their imports and usages.

## Findings

### H1b-01 Backfill never mixes feeds in one corpus, and labels its rows `historical_backfill` (positive, VERIFIED)

`deploy/backfill.py:327-344` resolves the feed (configured or the provider default) and refuses to write into a corpus recorded with a different feed ("use a separate corpus"). Each partition is marked `historical_backfill` (`:388`, `:409`, `:421`), with a truthful `observed_at` (`:13`).

### H1b-02 Bars rely on Alpaca's default `adjustment=raw`, which is correct for intraday trading (INFO, VERIFIED)

- No module sets `adjustment` (grep over `agent/alpaca_market_data.py`, `agent/alpaca_provider.py`, `deploy/recorder*.py`), so alpaca-py sends `None` and the API default `raw` applies (Alpaca stock-bars reference).
- Raw prices match executable prices.
- The only caveat is that multi-session features (same-clock relative volume, ATR across days) would see a split as a jump. The 24 ETFs rarely split.

### H1b-03 `feed_study.py` is a read-only paired IEX versus SIP coverage diagnostic that never falls back between feeds (positive, VERIFIED)

It requires an exact regular-session calendar (`deploy/feed_study.py:20-24`) and rejects feed mismatches and duplicate minutes (`:26-39`).

### H1b-04 Research cache identity includes code identity and inputs; writes are staged, hashed and fsynced (positive, VERIFIED)

`identity_document` and `make_cache_key` (`deploy/research_cache.py:122-160`) bind the code, inputs and context. Publication copies with SHA-256 into staging and fsyncs directories (`:230-353`). Stale caches cannot silently feed a new code version.

### H1b-05 The research data path has no public historical SIP lane (MEDIUM, VERIFIED, cross-group)

The dataset builder and backfill operate on the recorder's own feed (IEX on the free plan). `delayed_sip` is treated as diagnostic-only throughout the documentation (for example `deploy/README.md:12`), yet Alpaca's free plan serves historical consolidated (SIP) bars since 2016, restricted only for the latest 15 minutes (Alpaca market data documentation).

As designed, the system can only learn from what its own recorder has seen since mid-September 2026. Ten years of consolidated one-minute history is unused for hypothesis development.

## Over-engineering

`research_cache.py` (934 lines) implements a content-addressed artefact store with five exception classes, a rename-domain probe and a CLI. `research_snapshot.py` (550 lines) implements symlink-safe snapshotting. These are sound, but they are heavy for a research job that could be re-run from source data.
