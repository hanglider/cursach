import pytest

from workloadgen import spec


def test_node_port_deterministic_and_in_range():
    assert spec.node_port_for(1) == 30001
    assert spec.node_port_for(2000) == 30000
    for n in (0, 7, 1999, 4321):
        assert 30000 <= spec.node_port_for(n) < 32000
    with pytest.raises(ValueError):
        spec.node_port_for(-1)


def test_validate_name():
    assert spec.validate_name("w-12") == "w-12"
    for bad in ("W", "-x", "a_b", "x" * 41):
        with pytest.raises(ValueError):
            spec.validate_name(bad)


def test_objects_are_consistent():
    w = spec.Workload("w-3", "cluster-b", 30003)
    d, s = spec.deployment(w), spec.service(w)
    b, r = spec.backend(w, "10.0.0.5"), spec.http_route(w)

    assert d["spec"]["selector"]["matchLabels"] == s["spec"]["selector"]
    assert s["spec"]["ports"][0]["nodePort"] == 30003
    assert b["spec"]["endpoints"][0]["ip"] == {"address": "10.0.0.5", "port": 30003}
    assert r["spec"]["hostnames"] == ["w-3.fed.local"]
    assert r["spec"]["rules"][0]["backendRefs"][0]["name"] == "w-3"
    for obj in (d, s, b, r):
        assert obj["metadata"]["labels"][spec.MANAGED_BY_LABEL] == spec.MANAGED_BY
        assert obj["metadata"]["labels"]["federation.spbu/workload"] == "w-3"

    env = {e["name"]: e["value"] for e in d["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["WORKLOAD"] == "w-3"
    assert env["BACKEND_URL"] == "http://backend.workloads.svc:8080"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://otel-collector.observability.svc:4318"


def test_annotations_land_on_deployment_metadata() -> None:
    w = spec.Workload(
        "pv-1", "cluster-b", 30001, annotations={"federation.spbu/require-region": "eu-west"}
    )
    d = spec.deployment(w)
    assert d["metadata"]["annotations"] == {"federation.spbu/require-region": "eu-west"}
    # Аннотации — на ресурсе, не на шаблоне пода (ADR 0009).
    assert "annotations" not in d["spec"]["template"]["metadata"]
    assert (
        "annotations" not in spec.deployment(spec.Workload("w-1", "cluster-a", 30001))["metadata"]
    )


def test_parse_annotation() -> None:
    assert spec.parse_annotation("a/b=c,d") == ("a/b", "c,d")
    assert spec.parse_annotation(" k = v ") == ("k", "v")
    for bad in ("novalue", "=v"):
        try:
            spec.parse_annotation(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_workload_cr_shape() -> None:
    w = spec.Workload(
        "w-3", "", 30003, annotations={"federation.spbu/require-certification": "pci-dss"}
    )
    cr = spec.workload_cr(w)
    assert cr["apiVersion"] == "federation.spbu/v1alpha1" and cr["kind"] == "Workload"
    assert cr["metadata"]["namespace"] == "federation"
    assert cr["metadata"]["labels"][spec.MANAGED_BY_LABEL] == spec.MANAGED_BY
    assert cr["metadata"]["annotations"] == {"federation.spbu/require-certification": "pci-dss"}
    assert cr["spec"] == {"port": 30003, "image": spec.IMAGE, "replicas": 1}
    assert "annotations" not in spec.workload_cr(spec.Workload("w-4", "", 30004))["metadata"]
