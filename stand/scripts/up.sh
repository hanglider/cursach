#!/usr/bin/env bash
# Поднять стенд с нуля: control + федерация, образ, backend'ы, шлюз, дымовой тест.
# Идемпотентен: уже существующие кластеры не пересоздаются, манифесты применяются повторно.
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need docker kind kubectl helm yq uv curl jq
docker info >/dev/null 2>&1 || die "docker не отвечает; запусти OrbStack"
mkdir -p "$KUBECONFIG_DIR"

# --- 1. Кластеры (параллельно) ---------------------------------------------
pids=()
for c in "$CONTROL" "${FEDERATION[@]}"; do
  if cluster_exists "$c"; then
    log "кластер $c уже есть"
  else
    log "создаю кластер $c"
    kind create cluster --config "$STAND/kind/$c.yaml" --wait 120s >"$KUBECONFIG_DIR/$c.create.log" 2>&1 &
    pids+=($!)
  fi
done
for p in "${pids[@]:-}"; do
  [ -z "$p" ] && continue
  wait "$p" || { cat "$KUBECONFIG_DIR"/*.create.log >&2; die "kind create не удался"; }
done

# Два набора kubeconfig: с хоста и внутрисетевой для компонентов контура (ADR 0021).
for c in "$CONTROL" "${FEDERATION[@]}"; do
  kind get kubeconfig --name "$c" >"$KUBECONFIG_DIR/$c.kubeconfig"
  kind get kubeconfig --name "$c" --internal >"$KUBECONFIG_DIR/$c.internal.kubeconfig"
done

# --- 2. Образ тестового сервиса --------------------------------------------
"$STAND/scripts/image.sh"

# --- 3. Федерация: namespace, идентичность, backend ------------------------
for c in "${FEDERATION[@]}"; do
  log "$c: namespace, cluster-identity, backend"
  kc "$c" apply -f "$STAND/federation/namespaces.yaml" >/dev/null
  "$STAND/scripts/render-identity.sh" "$c" | kc "$c" apply -f - >/dev/null
  kc "$c" apply -f "$STAND/federation/backend.yaml" >/dev/null
done
for c in "${FEDERATION[@]}"; do
  kc "$c" -n workloads rollout status deployment/backend --timeout=120s >/dev/null
done

# --- 4. Control: Envoy Gateway + Gateway -----------------------------------
log "$CONTROL: Envoy Gateway $EG_VERSION"
helm upgrade --install eg oci://docker.io/envoyproxy/gateway-helm --version "$EG_VERSION" \
  --kubeconfig "$KUBECONFIG_DIR/$CONTROL.kubeconfig" \
  -n "$EG_NAMESPACE" --create-namespace \
  --set config.envoyGateway.extensionApis.enableBackend=true \
  --wait --timeout 5m >/dev/null
kc "$CONTROL" apply -f "$STAND/gateway/gateway.yaml" >/dev/null
kc "$CONTROL" -n gateway wait gateway/stand --for=condition=Programmed --timeout=180s >/dev/null
# Прокси поднимается отдельным Deployment'ом после Gateway.
kc "$CONTROL" -n "$EG_NAMESPACE" wait deployment -l gateway.envoyproxy.io/owning-gateway-name=stand \
  --for=condition=Available --timeout=180s >/dev/null

# --- 5. Дымовой тест: workload в первом кластере федерации через шлюз -------
log "дымовой тест: smoke.fed.local -> ${FEDERATION[0]}"
GEN="uv run --quiet --project $ROOT/experiments/load/workloadgen workloadgen"
$GEN --timeout 120 place --name smoke --cluster "${FEDERATION[0]}" --port 31999 --wait >/dev/null
body=$(curl -sf -H 'Host: smoke.fed.local' http://127.0.0.1:8080/)
$GEN remove --name smoke >/dev/null
echo "$body" | jq -e '.tier == "frontend" and .backend.tier == "backend"' >/dev/null \
  || die "дымовой тест: неожиданный ответ: $body"

log "стенд готов"
"$STAND/scripts/status.sh"
