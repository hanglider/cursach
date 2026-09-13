package app

import (
	"net/http"
	"strconv"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
)

// Контракт метрик — architecture §2.3. Лейбл tier — константный на процесс,
// cluster и workload добавляет слой сбора (external_labels, relabeling).
type metrics struct {
	requests *prometheus.CounterVec
	duration *prometheus.HistogramVec
}

// Границы включают 0.2 с — бюджет p95 из spec §1.3.
var durationBuckets = []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 0.75, 1, 2.5, 5}

func newMetrics(reg *prometheus.Registry, tier string) (*metrics, error) {
	m := &metrics{
		requests: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name:        "http_requests_total",
			Help:        "HTTP requests by route, method and status.",
			ConstLabels: prometheus.Labels{"tier": tier},
		}, []string{"route", "method", "status"}),
		duration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name:        "http_request_duration_seconds",
			Help:        "HTTP request duration in seconds.",
			ConstLabels: prometheus.Labels{"tier": tier},
			Buckets:     durationBuckets,
		}, []string{"route"}),
	}
	for _, c := range []prometheus.Collector{
		m.requests, m.duration,
		collectors.NewGoCollector(),
		collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}),
	} {
		if err := reg.Register(c); err != nil {
			return nil, err
		}
	}
	return m, nil
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (s *statusWriter) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}

func (m *metrics) instrument(route string, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sw := &statusWriter{ResponseWriter: w, status: http.StatusOK}
		start := time.Now()
		next.ServeHTTP(sw, r)
		m.duration.WithLabelValues(route).Observe(time.Since(start).Seconds())
		m.requests.WithLabelValues(route, r.Method, strconv.Itoa(sw.status)).Inc()
	})
}
