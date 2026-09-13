# Стенд и проверки. Подробности — stand/README.md.
SHELL := /bin/bash
.DEFAULT_GOAL := help

GO_MODULES := test-service
PY_PROJECTS := experiments/load/workloadgen registry

.PHONY: help stand-up stand-down stand-status image load lint test

help:  ## список целей
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

stand-up:  ## поднять стенд: 3 kind-кластера, образ, backend'ы, шлюз, дымовой тест
	stand/scripts/up.sh

stand-down:  ## снести стенд (только вручную)
	stand/scripts/down.sh

stand-status:  ## состояние стенда
	stand/scripts/status.sh

image:  ## пересобрать образы тестового сервиса и каталога и загрузить в кластеры
	stand/scripts/image.sh

load:  ## прогон нагрузки: make load RUN=<id> [DURATION=3m] [INTERVAL=30] [TTL=180]
	experiments/load/run.sh $(RUN) $(DURATION) $(INTERVAL) $(TTL)

lint:  ## линт всего проекта
	@for m in $(GO_MODULES); do (cd $$m && test -z "$$(gofmt -l .)" && go vet ./... && golangci-lint run ./...) || exit 1; done
	@for p in $(PY_PROJECTS); do (cd $$p && uv run --quiet ruff check . && uv run --quiet ruff format --check .) || exit 1; done
	@yamllint -f parsable .yamllint stand experiments/load
	@kubeconform -summary -ignore-missing-schemas stand/federation/*.yaml stand/gateway/*.yaml stand/registry/*.yaml
	@shellcheck -x -P SCRIPTDIR -f gcc stand/scripts/*.sh experiments/load/*.sh
	@hadolint test-service/Dockerfile registry/Dockerfile
	@sqlfluff lint registry/migrations

test:  ## тесты
	@for m in $(GO_MODULES); do (cd $$m && go test ./...) || exit 1; done
	@for p in $(PY_PROJECTS); do (cd $$p && uv run --quiet pytest -q) || exit 1; done
