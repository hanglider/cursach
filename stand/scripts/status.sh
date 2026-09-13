#!/usr/bin/env bash
# Состояние стенда одним экраном.
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need kind kubectl docker
echo "kind clusters: $(kind get clusters 2>/dev/null | tr '\n' ' ')"
for c in "$CONTROL" "${FEDERATION[@]}"; do
  cluster_exists "$c" || { echo "$c: нет"; continue; }
  ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$c-control-plane" 2>/dev/null || true)
  echo "--- $c ($ip)"
  if [ "$c" = "$CONTROL" ]; then
    kc "$c" -n "$EG_NAMESPACE" get pods --no-headers 2>/dev/null | awk '{print "  " $1 "  " $3}'
    kc "$c" -n gateway get gateway,httproute --no-headers 2>/dev/null | awk '{print "  " $1 "  " $2}'
    kc "$c" -n observability get pods --no-headers 2>/dev/null | awk '{print "  " $1 "  " $3}'
    stores=$(curl -sf http://127.0.0.1:10902/api/v1/stores 2>/dev/null | jq -r '.data[][] | "\(.name) \(.lastError // "ok")"' 2>/dev/null)
    [ -n "$stores" ] && { echo "  thanos stores:"; echo "$stores" | awk '{print "    " $0}'; }
    echo "  grafana: http://127.0.0.1:3000  prometheus: http://127.0.0.1:9090  thanos: http://127.0.0.1:10902"
  else
    kc "$c" -n federation-system get configmap cluster-identity -o jsonpath='  identity: {.data}{"\n"}' 2>/dev/null
    kc "$c" -n workloads get pods --no-headers 2>/dev/null | awk '{print "  " $1 "  " $3}'
    kc "$c" -n observability get pods --no-headers 2>/dev/null | awk '{print "  " $1 "  " $3}'
  fi
done
