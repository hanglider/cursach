"""Watcher нарушений Kyverno (ADR 0019, 0026): разбор событий и отчётов, запись в audit log."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from app import watcher
from app.watcher import (
    Observed,
    StaticViolationSource,
    parse_event,
    parse_report,
    parse_requirements,
)

from .conftest import auth

T0 = datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)
CERT_MESSAGE = (
    "federation.spbu/require-certification=pci-dss not satisfied: "
    "cluster cluster-b has certifications iso-27001"
)
REGION_MESSAGE = (
    "federation.spbu/require-region=eu-west not satisfied: "
    "cluster cluster-b is in region eu-central"
)


def blocked_event(uid: str = "ev-1", **overrides) -> dict:
    """Отказ admission: regarding — политика, related — ресурс (kyverno pkg/event)."""
    ev = {
        "metadata": {"name": "require-certification.18abc", "namespace": "default", "uid": uid},
        "reason": "PolicyViolation",
        "action": "Resource Blocked",
        "reportingController": "kyverno-admission",
        "eventTime": "2026-09-15T12:00:00.250000Z",
        "regarding": {
            "apiVersion": "kyverno.io/v1",
            "kind": "ClusterPolicy",
            "name": "require-certification",
        },
        "related": {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "namespace": "workloads",
            "name": "pv-1",
        },
        "note": (
            f"Deployment workloads/pv-1: [certifications-present] fail (blocked); {CERT_MESSAGE}"
        ),
    }
    ev.update(overrides)
    return ev


def scan_event(uid: str = "ev-2") -> dict:
    """Background-скан: regarding — сам ресурс."""
    return {
        "metadata": {"name": "legacy.18abd", "namespace": "workloads", "uid": uid},
        "reason": "PolicyViolation",
        "reportingController": "kyverno-scan",
        "deprecatedLastTimestamp": "2026-09-15T12:00:05Z",
        "regarding": {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "namespace": "workloads",
            "name": "legacy",
        },
        "note": f"policy require-region-match/region-matches-cluster fail: {REGION_MESSAGE}",
    }


def report() -> dict:
    return {
        "apiVersion": "wgpolicyk8s.io/v1alpha2",
        "kind": "PolicyReport",
        "metadata": {"name": "res-uid-1", "namespace": "workloads", "uid": "rep-uid"},
        "scope": {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "name": "legacy",
            "namespace": "workloads",
            "uid": "res-uid-1",
        },
        "results": [
            {
                "policy": "require-certification",
                "rule": "certifications-present",
                "result": "pass",
                "message": "ok",
            },
            {
                "policy": "require-region-match",
                "rule": "region-matches-cluster",
                "result": "fail",
                "message": REGION_MESSAGE,
                "timestamp": {"seconds": int(T0.timestamp()) + 10, "nanos": 0},
            },
        ],
    }


# --- разбор ------------------------------------------------------------------------


def test_parse_blocked_event_uses_related_resource_and_message_contract() -> None:
    obs = parse_event(blocked_event())
    assert obs is not None
    assert obs.source == "event" and obs.key == "ev-1"
    assert obs.workload_ref == "workloads/pv-1" and obs.kind == "Deployment"
    assert obs.policy_name == "require-certification" and obs.rule == "certifications-present"
    assert obs.message == CERT_MESSAGE and obs.action == "Resource Blocked"
    assert obs.occurred_at == datetime(2026, 9, 15, 12, 0, 0, 250000, tzinfo=UTC)


def test_parse_blocked_event_without_related_falls_back_to_note() -> None:
    ev = blocked_event()
    del ev["related"]
    obs = parse_event(ev)
    assert obs is not None and obs.workload_ref == "workloads/pv-1" and obs.kind == "Deployment"


def test_parse_scan_event_on_resource() -> None:
    obs = parse_event(scan_event())
    assert obs is not None
    assert obs.workload_ref == "workloads/legacy"
    assert obs.policy_name == "require-region-match" and obs.rule == "region-matches-cluster"
    assert obs.message == REGION_MESSAGE
    assert obs.occurred_at == datetime(2026, 9, 15, 12, 0, 5, tzinfo=UTC)


def test_parse_event_ignores_other_reasons_and_kinds() -> None:
    assert parse_event(blocked_event(reason="PolicyApplied")) is None
    pod = blocked_event(related={"kind": "Pod", "namespace": "workloads", "name": "p"})
    pod["note"] = "Pod workloads/p: [r] fail (blocked); x"
    assert parse_event(pod) is None
    assert (
        parse_event({"reason": "PolicyViolation", "regarding": {"kind": "Node", "name": "n"}})
        is None
    )


def test_parse_report_takes_only_fail_results() -> None:
    out = parse_report(report())
    assert len(out) == 1
    obs = out[0]
    assert obs.source == "policyreport"
    assert obs.key == "res-uid-1/require-region-match/region-matches-cluster"
    assert obs.workload_ref == "workloads/legacy" and obs.policy_name == "require-region-match"
    assert obs.occurred_at == datetime(2026, 9, 15, 12, 0, 10, tzinfo=UTC)


def test_parse_requirements_from_message_contract() -> None:
    req = parse_requirements(CERT_MESSAGE)
    assert req is not None and req.certifications == ["pci-dss"] and req.region is None
    req = parse_requirements(REGION_MESSAGE)
    assert req is not None and req.region == "eu-west" and req.certifications == []
    both = parse_requirements(
        "federation.spbu/require-region=eu-west not satisfied: x; "
        "federation.spbu/require-certification=pci-dss,gdpr not satisfied: y"
    )
    assert (
        both is not None and both.region == "eu-west" and both.certifications == ["pci-dss", "gdpr"]
    )
    assert parse_requirements("validation failure: something else") is None


# --- запись в audit log ---------------------------------------------------------------


async def test_watch_once_records_dedups_and_survives_cluster_outage(
    app, client: httpx.AsyncClient, register
):
    await register("cluster-a")
    await register("cluster-b")
    observed = [
        parse_event(blocked_event()),
        parse_event(scan_event()),
        *parse_report(report()),
    ]
    source = StaticViolationSource({"cluster-b": observed}, fail={"cluster-a"})

    assert await watcher.watch_once(app.state.pool, source, app.state.settings) == 3
    # Повтор цикла: те же события и отчёты видны снова, но записей не прибавляется.
    assert await watcher.watch_once(app.state.pool, source, app.state.settings) == 0

    r = await client.get(
        "/api/v1/violations", params={"cluster_id": "cluster-b"}, headers=auth("admin")
    )
    rows = r.json()
    assert len(rows) == 3
    blocked = next(v for v in rows if v["workload_ref"] == "workloads/pv-1")
    assert blocked["policy_name"] == "require-certification" and blocked["detected_by"] == "kyverno"
    assert blocked["source_key"] == "event/cluster-b/ev-1"
    assert blocked["occurred_at"].startswith("2026-09-15T12:00:00.250")
    d = blocked["details"]
    assert (
        d["reported_via"] == "watcher"
        and d["source"] == "event"
        and d["action"] == "Resource Blocked"
    )
    assert d["requirements"]["certifications"] == ["pci-dss"]
    assert d["eligible_clusters"] == ["cluster-a"], "каталог предлагает корректный кластер"
    assert d["detected_at"].endswith("Z")

    legacy = [v for v in rows if v["workload_ref"] == "workloads/legacy"]
    assert {v["details"]["source"] for v in legacy} == {"event", "policyreport"}
    assert all(v["details"]["eligible_clusters"] == ["cluster-a"] for v in legacy)

    # Фильтр по workload_ref — для дымового теста стенда и раннера сценария 3.
    r = await client.get(
        "/api/v1/violations", params={"workload_ref": "workloads/pv-1"}, headers=auth("admin")
    )
    assert [v["id"] for v in r.json()] == [blocked["id"]]

    # Запись планировщика (POST) без source_key живёт рядом и не конфликтует.
    r = await client.post(
        "/api/v1/violations",
        json={
            "workload_ref": "workloads/pv-1",
            "cluster_id": "cluster-b",
            "policy_name": "require-certification",
            "detected_by": "kyverno",
        },
        headers=auth("scheduler"),
    )
    assert r.status_code == 201 and r.json()["source_key"] is None


async def test_watch_once_without_clusters_or_observations(app):
    assert (
        await watcher.watch_once(app.state.pool, StaticViolationSource(), app.state.settings) == 0
    )


def test_observed_is_hashable_dataclass() -> None:
    obs = Observed("k", "event", T0, "workloads/x", "Deployment", "p", "r", "m")
    assert obs == Observed("k", "event", T0, "workloads/x", "Deployment", "p", "r", "m")
