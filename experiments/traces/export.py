"""Выгрузка трейсов прогона из Jaeger (API v3) и разложение задержки по звеньям (ADR 0029).

    python export.py <каталог прогона> <start_unix_s> <end_unix_s> [jaeger_url]
    python export.py --offline <каталог прогона>   # пересчёт из traces/spans.csv.gz

Трейсы ищутся по сервису `gateway` (корень трейса — шлюз control) окнами по 30 с, чтобы
ответ Jaeger оставался небольшим. Сырые спаны выгружаются за всё окно экспорта (с
запасом, как ряды); статистика — только по трейсам, начавшимся внутри прогона
[started_at, finished_at) из meta.json: запас захватывает хвосты соседних прогонов.
Результат в <каталог прогона>/traces/:

- spans.csv.gz — сырые спаны: трейс, спан, родитель, сервис, кластер, workload,
  начало и длительность в мкс, HTTP-статус;
- decomposition.csv.gz — трейс на строку, задержка по звеньям в мс;
- summary.json — перцентили звеньев по кластерам и в целом.

Звенья одного запроса (architecture §2.1, spec §2.2). У шлюза два спана: ingress
(сервер: запрос целиком) и egress (клиент: вызов upstream'а, spawn_upstream_span):

    total        ingress шлюза: запрос целиком, как его видит Envoy в control
    gateway      ingress − egress: собственная работа Envoy
    hop          egress − frontend: сеть control → кластер, kube-proxy NodePort
    frontend     frontend − backend_call: работа frontend'а
    backend_net  backend_call − backend: сеть и kube-proxy frontend → backend
    backend      серверный спан backend'а

Только стандартная библиотека: запускается `uv run --no-project`.
"""

from __future__ import annotations

import csv
import gzip
import json
import math
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

GATEWAY = "gateway"
WINDOW_S = 30
SEARCH_DEPTH = 5000
KIND_SERVER, KIND_CLIENT = 2, 3
COMPONENTS = ("total", "gateway", "hop", "frontend", "backend_net", "backend")


def rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def fetch(jaeger: str, start: float, end: float) -> list[dict]:
    """resourceSpans всех трейсов шлюза с корнем в [start, end)."""
    q = urllib.parse.urlencode(
        {
            "query.service_name": GATEWAY,
            "query.start_time_min": rfc3339(start),
            "query.start_time_max": rfc3339(end),
            "query.search_depth": SEARCH_DEPTH,
        }
    )
    try:
        with urllib.request.urlopen(f"{jaeger}/api/v3/traces?{q}", timeout=60) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # Jaeger отвечает 404, если трейсов в окне нет
            return []
        raise
    return (body.get("result") or {}).get("resourceSpans") or []


def attr_value(v: dict):
    for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if k in v:
            return v[k]
    return None


def attrs(items: list[dict] | None) -> dict:
    return {a["key"]: attr_value(a.get("value", {})) for a in items or []}


def spans_of(resource_spans: list[dict]) -> list[dict]:
    """Плоский список спанов с атрибутами ресурса."""
    out = []
    for rs in resource_spans:
        res = attrs((rs.get("resource") or {}).get("attributes"))
        for ss in rs.get("scopeSpans") or []:
            for s in ss.get("spans") or []:
                a = attrs(s.get("attributes"))
                start, end = int(s["startTimeUnixNano"]), int(s["endTimeUnixNano"])
                status = a.get("http.response.status_code", a.get("http.status_code"))
                out.append(
                    {
                        "trace_id": s["traceId"],
                        "span_id": s["spanId"],
                        "parent_span_id": s.get("parentSpanId", ""),
                        "service": res.get("service.name", ""),
                        "name": s.get("name", ""),
                        "kind": int(s.get("kind", 0)),
                        # Кластер: у сервиса — ресурс от коллектора, у шлюза — тег спана.
                        "cluster": res.get("federation.cluster") or a.get("federation.cluster", ""),
                        "workload": res.get("federation.workload", ""),
                        "start_us": start // 1000,
                        "duration_us": (end - start) // 1000,
                        "http_status": int(status) if status not in (None, "") else "",
                        "otel_status": int((s.get("status") or {}).get("code", 0)),
                        "upstream": a.get("upstream_address", ""),
                    }
                )
    return out


def decompose(spans: list[dict]) -> list[dict]:
    """Трейс на строку: звенья в мс; None — звена нет (запрос не дошёл до сервиса)."""
    by_trace: dict[str, list[dict]] = {}
    for s in spans:
        by_trace.setdefault(s["trace_id"], []).append(s)
    rows = []
    for tid, ss in by_trace.items():

        def pick(service: str, kind: int, spans: list[dict] = ss) -> dict | None:
            cands = [s for s in spans if s["service"] == service and s["kind"] == kind]
            return min(cands, key=lambda s: s["start_us"]) if cands else None

        gw, egress = pick(GATEWAY, KIND_SERVER), pick(GATEWAY, KIND_CLIENT)
        if gw is None:
            continue
        fe, call, be = (
            pick("frontend", KIND_SERVER),
            pick("frontend", KIND_CLIENT),
            pick("backend", KIND_SERVER),
        )
        ms = {k: None for k in COMPONENTS}
        ms["total"] = gw["duration_us"] / 1000
        if egress:
            ms["gateway"] = (gw["duration_us"] - egress["duration_us"]) / 1000
        if fe:
            # Без спана egress звено шлюза неотделимо от сети: hop включает оба.
            ms["hop"] = ((egress or gw)["duration_us"] - fe["duration_us"]) / 1000
        if fe and call:
            ms["frontend"] = (fe["duration_us"] - call["duration_us"]) / 1000
        if call and be:
            ms["backend_net"] = (call["duration_us"] - be["duration_us"]) / 1000
        if be:
            ms["backend"] = be["duration_us"] / 1000
        rows.append(
            {
                "trace_id": tid,
                "start_us": gw["start_us"],
                "cluster": (fe or be or {}).get("cluster", ""),
                "workload": (fe or {}).get("workload", ""),
                "http_status": gw["http_status"],
                "complete": int(all(v is not None for v in ms.values())),
                **{f"{k}_ms": v for k, v in ms.items()},
            }
        )
    rows.sort(key=lambda r: r["start_us"])
    return rows


def read_spans(path: Path) -> list[dict]:
    """spans.csv.gz обратно в словари с числовыми полями (режим --offline)."""
    ints = ("kind", "start_us", "duration_us", "otel_status")
    out = []
    with gzip.open(path, "rt", newline="") as f:
        for row in csv.DictReader(f):
            for k in ints:
                row[k] = int(row[k])
            row["http_status"] = int(row["http_status"]) if row["http_status"] else ""
            out.append(row)
    return out


def run_window(run_dir: Path) -> tuple[int, int] | None:
    """[started_at, finished_at) прогона в мкс из meta.json; None — метаданных нет."""
    meta = run_dir / "meta.json"
    if not meta.exists():
        return None
    m = json.loads(meta.read_text())
    if not m.get("started_at") or not m.get("finished_at"):
        return None
    started, finished = (datetime.fromisoformat(m[k]) for k in ("started_at", "finished_at"))
    return int(started.timestamp() * 1_000_000), int(finished.timestamp() * 1_000_000)


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, math.ceil(q * len(s)) - 1)], 3)


def stats(rows: list[dict]) -> dict:
    out = {
        "traces": len(rows),
        "complete": sum(r["complete"] for r in rows),
        "errors": sum(1 for r in rows if not r["http_status"] or int(r["http_status"]) >= 500),
    }
    for k in COMPONENTS:
        vals = [r[f"{k}_ms"] for r in rows if r[f"{k}_ms"] is not None]
        out[k] = {
            "n": len(vals),
            "p50": pct(vals, 0.5),
            "p95": pct(vals, 0.95),
            "p99": pct(vals, 0.99),
            "mean": None if not vals else round(sum(vals) / len(vals), 3),
        }
    return out


def write_csv_gz(path: Path, rows: list[dict], fields: list[str]) -> None:
    with gzip.open(path, "wt", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def collect(jaeger: str, start: float, end: float) -> list[dict]:
    seen: set[str] = set()
    spans: list[dict] = []
    t = start
    while t < end:
        for s in spans_of(fetch(jaeger, t, min(t + WINDOW_S, end))):
            if s["span_id"] not in seen:  # трейс на границе окон мог прийти дважды
                seen.add(s["span_id"])
                spans.append(s)
        t += WINDOW_S
    spans.sort(key=lambda s: (s["trace_id"], s["start_us"]))
    return spans


def main(run_dir: Path, start: float, end: float, jaeger: str, offline: bool = False) -> dict:
    out = run_dir / "traces"
    spans = read_spans(out / "spans.csv.gz") if offline else collect(jaeger, start, end)
    rows = decompose(spans)
    window = run_window(run_dir)
    for r in rows:
        r["in_run"] = int(window is None or window[0] <= r["start_us"] < window[1])
    in_run = [r for r in rows if r["in_run"]]

    out.mkdir(parents=True, exist_ok=True)
    span_fields = [
        "trace_id",
        "span_id",
        "parent_span_id",
        "service",
        "name",
        "kind",
        "cluster",
        "workload",
        "start_us",
        "duration_us",
        "http_status",
        "otel_status",
        "upstream",
    ]
    if not offline:
        write_csv_gz(out / "spans.csv.gz", spans, span_fields)
    row_fields = [
        "trace_id",
        "start_us",
        "in_run",
        "cluster",
        "workload",
        "http_status",
        "complete",
    ]
    write_csv_gz(out / "decomposition.csv.gz", rows, row_fields + [f"{k}_ms" for k in COMPONENTS])
    clusters = sorted({r["cluster"] for r in in_run if r["cluster"]})
    summary = {
        "export_window": None if offline else {"start": rfc3339(start), "end": rfc3339(end)},
        "run_window": None
        if window is None
        else {"start": rfc3339(window[0] / 1e6), "end": rfc3339(window[1] / 1e6)},
        "jaeger": None if offline else jaeger,
        "spans": len(spans),
        "traces_exported": len(rows),
        "components": list(COMPONENTS),
        # Статистика — по трейсам внутри прогона (in_run), а не по всему окну экспорта.
        "all": stats(in_run),
        "by_cluster": {c: stats([r for r in in_run if r["cluster"] == c]) for c in clusters},
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    return summary


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--offline":
        s = main(Path(sys.argv[2]), 0, 0, "", offline=True)
    elif len(sys.argv) in (4, 5):
        url = (sys.argv[4] if len(sys.argv) == 5 else "http://127.0.0.1:16686").rstrip("/")
        s = main(Path(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3]), url)
    else:
        raise SystemExit(__doc__)
    a = s["all"]
    print(
        json.dumps(
            {"traces": a["traces"], "complete": a["complete"], "hop_p50_ms": a["hop"]["p50"]},
            ensure_ascii=False,
        )
    )
