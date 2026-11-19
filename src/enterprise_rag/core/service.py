"""Orchestrate ingest and grounded question answering independently of HTTP."""

import logging
from typing import Protocol
from uuid import UUID, uuid4

from .citations import Answer, answer_from_draft, insufficient
from .documents import (
    Build, Chunk, IngestResult, ParsedDocument, Scope, SourceView, chunk_units, parse_document,
)
from .embeddings import Embedding, EmbeddingError, IndexProfile, validate_vectors
from .generation import Chat
from .retrieval import Retriever, SearchStore

logger = logging.getLogger(__name__)
EMBEDDING_BATCH_SIZE = 32


class Store(SearchStore, Protocol):
    def reserve(self, scope: Scope, parsed: ParsedDocument, data: bytes,
                profile: IndexProfile, document_id: UUID | None) -> Build: ...
    def record_failure(self, index_id: UUID, code: str) -> None: ...
    def publish(self, build: Build, parsed: ParsedDocument, profile: IndexProfile,
                chunks: list[Chunk], vectors: list[list[float]]) -> bool: ...
    def source(self, scope: Scope, document_id: UUID, version_id: UUID, unit_id: UUID) -> SourceView: ...


class RAGService:
    def __init__(self, store: Store, embedding: Embedding, chat: Chat, profile: IndexProfile):
        self.store = store
        self.embedding = embedding
        self.chat = chat
        self.profile = profile
        self.retriever = Retriever(store, embedding, profile)

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
        candidates = self.retriever.search(scope, question, k)
        if not candidates:
            return insufficient(answer_id)

        generated = self.chat.generate(question, candidates)
        result = answer_from_draft(generated.draft, candidates, scope.id, answer_id)
        logger.info("generated request_id=%s answer_id=%s status=%s input_tokens=%s output_tokens=%s",
                    request_id, answer_id, result.answer_status, generated.input_tokens, generated.output_tokens)
        return result
