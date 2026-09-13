#!/usr/bin/env bash
# ConfigMap prometheus-config для кластера федерации: подставляет cluster id
# в external_labels (architecture §2.3).
#   render-prometheus-config.sh <cluster-id>  -> YAML в stdout
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

id=${1:?cluster-id}
for c in "${FEDERATION[@]}"; do [ "$c" = "$id" ] && found=1; done
[ -n "${found:-}" ] || die "кластер $id не описан в clusters.yaml"
sed "s/__CLUSTER_ID__/$id/g" "$STAND/observability/federation/prometheus-config.tmpl.yaml"
