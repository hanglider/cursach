package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// Фазы размещения (ADR 0027). Placed и Rejected терминальны; NoCandidates и Error
// повторяются планировщиком.
const (
	PhasePending      = "Pending"
	PhasePlaced       = "Placed"
	PhaseRejected     = "Rejected"
	PhaseNoCandidates = "NoCandidates"
	PhaseError        = "Error"
)

// Finalizer — планировщик убирает Deployment, Service и маршрут при удалении CR.
const Finalizer = "federation.spbu/placement"

// WorkloadSpec — что разместить. Hard constraints задаются аннотациями
// federation.spbu/require-* на самом CR (ADR 0009) и копируются на Deployment.
type WorkloadSpec struct {
	// NodePort Service'а в кластере федерации; вход шлюза (ADR 0022).
	// +kubebuilder:validation:Minimum=30000
	// +kubebuilder:validation:Maximum=32767
	Port int32 `json:"port"`

	// Образ frontend'а тестового сервиса; по умолчанию — образ стенда.
	// +optional
	Image string `json:"image,omitempty"`

	// Число реплик frontend'а.
	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=1
	Replicas *int32 `json:"replicas,omitempty"`
}

// WorkloadStatus — решение планировщика и состояние размещения.
type WorkloadStatus struct {
	// +optional
	Phase string `json:"phase,omitempty"`
	// Выбранный кластер (id в каталоге).
	// +optional
	Cluster string `json:"cluster,omitempty"`
	// id строки placement_decisions в каталоге.
	// +optional
	DecisionID *int64 `json:"decisionId,omitempty"`
	// Режим планировщика, принявшего решение: meta | naive.
	// +optional
	Scheduler string `json:"scheduler,omitempty"`
	// Хост маршрута на шлюзе.
	// +optional
	Host string `json:"host,omitempty"`
	// Адрес узла кластера, куда указывает Backend шлюза.
	// +optional
	NodeAddress string `json:"nodeAddress,omitempty"`
	// Причина текущей фазы: текст отказа, ошибка, «нет кандидатов».
	// +optional
	Message string `json:"message,omitempty"`
	// +optional
	PlacedAt *metav1.Time `json:"placedAt,omitempty"`
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
}

// Workload — запрос на размещение экземпляра frontend'а (ADR 0015).
//
// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=wl
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Cluster",type=string,JSONPath=`.status.cluster`
// +kubebuilder:printcolumn:name="Scheduler",type=string,JSONPath=`.status.scheduler`
// +kubebuilder:printcolumn:name="Decision",type=integer,JSONPath=`.status.decisionId`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`
type Workload struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   WorkloadSpec   `json:"spec"`
	Status WorkloadStatus `json:"status,omitempty"`
}

// WorkloadList — список Workload.
//
// +kubebuilder:object:root=true
type WorkloadList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Workload `json:"items"`
}
