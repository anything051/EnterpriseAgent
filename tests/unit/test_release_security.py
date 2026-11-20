"""Release ingress must avoid returning or logging sensitive request details."""

import logging

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from enterprise_rag.api.main import create_app
from enterprise_rag.config import Settings


def api_settings(hosts):
    return Settings(
        _env_file=None, database_url="postgresql://127.0.0.1/demo",
        collection_tokens={"demo": "fixture-only-authorization-token"},
        chat_provider="ollama", chat_model="fixture",
        embedding_provider="ollama", embedding_model="fixture", embedding_revision="a" * 64,
        embedding_dimension=3, trusted_hosts=hosts,
    )


def test_released_api_protects_private_responses_and_logs(caplog):
    sentinel = "fictional-release-redaction-marker"
    cfg = api_settings(["127.0.0.1"])

    def register(app, _service):
        @app.get("/diagnostic/{identifier}")
        def diagnostic(identifier: str):
            raise RuntimeError(identifier)

    with TestClient(create_app(cfg, object(), register=register), base_url="http://127.0.0.1") as client:
        with caplog.at_level(logging.INFO, logger="enterprise_rag.api.main"):
            response = client.get(f"/diagnostic/{sentinel}", params={"value": sentinel},
                                  headers={"Authorization": f"Bearer {sentinel}"})
            rejected = client.get("/healthz", headers={"Host": f"{sentinel}.invalid"})
            unusual_method = client.request(sentinel, "/healthz")
            unauthenticated = client.get("/documents/invalid/versions/invalid/units/invalid")
    assert response.status_code == 500
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert rejected.status_code == 400
    assert unusual_method.status_code == 405
    assert unauthenticated.status_code == 401
    for reply in (response, rejected, unauthenticated):
        assert reply.headers["cache-control"] == "private, no-store"
        assert reply.headers["x-content-type-options"] == "nosniff"
    assert sentinel not in response.text and sentinel not in caplog.text.lower()
    assert "method=OTHER" in caplog.text
    assert "status=500" in caplog.text and "RuntimeError" in caplog.text


def test_invalid_trusted_host_setting_is_rejected():
    with pytest.raises(ValidationError):
        api_settings(["http://127.0.0.1"])


def test_container_ollama_default_never_overrides_openai_endpoint():
    raw = api_settings([]).model_dump()
    raw.update(chat_provider="openai", chat_api_key="fixture-only", chat_base_url="",
               ollama_base_url="http://host.docker.internal:11434")
    settings = Settings.model_validate(raw)
    assert settings.model_url("chat") == "https://api.openai.com/v1"
    assert settings.model_url("embedding") == "http://host.docker.internal:11434"
