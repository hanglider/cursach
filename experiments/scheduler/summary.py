"""Сводка прогона сценария 2 (spec §6, ADR 0030, 0031): error rate потока по окнам —
клиентский ряд k6 (приёмочный) и серверный из Thanos, реакция планировщика по каталогу,
признаки брака прогона.

    python summary.py <каталог прогона> [k6.csv]

Без k6.csv сводка пересчитывается из requests.csv.gz прогона: CSV k6 раннер не хранит.

Вход: meta.json (времена отказа из Chaos Mesh), CSV k6 (K6_CSV_TIME_FORMAT=unix_milli;
отметка — момент завершения запроса; тег kind — поток или фон, target_cluster — кластер
workload'а), workloadgen.log, incidents.json, k6-summary.json, registry/*.csv и
series/http_requests_total.json (export.sh). Выход в каталоге прогона: requests.csv.gz
(запрос на строку), per-second.csv, summary.json.

Только стандартная библиотека: запускается `uv run --no-project`.
"""

from __future__ import annotations

import csv
import gzip
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl

# Предрегистрированные параметры scoring (ADR 0005, 0006) — если в решениях их нет.
WEIGHTS = (0.5, 0.3, 0.2)
LATENCY_BUDGET_MS = 200.0
NEUTRAL = 0.5
TIE = 1e-6
# Пороги статуса degraded в каталоге (ADR 0025).
DEGRADED_ERROR_RATE = 0.05
DEGRADED_P95_MS = 200.0
INCIDENT_FIELDS = (
    "id",
    "cluster_id",
    "dr_mode",
    "peak_state",
    "detected_at",
    "ttd_seconds",
    "fault_ref",
)


def ms(iso: str | None) -> int | None:
    """ISO 8601 из Chaos Mesh, API каталога или CSV PostgreSQL (`+00`) → мс эпохи."""
    if not iso:
        return None
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def secs(delta_ms: int | None) -> float | None:
    return None if delta_ms is None else round(delta_ms / 1000, 3)


# --- клиентский ряд ------------------------------------------------------------------------


def read_requests(path: Path) -> list[dict]:
    """Строки http_reqs k6 → запросы потока и фона: ts_ms, kind, cluster, status, failed,
    duration_ms. Запросы к списку живых (kind=live-list) отбрасываются.

    Сэмплы одного запроса k6 пишет подряд: за http_reqs следует его http_req_duration с
    той же отметкой, сценарием, тегами и статусом — по ним длительность и присоединяется."""
    out: list[dict] = []
    pending: tuple | None = None
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            name = row["metric_name"]
            key = (row["timestamp"], row["scenario"], row["extra_tags"], row["status"])
            if name == "http_reqs":
                tags = dict(parse_qsl(row["extra_tags"] or ""))
                kind = tags.get("kind", "")
                if kind not in ("workload", "background"):
                    pending = None
                    continue
                out.append(
                    {
                        "ts_ms": int(row["timestamp"]),
                        "kind": kind,
                        "cluster": tags.get("target_cluster", ""),
                        "status": int(row["status"] or 0),
                        "failed": 0 if row["expected_response"] == "true" else 1,
                        "duration_ms": None,
                    }
                )
                pending = key
            elif name == "http_req_duration" and pending == key and out:
                out[-1]["duration_ms"] = float(row["metric_value"])
                pending = None
    out.sort(key=lambda r: r["ts_ms"])
    return out


def read_saved_requests(path: Path) -> list[dict]:
    """requests.csv.gz прогона (write_requests) → те же записи, что у read_requests."""
    out: list[dict] = []
    with gzip.open(path, "rt", newline="") as f:
        for row in csv.DictReader(f):
            d = row["duration_ms"]
            out.append(
                {
                    "ts_ms": int(row["ts_ms"]),
                    "kind": row["kind"],
                    "cluster": row["target_cluster"],
                    "status": int(row["status"]),
                    "failed": int(row["failed"]),
                    "duration_ms": float(d) if d else None,
                }
            )
    return out


def p95(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, math.ceil(0.95 * len(s)) - 1)]


def stats(reqs: list[dict]) -> dict:
    n = len(reqs)
    failed = sum(r["failed"] for r in reqs)
    durations = [r["duration_ms"] for r in reqs if r["duration_ms"] is not None]
    q = p95(durations)
    return {
        "requests": n,
        "failed": failed,
        "error_rate": None if n == 0 else round(failed / n, 6),
        "p95_ms": None if q is None else round(q, 3),
    }


def window_stats(reqs: list[dict], bounds: dict, by_cluster: bool) -> dict:
    out = {}
    for w, (start, end) in bounds.items():
        sel = [r for r in reqs if start <= r["ts_ms"] < end]
        st = {"from_ms": start, "to_ms": end, "seconds": secs(end - start), **stats(sel)}
        if by_cluster:
            groups: dict[str, list[dict]] = defaultdict(list)
            for r in sel:
                groups[r["cluster"] or "unknown"].append(r)
            st["by_cluster"] = {c: stats(g) for c, g in sorted(groups.items())}
        out[w] = st
    return out


def write_requests(run_dir: Path, reqs: list[dict], cluster: str) -> None:
    with gzip.open(run_dir / "requests.csv.gz", "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts_ms", "kind", "target_cluster", "status", "failed", "duration_ms"])
        for r in reqs:
            d = r["duration_ms"]
            dur = "" if d is None else round(d, 3)
            w.writerow([r["ts_ms"], r["kind"], r["cluster"], r["status"], r["failed"], dur])
    per_second: dict[int, Counter] = defaultdict(Counter)
    for r in reqs:
        c = per_second[r["ts_ms"] // 1000]
        prefix = "stream" if r["kind"] == "workload" else "background"
        c[f"{prefix}_requests"] += 1
        c[f"{prefix}_failed"] += r["failed"]
        if r["kind"] == "workload" and r["cluster"] == cluster:
            c["stream_degraded_requests"] += 1
            c["stream_degraded_failed"] += r["failed"]
    cols = [
        "stream_requests",
        "stream_failed",
        "stream_degraded_requests",
        "stream_degraded_failed",
        "background_requests",
        "background_failed",
    ]
    with (run_dir / "per-second.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["second", *cols])
        for sec in sorted(per_second):
            w.writerow([sec, *(per_second[sec][c] for c in cols)])


# --- серверный ряд -------------------------------------------------------------------------


def increase(values: list[list], t0: float, t1: float) -> float:
    """Прирост счётчика в (t0, t1] по выгрузке с шагом 5 с. Ряд, родившийся внутри окна
    (новый под), считается от нуля; падение значения — сброс счётчика."""
    inc = 0.0
    prev: float | None = None
    born_inside = float(values[0][0]) > t0
    for ts, v in values:
        ts, v = float(ts), float(v)
        if ts <= t0:
            prev = v
            continue
        if ts > t1:
            break
        if prev is None:
            prev = 0.0 if born_inside else v
        inc += v - prev if v >= prev else v
        prev = v
    return inc


def server_rows(run_dir: Path, bounds: dict) -> dict | None:
    path = run_dir / "series" / "http_requests_total.json"
    if not path.exists():
        return None
    result = json.loads(path.read_text())["data"]["result"]
    out: dict = {"stream": {}, "background": {}}
    for w, (start, end) in bounds.items():
        t0, t1 = start / 1000, end / 1000
        acc = {k: defaultdict(lambda: [0.0, 0.0]) for k in out}
        for s in result:
            m = s["metric"]
            if m.get("tier") != "frontend":
                continue
            wl = m.get("workload", "")
            kind = "stream" if wl.startswith("w-") else "background" if wl == "app" else None
            if kind is None:
                continue
            inc = increase(s["values"], t0, t1)
            cell = acc[kind][m.get("cluster", "unknown")]
            cell[0] += inc
            if m.get("status", "").startswith("5"):
                cell[1] += inc
        for kind in out:
            total = sum(c[0] for c in acc[kind].values())
            errors = sum(c[1] for c in acc[kind].values())
            out[kind][w] = {
                "requests": round(total),
                "errors": round(errors),
                "error_rate": None if total == 0 else round(errors / total, 6),
                "by_cluster": {
                    c: {
                        "requests": round(v[0]),
                        "errors": round(v[1]),
                        "error_rate": None if v[0] == 0 else round(v[1] / v[0], 6),
                    }
                    for c, v in sorted(acc[kind].items())
                },
            }
    return out


# --- планировщик и каталог -----------------------------------------------------------------


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def num(v: str | None) -> float | None:
    return None if v in (None, "") else float(v)


def score(m: dict, weights: tuple, budget: float) -> float:
    """Score ADR 0005 по снимку каталога: отсутствующая метрика — нейтральные 0.5."""
    e, lat, sat = m.get("error_rate"), m.get("latency_p95_ms"), m.get("saturation")
    t_err = NEUTRAL if e is None else 1 - min(max(e, 0), 1)
    t_lat = NEUTRAL if lat is None else 1 - min(max(lat, 0) / budget, 1)
    t_sat = NEUTRAL if sat is None else 1 - min(max(sat, 0), 1)
    return weights[0] * t_err + weights[1] * t_lat + weights[2] * t_sat


def degraded(m: dict) -> bool:
    e, lat = m.get("error_rate"), m.get("latency_p95_ms")
    bad_errors = e is not None and e > DEGRADED_ERROR_RATE
    return bad_errors or (lat is not None and lat > DEGRADED_P95_MS)


def placement_rows(
    run_dir: Path, bounds: dict, cluster: str, inj: int, rec: int
) -> tuple[dict, list[dict]]:
    decisions = []
    for r in read_csv(run_dir / "registry" / "placement_decisions.csv"):
        decisions.append(
            {
                "ts_ms": ms(r["decided_at"]),
                "workload": r["workload_ref"],
                "kind": r["scheduler_kind"],
                "chosen": r["chosen_cluster"],
                "outcome": r["outcome"],
                "candidates": json.loads(r["candidates"] or "[]"),
                "scoring": json.loads(r["scoring"]) if r.get("scoring") else None,
            }
        )
    stream = [d for d in decisions if d["workload"].startswith("workloads/w-")]
    placed = [d for d in stream if d["outcome"] == "placed"]

    scoring = next((d["scoring"] for d in stream if d["scoring"]), None) or {}
    weights = tuple(scoring.get("weights") or WEIGHTS)
    budget = float(scoring.get("latency_budget_ms") or LATENCY_BUDGET_MS)

    out: dict = {"weights": list(weights), "latency_budget_ms": budget}
    for w, (start, end) in bounds.items():
        sel = [d for d in placed if start <= d["ts_ms"] < end]
        to_deg = sum(d["chosen"] == cluster for d in sel)
        out[w] = {
            "placements": len(sel),
            "to_degraded": to_deg,
            "share_to_degraded": None if not sel else round(to_deg / len(sel), 4),
        }

    # Первое решение meta в обход деградировавшего кластера: он был кандидатом со score
    # ниже выбранного (не ничья).
    out["first_avoid_s"] = None
    for d in placed:
        if d["kind"] != "meta" or not (inj <= d["ts_ms"] < rec) or d["chosen"] == cluster:
            continue
        sc = {c["cluster"]: c.get("score") for c in d["candidates"] if not c.get("filtered_by")}
        own, chosen = sc.get(cluster), sc.get(d["chosen"])
        if own is not None and chosen is not None and chosen - own > TIE:
            out["first_avoid_s"] = secs(d["ts_ms"] - inj)
            break
    ret = next((d for d in placed if d["ts_ms"] >= rec and d["chosen"] == cluster), None)
    out["first_return_s"] = None if ret is None else secs(ret["ts_ms"] - rec)

    # Снимки каталога: когда деградация стала видна планировщику (одинаково для обоих режимов).
    snaps: dict[int, dict[str, dict]] = defaultdict(dict)
    for r in read_csv(run_dir / "registry" / "cluster_metrics.csv"):
        snaps[ms(r["observed_at"])][r["cluster_id"]] = {
            "error_rate": num(r["error_rate"]),
            "latency_p95_ms": num(r["latency_p95_ms"]),
            "saturation": num(r["saturation"]),
        }
    times = sorted(t for t in snaps if cluster in snaps[t])

    def loses(t: int) -> bool:
        others = [score(m, weights, budget) for c, m in snaps[t].items() if c != cluster]
        return bool(others) and score(snaps[t][cluster], weights, budget) < max(others) - TIE

    in_fault = [t for t in times if inj <= t < rec]
    out["degraded_s"] = next((secs(t - inj) for t in in_fault if degraded(snaps[t][cluster])), None)
    # Устойчивый проигрыш: с этого снимка и до конца отказа кластер проигрывает в каждом, —
    # иначе шум saturation до отказа давал бы «реакцию» в нулевую секунду.
    out["score_loss_s"] = None
    for i, t in enumerate(in_fault):
        if all(loses(u) for u in in_fault[i:]):
            out["score_loss_s"] = secs(t - inj)
            break
    after = [t for t in times if t >= rec]
    out["catalog_recovered_s"] = next(
        (secs(t - rec) for t in after if not degraded(snaps[t][cluster])), None
    )
    return out, stream


# --- сводка --------------------------------------------------------------------------------


def generator_events(run_dir: Path) -> dict:
    counts: dict[str, Counter] = defaultdict(Counter)
    path = run_dir / "workloadgen.log"
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict) and "event" in ev:
                counts[ev["event"]][ev.get("cluster") or "-"] += 1
    return {e: dict(c) for e, c in sorted(counts.items())}


def main(run_dir: Path, csv_path: Path | None) -> None:
    meta = json.loads((run_dir / "meta.json").read_text())
    cluster = meta["cluster"]
    inj = ms(meta["fault"]["injected_at"])
    rec = ms(meta["fault"].get("recovered_at")) or inj + meta["structure"]["fault_s"] * 1000

    reqs = read_requests(csv_path) if csv_path else read_saved_requests(run_dir / "requests.csv.gz")
    stream = [r for r in reqs if r["kind"] == "workload"]
    background = [r for r in reqs if r["kind"] == "background"]
    first = min(r["ts_ms"] for r in reqs)
    last = max(r["ts_ms"] for r in reqs) + 1
    bounds = {"baseline": (first, inj), "fault": (inj, rec), "recovery": (rec, last)}
    if csv_path:
        write_requests(run_dir, reqs, cluster)

    placement, decisions = placement_rows(run_dir, bounds, cluster, inj, rec)
    inc_path = run_dir / "incidents.json"
    incidents = json.loads(inc_path.read_text()) if inc_path.exists() else []
    k6_metrics = json.loads((run_dir / "k6-summary.json").read_text())["metrics"]
    dropped = int(k6_metrics.get("dropped_iterations", {}).get("values", {}).get("count", 0))
    events = generator_events(run_dir)

    reasons, notes = [], []
    downs = sorted({i["cluster_id"] for i in incidents if i.get("peak_state") == "down"})
    if downs:
        # observe не трогает маршрут и каталог: DOWN — оговорка, не брак (ADR 0033).
        notes.append(f"DR в observe дошёл до DOWN: {','.join(downs)} — с failover вмешался бы")
    if any("down" in (c.get("filtered_by") or "") for d in decisions for c in d["candidates"]):
        reasons.append("каталог отсеивал кластер как down")
    bad = Counter(d["outcome"] for d in decisions if d["outcome"] != "placed")
    if bad:
        reasons.append(f"решения не placed: {dict(bad)}")
    if dropped:
        reasons.append(f"k6 пропустил итерации: {dropped}")
    if not meta["fault"].get("recovered_at"):
        notes.append("Chaos Mesh не сообщил Recover: конец окна отказа — injected_at + fault_s")
    not_ready = events.get("not_ready", {})
    if not_ready:
        notes.append(f"не стали готовыми (без трафика, их ошибки вне ряда): {not_ready}")
    if events.get("not_placed"):
        notes.append(f"планировщик не разместил вовремя: {events['not_placed']}")

    client = {
        "stream": window_stats(stream, bounds, by_cluster=True),
        "background": window_stats(background, bounds, by_cluster=False),
    }
    summary = {
        "run_id": meta["run_id"],
        "scenario": meta["scenario"],
        "cluster": cluster,
        "scheduler": meta["scheduler"],
        "dr_mode": meta["dr_mode"],
        "fault": meta["fault"],
        "acceptance": {
            "error_rate": client["stream"]["fault"]["error_rate"],
            "series": "client k6, поток workload'ов",
            "window": "fault: Apply..Recover Chaos Mesh",
        },
        "client": client,
        "server": server_rows(run_dir, bounds),
        "placement": placement,
        "dr": {"incidents": [{k: i.get(k) for k in INCIDENT_FIELDS} for i in incidents]},
        "generator": events,
        "k6": {"exit_code": meta["k6"]["exit_code"], "dropped_iterations": dropped},
        "validity": {"valid": not reasons, "reasons": reasons, "notes": notes},
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) > 2 else None)
