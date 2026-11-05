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

Use a new timestamped filename, retain generations and copy them to another device. Back up configuration with your normal secret storage. Legacy thumbnails are disposable only after an approved owned-cache cleanup; current browsing writes no media. SQLite contains human labels and provenance. Explicit dataset-export media and the collection require their own filesystem backup.

`deploy/backup_compose.py` automates the online backup through an executable `<deployment-root>/studio-compose` wrapper that selects the correct project, environment and Compose files. It locks out overlapping backups, copies the result outside the volume, checks SQLite integrity, publishes without overwriting a file, and retains 30 days by default. Run it on the Ubuntu host with `python3 deploy/backup_compose.py --deployment-root /opt/hydra-studio`. A failed host copy leaves the volume backup available for recovery. At most three timestamped recovery copies are retained in the volume after successful snapshot verification, so repeated host-copy failures cannot accumulate unbounded generations.

The supplied `hydra-studio-backup.service` and `.timer` target user `vector` and `/opt/hydra-studio/current`; adjust them for another deployment. Install into `/etc/systemd/system/`, reload systemd, and enable `hydra-studio-backup.timer`. The daily schedule is 03:15 Asia/Kuala_Lumpur with up to five minutes of jitter and missed-run recovery. Run the service once manually and inspect its journal before relying on the schedule. Host copies remain on the same physical server until copied to another device.

To restore: stop Studio, preserve the current data root, restore the backup as `studio.sqlite3` in a fresh directory with correct ownership, and start against that directory. Never mix restored databases with stale WAL/SHM files or overwrite a running database.

Disk-full/SQLite failures return an unacknowledged-save error. Free space and retry. Failed media writes never mark an asset valid. Interactive thumbnails can be retried by reopening the query; upstream cooldowns are shared. Retry explicit review/import requests with their original source identity. A retry with the same request ID replays its receipt; a deliberate new import uses a new ID.

## Optional GPU image search

Follow [processing laptop setup](similarity.md). Image search requires a current authenticated GPU capability and heartbeat; it shows unavailable when the RTX 4070 worker is offline or unready. The worker polls outbound, with no inbound laptop port. Configure `STUDIO_SIMILARITY_WORKER_TOKEN` privately on both sides. Existing reviewed results can remain reviewable for their session lifetime after disconnect. Search state is not restored after server restart.

All queries encode/search images against the laptop's local corpus. There is no historical-array reader, remote published-neighbor API, legacy worker protocol or `STUDIO_PUBLISHED_NEIGHBORS` setting. Remove that obsolete environment setting when updating deployment configuration. Saved historical provenance remains auditable. `STUDIO_UPSTREAM_INTERVAL` still gates metadata/media starts; reference byte/pixel bounds are configurable in Compose. A 16 MiB byte cap supports the verified Radar reference. Keep model/corpus dependencies off the server.

If a reverse proxy limits JSON requests to 64 KiB, preserve that general limit
and allow 16 MiB only for `^/api/projects/[1-9][0-9]*/similar/reference$`.
Use the same Host/proxy settings as the ordinary API, HTTP/1.1 and
`proxy_request_buffering off` on that upload location. Validate configuration
before switching. The application still enforces reference bytes, decoded pixels,
formats, authentication and its shared memory budget.

## Optional heart saves to the main collection

Merge `deploy/docker-compose.library.yml` after the base Compose file and set `STUDIO_LIBRARY_HOST_ROOT` to the existing, mounted main collection folder. Set `STUDIO_LIBRARY_SUBDIRECTORY=Inbox` to save new originals in its existing `Inbox` child. The collection identity marker and temporary staging remain at the mounted parent. New saves create only the original media, with no JSON/TXT/XMP sidecars. Each ownership record stores its destination, so old top-level saves remain recoverable until explicitly migrated. Without this configuration the heart is disabled; review still works. The container's UID 10001 needs write/traverse access to the destination; grant only that folder's named-user ACL when needed. Do not recursively change library ownership or permissions.

Initialize only the intended mounted folder once, using the same merged configuration:

```sh
docker compose --env-file .env -f deploy/docker-compose.yml -f deploy/docker-compose.library.yml run --rm --no-deps studio python -m backend.init_library /library
```

This creates only a hidden `.hydra-studio-library-id` marker. Startup/save never creates a missing collection root or marker. The marker and the database identity must agree; a missing mount or different collection fails closed. The bind uses `create_host_path: false`. Keep the marker when backing up or restoring this collection.

Double-tap or double-click a stationary thumbnail or enlarged image/video to toggle its global favourite. A single thumbnail tap opens after a brief double-tap window; keyboard Enter opens immediately and F on focused media toggles the favourite. Movement/scrolling cancels the gesture. A small pink paw in the top-right marks a favourite, with a dashed border while saving. Repeat the gesture to remove saved files or cancel a queued/active transfer and clean its owned partial files. The thumbnail controls contain only positive and negative review icons. Downloads reuses the existing preview, displays actual `score.up` and the originating target tag, and colours Completed, In progress and Failed. Delete queues a version-checked off intent, aborts a stalled transfer and joins its staging cleanup, then removes only Studio-owned originals/sidecars. Fully deleted rows disappear; durable job history remains. Old jobs without origin/vote metadata show an unavailable value rather than substituting net score. Failed saves/removals can be retried. These actions do not change human reviews, e621 favorites, project membership or frozen datasets. A save refetches e621 metadata and checks the original's MD5; a changed source requires a refreshed view. Each new save is one `e621_<post-id>_<md5>.<original-extension>` file with unchanged original bytes. Source metadata stays in SQLite; no tag sidecars are created.

One shared durable download worker pool serves collection saves and frozen dataset media exports under the same CDN limits. `STUDIO_DOWNLOAD_CONCURRENCY` defaults to 2 (range 1–4), additionally bounded to reserve interactive media capacity. Defaults: 256 MiB per original, 128 MiB free-space reserve, 300 seconds per operation, a 16-job admission limit (allowing replacement/cancellation of the same post) and four bounded attempts. Environment overrides are in the optional Compose file. Pending work resumes after restart. Files are downloaded under hidden `.hydra-studio-tmp` storage, checksummed, and published with metadata first and original last. A durable inode/hash manifest and POSIX directory fsync order protect interrupted publication/removal. Timeout/shutdown waits for active filesystem work before releasing writer ownership. An unresponsive filesystem can therefore delay shutdown; forcibly stopping still relies on the journal for recovery.

Studio refuses filename collisions and never adopts pre-existing files. Removal verifies the owned files, atomically quarantines them and checks identity again before deletion; edited/replaced files cause a conflict and remain recoverable. Do not remove the hidden staging area while a job is active. If an external replacement also blocks restoration to its old name, the error gives its retained recovery path. There is no recursive library deletion. The marker and hidden staging folder remain after removing a saved post. Local POSIX filesystems with hard links, atomic rename and directory fsync are required for the deployment durability guarantees; Windows tests exercise application logic only.

Schema v4 adds canonical identity/conflict views and download progress/partial ownership after v3 collection identity. Upgrading v1/v2/v3 creates a verified `pre-schema-v4-*.sqlite3` online backup before migration. **Do not start an older-schema image against a migrated volume.** For rollback, stop Studio, preserve the entire v4 volume, export media and library, restore the pre-upgrade backup to a fresh data volume, and start the previous image against that fresh volume. Files saved after that backup remain on disk but cannot be adopted/deleted by the older database. For normal v4 recovery, retain both the database and collection marker/files; inode checks intentionally reject copied/replaced files whose ownership can no longer be verified. Collection originals/sidecars need their own filesystem backup; the SQLite backup does not include them.

## Monitoring

`/health` checks the DB and local tasks. Authenticated `/api/metrics` reports metadata requests/throttles/latest latency, media bytes/concurrency, legacy cache inventory, transient metadata count, shared download active/peak/bytes and queue counts. Verbose HTTP logs are disabled; secrets never appear in job errors.

Measure on the target server: cold/warm gallery time, preview response p95, RSS during several fresh query batches, queue depth, explicit-export storage growth and label acknowledgment time. Browser Performance/Network tools expose rendering and transferred bytes. Large originals can consume browser memory even though server buffers remain bounded. Tune from measurements.

Mocked regression tests and browser tests are separate from live API/CDN probes. Physical iPhone playback depends on codecs; use the e621-provided MP4 fallback where available. No hardware performance threshold is claimed without measurement.

To run the browser suite against a container, start an isolated Compose project with its own volume, `STUDIO_MOCK_E621=true`, and either private passwordless access or the disposable password `studio-browser-test-password` (generate its hash with `backend.app.auth`). Set `STUDIO_E2E_BASE_URL` to that deployment's exact origin, then run `npm run test:e2e` from `frontend/`. The override disables the local demo server. Tests verify mock mode after connecting before creating projects or reviews. Never point this suite at the real review database. Leave the override unset for the normal temporary local demo server. Use trusted HTTPS for private-origin browser checks; the suite does not bypass certificate validation.

Browser checks cover desktop Chromium, mobile Chromium and iPhone-sized WebKit. Before the WebKit playback test, an independent local MP4 probe checks the host decoder. If the host cannot decode that clip, playback is explicitly skipped and the unsupported-codec UI is tested instead. This is not physical iPhone playback verification; [Playwright documents OS-dependent WebKit codecs](https://playwright.dev/docs/browsers#webkit). Run the playback case on macOS WebKit or an actual iPhone before declaring that device verified.

The obsolete PWA worker URL serves a retirement script that removes only `e621tagger-*` caches and unregisters itself. No offline save queue is installed. Previously cached clients may need one online reload.

## Storage/review release boundary (24 October 2026)

Read-only inspection confirmed production `ui-20261024-v4`, schema v2, with no library mount. Jellyfin's existing host library is `/media/vector/jellyfin-data` (mounted as `/Media` there); its owner is root:root with a writable ACL for vector and existing default ACLs. There is no literal Smut subfolder. This is the user-identified collection destination; do not invent a new mountpoint. Configure its existing path through `STUDIO_LIBRARY_HOST_ROOT`, bind it as `/library` with create_host_path=false, and explicitly initialize the hidden identity marker at release time. Do not chmod/chown the existing collection recursively. Studio runs as UID 10001; verify access without modifying manual media before enabling the mount.

Deploy code/schema together, with an online SQLite backup and an isolated synthetic stage first. Pending legacy discovery/cache jobs are cancelled additively; labels, events, frozen manifests, legacy memberships and old cache files remain. No cleanup/purge or conflict merge is included. Query is fresh by default; Tagged lists canonical-family human reviews; Downloads is global across projects. Legacy project URLs resolve to their canonical target and preserve conflicting original decisions.

Prepared releases must identify the source snapshot, previous image/current symlink and pre-schema backup. Rollback uses a fresh volume restored from that backup, retaining the migrated volume and all newly saved files for recovery. Do not copy an active WAL database as a rollback snapshot. A release package is not evidence of a production deployment; record whether the switch actually occurred.

Schema v6 and the image-only worker must be released with a verified backup and matching frontend/worker contracts. See [preparation, measured evidence, backup and rollback](image-retrieval.md). Do not treat older deployment evidence as validation of this revision.


### Existing favourite images into Inbox

`python -m backend.migrate_favourites --data-root /data --library-root /library --report /data/backups/favourite-inbox-migration.json --apply` requires the application to be stopped: it takes the same exclusive database process lock. Without `--apply` it validates and describes the plan. It verifies every Studio-owned original/sidecar and rejects destination collisions, hard-links originals into the existing same-filesystem Inbox, updates only their ownership manifests, removes the verified old names and deletes owned JSON/TXT/XMP sidecars. The fsynced report makes interrupted moves resumable. It never selects files by a broad extension glob. Manual/replaced files and review/dataset records are preserved.

The current deployment uses host `/media/vector/jellyfin-data/Inbox`, visible as `/existing-adult/Inbox` through Nextcloud furry's Inbox mount and within Stash's `/media/vector/jellyfin-data` library root. Nextcloud's service maps to host UID1000; Studio runs as UID10001. Keep their existing permissions, adding only the Inbox access Studio requires. Older code that ignores recorded destinations cannot safely manage migrated favourites: any rollback must restore both paths and ownership records together.
