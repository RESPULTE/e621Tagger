# Validation — 21 October 2026

## Embedding discovery — 24 October 2026

Release `fdd07f9` (feature `72fdd53`) is deployed privately. Windows Python 3.10 and Ubuntu image Python 3.11 each passed **46 backend tests**; Ruff, regenerated API contracts and local/Docker TypeScript/Vite builds passed. Full local browser matrix: **25 passed, 8 expected skips** (includes three inapplicable passwordless reconnect cases). Full isolated Ubuntu passwordless matrix: **28 passed, 5 expected browser/device skips**. A subsequent UI-only correction makes Hide tags also hide neighbor suggestions after reload; its complete neighbor flow passed **3/3 locally and 3/3 against the final Ubuntu image**. No backend source changed for that correction.

Real published range lookups matched post 14 to row 0 (6,688 array bytes/7 reads) and post 757037 to row 703044 (14,880 bytes/9 reads). The interior row digest matched across Windows and Ubuntu; Ubuntu took 13.12 seconds. A separate real-data container imported two explicitly selected neighbors through the actual paced e621 source and returned 20 source-tag suggestions from two known metadata records, with no human labels created. Actual restart retained the completed row and import provenance; integrity/foreign keys passed. Browser fixtures are marked synthetic and do not contact the published index. No independent imagery/relevance judgment or arbitrary-image embedding parity is claimed.

The final image is `hydra-training-studio:fdd07f9`, digest `sha256:d2e6e52cb1aad340d6f4834df7302d6ebe335e96b8747185726c09bf13077081`, 59,737,877 bytes. Runtime UID is 10001; Torch and NumPy are absent. No schema migration was required. Verified production backups preceded both switches. All nine audited durable-table hashes matched before/after: four projects, 459 candidates/discoveries, one human label, ten review events, ten search runs and 52 mutation receipts. Final live status/quality/dataset/evidence reads passed for all projects; unauthenticated requests and invalid Origins were rejected. No production imports, labels or datasets were created by validation. The post-release verified backup is `studio-20261023T160229518881Z.sqlite3` (UTC filename; 24 October Malaysia time).

Read-only Luna High review found no blocker and identified an oversized numeric ID boundary, fixed with clean 422 validation. Main review also bounded candidate membership checks and verified preserved human/frozen-data contracts. Disposable validation containers are stopped, their isolated volumes retained, and the SSH tunnel closed. Public pushes, private-asset copies, giant downloads, training and GPU transport were not performed. [Optional discovery](optional-workers.md) records the exact bounds, publisher identities and unresolved extractor/centroid parity. Physical iPhone, offsite backups and fresh-volume restore were not revalidated in this slice.


## Local continuation checks — 23 October 2026

### Search/media deployment and live measurements

Commit `81f506e` was built on the Ubuntu server and tested in an isolated mock container/volume through an SSH loopback tunnel. Full browser matrix: **22 passed, 5 expected skips** (desktop keyboard-only cases, inapplicable codec branch, Windows WebKit's independently unsupported H264 playback). Passwordless private HTTP reconnect and all new overlapping/empty/exhausted search cases passed. Production then received a verified online backup and the new image; hashes of projects, candidates, discoveries, labels, label events, search runs and mutation receipts matched before/after. Preserved counts: 3 projects, 359 candidates, 359 provenance records, 1 current label, 10 review events, 8 search runs and 50 receipts. No production review or import was created by these checks.

Actual server cold-CDN probe: 16 existing safe-rated metadata records were copied read-only into fresh temporary databases in one-off containers; only preview bytes were fetched. Baseline `a496223`: 16/16 success, 5,517.93ms cold completion, 3.50ms warm server-side p95, 391.79ms DNS lookup, 63,704KiB peak RSS. Updated `81f506e`: 16/16 success, 4,359.43ms cold completion, 2.38ms warm p95, 148.44ms DNS lookup, 65,428KiB peak RSS. Updated upstream-header p95 was 1,133.95ms and host-budget wait p95 538.54ms. Sequential runs have different DNS/CDN cache conditions; they do not isolate causation as tightly as the controlled benchmark below.

Read-only production HTTP checks returned 16/16 warm previews before and after. Concurrent warm response maximum was 241.48ms before and 239.40ms after over the LAN/client path. A 1MiB original-image Range request returned 206 in both cases (2,368.37ms versus 2,301.67ms total; header time 1,174.92ms versus 1,051.33ms). This does not show a meaningful warm-path improvement; upstream/network time remains real. Raw originals were streamed, not stored.

### Dataset slice validation

P3 adds schema-v2 migration, cache-source identity/fingerprints, duplicate grouping and frozen metadata exports. The final Windows backend suite passed **33 tests**, including all six dataset tests and a metadata-refresh race that must never bind old preview bytes to a new source MD5. Ruff passed, generated contracts were refreshed, and the production build passed. The dataset freeze/download/preservation browser scenario passed on desktop Chromium, mobile Chromium and iPhone-sized WebKit. Tests include idempotent concurrent freezes, exact JSON hash verification, preserved manifests after later label edits, unknown/unsupported exclusions, transitive grouping, exact-label conflicts, migration backup/integrity and lazy rebuilding of legacy/stale thumbnails.

Release `a71d42c` built successfully on Ubuntu. Python 3.11 inside a disposable image container passed all **33 backend tests** and Ruff; the full browser matrix against the isolated Ubuntu application passed **25 tests with 5 expected skips**. That isolated volume was upgraded from the actual schema-v1 image. Its automatic pre-migration backup remained valid schema v1. After browser tests, restart preserved hashes of all review/provenance tables and three immutable dataset manifests. An online schema-v2 backup restored into a fresh isolated volume with UID 10001 ownership; the running restored application was healthy, SQLite integrity/foreign keys passed, and the same table and manifest hashes matched (41 synthetic projects, 2,164 candidates, 24 current labels, 49 review events, three datasets).

Production received release `a71d42c` after verified host backup `studio-20261023T150359925507Z.sqlite3`. Migration reached schema v2 with an independently checked automatic schema-v1 backup. Core table hashes and all counts listed above remained unchanged. Live quality and dataset-list endpoints returned 200 for all three projects; unauthenticated reads returned 401 and invalid-Origin mutations returned 403. No production reviews or frozen datasets were created. Sixteen live previews completed and warmed successfully; a 1MiB original Range returned 206. A post-upgrade online backup was published as `studio-20261023T150447311508Z.sqlite3`. These are private same-server backups, not offsite replication. Rollback must use a verified pre-upgrade backup in a fresh volume; do not launch the schema-v1 image against the migrated volume.

On 23 October the owner explicitly kept published neighbor arrays and laptop training optional for now. No embedding/index downloads, Torch dependencies or checkpoint copies were added.

Branch `fix/discovery-media-datasets`, starting at `3a5dee5`. Three new regressions failed before the fix and passed afterward: overlapping import counts/run filtering, concurrent same-host starts before delayed headers, and preview progress while three originals hold slots. Full Windows Python 3.10 backend suite: **27 passed**; Ruff passed; OpenAPI/types regenerated; frontend production build passed. New search browser scenario passed on desktop Chromium, mobile Chromium and iPhone-sized WebKit, covering duplicate-only provenance, filtered results, empty/exhausted searches, and preserved positive reviews. Existing desktop suite had six passes and two expected skips; its first new scenario failure was an accessible-name selector, corrected and rerun across all three browsers. Physical iPhone behavior is not inferred.

A local controlled benchmark used 16 synthetic thumbnails, 600ms mock CDN response-header latency, 250ms host start spacing, identical 41,071 transferred bytes, and fresh temporary SQLite/cache roots. Baseline `3a5dee5`: cold completion 9,833.40ms, warm sequential request p95 23.90ms, peak four permits. Updated: cold completion 4,388.93ms, warm p95 2.68ms, peak three permits. This isolates application scheduling/cache-write effects; it does not measure home-server CPU/RAM, real CDN/DNS, or browser decoding. Original byte streaming/Range behavior is unchanged and covered by existing tests.

Cloudflare WARP reported Connected, healthy, Mode Warp before reading the official API page on 23 October. The page still specifies a descriptive User-Agent and best-effort sustained requests at no more than one per second. The metadata scheduler was not relaxed. The sections below retain historical validation of the previous deployed version; current continuation checks are above.

## Ubuntu Docker deployment

Validated on the user's ThinkPad X260, Ubuntu 24.04.4 LTS x86_64, Intel i5-6300U (4 logical CPUs), 7.6 GiB RAM, with 56 GiB free disk before deployment. Docker Engine 29.6.1 and Compose 5.3.1 were already installed.

The actual multi-stage Dockerfile built successfully. Runtime image `hydra-training-studio:a496223` has image ID `sha256:cd874f3ef81c856de7b6feafa50aac06759622dbb3a2962dccadd0d6c1625de6` and size 59,721,239 bytes. Runtime is Python 3.11, UID 10001, one Uvicorn worker; `/data` belongs to UID 10001. Torch, Hydra sources and model weights are absent from the runtime. Only selected tracked Studio source files were transferred; no weights, `home-media/` or legacy `static/` files were transferred.

Production uses `/opt/hydra-studio`, Compose project `hydra-studio`, named volume `hydra-studio_studio-data`, and `unless-stopped` restart policies. Backend binds `127.0.0.1:8000`; a dedicated Nginx proxy binds only `192.168.0.15:8447`. The final URL is `http://192.168.0.15:8447`, as explicitly requested by the owner after an iPhone certificate warning. Private HTTPS setup is deferred. Exact Host is preserved; proxy buffering and media temporary-file spooling are disabled. LAN `192.168.0.0/24` and WireGuard `10.15.39.0/24` are allowed, with other source addresses denied. Requests from the Docker bridge subnet returned 403 under both the initial HTTPS setup and final HTTP setup. No IPv6/public wildcard binding was added. Existing services remained running.

The user requested no Studio password. Production explicitly sets `STUDIO_PASSWORDLESS=true` with no password hash. Browser startup creates a normal session automatically; Origin, Host and CSRF checks remain enforced. HTTP is explicitly configured with `STUDIO_SECURE_COOKIE=false` and a separate HttpOnly/SameSite Strict `studio_session_http` cookie; HTTPS keeps its Secure cookie name. Password authentication stays the default for other deployments. Production initially contained no projects or human labels; acceptance tests used separate mock/live/restore volumes. The owner subsequently created projects in production; those were preserved during the HTTP upgrade.

Windows Docker Desktop remains unavailable: the client hung and host logs showed its inference-listener startup failure. All image execution in this report used the working Ubuntu engine. No Docker reset or unrelated service changes were performed.

## Automated checks

- Windows Python 3.10.19: **24 backend tests passed**. This includes the original 21 workflow/media/scheduler checks plus passwordless opt-in, session/Origin/CSRF/cookie protections and the HTTP cookie-name transition.
- Ruff: `backend` and `deploy/backup_compose.py` passed. Git whitespace check passed.
- OpenAPI regenerated from the application; frontend types regenerated from that schema. TypeScript/Vite production build passed locally and inside the actual Docker build.
- Final HTTP Playwright matrix against the passwordless Docker deployment: **19 passed, 5 skipped, 0 failed**, across desktop Chromium, mobile Chromium and iPhone-sized WebKit. Covers originals, labels/undo, selected batches, failed-save retry, playback/seeking, keyboard undo after auto-advance, scroll restoration, layout width, automatic reconnect with a retained HTTPS cookie, and the prefilled/full-editable additional query. The private-IP HTTP tests explicitly observed an insecure browser context without native `crypto.randomUUID`; valid UUID4 mutations succeeded through the `getRandomValues` fallback. The earlier HTTPS matrix passed 16 cases with five expected skips.
- Skips: two mobile keyboard scenarios, two inapplicable unsupported-codec scenarios, and Windows WebKit playback whose independent standalone H264 decoder probe fails. Unsupported-codec controls passed. Physical iPhone playback is not inferred from WebKit emulation.
- First container run found a test-readiness race: the route heading rendered before candidate data arrived, so the keypress preceded the detail response and sent no mutation. The test now waits for review controls before using shortcuts; focused password-mode keyboard check and final container matrix passed.
- Fresh desktop/mobile gallery screenshots were inspected; no horizontal clipping was observed. Browser width assertions passed.

## Target-server synthetic load and persistence

An isolated Docker volume imported 500 mock candidates through the real embedded worker and one-second metadata scheduler. The cold-cache/API measurements below used the initial image `3d9c3a2` over HTTPS before the HTTP preference change. Sixteen thumbnail requests were prefetched with four shared media permits. The acceptance client ran on the Ubuntu host over verified private HTTPS; these are server-host HTTPS timings, not CDN or browser-render timings.

| Measurement | Observed |
| --- | ---: |
| Initial 50 plus nine additional pages | 10.15 s |
| Candidate reads during import | 66 |
| API read median / p95 | 39.34 / 72.74 ms |
| Label save during import | 11.68 ms |
| Uvicorn RSS minimum / peak | 60.53 / 68.05 MiB |
| Cold thumbnail readiness p95, 16 queued | 4032.89 ms |
| Warm thumbnail HTTP response p95 | 10.95 ms |
| Peak media concurrency | 4 |

A final desktop Chromium run against image `a496223` over private HTTP imported another project to 500 while the detail view remained open with bounded 16-thumbnail prefetch (warm cache). The nine additional pages finished in 8.83 seconds. A real UI review completed while import was still active: client label acknowledgment 39.4 ms, click-to-visible-Saved 149.15 ms. Across 531 animation frames, frame interval p95 was 16.7 ms. Concurrent server sampling (199 samples over 20 seconds) measured Uvicorn RSS from 67.21 to 67.52 MiB. These are a single synthetic run on this server/client pair, not guarantees for large originals or slower networks.

Restarting the container preserved all 500 candidates, positive/negative/unsure labels, an undone review's version/event, and rebuildable thumbnails. A SQLite online backup was copied to a fresh named volume with UID 10001 ownership. SQLite integrity/foreign-key checks and the running restored application's candidate/review assertions passed. The active production volume was never overwritten.

## Live e621 and browser checks

The Ubuntu application adapters used the provided e621 username, without an API key or routing/VPN changes. Four paced metadata requests completed without errors or throttles: exact tag search, 50 safe-rated dragon posts, and 50 safe-rated WebM posts. Four live thumbnails decoded and cached. API-returned original-image, WebM and supplied MP4 URLs each returned valid bounded 206 Range responses through Studio. Live verification created no human labels.

Desktop Chromium loaded an actual 3000-pixel original and played the upstream 1920-pixel MP4 (13.845 s duration), then completed a seek to one second under both HTTPS and final HTTP. A final live browser query `dragon rating:s score:>100` imported 50 matching safe posts; stored metadata had scores from 101 to 1071, with no human labels created by that check. The additional-query field was visibly prefilled with `dragon rating:s`, retained the appended filter through polling, and executed the full displayed query. Production private HTTP opened without a password prompt in desktop Chromium, mobile Chromium and iPhone-sized WebKit. Mobile cases are emulated viewports on this Windows computer. The owner reported physical-iPhone HTTPS access with no password prompt but a certificate warning, then explicitly chose HTTP; final physical-iPhone HTTP/video behavior and active WireGuard-client access were not reported/verified.

Earlier Windows inspection of the official e621 API page was performed with Cloudflare WARP connected. This deployment task did not revisit that page or change Windows/server routing.

## Backups and operational verification

The daily systemd timer is enabled at 03:15 Asia/Kuala_Lumpur with up to five minutes of jitter and persistent missed-run recovery. The service ran successfully. Verified host backups are owner-only files outside the named volume with 30-day retention. Copies remain on the same physical server until exported to another device.

Four induced host-copy failures published no partial host backup, preserved the previous verified host backup, and retained only three volume recovery generations. Overlapping backup execution was rejected by the lock. A subsequent successful backup and the production systemd service both passed after this failure exercise. Unit syntax was checked with `systemd-analyze verify`.

## Remaining boundaries

Physical iPhone image/video/final-HTTP behavior, actual VPN-client access, another-device backup replication and server reboot recovery were not exercised by the agent. Container restart, configured restart policies and daily scheduling were verified. HTTPS was deferred by the owner; its saved certificate expires on 18 October 2027 and would need renewal before later reuse beyond that date. Dataset freezing/exports are now implemented and deployed; embeddings and laptop training remain a separate follow-up.
