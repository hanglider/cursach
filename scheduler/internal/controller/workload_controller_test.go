package controller

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/tools/events"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	"github.com/hanglider/cursach/scheduler/api/v1alpha1"
	"github.com/hanglider/cursach/scheduler/internal/placement"
	"github.com/hanglider/cursach/scheduler/internal/registry"
	"github.com/hanglider/cursach/scheduler/internal/scoring"
)

const kyvernoDenial = "admission webhook \"validate.kyverno.svc-fail\" denied the request: \n\n" +
	"resource Deployment/workloads/w-1 was blocked due to the following policies \n\n" +
	"require-certification:\n  certifications-present: 'federation.spbu/require-certification=pci-dss not satisfied: cluster cluster-b has certifications iso-27001'\n"

var now = time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC)

func f(v float64) *float64 { return &v }

func newScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	s := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(s); err != nil {
		t.Fatal(err)
	}
	if err := v1alpha1.AddToScheme(s); err != nil {
		t.Fatal(err)
	}
	return s
}

// --- фейки ---------------------------------------------------------------------

type fakeRegistry struct {
	eligible    *registry.Eligible
	eligibleErr error
	decisions   []registry.Decision
	patches     map[int64]string
	violations  []registry.Violation
}

func (r *fakeRegistry) Eligible(context.Context, registry.Requirements) (*registry.Eligible, error) {
	if r.eligibleErr != nil {
		return nil, r.eligibleErr
	}
	return r.eligible, nil
}

func (r *fakeRegistry) CreateDecision(_ context.Context, d registry.Decision) (int64, error) {
	r.decisions = append(r.decisions, d)
	return int64(len(r.decisions)), nil
}

func (r *fakeRegistry) PatchDecision(_ context.Context, id int64, outcome string) error {
	if r.patches == nil {
		r.patches = map[int64]string{}
	}
	r.patches[id] = outcome
	return nil
}

func (r *fakeRegistry) CreateViolation(_ context.Context, v registry.Violation) error {
	r.violations = append(r.violations, v)
	return nil
}

type fakeClusters struct {
	clients map[string]client.Client
	addr    map[string]string
	err     error
}

func (c *fakeClusters) Get(id string) (client.Client, error) {
	if c.err != nil {
		return nil, c.err
	}
	cl, ok := c.clients[id]
	if !ok {
		return nil, errors.New("no kubeconfig for " + id)
	}
	return cl, nil
}

func (c *fakeClusters) NodeAddress(_ context.Context, id string) (string, error) {
	if c.err != nil {
		return "", c.err
	}
	return c.addr[id], nil
}

type env struct {
	control  client.Client
	gateway  client.Client
	clusters *fakeClusters
	reg      *fakeRegistry
	rec      *Reconciler
	recorder *events.FakeRecorder
}

func metrics(err, p95, sat float64) *registry.Metrics {
	return &registry.Metrics{ObservedAt: now.Add(-5 * time.Second), ErrorRate: f(err), LatencyP95Ms: f(p95), Saturation: f(sat)}
}

func setup(t *testing.T, mode string, reg *fakeRegistry, workloads ...*v1alpha1.Workload) *env {
	t.Helper()
	s := newScheme(t)
	objs := make([]client.Object, 0, len(workloads))
	for _, w := range workloads {
		objs = append(objs, w)
	}
	control := fake.NewClientBuilder().WithScheme(s).WithObjects(objs...).WithStatusSubresource(&v1alpha1.Workload{}).Build()
	gateway := fake.NewClientBuilder().WithScheme(s).Build()
	clusters := &fakeClusters{
		clients: map[string]client.Client{
			"cluster-a": fake.NewClientBuilder().WithScheme(s).Build(),
			"cluster-b": fake.NewClientBuilder().WithScheme(s).Build(),
		},
		addr: map[string]string{"cluster-a": "192.168.117.3", "cluster-b": "192.168.117.4"},
	}
	recorder := events.NewFakeRecorder(64)
	cfg := Config{
		Mode: mode, Params: scoring.DefaultParams(), RetryInterval: 30 * time.Second, FinalizeTimeout: 2 * time.Minute,
		WorkloadNamespace: "workloads", Image: "federation/test-service:dev", BackendURL: "http://backend.workloads.svc:8080",
		Domain: "fed.local", GatewayNamespace: "gateway", GatewayName: "stand",
	}
	rec := &Reconciler{
		Client: control, Gateway: gateway, Clusters: clusters, Registry: reg, Events: recorder,
		Config: cfg, RR: &scoring.RoundRobin{}, Now: func() time.Time { return now },
		Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
	return &env{control: control, gateway: gateway, clusters: clusters, reg: reg, rec: rec, recorder: recorder}
}

func workload(name string, ann map[string]string) *v1alpha1.Workload {
	return &v1alpha1.Workload{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "federation", Annotations: ann, Generation: 1},
		Spec:       v1alpha1.WorkloadSpec{Port: 30001},
	}
}

func (e *env) reconcile(t *testing.T, name string) ctrl.Result {
	t.Helper()
	res, err := e.rec.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: "federation", Name: name}})
	if err != nil {
		t.Fatalf("reconcile %s: %v", name, err)
	}
	return res
}

func (e *env) get(t *testing.T, name string) *v1alpha1.Workload {
	t.Helper()
	var w v1alpha1.Workload
	if err := e.control.Get(context.Background(), types.NamespacedName{Namespace: "federation", Name: name}, &w); err != nil {
		t.Fatalf("get %s: %v", name, err)
	}
	return &w
}

func gatewayObj(gvkKind string, name string) *unstructured.Unstructured {
	u := &unstructured.Unstructured{}
	if gvkKind == "Backend" {
		u.SetGroupVersionKind(placement.BackendGVK)
	} else {
		u.SetGroupVersionKind(placement.HTTPRouteGVK)
	}
	u.SetName(name)
	u.SetNamespace("gateway")
	return u
}

func exists(t *testing.T, c client.Client, obj client.Object) bool {
	t.Helper()
	err := c.Get(context.Background(), client.ObjectKeyFromObject(obj), obj)
	if err == nil {
		return true
	}
	if apierrors.IsNotFound(err) {
		return false
	}
	t.Fatalf("get %T: %v", obj, err)
	return false
}

// --- сценарии ------------------------------------------------------------------

func TestMetaPlacesInBestClusterAndRecordsAllCandidates(t *testing.T) {
	reg := &fakeRegistry{eligible: &registry.Eligible{Eligible: []registry.Entry{
		{ID: "cluster-a", SloStatus: "degraded", Metrics: metrics(0.3, 350, 0.5)},
		{ID: "cluster-b", SloStatus: "healthy", Metrics: metrics(0.01, 80, 0.3)},
	}}}
	e := setup(t, ModeMeta, reg, workload("w-1", map[string]string{"federation.spbu/require-region": ""}))

	e.reconcile(t, "w-1")
	w := e.get(t, "w-1")
	if w.Status.Phase != v1alpha1.PhasePlaced || w.Status.Cluster != "cluster-b" || w.Status.Scheduler != "meta" {
		t.Fatalf("status %+v", w.Status)
	}
	if w.Status.Host != "w-1.fed.local" || w.Status.NodeAddress != "192.168.117.4" || *w.Status.DecisionID != 1 || w.Status.PlacedAt == nil {
		t.Fatalf("status %+v", w.Status)
	}
	if len(w.Finalizers) != 1 || w.Finalizers[0] != v1alpha1.Finalizer {
		t.Fatalf("finalizers %v", w.Finalizers)
	}

	if len(reg.decisions) != 1 {
		t.Fatalf("decisions %d", len(reg.decisions))
	}
	d := reg.decisions[0]
	if d.Outcome != "placed" || *d.ChosenCluster != "cluster-b" || d.WorkloadRef != "workloads/w-1" || d.SchedulerKind != "meta" {
		t.Fatalf("decision %+v", d)
	}
	if len(d.Candidates) != 2 || d.Candidates[0].Score == nil || d.Candidates[1].Score == nil || *d.Candidates[1].Score <= *d.Candidates[0].Score {
		t.Fatalf("candidates %+v", d.Candidates)
	}
	if d.Scoring["formula"] != "adr-0005" || d.Candidates[0].Terms["error"] != 0.7 {
		t.Fatalf("scoring %v terms %v", d.Scoring, d.Candidates[0].Terms)
	}

	clB := e.clusters.clients["cluster-b"]
	dep := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: "w-1", Namespace: "workloads"}}
	if !exists(t, clB, dep) || !exists(t, clB, &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: "w-1", Namespace: "workloads"}}) {
		t.Fatal("deployment and service must exist in cluster-b")
	}
	if dep.Labels[placement.LabelDecisionID] != "1" || dep.Labels[placement.LabelScheduler] != "meta" {
		t.Fatalf("deployment labels %v", dep.Labels)
	}
	if exists(t, e.clusters.clients["cluster-a"], &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: "w-1", Namespace: "workloads"}}) {
		t.Fatal("nothing must be created in cluster-a")
	}
	if !exists(t, e.gateway, gatewayObj("Backend", "w-1")) || !exists(t, e.gateway, gatewayObj("HTTPRoute", "w-1")) {
		t.Fatal("backend and httproute must exist on the gateway")
	}

	// Терминальная фаза: повторный reconcile не порождает решений.
	e.reconcile(t, "w-1")
	if len(reg.decisions) != 1 {
		t.Fatalf("placed workload must not be rescheduled, decisions %d", len(reg.decisions))
	}
}

func TestNaiveRoundRobinWithoutScores(t *testing.T) {
	reg := &fakeRegistry{eligible: &registry.Eligible{Eligible: []registry.Entry{
		{ID: "cluster-b", SloStatus: "healthy", Metrics: metrics(0.0, 10, 0.1)},
		{ID: "cluster-a", SloStatus: "degraded", Metrics: metrics(0.5, 500, 0.9)},
	}}}
	e := setup(t, ModeNaive, reg, workload("w-1", nil), workload("w-2", nil), workload("w-3", nil))
	for _, n := range []string{"w-1", "w-2", "w-3"} {
		e.reconcile(t, n)
	}
	got := []string{e.get(t, "w-1").Status.Cluster, e.get(t, "w-2").Status.Cluster, e.get(t, "w-3").Status.Cluster}
	if got[0] != "cluster-a" || got[1] != "cluster-b" || got[2] != "cluster-a" {
		t.Fatalf("round-robin order %v", got)
	}
	for _, d := range reg.decisions {
		if d.SchedulerKind != "naive" || d.Scoring != nil || len(d.Candidates) != 2 {
			t.Fatalf("decision %+v", d)
		}
		for _, c := range d.Candidates {
			if c.Score != nil || c.Metrics == nil {
				t.Fatalf("naive candidate must keep metrics snapshot and no score: %+v", c)
			}
		}
	}
}

func TestAdmissionDenialBecomesRejectedDecisionAndViolation(t *testing.T) {
	reg := &fakeRegistry{eligible: &registry.Eligible{Eligible: []registry.Entry{{ID: "cluster-b", SloStatus: "healthy"}}}}
	e := setup(t, ModeMeta, reg, workload("w-1", map[string]string{"federation.spbu/require-certification": "pci-dss"}))
	e.clusters.clients["cluster-b"] = interceptor.NewClient(e.clusters.clients["cluster-b"].(client.WithWatch), interceptor.Funcs{
		Create: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.CreateOption) error {
			if _, ok := obj.(*appsv1.Deployment); ok {
				return apierrors.NewBadRequest(kyvernoDenial)
			}
			return c.Create(ctx, obj, opts...)
		},
	})

	e.reconcile(t, "w-1")
	w := e.get(t, "w-1")
	if w.Status.Phase != v1alpha1.PhaseRejected || w.Status.Cluster != "cluster-b" {
		t.Fatalf("status %+v", w.Status)
	}
	if reg.patches[1] != "rejected" {
		t.Fatalf("decision must be patched to rejected, got %v", reg.patches)
	}
	if len(reg.violations) != 1 {
		t.Fatalf("violations %d", len(reg.violations))
	}
	v := reg.violations[0]
	if v.PolicyName != "require-certification" || v.ClusterID != "cluster-b" || v.WorkloadRef != "workloads/w-1" || v.DetectedBy != "kyverno" {
		t.Fatalf("violation %+v", v)
	}
	if v.Details["reported_via"] != "scheduler" || v.Details["rule"] != "certifications-present" || v.Requirements.Certifications[0] != "pci-dss" {
		t.Fatalf("violation details %+v reqs %+v", v.Details, v.Requirements)
	}
	if exists(t, e.clusters.clients["cluster-b"], &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: "w-1", Namespace: "workloads"}}) ||
		exists(t, e.gateway, gatewayObj("HTTPRoute", "w-1")) {
		t.Fatal("rejected workload must not get a service or a route")
	}
	e.reconcile(t, "w-1")
	if len(reg.decisions) != 1 {
		t.Fatal("rejected is terminal")
	}
}

func TestNoCandidatesIsRecordedAndRetried(t *testing.T) {
	reg := &fakeRegistry{eligible: &registry.Eligible{Rejected: []registry.Entry{
		{ID: "cluster-a", SloStatus: "down", FilteredBy: "down"},
		{ID: "cluster-b", SloStatus: "healthy", FilteredBy: "certification:pci-dss"},
	}}}
	e := setup(t, ModeMeta, reg, workload("w-1", nil))
	res := e.reconcile(t, "w-1")
	if res.RequeueAfter != 30*time.Second {
		t.Fatalf("requeue %v", res)
	}
	w := e.get(t, "w-1")
	if w.Status.Phase != v1alpha1.PhaseNoCandidates || *w.Status.DecisionID != 1 || w.Status.Cluster != "" {
		t.Fatalf("status %+v", w.Status)
	}
	d := reg.decisions[0]
	if d.Outcome != "no_candidates" || d.ChosenCluster != nil || len(d.Candidates) != 2 || *d.Candidates[1].FilteredBy != "certification:pci-dss" {
		t.Fatalf("decision %+v", d)
	}
	// Повтор: каталог теперь отдаёт кандидата — размещение состоится, решение новое.
	reg.eligible = &registry.Eligible{Eligible: []registry.Entry{{ID: "cluster-a", SloStatus: "healthy"}}}
	e.reconcile(t, "w-1")
	if e.get(t, "w-1").Status.Phase != v1alpha1.PhasePlaced || len(reg.decisions) != 2 {
		t.Fatalf("retry: phase %s decisions %d", e.get(t, "w-1").Status.Phase, len(reg.decisions))
	}
}

func TestRegistryUnavailableSetsErrorAndReturnsError(t *testing.T) {
	reg := &fakeRegistry{eligibleErr: errors.New("connection refused")}
	e := setup(t, ModeMeta, reg, workload("w-1", nil))
	_, err := e.rec.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: "federation", Name: "w-1"}})
	if err == nil {
		t.Fatal("registry failure must be returned for backoff")
	}
	w := e.get(t, "w-1")
	if w.Status.Phase != v1alpha1.PhaseError || w.Status.Message == "" || len(reg.decisions) != 0 {
		t.Fatalf("status %+v decisions %d", w.Status, len(reg.decisions))
	}
}

func TestInvalidRequirementsIsTerminalError(t *testing.T) {
	reg := &fakeRegistry{}
	e := setup(t, ModeMeta, reg, workload("w-1", map[string]string{"federation.spbu/require-cpu": "many"}))
	res := e.reconcile(t, "w-1")
	if res.RequeueAfter != 0 || e.get(t, "w-1").Status.Phase != v1alpha1.PhaseError || len(reg.decisions) != 0 {
		t.Fatalf("res %v status %+v", res, e.get(t, "w-1").Status)
	}
}

func TestFinalizerRemovesEverything(t *testing.T) {
	reg := &fakeRegistry{eligible: &registry.Eligible{Eligible: []registry.Entry{{ID: "cluster-a", SloStatus: "healthy"}}}}
	e := setup(t, ModeNaive, reg, workload("w-1", nil))
	e.reconcile(t, "w-1")
	w := e.get(t, "w-1")
	if err := e.control.Delete(context.Background(), w); err != nil {
		t.Fatal(err)
	}
	w = e.get(t, "w-1") // финализатор держит объект
	if w.DeletionTimestamp.IsZero() {
		t.Fatal("deletion timestamp must be set while finalizer is present")
	}
	e.reconcile(t, "w-1")
	clA := e.clusters.clients["cluster-a"]
	if exists(t, clA, &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: "w-1", Namespace: "workloads"}}) ||
		exists(t, clA, &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: "w-1", Namespace: "workloads"}}) {
		t.Fatal("cluster objects must be deleted")
	}
	if exists(t, e.gateway, gatewayObj("Backend", "w-1")) || exists(t, e.gateway, gatewayObj("HTTPRoute", "w-1")) {
		t.Fatal("gateway objects must be deleted")
	}
	var gone v1alpha1.Workload
	if err := e.control.Get(context.Background(), types.NamespacedName{Namespace: "federation", Name: "w-1"}, &gone); !apierrors.IsNotFound(err) {
		t.Fatalf("workload must be gone, err %v", err)
	}
}

func TestFinalizerWaitsForUnreachableClusterThenGivesUp(t *testing.T) {
	reg := &fakeRegistry{eligible: &registry.Eligible{Eligible: []registry.Entry{{ID: "cluster-a", SloStatus: "healthy"}}}}
	e := setup(t, ModeNaive, reg, workload("w-1", nil))
	e.reconcile(t, "w-1")
	if err := e.control.Delete(context.Background(), e.get(t, "w-1")); err != nil {
		t.Fatal(err)
	}
	e.clusters.err = errors.New("cluster-a: dial tcp: i/o timeout")
	deleted := e.get(t, "w-1").DeletionTimestamp.Time
	e.rec.Now = func() time.Time { return deleted.Add(30 * time.Second) }
	res := e.reconcile(t, "w-1")
	if res.RequeueAfter != 10*time.Second {
		t.Fatalf("must retry while within finalize timeout, got %v", res)
	}
	if e.get(t, "w-1").Name == "" {
		t.Fatal("workload must still exist")
	}
	e.rec.Now = func() time.Time { return deleted.Add(3 * time.Minute) }
	e.reconcile(t, "w-1")
	var gone v1alpha1.Workload
	if err := e.control.Get(context.Background(), types.NamespacedName{Namespace: "federation", Name: "w-1"}, &gone); !apierrors.IsNotFound(err) {
		t.Fatalf("finalizer must be abandoned after timeout, err %v", err)
	}
}
