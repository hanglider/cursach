#!/usr/bin/env bash
# Интеграционная проверка DR Controller'а на живом стенде (ADR 0023 вариант В, ADR 0028).
#
#   experiments/dr/check.sh [кластер=cluster-b]
#
# 1. Инвариант 6: у кластера пропадают метрики (Prometheus недостижим по сети), сервис жив.
#    Ожидается: sli_ok неизвестен, каталог ставит unknown, DR остаётся HEALTHY, вес
#    номинальный, инцидента нет.
# 2. Вариант В: app кластера недостижим по сети, Prometheus и API-сервер живы.
#    Ожидается: DOWN по пробе сервиса, вес 0, down в каталоге; после снятия — RECOVERING,
#    ступени веса, HEALTHY, инцидент со всеми фазами.
# Во время проверки k6 шлёт CHECK_RPS в app.fed.local, чтобы у error rate был знаменатель.
# Нужен режим DR active. Код 0 — обе проверки прошли; вывод — JSON с наблюдениями.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=../../stand/scripts/lib.sh
source "$ROOT/stand/scripts/lib.sh"

CLUSTER=${1:-cluster-b}
CHECK_RPS=${CHECK_RPS:-10}
METRICS_HOLD=${METRICS_HOLD:-90}   # сколько держать кластер без метрик
CHAOS_DIR="$ROOT/experiments/chaos"

need k6 jq kubectl curl
load_env || die "нет $ENV_FILE; его создаёт make stand-up"
printf '%s\n' "${FEDERATION[@]}" | grep -qx "$CLUSTER" || die "кластер $CLUSTER не из федерации"
[ "$("$ROOT/stand/scripts/dr-mode.sh")" = active ] || die "проверка требует режима DR active: make dr-mode MODE=active"

K6_PID=""
cleanup() {
  [ -n "$K6_PID" ] && kill "$K6_PID" 2>/dev/null || true
  kc "$CLUSTER" delete -f "$CHAOS_DIR/prometheus-unreachable.yaml" -f "$CHAOS_DIR/app-unreachable.yaml" \
    --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Кратковременный отказ /status (рестарт пода DR) — пустой снимок, а не выход по set -e.
st() { dr_status 2>/dev/null | jq -c --arg c "$CLUSTER" '.clusters[$c] // {}' 2>/dev/null || echo '{}'; }
registry_status() {
  curl -sf -H "Authorization: Bearer $REGISTRY_TOKEN_ADMIN" "$REGISTRY_URL/api/v1/clusters/$CLUSTER" | jq -r .slo_status
}
wait_for() {  # wait_for <секунд> <jq-условие над st> — печатает st, когда условие выполнено
  local deadline=$(( $(date +%s) + $1 )) s
  while [ "$(date +%s)" -lt "$deadline" ]; do
    s=$(st)
    if jq -e "$2" <<<"$s" >/dev/null 2>&1; then echo "$s"; return 0; fi
    sleep 2
  done
  echo "$s"
  return 1
}

jq -e 'all(.clusters[]; .state == "healthy" and .weight == .desired_weight)' <<<"$(dr_status)" >/dev/null \
  || die "стенд не в штатном состоянии: $(dr_status | jq -c .clusters)"
nominal=$(dr_status | jq .nominal_weight)
started=$(date -u +%FT%TZ)

log "фон: k6 $CHECK_RPS RPS в app.fed.local"
DURATION=30m RPS="$CHECK_RPS" TARGET_HOST=app.fed.local \
  k6 run --quiet "$ROOT/experiments/load/steady.js" >/dev/null 2>&1 &
K6_PID=$!
wait_for 90 '.sli == "ok"' >/dev/null || die "у $CLUSTER нет error rate даже под нагрузкой: $(st)"

# --- 1. Инвариант 6 -------------------------------------------------------------------
log "1/2 инвариант 6: Prometheus $CLUSTER недостижим ${METRICS_HOLD}s"
kc "$CLUSTER" apply -f "$CHAOS_DIR/prometheus-unreachable.yaml" >/dev/null
saw_unknown=false; saw_registry_unknown=false; bad=""
deadline=$(( $(date +%s) + METRICS_HOLD ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  s=$(st)
  [ "$(jq -r .sli <<<"$s")" = unknown ] && saw_unknown=true
  [ "$(registry_status)" = unknown ] && saw_registry_unknown=true
  if ! jq -e --argjson n "$nominal" '.state == "healthy" and .weight == $n' <<<"$s" >/dev/null; then
    bad=$s; break
  fi
  sleep 3
done
kc "$CLUSTER" delete -f "$CHAOS_DIR/prometheus-unreachable.yaml" --ignore-not-found >/dev/null
[ -z "$bad" ] || die "инвариант 6 нарушен: без метрик DR сменил состояние или вес: $bad"
$saw_unknown || die "проверка 1 не состоялась: sli у $CLUSTER так и не стал unknown"
opened=$(curl -sf -G -H "Authorization: Bearer $REGISTRY_TOKEN_ADMIN" "$REGISTRY_URL/api/v1/incidents" \
  --data-urlencode "cluster_id=$CLUSTER" --data-urlencode "from=$started" | jq length)
[ "$opened" -eq 0 ] || die "инвариант 6 нарушен: открыт инцидент без отказа сервиса"
log "1/2 пройдено: sli unknown, каталог unknown=$saw_registry_unknown, DR HEALTHY, вес $nominal"
wait_for 120 '.sli == "ok"' >/dev/null || log "метрики $CLUSTER не вернулись за 120 с (продолжаю)"

# --- 2. Вариант В ---------------------------------------------------------------------
log "2/2 вариант В: app $CLUSTER недостижим"
t_fault=$(date +%s)
kc "$CLUSTER" apply -f "$CHAOS_DIR/app-unreachable.yaml" >/dev/null
s=$(wait_for 90 '.state == "down" and .weight == 0') || die "DOWN не наступил за 90 с: $s"
t_down=$(( $(date +%s) - t_fault ))
[ "$(registry_status)" = down ] || die "DOWN без down в каталоге"
incident=$(jq .incident_id <<<"$s")
log "DOWN за ~${t_down}s (по часам скрипта), вес 0, каталог down, инцидент $incident"

kc "$CLUSTER" delete -f "$CHAOS_DIR/app-unreachable.yaml" --ignore-not-found >/dev/null
t_clear=$(date +%s)
s=$(wait_for 60 '.state == "recovering"') || die "RECOVERING не наступил за 60 с после снятия: $s"
[ "$(registry_status)" != down ] || die "down в каталоге не снят на RECOVERING"
s=$(wait_for 180 '.state == "healthy"') || die "HEALTHY не наступил за 180 с: $s"
jq -e --argjson n "$nominal" '.weight == $n' <<<"$s" >/dev/null || die "вес не вернулся к номиналу: $s"
t_healthy=$(( $(date +%s) - t_clear ))

inc=$(curl -sf -H "Authorization: Bearer $REGISTRY_TOKEN_ADMIN" -G "$REGISTRY_URL/api/v1/incidents" \
  --data-urlencode "cluster_id=$CLUSTER" --data-urlencode "from=$started" | jq --argjson id "$incident" '.[] | select(.id == $id)')
jq -e '.peak_state == "down" and .failover_completed_at != null and .recovery_started_at != null
       and .recovery_completed_at != null' <<<"$inc" >/dev/null || die "инцидент неполный: $inc"
log "2/2 пройдено: HEALTHY через ~${t_healthy}s после снятия"
jq -n --arg c "$CLUSTER" --argjson inc "$inc" --argjson down "$t_down" --argjson healthy "$t_healthy" \
  --argjson reg_unknown "$saw_registry_unknown" \
  '{cluster:$c, invariant6:{passed:true, registry_saw_unknown:$reg_unknown},
    variant_v:{passed:true, down_after_s:$down, healthy_after_clear_s:$healthy, incident:$inc}}'
