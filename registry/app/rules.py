"""Правила каталога как чистые функции: hard constraints (ADR 0018), статус по метрикам
и приоритет записи slo_status (ADR 0003). Без БД и HTTP, чтобы каждое правило имело unit-тест."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

STATUSES = ("healthy", "degraded", "down", "unknown")


@dataclass(frozen=True)
class Metrics:
    """Снимок метрик кластера; None — величина не получена."""

    observed_at: datetime
    error_rate: float | None = None
    latency_p95_ms: float | None = None
    saturation: float | None = None
    cpu_available: float | None = None
    memory_available_gi: float | None = None

    @property
    def empty(self) -> bool:
        return all(
            v is None
            for v in (
                self.error_rate,
                self.latency_p95_ms,
                self.saturation,
                self.cpu_available,
                self.memory_available_gi,
            )
        )


@dataclass(frozen=True)
class Constraints:
    """Hard constraints workload'а из аннотаций federation.spbu/require-* (ADR 0009)."""

    region: str | None = None
    certifications: tuple[str, ...] = ()
    min_cpu: float | None = None
    min_memory_gi: float | None = None


@dataclass(frozen=True)
class Candidate:
    id: str
    region: str
    certifications: tuple[str, ...]
    slo_status: str
    metrics: Metrics | None


def rejection_reasons(candidate: Candidate, req: Constraints) -> list[str]:
    """Причины отсева кандидата; пустой список — кандидат допустим.

    `down` отсеивается всегда, `unknown` и `degraded` остаются (инвариант 6).
    Ресурсы проверяются по последнему снимку; неизвестная ёмкость не подтверждает
    hard constraint и отсеивает кандидата (fail-closed, ADR 0025).
    """
    reasons: list[str] = []
    if candidate.slo_status == "down":
        reasons.append("down")
    if req.region and candidate.region != req.region:
        reasons.append(f"region:{req.region}")
    for cert in req.certifications:
        if cert not in candidate.certifications:
            reasons.append(f"certification:{cert}")
    m = candidate.metrics
    if req.min_cpu is not None:
        cpu = m.cpu_available if m else None
        if cpu is None or cpu < req.min_cpu:
            reasons.append(f"cpu:{req.min_cpu:g}")
    if req.min_memory_gi is not None:
        mem = m.memory_available_gi if m else None
        if mem is None or mem < req.min_memory_gi:
            reasons.append(f"memory_gi:{req.min_memory_gi:g}")
    return reasons


def compute_status(
    metrics: Metrics | None, *, error_rate_threshold: float, latency_budget_ms: float
) -> str:
    """Статус по метрикам: unknown без единой величины, degraded при превышении порогов."""
    if metrics is None or metrics.empty:
        return "unknown"
    if metrics.error_rate is not None and metrics.error_rate > error_rate_threshold:
        return "degraded"
    if metrics.latency_p95_ms is not None and metrics.latency_p95_ms > latency_budget_ms:
        return "degraded"
    return "healthy"


def next_status(current: str, computed: str) -> str:
    """Приоритет записи (ADR 0003): фоновый опрос не затирает `down` DR-контроллера."""
    return "down" if current == "down" else computed
