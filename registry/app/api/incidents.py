"""Инциденты DR Controller'а (architecture §6.4, ADR 0028): фазы DR-цикла одной строкой.

DR Controller открывает инцидент на переходе кластера в DEGRADED и дописывает времена
failover и recovery; раннер сценария (admin) дописывает время внесения отказа из
Chaos Mesh (инвариант 7). TTD и RTO в ответе производные.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from .. import repo
from ..auth import DR_CONTROLLER, require
from ..log import event
from ..models import IncidentIn, IncidentOut, IncidentPatch

log = logging.getLogger("registry.api")
router = APIRouter(prefix="/api/v1/incidents", tags=["incidents"])

DR = Annotated[str, Depends(require(DR_CONTROLLER))]


@router.post("", response_model=IncidentOut, status_code=status.HTTP_201_CREATED)
async def open_incident(body: IncidentIn, request: Request, _: DR) -> IncidentOut:
    async with request.app.state.pool.acquire() as conn:
        try:
            row = await repo.insert_incident(conn, **body.model_dump())
        except asyncpg.ForeignKeyViolationError as e:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"cluster {body.cluster_id} is not registered",
            ) from e
    event(
        log,
        "incident_opened",
        id=row["id"],
        cluster=body.cluster_id,
        mode=body.dr_mode,
        detected_at=body.detected_at.isoformat(),
    )
    return repo.incident_out(row)


@router.patch("/{incident_id}", response_model=IncidentOut)
async def patch_incident(
    incident_id: int, body: IncidentPatch, request: Request, role: DR
) -> IncidentOut:
    fields = body.model_dump(exclude_unset=True, exclude={"details"})
    async with request.app.state.pool.acquire() as conn:
        row = await repo.update_incident(conn, incident_id, fields, body.details)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"incident {incident_id} not found")
    if fields or body.details:
        event(log, "incident_updated", id=incident_id, fields=sorted(fields), by=role)
    return repo.incident_out(row)


@router.get("", response_model=list[IncidentOut])
async def list_incidents(
    request: Request,
    _: DR,
    since: Annotated[datetime | None, Query(alias="from")] = None,
    until: Annotated[datetime | None, Query(alias="to")] = None,
    cluster_id: str | None = None,
    open_only: Annotated[bool, Query(alias="open")] = False,
    limit: Annotated[int, Query(ge=1, le=10000)] = 1000,
) -> list[IncidentOut]:
    async with request.app.state.pool.acquire() as conn:
        rows = await repo.list_incidents(
            conn,
            since=since,
            until=until,
            cluster_id=cluster_id,
            open_only=open_only,
            limit=limit,
        )
    return [repo.incident_out(r) for r in rows]
