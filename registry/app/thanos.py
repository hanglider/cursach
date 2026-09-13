"""Источник метрик: Thanos Querier, один instant-запрос на все recording rules (ADR 0024)."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Protocol

import httpx

from .rules import Metrics

# Per-cluster recording rules (stand/observability/federation/prometheus-rules.yaml)
# и константы ёмкости (stand/scripts/render-capacity-rules.sh).
ERROR_RATE = "cluster:http_error_rate:ratio5m"
LATENCY_P95 = "cluster:http_request_duration_seconds:p95_5m"
SATURATION = "cluster:saturation:ratio"
CPU_SATURATION = "cluster:cpu_saturation:ratio"
MEMORY_SATURATION = "cluster:memory_saturation:ratio"
CPU_ALLOCATABLE = "node:cpu_allocatable:cores"
MEMORY_ALLOCATABLE = "node:memory_allocatable:bytes"
_NAMES = (
    ERROR_RATE,
    LATENCY_P95,
    SATURATION,
    CPU_SATURATION,
    MEMORY_SATURATION,
    CPU_ALLOCATABLE,
    MEMORY_ALLOCATABLE,
)
QUERY = '{__name__=~"' + "|".join(_NAMES) + '"}'


class MetricsSource(Protocol):
    async def fetch(self) -> dict[str, Metrics]:
        """Метрики по кластерам (ключ — external label `cluster`).

        Исключение, если источник недоступен.
        """
        ...


class ThanosError(RuntimeError):
    pass


def _float(raw: str) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) or math.isinf(value) else value


def parse(result: Iterable[dict], observed_at: datetime) -> dict[str, Metrics]:
    """Разобрать `data.result` instant-запроса в снимки по кластерам."""
    values: dict[str, dict[str, float]] = defaultdict(dict)
    for series in result:
        labels = series.get("metric", {})
        name, cluster = labels.get("__name__"), labels.get("cluster")
        if not name or not cluster:
            continue
        value = _float(series.get("value", [None, None])[1])
        if value is not None:
            values[cluster][name] = value

    out: dict[str, Metrics] = {}
    for cluster, v in values.items():
        p95 = v.get(LATENCY_P95)
        cpu_cores, cpu_sat = v.get(CPU_ALLOCATABLE), v.get(CPU_SATURATION)
        mem_bytes, mem_sat = v.get(MEMORY_ALLOCATABLE), v.get(MEMORY_SATURATION)
        out[cluster] = Metrics(
            observed_at=observed_at,
            error_rate=v.get(ERROR_RATE),
            latency_p95_ms=p95 * 1000 if p95 is not None else None,
            saturation=v.get(SATURATION),
            cpu_available=(
                cpu_cores * (1 - cpu_sat) if cpu_cores is not None and cpu_sat is not None else None
            ),
            memory_available_gi=(
                mem_bytes * (1 - mem_sat) / 2**30
                if mem_bytes is not None and mem_sat is not None
                else None
            ),
        )
    return out


class ThanosSource:
    def __init__(self, url: str, client: httpx.AsyncClient, timeout_s: float = 10.0):
        self._url = url.rstrip("/")
        self._client = client
        self._timeout = timeout_s

    async def fetch(self) -> dict[str, Metrics]:
        # partial_response: недоступный store одного кластера не должен ронять
        # запрос целиком — этот кластер просто останется без метрик (unknown).
        try:
            resp = await self._client.get(
                f"{self._url}/api/v1/query",
                params={"query": QUERY, "partial_response": "true", "dedup": "true"},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ThanosError(str(e)) from e
        if body.get("status") != "success":
            raise ThanosError(f"{body.get('errorType')}: {body.get('error')}")
        return parse(body["data"]["result"], datetime.now(UTC))


class StaticSource:
    """Источник для тестов: отдаёт заданные снимки или бросает исключение."""

    def __init__(self, metrics: dict[str, Metrics] | None = None, fail: bool = False):
        self.metrics = metrics or {}
        self.fail = fail

    async def fetch(self) -> dict[str, Metrics]:
        if self.fail:
            raise ThanosError("static source unavailable")
        return dict(self.metrics)
