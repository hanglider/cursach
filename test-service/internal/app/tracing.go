package app

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
	"go.opentelemetry.io/otel/trace/noop"
)

// Трассировка (ADR 0014, 0029): otelhttp на обработчиках нагрузки и на клиенте
// frontend → backend, экспорт OTLP/HTTP в коллектор своего кластера.
//
// Решение о сэмплинге принимает только шлюз (корень трейса): сервис продолжает
// трейс, пришедший с сэмплированным traceparent, и никогда не начинает свой.
// Поэтому пробы Kubernetes, smoke-тест DR и прямые запросы мимо шлюза не трассируются.

// EnvOTLPEndpoint — стандартная переменная OTel SDK; пустая — трассировка выключена.
const EnvOTLPEndpoint = "OTEL_EXPORTER_OTLP_ENDPOINT"

// Атрибуты ресурса. Кластер ставит коллектор (federation.cluster), как Prometheus —
// external label: сервис не знает, в каком кластере работает.
const (
	AttrWorkload = "federation.workload"
	AttrTier     = "federation.tier"
)

// Tracing — провайдер трейсов и пропагатор; нулевое значение — выключено.
type Tracing struct {
	Provider   trace.TracerProvider
	Propagator propagation.TextMapPropagator
	shutdown   func(context.Context) error
}

// Shutdown выгружает накопленные спаны.
func (t Tracing) Shutdown(ctx context.Context) error {
	if t.shutdown == nil {
		return nil
	}
	return t.shutdown(ctx)
}

// Enabled — трейсы экспортируются.
func (t Tracing) Enabled() bool { return t.shutdown != nil }

func (t Tracing) withDefaults() Tracing {
	if t.Provider == nil {
		t.Provider = noop.NewTracerProvider()
	}
	if t.Propagator == nil {
		t.Propagator = propagation.TraceContext{}
	}
	return t
}

// NewSampler — только продолжение сэмплированных трейсов шлюза.
func NewSampler() sdktrace.Sampler { return sdktrace.ParentBased(sdktrace.NeverSample()) }

// NewTracing — OTLP/HTTP-экспорт по OTEL_EXPORTER_OTLP_ENDPOINT; без переменной —
// выключенная трассировка (Tracing{}), как требует ADR 0014.
func NewTracing(ctx context.Context, role, workload string) (Tracing, error) {
	if os.Getenv(EnvOTLPEndpoint) == "" {
		return Tracing{}, nil
	}
	exp, err := otlptracehttp.New(ctx)
	if err != nil {
		return Tracing{}, fmt.Errorf("otlp exporter: %w", err)
	}
	hostname, _ := os.Hostname()
	attrs := []attribute.KeyValue{
		attribute.String("service.name", role),
		attribute.String("service.instance.id", hostname),
		attribute.String(AttrTier, role),
	}
	if workload != "" {
		attrs = append(attrs, attribute.String(AttrWorkload, workload))
	}
	tp := sdktrace.NewTracerProvider(
		sdktrace.WithSampler(NewSampler()),
		sdktrace.WithResource(resource.NewSchemaless(attrs...)),
		sdktrace.WithBatcher(exp, sdktrace.WithBatchTimeout(2*time.Second)),
	)
	return Tracing{Provider: tp, Propagator: propagation.TraceContext{}, shutdown: tp.Shutdown}, nil
}

// traceHandler — серверный спан с именем маршрута («GET /», «GET /work»).
func (a *App) traceHandler(route string, next http.Handler) http.Handler {
	name := "GET " + route
	return otelhttp.NewHandler(next, name,
		otelhttp.WithTracerProvider(a.tracing.Provider),
		otelhttp.WithPropagators(a.tracing.Propagator),
		// Без форматтера otelhttp берёт шаблон ServeMux («GET /{$}»).
		otelhttp.WithSpanNameFormatter(func(string, *http.Request) string { return name }),
	)
}

// traceTransport — клиентский спан вызова backend'а и traceparent в его заголовках.
func (a *App) traceTransport(base http.RoundTripper) http.RoundTripper {
	return otelhttp.NewTransport(base,
		otelhttp.WithTracerProvider(a.tracing.Provider),
		otelhttp.WithPropagators(a.tracing.Propagator),
		otelhttp.WithSpanNameFormatter(func(_ string, r *http.Request) string {
			return "backend " + r.URL.Path
		}),
	)
}
