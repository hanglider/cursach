// Package fsm — конечный автомат состояния кластера DR Controller'а (architecture §6.1,
// ADR 0007, 0028). Чистая функция от сигналов цикла: без ввода-вывода и времени,
// всё, что меняет мир (веса маршрута, статус в каталоге, инцидент), делает монитор
// по результату шага.
package fsm

import "fmt"

// State — состояние кластера с точки зрения DR.
type State string

// Состояния автомата (architecture §6.1).
const (
	Healthy    State = "healthy"
	Degraded   State = "degraded"
	Down       State = "down"
	Recovering State = "recovering"
)

// States — все состояния в порядке «от лучшего к худшему»; для метрик.
var States = []State{Healthy, Degraded, Recovering, Down}

// SLI — сигнал error rate из Thanos. Unknown — метрики нет (Querier недоступен,
// store кластера выпал, трафика нет): это не отказ (инвариант 6).
type SLI int

// Значения сигнала SLI.
const (
	SLIUnknown SLI = iota
	SLIOK
	SLIBad
)

func (s SLI) String() string {
	switch s {
	case SLIOK:
		return "ok"
	case SLIBad:
		return "bad"
	default:
		return "unknown"
	}
}

// Signals — результат одного цикла опроса кластера (ADR 0007).
type Signals struct {
	Svc bool // GET /healthz долгоживущего app по адресу узла, 200
	API bool // GET {api_endpoint}/readyz, 200
	SLI SLI  // cluster:http_error_rate:ratio1m относительно порога
	// Smoke — smoke-тест (серия запросов к app, все 200). Имеет смысл только в DOWN при
	// Svc: монитор выполняет его лишь тогда, в остальных случаях поле игнорируется.
	Smoke bool
}

// Clean — все сигналы в норме; отсутствие SLI нормой считается (инвариант 6).
func (s Signals) Clean() bool { return s.Svc && s.API && s.SLI != SLIBad }

// Params — пороги переходов (ADR 0007). Значения по умолчанию — Defaults.
type Params struct {
	// DownCycles — сколько циклов подряд с !svc_ok в DEGRADED (и в RECOVERING) ведут в
	// DOWN. Счёт начинается после перехода в DEGRADED: DOWN наступает на четвёртой
	// неудачной пробе подряд, через T = 30 с после первой (ADR 0028).
	DownCycles int
	// HealthyCycles — чистых циклов подряд для DEGRADED → HEALTHY.
	HealthyCycles int
	// StepCycles — чистых циклов, которые удерживается каждая ступень веса в RECOVERING.
	StepCycles int
	// Steps — доли номинального веса при возврате трафика, по возрастанию, последняя 1.
	Steps []float64
}

// Defaults — параметры ADR 0007.
func Defaults() Params {
	return Params{DownCycles: 3, HealthyCycles: 3, StepCycles: 2, Steps: []float64{0.1, 0.5, 1.0}}
}

// Validate проверяет, что параметры осмысленны.
func (p Params) Validate() error {
	if p.DownCycles < 1 || p.HealthyCycles < 1 || p.StepCycles < 1 {
		return fmt.Errorf("fsm: cycle counts must be >= 1: %+v", p)
	}
	if len(p.Steps) == 0 || p.Steps[len(p.Steps)-1] != 1.0 {
		return fmt.Errorf("fsm: steps must end with 1.0: %v", p.Steps)
	}
	for i, s := range p.Steps {
		if s <= 0 || s > 1 || (i > 0 && s <= p.Steps[i-1]) {
			return fmt.Errorf("fsm: steps must increase within (0, 1]: %v", p.Steps)
		}
	}
	return nil
}

// FSM — состояние одного кластера и счётчики гистерезиса.
type FSM struct {
	params Params
	state  State
	// svcFail — циклов подряд с !svc_ok в DEGRADED или RECOVERING.
	svcFail int
	// clean — чистых циклов подряд: в DEGRADED до HEALTHY, в RECOVERING на текущей ступени.
	clean int
	// step — индекс ступени веса в RECOVERING.
	step int
}

// New — автомат в начальном состоянии. Начальное DOWN — при рестарте DR, когда кластер
// уже помечен down (монитор восстанавливает состояние из каталога и маршрута).
func New(p Params, initial State) *FSM {
	return &FSM{params: p, state: initial}
}

// State — текущее состояние.
func (f *FSM) State() State { return f.state }

// Step — индекс текущей ступени веса (имеет смысл в RECOVERING).
func (f *FSM) Step() int { return f.step }

// WeightFraction — доля номинального веса маршрута, которую кластер должен получать в
// текущем состоянии: DEGRADED трафик не теряет (ADR 0007: failover только на DOWN).
func (f *FSM) WeightFraction() float64 {
	switch f.state {
	case Down:
		return 0
	case Recovering:
		return f.params.Steps[f.step]
	default:
		return 1
	}
}

// Next — шаг автомата по сигналам цикла. Возвращает прежнее состояние; смена
// состояния — prev != f.State(). Смена ступени веса в RECOVERING видна по Step().
func (f *FSM) Next(s Signals) (prev State) {
	prev = f.state
	switch f.state {
	case Healthy:
		if !s.Clean() {
			f.enter(Degraded)
		}
	case Degraded:
		if !s.Svc {
			f.clean = 0
			f.svcFail++
			if f.svcFail >= f.params.DownCycles {
				f.enter(Down)
			}
			return prev
		}
		f.svcFail = 0
		if !s.Clean() {
			f.clean = 0
			return prev
		}
		f.clean++
		if f.clean >= f.params.HealthyCycles {
			f.enter(Healthy)
		}
	case Down:
		if s.Svc && s.Smoke {
			f.enter(Recovering)
		}
	case Recovering:
		if !s.Svc {
			f.clean = 0
			f.svcFail++
			if f.svcFail >= f.params.DownCycles {
				f.enter(Down)
			}
			return prev
		}
		f.svcFail = 0
		if !s.Clean() {
			// Грязный цикл не откатывает вес, а обнуляет счётчик ступени (ADR 0007).
			f.clean = 0
			return prev
		}
		f.clean++
		if f.clean < f.params.StepCycles {
			return prev
		}
		if f.step == len(f.params.Steps)-1 {
			f.enter(Healthy)
			return prev
		}
		f.step++
		f.clean = 0
	}
	return prev
}

func (f *FSM) enter(s State) {
	f.state = s
	f.svcFail = 0
	f.clean = 0
	f.step = 0
}
