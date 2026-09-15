package placement

import (
	"errors"
	"testing"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime/schema"
)

const kyvernoDenial = "admission webhook \"validate.kyverno.svc-fail\" denied the request: \n\n" +
	"resource Deployment/workloads/w-1 was blocked due to the following policies \n\n" +
	"require-certification:\n  certifications-present: 'federation.spbu/require-certification=pci-dss not satisfied: cluster cluster-b has certifications iso-27001'\n"

func sample() Spec {
	return Spec{
		Name: "w-7", Namespace: "workloads", Image: "federation/test-service:dev", Replicas: 1, Port: 30007,
		Scheduler: "meta", DecisionID: 12,
		Annotations: map[string]string{
			"federation.spbu/require-certification": "pci-dss",
			"kubectl.kubernetes.io/last-applied":    "x",
		},
		BackendURL: "http://backend.workloads.svc:8080", GatewayNamespace: "gateway", GatewayName: "stand",
		Host: "w-7.fed.local", NodeAddress: "192.168.117.4",
	}
}

func TestDeploymentShape(t *testing.T) {
	d := Deployment(sample())
	if d.Namespace != "workloads" || *d.Spec.Replicas != 1 {
		t.Fatalf("meta %+v", d.ObjectMeta)
	}
	if d.Labels[LabelWorkload] != "w-7" || d.Labels[LabelScheduler] != "meta" || d.Labels[LabelDecisionID] != "12" || d.Labels[LabelManagedBy] != ManagedBy {
		t.Fatalf("labels %v", d.Labels)
	}
	if len(d.Annotations) != 1 || d.Annotations["federation.spbu/require-certification"] != "pci-dss" {
		t.Fatalf("only require-* annotations must be copied, got %v", d.Annotations)
	}
	if d.Spec.Selector.MatchLabels[LabelWorkload] != "w-7" || d.Spec.Template.Labels[LabelWorkload] != "w-7" {
		t.Fatalf("selector %v", d.Spec.Selector)
	}
	c := d.Spec.Template.Spec.Containers[0]
	if c.Args[0] != "--role=frontend" || c.Env[0].Value != "w-7" || c.Env[1].Value != "http://backend.workloads.svc:8080" {
		t.Fatalf("container %+v", c)
	}
	if len(d.Spec.Template.Annotations) != 0 {
		t.Fatal("pod template must not carry requirement annotations (ADR 0009)")
	}
}

func TestServiceAndGatewayObjects(t *testing.T) {
	s := sample()
	svc := Service(s)
	if svc.Spec.Type != "NodePort" || svc.Spec.Ports[0].NodePort != 30007 || svc.Spec.Selector[LabelWorkload] != "w-7" {
		t.Fatalf("service %+v", svc.Spec)
	}
	b := Backend(s)
	if b.GroupVersionKind() != BackendGVK || b.GetNamespace() != "gateway" || b.GetName() != "w-7" {
		t.Fatalf("backend meta %v", b.Object["metadata"])
	}
	ep := b.Object["spec"].(map[string]any)["endpoints"].([]any)[0].(map[string]any)["ip"].(map[string]any)
	if ep["address"] != "192.168.117.4" || ep["port"] != int64(30007) {
		t.Fatalf("endpoint %v", ep)
	}
	r := HTTPRoute(s)
	if r.GroupVersionKind() != (schema.GroupVersionKind{Group: "gateway.networking.k8s.io", Version: "v1", Kind: "HTTPRoute"}) {
		t.Fatalf("gvk %v", r.GroupVersionKind())
	}
	spec := r.Object["spec"].(map[string]any)
	if spec["hostnames"].([]any)[0] != "w-7.fed.local" || spec["parentRefs"].([]any)[0].(map[string]any)["name"] != "stand" {
		t.Fatalf("route spec %v", spec)
	}
	ref := spec["rules"].([]any)[0].(map[string]any)["backendRefs"].([]any)[0].(map[string]any)
	if ref["kind"] != "Backend" || ref["name"] != "w-7" || ref["weight"] != int64(100) {
		t.Fatalf("backendRef %v", ref)
	}
	if b.GetLabels()[LabelDecisionID] != "12" || r.GetLabels()[LabelWorkload] != "w-7" {
		t.Fatalf("gateway labels %v %v", b.GetLabels(), r.GetLabels())
	}
}

func TestIsAdmissionDenied(t *testing.T) {
	if !IsAdmissionDenied(apierrors.NewBadRequest(kyvernoDenial)) {
		t.Fatal("400 with denial text must be a denial")
	}
	if !IsAdmissionDenied(apierrors.NewForbidden(schema.GroupResource{}, "w-1", errors.New(kyvernoDenial))) {
		t.Fatal("403 with denial text must be a denial")
	}
	if IsAdmissionDenied(apierrors.NewInternalError(errors.New(`failed calling webhook "validate.kyverno.svc-fail": connection refused`))) {
		t.Fatal("webhook failure is not a denial")
	}
	if IsAdmissionDenied(apierrors.NewBadRequest("spec.replicas: invalid")) || IsAdmissionDenied(errors.New(kyvernoDenial)) {
		t.Fatal("plain 400 or non-API error is not a denial")
	}
}

func TestParseDenial(t *testing.T) {
	d := ParseDenial(kyvernoDenial)
	if d.Policy != "require-certification" || d.Rule != "certifications-present" {
		t.Fatalf("denial %+v", d)
	}
	if d.Message != "federation.spbu/require-certification=pci-dss not satisfied: cluster cluster-b has certifications iso-27001" {
		t.Fatalf("message %q", d.Message)
	}
	other := ParseDenial("something else entirely")
	if other.Policy != "kyverno" || other.Message != "something else entirely" {
		t.Fatalf("fallback %+v", other)
	}
}
