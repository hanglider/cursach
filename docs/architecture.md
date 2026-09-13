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
> компонента. На 2026-09-13 такие разделы остались только в §5.2 и §6.2
> (вопросы `docs/open-questions.md` §2, отложены до задач 6–7).

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
| Шлюз (Envoy Gateway) | Вне федерации | Единая точка входа трафика, веса по кластерам |
| Thanos sidecar | В каждом кластере | Рядом с локальным Prometheus |
| Prometheus, OTel Collector | В каждом кластере | Сбор локальных метрик |
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

Каждый кластер: `Prometheus` (scrape локальных таргетов) + `Thanos sidecar`
(отдаёт данные наружу по StoreAPI) + `OpenTelemetry Collector` (трейсы).

`Thanos Querier` вне федерации агрегирует все sidecar'ы и предоставляет единый
PromQL-endpoint.

```
cluster-a: Prometheus ──> Thanos sidecar ──┐
cluster-b: Prometheus ──> Thanos sidecar ──┼──> Thanos Querier ──> PromQL API
control:   Prometheus ──> Thanos sidecar ──┘           │
           ▲ k6 remote write, метрики шлюза            └──> Grafana
```

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

# глобальная availability федерации
sum(rate(http_requests_total{tier="frontend",status!~"5.."}[5m]))
  / sum(rate(http_requests_total{tier="frontend"}[5m]))
```

Первые две — вход для scoring-функции. Третья — целевой показатель ≥ 99.0%.

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
    candidates      JSONB NOT NULL,       -- [{cluster, score, filtered_by}]
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
GET    /api/v1/decisions                 выборка для анализа, ?from=&to=&scheduler_kind=

POST   /api/v1/violations                фиксация нарушения политики
GET    /api/v1/violations

GET    /healthz                          liveness
```

Ключевой эндпоинт — `/clusters/eligible`. Он инкапсулирует фильтрацию по hard
constraints на стороне каталога, а не планировщика. Логика фильтрации оказывается
в одном месте и переиспользуется наивным планировщиком (которому тоже нельзя
нарушать политики — иначе сравнение нечестное). Ответ содержит и допустимых
кандидатов с последним снимком метрик, и отсеянных с причиной; `down`
отсеивается, `unknown` и `degraded` остаются (ADR 0018).

**Аутентификация** (ADR 0018): статические bearer-токены на компонент.
Полноценный mTLS для стенда — избыточная сложность; «базовые проверки доверия»
из vision покрываются токенами и проверкой при регистрации, что ConfigMap
идентичности кластера (§4.2) совпадает с переданными атрибутами.

### 3.4. Цикл обновления статуса

Фоновая задача каталога, период **30 секунд** (задан в spec):

1. Для каждого зарегистрированного кластера выполнить PromQL-запросы (§2.4)
   к Thanos Querier
2. Записать результат в `cluster_metrics`
3. Пересчитать `slo_status` по порогам
4. Кластер, не ответивший на запрос — `unknown`, не `down`
5. Статус `down` не трогать: его ставит и снимает только DR Controller
   (ADR 0003)

Различие `unknown` и `down` существенно: `down` объявляет DR Controller по
результатам собственного мониторинга, `unknown` означает лишь, что каталог не
смог получить метрики. Смешение этих состояний даст ложные срабатывания
failover при проблемах с самим Querier'ом.

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

```
watch (Kubernetes API)
   │
   ▼
[workload ожидает размещения]
   │
   ▼
GET /clusters/eligible?<hard constraints из аннотаций>
   │
   ├── пусто ──> записать decision(outcome=no_candidates), событие, выход
   │
   ▼
для каждого кандидата: score = f(error_rate, latency_p95, saturation)
   │
   ▼
argmax(score) ──> создать Deployment/Job в выбранном кластере + HTTPRoute на шлюзе
   │
   ▼
POST /decisions (все кандидаты + скоры + выбор)
```

### 5.2. Scoring-функция

Формула из spec:

```
score = w1*(1 - error_rate) + w2*(1/latency_p95) + w3*(1 - saturation)
```

**Проблема нормализации.** `error_rate` и `saturation` лежат в [0, 1], поэтому
первое и третье слагаемые тоже в [0, 1]. Второе слагаемое — обратная величина
латентности, не ограничена сверху: при p95 = 10 мс даёт 100, при p95 = 1 мс —
1000. Оно подавляет остальные независимо от весов, и планировщик вырождается в
выбор по одной латентности.

`[предложение]` Нормализованный вариант:

```
latency_score = 1 - min(latency_p95 / latency_budget, 1)
score = w1*(1 - error_rate) + w2*latency_score + w3*(1 - saturation)
```

где `latency_budget` = 200 мс, то есть целевой показатель p95 из spec. Все три
слагаемых оказываются в [0, 1], веса становятся интерпретируемыми, и
`latency_budget` связывает scoring с SLO, а не задаёт ещё одну независимую
константу.

Это меняет формулу из spec — обязательно оформить ADR со ссылкой на открытый
вопрос 8.3.

**Веса.** Начальные значения `w1 = 0.5, w2 = 0.3, w3 = 0.2` — error rate
доминирует, поскольку он ближе всего к пользовательскому опыту отказа.
Sensitivity-анализ (как веса влияют на результат в сценарии 2) — самостоятельный
результат работы, а не деталь реализации.

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

---

## 6. DR Controller

### 6.1. Конечный автомат кластера

```
                  error_rate > порог ИЛИ
                  недоступен > T сек
    ┌─────────┐   ────────────────────>   ┌──────────┐
    │ HEALTHY │                            │ DEGRADED │
    └─────────┘   <────────────────────    └────┬─────┘
         ▲        smoke-тест пройден,            │ подтверждено
         │        трафик возвращён               │ N циклов подряд
         │                                       ▼
         │                                 ┌──────────┐
         └─────────────────────────────────│   DOWN   │
                  RECOVERING               └──────────┘
                                                 │
                                                 ▼
                                          failover: трафик
                                          уходит, каталог
                                          обновлён
```

### 6.2. Защита от флаппинга `[предложение]`

Открытый вопрос 8.4. Предлагается стандартный для SRE подход — асимметричные
условия перехода:

| Переход | Условие |
|---|---|
| HEALTHY → DEGRADED | 1 цикл превышения порога |
| DEGRADED → DOWN | N = 3 цикла подряд |
| DOWN → RECOVERING | кластер отвечает + smoke-тест пройден |
| RECOVERING → HEALTHY | M = 5 циклов без деградации, трафик возвращён поэтапно |

Асимметрия намеренная: деградация признаётся быстро (это влияет на TTD < 60 с),
восстановление — медленно. Возврат трафика в кластер, который ещё не
стабилизировался, стоит дороже, чем лишняя минута работы на резерве.

Пороги: `error_rate > 0.05` или недоступность `T = 30 с`, период опроса 10 с.
При периоде 10 с и одном цикле на переход TTD укладывается в требуемые 60 с
с запасом.

### 6.3. Переключение трафика

```
DR Controller ──> HTTPRoute.backendRefs[].weight ──> Envoy Gateway ──> кластеры
                  (Kubernetes API control-кластера)
```

Шлюз — Gateway API + Envoy Gateway в control-кластере (ADR 0017); DNS не
участвует. Веса — на маршруте каждого хоста, по одному backend'у на кластер.

Возврат трафика — поэтапный: 10% → 50% → 100% с проверкой метрик между шагами.
Одномоментный возврат 100% на едва поднявшийся кластер — типовой способ
получить второй инцидент сразу после первого.

### 6.4. Измерение TTD / RTO

Контроллер пишет события в структурированном виде для последующего анализа:

```json
{
  "incident_id": "inc-2026-09-03-001",
  "cluster": "cluster-b",
  "fault_injected_at": "...",
  "detected_at": "...",
  "failover_started_at": "...",
  "failover_completed_at": "...",
  "recovery_started_at": "...",
  "recovery_completed_at": "...",
  "ttd_seconds": 0,
  "rto_seconds": 0
}
```

`fault_injected_at` берётся из Chaos Mesh, а не из наблюдений контроллера —
иначе TTD измерялся бы от момента, когда контроллер заметил, что что-то не так,
то есть был бы тождественно нулевым.

**RPO не включён**: показатель снят (ADR 0010). Число потерянных запросов за
инцидент считает k6 по окну от `fault_injected_at` до
`failover_completed_at`; `analysis/` соединяет его с этой записью по
`incident_id`.

---

## 7. Стенд

### 7.1. Топология

```
хост-машина
├── k6 + генератор workload'ов    вне федерации (ADR 0008, 0015)
├── kind cluster "control"        управляющий контур
│   ├── registry-service + postgres
│   ├── meta-scheduler
│   ├── dr-controller
│   ├── envoy-gateway                 шлюз, вход для k6 (ADR 0017)
│   ├── prometheus + thanos sidecar   k6 remote write, метрики шлюза
│   ├── thanos-querier
│   └── grafana
├── kind cluster "cluster-a"      федерация
└── kind cluster "cluster-b"
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

Workload — экземпляр `frontend` с именем `w-<n>`: Deployment + Service
NodePort `30000 + n mod 2000` в выбранном кластере + `Backend` и `HTTPRoute`
для хоста `w-<n>.fed.local` на шлюзе (ADR 0015, 0022). Генератор на хосте каждые `N = 30 с` подаёт новый workload и
удаляет старше `TTL = 180 с`; живых около шести. k6 распределяет 50 RPS
равномерно по живым workload'ам заголовком `Host`. Error rate сценария 2 —
доля неуспешных запросов по всем живым workload'ам, в двух рядах: серверном
(Thanos) и клиентском (k6).

---

## 8. Структура репозитория

```
.
├── CLAUDE.md
├── Brewfile                     зависимости хоста
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
│   ├── observability/           Prometheus, Thanos, Grafana, OTel (задача 2)
│   ├── policies/                Kyverno-политики (задача 5)
│   └── scripts/                 up / down / status / image / render-identity
├── test-service/                Go, frontend + backend, один бинарь
├── registry/                    FastAPI + PostgreSQL
│   ├── app/
│   ├── migrations/
│   └── tests/
├── scheduler/                   Go, meta + naive
│   ├── cmd/
│   ├── internal/scoring/
│   └── internal/client/         клиент каталога
├── dr-controller/               Go
│   ├── cmd/
│   └── internal/
├── experiments/
│   ├── chaos/                   манифесты Chaos Mesh
│   ├── load/                    steady.js, run.sh, workloadgen/
│   └── results/                 сырые данные прогонов
└── analysis/                    обработка результатов, графики
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
7. **Время внесения отказа берётся из Chaos Mesh.** Не из наблюдений контура.

---

## 10. ADR

Принятые решения — `docs/decisions/`, индекс в `docs/decisions/README.md`.
Номера ниже зарезервированы и ждут ответов на вопросы `docs/open-questions.md`
§2; решаются перед задачами 6–7:

| № | Тема | Открытый вопрос |
|---|---|---|
| 0005 | Нормализация scoring-функции по latency budget | spec 8.3, oq 2.1 |
| 0006 | Начальные веса и план sensitivity-анализа | spec 8.3, oq 2.1 |
| 0007 | Асимметричные пороги переходов, защита от флаппинга | spec 8.4, oq 2.2 |
| 0011 | Судьба размещённых workloads при failover | spec 8.6, oq 2.3 |
| 0012 | Включение HPA в эксперименты или сужение RQ2 | spec 8.2, oq 2.4 |

0011 и 0012 блокирующие: без них неясно, что именно измеряется в сценарии 1
и на какой вопрос отвечает работа.
