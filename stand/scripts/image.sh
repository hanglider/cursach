#!/usr/bin/env bash
# Собрать образы тестового сервиса и каталога; первый загрузить в кластеры федерации
# (ADR 0014), второй — в control (ADR 0025). Registry образов нет: kind load.
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
