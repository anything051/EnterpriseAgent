from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from enterprise_rag.core.documents import Scope
from enterprise_rag.core.embeddings import EmbeddingError, IndexProfileConflict
from enterprise_rag.core.service import RAGService

SAMPLES = Path(__file__).resolve().parents[2] / "data" / "samples"
MD = "# 页表\n\n页表保存虚拟页到物理页框的映射。".encode()
HEADERS = {"Authorization": "Bearer 01-course-fixture-token-aaaaaaaa"}
OTHER = {"Authorization": "Bearer 02-other-fixture-token-bbbbbbbb"}


def upload(client, data=MD, filename="lesson.md", headers=HEADERS, **fields):
    return client.post("/documents", headers=headers, data={"collection_id": "course", **fields},
                       files={"file": (filename, data, "application/octet-stream")})


def ask(client, question, headers=HEADERS, collection_id="course", k=5):
    return client.post("/questions", headers=headers,
                       json={"question": question, "collection_id": collection_id, "k": k})


def test_ingest_retrieve_resolve_duplicate_and_revision(app):
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        first = upload(client)
        assert first.status_code == 201, first.text
        first_body = first.json()
        repeat = upload(client)
        assert repeat.status_code == 200
        assert repeat.json() == first_body
        assert first_body["chunk_count"] >= 1

        answered = ask(client, "页表保存什么？")
        assert answered.status_code == 200, answered.text
        result = answered.json()
        assert result["answer_status"] == "answered"
        ev = result["evidence"][0]
        loc = ev["locator"]
        assert ev["source_id"] == first_body["document_id"]
        assert result["citations"][0]["evidence_id"] == ev["evidence_id"]
        assert loc["heading_path"] == ["页表"]
        url = f"/documents/{ev['source_id']}/versions/{loc['version_id']}/units/{loc['unit_id']}"
        source = client.get(url, headers=HEADERS)
        assert source.status_code == 200
        assert source.json()["source_text"][loc["start"]:loc["end"]] == ev["content"]
        assert client.get(url, headers=OTHER).status_code == 404
        assert client.get(url).status_code == 401
        assert ask(client, "页表", OTHER).status_code == 404

        changed = upload(client, "更新：页表只描述页和页框的映射。".encode(),
                         document_id=first_body["document_id"])
        assert changed.status_code == 201
        assert changed.json()["version_id"] != first_body["version_id"]
        assert ask(client, "页表").json()["evidence"][0]["locator"]["version_id"] == changed.json()["version_id"]
        assert client.get(url, headers=HEADERS).json() == source.json()
        assert upload(client).status_code == 200
        assert ask(client, "页表").json()["evidence"][0]["locator"]["version_id"] == changed.json()["version_id"]


def test_pdf_evidence_has_physical_page_and_excerpt(app):
    with TestClient(app) as client:
        pdf = upload(client, (SAMPLES / "course_memory.pdf").read_bytes(), "course_memory.pdf")
        assert pdf.status_code == 201, pdf.text
        result = ask(client, "页面置换", k=1).json()
        assert result["answer_status"] == "answered"
        evidence = result["evidence"][0]
        assert evidence["locator"]["page"] == 2
        assert "页面置换" in evidence["content"]


def test_empty_and_model_refused_questions_are_explicit(app):
    with TestClient(app) as client:
        empty = ask(client, "谁在火星教课？")
        assert empty.status_code == 200
        assert empty.json()["answer_status"] == "insufficient_evidence"
        assert upload(client).status_code == 201
        refused = ask(client, "谁在火星教课？")
        assert refused.status_code == 200
        assert refused.json()["answer_status"] == "insufficient_evidence"
        assert refused.json()["evidence"] == []


def test_invalid_requests_and_cross_scope_ingestion(app):
    with TestClient(app) as client:
        document = upload(client)
        assert document.status_code == 201
        assert upload(client, b"xyz", "doc.csv").status_code == 415
        assert upload(client, b"%PDF-invalid", "doc.pdf").status_code == 422
        assert upload(client, b"").status_code == 422
        assert upload(client, b"x" * (5 * 1024 * 1024 + 1)).status_code == 413
        assert upload(client, headers=OTHER).status_code == 404
        assert upload(client, headers=OTHER, collection_id="other",
                      document_id=document.json()["document_id"]).status_code == 404
        assert ask(client, "   ").status_code == 422
        assert ask(client, "valid", k=11).status_code == 422
        assert ask(client, "页表", OTHER, "other").json()["answer_status"] == "insufficient_evidence"


def test_retrieval_filters_before_ranking_and_breaks_ties_stably(service, settings):
    course = Scope(settings.app_id, "course")
    other = Scope(settings.app_id, "other")
    service.ingest(course, "a.md", b"A evidence.")
    service.ingest(course, "b.md", b"B evidence.")
    service.ingest(other, "secret.md", "保护区 evidence".encode())
    answer = service.ask(course, "保护区", 10)
    assert answer.answer_status == "answered"
    assert "保护区" not in answer.evidence[0].content
    candidates = service.retriever.search(course, "same", 10)
    chunk_ids = [str(item.locator.chunk_id) for item in candidates]
    assert len(chunk_ids) == 2
    assert chunk_ids == sorted(chunk_ids)


def test_parallel_same_upload_publishes_one_build(service, settings):
    course = Scope(settings.app_id, "course")
    with ThreadPoolExecutor(max_workers=4) as threads:
        results = list(threads.map(lambda _: service.ingest(course, "race.md", MD), range(4)))
    assert len({item.index_id for item in results}) == 1
    assert sum(item.created for item in results) == 1
    assert len(service.retriever.search(course, "页表", 10)) == results[0].chunk_count


def test_failed_new_version_preserves_old_index_and_can_retry(service, settings, monkeypatch):
    course = Scope(settings.app_id, "course")
    original = service.ingest(course, "retry.md", MD)
    new_body = "新版本内容。".encode()
    embed_documents = service.embedding.embed_documents

    def fail(_):
        raise EmbeddingError("Could not obtain document embeddings.")

    monkeypatch.setattr(service.embedding, "embed_documents", fail)
    with pytest.raises(EmbeddingError):
        service.ingest(course, "retry.md", new_body)
    assert service.ask(course, "页表", 1).evidence[0].locator.version_id == original.version_id
    monkeypatch.setattr(service.embedding, "embed_documents", embed_documents)
    newer = service.ingest(course, "retry.md", new_body)
    assert newer.version_id != original.version_id
    assert service.ask(course, "页表", 1).evidence[0].locator.version_id == newer.version_id


def test_older_concurrent_build_does_not_replace_newer_build(service, settings, monkeypatch):
    course = Scope(settings.app_id, "course")
    older_waiting = Event()
    release_older = Event()
    embed_documents = service.embedding.embed_documents

    def ordered(texts):
        if "旧内容" in texts[0]:
            older_waiting.set()
            assert release_older.wait(timeout=10)
        return embed_documents(texts)

    monkeypatch.setattr(service.embedding, "embed_documents", ordered)
    with ThreadPoolExecutor(max_workers=2) as threads:
        first = threads.submit(service.ingest, course, "ordered.md", "旧内容".encode())
        try:
            assert older_waiting.wait(timeout=10)
            published = service.ingest(course, "ordered.md", "新内容".encode())
        finally:
            release_older.set()
        assert first.result().version_id != published.version_id
    assert service.ask(course, "内容", 1).evidence[0].locator.version_id == published.version_id


def test_model_failure_is_distinct_from_insufficient_evidence(app, service, monkeypatch):
    def fail(_):
        raise EmbeddingError("private provider detail")

    with TestClient(app) as client:
        assert upload(client).status_code == 201
        monkeypatch.setattr(service.embedding, "embed_query", fail)
        response = ask(client, "页表")
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "embedding_unavailable"
        assert "private provider detail" not in response.text
        assert response.headers["x-request-id"] == response.json()["request_id"]


def test_transport_body_errors_include_request_id_and_error_code(app):
    with TestClient(app) as client:
        malformed = client.post("/documents", headers={**HEADERS, "Content-Type": "multipart/form-data"},
                                content=b"missing multipart boundary")
        oversized = client.post("/questions", headers={**HEADERS, "Content-Type": "application/json"},
                                content=b" " * (16 * 1024 + 1))
    for response, status, code in ((malformed, 400, "invalid_request"), (oversized, 413, "request_too_large")):
        assert response.status_code == status
        assert response.json()["error"]["code"] == code
        assert response.headers["x-request-id"] == response.json()["request_id"]


def test_same_collection_name_is_isolated_between_apps(service, settings):
    original = Scope(settings.app_id, "course")
    another_app = Scope(f"{settings.app_id}-other", "course")
    service.ingest(original, "lesson.md", MD)
    assert service.ask(another_app, "页表", 1).answer_status == "insufficient_evidence"


def test_rejects_an_existing_collection_with_different_index_profile(service, settings):
    course = Scope(settings.app_id, "course")
    service.ingest(course, "profile.md", MD)
    alternative = RAGService(service.store, service.embedding, service.chat,
                             replace(service.profile, embedding_revision="b" * 64))
    with pytest.raises(IndexProfileConflict):
        alternative.ask(course, "页表", 1)
    with pytest.raises(IndexProfileConflict):
        alternative.ingest(course, "another.md", b"new text")
