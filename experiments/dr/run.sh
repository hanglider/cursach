#!/usr/bin/env bash
# Сценарий 1, отказ кластера (spec §6, ADR 0007, 0010, 0011, 0023, 0028): PodChaos
# pod-failure на все поды namespace workloads целевого кластера; трафик k6 — постоянные
# 50 RPS в долгоживущий app.fed.local.
#
#   experiments/dr/run.sh <run-id> [кластер=cluster-b]
#
# Структура (ADR 0008): BASELINE=180 с → отказ FAULT=300 с (снимает Chaos Mesh по
# duration) → RECOVERY=300 с. Режим DR — фактический, из Deployment: переключается заранее,
# make dr-mode MODE=observe — «без DR». Замер: TTD и RTO — по инциденту каталога и времени
# внесения из Chaos Mesh (инвариант 7), потерянные запросы — по k6 (ADR 0010).
#
# Результат: experiments/results/<run-id>/{meta.json, chaos.json, incidents.json,
# summary.json, requests.csv.gz, per-second.csv, k6-summary.json, k6.log, dr-status.json},
# series/ из Thanos и registry/ из каталога (export.sh).
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
HOST=${HOST:-app.fed.local}
GATEWAY_URL=${GATEWAY_URL:-http://127.0.0.1:8080}
PROM_URL=${PROM_URL:-http://127.0.0.1:9090}
THANOS_URL=${THANOS_URL:-http://127.0.0.1:10902}
RETURN_WAIT=${RETURN_WAIT:-240}   # сколько ждать HEALTHY после k6, если возврат трафика не успел
RESULTS_ROOT=${RESULTS_ROOT:-$ROOT/experiments/results}
OUT="$RESULTS_ROOT/$RUN_ID"
CHAOS_NAME="scenario1-$(printf '%s' "$RUN_ID" | tr -c 'a-z0-9-' '-' | cut -c1-40)"
FAULT_REF="podchaos/$CHAOS_NAMESPACE/$CHAOS_NAME@$CLUSTER"

log() { printf '%s dr/run.sh: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

need k6 jq yq kubectl curl uv gdate
[ -e "$OUT" ] && die "$OUT уже существует; результаты не перезаписываются"
load_env || die "нет $ENV_FILE; его создаёт make stand-up"
printf '%s\n' "${FEDERATION[@]}" | grep -qx "$CLUSTER" || die "кластер $CLUSTER не из федерации: ${FEDERATION[*]}"
kc "$CLUSTER" get crd podchaos.chaos-mesh.org >/dev/null 2>&1 || die "$CLUSTER: нет Chaos Mesh; make stand-up"
if [ -n "$(kc "$CLUSTER" get podchaos,networkchaos -A --no-headers 2>/dev/null)" ]; then
  die "$CLUSTER: остались эксперименты Chaos Mesh: $(kc "$CLUSTER" get podchaos,networkchaos -A --no-headers | tr '\n' ';')"
fi

# Штатное состояние до прогона: все кластеры HEALTHY, веса номинальные, down нет.
status=$(dr_status) || die "нет ответа /status DR Controller'а"
jq -e 'all(.clusters[]; .state == "healthy" and .weight == .desired_weight and .registry_status != "down")' \
  <<<"$status" >/dev/null || die "стенд не в штатном состоянии: $(jq -c '.clusters' <<<"$status")"
DR_MODE=$("$ROOT/stand/scripts/dr-mode.sh")
mkdir -p "$OUT"
echo "$status" >"$OUT/dr-status-before.json"

TMP=$(mktemp -d)
K6_PID=""
cleanup() {
  [ -n "$K6_PID" ] && kill "$K6_PID" 2>/dev/null || true
  kc "$CLUSTER" -n "$CHAOS_NAMESPACE" delete podchaos "$CHAOS_NAME" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT

# --- нагрузка -------------------------------------------------------------------------
K6_OUT=(--out "csv=$TMP/k6.csv")
k6_remote_write "$PROM_URL" || log "Prometheus control недоступен на $PROM_URL: без remote write"
TOTAL=$((BASELINE + FAULT + RECOVERY))
STARTED=$(date -u +%FT%TZ)
log "режим DR: $DR_MODE; k6 ${TOTAL}s при $RPS RPS в $HOST; отказ $CLUSTER через ${BASELINE}s на ${FAULT}s"
K6_CSV_TIME_FORMAT=unix_milli DURATION="${TOTAL}s" RPS="$RPS" GATEWAY_URL="$GATEWAY_URL" TARGET_HOST="$HOST" \
  SUMMARY_PATH="$OUT/k6-summary.json" \
  k6 run --quiet --tag "run_id=$RUN_ID" "${K6_OUT[@]}" "$ROOT/experiments/load/steady.js" >"$OUT/k6.log" 2>&1 &
K6_PID=$!
T0=$(date +%s)

# --- отказ ------------------------------------------------------------------------------
sleep "$BASELINE"
RUNNER_APPLIED_AT=$(now_iso)
CHAOS_NAME="$CHAOS_NAME" FAULT="${FAULT}s" \
  yq '.metadata.name = strenv(CHAOS_NAME) | .spec.duration = strenv(FAULT)' \
  "$ROOT/experiments/chaos/pod-failure-workloads.yaml" | kc "$CLUSTER" apply -f - >/dev/null
log "PodChaos $CHAOS_NAME применён в $CLUSTER"

# Время внесения — самое раннее событие Apply в статусе эксперимента (инвариант 7).
INJECTED_AT=$(wait_chaos_applied "$CLUSTER" podchaos "$CHAOS_NAME") \
  || die "Chaos Mesh не сообщил о внесении отказа: $(kc "$CLUSTER" -n "$CHAOS_NAMESPACE" get podchaos "$CHAOS_NAME" -o json | jq -c .status)"
log "отказ внесён: $INJECTED_AT (по Chaos Mesh)"

set +e
wait "$K6_PID"
K6_RC=$?
set -e
K6_PID=""
FINISHED=$(date -u +%FT%TZ)
log "k6 завершён (код $K6_RC) через $(( $(date +%s) - T0 )) с"

RECOVERED_AT=$(wait_chaos_recovered "$CLUSTER" podchaos "$CHAOS_NAME")
[ -n "$RECOVERED_AT" ] || log "Chaos Mesh не сообщил о снятии отказа"
kc "$CLUSTER" -n "$CHAOS_NAMESPACE" get podchaos "$CHAOS_NAME" -o json >"$OUT/chaos.json"

# Возврат трафика (≥ 6 циклов после снятия) обычно укладывается в RECOVERY; если нет — ждём.
for _ in $(seq 1 $((RETURN_WAIT / 5))); do
  jq -e --arg c "$CLUSTER" '.clusters[$c].state == "healthy"' <<<"$(dr_status 2>/dev/null)" >/dev/null 2>&1 && break
  sleep 5
done
dr_status >"$OUT/dr-status-after.json" 2>/dev/null || true

# --- инцидент ---------------------------------------------------------------------------
linked=$(link_incident "$CLUSTER" "$STARTED" "$INJECTED_AT" "$FAULT_REF")
if [ -n "$linked" ]; then
  log "инцидент $linked связан с отказом"
else
  log "инцидента после отказа нет: DR не обнаружил отказ"
fi
registry_admin -G "$REGISTRY_URL/api/v1/incidents" --data-urlencode "cluster_id=$CLUSTER" \
  --data-urlencode "from=$STARTED" >"$OUT/incidents.json"

jq -n \
  --arg run_id "$RUN_ID" --arg started "$STARTED" --arg finished "$FINISHED" --arg cluster "$CLUSTER" \
  --arg dr_mode "$DR_MODE" --arg host "$HOST" --argjson rps "$RPS" --argjson k6_rc "$K6_RC" \
  --argjson baseline "$BASELINE" --argjson fault "$FAULT" --argjson recovery "$RECOVERY" \
  --arg injected "$INJECTED_AT" --arg recovered "$RECOVERED_AT" --arg runner_applied "$RUNNER_APPLIED_AT" \
  --arg ref "$FAULT_REF" --arg scheduler "$("$ROOT/stand/scripts/scheduler-mode.sh" 2>/dev/null || echo unknown)" \
  --arg git "$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)" \
  '{run_id:$run_id, scenario:"1-cluster-failure", variant:"pod-failure", cluster:$cluster, dr_mode:$dr_mode,
    started_at:$started, finished_at:$finished,
    structure:{baseline_s:$baseline, fault_s:$fault, recovery_s:$recovery},
    k6:{host:$host, rps:$rps, exit_code:$k6_rc},
    fault:{ref:$ref, tool:"chaos-mesh PodChaos pod-failure, namespace workloads",
           injected_at:$injected, recovered_at:(if $recovered == "" then null else $recovered end),
           runner_applied_at:$runner_applied,
           note:"injected_at/recovered_at — события Apply/Recover Chaos Mesh, точность 1 с"},
    scheduler:$scheduler, git_rev:$git}' >"$OUT/meta.json"

uv run --no-project --quiet --python 3.13 python "$ROOT/experiments/dr/summary.py" "$OUT" "$TMP/k6.csv"

if curl -sf "$THANOS_URL/-/healthy" >/dev/null 2>&1; then
  sleep 15
  RESULTS_ROOT="$RESULTS_ROOT" THANOS_URL="$THANOS_URL" "$ROOT/experiments/load/export.sh" "$RUN_ID"
else
  log "Thanos Querier недоступен на $THANOS_URL: серверные ряды не выгружены"
fi
log "готово: $OUT"
jq '{dr_mode, incident: (.incident | if . then {ttd_seconds, rto_seconds, return_seconds} else null end),
     lost_requests, availability_fault: .windows.fault.availability}' "$OUT/summary.json"
