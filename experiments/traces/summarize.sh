#!/usr/bin/env bash
# Сводка прогона замера трассировки (ADR 0029): клиентская задержка k6 (цена трассировки)
# и разложение по звеньям из трейсов внутри прогона (её польза).
#
#   experiments/traces/summarize.sh <каталог прогона>   -> <каталог прогона>/summary.json
#
# Вход: meta.json, k6-summary.json, traces/summary.json. Пересобирается без стенда.
set -euo pipefail

OUT=${1:?каталог прогона}
jq -n --slurpfile meta "$OUT/meta.json" --slurpfile k6 "$OUT/k6-summary.json" \
  --slurpfile tr "$OUT/traces/summary.json" '
  ($k6[0].metrics) as $m
  | ($m["http_req_duration{kind:workload}"].values) as $d
  | ($m["http_reqs{kind:workload}"].values.count) as $n
  | {run_id:$meta[0].run_id, gateway_sampling_percent:$meta[0].tracing.gateway_sampling_percent,
     client:{requests:$n, failed_rate:$m["http_req_failed{kind:workload}"].values.rate,
             latency_ms:{avg:$d.avg, p50:$d.med, p90:$d["p(90)"], p95:$d["p(95)"], p99:$d["p(99)"], max:$d.max}},
     traces:{count:$tr[0].all.traces, complete:$tr[0].all.complete, exported:$tr[0].traces_exported,
             sampled_share:(if $n > 0 then ($tr[0].all.traces / $n) else null end)},
     decomposition_ms:($tr[0].all | {total, gateway, hop, frontend, backend_net, backend}),
     by_cluster:($tr[0].by_cluster | map_values({traces, total: .total.p50, gateway: .gateway.p50, hop: .hop.p50,
                                                 frontend: .frontend.p50, backend_net: .backend_net.p50,
                                                 backend: .backend.p50}))}' >"$OUT/summary.json"
