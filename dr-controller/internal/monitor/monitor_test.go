package monitor

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http/httptest"
	"sort"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"

	"github.com/hanglider/cursach/dr-controller/internal/fsm"
	"github.com/hanglider/cursach/dr-controller/internal/gateway"
	"github.com/hanglider/cursach/dr-controller/internal/probe"
	"github.com/hanglider/cursach/dr-controller/internal/registry"
)

// --- фейки ---------------------------------------------------------------------------

type fakeRegistry struct {
	down      bool // каталог недоступен
	statuses  map[string]string
	incidents map[int64]*registry.Incident
	nextID    int64
	setCalls  []string
}

func newRegistry() *fakeRegistry {
	return &fakeRegistry{
		statuses:  map[string]string{"cluster-a": "healthy", "cluster-b": "healthy"},
		incidents: map[int64]*registry.Incident{},
	}
}

var errUnavailable = errors.New("unavailable")

func (r *fakeRegistry) Clusters(context.Context) ([]registry.Cluster, error) {
	if r.down {
		return nil, errUnavailable
	}
	var out []registry.Cluster
	for id, s := range r.statuses {
		out = append(out, registry.Cluster{ID: id, APIEndpoint: "https://" + id + ":6443", SloStatus: s})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out, nil
}

func (r *fakeRegistry) SetStatus(_ context.Context, id, status string) error {
	if r.down {
		return errUnavailable
	}
	r.statuses[id] = status
	r.setCalls = append(r.setCalls, id+"="+status)
	return nil
}

func (r *fakeRegistry) OpenIncident(_ context.Context, inc registry.Incident) (int64, error) {
	if r.down {
		return 0, errUnavailable
	}
	r.nextID++
	inc.ID = r.nextID
	inc.Details = copyDetails(inc.Details)
	r.incidents[inc.ID] = &inc
	return inc.ID, nil
}

func (r *fakeRegistry) UpdateIncident(_ context.Context, inc registry.Incident) error {
	if r.down {
		return errUnavailable
	}
	cur, ok := r.incidents[inc.ID]
	if !ok {
		return errors.New("404")
	}
	cur.PeakState = inc.PeakState
	for _, p := range []struct {
		dst **time.Time
		src *time.Time
	}{
		{&cur.FailoverStartedAt, inc.FailoverStartedAt},
		{&cur.FailoverCompletedAt, inc.FailoverCompletedAt},
		{&cur.RecoveryStartedAt, inc.RecoveryStartedAt},
		{&cur.RecoveryCompletedAt, inc.RecoveryCompletedAt},
	} {
		if p.src != nil {
			v := *p.src
			*p.dst = &v
		}
	}
	for k, v := range inc.Details {
		cur.Details[k] = v
	}
	return nil
}

func (r *fakeRegistry) OpenIncidents(_ context.Context, cluster string) ([]registry.Incident, error) {
	if r.down {
		return nil, errUnavailable
	}
	var out []registry.Incident
	for _, inc := range r.incidents {
		if inc.ClusterID == cluster && inc.RecoveryCompletedAt == nil {
			c := *inc
			c.Details = copyDetails(inc.Details)
			out = append(out, c)
		}
	}
	return out, nil
}

func copyDetails(d map[string]any) map[string]any {
	out := map[string]any{}
	for k, v := range d {
		out[k] = v
	}
	return out
}

func (r *fakeRegistry) only(t *testing.T) *registry.Incident {
	t.Helper()
	if len(r.incidents) != 1 {
		t.Fatalf("expected exactly one incident, got %d", len(r.incidents))
	}
	for _, inc := range r.incidents {
		return inc
	}
	return nil
}

type fakeRoute struct {
	weights map[string]int64
	updates int
	fail    bool
}

func (r *fakeRoute) Backends(context.Context) (map[string]gateway.Backend, error) {
	return map[string]gateway.Backend{
		"cluster-a": {Name: "app-cluster-a", Cluster: "cluster-a", Addr: "a:31998"},
		"cluster-b": {Name: "app-cluster-b", Cluster: "cluster-b", Addr: "b:31998"},
	}, nil
}

func (r *fakeRoute) Weights(context.Context, map[string]gateway.Backend) (map[string]int64, error) {
	out := map[string]int64{}
	for k, v := range r.weights {
		out[k] = v
	}
	return out, nil
}

func (r *fakeRoute) SetWeights(ctx context.Context, b map[string]gateway.Backend, desired map[string]int64) (map[string]int64, bool, error) {
	if r.fail {
		return nil, false, errUnavailable
	}
	changed := false
	for k, v := range desired {
		if r.weights[k] != v {
			r.weights[k] = v
			changed = true
		}
	}
	if changed {
		r.updates++
	}
	w, _ := r.Weights(ctx, b)
	return w, changed, nil
}

type fakeProber struct {
	svc, api, smoke map[string]bool // по адресу / кластеру
	smokeCalls      int
}

func (p *fakeProber) Svc(_ context.Context, addr string) probe.Result {
	if p.svc[addr] {
		return probe.Result{OK: true}
	}
	return probe.Result{Detail: "connection refused"}
}

func (p *fakeProber) API(_ context.Context, cluster, _ string) probe.Result {
	return probe.Result{OK: p.api[cluster], Detail: map[bool]string{false: "HTTP 500"}[p.api[cluster]]}
}

func (p *fakeProber) Smoke(_ context.Context, addr string, n int) probe.Result {
	p.smokeCalls++
	if p.smoke[addr] && n == 10 {
		return probe.Result{OK: true}
	}
	return probe.Result{Detail: "request 1/10: HTTP 502"}
}

type fakeSLI struct {
	rates map[string]float64
	err   error
}

func (s *fakeSLI) ErrorRates(context.Context) (map[string]float64, error) { return s.rates, s.err }

type harness struct {
	m      *Monitor
	reg    *fakeRegistry
	route  *fakeRoute
	prober *fakeProber
	sli    *fakeSLI
	now    time.Time
}

var t0 = time.Date(2026, 9, 24, 12, 0, 0, 0, time.UTC)

func newHarness(mode string) *harness {
	h := &harness{
		reg:   newRegistry(),
		route: &fakeRoute{weights: map[string]int64{"cluster-a": 50, "cluster-b": 50}},
		prober: &fakeProber{
			svc:   map[string]bool{"a:31998": true, "b:31998": true},
			api:   map[string]bool{"cluster-a": true, "cluster-b": true},
			smoke: map[string]bool{"a:31998": true, "b:31998": true},
		},
		sli: &fakeSLI{rates: map[string]float64{"cluster-a": 0, "cluster-b": 0.01}},
		now: t0,
	}
	cfg := Config{Mode: mode, Interval: 10 * time.Second, ErrorRateThreshold: 0.05, NominalWeight: 50,
		SmokeRequests: 10, FSM: fsm.Defaults()}
	log := slog.New(slog.NewJSONHandler(io.Discard, nil))
	h.m = New(h.reg, h.route, h.prober, h.sli, cfg, NewMetrics(prometheus.NewRegistry()), log)
	h.m.Now = func() time.Time { return h.now }
	return h
}

// cycle — один цикл; часы идут на период опроса.
func (h *harness) cycle(n int) {
	for range n {
		h.m.Cycle(context.Background())
		h.now = h.now.Add(10 * time.Second)
	}
}

func (h *harness) state(id string) fsm.State { return h.m.Status().Clusters[id].State }

// --- сценарии -------------------------------------------------------------------------

// Сценарий 1 целиком: отказ app в cluster-b → DEGRADED → DOWN (вес 0, down в каталоге)
// → возврат → smoke → ступени 5 → 25 → 50 → HEALTHY; инцидент со всеми фазами.
func TestFailoverAndRecovery(t *testing.T) {
	h := newHarness(ModeActive)
	h.cycle(2)
	if h.state("cluster-b") != fsm.Healthy || len(h.reg.incidents) != 0 || h.route.updates != 0 {
		t.Fatalf("steady state disturbed: %+v", h.m.Status())
	}

	h.prober.svc["b:31998"] = false
	h.cycle(1) // t0+20: DEGRADED
	inc := h.reg.only(t)
	if h.state("cluster-b") != fsm.Degraded || !inc.DetectedAt.Equal(t0.Add(20*time.Second)) {
		t.Fatalf("detection: %v %+v", h.state("cluster-b"), inc)
	}
	if h.route.weights["cluster-b"] != 50 {
		t.Fatal("DEGRADED must not move traffic")
	}
	h.cycle(2)
	if h.state("cluster-b") != fsm.Degraded {
		t.Fatal("DOWN too early")
	}
	h.cycle(1) // t0+50: четвёртая неудачная проба — DOWN
	if h.state("cluster-b") != fsm.Down || h.route.weights["cluster-b"] != 0 || h.route.weights["cluster-a"] != 50 {
		t.Fatalf("failover: %v %v", h.state("cluster-b"), h.route.weights)
	}
	if h.reg.statuses["cluster-b"] != "down" {
		t.Fatalf("registry status: %v", h.reg.statuses)
	}
	inc = h.reg.only(t)
	if inc.PeakState != "down" || !inc.FailoverStartedAt.Equal(t0.Add(50*time.Second)) ||
		inc.FailoverCompletedAt == nil || count(inc.Details["failovers"]) != 1 {
		t.Fatalf("incident after failover: %+v", inc)
	}

	// Сервис вернулся, но smoke-тест не проходит — остаёмся в DOWN.
	h.prober.svc["b:31998"] = true
	h.prober.smoke["b:31998"] = false
	h.cycle(1)
	if h.state("cluster-b") != fsm.Down || h.route.weights["cluster-b"] != 0 {
		t.Fatal("recovery without smoke test")
	}
	h.prober.smoke["b:31998"] = true
	h.cycle(1) // RECOVERING: 10% номинала
	if h.state("cluster-b") != fsm.Recovering || h.route.weights["cluster-b"] != 5 {
		t.Fatalf("recovering: %v %v", h.state("cluster-b"), h.route.weights)
	}
	if h.reg.statuses["cluster-b"] != "healthy" {
		t.Fatalf("down must be cleared on RECOVERING: %v", h.reg.statuses)
	}
	var seen []int64
	for range 6 {
		h.cycle(1)
		seen = append(seen, h.route.weights["cluster-b"])
	}
	want := []int64{5, 25, 25, 50, 50, 50}
	for i := range want {
		if seen[i] != want[i] {
			t.Fatalf("recovery weights: got %v, want %v", seen, want)
		}
	}
	if h.state("cluster-b") != fsm.Healthy {
		t.Fatalf("expected HEALTHY, got %v", h.state("cluster-b"))
	}
	inc = h.reg.only(t)
	if inc.RecoveryStartedAt == nil || inc.RecoveryCompletedAt == nil ||
		!inc.RecoveryCompletedAt.After(*inc.RecoveryStartedAt) {
		t.Fatalf("recovery timestamps: %+v", inc)
	}
	if h.m.Status().Clusters["cluster-b"].IncidentID != 0 {
		t.Fatal("closed incident still attached")
	}
	if got := testutil.ToFloat64(h.m.Metrics.Transitions.WithLabelValues("cluster-b", "down", "recovering")); got != 1 {
		t.Fatalf("transition metric: %v", got)
	}
}

// Режим observe («без DR»): обнаружение и инцидент есть, трафик и каталог не трогаются.
func TestObserveModeDoesNotAct(t *testing.T) {
	h := newHarness(ModeObserve)
	h.prober.svc["b:31998"] = false
	h.cycle(5)
	if h.state("cluster-b") != fsm.Down {
		t.Fatalf("observe mode must still detect: %v", h.state("cluster-b"))
	}
	if h.route.updates != 0 || len(h.reg.setCalls) != 0 {
		t.Fatalf("observe mode acted: route updates %d, status calls %v", h.route.updates, h.reg.setCalls)
	}
	inc := h.reg.only(t)
	if inc.DRMode != ModeObserve || inc.FailoverStartedAt == nil || inc.FailoverCompletedAt != nil {
		t.Fatalf("observe incident: %+v", inc)
	}
	if h.m.Status().Clusters["cluster-b"].DesiredWeight != 0 {
		t.Fatal("desired weight must still be reported")
	}
}

// Инвариант 6: метрик нет (Thanos недоступен) — это не отказ; ни DEGRADED, ни failover.
func TestMissingMetricsIsNotAFault(t *testing.T) {
	h := newHarness(ModeActive)
	h.sli.err = errUnavailable
	h.cycle(10)
	for _, id := range []string{"cluster-a", "cluster-b"} {
		if h.state(id) != fsm.Healthy {
			t.Fatalf("%s: %v", id, h.state(id))
		}
	}
	if len(h.reg.incidents) != 0 || h.route.updates != 0 {
		t.Fatal("missing metrics caused an incident")
	}
	if got := testutil.ToFloat64(h.m.Metrics.Signal.WithLabelValues("cluster-a", "sli")); got != -1 {
		t.Fatalf("sli signal metric: %v", got)
	}
}

// Высокий error rate при живом сервисе — DEGRADED без failover (сценарий 2, ADR 0007);
// инцидент degraded закрывается после трёх чистых циклов.
func TestErrorRateDegradesWithoutFailover(t *testing.T) {
	h := newHarness(ModeActive)
	h.sli.rates["cluster-b"] = 0.3
	h.cycle(6)
	if h.state("cluster-b") != fsm.Degraded || h.route.updates != 0 || len(h.reg.setCalls) != 0 {
		t.Fatalf("error rate must only degrade: %v", h.m.Status())
	}
	h.sli.rates["cluster-b"] = 0.01
	h.cycle(3)
	inc := h.reg.only(t)
	if h.state("cluster-b") != fsm.Healthy || inc.PeakState != "degraded" || inc.RecoveryCompletedAt == nil ||
		inc.FailoverStartedAt != nil {
		t.Fatalf("degraded incident: %v %+v", h.state("cluster-b"), inc)
	}
}

// Все кластеры DOWN: последний backend не обнуляется, веса возвращаются к номиналу.
func TestPanicKeepsTrafficWhenAllDown(t *testing.T) {
	h := newHarness(ModeActive)
	h.prober.svc["b:31998"] = false
	h.cycle(4)
	if h.route.weights["cluster-b"] != 0 {
		t.Fatal("first failover did not happen")
	}
	h.prober.svc["a:31998"] = false
	h.cycle(4)
	if h.state("cluster-a") != fsm.Down || h.state("cluster-b") != fsm.Down {
		t.Fatal("both clusters must be DOWN")
	}
	if h.route.weights["cluster-a"] != 50 || h.route.weights["cluster-b"] != 50 || !h.m.Status().Panic {
		t.Fatalf("panic weights: %v", h.route.weights)
	}
	// Оба помечены down в каталоге: планировщик не размещает никуда — это правда.
	if h.reg.statuses["cluster-a"] != "down" || h.reg.statuses["cluster-b"] != "down" {
		t.Fatalf("statuses: %v", h.reg.statuses)
	}
	// failover_completed_at у cluster-a нет: вес не обнулялся.
	for _, inc := range h.reg.incidents {
		if inc.ClusterID == "cluster-a" && inc.FailoverCompletedAt != nil {
			t.Fatalf("panic must not record failover completion: %+v", inc)
		}
	}
}

// Рестарт DR посреди инцидента: кластер down в каталоге → старт в DOWN с тем же
// инцидентом; вес доводится до 0; новый инцидент не открывается.
func TestRestartResumesDownCluster(t *testing.T) {
	h := newHarness(ModeActive)
	h.reg.statuses["cluster-b"] = "down"
	detected := t0.Add(-time.Minute)
	h.reg.nextID = 41
	_, _ = h.reg.OpenIncident(context.Background(), registry.Incident{ClusterID: "cluster-b", DRMode: ModeActive,
		PeakState: "down", DetectedAt: detected, FailoverStartedAt: &detected, Details: map[string]any{"failovers": 1.0}})
	h.route.weights["cluster-b"] = 50 // вес успели вернуть руками — DR выведет снова
	h.prober.svc["b:31998"] = false
	h.cycle(1)
	if h.state("cluster-b") != fsm.Down || h.route.weights["cluster-b"] != 0 {
		t.Fatalf("resume: %v %v", h.state("cluster-b"), h.route.weights)
	}
	inc := h.reg.only(t)
	if inc.ID != 42 || inc.FailoverCompletedAt == nil || inc.Details["resumed_at"] == nil {
		t.Fatalf("resumed incident: %+v", inc)
	}
	if h.m.Status().Clusters["cluster-b"].IncidentID != 42 {
		t.Fatal("status must show the resumed incident")
	}
}

// Каталог недоступен во время отказа: трафик всё равно переключается, а статус и
// инцидент записываются, когда каталог вернётся (level-triggered).
func TestRegistryOutageDoesNotBlockFailover(t *testing.T) {
	h := newHarness(ModeActive)
	h.cycle(1) // инвентарь в кэше
	h.reg.down = true
	h.prober.svc["b:31998"] = false
	h.cycle(4)
	if h.state("cluster-b") != fsm.Down || h.route.weights["cluster-b"] != 0 {
		t.Fatalf("failover without registry: %v %v", h.state("cluster-b"), h.route.weights)
	}
	if len(h.reg.incidents) != 0 {
		t.Fatal("registry is down, nothing can be written")
	}
	h.reg.down = false
	h.cycle(1)
	inc := h.reg.only(t)
	if h.reg.statuses["cluster-b"] != "down" || inc.PeakState != "down" || inc.FailoverCompletedAt == nil ||
		!inc.DetectedAt.Equal(t0.Add(10*time.Second)) {
		t.Fatalf("late sync: %v %+v", h.reg.statuses, inc)
	}
}

// Отказ API-сервера при живом сервисе — DEGRADED, не failover (ADR 0007).
func TestAPIDownOnlyDegrades(t *testing.T) {
	h := newHarness(ModeActive)
	h.prober.api["cluster-a"] = false
	h.cycle(8)
	if h.state("cluster-a") != fsm.Degraded || h.route.updates != 0 {
		t.Fatalf("api down: %v, route updates %d", h.state("cluster-a"), h.route.updates)
	}
}

func TestStatusHandlerAndHealth(t *testing.T) {
	h := newHarness(ModeActive)
	if err := h.m.Ready(nil); err == nil {
		t.Fatal("not ready before first cycle")
	}
	h.cycle(1)
	if err := h.m.Ready(nil); err != nil {
		t.Fatal(err)
	}
	rec := httptest.NewRecorder()
	h.m.StatusHandler().ServeHTTP(rec, httptest.NewRequest("GET", "/status", nil))
	var s Snapshot
	if err := json.Unmarshal(rec.Body.Bytes(), &s); err != nil {
		t.Fatal(err)
	}
	if s.Mode != ModeActive || s.Cycles != 1 || s.Nominal != 50 || s.Clusters["cluster-a"].State != fsm.Healthy ||
		*s.Clusters["cluster-a"].Weight != 50 || s.Clusters["cluster-b"].Detail.ErrorRate == nil {
		t.Fatalf("status: %s", rec.Body.String())
	}
	if err := h.m.Healthy(nil); err != nil {
		t.Fatal(err)
	}
	h.now = h.now.Add(time.Minute)
	if err := h.m.Healthy(nil); err == nil {
		t.Fatal("stale loop must fail liveness")
	}
}
