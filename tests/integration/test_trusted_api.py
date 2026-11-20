from pathlib import Path

from fastapi.testclient import TestClient

from enterprise_rag.api.main import create_app
from enterprise_rag.core.documents import Scope
from enterprise_rag.core.ranking import RerankError
from enterprise_rag.core.service import RAGService

SAMPLES = Path(__file__).resolve().parents[2] / "data/samples"
COURSE_HEADERS = {"Authorization": "Bearer 01-course-fixture-token-aaaaaaaa"}
OTHER_HEADERS = {"Authorization": "Bearer 02-other-fixture-token-bbbbbbbb"}


class TimedOutReranker:
    name = "timeout-fixture"

    def score(self, question, passages):
        raise RerankError("timeout")


def test_hybrid_scope_and_visible_rerank_fallback(settings, service):
    trusted = RAGService(service.store, service.embedding, service.chat, service.profile,
                         retrieval_mode="hybrid", reranker=TimedOutReranker())
    course = Scope(settings.app_id, "course")
    hidden = Scope(settings.app_id, "other")
    for i in range(22):
        trusted.ingest(course, f"distractor-{i}.md", "其它流程。".encode())
    trusted.ingest(course, "exact.md", "保护区访问码 AB-170 的复核要求。".encode())
    trusted.ingest(hidden, "private.md", "AB-170 密钥。".encode())

    result = trusted.ask(course, "AB-170", 5)
    trace = result.retrieval
    assert trace.mode == "hybrid"
    assert trace.rerank_status == "timeout"
    assert trace.strategy == "hybrid_rrf"
    assert trace.lexical_ids[0] in trace.final_ids
    assert "密钥" not in result.answer
    assert trace.lexical_ids[0] not in trace.vector_ids


def test_pdf_highlight_endpoint_respects_scope_and_span(settings, app):
    with TestClient(app) as client:
        response = client.post("/documents", headers=COURSE_HEADERS, data={"collection_id": "course"},
                               files={"file": ("course_memory.pdf", (SAMPLES / "course_memory.pdf").read_bytes())})
        assert response.status_code == 201, response.text
        response = client.post("/questions", headers=COURSE_HEADERS,
                               json={"collection_id": "course", "question": "页面置换", "k": 1})
        evidence = response.json()["evidence"][0]
        locator = evidence["locator"]
        url = f"/documents/{evidence['source_id']}/versions/{locator['version_id']}/units/{locator['unit_id']}/highlight"
        params = {"start": locator["start"], "end": locator["end"]}
        result = client.get(url, headers=COURSE_HEADERS, params=params)
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "highlight"
        assert result.json()["page"] == 2
        assert result.json()["excerpt"] == evidence["content"]
        assert client.get(url, headers=OTHER_HEADERS, params=params).status_code == 404
        assert client.get(url, headers=COURSE_HEADERS, params={"start": 9, "end": 2}).status_code == 422


def test_lexical_and_vector_rows_use_the_same_published_revision(settings, service):
    scope = Scope(settings.app_id, "course")
    old = service.ingest(scope, "policy.md", b"Old release.")
    with service.store.search_snapshot(scope, service.profile.id) as snapshot:
        dense = snapshot.vector_ids([1, 0, 0], 20)
        new = service.ingest(scope, "policy.md", b"New release.")
        assert snapshot.lexical_corpus(20) == [(dense[0], "Old release.")]
        assert snapshot.candidates(dense)[0].locator.version_id == old.version_id
    assert service.retriever.search(scope, "release", 1)[0].locator.version_id == new.version_id


def test_gate_returns_conflict_source_outside_cited_top_k(settings, service):
    from enterprise_rag.core.generation import Draft, DraftClaim, GeneratedDraft
    from enterprise_rag.core.validation import ClaimJudgment, EvidenceJudgment, JudgmentBatch

    class SelectOnePolicy:
        def generate(self, question, candidates):
            return GeneratedDraft(Draft(claims=[DraftClaim(text="The deadline is one day.",
                                                           evidence_ids=[candidates[0].evidence_id])]))

    class FlagConflict:
        version = "test-fixture"

        def review(self, question, answer, candidates):
            cited = answer.citations[0].evidence_id
            distinct = next(item.evidence_id for item in candidates if item.evidence_id != cited)
            return JudgmentBatch(coverage="complete", claims=[
                ClaimJudgment(number=1, status="supported", evidence=[
                    EvidenceJudgment(evidence_id=cited, status="supported", quote=""),
                ], conflict_ids=[distinct]),
            ])

    trusted = RAGService(service.store, service.embedding, SelectOnePolicy(), service.profile,
                         retrieval_mode="hybrid", validation_mode="gate", judge=FlagConflict())
    scope = Scope(settings.app_id, "course")
    trusted.ingest(scope, "one.md", b"The deadline is one day.")
    trusted.ingest(scope, "two.md", b"The deadline is two days.")
    with TestClient(create_app(settings, trusted)) as client:
        response = client.post("/questions", headers=COURSE_HEADERS,
                               json={"collection_id": "course", "question": "What is the deadline?", "k": 1})
    assert response.status_code == 200
    result = response.json()
    assert result["answer_status"] == "insufficient_evidence"
    assert result["refusal_reason"] == "contradictory_evidence"
    assert result["claims"] == []
    conflicts = result["verification"]["conflict_evidence"]
    assert len(conflicts) == 1
    assert conflicts[0]["locator"]["chunk_id"] not in result["retrieval"]["final_ids"]


def test_bounded_lexical_corpus_degrades_to_exact_vector(settings, service, monkeypatch):
    monkeypatch.setattr("enterprise_rag.core.retrieval.LEXICAL_CHUNK_LIMIT", 1)
    scope = Scope(settings.app_id, "course")
    trusted = RAGService(service.store, service.embedding, service.chat, service.profile, retrieval_mode="hybrid")
    empty = trusted.ask(scope, "anything", 1)
    assert empty.retrieval.strategy == "none"

    trusted.ingest(scope, "one.md", b"First text.")
    trusted.ingest(scope, "two.md", b"Second text.")
    result = trusted.ask(scope, "text", 1)
    assert result.retrieval.strategy == "vector"
    assert result.retrieval.lexical_status == "corpus_limit"
    assert not result.retrieval.lexical_ids
