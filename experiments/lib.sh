#!/usr/bin/env bash
# Общее для раннеров сценариев с Chaos Mesh (experiments/dr, experiments/scheduler).
# Подключается после stand/scripts/lib.sh: нужны kc, CHAOS_NAMESPACE, REGISTRY_URL и
# REGISTRY_TOKEN_ADMIN из load_env.

now_iso() { gdate -u +%FT%T.%3NZ 2>/dev/null || date -u +%FT%TZ; }   # gdate — coreutils из Brewfile

# chaos_times <кластер> <вид> <имя> — печатает "<первый Apply> <последний Recover>" из
# статуса эксперимента Chaos Mesh или пусто. Время внесения — отсюда (инвариант 7).
chaos_times() {
  kc "$1" -n "$CHAOS_NAMESPACE" get "$2" "$3" -o json 2>/dev/null | jq -r '
    [.status.experiment.containerRecords[]?.events[]? | select(.type == "Succeeded")] as $e
    | ([$e[] | select(.operation == "Apply") | .timestamp] | min) as $a
    | ([$e[] | select(.operation == "Recover") | .timestamp] | max) as $r
    | "\($a // "") \($r // "")"'
}

# wait_chaos_applied <кластер> <вид> <имя> — ждёт Apply до 60 с, печатает его время.
wait_chaos_applied() {
  local applied=""
  for _ in $(seq 1 60); do
    read -r applied _ <<<"$(chaos_times "$@")" || true
    [ -n "$applied" ] && break
    sleep 1
  done
  [ -n "$applied" ] || return 1
  echo "$applied"
}

# wait_chaos_recovered <кластер> <вид> <имя> — ждёт Recover до 60 с, печатает его время
# или пусто.
wait_chaos_recovered() {
  local recovered=""
  for _ in $(seq 1 30); do
    read -r _ recovered <<<"$(chaos_times "$@")" || true
    [ -n "$recovered" ] && break
    sleep 2
  done
  echo "$recovered"
}

registry_admin() { curl -sf -H "Authorization: Bearer $REGISTRY_TOKEN_ADMIN" "$@"; }

# link_incident <кластер> <с какого момента искать> <время внесения> <fault_ref> — первый
# инцидент кластера, обнаруженный не раньше внесения, получает fault_injected_at и fault_ref
# (роль admin, ADR 0028). Печатает id или пусто. Обе отметки сравниваются до секунды: у
# Chaos Mesh точность 1 с, дробную часть каталога отбрасываем.
link_incident() {
  local incidents id
  incidents=$(registry_admin -G "$REGISTRY_URL/api/v1/incidents" --data-urlencode "cluster_id=$1" \
    --data-urlencode "from=$2") || return 0
  id=$(jq -r --arg a "$3" '
    def ts: sub("\\.[0-9]+"; "") | sub("\\+00:00$"; "Z") | fromdateiso8601;
    [.[] | select((.detected_at | ts) >= ($a | ts))] | first | .id // empty' <<<"$incidents")
  [ -n "$id" ] || return 0
  registry_admin -X PATCH -H 'Content-Type: application/json' "$REGISTRY_URL/api/v1/incidents/$id" \
    -d "$(jq -n --arg f "$3" --arg r "$4" '{fault_injected_at:$f, fault_ref:$r}')" >/dev/null
  echo "$id"
}

# k6_remote_write <url Prometheus> — если Prometheus control готов, дописывает в массив
# K6_OUT флаг remote write (ADR 0008) и экспортирует его переменные; иначе код 1.
# Вызывается без подоболочки: иначе export не дойдёт до k6.
k6_remote_write() {
  curl -sf "$1/-/ready" >/dev/null 2>&1 || return 1
  export K6_PROMETHEUS_RW_SERVER_URL="$1/api/v1/write"
  export K6_PROMETHEUS_RW_TREND_STATS="p(95),p(99),avg,max"
  K6_OUT+=(-o experimental-prometheus-rw)
}
