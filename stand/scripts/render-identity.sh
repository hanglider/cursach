#!/usr/bin/env bash
# ConfigMap cluster-identity для кластера из stand/clusters.yaml (ADR 0009).
#   render-identity.sh <cluster-id>   -> YAML в stdout
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

id=${1:?cluster-id}
# yq (mikefarah) берёт переменные из окружения через strenv(), не через --arg.
out=$(CLUSTER_ID="$id" yq '
  .clusters[] | select(.id == strenv(CLUSTER_ID)) |
  {
    "apiVersion": "v1", "kind": "ConfigMap",
    "metadata": {"name": "cluster-identity", "namespace": "federation-system"},
    "data": {
      "cluster-id": .id, "region": .region, "provider": .provider,
      "certifications": (.certifications | join(","))
    }
  }' "$STAND/clusters.yaml")
[ -n "$out" ] || die "кластер $id не описан в clusters.yaml"
printf '%s\n' "$out"
