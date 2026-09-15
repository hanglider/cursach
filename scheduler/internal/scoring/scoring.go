// Package scoring — выбор кластера: нормализованный score (ADR 0005) с
// предрегистрированными весами (ADR 0006) и round-robin наивного планировщика
// (ADR 0020). Без обращений к сети: каждое правило покрыто unit-тестом.
package scoring

import (
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Neutral — вклад слагаемого, когда метрики нет или она устарела (ADR 0005).
const Neutral = 0.5

// tieEpsilon — разница score, при которой кандидаты считаются равными.
const tieEpsilon = 1e-6

// StatusUnknown — кластер без метрик в каталоге: 0.5 по всем слагаемым.
const StatusUnknown = "unknown"

// Metrics — снимок метрик кластера из ответа каталога; nil — величина не получена.
type Metrics struct {
	ObservedAt   time.Time
	ErrorRate    *float64
	LatencyP95Ms *float64
	Saturation   *float64
}

// Input — кандидат из `eligible` ответа каталога.
type Input struct {
	ID        string
	SloStatus string   // healthy | degraded | unknown; down каталог не отдаёт
	Metrics   *Metrics // nil — снимка нет
}

// Params — параметры scoring, пишутся в каждое решение (ADR 0006).
type Params struct {
	Weights         [3]float64 // error rate, latency, saturation
	LatencyBudgetMs float64
	MetricsMaxAge   time.Duration
}

// DefaultParams — предрегистрированные значения: веса 0.5/0.3/0.2, budget 200 мс,
// свежесть 60 с (два периода опроса каталога).
func DefaultParams() Params {
	return Params{
		Weights:         [3]float64{0.5, 0.3, 0.2},
		LatencyBudgetMs: 200,
		MetricsMaxAge:   60 * time.Second,
	}
}

// ParseWeights разбирает "0.5,0.3,0.2": три неотрицательных числа с суммой 1.
func ParseWeights(s string) ([3]float64, error) {
	var w [3]float64
	parts := strings.Split(s, ",")
	if len(parts) != 3 {
		return w, fmt.Errorf("weights: expected 3 comma-separated numbers, got %q", s)
	}
	sum := 0.0
	for i, p := range parts {
		v, err := strconv.ParseFloat(strings.TrimSpace(p), 64)
		if err != nil || v < 0 {
			return w, fmt.Errorf("weights: bad value %q", p)
		}
		w[i] = v
		sum += v
	}
	if math.Abs(sum-1) > 1e-6 {
		return w, fmt.Errorf("weights: sum must be 1, got %g", sum)
	}
	return w, nil
}

// JSON — параметры для поля placement_decisions.scoring.
func (p Params) JSON() map[string]any {
	return map[string]any{
		"formula":           "adr-0005",
		"weights":           []float64{p.Weights[0], p.Weights[1], p.Weights[2]},
		"latency_budget_ms": p.LatencyBudgetMs,
		"metrics_max_age_s": p.MetricsMaxAge.Seconds(),
	}
}

// Terms — слагаемые до взвешивания; Fresh — использовался ли снимок метрик.
type Terms struct {
	Error, Latency, Saturation float64
	Fresh                      bool
}

// Terms считает слагаемые кандидата на момент now.
func (p Params) Terms(in Input, now time.Time) Terms {
	t := Terms{Error: Neutral, Latency: Neutral, Saturation: Neutral}
	if in.SloStatus == StatusUnknown || in.Metrics == nil {
		return t
	}
	if now.Sub(in.Metrics.ObservedAt) > p.MetricsMaxAge {
		return t // устаревший снимок: как отсутствующий (ADR 0005)
	}
	t.Fresh = true
	if v := in.Metrics.ErrorRate; v != nil {
		t.Error = 1 - clamp01(*v)
	}
	if v := in.Metrics.LatencyP95Ms; v != nil && p.LatencyBudgetMs > 0 {
		t.Latency = 1 - math.Min(math.Max(*v, 0)/p.LatencyBudgetMs, 1)
	}
	if v := in.Metrics.Saturation; v != nil {
		t.Saturation = 1 - clamp01(*v)
	}
	return t
}

// Score = w1*(1-error_rate) + w2*latency_score + w3*(1-saturation).
func (p Params) Score(in Input, now time.Time) float64 {
	t := p.Terms(in, now)
	return p.Weights[0]*t.Error + p.Weights[1]*t.Latency + p.Weights[2]*t.Saturation
}

func clamp01(v float64) float64 {
	return math.Min(math.Max(v, 0), 1)
}

// RoundRobin — счётчик в памяти процесса (ADR 0020); сброс при перезапуске допустим.
type RoundRobin struct {
	mu   sync.Mutex
	next uint64
}

// Pick возвращает следующий по кругу id из отсортированного списка.
func (r *RoundRobin) Pick(ids []string) string {
	if len(ids) == 0 {
		return ""
	}
	sorted := append([]string(nil), ids...)
	sort.Strings(sorted)
	r.mu.Lock()
	defer r.mu.Unlock()
	chosen := sorted[r.next%uint64(len(sorted))]
	r.next++
	return chosen
}

// PickNaive — round-robin по кандидатам, метрики не читаются (ADR 0020).
func PickNaive(cands []Input, rr *RoundRobin) string {
	ids := make([]string, 0, len(cands))
	for _, c := range cands {
		ids = append(ids, c.ID)
	}
	return rr.Pick(ids)
}

// PickMeta — argmax score; ничья (разница < 1e-6) разрешается round-robin по
// равным кандидатам (ADR 0005). Возвращает выбранный id и score каждого.
func PickMeta(cands []Input, p Params, now time.Time, rr *RoundRobin) (string, map[string]float64) {
	scores := make(map[string]float64, len(cands))
	best := math.Inf(-1)
	for _, c := range cands {
		s := p.Score(c, now)
		scores[c.ID] = s
		if s > best {
			best = s
		}
	}
	var tied []string
	for id, s := range scores {
		if best-s < tieEpsilon {
			tied = append(tied, id)
		}
	}
	return rr.Pick(tied), scores
}
