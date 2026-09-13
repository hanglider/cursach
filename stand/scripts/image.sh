#!/usr/bin/env bash
# Собрать образ тестового сервиса и загрузить в кластеры федерации (ADR 0014).
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
