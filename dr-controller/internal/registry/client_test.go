package registry

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestClient(t *testing.T) {
	type call struct {
		method, path, query string
		body                map[string]any
	}
	var calls []call
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer tok" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		c := call{method: r.Method, path: r.URL.Path, query: r.URL.RawQuery}
		if raw, _ := io.ReadAll(r.Body); len(raw) > 0 {
			_ = json.Unmarshal(raw, &c.body)
		}
		calls = append(calls, c)
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/clusters":
			_, _ = w.Write([]byte(`[{"id":"cluster-a","api_endpoint":"https://a:6443","slo_status":"down","region":"x"}]`))
		case r.Method == http.MethodPost && r.URL.Path == "/api/v1/incidents":
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write([]byte(`{"id":7}`))
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/incidents":
			_, _ = w.Write([]byte(`[{"id":7,"cluster_id":"cluster-a","dr_mode":"active","peak_state":"down",
				"detected_at":"2026-09-24T12:00:15Z","failover_completed_at":"2026-09-24T12:00:45.5Z","details":{"failovers":1}}]`))
		case r.URL.Path == "/api/v1/clusters/cluster-z/status":
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"detail":"not registered"}`))
		default:
			_, _ = w.Write([]byte(`{}`))
		}
	}))
	defer srv.Close()
	c := New(srv.URL+"/", "tok", time.Second)
	ctx := context.Background()

	clusters, err := c.Clusters(ctx)
	if err != nil || len(clusters) != 1 || clusters[0].SloStatus != "down" || clusters[0].APIEndpoint != "https://a:6443" {
		t.Fatalf("clusters: %v %v", clusters, err)
	}
	if err := c.SetStatus(ctx, "cluster-a", StatusDown); err != nil {
		t.Fatal(err)
	}
	var apiErr *APIError
	if err := c.SetStatus(ctx, "cluster-z", StatusDown); !errors.As(err, &apiErr) || apiErr.Status != 404 {
		t.Fatalf("expected 404, got %v", err)
	}

	detected := time.Date(2026, 9, 24, 12, 0, 15, 0, time.UTC)
	id, err := c.OpenIncident(ctx, Incident{ID: 99, ClusterID: "cluster-a", DRMode: "active", PeakState: "degraded",
		DetectedAt: detected, Details: map[string]any{"signals": "svc"}})
	if err != nil || id != 7 {
		t.Fatalf("open: %d %v", id, err)
	}
	failover := detected.Add(30 * time.Second)
	if err := c.UpdateIncident(ctx, Incident{ID: 7, PeakState: "down", FailoverStartedAt: &failover,
		Details: map[string]any{"failovers": 1}}); err != nil {
		t.Fatal(err)
	}
	open, err := c.OpenIncidents(ctx, "cluster-a")
	if err != nil || len(open) != 1 || open[0].ID != 7 || open[0].FailoverCompletedAt == nil || open[0].RecoveryStartedAt != nil {
		t.Fatalf("open incidents: %+v %v", open, err)
	}

	post := calls[3]
	if post.body["cluster_id"] != "cluster-a" || post.body["detected_at"] != "2026-09-24T12:00:15Z" {
		t.Fatalf("post body: %v", post.body)
	}
	if _, ok := post.body["id"]; ok {
		t.Fatalf("post must not send id: %v", post.body)
	}
	if _, ok := post.body["failover_started_at"]; ok {
		t.Fatalf("unset phases must be omitted: %v", post.body)
	}
	p := calls[4]
	if p.method != http.MethodPatch || p.path != "/api/v1/incidents/7" || p.body["peak_state"] != "down" {
		t.Fatalf("patch: %+v", p)
	}
	for _, k := range []string{"fault_injected_at", "fault_ref", "recovery_started_at"} {
		if _, ok := p.body[k]; ok {
			t.Fatalf("patch must not send %s: %v", k, p.body)
		}
	}
	if q := calls[5].query; q != "cluster_id=cluster-a&open=true" {
		t.Fatalf("open incidents query: %s", q)
	}
}
