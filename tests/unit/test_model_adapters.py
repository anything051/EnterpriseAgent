import json
from uuid import uuid4

import httpx
import pytest
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from enterprise_rag.adapters.models import ModelChat, ModelEmbedding, OllamaEndpoint
from enterprise_rag.core.citations import Candidate, DocumentLocator
from enterprise_rag.core.embeddings import EmbeddingError

DIGEST = "a" * 64


def test_ollama_revision_mismatch_refuses_to_generate_vectors(monkeypatch):
    routes = []

    def reply(_endpoint, _method, route, _payload=None):
        routes.append(route)
        return {"models": [{"name": "fixture:latest", "digest": "b" * 64}]}

    monkeypatch.setattr(OllamaEndpoint, "request", reply, raising=False)
    provider = ModelEmbedding(OllamaEndpoint("http://test.invalid", "fixture", 1), "", "",
                              expected_ollama_digest=DIGEST)
    with pytest.raises(EmbeddingError) as caught:
        provider.embed_query("question")
    assert caught.value.code == "embedding_revision_mismatch"
    assert routes == ["/api/tags"]


def test_ollama_matching_digest_checks_and_forwards_both_prefixes(monkeypatch):
    inputs = []

    def reply(_endpoint, method, route, payload=None):
        if method == "GET":
            assert route == "/api/tags"
            return {"models": [{"name": "fixture:latest", "digest": DIGEST}]}
        assert route == "/api/embed"
        assert payload["truncate"] is False
        inputs.extend(payload["input"])
        return {"embeddings": [[1, 0]]}

    monkeypatch.setattr(OllamaEndpoint, "request", reply, raising=False)
    provider = ModelEmbedding(OllamaEndpoint("http://test.invalid", "fixture", 1), "doc: ", "search_query: ",
                              expected_ollama_digest=DIGEST)
    assert provider.embed_query("question") == [1, 0]
    assert provider.embed_documents(["excerpt"]) == [[1, 0]]
    assert inputs == ["search_query: question", "doc: excerpt"]


def test_openai_adapter_with_provider_protocol_and_no_network():
    def send(request):
        body = json.loads(request.content)
        if request.url.path == "/v1/embeddings":
            assert body["input"] == ["q: meaning"]
            return httpx.Response(200, json={"object": "list", "data": [
                {"object": "embedding", "index": 0, "embedding": [1.0, 0.0]}],
                "model": "test-embed", "usage": {"prompt_tokens": 2, "total_tokens": 2}})
        assert request.url.path == "/v1/chat/completions"
        assert body["response_format"]["type"] == "json_schema"
        draft_json = json.dumps({"claims": [{"text": "value", "evidence_ids": ["E1", "invented"]}]})
        return httpx.Response(200, json={
            "id": "chatcmpl-fixture", "object": "chat.completion", "created": 1, "model": "test-chat",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": draft_json}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
        })

    with httpx.Client(transport=httpx.MockTransport(send)) as client:
        embedder = ModelEmbedding(OpenAIEmbeddings(
            model="test-embed", api_key="local-stub-token", base_url="https://test.invalid/v1",
            http_client=client, check_embedding_ctx_length=False,
        ), "doc: ", "q: ")
        generator = ModelChat(ChatOpenAI(
            model="test-chat", api_key="local-stub-token", base_url="https://test.invalid/v1", http_client=client,
        ))
        ev = Candidate(evidence_id="real-id", scope_id="app:scope", source_id=uuid4(),
                       locator=DocumentLocator(version_id=uuid4(), parser_profile_id="pypdf:fixture",
                                               unit_id=uuid4(), paragraph_id="p1", start=0, end=5, chunk_id=uuid4()),
                       content="value", source_text="value")
        assert embedder.embed_query("meaning") == [1.0, 0.0]
        result = generator.generate("meaning?", [ev])
        assert result.draft.claims[0].evidence_ids == ["real-id"]
        assert (result.input_tokens, result.output_tokens) == (11, 3)
