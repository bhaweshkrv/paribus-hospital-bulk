from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.directory_client import DirectoryClient, RetryPolicy
from app.jobs import JobStore
from app.processor import BulkProcessor
from app.routes import router

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    settings: Optional[Settings] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> FastAPI:
    """Build the app. `transport` lets tests swap in a fake upstream."""
    settings = settings or Settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http = httpx.AsyncClient(
            base_url=settings.upstream_base_url,
            transport=transport,
            timeout=httpx.Timeout(settings.read_timeout, connect=settings.connect_timeout),
            # One pooled client for the whole process: connections are reused
            # across rows and jobs instead of a TLS handshake per hospital.
            limits=httpx.Limits(
                max_connections=settings.max_concurrency,
                max_keepalive_connections=settings.max_concurrency,
            ),
        )
        client = DirectoryClient(
            http,
            retry=RetryPolicy(
                max_attempts=settings.max_attempts,
                base_delay=settings.retry_base_delay,
                max_delay=settings.retry_max_delay,
            ),
            max_concurrency=settings.max_concurrency,
        )
        app.state.settings = settings
        app.state.directory = client
        app.state.processor = BulkProcessor(client, JobStore(settings.max_jobs))
        warmup = asyncio.create_task(client.ping()) if settings.warm_upstream_on_startup else None
        try:
            yield
        finally:
            if warmup:
                warmup.cancel()
            await app.state.processor.shutdown()
            await http.aclose()

    app = FastAPI(
        title="Hospital Bulk Processing API",
        version="1.0.0",
        description="Bulk-creates hospitals from CSV uploads via the Hospital Directory API.",
        lifespan=lifespan,
    )
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/health", tags=["ops"])
    async def health():
        return {"status": "ok", "upstream": settings.upstream_base_url}

    @app.get("/health/upstream", tags=["ops"])
    async def upstream_health():
        started = time.perf_counter()
        reachable = await app.state.directory.ping()
        latency_ms = round((time.perf_counter() - started) * 1000)
        return {"reachable": reachable, "latency_ms": latency_ms}

    @app.get("/", include_in_schema=False)
    async def ui():
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
