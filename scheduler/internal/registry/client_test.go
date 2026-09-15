package registry

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
)

type recorded struct {
	method, path, query, auth string
	body                      map[string]any
}

func server(t *testing.T, status int, reply string, rec *recorded) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rec.method, rec.path, rec.query, rec.auth = r.Method, r.URL.Path, r.URL.RawQuery, r.Header.Get("Authorization")
		if r.Body != nil {
			_ = json.NewDecoder(r.Body).Decode(&rec.body)
		}
		w.WriteHeader(status)
		_, _ = w.Write([]byte(reply))
	}))
	t.Cleanup(srv.Close)
	return New(srv.URL+"/", "tok")
}

func TestEligibleQueryAndDecode(t *testing.T) {
	rec := &recorded{}
	c := server(t, 200, `{"eligible":[{"id":"cluster-a","slo_status":"healthy","metrics":{"observed_at":"2026-09-15T12:00:00Z","error_rate":0.01,"latency_p95_ms":120.5,"saturation":null}}],"rejected":[{"id":"cluster-b","slo_status":"unknown","metrics":null,"filtered_by":"certification:pci-dss"}]}`, rec)
	cpu := 0.5
	out, err := c.Eligible(context.Background(), Requirements{Region: "eu-west", Certifications: []string{"pci-dss", "iso-27001"}, MinCPU: &cpu})
	if err != nil {
		t.Fatal(err)
	}
	if rec.method != "GET" || rec.path != "/api/v1/clusters/eligible" || rec.auth != "Bearer tok" {
		t.Fatalf("request %+v", rec)
	}
	if rec.query != "certification=pci-dss&certification=iso-27001&min_cpu=0.5&region=eu-west" {
		t.Fatalf("query %q", rec.query)
	}
	if len(out.Eligible) != 1 || *out.Eligible[0].Metrics.ErrorRate != 0.01 || out.Eligible[0].Metrics.Saturation != nil {
		t.Fatalf("eligible %+v", out.Eligible)
	}
	if out.Rejected[0].FilteredBy != "certification:pci-dss" || out.Rejected[0].Metrics != nil {
		t.Fatalf("rejected %+v", out.Rejected)
	}
}

func TestCreateDecisionBodyKeepsAllCandidates(t *testing.T) {
	rec := &recorded{}
	c := server(t, 201, `{"id": 42, "outcome": "placed"}`, rec)
	score := 0.83
	fb := "certification:pci-dss"
	chosen := "cluster-a"
	id, err := c.CreateDecision(context.Background(), Decision{
		WorkloadRef: "workloads/w-1", SchedulerKind: "meta", ChosenCluster: &chosen,
		Candidates: []Candidate{{Cluster: "cluster-a", Score: &score}, {Cluster: "cluster-b", FilteredBy: &fb}},
		Scoring:    map[string]any{"formula": "adr-0005"}, Outcome: OutcomePlaced,
	})
	if err != nil || id != 42 {
		t.Fatalf("id %d err %v", id, err)
	}
	cands := rec.body["candidates"].([]any)
	if len(cands) != 2 {
		t.Fatalf("candidates %v", cands)
	}
	second := cands[1].(map[string]any)
	if second["score"] != nil || second["filtered_by"] != "certification:pci-dss" {
		t.Fatalf("second candidate %v", second)
	}
	if rec.body["scheduler_kind"] != "meta" || rec.body["chosen_cluster"] != "cluster-a" || rec.body["outcome"] != "placed" {
		t.Fatalf("body %v", rec.body)
	}
	// Наивный: scoring null, chosen_cluster null при no_candidates.
	rec2 := &recorded{}
	c2 := server(t, 201, `{"id": 43}`, rec2)
	if _, err := c2.CreateDecision(context.Background(), Decision{
		WorkloadRef: "workloads/w-2", SchedulerKind: "naive", Candidates: []Candidate{}, Outcome: OutcomeNoCandidates,
	}); err != nil {
		t.Fatal(err)
	}
	if rec2.body["scoring"] != nil || rec2.body["chosen_cluster"] != nil {
		t.Fatalf("naive body %v", rec2.body)
	}
	if _, ok := rec2.body["candidates"].([]any); !ok {
		t.Fatalf("candidates must be an empty array, got %v", rec2.body["candidates"])
	}
}

func TestPatchDecisionAndViolation(t *testing.T) {
	rec := &recorded{}
	c := server(t, 200, `{"id": 7, "outcome": "rejected"}`, rec)
	if err := c.PatchDecision(context.Background(), 7, OutcomeRejected); err != nil {
		t.Fatal(err)
	}
	if rec.method != "PATCH" || rec.path != "/api/v1/decisions/7" || rec.body["outcome"] != "rejected" {
		t.Fatalf("patch %+v", rec)
	}

	rec = &recorded{}
	c = server(t, 201, `{"id": 1}`, rec)
	err := c.CreateViolation(context.Background(), Violation{
		WorkloadRef: "workloads/w-1", ClusterID: "cluster-b", PolicyName: "require-certification", DetectedBy: "kyverno",
		Details:      map[string]any{"reported_via": "scheduler"},
		Requirements: &Requirements{Certifications: []string{"pci-dss"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	reqs := rec.body["requirements"].(map[string]any)
	if rec.path != "/api/v1/violations" || reqs["certifications"].([]any)[0] != "pci-dss" || rec.body["detected_by"] != "kyverno" {
		t.Fatalf("violation %+v", rec)
	}
}

func TestAPIError(t *testing.T) {
	rec := &recorded{}
	c := server(t, 401, `{"detail":"unknown token"}`, rec)
	_, err := c.Eligible(context.Background(), Requirements{})
	var apiErr *APIError
	if !errors.As(err, &apiErr) || apiErr.Status != 401 {
		t.Fatalf("err %v", err)
	}
}

func TestRequirementsFromAnnotations(t *testing.T) {
	req, err := RequirementsFromAnnotations(map[string]string{
		AnnotationRegion:        " eu-west ",
		AnnotationCertification: "pci-dss, iso-27001,,",
		AnnotationCPU:           "500m",
		AnnotationMemoryGi:      "4",
		"other":                 "ignored",
	})
	if err != nil {
		t.Fatal(err)
	}
	if req.Region != "eu-west" || len(req.Certifications) != 2 || req.Certifications[1] != "iso-27001" {
		t.Fatalf("req %+v", req)
	}
	if *req.MinCPU != 0.5 || *req.MinMemoryGi != 4 {
		t.Fatalf("resources %v %v", *req.MinCPU, *req.MinMemoryGi)
	}
	empty, err := RequirementsFromAnnotations(nil)
	if err != nil || empty.Region != "" || empty.Certifications == nil || len(empty.Certifications) != 0 || empty.MinCPU != nil {
		t.Fatalf("empty %+v err %v", empty, err)
	}
	if _, err := RequirementsFromAnnotations(map[string]string{AnnotationCPU: "two"}); err == nil {
		t.Fatal("bad cpu must fail")
	}
	if _, err := RequirementsFromAnnotations(map[string]string{AnnotationMemoryGi: "4Gi"}); err == nil {
		t.Fatal("memory must be a plain number of Gi")
	}
}
