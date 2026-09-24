#!/usr/bin/env bash
# Серия сценария 2 (ADR 0031): REPEATS прогонов на режим, meta и naive чередуются
# (m, n, m, n, …), чтобы дрейф стенда делился поровну. DR на время серии — в observe; на
# выходе режимы возвращаются к состоянию после make stand-up (планировщик meta, DR active).
#
#   experiments/scheduler/series.sh <префикс> [повторов=3] [кластер=cluster-b]
#
# Прогоны: <префикс>-meta-r<i>, <префикс>-naive-r<i> в experiments/results/. Прогон,
# упавший с ошибкой, останавливает серию; брак по признакам сводки — нет: он виден в
# таблице сравнения и в среднее не входит. Таблица — analysis/scenario2/<префикс>.{md,json}.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
PREFIX=${1:?префикс run-id}
REPEATS=${2:-3}
CLUSTER=${3:-cluster-b}
RESULTS_ROOT=${RESULTS_ROOT:-$ROOT/experiments/results}
ANALYSIS_DIR=${ANALYSIS_DIR:-$ROOT/analysis/scenario2}

log() { printf '%s scheduler/series.sh: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

restore() {
  "$ROOT/stand/scripts/scheduler-mode.sh" meta >/dev/null || log "не удалось вернуть планировщик в meta"
  "$ROOT/stand/scripts/dr-mode.sh" active >/dev/null || log "не удалось вернуть DR в active"
}
trap restore EXIT

"$ROOT/stand/scripts/dr-mode.sh" observe >/dev/null
runs=()
for i in $(seq 1 "$REPEATS"); do
  for mode in meta naive; do
    run="$PREFIX-$mode-r$i"
    "$ROOT/stand/scripts/scheduler-mode.sh" "$mode" >/dev/null
    log "прогон $run ($mode, отказ в $CLUSTER)"
    RESULTS_ROOT="$RESULTS_ROOT" "$ROOT/experiments/scheduler/run.sh" "$run" "$CLUSTER"
    runs+=("$RESULTS_ROOT/$run")
  done
done

mkdir -p "$ANALYSIS_DIR"
uv run --no-project --quiet --python 3.13 python "$ROOT/experiments/scheduler/compare.py" \
  --json "$ANALYSIS_DIR/$PREFIX.json" "${runs[@]}" | tee "$ANALYSIS_DIR/$PREFIX.md"
