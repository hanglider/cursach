package app

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

// recorded — провайдер с тем же сэмплером, что в NewTracing, и запись спанов в память.
func recorded() (Tracing, *tracetest.SpanRecorder) {
	rec := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSampler(NewSampler()), sdktrace.WithSpanProcessor(rec))
	return Tracing{Provider: tp, Propagator: propagation.TraceContext{}}, rec
}

func sampledParent() (string, trace.TraceID, trace.SpanID) {
	tid := trace.TraceID{0x0a, 0xf7, 0x65, 0x19, 0x16, 0xcd, 0x43, 0xdd, 0x84, 0x48, 0xeb, 0x21, 0x1c, 0x80, 0x31, 0x9c}
	sid := trace.SpanID{0xb7, 0xad, 0x6b, 0x71, 0x69, 0x20, 0x33, 0x31}
	return "00-" + tid.String() + "-" + sid.String() + "-01", tid, sid
}

func getWith(t *testing.T, h http.Handler, path, traceparent string) int {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, path, nil)
	if traceparent != "" {
		req.Header.Set("traceparent", traceparent)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec.Code
}

// Трейс шлюза продолжается через frontend и backend одной цепочкой:
// шлюз → GET / (frontend, server) → backend /work (frontend, client) → GET /work (backend, server).
func TestTraceContinuesGatewayTrace(t *testing.T) {
	beTracing, beRec := recorded()
	be, _ := newApp(t, Config{Role: RoleBackend, Tracing: beTracing})
	backend := httptest.NewServer(be.Handler())
	defer backend.Close()

	feTracing, feRec := recorded()
	fe, _ := newApp(t, Config{Role: RoleFrontend, BackendURL: backend.URL, BackendTimeout: time.Second, Tracing: feTracing})
	parent, tid, gatewaySpan := sampledParent()
	if code := getWith(t, fe.Handler(), "/", parent); code != http.StatusOK {
		t.Fatalf("status %d", code)
	}

	fs := feRec.Ended()
	bs := beRec.Ended()
	if len(fs) != 2 || len(bs) != 1 {
		t.Fatalf("spans: frontend %d, backend %d", len(fs), len(bs))
	}
	byName := map[string]sdktrace.ReadOnlySpan{}
	for _, s := range append(fs, bs...) {
		if s.SpanContext().TraceID() != tid {
			t.Fatalf("span %q in another trace", s.Name())
		}
		byName[s.Name()] = s
	}
	server, client, work := byName["GET /"], byName["backend /work"], byName["GET /work"]
	if server == nil || client == nil || work == nil {
		t.Fatalf("span names: %v", byName)
	}
	if server.Parent().SpanID() != gatewaySpan || server.SpanKind() != trace.SpanKindServer {
		t.Fatal("frontend server span must be a child of the gateway span")
	}
	if client.Parent().SpanID() != server.SpanContext().SpanID() || client.SpanKind() != trace.SpanKindClient {
		t.Fatal("backend call must be a child client span of the frontend span")
	}
	if work.Parent().SpanID() != client.SpanContext().SpanID() || !work.Parent().IsRemote() {
		t.Fatal("backend server span must continue the propagated client span")
	}
}

// Без сэмплированного traceparent (DR smoke, прямой запрос мимо шлюза) и на пробах
// спанов нет: сервис не начинает трейс сам.
func TestNoTraceWithoutSampledParent(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"tier":"backend"}`))
	}))
	defer backend.Close()
	tr, rec := recorded()
	fe, _ := newApp(t, Config{Role: RoleFrontend, BackendURL: backend.URL, BackendTimeout: time.Second, Tracing: tr})

	parent, _, _ := sampledParent()
	unsampled := parent[:len(parent)-2] + "00"
	for _, tc := range []struct{ path, parent string }{
		{"/", ""}, {"/", unsampled}, {"/readyz", ""}, {"/healthz", parent}, {"/metrics", parent},
	} {
		if code := getWith(t, fe.Handler(), tc.path, tc.parent); code != http.StatusOK {
			t.Fatalf("%s: status %d", tc.path, code)
		}
	}
	if n := len(rec.Ended()); n != 0 {
		t.Fatalf("expected no spans, got %d", n)
	}
}

// Без OTEL_EXPORTER_OTLP_ENDPOINT трассировка выключена, приложение работает.
func TestTracingDisabledWithoutEndpoint(t *testing.T) {
	t.Setenv(EnvOTLPEndpoint, "")
	tr, err := NewTracing(context.Background(), RoleBackend, "")
	if err != nil || tr.Enabled() {
		t.Fatalf("tracing must be off: %+v %v", tr, err)
	}
	a, _ := newApp(t, Config{Role: RoleBackend, Tracing: tr})
	parent, _, _ := sampledParent()
	if code := getWith(t, a.Handler(), "/work", parent); code != http.StatusOK {
		t.Fatalf("status %d", code)
	}
	if err := tr.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
}

// С переменной — экспортёр создаётся (соединение ленивое, коллектор не нужен).
func TestTracingEnabledWithEndpoint(t *testing.T) {
	t.Setenv(EnvOTLPEndpoint, "http://127.0.0.1:1")
	tr, err := NewTracing(context.Background(), RoleFrontend, "w-1")
	if err != nil || !tr.Enabled() {
		t.Fatalf("tracing must be on: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	_ = tr.Shutdown(ctx) // коллектора нет: ошибка выгрузки ожидаема
}
