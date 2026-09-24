"""Тесты сводки сценария 2: из неё берётся приёмочный показатель (ADR 0031)."""

import csv
import json

import pytest
import summary

K6_COLUMNS = [
    "metric_name",
    "timestamp",
    "metric_value",
    "check",
    "error",
    "error_code",
    "expected_response",
    "group",
    "method",
    "name",
    "proto",
    "scenario",
    "service",
    "status",
    "subproto",
    "tls_version",
    "url",
    "extra_tags",
    "metadata",
]


def k6_row(metric, ts, value, scenario, tags, status, ok=True):
    row = dict.fromkeys(K6_COLUMNS, "")
    row.update(
        metric_name=metric,
        timestamp=str(ts),
        metric_value=str(value),
        scenario=scenario,
        extra_tags=tags,
        status=str(status),
        expected_response="true" if ok else "false",
        url="http://127.0.0.1:8080/",
    )
    return row


def test_read_requests_pairs_duration_and_drops_live_list(tmp_path):
    wl = "kind=workload&target_cluster=cluster-b"
    rows = [
        k6_row("http_reqs", 1000, 1, "steady", "kind=live-list", 200),
        k6_row("http_req_duration", 1000, 1.5, "steady", "kind=live-list", 200),
        k6_row("http_reqs", 1000, 1, "steady", wl, 502, ok=False),
        k6_row("http_req_duration", 1000, 1001.0, "steady", wl, 502),
        k6_row("http_reqs", 1000, 1, "background", "kind=background", 200),
        k6_row("http_req_duration", 1000, 3.0, "background", "kind=background", 200),
    ]
    path = tmp_path / "k6.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=K6_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    reqs = summary.read_requests(path)
    assert [(r["kind"], r["cluster"], r["failed"], r["duration_ms"]) for r in reqs] == [
        ("workload", "cluster-b", 1, 1001.0),
        ("background", "", 0, 3.0),
    ]


def test_increase_handles_existing_born_inside_and_reset():
    existing = [[0, "10"], [5, "15"], [10, "20"], [15, "25"]]
    assert summary.increase(existing, 5, 15) == 10  # 15 → 25
    born = [[10, "4"], [15, "9"]]
    assert summary.increase(born, 5, 15) == 9  # под родился в окне: от нуля
    reset = [[0, "50"], [5, "60"], [10, "3"], [15, "8"]]
    assert summary.increase(reset, 0, 15) == 10 + 3 + 5


def test_score_matches_adr_0005():
    healthy = {"error_rate": 0.0, "latency_p95_ms": 5.0, "saturation": 0.2}
    idle = {"error_rate": None, "latency_p95_ms": None, "saturation": 0.2}
    slow = {"error_rate": 0.1, "latency_p95_ms": 900.0, "saturation": 0.2}
    w, b = summary.WEIGHTS, summary.LATENCY_BUDGET_MS
    assert summary.score(healthy, w, b) == pytest.approx(0.5 + 0.3 * 0.975 + 0.2 * 0.8)
    assert summary.score(idle, w, b) == pytest.approx(0.4 + 0.16)
    # ADR 0030: деградировавший по латентности выигрывает у пустого при e < 0.2.
    assert summary.score(slow, w, b) > summary.score(idle, w, b)
    assert summary.degraded(slow) and not summary.degraded(healthy) and not summary.degraded(idle)


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def decision(ts, chosen, scores, kind="meta", workload="w-1"):
    cands = [{"cluster": c, "score": s, "filtered_by": None} for c, s in scores.items()]
    return {
        "decided_at": ts,
        "workload_ref": f"workloads/{workload}",
        "scheduler_kind": kind,
        "chosen_cluster": chosen,
        "candidates": json.dumps(cands),
        "scoring": json.dumps({"weights": [0.5, 0.3, 0.2], "latency_budget_ms": 200}),
        "outcome": "placed",
    }


def snapshot(ts, cluster, e, p95, sat=0.2):
    return {
        "cluster_id": cluster,
        "observed_at": ts,
        "error_rate": "" if e is None else e,
        "latency_p95_ms": "" if p95 is None else p95,
        "saturation": sat,
    }


def test_placement_rows_reaction(tmp_path):
    t = "2026-09-25 10:00:{:02d}+00".format
    inj, rec = summary.ms(t(10)), summary.ms(t(50))
    bounds = {
        "baseline": (summary.ms(t(0)), inj),
        "fault": (inj, rec),
        "recovery": (rec, rec + 60_000),
    }
    write_csv(
        tmp_path / "registry" / "placement_decisions.csv",
        [
            decision(t(5), "cluster-b", {"cluster-a": 0.94, "cluster-b": 0.95}),
            decision(t(15), "cluster-b", {"cluster-a": 0.94, "cluster-b": 0.95}, workload="w-2"),
            decision(t(35), "cluster-a", {"cluster-a": 0.94, "cluster-b": 0.60}, workload="w-3"),
            decision(t(55), "cluster-a", {"cluster-a": 0.94, "cluster-b": 0.60}, workload="w-4"),
        ],
    )
    snaps = []
    # До отказа b выигрывает; на 12 с проигрывает по шуму saturation и снова выигрывает;
    # с 30 с деградация видна и держится до конца отказа.
    for sec, b in (
        (0, (0, 5, 0.19)),
        (12, (0, 5, 0.25)),
        (20, (0, 5, 0.19)),
        (30, (0.1, 900, 0.2)),
        (40, (0.1, 900, 0.2)),
        (55, (0, 5, 0.2)),
    ):
        snaps.append(snapshot(t(sec), "cluster-a", 0, 5, 0.2))
        snaps.append(snapshot(t(sec), "cluster-b", *b))
    write_csv(tmp_path / "registry" / "cluster_metrics.csv", snaps)

    out, _ = summary.placement_rows(tmp_path, bounds, "cluster-b", inj, rec)
    assert out["fault"] == {"placements": 2, "to_degraded": 1, "share_to_degraded": 0.5}
    assert out["degraded_s"] == 20.0
    assert out["score_loss_s"] == 20.0  # проигрыш на 12 с не устойчивый
    assert out["first_avoid_s"] == 25.0
    assert out["catalog_recovered_s"] == 5.0
    assert out["first_return_s"] is None
