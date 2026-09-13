package app

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

func newApp(t *testing.T, cfg Config) (*App, *prometheus.Registry) {
	t.Helper()
	reg := prometheus.NewRegistry()
	a, err := New(cfg, reg, nil)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return a, reg
}

func get(t *testing.T, h http.Handler, path string) (*http.Response, string) {
	t.Helper()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
	resp := rec.Result()
	body, _ := io.ReadAll(resp.Body)
	return resp, string(body)
}

func TestNewRejectsUnknownRole(t *testing.T) {
	if _, err := New(Config{Role: "sidecar"}, prometheus.NewRegistry(), nil); err == nil {
		t.Fatal("expected error for unknown role")
	}
}

func TestBackendWork(t *testing.T) {
	a, _ := newApp(t, Config{Role: RoleBackend})
	resp, body := get(t, a.Handler(), "/work")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status %d", resp.StatusCode)
	}
	if !strings.Contains(body, `"tier":"backend"`) {
		t.Fatalf("body %q", body)
	}
	if resp, _ := get(t, a.Handler(), "/readyz"); resp.StatusCode != http.StatusOK {
		t.Fatalf("readyz %d", resp.StatusCode)
	}
}

func TestFrontendHappyPath(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"tier":"backend"}`))
	}))
	defer backend.Close()

	a, _ := newApp(t, Config{Role: RoleFrontend, BackendURL: backend.URL, BackendTimeout: time.Second, Workload: "w-1"})
	resp, body := get(t, a.Handler(), "/")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status %d body %s", resp.StatusCode, body)
	}
	for _, want := range []string{`"tier":"frontend"`, `"workload":"w-1"`, `"backend":{"tier":"backend"}`} {
		if !strings.Contains(body, want) {
			t.Errorf("body lacks %s: %s", want, body)
		}
	}
	if resp, _ := get(t, a.Handler(), "/readyz"); resp.StatusCode != http.StatusOK {
		t.Fatalf("readyz %d", resp.StatusCode)
	}
}

func TestFrontendBackendError(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	defer backend.Close()

	a, _ := newApp(t, Config{Role: RoleFrontend, BackendURL: backend.URL})
	resp, body := get(t, a.Handler(), "/")
	if resp.StatusCode != http.StatusBadGateway {
		t.Fatalf("status %d body %s", resp.StatusCode, body)
	}
	if !strings.Contains(body, "backend status 500") {
		t.Fatalf("body %s", body)
	}
}

func TestFrontendBackendTimeout(t *testing.T) {
	release := make(chan struct{})
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-release:
		case <-r.Context().Done():
		}
	}))
	defer backend.Close()
	defer close(release)

	a, _ := newApp(t, Config{Role: RoleFrontend, BackendURL: backend.URL, BackendTimeout: 50 * time.Millisecond})
	start := time.Now()
	resp, body := get(t, a.Handler(), "/")
	if resp.StatusCode != http.StatusBadGateway {
		t.Fatalf("status %d body %s", resp.StatusCode, body)
	}
	if elapsed := time.Since(start); elapsed > 500*time.Millisecond {
		t.Fatalf("timeout not enforced: %v", elapsed)
	}
	if !strings.Contains(body, "backend unreachable") {
		t.Fatalf("body %s", body)
	}
}

func TestFrontendBackendDownNotReady(t *testing.T) {
	a, _ := newApp(t, Config{Role: RoleFrontend, BackendURL: "http://127.0.0.1:1", BackendTimeout: 100 * time.Millisecond})
	if resp, _ := get(t, a.Handler(), "/readyz"); resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("readyz %d", resp.StatusCode)
	}
	if resp, _ := get(t, a.Handler(), "/healthz"); resp.StatusCode != http.StatusOK {
		t.Fatalf("healthz must stay OK, got %d", resp.StatusCode)
	}
}

func TestMetricsContract(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	defer backend.Close()

	a, _ := newApp(t, Config{Role: RoleFrontend, BackendURL: backend.URL})
	h := a.Handler()
	get(t, h, "/")
	get(t, h, "/healthz")
	get(t, h, "/readyz")
	_, body := get(t, h, "/metrics")

	for _, want := range []string{
		`http_requests_total{method="GET",route="/",status="502",tier="frontend"} 1`,
		`http_request_duration_seconds_bucket{route="/",tier="frontend",le="0.2"}`,
	} {
		if !strings.Contains(body, want) {
			t.Errorf("metrics lack %s", want)
		}
	}
	for _, forbidden := range []string{`route="/healthz"`, `route="/readyz"`, `route="/metrics"`} {
		if strings.Contains(body, forbidden) {
			t.Errorf("probe route leaked into metrics: %s", forbidden)
		}
	}
}
