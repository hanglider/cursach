"""Список живых workload'ов для k6: HTTP на localhost (ADR 0015).

k6 не умеет перечитывать файлы на лету, поэтому список отдаётся по HTTP.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class LiveSet:
    """Живые хосты в порядке добавления и кластер каждого: k6 помечает запрос
    кластером назначения, чтобы клиентский ряд раскладывался по кластерам (ADR 0031)."""

    def __init__(self) -> None:
        self._hosts: dict[str, str] = {}
        self._lock = threading.Lock()
        self.updated = time.time()

    def add(self, host: str, cluster: str = "") -> None:
        with self._lock:
            self._hosts[host] = cluster
            self.updated = time.time()

    def discard(self, host: str) -> None:
        with self._lock:
            self._hosts.pop(host, None)
            self.updated = time.time()

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self._hosts)

    def clusters(self) -> dict[str, str]:
        with self._lock:
            return {h: c for h, c in self._hosts.items() if c}


def serve(live: LiveSet, listen: str) -> ThreadingHTTPServer:
    host, port = listen.rsplit(":", 1)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — имя задаёт http.server
            if self.path != "/live":
                self.send_error(404)
                return
            body = json.dumps(
                {"hosts": live.snapshot(), "clusters": live.clusters(), "updated": live.updated}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:  # тишина: k6 опрашивает часто
            pass

    srv = ThreadingHTTPServer((host, int(port)), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
