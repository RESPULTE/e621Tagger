> Storage/review revision: Query fetches fresh transient e621 results; only human labels become reviewed dataset members. Tagged contains positive/negative/unsure reviews and export actions. Downloads tracks explicit collection saves and dataset-media exports through one bounded queue. Double-tap saves idempotently and never labels or unsaves. Existing data is preserved through additive schema v5 migration. Hydra multi-tag training is unavailable without a compatible external trainer/loader. See [storage plan](docs/storage-review-plan.md) and [architecture](docs/architecture.md).

# Hydra Training Studio

A private e621-style workspace for finding posts and reviewing examples for a target tag. Supports existing e621 tags and custom labels, fresh 50-post query batches, a virtualized thumbnail gallery, original and animated images, video with seeking, and audited positive / negative / unsure reviews.

**Milestone:** P0–P4 discovery. Dataset quality, immutable metadata exports and bounded published-Hydra neighbor discovery are available. Arbitrary-image extraction, training and remote GPU work remain optional follow-on phases. The runtime has no PyTorch or checkpoint dependency. The original `hydra/` library and local `models/` assets are preserved for future GPU integration.

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

1. Search tags by name/category/popularity, or create a custom target using a query and/or up to 50 seed IDs.
2. Query fetches fresh e621 results on each visit and continues in fifty-post batches while scrolling. The editable query starts with the project's selection; search modifiers and multi-tag queries never change the named review target. Autocomplete replaces only the current token and preserves surrounding syntax and prefixes. Query pages are transient; there is no cumulative imported-post archive or New count.
3. Single-tap a thumbnail to open its original (keyboard Enter opens immediately). Double-tap a stationary thumbnail, or use its heart button, to queue an idempotent original/sidecar save to the configured collection. Movement and scrolling cancel the gesture. Downloads shows queued, verified, failed and cancelled work. Repeated saves never unsave or label; removal is a separately named post-view action. Display media stays off server disk.
4. Use `1` positive, `2` negative, `3` unsure, arrows to navigate, and `Z` to undo, or use the mobile buttons. Skip leaves the label unchanged.
5. Labels and undo history survive restarts. Failed/conflicting saves show retry controls. Review each thumbnail with + / − / ?; the gallery has no selection checkboxes or persistent bottom bar. Undo is available in the toolbar and with Z.

Tag absence never becomes a verified negative automatically. Reviews are project-specific; unsure and unreviewed are distinct.

## Similar posts and tag suggestions

Hold any gallery thumbnail or expanded post for 600 ms and release on the same post to queue **Find Similar**. Shift+S is the keyboard equivalent. Stay in the current view or follow the notice to the Similarity tab, which contains a result folder per source. It shows up to 32 nearest published neighbors with progressive previews and existing review controls. Completed progress bars disappear in both Similarity and Downloads.

Similarity uses three read-only, memory-mapped NumPy files from the pinned Hydra 3.5 KNN index **on the RTX 4070 processing PC**, with an outbound polling worker. The server keeps no index. The Similarity tab shows **Processing machine offline** while that worker is disconnected. Install it using [the similarity setup guide](docs/similarity.md); this known-ID lookup needs no GPU, PyTorch, full embedding shards, or vector database. Searches are session-only and disappear on server restart. Only explicit reviews persist verified seed/rank/distance/revision provenance; unreviewed browsing never becomes training data. Existing neighbor APIs remain compatible.

**Suggested tags** counts recurring general tags in available neighbor metadata, under Source tags and neighbor tag counts. These are descriptive source tags, not predictions or human labels. **Hide tags** hides both source tags and suggestions. No public e621 tags are edited. Frozen datasets retain the provenance of the selected review decision.

The published snapshot ends in July 2026 and may not cover newer/unindexed posts. Offline, missing or malformed index results leave review available. `STUDIO_PUBLISHED_NEIGHBORS=false` disables new lookups while retaining saved evidence. See [optional discovery](docs/optional-workers.md) for transfer limits, integrity and representation boundaries.

## Dataset quality and exports

Open **Tagged → Dataset & export** from a project. Additional queries may be marked unrelated or lookalike/hard-negative triage; these are provenance categories, not labels. Review positives and real negatives, inspect availability/conflicting exact duplicates, and prepare missing preview fingerprints in batches of 64.

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
