package monitor

import (
	"github.com/prometheus/client_golang/prometheus"

	"github.com/hanglider/cursach/dr-controller/internal/fsm"
)

// Metrics — ряды DR Controller'а для графиков деградации и восстановления (задача 8):
// скрейпит Prometheus control-кластера, экспорт прогона выгружает их вместе с SLI.
//
// Кластер федерации — метка cluster_id, а не cluster: cluster — внешняя метка
// Prometheus (здесь cluster=control), и Thanos Querier отбирает store'ы по внешним
// меткам — запрос dr_*{cluster="cluster-b"} не нашёл бы ничего.
type Metrics struct {
	State         *prometheus.GaugeVec
	Signal        *prometheus.GaugeVec
	ErrorRate     *prometheus.GaugeVec
	Weight        *prometheus.GaugeVec
	DesiredWeight *prometheus.GaugeVec
	Transitions   *prometheus.CounterVec
	Panic         prometheus.Gauge
	Mode          *prometheus.GaugeVec
	CycleDuration prometheus.Histogram
	CycleErrors   *prometheus.CounterVec
}

// NewMetrics регистрирует ряды в reg.
func NewMetrics(reg prometheus.Registerer) *Metrics {
	m := &Metrics{
		State: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "dr_cluster_state", Help: "1 для текущего состояния кластера в автомате DR (ADR 0007)",
		}, []string{"cluster_id", "state"}),
		Signal: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "dr_signal", Help: "Сигнал цикла: 1 норма, 0 отказ, -1 неизвестно (только sli)",
		}, []string{"cluster_id", "signal"}),
		ErrorRate: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "dr_error_rate", Help: "cluster:http_error_rate:ratio1m, прочитанный DR в последнем цикле",
		}, []string{"cluster_id"}),
		Weight: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "dr_route_weight", Help: "Вес backend'а кластера на маршруте app.fed.local",
		}, []string{"cluster_id"}),
		DesiredWeight: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "dr_route_weight_desired", Help: "Вес, который DR считает нужным (в observe не применяется)",
		}, []string{"cluster_id"}),
		Transitions: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "dr_transitions_total", Help: "Переходы автомата DR",
		}, []string{"cluster_id", "from", "to"}),
		Panic: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "dr_panic", Help: "1 — все кластеры DOWN, веса оставлены номинальными",
		}),
		Mode: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "dr_mode", Help: "1 для режима DR: active или observe",
		}, []string{"mode"}),
		CycleDuration: prometheus.NewHistogram(prometheus.HistogramOpts{
			Name: "dr_cycle_duration_seconds", Help: "Длительность цикла опроса",
			Buckets: []float64{0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10},
		}),
		CycleErrors: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "dr_cycle_errors_total", Help: "Ошибки этапов цикла (registry, backends, thanos, route, status, incident)",
		}, []string{"stage"}),
	}
	reg.MustRegister(m.State, m.Signal, m.ErrorRate, m.Weight, m.DesiredWeight, m.Transitions,
		m.Panic, m.Mode, m.CycleDuration, m.CycleErrors)
	return m
}

func (m *Metrics) setState(cluster string, s fsm.State) {
	for _, st := range fsm.States {
		v := 0.0
		if st == s {
			v = 1
		}
		m.State.WithLabelValues(cluster, string(st)).Set(v)
	}
}

func boolGauge(ok bool) float64 {
	if ok {
		return 1
	}
	return 0
}

func (m *Metrics) setSignals(cluster string, s fsm.Signals, errorRate *float64) {
	m.Signal.WithLabelValues(cluster, "svc").Set(boolGauge(s.Svc))
	m.Signal.WithLabelValues(cluster, "api").Set(boolGauge(s.API))
	sli := -1.0
	switch s.SLI {
	case fsm.SLIOK:
		sli = 1
	case fsm.SLIBad:
		sli = 0
	}
	m.Signal.WithLabelValues(cluster, "sli").Set(sli)
	if errorRate != nil {
		m.ErrorRate.WithLabelValues(cluster).Set(*errorRate)
	} else {
		m.ErrorRate.DeleteLabelValues(cluster)
	}
}
