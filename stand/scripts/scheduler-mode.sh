#!/usr/bin/env bash
# Режим планировщика (ADR 0020, 0027): meta | naive.
#   scheduler-mode.sh          -> печатает текущий режим
#   scheduler-mode.sh naive    -> переключает (kubectl set env + rollout) и печатает
# Переключение перезапускает под: счётчик round-robin сбрасывается (допущено ADR 0020).
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need kubectl
ns=$REGISTRY_NAMESPACE
current=$(kc "$CONTROL" -n "$ns" get deployment/scheduler \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="SCHEDULER_MODE")].value}' 2>/dev/null || true)
if [ $# -eq 0 ]; then
  [ -n "$current" ] || die "планировщик не развёрнут в $CONTROL/$ns"
  echo "$current"
  exit 0
fi
mode=$1
case "$mode" in
  meta|naive) ;;
  *) die "режим: meta | naive (получено '$mode')" ;;
esac
[ -n "$current" ] || die "планировщик не развёрнут в $CONTROL/$ns"
if [ "$mode" != "$current" ]; then
  log "планировщик: $current -> $mode"
  kc "$CONTROL" -n "$ns" set env deployment/scheduler SCHEDULER_MODE="$mode" >/dev/null
  kc "$CONTROL" -n "$ns" rollout status deployment/scheduler --timeout=120s >/dev/null
fi
echo "$mode"
