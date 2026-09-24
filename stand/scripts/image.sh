#!/usr/bin/env bash
# Собрать образы тестового сервиса, каталога, планировщика и DR Controller'а; первый
# загрузить в кластеры федерации (ADR 0014), остальные — в control (ADR 0025, 0027, 0028).
# Registry образов нет: kind load.
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need docker kind
log "сборка $IMAGE"
docker build -q -t "$IMAGE" "$ROOT/test-service" >/dev/null
for c in "${FEDERATION[@]}"; do
  cluster_exists "$c" || continue
  log "загрузка в $c"
  kind load docker-image "$IMAGE" --name "$c" >/dev/null 2>&1
done

log "сборка $REGISTRY_IMAGE"
docker build -q -t "$REGISTRY_IMAGE" "$ROOT/registry" >/dev/null
if cluster_exists "$CONTROL"; then
  log "загрузка в $CONTROL"
  kind load docker-image "$REGISTRY_IMAGE" --name "$CONTROL" >/dev/null 2>&1
fi

log "сборка $SCHEDULER_IMAGE"
docker build -q -t "$SCHEDULER_IMAGE" "$ROOT/scheduler" >/dev/null
if cluster_exists "$CONTROL"; then
  log "загрузка в $CONTROL"
  kind load docker-image "$SCHEDULER_IMAGE" --name "$CONTROL" >/dev/null 2>&1
fi

log "сборка $DR_IMAGE"
docker build -q -t "$DR_IMAGE" "$ROOT/dr-controller" >/dev/null
if cluster_exists "$CONTROL"; then
  log "загрузка в $CONTROL"
  kind load docker-image "$DR_IMAGE" --name "$CONTROL" >/dev/null 2>&1
fi
