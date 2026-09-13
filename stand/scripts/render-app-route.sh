#!/usr/bin/env bash
# Маршрут app.fed.local с backend'ом на каждый кластер федерации (ADR 0011).
# Адреса узлов — IP из docker (ADR 0022), поэтому рендерится при stand-up.
#   render-app-route.sh  -> YAML в stdout
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need docker
refs=""
for c in "${FEDERATION[@]}"; do
  ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$c-control-plane")
  [ -n "$ip" ] || die "нет IP у $c-control-plane"
  cat <<YAML
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: Backend
metadata:
  name: app-$c
  namespace: gateway
  labels:
    federation.spbu/workload: app
    federation.spbu/cluster: $c
spec:
  endpoints:
    - ip:
        address: $ip
        port: 31998
---
YAML
  refs+="        - group: gateway.envoyproxy.io
          kind: Backend
          name: app-$c
          weight: 50
"
done
cat <<YAML
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: app
  namespace: gateway
  labels:
    federation.spbu/workload: app
spec:
  parentRefs:
    - name: stand
  hostnames:
    - app.fed.local
  rules:
    - backendRefs:
$refs
YAML
