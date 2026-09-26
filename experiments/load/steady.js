// Постоянная нагрузка на стенд (ADR 0008): RPS запросов в секунду, равномерно
// по живым workload'ам (ADR 0015). Список живых отдаёт генератор по HTTP.
// TARGET_HOST=app.fed.local — весь трафик в один хост, без генератора: сценарий 1,
// долгоживущий app (ADR 0011). BACKGROUND_RPS>0 — отдельный сценарий k6 с фоновым
// трафиком в BACKGROUND_HOST (по умолчанию app.fed.local): у каждого кластера есть SLI
// независимо от того, куда размещён поток (сценарий 2, ADR 0030).
//
//   k6 run experiments/load/steady.js
//   DURATION=3m RPS=50 GATEWAY_URL=http://127.0.0.1:8080 LIVE_URL=http://127.0.0.1:9797/live
//   SUMMARY_PATH=experiments/results/<run>/k6-summary.json TARGET_HOST=app.fed.local
//   BACKGROUND_RPS=5 BACKGROUND_HOST=app.fed.local
//
// Запрос в workload потока помечен тегом target_cluster — кластером, куда его
// разместили (из списка живых генератора): клиентский ряд раскладывается по кластерам
// без тега на каждый хост (ADR 0031).
import http from 'k6/http';
import { sleep } from 'k6';

const GATEWAY = __ENV.GATEWAY_URL || 'http://127.0.0.1:8080';
const LIVE = __ENV.LIVE_URL || 'http://127.0.0.1:9797/live';
const RPS = Number(__ENV.RPS || 50);
const TARGET_HOST = __ENV.TARGET_HOST || '';
const DURATION = __ENV.DURATION || '3m';
const BACKGROUND_RPS = Number(__ENV.BACKGROUND_RPS || 0);
const BACKGROUND_HOST = __ENV.BACKGROUND_HOST || 'app.fed.local';
// Должно быть заметно меньше --drain генератора: иначе VU с устаревшим списком
// шлёт запросы в уже удалённый маршрут и получает ложные ошибки.
const LIVE_REFRESH_MS = 2000;

// VU выделяются заранее с запасом на худший случай сценария 2: весь поток в кластер с
// деградировавшим backend'ом, ответ ~1 с (таймаут frontend → backend) — около RPS
// одновременных запросов. VU, инициализируемый на ходу, опаздывает к своей итерации, и
// k6 её пропускает (dropped_iterations — признак брака прогона, ADR 0031).
const scenarios = {
  steady: {
    executor: 'constant-arrival-rate',
    rate: RPS,
    timeUnit: '1s',
    duration: DURATION,
    preAllocatedVUs: Math.max(20, Math.ceil(RPS * 1.6)),
    maxVUs: 200,
  },
};
// Пороги только по трафику в workload'ы: запросы к списку живых и фон помечены
// другим тегом и в SLI не входят.
const thresholds = {
  // Порог на http_reqs нужен только чтобы k6 включил суб-метрику в сводку.
  'http_reqs{kind:workload}': ['count>0'],
  'http_req_failed{kind:workload}': ['rate<0.01'],
  'http_req_duration{kind:workload}': ['p(95)<200'],
};
if (BACKGROUND_RPS > 0) {
  scenarios.background = {
    executor: 'constant-arrival-rate',
    exec: 'background',
    rate: BACKGROUND_RPS,
    timeUnit: '1s',
    duration: DURATION,
    preAllocatedVUs: Math.max(5, BACKGROUND_RPS * 2),
    maxVUs: 50,
  };
  // Без порога суб-метрики фона не попадут в сводку; сам порог ни на что не влияет.
  thresholds['http_reqs{kind:background}'] = ['count>=0'];
  thresholds['http_req_failed{kind:background}'] = ['rate<=1'];
  thresholds['http_req_duration{kind:background}'] = ['p(95)>=0'];
}

export const options = {
  scenarios,
  thresholds,
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

// Состояние на VU: у каждого VU свой рантайм, поэтому кэш списка — на VU.
let hosts = [];
let clusters = {};
let fetchedAt = 0;

function liveHosts() {
  if (TARGET_HOST) {
    return [TARGET_HOST];
  }
  const now = Date.now();
  if (now - fetchedAt > LIVE_REFRESH_MS) {
    const r = http.get(LIVE, { tags: { kind: 'live-list' }, timeout: '2s' });
    if (r.status === 200) {
      try {
        const body = JSON.parse(r.body);
        hosts = body.hosts || [];
        clusters = body.clusters || {};
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
  const tags = { kind: 'workload' };
  if (clusters[host]) {
    tags.target_cluster = clusters[host];
  }
  const r = http.get(GATEWAY + '/', {
    headers: { Host: host },
    tags,
    timeout: '5s',
  });
  if (r.status !== 200) {
    // В k6.log прогона: когда и какой workload отвечал ошибкой.
    console.warn(`${new Date().toISOString()} ${host} status=${r.status} err=${r.error || ''}`);
  }
}

// Фон (ADR 0030): кластер выбирает шлюз по весам маршрута, поэтому target_cluster нет.
export function background() {
  http.get(GATEWAY + '/', {
    headers: { Host: BACKGROUND_HOST },
    tags: { kind: 'background' },
    timeout: '5s',
  });
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
  const bgReqs = pick('http_reqs{kind:background}');
  const bgFailed = pick('http_req_failed{kind:background}');
  const lines = [
    '',
    `workload requests: ${reqs.count || 0} (${(reqs.rate || 0).toFixed(1)}/s)`,
    `error rate:        ${((failed.rate || 0) * 100).toFixed(3)}% (${failed.passes || 0} failed)`,
    `latency p95/p99:   ${(dur['p(95)'] || 0).toFixed(1)} / ${(dur['p(99)'] || 0).toFixed(1)} ms`,
    `latency avg/max:   ${(dur.avg || 0).toFixed(1)} / ${(dur.max || 0).toFixed(1)} ms`,
    `dropped iterations: ${(pick('dropped_iterations').count || 0)}`,
    '',
  ];
  if (bgReqs.count) {
    lines.splice(
      lines.length - 1,
      0,
      `background:        ${bgReqs.count} requests, error rate ${((bgFailed.rate || 0) * 100).toFixed(3)}%`,
    );
  }
  for (const [name, t] of Object.entries(data.metrics)) {
    if (t.thresholds) {
      for (const [expr, res] of Object.entries(t.thresholds)) {
        lines.push(`${res.ok ? 'PASS' : 'FAIL'} ${name}: ${expr}`);
      }
    }
  }
  return lines.join('\n') + '\n';
}
