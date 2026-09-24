#!/usr/bin/env bash
# Выгрузка сырых рядов прогона из Thanos Querier (ADR 0002).
#
#   experiments/load/export.sh <run-id>
#
# Окно: [started_at − 60 с, finished_at + 60 с] из meta.json, шаг 5 с.
# Результат: experiments/results/<run-id>/series/<имя>.json (формат
# Prometheus matrix) и series/index.json со списком запросов; registry/*.csv —
# что за окно видел каталог: снимки метрик, решения, нарушения, инциденты DR (ADR 0025, 0028).
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
RUN_ID=${1:?run-id}
RESULTS_ROOT=${RESULTS_ROOT:-$ROOT/experiments/results}
THANOS_URL=${THANOS_URL:-http://127.0.0.1:10902}
STEP=${STEP:-5s}
PAD=${PAD:-60}
OUT="$RESULTS_ROOT/$RUN_ID"
QUERIES="$ROOT/experiments/load/export-queries.tsv"

log() { printf '%s export.sh: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

[ -f "$OUT/meta.json" ] || { log "нет $OUT/meta.json"; exit 1; }
curl -sf "$THANOS_URL/-/healthy" >/dev/null || { log "Thanos Querier недоступен на $THANOS_URL"; exit 1; }

start=$(( $(jq -r '.started_at | fromdateiso8601' "$OUT/meta.json") - PAD ))
end=$(( $(jq -r '.finished_at | fromdateiso8601' "$OUT/meta.json") + PAD ))
mkdir -p "$OUT/series"

index='[]'
while IFS=$'\t' read -r name expr; do
  case "$name" in ''|'#'*) continue ;; esac
  # partial_response: при отказе целого кластера (ADR 0023, вариант Б) store
  # недоступен, а окно отказа терять нельзя.
  if ! curl -sfG "$THANOS_URL/api/v1/query_range" \
      --data-urlencode "query=$expr" --data-urlencode "start=$start" \
      --data-urlencode "end=$end" --data-urlencode "step=$STEP" \
      --data-urlencode "partial_response=true" --data-urlencode "dedup=true" \
      -o "$OUT/series/$name.json"; then
    log "запрос $name не удался"; exit 1
  fi
  status=$(jq -r .status "$OUT/series/$name.json")
  [ "$status" = "success" ] || { log "запрос $name: $status"; exit 1; }
  n=$(jq '.data.result | length' "$OUT/series/$name.json")
  index=$(jq -c --arg n "$name" --arg e "$expr" --argjson c "$n" '. + [{name:$n, query:$e, series:$c}]' <<<"$index")
  log "$name: $n рядов"
done <"$QUERIES"

jq -n --argjson q "$index" --argjson s "$start" --argjson e "$end" --arg step "$STEP" \
  '{start:$s, end:$e, step:$step, queries:$q}' >"$OUT/series/index.json"

# Каталог: таблицы за то же окно, CSV через psql в поде control. Сверка того,
# что видели планировщик и DR, с рядами Thanos — задача 9.
KC_CONTROL=(kubectl --kubeconfig "$ROOT/stand/kubeconfig/control.kubeconfig" -n federation)
if "${KC_CONTROL[@]}" get deployment/postgres >/dev/null 2>&1; then
  mkdir -p "$OUT/registry"
  dump() {  # dump <имя> <SQL>
    "${KC_CONTROL[@]}" exec deploy/postgres -- psql -q -U registry -d registry \
      -c "COPY ($2) TO STDOUT WITH (FORMAT csv, HEADER)" >"$OUT/registry/$1.csv" \
      || { log "выгрузка $1 не удалась"; exit 1; }
    log "registry/$1: $(( $(wc -l <"$OUT/registry/$1.csv") - 1 )) строк"
  }
  window="BETWEEN to_timestamp($start) AND to_timestamp($end)"
  dump clusters "SELECT * FROM clusters ORDER BY id"
  dump cluster_metrics "SELECT * FROM cluster_metrics WHERE observed_at $window ORDER BY observed_at, cluster_id"
  dump placement_decisions "SELECT * FROM placement_decisions WHERE decided_at $window ORDER BY id"
  dump policy_violations "SELECT * FROM policy_violations WHERE occurred_at $window ORDER BY id"
  dump incidents "SELECT * FROM incidents WHERE detected_at $window ORDER BY id"
else
  log "каталог в control не найден: registry/*.csv не выгружены"
fi
log "готово: $OUT/series, $OUT/registry"
