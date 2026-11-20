import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from enterprise_rag.adapters.models import create_models
from enterprise_rag.adapters.postgres import PostgresStore
from enterprise_rag.api.main import create_app
from enterprise_rag.config import Settings
from enterprise_rag.core.citations import answer_from_draft
from enterprise_rag.core.documents import Scope
from enterprise_rag.core.generation import Draft, DraftClaim
from enterprise_rag.core.service import RAGService
from enterprise_rag.core.validation import validate_answer

SAMPLES = Path(__file__).resolve().parents[2] / "data/samples"
TOKEN = "local-trusted-rag-smoke-test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def live_settings(**kwargs):
    if os.environ.get("RAG_RUN_LIVE") != "1":
        pytest.skip("Set RAG_RUN_LIVE=1 to exercise real models")
    return Settings(app_id=f"trusted-{uuid4().hex}", collection_tokens={"demo": TOKEN}, **kwargs)


@pytest.mark.live
def test_real_hybrid_rerank_validation_and_pdf_highlight():
    settings = live_settings(retrieval_mode="hybrid", validation_mode="gate")
    if settings.reranker_dir is None:
        pytest.skip("Set RAG_RERANKER_DIR to the verified BGE artifacts")
    with TestClient(create_app(settings)) as client:
        for name in ("course_memory.pdf", "enterprise_inventory.md"):
            response = client.post("/documents", headers=HEADERS, data={"collection_id": "demo"},
                                   files={"file": (name, (SAMPLES / name).read_bytes())})
            assert response.status_code == 201, response.text
        result = client.post("/questions", headers=HEADERS,
                             json={"collection_id": "demo", "question": "根据课程笔记，LRU 选择哪个页面？"})
        assert result.status_code == 200, result.text
        answer = result.json()
        assert answer["retrieval"]["strategy"] == "hybrid_reranked", answer
        assert answer["answer_status"] == "answered", answer
        assert "最长时间" in answer["answer"]
        assert all(claim["status"] == "supported" for claim in answer["verification"]["claims"])
        located = next(item for item in answer["evidence"] if item["locator"]["page"] == 2)
        loc = located["locator"]
        matched = next(cite for cite in answer["citations"] if cite["evidence_id"] == located["evidence_id"])
        span = matched["quote_span"] or loc
        highlight = client.get(
            f"/documents/{located['source_id']}/versions/{loc['version_id']}/units/{loc['unit_id']}/highlight",
            headers=HEADERS, params={"start": span["start"], "end": span["end"]},
        )
        assert highlight.json()["status"] == "highlight", highlight.text


@pytest.mark.live
def test_real_judge_identifies_same_scope_contradiction():
    settings = live_settings()
    embedding, chat = create_models(settings)
    store = PostgresStore(settings.database_url.get_secret_value())
    service = RAGService(store, embedding, chat, settings.index_profile(), retrieval_mode="hybrid")
    scope = Scope(settings.app_id, "demo")
    service.ingest(scope, "old.md", "青禾公司当前库房规定：盘点差异须在24小时内登记。".encode())
    service.ingest(scope, "conflict.md", "青禾公司当前库房规定：盘点差异必须在48小时内登记，不是24小时。".encode())
    candidates = service.retriever.search(scope, "青禾公司盘点差异多久内登记？", 5)
    citation = next(item for item in candidates if "48小时" not in item.content)
    draft = Draft(claims=[DraftClaim(text="青禾公司当前规定盘点差异须在24小时内登记。",
                                   evidence_ids=[citation.evidence_id])])
    answer = answer_from_draft(draft, candidates, scope.id, uuid4())
    review = chat.review("青禾公司盘点差异多久内登记？", answer, candidates)
    checked = validate_answer(answer, candidates, review.claims, "gate", chat.version, coverage=review.coverage)
    assert checked.refusal_reason == "contradictory_evidence", checked
    assert any("48小时" in item.content for item in checked.verification.conflict_evidence)


@pytest.mark.live
def test_real_judge_combines_evidence_and_detects_an_unanswered_subquestion():
    settings = live_settings()
    embedding, chat = create_models(settings)
    store = PostgresStore(settings.database_url.get_secret_value())
    service = RAGService(store, embedding, chat, settings.index_profile(), retrieval_mode="hybrid")
    scope = Scope(settings.app_id, "demo")
    service.ingest(scope, "warehouse.md", "青禾仓的盘点差异登记执行Q规则。".encode())
    service.ingest(scope, "rule.md", "Q规则要求在24小时内登记盘点差异。".encode())
    candidates = service.retriever.search(scope, "青禾仓盘点差异登记期限是多少？", 5)
    draft = Draft(claims=[DraftClaim(text="青禾仓盘点差异登记期限是24小时。",
                                   evidence_ids=[item.evidence_id for item in candidates])])
    answer = answer_from_draft(draft, candidates, scope.id, uuid4())

    complete = chat.review("青禾仓盘点差异登记期限是多少？", answer, candidates)
    assert validate_answer(answer, candidates, complete.claims, "gate", chat.version,
                           coverage=complete.coverage).answer_status == "answered"
    partial = chat.review("青禾仓盘点差异登记期限是多少，负责登记的人员是谁？", answer, candidates)
    checked = validate_answer(answer, candidates, partial.claims, "gate", chat.version, coverage=partial.coverage)
    assert checked.refusal_reason == "incomplete_answer", checked
    assert checked.verification.claims[0].status == "supported"
