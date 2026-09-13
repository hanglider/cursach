"""Схемы запросов и ответов API (architecture §3.3, ADR 0004, 0018)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SloStatus = Literal["healthy", "degraded", "down", "unknown"]
SchedulerKind = Literal["meta", "naive"]
Outcome = Literal["placed", "rejected", "no_candidates"]
DetectedBy = Literal["kyverno", "meta-scheduler"]

_ID_PATTERN = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"


class MetricsOut(BaseModel):
    observed_at: datetime
    error_rate: float | None = None
    latency_p95_ms: float | None = None
    saturation: float | None = None
    cpu_available: float | None = None
    memory_available_gi: float | None = None


class ClusterIn(BaseModel):
    id: str = Field(pattern=_ID_PATTERN, max_length=63)
    region: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    api_endpoint: str = Field(pattern=r"^https?://")
    labels: dict[str, str] = Field(default_factory=dict)
    certifications: list[str] = Field(default_factory=list)


class ClusterOut(BaseModel):
    id: str
    region: str
    provider: str
    api_endpoint: str
    labels: dict[str, str]
    certifications: list[str]
    slo_status: SloStatus
    registered_at: datetime
    metrics: MetricsOut | None = None


class StatusPatch(BaseModel):
    slo_status: SloStatus


class EligibleEntry(BaseModel):
    id: str
    slo_status: SloStatus
    metrics: MetricsOut | None = None


class RejectedEntry(EligibleEntry):
    filtered_by: str


class EligibleOut(BaseModel):
    eligible: list[EligibleEntry]
    rejected: list[RejectedEntry]


class CandidateIn(BaseModel):
    """Кандидат в записи решения (ADR 0004). Лишние поля сохраняются как есть."""

    model_config = ConfigDict(extra="allow")

    cluster: str
    score: float | None = None
    metrics: dict[str, Any] | None = None
    filtered_by: str | None = None


class DecisionIn(BaseModel):
    workload_ref: str = Field(min_length=1)
    scheduler_kind: SchedulerKind
    chosen_cluster: str | None = None
    candidates: list[CandidateIn]
    scoring: dict[str, Any] | None = None
    outcome: Outcome

    @model_validator(mode="after")
    def _chosen_matches_outcome(self) -> DecisionIn:
        if self.outcome == "placed" and not self.chosen_cluster:
            raise ValueError("outcome=placed requires chosen_cluster")
        if self.outcome == "no_candidates" and self.chosen_cluster:
            raise ValueError("outcome=no_candidates cannot have chosen_cluster")
        return self


class DecisionPatch(BaseModel):
    outcome: Outcome


class DecisionOut(BaseModel):
    id: int
    decided_at: datetime
    workload_ref: str
    scheduler_kind: SchedulerKind
    chosen_cluster: str | None
    candidates: list[dict[str, Any]]
    scoring: dict[str, Any] | None
    outcome: Outcome


class Requirements(BaseModel):
    """Hard constraints отклонённого workload'а: по ним каталог считает eligible_clusters."""

    region: str | None = None
    certifications: list[str] = Field(default_factory=list)
    min_cpu: float | None = None
    min_memory_gi: float | None = None


class ViolationIn(BaseModel):
    workload_ref: str = Field(min_length=1)
    cluster_id: str | None = None
    policy_name: str = Field(min_length=1)
    detected_by: DetectedBy
    details: dict[str, Any] | None = None
    requirements: Requirements | None = None


class ViolationOut(BaseModel):
    id: int
    occurred_at: datetime
    workload_ref: str
    cluster_id: str | None
    policy_name: str
    detected_by: DetectedBy
    details: dict[str, Any] | None
