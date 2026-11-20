"""Deterministic lexical ranking, rank fusion, and observable retrieval stages."""

from collections import defaultdict
import re
from typing import Literal, Protocol
from unicodedata import normalize
from uuid import UUID

import bm25s
import jieba
from pydantic import BaseModel, Field

RetrievalMode = Literal["vector", "hybrid"]
RerankStatus = Literal["disabled", "not_run", "applied", "timeout", "unavailable", "invalid_scores"]
TOKEN = re.compile(r"[\u3400-\u9fff]+|[a-z0-9]+(?:[._:/-][a-z0-9]+)*")
SEGMENTER = jieba.Tokenizer()


class RetrievalTrace(BaseModel):
    mode: RetrievalMode
    strategy: Literal["none", "vector", "hybrid_rrf", "hybrid_reranked"] = "none"
    lexical_status: Literal["not_run", "applied", "corpus_limit"] = "not_run"
    rerank_status: RerankStatus = "not_run"
    reranker: str | None = None
    rerank_pairs: int = 0
    vector_ids: list[UUID] = Field(default_factory=list)
    lexical_ids: list[UUID] = Field(default_factory=list)
    final_ids: list[UUID] = Field(default_factory=list)
    review_ids: list[UUID] = Field(default_factory=list)


class RerankError(Exception):
    def __init__(self, reason: Literal["timeout", "unavailable", "invalid_scores"]):
        super().__init__(reason)
        self.reason = reason


class Reranker(Protocol):
    name: str

    def score(self, question: str, passages: list[str]) -> list[float]: ...


def tokenize(text: str) -> list[str]:
    tokens = []
    for token in TOKEN.findall(normalize("NFKC", text).lower()):
        if "\u3400" <= token[0] <= "\u9fff":
            tokens.extend(SEGMENTER.cut(token, HMM=False))
        else:
            tokens.append(token)
    return tokens


def bm25_rank(question: str, corpus: list[tuple[UUID, str]], limit: int) -> list[UUID]:
    query = tokenize(question)
    if not query or not corpus:
        return []
    documents = [tokenize(text) for _, text in corpus]
    if not any(documents):
        return []
    index = bm25s.BM25(method="lucene")
    index.index(documents, show_progress=False)
    scores = index.get_scores(query)
    ranked = sorted(((float(score), chunk_id) for (chunk_id, _), score in zip(corpus, scores)
                     if score > 0), key=lambda item: (-item[0], item[1]))
    return [chunk_id for _, chunk_id in ranked[:limit]]


def reciprocal_rank_fusion(*rankings: list[UUID], constant: int = 60) -> list[UUID]:
    scores = defaultdict(float)
    for ranking in rankings:
        for rank, chunk_id in enumerate(dict.fromkeys(ranking), 1):
            scores[chunk_id] += 1 / (constant + rank)
    return sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))
