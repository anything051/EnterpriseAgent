"""Environment-only configuration for the service and its model providers."""

from pathlib import Path
import re
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from enterprise_rag.core.documents import PARSER_PROFILE_ID
from enterprise_rag.core.embeddings import IndexProfile

SLUG_RE = r"^[a-z0-9][a-z0-9_-]{0,62}$"
TOKEN_RE = r"[A-Za-z0-9._~+-]{24,256}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RAG_", env_file=".env", extra="ignore",
                                      hide_input_in_errors=True)

    database_url: SecretStr
    app_id: str = Field(default="enterprise-rag-demo", pattern=SLUG_RE)
    collection_tokens: dict[str, SecretStr]
    trusted_hosts: list[str] = Field(default_factory=list)
    max_upload_bytes: int = Field(default=5 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    chunk_chars: int = Field(default=450, ge=64, le=900)
    retrieval_mode: Literal["vector", "hybrid"] = "hybrid"
    validation_mode: Literal["off", "annotate", "gate"] = "gate"
    reranker_dir: Path | None = None
    reranker_timeout: float = Field(default=8, gt=0, le=60)
    ollama_base_url: AnyHttpUrl = AnyHttpUrl("http://127.0.0.1:11434")

    chat_provider: Literal["openai", "ollama"] = "openai"
    chat_model: str = Field(min_length=1, max_length=200)
    chat_base_url: AnyHttpUrl | None = None
    chat_api_key: SecretStr | None = None
    chat_timeout: float = Field(default=60, gt=0, le=300)

    embedding_provider: Literal["openai", "ollama"] = "openai"
    embedding_model: str = Field(min_length=1, max_length=200)
    embedding_revision: str = Field(min_length=1, max_length=200)
    embedding_base_url: AnyHttpUrl | None = None
    embedding_api_key: SecretStr | None = None
    embedding_dimension: int = Field(ge=1, le=16_000)
    embedding_timeout: float = Field(default=30, gt=0, le=300)
    embedding_document_prefix: str = Field(default="", max_length=100)
    embedding_query_prefix: str = Field(default="", max_length=100)

    @field_validator("collection_tokens")
    @classmethod
    def check_tokens(cls, tokens: dict[str, SecretStr]) -> dict[str, SecretStr]:
        values = [value.get_secret_value() for value in tokens.values()]
        if (not values or len(values) != len(set(values))
                or not all(re.fullmatch(SLUG_RE, key) for key in tokens)
                or not all(re.fullmatch(TOKEN_RE, value) and not value.startswith("REPLACE_WITH_")
                           for value in values)):
            raise ValueError("Set one distinct URL-safe bearer token (at least 24 characters) per collection.")
        return tokens

    @field_validator("chat_base_url", "embedding_base_url", mode="before")
    @classmethod
    def empty_model_url(cls, url):
        return None if url == "" else url

    @field_validator("chat_base_url", "embedding_base_url", "ollama_base_url")
    @classmethod
    def check_base_url(cls, url: AnyHttpUrl | None) -> AnyHttpUrl | None:
        if url and (url.username is not None or url.password is not None or url.query is not None
                    or url.fragment is not None):
            raise ValueError("Model base URLs must not contain credentials, queries, or fragments.")
        return url

    @field_validator("trusted_hosts")
    @classmethod
    def check_hosts(cls, hosts: list[str]) -> list[str]:
        if any(re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", host) is None for host in hosts):
            raise ValueError("Use literal DNS names or IPv4 addresses without ports in trusted hosts.")
        return hosts

    @model_validator(mode="after")
    def check_provider_credentials(self):
        for kind in ("chat", "embedding"):
            provider = getattr(self, f"{kind}_provider")
            key = getattr(self, f"{kind}_api_key")
            if provider == "openai" and not (key and key.get_secret_value().strip()):
                raise ValueError(f"Set RAG_{kind.upper()}_API_KEY for the OpenAI provider.")
        if self.embedding_provider == "ollama":
            self.embedding_revision = self.embedding_revision.removeprefix("sha256:")
            if not re.fullmatch(r"[a-f0-9]{64}", self.embedding_revision):
                raise ValueError("Set RAG_EMBEDDING_REVISION to the Ollama model digest from /api/tags.")
        return self

    def model_url(self, kind: Literal["chat", "embedding"]) -> str:
        explicit = self.chat_base_url if kind == "chat" else self.embedding_base_url
        provider = self.chat_provider if kind == "chat" else self.embedding_provider
        if explicit:
            return str(explicit).rstrip("/")
        return str(self.ollama_base_url).rstrip("/") if provider == "ollama" else "https://api.openai.com/v1"

    def index_profile(self) -> IndexProfile:
        return IndexProfile(
            PARSER_PROFILE_ID, self.chunk_chars, self.embedding_provider, self.embedding_model,
            self.embedding_revision, self.model_url("embedding"), self.embedding_dimension,
            self.embedding_document_prefix, self.embedding_query_prefix,
        )
