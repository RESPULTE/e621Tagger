# Find Similar

Hold a thumbnail or expanded image for 600 ms, then release on the same post.
Moving more than 10 pixels, scrolling, or releasing outside cancels. Single tap
still opens a post; double tap still saves to the collection. Shift+S on focused
post media is the keyboard equivalent. Queuing keeps the current page open.

The Similarity project tab contains one folder per source, queue status, cancellation,
retry, and the usual virtualized review gallery. Completed progress bars disappear
in both Similarity and Downloads; result folders and completion links remain.
Distances are publisher L2 distances, not confidence percentages. Results are
candidates for human review, never automatically positive or negative labels.

## Install on the processing PC

Install application requirements, then run:

```sh
python -m backend.install_hydra deploy/hydra-index
# For files already downloaded:
python -m backend.install_hydra --validate-only deploy/hydra-index
```

Keep these files on the RTX 4070 processing PC. The server does not download or
mount the index. Similarity is available only while the PC's worker is connected
and has validated its index. Otherwise the tab shows **Processing machine offline**
(or an index-validation explanation if the PC is connected with invalid files).

Generate a random secret with at least 32 characters. Store it in a private local
file outside Git, and set the same value as `STUDIO_SIMILARITY_WORKER_TOKEN` in the
server's Compose environment. Never put this credential in browser configuration;
it grants only worker polling and leased lookup-result submission, not review or
session access. Start the PC worker:

```sh
python -m backend.similarity_worker --server http://192.168.0.15:8447 --index deploy/hydra-index --token-file /private/path/similarity-worker-token
```

Use the deployment's exact origin. HTTPS is preferred; the existing private HTTP
deployment requires the trusted LAN/VPN path. The PC makes outbound requests only;
no incoming port or GPU is needed for known-ID similarity lookup. Stop the worker
with Ctrl+C. Starting it again verifies the files and reconnects automatically.
Keep it running while using Similarity. A missing heartbeat marks it offline after
15 seconds. New searches and retries return 503 while offline; retained results
remain visible and can still be reviewed using cached verified row evidence.
An interrupted lookup fails after 20 seconds and requires an explicit retry.

The three arrays total 3,967,504,800 bytes (about 3.70 GiB), plus validation and
optional sorted-row mapping overhead. Installation pins revision
`02bd7bd04c732dd3de011d88d642a1d21024f70d` and verifies publisher SHA-256 checksums.
Existing files are validated, not overwritten; replace invalid files explicitly.
Never modify the index while its PC worker is running. Restart that worker after
replacing it. Worker startup verifies checksums before advertising readiness;
this can take time. Missing or invalid data disables similarity only.
The existing remote-neighbor API remains compatible; this feature never falls back
to it. No embedding shards, GPU, vector database, or model inference are needed.

## Lifetime and persistence

Searches are scoped to the authenticated session and canonical project. At most
16 searches may be active; two workers hydrate through the existing metadata
scheduler. At most 64 finished searches remain in memory, expiring after an hour
without access, with a shared 32 MiB serialized metadata budget. Old finished searches
are evicted first when this budget is reached. A server restart clears searches; browser refresh can recover a
retained search. Retrying a failed search starts metadata preparation again.

Ranked IDs appear before metadata; unavailable posts are replaced from the remaining
64 published neighbors. Temporary API failures require retry, not silent omission.
Previews use a 32 MiB transient memory cache, shared in-flight requests, and browser
blob caching. Original bytes are saved only through explicit collection/export actions.
Existing disk caches are not removed. All upstream request starts share the
`STUDIO_UPSTREAM_INTERVAL` gate (default one second) and existing cooldowns.

The worker reads the local index and returns all published neighbors through
an authenticated, seed-bound lease. The server validates revision, seed, rank,
distance ordering and response bounds, and keeps at most 128 verified rows for
one hour in transient memory. Duplicate lookups share work; cancelled or expired
leases cannot submit results. Browser requests cannot submit rank or distance.

Reviews validate seed membership against this worker evidence and atomically store
event-linked seed/rank/distance/revision evidence. Undo retains history and restores
the preceding decision's provenance in new dataset snapshots. Old frozen manifests
are unchanged. Browsing never creates permanent candidates, search runs or labels.

## API

Authenticated, CSRF-protected POST `/api/projects/{id}/similar` accepts `post_id`,
`limit` (1–32, default 32) and `request_id`. GET on that path lists the session queue;
GET `/{search_id}` returns progressive results and current review states. POST
`/{search_id}/cancel` or `/retry` controls work. Expired results return 410.
`not_indexed` is an explicit terminal status; an offline worker or invalid index returns 503.
Review requests optionally carry `similarity: {seed_post_id, index_revision}`.
Clients never supply trusted rank or distance.

Use synthetic fixtures for automated tests. Measure production warm lookup latency
on the processing PC separately from polling, metadata or thumbnails. The warm
local lookup target is below 100 ms. Server polling/network time is additional.
