"""SQL-запросы к каталогу. Чистый SQL через asyncpg, без ORM (ADR 0025)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg

from .models import ClusterOut, DecisionOut, MetricsOut, ViolationOut
from .rules import Candidate, Constraints, Metrics, rejection_reasons

_CLUSTER_WITH_METRICS = """
SELECT c.id, c.region, c.provider, c.api_endpoint, c.labels, c.certifications,
       c.slo_status, c.registered_at,
       m.observed_at, m.error_rate, m.latency_p95_ms, m.saturation,
       m.cpu_available, m.memory_available_gi
FROM clusters AS c
LEFT JOIN LATERAL (
    SELECT * FROM cluster_metrics
    WHERE cluster_id = c.id
    ORDER BY observed_at DESC
    LIMIT 1
) AS m ON true
"""


def _metrics(row: asyncpg.Record) -> Metrics | None:
    if row["observed_at"] is None:
        return None
    return Metrics(
        observed_at=row["observed_at"],
        error_rate=row["error_rate"],
        latency_p95_ms=row["latency_p95_ms"],
        saturation=row["saturation"],
        cpu_available=row["cpu_available"],
        memory_available_gi=row["memory_available_gi"],
    )


def metrics_out(m: Metrics | None) -> MetricsOut | None:
    return None if m is None else MetricsOut(**m.__dict__)


def cluster_out(row: asyncpg.Record) -> ClusterOut:
    return ClusterOut(
        id=row["id"],
        region=row["region"],
        provider=row["provider"],
        api_endpoint=row["api_endpoint"],
        labels=row["labels"],
        certifications=list(row["certifications"]),
        slo_status=row["slo_status"],
        registered_at=row["registered_at"],
        metrics=metrics_out(_metrics(row)),
    )


def candidate(row: asyncpg.Record) -> Candidate:
    return Candidate(
        id=row["id"],
        region=row["region"],
        certifications=tuple(row["certifications"]),
        slo_status=row["slo_status"],
        metrics=_metrics(row),
    )


# --- clusters -----------------------------------------------------------------


async def list_clusters(
    conn: asyncpg.Connection,
    *,
    status: str | None = None,
    region: str | None = None,
    certifications: list[str] | None = None,
) -> list[asyncpg.Record]:
    return await conn.fetch(
        _CLUSTER_WITH_METRICS
        + """
        WHERE ($1::text IS NULL OR c.slo_status = $1)
          AND ($2::text IS NULL OR c.region = $2)
          AND c.certifications @> $3::text[]
        ORDER BY c.id
        """,
        status,
        region,
        certifications or [],
    )


async def get_cluster(conn: asyncpg.Connection, cluster_id: str) -> asyncpg.Record | None:
    return await conn.fetchrow(_CLUSTER_WITH_METRICS + " WHERE c.id = $1", cluster_id)


async def upsert_cluster(
    conn: asyncpg.Connection,
    *,
    id: str,
    region: str,
    provider: str,
    api_endpoint: str,
    labels: dict[str, str],
    certifications: list[str],
) -> bool:
    """Регистрация идемпотентна (ADR 0021): повтор обновляет атрибуты, статус и историю не трогает.
    Возвращает True, если кластер создан впервые."""
    row = await conn.fetchrow(
        """
        INSERT INTO clusters (id, region, provider, api_endpoint, labels, certifications)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (id) DO UPDATE SET
            region = EXCLUDED.region, provider = EXCLUDED.provider,
            api_endpoint = EXCLUDED.api_endpoint, labels = EXCLUDED.labels,
            certifications = EXCLUDED.certifications
        RETURNING (xmax = 0) AS inserted
        """,
        id,
        region,
        provider,
        api_endpoint,
        labels,
        certifications,
    )
    return bool(row["inserted"])


async def set_status(conn: asyncpg.Connection, cluster_id: str, slo_status: str) -> str | None:
    """Безусловная запись статуса (DR Controller, ADR 0003). Возвращает прежний статус."""
    row = await conn.fetchrow(
        """
        UPDATE clusters AS c SET slo_status = $2
        FROM (SELECT slo_status FROM clusters WHERE id = $1 FOR UPDATE) AS old
        WHERE c.id = $1
        RETURNING old.slo_status AS previous
        """,
        cluster_id,
        slo_status,
    )
    return None if row is None else row["previous"]


async def compare_and_set_status(
    conn: asyncpg.Connection, cluster_id: str, expected: str, new: str
) -> bool:
    """Запись фонового опроса: только если статус не изменился с момента чтения."""
    result = await conn.execute(
        "UPDATE clusters SET slo_status = $3 WHERE id = $1 AND slo_status = $2",
        cluster_id,
        expected,
        new,
    )
    return result == "UPDATE 1"


async def delete_cluster(conn: asyncpg.Connection, cluster_id: str) -> bool:
    return await conn.execute("DELETE FROM clusters WHERE id = $1", cluster_id) == "DELETE 1"


async def cluster_statuses(conn: asyncpg.Connection) -> dict[str, str]:
    rows = await conn.fetch("SELECT id, slo_status FROM clusters ORDER BY id")
    return {r["id"]: r["slo_status"] for r in rows}


async def cluster_endpoints(conn: asyncpg.Connection) -> dict[str, str]:
    rows = await conn.fetch("SELECT id, api_endpoint FROM clusters ORDER BY id")
    return {r["id"]: r["api_endpoint"] for r in rows}


async def eligible_cluster_ids(conn: asyncpg.Connection, req: Constraints) -> list[str]:
    """Кластеры, допустимые под hard constraints: ответ на «предлагает ли система
    корректный кластер» из сценария 3 (ADR 0019). Та же логика, что у /clusters/eligible."""
    rows = await list_clusters(conn)
    return [r["id"] for r in rows if not rejection_reasons(candidate(r), req)]


async def insert_metrics(conn: asyncpg.Connection, cluster_id: str, m: Metrics) -> None:
    await conn.execute(
        """
        INSERT INTO cluster_metrics
            (cluster_id, observed_at, error_rate, latency_p95_ms, saturation,
             cpu_available, memory_available_gi)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT DO NOTHING
        """,
        cluster_id,
        m.observed_at,
        m.error_rate,
        m.latency_p95_ms,
        m.saturation,
        m.cpu_available,
        m.memory_available_gi,
    )


# --- decisions ----------------------------------------------------------------


def decision_out(row: asyncpg.Record) -> DecisionOut:
    return DecisionOut(**dict(row))


async def insert_decision(
    conn: asyncpg.Connection,
    *,
    workload_ref: str,
    scheduler_kind: str,
    chosen_cluster: str | None,
    candidates: list[dict[str, Any]],
    scoring: dict[str, Any] | None,
    outcome: str,
) -> asyncpg.Record:
    return await conn.fetchrow(
        """
        INSERT INTO placement_decisions
            (workload_ref, scheduler_kind, chosen_cluster, candidates, scoring, outcome)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING *
        """,
        workload_ref,
        scheduler_kind,
        chosen_cluster,
        candidates,
        scoring,
        outcome,
    )


async def update_decision_outcome(
    conn: asyncpg.Connection, decision_id: int, outcome: str
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        "UPDATE placement_decisions SET outcome = $2 WHERE id = $1 RETURNING *",
        decision_id,
        outcome,
    )


async def list_decisions(
    conn: asyncpg.Connection,
    *,
    since: datetime | None,
    until: datetime | None,
    scheduler_kind: str | None,
    workload_ref: str | None,
    limit: int,
) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT * FROM placement_decisions
        WHERE ($1::timestamptz IS NULL OR decided_at >= $1)
          AND ($2::timestamptz IS NULL OR decided_at <= $2)
          AND ($3::text IS NULL OR scheduler_kind = $3)
          AND ($4::text IS NULL OR workload_ref = $4)
        ORDER BY decided_at, id
        LIMIT $5
        """,
        since,
        until,
        scheduler_kind,
        workload_ref,
        limit,
    )


# --- violations ---------------------------------------------------------------


def violation_out(row: asyncpg.Record) -> ViolationOut:
    return ViolationOut(**dict(row))


async def insert_violation(
    conn: asyncpg.Connection,
    *,
    workload_ref: str,
    cluster_id: str | None,
    policy_name: str,
    detected_by: str,
    details: dict[str, Any] | None,
    occurred_at: datetime | None = None,
    source_key: str | None = None,
) -> asyncpg.Record | None:
    """occurred_at по умолчанию — now(); watcher передаёт время из Kyverno (ADR 0026).
    Возвращает None, если запись с таким source_key уже есть (дедупликация)."""
    return await conn.fetchrow(
        """
        INSERT INTO policy_violations
            (occurred_at, workload_ref, cluster_id, policy_name, detected_by, details, source_key)
        VALUES (COALESCE($6::timestamptz, now()), $1, $2, $3, $4, $5, $7)
        ON CONFLICT (source_key) WHERE source_key IS NOT NULL DO NOTHING
        RETURNING *
        """,
        workload_ref,
        cluster_id,
        policy_name,
        detected_by,
        details,
        occurred_at,
        source_key,
    )


async def known_source_keys(conn: asyncpg.Connection, keys: list[str]) -> set[str]:
    rows = await conn.fetch(
        "SELECT source_key FROM policy_violations WHERE source_key = ANY($1::text[])", keys
    )
    return {r["source_key"] for r in rows}


async def list_violations(
    conn: asyncpg.Connection,
    *,
    since: datetime | None,
    until: datetime | None,
    cluster_id: str | None,
    workload_ref: str | None = None,
    limit: int,
) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT * FROM policy_violations
        WHERE ($1::timestamptz IS NULL OR occurred_at >= $1)
          AND ($2::timestamptz IS NULL OR occurred_at <= $2)
          AND ($3::text IS NULL OR cluster_id = $3)
          AND ($4::text IS NULL OR workload_ref = $4)
        ORDER BY occurred_at, id
        LIMIT $5
        """,
        since,
        until,
        cluster_id,
        workload_ref,
        limit,
    )
