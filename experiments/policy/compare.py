"""Сводка прогонов сценария 3 (spec §6, ADR 0026, 0032, 0034): таблица по прогонам и итоги
по наборам — enforce («после»), audit («до») и audit с удержанием нарушителей до записи из
PolicyReport. Задержки фиксации наборов не смешиваются: у audit и audit-hold разный момент
удаления нарушителей (ADR 0034). Целевой показатель spec §1.3 — 0 размещённых нарушителей
в enforce.

    python compare.py [--json <путь>] <каталог прогона>...

Markdown — в stdout, числа — в JSON. Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import median


def load(run_dir: Path) -> dict:
    s = json.loads((run_dir / "summary.json").read_text())
    meta = json.loads((run_dir / "meta.json").read_text())
    kind = s["enforcement"] + ("-hold" if s.get("hold") else "")
    # Уникальные нарушители по источнику — из сырых записей: в сводках до ADR 0034 их нет.
    by_src: dict[str, set[str]] = {}
    for v in json.loads((run_dir / "violations.json").read_text()):
        if v["workload_ref"].startswith("workloads/pv-"):
            src = (v.get("source_key") or "scheduler").split("/")[0]
            by_src.setdefault(src, set()).add(v["workload_ref"])
    unique = {k: len(refs) for k, refs in by_src.items()}
    return {
        "run_id": meta["run_id"],
        "set": kind,
        "attempts": s["attempts"],
        "rejected": s["rejected"],
        "admitted": s["admitted"],
        "recorded": s["recorded"],
        "recorded_raw": s["recorded_raw"],
        "unique_by_source": unique,
        "eligible_ok": s["eligible_ok"],
        "latency": s["detection_latency_s"],
        "latency_by_source": s.get("detection_latency_by_source_s"),
        "policyreport_since_event": s.get("policyreport_since_event_s"),
        "other_errors": s["other_errors"],
        "left": s["violating_deployments_left"],
    }


def num(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}"


def lat(d: dict | None) -> str:
    return "—" if not d or d.get("median") is None else f"{num(d['median'])} / {num(d['max'])}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path)
    ap.add_argument("runs", nargs="+", type=Path)
    args = ap.parse_args()
    runs = [load(r) for r in args.runs]

    sets = {}
    for name in ("enforce", "audit", "audit-hold"):
        rs = [r for r in runs if r["set"] == name]
        if not rs:
            continue
        pr = [r["unique_by_source"].get("policyreport", 0) for r in rs]
        sets[name] = {
            "runs": len(rs),
            "attempts": sum(r["attempts"] for r in rs),
            "rejected": sum(r["rejected"] for r in rs),
            "admitted": sum(r["admitted"] for r in rs),
            "recorded": sum(r["recorded"] for r in rs),
            "recorded_policyreport": sum(pr),
            "eligible_ok_all": all(r["eligible_ok"] == r["recorded_raw"] for r in rs),
            # медиана медиан и максимум максимумов по прогонам набора
            "latency_median_of_medians": median(
                [r["latency"]["median"] for r in rs if r["latency"]["median"] is not None] or [0]
            ),
            "latency_max": max((r["latency"]["max"] or 0) for r in rs),
            "policyreport_since_event_median_of_medians": median(
                [
                    r["policyreport_since_event"]["median"]
                    for r in rs
                    if r["policyreport_since_event"]
                    and r["policyreport_since_event"]["median"] is not None
                ]
                or [0]
            )
            if name == "audit-hold"
            else None,
        }
    enforce = sets.get("enforce")
    result = {
        "runs": runs,
        "sets": sets,
        "target_zero_admitted_in_enforce": None if not enforce else enforce["admitted"] == 0,
    }
    if args.json:
        args.json.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")

    out = [
        "| прогон | набор | отклонено | размещено | в каталоге | из событий | из PolicyReport "
        "| задержка, с (мед / макс) | PolicyReport от события, с | eligible |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in runs:
        u = r["unique_by_source"]
        by = r["latency_by_source"] or {}
        pr_lat = f" (PR {lat(by['policyreport'])})" if by.get("policyreport") else ""
        out.append(
            f"| {r['run_id']} | {r['set']} | {r['rejected']}/{r['attempts']} "
            f"| {r['admitted']} | {r['recorded']} "
            f"| {u.get('event', 0)} | {u.get('policyreport', 0)} "
            f"| {lat(r['latency'])}{pr_lat} | {lat(r['policyreport_since_event'])} "
            f"| {r['eligible_ok']}/{r['recorded_raw']} |"
        )
    out.append("")
    for name, a in sets.items():
        line = (
            f"- {name}: прогонов {a['runs']}, отклонено {a['rejected']}/{a['attempts']}, "
            f"размещено {a['admitted']}, в каталоге {a['recorded']} "
            f"(из PolicyReport {a['recorded_policyreport']}); "
            f"eligible корректен {'у всех' if a['eligible_ok_all'] else 'не у всех'}; "
            f"задержка — медиана медиан {num(a['latency_median_of_medians'])} с, "
            f"максимум {num(a['latency_max'])} с"
        )
        if a["policyreport_since_event_median_of_medians"] is not None:
            line += (
                f"; PolicyReport от события — медиана медиан "
                f"{num(a['policyreport_since_event_median_of_medians'])} с"
            )
        out.append(line)
    if enforce:
        out.append(
            f"- размещённых нарушителей в enforce: {enforce['admitted']} (цель — 0): "
            f"{'да' if result['target_zero_admitted_in_enforce'] else 'нет'}"
        )
    for r in runs:
        if r["other_errors"] or r["left"]:
            out.append(f"- {r['run_id']}: иных ошибок {r['other_errors']}, осталось {r['left']}")
    print("\n".join(out))


if __name__ == "__main__":
    main()
