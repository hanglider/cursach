"""Графики деградации и восстановления задачи 8 (spec §5): доступность app.fed.local в
сценарии 1 (DR active против observe) и error rate потока в сценарии 2 (meta против naive)
по секундам относительно внесения отказа — события Apply в Chaos Mesh (инвариант 7).
Линия — среднее трёх повторов, полоса — от минимума до максимума, окно отказа закрашено.

    make plots
    uv run --no-project --python 3.13 --with matplotlib==3.11.2 python analysis/plots.py \
        [--s1 task8-s1] [--s2 task8-s2]

Вход — per-second.csv и summary.json прогонов в experiments/results/, выход — PNG и SVG в
analysis/scenario1/ и analysis/scenario2/. Стенд не нужен.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from statistics import mean

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "experiments" / "results"
BEFORE, AFTER = 180, 600  # baseline и отказ + восстановление, с (ADR 0008)

# Палитра — первые два категориальных слота (синий, оранжевый), проверены на CVD и контраст
# к поверхности; синий — вариант с механизмом, оранжевый — базовый.
SERIES = {"with": "#2a78d6", "base": "#eb6834"}
INK, INK_2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, FAULT_BAND, SURFACE = "#e1e0d9", "#c3c2b7", "#f0efec", "#fcfcfb"


def epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def per_second(run: Path, req_col: str, fail_col: str) -> dict[int, tuple[int, int]]:
    with (run / "per-second.csv").open(newline="") as f:
        return {
            int(r["second"]): (int(r[req_col]), int(r[fail_col]))
            for r in csv.DictReader(f)
        }


def trailing_ratio(
    data: dict[int, tuple[int, int]], t0: float, window: int
) -> list[float | None]:
    """Доля неуспешных за скользящее окно `window` с, заканчивающееся в t (t от внесения)."""
    out = []
    for t in range(-BEFORE, AFTER + 1):
        req = fail = 0
        for s in range(int(t0) + t - window + 1, int(t0) + t + 1):
            r, f = data.get(s, (0, 0))
            req, fail = req + r, fail + f
        out.append(fail / req if req else None)
    return out


def envelope(series: list[list[float | None]]) -> tuple[list, list, list]:
    lo, hi, avg = [], [], []
    for values in zip(*series, strict=True):
        v = [x for x in values if x is not None]
        lo.append(min(v) if v else float("nan"))
        hi.append(max(v) if v else float("nan"))
        avg.append(mean(v) if v else float("nan"))
    return lo, hi, avg


def style(ax, ylabel: str) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(colors=MUTED, length=0, labelsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.set_xlim(-BEFORE, AFTER)
    ax.set_xticks(range(-180, AFTER + 1, 60))
    ax.set_xlabel("время от внесения отказа (Chaos Mesh), с", color=INK_2, fontsize=9)
    ax.set_ylabel(ylabel, color=INK_2, fontsize=9)
    ax.axvspan(0, 300, color=FAULT_BAND, zorder=0, linewidth=0)


def event(ax, x: float, text: str, y: float, line_top: float) -> None:
    """Отметка события: линия до line_top (в единицах оси y), подпись справа от неё на y."""
    ax.axvline(
        x, ymax=line_top / ax.get_ylim()[1], color=MUTED, linewidth=0.8, zorder=1
    )
    ax.annotate(
        text,
        (x, y),
        xytext=(4, 0),
        textcoords="offset points",
        ha="left",
        va="center",
        fontsize=8.5,
        color=INK_2,
        linespacing=1.3,
    )


def plot_modes(ax, modes: dict, scale: float) -> None:
    t = list(range(-BEFORE, AFTER + 1))
    for key, (label, series) in modes.items():
        lo, hi, avg = envelope(series)
        color = SERIES[key]
        ax.fill_between(
            t,
            [v * scale for v in lo],
            [v * scale for v in hi],
            color=color,
            alpha=0.14,
            linewidth=0,
            zorder=2,
        )
        ax.plot(
            t,
            [v * scale for v in avg],
            color=color,
            linewidth=2,
            solid_joinstyle="round",
            solid_capstyle="round",
            label=label,
            zorder=3,
        )


def save(fig, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        out.with_suffix(".png"), dpi=200, facecolor=SURFACE, metadata={"Software": None}
    )
    fig.savefig(out.with_suffix(".svg"), facecolor=SURFACE, metadata={"Date": None})
    plt.close(fig)
    print(
        out.with_suffix(".png").relative_to(ROOT),
        out.with_suffix(".svg").relative_to(ROOT),
    )


def scenario1(prefix: str, window: int = 5) -> None:
    modes, events = {}, {"ttd": [], "rto": [], "back": []}
    for key, mode, label in (
        ("with", "active", "с DR (active)"),
        ("base", "observe", "без DR (observe)"),
    ):
        runs = sorted(RESULTS.glob(f"{prefix}-{mode}-r*"))
        series = []
        for run in runs:
            s = json.loads((run / "summary.json").read_text())
            t0 = epoch(s["fault"]["injected_at"])
            err = trailing_ratio(per_second(run, "requests", "failed"), t0, window)
            series.append([None if e is None else 1 - e for e in err])
            inc = s["incident"]
            if mode == "active":
                events["ttd"].append(epoch(inc["detected_at"]) - t0)
                events["rto"].append(epoch(inc["failover_completed_at"]) - t0)
                events["back"].append(epoch(inc["recovery_completed_at"]) - t0)
        modes[key] = (f"{label}, n={len(runs)}", series)

    fig, ax = plt.subplots(figsize=(10, 4.6), facecolor=SURFACE)
    style(ax, f"доступность app.fed.local, % (окно {window} с)")
    plot_modes(ax, modes, 100)
    ax.set_ylim(0, 108)
    ttd, rto, back = (mean(events[k]) for k in ("ttd", "rto", "back"))
    event(ax, ttd, f"обнаружение, {ttd:.0f} с", 20, 100)
    event(ax, rto, f"failover завершён (RTO), {rto:.0f} с", 12, 100)
    event(ax, back, f"трафик возвращён, {back:.0f} с", 20, 100)
    ax.text(
        150,
        104,
        "PodChaos на workloads cluster-b, 300 с",
        ha="center",
        fontsize=8.5,
        color=MUTED,
    )
    ax.legend(loc="lower right", frameon=False, fontsize=9, labelcolor=INK)
    fig.suptitle(
        "Сценарий 1: отказ кластера — доступность с DR и без",
        x=0.07,
        ha="left",
        color=INK,
        fontsize=12,
    )
    ax.set_title(
        "50 RPS в app.fed.local; линия — среднее повторов, полоса — мин…макс; отметки — среднее по active",
        loc="left",
        color=INK_2,
        fontsize=9,
    )
    fig.tight_layout()
    save(fig, ROOT / "analysis" / "scenario1" / f"{prefix}-availability")


def scenario2(prefix: str, window: int = 15) -> None:
    modes, avoid = {}, []
    for key, mode, label in (
        ("with", "meta", "meta-scheduler"),
        ("base", "naive", "наивный (round-robin)"),
    ):
        runs = sorted(RESULTS.glob(f"{prefix}-{mode}-r*"))
        series = []
        for run in runs:
            s = json.loads((run / "summary.json").read_text())
            t0 = epoch(s["fault"]["injected_at"])
            series.append(
                trailing_ratio(
                    per_second(run, "stream_requests", "stream_failed"), t0, window
                )
            )
            if mode == "meta" and s["placement"]["first_avoid_s"] is not None:
                avoid.append(s["placement"]["first_avoid_s"])
        modes[key] = (f"{label}, n={len(runs)}", series)

    fig, ax = plt.subplots(figsize=(10, 4.6), facecolor=SURFACE)
    style(ax, f"error rate потока, % (окно {window} с)")
    plot_modes(ax, modes, 100)
    top = max(ax.get_ylim()[1], 10)  # максимум полосы; сверху — место под подписи
    ax.set_ylim(0, top * 1.3)
    ax.text(
        150,
        top * 1.24,
        "NetworkChaos 500 мс / 10% на backend cluster-b, 300 с",
        ha="center",
        fontsize=8.5,
        color=MUTED,
    )
    if avoid:
        a = mean(avoid)
        event(
            ax,
            a,
            f"meta перестаёт размещать в cluster-b, {a:.0f} с",
            top * 1.12,
            top * 1.17,
        )
    # После TTL в cluster-b не остаётся workload'ов с трафиком ни в одном режиме: размещённые
    # до отказа истекли, размещённые во время отказа не проходят готовность (ADR 0031).
    event(
        ax,
        180,
        "TTL 180 с: workload'ы, размещённые до отказа, истекли;\n"
        "размещённые в cluster-b во время отказа не готовы —\n"
        "трафика в cluster-b нет ни в одном режиме",
        top * 0.88,
        top * 1.0,
    )
    ax.legend(loc="center right", frameon=False, fontsize=9, labelcolor=INK)
    fig.suptitle(
        "Сценарий 2: деградация сети — error rate потока workload'ов",
        x=0.07,
        ha="left",
        color=INK,
        fontsize=12,
    )
    ax.set_title(
        "50 RPS в поток (k6, клиентский ряд); линия — среднее повторов, полоса — мин…макс",
        loc="left",
        color=INK_2,
        fontsize=9,
    )
    fig.tight_layout()
    save(fig, ROOT / "analysis" / "scenario2" / f"{prefix}-error-rate")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--s1", default="task8-s1", help="префикс серии сценария 1")
    ap.add_argument("--s2", default="task8-s2", help="префикс серии сценария 2")
    args = ap.parse_args()
    plt.rcParams.update({"svg.hashsalt": "cursach", "font.family": "DejaVu Sans"})
    scenario1(args.s1)
    scenario2(args.s2)


if __name__ == "__main__":
    main()
