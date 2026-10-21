# Validation — 21 October 2026

## Ubuntu Docker deployment

Validated on the user's ThinkPad X260, Ubuntu 24.04.4 LTS x86_64, Intel i5-6300U (4 logical CPUs), 7.6 GiB RAM, with 56 GiB free disk before deployment. Docker Engine 29.6.1 and Compose 5.3.1 were already installed.

The actual multi-stage Dockerfile built successfully. Runtime image `hydra-training-studio:3d9c3a2` has image ID `sha256:4a17fd17981578a716bbdf7f2fecee713b5549f2d0314ad42f7a7dc24c24d20f` and size 59,720,698 bytes. Runtime is Python 3.11, UID 10001, one Uvicorn worker; `/data` belongs to UID 10001. Torch, Hydra sources and model weights are absent from the runtime. Only selected tracked Studio source files were transferred; no weights, `home-media/` or legacy `static/` files were transferred.

Production uses `/opt/hydra-studio`, Compose project `hydra-studio`, named volume `hydra-studio_studio-data`, and `unless-stopped` restart policies. Backend binds `127.0.0.1:8000`; a dedicated Nginx proxy binds only `192.168.0.15:8447`. The existing private CA certificate is trusted by the Windows account. Exact Host is preserved; proxy buffering and media temporary-file spooling are disabled. LAN `192.168.0.0/24` and WireGuard `10.15.39.0/24` are allowed, with other source addresses denied. A request from the Docker bridge subnet returned 403 with TLS verification enabled. No IPv6/public wildcard binding was added. Existing services remained running.

The user requested no Studio password. Production explicitly sets `STUDIO_PASSWORDLESS=true` with no password hash. Browser startup creates a normal secure session automatically; Origin, Host and CSRF checks remain enforced. Password authentication stays the default for other deployments. Production initially contained no projects or human labels; acceptance tests used separate mock/live/restore volumes.

Windows Docker Desktop remains unavailable: the client hung and host logs showed its inference-listener startup failure. All image execution in this report used the working Ubuntu engine. No Docker reset or unrelated service changes were performed.

## Automated checks

- Windows Python 3.10.19: **23 backend tests passed**. This includes the original 21 workflow/media/scheduler checks plus explicit passwordless opt-in and session/Origin/CSRF/cookie regression cases.
- Ruff: `backend` and `deploy/backup_compose.py` passed. Git whitespace check passed.
- OpenAPI regenerated from the application; frontend types regenerated from that schema. TypeScript/Vite production build passed locally and inside the actual Docker build.
- HTTPS Playwright matrix against the passwordless Docker deployment: **16 passed, 5 skipped, 0 failed**, across desktop Chromium, mobile Chromium and iPhone-sized WebKit. Covers originals, labels/undo, selected batches, failed-save retry, playback/seeking, keyboard undo after auto-advance, scroll restoration, layout width and automatic reconnect.
- Skips: two mobile keyboard scenarios, two inapplicable unsupported-codec scenarios, and Windows WebKit playback whose independent standalone H264 decoder probe fails. Unsupported-codec controls passed. Physical iPhone playback is not inferred from WebKit emulation.
- First container run found a test-readiness race: the route heading rendered before candidate data arrived, so the keypress preceded the detail response and sent no mutation. The test now waits for review controls before using shortcuts; focused password-mode keyboard check and final container matrix passed.
- Fresh desktop/mobile gallery screenshots were inspected; no horizontal clipping was observed. Browser width assertions passed.

## Target-server synthetic load and persistence

An isolated Docker volume imported 500 mock candidates through the real embedded worker and one-second metadata scheduler. Sixteen thumbnail requests were prefetched with four shared media permits. The acceptance client ran on the Ubuntu host over verified private HTTPS; these are server-host HTTPS timings, not CDN or browser-render timings.

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

A separate desktop Chromium run over the LAN imported another project to 500 while the detail view remained open with bounded 16-thumbnail prefetch (warm cache). A real UI review completed while import was still active: client label acknowledgment 16.5 ms, click-to-visible-Saved 126.53 ms. Across 530 animation frames, frame interval p95 was 16.7 ms. These are a single synthetic run on this server/client pair, not guarantees for large originals or slower networks.

Restarting the container preserved all 500 candidates, positive/negative/unsure labels, an undone review's version/event, and rebuildable thumbnails. A SQLite online backup was copied to a fresh named volume with UID 10001 ownership. SQLite integrity/foreign-key checks and the running restored application's candidate/review assertions passed. The active production volume was never overwritten.

## Live e621 and browser checks

The Ubuntu application adapters used the provided e621 username, without an API key or routing/VPN changes. Four paced metadata requests completed without errors or throttles: exact tag search, 50 safe-rated dragon posts, and 50 safe-rated WebM posts. Four live thumbnails decoded and cached. API-returned original-image, WebM and supplied MP4 URLs each returned valid bounded 206 Range responses through Studio. Live verification created no human labels.

Desktop Chromium loaded an actual 3000-pixel original and played the upstream 1920-pixel MP4 (13.845 s duration), then completed a seek to one second. Production private HTTPS opened without a certificate warning/password prompt in desktop Chromium, mobile Chromium and iPhone-sized WebKit. Mobile cases are emulated viewports on this Windows computer; no physical iPhone or active WireGuard-client verification was performed by the agent.

Earlier Windows inspection of the official e621 API page was performed with Cloudflare WARP connected. This deployment task did not revisit that page or change Windows/server routing.

## Backups and operational verification

The daily systemd timer is enabled at 03:15 Asia/Kuala_Lumpur with up to five minutes of jitter and persistent missed-run recovery. The service ran successfully. Verified host backups are owner-only files outside the named volume with 30-day retention. Copies remain on the same physical server until exported to another device.

Four induced host-copy failures published no partial host backup, preserved the previous verified host backup, and retained only three volume recovery generations. Overlapping backup execution was rejected by the lock. A subsequent successful backup and the production systemd service both passed after this failure exercise. Unit syntax was checked with `systemd-analyze verify`.

## Remaining boundaries

Physical iPhone image/video/HTTPS behavior, actual VPN-client access, another-device backup replication and server reboot recovery were not exercised. Container restart, configured restart policies and daily scheduling were verified. Private TLS certificate renewal is still required before its 18 October 2027 expiry. Exporting/freezing datasets, embeddings and training remain deferred.
