// Meta-scheduler федерации (spec §4.5, architecture §5, ADR 0027): один бинарь,
// режим задаётся флагом (ADR 0020).
//
//	--scheduler=meta   scoring по метрикам каталога (ADR 0005, 0006)
//	--scheduler=naive  round-robin по допустимым кластерам
//
// Все флаги читают значения по умолчанию из переменных окружения SCHEDULER_* и
// REGISTRY_*, чтобы Deployment переключал режим одной переменной.
package main

import (
	"flag"
	"fmt"
	"log/slog"
	"os"
	"time"

	"github.com/go-logr/logr"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	"github.com/hanglider/cursach/scheduler/api/v1alpha1"
	"github.com/hanglider/cursach/scheduler/internal/clusters"
	"github.com/hanglider/cursach/scheduler/internal/controller"
	"github.com/hanglider/cursach/scheduler/internal/registry"
	"github.com/hanglider/cursach/scheduler/internal/scoring"
)

func main() {
	var (
		cfg           controller.Config
		weights       string
		latencyBudget time.Duration
		registryURL   string
		registryToken string
		kubeconfigDir string
		crNamespace   string
		healthAddr    string
	)
	flag.StringVar(&cfg.Mode, "scheduler", envOr("SCHEDULER_MODE", controller.ModeMeta), "meta | naive (ADR 0020)")
	flag.StringVar(&weights, "weights", envOr("SCHEDULER_WEIGHTS", "0.5,0.3,0.2"),
		"веса error rate, latency, saturation (ADR 0006)")
	flag.DurationVar(&latencyBudget, "latency-budget", envDurationOr("SCHEDULER_LATENCY_BUDGET", 200*time.Millisecond),
		"latency budget для latency_score (ADR 0005)")
	flag.DurationVar(&cfg.Params.MetricsMaxAge, "metrics-max-age", envDurationOr("SCHEDULER_METRICS_MAX_AGE", 60*time.Second),
		"снимок метрик старше — как отсутствующий (ADR 0005)")
	flag.DurationVar(&cfg.RetryInterval, "retry-interval", envDurationOr("SCHEDULER_RETRY_INTERVAL", 30*time.Second),
		"повтор планирования при отсутствии кандидатов")
	flag.DurationVar(&cfg.FinalizeTimeout, "finalize-timeout", envDurationOr("SCHEDULER_FINALIZE_TIMEOUT", 2*time.Minute),
		"сколько ждать уборки объектов в недоступном кластере при удалении Workload")
	flag.StringVar(&registryURL, "registry-url", envOr("REGISTRY_URL", "http://registry.federation.svc:8000"), "адрес каталога")
	flag.StringVar(&registryToken, "registry-token", os.Getenv("REGISTRY_TOKEN"), "bearer-токен роли scheduler (ADR 0018)")
	flag.StringVar(&kubeconfigDir, "kubeconfig-dir", envOr("SCHEDULER_KUBECONFIG_DIR", "/etc/scheduler/kubeconfig"),
		"каталог <cluster-id>.kubeconfig кластеров федерации")
	flag.StringVar(&crNamespace, "namespace", envOr("SCHEDULER_NAMESPACE", "federation"), "namespace объектов Workload в control")
	flag.StringVar(&cfg.WorkloadNamespace, "workload-namespace", envOr("SCHEDULER_WORKLOAD_NAMESPACE", "workloads"),
		"namespace Deployment и Service в кластере федерации")
	flag.StringVar(&cfg.Image, "image", envOr("SCHEDULER_IMAGE", "federation/test-service:dev"), "образ frontend'а по умолчанию")
	flag.StringVar(&cfg.BackendURL, "backend-url", envOr("SCHEDULER_BACKEND_URL", "http://backend.workloads.svc:8080"),
		"адрес backend'а тестового сервиса внутри кластера")
	flag.StringVar(&cfg.Domain, "domain", envOr("SCHEDULER_DOMAIN", "fed.local"), "домен хостов workload'ов на шлюзе")
	flag.StringVar(&cfg.GatewayNamespace, "gateway-namespace", envOr("SCHEDULER_GATEWAY_NAMESPACE", "gateway"), "namespace Gateway API")
	flag.StringVar(&cfg.GatewayName, "gateway-name", envOr("SCHEDULER_GATEWAY_NAME", "stand"), "имя Gateway")
	flag.StringVar(&healthAddr, "health-addr", envOr("SCHEDULER_HEALTH_ADDR", ":8081"), "адрес /healthz и /readyz")
	flag.Parse()

	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	ctrl.SetLogger(logr.FromSlogHandler(logger.Handler()))

	if cfg.Mode != controller.ModeMeta && cfg.Mode != controller.ModeNaive {
		fatal(logger, "config", fmt.Errorf("--scheduler must be meta or naive, got %q", cfg.Mode))
	}
	w, err := scoring.ParseWeights(weights)
	if err != nil {
		fatal(logger, "config", err)
	}
	cfg.Params.Weights = w
	cfg.Params.LatencyBudgetMs = float64(latencyBudget) / float64(time.Millisecond)
	if registryToken == "" {
		fatal(logger, "config", fmt.Errorf("registry token is empty: set REGISTRY_TOKEN"))
	}

	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		fatal(logger, "scheme", err)
	}
	if err := v1alpha1.AddToScheme(scheme); err != nil {
		fatal(logger, "scheme", err)
	}

	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsserver.Options{BindAddress: "0"},
		HealthProbeBindAddress: healthAddr,
		LeaderElection:         false,
		Cache: cache.Options{
			DefaultNamespaces: map[string]cache.Config{crNamespace: {}},
		},
	})
	if err != nil {
		fatal(logger, "manager", err)
	}
	// Шлюзовые объекты — без кэша: не заводить информеры на HTTPRoute и Backend.
	gateway, err := client.New(mgr.GetConfig(), client.Options{Scheme: scheme})
	if err != nil {
		fatal(logger, "gateway client", err)
	}

	rec := &controller.Reconciler{
		Client:   mgr.GetClient(),
		Gateway:  gateway,
		Clusters: clusters.New(kubeconfigDir, scheme),
		Registry: registry.New(registryURL, registryToken),
		Events:   mgr.GetEventRecorder("scheduler"),
		Config:   cfg,
		RR:       &scoring.RoundRobin{},
		Now:      time.Now,
		Log:      logger,
	}
	if err := rec.SetupWithManager(mgr); err != nil {
		fatal(logger, "controller", err)
	}
	if err := mgr.AddHealthzCheck("ping", healthz.Ping); err != nil {
		fatal(logger, "healthz", err)
	}
	if err := mgr.AddReadyzCheck("ping", healthz.Ping); err != nil {
		fatal(logger, "readyz", err)
	}

	// Счётчик round-robin живёт в памяти: каждый старт — сброс (ADR 0020).
	logger.Info("started", "scheduler", cfg.Mode, "weights", cfg.Params.Weights,
		"latency_budget_ms", cfg.Params.LatencyBudgetMs, "metrics_max_age", cfg.Params.MetricsMaxAge.String(),
		"retry_interval", cfg.RetryInterval.String(), "namespace", crNamespace,
		"workload_namespace", cfg.WorkloadNamespace, "registry", registryURL, "round_robin", "reset")
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		fatal(logger, "run", err)
	}
}

func fatal(logger *slog.Logger, stage string, err error) {
	logger.Error(stage, "err", err.Error())
	os.Exit(2)
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
