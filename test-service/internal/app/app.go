// Package app — HTTP-обработчики и метрики тестового сервиса.
package app

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// Роли сервиса.
const (
	RoleFrontend = "frontend"
	RoleBackend  = "backend"
)

// Config — параметры запуска.
type Config struct {
	Role           string
	Listen         string
	BackendURL     string
	BackendTimeout time.Duration
	Workload       string
}

// App — собранное приложение одной роли.
type App struct {
	cfg      Config
	metrics  *metrics
	registry *prometheus.Registry
	client   *http.Client
	hostname string
	logger   *slog.Logger
}

// New проверяет конфигурацию и регистрирует метрики.
func New(cfg Config, reg *prometheus.Registry, logger *slog.Logger) (*App, error) {
	if cfg.Role != RoleFrontend && cfg.Role != RoleBackend {
		return nil, fmt.Errorf("unknown role %q", cfg.Role)
	}
	if cfg.Role == RoleFrontend && cfg.BackendURL == "" {
		return nil, fmt.Errorf("frontend requires backend url")
	}
	if cfg.BackendTimeout <= 0 {
		cfg.BackendTimeout = time.Second
	}
	if logger == nil {
		logger = slog.Default()
	}
	hostname, _ := os.Hostname()

	m, err := newMetrics(reg, cfg.Role)
	if err != nil {
		return nil, err
	}

	return &App{
		cfg:      cfg,
		metrics:  m,
		registry: reg,
		client: &http.Client{
			// Таймаут задаётся через контекст запроса, чтобы был виден в 502.
			Transport: &http.Transport{
				MaxIdleConns:        100,
				MaxIdleConnsPerHost: 100,
				IdleConnTimeout:     90 * time.Second,
			},
		},
		hostname: hostname,
		logger:   logger,
	}, nil
}

// Handler собирает маршруты роли. Пробы и /metrics не инструментируются,
// чтобы не попадать в SLI.
func (a *App) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", a.handleHealthz)
	mux.HandleFunc("GET /readyz", a.handleReadyz)
	mux.Handle("GET /metrics", promhttp.HandlerFor(a.registry, promhttp.HandlerOpts{}))

	switch a.cfg.Role {
	case RoleBackend:
		mux.Handle("GET /work", a.metrics.instrument("/work", http.HandlerFunc(a.handleWork)))
	case RoleFrontend:
		mux.Handle("GET /{$}", a.metrics.instrument("/", http.HandlerFunc(a.handleFrontend)))
	}
	return mux
}

func (a *App) handleHealthz(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// handleReadyz: backend готов всегда; frontend — только когда достучался до
// backend'а. Так workload не получает трафик раньше, чем сможет его обслужить.
func (a *App) handleReadyz(w http.ResponseWriter, r *http.Request) {
	if a.cfg.Role == RoleBackend {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ready"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), a.cfg.BackendTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, a.cfg.BackendURL+"/healthz", nil)
	if err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"status": "backend url invalid"})
		return
	}
	resp, err := a.client.Do(req)
	if err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"status": "backend unreachable", "detail": err.Error()})
		return
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"status": fmt.Sprintf("backend healthz %d", resp.StatusCode)})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ready"})
}

type workResponse struct {
	Tier     string    `json:"tier"`
	Hostname string    `json:"hostname"`
	Time     time.Time `json:"time"`
}

func (a *App) handleWork(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, workResponse{Tier: RoleBackend, Hostname: a.hostname, Time: time.Now().UTC()})
}

type frontendResponse struct {
	Tier          string          `json:"tier"`
	Workload      string          `json:"workload,omitempty"`
	Hostname      string          `json:"hostname"`
	BackendMillis float64         `json:"backend_ms"`
	Backend       json.RawMessage `json:"backend,omitempty"`
	Error         string          `json:"error,omitempty"`
}

// handleFrontend вызывает backend и возвращает 502 при любой его ошибке или
// таймауте. Именно так отказ, внесённый в backend, попадает в метрики frontend'а.
func (a *App) handleFrontend(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), a.cfg.BackendTimeout)
	defer cancel()

	start := time.Now()
	fail := func(detail string) {
		a.logger.Warn("backend call failed", "detail", detail)
		writeJSON(w, http.StatusBadGateway, frontendResponse{
			Tier: RoleFrontend, Workload: a.cfg.Workload, Hostname: a.hostname,
			BackendMillis: millisSince(start), Error: detail,
		})
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, a.cfg.BackendURL+"/work", nil)
	if err != nil {
		fail("backend url invalid: " + err.Error())
		return
	}
	resp, err := a.client.Do(req)
	if err != nil {
		fail("backend unreachable: " + err.Error())
		return
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
	if err != nil {
		fail("backend body: " + err.Error())
		return
	}
	if resp.StatusCode != http.StatusOK {
		fail(fmt.Sprintf("backend status %d", resp.StatusCode))
		return
	}
	if !json.Valid(body) {
		fail("backend returned non-JSON")
		return
	}
	writeJSON(w, http.StatusOK, frontendResponse{
		Tier: RoleFrontend, Workload: a.cfg.Workload, Hostname: a.hostname,
		BackendMillis: millisSince(start), Backend: body,
	})
}

func millisSince(t time.Time) float64 {
	return float64(time.Since(t).Microseconds()) / 1000
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
