#!/usr/bin/env bash
# Поднять стенд с нуля: control + федерация, образы, backend'ы, Kyverno, Chaos Mesh, шлюз,
# наблюдаемость, каталог, планировщик, DR Controller, дымовые тесты.
# Идемпотентен: уже существующие кластеры не пересоздаются, манифесты применяются повторно.
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need docker kind kubectl helm yq uv curl jq openssl
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
  kc "$c" apply -f "$STAND/federation/app.yaml" >/dev/null
  kc "$c" -n workloads rollout status deployment/backend --timeout=120s >/dev/null
  kc "$c" -n workloads rollout status deployment/app --timeout=120s >/dev/null
done

# --- 3b. Федерация: Prometheus + Thanos sidecar (ADR 0002, 0024) ------------
for c in "${FEDERATION[@]}"; do
  log "$c: Prometheus + Thanos sidecar"
  kc "$c" apply -f "$STAND/observability/federation/namespace-rbac.yaml" >/dev/null
  "$STAND/scripts/render-prometheus-config.sh" "$c" | kc "$c" apply -f - >/dev/null
  "$STAND/scripts/render-capacity-rules.sh" "$c" | kc "$c" apply -f - >/dev/null
  kc "$c" apply -f "$STAND/observability/federation/prometheus-rules.yaml" >/dev/null
  kc "$c" apply -f "$STAND/observability/federation/prometheus.yaml" >/dev/null
  # Конфиг мог измениться при повторном stand-up: перечитать без пересоздания пода.
  kc "$c" -n observability rollout status deployment/prometheus --timeout=180s >/dev/null
  kc "$c" -n observability exec deploy/prometheus -c prometheus -- \
    wget -q -O /dev/null --post-data '' http://localhost:9090/-/reload 2>/dev/null || true
done

# --- 3c. Федерация: Kyverno + политики hard constraints (ADR 0019, 0026) ----
pids=()
for c in "${FEDERATION[@]}"; do
  log "$c: Kyverno (чарт $KYVERNO_CHART_VERSION)"
  helm upgrade --install kyverno oci://ghcr.io/kyverno/charts/kyverno --version "$KYVERNO_CHART_VERSION" \
    --kubeconfig "$KUBECONFIG_DIR/$c.kubeconfig" -n "$KYVERNO_NAMESPACE" --create-namespace \
    -f "$STAND/policies/kyverno-values.yaml" --wait --timeout 5m >"$KUBECONFIG_DIR/$c.kyverno.log" 2>&1 &
  pids+=($!)
done
for p in "${pids[@]:-}"; do
  [ -z "$p" ] && continue
  wait "$p" || { cat "$KUBECONFIG_DIR"/*.kyverno.log >&2; die "helm install kyverno не удался"; }
done
for c in "${FEDERATION[@]}"; do
  # Webhook валидации политик поднимается позже подов: apply с повтором.
  for i in $(seq 1 15); do
    kc "$c" apply -f "$STAND/policies/require-region-match.yaml" \
      -f "$STAND/policies/require-certification.yaml" >/dev/null 2>&1 && break
    [ "$i" -eq 15 ] && die "$c: не удалось применить политики Kyverno"
    sleep 2
  done
  kc "$c" wait --for=condition=Ready clusterpolicy --all --timeout=120s >/dev/null
done

# --- 3d. Федерация: Chaos Mesh — внесение отказов (ADR 0023, 0028) ----------
pids=()
for c in "${FEDERATION[@]}"; do
  log "$c: Chaos Mesh (чарт $CHAOS_CHART_VERSION)"
  helm upgrade --install chaos-mesh chaos-mesh --repo https://charts.chaos-mesh.org \
    --version "$CHAOS_CHART_VERSION" --kubeconfig "$KUBECONFIG_DIR/$c.kubeconfig" \
    -n "$CHAOS_NAMESPACE" --create-namespace -f "$STAND/chaos/chaos-mesh-values.yaml" \
    --wait --timeout 5m >"$KUBECONFIG_DIR/$c.chaos.log" 2>&1 &
  pids+=($!)
done
for p in "${pids[@]:-}"; do
  [ -z "$p" ] && continue
  wait "$p" || { cat "$KUBECONFIG_DIR"/*.chaos.log >&2; die "helm install chaos-mesh не удался"; }
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
# Маршрут долгоживущего сервиса app с весами по кластерам (ADR 0011).
"$STAND/scripts/render-app-route.sh" | kc "$CONTROL" apply -f - >/dev/null

# --- 4b. Control: Prometheus, Thanos Querier + Ruler, Grafana (ADR 0024) ---
log "$CONTROL: Prometheus, Thanos Querier + Ruler, Grafana"
kc "$CONTROL" apply -f "$STAND/observability/federation/namespace-rbac.yaml" >/dev/null
kc "$CONTROL" apply -f "$STAND/observability/control/prometheus-config.yaml" >/dev/null
kc "$CONTROL" apply -f "$STAND/observability/federation/prometheus.yaml" >/dev/null
kc "$CONTROL" apply -f "$STAND/observability/control/prometheus-nodeport.yaml" >/dev/null
"$STAND/scripts/render-thanos-sd.sh" | kc "$CONTROL" apply -f - >/dev/null
kc "$CONTROL" apply -f "$STAND/observability/control/thanos-query.yaml" >/dev/null
kc "$CONTROL" apply -f "$STAND/observability/control/thanos-rule.yaml" >/dev/null
kc "$CONTROL" create configmap grafana-dashboards -n observability \
  --from-file="$STAND/observability/control/grafana/dashboards/federation-slo.json" \
  --dry-run=client -o yaml | kc "$CONTROL" apply -f - >/dev/null
kc "$CONTROL" apply -f "$STAND/observability/control/grafana.yaml" >/dev/null
for d in prometheus thanos-query thanos-rule grafana; do
  kc "$CONTROL" -n observability rollout status "deployment/$d" --timeout=180s >/dev/null
done
# Конфиг scrape мог измениться (новый job): ConfigMap доходит до пода с задержкой kubelet
# до минуты — ждём совпадения смонтированного файла и перечитываем без пересоздания пода.
want=$(kc "$CONTROL" -n observability get configmap prometheus-config -o jsonpath='{.data.prometheus\.yml}')
for _ in $(seq 1 45); do
  have=$(kc "$CONTROL" -n observability exec deploy/prometheus -c prometheus -- \
    cat /etc/prometheus/prometheus.yml 2>/dev/null || true)
  [ "$have" = "$want" ] && break
  sleep 2
done
kc "$CONTROL" -n observability exec deploy/prometheus -c prometheus -- \
  wget -q -O /dev/null --post-data '' http://localhost:9090/-/reload 2>/dev/null || true
# Список store'ов мог измениться (IP узлов): Querier перечитывает файл сам,
# но после stand-up с нуля ждать 30 с незачем.
kc "$CONTROL" -n observability rollout restart deployment/thanos-query >/dev/null
kc "$CONTROL" -n observability rollout status deployment/thanos-query --timeout=120s >/dev/null

# --- 4c. Control: каталог кластеров (ADR 0004, 0018, 0025) ------------------
log "$CONTROL: каталог (PostgreSQL + registry)"
ensure_env
kc "$CONTROL" apply -f "$STAND/registry/namespace.yaml" >/dev/null
kc "$CONTROL" create secret generic registry-db -n "$REGISTRY_NAMESPACE" \
  --from-literal=password="$REGISTRY_DB_PASSWORD" \
  --from-literal=database_url="postgresql://registry:$REGISTRY_DB_PASSWORD@postgres.$REGISTRY_NAMESPACE.svc:5432/registry" \
  --dry-run=client -o yaml | kc "$CONTROL" apply -f - >/dev/null
kc "$CONTROL" create secret generic registry-tokens -n "$REGISTRY_NAMESPACE" \
  --from-literal=scheduler="$REGISTRY_TOKEN_SCHEDULER" \
  --from-literal=dr-controller="$REGISTRY_TOKEN_DR_CONTROLLER" \
  --from-literal=admin="$REGISTRY_TOKEN_ADMIN" \
  --dry-run=client -o yaml | kc "$CONTROL" apply -f - >/dev/null
# Внутрисетевые kubeconfig'и федерации: проверка идентичности при регистрации
# и watcher нарушений (ADR 0018, 0019).
kubeconfigs=()
for c in "${FEDERATION[@]}"; do
  kubeconfigs+=(--from-file="$c.kubeconfig=$KUBECONFIG_DIR/$c.internal.kubeconfig")
done
kc "$CONTROL" create secret generic federation-kubeconfigs -n "$REGISTRY_NAMESPACE" \
  "${kubeconfigs[@]}" --dry-run=client -o yaml | kc "$CONTROL" apply -f - >/dev/null
kc "$CONTROL" apply -f "$STAND/registry/postgres.yaml" >/dev/null
kc "$CONTROL" apply -f "$STAND/registry/registry.yaml" >/dev/null
kc "$CONTROL" -n "$REGISTRY_NAMESPACE" rollout status deployment/postgres --timeout=180s >/dev/null
# Secret'ы и образ могли смениться: перечитать через новый под.
kc "$CONTROL" -n "$REGISTRY_NAMESPACE" rollout restart deployment/registry >/dev/null
kc "$CONTROL" -n "$REGISTRY_NAMESPACE" rollout status deployment/registry --timeout=180s >/dev/null
# После rollout NodePort ещё немного отдаёт запросы старому (Terminating) поду —
# конкретный ответ то есть, то нет; ждём до 3 минут и трёх подряд успехов, чтобы не
# поймать именно момент перехода.
ok=0
for _ in $(seq 1 90); do
  if curl -sf "$REGISTRY_URL/healthz" >/dev/null 2>&1; then
    ok=$((ok + 1))
    [ "$ok" -ge 3 ] && break
  else
    ok=0
  fi
  sleep 2
done
[ "$ok" -ge 3 ] \
  || die "каталог не отвечает на $REGISTRY_URL: $(curl -s -m 5 "$REGISTRY_URL/healthz" 2>&1 | head -c 200); поды: $(kc "$CONTROL" -n "$REGISTRY_NAMESPACE" get pods --no-headers 2>/dev/null | tr '\n' ';')"
"$STAND/scripts/register-clusters.sh"

# --- 4d. Control: планировщик (ADR 0020, 0027) -------------------------------
log "$CONTROL: планировщик (CRD Workload, режим meta)"
kc "$CONTROL" apply -f "$STAND/scheduler/federation.spbu_workloads.yaml" >/dev/null
kc "$CONTROL" apply -f "$STAND/scheduler/rbac.yaml" -f "$STAND/scheduler/deployment.yaml" >/dev/null
# Образ и Secret'ы могли смениться: перечитать через новый под.
kc "$CONTROL" -n "$REGISTRY_NAMESPACE" rollout restart deployment/scheduler >/dev/null
kc "$CONTROL" -n "$REGISTRY_NAMESPACE" rollout status deployment/scheduler --timeout=180s >/dev/null

# --- 4e. Control: DR Controller (ADR 0007, 0028) -----------------------------
log "$CONTROL: DR Controller (режим active)"
kc "$CONTROL" apply -f "$STAND/dr-controller/rbac.yaml" -f "$STAND/dr-controller/deployment.yaml" >/dev/null
# Образ и Secret'ы могли смениться: перечитать через новый под.
kc "$CONTROL" -n "$REGISTRY_NAMESPACE" rollout restart deployment/dr-controller >/dev/null
kc "$CONTROL" -n "$REGISTRY_NAMESPACE" rollout status deployment/dr-controller --timeout=180s >/dev/null

# --- 5. Дымовой тест: workload через планировщик и шлюз ---------------------
GEN="uv run --quiet --project $ROOT/experiments/load/workloadgen workloadgen"
registry_get() { curl -sf -H "Authorization: Bearer $REGISTRY_TOKEN_ADMIN" "$REGISTRY_URL$1"; }
pci_clusters=$(yq -o=json -I=0 '[.clusters[] | select((.certifications // []) | contains(["pci-dss"])) | .id] | sort' \
  "$STAND/clusters.yaml")
deny_cluster=$(yq -r '.clusters[] | select((.certifications // []) | contains(["pci-dss"]) | not) | .id' \
  "$STAND/clusters.yaml" | head -1)

log "дымовой тест: smoke.fed.local через планировщик"
$GEN --mode scheduler --timeout 120 place --name smoke --port 31999 --wait >/dev/null
body=$(curl -sf -H 'Host: smoke.fed.local' http://127.0.0.1:8080/)
smoke_cluster=$(kc "$CONTROL" -n "$REGISTRY_NAMESPACE" get workload smoke -o jsonpath='{.status.cluster}')
decision=$(registry_get "/api/v1/decisions?workload_ref=workloads/smoke" | jq -c 'last')
$GEN --mode scheduler remove --name smoke >/dev/null
echo "$body" | jq -e '.tier == "frontend" and .backend.tier == "backend"' >/dev/null \
  || die "дымовой тест: неожиданный ответ: $body"
jq -e --arg c "$smoke_cluster" --argjson n "${#FEDERATION[@]}" \
  '.outcome == "placed" and .chosen_cluster == $c and (.candidates | length) == $n and .scoring.formula == "adr-0005"' \
  <<<"$decision" >/dev/null || die "дымовой тест: решение планировщика неполное: $decision"

# Hard constraint на рубеже 1: pci-dss workload попадает только в кластер с сертификацией,
# остальные отсеяны в решении с причиной.
log "дымовой тест: планировщик соблюдает require-certification=pci-dss"
$GEN --mode scheduler --timeout 120 place --name smoke-pci --port 31996 \
  --annotate federation.spbu/require-certification=pci-dss --wait >/dev/null
pci_cluster=$(kc "$CONTROL" -n "$REGISTRY_NAMESPACE" get workload smoke-pci -o jsonpath='{.status.cluster}')
decision=$(registry_get "/api/v1/decisions?workload_ref=workloads/smoke-pci" | jq -c 'last')
$GEN --mode scheduler remove --name smoke-pci >/dev/null
jq -e --arg c "$pci_cluster" 'index($c) != null' <<<"$pci_clusters" >/dev/null \
  || die "дымовой тест: smoke-pci размещён в $pci_cluster, допустимы $pci_clusters"
if [ -n "$deny_cluster" ]; then
  jq -e --arg c "$deny_cluster" '[.candidates[] | select(.cluster == $c and (.filtered_by // "" | contains("certification:pci-dss")))] | length == 1' \
    <<<"$decision" >/dev/null || die "дымовой тест: в решении нет отсева $deny_cluster по сертификации: $decision"
fi
app=$(curl -sf -H 'Host: app.fed.local' http://127.0.0.1:8080/ | jq -r .workload)
[ "$app" = "app" ] || die "дымовой тест: app.fed.local не отвечает"

# Наблюдаемость: Querier видит все store'ы, Grafana жива.
log "дымовой тест: Thanos Querier и Grafana"
expected=$(( ${#FEDERATION[@]} + 2 ))
for _ in $(seq 1 30); do
  stores=$(curl -sf http://127.0.0.1:10902/api/v1/stores | jq '[.data[][] | select(.lastCheck != null)] | length' 2>/dev/null || echo 0)
  [ "$stores" -ge "$expected" ] && break
  sleep 2
done
[ "$stores" -ge "$expected" ] || die "Thanos Querier видит $stores store'ов из $expected"
curl -sf http://127.0.0.1:3000/api/health | jq -e '.database == "ok"' >/dev/null || die "Grafana не отвечает"

# Каталог: все кластеры федерации зарегистрированы и получили метрики из Thanos
# (первый цикл опроса — при старте, далее раз в 30 с).
log "дымовой тест: каталог видит метрики кластеров"
for _ in $(seq 1 45); do
  with_metrics=$(curl -sf -H "Authorization: Bearer $REGISTRY_TOKEN_ADMIN" "$REGISTRY_URL/api/v1/clusters" \
    | jq '[.[] | select(.metrics.saturation != null)] | length' 2>/dev/null || echo 0)
  [ "$with_metrics" -ge "${#FEDERATION[@]}" ] && break
  sleep 2
done
[ "$with_metrics" -ge "${#FEDERATION[@]}" ] \
  || die "каталог: метрики есть у $with_metrics кластеров из ${#FEDERATION[@]}"

# DR Controller: все кластеры федерации HEALTHY, веса маршрута app номинальные, режим active.
log "дымовой тест: DR Controller видит кластеры здоровыми"
dr=""
for _ in $(seq 1 30); do
  dr=$(dr_status 2>/dev/null || true)
  if jq -e --argjson n "${#FEDERATION[@]}" \
    '.mode == "active" and (.clusters | length) == $n
     and all(.clusters[]; .state == "healthy" and .weight == .desired_weight and .registry_status != "down")' \
    <<<"$dr" >/dev/null 2>&1; then
    break
  fi
  dr=""
  sleep 3
done
[ -n "$dr" ] || die "DR Controller: кластеры не HEALTHY или веса не номинальные: $(dr_status 2>&1 | head -c 600)"

# Kyverno (рубеж 2, в обход планировщика): workload с требованием сертификации, которой
# нет у кластера, отклоняется (код 3 у генератора) и попадает в audit log через watcher
# (ADR 0019, 0026).
if [ -n "$deny_cluster" ]; then
  log "дымовой тест: Kyverno отклоняет pci-dss workload в $deny_cluster, каталог фиксирует"
  set +e
  $GEN place --name smoke-deny --cluster "$deny_cluster" --port 31997 \
    --annotate federation.spbu/require-certification=pci-dss >"$KUBECONFIG_DIR/smoke-deny.log" 2>&1
  rc=$?
  set -e
  [ "$rc" -eq 3 ] || { cat "$KUBECONFIG_DIR/smoke-deny.log" >&2; die "дымовой тест: ожидался отказ Kyverno (код 3), код $rc"; }
  if kc "$deny_cluster" -n workloads get deployment smoke-deny >/dev/null 2>&1; then
    die "дымовой тест: Deployment smoke-deny существует в $deny_cluster"
  fi
  expected_eligible=$pci_clusters
  eligible=null
  for _ in $(seq 1 30); do
    eligible=$(registry_get "/api/v1/violations?workload_ref=workloads/smoke-deny&cluster_id=$deny_cluster" \
      | jq -c 'last | .details.eligible_clusters | sort' 2>/dev/null || echo null)
    [ "$eligible" = "$expected_eligible" ] && break
    sleep 2
  done
  [ "$eligible" = "$expected_eligible" ] \
    || die "дымовой тест: нарушение smoke-deny не в каталоге (eligible_clusters=$eligible, ожидалось $expected_eligible)"
else
  warn "в clusters.yaml нет кластера без pci-dss: проверка отказа Kyverno пропущена"
fi

log "стенд готов"
"$STAND/scripts/status.sh"
