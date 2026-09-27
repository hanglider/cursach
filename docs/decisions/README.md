# Architecture Decision Records

Формат: `NNNN-slug.md`, секции Статус / Контекст / Решение / Последствия.
Секция «Последствия» обязана содержать минусы принятого варианта. На
2026-09-25 все вопросы `docs/open-questions.md` закрыты; новые заводятся там
же и закрываются ADR со следующим свободным номером.

| № | Решение | Закрывает |
|---|---|---|
| [0001](0001-control-plane-outside-federation.md) | Контур вне федерации, в control-кластере kind | oq 3.1 |
| [0002](0002-thanos-without-object-storage.md) | Thanos без object storage, экспорт результатов вместо MinIO | spec 8.7, oq 1.6 |
| [0003](0003-dr-scheduler-via-registry.md) | DR ↔ Scheduler только через каталог, правило записи `slo_status` | spec 8.5, oq 3.2 |
| [0004](0004-registry-schema-all-candidates.md) | Схема каталога; `placement_decisions` хранит всех кандидатов | oq 3.3 |
| [0005](0005-scoring-normalization.md) | Нормализация scoring по latency budget; нейтральное 0.5 для отсутствующих метрик; ничья → round-robin | spec 8.3, oq 2.1 |
| [0006](0006-scoring-weights-and-sensitivity.md) | Веса 0.5/0.3/0.2 предрегистрированы; sensitivity офлайн по `placement_decisions` | spec 8.3, oq 2.1 |
| [0007](0007-dr-thresholds-and-hysteresis.md) | Опрос 10 с, DOWN только по пробе сервиса за 30 с, асимметричные переходы, TTD и RTO | spec 8.4, oq 2.2 |
| [0008](0008-k6-constant-load-run-structure.md) | k6, постоянные 50 RPS, структура прогона, три повтора | spec 8.8, oq 1.2 |
| [0009](0009-federation-labels-and-cluster-identity.md) | Префикс `federation.spbu/`, ConfigMap идентичности, атрибуты стенда | oq 3.6, 3.10 |
| [0010](0010-rpo-replaced-by-lost-requests.md) | RPO снят, вместо него потерянные запросы за инцидент | spec 8.1, oq 1.7 |
| [0011](0011-failover-traffic-only-app-service.md) | Failover только трафика; долгоживущий `app` в обоих кластерах как объект переключения | spec 8.6, oq 2.3 |
| [0012](0012-rq2-scope-without-hpa.md) | RQ2 сужается до механизмов контура; HPA вне работы | spec 8.2, oq 2.4 |
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
| [0023](0023-cluster-fault-injection.md) | Отказ кластера в сценарии 1: PodChaos на `workloads`; `docker pause` вторым вариантом; инвариант 7 обобщён | oq 2.5 |
| [0024](0024-observability-stack-on-stand.md) | Наблюдаемость: raw-манифесты, scrape 5 с, Thanos Ruler для `federation:*`, определение saturation, file SD | задача 2 |
| [0025](0025-registry-implementation.md) | Каталог: asyncpg и SQL-миграции в сервисе, пороги `degraded`, `PATCH /decisions/{id}`, ns `federation`, порт 8000; watcher — в задаче 5 | задача 4 |
| [0026](0026-kyverno-and-violation-watcher.md) | Kyverno 1.19 с ClusterPolicy (устаревание принято), контракт сообщения политики, watcher нарушений периодическим чтением с `source_key`, замер сценария 3 | задача 5 |
| [0027](0027-scheduler-implementation.md) | Планировщик: CR `Workload` в control, reconcile с записью всех кандидатов, режимы meta/naive флагом, отказ admission → `rejected`, адрес узла из Node API | задача 6, oq ADR 0015 |
| [0028](0028-dr-controller-implementation.md) | DR Controller: level-triggered цикл, DOWN на четвёртой неудачной пробе, SLI без ряда — норма, panic, инциденты в каталоге, режимы active/observe, Chaos Mesh и раннер сценария 1 | задача 7 |
| [0029](0029-tracing-otel-jaeger.md) | Трассировка: корень трейса — шлюз, сэмплинг 10% только на шлюзе, OTel Collector в кластерах, Jaeger v2 в control, трейсы прогона в `traces/`, разложение задержки по звеньям | задача 3 |
| [0030](0030-scenario2-background-traffic.md) | Сценарий 2: фоновые 5 RPS в `app`, чтобы у каждого кластера был SLI; дефект правила 0.5 — ограничение в отчёте | spec 8.9, oq 4.1 |
| [0031](0031-scenario2-measurement.md) | Сценарий 2: приёмочный ряд — k6 по потоку в окне отказа Chaos Mesh, свёртка трёх повторов, DR `observe`, реакция планировщика, признаки брака | spec 8.10, 8.11, oq 4.2, 4.3 |
| [0032](0032-scenario3-audit-baseline.md) | Сценарий 3: прогон «до» с политиками в `Audit`, уникальные нарушители, возврат `Enforce` | spec 8.12, oq 4.4 |
| [0033](0033-scenario2-dr-down-in-observe.md) | Сценарий 2: DOWN в DR при `observe` — оговорка, не брак; сводки серии пересчитываются из `requests.csv.gz` | задача 8 |
| [0034](0034-scenario3-policyreport-hold.md) | Сценарий 3: путь PolicyReport — audit-прогоны с удержанием нарушителей, задержка фиксации по источникам | spec 8.13, oq 5.1 |
| [0035](0035-task9-hypotheses.md) | Задача 9: гипотезы H1–H5 и правило решения, вторичный ряд сценария 2, детали sensitivity, сверка каталога с Thanos | spec 8.14, 8.15, oq 6.1, 6.2 |
| [0036](0036-federation-sli-at-gateway.md) | SLI федерации — на шлюзе: availability, burn rate, p95 и алерты по метрикам Envoy; серверные ряды — диагностика | spec 8.16, oq 6.3 |
| [0037](0037-backend-probes-and-scenario2-rerun.md) | Пробы backend'а с запасом под netem; пересъёмка сценария 2 (`task8-s2c`) — приёмка H4, первая серия — результат об усилении отказа | spec 8.17, oq 7.1 |

`oq` — `docs/open-questions.md` (номера на момент закрытия; закрытые пункты
из документа удалены).
