// Package placement — форма размещённого workload'а: Deployment и Service в кластере
// федерации, Backend и HTTPRoute на шлюзе control-кластера (ADR 0015, 0017, 0022).
// Зеркало experiments/load/workloadgen/workloadgen/spec.py; метки — ADR 0009.
package placement

import (
	"errors"
	"regexp"
	"strconv"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/util/intstr"
)

// Метки федерации (ADR 0009).
const (
	LabelWorkload     = "federation.spbu/workload"
	LabelScheduler    = "federation.spbu/scheduler"
	LabelDecisionID   = "federation.spbu/decision-id"
	LabelManagedBy    = "federation.spbu/managed-by"
	ManagedBy         = "scheduler"
	RequirementPrefix = "federation.spbu/require-"
)

// GVK шлюзовых объектов; типизированных модулей не подключаем (ADR 0027).
var (
	BackendGVK   = schema.GroupVersionKind{Group: "gateway.envoyproxy.io", Version: "v1alpha1", Kind: "Backend"}
	HTTPRouteGVK = schema.GroupVersionKind{Group: "gateway.networking.k8s.io", Version: "v1", Kind: "HTTPRoute"}
)

// Spec — всё, что нужно для четырёх объектов одного workload'а.
type Spec struct {
	Name        string
	Namespace   string // ns workload'ов в кластере федерации
	Image       string
	Replicas    int32
	Port        int32 // NodePort
	Scheduler   string
	DecisionID  int64
	Annotations map[string]string // federation.spbu/require-*, копируются на Deployment
	BackendURL  string

	GatewayNamespace string
	GatewayName      string
	Host             string
	NodeAddress      string
}

// RequirementAnnotations оставляет только аннотации требований.
func RequirementAnnotations(all map[string]string) map[string]string {
	out := map[string]string{}
	for k, v := range all {
		if strings.HasPrefix(k, RequirementPrefix) {
			out[k] = v
		}
	}
	return out
}

// Labels — общие метки четырёх объектов.
func Labels(s Spec) map[string]string {
	return map[string]string{
		"app.kubernetes.io/name":      "test-service",
		"app.kubernetes.io/component": "frontend",
		LabelWorkload:                 s.Name,
		LabelScheduler:                s.Scheduler,
		LabelDecisionID:               strconv.FormatInt(s.DecisionID, 10),
		LabelManagedBy:                ManagedBy,
	}
}

// Deployment frontend'а тестового сервиса (ADR 0014); аннотации требований — на
// самом Deployment, их проверяет Kyverno (ADR 0019).
func Deployment(s Spec) *appsv1.Deployment {
	lbl := Labels(s)
	replicas := s.Replicas
	if replicas < 1 {
		replicas = 1
	}
	return &appsv1.Deployment{
		TypeMeta: metav1.TypeMeta{APIVersion: "apps/v1", Kind: "Deployment"},
		ObjectMeta: metav1.ObjectMeta{
			Name:        s.Name,
			Namespace:   s.Namespace,
			Labels:      lbl,
			Annotations: RequirementAnnotations(s.Annotations),
		},
		Spec: appsv1.DeploymentSpec{
			Replicas: &replicas,
			Selector: &metav1.LabelSelector{MatchLabels: map[string]string{LabelWorkload: s.Name}},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: lbl},
				Spec: corev1.PodSpec{
					Containers: []corev1.Container{{
						Name:            "frontend",
						Image:           s.Image,
						ImagePullPolicy: corev1.PullNever,
						Args:            []string{"--role=frontend"},
						Env: []corev1.EnvVar{
							{Name: "WORKLOAD", Value: s.Name},
							{Name: "BACKEND_URL", Value: s.BackendURL},
						},
						Ports: []corev1.ContainerPort{{Name: "http", ContainerPort: 8080}},
						ReadinessProbe: &corev1.Probe{
							ProbeHandler: corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{
								Path: "/readyz", Port: intstr.FromString("http"),
							}},
							PeriodSeconds: 2,
						},
						LivenessProbe: &corev1.Probe{
							ProbeHandler: corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{
								Path: "/healthz", Port: intstr.FromString("http"),
							}},
							PeriodSeconds: 10,
						},
						Resources: corev1.ResourceRequirements{
							Requests: corev1.ResourceList{
								corev1.ResourceCPU:    resource.MustParse("50m"),
								corev1.ResourceMemory: resource.MustParse("32Mi"),
							},
							Limits: corev1.ResourceList{
								corev1.ResourceCPU:    resource.MustParse("500m"),
								corev1.ResourceMemory: resource.MustParse("64Mi"),
							},
						},
					}},
				},
			},
		},
	}
}

// Service NodePort — вход в кластер федерации (ADR 0022).
func Service(s Spec) *corev1.Service {
	return &corev1.Service{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Service"},
		ObjectMeta: metav1.ObjectMeta{Name: s.Name, Namespace: s.Namespace, Labels: Labels(s)},
		Spec: corev1.ServiceSpec{
			Type:     corev1.ServiceTypeNodePort,
			Selector: map[string]string{LabelWorkload: s.Name},
			Ports: []corev1.ServicePort{{
				Name:       "http",
				Port:       8080,
				TargetPort: intstr.FromString("http"),
				NodePort:   s.Port,
			}},
		},
	}
}

func labelsAny(s Spec) map[string]any {
	out := map[string]any{}
	for k, v := range Labels(s) {
		out[k] = v
	}
	return out
}

// Backend Envoy Gateway: <IP узла>:<NodePort> (ADR 0021, 0022).
func Backend(s Spec) *unstructured.Unstructured {
	u := &unstructured.Unstructured{Object: map[string]any{
		"metadata": map[string]any{
			"name":      s.Name,
			"namespace": s.GatewayNamespace,
			"labels":    labelsAny(s),
		},
		"spec": map[string]any{
			"endpoints": []any{
				map[string]any{"ip": map[string]any{"address": s.NodeAddress, "port": int64(s.Port)}},
			},
		},
	}}
	u.SetGroupVersionKind(BackendGVK)
	return u
}

// HTTPRoute для хоста workload'а на общем Gateway (ADR 0017).
func HTTPRoute(s Spec) *unstructured.Unstructured {
	u := &unstructured.Unstructured{Object: map[string]any{
		"metadata": map[string]any{
			"name":      s.Name,
			"namespace": s.GatewayNamespace,
			"labels":    labelsAny(s),
		},
		"spec": map[string]any{
			"parentRefs": []any{map[string]any{"name": s.GatewayName}},
			"hostnames":  []any{s.Host},
			"rules": []any{
				map[string]any{
					"backendRefs": []any{
						map[string]any{
							"group":  BackendGVK.Group,
							"kind":   BackendGVK.Kind,
							"name":   s.Name,
							"weight": int64(100),
						},
					},
				},
			},
		},
	}}
	u.SetGroupVersionKind(HTTPRouteGVK)
	return u
}

// IsAdmissionDenied — отказ admission webhook'а (Kyverno, ADR 0026): 400 или 403 с
// текстом «denied the request». 5xx «failed calling webhook» сюда не попадает.
func IsAdmissionDenied(err error) bool {
	var status apierrors.APIStatus
	if !errors.As(err, &status) {
		return false
	}
	code := status.Status().Code
	if code != 400 && code != 403 {
		return false
	}
	return strings.Contains(status.Status().Message, "denied the request")
}

// Denial — разобранный текст отказа Kyverno.
type Denial struct {
	Policy  string
	Rule    string
	Message string
}

// Текст Kyverno:
//
//	resource Deployment/workloads/x was blocked due to the following policies
//
//	require-certification:
//	  certifications-present: 'federation.spbu/require-certification=pci-dss not satisfied: ...'
var denialRe = regexp.MustCompile(`(?m)^([A-Za-z0-9.-]+):\s*\n\s+([A-Za-z0-9.-]+):\s*'?([^'\n]*)'?\s*$`)

// ParseDenial достаёт политику, правило и сообщение; при незнакомом формате
// Policy = "kyverno", Message — весь текст.
func ParseDenial(msg string) Denial {
	if m := denialRe.FindStringSubmatch(msg); m != nil {
		return Denial{Policy: m[1], Rule: m[2], Message: strings.TrimSpace(m[3])}
	}
	return Denial{Policy: "kyverno", Message: strings.TrimSpace(msg)}
}
