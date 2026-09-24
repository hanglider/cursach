// DR Controller федерации (spec §4.6, architecture §6, ADR 0007, 0028): раз в 10 с
// опрашивает кластеры федерации, ведёт автомат HEALTHY → DEGRADED → DOWN → RECOVERING,
// переключает трафик app.fed.local весами маршрута на шлюзе, помечает down в каталоге и
// пишет инциденты с временами фаз.
//
//	--mode=active   переключает трафик и пишет down в каталог
//	--mode=observe  только наблюдает и пишет инциденты: сценарий 1 «без DR»
//
// Флаги читают значения по умолчанию из переменных окружения DR_* и REGISTRY_*.
package main

import (
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/go-logr/logr"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/manager"
	ctrlmetrics "sigs.k8s.io/controller-runtime/pkg/metrics"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	"github.com/hanglider/cursach/dr-controller/internal/fsm"
	"github.com/hanglider/cursach/dr-controller/internal/gateway"
	"github.com/hanglider/cursach/dr-controller/internal/monitor"
	"github.com/hanglider/cursach/dr-controller/internal/probe"
	"github.com/hanglider/cursach/dr-controller/internal/registry"
	"github.com/hanglider/cursach/dr-controller/internal/thanos"
)

func main() {
	var (
		cfg            monitor.Config
		steps          string
		probeTimeout   time.Duration
		registryURL    string
		registryToken  string
		thanosURL      string
		kubeconfigDir  string
		routeNamespace string
		routeName      string
		workload       string
		metricsAddr    string
		healthAddr     string
	)
	defaults := fsm.Defaults()
	flag.StringVar(&cfg.Mode, "mode", envOr("DR_MODE", monitor.ModeActive), "active | observe (ADR 0028)")
	flag.DurationVar(&cfg.Interval, "interval", envDuration("DR_INTERVAL", 10*time.Second), "период опроса (ADR 0007)")
	flag.DurationVar(&probeTimeout, "probe-timeout", envDuration("DR_PROBE_TIMEOUT", 2*time.Second), "таймаут пробы (ADR 0007)")
	flag.Float64Var(&cfg.ErrorRateThreshold, "error-rate-threshold", envFloat("DR_ERROR_RATE_THRESHOLD", 0.05),
		"sli_ok: error rate за 1 мин не выше (ADR 0007)")
	flag.Int64Var(&cfg.NominalWeight, "nominal-weight", int64(envFloat("DR_NOMINAL_WEIGHT", 50)),
		"номинальный вес кластера на маршруте app (ADR 0011)")
	flag.IntVar(&cfg.SmokeRequests, "smoke-requests", int(envFloat("DR_SMOKE_REQUESTS", 10)), "запросов в smoke-тесте (ADR 0007)")
	flag.IntVar(&cfg.FSM.DownCycles, "down-cycles", int(envFloat("DR_DOWN_CYCLES", float64(defaults.DownCycles))),
		"циклов подряд с !svc_ok в DEGRADED до DOWN (ADR 0007, 0028)")
	flag.IntVar(&cfg.FSM.HealthyCycles, "healthy-cycles", int(envFloat("DR_HEALTHY_CYCLES", float64(defaults.HealthyCycles))),
		"чистых циклов для DEGRADED → HEALTHY")
	flag.IntVar(&cfg.FSM.StepCycles, "step-cycles", int(envFloat("DR_STEP_CYCLES", float64(defaults.StepCycles))),
		"чистых циклов на ступень веса в RECOVERING")
	flag.StringVar(&steps, "steps", envOr("DR_STEPS", "0.1,0.5,1"), "ступени веса при возврате трафика, доли номинала")
	flag.StringVar(&registryURL, "registry-url", envOr("REGISTRY_URL", "http://registry.federation.svc:8000"), "адрес каталога")
	flag.StringVar(&registryToken, "registry-token", os.Getenv("REGISTRY_TOKEN"), "bearer-токен роли dr-controller (ADR 0018)")
	flag.StringVar(&thanosURL, "thanos-url", envOr("DR_THANOS_URL", "http://thanos-query.observability.svc:10902"), "Thanos Querier")
	flag.StringVar(&kubeconfigDir, "kubeconfig-dir", envOr("DR_KUBECONFIG_DIR", "/etc/dr/kubeconfig"),
		"каталог <cluster-id>.kubeconfig: CA API-серверов для api_ok")
	flag.StringVar(&routeNamespace, "route-namespace", envOr("DR_ROUTE_NAMESPACE", "gateway"), "namespace маршрута app")
	flag.StringVar(&routeName, "route-name", envOr("DR_ROUTE_NAME", "app"), "HTTPRoute долгоживущего сервиса (ADR 0011)")
	flag.StringVar(&workload, "workload", envOr("DR_WORKLOAD", "app"), "метка federation.spbu/workload Backend'ов маршрута")
	flag.StringVar(&metricsAddr, "metrics-addr", envOr("DR_METRICS_ADDR", ":8080"), "адрес /metrics и /status")
	flag.StringVar(&healthAddr, "health-addr", envOr("DR_HEALTH_ADDR", ":8081"), "адрес /healthz и /readyz")
	flag.Parse()

	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	ctrl.SetLogger(logr.FromSlogHandler(logger.Handler()))

	if cfg.Mode != monitor.ModeActive && cfg.Mode != monitor.ModeObserve {
		fatal(logger, "config", fmt.Errorf("--mode must be active or observe, got %q", cfg.Mode))
	}
	var err error
	if cfg.FSM.Steps, err = parseSteps(steps); err != nil {
		fatal(logger, "config", err)
	}
	if err := cfg.FSM.Validate(); err != nil {
		fatal(logger, "config", err)
	}
	if registryToken == "" {
		fatal(logger, "config", fmt.Errorf("registry token is empty: set REGISTRY_TOKEN"))
	}

	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		fatal(logger, "scheme", err)
	}
	restCfg := ctrl.GetConfigOrDie()
	restCfg.Timeout = 5 * time.Second

	metrics := monitor.NewMetrics(ctrlmetrics.Registry)
	route := &gateway.Route{Namespace: routeNamespace, Name: routeName, Workload: workload}
	mon := monitor.New(
		registry.New(registryURL, registryToken, 5*time.Second),
		route,
		probe.New(probeTimeout, kubeconfigDir),
		thanos.New(thanosURL, 3*time.Second),
		cfg, metrics, logger,
	)

	mgr, err := ctrl.NewManager(restCfg, ctrl.Options{
		Scheme: scheme,
		Metrics: metricsserver.Options{
			BindAddress:   metricsAddr,
			ExtraHandlers: map[string]http.Handler{"/status": mon.StatusHandler()},
		},
		HealthProbeBindAddress: healthAddr,
		LeaderElection:         false,
		Cache:                  cache.Options{DefaultNamespaces: map[string]cache.Config{routeNamespace: {}}},
	})
	if err != nil {
		fatal(logger, "manager", err)
	}
	// Маршрут и Backend'ы — без кэша: читаем то, что видит шлюз, в момент решения.
	if route.Client, err = client.New(mgr.GetConfig(), client.Options{Scheme: scheme}); err != nil {
		fatal(logger, "gateway client", err)
	}
	if err := mgr.Add(manager.RunnableFunc(mon.Run)); err != nil {
		fatal(logger, "monitor", err)
	}
	if err := mgr.AddHealthzCheck("loop", mon.Healthy); err != nil {
		fatal(logger, "healthz", err)
	}
	if err := mgr.AddReadyzCheck("cycle", mon.Ready); err != nil {
		fatal(logger, "readyz", err)
	}

	logger.Info("started", "mode", cfg.Mode, "interval", cfg.Interval.String(), "probe_timeout", probeTimeout.String(),
		"error_rate_threshold", cfg.ErrorRateThreshold, "nominal_weight", cfg.NominalWeight,
		"smoke_requests", cfg.SmokeRequests, "fsm", cfg.FSM, "registry", registryURL, "thanos", thanosURL,
		"route", routeNamespace+"/"+routeName)
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		fatal(logger, "run", err)
	}
}

func parseSteps(s string) ([]float64, error) {
	var out []float64
	for _, part := range strings.Split(s, ",") {
		v, err := strconv.ParseFloat(strings.TrimSpace(part), 64)
		if err != nil {
			return nil, fmt.Errorf("--steps: %w", err)
		}
		out = append(out, v)
	}
	return out, nil
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

func envDuration(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}

func envFloat(key string, def float64) float64 {
	if v := os.Getenv(key); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			return f
		}
	}
	return def
}
