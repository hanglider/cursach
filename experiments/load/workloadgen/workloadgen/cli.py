"""CLI генератора.

workloadgen [--mode scheduler|direct] place --name smoke --port 31999 [--cluster cluster-a]
                   [--wait] [--annotate K=V ...]
                   scheduler (по умолчанию для run): CR Workload в control, кластер выбирает
                   планировщик (ADR 0027); direct (по умолчанию для place/remove): напрямую в
                   --cluster, в обход планировщика. Код выхода 3 — отклонено admission (Kyverno).
workloadgen [--mode ...] remove --name smoke
workloadgen [--mode ...] run [--interval 30] [--ttl 180] [--duration 0] [--listen 127.0.0.1:9797]
workloadgen cleanup
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path

from . import spec
from .live import LiveSet, serve
from .stand import MODE_DIRECT, MODE_SCHEDULER, MODES, PlacementRejected, Stand, federation_clusters

EXIT_REJECTED = 3


def _root() -> Path:
    return Path(os.environ.get("STAND_ROOT", Path(__file__).resolve().parents[4]))


def _stand(args: argparse.Namespace, default_mode: str) -> Stand:
    root = _root()
    clusters = (
        args.clusters.split(",")
        if args.clusters
        else federation_clusters(root / "stand" / "clusters.yaml")
    )
    return Stand(
        root / "stand" / "kubeconfig", clusters, args.gateway_url, mode=args.mode or default_mode
    )


def cmd_place(args: argparse.Namespace) -> int:
    st = _stand(args, MODE_DIRECT)
    if st.mode == MODE_DIRECT and not args.cluster:
        print("place: --cluster обязателен в режиме direct", file=sys.stderr)
        return 2
    annotations = dict(spec.parse_annotation(a) for a in args.annotate)
    w = spec.Workload(
        spec.validate_name(args.name), args.cluster or "", args.port, args.scheduler, annotations
    )
    try:
        st.place(w, args.image)
        if args.wait and st.wait_ready(w, args.timeout) is None:
            return 1
    except PlacementRejected as e:
        print(f"rejected by {e.cluster}: {e.message}", file=sys.stderr)
        return EXIT_REJECTED
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    _stand(args, MODE_DIRECT).remove(spec.validate_name(args.name))
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    removed = _stand(args, MODE_DIRECT).cleanup()
    print(f"removed {len(removed)}: {' '.join(removed)}", file=sys.stderr)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    st = _stand(args, MODE_SCHEDULER)
    clusters = list(st.federation)
    live = LiveSet()
    server = serve(live, args.listen)
    logging.getLogger("workloadgen").info(
        '{"event":"start","mode":"%s","interval":%s,"ttl":%s,"clusters":"%s"}',
        st.mode,
        args.interval,
        args.ttl,
        ",".join(clusters),
    )

    placed: dict[str, tuple[spec.Workload, float]] = {}
    stop = False

    def _stop(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    n = 0
    started = time.monotonic()
    next_place = started
    try:
        while not stop:
            now = time.monotonic()
            if args.duration and now - started >= args.duration:
                break
            if now >= next_place:
                n += 1
                # В режиме direct генератор размещает сам по кругу (отладка стенда,
                # ADR 0015); в режиме scheduler кластер выбирает планировщик.
                cluster = clusters[(n - 1) % len(clusters)] if st.mode == MODE_DIRECT else ""
                w = spec.Workload(f"w-{n}", cluster, spec.node_port_for(n))
                next_place += args.interval
                try:
                    st.place(w, args.image)
                except PlacementRejected:
                    continue  # событие rejected уже в логе; поток не останавливается
                placed[w.name] = (w, time.monotonic())
                try:
                    ready = st.wait_ready(w, min(args.timeout, args.interval))
                except PlacementRejected:
                    ready = None  # CR остаётся до TTL, событие rejected уже в логе
                if ready is not None:
                    placed[w.name] = (ready, placed[w.name][1])
                    live.add(ready.host)
            for name, (w, t0) in list(placed.items()):
                if time.monotonic() - t0 >= args.ttl:
                    live.discard(w.host)
                    time.sleep(args.drain)
                    st.remove(name, w.cluster)
                    del placed[name]
            time.sleep(0.5)
    finally:
        server.shutdown()
        for w, _ in placed.values():
            live.discard(w.host)
        for name, (w, _) in list(placed.items()):
            st.remove(name, w.cluster)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    p = argparse.ArgumentParser(prog="workloadgen", description=__doc__)
    p.add_argument(
        "--mode",
        choices=MODES,
        default=None,
        help="scheduler — через планировщик (по умолчанию для run); direct — напрямую в --cluster",
    )
    p.add_argument("--clusters", default="", help="список id через запятую; по умолчанию все")
    p.add_argument("--gateway-url", default=os.environ.get("GATEWAY_URL", "http://127.0.0.1:8080"))
    p.add_argument("--image", default=spec.IMAGE)
    p.add_argument("--timeout", type=float, default=90, help="ожидание готовности, с")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("place")
    sp.add_argument("--name", required=True)
    sp.add_argument("--cluster", default="", help="кластер; обязателен в режиме direct")
    sp.add_argument("--port", type=int, required=True)
    sp.add_argument("--scheduler", default="manual")
    sp.add_argument(
        "--annotate",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="аннотация Deployment'а, например federation.spbu/require-certification=pci-dss",
    )
    sp.add_argument("--wait", action="store_true")
    sp.set_defaults(fn=cmd_place)

    sp = sub.add_parser("remove")
    sp.add_argument("--name", required=True)
    sp.set_defaults(fn=cmd_remove)

    sp = sub.add_parser("cleanup")
    sp.set_defaults(fn=cmd_cleanup)

    sp = sub.add_parser("run")
    sp.add_argument("--interval", type=float, default=30, help="N, с (ADR 0015)")
    sp.add_argument("--ttl", type=float, default=180, help="TTL, с (ADR 0015)")
    sp.add_argument("--duration", type=float, default=0, help="0 = до сигнала")
    sp.add_argument(
        "--drain",
        type=float,
        default=6,
        help="пауза между снятием с трафика и удалением, с; больше кэша списка в k6",
    )
    sp.add_argument("--listen", default="127.0.0.1:9797")
    sp.set_defaults(fn=cmd_run)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
