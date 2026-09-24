package fsm

import (
	"testing"
)

var (
	ok      = Signals{Svc: true, API: true, SLI: SLIOK}
	noSLI   = Signals{Svc: true, API: true, SLI: SLIUnknown}
	svcDown = Signals{Svc: false, API: true, SLI: SLIUnknown}
	apiDown = Signals{Svc: true, API: false, SLI: SLIOK}
	errRate = Signals{Svc: true, API: true, SLI: SLIBad}
	smoked  = Signals{Svc: true, API: true, SLI: SLIUnknown, Smoke: true}
	smokeNo = Signals{Svc: true, API: true, SLI: SLIUnknown, Smoke: false}
)

// run прогоняет сигналы и возвращает состояние после каждого цикла.
func run(f *FSM, seq ...Signals) []State {
	out := make([]State, 0, len(seq))
	for _, s := range seq {
		f.Next(s)
		out = append(out, f.State())
	}
	return out
}

func eq(t *testing.T, got, want []State) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("len: got %v, want %v", got, want)
	}
	for i := range got {
		if got[i] != want[i] {
			t.Fatalf("cycle %d: got %v, want %v", i+1, got, want)
		}
	}
}

func repeat(s Signals, n int) []Signals {
	out := make([]Signals, n)
	for i := range out {
		out[i] = s
	}
	return out
}

func TestDefaultsValid(t *testing.T) {
	if err := Defaults().Validate(); err != nil {
		t.Fatal(err)
	}
	for _, p := range []Params{
		{DownCycles: 0, HealthyCycles: 3, StepCycles: 2, Steps: []float64{1}},
		{DownCycles: 3, HealthyCycles: 3, StepCycles: 2, Steps: []float64{0.5, 0.9}},
		{DownCycles: 3, HealthyCycles: 3, StepCycles: 2, Steps: []float64{0.5, 0.1, 1}},
		{DownCycles: 3, HealthyCycles: 3, StepCycles: 2},
	} {
		if p.Validate() == nil {
			t.Errorf("expected error for %+v", p)
		}
	}
}

// Отказ сервиса: DEGRADED с первого цикла, DOWN на четвёртой неудачной пробе подряд
// (T = 30 с при периоде 10 с, ADR 0007, 0028).
func TestServiceFailureLeadsToDown(t *testing.T) {
	f := New(Defaults(), Healthy)
	eq(t, run(f, repeat(svcDown, 5)...), []State{Degraded, Degraded, Degraded, Down, Down})
	if f.WeightFraction() != 0 {
		t.Fatalf("weight in DOWN: %v", f.WeightFraction())
	}
}

// Прерывание серии неудачных проб сбрасывает счётчик DOWN.
func TestFlappingProbeDoesNotReachDown(t *testing.T) {
	f := New(Defaults(), Healthy)
	got := run(f, svcDown, svcDown, svcDown, noSLI, svcDown, svcDown, svcDown)
	eq(t, got, []State{Degraded, Degraded, Degraded, Degraded, Degraded, Degraded, Down})
}

// Error rate и отказ API ведут только в DEGRADED, никогда в DOWN (ADR 0007).
func TestErrorRateAndAPIOnlyDegrade(t *testing.T) {
	for name, bad := range map[string]Signals{"sli": errRate, "api": apiDown} {
		f := New(Defaults(), Healthy)
		for i, s := range run(f, repeat(bad, 20)...) {
			if s != Degraded {
				t.Fatalf("%s: cycle %d: %v", name, i+1, s)
			}
		}
		if f.WeightFraction() != 1 {
			t.Fatalf("%s: DEGRADED must keep traffic, weight %v", name, f.WeightFraction())
		}
	}
}

// Отсутствие метрик — не отказ (инвариант 6): HEALTHY остаётся HEALTHY.
func TestUnknownSLIIsNotAFault(t *testing.T) {
	f := New(Defaults(), Healthy)
	for _, s := range run(f, repeat(noSLI, 10)...) {
		if s != Healthy {
			t.Fatalf("unknown SLI moved state to %v", s)
		}
	}
}

// DEGRADED → HEALTHY после трёх чистых циклов подряд; грязный цикл сбрасывает счёт.
func TestDegradedRecoversAfterThreeCleanCycles(t *testing.T) {
	f := New(Defaults(), Healthy)
	got := run(f, errRate, ok, ok, errRate, ok, ok, ok)
	eq(t, got, []State{Degraded, Degraded, Degraded, Degraded, Degraded, Degraded, Healthy})
}

// DOWN не выходит без smoke-теста; с ним — RECOVERING, ступени 10% → 50% → 100% по два
// чистых цикла, затем HEALTHY: не менее 6 циклов (ADR 0007).
func TestRecoveryStepsAndSmoke(t *testing.T) {
	f := New(Defaults(), Down)
	eq(t, run(f, svcDown, smokeNo, smokeNo), []State{Down, Down, Down})

	f.Next(smoked)
	if f.State() != Recovering || f.WeightFraction() != 0.1 {
		t.Fatalf("after smoke: %v %v", f.State(), f.WeightFraction())
	}
	var fractions []float64
	for range 6 {
		f.Next(noSLI)
		fractions = append(fractions, f.WeightFraction())
	}
	want := []float64{0.1, 0.5, 0.5, 1.0, 1.0, 1.0}
	for i := range want {
		if fractions[i] != want[i] {
			t.Fatalf("fractions: got %v, want %v", fractions, want)
		}
	}
	if f.State() != Healthy {
		t.Fatalf("expected HEALTHY after 6 clean cycles, got %v", f.State())
	}
}

// Грязный цикл в RECOVERING не откатывает вес, а обнуляет счётчик ступени.
func TestDirtyCycleInRecoveringResetsStepCounter(t *testing.T) {
	f := New(Defaults(), Down)
	f.Next(smoked)
	f.Next(ok)      // 1 чистый на 10%
	f.Next(errRate) // сброс
	if f.WeightFraction() != 0.1 || f.State() != Recovering {
		t.Fatalf("dirty cycle changed weight: %v %v", f.State(), f.WeightFraction())
	}
	f.Next(ok)
	if f.WeightFraction() != 0.1 {
		t.Fatalf("step advanced after one clean cycle: %v", f.WeightFraction())
	}
	f.Next(ok)
	if f.WeightFraction() != 0.5 {
		t.Fatalf("step did not advance after two clean cycles: %v", f.WeightFraction())
	}
}

// RECOVERING → DOWN после трёх неудачных проб подряд; вес снова 0.
func TestRecoveringFallsBackToDown(t *testing.T) {
	f := New(Defaults(), Down)
	f.Next(smoked)
	f.Next(ok)
	f.Next(ok) // 50%
	eq(t, run(f, svcDown, svcDown, svcDown), []State{Recovering, Recovering, Down})
	if f.WeightFraction() != 0 {
		t.Fatalf("weight after fallback: %v", f.WeightFraction())
	}
	// Повторный подъём начинается снова с первой ступени.
	f.Next(smoked)
	if f.WeightFraction() != 0.1 {
		t.Fatalf("second recovery must restart at the first step: %v", f.WeightFraction())
	}
}

func TestSLIString(t *testing.T) {
	if SLIOK.String() != "ok" || SLIBad.String() != "bad" || SLIUnknown.String() != "unknown" {
		t.Fatal("SLI names")
	}
}
