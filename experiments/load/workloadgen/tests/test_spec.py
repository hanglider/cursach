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
