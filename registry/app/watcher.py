"""Watcher нарушений политик (ADR 0019, 0026): раз в 10 с читает из каждого кластера
федерации события Kyverno `PolicyViolation` и PolicyReport'ы с `result: fail` и пишет
их в policy_violations. Ловит размещения в обход планировщика (сценарий 3).

Не watch-поток, а периодический list: отказ кластера переживается без переподключений,
а дедупликация (source_key) нужна в любом случае. occurred_at — время из Kyverno,
details.detected_at — момент записи: разница и есть задержка фиксации нарушения."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import asyncpg

from . import repo
from .config import Settings
from .identity import kubeconfig_for
from .log import event
from .models import Requirements

log = logging.getLogger("registry.watcher")

WORKLOAD_KINDS = ("Deployment", "Job")
POLICY_KINDS = ("ClusterPolicy", "Policy")
REPORT_GROUP, REPORT_VERSION, REPORT_PLURAL = "wgpolicyk8s.io", "v1alpha2", "policyreports"

# Контракт сообщения политик (stand/policies/*.yaml):
#   federation.spbu/require-region=<v> not satisfied: ...
#   federation.spbu/require-certification=<v> not satisfied: ...
_REQUIREMENT_RE = re.compile(r"federation\.spbu/require-(region|certification)=(\S+) not satisfied")
# Событие отказа admission (kyverno pkg/event): "Kind ns/name: [rule] fail (blocked); <message>"
_BLOCKED_RE = re.compile(
    r"^(?P<kind>\w+) (?P<namespace>[\w.-]+)/(?P<name>[\w.-]+): \[(?P<rule>[^\]]+)\] (?P<status>\w+)"
    r"(?P<blocked> \(blocked\))?(?:; (?P<message>.*))?$",
    re.S,
)
# Событие background-скана на самом ресурсе: "policy <policy>/<rule> fail: <message>"
_SCAN_RE = re.compile(
    r"^policy (?P<policy>[\w.-]+)/(?P<rule>[\w.-]+) (?P<status>\w+): (?P<message>.*)$", re.S
)


@dataclass(frozen=True)
class Observed:
    """Одно нарушение, увиденное в кластере."""

    key: str  # уникален в кластере: uid события или <uid ресурса>/<policy>/<rule> отчёта
    source: str  # event | policyreport
    occurred_at: datetime
    workload_ref: str  # namespace/name
    kind: str
    policy_name: str
    rule: str
    message: str
    action: str | None = None


class ViolationSource(Protocol):
    async def fetch(self, cluster_id: str, api_endpoint: str) -> list[Observed]:
        """Нарушения, видимые в кластере сейчас. Исключение — кластер недоступен."""
        ...


def _timestamp(raw: Any) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, dict):  # PolicyReport: {"seconds": ..., "nanos": ...}
        try:
            return datetime.fromtimestamp(int(raw.get("seconds", 0)), UTC)
        except (TypeError, ValueError, OSError):
            return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _ref(obj: dict[str, Any] | None) -> tuple[str, str, str] | None:
    """(kind, namespace, name) из ObjectReference."""
    if not obj or not obj.get("kind") or not obj.get("name"):
        return None
    return str(obj["kind"]), str(obj.get("namespace") or ""), str(obj["name"])


def parse_event(ev: dict[str, Any]) -> Observed | None:
    """Событие events.k8s.io/v1 (сериализованное в dict) → нарушение или None.

    Отказ admission: regarding — политика, related — ресурс (его нет в кластере),
    note по _BLOCKED_RE. Background-скан: regarding — ресурс, note по _SCAN_RE.
    """
    if ev.get("reason") != "PolicyViolation":
        return None
    note = str(ev.get("note") or "")
    regarding = _ref(ev.get("regarding"))
    if regarding is None:
        return None
    kind, namespace, name = regarding
    if kind in POLICY_KINDS:
        policy_name, rule, message = name, "", note
        m = _BLOCKED_RE.match(note)
        if m:
            rule, message = m["rule"], m["message"] or ""
        related = _ref(ev.get("related"))
        if related is None and m:
            related = (m["kind"], m["namespace"], m["name"])
        if related is None:
            return None
        kind, namespace, name = related
    elif kind in WORKLOAD_KINDS:
        m = _SCAN_RE.match(note)
        if not m:
            return None
        policy_name, rule, message = m["policy"].rsplit("/", 1)[-1], m["rule"], m["message"]
    else:
        return None
    if kind not in WORKLOAD_KINDS:
        return None
    meta = ev.get("metadata") or {}
    occurred_at = (
        _timestamp(ev.get("eventTime"))
        or _timestamp(ev.get("deprecatedLastTimestamp"))
        or _timestamp(meta.get("creationTimestamp"))
        or datetime.now(UTC)
    )
    return Observed(
        key=str(meta.get("uid") or meta.get("name") or ""),
        source="event",
        occurred_at=occurred_at,
        workload_ref=f"{namespace}/{name}",
        kind=kind,
        policy_name=policy_name,
        rule=rule,
        message=message.strip(),
        action=ev.get("action"),
    )


def parse_report(report: dict[str, Any]) -> list[Observed]:
    """PolicyReport (один на ресурс) → нарушения по результатам `fail`."""
    meta = report.get("metadata") or {}
    scope = report.get("scope") or {}
    out: list[Observed] = []
    for r in report.get("results") or []:
        if r.get("result") != "fail":
            continue
        resource = (r.get("resources") or [scope])[0] or {}
        ref = _ref(resource)
        if ref is None or ref[0] not in WORKLOAD_KINDS:
            continue
        kind, namespace, name = ref
        namespace = namespace or str(meta.get("namespace") or "")
        uid = str(resource.get("uid") or meta.get("uid") or meta.get("name") or "")
        policy = str(r.get("policy") or "").rsplit("/", 1)[-1]
        rule = str(r.get("rule") or "")
        out.append(
            Observed(
                key=f"{uid}/{policy}/{rule}",
                source="policyreport",
                occurred_at=_timestamp(r.get("timestamp"))
                or _timestamp(meta.get("creationTimestamp"))
                or datetime.now(UTC),
                workload_ref=f"{namespace}/{name}",
                kind=kind,
                policy_name=policy,
                rule=rule,
                message=str(r.get("message") or "").strip(),
            )
        )
    return out


def parse_requirements(message: str) -> Requirements | None:
    """Hard constraints отклонённого workload'а из сообщения политики (контракт выше)."""
    found = _REQUIREMENT_RE.findall(message)
    if not found:
        return None
    req = Requirements()
    for kind, value in found:
        if kind == "region":
            req.region = value
        else:
            req.certifications = [c for c in value.split(",") if c]
    return req


class KubeViolationSource:
    """События и PolicyReport'ы кластера через kubeconfig, подобранный по api_endpoint."""

    def __init__(self, directory: Path, timeout_s: float = 10.0):
        self.directory = directory
        self._timeout = timeout_s

    async def fetch(self, cluster_id: str, api_endpoint: str) -> list[Observed]:
        return await asyncio.to_thread(self._fetch, api_endpoint)

    def _fetch(self, api_endpoint: str) -> list[Observed]:
        # Импорт здесь: клиент kubernetes тяжёлый и не нужен в тестах разбора.
        from kubernetes import client, config
        from kubernetes.client.exceptions import ApiException

        api = config.new_client_from_config(
            config_file=str(kubeconfig_for(self.directory, api_endpoint))
        )
        try:
            events = client.EventsV1Api(api).list_event_for_all_namespaces(
                field_selector="reason=PolicyViolation", _request_timeout=self._timeout
            )
            try:
                reports = client.CustomObjectsApi(api).list_cluster_custom_object(
                    REPORT_GROUP, REPORT_VERSION, REPORT_PLURAL, _request_timeout=self._timeout
                )
            except ApiException as e:
                if e.status != 404:  # нет CRD — Kyverno не установлен, отчётов нет
                    raise
                reports = {}
            items = api.sanitize_for_serialization(events).get("items") or []
        finally:
            api.close()
        out = [obs for obs in (parse_event(ev) for ev in items) if obs is not None]
        for report in reports.get("items") or []:
            out.extend(parse_report(report))
        return out


class StaticViolationSource:
    """Источник для тестов: нарушения по кластерам; кластеры из `fail` недоступны."""

    def __init__(
        self, observed: dict[str, list[Observed]] | None = None, fail: set[str] | None = None
    ):
        self.observed = observed or {}
        self.fail = fail or set()

    async def fetch(self, cluster_id: str, api_endpoint: str) -> list[Observed]:
        if cluster_id in self.fail:
            raise RuntimeError(f"{cluster_id} unavailable")
        return list(self.observed.get(cluster_id, []))


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def watch_once(pool: asyncpg.Pool, source: ViolationSource, settings: Settings) -> int:
    """Один цикл по всем зарегистрированным кластерам. Возвращает число новых записей."""
    async with pool.acquire() as conn:
        clusters = await repo.cluster_endpoints(conn)
    recorded = 0
    for cluster_id, api_endpoint in clusters.items():
        try:
            observed = await source.fetch(cluster_id, api_endpoint)
        except Exception as e:  # noqa: BLE001 — отказ одного кластера не мешает остальным
            event(log, "violations_fetch_failed", logging.WARNING, cluster=cluster_id, error=str(e))
            continue
        if not observed:
            continue
        detected_at = datetime.now(UTC)
        keyed = {f"{obs.source}/{cluster_id}/{obs.key}": obs for obs in observed if obs.key}
        async with pool.acquire() as conn:
            known = await repo.known_source_keys(conn, list(keyed))
            for source_key, obs in keyed.items():
                if source_key in known:
                    continue
                details: dict[str, Any] = {
                    "reported_via": "watcher",
                    "source": obs.source,
                    "kind": obs.kind,
                    "rule": obs.rule,
                    "message": obs.message,
                    "detected_at": _iso(detected_at),
                }
                if obs.action:
                    details["action"] = obs.action
                requirements = parse_requirements(obs.message)
                if requirements is not None:
                    details["requirements"] = requirements.model_dump()
                    details["eligible_clusters"] = await repo.eligible_cluster_ids(
                        conn, requirements.to_constraints()
                    )
                row = await repo.insert_violation(
                    conn,
                    workload_ref=obs.workload_ref,
                    cluster_id=cluster_id,
                    policy_name=obs.policy_name,
                    detected_by="kyverno",
                    details=details,
                    occurred_at=obs.occurred_at,
                    source_key=source_key,
                )
                if row is None:
                    continue  # параллельная запись того же ключа
                recorded += 1
                event(
                    log,
                    "violation_recorded",
                    id=row["id"],
                    cluster=cluster_id,
                    workload=obs.workload_ref,
                    policy=obs.policy_name,
                    source=obs.source,
                    detection_latency_s=round((detected_at - obs.occurred_at).total_seconds(), 3),
                    eligible_clusters=details.get("eligible_clusters"),
                )
    return recorded


async def run(pool: asyncpg.Pool, source: ViolationSource, settings: Settings) -> None:
    """Бесконечный цикл до отмены задачи."""
    while True:
        try:
            await watch_once(pool, source, settings)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — один неудачный цикл не останавливает watcher
            event(log, "watch_failed", logging.ERROR, error=repr(e))
        await asyncio.sleep(settings.watch_interval_s)
