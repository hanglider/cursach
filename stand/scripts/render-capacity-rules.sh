#!/usr/bin/env bash
# Ёмкость узлов кластера как recording rules-константы (ADR 0024): kubelet не
# отдаёт allocatable через cAdvisor, а kube-state-metrics ради двух чисел не нужен.
#   render-capacity-rules.sh <cluster-id>  -> ConfigMap prometheus-capacity в stdout
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

id=${1:?cluster-id}
nodes=$(kc "$id" get nodes -o json)
cpu=$(jq -r '[.items[].status.allocatable.cpu | if test("m$") then (.[:-1] | tonumber / 1000) else tonumber end] | add' <<<"$nodes")
mem=$(jq -r '[.items[].status.allocatable.memory
  | if test("Ki$") then (.[:-2] | tonumber * 1024)
    elif test("Mi$") then (.[:-2] | tonumber * 1048576)
    elif test("Gi$") then (.[:-2] | tonumber * 1073741824)
    else tonumber end] | add' <<<"$nodes")
[ -n "$cpu" ] && [ -n "$mem" ] || die "не удалось прочитать allocatable у $id"
cat <<YAML
apiVersion: v1
kind: ConfigMap
metadata:
  name: prometheus-capacity
  namespace: observability
data:
  capacity.yaml: |
    groups:
      - name: capacity
        rules:
          - record: node:cpu_allocatable:cores
            expr: vector($cpu)
          - record: node:memory_allocatable:bytes
            expr: vector($mem)
YAML
