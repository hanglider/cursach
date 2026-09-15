# ADR 0026. Реализация задачи 5: Kyverno 1.19 с ClusterPolicy, контракт сообщения политики, watcher нарушений периодическим чтением, замер сценария 3

Дата: 2026-09-15. Реализация задачи 5 плана; уточняет ADR 0019 и 0025,
architecture §3.5, §4.3.

## Статус

Принят.

## Контекст

ADR 0019 задал две ClusterPolicy (регион, сертификации) и watcher нарушений
как фоновую задачу каталога; ADR 0025 отложил watcher до появления Kyverno.
При реализации выяснилось:

1. Актуальный Kyverno — 1.19.1 (чарт 3.9.1). В нём `kyverno.io/v1`
   ClusterPolicy объявлены устаревшими в пользу CEL-политик
   `policies.kyverno.io` (ValidatingPolicy и др.); устарело и
   `spec.validationFailureAction` — действие задаётся в
   `rules[].validate.failureAction`.
2. Ресурс, отклонённый в `Enforce`, не существует. Событие Kyverno
   (`events.k8s.io/v1`, reason `PolicyViolation`, action `Resource Blocked`)
   лежит на политике (`regarding`), ресурс — в `related`, текст — в `note`
   вида `Kind ns/name: [rule] fail (blocked); <message>`. Требования
   workload'а (аннотации `require-*`) из события напрямую не достать.
3. События Kyverno не агрегирует (каждое уникально), TTL событий в
   Kubernetes — 1 час. Нарушения существующих ресурсов (созданных при
   недоступном Kyverno) видны в PolicyReport, по одному на ресурс.
4. По принципу «механизм вместе со способом измерить» нужен замер сценария 3
   до серии экспериментов задачи 8.
5. Политики надо проверять без кластера, иначе каждая правка — `stand-up`.

## Решение

1. **Kyverno 1.19.1** helm-чартом 3.9.1 в namespace `kyverno` каждого
   кластера федерации (`stand/policies/kyverno-values.yaml`): контроллеры
   admission, background (для `background: true`) и reports (PolicyReport);
   cleanup-контроллер выключен; requests урезаны под kind;
   `backgroundScanInterval: 5m`. Версия закреплена в `stand/scripts/lib.sh`,
   установка — шаг `stand-up`, параллельно для кластеров. Control-кластер без
   Kyverno: там нет workload'ов федерации.
2. **Тип политик — ClusterPolicy**, как в ADR 0019 (решение пользователя от
   2026-09-15 при известном устаревании): схема ADR 0009/0019 — ConfigMap
   идентичности через `context`, JMESPath, `background: true` — ложится без
   изменений; офлайн-тест с подстановкой данных ConfigMap подтверждён;
   устаревание при закреплённой версии — только предупреждение. Действие —
   `validate.failureAction: Enforce`. Миграция на CEL — вне работы.
3. **Контракт сообщения политики** — машиночитаемая строка:
   `federation.spbu/require-region=<v> not satisfied: cluster <id> is in region <r>`
   и `federation.spbu/require-certification=<v> not satisfied: cluster <id>
   has certifications <c>`. По ней watcher (и в задаче 6 планировщик)
   восстанавливает требования отклонённого workload'а и вычисляет
   `details.eligible_clusters` тем же кодом, что `POST /violations`.
   Особенность JMESPath: внутри фильтра `[?...]` имена разрешаются
   относительно элемента, поэтому сертификации кластера подставляются
   вложенным `{{ identity.data.certifications }}` как строковый литерал.
4. **Watcher** — фоновая задача каталога (`registry/app/watcher.py`),
   период 10 с (`REGISTRY_WATCH_INTERVAL_S`), **периодический list**, а не
   watch-поток: читает события `events.k8s.io/v1` с `reason=PolicyViolation`
   во всех namespace'ах и PolicyReport'ы с `result: fail` из каждого
   кластера таблицы `clusters` через kubeconfig, подобранный по
   `api_endpoint` (тот же поиск, что у проверки идентичности). Учитываются
   только Deployment и Job. Недоступный кластер даёт событие лога
   `violations_fetch_failed`, остальные обрабатываются.
5. **Дедупликация и время.** Колонка `policy_violations.source_key`
   (миграция 0002) с частичным уникальным индексом: `event/<cluster>/<uid
   события>` и `policyreport/<cluster>/<uid ресурса>/<policy>/<rule>`.
   `occurred_at` — время события или результата из Kyverno;
   `details.detected_at` — момент записи. Их разница — **задержка фиксации
   нарушения**, пишется в лог `violation_recorded` и считается раннером
   сценария 3. `details` записи watcher'а: `reported_via=watcher`, `source`
   (`event` | `policyreport`), `kind`, `rule`, `action`, `message`,
   `requirements`, `eligible_clusters`.
6. **API.** `GET /api/v1/violations` получил фильтр `workload_ref`; в ответе
   появилось `source_key` (`null` у записей планировщика).
7. **Генератор.** `workloadgen place --annotate KEY=VALUE` ставит аннотации
   на Deployment; отказ admission (4xx с `denied the request`) даёт событие
   `rejected`, код выхода 3, Service и маршрут не создаются. До задачи 6 это
   единственный путь размещения «в обход планировщика».
8. **Замер сценария 3** — `experiments/policy/run.sh`
   (`make policy-run RUN=<id> [ATTEMPTS=5]`): N попыток разместить workload с
   `require-certification=pci-dss` в кластер без pci-dss. `summary.json`:
   `attempts`, `rejected`, `admitted` и `violating_deployments_left`
   (целевой показатель «0 нарушений» — оба нули), `recorded`, `eligible_ok`
   (каталог предложил ровно кластеры с pci-dss), `detection_latency_s`
   (max, median). Серия прогонов — задача 8; пробные — через `RESULTS_ROOT`.
9. **Проверки без кластера.** `kyverno test stand/policies/tests` в
   `make test` (CLI — в Brewfile): данные ConfigMap подставляются файлом
   `Values`; 16 проверок на pass / fail / skip. kubeconform проверяет
   ClusterPolicy по схемам datree CRDs-catalog.
10. **Дымовой тест `stand-up`**: `smoke-deny` с pci-dss в первый кластер без
    неё → код 3, Deployment отсутствует, запись в каталоге с корректными
    `eligible_clusters` не позже 60 с.

## Последствия

Плюсы:

- Второй рубеж работает независимо от планировщика (инвариант 4): нарушение
  фиксируется в audit log, кто бы ни создавал ресурс, и с подсказкой
  допустимых кластеров.
- Задержка фиксации измеряется, а не предполагается; сценарий 3 прогоняется
  одной командой и даёт числа, а не «отклонило».
- Политики тестируются офлайн за секунды, контракт сообщения проверен с
  обеих сторон (kyverno test и тест разбора в каталоге).

Минусы:

- Устаревший тип политик: обновление Kyverno за пределы ветки 1.x потребует
  переписать политики на CEL; при apply и в helm — предупреждения.
- `failurePolicy: Fail` у webhook'а Kyverno: при недоступном Kyverno
  создание Deployment/Job в кластере отклоняется целиком. Это отказ
  размещения, не нарушение; планировщик (задача 6) обязан различать 400
  `denied the request` (→ `rejected`, `POST /violations`) и 5xx `failed
  calling webhook` (→ повтор или `no_candidates`).
- Требования восстанавливаются из текста сообщения: изменение формулировки в
  политике без правки регулярного выражения в watcher'е (и наоборот) ломает
  `eligible_clusters`. Смягчается тестами контракта с обеих сторон.
- Периодическое чтение вместо watch: задержка фиксации до 10 с плюс цикл;
  при простое каталога дольше TTL событий (1 ч) отказы admission теряются —
  PolicyReport покрывает только существующие ресурсы.
- Дымовой тест оставляет запись `smoke-deny` в `policy_violations` при
  каждом `stand-up`; она вне окон прогонов, но видна в выгрузках за широкие
  окна и отличима по `workload_ref`.
- Kyverno — три пода на кластер, по `crictl stats` около 400 MiB рабочей
  памяти на кластер (admission 147, background 130, reports 121 MiB в
  cluster-a); kube-apiserver кластера федерации вырос до ~750 MiB (CRD,
  webhook'и, отчёты). Анонимная память узла федерации — около 1.4 GiB против
  ~0.9 GiB до задачи 5. PolicyReport'ы генерируются для всех Deployment и
  Job во всех namespace'ах, включая observability.
- Задержка фиксации в пробном прогоне (5 попыток): медиана 7.7 с, максимум
  8.9 с — определяется периодом чтения 10 с, не Kyverno (событие появляется
  за доли секунды).
