# Architecture Decision Records

Формат: `NNNN-slug.md`, секции Статус / Контекст / Решение / Последствия.
Секция «Последствия» обязана содержать минусы принятого варианта. Номера
0005–0007, 0011, 0012 зарезервированы за вопросами `docs/open-questions.md`
§2 и заполняются перед задачами 6–7.

| № | Решение | Закрывает |
|---|---|---|
| [0001](0001-control-plane-outside-federation.md) | Контур вне федерации, в control-кластере kind | oq 3.1 |
| [0002](0002-thanos-without-object-storage.md) | Thanos без object storage, экспорт результатов вместо MinIO | spec 8.7, oq 1.6 |
| [0003](0003-dr-scheduler-via-registry.md) | DR ↔ Scheduler только через каталог, правило записи `slo_status` | spec 8.5, oq 3.2 |
| [0004](0004-registry-schema-all-candidates.md) | Схема каталога; `placement_decisions` хранит всех кандидатов | oq 3.3 |
| 0005 | Нормализация scoring-функции | spec 8.3, oq 2.1 — ожидает |
| 0006 | Веса scoring и sensitivity-анализ | spec 8.3, oq 2.1 — ожидает |
| 0007 | Пороги переходов DR, защита от флаппинга | spec 8.4, oq 2.2 — ожидает |
| [0008](0008-k6-constant-load-run-structure.md) | k6, постоянные 50 RPS, структура прогона, три повтора | spec 8.8, oq 1.2 |
| [0009](0009-federation-labels-and-cluster-identity.md) | Префикс `federation.spbu/`, ConfigMap идентичности, атрибуты стенда | oq 3.6, 3.10 |
| [0010](0010-rpo-replaced-by-lost-requests.md) | RPO снят, вместо него потерянные запросы за инцидент | spec 8.1, oq 1.7 |
| 0011 | Судьба размещённых workloads при failover | spec 8.6, oq 2.3 — ожидает |
| 0012 | HPA и формулировка RQ2 | spec 8.2, oq 2.4 — ожидает |
| [0013](0013-two-cluster-federation.md) | Федерация из двух кластеров плюс control | oq §0 |
| [0014](0014-test-service-two-tier-go.md) | Тестовый сервис: собственный двухзвенный Go-сервис | spec 8.8, oq 1.1 |
| [0015](0015-workload-stream.md) | Единица планирования: поток экземпляров workload | oq 1.3 |
| [0016](0016-sli-from-frontend-chaos-on-backend.md) | SLI из метрик frontend'а, отказ вносится в backend | oq 1.4 |
| [0017](0017-l7-gateway-instead-of-externaldns.md) | Gateway API + Envoy Gateway вместо ExternalDNS | oq 1.5 |
| [0018](0018-registry-api-eligible-and-tokens.md) | API каталога: `/clusters/eligible`, bearer-токены | oq 3.4, 3.5 |
| [0019](0019-kyverno-scope-and-violation-reporting.md) | Kyverno: регион и сертификации; ресурсы на каталоге; watcher в каталоге | oq 3.7 |
| [0020](0020-naive-scheduler-round-robin-flag.md) | Наивный планировщик: round-robin, флаг `--scheduler` | oq 3.8 |
| [0021](0021-stand-network-kind-internal-endpoints.md) | Одна docker-сеть kind, внутрисетевые адреса | oq 3.9 |
| [0022](0022-workload-entry-nodeport.md) | Вход в кластер федерации: NodePort на workload, узел по IP | ADR 0017, 0021 |

`oq` — `docs/open-questions.md` (номера на момент закрытия; закрытые пункты
из документа удалены). Вопрос 2.5 (внесение отказа кластера в сценарии 1)
пока без номера ADR.
