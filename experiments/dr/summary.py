"""Сводка прогона сценария 1 (spec §6, ADR 0007, 0010, 0028): TTD, RTO, потерянные
запросы и доступность app.fed.local по окнам прогона.

    python summary.py <каталог прогона> <k6.csv>

Вход: meta.json (времена отказа из Chaos Mesh, режим DR), incidents.json (записи
каталога), CSV k6 с отметкой каждого запроса (K6_CSV_TIME_FORMAT=unix_milli; отметка —
момент завершения запроса). Выход в каталоге прогона: requests.csv.gz (запрос на строку:
ts_ms, status, failed, duration_ms), per-second.csv, summary.json.

Только стандартная библиотека: запускается `uv run --no-project`.
"""

from __future__ import annotations

import csv
import gzip
import json
import math
import sys
from datetime import datetime
from pathlib import Path


def ms(iso: str | None) -> int | None:
    if not iso:
        return None
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def read_requests(path: Path) -> list[tuple[int, int, int, float | None]]:
    """Строки http_reqs k6 → (ts_ms, status, failed, duration_ms).

    Сэмплы одного запроса k6 пишет подряд: за http_reqs следует его http_req_duration с
    той же отметкой, URL и статусом — по ним длительность и присоединяется."""
    out: list[list] = []
    pending: tuple | None = None
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            name = row["metric_name"]
            key = (row["timestamp"], row["url"], row["status"])
            if name == "http_reqs":
                failed = 0 if row["expected_response"] == "true" else 1
                out.append([int(row["timestamp"]), int(row["status"] or 0), failed, None])
                pending = key
            elif name == "http_req_duration" and pending == key and out:
                out[-1][3] = float(row["metric_value"])
                pending = None
    out.sort(key=lambda r: r[0])
    return [tuple(r) for r in out]


def p95(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, math.ceil(0.95 * len(s)) - 1)]


def window(reqs, start: int | None, end: int | None) -> dict | None:
    """Запросы, завершившиеся в [start, end)."""
    if start is None or end is None or end < start:
        return None
    sel = [r for r in reqs if start <= r[0] < end]
    n = len(sel)
    failed = sum(r[2] for r in sel)
    durations = [r[3] for r in sel if r[3] is not None]
    return {
        "from_ms": start,
        "to_ms": end,
        "seconds": round((end - start) / 1000, 3),
        "requests": n,
        "failed": failed,
        "availability": None if n == 0 else round(1 - failed / n, 6),
        "p95_ms": None if not durations else round(p95(durations), 3),
    }


def main(run_dir: Path, csv_path: Path) -> None:
    meta = json.loads((run_dir / "meta.json").read_text())
    incidents = json.loads((run_dir / "incidents.json").read_text())
    reqs = read_requests(csv_path)
    if not reqs:
        raise SystemExit("summary: в CSV k6 нет запросов")

    with gzip.open(run_dir / "requests.csv.gz", "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts_ms", "status", "failed", "duration_ms"])
        w.writerows(reqs)
    per_second: dict[int, list[int]] = {}
    for ts, _, failed, _ in reqs:
        b = per_second.setdefault(ts // 1000, [0, 0])
        b[0] += 1
        b[1] += failed
    with (run_dir / "per-second.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["second", "requests", "failed"])
        w.writerows([s, *per_second[s]] for s in sorted(per_second))

    fault = meta["fault"]
    injected = ms(fault["injected_at"])
    recovered = ms(fault.get("recovered_at"))
    if recovered is None:
        # Chaos Mesh не сообщил о снятии: конец отказа — по заданной длительности.
        recovered = injected + meta["structure"]["fault_s"] * 1000
        fault = {**fault, "recovered_at_estimated": True}
    first, last = reqs[0][0], reqs[-1][0] + 1
    linked = next((i for i in incidents if i.get("fault_ref") == fault["ref"]), None)
    failover = ms(linked.get("failover_completed_at")) if linked else None

    windows = {
        "baseline": window(reqs, first, injected),
        "fault": window(reqs, injected, recovered),
        "recovery": window(reqs, recovered, last),
        "until_failover": window(reqs, injected, failover),
        "after_failover_in_fault": window(reqs, failover, recovered),
    }
    # ADR 0010: потерянные запросы — ошибки от внесения отказа до завершения failover.
    # Без failover (observe, «без DR») окно — весь отказ: трафик так и не переключили.
    if failover is not None:
        lost, lost_window = (
            windows["until_failover"]["failed"],
            "fault_injected..failover_completed",
        )
    else:
        lost, lost_window = windows["fault"]["failed"], "fault_injected..fault_recovered"

    incident = None
    if linked:
        incident = {
            k: linked.get(k)
            for k in (
                "id",
                "dr_mode",
                "peak_state",
                "detected_at",
                "failover_started_at",
                "failover_completed_at",
                "recovery_started_at",
                "recovery_completed_at",
                "ttd_seconds",
                "rto_seconds",
            )
        }
        done = ms(linked.get("recovery_completed_at"))
        # Возврат трафика после снятия отказа: от снятия до HEALTHY (ступени 10→50→100%).
        incident["return_seconds"] = (
            None if done is None or recovered is None else round((done - recovered) / 1000, 3)
        )
        incident["details"] = linked.get("details")

    ttd = incident and incident["ttd_seconds"]
    rto = incident and incident["rto_seconds"]
    summary = {
        "run_id": meta["run_id"],
        "scenario": meta["scenario"],
        "cluster": meta["cluster"],
        "dr_mode": meta["dr_mode"],
        "fault": fault,
        "incident": incident,
        "incidents_in_run": len(incidents),
        "incidents_before_fault": sum(1 for i in incidents if ms(i["detected_at"]) < injected),
        "requests": {"total": len(reqs), "failed": sum(r[2] for r in reqs)},
        "lost_requests": lost,
        "lost_requests_window": lost_window,
        "windows": windows,
        # Целевые показатели spec §1.3: TTD < 60 с, RTO < 5 мин.
        "targets": {
            "ttd_lt_60s": None if ttd is None else ttd < 60,
            "rto_lt_300s": None if rto is None else rto < 300,
        },
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({k: summary[k] for k in ("lost_requests", "targets")}, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
