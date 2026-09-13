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
| `cluster-a`, `cluster-b` | ns `federation-system` с ConfigMap `cluster-identity`; ns `workloads` с backend и размещёнными frontend'ами | 1 |
| хост | k6, генератор workload'ов (`experiments/load/`) | 1 |

Prometheus, Thanos, Grafana — задача 2; Kyverno — задача 5; Chaos Mesh —
задача 8. Каталог, планировщик и DR-контроллер появятся в `control` по мере
задач 4, 6, 7.

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
