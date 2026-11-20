import os
from uuid import uuid4

import pytest

from enterprise_rag.adapters.postgres import PostgresStore
from enterprise_rag.api.main import create_app
from enterprise_rag.config import Settings
from enterprise_rag.core.generation import Draft, DraftClaim, GeneratedDraft
from enterprise_rag.core.service import RAGService


class TestEmbedding:
    def embed_documents(self, texts):
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text):
        if "保护区" in text:
            return [0.0, 1.0, 0.0]
        if "页面置换" in text:
            return [0.0, 0.0, 1.0]
        return [1.0, 0.0, 0.0]


class GroundedChat:
    def generate(self, question, candidates):
        if "火星" in question:
            return GeneratedDraft(Draft(claims=[]))
        return GeneratedDraft(Draft(claims=[DraftClaim(
            text=candidates[0].content, evidence_ids=[candidates[0].evidence_id],
        )]))


@pytest.fixture
def settings():
    dsn = os.environ.get("RAG_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set RAG_TEST_DATABASE_URL to a migrated test database")
    return Settings(
        _env_file=None, database_url=dsn, app_id=f"test-{uuid4().hex}",
        collection_tokens={"course": "01-course-fixture-token-aaaaaaaa",
                           "other": "02-other-fixture-token-bbbbbbbb"},
        chat_provider="ollama", chat_model="test-chat",
        embedding_provider="ollama", embedding_model="test-embedding",
        embedding_revision="a" * 64, embedding_dimension=3,
    )


@pytest.fixture
def service(settings):
    return RAGService(PostgresStore(settings.database_url.get_secret_value()),
                      TestEmbedding(), GroundedChat(), settings.index_profile())


@pytest.fixture
def app(settings, service):
    return create_app(settings, service)
