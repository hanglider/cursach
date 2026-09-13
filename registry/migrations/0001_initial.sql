-- Схема каталога: architecture §3.2, ADR 0004. Колонка scoring — architecture §5.2.
-- Применяется сервисом при старте (app/db.py), версия = имя файла.

CREATE TABLE clusters (
    id TEXT PRIMARY KEY,
    region TEXT NOT NULL,
    provider TEXT NOT NULL,
    api_endpoint TEXT NOT NULL,
    labels JSONB NOT NULL DEFAULT '{}',
    certifications TEXT[] NOT NULL DEFAULT '{}',
    -- healthy | degraded | down | unknown; правила записи — ADR 0003
    slo_status TEXT NOT NULL DEFAULT 'unknown'
    CHECK (slo_status IN ('healthy', 'degraded', 'down', 'unknown')),
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Отделено от clusters: пишется каждые 30 секунд.
CREATE TABLE cluster_metrics (
    cluster_id TEXT NOT NULL REFERENCES clusters (id) ON DELETE CASCADE,
    observed_at TIMESTAMPTZ NOT NULL,
    error_rate DOUBLE PRECISION,
    latency_p95_ms DOUBLE PRECISION,
    saturation DOUBLE PRECISION,
    cpu_available DOUBLE PRECISION,
    memory_available_gi DOUBLE PRECISION,
    PRIMARY KEY (cluster_id, observed_at)
);

CREATE TABLE placement_decisions (
    id BIGSERIAL PRIMARY KEY,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    workload_ref TEXT NOT NULL,
    scheduler_kind TEXT NOT NULL CHECK (scheduler_kind IN ('meta', 'naive')),
    chosen_cluster TEXT REFERENCES clusters (id),
    -- все кандидаты со скорами, снимком метрик и причиной отсева (ADR 0004)
    candidates JSONB NOT NULL,
    -- параметры scoring на момент решения (ADR 0006); null у наивного
    scoring JSONB,
    outcome TEXT NOT NULL CHECK (outcome IN ('placed', 'rejected', 'no_candidates'))
);

CREATE INDEX placement_decisions_decided_at_idx ON placement_decisions (decided_at);

CREATE TABLE policy_violations (
    id BIGSERIAL PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    workload_ref TEXT NOT NULL,
    cluster_id TEXT REFERENCES clusters (id),
    policy_name TEXT NOT NULL,
    detected_by TEXT NOT NULL CHECK (detected_by IN ('kyverno', 'meta-scheduler')),
    details JSONB
);

CREATE INDEX policy_violations_occurred_at_idx ON policy_violations (occurred_at);
