"""Разбор ответа Thanos в снимки метрик."""

from datetime import UTC, datetime

from app import thanos

NOW = datetime(2026, 9, 14, tzinfo=UTC)


def series(name: str, cluster: str | None, value: str) -> dict:
    labels = {"__name__": name}
    if cluster:
        labels["cluster"] = cluster
    return {"metric": labels, "value": [1757800000, value]}


def test_parse_builds_snapshot_per_cluster() -> None:
    result = [
        series(thanos.ERROR_RATE, "cluster-a", "0.02"),
        series(thanos.LATENCY_P95, "cluster-a", "0.150"),
        series(thanos.SATURATION, "cluster-a", "0.4"),
        series(thanos.CPU_SATURATION, "cluster-a", "0.25"),
        series(thanos.CPU_ALLOCATABLE, "cluster-a", "8"),
        series(thanos.MEMORY_SATURATION, "cluster-a", "0.5"),
        series(thanos.MEMORY_ALLOCATABLE, "cluster-a", str(16 * 2**30)),
        # cluster-b без трафика: ratio = NaN, p95 отсутствует
        series(thanos.ERROR_RATE, "cluster-b", "NaN"),
        series(thanos.SATURATION, "cluster-b", "0.1"),
        # ряд без external label cluster игнорируется
        series(thanos.SATURATION, None, "0.9"),
    ]
    out = thanos.parse(result, NOW)
    assert set(out) == {"cluster-a", "cluster-b"}
    a = out["cluster-a"]
    assert a.observed_at == NOW
    assert a.error_rate == 0.02
    assert a.latency_p95_ms == 150.0
    assert a.saturation == 0.4
    assert a.cpu_available == 6.0
    assert a.memory_available_gi == 8.0
    b = out["cluster-b"]
    assert b.error_rate is None
    assert b.latency_p95_ms is None
    assert b.saturation == 0.1
    assert b.cpu_available is None


def test_query_covers_all_rules() -> None:
    for name in (thanos.ERROR_RATE, thanos.LATENCY_P95, thanos.SATURATION, thanos.CPU_ALLOCATABLE):
        assert name in thanos.QUERY
