"""Фоновый опрос Thanos раз в 30 с (spec §4.3, architecture §3.4):
снимок в cluster_metrics и пересчёт slo_status с приоритетом ADR 0003."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime

import asyncpg

from . import repo
from .config import Settings
from .log import event
from .rules import Metrics, compute_status, next_status
from .thanos import MetricsSource

log = logging.getLogger("registry.poller")


async def poll_once(
    pool: asyncpg.Pool, source: MetricsSource, settings: Settings
) -> dict[str, str]:
    """Один цикл. Возвращает статусы после цикла; кластер без метрик получает unknown."""
    observed_at = datetime.now(UTC)
    try:
        fetched = await source.fetch()
    except Exception as e:  # noqa: BLE001 — недоступный Querier даёт unknown, а не падение
        event(log, "metrics_fetch_failed", logging.WARNING, error=str(e))
        fetched = {}

    statuses: dict[str, str] = {}
    async with pool.acquire() as conn:
        current = await repo.cluster_statuses(conn)
        for cluster_id, old in current.items():
            m = fetched.get(cluster_id)
            m = (
                Metrics(observed_at=observed_at)
                if m is None
                else replace(m, observed_at=observed_at)
            )
            computed = compute_status(
                m,
                error_rate_threshold=settings.error_rate_threshold,
                latency_budget_ms=settings.latency_budget_ms,
            )
            new = next_status(old, computed)
            async with conn.transaction():
                await repo.insert_metrics(conn, cluster_id, m)
                # Compare-and-set: если DR Controller успел поставить down между
                # чтением и записью, статус опроса не должен его перекрыть.
                if new != old and not await repo.compare_and_set_status(conn, cluster_id, old, new):
                    new = old
            if new != old:
                event(
                    log, "status_changed", cluster=cluster_id, previous=old, status=new, by="poller"
                )
            statuses[cluster_id] = new
    event(
        log,
        "poll",
        clusters=len(current),
        with_metrics=sum(1 for c in current if c in fetched),
        statuses=statuses,
    )
    return statuses


async def run(pool: asyncpg.Pool, source: MetricsSource, settings: Settings) -> None:
    """Бесконечный цикл до отмены задачи."""
    while True:
        try:
            await poll_once(pool, source, settings)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — один неудачный цикл не останавливает опрос
            event(log, "poll_failed", logging.ERROR, error=repr(e))
        await asyncio.sleep(settings.poll_interval_s)
