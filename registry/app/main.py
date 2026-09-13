"""Сборка приложения: пул БД, миграции, фоновый опрос, маршруты."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, status

from . import db, poller
from .api import clusters, decisions, violations
from .auth import Auth
from .config import Settings
from .identity import IdentityReader, KubeconfigIdentityReader
from .log import event
from .thanos import MetricsSource, ThanosSource

log = logging.getLogger("registry")


def create_app(
    settings: Settings,
    *,
    identity: IdentityReader | None = None,
    source: MetricsSource | None = None,
) -> FastAPI:
    """`identity` и `source` подменяются в тестах; None — по настройкам."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool = await db.create_pool(settings.database_url)
        applied = await db.migrate(pool)
        event(log, "started", migrations_applied=applied, poll=settings.poll_enabled)
        http = httpx.AsyncClient()
        app.state.settings = settings
        app.state.pool = pool
        app.state.auth = Auth(settings.tokens)
        app.state.identity = identity or (
            KubeconfigIdentityReader(settings.kubeconfig_dir) if settings.identity_check else None
        )
        app.state.source = source or ThanosSource(settings.thanos_url, http)
        task = (
            asyncio.create_task(poller.run(pool, app.state.source, settings))
            if settings.poll_enabled
            else None
        )
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await http.aclose()
            await pool.close()

    app = FastAPI(title="Federation registry", version="0.1.0", lifespan=lifespan)
    app.include_router(clusters.router)
    app.include_router(decisions.router)
    app.include_router(violations.router)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        try:
            async with app.state.pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
        except Exception as e:  # noqa: BLE001 — любая ошибка БД: не готов
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"database: {e}") from e
        return {"status": "ok"}

    return app
