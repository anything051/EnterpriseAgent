"""Provider-independent embedding contract and index identity."""

from array import array
from dataclasses import dataclass
from hashlib import sha256
import json
from math import hypot, isfinite
from typing import Protocol


class EmbeddingError(Exception):
    def __init__(self, message: str, *, code: str = "embedding_unavailable"):
        super().__init__(message)
        self.code = code


class IndexProfileConflict(Exception):
    pass


class Embedding(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


@dataclass(frozen=True)
class IndexProfile:
    parser_profile_id: str
    chunk_chars: int
    embedding_provider: str
    embedding_model: str
    embedding_revision: str
    embedding_endpoint: str
    dimension: int
    document_prefix: str = ""
    query_prefix: str = ""

    @property
    def id(self) -> str:
        settings = [
            self.parser_profile_id, self.chunk_chars, "paragraph-fixed-codepoints-v1",
            self.embedding_provider, self.embedding_model, self.embedding_revision,
            self.embedding_endpoint, self.dimension, self.document_prefix, self.query_prefix,
            "cosine-normalized-float32-v1",
        ]
        return sha256(json.dumps(settings, ensure_ascii=False).encode()).hexdigest()


def validate_vectors(vectors: list[list[float]], count: int, dimension: int) -> list[list[float]]:
    if not isinstance(vectors, list) or len(vectors) != count:
        raise EmbeddingError("Embedding response has an unexpected vector count.", code="invalid_embedding")

    normalized = []
    for vector in vectors:
        if (not isinstance(vector, list) or len(vector) != dimension
                or any(type(value) not in (int, float) for value in vector)):
            raise EmbeddingError("Embedding response has an invalid vector shape.", code="invalid_embedding")
        try:
            values = array("f", vector)
        except (OverflowError, ValueError, TypeError) as error:
            raise EmbeddingError("Embedding vectors must be representable as float32.", code="invalid_embedding") from error
        if not all(isfinite(value) for value in values):
            raise EmbeddingError("Embedding vectors must contain only finite values.", code="invalid_embedding")
        norm = hypot(*values)
        if not norm:
            raise EmbeddingError("Cosine search requires nonzero embedding vectors.", code="invalid_embedding")
        normalized.append([value / norm for value in values])
    return normalized
