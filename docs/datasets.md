# Dataset versions and quality

P3 adds unrelated and lookalike query provenance, project quality summaries, duplicate grouping, deterministic group splits and immutable versions. No source kind assigns a human label. Query imports continue to append candidates and preserve reviews and audit history.

## Snapshot contract

`POST /api/projects/{id}/datasets` takes `request_id`, `name`, `split_seed` (default `studio-v1`), `group_artists` (default true), `group_series` (default false), and `near_hamming` (default 4, range 0–8). A short SQLite transaction captures project metadata, every candidate's reviewed label/version/latest event, availability, media URL/MD5, provenance and matching preview fingerprint. Analysis runs off the event loop. A second short transaction publishes the version and idempotency receipt together; concurrent identical retries publish one version. Later reviews cannot mutate a frozen manifest, enforced by database triggers. Reusing a request ID with different inputs returns 409.

The canonical full manifest is UTF-8 JSON with sorted keys and compact separators. Its SHA-256 is stored alongside it and returned as `X-Manifest-SHA256` on exports. The hash covers the **full JSON manifest**, not CSV/JSONL bytes. Each version has a UUID and format `schema_version=1`; split policy is versioned separately. A freeze captures the state when its snapshot read occurs, even if reviews continue during analysis.

`GET /api/projects/{id}/datasets` lists the latest 100 versions. `GET /api/datasets/{uuid}/export?format=json` exports the exact full manifest including excluded samples. `format=jsonl` emits one `type=manifest` metadata line, then `type=sample` lines for supervised examples. `format=csv` emits supervised examples with dataset ID/hash, review versions, URLs/MD5, group/split and JSON provenance. Spreadsheet formula prefixes are escaped. All exports require a session and stay private. No original media is downloaded or bundled; relative media paths are null.

## Eligibility and grouping

- Only human `positive` and `negative` reviews can have targets 1 and 0. Unsure and unreviewed remain explicit exclusions with null targets and splits.
- Deleted/unavailable posts, missing original URLs, unsupported media, missing MD5 and missing preview URLs are separate availability states. Current supported supervised media extensions are JPEG, PNG and WebP. GIF/video frame sampling is undefined and those formats are excluded. Container formats such as APNG/animated WebP are not independently probed; preview-based grouping does not certify their animation behavior.
- Exact MD5 duplicates use the smallest eligible post ID as their supervised representative. If any positive and negative reviews share an MD5, that exact group is excluded pending review. All original records/provenance remain in the full manifest.
- A 64-bit difference hash is computed from each validated 320px preview derivative: grayscale, Lanczos resize to 9×8, row-wise adjacent-pixel comparisons. Hamming distance ≤4 is the default near-duplicate edge; connected components include transitive edges. Stored fingerprints bind to the source MD5 and preview URL and survive disposable-cache eviction.
- Artist grouping links matching artist tags, excluding generic unknown/anonymous/administrative markers. Optional series grouping uses matching **copyright** tags as a conservative proxy, not a canonical series resolver. Shared tags can create large groups.
- The group ID is its smallest post ID. SHA-256 of `seed:post:<id>`, first 32 bits modulo 100, selects train (0–79), validation (80–89), or test (90–99). Known exact/near duplicates and selected artist/series groups cannot cross splits. Ratios are group assignments, not exact row counts; no balancing step breaks groups. New versions can regroup as new candidates/bridges arrive; old manifests remain immutable.

Preview dHash is a heuristic. Crops, edits, composition changes and missing fingerprints among unreviewed bridging posts can escape detection. It can also conservatively merge visually similar but distinct samples. Inspect held-out groups and ambiguous examples before evaluating a classifier; this is not a guarantee against every semantic leak. Frozen availability records metadata at capture time, not a live download guarantee.

## Preparation and bounds

`GET /api/projects/{id}/quality` reports human-label counts, source diversity, availability, exact/known near duplicate groups, contradictory exact labels and unsure examples. Duplicate counts depend on fingerprint coverage. `POST /api/projects/{id}/quality/prepare` queues up to 64 eligible reviewed missing fingerprints through the existing durable preview queue; repeatedly preparing is safe. Failed previews show errors and retain the five-minute retry cooldown. A freeze returns 409 until every otherwise-eligible reviewed sample has a source-matched fingerprint.

The workflow caps projects at 5,000 candidates, manifests at 16 MiB and BK-tree near-hash analysis at one million comparisons. It rejects excess work explicitly instead of freezing an incomplete subset. No full embedding index, GPU or new dependency enters the server runtime.

Schema version 2 is additive. Before upgrading an existing v1 database, startup creates and integrity-checks an online SQLite backup under `data/backups/pre-schema-v2-*.sqlite3`; failure aborts migration. Legacy thumbnails lack source identity and are lazily rebuilt on first access, without deleting labels or provenance. V2 thumbnails invalidate on source URL/MD5 change. A downgrade to a v1 application requires restoring the pre-migration backup into a **fresh volume** and preserving the v2 volume; never overwrite the live database or discard later reviews.
