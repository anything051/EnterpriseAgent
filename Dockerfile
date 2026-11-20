FROM python:3.11.15-slim-bookworm AS build
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never
WORKDIR /app
RUN pip install --no-cache-dir uv==0.11.10
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.11.15-slim-bookworm
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "enterprise_rag.api.main:create_app", "--factory", "--host", "0.0.0.0"]
