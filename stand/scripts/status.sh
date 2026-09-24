#!/usr/bin/env bash
# Состояние стенда одним экраном.
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need kind kubectl docker jq
echo "kind clusters: $(kind get clusters 2>/dev/null | tr '\n' ' ')"
for c in "$CONTROL" "${FEDERATION[@]}"; do
  cluster_exists "$c" || { echo "$c: нет"; continue; }
  ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$c-control-plane" 2>/dev/null || true)
  echo "--- $c ($ip)"
  if [ "$c" = "$CONTROL" ]; then
    kc "$c" -n "$EG_NAMESPACE" get pods --no-headers 2>/dev/null | awk '{print "  " $1 "  " $3}'
    kc "$c" -n gateway get gateway,httproute --no-headers 2>/dev/null | awk '{print "  " $1 "  " $2}'
    kc "$c" -n observability get pods --no-headers 2>/dev/null | awk '{print "  " $1 "  " $3}'
    kc "$c" -n "$REGISTRY_NAMESPACE" get pods --no-headers 2>/dev/null | awk '{print "  " $1 "  " $3}'
    mode=$("$STAND/scripts/scheduler-mode.sh" 2>/dev/null || true)
    [ -n "$mode" ] && echo "  scheduler mode: $mode"
    kc "$c" -n "$REGISTRY_NAMESPACE" get workloads --no-headers 2>/dev/null | awk '{print "  workload/" $1 "  " $2 "  " $3}'
    dr=$(dr_status 2>/dev/null | jq -r '"  dr: mode=\(.mode) cycles=\(.cycles) panic=\(.panic)",
      (.clusters | to_entries[] | "    \(.key) \(.value.state) weight=\(.value.weight)/\(.value.desired_weight) svc=\(.value.svc) api=\(.value.api) sli=\(.value.sli) incident=\(.value.incident_id // "-")")' 2>/dev/null || true)
    [ -n "$dr" ] && echo "$dr"
    if load_env 2>/dev/null; then
      registry=$(curl -sf -H "Authorization: Bearer $REGISTRY_TOKEN_ADMIN" "$REGISTRY_URL/api/v1/clusters" 2>/dev/null \
        | jq -r '.[] | "\(.id) \(.slo_status) err=\(.metrics.error_rate // "-") p95ms=\(.metrics.latency_p95_ms // "-") sat=\(.metrics.saturation // "-") at=\(.metrics.observed_at // "-")"' 2>/dev/null)
      [ -n "$registry" ] && { echo "  registry:"; echo "$registry" | awk '{print "    " $0}'; }
    fi
    stores=$(curl -sf http://127.0.0.1:10902/api/v1/stores 2>/dev/null | jq -r '.data[][] | "\(.name) \(.lastError // "ok")"' 2>/dev/null)
    [ -n "$stores" ] && { echo "  thanos stores:"; echo "$stores" | awk '{print "    " $0}'; }
    echo "  grafana: http://127.0.0.1:3000  prometheus: http://127.0.0.1:9090  thanos: http://127.0.0.1:10902  registry: $REGISTRY_URL"
  else
    kc "$c" -n federation-system get configmap cluster-identity -o jsonpath='  identity: {.data}{"\n"}' 2>/dev/null
    kc "$c" -n workloads get pods --no-headers 2>/dev/null | awk '{print "  " $1 "  " $3}'
    kc "$c" -n observability get pods --no-headers 2>/dev/null | awk '{print "  " $1 "  " $3}'
    kc "$c" -n "$KYVERNO_NAMESPACE" get pods --no-headers 2>/dev/null | awk '{print "  " $1 "  " $3}'
    kc "$c" -n "$CHAOS_NAMESPACE" get pods --no-headers 2>/dev/null | awk '{print "  " $1 "  " $3}'
    kc "$c" get clusterpolicy --no-headers \
      -o custom-columns='NAME:.metadata.name,READY:.status.conditions[?(@.type=="Ready")].status' 2>/dev/null \
      | awk '{print "  clusterpolicy/" $1 "  ready=" $2}'
  fi
done
