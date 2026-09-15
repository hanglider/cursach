package scoring

import (
	"math"
	"testing"
	"time"
)

var now = time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC)

func f(v float64) *float64 { return &v }

func fresh(err, p95, sat float64) *Metrics {
	return &Metrics{ObservedAt: now.Add(-10 * time.Second), ErrorRate: f(err), LatencyP95Ms: f(p95), Saturation: f(sat)}
}

func near(t *testing.T, got, want float64) {
	t.Helper()
	if math.Abs(got-want) > 1e-9 {
		t.Fatalf("got %g, want %g", got, want)
	}
}

func TestScoreFormula(t *testing.T) {
	p := DefaultParams()
	// 0.5*(1-0.02) + 0.3*(1-100/200) + 0.2*(1-0.4)
	near(t, p.Score(Input{ID: "a", SloStatus: "healthy", Metrics: fresh(0.02, 100, 0.4)}, now), 0.76)
	terms := p.Terms(Input{ID: "a", SloStatus: "healthy", Metrics: fresh(0.02, 100, 0.4)}, now)
	if !terms.Fresh || terms.Error != 0.98 || terms.Latency != 0.5 {
		t.Fatalf("terms %+v", terms)
	}
}

func TestLatencyClampedToBudget(t *testing.T) {
	p := DefaultParams()
	near(t, p.Terms(Input{Metrics: fresh(0, 400, 0)}, now).Latency, 0)
	near(t, p.Terms(Input{Metrics: fresh(0, 200, 0)}, now).Latency, 0)
	near(t, p.Terms(Input{Metrics: fresh(0, 0, 0)}, now).Latency, 1)
	near(t, p.Terms(Input{Metrics: fresh(1.5, 0, -1)}, now).Error, 0) // error rate > 1 обрезается
}

func TestNeutralWhenMetricsMissingStaleOrUnknown(t *testing.T) {
	p := DefaultParams()
	cases := map[string]Input{
		"no snapshot": {ID: "a", SloStatus: "healthy"},
		"unknown":     {ID: "a", SloStatus: StatusUnknown, Metrics: fresh(0, 10, 0)},
		"stale": {ID: "a", SloStatus: "healthy", Metrics: &Metrics{
			ObservedAt: now.Add(-61 * time.Second), ErrorRate: f(0), LatencyP95Ms: f(10), Saturation: f(0),
		}},
	}
	for name, in := range cases {
		terms := p.Terms(in, now)
		if terms.Fresh || terms.Error != Neutral || terms.Latency != Neutral || terms.Saturation != Neutral {
			t.Fatalf("%s: terms %+v", name, terms)
		}
		near(t, p.Score(in, now), 0.5)
	}
	// Частичный снимок: известна только error rate.
	partial := Input{SloStatus: "healthy", Metrics: &Metrics{ObservedAt: now, ErrorRate: f(0.1)}}
	near(t, p.Score(partial, now), 0.5*0.9+0.3*0.5+0.2*0.5)
}

func TestPickMetaChoosesBestAndBreaksTiesRoundRobin(t *testing.T) {
	p := DefaultParams()
	rr := &RoundRobin{}
	good := Input{ID: "cluster-b", SloStatus: "healthy", Metrics: fresh(0.01, 50, 0.2)}
	bad := Input{ID: "cluster-a", SloStatus: "degraded", Metrics: fresh(0.3, 300, 0.2)}
	chosen, scores := PickMeta([]Input{bad, good}, p, now, rr)
	if chosen != "cluster-b" || len(scores) != 2 || scores["cluster-b"] <= scores["cluster-a"] {
		t.Fatalf("chosen %q scores %v", chosen, scores)
	}

	// Ничья: без метрик оба 0.5 → round-robin по отсортированным id.
	a, b := Input{ID: "cluster-b"}, Input{ID: "cluster-a"}
	rr = &RoundRobin{}
	var seq []string
	for range 4 {
		c, _ := PickMeta([]Input{a, b}, p, now, rr)
		seq = append(seq, c)
	}
	if got := seq[0] + seq[1] + seq[2] + seq[3]; got != "cluster-acluster-bcluster-acluster-b" {
		t.Fatalf("tie sequence %v", seq)
	}
	// Почти равные (в пределах 1e-6) — тоже ничья.
	x := Input{ID: "x", SloStatus: "healthy", Metrics: fresh(0, 0, 0)}
	y := Input{ID: "y", SloStatus: "healthy", Metrics: fresh(1e-9, 0, 0)}
	rr = &RoundRobin{}
	c1, _ := PickMeta([]Input{x, y}, p, now, rr)
	c2, _ := PickMeta([]Input{x, y}, p, now, rr)
	if c1 == c2 {
		t.Fatalf("near-tie must alternate, got %s %s", c1, c2)
	}
}

func TestPickNaiveRoundRobinSortedByID(t *testing.T) {
	rr := &RoundRobin{}
	cands := []Input{{ID: "cluster-b"}, {ID: "cluster-a"}}
	want := []string{"cluster-a", "cluster-b", "cluster-a"}
	for i, w := range want {
		if got := PickNaive(cands, rr); got != w {
			t.Fatalf("step %d: got %s want %s", i, got, w)
		}
	}
	if PickNaive(nil, rr) != "" {
		t.Fatal("empty candidates must give empty id")
	}
}

func TestMetaWithoutSignalEqualsNaive(t *testing.T) {
	p := DefaultParams()
	cands := []Input{{ID: "cluster-a", SloStatus: StatusUnknown}, {ID: "cluster-b", SloStatus: StatusUnknown}}
	rrMeta, rrNaive := &RoundRobin{}, &RoundRobin{}
	for range 5 {
		m, _ := PickMeta(cands, p, now, rrMeta)
		if n := PickNaive(cands, rrNaive); m != n {
			t.Fatalf("meta %s != naive %s", m, n)
		}
	}
}

func TestParseWeights(t *testing.T) {
	w, err := ParseWeights("0.5, 0.3,0.2")
	if err != nil || w != [3]float64{0.5, 0.3, 0.2} {
		t.Fatalf("got %v, %v", w, err)
	}
	for _, bad := range []string{"0.5,0.5", "0.5,0.3,0.3", "-0.1,0.6,0.5", "a,b,c"} {
		if _, err := ParseWeights(bad); err == nil {
			t.Fatalf("%q must fail", bad)
		}
	}
}

func TestParamsJSON(t *testing.T) {
	j := DefaultParams().JSON()
	if j["formula"] != "adr-0005" || j["latency_budget_ms"] != 200.0 || j["metrics_max_age_s"] != 60.0 {
		t.Fatalf("json %v", j)
	}
}
