import json
import urllib.request

from workloadgen.live import LiveSet, serve


def test_live_set_keeps_order_and_clusters():
    live = LiveSet()
    live.add("w-1.fed.local", "cluster-a")
    live.add("w-2.fed.local", "cluster-b")
    live.add("w-3.fed.local")
    live.discard("w-1.fed.local")
    assert live.snapshot() == ["w-2.fed.local", "w-3.fed.local"]
    assert live.clusters() == {"w-2.fed.local": "cluster-b"}


def test_live_endpoint_returns_hosts_and_clusters():
    live = LiveSet()
    live.add("w-1.fed.local", "cluster-a")
    srv = serve(live, "127.0.0.1:0")
    try:
        port = srv.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/live", timeout=2) as r:
            body = json.loads(r.read())
    finally:
        srv.shutdown()
    assert body["hosts"] == ["w-1.fed.local"]
    assert body["clusters"] == {"w-1.fed.local": "cluster-a"}
