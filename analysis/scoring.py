"""Scoring meta-scheduler'а на Python для офлайн-пересчёта решений (ADR 0005, 0006, 0035):
та же формула и правила на краях, что `scheduler/internal/scoring`. Совпадение с
Go-реализацией проверяет `test_scoring.py` на записанных решениях серии task8-s2.

Решение берётся из `registry/placement_decisions.csv` экспорта прогона: у каждого
кандидата снимок метрик на момент решения (ADR 0004); у meta ещё `terms` и `score`.

Только стандартная библиотека.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

NEUTRAL = 0.5  # вклад отсутствующей или устаревшей метрики (ADR 0005)
TIE_EPSILON = 1e-6
BASE_WEIGHTS = (0.5, 0.3, 0.2)  # предрегистрированы (ADR 0006)
LATENCY_BUDGET_MS = 200.0
METRICS_MAX_AGE_S = 60.0


def parse_ts(value: str) -> datetime:
    """ISO-время из каталога и экспорта: `...Z`, `+00:00` и PostgreSQL-вида `+00`."""
    v = value.strip().replace(" ", "T").replace("Z", "+00:00")
    if v[-3] in "+-" and v[-3:].lstrip("+-").isdigit():
        v += ":00"
    return datetime.fromisoformat(v)


@dataclass
class Candidate:
    cluster: str
    metrics: dict | None
    recorded_terms: dict | None = None
    recorded_score: float | None = None


@dataclass
class Decision:
    run_id: str
    decided_at: datetime
    scheduler: str
    chosen: str | None
    outcome: str
    candidates: list[Candidate] = field(default_factory=list)


def terms(metrics: dict | None, now: datetime) -> tuple[float, float, float]:
    """Слагаемые (error, latency, saturation) до взвешивания — Params.Terms в Go."""
    if not metrics or not metrics.get("observed_at"):
        return NEUTRAL, NEUTRAL, NEUTRAL
    if (now - parse_ts(metrics["observed_at"])).total_seconds() > METRICS_MAX_AGE_S:
        return NEUTRAL, NEUTRAL, NEUTRAL  # устаревший снимок — как отсутствующий

    def clamp01(v: float) -> float:
        return min(max(v, 0.0), 1.0)

    err, lat, sat = (
        metrics.get("error_rate"),
        metrics.get("latency_p95_ms"),
        metrics.get("saturation"),
    )
    return (
        NEUTRAL if err is None else 1 - clamp01(err),
        NEUTRAL if lat is None else 1 - min(max(lat, 0.0) / LATENCY_BUDGET_MS, 1.0),
        NEUTRAL if sat is None else 1 - clamp01(sat),
    )


def score(t: tuple[float, float, float], weights: tuple[float, float, float]) -> float:
    return weights[0] * t[0] + weights[1] * t[1] + weights[2] * t[2]


def choice(decision: Decision, weights: tuple[float, float, float]) -> dict[str, float]:
    """Доли выбора по кандидатам: argmax score, ничья делится поровну (ADR 0035, п. 5)."""
    scores = {
        c.cluster: score(terms(c.metrics, decision.decided_at), weights)
        for c in decision.candidates
    }
    if not scores:
        return {}
    best = max(scores.values())
    tied = [c for c, s in scores.items() if best - s < TIE_EPSILON]
    return {c: 1 / len(tied) for c in tied}


def load_decisions(run_dir: Path) -> list[Decision]:
    path = run_dir / "registry" / "placement_decisions.csv"
    if not path.exists():
        return []
    csv.field_size_limit(1 << 30)
    out = []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            cands = [
                Candidate(
                    cluster=c["cluster"],
                    metrics=c.get("metrics"),
                    recorded_terms=c.get("terms"),
                    recorded_score=c.get("score"),
                )
                for c in json.loads(row["candidates"])
                if not c.get("filtered_by")
            ]
            out.append(
                Decision(
                    run_id=run_dir.name,
                    decided_at=parse_ts(row["decided_at"]),
                    scheduler=row["scheduler_kind"],
                    chosen=row["chosen_cluster"] or None,
                    outcome=row["outcome"],
                    candidates=cands,
                )
            )
    return out
