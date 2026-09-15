// Package clusters — доступ к API кластеров федерации из control по kubeconfig'ам
// Secret'а federation-kubeconfigs (ADR 0018, 0021): файл <cluster-id>.kubeconfig.
package clusters

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/tools/clientcmd"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// Provider — то, что нужно контроллеру; подменяется в тестах.
type Provider interface {
	Get(id string) (client.Client, error)
	NodeAddress(ctx context.Context, id string) (string, error)
}

// Clients — ленивый кэш клиентов по id кластера.
type Clients struct {
	dir    string
	scheme *runtime.Scheme

	mu    sync.Mutex
	cache map[string]client.Client
}

// New — каталог kubeconfig'ов и scheme для типизированных объектов.
func New(dir string, scheme *runtime.Scheme) *Clients {
	return &Clients{dir: dir, scheme: scheme, cache: map[string]client.Client{}}
}

// Get — клиент кластера id; без кэша controller-runtime, чтения идут в API-сервер.
func (c *Clients) Get(id string) (client.Client, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if cl, ok := c.cache[id]; ok {
		return cl, nil
	}
	path := filepath.Join(c.dir, id+".kubeconfig")
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("cluster %s: %w", id, err)
	}
	cfg, err := clientcmd.RESTConfigFromKubeConfig(raw)
	if err != nil {
		return nil, fmt.Errorf("cluster %s: kubeconfig: %w", id, err)
	}
	cfg.Timeout = 15 * time.Second
	cl, err := client.New(cfg, client.Options{Scheme: c.scheme})
	if err != nil {
		return nil, fmt.Errorf("cluster %s: client: %w", id, err)
	}
	c.cache[id] = cl
	return cl, nil
}

// NodeAddress — InternalIP первого узла: в kind совпадает с IP контейнера в
// docker-сети (ADR 0022, 0027), других адресов у узла нет.
func (c *Clients) NodeAddress(ctx context.Context, id string) (string, error) {
	cl, err := c.Get(id)
	if err != nil {
		return "", err
	}
	var nodes corev1.NodeList
	if err := cl.List(ctx, &nodes); err != nil {
		return "", fmt.Errorf("cluster %s: list nodes: %w", id, err)
	}
	return FirstInternalIP(nodes.Items)
}

// FirstInternalIP — InternalIP первого узла списка.
func FirstInternalIP(nodes []corev1.Node) (string, error) {
	for _, n := range nodes {
		for _, a := range n.Status.Addresses {
			if a.Type == corev1.NodeInternalIP && a.Address != "" {
				return a.Address, nil
			}
		}
	}
	return "", fmt.Errorf("no node with InternalIP among %d nodes", len(nodes))
}
