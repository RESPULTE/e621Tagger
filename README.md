> Storage/review revision: Query fetches fresh transient e621 results; only human labels become reviewed dataset members. Tagged contains positive/negative reviews and export actions. Downloads tracks explicit collection saves and dataset-media exports through one bounded queue. Double-tap toggles a favourite without changing review labels. Existing data is preserved through additive schema v6 migration. Hydra multi-tag training is unavailable without a compatible external trainer/loader. See [storage plan](docs/storage-review-plan.md) and [architecture](docs/architecture.md).

# Hydra Training Studio

A private e621-style workspace for finding posts and reviewing examples for a target tag. Supports existing e621 tags and custom labels, fresh 50-post query batches, a virtualized thumbnail gallery, original and animated images, video with seeking, and audited positive / negative reviews.

**Milestone:** Dataset quality and immutable binary exports are available. An optional outbound GPU worker supports arbitrary-reference retrieval against a bounded local corpus; see [implementation, evidence and release gates](docs/image-retrieval.md). Similarity is image-only and unavailable while the GPU worker is offline. A processing-PC [native Hydra 3.5 character trainer](docs/character-training.md) consumes frozen media bundles and exports one new character's native query/head extension. The home-server runtime has no PyTorch or checkpoint dependency. Reviewed-data recognition quality still requires evaluation.

## Local setup

Use Python 3.11+ and Node 22.12+. From the repository root:

```sh
python -m venv .venv
# Activate .venv using your shell's activation command.
python -m pip install -r requirements-dev.txt
python -m backend.app.auth hash-password
```

Copy `deploy/.env.example` to `.env` in the repository root. Paste the generated hash into `STUDIO_PASSWORD_HASH`, keeping the single quotes. Configure your e621 username and exact browser-facing origin.

For loopback development, set `STUDIO_PUBLIC_ORIGIN=http://localhost:8000` and `STUDIO_SECURE_COOKIE=false`.

```sh
cd frontend
npm ci
npm run build
cd ..
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Open `http://localhost:8000` and enter your Studio password. Set `STUDIO_MOCK_E621=true` for synthetic images without e621 traffic (try `dragon`, `demo_character`, or exact alias `drake`). Use a separate `STUDIO_DATA_ROOT` for demonstrations; demo reviews also persist.

For Vite hot reload, set `STUDIO_PUBLIC_ORIGIN=http://localhost:5173` and run `npm run dev` alongside the API. Use the built app for browser/performance validation.

## Ubuntu / Docker Compose

```sh
cp deploy/.env.example .env
# Configure .env, then:
docker compose --env-file .env -f deploy/docker-compose.yml up --build -d
```

Compose binds loopback by default. Put Studio behind private HTTPS through your VPN/reverse proxy, preserve the configured Host header, and set `STUDIO_PUBLIC_ORIGIN` to that HTTPS origin. One service serves UI and API, using one persistent named volume. Do not scale backend processes; a data-directory lock rejects concurrent instances.

For automatic access on a trusted private network, set `STUDIO_PASSWORDLESS=true` and leave `STUDIO_PASSWORD_HASH` empty. Restrict the reverse proxy to your LAN/VPN clients and keep the backend bound to loopback. Studio then opens without a sign-in prompt while retaining sessions, secure cookies, exact Origin checks and CSRF tokens. Password authentication remains the default.

For a private HTTP deployment, explicitly set its exact `http://` origin and `STUDIO_SECURE_COOKIE=false`. HTTP and HTTPS use separate session cookie names so a previous Secure cookie does not prevent reconnecting. Private HTTP mutation IDs still use cryptographically random UUIDs.

## Review workflow

1. Open the fox at the bottom left to manage projects. The last active project opens automatically; a new workspace shows a fading notice. Pull the project list down at least 64 pixels and hold for one second (or select New project) to enter an e621 tag with the usual autocomplete. Existing tags and aliases open the same project. Rows show positive and negative totals; select one to switch immediately, including while viewing an artwork. Swipe left (or use the row menu) and click the red trash icon to delete without confirmation. Custom targets remain available through the API.
2. Query fetches fresh e621 results on each visit and continues in fifty-post batches while scrolling. The editable query starts with the project's selection; search modifiers and multi-tag queries never change the named review target. Autocomplete replaces only the current token and preserves surrounding syntax and prefixes. Query pages are transient; there is no cumulative imported-post archive or New count.
3. Single-tap a thumbnail to open its original (Enter opens immediately). Double-tap or double-click a stationary thumbnail or enlarged image/video to toggle its favourite; F on focused media is the keyboard equivalent. A small pink paw appears in the top-right corner when wanted, with a dashed border while saving. Repeat the gesture to remove it: completed app-owned originals are deleted by the worker, while active transfers are cancelled and their owned partial files cleaned. Errors remain visible with retry. Unrelated files, edits, previews and review labels are preserved. Scrolling, movement and long holds do not toggle favourites. Thumbnails show only the green tick and red cross review controls. Display media stays off server disk.

   For GIF, video and animated WebP/AVIF posts, enable frame sampling in the viewer to save stills from chosen moments. Pull the yellow cursor down and hold for one second to save a frame as a yellow horizontal notch on the timeline; a circular progress outline fills during the hold and disappears when capture starts. Release early or pull back to cancel; select a notch to review that frame. For videos and animations, the bottom review pill appears only after a saved frame is focused; positive/negative decisions then stay visible until playback resumes. Use the normal video controls (or the animation Play control) to return to playback. Videos automatically loop back to the beginning. Review each saved frame independently; the gallery outline summarizes reviewed thumbnail and frame decisions by majority. Favouriting a sampled frame saves its PNG to the collection while keeping the review sample.

4. Use `1` positive, `2` negative, arrows to navigate, and `Z` to undo, or use the mobile buttons. Skip leaves the label unchanged.
5. Labels and undo history survive restarts. Failed/conflicting saves show retry controls. Use the Positive / Negative icons; clicking the selected icon again clears that review. Expanded artwork stays in place after a review. Its bottom review bar hides when scrolling up and returns when scrolling down. Z retains keyboard undo.

Tag absence never becomes a verified negative automatically. Reviews are project-specific. Legacy unsure decisions read as unreviewed; their audit history remains preserved.

## Similar posts and tag suggestions

Hold any gallery thumbnail or expanded post for one second and release on the same post to queue **Find Similar**. Shift+S is the keyboard equivalent. Stay in the current view or follow the notice to the Similarity tab, which contains a result folder per source. It shows up to 32 nearest indexed images with progressive previews and existing review controls. Completed progress bars disappear in both Similarity and Downloads.

Similarity requires the RTX 4070 laptop's authenticated GPU image worker. Upload a reference or select any supported e621 post image; neither needs published post-ID coverage. The worker searches artwork encoded in its local exact vector corpus and incrementally encodes missing source posts. There is no historical KNN or remote-neighbor fallback. The server never loads the model/index. Offline, the feature says **Image search unavailable: RTX 4070 laptop offline**. See [setup](docs/similarity.md).

Results are session-only; explicit human reviews retain verified query, candidate, model/index, rank and distance provenance. Similarity history uses project and artist names, with numbering for multiple source artworks by the same artist. Expanded artwork shows every tag beneath the media. Existing historical audit records and frozen datasets remain preserved.

## Dataset quality and exports

Open the visible **Export** button beside the project heading from any project tab. Additional queries may be marked unrelated or lookalike/hard-negative triage; these are provenance categories, not labels. Review positives and real negatives, inspect availability/conflicting exact duplicates, and prepare missing preview fingerprints in batches of 64.

Freeze a named version with a repeatable split seed. Exact MD5 and preview dHash groups remain in one train/validation/test split; optional artist and copyright/series grouping is conservative. Exact duplicate labels that conflict are excluded. The full JSON manifest includes exclusions; CSV and JSONL contain supervised positive/negative rows. JSONL begins with a manifest metadata record. Metadata exports download no originals. Explicit dataset-media export queues checked originals through Downloads and can produce a reproducible media bundle.

The current bounded workflow supports at most 5,000 reviewed samples per project and a 16 MiB manifest. Small or highly connected groups can leave splits empty; the UI reports warnings rather than distributing duplicates across splits. See [dataset policy](docs/datasets.md) for algorithms, availability states and limitations.

## Checks

```sh
python -m pytest -q
python -m ruff check backend
python -m backend.export_schema
cd frontend
npm run generate:api
npm run build
npx playwright install chromium webkit
npm run test:e2e
```

Browser tests use an isolated mock server and synthetic media. Generated API types are checked in; regenerate them after schema changes. `/health` checks readiness; authenticated `/api/metrics` reports upstream requests, throttles, media bytes, shared download concurrency and queue depth.

See [architecture](docs/architecture.md), [operations](docs/operations.md), and [data policy](docs/data-policy.md) for backups, limits and validation boundaries.

See [validation results](docs/validation.md) for automated/live checks and remaining Docker, hardware and physical-iPhone verification.

## Attribution

This project began as [Fenrir784/e621tagger](https://github.com/Fenrir784/e621tagger). Original Hydra attribution and the [Apache 2.0 license](LICENSE) are retained. Studio does not edit or upload public e621 tags.
