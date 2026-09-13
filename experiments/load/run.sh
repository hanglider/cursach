#!/usr/bin/env bash
# Один прогон нагрузки: генератор workload'ов + k6 (ADR 0008, 0015).
#
#   experiments/load/run.sh <run-id> [k6 duration] [интервал N, с] [TTL, с]
#
# Результат: experiments/results/<run-id>/{meta.json,k6-summary.json,workloadgen.log}.
# RESULTS_ROOT=<dir> переопределяет каталог результатов (для пробных прогонов).
# Задача 1: только клиентский ряд k6. Экспорт серверных рядов из Thanos — задача 2.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
RUN_ID=${1:?run-id}
DURATION=${2:-3m}
INTERVAL=${3:-30}
TTL=${4:-180}
RPS=${RPS:-50}
GATEWAY_URL=${GATEWAY_URL:-http://127.0.0.1:8080}
LIVE_LISTEN=${LIVE_LISTEN:-127.0.0.1:9797}
WARMUP_HOSTS=${WARMUP_HOSTS:-3}   # сколько живых workload'ов ждать до старта k6

RESULTS_ROOT=${RESULTS_ROOT:-$ROOT/experiments/results}   # пробные прогоны — в другой каталог
OUT="$RESULTS_ROOT/$RUN_ID"
GEN="uv run --quiet --project $ROOT/experiments/load/workloadgen workloadgen"

if [ -e "$OUT" ]; then
  echo "run.sh: $OUT уже существует; результаты не перезаписываются" >&2
  exit 1
fi
mkdir -p "$OUT"

log() { printf '%s run.sh: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

# Ничего от прошлых прогонов не должно остаться.
$GEN cleanup

log "старт генератора: interval=${INTERVAL}s ttl=${TTL}s"
$GEN run --interval "$INTERVAL" --ttl "$TTL" --listen "$LIVE_LISTEN" >"$OUT/workloadgen.log" 2>&1 &
GEN_PID=$!
trap 'kill -INT $GEN_PID 2>/dev/null || true; wait $GEN_PID 2>/dev/null || true' EXIT

# Ждём, пока живых станет хотя бы WARMUP_HOSTS, иначе k6 начнёт с пустого списка.
deadline=$(( $(date +%s) + INTERVAL * WARMUP_HOSTS + 120 ))
while :; do
  n=$(curl -sf "http://$LIVE_LISTEN/live" 2>/dev/null | jq '.hosts | length' 2>/dev/null || echo 0)
  [ "$n" -ge "$WARMUP_HOSTS" ] && break
  if [ "$(date +%s)" -ge "$deadline" ]; then
    log "живых workload'ов: $n, ждали слишком долго; см. $OUT/workloadgen.log"
    exit 1
  fi
  sleep 2
done
log "живых workload'ов: $n, запускаю k6 на $DURATION при $RPS RPS"

# Remote write в Prometheus control (ADR 0008), если он доступен; иначе только сводка.
PROM_URL=${PROM_URL:-http://127.0.0.1:9090}
k6_out=()
if curl -sf "$PROM_URL/-/ready" >/dev/null 2>&1; then
  k6_out=(-o experimental-prometheus-rw)
  export K6_PROMETHEUS_RW_SERVER_URL="$PROM_URL/api/v1/write"
  export K6_PROMETHEUS_RW_TREND_STATS="p(95),p(99),avg,max"
else
  log "Prometheus control недоступен на $PROM_URL: без remote write"
fi

STARTED=$(date -u +%FT%TZ)
set +e
DURATION="$DURATION" RPS="$RPS" GATEWAY_URL="$GATEWAY_URL" LIVE_URL="http://$LIVE_LISTEN/live" \
  SUMMARY_PATH="$OUT/k6-summary.json" \
  k6 run --quiet --tag "run_id=$RUN_ID" "${k6_out[@]}" "$ROOT/experiments/load/steady.js" 2>&1 | tee "$OUT/k6.log"
K6_RC=${PIPESTATUS[0]}
set -e
FINISHED=$(date -u +%FT%TZ)

jq -n \
  --arg run_id "$RUN_ID" --arg started "$STARTED" --arg finished "$FINISHED" \
  --arg duration "$DURATION" --argjson rps "$RPS" --argjson interval "$INTERVAL" --argjson ttl "$TTL" \
  --arg git "$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)" \
  --arg scenario "${SCENARIO:-none}" --arg scheduler "${SCHEDULER:-generator-round-robin}" \
  --argjson k6_rc "$K6_RC" \
  '{run_id:$run_id, started_at:$started, finished_at:$finished, k6:{duration:$duration, rps:$rps, exit_code:$k6_rc},
    workloads:{interval_s:$interval, ttl_s:$ttl}, scenario:$scenario, scheduler:$scheduler, git_rev:$git}' \
  >"$OUT/meta.json"

# Серверные ряды из Thanos (ADR 0002): ждём последний scrape и выгружаем.
THANOS_URL=${THANOS_URL:-http://127.0.0.1:10902}
if curl -sf "$THANOS_URL/-/healthy" >/dev/null 2>&1; then
  sleep 15
  RESULTS_ROOT="$RESULTS_ROOT" THANOS_URL="$THANOS_URL" "$ROOT/experiments/load/export.sh" "$RUN_ID"
else
  log "Thanos Querier недоступен на $THANOS_URL: серверные ряды не выгружены"
fi

log "готово: $OUT (k6 exit $K6_RC)"
exit "$K6_RC"
