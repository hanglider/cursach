"""Пересчёт scoring на Python совпадает с Go-планировщиком (ADR 0035, п. 5): на всех решениях
meta серии task8-s2 — те же слагаемые, тот же score, выбранный кластер среди argmax."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from scoring import (
    BASE_WEIGHTS,
    NEUTRAL,
    Candidate,
    Decision,
    choice,
    load_decisions,
    parse_ts,
    score,
    terms,
)

RESULTS = Path(__file__).resolve().parent.parent / "experiments" / "results"
META_RUNS = sorted(RESULTS.glob("task8-s2-meta-r*"))


def test_parse_ts_variants():
    want = datetime(2026, 9, 26, 15, 8, 33, 537008, tzinfo=UTC)
    assert parse_ts("2026-09-26 15:08:33.537008+00") == want
    assert parse_ts("2026-09-26T15:08:33.537008Z") == want
    assert parse_ts("2026-09-26T15:08:33.537008+00:00") == want


def test_stale_and_missing_metrics_are_neutral():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    fresh = {
        "observed_at": (now - timedelta(seconds=30)).isoformat(),
        "error_rate": 0.1,
    }
    stale = dict(fresh, observed_at=(now - timedelta(seconds=61)).isoformat())
    assert terms(None, now) == (NEUTRAL,) * 3
    assert terms(stale, now) == (NEUTRAL,) * 3
    assert terms(fresh, now) == (pytest.approx(0.9), NEUTRAL, NEUTRAL)


def test_latency_term_is_capped_by_budget():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    m = {"observed_at": now.isoformat(), "latency_p95_ms": 700.0}
    assert terms(m, now)[1] == 0.0


def test_tie_is_split_equally():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    d = Decision(
        "r", now, "meta", "a", "placed", [Candidate("a", None), Candidate("b", None)]
    )
    assert choice(d, BASE_WEIGHTS) == {"a": 0.5, "b": 0.5}


@pytest.mark.skipif(not META_RUNS, reason="нет прогонов task8-s2")
def test_matches_go_scheduler_on_recorded_decisions():
    checked = 0
    for run in META_RUNS:
        for d in load_decisions(run):
            if d.scheduler != "meta" or d.outcome != "placed":
                continue
            for c in d.candidates:
                t = terms(c.metrics, d.decided_at)
                rt = c.recorded_terms
                assert t == pytest.approx(
                    (rt["error"], rt["latency"], rt["saturation"]), abs=1e-9
                ), (run.name, d.decided_at, c.cluster)
                assert score(t, BASE_WEIGHTS) == pytest.approx(
                    c.recorded_score, abs=1e-9
                )
            assert d.chosen in choice(d, BASE_WEIGHTS), (run.name, d.decided_at)
            checked += 1
    assert checked >= 20
