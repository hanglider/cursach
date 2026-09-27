# Architecture: сквозной контур управления надёжностью федерации

> Как система устроена: компоненты, границы, контракты между ними, потоки
> данных, схема стенда.
>
> Зачем это всё — `docs/vision.md`.
> Постановка задачи, целевые показатели, план — `docs/spec.md`.
> Обоснование конкретных выборов — `docs/decisions/`.
>
> **Статус документа:** проектный. Разделы, помеченные `[предложение]`,
> выведены при проектировании и не зафиксированы в spec — они подлежат
> подтверждению и оформлению как ADR перед реализацией соответствующего
> компонента. На 2026-09-13 таких разделов не осталось: все предложения
> приняты или пересмотрены ADR (`docs/decisions/README.md`).

---

## 1. Обзор

### 1.1. Карта компонентов

```
┌─────────────────────────────────────────────────────────────────┐
│                      УПРАВЛЯЮЩИЙ КОНТУР                          │
│                                                                   │
│   ┌──────────────┐        ┌──────────────┐      ┌─────────────┐ │
│   │   Thanos     │───────>│   Registry   │<────>│    Meta-    │ │
│   │   Querier    │  SLI   │   Service    │      │  Scheduler  │ │
│   │              │        │ (FastAPI+PG) │      │    (Go)     │ │
│   └──────▲───────┘        └──────▲───────┘      └──────┬──────┘ │
│          │                       │                      │        │
│          │                ┌──────┴───────┐              │        │
│          │                │      DR      │              │        │
│          │                │  Controller  │              │        │
│          └────────────────┤     (Go)     │              │        │
│                           └──────┬───────┘              │        │
└──────────────────────────────────┼──────────────────────┼────────┘
                                   │ traffic              │ create
                                   │ switching            │ workload
        ┌──────────────────────────┼──────────────────────┼────────┐
        │  ФЕДЕРАЦИЯ (2-3 кластера)│                      │        │
        │                          ▼                      ▼        │
        │  ┌────────────────────────────────────────────────────┐  │
        │  │  cluster-a          cluster-b          cluster-c   │  │
        │  │  ┌──────────┐      ┌──────────┐      ┌──────────┐ │  │
        │  │  │ Kyverno  │      │ Kyverno  │      │ Kyverno  │ │  │
        │  │  ├──────────┤      ├──────────┤      ├──────────┤ │  │
        │  │  │ workload │      │ workload │      │ workload │ │  │
        │  │  ├──────────┤      ├──────────┤      ├──────────┤ │  │
        │  │  │Prometheus│      │Prometheus│      │Prometheus│ │  │
        │  │  │ + OTel   │      │ + OTel   │      │ + OTel   │ │  │
        │  │  │ + sidecar│      │ + sidecar│      │ + sidecar│ │  │
        │  │  └──────────┘      └──────────┘      └──────────┘ │  │
        │  └────────────────────────────────────────────────────┘  │
        │                    ▲                                     │
        │            Chaos Mesh (внесение отказов)                 │
        └──────────────────────────────────────────────────────────┘
```

### 1.2. Где что живёт

Управляющий контур размещается **вне** федерации — на отдельном хост-кластере
или на машине разработчика. Это принципиально: контур, живущий внутри одного из
управляемых кластеров, теряет наблюдателя при отказе именно этого кластера,
и сценарий 1 из spec становится непроверяемым.

| Компонент | Где | Почему |
|---|---|---|
| Registry Service | Вне федерации | Единственный источник состояния, должен переживать отказ любого кластера |
| Meta-Scheduler | Вне федерации | Нужен доступ к API всех кластеров одновременно |
| DR Controller | Вне федерации | Наблюдатель не может жить в наблюдаемом |
| Thanos Querier | Вне федерации | Точка агрегации |
| Шлюз (Envoy Gateway) | Вне федерации | Единая точка входа трафика, веса по кластерам; корень трейса (ADR 0029) |
| Jaeger | Вне федерации | Хранилище трейсов; наблюдатель не в наблюдаемом (ADR 0029) |
| Thanos sidecar | В каждом кластере | Рядом с локальным Prometheus |
| Prometheus, OTel Collector | В каждом кластере | Сбор локальных метрик и трейсов |
| Kyverno | В каждом кластере | Admission webhook работает только внутри |
| Chaos Mesh | В каждом кластере | Внесение отказов на месте |

### 1.3. Три роли компонентов

Понимание ролей важнее, чем список технологий:

- **Источники истины.** Prometheus (что происходит), Registry (какие кластеры
  есть и в каком они состоянии). Всё остальное читает из них.
- **Решающие.** Meta-Scheduler (куда разместить), DR Controller (объявлять ли
  инцидент). Пишут решения в audit log.
- **Исполнители.** Kyverno (пропустить/отклонить), шлюз (куда идёт трафик,
  по весам маршрутов). Не принимают решений.

---

## 2. Слой наблюдаемости

### 2.1. Топология сбора

Каждый кластер: `Prometheus` (scrape локальных таргетов каждые 5 с, recording
rules `cluster:*`) + `Thanos sidecar` (отдаёт данные наружу по StoreAPI) +
`OpenTelemetry Collector` (трейсы, ADR 0029). Всё — raw-манифесты, без
оператора (ADR 0024).

`Thanos Querier` вне федерации агрегирует все sidecar'ы и предоставляет единый
PromQL-endpoint; `Thanos Ruler` считает поверх него глобальные правила
`federation:*` и burn-rate алерты и отдаёт результат обратно Querier'у.

```
cluster-a: Prometheus ──> Thanos sidecar ──┐
cluster-b: Prometheus ──> Thanos sidecar ──┼──> Thanos Querier ──> PromQL API
control:   Prometheus ──> Thanos sidecar ──┤           │  ▲
           ▲ k6 remote write, метрики шлюза │           │  │ federation:*, ALERTS
           Thanos Ruler ────────────────────┘           │  │
                  └──── query ──────────────────────────┼──┘
                                                        └──> Grafana
```

Трассировка (ADR 0029): корень трейса — Envoy в control, он же принимает решение о
сэмплинге (10%) и передаёт `traceparent` в кластер; тестовый сервис продолжает только
сэмплированные трейсы. Коллектор кластера ставит метку `federation.cluster` и шлёт
спаны в Jaeger control.

```
k6 ──> Envoy (control) ──span──────────────────────────────> Jaeger (control)
          │ traceparent                                        ▲  OTLP gRPC,
          ▼                                                    │  IP узла:30417
cluster-a: frontend ──> backend ──OTLP/HTTP──> OTel Collector ─┘
           (span)       (span)                 + federation.cluster
```

Экспорт прогона выгружает трейсы окна в `traces/`: сырые спаны и разложение
задержки по звеньям — шлюз, переход control → кластер, frontend, сеть до backend,
backend.

### 2.2. Без object storage

Thanos sidecar работает только как StoreAPI поверх живого Prometheus; блоки
никуда не загружаются, MinIO не разворачивается (ADR 0002). Retention
Prometheus — 15 дней. Долгосрочное хранилище результатов — экспорт сырых рядов
каждого прогона из Thanos Query API в `experiments/results/<run-id>/`,
встроенный в скрипт-раннер эксперимента. Что именно выгружается — список в
скрипте экспорта, версионируется вместе с ним.

### 2.3. Контракт: какие метрики публикует тестовый сервис

Meta-Scheduler и DR Controller опираются на три величины по каждому кластеру.
Чтобы они были вычислимы, тестовый сервис (собственный двухзвенный Go-сервис,
ADR 0014, §7.4) экспонирует:

| Метрика | Тип | Назначение |
|---|---|---|
| `http_requests_total{cluster, tier, workload, status}` | counter | error rate, availability |
| `http_request_duration_seconds{cluster, tier, workload}` | histogram | p95 / p99 latency |
| стандартные `container_*` из cAdvisor | gauge | saturation |

Лейбл `cluster` проставляется через `external_labels` в конфигурации
Prometheus — это то, что позволяет Querier'у различать источники после
агрегации. `tier` (`frontend` | `backend`) ставит сам сервис, `workload` —
relabeling kube-sd из pod-метки `federation.spbu/workload`. SLI для каталога
считаются по `tier="frontend"` (ADR 0016).

### 2.4. Производные SLI

```promql
# error rate кластера, окно 5 минут
sum by (cluster) (rate(http_requests_total{tier="frontend",status=~"5.."}[5m]))
  / sum by (cluster) (rate(http_requests_total{tier="frontend"}[5m]))

# p95 latency кластера
histogram_quantile(0.95,
  sum by (cluster, le) (rate(http_request_duration_seconds_bucket{tier="frontend"}[5m])))

# глобальная availability федерации — на шлюзе, точке входа (ADR 0036)
1 - sum(rate(envoy_cluster_upstream_rq_xx{envoy_cluster_name=~"httproute/.*",envoy_response_code_class="5"}[5m]))
  / sum(rate(envoy_cluster_upstream_rq_xx{envoy_cluster_name=~"httproute/.*"}[5m]))
```

Первые две — вход для scoring-функции. Третья — целевой показатель ≥ 99.0%.
Глобальный SLI меряется на шлюзе, а не суммой серверных SLI кластеров: при
отказе кластера целиком его запросы падают на шлюзе и до frontend'ов не доходят.
Серверная сумма выбрасывает их из знаменателя и показывает 100% (задача 9,
ADR 0036).

На стенде это recording rules: `cluster:http_error_rate:ratio5m`,
`cluster:http_request_duration_seconds:p95_5m`, `cluster:saturation:ratio`
(per-cluster, `stand/observability/federation/prometheus-rules.yaml`) и
глобальные в Thanos Ruler (`stand/observability/control/thanos-rule.yaml`):
`federation:gateway_availability:ratio*`,
`federation:gateway_error_budget_burn_rate:*`,
`federation:gateway_request_duration_seconds:p95_5m` — SLO и алерты;
серверные `federation:availability:ratio*`, `federation:error_budget_burn_rate:*` —
диагностика.
Окно 1 мин (`cluster:*:ratio1m`) — для DR Controller'а (ADR 0007).

---

## 3. Registry Service

Центральный компонент архитектуры. Не потому что делает больше всех, а потому
что **все остальные общаются через него**, а не напрямую.

### 3.1. Почему через каталог, а не напрямую

DR Controller обнаруживает деградацию и должен сообщить об этом
Meta-Scheduler'у. Прямой вызов создал бы связность двух контроллеров и второй
источник истины о здоровье кластера.

Вместо этого DR Controller обновляет `slo_status` в каталоге, а Meta-Scheduler
читает каталог перед каждым решением. Компоненты не знают друг о друге.
Принято как ADR 0003; там же правило приоритета записи в `slo_status`:
фоновый опрос каталога не затирает `down`, выставленный DR Controller'ом.

### 3.2. Схема данных

Принята как ADR 0004; выведена из перечня атрибутов (§4.3 spec).

```sql
CREATE TABLE clusters (
    id              TEXT PRIMARY KEY,
    region          TEXT NOT NULL,
    provider        TEXT NOT NULL,
    api_endpoint    TEXT NOT NULL,
    labels          JSONB NOT NULL DEFAULT '{}',
    certifications  TEXT[] NOT NULL DEFAULT '{}',
    slo_status      TEXT NOT NULL DEFAULT 'unknown',
        -- healthy | degraded | down | unknown
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- отделено от clusters: меняется каждые 30 секунд,
-- тогда как сам кластер — статическая сущность
CREATE TABLE cluster_metrics (
    cluster_id      TEXT REFERENCES clusters(id) ON DELETE CASCADE,
    observed_at     TIMESTAMPTZ NOT NULL,
    error_rate      DOUBLE PRECISION,
    latency_p95_ms  DOUBLE PRECISION,
    saturation      DOUBLE PRECISION,
    cpu_available   DOUBLE PRECISION,
    memory_available_gi DOUBLE PRECISION,
    PRIMARY KEY (cluster_id, observed_at)
);

CREATE TABLE placement_decisions (
    id              BIGSERIAL PRIMARY KEY,
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    workload_ref    TEXT NOT NULL,        -- namespace/name
    scheduler_kind  TEXT NOT NULL,        -- meta | naive
    chosen_cluster  TEXT REFERENCES clusters(id),
    candidates      JSONB NOT NULL,       -- [{cluster, score, metrics, filtered_by}]
    scoring         JSONB,                -- параметры scoring (§5.2); null у наивного
    outcome         TEXT NOT NULL         -- placed | rejected | no_candidates
);

CREATE TABLE policy_violations (
    id              BIGSERIAL PRIMARY KEY,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    workload_ref    TEXT NOT NULL,
    cluster_id      TEXT REFERENCES clusters(id),
    policy_name     TEXT NOT NULL,
    detected_by     TEXT NOT NULL,        -- kyverno | meta-scheduler
    details         JSONB
);

-- инциденты DR Controller'а (§6.4, ADR 0028): эпизод DEGRADED → … → HEALTHY
CREATE TABLE incidents (
    id                    BIGSERIAL PRIMARY KEY,
    cluster_id            TEXT NOT NULL REFERENCES clusters(id),
    dr_mode               TEXT NOT NULL,          -- active | observe
    peak_state            TEXT NOT NULL,          -- degraded | down
    detected_at           TIMESTAMPTZ NOT NULL,
    failover_started_at   TIMESTAMPTZ,
    failover_completed_at TIMESTAMPTZ,
    recovery_started_at   TIMESTAMPTZ,
    recovery_completed_at TIMESTAMPTZ,            -- NULL: инцидент открыт
    fault_injected_at     TIMESTAMPTZ,            -- пишет раннер сценария (инвариант 7)
    fault_ref             TEXT,
    details               JSONB NOT NULL DEFAULT '{}'
);
```

Замечание по `placement_decisions.candidates`: хранится **весь** список
кандидатов со скорами, снимком метрик на момент решения и причиной отсева, а
не только победитель. Без этого сравнительный анализ (задача 9 плана)
сведётся к сравнению итоговых SLO без понимания, почему планировщик выбрал
именно так.

Поле `scheduler_kind` позволяет прогонять оба планировщика и разделять их
решения в анализе — иначе для наивной версии понадобится второй экземпляр
каталога.

### 3.3. API

```
POST   /api/v1/clusters                  регистрация кластера
GET    /api/v1/clusters                  список, ?status=&region=&certification=
GET    /api/v1/clusters/{id}             карточка кластера
PATCH  /api/v1/clusters/{id}/status      обновление slo_status (DR Controller)
DELETE /api/v1/clusters/{id}             дерегистрация

GET    /api/v1/clusters/eligible         кандидаты под hard constraints
                                         ?region=&certification=&min_cpu=&min_memory_gi=

POST   /api/v1/decisions                 запись решения о размещении
PATCH  /api/v1/decisions/{id}            outcome после admission (ADR 0025)
GET    /api/v1/decisions                 выборка для анализа, ?from=&to=&scheduler_kind=

POST   /api/v1/violations                фиксация нарушения политики
GET    /api/v1/violations

POST   /api/v1/incidents                 открытие инцидента (DR Controller, ADR 0028)
PATCH  /api/v1/incidents/{id}            фазы DR-цикла; fault_injected_at — раннер
GET    /api/v1/incidents                 ?cluster_id=&open=&from=&to=; ttd/rto в ответе

GET    /healthz                          liveness
```

Ключевой эндпоинт — `/clusters/eligible`. Он инкапсулирует фильтрацию по hard
constraints на стороне каталога, а не планировщика. Логика фильтрации оказывается
в одном месте и переиспользуется наивным планировщиком (которому тоже нельзя
нарушать политики — иначе сравнение нечестное). Ответ содержит и допустимых
кандидатов с последним снимком метрик, и отсеянных с причиной; `down`
отсеивается, `unknown` и `degraded` остаются (ADR 0018).

`POST /violations` принимает `requirements` workload'а; каталог дописывает в
`details.eligible_clusters` допустимые кластеры (ADR 0019, 0025).

**Аутентификация** (ADR 0018): статические bearer-токены на компонент.
Полноценный mTLS для стенда — избыточная сложность; «базовые проверки доверия»
из vision покрываются токенами и проверкой при регистрации, что ConfigMap
идентичности кластера (§4.2) совпадает с переданными атрибутами.

### 3.4. Цикл обновления статуса

Фоновая задача каталога, период **30 секунд** (задан в spec):

1. Для каждого зарегистрированного кластера выполнить PromQL-запросы (§2.4)
   к Thanos Querier
2. Записать результат в `cluster_metrics`
3. Пересчитать `slo_status` по порогам: `degraded` при error rate за 5 мин
   > 0.05 или p95 > 200 мс (ADR 0025)
4. Кластер, не ответивший на запрос — `unknown`, не `down`
5. Статус `down` не трогать: его ставит и снимает только DR Controller
   (ADR 0003)

Различие `unknown` и `down` существенно: `down` объявляет DR Controller по
результатам собственного мониторинга, `unknown` означает лишь, что каталог не
смог получить метрики. Смешение этих состояний даст ложные срабатывания
failover при проблемах с самим Querier'ом.

### 3.5. Watcher нарушений

Вторая фоновая задача каталога, период **10 секунд** (ADR 0019, 0026): ловит
размещения в обход планировщика (§4.3, сценарий 3).

1. Для каждого кластера из `clusters` — через kubeconfig по `api_endpoint` —
   прочитать события `events.k8s.io/v1` с `reason=PolicyViolation` и
   PolicyReport'ы с `result: fail`; учитываются только Deployment и Job
2. Отбросить уже записанное по `source_key` (`event/<cluster>/<uid>`,
   `policyreport/<cluster>/<uid ресурса>/<policy>/<rule>`)
3. Из сообщения политики (контракт §4.3) восстановить требования workload'а
   и посчитать `eligible_clusters` тем же кодом, что у `POST /violations`
4. Записать в `policy_violations` с `detected_by = kyverno`,
   `occurred_at` — время события в Kyverno, `details.detected_at` — момент
   записи; их разница — задержка фиксации нарушения

Недоступный кластер даёт предупреждение в логе, остальные обрабатываются.
Периодическое чтение выбрано вместо watch-потока: переподключений нет, а
дедупликация нужна в любом случае.

---

## 4. Политики размещения

### 4.1. Два рубежа

```
                    ┌─────────────────────────────────────┐
                    │  Рубеж 1: Meta-Scheduler            │
   workload  ──────>│  фильтрация кандидатов по каталогу  │
                    │  (hard) + scoring (soft)            │
                    └──────────────┬──────────────────────┘
                                   │ выбранный кластер
                                   ▼
                    ┌─────────────────────────────────────┐
                    │  Рубеж 2: Kyverno в кластере        │
                    │  admission webhook, hard constraints│
                    └──────────────┬──────────────────────┘
                                   │
                          принято / отклонено
```

Рубеж 2 не дублирует рубеж 1 без нужды. Он ловит три класса ситуаций:

- Meta-Scheduler ошибся или работает на устаревших данных каталога
- Workload создан в обход планировщика (вручную, другим контроллером)
- Каталог рассинхронизирован с реальностью кластера

Именно наличие второго рубежа делает достижимым целевой показатель «0 нарушений
политик»: первый рубеж — оптимизация, второй — гарантия.

### 4.2. Разметка кластеров

Hard constraints опираются на метки, которые должны существовать в двух местах
согласованно: в каталоге (для фильтрации) и на самих кластерах (для Kyverno).

Соглашение (ADR 0009): префикс `federation.spbu/`. В Kubernetes нет объекта
«кластер», поэтому идентичность кластера живёт в ConfigMap
`federation-system/cluster-identity`, который Kyverno читает через `context`:

```yaml
data:
  cluster-id: cluster-a
  region: eu-west
  provider: local
  certifications: pci-dss,iso-27001
```

Требования workload'а выражаются аннотациями на самом ресурсе:

```yaml
metadata:
  annotations:
    federation.spbu/require-region: eu-west
    federation.spbu/require-certification: pci-dss
    federation.spbu/require-cpu: "2"
    federation.spbu/require-memory-gi: "4"
```

Рассинхронизация каталога и кластера — отдельный риск. Источник истины один —
`stand/clusters.yaml`, из которого `make stand-up` создаёт ConfigMap и
регистрирует кластер; каталог при регистрации читает ConfigMap по
`api_endpoint` и отказывает при расхождении. Атрибуты кластеров стенда —
в ADR 0009.

### 4.3. Структура политик Kyverno

Две политики (ADR 0019), обе `Enforce` и `background: true`, чтобы отлавливать
не только новые, но и существующие нарушающие ресурсы:

| Политика | Проверяет |
|---|---|
| `require-region-match` | аннотация `require-region` совпадает с `region` из ConfigMap идентичности |
| `require-certification` | все запрошенные сертификации есть в `certifications` кластера |

Ресурсные ограничения (`require-cpu`, `require-memory-gi`) Kyverno не
проверяет: реализация через `apiCall` к узлам хрупкая, а in-cluster backstop
для ресурсов уже есть — kube-scheduler оставляет под без ресурсов в `Pending`.
Ресурсы фильтрует только рубеж 1.

Обе политики — `kyverno.io/v1` ClusterPolicy с `validate.failureAction:
Enforce` (`stand/policies/`); Kyverno 1.19 объявил этот тип устаревшим,
версия закреплена (ADR 0026). Сообщение об отказе машиночитаемо —
`federation.spbu/require-<region|certification>=<v> not satisfied: cluster
<id> ...` — по нему watcher каталога и планировщик восстанавливают
требования отклонённого workload'а. Политики проверяются без кластера:
`kyverno test stand/policies/tests` в `make test`. Прогон «до» сценария 3
временно переводит политики целевого кластера в `Audit` и возвращает `Enforce`
(ADR 0032); путь PolicyReport меряется audit-прогонами `HOLD=report`, где
нарушители живут до записи из отчёта (ADR 0034).

Фиксация нарушения в audit log каталога — два пути. Планировщик, получив отказ
admission, сам пишет `POST /api/v1/violations`. Размещения в обход
планировщика ловит фоновый watcher каталога: он следит за Event'ами Kyverno
(`PolicyViolation`) и `PolicyReport`'ами во всех кластерах федерации.
Отклонённый в `Enforce` ресурс не существует и в `PolicyReport` не попадает,
поэтому одних отчётов недостаточно. Прямого способа отправить HTTP-запрос из
Kyverno-политики нет.

---

## 5. Meta-Scheduler

### 5.1. Цикл reconcile

Вход планировщика — CR `Workload` (`federation.spbu/v1alpha1`) в control,
ns `federation` (ADR 0027): `spec.port`, `spec.image`, `spec.replicas`,
требования — аннотации `federation.spbu/require-*` на CR. Контроллер
реагирует на изменение `generation`, статус — в `status.phase`.

```
watch Workload (control, ns federation)
   │
   ▼
GET /clusters/eligible?<hard constraints из аннотаций>
   │
   ├── каталог недоступен ──> phase=Error, повтор с backoff (решение не пишется)
   ├── пусто ──> POST /decisions(outcome=no_candidates, все отсеянные), phase=NoCandidates,
   │             повтор через 30 с (каждый повтор — новое решение)
   ▼
meta: score = f(error_rate, latency_p95, saturation), ничья → round-robin
naive: round-robin по id
   │
   ▼
POST /decisions (все кандидаты + скоры + выбор) ──> id ──> status.cluster, status.decisionId
   │
   ▼
Deployment + Service в выбранном кластере (метки workload/scheduler/decision-id,
аннотации require-* скопированы), Backend + HTTPRoute на шлюзе ──> phase=Placed
   │
   └── admission отклонил (400 «denied the request») ──> PATCH /decisions/{id} rejected,
       POST /violations (reported_via=scheduler), phase=Rejected (терминально)
```

Финализатор `federation.spbu/placement` убирает все четыре объекта при удалении
CR; недоступный кластер держит удаление не дольше 2 минут. `Placed` и
`Rejected` терминальны: CR не перепланируется. Адрес узла для Backend —
`InternalIP` из API кластера федерации (ADR 0022, 0027).

### 5.2. Scoring-функция

Нормализованная формула (ADR 0005), заменяет исходную из spec:

```
latency_score = 1 - min(latency_p95 / latency_budget, 1)
score = w1*(1 - error_rate) + w2*latency_score + w3*(1 - saturation)
```

`latency_budget` = 200 мс — целевой p95 из spec §1.3, флаг
`--latency-budget`. Все три слагаемых в [0, 1]; исходное `1/latency_p95` не
было ограничено сверху и подавляло остальные независимо от весов.

Правила на краях:

- отсутствующая или устаревшая (старше 60 с) метрика даёт своему слагаемому
  нейтральное 0.5; кластер `unknown` получает 0.5 по всем трём;
- ничья (разница меньше 1e-6) разрешается round-robin, как у наивного
  планировщика. Без сигнала умный планировщик вырождается ровно в наивный.

Ограничение правила 0.5 (ADR 0030): кластер без трафика тоже получает 0.5 по
error rate и латентности, то есть score ≈ 0.55 против ≈ 0.95 у кластера с
трафиком. С холодного старта умный планировщик собирает поток в первом
кластере с метриками. Деградировавший по латентности кластер
(`0.5·(1 − e) + 0.15`) выигрывает у пустого при error rate < 0.2. На стенде
сценарий 2 обходит это фоновым трафиком в `app` каждого кластера; правило не
меняется, в отчёте это ограничение формулы.

**Веса** `w1 = 0.5, w2 = 0.3, w3 = 0.2` (ADR 0006), флаг `--weights`,
предрегистрированы: целевой показатель считается только с ними. Каждое
решение хранит параметры scoring в `placement_decisions.scoring`, так что
sensitivity-анализ (задача 9) делается офлайн пересчётом по сетке весов без
повторных chaos-прогонов.

### 5.3. Наивный планировщик

Round-robin по кандидатам из `/clusters/eligible`.

Существенно: наивный планировщик **тоже** ходит в `/clusters/eligible`, то есть
соблюдает hard constraints. Разница между планировщиками должна быть строго в
учёте динамических метрик. Если наивная версия ещё и нарушает политики, разница
в error rate окажется смесью двух эффектов, и целевой показатель «≥ 20%» станет
неинтерпретируемым.

Переключение реализуется флагом `--scheduler=meta|naive` без пересборки —
эксперименты прогоняются одним бинарём (ADR 0020). Наивный планировщик пишет
кандидатов в `placement_decisions` со `score: null` и снимком метрик, чтобы
офлайн-анализ мог посчитать, что выбрал бы умный.

На стенде режим — env `SCHEDULER_MODE` Deployment'а `scheduler` в
`control/federation`; `make scheduler-mode MODE=naive` меняет его и ждёт
rollout, `make stand-up` возвращает `meta`. Генератор (`--mode scheduler`)
создаёт CR `Workload` и ждёт `status.phase == Placed`; прямой режим
`--mode direct` размещает сам, в обход планировщика — для дымового теста
Kyverno и сценария 3 (ADR 0027).

---

## 6. DR Controller

### 6.1. Конечный автомат кластера

```
                 !sli_ok | !svc_ok | !api_ok        !svc_ok 3 цикла подряд
    ┌─────────┐  ───────────────────────>  ┌──────────┐  ───────────────>  ┌──────┐
    │ HEALTHY │                            │ DEGRADED │                    │ DOWN │
    └─────────┘  <───────────────────────  └──────────┘                    └──┬───┘
         ▲       3 чистых цикла подряд                                        │
         │                                                                    │ svc_ok +
         │  вес возвращён 10% → 50% → 100%,                                   │ smoke-тест
         │  по 2 чистых цикла на ступень      ┌────────────┐                  │
         └────────────────────────────────────│ RECOVERING │<─────────────────┘
                                              └─────┬──────┘
                                                    │ !svc_ok 3 цикла подряд
                                                    └──────────────> DOWN
```

Три сигнала за цикл (ADR 0007): `svc_ok` — проба `app` кластера по адресу
узла и NodePort 31998; `api_ok` — `{api_endpoint}/readyz`; `sli_ok` — error
rate за 1 мин из Thanos ≤ 0.05. Недоступность определяется только пробой
сервиса: PodChaos сценария 1 не трогает API-сервер, а отказ control plane
при живом сервисе не повод переключать трафик.

Реализация (ADR 0028): адрес пробы — из Backend `app-<cluster>` шлюза, то есть
ровно туда, куда идёт трафик; SLI без ряда (Thanos недоступен, трафика нет)
неизвестен и считается нормой — инвариант 6. Автомат — чистая функция
(`dr-controller/internal/fsm`), действия выводятся из состояний каждый цикл
(level-triggered): веса маршрута и `down` в каталоге сводятся к желаемым, так что
неудачная запись и рестарт DR лечатся следующим циклом. При рестарте кластер с
`down` в каталоге или весом 0 стартует в DOWN с продолжением открытого инцидента.

### 6.2. Защита от флаппинга

Асимметричные переходы (ADR 0007): период опроса 10 с.

| Переход | Условие |
|---|---|
| HEALTHY → DEGRADED | 1 цикл любого сигнала не в норме |
| DEGRADED → HEALTHY | 3 чистых цикла подряд |
| DEGRADED → DOWN | `!svc_ok` 3 цикла подряд в DEGRADED — четвёртая неудачная проба, T = 30 с от первой (ADR 0028); error rate в DOWN не ведёт |
| DOWN → RECOVERING | `svc_ok` + smoke-тест (10 запросов, все 200) |
| RECOVERING → HEALTHY | ступени веса 10% → 50% → 100% от номинала, по 2 чистых цикла на ступень |
| RECOVERING → DOWN | `!svc_ok` 3 цикла подряд |

Асимметрия намеренная: деградация признаётся быстро (TTD < 60 с), возврат
трафика — медленно. Действия: на DOWN вес кластера на `app.fed.local` → 0 и
`down` в каталоге (ADR 0003); на RECOVERING `down` снимается сразу (DR пишет
`healthy`, опрос каталога уточняет по метрикам), трафик возвращается ступенями.
Если DOWN все кластеры, веса остаются номинальными (panic, ADR 0028): обнулять
последний backend бессмысленно, а при ложном DOWN это был бы отказ по вине DR.

Режим `observe` (ADR 0028) — сценарий 1 «без DR» и все прогоны сценария 2
(ADR 0031): автомат, обнаружение и инциденты те же, но маршрут и каталог DR не
трогает.

`TTD = detected_at − fault_injected_at` по переходу в DEGRADED;
`RTO = failover_completed_at − fault_injected_at` по успешному PATCH
HTTPRoute. В сценарии 2 трафик не переключается — эффект планировщика не
смешивается с эффектом DR; режим `observe` гарантирует это по построению,
`peak_state` инцидентов идёт в сводку прогона (ADR 0031). DOWN там достигается:
проба `app` падает на netem backend'а; это оговорка прогона, не брак (ADR 0033).

### 6.3. Переключение трафика

```
DR Controller ──> HTTPRoute.backendRefs[].weight ──> Envoy Gateway ──> кластеры
                  (Kubernetes API control-кластера)
```

Шлюз — Gateway API + Envoy Gateway в control-кластере (ADR 0017); DNS не
участвует. Объект переключения — маршрут `app.fed.local` долгоживущего сервиса с
backend'ом на каждый кластер и номинальными весами 50/50 (ADR 0011); маршруты
потока `w-<n>` DR не трогает.

Возврат трафика — поэтапный: 10% → 50% → 100% с проверкой метрик между шагами.
Одномоментный возврат 100% на едва поднявшийся кластер — типовой способ
получить второй инцидент сразу после первого.

### 6.4. Измерение TTD / RTO

Контроллер пишет инцидент в каталог — таблица `incidents` (§3.2, ADR 0028):
строка открывается на переходе в DEGRADED и дописывается на каждой фазе.
Ответ `GET /api/v1/incidents`:

```json
{
  "id": 7,
  "cluster_id": "cluster-b",
  "dr_mode": "active",
  "peak_state": "down",
  "fault_injected_at": "...",
  "detected_at": "...",
  "failover_started_at": "...",
  "failover_completed_at": "...",
  "recovery_started_at": "...",
  "recovery_completed_at": "...",
  "fault_ref": "podchaos/chaos-mesh/scenario1-...@cluster-b",
  "details": {"detected_signals": {"svc": false, "api": true, "sli": "unknown"}, "failovers": 1},
  "ttd_seconds": 0,
  "rto_seconds": 0
}
```

`fault_injected_at` берётся из Chaos Mesh, а не из наблюдений контроллера —
иначе TTD измерялся бы от момента, когда контроллер заметил, что что-то не так,
то есть был бы тождественно нулевым. Пишет его раннер сценария
(`experiments/dr/run.sh`): самое раннее событие Apply эксперимента, `PATCH` с
ролью admin. TTD и RTO не хранятся, а выводятся из времён в ответе.

**RPO не включён**: показатель снят (ADR 0010). Число потерянных запросов за
инцидент считает раннер по запросам k6 (CSV с отметкой каждого запроса) в окне
от `fault_injected_at` до `failover_completed_at` и кладёт рядом с инцидентом в
`summary.json` прогона; связь с записью каталога — по `fault_ref` и `id`. Без
failover (режим `observe`) окно — весь отказ.

---

## 7. Стенд

### 7.1. Топология

```
хост-машина
├── k6 + генератор workload'ов    вне федерации (ADR 0008, 0015)
├── kind cluster "control"        управляющий контур
│   ├── registry + postgres           ns federation, каталог (ADR 0025), хост 8000
│   ├── scheduler                     ns federation, CR Workload, режим meta|naive (ADR 0027)
│   ├── dr-controller                 ns federation, режим active|observe (ADR 0028)
│   ├── envoy-gateway                 шлюз, вход для k6 (ADR 0017)
│   ├── prometheus + thanos sidecar   k6 remote write, метрики шлюза
│   ├── thanos-query                  единый PromQL над всеми sidecar'ами
│   ├── thanos-rule                   federation:*, burn-rate алерты
│   ├── jaeger                        трейсы, UI и API на хост 16686 (ADR 0029)
│   └── grafana
├── kind cluster "cluster-a"      федерация: workloads, Prometheus + sidecar,
└── kind cluster "cluster-b"      OTel Collector (ADR 0029), Kyverno (ns kyverno)
                                  с двумя ClusterPolicy, Chaos Mesh (ns chaos-mesh, ADR 0028)
    (cluster-c — позже, при наличии ресурсов)
```

Три кластера kind на одной машине: control и федерация из двух. Решение от
2026-09-05: у машины разработчика 16 GB RAM и docker-VM на 8 GB, третий
кластер федерации вместе с Prometheus, Thanos, Kyverno и Chaos Mesh туда не
помещается. spec допускает 2–3, все три сценария работают на двух. Третий
кластер (`cluster-c`) добавляется после того, как контур заработает, если
ресурсы позволят.

Следствие для сценария 2: после деградации одного кластера у планировщика
остаётся единственный кандидат. Сравнение с наивным остаётся корректным —
round-robin продолжает отправлять половину размещений в деградировавший
кластер, — но эффект scoring при трёх кластерах был бы нагляднее.

### 7.2. Сетевая связность

Отдельная проблема kind: кластеры по умолчанию не видят друг друга. Требуется
общая docker-сеть и обращение по внутренним адресам контейнеров. Это влияет на
всё: доступ Querier'а к sidecar'ам, доступ Meta-Scheduler'а к API-серверам,
доступ шлюза к точкам входа сервиса.

Все кластеры живут в docker-сети `kind`; `api_endpoint` в каталоге и
kubeconfig'и компонентов контура — внутрисетевые адреса из
`kind get kubeconfig --internal`, а не `localhost:port` (ADR 0021). Узлы
федерации из control адресуются по IP из `docker inspect`: имена контейнеров
из подов резолвятся на OrbStack, но это свойство рантайма (ADR 0022).

### 7.3. Генератор нагрузки

**k6** на хост-машине (ADR 0008): сценарии в `experiments/load/`, постоянные
50 RPS (`constant-arrival-rate`), клиентские метрики через remote write в
Prometheus control-кластера. Постоянная нагрузка предпочтительнее ramp-up: при
сравнении «до/после» переменная нагрузка вносит второй источник изменения
метрик.

Структура прогона: 3 мин baseline → отказ → 5 мин → снятие → 5 мин
восстановления; три повтора на конфигурацию; параметры прогона — в
`experiments/results/<run-id>/meta.json`.

### 7.4. Тестовый сервис

Собственный Go-сервис `test-service/`, один бинарь с `--role=frontend|backend`
(ADR 0014). В каждом кластере федерации — один долгоживущий `backend` и по
одному `frontend` на workload. `frontend` вызывает `backend` с таймаутом 1 с и
отвечает `502` при ошибке или таймауте: так отказ, внесённый в backend
(сетевая деградация сценария 2), становится виден в серверных метриках
frontend'а, которые и читает каталог (ADR 0016).

### 7.5. Поток workload'ов

Workload — экземпляр `frontend` с именем `w-<n>`: CR `Workload` в control
(запрос на размещение, ADR 0027) → Deployment + Service NodePort
`30000 + n mod 2000` в выбранном кластере + `Backend` и `HTTPRoute` для
хоста `w-<n>.fed.local` на шлюзе (ADR 0015, 0022).

Отдельно от потока в каждом кластере федерации живёт `app` — тот же
`frontend` с двумя репликами и фиксированным NodePort 31998, а на шлюзе —
один маршрут `app.fed.local` с весами по кластерам. Это объект переключения
трафика для DR Controller'а, цель его пробы и носитель SLO в сценарии 1
(ADR 0011). Генератор на хосте каждые `N = 30 с` подаёт новый workload и
удаляет старше `TTL = 180 с`; живых около шести. k6 распределяет 50 RPS
равномерно по живым workload'ам заголовком `Host`. Error rate сценария 2 —
доля неуспешных запросов по всем живым workload'ам, в двух рядах: клиентском
(k6, приёмочный) и серверном (Thanos), окно — отказ по событиям Chaos Mesh
(ADR 0031). В сценарии 2 k6 дополнительно шлёт фоновые 5 RPS в `app.fed.local`:
иначе у кластера без workload'ов потока нет SLI, и scoring получает
нейтральные 0.5 (ADR 0030).

---

## 8. Структура репозитория

```
.
├── CLAUDE.md
├── Brewfile                     зависимости хоста
├── .env.example                 формат .env с токенами каталога (ADR 0018)
├── docs/
│   ├── vision.md
│   ├── spec.md
│   ├── architecture.md          (этот файл)
│   ├── open-questions.md        вопросы без ответа
│   └── decisions/               ADR, индекс в README.md
├── Makefile                     make stand-up / stand-down / load / lint / test
├── stand/                       развёртывание стенда, см. stand/README.md
│   ├── clusters.yaml            атрибуты кластеров: источник для ConfigMap и каталога
│   ├── kind/                    конфигурации кластеров
│   ├── federation/              namespace'ы, backend тестового сервиса
│   ├── gateway/                 EnvoyProxy, GatewayClass, Gateway
│   ├── observability/           federation/: Prometheus + sidecar, OTel Collector; control/: Querier, Ruler, Grafana, Jaeger
│   ├── registry/                ns federation, PostgreSQL, каталог (задача 4)
│   ├── scheduler/               CRD Workload (generate), RBAC, Deployment планировщика
│   ├── dr-controller/           RBAC, Deployment и Service DR Controller'а
│   ├── chaos/                   values чарта Chaos Mesh
│   ├── policies/                Kyverno: values чарта, ClusterPolicy, tests/ для kyverno test
│   └── scripts/                 up / down / status / image / scheduler-mode / dr-mode / render-*
├── test-service/                Go, frontend + backend, один бинарь
├── registry/                    FastAPI + PostgreSQL
│   ├── app/
│   ├── migrations/
│   └── tests/
├── scheduler/                   Go, meta + naive (ADR 0027)
│   ├── main.go                  флаги/env, manager
│   ├── api/v1alpha1/            CRD Workload
│   └── internal/                scoring, registry (клиент каталога), placement, clusters, controller
├── dr-controller/               Go (ADR 0028)
│   ├── main.go                  флаги/env, manager, /metrics и /status
│   └── internal/                fsm, probe, thanos, registry, gateway, monitor
├── experiments/
│   ├── lib.sh                   общее раннеров: времена Chaos Mesh, связь инцидента с отказом
│   ├── chaos/                   манифесты Chaos Mesh: сценарии 1 и 2, проверки DR
│   ├── load/                    steady.js, run.sh, export.sh, workloadgen/
│   ├── policy/                  run.sh: сценарий 3, нарушение политики в обход планировщика; series.sh + compare.py: серия
│   ├── dr/                      run.sh + summary.py: сценарий 1; series.sh + compare.py: серия; check.sh: проверка DR
│   ├── scheduler/               run.sh + summary.py: сценарий 2; series.sh + compare.py: серия
│   ├── traces/                  export.py: трейсы прогона и разложение задержки; run.sh: замер
│   └── results/                 сырые данные прогонов: series/ из Thanos, registry/ из каталога
└── analysis/                    plots.py: графики; task9.py + scoring.py: гипотезы и sensitivity (task9/); scenario1/–3/: серии
```

Разделение `scheduler` и `dr-controller` на отдельные модули, а не один
репозиторий-монолит с двумя бинарями: они разрабатываются в разные недели
(6–9 и 8–10 по плану) и могут вестись параллельно без конфликтов.

`experiments/results/` коммитится в git. Сырые данные экспериментов — основной
результат НИР, и они должны переживать переустановку стенда.

---

## 9. Инварианты

Свойства, которые не должны нарушаться ни при каких изменениях. Полезны как
чек-лист при ревью.

1. **Наблюдатель вне наблюдаемого.** Ни один компонент контура не размещается
   в кластере федерации.
2. **Каталог — единственный источник состояния для планировщика.**
   Meta-Scheduler не ходит в Prometheus напрямую.
3. **Оба планировщика соблюдают hard constraints.** Разница только в soft.
4. **Два рубежа политик независимы.** Отключение Meta-Scheduler'а не должно
   приводить к нарушению политик — Kyverno продолжает работать.
5. **Каждое решение фиксируется.** Размещение без записи в `placement_decisions`
   — дефект, а не оптимизация.
6. **`unknown` ≠ `down`.** Отсутствие метрик не является основанием для failover.
7. **Время внесения отказа берётся из инструмента внесения отказа** — Chaos
   Mesh или раннер сценария (ADR 0023). Не из наблюдений контура.

---

## 10. ADR

Все решения — `docs/decisions/`, индекс в `docs/decisions/README.md`.
Зарезервированных номеров нет; новый ADR получает следующий свободный номер.
