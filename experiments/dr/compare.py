"""Сравнение прогонов сценария 1 (spec §6, ADR 0008, 0010, 0028): таблица по прогонам,
средние по режимам DR и «с DR / без DR» — потерянные запросы и доступность app.fed.local
в окне отказа. Целевые показатели spec §1.3: TTD < 60 с — во всех прогонах (обнаружение в
observe то же), RTO < 5 мин — в active. Отдельного порога на потерянные запросы нет
(ADR 0010): величина фиксируется и сравнивается.

Критериев брака для сценария 1 не задано: в средние входят все прогоны, а настораживающее
(инцидента нет, инцидент до отказа, повторный failover, k6 с ошибкой) выводится заметками.

    python compare.py [--json <путь>] <каталог прогона>...

Markdown — в stdout, числа — в JSON. Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from statistics import mean

TTD_TARGET_S = 60  # spec §1.3
RTO_TARGET_S = 300


def load(run_dir: Path) -> dict:
    s = json.loads((run_dir / "summary.json").read_text())
    meta = json.loads((run_dir / "meta.json").read_text())
    inc = s.get("incident") or {}
    fault = s["windows"]["fault"]
    m = re.search(r"-r(\d+)$", s["run_id"])
    notes = []
    if not inc:
        notes.append("инцидента после отказа нет")
    if s.get("incidents_before_fault"):
        notes.append(f"инцидентов до отказа: {s['incidents_before_fault']}")
    if s.get("incidents_in_run", 1) > 1:
        notes.append(f"инцидентов за прогон: {s['incidents_in_run']}")
    failovers = (inc.get("details") or {}).get("failovers")
    if failovers and failovers > 1:
        notes.append(f"failover'ов в инциденте: {failovers}")
    if s["dr_mode"] == "active" and inc and inc.get("rto_seconds") is None:
        notes.append("active без failover")
    if inc and inc.get("peak_state") != "down":
        notes.append(f"пик инцидента {inc.get('peak_state')}, не down")
    # 99 — нарушены пороги steady.js (SLO): при отказе это ожидаемо, не сбой прогона.
    if meta["k6"]["exit_code"] not in (0, 99):
        notes.append(f"k6 завершился с кодом {meta['k6']['exit_code']}")
    return {
        "run_id": s["run_id"],
        "dr_mode": s["dr_mode"],
        "repeat": int(m.group(1)) if m else None,
        "ttd_s": inc.get("ttd_seconds"),
        "rto_s": inc.get("rto_seconds"),
        "return_s": inc.get("return_seconds"),
        "lost_requests": s["lost_requests"],
        "fault_requests": fault["requests"],
        "fault_failed": fault["failed"],
        "fault_availability": fault["availability"],
        "fault_p95_ms": fault["p95_ms"],
        "run_availability": 1 - s["requests"]["failed"] / s["requests"]["total"],
        "notes": notes,
    }


def agg(values: list[float]) -> dict | None:
    if not values:
        return None
    return {"n": len(values), "mean": mean(values), "min": min(values), "max": max(values)}


def reduction(with_dr: float | None, without_dr: float | None) -> float | None:
    if with_dr is None or not without_dr:
        return None
    return 1 - with_dr / without_dr


def pct(v: float | None, digits: int = 2) -> str:
    return "—" if v is None else f"{v * 100:.{digits}f}%"


def num(v: float | None, digits: int = 1) -> str:
    return "—" if v is None else f"{v:.{digits}f}"


def span(a: dict | None, fmt, unit: str = "") -> str:
    if not a:
        return "—"
    return f"{fmt(a['mean'])}{unit} (мин {fmt(a['min'])}, макс {fmt(a['max'])}, n={a['n']})"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path)
    ap.add_argument("runs", nargs="+", type=Path)
    args = ap.parse_args()
    runs = [load(r) for r in args.runs]

    by_mode = {}
    for mode in ("active", "observe"):
        rs = [r for r in runs if r["dr_mode"] == mode]
        by_mode[mode] = {
            key: agg([r[key] for r in rs if r[key] is not None])
            for key in (
                "ttd_s",
                "rto_s",
                "return_s",
                "lost_requests",
                "fault_availability",
                "fault_p95_ms",
                "run_availability",
            )
        }
    la, lo = by_mode["active"]["lost_requests"], by_mode["observe"]["lost_requests"]
    fa, fo = by_mode["active"]["fault_availability"], by_mode["observe"]["fault_availability"]
    ttd_all = [r["ttd_s"] for r in runs]
    rto_active = [r["rto_s"] for r in runs if r["dr_mode"] == "active"]
    result = {
        "runs": runs,
        "by_mode": by_mode,
        "lost_requests_reduction": reduction(la and la["mean"], lo and lo["mean"]),
        "fault_availability_gain": None if not (fa and fo) else fa["mean"] - fo["mean"],
        "targets": {
            "ttd_lt_60s_all": bool(ttd_all)
            and all(t is not None and t < TTD_TARGET_S for t in ttd_all),
            "rto_lt_300s_all_active": bool(rto_active)
            and all(t is not None and t < RTO_TARGET_S for t in rto_active),
        },
    }
    if args.json:
        args.json.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")

    out = [
        "| прогон | DR | TTD, с | RTO, с | возврат, с | потеряно | доступность отказа "
        "| p95 отказа, мс | доступность прогона |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in runs:
        out.append(
            f"| {r['run_id']} | {r['dr_mode']} | {num(r['ttd_s'])} | {num(r['rto_s'])} "
            f"| {num(r['return_s'])} | {r['lost_requests']} "
            f"| {pct(r['fault_availability'])} ({r['fault_failed']}/{r['fault_requests']}) "
            f"| {num(r['fault_p95_ms'])} | {pct(r['run_availability'])} |"
        )
    out.append("")
    for mode, a in by_mode.items():
        out.append(
            f"- {mode}: TTD {span(a['ttd_s'], num, ' с')}; RTO {span(a['rto_s'], num, ' с')}; "
            f"потеряно {span(a['lost_requests'], lambda v: f'{v:.0f}')}; "
            f"доступность отказа {span(a['fault_availability'], pct)}"
        )
    gain = result["fault_availability_gain"]
    out.append(
        f"- потерянных запросов с DR меньше на {pct(result['lost_requests_reduction'], 1)}; "
        f"доступность в окне отказа выше на {num(None if gain is None else gain * 100, 2)} п.п."
    )
    t = result["targets"]
    out.append(
        f"- TTD < {TTD_TARGET_S} с во всех прогонах: {'да' if t['ttd_lt_60s_all'] else 'нет'}; "
        f"RTO < {RTO_TARGET_S} с во всех active: {'да' if t['rto_lt_300s_all_active'] else 'нет'}"
    )
    for r in runs:
        for msg in r["notes"]:
            out.append(f"- {r['run_id']}: {msg}")
    print("\n".join(out))


if __name__ == "__main__":
    main()
