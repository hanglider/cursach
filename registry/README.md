# Registry: федеративный каталог кластеров

REST-сервис каталога (spec §4.3, architecture §3; ADR 0003, 0004, 0018, 0025).
Разворачивается в control-кластере через `make stand-up`; здесь — как
запускать локально и тестировать.

```bash
uv sync                      # окружение, Python из .python-version
uv run pytest -q             # правила, API, опрос; PostgreSQL поднимает testcontainers (нужен docker)
uv run ruff check . && uv run ruff format --check .
```

Переменные окружения (`app/config.py`):

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `REGISTRY_DATABASE_URL` | — | `postgresql://user:pass@host:5432/db` |
| `REGISTRY_TOKEN_SCHEDULER`, `REGISTRY_TOKEN_DR_CONTROLLER`, `REGISTRY_TOKEN_ADMIN` | пусто = роль выключена | bearer-токены ролей (ADR 0018) |
| `REGISTRY_THANOS_URL` | `http://thanos-query.observability.svc:10902` | источник метрик |
| `REGISTRY_POLL_INTERVAL_S` | `30` | период опроса |
| `REGISTRY_POLL_ENABLED` | `true` | выключить опрос (тесты, отладка) |
| `REGISTRY_IDENTITY_CHECK` | `true` | проверять ConfigMap `cluster-identity` при регистрации |
| `REGISTRY_KUBECONFIG_DIR` | `/etc/registry/kubeconfig` | `<id>.kubeconfig` кластеров федерации |
| `REGISTRY_WATCH_ENABLED`, `REGISTRY_WATCH_INTERVAL_S` | `true`, `10` | watcher нарушений Kyverno (ADR 0026) |
| `REGISTRY_ERROR_RATE_THRESHOLD`, `REGISTRY_LATENCY_BUDGET_MS` | `0.05`, `200` | пороги `degraded` |

Структура: `app/rules.py` — hard constraints и статус как чистые функции;
`app/poller.py` — цикл опроса Thanos; `app/watcher.py` — разбор событий и
PolicyReport'ов Kyverno и запись нарушений; `app/api/` — эндпоинты;
`migrations/` — SQL, применяется при старте. OpenAPI:
`http://127.0.0.1:8000/docs` на стенде.
