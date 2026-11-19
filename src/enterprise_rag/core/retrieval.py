"""Exact retrieval within a single authorized collection and index profile."""

from typing import Protocol

from .citations import Candidate
from .documents import Scope
from .embeddings import Embedding, IndexProfile, validate_vectors


class SearchStore(Protocol):
    def has_ready_documents(self, scope: Scope, profile_id: str) -> bool: ...
    def top_chunks(self, scope: Scope, profile_id: str, vector: list[float], k: int) -> list[Candidate]: ...


class Retriever:
    def __init__(self, store: SearchStore, embedding: Embedding, profile: IndexProfile):
        self.store = store
        self.embedding = embedding
        self.profile = profile

    def search(self, scope: Scope, question: str, k: int) -> list[Candidate]:
        if not self.store.has_ready_documents(scope, self.profile.id):
            return []
        vector, = validate_vectors([self.embedding.embed_query(question)], count=1,
                                   dimension=self.profile.dimension)
        return self.store.top_chunks(scope, self.profile.id, vector, k)
