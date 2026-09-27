#!/usr/bin/env bash
# Сценарий 3 (spec §6, ADR 0019 путь 2, ADR 0026): попытки разместить workload с
# требованием сертификации в кластер, у которого её нет, в обход планировщика.
#
#   experiments/policy/run.sh <run-id> [attempts=5]
#   ENFORCEMENT=audit experiments/policy/run.sh <run-id>   # «до» enforcement (ADR 0032)
#   ENFORCEMENT=audit HOLD=report experiments/policy/run.sh <run-id>   # путь PolicyReport (ADR 0034)
#
# Замер: attempts; rejected — отклонил Kyverno (код 3 генератора); admitted — Deployment
# всё же создан (нарушение, целевой показатель в Enforce — 0); recorded — нарушителей в
# audit log каталога (уникальные workload_ref; recorded_raw и recorded_by_source — сырые
# записи по событиям и PolicyReport); eligible_ok — каталог предложил корректные кластеры;
# detection_latency_s — от occurred_at (время события Kyverno) до details.detected_at.
#
# ENFORCEMENT=audit: политики целевого кластера на время прогона переводятся в Audit и
# возвращаются в Enforce из stand/policies/ в ловушке выхода. Нарушители остаются до
# фиксации в каталоге (или WAIT_S), затем удаляются. Перед прогоном в любом режиме
# политики должны быть в Enforce и Ready.
#
# HOLD=report (только с audit): нарушители живут, пока каждый не попадёт в каталог и из
# PolicyReport, или до WAIT_S=360 с — больше периода фонового сканирования Kyverno. В
# сводке — нарушители и задержка фиксации по источникам и policyreport_since_event_s: от
# события admission нарушителя до записи из отчёта (ADR 0034).
# Результат: experiments/results/<run-id>/{meta.json,violations.json,summary.json,workloadgen.log}.
# RESULTS_ROOT=<dir> переопределяет каталог результатов (пробные прогоны).
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
RUN_ID=${1:?run-id}
ATTEMPTS=${2:-5}
CERT=${CERT:-pci-dss}
RESULTS_ROOT=${RESULTS_ROOT:-$ROOT/experiments/results}
REGISTRY_URL=${REGISTRY_URL:-http://127.0.0.1:8000}
ENFORCEMENT=${ENFORCEMENT:-enforce}        # enforce | audit (ADR 0032)
HOLD=${HOLD:-}                             # пусто | report (ADR 0034)
# Ожидание записей watcher'а (период 10 с); с HOLD=report — ещё и агрегации PolicyReport.
if [ "$HOLD" = report ]; then WAIT_S=${WAIT_S:-360}; else WAIT_S=${WAIT_S:-90}; fi
POLICIES=(require-certification require-region-match)
OUT="$RESULTS_ROOT/$RUN_ID"
GEN="uv run --quiet --project $ROOT/experiments/load/workloadgen workloadgen"
CLUSTERS="$ROOT/stand/clusters.yaml"

log() { printf '%s policy/run.sh: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

if [ -e "$OUT" ]; then
  log "$OUT уже существует; результаты не перезаписываются"
  exit 1
fi
[ -f "$ROOT/.env" ] || { log "нет $ROOT/.env; его создаёт make stand-up"; exit 1; }
set -a
# shellcheck disable=SC1091
source "$ROOT/.env"
set +a
: "${REGISTRY_TOKEN_ADMIN:?нет REGISTRY_TOKEN_ADMIN в .env}"

# Целевой кластер — первый без требуемой сертификации; ожидаемая подсказка каталога — кластеры с ней.
target=$(CERT="$CERT" yq -r '.clusters[] | select((.certifications // []) | contains([strenv(CERT)]) | not) | .id' \
  "$CLUSTERS" | head -1)
expected=$(CERT="$CERT" yq -o=json -I=0 '[.clusters[] | select((.certifications // []) | contains([strenv(CERT)])) | .id] | sort' \
  "$CLUSTERS")
[ -n "$target" ] || { log "в $CLUSTERS нет кластера без $CERT"; exit 1; }
case "$ENFORCEMENT" in enforce|audit) ;; *) log "ENFORCEMENT: enforce | audit"; exit 1 ;; esac
case "$HOLD" in ''|report) ;; *) log "HOLD: пусто | report"; exit 1 ;; esac
[ -z "$HOLD" ] || [ "$ENFORCEMENT" = audit ] || { log "HOLD=report — только с ENFORCEMENT=audit (ADR 0034)"; exit 1; }
KC_TARGET=(kubectl --kubeconfig "$ROOT/stand/kubeconfig/$target.kubeconfig")

policies_in() {  # policies_in <Enforce|Audit> — все правила обеих политик в режиме и Ready
  local p
  for p in "${POLICIES[@]}"; do
    "${KC_TARGET[@]}" get clusterpolicy "$p" -o json 2>/dev/null | jq -e --arg m "$1" '
      all(.spec.rules[]; .validate.failureAction == $m)
      and any(.status.conditions[]?; .type == "Ready" and .status == "True")' >/dev/null || return 1
  done
}
wait_policies() {  # wait_policies <режим>; пауза — на обновление webhook'ов Kyverno
  for _ in $(seq 1 30); do
    policies_in "$1" && { sleep 10; return 0; }
    sleep 2
  done
  return 1
}
# shellcheck disable=SC2329  # вызывается из trap
restore_policies() {
  if "${KC_TARGET[@]}" apply -f "$ROOT/stand/policies/require-certification.yaml" \
      -f "$ROOT/stand/policies/require-region-match.yaml" >/dev/null && wait_policies Enforce; then
    log "политики $target снова в Enforce"
  else
    log "политики $target не вернулись в Enforce: kubectl apply -f stand/policies/require-*.yaml"
  fi
}

policies_in Enforce || { log "политики $target не в Enforce/Ready: kubectl apply -f stand/policies/require-*.yaml"; exit 1; }
mkdir -p "$OUT"
if [ "$ENFORCEMENT" = audit ]; then
  trap restore_policies EXIT
  # JSON patch, не replace: replace перезаписывает last-applied-configuration полным
  # объектом с resourceVersion, и возврат через apply API отвергает (ADR 0032).
  for p in "${POLICIES[@]}"; do
    patch=$("${KC_TARGET[@]}" get clusterpolicy "$p" -o json | jq -c '
      [range(0; .spec.rules | length) | {op: "replace", value: "Audit",
        path: "/spec/rules/\(.)/validate/failureAction"}]')
    "${KC_TARGET[@]}" patch clusterpolicy "$p" --type=json -p "$patch" >/dev/null
  done
  wait_policies Audit || { log "политики $target не перешли в Audit"; exit 1; }
  log "политики $target в Audit на время прогона"
fi

$GEN cleanup >/dev/null 2>&1 || true
STARTED=$(date -u +%FT%TZ)
rejected=0; admitted=0; other=0
placed=()
for i in $(seq 1 "$ATTEMPTS"); do
  name="pv-$i"
  set +e
  $GEN place --name "$name" --cluster "$target" --port $((31900 + i % 90)) \
    --annotate "federation.spbu/require-certification=$CERT" >>"$OUT/workloadgen.log" 2>&1
  rc=$?
  set -e
  case $rc in
    3) rejected=$((rejected + 1)) ;;
    0) admitted=$((admitted + 1)); log "$name: размещён в $target — нарушение политики"
       placed+=("$name")
       # В Enforce нарушитель — дефект, убираем сразу; в Audit он ждёт фиксации в каталоге.
       [ "$ENFORCEMENT" = audit ] || $GEN remove --name "$name" >/dev/null 2>&1 || true ;;
    *) other=$((other + 1)); log "$name: код $rc (не отказ Kyverno), см. workloadgen.log" ;;
  esac
  sleep 1
done
log "попыток $ATTEMPTS: отклонено $rejected, размещено $admitted, иных ошибок $other"

# Записи watcher'а: ждём, пока нарушителей в каталоге станет не меньше ожидаемого — отказов
# в Enforce, размещённых в Audit. Нарушитель считается один раз (ADR 0032). С HOLD=report
# каждый должен прийти ещё и из PolicyReport (ADR 0034).
if [ "$ENFORCEMENT" = audit ]; then want=$admitted; else want=$rejected; fi
deadline=$(( $(date +%s) + WAIT_S ))
recorded=0; reported=0
while :; do
  curl -sf -H "Authorization: Bearer $REGISTRY_TOKEN_ADMIN" \
    "$REGISTRY_URL/api/v1/violations?from=$STARTED&cluster_id=$target" >"$OUT/violations.json" \
    || echo '[]' >"$OUT/violations.json"
  recorded=$(jq '[.[] | select(.workload_ref | startswith("workloads/pv-")) | .workload_ref] | unique | length' \
    "$OUT/violations.json")
  reported=$(jq '[.[] | select(.workload_ref | startswith("workloads/pv-"))
    | select((.source_key // "") | startswith("policyreport/")) | .workload_ref] | unique | length' \
    "$OUT/violations.json")
  if [ "$recorded" -ge "$want" ] && { [ "$HOLD" != report ] || [ "$reported" -ge "$want" ]; }; then
    break
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    log "в каталоге $recorded нарушителей из $want (из PolicyReport — $reported) за $WAIT_S с"
    break
  fi
  sleep 3
done
FINISHED=$(date -u +%FT%TZ)
if [ "$ENFORCEMENT" = audit ]; then
  for name in "${placed[@]+"${placed[@]}"}"; do $GEN remove --name "$name" >/dev/null 2>&1 || true; done
fi

# Нарушающих Deployment'ов в целевом кластере остаться не должно.
leftover=$("${KC_TARGET[@]}" -n workloads \
  get deployment -l federation.spbu/managed-by=workloadgen -o name 2>/dev/null | grep -c '/pv-' || true)

jq -n --arg run_id "$RUN_ID" --arg started "$STARTED" --arg finished "$FINISHED" \
  --argjson attempts "$ATTEMPTS" --arg target "$target" --argjson expected "$expected" --arg cert "$CERT" \
  --arg enforcement "$ENFORCEMENT" --arg hold "$HOLD" \
  --arg git "$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)" \
  '{run_id:$run_id, scenario:"3-policy-violation", started_at:$started, finished_at:$finished,
    enforcement:$enforcement, hold:(if $hold == "" then null else $hold end), wait_s:'"$WAIT_S"',
    attempts:$attempts, certification:$cert, target_cluster:$target, expected_eligible:$expected,
    placed_by:"workloadgen (в обход планировщика)", git_rev:$git}' >"$OUT/meta.json"

# fromdateiso8601 в jq не понимает доли секунды и смещение +00:00: целую часть
# разбираем им, доли секунды прибавляем отдельно.
jq --argjson attempts "$ATTEMPTS" --argjson rejected "$rejected" --argjson admitted "$admitted" \
   --argjson other "$other" --argjson leftover "$leftover" --argjson expected "$expected" \
   --arg enforcement "$ENFORCEMENT" --arg hold "$HOLD" '
  def ts: . as $s
    | ($s | sub("\\.[0-9]+"; "") | sub("\\+00:00$"; "Z") | fromdateiso8601)
    + ((($s | capture("\\.(?<f>[0-9]+)") | .f) // "0") | ("0." + .) | tonumber);
  def src: .source_key // "scheduler" | split("/")[0];
  def lat: map(select(.details.detected_at != null) | ((.details.detected_at | ts) - (.occurred_at | ts)));
  def stats: sort | if length == 0 then {max:null, median:null}
    else {max:(max * 1000 | round / 1000), median:(.[length / 2 | floor] * 1000 | round / 1000)} end;
  [.[] | select(.workload_ref | startswith("workloads/pv-"))] as $v
  | ($v | map(select((.details.eligible_clusters // []) | sort == $expected)) | length) as $ok
  | ($v | group_by(src)) as $by_src
  # Путь PolicyReport целиком: от события admission нарушителя до записи из отчёта (ADR 0034).
  | [$v | group_by(.workload_ref)[]
      | (map(select(src == "event") | .occurred_at | ts) | min) as $ev
      | (map(select(src == "policyreport" and .details.detected_at != null) | .details.detected_at | ts) | min) as $pr
      | select($ev != null and $pr != null) | $pr - $ev] as $pr_since_event
  | {enforcement:$enforcement, hold:(if $hold == "" then null else $hold end),
     attempts:$attempts, rejected:$rejected, admitted:$admitted, other_errors:$other,
     violating_deployments_left:$leftover, recorded:($v | map(.workload_ref) | unique | length),
     recorded_raw:($v|length),
     recorded_by_source:($by_src | map({key:(.[0] | src), value:length}) | from_entries),
     recorded_unique_by_source:($by_src
       | map({key:(.[0] | src), value:(map(.workload_ref) | unique | length)}) | from_entries),
     eligible_ok:$ok,
     detection_latency_s:($v | lat | stats),
     detection_latency_by_source_s:($by_src | map({key:(.[0] | src), value:(lat | stats)}) | from_entries),
     policyreport_since_event_s:($pr_since_event | stats)}' \
  "$OUT/violations.json" >"$OUT/summary.json"
cat "$OUT/summary.json" >&2

if [ "$ENFORCEMENT" = audit ]; then
  # «До»: размещение нарушителей — ожидаемый результат, а не сбой прогона.
  if [ "$leftover" -eq 0 ] && [ "$other" -eq 0 ]; then
    [ "$recorded" -ge "$admitted" ] || log "зафиксировано $recorded нарушителей из $admitted размещённых"
    [ "$HOLD" != report ] || [ "$reported" -ge "$admitted" ] \
      || log "из PolicyReport зафиксировано $reported нарушителей из $admitted размещённых (ADR 0034)"
    log "готово: $OUT"
    exit 0
  fi
elif [ "$admitted" -eq 0 ] && [ "$leftover" -eq 0 ] && [ "$other" -eq 0 ] && [ "$recorded" -ge "$rejected" ]; then
  log "готово: $OUT"
  exit 0
fi
log "сценарий 3 не сошёлся: enforcement=$ENFORCEMENT admitted=$admitted leftover=$leftover other=$other recorded=$recorded/$want"
exit 1
