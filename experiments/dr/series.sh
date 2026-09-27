#!/usr/bin/env bash
# Серия сценария 1 (ADR 0008, 0028): REPEATS прогонов на режим DR, active и observe
# чередуются (a, o, a, o, …), чтобы дрейф стенда делился поровну. На выходе DR
# возвращается в active — состояние после make stand-up.
#
#   experiments/dr/series.sh <префикс> [повторов=3] [кластер=cluster-b]
#
# Прогоны: <префикс>-active-r<i>, <префикс>-observe-r<i> в experiments/results/. Перед
# каждым прогоном ждём штатного стенда (HEALTHY, веса номинальные, down нет) до
# NOMINAL_WAIT секунд: возврат трафика предыдущего прогона мог не закончиться. Прогон,
# упавший с ошибкой, останавливает серию. Таблица — analysis/scenario1/<префикс>.{md,json}.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=../../stand/scripts/lib.sh
source "$ROOT/stand/scripts/lib.sh"

PREFIX=${1:?префикс run-id}
REPEATS=${2:-3}
CLUSTER=${3:-cluster-b}
NOMINAL_WAIT=${NOMINAL_WAIT:-600}
RESULTS_ROOT=${RESULTS_ROOT:-$ROOT/experiments/results}
ANALYSIS_DIR=${ANALYSIS_DIR:-$ROOT/analysis/scenario1}

log() { printf '%s dr/series.sh: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

wait_nominal() {
  local deadline=$(( $(date +%s) + NOMINAL_WAIT ))
  until dr_status 2>/dev/null | jq -e 'all(.clusters[];
      .state == "healthy" and .weight == .desired_weight and .registry_status != "down")' >/dev/null; do
    [ "$(date +%s)" -lt "$deadline" ] || { log "стенд не вернулся в штатное состояние за ${NOMINAL_WAIT}s"; return 1; }
    sleep 10
  done
}

# shellcheck disable=SC2329  # вызывается из trap
restore() {
  wait_nominal || true
  "$ROOT/stand/scripts/dr-mode.sh" active >/dev/null || log "не удалось вернуть DR в active"
}
trap restore EXIT

runs=()
for i in $(seq 1 "$REPEATS"); do
  for mode in active observe; do
    run="$PREFIX-$mode-r$i"
    wait_nominal || exit 1
    "$ROOT/stand/scripts/dr-mode.sh" "$mode" >/dev/null
    log "прогон $run (DR $mode, отказ в $CLUSTER)"
    RESULTS_ROOT="$RESULTS_ROOT" "$ROOT/experiments/dr/run.sh" "$run" "$CLUSTER"
    runs+=("$RESULTS_ROOT/$run")
  done
done

mkdir -p "$ANALYSIS_DIR"
uv run --no-project --quiet --python 3.13 python "$ROOT/experiments/dr/compare.py" \
  --json "$ANALYSIS_DIR/$PREFIX.json" "${runs[@]}" | tee "$ANALYSIS_DIR/$PREFIX.md"
