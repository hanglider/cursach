# ADR 0009. Идентичность кластера: префикс `federation.spbu/`, ConfigMap, атрибуты стенда

Дата: 2026-09-13. Закрывает open-questions 3.6 и 3.10.

## Статус

Принят.

## Контекст

Hard constraints опираются на атрибуты кластера (регион, провайдер,
сертификации), которые должны существовать согласованно в двух местах: в
каталоге (фильтрация, ADR 0018) и внутри самого кластера (Kyverno, ADR 0019).
architecture §4.2 предлагал «метки на уровне самого кластера», но в
Kubernetes нет объекта «кластер», на который можно повесить метки. Требования
workload'а нужно выражать на самом ресурсе.

## Решение

**Префикс** всех меток и аннотаций федерации — `federation.spbu/`.

**Идентичность кластера** хранится в ConfigMap `cluster-identity` в
namespace `federation-system` каждого кластера федерации:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: cluster-identity
  namespace: federation-system
data:
  cluster-id: cluster-a
  region: eu-west
  provider: local
  certifications: pci-dss,iso-27001   # через запятую, в нижнем регистре
```

ConfigMap выбран, потому что Kyverno читает ConfigMap через `context`
штатно и в admission, и в background-режиме, без дополнительных вызовов API.
Node-метки отклонены: их пришлось бы держать согласованными на всех узлах и
читать через `apiCall`.

Источник истины для значений — один файл в репозитории
(`stand/clusters.yaml`); из него `make stand-up` и создаёт ConfigMap в
кластере, и регистрирует кластер в каталоге. Каталог при регистрации читает
ConfigMap по `api_endpoint` и отказывает при расхождении с переданными
атрибутами.

**Требования workload'а** — аннотации на ресурсе (Deployment, Job):

| Аннотация | Значение |
|---|---|
| `federation.spbu/require-region` | один регион |
| `federation.spbu/require-certification` | список через запятую, требуются все |
| `federation.spbu/require-cpu` | минимум CPU в кластере, в единицах k8s |
| `federation.spbu/require-memory-gi` | минимум памяти, Gi |

Отсутствие аннотации — отсутствие требования.

**Метки, которые проставляет планировщик** на созданные ресурсы:
`federation.spbu/workload: w-<n>`, `federation.spbu/scheduler: meta|naive`,
`federation.spbu/decision-id: <id из placement_decisions>`. Звено сервиса —
стандартная метка `app.kubernetes.io/component: frontend|backend`.

**Атрибуты кластеров стенда:**

| Кластер | region | provider | certifications |
|---|---|---|---|
| cluster-a | eu-west | local | pci-dss, iso-27001 |
| cluster-b | eu-central | local | iso-27001 |

Сценарий 3: workload с `require-certification: pci-dss` допустим только в
`cluster-a`; попытка положить его в `cluster-b` отклоняется Kyverno и
фиксируется в audit log. Workload'ы сценария 2 без требований к
сертификации, чтобы оба кластера были кандидатами.

## Последствия

Плюсы:

- Одно место в кластере и один файл в репозитории; расхождение ловится при
  регистрации.
- Kyverno-политики получаются простыми: сравнение аннотации с полем ConfigMap.

Минусы:

- ConfigMap редактируется любым, у кого есть права в namespace; это граница
  доверия, приемлемая на стенде и неприемлемая в production.
- Изменение атрибутов кластера (например, добавление сертификации) требует
  обновить и ConfigMap, и каталог; делается только через `stand/clusters.yaml`
  и повторную регистрацию, вручную — нет.
- Формулировка architecture §4.2 «метки на уровне кластера» заменена на
  ConfigMap; документ поправлен.
