"""API каталога через ASGI-транспорт на живой PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app import poller
from app.rules import Metrics

from .conftest import ENDPOINTS, auth

NOW = datetime(2026, 9, 14, tzinfo=UTC)


async def test_healthz(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


# --- аутентификация (ADR 0018) --------------------------------------------------


async def test_token_required(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/clusters")).status_code == 401
    r = await client.get("/api/v1/clusters", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "role", "expected"),
    [
        ("GET", "/api/v1/clusters", "scheduler", 200),
        ("GET", "/api/v1/clusters", "dr-controller", 200),
        ("GET", "/api/v1/clusters/eligible", "scheduler", 200),
        ("POST", "/api/v1/clusters", "scheduler", 403),
        ("PATCH", "/api/v1/clusters/x/status", "scheduler", 403),
        ("POST", "/api/v1/decisions", "dr-controller", 403),
        ("GET", "/api/v1/decisions", "scheduler", 403),
        ("GET", "/api/v1/decisions", "admin", 200),
        ("POST", "/api/v1/violations", "dr-controller", 403),
        ("GET", "/api/v1/violations", "admin", 200),
        ("POST", "/api/v1/incidents", "scheduler", 403),
        ("PATCH", "/api/v1/incidents/1", "scheduler", 403),
        ("GET", "/api/v1/incidents", "scheduler", 403),
        ("GET", "/api/v1/incidents", "dr-controller", 200),
        ("GET", "/api/v1/incidents", "admin", 200),
    ],
)
async def test_roles(client: httpx.AsyncClient, method: str, path: str, role: str, expected: int):
    r = await client.request(method, path, headers=auth(role), json={})
    assert r.status_code == expected, r.text


# --- регистрация (ADR 0009, 0021) ------------------------------------------------


async def test_register_is_idempotent_upsert(client: httpx.AsyncClient, register) -> None:
    first = await client.post(
        "/api/v1/clusters",
        json={
            "id": "cluster-a",
            "region": "eu-west",
            "provider": "local",
            "certifications": ["pci-dss", "iso-27001"],
            "api_endpoint": ENDPOINTS["cluster-a"],
        },
        headers=auth("admin"),
    )
    assert first.status_code == 201
    assert first.json()["slo_status"] == "unknown"
    assert first.json()["metrics"] is None

    r = await client.patch(
        "/api/v1/clusters/cluster-a/status",
        json={"slo_status": "down"},
        headers=auth("dr-controller"),
    )
    assert r.status_code == 200
    again = await register("cluster-a", labels={"tier": "gold"})
    assert again["labels"] == {"tier": "gold"}
    assert again["slo_status"] == "down", "повторная регистрация не сбрасывает статус"


async def test_register_rejects_identity_mismatch(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/v1/clusters",
        json={
            "id": "cluster-b",
            "region": "eu-west",  # в кластере eu-central
            "provider": "local",
            "certifications": ["pci-dss"],  # в кластере только iso-27001
            "api_endpoint": ENDPOINTS["cluster-b"],
        },
        headers=auth("admin"),
    )
    assert r.status_code == 409
    mismatches = r.json()["detail"]["mismatches"]
    assert len(mismatches) == 2
    assert any(m.startswith("region:") for m in mismatches)
    assert any(m.startswith("certifications:") for m in mismatches)
    assert (await client.get("/api/v1/clusters", headers=auth("admin"))).json() == []


async def test_register_needs_kubeconfig_for_endpoint(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/v1/clusters",
        json={
            "id": "cluster-c",
            "region": "eu-west",
            "provider": "local",
            "api_endpoint": "https://cluster-c-control-plane:6443",
        },
        headers=auth("admin"),
    )
    assert r.status_code == 422
    assert "no kubeconfig" in r.json()["detail"]


async def test_get_and_list_filters(client: httpx.AsyncClient, register) -> None:
    await register("cluster-a")
    await register("cluster-b")
    r = await client.get("/api/v1/clusters/cluster-b", headers=auth("scheduler"))
    assert r.status_code == 200 and r.json()["region"] == "eu-central"
    assert (await client.get("/api/v1/clusters/nope", headers=auth("scheduler"))).status_code == 404

    async def ids(**params) -> list[str]:
        r = await client.get("/api/v1/clusters", params=params, headers=auth("scheduler"))
        assert r.status_code == 200, r.text
        return [c["id"] for c in r.json()]

    assert await ids() == ["cluster-a", "cluster-b"]
    assert await ids(region="eu-west") == ["cluster-a"]
    assert await ids(certification="pci-dss") == ["cluster-a"]
    assert await ids(certification=["iso-27001", "pci-dss"]) == ["cluster-a"]
    assert await ids(status="unknown") == ["cluster-a", "cluster-b"]
    assert await ids(status="down") == []


async def test_deregister(client: httpx.AsyncClient, register) -> None:
    await register("cluster-a")
    await register("cluster-b")
    r = await client.post(
        "/api/v1/decisions",
        json={
            "workload_ref": "workloads/w-1",
            "scheduler_kind": "naive",
            "chosen_cluster": "cluster-a",
            "candidates": [{"cluster": "cluster-a"}],
            "outcome": "placed",
        },
        headers=auth("scheduler"),
    )
    assert r.status_code == 201
    assert (
        await client.delete("/api/v1/clusters/cluster-a", headers=auth("admin"))
    ).status_code == 409
    assert (
        await client.delete("/api/v1/clusters/cluster-b", headers=auth("admin"))
    ).status_code == 204
    assert (
        await client.delete("/api/v1/clusters/cluster-b", headers=auth("admin"))
    ).status_code == 404


# --- фоновый опрос и приоритет записи статуса (ADR 0003) -------------------------


async def test_poll_writes_metrics_and_status(app, client: httpx.AsyncClient, register, source):
    await register("cluster-a")
    await register("cluster-b")
    source.metrics = {
        "cluster-a": Metrics(NOW, 0.01, 120.0, 0.4, 3.0, 8.0),
        "cluster-b": Metrics(NOW, 0.2, 90.0, 0.4, 3.0, 8.0),
        "control": Metrics(NOW, saturation=0.1),  # не зарегистрирован: игнорируется
    }
    statuses = await poller.poll_once(app.state.pool, source, app.state.settings)
    assert statuses == {"cluster-a": "healthy", "cluster-b": "degraded"}

    r = await client.get("/api/v1/clusters/cluster-a", headers=auth("scheduler"))
    m = r.json()["metrics"]
    assert m["error_rate"] == 0.01 and m["latency_p95_ms"] == 120.0 and m["cpu_available"] == 3.0
    assert m["observed_at"] != NOW.isoformat(), "observed_at — время цикла, а не источника"

    # Источник недоступен: unknown, а не down; снимок с пустыми метриками пишется.
    source.fail = True
    assert await poller.poll_once(app.state.pool, source, app.state.settings) == {
        "cluster-a": "unknown",
        "cluster-b": "unknown",
    }
    r = await client.get("/api/v1/clusters/cluster-a", headers=auth("scheduler"))
    assert r.json()["metrics"]["error_rate"] is None
    async with app.state.pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM cluster_metrics") == 4


async def test_poll_does_not_override_down(app, client: httpx.AsyncClient, register, source):
    await register("cluster-b")
    source.metrics = {"cluster-b": Metrics(NOW, 0.0, 50.0, 0.1, 3.0, 8.0)}
    assert await poller.poll_once(app.state.pool, source, app.state.settings) == {
        "cluster-b": "healthy"
    }
    r = await client.patch(
        "/api/v1/clusters/cluster-b/status",
        json={"slo_status": "down"},
        headers=auth("dr-controller"),
    )
    assert r.status_code == 200 and r.json()["slo_status"] == "down"
    assert await poller.poll_once(app.state.pool, source, app.state.settings) == {
        "cluster-b": "down"
    }
    source.fail = True
    assert await poller.poll_once(app.state.pool, source, app.state.settings) == {
        "cluster-b": "down"
    }
    # DR снимает down: следующий цикл считает статус по метрикам.
    source.fail = False
    r = await client.patch(
        "/api/v1/clusters/cluster-b/status",
        json={"slo_status": "unknown"},
        headers=auth("dr-controller"),
    )
    assert r.json()["slo_status"] == "unknown"
    assert await poller.poll_once(app.state.pool, source, app.state.settings) == {
        "cluster-b": "healthy"
    }
    assert (
        await client.patch(
            "/api/v1/clusters/x/status", json={"slo_status": "down"}, headers=auth("dr-controller")
        )
    ).status_code == 404


# --- eligible (ADR 0018) ---------------------------------------------------------


async def test_eligible(app, client: httpx.AsyncClient, register, source) -> None:
    await register("cluster-a")
    await register("cluster-b")

    async def eligible(**params) -> dict:
        r = await client.get("/api/v1/clusters/eligible", params=params, headers=auth("scheduler"))
        assert r.status_code == 200, r.text
        return r.json()

    both = await eligible()
    assert [c["id"] for c in both["eligible"]] == ["cluster-a", "cluster-b"]
    assert both["eligible"][0]["slo_status"] == "unknown" and both["rejected"] == []

    by_cert = await eligible(certification="pci-dss")
    assert [c["id"] for c in by_cert["eligible"]] == ["cluster-a"]
    assert by_cert["rejected"] == [
        {
            "id": "cluster-b",
            "slo_status": "unknown",
            "metrics": None,
            "filtered_by": "certification:pci-dss",
        }
    ]
    assert [c["id"] for c in (await eligible(region="eu-central"))["eligible"]] == ["cluster-b"]

    # Ресурсы без снимка метрик не подтверждены.
    none = await eligible(min_cpu=1)
    assert none["eligible"] == [] and {c["filtered_by"] for c in none["rejected"]} == {"cpu:1"}

    source.metrics = {
        "cluster-a": Metrics(NOW, 0.0, 100.0, 0.5, 2.0, 4.0),
        "cluster-b": Metrics(NOW, 0.5, 100.0, 0.5, 6.0, 4.0),
    }
    await poller.poll_once(app.state.pool, source, app.state.settings)
    with_metrics = await eligible(min_cpu=3)
    assert [c["id"] for c in with_metrics["eligible"]] == ["cluster-b"]
    assert with_metrics["eligible"][0]["slo_status"] == "degraded", "degraded остаётся кандидатом"
    assert with_metrics["eligible"][0]["metrics"]["error_rate"] == 0.5
    assert with_metrics["rejected"][0]["filtered_by"] == "cpu:3"

    await client.patch(
        "/api/v1/clusters/cluster-b/status",
        json={"slo_status": "down"},
        headers=auth("dr-controller"),
    )
    down = await eligible()
    assert [c["id"] for c in down["eligible"]] == ["cluster-a"]
    assert down["rejected"][0]["filtered_by"] == "down"


# --- decisions (ADR 0004) --------------------------------------------------------


async def test_decisions_flow(client: httpx.AsyncClient, register) -> None:
    await register("cluster-a")
    await register("cluster-b")
    body = {
        "workload_ref": "workloads/w-7",
        "scheduler_kind": "meta",
        "chosen_cluster": "cluster-a",
        "candidates": [
            {
                "cluster": "cluster-a",
                "score": 0.83,
                "metrics": {"error_rate": 0.01},
                "filtered_by": None,
            },
            {
                "cluster": "cluster-b",
                "score": None,
                "metrics": None,
                "filtered_by": "certification:pci-dss",
            },
        ],
        "scoring": {"weights": [0.5, 0.3, 0.2], "latency_budget_ms": 200},
        "outcome": "placed",
    }
    r = await client.post("/api/v1/decisions", json=body, headers=auth("scheduler"))
    assert r.status_code == 201, r.text
    decision = r.json()
    assert decision["id"] == 1 and decision["outcome"] == "placed"
    assert decision["candidates"][1]["filtered_by"] == "certification:pci-dss"
    assert decision["scoring"]["weights"] == [0.5, 0.3, 0.2]

    r = await client.patch(
        "/api/v1/decisions/1", json={"outcome": "rejected"}, headers=auth("scheduler")
    )
    assert r.status_code == 200 and r.json()["outcome"] == "rejected"
    assert (
        await client.patch(
            "/api/v1/decisions/99", json={"outcome": "rejected"}, headers=auth("scheduler")
        )
    ).status_code == 404

    naive = {**body, "scheduler_kind": "naive", "scoring": None, "chosen_cluster": "cluster-b"}
    assert (
        await client.post("/api/v1/decisions", json=naive, headers=auth("scheduler"))
    ).status_code == 201
    empty = {**body, "chosen_cluster": None, "outcome": "no_candidates", "candidates": []}
    assert (
        await client.post("/api/v1/decisions", json=empty, headers=auth("scheduler"))
    ).status_code == 201

    bad = {**body, "chosen_cluster": "cluster-z"}
    assert (
        await client.post("/api/v1/decisions", json=bad, headers=auth("scheduler"))
    ).status_code == 422
    inconsistent = {**body, "chosen_cluster": None}
    assert (
        await client.post("/api/v1/decisions", json=inconsistent, headers=auth("scheduler"))
    ).status_code == 422

    r = await client.get(
        "/api/v1/decisions", params={"scheduler_kind": "meta"}, headers=auth("admin")
    )
    assert [d["id"] for d in r.json()] == [1, 3]
    r = await client.get(
        "/api/v1/decisions", params={"workload_ref": "workloads/w-7"}, headers=auth("admin")
    )
    assert len(r.json()) == 3
    since = r.json()[1]["decided_at"]
    r = await client.get("/api/v1/decisions", params={"from": since}, headers=auth("admin"))
    assert [d["id"] for d in r.json()] == [2, 3]


# --- violations (ADR 0019) -------------------------------------------------------


async def test_violations_add_eligible_clusters(client: httpx.AsyncClient, register) -> None:
    await register("cluster-a")
    await register("cluster-b")
    body = {
        "workload_ref": "workloads/pci-job",
        "cluster_id": "cluster-b",
        "policy_name": "require-certification",
        "detected_by": "kyverno",
        "details": {"reported_via": "scheduler", "message": "certification pci-dss missing"},
        "requirements": {"certifications": ["pci-dss"]},
    }
    r = await client.post("/api/v1/violations", json=body, headers=auth("scheduler"))
    assert r.status_code == 201, r.text
    details = r.json()["details"]
    assert details["reported_via"] == "scheduler"
    assert details["eligible_clusters"] == ["cluster-a"]
    assert details["requirements"]["certifications"] == ["pci-dss"]

    plain = {k: v for k, v in body.items() if k not in ("details", "requirements")}
    r = await client.post("/api/v1/violations", json=plain, headers=auth("scheduler"))
    assert r.status_code == 201 and r.json()["details"] is None
    assert (
        await client.post(
            "/api/v1/violations", json={**plain, "cluster_id": "zzz"}, headers=auth("scheduler")
        )
    ).status_code == 422

    r = await client.get(
        "/api/v1/violations", params={"cluster_id": "cluster-b"}, headers=auth("admin")
    )
    assert [v["id"] for v in r.json()] == [1, 2]


# --- incidents (ADR 0028) --------------------------------------------------------


async def test_incident_lifecycle(client: httpx.AsyncClient, register) -> None:
    await register("cluster-b")
    detected = "2026-09-24T12:00:15Z"
    r = await client.post(
        "/api/v1/incidents",
        json={
            "cluster_id": "cluster-b",
            "dr_mode": "active",
            "detected_at": detected,
            "details": {"signals": {"svc": False, "api": True, "sli": "unknown"}},
        },
        headers=auth("dr-controller"),
    )
    assert r.status_code == 201, r.text
    inc = r.json()
    assert inc["id"] == 1 and inc["peak_state"] == "degraded"
    assert inc["ttd_seconds"] is None and inc["rto_seconds"] is None

    # DR: DOWN и переключение трафика; details сливается, а не заменяется.
    r = await client.patch(
        "/api/v1/incidents/1",
        json={
            "peak_state": "down",
            "failover_started_at": "2026-09-24T12:00:45Z",
            "failover_completed_at": "2026-09-24T12:00:45.300Z",
            "details": {"failovers": 1},
        },
        headers=auth("dr-controller"),
    )
    assert r.status_code == 200, r.text
    inc = r.json()
    assert inc["peak_state"] == "down"
    assert inc["details"] == {
        "signals": {"svc": False, "api": True, "sli": "unknown"},
        "failovers": 1,
    }

    # Раннер сценария (admin): время внесения отказа из Chaos Mesh (инвариант 7).
    r = await client.patch(
        "/api/v1/incidents/1",
        json={"fault_injected_at": "2026-09-24T12:00:05Z", "fault_ref": "podchaos/workloads/x"},
        headers=auth("admin"),
    )
    inc = r.json()
    assert inc["ttd_seconds"] == 10.0
    assert inc["rto_seconds"] == 40.3
    assert inc["peak_state"] == "down"  # не переданные поля не трогаются

    r = await client.get(
        "/api/v1/incidents", params={"open": "true"}, headers=auth("dr-controller")
    )
    assert [i["id"] for i in r.json()] == [1]
    await client.patch(
        "/api/v1/incidents/1",
        json={"recovery_completed_at": "2026-09-24T12:07:00Z"},
        headers=auth("dr-controller"),
    )
    r = await client.get(
        "/api/v1/incidents", params={"open": "true"}, headers=auth("dr-controller")
    )
    assert r.json() == []
    r = await client.get(
        "/api/v1/incidents",
        params={"cluster_id": "cluster-b", "from": "2026-09-24T12:00:00Z"},
        headers=auth("admin"),
    )
    assert [i["id"] for i in r.json()] == [1]

    assert (
        await client.patch("/api/v1/incidents/99", json={}, headers=auth("dr-controller"))
    ).status_code == 404
    bad = {"cluster_id": "cluster-z", "dr_mode": "active", "detected_at": detected}
    assert (
        await client.post("/api/v1/incidents", json=bad, headers=auth("dr-controller"))
    ).status_code == 422
    observe = {"cluster_id": "cluster-b", "dr_mode": "observe", "detected_at": detected}
    r = await client.post("/api/v1/incidents", json=observe, headers=auth("dr-controller"))
    assert r.status_code == 201 and r.json()["dr_mode"] == "observe"
