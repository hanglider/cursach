package gateway

import (
	"context"
	"testing"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

// Объекты в форме render-app-route.sh.
func backend(cluster, ip string) *unstructured.Unstructured {
	u := &unstructured.Unstructured{Object: map[string]any{
		"metadata": map[string]any{
			"name": "app-" + cluster, "namespace": "gateway",
			"labels": map[string]any{LabelWorkload: "app", LabelCluster: cluster},
		},
		"spec": map[string]any{"endpoints": []any{
			map[string]any{"ip": map[string]any{"address": ip, "port": int64(31998)}},
		}},
	}}
	u.SetGroupVersionKind(BackendGVK)
	return u
}

func appRoute() *unstructured.Unstructured {
	ref := func(name string) map[string]any {
		return map[string]any{"group": BackendGVK.Group, "kind": "Backend", "name": name, "weight": int64(50)}
	}
	u := &unstructured.Unstructured{Object: map[string]any{
		"metadata": map[string]any{"name": "app", "namespace": "gateway"},
		"spec": map[string]any{
			"hostnames": []any{"app.fed.local"},
			"rules": []any{map[string]any{"backendRefs": []any{
				ref("app-cluster-a"), ref("app-cluster-b"),
				map[string]any{"name": "foreign"}, // чужой backendRef не трогаем
			}}},
		},
	}}
	u.SetGroupVersionKind(HTTPRouteGVK)
	return u
}

func newRoute(t *testing.T, objs ...client.Object) (*Route, client.Client) {
	t.Helper()
	c := fake.NewClientBuilder().WithScheme(runtime.NewScheme()).WithObjects(objs...).Build()
	return &Route{Client: c, Namespace: "gateway", Name: "app", Workload: "app"}, c
}

func TestBackendsAndWeights(t *testing.T) {
	r, c := newRoute(t, backend("cluster-a", "192.168.117.4"), backend("cluster-b", "192.168.117.3"), appRoute())
	ctx := context.Background()
	backends, err := r.Backends(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if backends["cluster-b"].Addr != "192.168.117.3:31998" || backends["cluster-a"].Name != "app-cluster-a" {
		t.Fatalf("backends: %+v", backends)
	}
	w, err := r.Weights(ctx, backends)
	if err != nil || w["cluster-a"] != 50 || w["cluster-b"] != 50 || len(w) != 2 {
		t.Fatalf("weights: %v %v", w, err)
	}

	got, changed, err := r.SetWeights(ctx, backends, map[string]int64{"cluster-b": 0})
	if err != nil || !changed || got["cluster-b"] != 0 || got["cluster-a"] != 50 {
		t.Fatalf("set: %v %v %v", got, changed, err)
	}
	// Запись дошла до объекта; повтор с теми же весами маршрут не трогает.
	route := appRoute()
	if err := c.Get(ctx, client.ObjectKeyFromObject(route), route); err != nil {
		t.Fatal(err)
	}
	w, _ = weights(route, byName(backends))
	if w["cluster-b"] != 0 {
		t.Fatalf("stored weights: %v", w)
	}
	rv := route.GetResourceVersion()
	if _, changed, err := r.SetWeights(ctx, backends, map[string]int64{"cluster-b": 0, "cluster-a": 50}); err != nil || changed {
		t.Fatalf("no-op set: changed=%v err=%v", changed, err)
	}
	_ = c.Get(ctx, client.ObjectKeyFromObject(route), route)
	if route.GetResourceVersion() != rv {
		t.Fatal("no-op set must not update the route")
	}
	list, _ := refs(route)
	if list[2]["name"] != "foreign" || list[2]["weight"] != nil {
		t.Fatalf("foreign backendRef changed: %v", list[2])
	}
}

func TestMissingRoute(t *testing.T) {
	r, _ := newRoute(t, backend("cluster-a", "10.0.0.1"))
	backends, _ := r.Backends(context.Background())
	if _, err := r.Weights(context.Background(), backends); err == nil {
		t.Fatal("expected error without route")
	}
}
