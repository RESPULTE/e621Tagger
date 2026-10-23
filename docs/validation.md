# Validation — 21 October 2026

## Local continuation checks — 23 October 2026

Branch `fix/discovery-media-datasets`, starting at `3a5dee5`. Three new regressions failed before the fix and passed afterward: overlapping import counts/run filtering, concurrent same-host starts before delayed headers, and preview progress while three originals hold slots. Full Windows Python 3.10 backend suite: **27 passed**; Ruff passed; OpenAPI/types regenerated; frontend production build passed. New search browser scenario passed on desktop Chromium, mobile Chromium and iPhone-sized WebKit, covering duplicate-only provenance, filtered results, empty/exhausted searches, and preserved positive reviews. Existing desktop suite had six passes and two expected skips; its first new scenario failure was an accessible-name selector, corrected and rerun across all three browsers. Physical iPhone behavior is not inferred.

A local controlled benchmark used 16 synthetic thumbnails, 600ms mock CDN response-header latency, 250ms host start spacing, identical 41,071 transferred bytes, and fresh temporary SQLite/cache roots. Baseline `3a5dee5`: cold completion 9,833.40ms, warm sequential request p95 23.90ms, peak four permits. Updated: cold completion 4,388.93ms, warm p95 2.68ms, peak three permits. This isolates application scheduling/cache-write effects; it does not measure home-server CPU/RAM, real CDN/DNS, or browser decoding. Original byte streaming/Range behavior is unchanged and covered by existing tests.

Cloudflare WARP reported Connected, healthy, Mode Warp before reading the official API page on 23 October. The page still specifies a descriptive User-Agent and best-effort sustained requests at no more than one per second. The metadata scheduler was not relaxed. Deployment/live CDN verification for this continuation remains pending access; older results below describe the previous deployed version.

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

Physical iPhone image/video/final-HTTP behavior, actual VPN-client access, another-device backup replication and server reboot recovery were not exercised by the agent. Container restart, configured restart policies and daily scheduling were verified. HTTPS was deferred by the owner; its saved certificate expires on 18 October 2027 and would need renewal before later reuse beyond that date. Exporting/freezing datasets, embeddings and training remain deferred.
