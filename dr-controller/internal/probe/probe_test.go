package probe

import (
	"context"
	"encoding/base64"
	"encoding/pem"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestSvcAndSmoke(t *testing.T) {
	var calls atomic.Int32
	failAfter := int32(1 << 30)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/healthz":
			w.WriteHeader(http.StatusOK)
		case "/":
			if calls.Add(1) > failAfter {
				w.WriteHeader(http.StatusBadGateway)
				return
			}
			w.WriteHeader(http.StatusOK)
		}
	}))
	defer srv.Close()
	addr := strings.TrimPrefix(srv.URL, "http://")
	p := New(time.Second, t.TempDir())
	ctx := context.Background()

	if r := p.Svc(ctx, addr); !r.OK {
		t.Fatalf("svc: %+v", r)
	}
	if r := p.Smoke(ctx, addr, 10); !r.OK || calls.Load() != 10 {
		t.Fatalf("smoke: %+v, calls %d", r, calls.Load())
	}
	calls.Store(0)
	failAfter = 3
	r := p.Smoke(ctx, addr, 10)
	if r.OK || !strings.Contains(r.Detail, "request 4/10: HTTP 502") || calls.Load() != 4 {
		t.Fatalf("smoke must stop at first failure: %+v, calls %d", r, calls.Load())
	}
}

func TestSvcFailures(t *testing.T) {
	p := New(200*time.Millisecond, t.TempDir())
	ctx := context.Background()
	// Порт без слушателя: отказ соединения.
	srv := httptest.NewServer(http.NotFoundHandler())
	addr := strings.TrimPrefix(srv.URL, "http://")
	srv.Close()
	if r := p.Svc(ctx, addr); r.OK || r.Detail == "" {
		t.Fatalf("closed port must fail: %+v", r)
	}
	// Зависший сервер: таймаут пробы.
	slow := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(time.Second)
	}))
	defer slow.Close()
	if r := p.Svc(ctx, strings.TrimPrefix(slow.URL, "http://")); r.OK {
		t.Fatalf("slow server must time out: %+v", r)
	}
}

// api_ok проверяет TLS по CA из kubeconfig кластера и не шлёт учётных данных.
func TestAPIUsesClusterCA(t *testing.T) {
	var sawAuth atomic.Bool
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "" {
			sawAuth.Store(true)
		}
		if r.URL.Path == "/readyz" {
			_, _ = w.Write([]byte("ok"))
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srv.Close()
	ca := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw})
	dir := t.TempDir()
	kubeconfig := fmt.Sprintf(`apiVersion: v1
kind: Config
clusters:
- name: c
  cluster:
    server: %s
    certificate-authority-data: %s
users:
- name: u
  user:
    token: secret
contexts:
- name: c
  context: {cluster: c, user: u}
current-context: c
`, srv.URL, base64.StdEncoding.EncodeToString(ca))
	if err := os.WriteFile(filepath.Join(dir, "cluster-a.kubeconfig"), []byte(kubeconfig), 0o600); err != nil {
		t.Fatal(err)
	}
	p := New(time.Second, dir)
	if r := p.API(context.Background(), "cluster-a", srv.URL+"/"); !r.OK {
		t.Fatalf("api: %+v", r)
	}
	if sawAuth.Load() {
		t.Fatal("api probe must not send credentials")
	}
	// Нет kubeconfig — нет доверия к API-серверу: проба неуспешна с причиной.
	if r := p.API(context.Background(), "cluster-z", srv.URL); r.OK || !strings.Contains(r.Detail, "kubeconfig") {
		t.Fatalf("missing kubeconfig: %+v", r)
	}
}
