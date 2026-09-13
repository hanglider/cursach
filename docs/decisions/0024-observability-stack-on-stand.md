# ADR 0024. Слой наблюдаемости на стенде: raw-манифесты, Thanos Ruler для глобального SLO, определение saturation

Дата: 2026-09-13. Реализация задачи 2 плана; уточняет architecture §2.

## Статус

Принят.

## Контекст

spec §4.2 задаёт Prometheus в каждом кластере и Thanos Querier как единую
точку запросов; ADR 0002 убрал object storage. Открытыми оставались: чем
ставить Prometheus, где считать глобальный SLO (per-cluster Prometheus не
видит соседей), как определить saturation на kind, где все узлы видят один
хост, и с каким разрешением снимать метрики, чтобы TTD < 60 с был измерим.

## Решение

1. **Raw-манифесты, не kube-prometheus-stack.** Prometheus v3.14 как
   Deployment с PVC 2 Gi и Thanos sidecar v0.42 в поде; конфиг и правила —
   ConfigMap'ы в репозитории. Оператор, node-exporter, kube-state-metrics и
   Alertmanager не нужны для вопросов работы и стоили бы больше памяти, чем
   весь контур. Один и тот же манифест применяется в control и в федерации;
   различается только конфиг (external label `cluster`).
2. **Scrape и evaluation каждые 5 с.** TTD < 60 с и структура прогона
   13 минут требуют разрешения лучше стандартных 15 с. Целей мало, объём
   приемлем.
3. **Thanos Ruler в control** считает глобальные правила
   (`federation:*`: availability, error budget, burn rate по окнам 5m/30m/1h/6h,
   p95 федерации) и мультиоконные burn-rate алерты поверх Querier'а, отдавая
   результаты обратно Querier'у по StoreAPI. Alertmanager не ставится: на
   стенде «алерт» — это ряд `ALERTS` в Thanos и панель в Grafana.
4. **Per-cluster recording rules** (`cluster:*`) в каждом Prometheus, без
   `by (cluster)`: метку добавляет sidecar как external label. Два окна:
   5 мин для каталога и scoring, 1 мин для DR (ADR 0007).
5. **Saturation** = max(CPU, память), где CPU — `rate` корневой cgroup узла
   (cAdvisor через прокси API-сервера) к allocatable CPU узла, память —
   working set корневой cgroup к allocatable памяти. Allocatable kubelet в
   `/metrics/cadvisor` не отдаёт (`machine_*` там нет), поэтому `stand-up`
   читает его из объекта Node и кладёт в Prometheus константами
   `node:cpu_allocatable:cores`, `node:memory_allocatable:bytes`. На kind
   allocatable — docker-VM целиком, поэтому значения малы и почти не зависят
   от нагрузки; это ограничение стенда, а не определения.
6. **Querier находит store'ы через file SD** (`--store.sd-files`): ConfigMap
   с IP узлов федерации рендерится при `stand-up` (ADR 0022). Флаг помечен
   deprecated, но работает; формат нового `--endpoint.sd-config` не
   задокументирован и на пробах не распарсился.
7. **Prometheus control** принимает remote write от k6
   (`experimental-prometheus-rw`, trend-статистики `p(95), p(99), avg, max`,
   тег `run_id`) и скрейпит Envoy; через свой sidecar он виден Querier'у как
   `cluster=control`, и экспорт (ADR 0002) забирает клиентские ряды тем же
   путём, что и серверные.
8. **Grafana** с анонимным Viewer и admin/admin; datasource и дашборд
   provisioned из репозитория, JSON дашборда генерируется скриптом
   `build.py`, чтобы не править 500 строк JSON руками.

## Последствия

Плюсы:

- Весь слой — около 15 файлов YAML, читается целиком; никаких CRD сверх
  Gateway API.
- Глобальный SLO и алерты живут там же, где остальной контур, и переживают
  отказ любого кластера федерации (инвариант 1).

Минусы:

- Без оператора нет ServiceMonitor'ов: новая цель скрейпа — правка
  ConfigMap и reload. Для стенда с тремя видами целей приемлемо.
- Sidecar без object storage отдаёт только то, что есть в локальном TSDB:
  при отказе кластера (ADR 0023, вариант Б) окно отказа видно только после
  его возвращения, а экспорт должен запрашивать `partial_response=true`.
- Deprecated-флаг `--store.sd-files` может исчезнуть в будущих версиях
  Thanos; версия закреплена.
- Saturation на стенде малоинформативен; вклад третьего слагаемого scoring
  будет мал (см. ADR 0005).
- Burn-rate алерты с окнами 6 ч на 13-минутных прогонах не срабатывают;
  измеримы только быстрые (1 ч / 5 мин).
