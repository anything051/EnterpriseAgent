FROM python:3.11.15-slim-bookworm@sha256:d29f48a31a8b408ed19272ca1e7b10ebae13b240a27e862d3d4217c528e2e0c3 AS build
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /app
RUN pip install --no-cache-dir uv==0.11.10
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable

FROM build AS release-build
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable --extra course --extra agent --extra rerank

FROM python:3.11.15-slim-bookworm@sha256:d29f48a31a8b408ed19272ca1e7b10ebae13b240a27e862d3d4217c528e2e0c3 AS runtime-base
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
WORKDIR /app
USER 10001:10001
EXPOSE 8000

FROM runtime-base AS release
COPY --from=release-build /opt/venv /opt/venv
COPY data/samples/enterprise_inventory.md ./data/samples/enterprise_inventory.md
COPY data/enterprise_agent/demo/inventory.json ./data/enterprise_agent/demo/inventory.json
COPY docs/evaluation_runs/course-dev-20261120 ./docs/evaluation_runs/course-dev-20261120
COPY .streamlit/config.toml ./.streamlit/config.toml
COPY tools/release/uvicorn.json ./tools/release/uvicorn.json

FROM runtime-base AS runtime
COPY --from=build /opt/venv /opt/venv
CMD ["uvicorn", "enterprise_rag.api.main:create_app", "--factory", "--host", "0.0.0.0", "--no-access-log", "--no-proxy-headers"]
