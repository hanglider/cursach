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
| `control` | ns `federation`: PostgreSQL + каталог `registry`, NodePort 30800 → хост 8000 | 4 |
| `control` | ns `federation`: планировщик `scheduler` (CRD `Workload`, режим meta или naive) | 6 |
| `cluster-a`, `cluster-b` | ns `federation-system` с ConfigMap `cluster-identity`; ns `workloads` с `backend`, долгоживущим `app` и frontend'ами потока | 1 |
| `cluster-a`, `cluster-b` | ns `observability`: Prometheus + Thanos sidecar (StoreAPI на NodePort 30901) | 2 |
| `cluster-a`, `cluster-b` | ns `kyverno`: Kyverno (admission, background, reports); ClusterPolicy `require-region-match`, `require-certification` | 5 |
| хост | k6, генератор workload'ов, экспорт результатов (`experiments/load/`), раннер сценария 3 (`experiments/policy/`) | 1, 2, 5 |

Chaos Mesh — задача 8, DR-контроллер — задача 7 (в `control/federation`).

## Порты на хосте

| Хост | Что | Зачем |
|---|---|---|
| 8080 | Envoy Gateway | k6 и генератор, заголовок `Host: <w>.fed.local` |
| 9090 | Prometheus control | remote write из k6, ручные запросы |
| 10902 | Thanos Querier | единый PromQL, `experiments/load/export.sh` |
| 3000 | Grafana | дашборд «Federation SLO», anonymous Viewer, admin/admin |
| 8000 | Каталог | регистрация из `up.sh`, `export.sh`, ручные запросы с токеном из `.env` |
| 16686 | зарезервирован под Jaeger (задача 3) | порт проброшен заранее, чтобы не пересоздавать `control` |

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
(`scripts/render-identity.sh`) и регистрирует кластеры в каталоге
(`scripts/register-clusters.sh`).

## Каталог

Каталог (`registry/`, ADR 0018, 0025) живёт в `control`, ns `federation`,
вместе со своим PostgreSQL. Токены ролей и пароль БД `up.sh` генерирует в
`.env` в корне репозитория (формат — `.env.example`, файл в `.gitignore`) и
кладёт в Secret'ы; внутрисетевые kubeconfig'и федерации — в Secret
`federation-kubeconfigs`, по ним каталог сверяет ConfigMap идентичности при
регистрации. Раз в 30 с каталог опрашивает Thanos Querier и пишет снимок в
`cluster_metrics`; `down` ставит только DR-контроллер (ADR 0003).

```bash
source .env
curl -s -H "Authorization: Bearer $REGISTRY_TOKEN_ADMIN" http://127.0.0.1:8000/api/v1/clusters | jq
curl -sG -H "Authorization: Bearer $REGISTRY_TOKEN_ADMIN" http://127.0.0.1:8000/api/v1/clusters/eligible \
  --data-urlencode certification=pci-dss | jq
open http://127.0.0.1:8000/docs
```

Потерян `.env` — удали Secret'ы `registry-tokens` и `registry-db` в
`control/federation` вместе с PVC `postgres-data` и повтори `make stand-up`:
пароль БД нельзя сменить без пересоздания данных.

## Политики

Kyverno (`policies/`, ADR 0019, 0026) стоит в каждом кластере федерации;
две ClusterPolicy сравнивают аннотации `federation.spbu/require-region` и
`federation.spbu/require-certification` на Deployment и Job с ConfigMap
`cluster-identity`. Ресурсы без аннотаций проходят. Отказ admission виден
как событие `PolicyViolation` в ns `default` кластера; каталог читает такие
события и PolicyReport'ы раз в 10 с и пишет их в `policy_violations` с
подсказкой допустимых кластеров.

```bash
GEN="uv run --quiet --project experiments/load/workloadgen workloadgen"
$GEN place --name pv-demo --cluster cluster-b --port 31950 \
  --annotate federation.spbu/require-certification=pci-dss   # код 3: отклонено
kubectl --kubeconfig stand/kubeconfig/cluster-b.kubeconfig get events.events.k8s.io -A \
  --field-selector reason=PolicyViolation
source .env
curl -s -H "Authorization: Bearer $REGISTRY_TOKEN_ADMIN" \
  'http://127.0.0.1:8000/api/v1/violations?workload_ref=workloads/pv-demo' | jq
make policy-run RUN=<id> ATTEMPTS=5      # сценарий 3 целиком, summary.json в результатах
kyverno test policies/tests              # политики без кластера
```

## Планировщик

Планировщик (`scheduler/`, ADR 0020, 0027) живёт в `control`, ns `federation`.
Вход — CR `Workload`: `kubectl -n federation get workloads` показывает фазу,
кластер, режим и id решения в каталоге. Режим один на стенд:

```bash
make scheduler-mode              # текущий: meta | naive
make scheduler-mode MODE=naive   # переключить (rollout, счётчик round-robin сбрасывается)
make load RUN=<id>               # генератор создаёт CR, планировщик размещает
MODE=direct make load RUN=<id>   # генератор размещает сам, в обход планировщика
kubectl --kubeconfig stand/kubeconfig/control.kubeconfig -n federation logs deploy/scheduler
source .env
curl -s -H "Authorization: Bearer $REGISTRY_TOKEN_ADMIN" \
  'http://127.0.0.1:8000/api/v1/decisions?workload_ref=workloads/w-1' | jq
```

`make generate` пересобирает deepcopy и CRD (`stand/scheduler/federation.spbu_workloads.yaml`)
после правки `scheduler/api/v1alpha1/`.

## Как устроен путь запроса

```
генератор (хост) ──> control: Workload w-7 (ns federation) ──> планировщик: решение → cluster-b
k6 (хост) ──Host: w-7.fed.local──> 127.0.0.1:8080
   └─ kind extraPortMapping ──> control: NodePort 30080 ──> Envoy
        └─ HTTPRoute w-7 ──> Backend w-7 = <IP узла cluster-b>:30007
             └─ cluster-b: Service w-7 (NodePort) ──> под frontend w-7
                  └─ http://backend.workloads.svc:8080/work
```

Workload `w-<n>` — CR в control плюс четыре объекта (ADR 0015, 0022, 0027):
Deployment + Service NodePort в кластере федерации, Backend + HTTPRoute в
`control/gateway`. Порт `30000 + n mod 2000`. Объекты планировщика помечены
`federation.spbu/managed-by=scheduler` и убираются финализатором CR; объекты
прямого режима — `managed-by=workloadgen`, их находит `workloadgen cleanup`
(он удаляет и CR).

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
| Kyverno | v1.19.1, чарт 3.9.1 | `stand/scripts/lib.sh`, values — `stand/policies/kyverno-values.yaml` |
| PostgreSQL | 17.11-alpine | `stand/registry/postgres.yaml` |
| registry | образ `federation/registry:dev`, собирается локально | `registry/` |
| scheduler | образ `federation/scheduler:dev`, собирается локально; controller-runtime v0.25.1 | `scheduler/go.mod` |
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
- Регистрация кластера отвергнута с 409: `stand/clusters.yaml` разошёлся с
  ConfigMap `cluster-identity` в кластере; повторный `make stand-up`
  перерендерит ConfigMap. 422 — у каталога нет kubeconfig для
  `api_endpoint`: Secret `federation-kubeconfigs` устарел, тоже лечится
  `stand-up`.
- Кластеры в каталоге `unknown`: `kubectl -n federation logs deploy/registry`
  в `control`, событие `metrics_fetch_failed` означает, что Querier не
  отвечает или не видит store'ы федерации.
- Политика не `Ready` (`kubectl get clusterpolicy`): смотри
  `kubectl -n kyverno logs deploy/kyverno-admission-controller`; ConfigMap
  `cluster-identity` должен существовать до применения политик.
- Отказ есть, а записи в каталоге нет: в логах каталога
  `violations_fetch_failed` — kubeconfig в Secret `federation-kubeconfigs`
  устарел (лечится `stand-up`); `watch=false` в событии `started` —
  watcher выключен переменной `REGISTRY_WATCH_ENABLED`.
- Создание Deployment падает с `failed calling webhook`: Kyverno в кластере
  не отвечает, это не нарушение политики; `kubectl -n kyverno get pods`.
  У планировщика это `phase: Error` с тем же текстом в `status.message` и
  повтор с backoff.
- Workload висит в `Pending` или `Error`: `kubectl -n federation describe
  workload <name>` в `control` (события Placed / Rejected / NoCandidates /
  Error) и логи `deploy/scheduler`. `NoCandidates` — каталог не отдал ни
  одного допустимого кластера (`down` или hard constraints), повтор раз в 30 с.
- Остатки после падения планировщика: `kubectl -n workloads delete
  deploy,svc -l federation.spbu/managed-by=scheduler` в кластерах федерации и
  `kubectl -n gateway delete httproute,backend -l federation.spbu/managed-by=scheduler`
  в `control`.
