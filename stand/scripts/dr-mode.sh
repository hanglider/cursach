#!/usr/bin/env bash
# Режим DR Controller'а (ADR 0028): active | observe.
#   dr-mode.sh           -> печатает текущий режим
#   dr-mode.sh observe   -> переключает (kubectl set env + rollout) и печатает
# observe — сценарий 1 «без DR»: DR обнаруживает и пишет инциденты, но не трогает веса
# маршрута и статус в каталоге. Поэтому в observe переключаемся только со здорового
# стенда: иначе вес, обнулённый в active, так и остался бы нулём.
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need kubectl jq
ns=$REGISTRY_NAMESPACE
current=$(kc "$CONTROL" -n "$ns" get deployment/dr-controller \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="DR_MODE")].value}' 2>/dev/null || true)
[ -n "$current" ] || die "DR Controller не развёрнут в $CONTROL/$ns"
if [ $# -eq 0 ]; then
  echo "$current"
  exit 0
fi
mode=$1
case "$mode" in
  active|observe) ;;
  *) die "режим: active | observe (получено '$mode')" ;;
esac
if [ "$mode" != "$current" ]; then
  if [ "$mode" = observe ]; then
    status=$(dr_status) || die "нет ответа /status DR Controller'а"
    jq -e 'all(.clusters[]; .state == "healthy" and .weight == .desired_weight and .registry_status != "down")' \
      <<<"$status" >/dev/null \
      || die "стенд не в штатном состоянии, observe не включаю: $(jq -c '.clusters | map_values({state, weight, registry_status})' <<<"$status")"
  fi
  log "DR Controller: $current -> $mode"
  kc "$CONTROL" -n "$ns" set env deployment/dr-controller DR_MODE="$mode" >/dev/null
  kc "$CONTROL" -n "$ns" rollout status deployment/dr-controller --timeout=120s >/dev/null
fi
echo "$mode"
