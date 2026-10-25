# Architecture

React and FastAPI share one origin. Exactly one Uvicorn process owns the prioritized metadata scheduler, HTTP pools, media permits, durable job workers, SQLite connection and process lock. No neural model or trainer is loaded. Shutdown joins filesystem work before releasing writer ownership.

`feed.py` owns fresh discovery and expiring source metadata. `api.py` owns HTTP contracts and the reviewed gallery. `reviews.py` owns atomic human transitions, events and receipts. `e621.py` owns alias resolution and paced metadata requests. `media.py` validates CDN URLs, converts previews in memory and streams originals/ranges. `jobs.py` is the only durable scheduler. `transfers.py` provides the common original-transfer implementation; `library.py` publishes collection originals and sidecars; `downloads.py` publishes frozen dataset media and packages checked exports. Blocking disk work uses the cancellation-safe helpers in `filesystem.py`; network I/O is asynchronous.

## Storage lifecycle

Query requests e621 anew with descending-ID cursor pagination. Its results include reviewed and unreviewed posts. They do not insert posts, project membership, discoveries, labels or browsing-media jobs. The process feed caps source metadata at 2,000 posts and 16 MiB, with a 15-minute TTL and a 256 KiB per-post limit. Separate bounded project contexts prevent provenance leaking between projects that browse the same post. The frontend retains at most forty fifty-post pages, virtualizes cards, expires thumbnail blobs after unmount and revokes object URLs.

Previews cap incoming bytes at 8 MiB and decoded pixels at 20 million, then convert to 320px WebP in memory. Original/sample/video responses stream 64 KiB chunks with single-range, If-Range and HEAD support. All display media uses private no-store responses; thumbnail fetch explicitly bypasses old browser caches. Display never writes media or cache rows. Explicit quality preparation retains only source-bound numeric dHash fingerprints for human-reviewed images.

A review atomically promotes essential source identity/metadata and query provenance and writes a positive, negative or unsure label, version, event and receipt. Clearing a label appends history and removes it from Tagged, without changing collection files. Displayed media and review/save requests carry the original MD5. A different original is rejected instead of pairing an old decision with new bytes; the first human review may refresh legacy metadata, while a source with human history remains pinned. Frozen manifests capture the reviewed snapshot; later display refetches do not rewrite it. Legacy unreviewed memberships/cache files remain preserved but are excluded from gallery membership and new datasets. No purge runs at startup.

## Project identity and compatibility

Schema v4 adds a unique canonical target registry keyed by target kind/id. Resolve e621 aliases before the serialized get-or-create transaction. Query text is discovery context, never target identity; arbitrary e621 syntax is not sorted. Custom target names retain a stable definition; a different definition returns 409. Multi-tag searches inside a project still review only that project's named target.

Existing duplicate projects, labels, versions, events and dataset ownership are preserved. Read views map old IDs to their canonical family and show reviewed members together. Conflicting current labels are surfaced with their original project/state/version, blocked from editing through an implicit merge, and excluded from supervised samples. Existing frozen datasets retain exact bytes and remain discoverable across the family. No conflict winner is chosen automatically.

SQLite uses WAL, foreign keys, a busy timeout and short serialized transactions. Upgrading schema v1/v2/v3 first creates and verifies an online `pre-schema-v4` backup, then applies an additive transaction. Newer schemas are rejected. Legacy pending discovery/cache jobs are cancelled during migration; existing data and files are retained.

## Shared download queue

Only explicit collection saves and dataset-media exports publish originals. Both use the jobs table, renewable leases, one bounded worker pool (default two), the same transfer primitive, CDN pacing and shared media capacity. At the default four media permits, downloads occupy at most two, leaving capacity for an interactive original and preview. Smaller configured budgets degrade to serial service. Quality fingerprint and neighbor tasks use the existing scheduler for their separate bounded metadata responsibilities.

Intent keys include purpose/destination: collection post/version versus frozen dataset/post. Repeated same-intent saves/exports reuse jobs; the same source requested for two destinations is independently fulfilled and verified in each destination. Async workers overlap I/O; threads are used only for blocking disk/Pillow operations. Metrics expose measured active/peak transfers, verified bytes, output counts, media permits and queue states.

States are pending -> running -> done, transient failures -> delayed pending, terminal failures -> failed, and explicit cancellation -> cancelled. Attempts cap at four. Running jobs recover on restart; per-job heartbeats maintain leases. Cancelling before publication cooperatively stops a transfer; cancellation during the short publication boundary is rejected until completion. Retry waits until the old destination writer has relinquished ownership. Metadata requests prioritize browsing over background downloads and retain upstream Retry-After cooldowns.

Transfers check free space, bounded length, e621 MD5, SHA-256 and fsync before no-clobber publication. Journalled partial cleanup checks exact path containment, inode and filesystem device; it never recursively deletes or guesses ownership. Collection staging also retains the existing inode/hash publication journal, sidecars-first ordering and explicit, separately named owned-file removal action. Saves are idempotent; double-tapping never unsaves or labels a post. Offline/replaced roots and existing manual filename collisions fail without overwriting files.

Dataset media lives under the owned data root's `exports/<dataset UUID>/`, independently from Smut. Only eligible frozen positive/negative samples are queued. Checked manifests contain relative paths, byte lengths and SHA-256 values. Optional deterministic ZIP packaging rereads and checks each byte stream; its temporary archive is closed after delivery. ZIP packaging caps at 4 GiB; individual files and a checked manifest remain available for larger datasets. Metadata exports download no originals.

## Training boundary

Hydra checkpoint/inference code has many output labels, but this repository supplies no compatible trainer, data loader, loss or artifact writer. Studio exports one explicitly reviewed scalar target per project. Neither a multi-tag query nor a negative conjunction review is decomposed into component labels. No multi-tag training claim or new trainer is introduced. See [dataset policy](datasets.md), [operations](operations.md) and the evidence report retained with this release.


## Processing-machine similarity discovery

`similarity_worker.py` runs on the RTX 4070 PC, where `hydra_knn.py` performs exact
post-ID lookup against three pinned read-only NumPy arrays. The server never mounts
the index. `processing_machine.py` owns a bounded outbound-polled bridge with a
separate scoped bearer credential, seed-bound leases, timeouts and heartbeat expiry.
Only authenticated worker replies can supply ranks/distances. Offline status blocks
new searches and retries, while retained result folders remain available for review.
`similarity.py` owns session-scoped, bounded in-memory tasks and metadata hydration;
it reuses Feed, the upstream scheduler and the virtualized gallery. It does not write
searches or unreviewed candidates into SQLite. Upstream metadata and media starts
share one admission gate; previews share a bounded transient byte cache.
Schema v5 adds event-linked similarity provenance. Review transactions validate
membership against verified worker evidence, preserve version/idempotency checks, and persist
only selected evidence. Undo events copy the restored decision's evidence, allowing
bounded bulk reads during dataset freezing. See [the contract](similarity.md).
