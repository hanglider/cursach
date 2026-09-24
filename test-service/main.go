// Тестовый сервис стенда (ADR 0014): один бинарь, роль задаётся флагом.
// Трассировка включается переменной OTEL_EXPORTER_OTLP_ENDPOINT (ADR 0029).
//
//	--role=backend   отвечает на GET /work
//	--role=frontend  на GET / вызывает backend и отражает его ошибки и
//	                 задержки в собственных метриках (ADR 0016)
package main

import (
	"context"
	"errors"
	"flag"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/hanglider/cursach/test-service/internal/app"
)

func main() {
	var cfg app.Config
	flag.StringVar(&cfg.Role, "role", envOr("ROLE", app.RoleFrontend), "frontend | backend")
	flag.StringVar(&cfg.Listen, "listen", envOr("LISTEN", ":8080"), "адрес прослушивания")
	flag.StringVar(&cfg.BackendURL, "backend-url", envOr("BACKEND_URL", "http://backend:8080"),
		"адрес backend'а; только для frontend")
	flag.DurationVar(&cfg.BackendTimeout, "backend-timeout", envDurationOr("BACKEND_TIMEOUT", time.Second),
		"таймаут вызова backend'а; по истечении frontend отвечает 502")
	flag.StringVar(&cfg.Workload, "workload", envOr("WORKLOAD", ""), "имя workload'а, только для тела ответа")
	flag.Parse()

	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))

	tracing, err := app.NewTracing(context.Background(), cfg.Role, cfg.Workload)
	if err != nil {
		logger.Error("tracing", "err", err)
		os.Exit(2)
	}
	cfg.Tracing = tracing

	a, err := app.New(cfg, prometheus.NewRegistry(), logger)
	if err != nil {
		logger.Error("config", "err", err)
		os.Exit(2)
	}

	srv := &http.Server{
		Addr:              cfg.Listen,
		Handler:           a.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		logger.Info("listening", "role", cfg.Role, "addr", cfg.Listen, "tracing", tracing.Enabled(),
			"otlp_endpoint", os.Getenv(app.EnvOTLPEndpoint))
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("serve", "err", err)
			os.Exit(1)
		}
	}()

	<-ctx.Done()
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		logger.Error("shutdown", "err", err)
	}
	if err := tracing.Shutdown(shutdownCtx); err != nil {
		logger.Error("tracing shutdown", "err", err)
	}
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envDurationOr(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}
