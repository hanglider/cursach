"""Проверка доверия при регистрации (ADR 0009, 0018): ConfigMap cluster-identity
кластера должен совпадать с переданными атрибутами."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

IDENTITY_NAMESPACE = "federation-system"
IDENTITY_CONFIGMAP = "cluster-identity"


class IdentityError(Exception):
    """Идентичность прочитать не удалось: нет kubeconfig, кластер недоступен, нет ConfigMap."""


@dataclass(frozen=True)
class Identity:
    cluster_id: str
    region: str
    provider: str
    certifications: tuple[str, ...]


class IdentityReader(Protocol):
    async def read(self, api_endpoint: str) -> Identity: ...


def parse_certifications(raw: str) -> tuple[str, ...]:
    return tuple(c.strip() for c in raw.split(",") if c.strip())


def mismatches(
    identity: Identity, *, cluster_id: str, region: str, provider: str, certifications: list[str]
) -> list[str]:
    diff = []
    if identity.cluster_id != cluster_id:
        diff.append(f"cluster-id: cluster has {identity.cluster_id!r}, request has {cluster_id!r}")
    if identity.region != region:
        diff.append(f"region: cluster has {identity.region!r}, request has {region!r}")
    if identity.provider != provider:
        diff.append(f"provider: cluster has {identity.provider!r}, request has {provider!r}")
    if set(identity.certifications) != set(certifications):
        diff.append(
            f"certifications: cluster has {sorted(identity.certifications)}, "
            f"request has {sorted(certifications)}"
        )
    return diff


class StaticIdentityReader:
    """Для тестов: идентичности по api_endpoint."""

    def __init__(self, identities: dict[str, Identity]):
        self.identities = identities

    async def read(self, api_endpoint: str) -> Identity:
        try:
            return self.identities[api_endpoint]
        except KeyError:
            raise IdentityError(f"no kubeconfig for {api_endpoint}") from None


def kubeconfig_for(directory: Path, api_endpoint: str) -> Path:
    """kubeconfig из каталога, у которого сервер кластера — api_endpoint (ADR 0018, 0021).
    Общий для проверки идентичности и watcher'а нарушений."""
    wanted = api_endpoint.rstrip("/")
    for path in sorted(directory.glob("*.kubeconfig")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        servers = {
            str(c.get("cluster", {}).get("server", "")).rstrip("/") for c in doc.get("clusters", [])
        }
        if wanted in servers:
            return path
    raise IdentityError(f"no kubeconfig for {api_endpoint} in {directory}")


class KubeconfigIdentityReader:
    """Ищет в каталоге kubeconfig с сервером api_endpoint и читает ConfigMap через него."""

    def __init__(self, directory: Path):
        self.directory = directory

    async def read(self, api_endpoint: str) -> Identity:
        return await asyncio.to_thread(self._read, api_endpoint)

    def _read(self, api_endpoint: str) -> Identity:
        # Импорт здесь: клиент kubernetes тяжёлый и не нужен в тестах правил.
        from kubernetes import client, config
        from kubernetes.client.exceptions import ApiException

        path = kubeconfig_for(self.directory, api_endpoint)
        try:
            api = config.new_client_from_config(config_file=str(path))
            cm = client.CoreV1Api(api).read_namespaced_config_map(
                IDENTITY_CONFIGMAP, IDENTITY_NAMESPACE, _request_timeout=10
            )
        except ApiException as e:
            raise IdentityError(f"{api_endpoint}: {e.status} {e.reason}") from e
        except Exception as e:  # noqa: BLE001 — urllib3/ssl: кластер недоступен
            raise IdentityError(f"{api_endpoint}: {e.__class__.__name__}: {e}") from e
        data = cm.data or {}
        return Identity(
            cluster_id=data.get("cluster-id", ""),
            region=data.get("region", ""),
            provider=data.get("provider", ""),
            certifications=parse_certifications(data.get("certifications", "")),
        )
