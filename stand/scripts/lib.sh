#!/usr/bin/env bash
# Общее для скриптов стенда. Подключается через `source`.
# shellcheck disable=SC2034  # переменные используются в скриптах, которые делают source

set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
STAND="$ROOT/stand"
KUBECONFIG_DIR="$STAND/kubeconfig"           # в .gitignore (kubeconfig*)
CONTROL=control
IMAGE=federation/test-service:dev
EG_VERSION=1.9.1                             # чарт Envoy Gateway (ADR 0017)
EG_NAMESPACE=envoy-gateway-system

# bash 3.2 на macOS: без mapfile.
FEDERATION=()
while IFS= read -r _id; do FEDERATION+=("$_id"); done < <(yq -r '.clusters[].id' "$STAND/clusters.yaml")

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33mwarn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

need() {
  for t in "$@"; do
    command -v "$t" >/dev/null 2>&1 || die "нет $t; см. Brewfile"
  done
}

kc() {  # kc <cluster> <kubectl args...>
  local c=$1; shift
  kubectl --kubeconfig "$KUBECONFIG_DIR/$c.kubeconfig" "$@"
}

cluster_exists() { kind get clusters 2>/dev/null | grep -qx "$1"; }
