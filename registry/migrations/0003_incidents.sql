-- Инциденты DR Controller'а (architecture §6.4, ADR 0007, 0028): одна строка на эпизод
-- от перехода кластера в DEGRADED до возврата в HEALTHY. Времена фаз пишет DR,
-- время внесения отказа — раннер сценария из инструмента внесения (инвариант 7).
CREATE TABLE incidents (
    id BIGSERIAL PRIMARY KEY,
    cluster_id TEXT NOT NULL REFERENCES clusters (id),
    -- active — DR переключает трафик; observe — только наблюдает (сценарий 1 «без DR»)
    dr_mode TEXT NOT NULL CHECK (dr_mode IN ('active', 'observe')),
    -- худшее состояние за инцидент: degraded (без failover) или down
    peak_state TEXT NOT NULL DEFAULT 'degraded'
    CHECK (peak_state IN ('degraded', 'down')),
    detected_at TIMESTAMPTZ NOT NULL,
    failover_started_at TIMESTAMPTZ,
    failover_completed_at TIMESTAMPTZ,
    recovery_started_at TIMESTAMPTZ,
    -- возврат в HEALTHY; NULL — инцидент открыт
    recovery_completed_at TIMESTAMPTZ,
    fault_injected_at TIMESTAMPTZ,
    -- ссылка на внесённый отказ, например podchaos/workloads/<имя> в <кластер>
    fault_ref TEXT,
    details JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX incidents_detected_at_idx ON incidents (detected_at);

CREATE INDEX incidents_open_idx ON incidents (cluster_id)
WHERE recovery_completed_at IS NULL;
