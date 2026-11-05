# Image-only production release — 25 October 2026

Deployed **image-only-20261025-888f88bee8** at
[the private Studio](http://192.168.0.15:8447). Similarity now requires the RTX 4070
image worker; historical array lookup, remote published neighbors, v1 worker
protocol, automatic fallback, old installer and unused neighbor UI are removed.
Saved historical provenance remains readable/copyable for audit and undo.
The user removed historical compatibility/scaling/ANN and additional server
performance benchmarking from the release scope.

## Source and validation

The deployed source snapshot identity is
`888f88bee82edda41371436480c87ee150f81133437d5fd66da0789df33cdb85`;
source archive SHA-256 is
`3f2f85cf86628194093dcdc1423b4fe976d760c2bfcfe7cdab476ea71412c11c`.
Runtime image ID:
`sha256:566f805a3b780d83305a195bd27c27bf2731a75e93244c3e9fae7f3c8b2fbdea`.
The release was built from the reviewed working tree based on `d95ab19`; no public
push was performed. Source files/checksums are retained beside the server stage.
This outcome report was written after deployment and is separate from that
immutable pre-deployment source snapshot.

- Windows backend: **102 passed, one existing symlink-permission skip**.
- Isolated Ubuntu/Python3.11 image: **103 passed**; Ruff passed.
- Full local browser matrix: **49 passed, eight platform/auth skips**.
- API generation, TypeScript/Vite, Compose, nginx and whitespace checks passed.
- Independent Luna source/cutover review found no blocker. Two E2E synchronization
  defects were corrected: wait for committed label response before reload, and
  recognize automatic WebKit MP4 fallback. Existing behavior assertions remain.

The browser matrix used a disposable mock database. The isolated server served
byte-identical frontend JS/CSS and passed authenticated HTTP upload/review and
offline/v1-rejection checks. A fresh read-only live Chromium check at desktop1440
and mobile390 found enabled post-image search when ready, no historical controls,
no page errors and no horizontal overflow. No production mutation-heavy E2E ran.
The optional SSH validation tunnel was rejected by automatic review; staging
remained loopback-only and these separate validation paths were used instead.

## Live behavior and data preservation

The actual laptop GPU worker encoded a **13,967,751-byte Radar reference** through
the normal server/proxy and returned **32 ranked artwork candidates**. Post
6774371 also completed image retrieval and deduplicated 31 previously shown items.
The old neighbor API returns 404, and an explicit old mode is rejected with 422.
Stopping the owned worker and waiting for heartbeat expiry made new post/upload
queries and refresh return 503/unavailable; retained results remained readable.
The worker was restarted and left connected. No projects, reviews or saved media
were created by these production searches. Its local corpus currently has 35
images; this is not a search over every e621 post.

The source data volume was stopped, cloned and migrated to schema v6 before
opening the new proxy. Every existing table hash matched through migration.
Preserved raw counts: **5 projects, 534 posts, 757 memberships, 9 labels,
18 review events and 0 frozen datasets**. After live searches, all business-table
hashes still matched startup. Expiring sessions and request cooldowns changed
normally. Tests separately exercise preservation of nonempty frozen manifests
and legacy review provenance. Torch, the checkpoint and retired runtime readers
are absent from the server image.

The proxy retains **64 KiB** for ordinary requests and allows **16 MiB only on
the reference-upload location**, with request buffering disabled. A 70 KiB ordinary
API body was rejected with 413; the 13.97 MB reference succeeded. Authentication,
Origin/CSRF, image byte/pixel checks and LAN/VPN restrictions remain enforced.

## Operations and rollback

Current source: `/opt/hydra-studio/current` →
`releases/image-only-20261025-888f88bee8`.
Current volume: `hydra-studio_image-only-20261025-888f88bee8`.

Backups/configuration and hash evidence are under
`/opt/hydra-studio/validation/image-only-20261025-888f88bee8/activation/`:
`online-before-cutover.sqlite3`, `cloned-before-v6.sqlite3`,
`cloned-after-v6.sqlite3`, `active-after-start.sqlite3`, `post-verified.sqlite3`,
the prior `.env`, `compose.private.yml`, `nginx.conf`, and activation/final JSON.
The final verified backup SHA-256 is
`b77578ac70e06be53321eacc928db19990d44e6d4c57faedc31b551a3dabbf48`.
Automatic pre-schema-v6 backup was independently checked on the isolated copy.

The old release/image `find-similar-pc-final2-20261025` and old volume
`hydra-studio_similarity-pc-20261025` remain intact. To roll back, stop writers,
preserve the upgraded volume/new decisions/media, reconcile post-release changes,
restore the old configuration and current symlink, and start the old image against
its old schema-v5 volume. Never run it against schema v6. The image-only worker
cannot serve the retired protocol; review/export can operate with similarity offline.
Do not overwrite a running database or discard post-release reviews.

The laptop uses `.venv-similarity` and private corpus/token files under
`%LOCALAPPDATA%/HydraTrainingStudio/`. It is running in the background; launch
`deploy/run-similarity-worker.ps1 -Server http://192.168.0.15:8447` again after
sign-in/reboot. Automatic login startup was not installed. Existing historical
array data was left untouched and is no longer read. Staging containers were
stopped/removed; the production worker remains running.

See [machine-readable evidence](evidence/image-only-release-2026-10-25.json),
[image-search setup](similarity.md), and [implementation](image-retrieval.md).
