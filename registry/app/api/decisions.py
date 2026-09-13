"""Audit log решений о размещении (ADR 0004). Каждое решение — со всеми кандидатами."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from .. import repo
from ..auth import ADMIN, SCHEDULER, require
from ..log import event
from ..models import DecisionIn, DecisionOut, DecisionPatch, SchedulerKind

log = logging.getLogger("registry.api")
router = APIRouter(prefix="/api/v1/decisions", tags=["decisions"])

Scheduler = Annotated[str, Depends(require(SCHEDULER))]
Admin = Annotated[str, Depends(require(ADMIN))]


@router.post("", response_model=DecisionOut, status_code=status.HTTP_201_CREATED)
async def record_decision(body: DecisionIn, request: Request, _: Scheduler) -> DecisionOut:
    async with request.app.state.pool.acquire() as conn:
        try:
            row = await repo.insert_decision(
                conn,
                workload_ref=body.workload_ref,
                scheduler_kind=body.scheduler_kind,
                chosen_cluster=body.chosen_cluster,
                candidates=[c.model_dump() for c in body.candidates],
                scoring=body.scoring,
                outcome=body.outcome,
            )
        except asyncpg.ForeignKeyViolationError as e:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"chosen_cluster {body.chosen_cluster} is not registered",
            ) from e
    event(
        log,
        "decision_recorded",
        id=row["id"],
        workload=body.workload_ref,
        scheduler=body.scheduler_kind,
        chosen=body.chosen_cluster,
        outcome=body.outcome,
    )
    return repo.decision_out(row)


@router.patch("/{decision_id}", response_model=DecisionOut)
async def patch_decision(
    decision_id: int, body: DecisionPatch, request: Request, _: Scheduler
) -> DecisionOut:
    """Исход известен после admission (ADR 0025): planned → rejected при отказе Kyverno."""
    async with request.app.state.pool.acquire() as conn:
        row = await repo.update_decision_outcome(conn, decision_id, body.outcome)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"decision {decision_id} not found")
    event(log, "decision_updated", id=decision_id, outcome=body.outcome)
    return repo.decision_out(row)


@router.get("", response_model=list[DecisionOut])
async def list_decisions(
    request: Request,
    _: Admin,
    since: Annotated[datetime | None, Query(alias="from")] = None,
    until: Annotated[datetime | None, Query(alias="to")] = None,
    scheduler_kind: SchedulerKind | None = None,
    workload_ref: str | None = None,
    limit: Annotated[int, Query(ge=1, le=10000)] = 1000,
) -> list[DecisionOut]:
    async with request.app.state.pool.acquire() as conn:
        rows = await repo.list_decisions(
            conn,
            since=since,
            until=until,
            scheduler_kind=scheduler_kind,
            workload_ref=workload_ref,
            limit=limit,
        )
    return [repo.decision_out(r) for r in rows]
