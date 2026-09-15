// Package v1alpha1 — API группы federation.spbu: CR Workload, запрос на размещение
// workload'а в федерации (ADR 0015, 0027). Живёт в control-кластере.
//
// +kubebuilder:object:generate=true
// +groupName=federation.spbu
package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
)

var (
	// GroupVersion — группа и версия API.
	GroupVersion = schema.GroupVersion{Group: "federation.spbu", Version: "v1alpha1"}

	// SchemeBuilder регистрирует типы группы в scheme.
	SchemeBuilder = runtime.NewSchemeBuilder(addKnownTypes)

	// AddToScheme добавляет типы группы в scheme.
	AddToScheme = SchemeBuilder.AddToScheme
)

func addKnownTypes(s *runtime.Scheme) error {
	s.AddKnownTypes(GroupVersion, &Workload{}, &WorkloadList{})
	metav1.AddToGroupVersion(s, GroupVersion)
	return nil
}
