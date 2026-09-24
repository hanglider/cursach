"""Общие фикстуры: PostgreSQL в docker (testcontainers), приложение с подменёнными
источником метрик и проверкой идентичности, клиент с токенами ролей."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator

import httpx
import pytest
from testcontainers.community.postgres import PostgresContainer

from app.config import Settings
from app.identity import Identity, StaticIdentityReader
from app.main import create_app
from app.thanos import StaticSource

TOKENS = {"scheduler": "tok-scheduler", "dr-controller": "tok-dr", "admin": "tok-admin"}
ENDPOINTS = {
    "cluster-a": "https://cluster-a-control-plane:6443",
    "cluster-b": "https://cluster-b-control-plane:6443",
}
# Атрибуты стенда (ADR 0009).
CLUSTERS = {
    "cluster-a": {
        "region": "eu-west",
        "provider": "local",
        "certifications": ["pci-dss", "iso-27001"],
    },
    "cluster-b": {"region": "eu-central", "provider": "local", "certifications": ["iso-27001"]},
}
IDENTITIES = {
    ENDPOINTS[c]: Identity(c, attrs["region"], attrs["provider"], tuple(attrs["certifications"]))
    for c, attrs in CLUSTERS.items()
}


def auth(role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKENS[role]}"}


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with PostgresContainer("postgres:17-alpine", driver=None) as pg:
        yield pg.get_connection_url()


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url, tokens=TOKENS, poll_enabled=False, watch_enabled=False
    )


@pytest.fixture
def source() -> StaticSource:
    return StaticSource()


@pytest.fixture
async def app(settings: Settings, source: StaticSource) -> AsyncIterator:
    app = create_app(settings, identity=StaticIdentityReader(IDENTITIES), source=source)
    async with app.router.lifespan_context(app):
        try:
            yield app
        finally:
            async with app.state.pool.acquire() as conn:
                await conn.execute(
                    "TRUNCATE clusters, placement_decisions, policy_violations, incidents "
                    "RESTART IDENTITY CASCADE"
                )


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://registry") as c:
        yield c


@pytest.fixture
def register(client: httpx.AsyncClient) -> Callable:
    async def _register(cluster_id: str, **overrides) -> dict:
        body = {
            "id": cluster_id,
            "api_endpoint": ENDPOINTS[cluster_id],
            **CLUSTERS[cluster_id],
            **overrides,
        }
        r = await client.post("/api/v1/clusters", json=body, headers=auth("admin"))
        assert r.status_code in (200, 201), r.text
        return r.json()

    return _register
