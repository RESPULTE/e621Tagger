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

## Optional published neighbors

`STUDIO_PUBLISHED_NEIGHBORS` defaults to true and is passed through Compose. It permits only explicit known-ID lookups against a pinned public Hydra index, using bounded byte ranges. Set false for offline-only operation; this does not remove previously stored evidence. Failure or missing coverage appears in the post's Similar posts panel; reviews do not depend on upstream index health. A separate durable `neighbors` queue has one worker and a 16-job active limit. No Torch, NumPy, checkpoint or index files are added to the server. See [the adapter contract](optional-workers.md) for limits and verification boundaries.

## Optional heart saves to the main collection

Merge `deploy/docker-compose.library.yml` after the base Compose file and set `STUDIO_LIBRARY_HOST_ROOT` to the existing, mounted main collection folder. Originals and sidecars go directly in that folder, without an Inbox subfolder. Without this configuration the heart is disabled; review still works. The container's UID 10001 needs write access to this folder. Do not recursively change library ownership or permissions.

Initialize only the intended mounted folder once, using the same merged configuration:

```sh
docker compose --env-file .env -f deploy/docker-compose.yml -f deploy/docker-compose.library.yml run --rm --no-deps studio python -m backend.init_library /library
```

This creates only a hidden `.hydra-studio-library-id` marker. Startup/save never creates a missing collection root or marker. The marker and the database identity must agree; a missing mount or different collection fails closed. The bind uses `create_host_path: false`. Keep the marker when backing up or restoring this collection.

Click the heart in the gallery or post view to queue a global save for that post. Click again to queue removal. The UI shows pending, saved and failed states, with a retry for failed operations. This does not change human review labels, e621 favorites, project membership or frozen datasets. A save refetches e621 metadata and checks the original's MD5; a changed source requires a refreshed view. The four files are `e621_<post-id>_<md5>.<original-extension>`, `.json`, `.txt` and `.xmp`. JSON retains the full source metadata and categorized tags; TXT and XMP contain all canonical source tags. Original bytes are preserved. External tools may require their own sidecar import configuration; no Stash tag-import integration is implied.

One separate durable worker uses the shared CDN limits. Defaults: 256 MiB per original, 128 MiB free-space reserve, 300 seconds per operation, 16 active queued operations and four bounded attempts. Environment overrides are in the optional Compose file. Pending work resumes after restart. Files are downloaded under hidden `.hydra-studio-tmp` storage, checksummed, and published with metadata first and original last. A durable inode/hash manifest and POSIX directory fsync order protect interrupted publication/removal. Timeout/shutdown waits for active filesystem work before releasing writer ownership. An unresponsive filesystem can therefore delay shutdown; forcibly stopping still relies on the journal for recovery.

Studio refuses filename collisions and never adopts pre-existing files. Removal verifies the owned files, atomically quarantines them and checks identity again before deletion; edited/replaced files cause a conflict and remain recoverable. Do not remove the hidden staging area while a job is active. If an external replacement also blocks restoration to its old name, the error gives its retained recovery path. There is no recursive library deletion. The marker and hidden staging folder remain after removing a saved post. Local POSIX filesystems with hard links, atomic rename and directory fsync are required for the deployment durability guarantees; Windows tests exercise application logic only.

Schema v3 adds collection identity and saved-file ownership. Upgrading v1/v2 creates a verified `pre-schema-v3-*.sqlite3` online backup before migration. **Do not start a schema-v2 image against a migrated volume.** For rollback, stop Studio, preserve the entire v3 volume and library, restore the pre-upgrade backup to a fresh data volume, and start the previous image against that fresh volume. Files saved after that backup remain on disk but cannot be adopted/deleted by the older database. For normal v3 recovery, retain both the database and collection marker/files; inode checks intentionally reject copied/replaced files whose ownership can no longer be verified. Collection originals/sidecars need their own filesystem backup; the SQLite backup does not include them.

## Monitoring

`/health` checks the DB and local tasks. Authenticated `/api/metrics` reports metadata requests/throttles/latest latency, media bytes/cache hits/misses/concurrency, cache usage and queue counts. Verbose HTTP logs are disabled; secrets never appear in job errors.

Measure on the target server: cold/warm gallery time, preview response p95, RSS during a 500-post import, cache-hit rate, queue depth, storage growth and label acknowledgment time. Browser Performance/Network tools expose rendering and transferred bytes. Large originals can consume browser memory even though server buffers remain bounded. Tune from measurements.

Mocked regression tests and browser tests are separate from live API/CDN probes. Physical iPhone playback depends on codecs; use the e621-provided MP4 fallback where available. No hardware performance threshold is claimed without measurement.

To run the browser suite against a container, start an isolated Compose project with its own volume, `STUDIO_MOCK_E621=true`, and either private passwordless access or the disposable password `studio-browser-test-password` (generate its hash with `backend.app.auth`). Set `STUDIO_E2E_BASE_URL` to that deployment's exact origin, then run `npm run test:e2e` from `frontend/`. The override disables the local demo server. Tests verify mock mode after connecting before creating projects or reviews. Never point this suite at the real review database. Leave the override unset for the normal temporary local demo server. Use trusted HTTPS for private-origin browser checks; the suite does not bypass certificate validation.

Browser checks cover desktop Chromium, mobile Chromium and iPhone-sized WebKit. Before the WebKit playback test, an independent local MP4 probe checks the host decoder. If the host cannot decode that clip, playback is explicitly skipped and the unsupported-codec UI is tested instead. This is not physical iPhone playback verification; [Playwright documents OS-dependent WebKit codecs](https://playwright.dev/docs/browsers#webkit). Run the playback case on macOS WebKit or an actual iPhone before declaring that device verified.

The obsolete PWA worker URL serves a retirement script that removes only `e621tagger-*` caches and unregisters itself. No offline save queue is installed. Previously cached clients may need one online reload.
