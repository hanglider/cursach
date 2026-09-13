# Образ каталога; собирается локально и загружается в control через kind load (ADR 0025).
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /bin/uv
WORKDIR /srv
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY migrations ./migrations

FROM python:3.13-slim
WORKDIR /srv
COPY --from=build /srv/.venv ./.venv
COPY --from=build /srv/app ./app
COPY --from=build /srv/migrations ./migrations
ENV PATH=/srv/.venv/bin:$PATH PYTHONPATH=/srv PYTHONUNBUFFERED=1
USER 65532:65532
EXPOSE 8000
CMD ["uvicorn", "app.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
