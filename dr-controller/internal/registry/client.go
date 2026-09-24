// Package registry — клиент каталога для DR Controller'а (architecture §3.3, ADR 0003,
// 0018, 0028): список кластеров, статус down, инциденты. Токен роли dr-controller.
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
)

// Статусы кластера в каталоге (ADR 0003): DR пишет только down и снятие down.
const (
	StatusHealthy = "healthy"
	StatusDown    = "down"
)

// Cluster — карточка кластера из GET /clusters (нужные DR поля).
type Cluster struct {
	ID          string `json:"id"`
	APIEndpoint string `json:"api_endpoint"`
	SloStatus   string `json:"slo_status"`
}

// Incident — строка incidents (ADR 0028). Поля времени — nil, пока фаза не наступила.
// fault_injected_at и fault_ref пишет раннер сценария, DR их не отправляет.
type Incident struct {
	ID                  int64          `json:"id,omitempty"`
	ClusterID           string         `json:"cluster_id"`
	DRMode              string         `json:"dr_mode"`
	PeakState           string         `json:"peak_state"`
	DetectedAt          time.Time      `json:"detected_at"`
	FailoverStartedAt   *time.Time     `json:"failover_started_at,omitempty"`
	FailoverCompletedAt *time.Time     `json:"failover_completed_at,omitempty"`
	RecoveryStartedAt   *time.Time     `json:"recovery_started_at,omitempty"`
	RecoveryCompletedAt *time.Time     `json:"recovery_completed_at,omitempty"`
	Details             map[string]any `json:"details"`
}

// patch — тело PATCH /incidents/{id}: поля, которыми владеет DR.
type patch struct {
	PeakState           string         `json:"peak_state"`
	FailoverStartedAt   *time.Time     `json:"failover_started_at,omitempty"`
	FailoverCompletedAt *time.Time     `json:"failover_completed_at,omitempty"`
	RecoveryStartedAt   *time.Time     `json:"recovery_started_at,omitempty"`
	RecoveryCompletedAt *time.Time     `json:"recovery_completed_at,omitempty"`
	Details             map[string]any `json:"details,omitempty"`
}

// APIError — ответ каталога с кодом не 2xx.
type APIError struct {
	Status int
	Body   string
}

func (e *APIError) Error() string {
	return fmt.Sprintf("registry: HTTP %d: %s", e.Status, strings.TrimSpace(e.Body))
}

// Client — HTTP-клиент каталога с bearer-токеном роли dr-controller.
type Client struct {
	BaseURL string
	Token   string
	HTTP    *http.Client
}

// New — клиент с таймаутом запроса timeout.
func New(baseURL, token string, timeout time.Duration) *Client {
	return &Client{BaseURL: strings.TrimRight(baseURL, "/"), Token: token, HTTP: &http.Client{Timeout: timeout}}
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

// Clusters — GET /api/v1/clusters: инвентарь федерации.
func (c *Client) Clusters(ctx context.Context) ([]Cluster, error) {
	var out []Cluster
	if err := c.do(ctx, http.MethodGet, "/api/v1/clusters", nil, nil, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// SetStatus — PATCH /api/v1/clusters/{id}/status (ADR 0003).
func (c *Client) SetStatus(ctx context.Context, id, status string) error {
	return c.do(ctx, http.MethodPatch, "/api/v1/clusters/"+url.PathEscape(id)+"/status", nil,
		map[string]string{"slo_status": status}, nil)
}

// OpenIncident — POST /api/v1/incidents; возвращает id.
func (c *Client) OpenIncident(ctx context.Context, inc Incident) (int64, error) {
	var out struct {
		ID int64 `json:"id"`
	}
	body := inc
	body.ID = 0
	if err := c.do(ctx, http.MethodPost, "/api/v1/incidents", nil, body, &out); err != nil {
		return 0, err
	}
	return out.ID, nil
}

// UpdateIncident — PATCH /api/v1/incidents/{id} полями, которыми владеет DR.
func (c *Client) UpdateIncident(ctx context.Context, inc Incident) error {
	body := patch{
		PeakState:           inc.PeakState,
		FailoverStartedAt:   inc.FailoverStartedAt,
		FailoverCompletedAt: inc.FailoverCompletedAt,
		RecoveryStartedAt:   inc.RecoveryStartedAt,
		RecoveryCompletedAt: inc.RecoveryCompletedAt,
		Details:             inc.Details,
	}
	return c.do(ctx, http.MethodPatch, "/api/v1/incidents/"+strconv.FormatInt(inc.ID, 10), nil, body, nil)
}

// OpenIncidents — незакрытые инциденты кластера: продолжение после рестарта DR.
func (c *Client) OpenIncidents(ctx context.Context, cluster string) ([]Incident, error) {
	q := url.Values{}
	q.Set("cluster_id", cluster)
	q.Set("open", "true")
	var out []Incident
	if err := c.do(ctx, http.MethodGet, "/api/v1/incidents", q, nil, &out); err != nil {
		return nil, err
	}
	return out, nil
}
