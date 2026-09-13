"""Настройки сервиса из переменных окружения REGISTRY_*."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

# Роли и их токены (ADR 0018). Имя переменной: REGISTRY_TOKEN_<ROLE>.
ROLES = ("scheduler", "dr-controller", "admin")


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    database_url: str
    tokens: Mapping[str, str] = field(default_factory=dict)
    thanos_url: str = "http://thanos-query.observability.svc:10902"
    poll_interval_s: float = 30.0  # период опроса Thanos, spec §4.3
    poll_enabled: bool = True
    identity_check: bool = True  # проверка ConfigMap cluster-identity при регистрации
    kubeconfig_dir: Path = Path("/etc/registry/kubeconfig")
    # Пороги статуса degraded (ADR 0025): error rate из ADR 0007, latency budget из spec §1.3.
    error_rate_threshold: float = 0.05
    latency_budget_ms: float = 200.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        url = env.get("REGISTRY_DATABASE_URL", "")
        if not url:
            raise RuntimeError("REGISTRY_DATABASE_URL is not set")
        tokens = {
            role: env.get(f"REGISTRY_TOKEN_{role.upper().replace('-', '_')}", "") for role in ROLES
        }
        return cls(
            database_url=url,
            tokens=tokens,
            thanos_url=env.get("REGISTRY_THANOS_URL", cls.thanos_url),
            poll_interval_s=float(env.get("REGISTRY_POLL_INTERVAL_S", cls.poll_interval_s)),
            poll_enabled=_bool(env.get("REGISTRY_POLL_ENABLED", "true")),
            identity_check=_bool(env.get("REGISTRY_IDENTITY_CHECK", "true")),
            kubeconfig_dir=Path(env.get("REGISTRY_KUBECONFIG_DIR", str(cls.kubeconfig_dir))),
            error_rate_threshold=float(
                env.get("REGISTRY_ERROR_RATE_THRESHOLD", cls.error_rate_threshold)
            ),
            latency_budget_ms=float(env.get("REGISTRY_LATENCY_BUDGET_MS", cls.latency_budget_ms)),
        )
