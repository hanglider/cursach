"""Доступ к стенду: kubeconfig'и, адреса узлов, создание и удаление объектов."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import requests
import yaml
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from . import spec

log = logging.getLogger("workloadgen")

# Режимы размещения (ADR 0027): через планировщик (CR Workload в control) или напрямую
# в кластер — обход планировщика для дымовых тестов и сценария 3.
MODE_SCHEDULER = "scheduler"
MODE_DIRECT = "direct"
MODES = (MODE_SCHEDULER, MODE_DIRECT)


class PlacementRejected(Exception):
    """Admission webhook кластера (Kyverno, ADR 0019) отклонил Deployment."""

    def __init__(self, workload: str, cluster: str, message: str):
        super().__init__(message)
        self.workload = workload
        self.cluster = cluster
        self.message = message


def _denial_message(e: ApiException) -> str | None:
    """Текст отказа admission из тела ответа API-сервера; None, если это другая ошибка."""
    if e.status not in (400, 403):
        return None
    try:
        body = json.loads(e.body or "")
        message = body.get("message", "")
    except (TypeError, ValueError):
        message = str(e.body or "")
    return message if "denied the request" in message else None


def _event(event: str, **fields: object) -> None:
    """Структурированный лог: одна JSON-строка на событие, для последующего анализа."""
    log.info(json.dumps({"ts": time.time(), "event": event, **fields}, ensure_ascii=False))


def federation_clusters(clusters_yaml: Path) -> list[str]:
    data = yaml.safe_load(clusters_yaml.read_text(encoding="utf-8"))
    return [c["id"] for c in data["clusters"]]


def node_address(cluster: str) -> str:
    """IP узла kind в docker-сети (ADR 0022).

    Имена контейнеров из подов резолвятся на OrbStack, но это свойство
    рантайма, а не docker; IP работает везде.
    """
    out = subprocess.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            f"{cluster}-control-plane",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not out:
        raise RuntimeError(f"no docker IP for {cluster}-control-plane")
    return out


class Cluster:
    """Один kubeconfig и клиенты к нему."""

    def __init__(self, name: str, kubeconfig: Path):
        self.name = name
        self.api = config.new_client_from_config(config_file=str(kubeconfig))
        self.apps = client.AppsV1Api(self.api)
        self.core = client.CoreV1Api(self.api)
        self.custom = client.CustomObjectsApi(self.api)

    # --- Deployment / Service -------------------------------------------------

    def apply_deployment(self, body: dict) -> None:
        ns, name = body["metadata"]["namespace"], body["metadata"]["name"]
        try:
            self.apps.create_namespaced_deployment(ns, body)
        except ApiException as e:
            if e.status != 409:
                raise
            self.apps.delete_namespaced_deployment(name, ns, propagation_policy="Background")
            _wait_gone(lambda: self.apps.read_namespaced_deployment(name, ns))
            self.apps.create_namespaced_deployment(ns, body)

    def apply_service(self, body: dict) -> None:
        ns, name = body["metadata"]["namespace"], body["metadata"]["name"]
        try:
            self.core.create_namespaced_service(ns, body)
        except ApiException as e:
            if e.status != 409:
                raise
            self.core.delete_namespaced_service(name, ns)
            _wait_gone(lambda: self.core.read_namespaced_service(name, ns))
            self.core.create_namespaced_service(ns, body)

    def delete_workload(self, name: str) -> None:
        _ignore_404(
            lambda: self.apps.delete_namespaced_deployment(
                name, spec.NAMESPACE, propagation_policy="Background"
            )
        )
        _ignore_404(lambda: self.core.delete_namespaced_service(name, spec.NAMESPACE))

    def deployment_available(self, name: str) -> bool:
        try:
            d = self.apps.read_namespaced_deployment_status(name, spec.NAMESPACE)
        except ApiException as e:
            if e.status == 404:
                return False
            raise
        return bool(d.status and (d.status.available_replicas or 0) >= 1)

    def managed_workloads(self) -> list[str]:
        sel = f"{spec.MANAGED_BY_LABEL}={spec.MANAGED_BY}"
        items = self.apps.list_namespaced_deployment(spec.NAMESPACE, label_selector=sel).items
        return [d.metadata.name for d in items]

    # --- Gateway API (control) -----------------------------------------------

    _ROUTE = ("gateway.networking.k8s.io", "v1", "httproutes")
    _BACKEND = ("gateway.envoyproxy.io", "v1alpha1", "backends")

    def apply_custom(self, gvp: tuple[str, str, str], body: dict) -> None:
        group, version, plural = gvp
        ns, name = body["metadata"]["namespace"], body["metadata"]["name"]
        try:
            self.custom.create_namespaced_custom_object(group, version, ns, plural, body)
        except ApiException as e:
            if e.status != 409:
                raise
            self.custom.delete_namespaced_custom_object(group, version, ns, plural, name)
            _wait_gone(
                lambda: self.custom.get_namespaced_custom_object(group, version, ns, plural, name)
            )
            self.custom.create_namespaced_custom_object(group, version, ns, plural, body)

    def apply_route(self, w: spec.Workload, node_addr: str) -> None:
        self.apply_custom(self._BACKEND, spec.backend(w, node_addr))
        self.apply_custom(self._ROUTE, spec.http_route(w))

    def delete_route(self, name: str) -> None:
        for group, version, plural in (self._ROUTE, self._BACKEND):
            _ignore_404(
                lambda g=group, v=version, pl=plural: self.custom.delete_namespaced_custom_object(
                    g, v, spec.GATEWAY_NAMESPACE, pl, name
                )
            )

    def managed_routes(self) -> list[str]:
        sel = f"{spec.MANAGED_BY_LABEL}={spec.MANAGED_BY}"
        group, version, plural = self._ROUTE
        items = self.custom.list_namespaced_custom_object(
            group, version, spec.GATEWAY_NAMESPACE, plural, label_selector=sel
        )["items"]
        return [i["metadata"]["name"] for i in items]

    # --- CR Workload планировщика (control, ADR 0027) --------------------------

    _WORKLOAD = (spec.WORKLOAD_GROUP, spec.WORKLOAD_VERSION, spec.WORKLOAD_PLURAL)

    def apply_workload_cr(self, body: dict) -> None:
        self.apply_custom(self._WORKLOAD, body)

    def workload_status(self, name: str) -> dict | None:
        group, version, plural = self._WORKLOAD
        try:
            obj = self.custom.get_namespaced_custom_object(
                group, version, spec.CONTROL_NAMESPACE, plural, name
            )
        except ApiException as e:
            if e.status == 404:
                return None
            raise
        return obj.get("status") or {}

    def delete_workload_cr(self, name: str) -> None:
        group, version, plural = self._WORKLOAD
        _ignore_404(
            lambda: self.custom.delete_namespaced_custom_object(
                group, version, spec.CONTROL_NAMESPACE, plural, name
            )
        )

    def managed_workload_crs(self) -> list[str]:
        sel = f"{spec.MANAGED_BY_LABEL}={spec.MANAGED_BY}"
        group, version, plural = self._WORKLOAD
        try:
            items = self.custom.list_namespaced_custom_object(
                group, version, spec.CONTROL_NAMESPACE, plural, label_selector=sel
            )["items"]
        except ApiException as e:
            if e.status == 404:  # CRD не установлен: планировщика на стенде нет
                return []
            raise
        return [i["metadata"]["name"] for i in items]


def _ignore_404(fn) -> None:
    try:
        fn()
    except ApiException as e:
        if e.status != 404:
            raise


def _wait_gone(fn, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            fn()
        except ApiException as e:
            if e.status == 404:
                return
            raise
        time.sleep(0.5)
    raise TimeoutError("object still exists")


class Stand:
    """Control-кластер + кластеры федерации."""

    def __init__(
        self,
        kubeconfig_dir: Path,
        clusters: list[str],
        gateway_url: str,
        mode: str = MODE_DIRECT,
    ):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
        self.mode = mode
        self.control = Cluster("control", kubeconfig_dir / "control.kubeconfig")
        self.federation = {c: Cluster(c, kubeconfig_dir / f"{c}.kubeconfig") for c in clusters}
        self.node_addr = {c: node_address(c) for c in clusters}
        self.gateway_url = gateway_url.rstrip("/")

    def place(self, w: spec.Workload, image: str = spec.IMAGE) -> None:
        if self.mode == MODE_SCHEDULER:
            # Кластер выбирает планировщик; исход — в status CR (wait_ready).
            self.control.apply_workload_cr(spec.workload_cr(w, image))
            _event("requested", workload=w.name, node_port=w.node_port)
            return
        cl = self.federation[w.cluster]
        try:
            cl.apply_deployment(spec.deployment(w, image))
        except ApiException as e:
            denial = _denial_message(e)
            if denial is None:
                raise
            # Отклонённый ресурс не существует: Service и маршрут не создаём.
            _event("rejected", workload=w.name, cluster=w.cluster, message=denial)
            raise PlacementRejected(w.name, w.cluster, denial) from e
        cl.apply_service(spec.service(w))
        self.control.apply_route(w, self.node_addr[w.cluster])
        _event("placed", workload=w.name, cluster=w.cluster, node_port=w.node_port)

    def remove(self, name: str, cluster: str | None = None) -> None:
        if self.mode == MODE_SCHEDULER:
            # Финализатор планировщика убирает Deployment, Service и маршрут.
            self.control.delete_workload_cr(name)
            _event("removed", workload=name, cluster=cluster, via="scheduler")
            return
        self.control.delete_route(name)
        targets = [self.federation[cluster]] if cluster else self.federation.values()
        for cl in targets:
            cl.delete_workload(name)
        _event("removed", workload=name, cluster=cluster)

    def wait_placed(self, w: spec.Workload, deadline: float) -> spec.Workload | None:
        """Ждать решения планировщика: Placed → workload с кластером; Rejected → исключение;
        иначе (Pending, NoCandidates, Error) — до дедлайна, затем None."""
        last = None
        while time.monotonic() < deadline:
            st = self.control.workload_status(w.name) or {}
            phase = st.get("phase")
            if phase == "Placed":
                _event(
                    "placed",
                    workload=w.name,
                    cluster=st.get("cluster"),
                    scheduler=st.get("scheduler"),
                    decision_id=st.get("decisionId"),
                    node_port=w.node_port,
                )
                return replace(w, cluster=st.get("cluster", ""), scheduler=st.get("scheduler", ""))
            if phase == "Rejected":
                _event(
                    "rejected",
                    workload=w.name,
                    cluster=st.get("cluster"),
                    message=st.get("message"),
                )
                raise PlacementRejected(w.name, st.get("cluster", ""), st.get("message", ""))
            if (phase, st.get("message")) != last:
                last = (phase, st.get("message"))
                _event("pending", workload=w.name, phase=phase, message=st.get("message"))
            time.sleep(1)
        _event("not_placed", workload=w.name, phase=last[0] if last else None)
        return None

    def wait_ready(self, w: spec.Workload, timeout: float = 90) -> spec.Workload | None:
        """Готов = Deployment доступен и шлюз отдаёт 200 по хосту workload'а.
        В режиме scheduler сначала ждёт решения; возвращает workload с кластером."""
        deadline = time.monotonic() + timeout
        if self.mode == MODE_SCHEDULER:
            placed = self.wait_placed(w, deadline)
            if placed is None:
                return None
            w = placed
        cl = self.federation[w.cluster]
        while time.monotonic() < deadline:
            if cl.deployment_available(w.name) and self.probe(w.host):
                _event("ready", workload=w.name, cluster=w.cluster)
                return w
            time.sleep(1)
        _event("not_ready", workload=w.name, cluster=w.cluster, timeout=timeout)
        return None

    def probe(self, host: str) -> bool:
        try:
            r = requests.get(self.gateway_url + "/", headers={"Host": host}, timeout=3)
        except requests.RequestException:
            return False
        return r.status_code == 200

    def cleanup(self) -> list[str]:
        """Удалить всё, что помечено как созданное генератором: CR планировщика и
        объекты прямого режима, независимо от текущего режима."""
        crs = set(self.control.managed_workload_crs())
        for name in sorted(crs):
            self.control.delete_workload_cr(name)
        names: set[str] = set(self.control.managed_routes())
        for cl in self.federation.values():
            names.update(cl.managed_workloads())
        for name in sorted(names):
            self.control.delete_route(name)
            for cl in self.federation.values():
                cl.delete_workload(name)
        removed = sorted(crs | names)
        if removed:
            _event("cleanup", workloads=removed)
        return removed
