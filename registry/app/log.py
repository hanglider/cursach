"""Структурированный лог: одна JSON-строка на событие, как у workloadgen."""

from __future__ import annotations

import json
import logging
import sys
import time


def event(logger: logging.Logger, name: str, level: int = logging.INFO, **fields: object) -> None:
    logger.log(
        level,
        json.dumps({"ts": time.time(), "event": name, **fields}, ensure_ascii=False, default=str),
    )


def configure(level: str = "INFO") -> None:
    logging.basicConfig(level=level, stream=sys.stderr, format="%(message)s")
    # httpx логирует каждый запрос к Thanos на INFO: раз в 30 с шум без пользы.
    logging.getLogger("httpx").setLevel(logging.WARNING)
