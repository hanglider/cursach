#!/usr/bin/env bash
#
# PostToolUse hook: проверяет файл, который агент только что создал или изменил.
#
# Установка:
#   1. Положить в .claude/hooks/lint.sh
#   2. chmod +x .claude/hooks/lint.sh        <-- без этого хук молча не работает
#   3. Прописать в .claude/settings.json:
#
#      {
#        "hooks": {
#          "PostToolUse": [
#            {
#              "matcher": "Edit|Write|MultiEdit",
#              "hooks": [
#                { "type": "command", "command": ".claude/hooks/lint.sh" }
#              ]
#            }
#          ]
#        }
#      }
#
# Как это работает:
#   На stdin приходит JSON события. Достаём путь к файлу, по расширению
#   выбираем проверку, запускаем её ТОЛЬКО на этом файле.
#   Выход 0 - всё чисто, агент продолжает.
#   Выход 2 - есть замечания; stderr уходит агенту, и он чинит сам.
#
# Зависимости: jq (brew install jq). Линтеры опциональны - отсутствующие
# пропускаются, скрипт не падает.

set -uo pipefail

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

# go vet требует, чтобы пакет компилировался. Пока агент пишет код
# по частям, это даёт шум на промежуточных состояниях.
# Выставь 0, если мешает - gofmt продолжит работать.
: "${LINT_GO_VET:=1}"

# Таймаут на один линтер, секунды. Хук блокирует агента, пока работает.
: "${LINT_TIMEOUT:=30}"

# ---------------------------------------------------------------------------
# Разбор входа
# ---------------------------------------------------------------------------

if ! command -v jq >/dev/null 2>&1; then
  echo "lint.sh: jq не установлен, проверки пропущены (brew install jq)" >&2
  exit 0
fi

INPUT=$(cat)
FILE=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty')

# Нет пути (например, инструмент не файловый) - выходим тихо.
[ -n "$FILE" ] || exit 0
[ -f "$FILE" ] || exit 0

# Не проверяем то, что не наше.
case "$FILE" in
  */.git/*|*/node_modules/*|*/vendor/*|*/.venv/*|*/experiments/results/*)
    exit 0
    ;;
esac

# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------

PROBLEMS=""

have() { command -v "$1" >/dev/null 2>&1; }

# Запуск с таймаутом. На macOS timeout идёт из coreutils (gtimeout),
# без него запускаем как есть.
run() {
  if have timeout; then
    timeout "$LINT_TIMEOUT" "$@"
  elif have gtimeout; then
    gtimeout "$LINT_TIMEOUT" "$@"
  else
    "$@"
  fi
}

add_problem() {
  PROBLEMS="${PROBLEMS}$1"$'\n'
}

# ---------------------------------------------------------------------------
# Проверки по типу файла
# ---------------------------------------------------------------------------

case "$FILE" in

  # ---- Go: meta-scheduler, dr-controller ----
  *.go)
    if have gofmt; then
      UNFORMATTED=$(run gofmt -l "$FILE" 2>&1)
      if [ -n "$UNFORMATTED" ]; then
        add_problem "gofmt: файл не отформатирован. Выполни: gofmt -w $FILE"
      fi
    fi

    if [ "$LINT_GO_VET" = "1" ] && have go; then
      PKG_DIR=$(dirname "$FILE")
      VET_OUT=$(run go vet "./$PKG_DIR" 2>&1)
      VET_CODE=$?
      # Код 124 - таймаут, код 1 с пустым выводом - обычно проблемы сборки
      # на промежуточном состоянии, их не репортим.
      if [ $VET_CODE -ne 0 ] && [ $VET_CODE -ne 124 ] && [ -n "$VET_OUT" ]; then
        add_problem "go vet:"$'\n'"$VET_OUT"
      fi
    fi
    ;;

  # ---- Python: registry service ----
  *.py)
    if have ruff; then
      RUFF_OUT=$(run ruff check "$FILE" 2>&1)
      [ $? -ne 0 ] && [ -n "$RUFF_OUT" ] && add_problem "ruff:"$'\n'"$RUFF_OUT"

      FMT_OUT=$(run ruff format --check "$FILE" 2>&1)
      [ $? -ne 0 ] && add_problem "ruff format: файл не отформатирован. Выполни: ruff format $FILE"
    elif have python3; then
      # Минимум: синтаксис валиден.
      SYNTAX_OUT=$(run python3 -m py_compile "$FILE" 2>&1)
      [ $? -ne 0 ] && add_problem "python: синтаксическая ошибка"$'\n'"$SYNTAX_OUT"
    fi
    ;;

  # ---- YAML: манифесты k8s, Kyverno-политики, конфиги ----
  *.yaml|*.yml)
    if have yamllint; then
      YL_OUT=$(run yamllint -f parsable "$FILE" 2>&1)
      [ $? -ne 0 ] && [ -n "$YL_OUT" ] && add_problem "yamllint:"$'\n'"$YL_OUT"
    elif have python3; then
      PY_OUT=$(run python3 -c "import sys,yaml; list(yaml.safe_load_all(open(sys.argv[1])))" "$FILE" 2>&1)
      [ $? -ne 0 ] && add_problem "yaml: файл не парсится"$'\n'"$PY_OUT"
    fi

    # Манифест Kubernetes - проверяем схему без обращения к кластеру.
    if have kubeconform && grep -qE '^(apiVersion|kind):' "$FILE" 2>/dev/null; then
      KC_OUT=$(run kubeconform -summary -ignore-missing-schemas "$FILE" 2>&1)
      [ $? -ne 0 ] && [ -n "$KC_OUT" ] && add_problem "kubeconform:"$'\n'"$KC_OUT"
    fi
    ;;

  # ---- Shell: скрипты стенда и сами хуки ----
  *.sh)
    if have shellcheck; then
      SC_OUT=$(run shellcheck -f gcc "$FILE" 2>&1)
      [ $? -ne 0 ] && [ -n "$SC_OUT" ] && add_problem "shellcheck:"$'\n'"$SC_OUT"
    fi
    # Скрипт без бита исполнения - частая причина молча неработающих хуков.
    if [ ! -x "$FILE" ]; then
      add_problem "chmod: скрипт не исполняемый. Выполни: chmod +x $FILE"
    fi
    ;;

  # ---- Dockerfile ----
  */Dockerfile|*.Dockerfile|Dockerfile)
    if have hadolint; then
      HL_OUT=$(run hadolint "$FILE" 2>&1)
      [ $? -ne 0 ] && [ -n "$HL_OUT" ] && add_problem "hadolint:"$'\n'"$HL_OUT"
    fi
    ;;

  # ---- Terraform, если появится ----
  *.tf)
    if have terraform; then
      TF_OUT=$(run terraform fmt -check "$FILE" 2>&1)
      [ $? -ne 0 ] && add_problem "terraform fmt: файл не отформатирован. Выполни: terraform fmt $FILE"
    fi
    ;;

  # ---- SQL: миграции каталога ----
  *.sql)
    if have sqlfluff; then
      SF_OUT=$(run sqlfluff lint --dialect postgres "$FILE" 2>&1)
      [ $? -ne 0 ] && [ -n "$SF_OUT" ] && add_problem "sqlfluff:"$'\n'"$SF_OUT"
    fi
    ;;

  *)
    exit 0
    ;;
esac

# ---------------------------------------------------------------------------
# Результат
# ---------------------------------------------------------------------------

if [ -n "$PROBLEMS" ]; then
  {
    echo "Проверка $FILE выявила замечания:"
    echo ""
    printf '%s' "$PROBLEMS"
    echo "Исправь их перед тем, как продолжать."
  } >&2
  exit 2
fi

exit 0
