#!/usr/bin/env bash
# Снести стенд целиком. Запускается только человеком (CLAUDE.md: кластеры не
# удаляются автоматически).
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need kind
for c in "$CONTROL" "${FEDERATION[@]}"; do
  if cluster_exists "$c"; then
    log "удаляю $c"
    kind delete cluster --name "$c"
  fi
done
rm -rf "$KUBECONFIG_DIR"
