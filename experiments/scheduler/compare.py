"""Сравнение прогонов сценария 2 (ADR 0031): таблица по прогонам и приёмочный показатель
`снижение = 1 − mean(ER_meta) / mean(ER_naive)` по error rate потока в окне отказа
(клиентский ряд k6). Бракованные прогоны (validity.valid = false) показываются, но в
средние не входят.

    python compare.py [--json <путь>] <каталог прогона>...

Markdown — в stdout, числа — в JSON. Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from statistics import mean

TARGET = 0.20  # spec §1.3


def load(run_dir: Path) -> dict:
    s = json.loads((run_dir / "summary.json").read_text())
    fault = s["client"]["stream"]["fault"]
    server = (s.get("server") or {}).get("stream", {}).get("fault", {})
    pl = s["placement"]
    m = re.search(r"-r(\d+)$", s["run_id"])
    return {
        "run_id": s["run_id"],
        "scheduler": s["scheduler"],
        "repeat": int(m.group(1)) if m else None,
        "valid": s["validity"]["valid"],
        "reasons": s["validity"]["reasons"],
        "notes": s["validity"]["notes"],
        "error_rate": fault["error_rate"],
        "failed": fault["failed"],
        "requests": fault["requests"],
        "p95_ms": fault["p95_ms"],
        "server_error_rate": server.get("error_rate"),
        "placements": pl["fault"]["placements"],
        "to_degraded": pl["fault"]["to_degraded"],
        "degraded_s": pl["degraded_s"],
        "score_loss_s": pl["score_loss_s"],
        "first_avoid_s": pl["first_avoid_s"],
    }


def agg(values: list[float]) -> dict | None:
    if not values:
        return None
    return {"n": len(values), "mean": mean(values), "min": min(values), "max": max(values)}


def reduction(meta_er: float | None, naive_er: float | None) -> float | None:
    if meta_er is None or not naive_er:
        return None  # ADR 0031 п. 4: без ошибок у наивного показатель не определён
    return 1 - meta_er / naive_er


def pct(v: float | None, digits: int = 2) -> str:
    return "—" if v is None else f"{v * 100:.{digits}f}%"


def num(v: float | None, digits: int = 1) -> str:
    return "—" if v is None else f"{v:.{digits}f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path)
    ap.add_argument("runs", nargs="+", type=Path)
    args = ap.parse_args()
    runs = [load(r) for r in args.runs]

    by_mode = {}
    for mode in ("meta", "naive"):
        ok = [
            r for r in runs if r["scheduler"] == mode and r["valid"] and r["error_rate"] is not None
        ]
        by_mode[mode] = {
            "error_rate": agg([r["error_rate"] for r in ok]),
            "server_error_rate": agg(
                [r["server_error_rate"] for r in ok if r["server_error_rate"] is not None]
            ),
            "p95_ms": agg([r["p95_ms"] for r in ok if r["p95_ms"] is not None]),
            "share_to_degraded": agg(
                [r["to_degraded"] / r["placements"] for r in ok if r["placements"]]
            ),
        }
    em, en = by_mode["meta"]["error_rate"], by_mode["naive"]["error_rate"]
    total = reduction(em and em["mean"], en and en["mean"])
    pairs = []
    for r in runs:
        if r["scheduler"] != "meta" or not r["valid"]:
            continue
        naive = [n for n in runs if n["scheduler"] == "naive" and n["valid"]]
        twin = next((n for n in naive if n["repeat"] == r["repeat"]), None)
        if twin:
            pairs.append(
                {
                    "repeat": r["repeat"],
                    "reduction": reduction(r["error_rate"], twin["error_rate"]),
                }
            )
    result = {
        "runs": runs,
        "by_mode": by_mode,
        "reduction": total,
        "absolute_difference": None if not (em and en) else en["mean"] - em["mean"],
        "pairs": pairs,
        "target": TARGET,
        "meets_target": None if total is None else total >= TARGET,
    }
    if args.json:
        args.json.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")

    out = [
        "| прогон | режим | годен | ER отказа (k6) | ER (сервер) | p95, мс | "
        "в деградир. | degraded, с | score_loss, с | first_avoid, с |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in runs:
        out.append(
            f"| {r['run_id']} | {r['scheduler']} | {'да' if r['valid'] else 'нет'} "
            f"| {pct(r['error_rate'])} ({r['failed']}/{r['requests']}) "
            f"| {pct(r['server_error_rate'])} | {num(r['p95_ms'])} "
            f"| {r['to_degraded']}/{r['placements']} | {num(r['degraded_s'])} "
            f"| {num(r['score_loss_s'])} | {num(r['first_avoid_s'])} |"
        )
    out.append("")
    for mode, a in by_mode.items():
        e = a["error_rate"]
        if e:
            out.append(
                f"- {mode}: ER {pct(e['mean'])} (мин {pct(e['min'])}, макс {pct(e['max'])}, "
                f"n={e['n']})"
            )
        else:
            out.append(f"- {mode}: годных прогонов нет")
    pair_txt = ", ".join(f"r{p['repeat']}: {pct(p['reduction'], 1)}" for p in pairs) or "—"
    out.append(
        f"- снижение error rate: {pct(total, 1)} (цель ≥ {pct(TARGET, 0)}); по парам: {pair_txt}"
    )
    for r in runs:
        for msg in r["reasons"]:
            out.append(f"- брак {r['run_id']}: {msg}")
        for msg in r["notes"]:
            out.append(f"- {r['run_id']}: {msg}")
    print("\n".join(out))


if __name__ == "__main__":
    main()
