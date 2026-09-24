// Package probe — сигналы svc_ok и api_ok и smoke-тест DR Controller'а (ADR 0007):
// HTTP-пробы долгоживущего app кластера по адресу узла и API-сервера кластера.
package probe

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"k8s.io/client-go/tools/clientcmd"
)

// Result — исход пробы; Detail — причина неудачи для лога и инцидента.
type Result struct {
	OK     bool
	Detail string
}

// Prober — HTTP-пробы с общим таймаутом на запрос.
type Prober struct {
	Timeout       time.Duration // таймаут одной пробы (ADR 0007: 2 с)
	KubeconfigDir string        // <cluster-id>.kubeconfig: CA API-сервера для api_ok

	svc *http.Client

	mu  sync.Mutex
	api map[string]*http.Client
}

// New — пробы с таймаутом timeout; kubeconfigDir — Secret federation-kubeconfigs.
func New(timeout time.Duration, kubeconfigDir string) *Prober {
	return &Prober{
		Timeout:       timeout,
		KubeconfigDir: kubeconfigDir,
		// Без keep-alive: каждая проба — новое соединение, как у первого запроса клиента;
		// живое соединение к исчезнувшему поду маскировало бы отказ.
		svc: &http.Client{Timeout: timeout, Transport: &http.Transport{DisableKeepAlives: true}},
		api: map[string]*http.Client{},
	}
}

func get(ctx context.Context, c *http.Client, url string) Result {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return Result{Detail: err.Error()}
	}
	resp, err := c.Do(req)
	if err != nil {
		return Result{Detail: shorten(err.Error())}
	}
	defer func() { _ = resp.Body.Close() }()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
	if resp.StatusCode != http.StatusOK {
		return Result{Detail: fmt.Sprintf("HTTP %d", resp.StatusCode)}
	}
	return Result{OK: true}
}

// Svc — svc_ok: GET http://<addr>/healthz долгоживущего app (NodePort 31998, ADR 0011).
func (p *Prober) Svc(ctx context.Context, addr string) Result {
	return get(ctx, p.svc, "http://"+addr+"/healthz")
}

// Smoke — n запросов GET / к app кластера (frontend → backend), все должны вернуть 200
// (ADR 0007). Останавливается на первой неудаче.
func (p *Prober) Smoke(ctx context.Context, addr string, n int) Result {
	for i := range n {
		if r := get(ctx, p.svc, "http://"+addr+"/"); !r.OK {
			return Result{Detail: fmt.Sprintf("request %d/%d: %s", i+1, n, r.Detail)}
		}
	}
	return Result{OK: true}
}

// API — api_ok: GET {endpoint}/readyz без аутентификации, TLS с CA кластера из kubeconfig.
func (p *Prober) API(ctx context.Context, cluster, endpoint string) Result {
	c, err := p.apiClient(cluster)
	if err != nil {
		return Result{Detail: err.Error()}
	}
	return get(ctx, c, strings.TrimRight(endpoint, "/")+"/readyz")
}

func (p *Prober) apiClient(cluster string) (*http.Client, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if c, ok := p.api[cluster]; ok {
		return c, nil
	}
	pool, err := caPool(filepath.Join(p.KubeconfigDir, cluster+".kubeconfig"))
	if err != nil {
		return nil, err
	}
	c := &http.Client{
		Timeout: p.Timeout,
		Transport: &http.Transport{
			DisableKeepAlives: true,
			TLSClientConfig:   &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12},
		},
	}
	p.api[cluster] = c
	return c, nil
}

// caPool — CA API-сервера из kubeconfig (certificate-authority-data или файл).
func caPool(path string) (*x509.CertPool, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("kubeconfig: %w", err)
	}
	cfg, err := clientcmd.RESTConfigFromKubeConfig(raw)
	if err != nil {
		return nil, fmt.Errorf("kubeconfig %s: %w", path, err)
	}
	ca := cfg.CAData
	if len(ca) == 0 && cfg.CAFile != "" {
		if ca, err = os.ReadFile(cfg.CAFile); err != nil {
			return nil, fmt.Errorf("kubeconfig %s: CA: %w", path, err)
		}
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(ca) {
		return nil, fmt.Errorf("kubeconfig %s: no CA certificate", path)
	}
	return pool, nil
}

func shorten(s string) string {
	const n = 200
	if len(s) > n {
		return s[:n] + "…"
	}
	return s
}
