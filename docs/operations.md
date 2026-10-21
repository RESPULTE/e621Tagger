# Operations

## Setup

Use `deploy/.env.example`. Generate an administrator hash with `python -m backend.app.auth hash-password`; startup also requires your e621 username in live mode. Optional e621 credentials never go to CDN hosts.

`STUDIO_PASSWORDLESS=true` explicitly enables automatic private-network sessions without a password hash. Use it only with a reverse proxy that allows your trusted LAN/VPN subnets and denies other clients, while the backend stays on loopback. Anyone on those allowed networks can use Studio. The UI obtains a secure session automatically through an Origin-checked POST; metadata, media and mutations still require that session, and mutations still require its CSRF token. Leave this option false for password authentication.

Set the exact `STUDIO_PUBLIC_ORIGIN`, including scheme and port. Preserve that Host at the reverse proxy; forwarded headers are not trusted. HTTPS/secure cookies are defaults. Private HTTP requires explicit `STUDIO_SECURE_COOKIE=false` and an exact HTTP origin. It uses a separate `studio_session_http` cookie to avoid collisions with existing Secure cookies. Compose binds loopback for use behind a private VPN/reverse proxy.

Exactly one backend instance may own a data root. Do not add Gunicorn workers or replicas. The persistent queue runs inside the API process; running jobs are requeued at startup and renew their leases during execution.

## Backups and recovery

Schedule a daily online SQLite backup:

```sh
python -m backend.backup /backups/studio-2026-10-21.sqlite3 --data-root data
# Compose: copy the resulting backup off the volume afterward.
docker compose --env-file .env -f deploy/docker-compose.yml exec studio python -m backend.backup /data/backups/studio-2026-10-21.sqlite3 --data-root /data
```

Use a new timestamped filename, retain generations and copy them to another device. Back up configuration with your normal secret storage. Thumbnails can be rebuilt; SQLite contains labels and provenance.

`deploy/backup_compose.py` automates the online backup through an executable `<deployment-root>/studio-compose` wrapper that selects the correct project, environment and Compose files. It locks out overlapping backups, copies the result outside the volume, checks SQLite integrity, publishes without overwriting a file, and retains 30 days by default. Run it on the Ubuntu host with `python3 deploy/backup_compose.py --deployment-root /opt/hydra-studio`. A failed host copy leaves the volume backup available for recovery. At most three timestamped recovery copies are retained in the volume after successful snapshot verification, so repeated host-copy failures cannot accumulate unbounded generations.

The supplied `hydra-studio-backup.service` and `.timer` target user `vector` and `/opt/hydra-studio/current`; adjust them for another deployment. Install into `/etc/systemd/system/`, reload systemd, and enable `hydra-studio-backup.timer`. The daily schedule is 03:15 Asia/Kuala_Lumpur with up to five minutes of jitter and missed-run recovery. Run the service once manually and inspect its journal before relying on the schedule. Host copies remain on the same physical server until copied to another device.

To restore: stop Studio, preserve the current data root, restore the backup as `studio.sqlite3` in a fresh directory with correct ownership, and start against that directory. Never mix restored databases with stale WAL/SHM files or overwrite a running database.

Disk-full/SQLite failures return an unacknowledged-save error. Free space and retry. Failed media writes never mark an asset valid. Failed thumbnail jobs cool down for five minutes. Retry imports using Add next 50 or the original query. A retry with the same request ID replays its receipt; a deliberate new import uses a new ID.

## Monitoring

`/health` checks the DB and local tasks. Authenticated `/api/metrics` reports metadata requests/throttles/latest latency, media bytes/cache hits/misses/concurrency, cache usage and queue counts. Verbose HTTP logs are disabled; secrets never appear in job errors.

Measure on the target server: cold/warm gallery time, preview response p95, RSS during a 500-post import, cache-hit rate, queue depth, storage growth and label acknowledgment time. Browser Performance/Network tools expose rendering and transferred bytes. Large originals can consume browser memory even though server buffers remain bounded. Tune from measurements.

Mocked regression tests and browser tests are separate from live API/CDN probes. Physical iPhone playback depends on codecs; use the e621-provided MP4 fallback where available. No hardware performance threshold is claimed without measurement.

To run the browser suite against a container, start an isolated Compose project with its own volume, `STUDIO_MOCK_E621=true`, and either private passwordless access or the disposable password `studio-browser-test-password` (generate its hash with `backend.app.auth`). Set `STUDIO_E2E_BASE_URL` to that deployment's exact origin, then run `npm run test:e2e` from `frontend/`. The override disables the local demo server. Tests verify mock mode after connecting before creating projects or reviews. Never point this suite at the real review database. Leave the override unset for the normal temporary local demo server. Use trusted HTTPS for private-origin browser checks; the suite does not bypass certificate validation.

Browser checks cover desktop Chromium, mobile Chromium and iPhone-sized WebKit. Before the WebKit playback test, an independent local MP4 probe checks the host decoder. If the host cannot decode that clip, playback is explicitly skipped and the unsupported-codec UI is tested instead. This is not physical iPhone playback verification; [Playwright documents OS-dependent WebKit codecs](https://playwright.dev/docs/browsers#webkit). Run the playback case on macOS WebKit or an actual iPhone before declaring that device verified.

The obsolete PWA worker URL serves a retirement script that removes only `e621tagger-*` caches and unregisters itself. No offline save queue is installed. Previously cached clients may need one online reload.
