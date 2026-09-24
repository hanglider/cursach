// Package monitor — цикл DR Controller'а (spec §4.6, architecture §6, ADR 0007, 0028):
// раз в 10 с по каждому кластеру федерации три сигнала (svc_ok, api_ok, sli_ok) → шаг
// автомата → действия.
//
// Действия выводятся из состояния, а не из перехода (level-triggered): каждый цикл
// монитор сводит веса маршрута app.fed.local и статус down в каталоге к тому, что
// следует из состояний автоматов. Поэтому неудачная запись повторяется в следующем
// цикле сама, а рестарт DR восстанавливает состояние из каталога и маршрута.
package monitor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"net/http"
	"sort"
	"sync"
	"time"

	"github.com/hanglider/cursach/dr-controller/internal/fsm"
	"github.com/hanglider/cursach/dr-controller/internal/gateway"
	"github.com/hanglider/cursach/dr-controller/internal/probe"
	"github.com/hanglider/cursach/dr-controller/internal/registry"
)

// Режимы DR (ADR 0028).
const (
	// ModeActive — DR переключает трафик и пишет down в каталог.
	ModeActive = "active"
	// ModeObserve — DR только наблюдает и пишет инциденты: сценарий 1 «без DR».
	ModeObserve = "observe"
)

// Registry — часть клиента каталога, нужная монитору.
type Registry interface {
	Clusters(ctx context.Context) ([]registry.Cluster, error)
	SetStatus(ctx context.Context, id, status string) error
	OpenIncident(ctx context.Context, inc registry.Incident) (int64, error)
	UpdateIncident(ctx context.Context, inc registry.Incident) error
	OpenIncidents(ctx context.Context, cluster string) ([]registry.Incident, error)
}

// Route — маршрут app.fed.local на шлюзе.
type Route interface {
	Backends(ctx context.Context) (map[string]gateway.Backend, error)
	Weights(ctx context.Context, backends map[string]gateway.Backend) (map[string]int64, error)
	SetWeights(ctx context.Context, backends map[string]gateway.Backend, desired map[string]int64) (map[string]int64, bool, error)
}

// Prober — HTTP-пробы (пакет probe).
type Prober interface {
	Svc(ctx context.Context, addr string) probe.Result
	API(ctx context.Context, cluster, endpoint string) probe.Result
	Smoke(ctx context.Context, addr string, n int) probe.Result
}

// SLISource — error rate кластеров из Thanos.
type SLISource interface {
	ErrorRates(ctx context.Context) (map[string]float64, error)
}

// Config — параметры DR; все пишутся в лог при старте.
type Config struct {
	Mode               string
	Interval           time.Duration // период опроса, ADR 0007: 10 с
	ErrorRateThreshold float64       // sli_ok: error rate ≤ порога, ADR 0007: 0.05
	NominalWeight      int64         // номинальный вес кластера на маршруте app, ADR 0011: 50
	SmokeRequests      int           // запросов в smoke-тесте, ADR 0007: 10
	FSM                fsm.Params
}

// Monitor — состояние DR: автомат и инцидент на кластер.
type Monitor struct {
	Registry Registry
	Route    Route
	Prober   Prober
	SLI      SLISource
	Config   Config
	Metrics  *Metrics
	Now      func() time.Time
	Log      *slog.Logger

	clusters  map[string]*cluster
	closed    []*incident // закрытые, но ещё не записанные в каталог
	inventory []registry.Cluster
	backends  map[string]gateway.Backend
	warned    map[string]bool
	panicked  bool

	mu       sync.RWMutex
	snapshot Snapshot
}

type cluster struct {
	id       string
	fsm      *fsm.FSM
	since    time.Time
	signals  fsm.Signals
	detail   signalDetail
	incident *incident
}

type signalDetail struct {
	Svc       string   `json:"svc,omitempty"`
	API       string   `json:"api,omitempty"`
	Smoke     string   `json:"smoke,omitempty"`
	ErrorRate *float64 `json:"error_rate,omitempty"`
}

type incident struct {
	rec   registry.Incident
	dirty bool
}

// New — монитор с пустым состоянием.
func New(reg Registry, route Route, prober Prober, sli SLISource, cfg Config, metrics *Metrics, log *slog.Logger) *Monitor {
	return &Monitor{
		Registry: reg, Route: route, Prober: prober, SLI: sli, Config: cfg, Metrics: metrics,
		Now: time.Now, Log: log,
		clusters: map[string]*cluster{}, backends: map[string]gateway.Backend{}, warned: map[string]bool{},
	}
}

// Run — цикл до отмены контекста; первый цикл сразу.
func (m *Monitor) Run(ctx context.Context) error {
	for _, mode := range []string{ModeActive, ModeObserve} {
		m.Metrics.Mode.WithLabelValues(mode).Set(boolGauge(mode == m.Config.Mode))
	}
	ticker := time.NewTicker(m.Config.Interval)
	defer ticker.Stop()
	for {
		cctx, cancel := context.WithTimeout(ctx, m.Config.Interval)
		m.Cycle(cctx)
		cancel()
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
		}
	}
}

func (m *Monitor) active() bool { return m.Config.Mode == ModeActive }

func (m *Monitor) stageError(stage string, err error) {
	m.Metrics.CycleErrors.WithLabelValues(stage).Inc()
	m.Log.Warn("cycle_error", "stage", stage, "err", err.Error())
}

type observation struct {
	svc, api probe.Result
}

// Cycle — один цикл опроса: сигналы → автоматы → веса → каталог → инциденты.
func (m *Monitor) Cycle(ctx context.Context) {
	started := m.Now()
	defer func() { m.Metrics.CycleDuration.Observe(m.Now().Sub(started).Seconds()) }()

	// 1. Инвентарь: кластеры из каталога, адреса app из Backend'ов шлюза. При ошибке —
	// последний известный: недоступный каталог не должен останавливать наблюдение.
	statuses := map[string]string{}
	registryOK := false
	if list, err := m.Registry.Clusters(ctx); err != nil {
		m.stageError("registry", err)
	} else {
		m.inventory, registryOK = list, true
		for _, c := range list {
			statuses[c.ID] = c.SloStatus
		}
	}
	if backends, err := m.Route.Backends(ctx); err != nil {
		m.stageError("backends", err)
	} else {
		m.backends = backends
	}
	// 2. SLI одним запросом на все кластеры; ошибка Thanos — «неизвестно» у всех.
	rates, err := m.SLI.ErrorRates(ctx)
	if err != nil {
		m.stageError("thanos", err)
		rates = map[string]float64{}
	}

	// 3. Пробы параллельно: цикл укладывается в таймаут одной пробы (2 с).
	var monitored []registry.Cluster
	for _, c := range m.inventory {
		if _, ok := m.backends[c.ID]; !ok {
			if !m.warned[c.ID] {
				m.Log.Warn("cluster_without_app_backend", "cluster", c.ID,
					"hint", "нет Backend app-<cluster> на шлюзе: кластер не наблюдается")
				m.warned[c.ID] = true
			}
			continue
		}
		m.warned[c.ID] = false
		monitored = append(monitored, c)
	}
	sort.Slice(monitored, func(i, j int) bool { return monitored[i].ID < monitored[j].ID })
	obs := make([]observation, len(monitored))
	var wg sync.WaitGroup
	for i, c := range monitored {
		addr := m.backends[c.ID].Addr
		wg.Add(2)
		go func() { defer wg.Done(); obs[i].svc = m.Prober.Svc(ctx, addr) }()
		go func() { defer wg.Done(); obs[i].api = m.Prober.API(ctx, c.ID, c.APIEndpoint) }()
	}
	wg.Wait()

	weights, werr := m.Route.Weights(ctx, m.backends)
	if werr != nil {
		m.stageError("route", werr)
	}

	// 4. Автоматы.
	for i, c := range monitored {
		st, ok := m.clusters[c.ID]
		if !ok {
			st = m.initCluster(ctx, c.ID, statuses[c.ID], registryOK, weights, werr == nil)
			m.clusters[c.ID] = st
		}
		sig := fsm.Signals{Svc: obs[i].svc.OK, API: obs[i].api.OK, SLI: fsm.SLIUnknown}
		detail := signalDetail{Svc: obs[i].svc.Detail, API: obs[i].api.Detail}
		if v, ok := rates[c.ID]; ok {
			rate := v
			detail.ErrorRate = &rate
			sig.SLI = fsm.SLIOK
			if v > m.Config.ErrorRateThreshold {
				sig.SLI = fsm.SLIBad
			}
		}
		if st.fsm.State() == fsm.Down && sig.Svc {
			smoke := m.Prober.Smoke(ctx, m.backends[c.ID].Addr, m.Config.SmokeRequests)
			sig.Smoke, detail.Smoke = smoke.OK, smoke.Detail
			if !smoke.OK {
				m.Log.Info("smoke_failed", "cluster", c.ID, "detail", smoke.Detail)
			}
		}
		st.signals, st.detail = sig, detail
		step := st.fsm.Step()
		prev := st.fsm.Next(sig)
		now := m.Now()
		switch {
		case prev != st.fsm.State():
			m.transition(st, prev, now)
		case st.fsm.State() == fsm.Recovering && st.fsm.Step() != step:
			m.Log.Info("recovery_step", "cluster", c.ID, "fraction", m.Config.FSM.Steps[st.fsm.Step()])
		}
	}

	// 5. Веса маршрута из состояний (только active).
	desired, panicMode := m.desired(monitored)
	m.Metrics.Panic.Set(boolGauge(panicMode))
	if panicMode != m.panicked {
		if panicMode {
			m.Log.Warn("panic", "reason", "все кластеры DOWN: веса не обнуляются, трафик распределяется по всем")
		} else {
			m.Log.Info("panic_over")
		}
		m.panicked = panicMode
	}
	for id, w := range desired {
		m.Metrics.DesiredWeight.WithLabelValues(id).Set(float64(w))
	}
	if m.active() && werr == nil {
		applied, changed, err := m.Route.SetWeights(ctx, m.backends, desired)
		if err != nil {
			m.stageError("route", err)
		} else {
			done := m.Now()
			if changed {
				m.Log.Info("route_updated", "weights", applied)
			}
			weights = applied
			for _, c := range monitored {
				st := m.clusters[c.ID]
				if w, ok := applied[c.ID]; ok && w == 0 && st.fsm.State() == fsm.Down && st.incident != nil &&
					st.incident.rec.FailoverCompletedAt == nil {
					st.incident.rec.FailoverCompletedAt = &done
					st.incident.dirty = true
					m.Log.Info("failover_completed", "cluster", c.ID, "at", done)
				}
			}
		}
	}
	for id, w := range weights {
		m.Metrics.Weight.WithLabelValues(id).Set(float64(w))
	}

	// 6. Статус down в каталоге (ADR 0003) — только active и по свежему инвентарю.
	if m.active() && registryOK {
		for _, c := range monitored {
			st := m.clusters[c.ID]
			var want string
			switch {
			case st.fsm.State() == fsm.Down && statuses[c.ID] != registry.StatusDown:
				want = registry.StatusDown
			case st.fsm.State() != fsm.Down && statuses[c.ID] == registry.StatusDown:
				// Снятие down; точный healthy/degraded по метрикам выставит опрос каталога
				// в течение 30 с (ADR 0003, 0025).
				want = registry.StatusHealthy
			default:
				continue
			}
			if err := m.Registry.SetStatus(ctx, c.ID, want); err != nil {
				m.stageError("status", err)
				continue
			}
			statuses[c.ID] = want
			m.Log.Info("registry_status", "cluster", c.ID, "status", want)
		}
	}

	// 7. Инциденты в каталог.
	m.syncIncidents(ctx)

	for _, c := range monitored {
		st := m.clusters[c.ID]
		m.Metrics.setState(c.ID, st.fsm.State())
		m.Metrics.setSignals(c.ID, st.signals, st.detail.ErrorRate)
	}
	m.publish(started, monitored, statuses, weights, desired, panicMode)
}

// desired — веса маршрута по состояниям. Все DOWN — номинал всем (panic): обнулять
// последний backend бессмысленно, а при ложном DOWN (сломалась сама проба DR) это
// превратило бы ошибку наблюдателя в полный отказ сервиса.
func (m *Monitor) desired(monitored []registry.Cluster) (map[string]int64, bool) {
	out := make(map[string]int64, len(monitored))
	var sum int64
	for _, c := range monitored {
		f := m.clusters[c.ID].fsm.WeightFraction()
		w := int64(math.Round(f * float64(m.Config.NominalWeight)))
		if f > 0 && w < 1 {
			w = 1
		}
		out[c.ID] = w
		sum += w
	}
	if sum > 0 || len(out) == 0 {
		return out, false
	}
	for id := range out {
		out[id] = m.Config.NominalWeight
	}
	return out, true
}

// initCluster — начальное состояние кластера при первом появлении (старт или рестарт DR).
// Кластер уже помечен down в каталоге или выведен из маршрута — продолжаем с DOWN и
// открытым инцидентом из каталога; иначе HEALTHY, а висящие открытые инциденты
// (DR упал посреди DEGRADED) закрываются с пометкой.
func (m *Monitor) initCluster(ctx context.Context, id, status string, registryOK bool,
	weights map[string]int64, weightsOK bool) *cluster {
	now := m.Now()
	w, onRoute := weights[id]
	down := (registryOK && status == registry.StatusDown) || (weightsOK && onRoute && w == 0)
	initial := fsm.Healthy
	if down {
		initial = fsm.Down
	}
	st := &cluster{id: id, fsm: fsm.New(m.Config.FSM, initial), since: now}
	var open []registry.Incident
	if registryOK {
		var err error
		if open, err = m.Registry.OpenIncidents(ctx, id); err != nil {
			m.stageError("incident", err)
		}
	}
	m.Log.Info("cluster_init", "cluster", id, "state", initial, "registry_status", status,
		"weight", weights[id], "open_incidents", len(open))
	if down {
		if n := len(open); n > 0 {
			rec := open[n-1]
			open = open[:n-1]
			if rec.Details == nil {
				rec.Details = map[string]any{}
			}
			rec.Details["resumed_at"] = now
			st.incident = &incident{rec: rec, dirty: true}
		} else {
			st.incident = &incident{dirty: true, rec: registry.Incident{
				ClusterID: id, DRMode: m.Config.Mode, PeakState: string(fsm.Down), DetectedAt: now,
				FailoverStartedAt: &now, Details: map[string]any{"resumed_without_record": true},
			}}
		}
	}
	for _, rec := range open {
		if rec.Details == nil {
			rec.Details = map[string]any{}
		}
		rec.Details["closed_on_restart"] = true
		rec.RecoveryCompletedAt = &now
		m.closed = append(m.closed, &incident{rec: rec, dirty: true})
	}
	return st
}

// transition — запись о переходе в инцидент и лог.
func (m *Monitor) transition(st *cluster, from fsm.State, now time.Time) {
	to := st.fsm.State()
	st.since = now
	m.Metrics.Transitions.WithLabelValues(st.id, string(from), string(to)).Inc()
	m.Log.Info("transition", "cluster", st.id, "from", from, "to", to,
		"svc", st.signals.Svc, "api", st.signals.API, "sli", st.signals.SLI.String(), "detail", st.detail)

	inc := st.incident
	if inc == nil && to == fsm.Degraded {
		inc = &incident{rec: registry.Incident{
			ClusterID: st.id, DRMode: m.Config.Mode, PeakState: string(fsm.Degraded), DetectedAt: now,
			Details: map[string]any{"detected_signals": signalsJSON(st)},
		}}
		st.incident = inc
	}
	if inc == nil {
		return
	}
	inc.dirty = true
	rec := &inc.rec
	switch to {
	case fsm.Down:
		rec.PeakState = string(fsm.Down)
		if rec.FailoverStartedAt == nil {
			rec.FailoverStartedAt = &now
		}
		rec.Details["failovers"] = count(rec.Details["failovers"]) + 1
	case fsm.Recovering:
		rec.RecoveryStartedAt = &now
		rec.Details["recovery_attempts"] = count(rec.Details["recovery_attempts"]) + 1
	case fsm.Healthy:
		rec.RecoveryCompletedAt = &now
		m.closed = append(m.closed, inc)
		st.incident = nil
	}
}

func count(v any) int {
	switch n := v.(type) {
	case int:
		return n
	case float64:
		return int(n)
	}
	return 0
}

func signalsJSON(st *cluster) map[string]any {
	out := map[string]any{"svc": st.signals.Svc, "api": st.signals.API, "sli": st.signals.SLI.String()}
	if st.detail.Svc != "" {
		out["svc_detail"] = st.detail.Svc
	}
	if st.detail.API != "" {
		out["api_detail"] = st.detail.API
	}
	if st.detail.ErrorRate != nil {
		out["error_rate"] = *st.detail.ErrorRate
	}
	return out
}

// syncIncidents — записать новые и изменённые инциденты; неудача — повтор в следующем цикле.
func (m *Monitor) syncIncidents(ctx context.Context) {
	push := func(inc *incident) bool {
		if !inc.dirty {
			return true
		}
		if inc.rec.ID == 0 {
			id, err := m.Registry.OpenIncident(ctx, inc.rec)
			if err != nil {
				m.stageError("incident", err)
				return false
			}
			inc.rec.ID = id
			m.Log.Info("incident_opened", "cluster", inc.rec.ClusterID, "id", id)
		} else if err := m.Registry.UpdateIncident(ctx, inc.rec); err != nil {
			m.stageError("incident", err)
			return false
		}
		inc.dirty = false
		return true
	}
	pending := m.closed[:0]
	for _, inc := range m.closed {
		if !push(inc) {
			pending = append(pending, inc)
		}
	}
	m.closed = pending
	ids := make([]string, 0, len(m.clusters))
	for id := range m.clusters {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		if inc := m.clusters[id].incident; inc != nil {
			push(inc)
		}
	}
}

// --- /status ------------------------------------------------------------------------

// ClusterStatus — состояние кластера в /status.
type ClusterStatus struct {
	State          fsm.State    `json:"state"`
	Since          time.Time    `json:"since"`
	Svc            bool         `json:"svc"`
	API            bool         `json:"api"`
	SLI            string       `json:"sli"`
	Detail         signalDetail `json:"detail"`
	Weight         *int64       `json:"weight"`
	DesiredWeight  int64        `json:"desired_weight"`
	RegistryStatus string       `json:"registry_status,omitempty"`
	IncidentID     int64        `json:"incident_id,omitempty"`
}

// Snapshot — состояние DR после последнего цикла: для скриптов стенда и раннера.
type Snapshot struct {
	Mode        string                   `json:"mode"`
	Cycles      int64                    `json:"cycles"`
	LastCycleAt time.Time                `json:"last_cycle_at"`
	Panic       bool                     `json:"panic"`
	Nominal     int64                    `json:"nominal_weight"`
	Clusters    map[string]ClusterStatus `json:"clusters"`
}

func (m *Monitor) publish(at time.Time, monitored []registry.Cluster, statuses map[string]string,
	weights, desired map[string]int64, panicMode bool) {
	s := Snapshot{Mode: m.Config.Mode, LastCycleAt: at, Panic: panicMode, Nominal: m.Config.NominalWeight,
		Clusters: map[string]ClusterStatus{}}
	for _, c := range monitored {
		st := m.clusters[c.ID]
		cs := ClusterStatus{
			State: st.fsm.State(), Since: st.since, Svc: st.signals.Svc, API: st.signals.API,
			SLI: st.signals.SLI.String(), Detail: st.detail, DesiredWeight: desired[c.ID],
			RegistryStatus: statuses[c.ID],
		}
		if w, ok := weights[c.ID]; ok {
			cs.Weight = &w
		}
		if st.incident != nil {
			cs.IncidentID = st.incident.rec.ID
		}
		s.Clusters[c.ID] = cs
	}
	m.mu.Lock()
	s.Cycles = m.snapshot.Cycles + 1
	m.snapshot = s
	m.mu.Unlock()
}

// Status — копия последнего снимка.
func (m *Monitor) Status() Snapshot {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.snapshot
}

// StatusHandler — GET /status: JSON снимка.
func (m *Monitor) StatusHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(m.Status())
	})
}

// Healthy — liveness: последний цикл не старше трёх периодов.
func (m *Monitor) Healthy(_ *http.Request) error {
	s := m.Status()
	if s.Cycles == 0 {
		return nil // первый цикл ещё идёт
	}
	if age := m.Now().Sub(s.LastCycleAt); age > 3*m.Config.Interval {
		return fmt.Errorf("last cycle %s ago", age.Round(time.Second))
	}
	return nil
}

// ErrNotReady — первый цикл ещё не завершён.
var ErrNotReady = errors.New("no cycle completed yet")

// Ready — readiness: хотя бы один цикл завершён.
func (m *Monitor) Ready(_ *http.Request) error {
	if m.Status().Cycles == 0 {
		return ErrNotReady
	}
	return nil
}
