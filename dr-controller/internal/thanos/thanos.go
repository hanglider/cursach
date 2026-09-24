// Package thanos — сигнал sli_ok DR Controller'а: error rate кластеров за 1 мин из
// Thanos Querier (ADR 0007). DR читает Thanos напрямую; инвариант 2 касается только
// планировщика.
package thanos

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// ErrorRateQuery — recording rule кластера, окно 1 мин (architecture §2.4, ADR 0007).
const ErrorRateQuery = "cluster:http_error_rate:ratio1m"

// Client — instant-запросы к Thanos Querier.
type Client struct {
	BaseURL string
	Query   string
	HTTP    *http.Client
}

// New — клиент с таймаутом запроса timeout.
func New(baseURL string, timeout time.Duration) *Client {
	return &Client{
		BaseURL: strings.TrimRight(baseURL, "/"),
		Query:   ErrorRateQuery,
		HTTP:    &http.Client{Timeout: timeout},
	}
}

type response struct {
	Status string `json:"status"`
	Error  string `json:"error"`
	Data   struct {
		ResultType string `json:"resultType"`
		Result     []struct {
			Metric map[string]string `json:"metric"`
			Value  [2]any            `json:"value"`
		} `json:"result"`
	} `json:"data"`
}

// ErrorRates — error rate по метке cluster. Кластера нет в ответе, если ряда нет или
// значение NaN (0/0 без трафика): это «неизвестно», а не «плохо» (инвариант 6).
// partial_response: выпавший store одного кластера не лишает сигнала остальные.
func (c *Client) ErrorRates(ctx context.Context) (map[string]float64, error) {
	q := url.Values{}
	q.Set("query", c.Query)
	q.Set("partial_response", "true")
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+"/api/v1/query?"+q.Encode(), nil)
	if err != nil {
		return nil, err
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, fmt.Errorf("thanos: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return nil, fmt.Errorf("thanos: %w", err)
	}
	var r response
	if err := json.Unmarshal(raw, &r); err != nil {
		return nil, fmt.Errorf("thanos: HTTP %d: decode: %w", resp.StatusCode, err)
	}
	if resp.StatusCode != http.StatusOK || r.Status != "success" {
		return nil, fmt.Errorf("thanos: HTTP %d: %s", resp.StatusCode, r.Error)
	}
	if r.Data.ResultType != "vector" {
		return nil, fmt.Errorf("thanos: unexpected result type %q", r.Data.ResultType)
	}
	out := map[string]float64{}
	for _, s := range r.Data.Result {
		cluster := s.Metric["cluster"]
		str, ok := s.Value[1].(string)
		if cluster == "" || !ok {
			continue
		}
		v, err := strconv.ParseFloat(str, 64)
		if err != nil || math.IsNaN(v) || math.IsInf(v, 0) {
			continue
		}
		out[cluster] = v
	}
	return out, nil
}
