#!/usr/bin/env bash
# Регистрация кластеров федерации в каталоге из stand/clusters.yaml (ADR 0009, 0018, 0021).
# Идемпотентно: повтор обновляет атрибуты, статус и историю метрик не трогает.
#   register-clusters.sh
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need yq curl jq
load_env || die "нет $ENV_FILE; его создаёт make stand-up"

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
for c in "${FEDERATION[@]}"; do
  kubeconfig="$KUBECONFIG_DIR/$c.internal.kubeconfig"
  [ -f "$kubeconfig" ] || die "нет $kubeconfig"
  endpoint=$(yq -r '.clusters[0].cluster.server' "$kubeconfig")
  body=$(CLUSTER_ID="$c" API_ENDPOINT="$endpoint" yq -o=json -I=0 '
    .clusters[] | select(.id == strenv(CLUSTER_ID)) |
    {"id": .id, "region": .region, "provider": .provider,
     "certifications": .certifications, "labels": (.labels // {}),
     "api_endpoint": strenv(API_ENDPOINT)}' "$STAND/clusters.yaml")
  [ -n "$body" ] || die "кластер $c не описан в clusters.yaml"
  code=$(curl -s -o "$tmp" -w '%{http_code}' -X POST "$REGISTRY_URL/api/v1/clusters" \
    -H "Authorization: Bearer $REGISTRY_TOKEN_ADMIN" -H 'Content-Type: application/json' \
    -d "$body")
  case "$code" in
    201) log "$c: зарегистрирован ($endpoint)" ;;
    200) log "$c: перерегистрирован ($endpoint)" ;;
    *) die "$c: регистрация не удалась, HTTP $code: $(cat "$tmp")" ;;
  esac
done
