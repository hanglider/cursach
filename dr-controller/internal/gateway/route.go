// Package gateway — объект переключения трафика DR Controller'а: маршрут app.fed.local
// на шлюзе control-кластера с backend'ом на каждый кластер федерации (ADR 0011, 0017).
//
// Backend `app-<cluster>` (Envoy Gateway) хранит адрес узла и NodePort app кластера —
// ровно то, куда шлюз шлёт трафик; проба svc_ok идёт туда же. Веса меняются правкой
// backendRefs[].weight HTTPRoute через Kubernetes API.
package gateway

import (
	"context"
	"fmt"
	"net"
	"strconv"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// Метки объектов маршрута app (render-app-route.sh).
const (
	LabelWorkload = "federation.spbu/workload"
	LabelCluster  = "federation.spbu/cluster"
)

// GVK шлюзовых объектов: без типизированных Go-модулей, как у планировщика (ADR 0027).
var (
	BackendGVK   = schema.GroupVersionKind{Group: "gateway.envoyproxy.io", Version: "v1alpha1", Kind: "Backend"}
	HTTPRouteGVK = schema.GroupVersionKind{Group: "gateway.networking.k8s.io", Version: "v1", Kind: "HTTPRoute"}
)

// Backend — вход в кластер федерации для маршрута app.
type Backend struct {
	Name    string // имя объекта Backend, на него ссылается backendRef
	Cluster string // id кластера федерации
	Addr    string // <IP узла>:<NodePort>
}

// Route — маршрут app в namespace шлюза.
type Route struct {
	Client    client.Client
	Namespace string // gateway
	Name      string // app
	Workload  string // app: значение метки federation.spbu/workload у Backend'ов
}

// Backends — Backend'ы маршрута app по кластерам.
func (r *Route) Backends(ctx context.Context) (map[string]Backend, error) {
	list := &unstructured.UnstructuredList{}
	list.SetGroupVersionKind(BackendGVK.GroupVersion().WithKind(BackendGVK.Kind + "List"))
	if err := r.Client.List(ctx, list, client.InNamespace(r.Namespace),
		client.MatchingLabels{LabelWorkload: r.Workload}); err != nil {
		return nil, fmt.Errorf("list backends: %w", err)
	}
	out := map[string]Backend{}
	for _, item := range list.Items {
		cluster := item.GetLabels()[LabelCluster]
		if cluster == "" {
			continue
		}
		addr, err := endpoint(item.Object)
		if err != nil {
			return nil, fmt.Errorf("backend %s: %w", item.GetName(), err)
		}
		out[cluster] = Backend{Name: item.GetName(), Cluster: cluster, Addr: addr}
	}
	return out, nil
}

func endpoint(obj map[string]any) (string, error) {
	eps, _, err := unstructured.NestedSlice(obj, "spec", "endpoints")
	if err != nil || len(eps) == 0 {
		return "", fmt.Errorf("no spec.endpoints")
	}
	ep, ok := eps[0].(map[string]any)
	if !ok {
		return "", fmt.Errorf("bad endpoint")
	}
	ip, _, _ := unstructured.NestedString(ep, "ip", "address")
	port, found, _ := unstructured.NestedFieldNoCopy(ep, "ip", "port")
	if ip == "" || !found {
		return "", fmt.Errorf("endpoint without ip.address and ip.port")
	}
	p, err := toInt64(port)
	if err != nil {
		return "", err
	}
	return net.JoinHostPort(ip, strconv.FormatInt(p, 10)), nil
}

func toInt64(v any) (int64, error) {
	switch n := v.(type) {
	case int64:
		return n, nil
	case int:
		return int64(n), nil
	case int32:
		return int64(n), nil
	case float64:
		return int64(n), nil
	default:
		return 0, fmt.Errorf("not a number: %v", v)
	}
}

// refs — backendRefs всех правил маршрута (указатели в объект, чтобы менять на месте).
func refs(route *unstructured.Unstructured) ([]map[string]any, error) {
	// NoCopy: NestedSlice вернул бы глубокую копию, и правка весов не дошла бы до объекта.
	raw, _, err := unstructured.NestedFieldNoCopy(route.Object, "spec", "rules")
	if err != nil {
		return nil, err
	}
	rules, _ := raw.([]any)
	var out []map[string]any
	for _, rule := range rules {
		rm, ok := rule.(map[string]any)
		if !ok {
			continue
		}
		list, ok := rm["backendRefs"].([]any)
		if !ok {
			continue
		}
		for _, ref := range list {
			if m, ok := ref.(map[string]any); ok {
				out = append(out, m)
			}
		}
	}
	return out, nil
}

func (r *Route) get(ctx context.Context) (*unstructured.Unstructured, error) {
	route := &unstructured.Unstructured{}
	route.SetGroupVersionKind(HTTPRouteGVK)
	if err := r.Client.Get(ctx, client.ObjectKey{Namespace: r.Namespace, Name: r.Name}, route); err != nil {
		return nil, fmt.Errorf("get httproute %s/%s: %w", r.Namespace, r.Name, err)
	}
	return route, nil
}

// Weights — текущие веса маршрута по кластерам. Вес без поля weight — 1 (Gateway API).
func (r *Route) Weights(ctx context.Context, backends map[string]Backend) (map[string]int64, error) {
	route, err := r.get(ctx)
	if err != nil {
		return nil, err
	}
	return weights(route, byName(backends))
}

func byName(backends map[string]Backend) map[string]string {
	out := make(map[string]string, len(backends))
	for c, b := range backends {
		out[b.Name] = c
	}
	return out
}

func weights(route *unstructured.Unstructured, names map[string]string) (map[string]int64, error) {
	list, err := refs(route)
	if err != nil {
		return nil, err
	}
	out := map[string]int64{}
	for _, ref := range list {
		name, _ := ref["name"].(string)
		cluster, ok := names[name]
		if !ok {
			continue
		}
		w := int64(1)
		if v, ok := ref["weight"]; ok {
			if w, err = toInt64(v); err != nil {
				return nil, fmt.Errorf("backendRef %s: weight: %w", name, err)
			}
		}
		out[cluster] = w
	}
	return out, nil
}

// SetWeights — выставить веса кластеров на маршруте; кластеры не из desired не трогаются.
// Возвращает веса после записи и признак, что маршрут изменён. Update с resourceVersion:
// при конкурентной правке — конфликт, повтор в следующем цикле.
func (r *Route) SetWeights(ctx context.Context, backends map[string]Backend, desired map[string]int64) (map[string]int64, bool, error) {
	route, err := r.get(ctx)
	if err != nil {
		return nil, false, err
	}
	names := byName(backends)
	list, err := refs(route)
	if err != nil {
		return nil, false, err
	}
	changed := false
	for _, ref := range list {
		name, _ := ref["name"].(string)
		cluster, ok := names[name]
		if !ok {
			continue
		}
		want, ok := desired[cluster]
		if !ok {
			continue
		}
		cur, has := ref["weight"]
		if c, err := toInt64(cur); has && err == nil && c == want {
			continue
		}
		ref["weight"] = want
		changed = true
	}
	if changed {
		if err := r.Client.Update(ctx, route); err != nil {
			return nil, false, fmt.Errorf("update httproute %s/%s: %w", r.Namespace, r.Name, err)
		}
	}
	w, err := weights(route, names)
	return w, changed, err
}
