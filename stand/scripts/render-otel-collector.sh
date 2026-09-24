#!/usr/bin/env bash
# OTel Collector кластера федерации (ADR 0029): подставляет id кластера и адрес OTLP gRPC
# Jaeger в control — IP узла control и NodePort 30417 (ADR 0021, 0022). Рендерится при
# каждом stand-up: IP узлов после перезапуска docker могут смениться.
#   render-otel-collector.sh <cluster-id>  -> YAML в stdout
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need docker
id=${1:?cluster-id}
for c in "${FEDERATION[@]}"; do [ "$c" = "$id" ] && found=1; done
[ -n "${found:-}" ] || die "кластер $id не описан в clusters.yaml"
ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$CONTROL-control-plane")
[ -n "$ip" ] || die "нет IP у $CONTROL-control-plane"
rendered=$(sed -e "s/__CLUSTER_ID__/$id/g" -e "s/__JAEGER_OTLP__/$ip:$JAEGER_OTLP_NODEPORT/g" \
  "$STAND/observability/federation/otel-collector.tmpl.yaml")
hash=$(printf '%s' "$rendered" | shasum | cut -c1-12)
printf '%s\n' "$rendered" | sed "s/__CONFIG_HASH__/$hash/"
