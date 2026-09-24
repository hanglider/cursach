#!/usr/bin/env python3
"""Генерирует federation-slo.json. Только stdlib: python3 build.py.

JSON в репозитории — артефакт; править этот файл, а не JSON.
"""

import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "thanos"}


def target(expr, legend):
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": "A"}


def targets(*pairs):
    out = []
    for i, (expr, legend) in enumerate(pairs):
        out.append({**target(expr, legend), "refId": chr(ord("A") + i)})
    return out


def stat(title, expr, unit, thresholds, x, y, w=6, h=4, decimals=2):
    return {
        "type": "stat",
        "title": title,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [target(expr, "")],
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"]},
            "colorMode": "background",
            "graphMode": "none",
            "textMode": "value",
        },
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "decimals": decimals,
                "thresholds": {"mode": "absolute", "steps": thresholds},
            },
            "overrides": [],
        },
    }


def ts(title, pairs, unit, x, y, w=12, h=8, thresholds=None, max_=None, min_=None):
    defaults = {"unit": unit, "custom": {"lineWidth": 2, "fillOpacity": 8}}
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute", "steps": thresholds}
        defaults["custom"]["thresholdsStyle"] = {"mode": "line"}
    if max_ is not None:
        defaults["max"] = max_
    if min_ is not None:
        defaults["min"] = min_
    return {
        "type": "timeseries",
        "title": title,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets(*pairs),
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        "fieldConfig": {"defaults": defaults, "overrides": []},
    }


GREEN, RED, ORANGE = "green", "red", "orange"

panels = [
    # --- строка 1: SLO федерации ---
    stat(
        "Availability федерации, 5 мин",
        "federation:availability:ratio5m",
        "percentunit",
        [{"color": RED, "value": None}, {"color": GREEN, "value": 0.99}],
        0, 0, decimals=3,
    ),
    stat(
        "p95 федерации, 5 мин",
        "federation:http_request_duration_seconds:p95_5m",
        "s",
        [{"color": GREEN, "value": None}, {"color": RED, "value": 0.2}],
        6, 0, decimals=3,
    ),
    stat(
        "Burn rate, 1 ч",
        "federation:error_budget_burn_rate:1h",
        "none",
        [{"color": GREEN, "value": None}, {"color": ORANGE, "value": 1}, {"color": RED, "value": 14.4}],
        12, 0,
    ),
    stat(
        "Алерты SLO",
        'count(ALERTS{alertstate="firing",alertname=~"Federation.*"}) or vector(0)',
        "none",
        [{"color": GREEN, "value": None}, {"color": RED, "value": 1}],
        18, 0, decimals=0,
    ),
    # --- строка 2: по кластерам, серверный ряд (источник каталога) ---
    ts(
        "Error rate по кластерам (frontend, серверный ряд)",
        [
            ("cluster:http_error_rate:ratio5m", "{{cluster}} 5m"),
            ("cluster:http_error_rate:ratio1m", "{{cluster}} 1m"),
        ],
        "percentunit", 0, 4, thresholds=[{"color": GREEN, "value": None}, {"color": RED, "value": 0.05}], min_=0,
    ),
    ts(
        "p95 по кластерам (frontend)",
        [
            ("cluster:http_request_duration_seconds:p95_5m", "{{cluster}} p95 5m"),
            ("cluster:http_request_duration_seconds:p99_5m", "{{cluster}} p99 5m"),
        ],
        "s", 12, 4, thresholds=[{"color": GREEN, "value": None}, {"color": RED, "value": 0.2}], min_=0,
    ),
    ts(
        "Запросов/с по кластерам (frontend)",
        [("cluster:http_requests:rate5m", "{{cluster}}")],
        "reqps", 0, 12, min_=0,
    ),
    ts(
        "Saturation по кластерам",
        [
            ("cluster:cpu_saturation:ratio", "{{cluster}} cpu"),
            ("cluster:memory_saturation:ratio", "{{cluster}} mem"),
        ],
        "percentunit", 12, 12, min_=0, max_=1,
    ),
    # --- строка 3: клиентский ряд k6 и поток workload'ов ---
    ts(
        "k6: error rate и p95 (клиентский ряд)",
        [
            ('k6_http_req_failed_rate{kind="workload"}', "error rate"),
            ('k6_http_req_duration_p95{kind="workload"} / 1000', "p95, с"),
        ],
        "none", 0, 20, min_=0,
    ),
    ts(
        "Живых frontend'ов по кластерам",
        [
            ("cluster:stream_workloads_ready:count", "{{cluster}} поток"),
            ("cluster:frontends_ready:count", "{{cluster}} всего"),
        ],
        "none", 12, 20, min_=0,
    ),
    ts(
        "Envoy: запросов/с по upstream'ам",
        [("sum by (envoy_cluster_name) (rate(envoy_cluster_upstream_rq_total[1m]))", "{{envoy_cluster_name}}")],
        "reqps", 0, 28, w=24, min_=0,
    ),
    # --- строка 4: DR Controller (ADR 0028); метка кластера — cluster_id ---
    ts(
        "DR: состояние (0 healthy, 1 degraded, 2 recovering, 3 down)",
        [(
            'max by (cluster_id) (dr_cluster_state{state="degraded"})'
            ' + 2 * max by (cluster_id) (dr_cluster_state{state="recovering"})'
            ' + 3 * max by (cluster_id) (dr_cluster_state{state="down"})',
            "{{cluster_id}}",
        )],
        "none", 0, 36, min_=0, max_=3,
    ),
    ts(
        "DR: веса маршрута app.fed.local",
        [
            ("dr_route_weight", "{{cluster_id}}"),
            ("dr_route_weight_desired", "{{cluster_id}} желаемый"),
        ],
        "none", 12, 36, min_=0,
    ),
]

for i, p in enumerate(panels, start=1):
    p["id"] = i

dashboard = {
    "uid": "federation-slo",
    "title": "Federation SLO",
    "tags": ["federation", "slo"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 1,
    "editable": True,
    "refresh": "10s",
    "time": {"from": "now-30m", "to": "now"},
    "templating": {"list": []},
    "annotations": {"list": []},
    "panels": panels,
}

out = Path(__file__).with_name("federation-slo.json")
out.write_text(json.dumps(dashboard, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"{out.name}: {len(panels)} panels")
