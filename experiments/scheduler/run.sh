#!/usr/bin/env bash
# Сценарий 2, деградация сети (spec §6, ADR 0016, 0030, 0031): NetworkChaos netem —
# задержка 500 мс и потери 10% на backend'е целевого кластера. Нагрузка — поток
# workload'ов через планировщик (50 RPS, ADR 0015) плюс фоновые 5 RPS в app.fed.local
# (ADR 0030).
#
#   experiments/scheduler/run.sh <run-id> [кластер=cluster-b]
#
# Структура (ADR 0008): разогрев генератора до WARMUP_HOSTS живых → k6: BASELINE=180 с →
# отказ FAULT=300 с (снимает Chaos Mesh по duration) → RECOVERY=300 с. Режим планировщика
# meta | naive — фактический, из Deployment (make scheduler-mode MODE=...). DR обязан быть в
# observe (ADR 0031): make dr-mode MODE=observe. Серия с чередованием режимов — series.sh.
#
# Результат: experiments/results/<run-id>/{meta.json, chaos.json, incidents.json,
# summary.json, requests.csv.gz, per-second.csv, k6-summary.json, k6.log, workloadgen.log,
# dr-status-before.json, dr-status-after.json}, series/, registry/ и traces/ (export.sh).
# RESULTS_ROOT=<dir> переопределяет каталог результатов (пробные прогоны).
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=../../stand/scripts/lib.sh
source "$ROOT/stand/scripts/lib.sh"
# shellcheck source=../lib.sh
source "$ROOT/experiments/lib.sh"

RUN_ID=${1:?run-id}
CLUSTER=${2:-cluster-b}
BASELINE=${BASELINE:-180}
FAULT=${FAULT:-300}
RECOVERY=${RECOVERY:-300}
RPS=${RPS:-50}
BACKGROUND_RPS=${BACKGROUND_RPS:-5}          # фон в app, ADR 0030
BACKGROUND_HOST=${BACKGROUND_HOST:-app.fed.local}
INTERVAL=${INTERVAL:-30}                     # поток workload'ов, ADR 0015
TTL=${TTL:-180}
WARMUP_HOSTS=${WARMUP_HOSTS:-3}              # сколько живых workload'ов ждать до старта k6
HEALTHY_WAIT=${HEALTHY_WAIT:-420}            # сколько ждать healthy в каталоге до старта
GATEWAY_URL=${GATEWAY_URL:-http://127.0.0.1:8080}
PROM_URL=${PROM_URL:-http://127.0.0.1:9090}
THANOS_URL=${THANOS_URL:-http://127.0.0.1:10902}
LIVE_LISTEN=${LIVE_LISTEN:-127.0.0.1:9797}
RESULTS_ROOT=${RESULTS_ROOT:-$ROOT/experiments/results}
OUT="$RESULTS_ROOT/$RUN_ID"
CHAOS_NAME="scenario2-$(printf '%s' "$RUN_ID" | tr -c 'a-z0-9-' '-' | cut -c1-40)"
FAULT_REF="networkchaos/$CHAOS_NAMESPACE/$CHAOS_NAME@$CLUSTER"
GEN=(uv run --quiet --project "$ROOT/experiments/load/workloadgen" workloadgen)

log() { printf '%s scheduler/run.sh: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

need k6 jq yq kubectl curl uv gdate
[ -e "$OUT" ] && die "$OUT уже существует; результаты не перезаписываются"
load_env || die "нет $ENV_FILE; его создаёт make stand-up"
printf '%s\n' "${FEDERATION[@]}" | grep -qx "$CLUSTER" || die "кластер $CLUSTER не из федерации: ${FEDERATION[*]}"
for c in "${FEDERATION[@]}"; do
  kc "$c" get crd networkchaos.chaos-mesh.org >/dev/null 2>&1 || die "$c: нет Chaos Mesh; make stand-up"
  left=$(kc "$c" get podchaos,networkchaos -A --no-headers 2>/dev/null || true)
  [ -z "$left" ] || die "$c: остались эксперименты Chaos Mesh: $(tr '\n' ';' <<<"$left")"
done
drift=$(addr_drift)
[ -z "$drift" ] || die "адреса узлов устарели (перезапуск docker?): $drift make stand-up"

# DR — только наблюдатель (ADR 0031): down в каталоге отсеял бы кластер для обоих режимов.
DR_MODE=$("$ROOT/stand/scripts/dr-mode.sh")
[ "$DR_MODE" = observe ] || die "DR в режиме $DR_MODE; сценарий 2 — в observe (ADR 0031): make dr-mode MODE=observe"
SCHEDULER=$("$ROOT/stand/scripts/scheduler-mode.sh") || die "планировщик не развёрнут"
status=$(dr_status) || die "нет ответа /status DR Controller'а"
jq -e 'all(.clusters[]; .state == "healthy" and .weight == .desired_weight and .registry_status != "down")' \
  <<<"$status" >/dev/null || die "стенд не в штатном состоянии: $(jq -c '.clusters' <<<"$status")"

# Каталог видит оба кластера здоровыми: хвост отказа предыдущего прогона ушёл из окна 5 мин.
deadline=$(( $(date +%s) + HEALTHY_WAIT ))
while :; do
  statuses=$(registry_admin "$REGISTRY_URL/api/v1/clusters" | jq -c 'map({(.id): .slo_status}) | add')
  jq -e 'all(.[]; . == "healthy")' <<<"$statuses" >/dev/null && break
  [ "$(date +%s)" -lt "$deadline" ] || die "каталог: не все кластеры healthy за ${HEALTHY_WAIT}s: $statuses"
  log "каталог: $statuses — жду healthy"
  sleep 15
done

mkdir -p "$OUT"
echo "$status" >"$OUT/dr-status-before.json"
TMP=$(mktemp -d)
K6_PID=""
GEN_PID=""
cleanup() {
  [ -n "$K6_PID" ] && kill "$K6_PID" 2>/dev/null || true
  if [ -n "$GEN_PID" ]; then
    kill -INT "$GEN_PID" 2>/dev/null || true
    wait "$GEN_PID" 2>/dev/null || true
  fi
  kc "$CLUSTER" -n "$CHAOS_NAMESPACE" delete networkchaos "$CHAOS_NAME" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT

# --- поток workload'ов -----------------------------------------------------------------
"${GEN[@]}" cleanup
STARTED=$(date -u +%FT%TZ)
log "планировщик: $SCHEDULER; генератор: interval=${INTERVAL}s ttl=${TTL}s"
"${GEN[@]}" --mode scheduler run --interval "$INTERVAL" --ttl "$TTL" --listen "$LIVE_LISTEN" \
  >"$OUT/workloadgen.log" 2>&1 &
GEN_PID=$!
deadline=$(( $(date +%s) + INTERVAL * WARMUP_HOSTS + 120 ))
while :; do
  n=$(curl -sf "http://$LIVE_LISTEN/live" 2>/dev/null | jq '.hosts | length' 2>/dev/null || echo 0)
  [ "$n" -ge "$WARMUP_HOSTS" ] && break
  [ "$(date +%s)" -lt "$deadline" ] || die "живых workload'ов: $n, ждали слишком долго; см. $OUT/workloadgen.log"
  sleep 2
done

# --- нагрузка ---------------------------------------------------------------------------
K6_OUT=(--out "csv=$TMP/k6.csv")
k6_remote_write "$PROM_URL" || log "Prometheus control недоступен на $PROM_URL: без remote write"
TOTAL=$((BASELINE + FAULT + RECOVERY))
K6_STARTED=$(now_iso)
log "живых: $n; k6 ${TOTAL}s: $RPS RPS в поток + $BACKGROUND_RPS RPS в $BACKGROUND_HOST; отказ $CLUSTER через ${BASELINE}s на ${FAULT}s"
K6_CSV_TIME_FORMAT=unix_milli DURATION="${TOTAL}s" RPS="$RPS" GATEWAY_URL="$GATEWAY_URL" \
  LIVE_URL="http://$LIVE_LISTEN/live" BACKGROUND_RPS="$BACKGROUND_RPS" BACKGROUND_HOST="$BACKGROUND_HOST" \
  SUMMARY_PATH="$OUT/k6-summary.json" \
  k6 run --quiet --tag "run_id=$RUN_ID" "${K6_OUT[@]}" "$ROOT/experiments/load/steady.js" >"$OUT/k6.log" 2>&1 &
K6_PID=$!
T0=$(date +%s)

# --- отказ ------------------------------------------------------------------------------
sleep "$BASELINE"
RUNNER_APPLIED_AT=$(now_iso)
CHAOS_NAME="$CHAOS_NAME" FAULT="${FAULT}s" \
  yq '.metadata.name = strenv(CHAOS_NAME) | .spec.duration = strenv(FAULT)' \
  "$ROOT/experiments/chaos/netem-backend.yaml" | kc "$CLUSTER" apply -f - >/dev/null
log "NetworkChaos $CHAOS_NAME применён в $CLUSTER"
INJECTED_AT=$(wait_chaos_applied "$CLUSTER" networkchaos "$CHAOS_NAME") \
  || die "Chaos Mesh не сообщил о внесении отказа: $(kc "$CLUSTER" -n "$CHAOS_NAMESPACE" get networkchaos "$CHAOS_NAME" -o json | jq -c .status)"
log "отказ внесён: $INJECTED_AT (по Chaos Mesh)"

set +e
wait "$K6_PID"
K6_RC=$?
set -e
K6_PID=""
FINISHED=$(date -u +%FT%TZ)
log "k6 завершён (код $K6_RC) через $(( $(date +%s) - T0 )) с; останавливаю генератор"
kill -INT "$GEN_PID" 2>/dev/null || true
wait "$GEN_PID" 2>/dev/null || true
GEN_PID=""

RECOVERED_AT=$(wait_chaos_recovered "$CLUSTER" networkchaos "$CHAOS_NAME")
[ -n "$RECOVERED_AT" ] || log "Chaos Mesh не сообщил о снятии отказа"
kc "$CLUSTER" -n "$CHAOS_NAMESPACE" get networkchaos "$CHAOS_NAME" -o json >"$OUT/chaos.json"
dr_status >"$OUT/dr-status-after.json" 2>/dev/null || true

# Инциденты observe за прогон по всем кластерам: вмешался бы DR или нет (ADR 0031).
linked=$(link_incident "$CLUSTER" "$STARTED" "$INJECTED_AT" "$FAULT_REF")
[ -n "$linked" ] && log "инцидент $linked связан с отказом"
registry_admin -G "$REGISTRY_URL/api/v1/incidents" --data-urlencode "from=$STARTED" >"$OUT/incidents.json"

jq -n \
  --arg run_id "$RUN_ID" --arg started "$STARTED" --arg finished "$FINISHED" --arg k6_started "$K6_STARTED" \
  --arg cluster "$CLUSTER" --arg scheduler "$SCHEDULER" --arg dr_mode "$DR_MODE" \
  --argjson rps "$RPS" --argjson bg_rps "$BACKGROUND_RPS" --arg bg_host "$BACKGROUND_HOST" --argjson k6_rc "$K6_RC" \
  --argjson interval "$INTERVAL" --argjson ttl "$TTL" --argjson warmup "$WARMUP_HOSTS" \
  --argjson baseline "$BASELINE" --argjson fault "$FAULT" --argjson recovery "$RECOVERY" \
  --arg injected "$INJECTED_AT" --arg recovered "$RECOVERED_AT" --arg runner_applied "$RUNNER_APPLIED_AT" \
  --arg ref "$FAULT_REF" --arg spec "$(yq -o=json -I=0 '.spec | pick(["action", "delay", "loss", "direction", "selector"])' "$ROOT/experiments/chaos/netem-backend.yaml")" \
  --arg git "$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)" \
  '{run_id:$run_id, scenario:"2-network-degradation", cluster:$cluster, scheduler:$scheduler, dr_mode:$dr_mode,
    started_at:$started, finished_at:$finished, k6_started_at:$k6_started,
    structure:{baseline_s:$baseline, fault_s:$fault, recovery_s:$recovery, warmup_hosts:$warmup},
    k6:{rps:$rps, background_rps:$bg_rps, background_host:$bg_host, exit_code:$k6_rc},
    workloads:{interval_s:$interval, ttl_s:$ttl, placed_by:"scheduler"},
    fault:{ref:$ref, tool:"chaos-mesh NetworkChaos netem, backend в namespace workloads", spec:($spec | fromjson),
           injected_at:$injected, recovered_at:(if $recovered == "" then null else $recovered end),
           runner_applied_at:$runner_applied,
           note:"injected_at/recovered_at — события Apply/Recover Chaos Mesh, точность 1 с"},
    git_rev:$git}' >"$OUT/meta.json"

# Сводка читает registry/ и series/, поэтому сначала экспорт.
if curl -sf "$THANOS_URL/-/healthy" >/dev/null 2>&1; then
  sleep 15
  RESULTS_ROOT="$RESULTS_ROOT" THANOS_URL="$THANOS_URL" "$ROOT/experiments/load/export.sh" "$RUN_ID"
else
  log "Thanos Querier недоступен на $THANOS_URL: серверные ряды не выгружены"
fi
uv run --no-project --quiet --python 3.13 python "$ROOT/experiments/scheduler/summary.py" "$OUT" "$TMP/k6.csv"
log "готово: $OUT"
jq '{scheduler, valid: .validity.valid, reasons: .validity.reasons,
     error_rate_fault: .client.stream.fault.error_rate, p95_fault_ms: .client.stream.fault.p95_ms,
     placements_fault: .placement.fault, degraded_s: .placement.degraded_s, first_avoid_s: .placement.first_avoid_s}' \
  "$OUT/summary.json"
