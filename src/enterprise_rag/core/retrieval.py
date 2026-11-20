"""Run vector and lexical retrieval against an authorized, version-consistent snapshot."""

from dataclasses import dataclass
from math import isfinite
from typing import ContextManager, Protocol
from uuid import UUID

from .citations import Candidate
from .documents import Scope
from .embeddings import Embedding, IndexProfile, validate_vectors
from .ranking import RetrievalMode, RetrievalTrace, RerankError, Reranker, bm25_rank, reciprocal_rank_fusion

BRANCH_LIMIT = 20
LEXICAL_CHUNK_LIMIT = 5000


class SearchSnapshot(Protocol):
    def vector_ids(self, vector: list[float], k: int) -> list[UUID]: ...
    def lexical_corpus(self, limit: int) -> list[tuple[UUID, str]]: ...
    def candidates(self, ids: list[UUID]) -> list[Candidate]: ...


class SearchStore(Protocol):
    def has_ready_documents(self, scope: Scope, profile_id: str) -> bool: ...
    def top_chunks(self, scope: Scope, profile_id: str, vector: list[float], k: int) -> list[Candidate]: ...
    def search_snapshot(self, scope: Scope, profile_id: str) -> ContextManager[SearchSnapshot]: ...


@dataclass
class SearchResult:
    candidates: list[Candidate]
    review_candidates: list[Candidate]
    trace: RetrievalTrace


class Retriever:
    def __init__(self, store: SearchStore, embedding: Embedding, profile: IndexProfile, *,
                 mode: RetrievalMode = "vector", reranker: Reranker | None = None):
        self.store = store
        self.embedding = embedding
        self.profile = profile
        self.mode = mode
        self.reranker = reranker

    def search(self, scope: Scope, question: str, k: int) -> list[Candidate]:
        return self.retrieve(scope, question, k).candidates

    def retrieve(self, scope: Scope, question: str, k: int, *, review_k: int = 0) -> SearchResult:
        trace = RetrievalTrace(mode=self.mode)
        if not self.store.has_ready_documents(scope, self.profile.id):
            return SearchResult([], [], trace)
        vector, = validate_vectors([self.embedding.embed_query(question)], count=1,
                                   dimension=self.profile.dimension)
        if self.mode == "vector":
            trace.strategy = "vector"
            ranked = self.store.top_chunks(scope, self.profile.id, vector, max(k, review_k))
            trace.vector_ids = [item.locator.chunk_id for item in ranked]
        else:
            ranked = self._hybrid(scope, question, vector, trace)

        final = ranked[:k]
        reviewed = ranked[:review_k]
        trace.final_ids = [item.locator.chunk_id for item in final]
        trace.review_ids = [item.locator.chunk_id for item in reviewed]
        return SearchResult(final, reviewed, trace)

    def _hybrid(self, scope: Scope, question: str, vector: list[float], trace: RetrievalTrace) -> list[Candidate]:
        with self.store.search_snapshot(scope, self.profile.id) as snapshot:
            trace.vector_ids = snapshot.vector_ids(vector, BRANCH_LIMIT)
            corpus = snapshot.lexical_corpus(LEXICAL_CHUNK_LIMIT + 1)
            if len(corpus) > LEXICAL_CHUNK_LIMIT:
                trace.lexical_status = "corpus_limit"
                trace.strategy = "vector"
                return snapshot.candidates(trace.vector_ids)

            trace.lexical_status = "applied"
            trace.lexical_ids = bm25_rank(question, corpus, BRANCH_LIMIT)
            fused = reciprocal_rank_fusion(trace.vector_ids, trace.lexical_ids)[:BRANCH_LIMIT]
            pool = snapshot.candidates(fused)

        trace.strategy = "hybrid_rrf"
        if self.reranker is None:
            trace.rerank_status = "disabled"
            return pool
        trace.reranker = self.reranker.name
        if not pool:
            return pool

        try:
            scores = self.reranker.score(question, [item.content for item in pool])
            if len(scores) != len(pool) or not all(isfinite(score) for score in scores):
                raise RerankError("invalid_scores")
        except RerankError as error:
            trace.rerank_status = error.reason
            return pool

        trace.rerank_status = "applied"
        trace.strategy = "hybrid_reranked"
        order = sorted(range(len(pool)), key=lambda index: (-scores[index], index))
        return [pool[index] for index in order]
