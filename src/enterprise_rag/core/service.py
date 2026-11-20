"""Orchestrate ingest and grounded question answering independently of HTTP."""

import logging
from typing import Protocol
from uuid import UUID, uuid4

from .citations import Answer, ValidationMode, answer_from_draft, insufficient, verified_candidates
from .documents import (
    Build, Chunk, IngestResult, ParsedDocument, Scope, SourceView, chunk_units, parse_document,
)
from .embeddings import Embedding, EmbeddingError, IndexProfile, validate_vectors
from .generation import Chat
from .ranking import Reranker, RetrievalMode
from .retrieval import Retriever, SearchStore
from .validation import EvidenceJudge, JudgmentBatch, validate_answer

logger = logging.getLogger(__name__)
EMBEDDING_BATCH_SIZE = 32
REVIEW_LIMIT = 10


class Store(SearchStore, Protocol):
    def reserve(self, scope: Scope, parsed: ParsedDocument, data: bytes,
                profile: IndexProfile, document_id: UUID | None) -> Build: ...
    def record_failure(self, index_id: UUID, code: str) -> None: ...
    def publish(self, build: Build, parsed: ParsedDocument, profile: IndexProfile,
                chunks: list[Chunk], vectors: list[list[float]]) -> bool: ...
    def source(self, scope: Scope, document_id: UUID, version_id: UUID, unit_id: UUID) -> SourceView: ...
    def highlight_source(self, scope: Scope, document_id: UUID, version_id: UUID,
                         unit_id: UUID) -> tuple[int | None, str, bytes | None]: ...


class RAGService:
    def __init__(self, store: Store, embedding: Embedding, chat: Chat, profile: IndexProfile, *,
                 retrieval_mode: RetrievalMode = "vector", reranker: Reranker | None = None,
                 validation_mode: ValidationMode = "off", judge: EvidenceJudge | None = None):
        if validation_mode != "off" and judge is None:
            raise ValueError("Evidence validation requires a judge.")
        self.store = store
        self.embedding = embedding
        self.chat = chat
        self.profile = profile
        self.retriever = Retriever(store, embedding, profile, mode=retrieval_mode, reranker=reranker)
        self.validation_mode = validation_mode
        self.judge = judge

    def ingest(self, scope: Scope, filename: str, data: bytes, document_id: UUID | None = None) -> IngestResult:
        parsed = parse_document(filename, data)
        chunks = chunk_units(parsed.units, self.profile.chunk_chars)
        build = self.store.reserve(scope, parsed, data, self.profile, document_id)
        if build.chunk_count is not None:
            return IngestResult(build.document_id, build.version_id, build.index_id,
                                build.chunk_count, False, parsed.skipped_pages)

        vectors = []
        try:
            for start in range(0, len(chunks), EMBEDDING_BATCH_SIZE):
                batch = chunks[start:start + EMBEDDING_BATCH_SIZE]
                vectors.extend(validate_vectors(self.embedding.embed_documents([item.text for item in batch]),
                                                count=len(batch), dimension=self.profile.dimension))
        except EmbeddingError as error:
            self.store.record_failure(build.index_id, error.code)
            raise

        created = self.store.publish(build, parsed, self.profile, chunks, vectors)
        return IngestResult(build.document_id, build.version_id, build.index_id,
                            len(chunks), created, parsed.skipped_pages)

    def ask(self, scope: Scope, question: str, k: int, request_id: str | None = None) -> Answer:
        answer_id = uuid4()
        review_enabled = self.validation_mode != "off"
        retrieved = self.retriever.retrieve(scope, question, k, review_k=REVIEW_LIMIT if review_enabled else 0)
        candidates = list(verified_candidates(retrieved.candidates, scope.id).values())
        if not candidates:
            result = insufficient(answer_id, "no_candidates")
        else:
            generated = self.chat.generate(question, candidates)
            result = answer_from_draft(generated.draft, candidates, scope.id, answer_id,
                                       keep_unbound=review_enabled)
            if self.validation_mode != "off" and self.judge is not None and result.claims:
                review_candidates = list(verified_candidates(retrieved.review_candidates, scope.id).values())
                review = (self.judge.review(question, result, review_candidates) if result.citations
                          else JudgmentBatch(coverage="uncertain", claims=[]))
                result = validate_answer(result, review_candidates, review.claims, self.validation_mode,
                                         self.judge.version, coverage=review.coverage)
            logger.info("generated request_id=%s answer_id=%s status=%s input_tokens=%s output_tokens=%s",
                        request_id, answer_id, result.answer_status, generated.input_tokens, generated.output_tokens)
        result.retrieval = retrieved.trace
        return result
