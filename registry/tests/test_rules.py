"""Правила каталога: по unit-тесту на каждое правило (ADR 0018, 0003)."""

from datetime import UTC, datetime

import pytest

from app.rules import (
    Candidate,
    Constraints,
    Metrics,
    compute_status,
    next_status,
    rejection_reasons,
)

NOW = datetime(2026, 9, 14, tzinfo=UTC)


def cand(**kw) -> Candidate:
    base = {
        "id": "cluster-a",
        "region": "eu-west",
        "certifications": ("pci-dss", "iso-27001"),
        "slo_status": "healthy",
        "metrics": Metrics(NOW, 0.01, 120.0, 0.4, 3.0, 8.0),
    }
    return Candidate(**{**base, **kw})


def test_no_constraints_accepts_everything() -> None:
    assert rejection_reasons(cand(), Constraints()) == []


def test_down_is_always_rejected() -> None:
    assert rejection_reasons(cand(slo_status="down"), Constraints()) == ["down"]


@pytest.mark.parametrize("status", ["unknown", "degraded"])
def test_unknown_and_degraded_stay_eligible(status: str) -> None:
    """Инвариант 6: отсутствие метрик — не основание для исключения."""
    assert rejection_reasons(cand(slo_status=status, metrics=None), Constraints()) == []


def test_region_mismatch() -> None:
    assert rejection_reasons(cand(), Constraints(region="eu-central")) == ["region:eu-central"]
    assert rejection_reasons(cand(), Constraints(region="eu-west")) == []


def test_all_certifications_required() -> None:
    req = Constraints(certifications=("pci-dss", "gdpr"))
    assert rejection_reasons(cand(), req) == ["certification:gdpr"]
    assert rejection_reasons(cand(certifications=("iso-27001",)), req) == [
        "certification:pci-dss",
        "certification:gdpr",
    ]


def test_resources_by_last_snapshot() -> None:
    assert rejection_reasons(cand(), Constraints(min_cpu=2, min_memory_gi=4)) == []
    assert rejection_reasons(cand(), Constraints(min_cpu=4)) == ["cpu:4"]
    assert rejection_reasons(cand(), Constraints(min_memory_gi=16.5)) == ["memory_gi:16.5"]


def test_unknown_capacity_does_not_satisfy_resource_constraint() -> None:
    """Fail-closed: hard constraint без данных не подтверждён."""
    assert rejection_reasons(cand(metrics=None), Constraints(min_cpu=1)) == ["cpu:1"]
    partial = Metrics(NOW, error_rate=0.0)
    assert rejection_reasons(cand(metrics=partial), Constraints(min_memory_gi=1)) == ["memory_gi:1"]


def test_reasons_accumulate_in_fixed_order() -> None:
    req = Constraints(region="eu-central", certifications=("gdpr",), min_cpu=100)
    assert rejection_reasons(cand(slo_status="down"), req) == [
        "down",
        "region:eu-central",
        "certification:gdpr",
        "cpu:100",
    ]


THRESHOLDS = {"error_rate_threshold": 0.05, "latency_budget_ms": 200.0}


def test_status_unknown_without_metrics() -> None:
    assert compute_status(None, **THRESHOLDS) == "unknown"
    assert compute_status(Metrics(NOW), **THRESHOLDS) == "unknown"


def test_status_healthy_with_partial_metrics() -> None:
    """Нет трафика (error rate и p95 отсутствуют), но saturation есть: кластер жив."""
    assert compute_status(Metrics(NOW, saturation=0.3), **THRESHOLDS) == "healthy"


def test_status_degraded_by_error_rate_or_latency() -> None:
    assert compute_status(Metrics(NOW, error_rate=0.051), **THRESHOLDS) == "degraded"
    assert compute_status(Metrics(NOW, error_rate=0.05), **THRESHOLDS) == "healthy"
    assert compute_status(Metrics(NOW, latency_p95_ms=250), **THRESHOLDS) == "degraded"
    assert compute_status(Metrics(NOW, latency_p95_ms=200), **THRESHOLDS) == "healthy"


def test_poller_never_overwrites_down() -> None:
    """ADR 0003: down ставит и снимает только DR Controller."""
    assert next_status("down", "healthy") == "down"
    assert next_status("down", "unknown") == "down"
    assert next_status("healthy", "degraded") == "degraded"
    assert next_status("degraded", "unknown") == "unknown"
    assert next_status("unknown", "healthy") == "healthy"
