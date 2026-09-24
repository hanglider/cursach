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
REGISTRY_IMAGE=federation/registry:dev
SCHEDULER_IMAGE=federation/scheduler:dev      # планировщик в control (ADR 0027)
DR_IMAGE=federation/dr-controller:dev         # DR Controller в control (ADR 0028)
REGISTRY_NAMESPACE=federation                # ns управляющего контура в control (ADR 0025)
REGISTRY_URL=${REGISTRY_URL:-http://127.0.0.1:8000}
KYVERNO_CHART_VERSION=3.9.1                  # Kyverno v1.19.1 в кластерах федерации (ADR 0019, 0026)
KYVERNO_NAMESPACE=kyverno
CHAOS_CHART_VERSION=2.8.4                    # Chaos Mesh в кластерах федерации (ADR 0023, 0028)
CHAOS_NAMESPACE=chaos-mesh
ENV_FILE="$ROOT/.env"                        # токены каталога, в .gitignore (ADR 0018)
ENV_VARS=(REGISTRY_TOKEN_SCHEDULER REGISTRY_TOKEN_DR_CONTROLLER REGISTRY_TOKEN_ADMIN REGISTRY_DB_PASSWORD)

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

# /status DR Controller'а (JSON) через прокси API-сервера control: порт-форвард не нужен.
dr_status() {
  kc "$CONTROL" get --raw "/api/v1/namespaces/$REGISTRY_NAMESPACE/services/dr-controller:http/proxy/status"
}

# .env: токены каталога и пароль БД. load_env — прочитать; ensure_env — создать, если нет.
load_env() {
  [ -f "$ENV_FILE" ] || return 1
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
}

ensure_env() {
  if [ ! -f "$ENV_FILE" ]; then
    log "создаю $ENV_FILE: токены каталога"
    {
      echo "# Токены каталога и пароль БД (ADR 0018). Создан stand-up, в git не попадает."
      for v in "${ENV_VARS[@]}"; do echo "$v=$(openssl rand -hex 24)"; done
    } >"$ENV_FILE"
    chmod 600 "$ENV_FILE"
  fi
  load_env
  for v in "${ENV_VARS[@]}"; do
    [ -n "${!v:-}" ] || die "в $ENV_FILE нет $v; удали файл, stand-up создаст заново"
  done
}
