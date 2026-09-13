#!/usr/bin/env bash
# ConfigMap со списком StoreAPI-endpoint'ов для Thanos Querier (ADR 0002, 0021):
# sidecar'ы федерации по IP узлов, sidecar и Ruler control по DNS.
#   render-thanos-sd.sh  -> YAML в stdout
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need docker
targets="      - thanos-sidecar.observability.svc:10901
      - thanos-rule.observability.svc:10901
"
for c in "${FEDERATION[@]}"; do
  ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$c-control-plane")
  [ -n "$ip" ] || die "нет IP у $c-control-plane"
  targets+="      - $ip:30901
"
done
cat <<YAML
apiVersion: v1
kind: ConfigMap
metadata:
  name: thanos-query-sd
  namespace: observability
data:
  endpoints.yaml: |
    - targets:
$targets
YAML
