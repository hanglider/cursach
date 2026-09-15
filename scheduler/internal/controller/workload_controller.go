// Package controller — reconcile CR Workload (architecture §5.1, ADR 0027): кандидаты
// из каталога, выбор (meta или naive), запись решения, создание объектов в кластере
// и на шлюзе, обработка отказа admission, уборка по финализатору.
package controller

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/predicate"

	"github.com/hanglider/cursach/scheduler/api/v1alpha1"
	"github.com/hanglider/cursach/scheduler/internal/clusters"
	"github.com/hanglider/cursach/scheduler/internal/placement"
	"github.com/hanglider/cursach/scheduler/internal/registry"
	"github.com/hanglider/cursach/scheduler/internal/scoring"
)

// Режимы планировщика (ADR 0020).
const (
	ModeMeta  = "meta"
	ModeNaive = "naive"
)

// Registry — часть клиента каталога, нужная контроллеру; подменяется в тестах.
type Registry interface {
	Eligible(ctx context.Context, req registry.Requirements) (*registry.Eligible, error)
	CreateDecision(ctx context.Context, d registry.Decision) (int64, error)
	PatchDecision(ctx context.Context, id int64, outcome string) error
	CreateViolation(ctx context.Context, v registry.Violation) error
}

// Events — запись событий на Workload (events.k8s.io через рекордер менеджера).
type Events interface {
	Eventf(regarding runtime.Object, related runtime.Object, eventtype, reason, action, note string, args ...interface{})
}

// Config — параметры планировщика; все попадают в лог при старте.
type Config struct {
	Mode            string
	Params          scoring.Params
	RetryInterval   time.Duration // повтор при NoCandidates
	FinalizeTimeout time.Duration // сколько ждать уборки в недоступном кластере

	WorkloadNamespace string // ns Deployment и Service в кластере федерации
	Image             string
	BackendURL        string
	Domain            string
	GatewayNamespace  string
	GatewayName       string
}

// Reconciler — контроллер Workload.
type Reconciler struct {
	client.Client               // control: Workload (через кэш менеджера)
	Gateway       client.Client // control без кэша: Backend и HTTPRoute
	Clusters      clusters.Provider
	Registry      Registry
	Events        Events
	Config        Config
	RR            *scoring.RoundRobin
	Now           func() time.Time
	Log           *slog.Logger
}

// SetupWithManager регистрирует контроллер. Только изменения generation: обновления
// status не должны запускать повторное планирование.
func (r *Reconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&v1alpha1.Workload{}, builder.WithPredicates(predicate.GenerationChangedPredicate{})).
		Complete(r)
}

func (r *Reconciler) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

func (r *Reconciler) workloadRef(w *v1alpha1.Workload) string {
	return r.Config.WorkloadNamespace + "/" + w.Name
}

// Reconcile — один проход по CR.
func (r *Reconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	var w v1alpha1.Workload
	if err := r.Get(ctx, req.NamespacedName, &w); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !w.DeletionTimestamp.IsZero() {
		return r.finalize(ctx, &w)
	}
	if !controllerutil.ContainsFinalizer(&w, v1alpha1.Finalizer) {
		controllerutil.AddFinalizer(&w, v1alpha1.Finalizer)
		if err := r.Update(ctx, &w); err != nil {
			return ctrl.Result{}, err
		}
	}
	switch w.Status.Phase {
	case v1alpha1.PhasePlaced, v1alpha1.PhaseRejected:
		return ctrl.Result{}, nil // терминальные фазы: CR не перепланируется (ADR 0011 п. 3)
	}
	if w.Status.Cluster != "" && w.Status.DecisionID != nil {
		// Решение уже записано (Pending после сбоя или Error): довести размещение.
		return r.place(ctx, &w)
	}
	return r.schedule(ctx, &w)
}

func (r *Reconciler) setStatus(ctx context.Context, w *v1alpha1.Workload, mutate func(*v1alpha1.WorkloadStatus)) error {
	mutate(&w.Status)
	w.Status.ObservedGeneration = w.Generation
	return r.Status().Update(ctx, w)
}

// fail — phase Error; возврат ошибки даёт экспоненциальный повтор controller-runtime.
func (r *Reconciler) fail(ctx context.Context, w *v1alpha1.Workload, what string, err error) (ctrl.Result, error) {
	msg := what + ": " + err.Error()
	r.Events.Eventf(w, nil, corev1.EventTypeWarning, "Error", "Reconcile", "%s", truncate(msg, 1024))
	if serr := r.setStatus(ctx, w, func(s *v1alpha1.WorkloadStatus) {
		s.Phase = v1alpha1.PhaseError
		s.Message = truncate(msg, 1024)
	}); serr != nil {
		return ctrl.Result{}, errors.Join(err, serr)
	}
	return ctrl.Result{}, err
}

func toInputs(entries []registry.Entry) []scoring.Input {
	out := make([]scoring.Input, 0, len(entries))
	for _, e := range entries {
		in := scoring.Input{ID: e.ID, SloStatus: e.SloStatus}
		if e.Metrics != nil {
			in.Metrics = &scoring.Metrics{
				ObservedAt:   e.Metrics.ObservedAt,
				ErrorRate:    e.Metrics.ErrorRate,
				LatencyP95Ms: e.Metrics.LatencyP95Ms,
				Saturation:   e.Metrics.Saturation,
			}
		}
		out = append(out, in)
	}
	return out
}

// schedule — кандидаты из каталога → выбор → запись решения → размещение.
func (r *Reconciler) schedule(ctx context.Context, w *v1alpha1.Workload) (ctrl.Result, error) {
	reqs, err := registry.RequirementsFromAnnotations(w.Annotations)
	if err != nil {
		// Ошибка пользователя в аннотациях: повторять нечего.
		r.Events.Eventf(w, nil, corev1.EventTypeWarning, "InvalidRequirements", "Schedule", "%s", err.Error())
		return ctrl.Result{}, r.setStatus(ctx, w, func(s *v1alpha1.WorkloadStatus) {
			s.Phase = v1alpha1.PhaseError
			s.Message = "invalid requirements: " + err.Error()
		})
	}
	elig, err := r.Registry.Eligible(ctx, reqs)
	if err != nil {
		return r.fail(ctx, w, "registry eligible", err)
	}
	ref := r.workloadRef(w)
	mode := r.Config.Mode

	candidates := make([]registry.Candidate, 0, len(elig.Eligible)+len(elig.Rejected))
	for _, e := range elig.Rejected {
		fb := e.FilteredBy
		candidates = append(candidates, registry.Candidate{Cluster: e.ID, Metrics: e.Metrics, FilteredBy: &fb})
	}

	if len(elig.Eligible) == 0 {
		id, err := r.Registry.CreateDecision(ctx, registry.Decision{
			WorkloadRef: ref, SchedulerKind: mode, Candidates: candidates, Outcome: registry.OutcomeNoCandidates,
		})
		if err != nil {
			return r.fail(ctx, w, "registry decision", err)
		}
		summary := summarizeRejected(elig.Rejected)
		r.Events.Eventf(w, nil, corev1.EventTypeWarning, "NoCandidates", "Schedule",
			"no eligible clusters: %s (decision %d)", summary, id)
		r.Log.Warn("no candidates", "workload", ref, "scheduler", mode, "decision_id", id, "rejected", summary)
		if err := r.setStatus(ctx, w, func(s *v1alpha1.WorkloadStatus) {
			s.Phase = v1alpha1.PhaseNoCandidates
			s.DecisionID = &id
			s.Scheduler = mode
			s.Message = "no eligible clusters: " + summary
		}); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{RequeueAfter: r.Config.RetryInterval}, nil
	}

	inputs := toInputs(elig.Eligible)
	now := r.now()
	var chosen string
	var scoringJSON map[string]any
	if mode == ModeMeta {
		var scores map[string]float64
		chosen, scores = scoring.PickMeta(inputs, r.Config.Params, now, r.RR)
		for i, e := range elig.Eligible {
			s := scores[e.ID]
			t := r.Config.Params.Terms(inputs[i], now)
			candidates = append(candidates, registry.Candidate{
				Cluster: e.ID, Score: &s, Metrics: e.Metrics,
				Terms: map[string]float64{"error": t.Error, "latency": t.Latency, "saturation": t.Saturation},
			})
		}
		scoringJSON = r.Config.Params.JSON()
	} else {
		chosen = scoring.PickNaive(inputs, r.RR)
		for _, e := range elig.Eligible {
			candidates = append(candidates, registry.Candidate{Cluster: e.ID, Metrics: e.Metrics})
		}
	}

	id, err := r.Registry.CreateDecision(ctx, registry.Decision{
		WorkloadRef: ref, SchedulerKind: mode, ChosenCluster: &chosen,
		Candidates: candidates, Scoring: scoringJSON, Outcome: registry.OutcomePlaced,
	})
	if err != nil {
		return r.fail(ctx, w, "registry decision", err)
	}
	r.Log.Info("decision", "workload", ref, "scheduler", mode, "cluster", chosen,
		"decision_id", id, "eligible", len(elig.Eligible), "rejected", len(elig.Rejected))
	if err := r.setStatus(ctx, w, func(s *v1alpha1.WorkloadStatus) {
		s.Phase = v1alpha1.PhasePending
		s.Cluster = chosen
		s.DecisionID = &id
		s.Scheduler = mode
		s.Message = "placing"
	}); err != nil {
		return ctrl.Result{}, err
	}
	return r.place(ctx, w)
}

func (r *Reconciler) spec(w *v1alpha1.Workload, nodeAddress string) placement.Spec {
	image := w.Spec.Image
	if image == "" {
		image = r.Config.Image
	}
	replicas := int32(1)
	if w.Spec.Replicas != nil {
		replicas = *w.Spec.Replicas
	}
	var decisionID int64
	if w.Status.DecisionID != nil {
		decisionID = *w.Status.DecisionID
	}
	return placement.Spec{
		Name:             w.Name,
		Namespace:        r.Config.WorkloadNamespace,
		Image:            image,
		Replicas:         replicas,
		Port:             w.Spec.Port,
		Scheduler:        w.Status.Scheduler,
		DecisionID:       decisionID,
		Annotations:      w.Annotations,
		BackendURL:       r.Config.BackendURL,
		GatewayNamespace: r.Config.GatewayNamespace,
		GatewayName:      r.Config.GatewayName,
		Host:             w.Name + "." + r.Config.Domain,
		NodeAddress:      nodeAddress,
	}
}

func createIgnoreExists(ctx context.Context, c client.Client, obj client.Object) error {
	err := c.Create(ctx, obj)
	if apierrors.IsAlreadyExists(err) {
		return nil
	}
	return err
}

// place — объекты в выбранном кластере и на шлюзе; идемпотентно (AlreadyExists — ок).
func (r *Reconciler) place(ctx context.Context, w *v1alpha1.Workload) (ctrl.Result, error) {
	cluster := w.Status.Cluster
	cl, err := r.Clusters.Get(cluster)
	if err != nil {
		return r.fail(ctx, w, "cluster client", err)
	}
	addr, err := r.Clusters.NodeAddress(ctx, cluster)
	if err != nil {
		return r.fail(ctx, w, "node address", err)
	}
	spec := r.spec(w, addr)

	if err := createIgnoreExists(ctx, cl, placement.Deployment(spec)); err != nil {
		if placement.IsAdmissionDenied(err) {
			return r.reject(ctx, w, err)
		}
		return r.fail(ctx, w, "create deployment", err)
	}
	if err := createIgnoreExists(ctx, cl, placement.Service(spec)); err != nil {
		return r.fail(ctx, w, "create service", err)
	}
	if err := createIgnoreExists(ctx, r.Gateway, placement.Backend(spec)); err != nil {
		return r.fail(ctx, w, "create backend", err)
	}
	if err := createIgnoreExists(ctx, r.Gateway, placement.HTTPRoute(spec)); err != nil {
		return r.fail(ctx, w, "create httproute", err)
	}

	placedAt := metav1.NewTime(r.now())
	r.Events.Eventf(w, nil, corev1.EventTypeNormal, "Placed", "Place", "placed in %s by %s scheduler (decision %d)",
		cluster, w.Status.Scheduler, spec.DecisionID)
	r.Log.Info("placed", "workload", r.workloadRef(w), "cluster", cluster, "host", spec.Host,
		"node", addr, "decision_id", spec.DecisionID)
	return ctrl.Result{}, r.setStatus(ctx, w, func(s *v1alpha1.WorkloadStatus) {
		s.Phase = v1alpha1.PhasePlaced
		s.Host = spec.Host
		s.NodeAddress = addr
		s.PlacedAt = &placedAt
		s.Message = ""
	})
}

// reject — отказ admission (Kyverno, рубеж 2): решение → rejected, нарушение в
// audit log (ADR 0019 путь 1), фаза Rejected терминальна.
func (r *Reconciler) reject(ctx context.Context, w *v1alpha1.Workload, denial error) (ctrl.Result, error) {
	d := placement.ParseDenial(denial.Error())
	id := *w.Status.DecisionID
	ref := r.workloadRef(w)
	if err := r.Registry.PatchDecision(ctx, id, registry.OutcomeRejected); err != nil {
		return r.fail(ctx, w, "registry patch decision", err)
	}
	reqs, _ := registry.RequirementsFromAnnotations(w.Annotations)
	v := registry.Violation{
		WorkloadRef: ref,
		ClusterID:   w.Status.Cluster,
		PolicyName:  d.Policy,
		DetectedBy:  "kyverno",
		Details: map[string]any{
			"reported_via": "scheduler",
			"scheduler":    w.Status.Scheduler,
			"decision_id":  id,
			"rule":         d.Rule,
			"message":      d.Message,
		},
		Requirements: &reqs,
	}
	if err := r.Registry.CreateViolation(ctx, v); err != nil {
		return r.fail(ctx, w, "registry violation", err)
	}
	r.Events.Eventf(w, nil, corev1.EventTypeWarning, "Rejected", "Place", "%s: %s/%s: %s",
		w.Status.Cluster, d.Policy, d.Rule, d.Message)
	r.Log.Warn("rejected by admission", "workload", ref, "cluster", w.Status.Cluster,
		"policy", d.Policy, "rule", d.Rule, "decision_id", id)
	return ctrl.Result{}, r.setStatus(ctx, w, func(s *v1alpha1.WorkloadStatus) {
		s.Phase = v1alpha1.PhaseRejected
		s.Message = truncate(d.Policy+"/"+d.Rule+": "+d.Message, 1024)
	})
}

func gatewayObject(gvkName string, spec placement.Spec) *unstructured.Unstructured {
	u := &unstructured.Unstructured{}
	if gvkName == "backend" {
		u.SetGroupVersionKind(placement.BackendGVK)
	} else {
		u.SetGroupVersionKind(placement.HTTPRouteGVK)
	}
	u.SetName(spec.Name)
	u.SetNamespace(spec.GatewayNamespace)
	return u
}

// finalize — уборка при удалении CR. Недоступный кластер не блокирует удаление
// дольше FinalizeTimeout: после него финализатор снимается с предупреждением.
func (r *Reconciler) finalize(ctx context.Context, w *v1alpha1.Workload) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(w, v1alpha1.Finalizer) {
		return ctrl.Result{}, nil
	}
	spec := r.spec(w, w.Status.NodeAddress)
	var errs []error
	if w.Status.Cluster != "" {
		cl, err := r.Clusters.Get(w.Status.Cluster)
		if err != nil {
			errs = append(errs, err)
		} else {
			meta := metav1.ObjectMeta{Name: spec.Name, Namespace: spec.Namespace}
			for _, obj := range []client.Object{
				&appsv1.Deployment{ObjectMeta: meta},
				&corev1.Service{ObjectMeta: meta},
			} {
				err := cl.Delete(ctx, obj, client.PropagationPolicy(metav1.DeletePropagationBackground))
				if client.IgnoreNotFound(err) != nil {
					errs = append(errs, err)
				}
			}
		}
	}
	for _, obj := range []client.Object{gatewayObject("httproute", spec), gatewayObject("backend", spec)} {
		if err := r.Gateway.Delete(ctx, obj); client.IgnoreNotFound(err) != nil {
			errs = append(errs, err)
		}
	}
	if len(errs) > 0 {
		err := errors.Join(errs...)
		if r.now().Sub(w.DeletionTimestamp.Time) < r.Config.FinalizeTimeout {
			r.Log.Warn("cleanup failed, will retry", "workload", w.Name, "error", err.Error())
			return ctrl.Result{RequeueAfter: 10 * time.Second}, nil
		}
		r.Events.Eventf(w, nil, corev1.EventTypeWarning, "CleanupAbandoned", "Finalize", "%s", truncate(err.Error(), 1024))
		r.Log.Error("cleanup abandoned after timeout", "workload", w.Name, "error", err.Error())
	}
	controllerutil.RemoveFinalizer(w, v1alpha1.Finalizer)
	return ctrl.Result{}, r.Update(ctx, w)
}

func summarizeRejected(entries []registry.Entry) string {
	if len(entries) == 0 {
		return "no clusters registered"
	}
	parts := make([]string, 0, len(entries))
	for _, e := range entries {
		parts = append(parts, fmt.Sprintf("%s(%s)", e.ID, e.FilteredBy))
	}
	return strings.Join(parts, ", ")
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}
