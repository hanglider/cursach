"""Кластеры: регистрация, список, карточка, статус, кандидаты под hard constraints."""

from __future__ import annotations

import logging
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from .. import repo
from ..auth import ADMIN, DR_CONTROLLER, SCHEDULER, require
from ..identity import IdentityError, mismatches
from ..log import event
from ..models import (
    ClusterIn,
    ClusterOut,
    EligibleEntry,
    EligibleOut,
    RejectedEntry,
    SloStatus,
    StatusPatch,
)
from ..rules import Constraints, rejection_reasons

log = logging.getLogger("registry.api")
router = APIRouter(prefix="/api/v1/clusters", tags=["clusters"])

Readers = Annotated[str, Depends(require(SCHEDULER, DR_CONTROLLER))]
Admin = Annotated[str, Depends(require(ADMIN))]


def _pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


@router.post("", response_model=ClusterOut, status_code=status.HTTP_201_CREATED)
async def register_cluster(
    body: ClusterIn, request: Request, response: Response, _: Admin
) -> ClusterOut:
    reader = request.app.state.identity
    if reader is not None:
        try:
            identity = await reader.read(body.api_endpoint)
        except IdentityError as e:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"identity check failed: {e}"
            ) from e
        diff = mismatches(
            identity,
            cluster_id=body.id,
            region=body.region,
            provider=body.provider,
            certifications=body.certifications,
        )
        if diff:
            raise HTTPException(
                status.HTTP_409_CONFLICT, {"message": "identity mismatch", "mismatches": diff}
            )
    async with _pool(request).acquire() as conn:
        inserted = await repo.upsert_cluster(
            conn,
            id=body.id,
            region=body.region,
            provider=body.provider,
            api_endpoint=body.api_endpoint,
            labels=body.labels,
            certifications=body.certifications,
        )
        row = await repo.get_cluster(conn, body.id)
    response.status_code = status.HTTP_201_CREATED if inserted else status.HTTP_200_OK
    event(log, "cluster_registered", cluster=body.id, created=inserted)
    return repo.cluster_out(row)


@router.get("", response_model=list[ClusterOut])
async def list_clusters(
    request: Request,
    _: Readers,
    status_: Annotated[SloStatus | None, Query(alias="status")] = None,
    region: str | None = None,
    certification: Annotated[list[str], Query()] = [],  # noqa: B006 — FastAPI: default списка
) -> list[ClusterOut]:
    async with _pool(request).acquire() as conn:
        rows = await repo.list_clusters(
            conn, status=status_, region=region, certifications=certification
        )
    return [repo.cluster_out(r) for r in rows]


@router.get("/eligible", response_model=EligibleOut)
async def eligible_clusters(
    request: Request,
    _: Readers,
    region: str | None = None,
    certification: Annotated[list[str], Query()] = [],  # noqa: B006
    min_cpu: Annotated[float | None, Query(ge=0)] = None,
    min_memory_gi: Annotated[float | None, Query(ge=0)] = None,
) -> EligibleOut:
    """Кандидаты под hard constraints (ADR 0018). Оба планировщика ходят сюда (инвариант 3)."""
    req = Constraints(
        region=region or None,
        certifications=tuple(certification),
        min_cpu=min_cpu,
        min_memory_gi=min_memory_gi,
    )
    async with _pool(request).acquire() as conn:
        rows = await repo.list_clusters(conn)
    eligible: list[EligibleEntry] = []
    rejected: list[RejectedEntry] = []
    for row in rows:
        c = repo.candidate(row)
        reasons = rejection_reasons(c, req)
        metrics = repo.metrics_out(c.metrics)
        if reasons:
            rejected.append(
                RejectedEntry(
                    id=c.id, slo_status=c.slo_status, metrics=metrics, filtered_by=",".join(reasons)
                )
            )
        else:
            eligible.append(EligibleEntry(id=c.id, slo_status=c.slo_status, metrics=metrics))
    return EligibleOut(eligible=eligible, rejected=rejected)


@router.get("/{cluster_id}", response_model=ClusterOut)
async def get_cluster(cluster_id: str, request: Request, _: Readers) -> ClusterOut:
    async with _pool(request).acquire() as conn:
        row = await repo.get_cluster(conn, cluster_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"cluster {cluster_id} is not registered")
    return repo.cluster_out(row)


@router.patch("/{cluster_id}/status", response_model=ClusterOut)
async def patch_status(
    cluster_id: str,
    body: StatusPatch,
    request: Request,
    role: Annotated[str, Depends(require(DR_CONTROLLER))],
) -> ClusterOut:
    """DR Controller ставит и снимает down (ADR 0003); запись безусловная."""
    async with _pool(request).acquire() as conn:
        previous = await repo.set_status(conn, cluster_id, body.slo_status)
        if previous is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"cluster {cluster_id} is not registered"
            )
        row = await repo.get_cluster(conn, cluster_id)
    if previous != body.slo_status:
        event(
            log,
            "status_changed",
            cluster=cluster_id,
            previous=previous,
            status=body.slo_status,
            by=role,
        )
    return repo.cluster_out(row)


@router.delete("/{cluster_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deregister_cluster(cluster_id: str, request: Request, _: Admin) -> None:
    async with _pool(request).acquire() as conn:
        try:
            deleted = await repo.delete_cluster(conn, cluster_id)
        except asyncpg.ForeignKeyViolationError as e:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"cluster {cluster_id} is referenced by decisions or violations",
            ) from e
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"cluster {cluster_id} is not registered")
    event(log, "cluster_deregistered", cluster=cluster_id)
