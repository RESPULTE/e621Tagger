# Image similarity search

Similarity uses Hydra 3.5 image embeddings and the local vector corpus on the
RTX 4070 laptop. There is no historical post-ID lookup or published-neighbor
fallback. A source post does not need to have appeared in any published dataset.

The feature is available only while the authenticated GPU worker advertises its
image capability and maintains its heartbeat. Otherwise the UI says **Image search
unavailable: RTX 4070 laptop offline** (or that the GPU worker is not ready).
Upload/post-search and refresh controls are disabled; hold/Shift+S check capability before submitting. The API independently returns 503
when unavailable, including if the laptop disconnects between those requests.

## Use

Open a project's existing Similarity tab, use the **+** control beside the source
(**Search another image**), and upload a reference or enter an e621
post ID. Hold a thumbnail/expanded image for two seconds and release on that post, or
press Shift+S on focused media, to search from artwork already being viewed.
A fading progress ring fills during the hold; the search starts only on release. Moving, scrolling or releasing
outside cancels a hold. Ordinary opening and explicit collection saves still work.

The filter query sits above the results. **Recent searches** shows the latest 20
image searches, newest first, in a collapsible list of rows. Select a row to reopen
its results; the plus icon loads more matches and the circular arrow refreshes
the search. Icon controls have accessible names and tooltips. Rows use the project name and source artists, with numbering when an artist has several source artworks. Technical search details are omitted; artwork tags are always expanded beneath the enlarged media.

Results are ranked artwork from the laptop's corpus, with progressively hydrated
metadata and previews. L2 distance is not a confidence percentage or proof of
character identity. Mark Positive or Negative after human review; skip navigates
without creating a label, event, dataset membership or saved original. Clear and
undo remain available. Each selected image keeps its complete ranking independently
of project tags, review decisions and earlier search folders. Source-tag counts
are descriptive metadata and do not affect ranking.

**Filter similar images** accepts the same e621 query syntax and tag autocomplete
as project discovery, including exclusions, ratings, wildcards, aliases, and
grouped queries. Apply an empty query or choose **Clear filter** to restore the
loaded matches. The filter stays in the URL for reloads and applies when switching
source folders or loading more. It filters only loaded matches, not the full image
corpus; **Load more matches** checks another ranked page. An empty filtered page
does not mean the retained search is exhausted.

Both discovery and filtering use `Feed.search_posts` and the shared e621 scheduler,
credentials, and retry/rate controls. Filtering sends `( <query> ) id:<loaded IDs>`
to `/posts.json`, then preserves the original similarity ranks and review evidence.
The official [query parser](https://github.com/e621ng/e621ng/blob/master/app/logical/tag_query.rb)
and [numeric list parser](https://github.com/e621ng/e621ng/blob/master/app/logical/parse_value.rb)
implement this intersection. Like discovery, explicit `order:` terms are rejected;
filtering keeps similarity order. Match-ID caches are session-only, bounded to
eight queries per search and expire after 60 seconds; refreshing the image search
clears them. API failures are shown as errors, never as successful empty searches.

**Load more matches** appends the next ranked page from the retained search (up to
64 neighbors), without repeating cards or rerunning the encoder. It remains usable
after the GPU disconnects while that search is retained. An explicit end message
appears when the retained result set is exhausted. **Refresh search** recomputes
the ranking against the current index and resets to its first page; the same image
and unchanged index correctly return the same order. Refresh does not manufacture
different matches by shuffling them.

The validated broad generation was enabled locally on 5 November 2026; see the [live validation record](validation.md#2026-11-05--broad-native-image-retrieval). The UI reports the searchable corpus size for each search. Coverage depends on
the generation loaded by the laptop: the pilot contains only a small evaluation
collection; the optional broad generation contains 6,122,692 published image
vectors plus subsequently encoded posts. Neither is a live index of every e621
upload. The optional worker count field is backward compatible with old workers;
deploy the server before restarting an updated worker. The old published
neighbor-ID arrays cannot answer arbitrary new images and remain unused.

Controls sit beside the gallery on desktop. On phones they are collapsed/bounded
above the gallery, with one-row tabs, so forms/history cannot consume the entire
image viewport.

## Processing laptop setup

Use Python 3.12 and the CUDA environment separately from the server:

```powershell
uv venv .venv-similarity --python 3.12
uv pip install --python .venv-similarity/Scripts/python.exe torch==2.8.0 --index-url https://download.pytorch.org/whl/cu128
uv pip install --python .venv-similarity/Scripts/python.exe -r requirements-similarity.txt
```

Prepare a JSONL manifest of original images obtained for indexing:
`{"post_id":123,"md5":"<32 hex>","image":"originals/123.png"}`.
Paths resolve relative to that manifest. Preparation checks original identity,
still-image formats, decoded pixels and pipeline compatibility. It is safe to
repeat after interruption:

```sh
python -m backend.prepare_similarity --checkpoint models/hydra-3.5.safetensors --index /private/image-index.sqlite3 --manifest corpus.jsonl --report ingestion.json
python -m backend.similarity_worker --server http://YOUR-PRIVATE-SERVER:8447 --vector-index /private/image-index.sqlite3 --checkpoint models/hydra-3.5.safetensors --token-file /private/processing-worker.token
```

Windows also provides `deploy/run-similarity-worker.ps1 -Server <origin>`.
It defaults to `.venv-similarity`, the repository checkpoint and corpus/token
files under `%LOCALAPPDATA%/HydraTrainingStudio/`. Keep the worker running while
using image search. Install automatic startup once from the same Windows account:

```powershell
./deploy/install-similarity-worker-startup.ps1 -Server http://YOUR-PRIVATE-SERVER:8447
```

This registers `HydraTrainingStudio-Similarity` in Windows Task Scheduler, starts
30 seconds after that user signs in, and retries a failed worker every minute.
It runs without elevation, permits battery operation, and has no runtime limit.
The installer resolves physical corpus/token paths so startup also works when
installed from a Windows app that virtualizes LocalAppData. Diagnostics append to
`similarity-worker.log` beside the token file (rotated at startup above 1 MiB).
Installation does not launch a second copy alongside an already running worker.
For an immediate managed start, first stop the existing worker, then use
`Start-ScheduledTask -TaskName HydraTrainingStudio-Similarity`. Inspect status with
`Get-ScheduledTaskInfo -TaskName HydraTrainingStudio-Similarity`. To remove startup,
stop that task and unregister it with `Unregister-ScheduledTask` (this does not
delete corpus, credentials or media). The laptop must be awake and signed in.
No inbound laptop port is required. HTTPS is preferred; private HTTP requires a trusted LAN/VPN.

The private token must match `STUDIO_SIMILARITY_WORKER_TOKEN` on the server.
Never put it in browser configuration or Git. Only the laptop loads the checkpoint
and vectors; the server handles sessions, paced metadata, reviews and datasets.
No precomputed neighbor-ID lookup or remote neighbor API is used. The optional
broad generation below supplies actual image vectors, not tag or post-ID lookup.

### Optional broad image generation

The approved broad build uses the official [Hydra image embeddings](https://huggingface.co/datasets/RedRocket/Hydra-Embeddings)
at pinned revision `02bd7bd04c732dd3de011d88d642a1d21024f70d`.
It requires about 25.2 GB of verified raw shard downloads, another 25.1 GB for the
decoded vector matrix and about 1.6 GB for compressed search codes, plus working
space. Keep these private artifacts outside Git and preserve the pilot corpus.

`backend.build_broad_index` consumes a pinned SHA-256 shard inventory and the
matching published post-ID catalog. It validates every row, journals completed
shards and computes the full-corpus mean. `backend.build_broad_ann` trains a
deterministic PQ256 index on 131,072 sampled vectors, scans all compressed codes
for candidates and retains the original 2,048-dimensional vectors for reranking.
`backend.validate_broad_index` independently checks the centroid and exact
full-corpus neighbors for 16 images excluded from quantizer training, then compares
six freshly encoded original images against their published vectors. Activation
requires mean Recall@32 ≥ 0.95, minimum ≥ 0.90, p95 query time ≤ 10 seconds and the
native-query compatibility gates. Failed/incomplete generations cannot be loaded.
These are numerical retrieval checks, not a character-recognition quality claim.

Load a validated generation with `--broad-index <generation-folder>` on the worker,
or `-BroadIndex <generation-folder>` on the Windows launcher. To replace the known
startup task, stop its worker, back up its task XML, then run the installer with
`-BroadIndex <generation-folder> -ReplaceExisting` and start the task. Do not run
two workers against one corpus. Deploy the updated server before this switch.

Broad queries encode the selected pixels, subtract the pinned corpus mean and
normalize; tags and project membership do not enter ranking. New/cached post
vectors use a separate SQLite supplement named with the centered space identity.
The original pilot SQLite file is preserved for rollback. File hashes and model,
preprocessing and centering identities are checked on every worker startup.
Publisher rows have no image hashes: the server binds fresh e621 MD5 metadata
before review and checks it again when committing provenance. Deleted/unavailable
posts and duplicate media are skipped while filling the requested page.

## Bounds and persistence

References accept still JPEG/PNG/WebP, default 8 MiB and 20 million pixels,
configurable up to 16 MiB/40 million via `STUDIO_REFERENCE_MAX_BYTES` and
`STUDIO_REFERENCE_MAX_PIXELS`. The preparation/benchmark CLI override is
`--max-image-bytes 16777216`. Radar post 6425469's original requires the 16 MiB
byte setting. Uploads stay temporary and never become training samples automatically.

The exact local supplement is capped at 50,000 vectors and reads blocks of 512. It
stores source MD5/SHA-256 and versioned model/space identity transactionally.
Post searches reuse an identity-matching vector, or fetch/verify/encode and insert
the missing original. Self-matches and repeated media are excluded. Neither a
resized sample nor a mismatched original silently stands in for the source.

Worker protocol v2 binds jobs/results/transfers to authenticated workers, leases,
image identity and embedding space. Heartbeats expire after 15 seconds; jobs time
out after 90 seconds. Queues are bounded and cancelled/stale work cannot complete.

Searches belong to the authenticated session/project, expire after an hour and
disappear on server restart. At most 16 searches run, 64 finished folders remain,
and references/metadata share a 32 MiB budget. Results retained in the same server
session can still be reviewed after the laptop disconnects. A server restart
requires a fresh search. Reviews persist event-linked query/checksum, candidate
identity, model/space/index revision, metric, rank and distance. Existing historical
audit evidence and frozen manifests remain intact, with no active historical retrieval.

See [implementation and evidence](image-retrieval.md), [datasets](datasets.md),
and [backup/rollback](operations.md).
