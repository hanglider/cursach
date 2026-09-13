# Зависимости стенда и инструментов разработки (macOS, Homebrew).
#
# Установка:  brew bundle install --file=Brewfile --no-upgrade
# Проверка:   brew bundle check --file=Brewfile
#
# Что сюда НЕ входит и ставится иначе:
#   - Python для registry/ — версию фиксирует uv (registry/.python-version)
#   - Go-инструменты (controller-gen, kubebuilder) — через go.mod и go run
#   - Chaos Mesh, Kyverno, Prometheus, Thanos, Grafana — helm-чарты в stand/

# ---- Контейнеры и кластеры ----
cask "orbstack"            # docker-рантайм; Docker Desktop не нужен
brew "kind"                # кластеры стенда: control + федерация
brew "kubernetes-cli"      # kubectl
brew "helm"

# ---- Языки ----
brew "go"                  # scheduler/, dr-controller/
brew "uv"                  # registry/: виртуальное окружение и версия Python

# ---- Нагрузка ----
brew "k6"                  # генератор нагрузки, experiments/load/

# ---- Линтеры: .claude/hooks/lint.sh и make lint ----
brew "golangci-lint"
brew "ruff"
brew "yamllint"
brew "kubeconform"         # схемы манифестов k8s без обращения к кластеру
brew "shellcheck"
brew "hadolint"
brew "sqlfluff"            # миграции каталога
brew "coreutils"           # gtimeout: таймауты линтеров в хуке

# ---- Утилиты ----
brew "jq"                  # разбор JSON в хуках и скриптах
brew "poppler"             # pdftoppm: чтение PDF-отчётов НИР из репозитория
