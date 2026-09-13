"""Точка входа uvicorn: `uvicorn app.asgi:app`."""

from . import log
from .config import Settings
from .main import create_app

log.configure()
app = create_app(Settings.from_env())
