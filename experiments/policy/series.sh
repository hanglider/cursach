#!/usr/bin/env bash
# Серия сценария 3 (ADR 0026, 0032, 0034): REPEATS прогонов на набор — audit («до»),
# enforce («после») и audit с удержанием нарушителей до записи из PolicyReport — и сводка.
#
#   experiments/policy/series.sh <префикс> [повторов=3] [попыток=5]
#
# Прогоны: <префикс>-audit-r<i>, <префикс>-enforce-r<i>, <префикс>-audit-hold-r<i> в
# experiments/results/. Прогон, упавший с ошибкой, останавливает серию. Сводка —
# analysis/scenario3/<префикс>.{md,json}; SETS="audit enforce" — только часть наборов.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
PREFIX=${1:?префикс run-id}
REPEATS=${2:-3}
ATTEMPTS=${3:-5}
SETS=${SETS:-audit enforce audit-hold}
RESULTS_ROOT=${RESULTS_ROOT:-$ROOT/experiments/results}
ANALYSIS_DIR=${ANALYSIS_DIR:-$ROOT/analysis/scenario3}

log() { printf '%s policy/series.sh: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

runs=()
for set in $SETS; do
  case "$set" in
    audit) env=(ENFORCEMENT=audit HOLD=) ;;
    enforce) env=(ENFORCEMENT=enforce HOLD=) ;;
    audit-hold) env=(ENFORCEMENT=audit HOLD=report) ;;
    *) log "набор: audit | enforce | audit-hold (получено '$set')"; exit 1 ;;
  esac
  for i in $(seq 1 "$REPEATS"); do
    run="$PREFIX-$set-r$i"
    log "прогон $run"
    env "${env[@]}" RESULTS_ROOT="$RESULTS_ROOT" "$ROOT/experiments/policy/run.sh" "$run" "$ATTEMPTS"
    runs+=("$RESULTS_ROOT/$run")
  done
done

mkdir -p "$ANALYSIS_DIR"
uv run --no-project --quiet --python 3.13 python "$ROOT/experiments/policy/compare.py" \
  --json "$ANALYSIS_DIR/$PREFIX.json" "${runs[@]}" | tee "$ANALYSIS_DIR/$PREFIX.md"
