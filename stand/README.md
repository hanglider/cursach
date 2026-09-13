# Стенд

Три kind-кластера на одной машине (ADR 0013): `control` с управляющим
контуром и федерация `cluster-a`, `cluster-b`. Всё поднимается из репозитория
одной командой; ручных шагов нет.

```bash
brew bundle install --file=Brewfile --no-upgrade   # один раз
make stand-up        # ~2 минуты с нуля; повторный запуск идемпотентен
make stand-status
make load RUN=<id> [DURATION=3m] [INTERVAL=30] [TTL=180]
make stand-down      # только вручную
```

Требования: OrbStack (или другой docker) с VM не меньше 4 GB; стенд без
observability занимает около 2.5 GB.

## Что где

| Кластер | Компоненты | Задача плана |
|---|---|---|
| `control` | Envoy Gateway + Gateway `stand` (ns `gateway`), NodePort 30080 → хост 8080 | 1 |
| `control` | ns `observability`: Prometheus (remote write от k6, метрики Envoy) + sidecar, Thanos Querier, Thanos Ruler, Grafana | 2 |
| `cluster-a`, `cluster-b` | ns `federation-system` с ConfigMap `cluster-identity`; ns `workloads` с `backend`, долгоживущим `app` и frontend'ами потока | 1 |
| `cluster-a`, `cluster-b` | ns `observability`: Prometheus + Thanos sidecar (StoreAPI на NodePort 30901) | 2 |
| хост | k6, генератор workload'ов, экспорт результатов (`experiments/load/`) | 1, 2 |

Kyverno — задача 5; Chaos Mesh — задача 8. Каталог, планировщик и
DR-контроллер появятся в `control` по мере задач 4, 6, 7.

## Порты на хосте

| Хост | Что | Зачем |
|---|---|---|
| 8080 | Envoy Gateway | k6 и генератор, заголовок `Host: <w>.fed.local` |
| 9090 | Prometheus control | remote write из k6, ручные запросы |
| 10902 | Thanos Querier | единый PromQL, `experiments/load/export.sh` |
| 3000 | Grafana | дашборд «Federation SLO», anonymous Viewer, admin/admin |

## Наблюдаемость

Каждый кластер: Prometheus (scrape 5 с) + Thanos sidecar, raw-манифесты без
оператора (ADR 0024). Recording rules per-cluster `cluster:*`
(`observability/federation/prometheus-rules.yaml`), глобальные `federation:*`
и burn-rate алерты — Thanos Ruler в control
(`observability/control/thanos-rule.yaml`). Метка `cluster` — external label,
внутри Prometheus её нет.

Querier находит sidecar'ы федерации по IP узлов: ConfigMap `thanos-query-sd`
рендерит `scripts/render-thanos-sd.sh` при каждом `stand-up`.

Дашборд генерируется: `python3 observability/control/grafana/dashboards/build.py`,
JSON рядом коммитится; `up.sh` кладёт его в ConfigMap `grafana-dashboards`.

Проверить руками:

```bash
curl -s http://127.0.0.1:10902/api/v1/stores | jq '.data[][] | {name, lastError}'
curl -sG http://127.0.0.1:10902/api/v1/query --data-urlencode 'query=federation:availability:ratio5m' | jq .data.result
open http://127.0.0.1:3000/d/federation-slo
```

Атрибуты кластеров федерации — `stand/clusters.yaml` (ADR 0009). Файл —
единственный источник: из него `up.sh` рендерит ConfigMap идентичности
(`scripts/render-identity.sh`), а с задачи 4 — регистрирует кластеры в
каталоге.

## Как устроен путь запроса

```
k6 (хост) ──Host: w-7.fed.local──> 127.0.0.1:8080
   └─ kind extraPortMapping ──> control: NodePort 30080 ──> Envoy
        └─ HTTPRoute w-7 ──> Backend w-7 = <IP узла cluster-b>:30007
             └─ cluster-b: Service w-7 (NodePort) ──> под frontend w-7
                  └─ http://backend.workloads.svc:8080/work
```

Workload `w-<n>` — четыре объекта (ADR 0015, 0022): Deployment + Service
NodePort в кластере федерации, Backend + HTTPRoute в `control/gateway`. Порт
`30000 + n mod 2000`. Все четыре помечены
`federation.spbu/managed-by=workloadgen`, чтобы `workloadgen cleanup` находил
остатки.

Долгоживущий `app` (ADR 0011) — тот же frontend с двумя репликами в каждом
кластере федерации, NodePort 31998, и один маршрут `app.fed.local` с весами
50/50 (`scripts/render-app-route.sh`). Это объект переключения трафика для
DR-контроллера и цель его пробы; сценарий 1 измеряется на нём.

Генератор (`experiments/load/workloadgen`) до задачи 6 размещает сам, по кругу.
Он добавляет workload в список живых только после того, как Deployment
доступен и шлюз ответил 200 по его хосту; снимает с трафика за `--drain`
секунд до удаления. k6 берёт список с `http://127.0.0.1:9797/live`.

## Kubeconfig

`stand/kubeconfig/` (в `.gitignore`), два набора на кластер:

- `<cluster>.kubeconfig` — с хоста (`kubectl --kubeconfig ...`);
- `<cluster>.internal.kubeconfig` — адреса docker-сети, для компонентов
  в `control` (ADR 0021).

## Версии

| Что | Версия | Где закреплено |
|---|---|---|
| kindest/node | v1.37.0 (digest) | `stand/kind/*.yaml` |
| Envoy Gateway | 1.9.1 | `stand/scripts/lib.sh` |
| Prometheus | v3.14.0 | `stand/observability/federation/prometheus.yaml` |
| Thanos | v0.42.4 | там же, `control/thanos-query.yaml`, `control/thanos-rule.yaml` |
| Grafana | 13.2.1 | `stand/observability/control/grafana.yaml` |
| test-service | образ `federation/test-service:dev`, собирается локально | `test-service/` |

## Если что-то не так

- `make stand-status` — поды, идентичность, маршруты, IP узлов.
- Дымовой тест в конце `up.sh` кладёт workload `smoke` в первый кластер
  федерации и убирает его; если он упал — смотри `kubectl -n workloads
  describe pod` в этом кластере и `kubectl -n gateway get httproute,backend`
  в `control`.
- Логи создания кластеров: `stand/kubeconfig/<cluster>.create.log`.
- Остатки workload'ов после прерванного прогона:
  `uv run --project experiments/load/workloadgen workloadgen cleanup`.
- Querier не видит sidecar федерации: IP узлов сменились после перезапуска
  docker — повторный `make stand-up` перерендерит `thanos-query-sd` и
  маршрут `app`.
- Grafana долго в `0/1`: она стартует около минуты и на старте ест до
  500 MB; лимит 768 Mi.
