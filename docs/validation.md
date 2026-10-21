# Validation — 21 October 2026

## Automated checks

- Python 3.11.9 / Starlette 1.9: **21 passed**. Backend regression cases cover imports, aliases, pagination, label retries/conflicts, atomic batches, undo, project isolation, restart/leases, global scheduling, media concurrency/cancellation, corrupt/null/unavailable media, cache eviction, disk/write-registration failures, login limits, online backups and SQLite busy recovery.
- `python -m ruff check backend`: passed.
- Generated OpenAPI structurally matches `app.openapi()`. TypeScript types derive from that schema.
- `npm run build`: TypeScript and Vite production build passed.
- Playwright: **13 passed, 5 skipped, 0 failed** across desktop Chromium, mobile Chromium and iPhone-sized WebKit. Tests cover search/import, originals, review/reload/undo, selected batches, failed-save retry, playback/seeking, keyboard undo after auto-advance, gallery scroll restoration, and unsupported-codec guidance.
- Skips: two mobile keyboard cases and two inapplicable unsupported-codec cases; one Windows WebKit playback case whose independent baseline-MP4 decoder probe fails. Chromium decodes the same fixture. Physical iPhone playback remains unverified.
- Desktop/mobile screenshots were inspected; two-column mobile cards, collapsed filters and review controls have no observed horizontal clipping. All three gallery width assertions passed.
- Compose configuration validates. The Docker image could not be built/run here because Docker Desktop fails during startup, independently of Studio. No target-server deployment was performed.

Pytest emits one upstream Starlette TestClient deprecation warning about its httpx adapter. It is not an assertion failure.

The Flask server, old frontend scripts/templates and obsolete deployment/docs were removed. Remaining legacy image assets in `static/` are unused and excluded from Docker; automatic approval review blocked deleting that directory. The original app files were archived locally before replacement; Hydra sources and checkpoint were preserved.

## Synthetic load check

A separate helper ran the embedded worker with a mock e621 transport, real one-second metadata pacing and a temporary SQLite database. Ten pages imported 500 unique candidates in 9.79 seconds. During import, 172 candidate reads stayed within their requested 50/100-record page limits. Local API latency was median 6.48 ms, p95 10.17 ms; a label saved during a later import in 7.56 ms and read back correctly.

These are local Python TestClient/API/SQLite measurements, not browser rendering, media decoding, process RSS or ThinkPad measurements. They are not hardware acceptance claims.

## Live source checks

Cloudflare WARP was connected before reading the [official e621 API page](https://e621.net/help/api), which returned HTTP 200. The documented User-Agent and sustained request-rate rules informed the client defaults.

A bounded live adapter probe made three metadata requests with a descriptive integration-check User-Agent and no credentials: tag search, alias resolution and 50 safe-rated posts. All 50 posts normalized and persisted with unique IDs. Request starts were 2.97 and 1.33 seconds apart. No media was downloaded and no human labels were created by that probe.

A separate one-byte request against an API-returned safe-rated video CDN URL returned 206 with `video/webm` and a valid Content-Range. This verifies the upstream range contract, separately from mocked streaming and browser tests.

## Remaining deployment checks

Build/run the image on a working Docker engine; configure the actual private HTTPS origin and administrator/e621 credentials; measure warm/cold load, thumbnail p95, RSS and disk growth on the ThinkPad; verify original and MP4 playback on a physical iPhone; schedule daily backups and verify a restore. Exports, embeddings and training remain P3–P5 work.
