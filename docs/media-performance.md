# Browsing media and scheduling

Metadata and media use separate scheduling lanes with a shared
admission budget (`STUDIO_UPSTREAM_INTERVAL`, default 0.7 seconds, about 1.43
request starts per second). Metadata
also retains its API pacing (`STUDIO_METADATA_INTERVAL`, minimum one second).
CDN host pacing and persisted cooldowns remain in place. HTTP 429/503
Retry-After responses defer all aggregate admissions. Interactive originals,
samples and Range requests precede previews/foreground metadata, which precede
queued original downloads and background metadata. Running transfers are not
preempted. Priorities apply to pending admissions, not bytes already streaming.

Cold-cache misses still incur aggregate pacing: 50 upstream admissions require
at least approximately 34.3 seconds between the first and last start, before
network/decoding time. Existing environment overrides take precedence; set
`STUDIO_UPSTREAM_INTERVAL=0.7` in an existing deployment and restart the service
to apply this setting.

Policy check (2026-11-06): after enabling the installed Cloudflare WARP VPN,
local host requests successfully read the [official API help](https://e621.net/help/api)
(HTTP 200). It confirms a hard ceiling of two requests per second, recommends
no more than one request per second sustained, and requires a descriptive,
non-empty User-Agent. Hitting the rate limit returns HTTP 503. No explicit CDN
exemption was verified from that documentation. The owner selected a shared
0.7-second interval while preserving one-second metadata API pacing. This is
not a claim that a CDN exemption or an official aggregate metadata-plus-CDN
requirement was verified. The separate mutation throttle in
the [application controller](https://github.com/e621ng/e621ng/blob/master/app/controllers/application_controller.rb)
does not establish the external static CDN's policy.

Prepared thumbnails use a server RAM LRU, default 64 MiB, configurable with
`STUDIO_TRANSIENT_PREVIEW_BYTES` (zero disables retention). Keys include post ID,
MD5, preview URL and availability. The browser retains at most 32 MiB of encoded
thumbnail blobs for five minutes, plus blobs/images used by mounted cards.
Object URLs are revoked on unmount. Gallery snapshots remain in React Query for
five inactive minutes; scroll offsets expire after five minutes and are bounded
to 100 gallery contexts. Session expiry/sign-out clears client query, scroll and blob
caches. No browsing media is written to server disk, browser persistent storage,
or an HTTP cache. Full page reloads discard the client RAM caches.

Opening a gallery candidate seeds the viewer immediately; the detail endpoint
still refreshes upstream metadata in the background. Review/favourite MD5 and
version safeguards remain authoritative. Still images with a distinct sample
URL display the sample first. **Load original** requests full resolution while
keeping the sample visible until the original loads. Missing samples fall back
to the original. Animation inspection continues to require its source; videos
use the MP4 alternative proactively when WebM capability is unavailable, and
retain error-triggered fallback and byte-range forwarding.

Authenticated `/api/metrics` exposes preview hits/misses, preview fetch and
conversion times, slot/host waits, first streamed chunk time (from request
handling, including resolution/admission), and per-lane aggregate scheduler
waits. Samples are bounded to 256 observations per metric. These are server
measurements, not browser paint times or live throughput benchmarks.

Compare cold visible thumbnails, scroll-away/back, viewer round trips, rapid
navigation, video seeks, and concurrent explicit downloads on the same server
and network. Synthetic regression tests establish behavior and resource cleanup;
they do not establish a deployed speedup. Persistent disk previews and bulk disk
write tuning are separate changes, pending policy decisions or measurements.

## Verification on 2026-11-06

- `.venv/Scripts/python.exe -m pytest -q backend/tests/test_upstream_priority.py backend/tests/test_preview_cache.py backend/tests/test_media_and_scheduler.py backend/tests/test_download_queue.py backend/tests/test_favourite_downloads.py backend/tests/test_similarity.py`: 64 passed.
- After increasing the new timing test's interval to span multiple Windows clock ticks with the same proportional tolerance, `test_upstream_priority.py`: 3 passed; the two existing shared-gate tests in `test_similarity.py`: 2 passed.
- `.venv/Scripts/python.exe -m ruff check backend`: passed. The generated OpenAPI JSON matches `app.openapi()`; frontend types were regenerated.
- In `frontend`, `npm run build`: passed, including the final session teardown change.
- `npm run test:e2e -- --project=desktop-chromium --project=iphone-webkit e2e/media-performance.spec.ts`: 6 passed. Warm gallery return produced zero query/preview requests and restored its scroll offset in both browsers. These tests also verify delayed-original sample display and zero original-video requests when selecting the MP4 alternative proactively.
- `npm run test:e2e -- --project=desktop-chromium e2e/viewer-ui.spec.ts e2e/storage-review.spec.ts e2e/frame-sampling.spec.ts`: 11 passed, 1 desktop-inapplicable touch test skipped.

The full `.venv/Scripts/python.exe -m pytest -q` run, before the timing-test
adjustment, returned 162 passed, 14 skipped and 5 failed. Four failures are
unchanged migration tests expecting schema 6/`pre-schema-v6` while the unchanged
migration implementation targets schema 8/`pre-schema-v8`. The fifth was the new
short-interval timing assertion; its focused recheck passed as described above.
The full suite was not rerun after that test-only adjustment.

Browser checks use synthetic demo media. Playwright's iPhone WebKit emulation
does not establish physical iPhone behavior or deployed-network throughput.
