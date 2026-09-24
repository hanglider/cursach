# Стенд и проверки. Подробности — stand/README.md.
SHELL := /bin/bash
.DEFAULT_GOAL := help

GO_MODULES := test-service scheduler dr-controller
PY_PROJECTS := experiments/load/workloadgen registry
CONTROLLER_GEN_VERSION := v0.22.0
# Схемы CRD (Kyverno ClusterPolicy) для kubeconform; встроенные схемы k8s — из default.
CRD_SCHEMAS := https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json

.PHONY: help stand-up stand-down stand-status image load policy-run scheduler-mode dr-mode dr-run dr-check sched-run sched-series trace-run generate lint test

help:  ## список целей
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

stand-up:  ## поднять стенд: 3 kind-кластера, образ, backend'ы, шлюз, дымовой тест
	stand/scripts/up.sh

stand-down:  ## снести стенд (только вручную)
	stand/scripts/down.sh

stand-status:  ## состояние стенда
	stand/scripts/status.sh

image:  ## пересобрать образы (тестовый сервис, каталог, планировщик, DR) и загрузить в кластеры
	stand/scripts/image.sh

load:  ## прогон нагрузки: make load RUN=<id> [DURATION=3m] [INTERVAL=30] [TTL=180]
	experiments/load/run.sh $(RUN) $(DURATION) $(INTERVAL) $(TTL)

policy-run:  ## сценарий 3, нарушение политики в обход планировщика: make policy-run RUN=<id> [ATTEMPTS=5]
	experiments/policy/run.sh $(RUN) $(ATTEMPTS)

scheduler-mode:  ## режим планировщика: make scheduler-mode [MODE=meta|naive]
	stand/scripts/scheduler-mode.sh $(MODE)

dr-mode:  ## режим DR Controller'а: make dr-mode [MODE=active|observe]
	stand/scripts/dr-mode.sh $(MODE)

dr-run:  ## сценарий 1, отказ кластера: make dr-run RUN=<id> [CLUSTER=cluster-b]
	experiments/dr/run.sh $(RUN) $(CLUSTER)

dr-check:  ## проверка DR на стенде: инвариант 6 и недостижимый app (ADR 0023 В)
	experiments/dr/check.sh $(CLUSTER)

sched-run:  ## сценарий 2, деградация сети (DR в observe): make sched-run RUN=<id> [CLUSTER=cluster-b]
	experiments/scheduler/run.sh $(RUN) $(CLUSTER)

sched-series:  ## серия сценария 2, meta и naive вперемешку: make sched-series PREFIX=<p> [REPEATS=3]
	experiments/scheduler/series.sh $(PREFIX) $(REPEATS) $(CLUSTER)

trace-run:  ## замер трассировки: make trace-run RUN=<id> [SAMPLING=10] (DURATION=3m, ADR 0029)
	experiments/traces/run.sh $(RUN) $(SAMPLING)

generate:  ## сгенерировать deepcopy и CRD планировщика (controller-gen)
	cd scheduler && go run sigs.k8s.io/controller-tools/cmd/controller-gen@$(CONTROLLER_GEN_VERSION) object paths=./api/...
	cd scheduler && go run sigs.k8s.io/controller-tools/cmd/controller-gen@$(CONTROLLER_GEN_VERSION) crd paths=./api/... output:crd:dir=../stand/scheduler

lint:  ## линт всего проекта
	@for m in $(GO_MODULES); do (cd $$m && test -z "$$(gofmt -l .)" && go vet ./... && golangci-lint run ./...) || exit 1; done
	@for p in $(PY_PROJECTS); do (cd $$p && uv run --quiet ruff check . && uv run --quiet ruff format --check .) || exit 1; done
	@ruff check experiments/dr experiments/traces experiments/scheduler && ruff format --check experiments/dr experiments/traces experiments/scheduler
	@yamllint -f parsable .yamllint stand experiments/load experiments/policy experiments/chaos
	@kubeconform -summary -ignore-missing-schemas -schema-location default \
	  -schema-location '$(CRD_SCHEMAS)' \
	  stand/federation/*.yaml stand/gateway/*.yaml stand/registry/*.yaml stand/policies/require-*.yaml \
	  stand/scheduler/*.yaml stand/dr-controller/*.yaml experiments/chaos/*.yaml \
	  stand/observability/control/jaeger.yaml
	@shellcheck -x -P SCRIPTDIR -f gcc stand/scripts/*.sh experiments/load/*.sh experiments/policy/*.sh experiments/dr/*.sh experiments/traces/*.sh experiments/scheduler/*.sh experiments/lib.sh
	@hadolint test-service/Dockerfile registry/Dockerfile scheduler/Dockerfile dr-controller/Dockerfile
	@sqlfluff lint registry/migrations

test:  ## тесты
	@for m in $(GO_MODULES); do (cd $$m && go test ./...) || exit 1; done
	@for p in $(PY_PROJECTS); do (cd $$p && uv run --quiet pytest -q) || exit 1; done
	@cd experiments/scheduler && uv run --no-project --quiet --python 3.13 --with pytest pytest -q -p no:cacheprovider
	@kyverno test stand/policies/tests >/dev/null 2>&1 || kyverno test stand/policies/tests --detailed-results
