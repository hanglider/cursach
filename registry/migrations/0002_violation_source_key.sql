-- Watcher нарушений Kyverno (ADR 0019, 0026): ключ источника для дедупликации записей,
-- которые каталог перечитывает из событий и PolicyReport'ов каждый цикл.
-- NULL у записей планировщика (POST /violations): для них ключа нет.
ALTER TABLE policy_violations ADD COLUMN source_key TEXT;

CREATE UNIQUE INDEX policy_violations_source_key_idx
ON policy_violations (source_key)
WHERE source_key IS NOT NULL;
