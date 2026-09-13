// Постоянная нагрузка на стенд (ADR 0008): RPS запросов в секунду, равномерно
// по живым workload'ам (ADR 0015). Список живых отдаёт генератор по HTTP.
//
//   k6 run experiments/load/steady.js
//   DURATION=3m RPS=50 GATEWAY_URL=http://127.0.0.1:8080 LIVE_URL=http://127.0.0.1:9797/live
//   SUMMARY_PATH=experiments/results/<run>/k6-summary.json
import http from 'k6/http';
import { sleep } from 'k6';

const GATEWAY = __ENV.GATEWAY_URL || 'http://127.0.0.1:8080';
const LIVE = __ENV.LIVE_URL || 'http://127.0.0.1:9797/live';
const RPS = Number(__ENV.RPS || 50);
const DURATION = __ENV.DURATION || '3m';
// Должно быть заметно меньше --drain генератора: иначе VU с устаревшим списком
// шлёт запросы в уже удалённый маршрут и получает ложные ошибки.
const LIVE_REFRESH_MS = 2000;

export const options = {
  scenarios: {
    steady: {
      executor: 'constant-arrival-rate',
      rate: RPS,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: 20,
      maxVUs: 200,
    },
  },
  // Пороги только по трафику в workload'ы: запросы к списку живых помечены
  // другим тегом и в SLI не входят.
  thresholds: {
    // Порог на http_reqs нужен только чтобы k6 включил суб-метрику в сводку.
    'http_reqs{kind:workload}': ['count>0'],
    'http_req_failed{kind:workload}': ['rate<0.01'],
    'http_req_duration{kind:workload}': ['p(95)<200'],
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

// Состояние на VU: у каждого VU свой рантайм, поэтому кэш списка — на VU.
let hosts = [];
let fetchedAt = 0;

function liveHosts() {
  const now = Date.now();
  if (now - fetchedAt > LIVE_REFRESH_MS) {
    const r = http.get(LIVE, { tags: { kind: 'live-list' }, timeout: '2s' });
    if (r.status === 200) {
      try {
        hosts = JSON.parse(r.body).hosts || [];
      } catch (e) {
        // список не разобрался — оставляем прежний
      }
    }
    fetchedAt = now;
  }
  return hosts;
}

export default function () {
  const h = liveHosts();
  if (h.length === 0) {
    sleep(1);
    return;
  }
  const host = h[Math.floor(Math.random() * h.length)];
  const r = http.get(GATEWAY + '/', {
    headers: { Host: host },
    tags: { kind: 'workload' },
    timeout: '5s',
  });
  if (r.status !== 200) {
    // В k6.log прогона: когда и какой workload отвечал ошибкой.
    console.warn(`${new Date().toISOString()} ${host} status=${r.status} err=${r.error || ''}`);
  }
}

export function handleSummary(data) {
  const out = { stdout: textSummary(data) };
  if (__ENV.SUMMARY_PATH) {
    out[__ENV.SUMMARY_PATH] = JSON.stringify(data, null, 2);
  }
  return out;
}

// Короткая текстовая сводка без внешних jslib: только то, что важно для SLO.
function textSummary(data) {
  const m = data.metrics;
  const pick = (name) => (m[name] ? m[name].values : {});
  const failed = pick('http_req_failed{kind:workload}');
  const dur = pick('http_req_duration{kind:workload}');
  const reqs = pick('http_reqs{kind:workload}');
  const lines = [
    '',
    `workload requests: ${reqs.count || 0} (${(reqs.rate || 0).toFixed(1)}/s)`,
    `error rate:        ${((failed.rate || 0) * 100).toFixed(3)}% (${failed.passes || 0} failed)`,
    `latency p95/p99:   ${(dur['p(95)'] || 0).toFixed(1)} / ${(dur['p(99)'] || 0).toFixed(1)} ms`,
    `latency avg/max:   ${(dur.avg || 0).toFixed(1)} / ${(dur.max || 0).toFixed(1)} ms`,
    `dropped iterations: ${(pick('dropped_iterations').count || 0)}`,
    '',
  ];
  for (const [name, t] of Object.entries(data.metrics)) {
    if (t.thresholds) {
      for (const [expr, res] of Object.entries(t.thresholds)) {
        lines.push(`${res.ok ? 'PASS' : 'FAIL'} ${name}: ${expr}`);
      }
    }
  }
  return lines.join('\n') + '\n';
}
