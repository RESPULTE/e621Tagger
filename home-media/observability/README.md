# Home-server observability

OpenObserve OSS v1.1.1 provides private log search on the server. Fluent Bit 5.1.3
ships server logs and approved Windows worker evidence. The server checker owns
Telegram delivery state; native OpenObserve notifications are not enabled
in parallel.

## Windows lifecycle

Existing approved queue launches and manual Start/Resume both enter `home_media.cli`.
That starts a run supervisor, which owns separate Windows job objects for the worker
and collector. It starts collection before inference initialization, samples existing
local progress while the worker runs, then writes the terminal outcome and shuts down
the collector within 30 seconds. All owned processes exit after processing. No service,
recurring monitoring task, boot-time uploader or idle network polling is installed.

The existing connection-triggered request prompt remains. Decline, empty queue, an
offline laptop and user stop are not processing failures. Unexpected loss of both
worker and supervisor produces unknown visibility, not invented success/failure.
Buffered evidence waits on disk for the next approved run if the final flush cannot
reach the server. Reconnecting while idle does not start monitoring or GPU work.

Private collector configuration is generated into ignored `data/monitoring-private/`;
the executable and bounded spool live under ignored `data/monitoring/`. Raw media,
prediction caches and application credentials are not shipped. Structured source
evidence under `data/observability/` is bounded and cleaned during approved starts.

## Installation

The server installer refuses to create storage until the existing SSD backup has a
verified success and at least 64 GiB root free remains. It creates a new, preallocated
24 GiB ext4 loop filesystem at `/srv/observability`, requires that mount for writers,
and fails closed if the mount is unavailable. It never formats an existing device.
The storage guard verifies the exact image size/backing loop device, disables loop
discard so periodic trim cannot undo that reserve, and gates every telemetry writer.

Run server modules from `/opt/homelab-monitor/source`:

```sh
sudo python3 -m observability.install_server --phase storage
sudo python3 -m observability.install_server --phase backend
sudo python3 -m observability.install_server --phase collectors
```

Configuration and secrets reside under `/etc/homelab-monitor`; private files are
root-only. SQLite/checker state, OpenObserve data and collector queues are on the
bounded filesystem. The server collector's per-output queues total 2 GiB. Ordinary
logs retain seven days and small structured events thirty days. Retention deletion
is asynchronous; the filesystem is the hard capacity boundary. OpenObserve has a
1.5 GiB/1-CPU cap, Fluent Bit 192 MiB/0.25 CPU, checker 128 MiB/0.15 CPU.

After server setup, run once on Windows:

```powershell
.\.venv\Scripts\python.exe -m observability.configure_windows
```

This uses the existing pinned SSH identity, retrieves only the collector's write-only
credential and public CA, sets private file ACLs, trusts the CA for the current Windows
user after Windows certificate confirmation and leaves the collector stopped. Trust
status is saved in `data/monitoring-private/certificate-trust.json`. Browser trust can
remain pending while the collector verifies TLS directly against its private CA file.
`observability.telegram_setup` provides a masked
local bot-token prompt and detects the private chat after `/start`. No token is printed.
`observability.access` displays the configured UI login privately when explicitly opened.
When a personal UI account is configured, its `ui_user`/`ui_password` fields in the
root-only runtime secret file take precedence over the bootstrap administrator login.
The bootstrap administrator remains available for installation and recovery.

```powershell
.\.venv\Scripts\pythonw.exe -m observability.access
```

UI: `https://192.168.0.15:5080/web/`, restricted to the existing LAN/VPN by a dedicated
nftables table. No router forwarding. TLS certificate expiry is one year; renew the
leaf certificate before expiry with the same CA and restart only OpenObserve.

## Checks and notifications

`deploy/server-config.json` is the source manifest. It lists the observed apps,
the monitoring container, selected systemd units, read-only application HTTP probes,
Pi-hole DNS and existing verified backup targets. rclone is an RC daemon, not a mount;
do not configure a nonexistent mount check. New apps require an explicit manifest
entry. Only mapped containers pass the collector filter after recreation.

The checker evaluates every minute. Service failures debounce over three samples;
confirmed run failures and suspected stalls have their own checks. Fresh liveness
cannot hide a stage without progress. An approved job with no progress for 15 minutes
(30 minutes during model load) is a suspected stall, not an automatic kill/retry.
Backup deadlines reuse the existing 30-day schedule, six-hour timer allowance and
24-hour run/verification allowance. Success requires existing verified state, not a
Kopia container's uptime or a newly created snapshot.

Unknown visibility cannot resolve a failure. Positive health clears service incidents
after three samples. Verified completion in a newly approved retry can resolve its
earlier failed requests, including superseded request IDs for the same entity. A user
stop closes as cancelled. These diagnostic checks stay in private log search.

Telegram sends only concise Docker error notices and completed-media summaries:

- A stopped/unhealthy mapped container requires three consecutive samples. Five
  Docker error/critical/fatal records in five minutes also open an error episode.
  There is one notice per episode, no reminders or recovery messages. Reopening
  requires healthy samples and ten minutes below the log-error threshold; stale
  Docker ingestion cannot prove recovery. Maintenance suppresses expected failures.
- Hydra sends only after a successful approved tagging run, with zero failed items.
  The worker reports actual selected source-file counts and stat sizes after tag
  publication succeeds. Empty, failed, interrupted and offline runs stay quiet.
- Unmanic's native postprocessor-complete event records successful adult-library
  video tasks after the final file move. The summary waits for its task queue to
  empty and two quiet minutes. Counts include successful no-op pipeline checks;
  the message says processing complete, because completion does not prove a
  resolution change. Size is the final measured output, not an estimated saving.
- Immich summarizes newly indexed uploads in the configured family upload library,
  using ingest time and measured metadata sizes. Recovered archives are excluded.
  Missing size metadata is retried durably and holds the batch; it is never counted
  as zero or silently discarded. The two-minute quiet window groups completed
  assets; it cannot prove the phone has finished its entire upload queue.
- Jellyfin can summarize new members of explicitly selected photo libraries.
  Its initial membership scan establishes a baseline without replaying history.
  Currently it is disabled: all existing collections map to adult roots, with no
  identifiable family-photo library. Select a library before enabling this source.

Counts and total sizes use KB, MB or GB (decimal units). Messages contain no media
names, paths, raw logs or long search links. Durable event IDs suppress duplicate
completion records across restarts. Disabled sources hold pending summaries.
The bounded Telegram outbox honors 429 backoff; delivery is at-least-once, so an
ambiguous network failure after Telegram accepts a message can still duplicate it.
The first activation clears old pending messages and starts from a current-time
baseline. A monitor on this server cannot notify through total power/network loss.

Completion sources live separately in root-only
`/etc/homelab-monitor/notifications.json`. Configure them with the existing pinned
SSH connection and a private stdin JSON object containing `adult_roots` (host paths):

```sh
sudo python3 -m observability.install_notifications < /path/to/private-input.json
```

The installer maps the explicit adult roots into Unmanic's existing container,
registers the small completion plugin as the app's UID, appends it to the existing
library plugin list while preserving settings, and selects Immich's existing
`/family/uploads` library and read-only viewer.
It does not restart apps or start scans/transcodes. It keeps the previous native
library/plugin settings privately for rollback. Reload only the monitoring checker
after deploying source/schema changes. The obsolete checker-failure Telegram unit
has been removed; non-Docker checker, backup and host diagnostics remain searchable.

## Source limits and maintenance

Apply Docker log limits only after collection is verified:

```sh
sudo python3 -m observability.source_limits --apply-config
sudo python3 -m observability.source_limits --restart CONTAINER_NAME
sudo python3 -m observability.source_limits --apply-journal
```

The helper reads complete Compose files, preserves other YAML data, validates syntax,
keeps root-only baseline copies, refuses concurrent edits or changed image tags, and
recreates just one named service with `--no-deps --pull never`. The policy is JSON logs
at 20 MiB × 3. Recreate services in a maintenance window and verify health between them.
An expiring maintenance record suppresses expected application reconnect alarms.
CasaOS may rewrite definitions; check the effective policy after CasaOS updates.
The journal policy limits persistent history to 512 MiB/seven days with 40 GiB free
reserved on its filesystem. Applying it can remove old archived journal history.

At 90% telemetry filesystem use the checker stops OpenObserve to protect the filesystem;
this also stops its compactor. Operator intervention is required: block collector ingress,
perform supported retention/deletion during a controlled start, and resume only after
space is safe. Do not unlink SQLite/WAL/parquet files manually. Do not remove media,
prediction state, family data or backup repositories to make monitoring fit.

## Validation and operations

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check home_media observability scripts tests
.\.venv\Scripts\python.exe -m observability.validate_windows
```

`validate_windows` exercises the actual binary against disposable fixtures and an offline
destination. It checks durable offsets/backlog, restart and bounded process shutdown;
it does not start GPU inference. Windows Tail globs require native backslashes for this
build. `validate_server` runs a disposable loopback-only OpenObserve container with data
in bounded RAM, tests write-only tokens and service-account search, then removes it.

After deployment, allow at least 72 hours with a representative busy overlap before
calling the resource pilot passed. Inspect `resources` and `check` events, ingestion
canaries, app behavior, memory/CPU/I/O, root/telemetry free space and collector drops.
Limits are provisional budgets, not performance guarantees. Do not raise caps merely
to make a failing pilot appear healthy.

Git tracks code, configs without secrets, tests and this runbook. The existing local
bare `backup` remote preserves code history. The family backup intentionally excludes
monitoring. Preserve encrypted separate copies of private runtime configuration and
credentials. For a full data backup/upgrade rollback, stop OpenObserve and copy its
complete consistent volume; copying a live SQLite file alone is insufficient. An older
binary must use its matching pre-upgrade data snapshot.

The deployment recovery capture is `data/monitoring-private/server-recovery.dpapi`
on Windows. It includes runtime configuration/certificates, service definitions and
a consistent SQLite backup of checker state. It excludes retained telemetry data and
application/media data. Windows DPAPI protects it for the current Windows account;
decryption was checked without printing plaintext. It is an off-server recovery copy,
not portable disaster recovery if this Windows account and its DPAPI keys are lost.
Refresh it after credentials/configuration change. Decrypt only into the private
directory on this Windows account, review the archive, and restore configuration to
its matching Git version before starting writers; do not extract over live databases.

To disable monitoring, stop `homelab-monitor`, `fluent-bit`, then `homelab-openobserve`.
Do not stop existing app services. Windows monitoring is already off while idle; remove
its launch integration only if rolling back code. Preserve volumes/spools for diagnosis.
Never delete the loop image until all writers have stopped and it is unmounted.
