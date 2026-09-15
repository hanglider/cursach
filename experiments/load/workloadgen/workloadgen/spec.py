"""Чистые функции: форма workload'а и вывод порта. Без обращений к API."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

NAMESPACE = "workloads"
GATEWAY_NAMESPACE = "gateway"
GATEWAY_NAME = "stand"
DOMAIN = "fed.local"
IMAGE = "federation/test-service:dev"
MANAGED_BY_LABEL = "federation.spbu/managed-by"
MANAGED_BY = "workloadgen"

NODEPORT_BASE = 30000
NODEPORT_SPAN = 2000  # 30000..31999; 30080 занят шлюзом только в control

_NAME_RE = re.compile(r"^[a-z]([a-z0-9-]*[a-z0-9])?$")


@dataclass(frozen=True)
class Workload:
    name: str
    cluster: str
    node_port: int
    scheduler: str = "generator"
    # Требования federation.spbu/require-* (ADR 0009): аннотации самого Deployment'а,
    # их проверяет Kyverno в кластере (ADR 0019).
    annotations: dict[str, str] = field(default_factory=dict)

    @property
    def host(self) -> str:
        return f"{self.name}.{DOMAIN}"


def validate_name(name: str) -> str:
    if not _NAME_RE.match(name) or len(name) > 40:
        raise ValueError(f"bad workload name {name!r}")
    return name


def node_port_for(n: int) -> int:
    """Порт workload'а w-<n>: детерминирован, без хранения состояния."""
    if n < 0:
        raise ValueError("n must be >= 0")
    return NODEPORT_BASE + (n % NODEPORT_SPAN)


def labels(w: Workload) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": "test-service",
        "app.kubernetes.io/component": "frontend",
        "federation.spbu/workload": w.name,
        "federation.spbu/scheduler": w.scheduler,
        MANAGED_BY_LABEL: MANAGED_BY,
    }


def parse_annotation(raw: str) -> tuple[str, str]:
    """`KEY=VALUE` из командной строки."""
    key, sep, value = raw.partition("=")
    if not sep or not key.strip():
        raise ValueError(f"bad annotation {raw!r}, expected KEY=VALUE")
    return key.strip(), value.strip()


def deployment(w: Workload, image: str = IMAGE) -> dict:
    lbl = labels(w)
    metadata: dict = {"name": w.name, "namespace": NAMESPACE, "labels": lbl}
    if w.annotations:
        metadata["annotations"] = dict(w.annotations)
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": metadata,
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"federation.spbu/workload": w.name}},
            "template": {
                "metadata": {"labels": lbl},
                "spec": {
                    "containers": [
                        {
                            "name": "frontend",
                            "image": image,
                            "imagePullPolicy": "Never",
                            "args": ["--role=frontend"],
                            "env": [
                                {"name": "WORKLOAD", "value": w.name},
                                {
                                    "name": "BACKEND_URL",
                                    "value": f"http://backend.{NAMESPACE}.svc:8080",
                                },
                            ],
                            "ports": [{"name": "http", "containerPort": 8080}],
                            "readinessProbe": {
                                "httpGet": {"path": "/readyz", "port": "http"},
                                "periodSeconds": 2,
                            },
                            "livenessProbe": {
                                "httpGet": {"path": "/healthz", "port": "http"},
                                "periodSeconds": 10,
                            },
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "32Mi"},
                                "limits": {"cpu": "500m", "memory": "64Mi"},
                            },
                        }
                    ]
                },
            },
        },
    }


def service(w: Workload) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": w.name, "namespace": NAMESPACE, "labels": labels(w)},
        "spec": {
            "type": "NodePort",
            "selector": {"federation.spbu/workload": w.name},
            "ports": [
                {"name": "http", "port": 8080, "targetPort": "http", "nodePort": w.node_port}
            ],
        },
    }


def backend(w: Workload, node_address: str) -> dict:
    """Backend Envoy Gateway: точка входа кластера федерации по docker-сети (ADR 0021)."""
    return {
        "apiVersion": "gateway.envoyproxy.io/v1alpha1",
        "kind": "Backend",
        "metadata": {"name": w.name, "namespace": GATEWAY_NAMESPACE, "labels": labels(w)},
        "spec": {"endpoints": [{"ip": {"address": node_address, "port": w.node_port}}]},
    }


def http_route(w: Workload) -> dict:
    return {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {"name": w.name, "namespace": GATEWAY_NAMESPACE, "labels": labels(w)},
        "spec": {
            "parentRefs": [{"name": GATEWAY_NAME}],
            "hostnames": [w.host],
            "rules": [
                {
                    "backendRefs": [
                        {
                            "group": "gateway.envoyproxy.io",
                            "kind": "Backend",
                            "name": w.name,
                            "weight": 100,
                        }
                    ]
                }
            ],
        },
    }
