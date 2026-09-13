"""Audit log нарушений политик (ADR 0019). Каталог дописывает eligible_clusters."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from .. import repo
from ..auth import ADMIN, SCHEDULER, require
from ..log import event
from ..models import Requirements, ViolationIn, ViolationOut
from ..rules import Constraints, rejection_reasons

log = logging.getLogger("registry.api")
router = APIRouter(prefix="/api/v1/violations", tags=["violations"])

Scheduler = Annotated[str, Depends(require(SCHEDULER))]
Admin = Annotated[str, Depends(require(ADMIN))]


async def eligible_for(conn: asyncpg.Connection, req: Requirements) -> list[str]:
    """Допустимые кластеры для требований отклонённого workload'а: ответ на
    «предлагает ли система корректный кластер» из сценария 3."""
    constraints = Constraints(
        region=req.region or None,
        certifications=tuple(req.certifications),
        min_cpu=req.min_cpu,
        min_memory_gi=req.min_memory_gi,
    )
    rows = await repo.list_clusters(conn)
    return [r["id"] for r in rows if not rejection_reasons(repo.candidate(r), constraints)]


@router.post("", response_model=ViolationOut, status_code=status.HTTP_201_CREATED)
async def record_violation(body: ViolationIn, request: Request, _: Scheduler) -> ViolationOut:
    details = dict(body.details or {})
    async with request.app.state.pool.acquire() as conn:
        if body.requirements is not None:
            details["requirements"] = body.requirements.model_dump()
            details["eligible_clusters"] = await eligible_for(conn, body.requirements)
        try:
            row = await repo.insert_violation(
                conn,
                workload_ref=body.workload_ref,
                cluster_id=body.cluster_id,
                policy_name=body.policy_name,
                detected_by=body.detected_by,
                details=details or None,
            )
        except asyncpg.ForeignKeyViolationError as e:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"cluster {body.cluster_id} is not registered",
            ) from e
    event(
        log,
        "violation_recorded",
        id=row["id"],
        workload=body.workload_ref,
        cluster=body.cluster_id,
        policy=body.policy_name,
        detected_by=body.detected_by,
    )
    return repo.violation_out(row)


@router.get("", response_model=list[ViolationOut])
async def list_violations(
    request: Request,
    _: Admin,
    since: Annotated[datetime | None, Query(alias="from")] = None,
    until: Annotated[datetime | None, Query(alias="to")] = None,
    cluster_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=10000)] = 1000,
) -> list[ViolationOut]:
    async with request.app.state.pool.acquire() as conn:
        rows = await repo.list_violations(
            conn, since=since, until=until, cluster_id=cluster_id, limit=limit
        )
    return [repo.violation_out(r) for r in rows]
