# Hydra Training Studio

A private e621-style workspace for finding posts and reviewing examples for a target tag. Supports existing e621 tags and custom labels, 50-post imports, a virtualized thumbnail gallery, original and animated images, video with seeking, and audited positive / negative / unsure reviews.

**Milestone:** P0–P2. Dataset exports, embeddings, training, and remote GPU work remain deferred. The runtime has no PyTorch or checkpoint dependency. The original `hydra/` library and local `models/` assets are preserved for future GPU integration.

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
2. Import 50 metadata records; **Add next 50** resumes the cursor. Originals load only when a post is opened.
   The additional-query field starts with the project's original selection. Edit the complete query to append terms such as `score:>100`, or remove the original tag when you want a broader search. Studio imports exactly the displayed query; existing candidates and labels are retained.
3. Click a thumbnail for the original image or video. An e621-provided MP4 fallback is available where supplied upstream.
4. Use `1` positive, `2` negative, `3` unsure, arrows to navigate, and `Z` to undo, or use the mobile buttons. Skip leaves the label unchanged.
5. Labels and undo history survive restarts. Failed/conflicting saves show retry controls. Batch reviews affect only explicitly selected posts and commit atomically.

Tag absence never becomes a verified negative automatically. Reviews are project-specific; unsure and unreviewed are distinct.

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

Browser tests use an isolated mock server and synthetic media. Generated API types are checked in; regenerate them after schema changes. `/health` checks readiness; authenticated `/api/metrics` reports upstream requests, throttles, media bytes/cache hits and queue depth.

See [architecture](docs/architecture.md), [operations](docs/operations.md), and [data policy](docs/data-policy.md) for backups, limits and validation boundaries.

See [validation results](docs/validation.md) for automated/live checks and remaining Docker, hardware and physical-iPhone verification.

## Attribution

This project began as [Fenrir784/e621tagger](https://github.com/Fenrir784/e621tagger). Original Hydra attribution and the [Apache 2.0 license](LICENSE) are retained. Studio does not edit or upload public e621 tags.
