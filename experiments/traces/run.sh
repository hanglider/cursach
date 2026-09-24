#!/usr/bin/env bash
# Замер трассировки (задача 3, ADR 0029): постоянная нагрузка k6 в app.fed.local при заданной
# доле сэмплинга на шлюзе. Два результата: разложение задержки по звеньям из трейсов
# (шлюз control → кластер → frontend → backend) и цена трассировки — клиентская задержка
# k6 при сэмплинге 0 / 10 / 100% на одном и том же стенде.
#
#   experiments/traces/run.sh <run-id> [сэмплинг, %=10]
#
# DURATION (3m) и RPS (50) — из окружения. Сэмплинг выставляется на EnvoyProxy на время
# прогона и возвращается к значению из stand/gateway/gateway.yaml.
# Результат: experiments/results/<run-id>/{meta.json, summary.json, k6-summary.json, k6.log},
# series/, registry/, traces/ (export.sh). RESULTS_ROOT=<dir> — пробные прогоны.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=../../stand/scripts/lib.sh
source "$ROOT/stand/scripts/lib.sh"

RUN_ID=${1:?run-id}
SAMPLING=${2:-10}
DURATION=${DURATION:-3m}
RPS=${RPS:-50}
HOST=${HOST:-app.fed.local}
GATEWAY_URL=${GATEWAY_URL:-http://127.0.0.1:8080}
PROM_URL=${PROM_URL:-http://127.0.0.1:9090}
RESULTS_ROOT=${RESULTS_ROOT:-$ROOT/experiments/results}
OUT="$RESULTS_ROOT/$RUN_ID"

log() { printf '%s traces/run.sh: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

need k6 jq yq kubectl curl uv
[ -e "$OUT" ] && die "$OUT уже существует; результаты не перезаписываются"
case "$SAMPLING" in ''|*[!0-9]*) die "сэмплинг — целое 0..100, получено '$SAMPLING'" ;; esac
[ "$SAMPLING" -le 100 ] || die "сэмплинг — целое 0..100, получено '$SAMPLING'"
curl -sf "$JAEGER_URL/api/v3/services" >/dev/null || die "Jaeger недоступен на $JAEGER_URL"
DEFAULT=$(gateway_sampling_default)
set_sampling() { gateway_sampling "$1"; }
envoy_pods() {
  kc "$CONTROL" -n "$EG_NAMESPACE" get pods -l gateway.envoyproxy.io/owning-gateway-name=stand \
    -o jsonpath='{.items[*].metadata.name}'
}
cleanup() { set_sampling "$DEFAULT" 2>/dev/null || true; }
trap cleanup EXIT

mkdir -p "$OUT"
pods_before=$(envoy_pods)
set_sampling "$SAMPLING"
sleep 10   # xDS доводит конфигурацию до Envoy за секунды; с запасом
pods_after=$(envoy_pods)
[ "$pods_before" = "$pods_after" ] || log "прокси Envoy пересоздан при смене сэмплинга: $pods_before -> $pods_after"
log "сэмплинг шлюза $SAMPLING%, k6 $DURATION при $RPS RPS в $HOST"

k6_out=()
if curl -sf "$PROM_URL/-/ready" >/dev/null 2>&1; then
  k6_out=(-o experimental-prometheus-rw)
  export K6_PROMETHEUS_RW_SERVER_URL="$PROM_URL/api/v1/write"
  export K6_PROMETHEUS_RW_TREND_STATS="p(95),p(99),avg,max"
fi
STARTED=$(date -u +%FT%TZ)
set +e
DURATION="$DURATION" RPS="$RPS" GATEWAY_URL="$GATEWAY_URL" TARGET_HOST="$HOST" \
  SUMMARY_PATH="$OUT/k6-summary.json" \
  k6 run --quiet --tag "run_id=$RUN_ID" "${k6_out[@]}" "$ROOT/experiments/load/steady.js" >"$OUT/k6.log" 2>&1
K6_RC=$?
set -e
FINISHED=$(date -u +%FT%TZ)
set_sampling "$DEFAULT"

jq -n --arg run_id "$RUN_ID" --arg started "$STARTED" --arg finished "$FINISHED" --arg host "$HOST" \
  --arg duration "$DURATION" --argjson rps "$RPS" --argjson sampling "$SAMPLING" --argjson k6_rc "$K6_RC" \
  --argjson default "$DEFAULT" --arg scheduler "$("$STAND/scripts/scheduler-mode.sh" 2>/dev/null || echo unknown)" \
  --arg dr "$("$STAND/scripts/dr-mode.sh" 2>/dev/null || echo unknown)" \
  --arg git "$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)" \
  '{run_id:$run_id, scenario:"tracing", started_at:$started, finished_at:$finished,
    k6:{host:$host, duration:$duration, rps:$rps, exit_code:$k6_rc},
    tracing:{gateway_sampling_percent:$sampling, stand_default_percent:$default,
             service_sampler:"parentbased(always_off)"},
    scheduler:$scheduler, dr_mode:$dr, git_rev:$git}' >"$OUT/meta.json"

sleep 15   # последний scrape и последний батч спанов (коллектор 2 с, Jaeger)
RESULTS_ROOT="$RESULTS_ROOT" "$ROOT/experiments/load/export.sh" "$RUN_ID"

# Сводка: клиентская задержка k6 (цена трассировки) и разложение по звеньям (её польза).
"$ROOT/experiments/traces/summarize.sh" "$OUT"
log "готово: $OUT"
jq -c '{gateway_sampling_percent, client: .client.latency_ms, traces: .traces}' "$OUT/summary.json"
