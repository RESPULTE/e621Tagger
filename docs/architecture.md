# Architecture

React and FastAPI share one origin. A single Uvicorn process owns the metadata scheduler, shared HTTP pools, media permits, job workers, SQLite connection and process lock. Shutdown cancels workers before closing clients and database. No neural model is loaded.

`api.py` owns HTTP contracts and candidate queries. `reviews.py` owns transitions, receipts and undo. `jobs.py` owns discovery and durable jobs. `e621.py` owns metadata scheduling and tag resolution. `media.py` owns URL validation, thumbnail caching and original streaming. `auth.py` owns password verification and persisted sessions.

SQLite uses WAL, foreign keys and a busy timeout. An async lock serializes short transactions and reads on one aiosqlite connection; network I/O never occurs inside a transaction. Schema version 2 adds source-bound preview fingerprints, cache source identity and immutable dataset manifests. Existing v1 databases receive an integrity-checked online backup before the additive transaction. Newer databases are rejected.

## Correctness

- Each project/post has one candidate record and multiple discovery provenance records. MD5 is retained without silently merging reviews.
- Labels store positive, negative or unsure. Unreviewed has no label record. Candidate revisions persist after undo to reject stale edits.
- Every transition appends an event. Undo requires the current event/version and appends a new event. Batches and receipts commit atomically.
- Request UUIDs bind to fingerprints and stored responses. Identical retries replay; reusing an ID with another operation returns 409.
- Discovery commits metadata, candidates, provenance, cursor and job completion together. Jobs have deduplication keys, bounded retries, renewable leases and restart recovery.

## Resource limits

One metadata scheduler coalesces requests and prioritizes interactive searches in a queue of 64. Requests start at least one second apart, with four bounded attempts. Cooldowns persist across restarts and honor Retry-After.

Media uses four shared permits, per-host request spacing/backoff, and at most 64 queued thumbnail jobs. A missing thumbnail returns 202 while queued; the frontend retries. Only visible rows plus one viewport of overscan mount and prefetch. Candidate pages default to 50 and cap at 100; the UI does not retain every candidate.

At the default four permits, previews and originals each have a three-request cap so neither can occupy the entire shared budget. A single configured permit remains serial. Host locks serialize request starts only, not response-header waits; sleepers recheck cooldown when a concurrent response extends Retry-After. Warm-cache LRU timestamps update at most once per minute; byte-length and SHA-256 checks remain intact. `media_timings` reports bounded samples of slot wait, host wait, upstream headers and preview readiness. These measurements contain no URLs or post IDs.

Discovery responses identify their search run. Candidate views can filter by that run without deleting any candidates or labels. Job results count new versus existing project candidates; frontend invalidation follows job transitions as well as total-count changes, so duplicate-only imports refresh provenance.

Preview downloads cap at 8 MiB and 20 million pixels, decode off the event loop, and resize to 320 px WebP. Atomic rename precedes the valid-cache marker. SHA-256 detects corrupt derivatives. The default 5 GiB LRU cache evicts unreviewed thumbnails first, then reviewed thumbnails if required by the hard quota.

Originals and video stream in bounded chunks, bypassing disk cache and Pillow. Range/If-Range and range response headers are preserved. Browsers decode media; MP4 fallbacks come from upstream metadata. Every media URL and redirect requires HTTPS and an exact approved CDN host. There is no transcoder.

## Deferred

`quality.py` owns deterministic grouping and eligibility; `datasets.py` owns snapshots, version publication and export. See [dataset policy](datasets.md). Embedding retrieval, model runs, GPU transport, Hydra recognition badges, offline saves and multi-user roles remain follow-on phases. Video-frame training policy and physical iPhone verification remain unresolved.
