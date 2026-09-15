# ADR 0027. Реализация планировщика: CR `Workload` как запрос на размещение, цикл reconcile, режимы meta и naive, адрес узла из API кластера

Дата: 2026-09-15. Реализация задачи 6 плана; закрывает отложенное в ADR 0015
(форма запроса на размещение, кто заводит маршрут на шлюзе), уточняет
ADR 0020 и 0022, architecture §5 и §7.5.

## Статус

Принят.

## Решение

1. **Запрос на размещение — CRD `Workload`** (`federation.spbu/v1alpha1`,
   namespaced) в control-кластере, namespace `federation`. `spec`: `port`
   (NodePort в кластере федерации), `image`, `replicas`. Hard constraints —
   аннотации `federation.spbu/require-*` на самом CR (ADR 0009); планировщик
   копирует их на Deployment без изменений, так что рубеж 2 (Kyverno,
   ADR 0019) проверяет ровно то, что фильтровал рубеж 1. Хост маршрута —
   `<name>.fed.local`. `status`: `phase` (`Pending` | `Placed` | `Rejected` |
   `NoCandidates` | `Error`), `cluster`, `decisionId`, `scheduler`, `host`,
   `nodeAddress`, `message`, `placedAt`. Контроллер реагирует только на
   изменение `generation`: обновления статуса не запускают повторное
   планирование.
2. **Один бинарь, режим — `--scheduler=meta|naive`** (env `SCHEDULER_MODE`,
   ADR 0020). `meta`: score ADR 0005 с параметрами ADR 0006 (веса
   `--weights`, `--latency-budget`, свежесть снимка `--metrics-max-age` 60 с),
   ничья → round-robin. `naive`: round-robin по `eligible`, отсортированному
   по `id`, метрики не читает. Счётчик round-robin один на процесс, общий
   для ничьих и наивного режима; сброс при старте пишется в лог. Режим
   переключается `stand/scripts/scheduler-mode.sh meta|naive`
   (`make scheduler-mode MODE=`): меняет env у Deployment и ждёт rollout,
   `make stand-up` возвращает `meta`.
3. **Цикл reconcile** (architecture §5.1). Аннотации →
   `GET /clusters/eligible`. Пусто: `POST /decisions` с
   `outcome=no_candidates` и всеми отсеянными с `filtered_by`,
   `phase=NoCandidates`, повтор через `--retry-interval` (30 с); каждый
   повтор — отдельная строка решения. Каталог недоступен: решение записать
   нельзя — `phase=Error`, экспоненциальный повтор controller-runtime
   (ADR 0018). Иначе выбор → `POST /decisions` (`outcome=placed`) → `status`
   с кластером и `decisionId` → Deployment и Service в кластере (метки
   `workload`, `scheduler`, `decision-id`, `managed-by=scheduler`) →
   Backend и HTTPRoute на шлюзе → `phase=Placed`. Создание идемпотентно
   (`AlreadyExists` — не ошибка): после сбоя между записью решения и
   созданием объектов планировщик доводит размещение по сохранённому
   `status.cluster`, а не принимает новое решение.
4. **Отказ admission** — HTTP 400/403 с `denied the request` (контракт
   ADR 0026): `PATCH /decisions/{id}` → `rejected`, `POST /violations`
   (`detected_by=kyverno`, `details.reported_via=scheduler`, имя политики и
   правила из текста отказа, `requirements` из аннотаций), `phase=Rejected` —
   терминально, генератор удалит CR по TTL. Любая другая ошибка создания,
   включая 5xx `failed calling webhook` (Kyverno недоступен), — `Error` и
   повтор: это отказ размещения, не нарушение.
5. **Терминальные фазы** `Placed` и `Rejected`: CR не перепланируется, даже
   если изменился spec (планировщик без состояния, ADR 0011 п. 3; workload'ы
   потока живут 180 с).
6. **Финализатор** `federation.spbu/placement`: при удалении CR планировщик
   удаляет Deployment и Service в кластере и Backend с HTTPRoute на шлюзе.
   Недоступный кластер не держит CR вечно: после `--finalize-timeout`
   (2 мин) финализатор снимается с событием `CleanupAbandoned`; объекты в
   кластере умрут вместе с ним или останутся до его возвращения.
7. **Адрес узла — `InternalIP` первого узла из API кластера федерации**, а
   не `docker inspect` на хосте (уточнение ADR 0022): в kind они совпадают
   (проверено: 192.168.117.3/.4), а планировщик живёт в поде без доступа к
   docker. Доступ к кластерам — kubeconfig'и Secret'а `federation-kubeconfigs`
   по имени файла `<cluster-id>.kubeconfig` (соглашение `up.sh`), клиенты
   создаются лениво и кэшируются.
8. **Шлюзовые объекты — `unstructured`**: у `Backend` Envoy Gateway нет
   отдельного Go-модуля, HTTPRoute для единообразия так же; формы совпадают
   с `workloadgen/spec.py`. Клиент для шлюза — без кэша controller-runtime,
   чтобы не заводить информеры на чужие типы.
9. **`workload_ref` решений и нарушений — `workloads/<name>`**, то есть
   namespace и имя Deployment в кластере федерации, а не CR в control: так
   решение планировщика, нарушение из планировщика и нарушение из watcher'а
   (ADR 0026) сходятся по одному ключу.
10. **Генератор** получил режим `--mode scheduler|direct`. `scheduler` (по
    умолчанию для `run`) создаёт CR, ждёт `Placed` и 200 от шлюза, по TTL
    удаляет CR; `Rejected`/`NoCandidates` — событие в логе, поток не
    останавливается. `direct` (по умолчанию для `place`) — прежний прямой
    путь: дымовой тест отказа Kyverno и сценарий 3 остаются «в обход
    планировщика». `run.sh` пишет в `meta.json` фактический режим
    планировщика (`scheduler`) и `workloads.placed_by`.
11. **Тесты** без envtest: чистые unit-тесты scoring, клиента каталога
    (`httptest`) и билдеров; reconcile — на fake-клиенте controller-runtime с
    фейковыми каталогом и кластерами (пути Placed, naive round-robin,
    Rejected через перехват создания Deployment, NoCandidates с повтором,
    каталог недоступен, финализатор с недоступным кластером). Сквозная
    проверка — дымовые тесты `stand-up`: `smoke` через планировщик с
    решением из всех кандидатов и `scoring`, `smoke-pci` только в кластер с
    pci-dss и отсев второго в решении, `smoke-deny` напрямую (рубеж 2).
12. **Развёртывание**: `stand/scheduler/` — CRD (генерируется `make generate`,
    controller-gen v0.22.0, коммитится), RBAC, Deployment с `Recreate` и
    одной репликой без leader election; образ `federation/scheduler:dev`
    собирается `image.sh`. Стек: Go 1.27, controller-runtime v0.25.1,
    client-go v0.37.0 под Kubernetes 1.37 стенда.

## Последствия

Плюсы:

- Каждое размещение — строка `placement_decisions` со всеми кандидатами,
  снимком метрик, слагаемыми score и параметрами scoring (ADR 0004, 0006):
  офлайн-пересчёт и sensitivity-анализ задачи 9 не требуют повторных
  прогонов.
- Оба режима проходят один и тот же код фильтрации, создания объектов и
  записи решения; разница — строго в функции выбора (инвариант 3).
- Планировщик не зависит от хоста: адрес узла, kubeconfig'и и токен —
  внутри control-кластера.

Минусы:

- Форма workload'а описана дважды — в Go (планировщик) и в Python
  (генератор, прямой режим); расхождение ловится только дымовыми тестами.
- Решение `placed` записывается до создания объектов; при сбое между ними
  остаётся решение без ресурса до следующего reconcile (сверка по метке
  `decision-id`, ADR 0025).
- `NoCandidates` даёт по строке решения на каждый повтор (30 с): в анализе
  такие workload'ы группируются по `workload_ref`.
- Переключение meta/naive перезапускает под: сброс round-robin, пауза
  размещений на время rollout; между прогонами это приемлемо (ADR 0020).
- Один экземпляр без leader election: недоступность планировщика
  останавливает поток размещений, что видно в `workloadgen.log` как
  `not_placed`.
- Стенд с планировщиком: ещё один образ при `stand-up` (сборка Go в docker
  около минуты при холодном кэше модулей).
