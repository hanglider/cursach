"""Задача 9 (spec §5, ADR 0035): количественные результаты по гипотезам H1–H5 из данных серий
задачи 8, анализ чувствительности к весам scoring (ADR 0006) и сверка каталога с Thanos
(ADR 0025, п. 10).

    make analysis
    uv run --no-project --python 3.13 --with matplotlib==3.11.2 python analysis/task9.py

Вход — experiments/results/ (серии task8-s1, task8-s2, task8-s3), выход —
analysis/task9/results.{md,json} и sensitivity.{png,svg}. Стенд не нужен.
"""

from __future__ import annotations

import csv
import json
import math
from itertools import pairwise, product
from pathlib import Path
from statistics import mean

from scoring import BASE_WEIGHTS, choice, load_decisions, parse_ts

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "experiments" / "results"
OUT = ROOT / "analysis" / "task9"
S1, S2, S3 = "task8-s1", "task8-s2c", "task8-s3"
# Первая серия сценария 2: backend убивался по liveness под netem (ADR 0037) — отдельный
# результат об усилении отказа, в приёмку H4 и H5 не входит.
S2_AMPLIFIED = "task8-s2"
REPEATS = (1, 2, 3)

AVAILABILITY_TARGET = 0.99  # spec §1.3
P95_TARGET_MS = 200
TTD_TARGET_S, RTO_TARGET_S = 60, 300
REDUCTION_TARGET = 0.20
SECONDARY_WINDOW_S = 180  # ADR 0035, п. 4: TTL workload'ов потока


def load(path: Path):
    return json.loads(path.read_text())


def run(prefix: str, mode: str, i: int) -> Path:
    return RESULTS / f"{prefix}-{mode}-r{i}"


def epoch(iso: str) -> float:
    return parse_ts(iso).timestamp()


def verdict(parts: dict[str, bool]) -> str:
    if all(parts.values()):
        return "подтверждена"
    if any(parts.values()):
        return "частично"
    return "не подтверждена"


# --- ряды экспорта --------------------------------------------------------------------


def series(run_dir: Path, name: str) -> list[dict]:
    path = run_dir / "series" / f"{name}.json"
    return load(path)["data"]["result"] if path.exists() else []


def increase(values: list, t0: float, t1: float) -> float:
    """Прирост счётчика за [t0, t1] с учётом сбросов (как increase в PromQL, без экстраполяции)."""
    pts = [
        (float(t), float(v)) for t, v in values if t0 <= float(t) <= t1 and v != "NaN"
    ]
    total = 0.0
    for (_, a), (_, b) in pairwise(pts):
        total += b - a if b >= a else b
    return total


def value_at(values: list, t: float, tolerance: float = 5.0) -> float | None:
    best = min(values, key=lambda p: abs(float(p[0]) - t), default=None)
    if best is None or abs(float(best[0]) - t) > tolerance or best[1] == "NaN":
        return None
    return float(best[1])


# --- H1: штатный режим и алерты -------------------------------------------------------


def fired_alerts(run_dir: Path, t0: float, t1: float) -> dict[str, str]:
    """alertname → лучший достигнутый в окне статус (firing важнее pending)."""
    out: dict[str, str] = {}
    for s in series(run_dir, "alerts"):
        if any(t0 <= float(t) <= t1 and v == "1" for t, v in s["values"]):
            name, state = s["metric"]["alertname"], s["metric"].get("alertstate", "")
            if out.get(name) != "firing":
                out[name] = state
    return out


def sli_views_s1(run_dir: Path, summary: dict) -> dict:
    """Доля ошибок за окно отказа тремя рядами: клиент (k6), шлюз (Envoy), сервер (frontend)."""
    t0, t1 = (
        epoch(summary["fault"]["injected_at"]),
        epoch(summary["fault"]["recovered_at"]),
    )
    w = summary["windows"]["fault"]
    gw = {"2": 0.0, "5": 0.0}
    for s in series(run_dir, "envoy_upstream_rq"):
        m = s["metric"]
        if m.get("__name__") == "envoy_cluster_upstream_rq_xx" and m.get(
            "envoy_cluster_name", ""
        ).startswith("httproute/gateway/app/"):
            cls = m.get("envoy_response_code_class")
            if cls in gw:
                gw[cls] += increase(s["values"], t0, t1)
    srv_total = srv_err = 0.0
    for s in series(run_dir, "http_requests_total"):
        m = s["metric"]
        if m.get("tier") == "frontend" and m.get("workload") == "app":
            inc = increase(s["values"], t0, t1)
            srv_total += inc
            if str(m.get("status", "")).startswith("5"):
                srv_err += inc
    gw_total = gw["2"] + gw["5"]
    return {
        "client": w["failed"] / w["requests"] if w["requests"] else None,
        "gateway": gw["5"] / gw_total if gw_total else None,
        "server": srv_err / srv_total if srv_total else None,
    }


SLO_TARGET = 0.99
MONTH_S = 30 * 86400
FAST_BURN, FAST_WINDOW_S = 14.4, 3600  # правило FederationErrorBudgetBurnFast


def gateway_view(run_dir: Path, summary: dict) -> dict:
    """SLI шлюза офлайн по выгруженным счётчикам Envoy (ADR 0036, п. 5): все маршруты
    `httproute/*`, как в правилах Ruler. Burn rate за 5 мин — по скользящему окну с шагом
    экспорта; бюджет месяца — при фактической нагрузке прогона."""
    t0, t1 = (
        epoch(summary["fault"]["injected_at"]),
        epoch(summary["fault"]["recovered_at"]),
    )
    total, errors, stamps = [], [], set()
    for s in series(run_dir, "envoy_upstream_rq"):
        m = s["metric"]
        if m.get("__name__") != "envoy_cluster_upstream_rq_xx":
            continue
        if not m.get("envoy_cluster_name", "").startswith("httproute/"):
            continue
        total.append(s["values"])
        if m.get("envoy_response_code_class") == "5":
            errors.append(s["values"])
        stamps.update(float(t) for t, _ in s["values"])
    if not total:
        return {}

    def inc(group, a, b):
        return sum(increase(v, a, b) for v in group)

    start = min(stamps)
    burns = []
    for t in sorted(x for x in stamps if t0 <= x <= t1 + 300 and x - 300 >= start):
        req = inc(total, t - 300, t)
        if req:
            burns.append(inc(errors, t - 300, t) / req / (1 - SLO_TARGET))
    fault_req, fault_err = inc(total, t0, t1), inc(errors, t0, t1)
    run_req = inc(total, start, max(stamps))
    rps = run_req / (max(stamps) - start)
    return {
        "fault_error_share": fault_err / fault_req if fault_req else None,
        "fault_errors": fault_err,
        "burn_5m_max": max(burns) if burns else None,
        "budget_month_share": fault_err / ((1 - SLO_TARGET) * rps * MONTH_S),
        # fast-burn требует > 14.4% ошибок за час при непрерывной нагрузке
        "fast_burn_errors_needed": FAST_BURN * (1 - SLO_TARGET) * rps * FAST_WINDOW_S,
        "rps": rps,
    }


def h1() -> dict:
    rows = []
    for prefix, modes in ((S1, ("active", "observe")), (S2, ("meta", "naive"))):
        for mode, i in product(modes, REPEATS):
            d = run(prefix, mode, i)
            s = load(d / "summary.json")
            if prefix == S1:
                b = s["windows"]["baseline"]
                avail, p95, target = b["availability"], b["p95_ms"], "app.fed.local"
            else:
                b = s["client"]["stream"]["baseline"]
                avail, p95, target = 1 - b["error_rate"], b["p95_ms"], "поток"
            t0, t1 = epoch(s["fault"]["injected_at"]), epoch(s["fault"]["recovered_at"])
            rows.append(
                {
                    "run_id": d.name,
                    "target": target,
                    "baseline_availability": avail,
                    "baseline_p95_ms": p95,
                    # алерт с for: до 15 мин может загореться после снятия отказа
                    "alerts": fired_alerts(d, t0, t1 + 900),
                    "sli_fault_error_share": sli_views_s1(d, s)
                    if prefix == S1
                    else None,
                    "gateway": gateway_view(d, s),
                }
            )
    burn_fired = [
        any(
            k.startswith("FederationErrorBudgetBurn") and v == "firing"
            for k, v in r["alerts"].items()
        )
        for r in rows
    ]
    parts = {
        "availability ≥ 99% в baseline каждого прогона": all(
            r["baseline_availability"] >= AVAILABILITY_TARGET for r in rows
        ),
        "p95 < 200 мс в baseline каждого прогона": all(
            r["baseline_p95_ms"] < P95_TARGET_MS for r in rows
        ),
        "burn-rate алерт срабатывает при отказе": all(burn_fired),
    }
    return {
        "runs": rows,
        "min_baseline_availability": min(r["baseline_availability"] for r in rows),
        "max_baseline_p95_ms": max(r["baseline_p95_ms"] for r in rows),
        "burn_alert_fired_runs": sum(burn_fired),
        "gateway_burn_5m_max": max(
            (r["gateway"]["burn_5m_max"] or 0) for r in rows if r["gateway"]
        ),
        "gateway_budget_month_share_max": max(
            r["gateway"]["budget_month_share"] for r in rows if r["gateway"]
        ),
        "parts": parts,
        "verdict": verdict(parts),
    }


# --- H2: DR -----------------------------------------------------------------------------


def h2() -> dict:
    rows = {}
    for mode, i in product(("active", "observe"), REPEATS):
        s = load(run(S1, mode, i) / "summary.json")
        inc = s.get("incident") or {}
        rows[(mode, i)] = {
            "run_id": s["run_id"],
            "ttd_s": inc.get("ttd_seconds"),
            "rto_s": inc.get("rto_seconds"),
            "lost": s["lost_requests"],
            "fault_availability": s["windows"]["fault"]["availability"],
        }
    act = [rows[("active", i)] for i in REPEATS]
    obs = [rows[("observe", i)] for i in REPEATS]
    pairs = [
        {
            "repeat": i,
            "lost_active": rows[("active", i)]["lost"],
            "lost_observe": rows[("observe", i)]["lost"],
            "reduction": 1 - rows[("active", i)]["lost"] / rows[("observe", i)]["lost"],
        }
        for i in REPEATS
    ]
    parts = {
        "TTD < 60 с в каждом прогоне": all(
            r["ttd_s"] is not None and r["ttd_s"] < TTD_TARGET_S for r in act + obs
        ),
        "RTO < 5 мин в каждом active": all(
            r["rto_s"] is not None and r["rto_s"] < RTO_TARGET_S for r in act
        ),
        "потерянных с DR меньше в каждой паре": all(p["reduction"] > 0 for p in pairs),
    }
    return {
        "runs": list(rows.values()),
        "mean": {
            "ttd_s": mean(r["ttd_s"] for r in act + obs),
            "rto_s": mean(r["rto_s"] for r in act),
            "lost_active": mean(r["lost"] for r in act),
            "lost_observe": mean(r["lost"] for r in obs),
            "fault_availability_active": mean(r["fault_availability"] for r in act),
            "fault_availability_observe": mean(r["fault_availability"] for r in obs),
        },
        "lost_reduction": 1
        - mean(r["lost"] for r in act) / mean(r["lost"] for r in obs),
        "pairs": pairs,
        "parts": parts,
        "verdict": verdict(parts),
    }


# --- H3: политики -----------------------------------------------------------------------


def h3() -> dict:
    sets = {}
    for name, mode in (
        ("enforce", "enforce"),
        ("audit", "audit"),
        ("audit-hold", "audit-hold"),
    ):
        rs = [load(run(S3, mode, i) / "summary.json") for i in REPEATS]
        sets[name] = {
            "attempts": sum(r["attempts"] for r in rs),
            "rejected": sum(r["rejected"] for r in rs),
            "admitted": sum(r["admitted"] for r in rs),
            "recorded": sum(r["recorded"] for r in rs),
            "runs": rs,
        }
    parts = {
        "enforce: 0 размещённых нарушителей в каждом прогоне": all(
            r["admitted"] == 0 for r in sets["enforce"]["runs"]
        ),
        "enforce: каждый отказ зафиксирован в каталоге": all(
            r["recorded"] >= r["rejected"] for r in sets["enforce"]["runs"]
        ),
        "audit: нарушители размещаются и фиксируются в каждом прогоне": all(
            r["admitted"] == r["attempts"] and r["recorded"] >= r["admitted"]
            for r in sets["audit"]["runs"] + sets["audit-hold"]["runs"]
        ),
    }
    for s in sets.values():
        s.pop("runs")
    return {"sets": sets, "parts": parts, "verdict": verdict(parts)}


# --- H4: планировщик --------------------------------------------------------------------


def window_error_rate(run_dir: Path, t0: float, t1: float) -> float | None:
    req = fail = 0
    with (run_dir / "per-second.csv").open(newline="") as f:
        for r in csv.DictReader(f):
            if t0 <= int(r["second"]) < t1:
                req += int(r["stream_requests"])
                fail += int(r["stream_failed"])
    return fail / req if req else None


def h4(prefix: str = S2) -> dict:
    rows = {}
    for mode, i in product(("meta", "naive"), REPEATS):
        d = run(prefix, mode, i)
        s = load(d / "summary.json")
        t0 = epoch(s["fault"]["injected_at"])
        gen = s["generator"]
        rows[(mode, i)] = {
            "run_id": s["run_id"],
            "er_fault": s["client"]["stream"]["fault"]["error_rate"],
            "er_first_180s": window_error_rate(d, t0, t0 + SECONDARY_WINDOW_S),
            "placed": sum(gen.get("placed", {}).values()),
            "not_ready": sum(gen.get("not_ready", {}).values()),
        }

    def red(key: str, i: int | None = None) -> float:
        if i is None:
            m = mean(rows[("meta", j)][key] for j in REPEATS)
            n = mean(rows[("naive", j)][key] for j in REPEATS)
        else:
            m, n = rows[("meta", i)][key], rows[("naive", i)][key]
        return 1 - m / n

    pairs = [{"repeat": i, "reduction": red("er_fault", i)} for i in REPEATS]
    not_ready = {
        mode: {
            "not_ready": sum(rows[(mode, i)]["not_ready"] for i in REPEATS),
            "placed": sum(rows[(mode, i)]["placed"] for i in REPEATS),
        }
        for mode in ("meta", "naive")
    }
    parts = {
        "снижение ER ≥ 20% на среднем": red("er_fault") >= REDUCTION_TARGET,
        "ER meta ниже naive в каждой паре": all(p["reduction"] > 0 for p in pairs),
    }
    return {
        "runs": list(rows.values()),
        "mean_er_fault": {
            m: mean(rows[(m, i)]["er_fault"] for i in REPEATS)
            for m in ("meta", "naive")
        },
        "reduction": red("er_fault"),
        "pairs": pairs,
        "secondary": {
            "window_s": SECONDARY_WINDOW_S,
            "mean_er": {
                m: mean(rows[(m, i)]["er_first_180s"] for i in REPEATS)
                for m in ("meta", "naive")
            },
            "reduction": red("er_first_180s"),
            "not_ready": not_ready,
        },
        "parts": parts,
        "verdict": verdict(parts),
    }


# --- H5: чувствительность к весам (ADR 0006, 0035) -------------------------------------


def weight_grid(step: int = 10) -> list[tuple[float, float, float]]:
    return [
        (a / step, b / step, (step - a - b) / step)
        for a in range(step + 1)
        for b in range(step + 1 - a)
    ]


def h5() -> dict:
    decisions, naive_hits, naive_total = [], 0, 0
    for mode, i in product(("meta", "naive"), REPEATS):
        d = run(S2, mode, i)
        s = load(d / "summary.json")
        t0, t1 = epoch(s["fault"]["injected_at"]), epoch(s["fault"]["recovered_at"])
        degraded = s["cluster"]
        for dec in load_decisions(d):
            if dec.outcome != "placed" or not dec.candidates:
                continue
            in_fault = t0 <= dec.decided_at.timestamp() <= t1
            decisions.append((dec, in_fault, degraded, mode))
            if mode == "naive" and in_fault:
                naive_total += 1
                naive_hits += dec.chosen == degraded
    naive_share = naive_hits / naive_total
    base = [choice(d, BASE_WEIGHTS) for d, *_ in decisions]
    points = []
    for w in weight_grid():
        changed, deg = [], []
        for (d, in_fault, degraded, _), b in zip(decisions, base, strict=True):
            c = choice(d, w)
            changed.append(
                1 - sum(min(c.get(k, 0), b.get(k, 0)) for k in set(c) | set(b))
            )
            if in_fault:
                deg.append(c.get(degraded, 0.0))
        points.append(
            {
                "weights": w,
                "changed_share": mean(changed),
                "degraded_share_fault": mean(deg),
            }
        )
    dynamic = [p for p in points if p["weights"][0] + p["weights"][1] > 1e-9]
    base_pt = next(
        p
        for p in points
        if all(abs(a - b) < 1e-9 for a, b in zip(p["weights"], BASE_WEIGHTS))
    )
    parts = {
        "доля в деградировавший ниже naive во всех точках с w1 + w2 > 0": all(
            p["degraded_share_fault"] < naive_share for p in dynamic
        )
    }
    return {
        "decisions": len(decisions),
        "decisions_in_fault": sum(1 for _, f, *_ in decisions if f),
        "naive_share_fault": naive_share,
        "base": base_pt,
        "worst_dynamic": max(dynamic, key=lambda p: p["degraded_share_fault"]),
        "points_below_naive": sum(
            p["degraded_share_fault"] < naive_share for p in dynamic
        ),
        "points_dynamic": len(dynamic),
        "max_changed_share": max(p["changed_share"] for p in points),
        "grid": points,
        "parts": parts,
        "verdict": verdict(parts),
    }


def plot_sensitivity(r: dict) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap

    # Последовательная шкала — синий ramp референсной палитры (100 → 700).
    ramp = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
    cmap = LinearSegmentedColormap.from_list("blue", ramp)
    ink, ink2, muted, surface = "#0b0b0b", "#52514e", "#898781", "#fcfcfb"
    h = math.sqrt(3) / 2

    def xy(w):  # w1 — левый нижний угол, w2 — правый нижний, w3 — верх
        return w[1] + w[2] / 2, w[2] * h

    plt.rcParams.update({"svg.hashsalt": "cursach", "font.family": "DejaVu Sans"})
    fig, ax = plt.subplots(figsize=(7.2, 6.6), facecolor=surface)
    ax.set_facecolor(surface)
    pts = r["grid"]
    xs, ys = zip(*(xy(p["weights"]) for p in pts), strict=True)
    vals = [p["degraded_share_fault"] * 100 for p in pts]
    sc = ax.scatter(
        xs, ys, c=vals, cmap=cmap, vmin=0, vmax=100, s=620, marker="h", linewidths=0
    )
    for p, x, y in zip(pts, xs, ys, strict=True):
        v = p["degraded_share_fault"] * 100
        ax.text(
            x,
            y,
            f"{v:.0f}",
            ha="center",
            va="center",
            fontsize=6.5,
            color="#ffffff" if v > 45 else ink,
        )
    bx, by = xy(BASE_WEIGHTS)
    ax.scatter(
        [bx], [by], s=900, marker="h", facecolors="none", edgecolors=ink, linewidths=1.6
    )
    for text, (x, y), ha, va, dy in (
        ("w1 = 1\nerror rate", (0, 0), "center", "top", -10),
        ("w2 = 1\nlatency", (1, 0), "center", "top", -10),
        ("w3 = 1\nsaturation", (0.5, h), "center", "bottom", 12),
    ):
        ax.annotate(
            text,
            (x, y),
            xytext=(0, dy),
            textcoords="offset points",
            ha=ha,
            va=va,
            fontsize=9,
            color=ink2,
        )
    ax.set_xlim(-0.12, 1.12)
    ax.set_ylim(-0.16, h + 0.14)
    ax.set_aspect("equal")
    ax.axis("off")
    cb = fig.colorbar(sc, ax=ax, shrink=0.6, pad=0.02)
    cb.outline.set_visible(False)
    cb.ax.tick_params(colors=muted, labelsize=8, length=0)
    cb.set_label("размещений в деградировавший кластер, %", color=ink2, fontsize=9)
    cb.ax.yaxis.set_label_position("left")
    naive = r["naive_share_fault"] * 100
    cb.ax.axhline(naive, color=ink, linewidth=1.2)
    cb.set_ticks(
        [0, 20, 40, naive, 80, 100],
        labels=["0", "20", "40", f"naive {naive:.0f}", "80", "100"],
    )
    fig.suptitle(
        "Чувствительность к весам scoring: сценарий 2, окно отказа",
        x=0.04,
        ha="left",
        color=ink,
        fontsize=12,
    )
    ax.set_title(
        f"офлайн-пересчёт {r['decisions_in_fault']} решений серии task8-s2 (meta и naive), сетка "
        "весов с шагом 0.1;\nобведены базовые веса 0.5 / 0.3 / 0.2",
        loc="left",
        color=ink2,
        fontsize=8.5,
    )
    fig.tight_layout()
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        OUT / "sensitivity.png", dpi=200, facecolor=surface, metadata={"Software": None}
    )
    fig.savefig(OUT / "sensitivity.svg", facecolor=surface, metadata={"Date": None})
    plt.close(fig)


# --- сверка каталога с Thanos (ADR 0025, п. 10) ----------------------------------------


def reconcile() -> dict:
    er_diff, p95_rel, absent_ok, absent_bad = [], [], 0, 0
    for prefix, mode, i in product((S2, S2_AMPLIFIED), ("meta", "naive"), REPEATS):
        d = run(prefix, mode, i)
        by = {}
        for s in series(d, "cluster_sli"):
            by[(s["metric"]["__name__"], s["metric"]["cluster"])] = s["values"]
        with (d / "registry" / "cluster_metrics.csv").open(newline="") as f:
            for row in csv.DictReader(f):
                t = parse_ts(row["observed_at"]).timestamp()
                c = row["cluster_id"]
                for col, name, scale, out in (
                    ("error_rate", "cluster:http_error_rate:ratio5m", 1.0, er_diff),
                    (
                        "latency_p95_ms",
                        "cluster:http_request_duration_seconds:p95_5m",
                        1000.0,
                        p95_rel,
                    ),
                ):
                    reg = float(row[col]) if row[col] not in ("", None) else None
                    th = value_at(by.get((name, c), []), t)
                    th = None if th is None or math.isnan(th) else th * scale
                    if reg is None or th is None:
                        if (reg is None) == (th is None):
                            absent_ok += 1
                        else:
                            absent_bad += 1
                        continue
                    out.append(
                        abs(reg - th)
                        if out is er_diff
                        else abs(reg - th) / max(th, 1.0)
                    )

    # Допуски: 1 п.п. для error rate (порог degraded — 5%), 10% для p95. Выбросы — моменты
    # переходов: каталог читает Thanos в реальном времени с лагом в секунды, экспорт — позже.
    def stats(v, tol):
        if not v:
            return None
        v = sorted(v)
        q = {f"p{int(p * 100)}": v[int(p * (len(v) - 1))] for p in (0.5, 0.95, 0.99)}
        return {
            "n": len(v),
            **q,
            "max": v[-1],
            "tolerance": tol,
            "within": sum(x <= tol for x in v) / len(v),
        }

    return {
        "error_rate_abs_diff": stats(er_diff, 0.01),
        "p95_rel_diff": stats(p95_rel, 0.10),
        "both_absent": absent_ok,
        "one_absent": absent_bad,
    }


# --- отчёт ------------------------------------------------------------------------------


def pct(v: float | None, d: int = 1) -> str:
    return "—" if v is None else f"{v * 100:.{d}f}%"


def render(r: dict) -> str:
    h1r, h2r, h3r, h4r, h5r, rec = (
        r[k] for k in ("h1", "h2", "h3", "h4", "h5", "reconcile")
    )
    enf, aud, hold = (h3r["sets"][k] for k in ("enforce", "audit", "audit-hold"))
    worst = h5r["worst_dynamic"]
    summary_rows = [
        (
            "H1 (RQ1) штатный режим и алерт",
            (
                f"мин. availability baseline {pct(h1r['min_baseline_availability'], 2)}, "
                f"макс. p95 baseline {h1r['max_baseline_p95_ms']:.1f} мс; burn-rate алерт — "
                f"в {h1r['burn_alert_fired_runs']} из {len(h1r['runs'])} прогонов; на SLI "
                f"шлюза (ADR 0036) burn 5m до {h1r['gateway_burn_5m_max']:.0f}, инцидент — до "
                f"{pct(h1r['gateway_budget_month_share_max'], 2)} бюджета месяца"
            ),
            h1r["verdict"],
        ),
        (
            "H2 (RQ2, RQ3) DR",
            (
                f"TTD {h2r['mean']['ttd_s']:.1f} с, RTO {h2r['mean']['rto_s']:.1f} с; потеряно "
                f"{h2r['mean']['lost_active']:.0f} против {h2r['mean']['lost_observe']:.0f} "
                f"(−{pct(h2r['lost_reduction'])})"
            ),
            h2r["verdict"],
        ),
        (
            "H3 (RQ4) политики",
            (
                f"enforce: размещено {enf['admitted']} из {enf['attempts']}; audit: "
                f"{aud['admitted'] + hold['admitted']} из {aud['attempts'] + hold['attempts']}"
            ),
            h3r["verdict"],
        ),
        (
            "H4 (RQ5) планировщик",
            (
                f"ER {pct(h4r['mean_er_fault']['meta'], 2)} против "
                f"{pct(h4r['mean_er_fault']['naive'], 2)}, снижение {pct(h4r['reduction'])} "
                "(цель ≥ 20%)"
            ),
            h4r["verdict"],
        ),
        (
            "H5 (RQ5) устойчивость к весам",
            (
                f"ниже naive ({pct(h5r['naive_share_fault'])}) в {h5r['points_below_naive']} из "
                f"{h5r['points_dynamic']} точек с w1 + w2 > 0; худшая "
                f"{pct(worst['degraded_share_fault'])} при {worst['weights']}"
            ),
            h5r["verdict"],
        ),
    ]
    out = [
        "# Задача 9: результаты по гипотезам",
        "",
        (
            "Генерируется `analysis/task9.py` (`make analysis`) из серий task8-s1, task8-s2, "
            "task8-s3; гипотезы и правило решения — ADR 0035. Не править руками."
        ),
        "",
        "| гипотеза | ключевые числа | вердикт |",
        "|---|---|---|",
    ]
    out += [f"| {h} | {nums} | {v} |" for h, nums, v in summary_rows]
    out.append("")
    for key, title in (
        ("h1", "H1"),
        ("h2", "H2"),
        ("h3", "H3"),
        ("h4", "H4"),
        ("h5", "H5"),
    ):
        out += [f"## {title}: части гипотезы", ""]
        out += [
            f"- {'да' if ok else 'нет'} — {part}"
            for part, ok in r[key]["parts"].items()
        ]
        out.append("")
        if key == "h1":
            out.append(
                "| прогон | цель | availability baseline | p95 baseline, мс "
                "| алерты (окно отказа + 15 мин) | ошибки за отказ: клиент / шлюз / сервер |"
            )
            out.append("|---|---|---|---|---|---|")
            for row in h1r["runs"]:
                v = row["sli_fault_error_share"]
                views = (
                    "—"
                    if not v
                    else f"{pct(v['client'])} / {pct(v['gateway'])} / {pct(v['server'])}"
                )
                alerts = (
                    ", ".join(f"{k} ({s})" for k, s in sorted(row["alerts"].items()))
                    or "нет"
                )
                out.append(
                    f"| {row['run_id']} | {row['target']} | {pct(row['baseline_availability'], 2)} "
                    f"| {row['baseline_p95_ms']:.1f} | {alerts} | {views} |"
                )
            out += [
                "",
                (
                    "SLI шлюза офлайн (ADR 0036, п. 5): те же счётчики Envoy, по которым теперь "
                    "считает Thanos Ruler; все маршруты `httproute/*`. Fast-burn требует больше "
                    "14.4% ошибок за час при непрерывной нагрузке."
                ),
                "",
                (
                    "| прогон | ошибки шлюза за отказ | burn 5m, макс. | бюджета месяца за инцидент "
                    "| ошибок инцидента / нужно для fast-burn |"
                ),
                "|---|---|---|---|---|",
            ]
            for row in h1r["runs"]:
                g = row["gateway"]
                if not g:
                    continue
                burn = "—" if g["burn_5m_max"] is None else f"{g['burn_5m_max']:.1f}"
                out.append(
                    f"| {row['run_id']} | {pct(g['fault_error_share'])} | {burn} "
                    f"| {pct(g['budget_month_share'], 2)} "
                    f"| {g['fault_errors']:.0f} / {g['fast_burn_errors_needed']:.0f} |"
                )
            out.append("")
        elif key == "h2":
            pairs = ", ".join(
                f"r{p['repeat']}: {pct(p['reduction'])}" for p in h2r["pairs"]
            )
            out += [f"- по парам, снижение потерянных: {pairs}", ""]
        elif key == "h4":
            sec, pairs = h4r["secondary"], h4r["pairs"]
            nr = sec["not_ready"]
            out.append(
                "- по парам: "
                + ", ".join(f"r{p['repeat']}: {pct(p['reduction'])}" for p in pairs)
            )
            out.append(
                f"- вторичный ряд, первые {sec['window_s']} с отказа: ER "
                f"{pct(sec['mean_er']['meta'], 2)} против {pct(sec['mean_er']['naive'], 2)}, "
                f"снижение {pct(sec['reduction'])}"
            )
            out.append(
                f"- размещения, не ставшие готовыми: meta {nr['meta']['not_ready']} из "
                f"{nr['meta']['placed']}, naive {nr['naive']['not_ready']} из "
                f"{nr['naive']['placed']} — вне error rate"
            )
            out.append("")
        elif key == "h5":
            b = h5r["base"]
            out.append(
                f"- решений в пересчёте: {h5r['decisions']}, из них в окне отказа "
                f"{h5r['decisions_in_fault']}"
            )
            out.append(
                f"- базовые веса: в деградировавший {pct(b['degraded_share_fault'])} "
                "размещений окна отказа"
            )
            out.append(
                "- максимальная доля решений, где выбор отличается от базового: "
                f"{pct(h5r['max_changed_share'])}"
            )
            out += ["- карта: `sensitivity.png`", ""]
    er, p95 = rec["error_rate_abs_diff"], rec["p95_rel_diff"]
    out += ["## Сверка каталога с Thanos (ADR 0025, п. 10)", ""]
    out.append(
        f"- error rate, |каталог − Thanos|: p50 {er['p50'] * 100:.3f}, p95 {er['p95'] * 100:.3f}, "
        f"p99 {er['p99'] * 100:.3f}, максимум {er['max'] * 100:.2f} п.п.; в пределах 1 п.п. — "
        f"{pct(er['within'])} снимков (n={er['n']})"
    )
    out.append(
        f"- p95, относительное расхождение: p50 {pct(p95['p50'], 2)}, p95 {pct(p95['p95'], 2)}, "
        f"p99 {pct(p95['p99'], 2)}, максимум {pct(p95['max'], 0)}; в пределах 10% — "
        f"{pct(p95['within'])} снимков (n={p95['n']})"
    )
    out.append(
        "- расхождения больше допуска — там, где сам ряд меняется на единицы п.п. за 10 с (начало "
        "и конец отказа, хвост окна 5 мин): каталог опрашивает Thanos в реальном времени с лагом "
        "в секунды, экспорт делается позже по полным данным"
    )
    out += [
        f"- нет значения в обоих: {rec['both_absent']}, только в одном: {rec['one_absent']}",
        "",
        "## Сценарий 2: рестарты backend'а по liveness и хвост восстановления (ADR 0037)",
        "",
        (
            f"Первая серия ({S2_AMPLIFIED}) снята с пробами backend'а по умолчанию (таймаут "
            "1 с): под netem kubelet убивал медленный, но живой backend, CrashLoopBackOff держал "
            "его лежащим и после снятия отказа. Основная серия "
            f"({S2}) — с таймаутами проб под netem. Backend — по ряду `up` Prometheus; хвост — "
            "фоновые 5 RPS в app.fed.local после Recover (сбоит cluster-b: DR в observe "
            "начинает его восстановление с той же задержкой)."
        ),
        "",
        (
            "| прогон | backend выключен в отказе | backend снова up, с после Recover "
            "| ошибок фона после Recover | последняя ошибка, с | DR: восстановление, с |"
        ),
        "|---|---|---|---|---|---|",
    ]

    def num0(v):
        return "—" if v is None else f"{v:.0f}"

    for t in r["tail"] + r["tail_amplified"]:
        out.append(
            f"| {t['run_id']} | {pct(t['backend_down_share'], 0)} "
            f"| {num0(t['backend_up_after_recover_s'])} | {t['failed']} из {t['requests']} "
            f"| {num0(t['last_error_s'])} | {num0(t['dr_recovery_started_s'])} |"
        )
    amp = r["h4_amplified"]
    pairs = ", ".join(f"r{p['repeat']}: {pct(p['reduction'])}" for p in amp["pairs"])
    out += [
        "",
        (
            f"Первая серия по тем же правилам, что H4, — отдельный результат, не приёмка: ER "
            f"{pct(amp['mean_er_fault']['meta'], 2)} против "
            f"{pct(amp['mean_er_fault']['naive'], 2)}, снижение {pct(amp['reduction'])}; по "
            f"парам {pairs}."
        ),
        "",
    ]
    return "\n".join(out)


def backend_up(run_dir: Path, cluster: str, t0: float, t1: float) -> dict:
    """Backend деградировавшего кластера по ряду `up` Prometheus: доля окна отказа, когда он
    не отвечал на scrape, и через сколько секунд после Recover снова ответил (ADR 0037)."""
    for s in series(run_dir, "up_test_service"):
        m = s["metric"]
        if m.get("cluster") != cluster or not m.get("pod", "").startswith("backend"):
            continue
        pts = [(float(t), v) for t, v in s["values"]]
        fault = [v for t, v in pts if t0 <= t < t1]
        back = next((t for t, v in pts if t >= t1 and v == "1"), None)
        return {
            "down_share": fault.count("0") / len(fault) if fault else None,
            "up_after_recover_s": None if back is None else max(back - t1, 0.0),
        }
    return {"down_share": None, "up_after_recover_s": None}


def recovery_tail(prefix: str = S2) -> list[dict]:
    """Ошибки фона app.fed.local после снятия отказа в сценарии 2, по секундам k6, начало
    восстановления деградировавшего кластера в DR и backend по ряду `up` — всё от Recover."""
    out = []
    for mode, i in product(("meta", "naive"), REPEATS):
        d = run(prefix, mode, i)
        s = load(d / "summary.json")
        t1 = epoch(s["fault"]["recovered_at"])
        be = backend_up(d, s["cluster"], epoch(s["fault"]["injected_at"]), t1)
        started = [
            epoch(x["recovery_started_at"]) - t1
            for x in load(d / "incidents.json")
            if x["cluster_id"] == s["cluster"] and x.get("recovery_started_at")
        ]
        req = fail = 0
        last = None
        with (d / "per-second.csv").open(newline="") as f:
            for row in csv.DictReader(f):
                t = int(row["second"]) - t1
                if 0 <= t < 300:
                    req += int(row["background_requests"])
                    if int(row["background_failed"]):
                        fail += int(row["background_failed"])
                        last = int(t)
        out.append(
            {
                "run_id": d.name,
                "requests": req,
                "failed": fail,
                "last_error_s": last,
                "dr_recovery_started_s": max(started) if started else None,
                "backend_down_share": be["down_share"],
                "backend_up_after_recover_s": be["up_after_recover_s"],
            }
        )
    return out


def main() -> None:
    result = {
        "h1": h1(),
        "h2": h2(),
        "h3": h3(),
        "h4": h4(),
        "h5": h5(),
        "reconcile": reconcile(),
        "tail": recovery_tail(S2),
        "h4_amplified": h4(S2_AMPLIFIED),
        "tail_amplified": recovery_tail(S2_AMPLIFIED),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n"
    )
    md = render(result)
    (OUT / "results.md").write_text(md + "\n")
    plot_sensitivity(result["h5"])
    print(md)


if __name__ == "__main__":
    main()
