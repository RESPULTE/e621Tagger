import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .api import public, router
from .config import Settings
from .datasets import router as datasets_router
from .db import Database
from .e621 import E621
from .embeddings import router as embeddings_router
from .jobs import Workers
from .media import Media
from .mock import SyntheticNeighbors
from .mock import transport as mock_transport
from .neighbor_index import PublishedNeighbors
from .telemetry import Telemetry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# httpx request URLs may include source queries; avoid verbose request logs.
logging.getLogger("httpx").setLevel(logging.WARNING)


def create_app(settings=None, *, transport=None, start_workers=True):
    @asynccontextmanager
    async def lifespan(app):
        app.state.settings = config = settings or Settings()
        app.state.db = db = Database(config.data_root)
        app.state.telemetry = Telemetry()
        source = media = workers = None
        try:
            await db.open()
            active_transport = transport or (httpx.MockTransport(mock_transport) if config.mock_e621 else None)
            app.state.source = source = E621(config, db, active_transport)
            app.state.media = media = Media(config, db, active_transport)
            # Remove abandoned partial downloads after a crash.
            for path in media.cache_root.glob("*.part"):
                path.unlink(missing_ok=True)
            source.start()
            app.state.workers = workers = Workers(
                db, source, media, SyntheticNeighbors() if config.mock_e621 else PublishedNeighbors()
            )
            if start_workers:
                await workers.start(config.media_concurrency)
            yield
        finally:
            if workers:
                await workers.close()
            if source:
                await source.close()
            if media:
                await media.close()
            await db.close()

    app = FastAPI(
        title="Hydra Training Studio",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def security(request: Request, call_next):
        config = getattr(app.state, "settings", None)
        if config:
            origin = config.public_origin.rstrip("/")
            expected_host = urlsplit(origin).netloc.lower()
            if request.headers.get("host", "").lower() != expected_host:
                return JSONResponse({"detail": "Unrecognized host"}, 400)
            supplied = request.headers.get("origin")
            if supplied and supplied.rstrip("/") != origin:
                return JSONResponse({"detail": "Cross-origin requests are not allowed"}, 403)
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not supplied:
                return JSONResponse({"detail": "Origin header is required"}, 403)
        started = time.monotonic()
        response = await call_next(request)
        if request.url.path.startswith("/api/media/") and request.url.path.endswith("/preview"):
            app.state.telemetry.record("local_thumbnail_ms", (time.monotonic() - started) * 1000)
        if "/labels/" in request.url.path and request.method in {"PATCH", "POST"}:
            app.state.telemetry.record("local_label_commit_ms", (time.monotonic() - started) * 1000)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                "Content-Security-Policy": "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
            }
        )
        if request.url.path.startswith("/api") and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(sqlite3.OperationalError)
    async def database_error(request, exc):
        logging.getLogger(__name__).error("SQLite operation failed: %s", type(exc).__name__)
        return JSONResponse(
            {"detail": "Storage is unavailable; your save was not acknowledged. Check disk space and retry."}, 503
        )

    @app.get("/health")
    async def health():
        try:
            await app.state.db.one("SELECT 1")
            healthy = app.state.source.task is not None and not app.state.source.task.done()
            if start_workers:
                healthy = healthy and all(not task.done() for task in app.state.workers.tasks)
            return JSONResponse({"status": "healthy" if healthy else "unhealthy"}, 200 if healthy else 503)
        except Exception:
            return JSONResponse({"status": "unhealthy"}, 503)

    @app.get("/service-worker.js")
    async def retire_worker():
        # A root-scoped old worker must see a successful update to retire its caches.
        script = """self.addEventListener('install',()=>self.skipWaiting());
self.addEventListener('activate',event=>event.waitUntil((async()=>{
for(const key of await caches.keys())if(key.startsWith('e621tagger-'))await caches.delete(key);
await self.registration.unregister();for(const client of await self.clients.matchAll({type:'window'}))client.navigate(client.url);
})()));"""
        return Response(script, media_type="application/javascript", headers={"Cache-Control": "no-store"})

    app.include_router(public)
    app.include_router(router)
    app.include_router(datasets_router)
    app.include_router(embeddings_router)

    @app.get("/{path:path}")
    async def frontend(path: str):
        if path.startswith("api/"):
            return JSONResponse({"detail": "Not found"}, 404)
        root = app.state.settings.frontend_root.resolve()
        target = (root / path).resolve()
        if target.is_relative_to(root) and target.is_file():
            return FileResponse(
                target,
                headers={
                    "Cache-Control": "public, max-age=31536000, immutable" if path.startswith("assets/") else "no-cache"
                },
            )
        if path.startswith("assets/"):
            return JSONResponse({"detail": "Not found"}, 404)
        index = root / "index.html"
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return JSONResponse({"detail": "Frontend is not built. Run npm ci && npm run build in frontend/."}, 503)

    return app


app = create_app()
