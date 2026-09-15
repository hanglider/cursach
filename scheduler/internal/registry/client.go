// Package registry — клиент каталога кластеров (architecture §3.3, ADR 0018, 0025):
// кандидаты под hard constraints, запись решений и нарушений. Единственный
// источник состояния для планировщика (инвариант 2).
package registry

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"k8s.io/apimachinery/pkg/api/resource"
)

// Аннотации требований workload'а (ADR 0009).
const (
	AnnotationRegion        = "federation.spbu/require-region"
	AnnotationCertification = "federation.spbu/require-certification"
	AnnotationCPU           = "federation.spbu/require-cpu"
	AnnotationMemoryGi      = "federation.spbu/require-memory-gi"
)

// Исходы решения (ADR 0004).
const (
	OutcomePlaced       = "placed"
	OutcomeRejected     = "rejected"
	OutcomeNoCandidates = "no_candidates"
)

// Metrics — последний снимок метрик кластера из каталога.
type Metrics struct {
	ObservedAt        time.Time `json:"observed_at"`
	ErrorRate         *float64  `json:"error_rate"`
	LatencyP95Ms      *float64  `json:"latency_p95_ms"`
	Saturation        *float64  `json:"saturation"`
	CPUAvailable      *float64  `json:"cpu_available"`
	MemoryAvailableGi *float64  `json:"memory_available_gi"`
}

// Entry — кластер в ответе /clusters/eligible.
type Entry struct {
	ID         string   `json:"id"`
	SloStatus  string   `json:"slo_status"`
	Metrics    *Metrics `json:"metrics"`
	FilteredBy string   `json:"filtered_by,omitempty"` // только у rejected
}

// Eligible — ответ каталога: допустимые и отсеянные кандидаты.
type Eligible struct {
	Eligible []Entry `json:"eligible"`
	Rejected []Entry `json:"rejected"`
}

// Requirements — hard constraints workload'а; те же поля, что у каталога.
type Requirements struct {
	Region         string   `json:"region,omitempty"`
	Certifications []string `json:"certifications"`
	MinCPU         *float64 `json:"min_cpu,omitempty"`
	MinMemoryGi    *float64 `json:"min_memory_gi,omitempty"`
}

// RequirementsFromAnnotations разбирает аннотации federation.spbu/require-*.
func RequirementsFromAnnotations(ann map[string]string) (Requirements, error) {
	req := Requirements{Certifications: []string{}}
	req.Region = strings.TrimSpace(ann[AnnotationRegion])
	for _, c := range strings.Split(ann[AnnotationCertification], ",") {
		if c = strings.TrimSpace(c); c != "" {
			req.Certifications = append(req.Certifications, c)
		}
	}
	if raw := strings.TrimSpace(ann[AnnotationCPU]); raw != "" {
		q, err := resource.ParseQuantity(raw)
		if err != nil {
			return req, fmt.Errorf("%s: %w", AnnotationCPU, err)
		}
		v := q.AsApproximateFloat64()
		req.MinCPU = &v
	}
	if raw := strings.TrimSpace(ann[AnnotationMemoryGi]); raw != "" {
		v, err := strconv.ParseFloat(raw, 64)
		if err != nil {
			return req, fmt.Errorf("%s: %w", AnnotationMemoryGi, err)
		}
		req.MinMemoryGi = &v
	}
	return req, nil
}

func (r Requirements) query() url.Values {
	q := url.Values{}
	if r.Region != "" {
		q.Set("region", r.Region)
	}
	for _, c := range r.Certifications {
		q.Add("certification", c)
	}
	if r.MinCPU != nil {
		q.Set("min_cpu", strconv.FormatFloat(*r.MinCPU, 'g', -1, 64))
	}
	if r.MinMemoryGi != nil {
		q.Set("min_memory_gi", strconv.FormatFloat(*r.MinMemoryGi, 'g', -1, 64))
	}
	return q
}

// Candidate — строка в placement_decisions.candidates: все кандидаты, не только
// победитель (ADR 0004); Score nil у отсеянных и у наивного планировщика.
type Candidate struct {
	Cluster    string             `json:"cluster"`
	Score      *float64           `json:"score"`
	Metrics    *Metrics           `json:"metrics"`
	FilteredBy *string            `json:"filtered_by"`
	Terms      map[string]float64 `json:"terms,omitempty"`
}

// Decision — тело POST /decisions.
type Decision struct {
	WorkloadRef   string         `json:"workload_ref"`
	SchedulerKind string         `json:"scheduler_kind"`
	ChosenCluster *string        `json:"chosen_cluster"`
	Candidates    []Candidate    `json:"candidates"`
	Scoring       map[string]any `json:"scoring"`
	Outcome       string         `json:"outcome"`
}

// Violation — тело POST /violations (ADR 0019, 0026).
type Violation struct {
	WorkloadRef  string         `json:"workload_ref"`
	ClusterID    string         `json:"cluster_id"`
	PolicyName   string         `json:"policy_name"`
	DetectedBy   string         `json:"detected_by"`
	Details      map[string]any `json:"details,omitempty"`
	Requirements *Requirements  `json:"requirements,omitempty"`
}

// APIError — ответ каталога с кодом не 2xx.
type APIError struct {
	Status int
	Body   string
}

func (e *APIError) Error() string {
	return fmt.Sprintf("registry: HTTP %d: %s", e.Status, strings.TrimSpace(e.Body))
}

// Client — HTTP-клиент каталога с bearer-токеном роли scheduler.
type Client struct {
	BaseURL string
	Token   string
	HTTP    *http.Client
}

// New создаёт клиент с таймаутом 10 с.
func New(baseURL, token string) *Client {
	return &Client{
		BaseURL: strings.TrimRight(baseURL, "/"),
		Token:   token,
		HTTP:    &http.Client{Timeout: 10 * time.Second},
	}
}

func (c *Client) do(ctx context.Context, method, path string, q url.Values, body, out any) error {
	u := c.BaseURL + path
	if len(q) > 0 {
		u += "?" + q.Encode()
	}
	var payload io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		payload = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, u, payload)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+c.Token)
	req.Header.Set("Accept", "application/json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return fmt.Errorf("registry: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return fmt.Errorf("registry: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return &APIError{Status: resp.StatusCode, Body: string(raw)}
	}
	if out != nil {
		if err := json.Unmarshal(raw, out); err != nil {
			return fmt.Errorf("registry: decode %s: %w", path, err)
		}
	}
	return nil
}

// Eligible — GET /api/v1/clusters/eligible под hard constraints workload'а.
func (c *Client) Eligible(ctx context.Context, req Requirements) (*Eligible, error) {
	var out Eligible
	if err := c.do(ctx, http.MethodGet, "/api/v1/clusters/eligible", req.query(), nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// CreateDecision — POST /api/v1/decisions; возвращает id строки.
func (c *Client) CreateDecision(ctx context.Context, d Decision) (int64, error) {
	var out struct {
		ID int64 `json:"id"`
	}
	if err := c.do(ctx, http.MethodPost, "/api/v1/decisions", nil, d, &out); err != nil {
		return 0, err
	}
	return out.ID, nil
}

// PatchDecision — PATCH /api/v1/decisions/{id}: исход после admission (ADR 0025).
func (c *Client) PatchDecision(ctx context.Context, id int64, outcome string) error {
	body := map[string]string{"outcome": outcome}
	return c.do(ctx, http.MethodPatch, "/api/v1/decisions/"+strconv.FormatInt(id, 10), nil, body, nil)
}

// CreateViolation — POST /api/v1/violations.
func (c *Client) CreateViolation(ctx context.Context, v Violation) error {
	return c.do(ctx, http.MethodPost, "/api/v1/violations", nil, v, nil)
}
