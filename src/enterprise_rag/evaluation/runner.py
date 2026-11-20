"""Run the paired A–D experiment on a dedicated scope and share B/C/D work."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from random import Random
from time import perf_counter
from uuid import UUID, uuid4, uuid5

import psycopg

from enterprise_rag.core.citations import Answer, Candidate, answer_from_draft, insufficient, verified_candidates
from enterprise_rag.core.documents import Scope
from enterprise_rag.core.embeddings import EmbeddingError
from enterprise_rag.core.generation import Draft, GenerationError
from enterprise_rag.core.retrieval import Retriever, SearchResult
from enterprise_rag.core.service import RAGService
from enterprise_rag.core.validation import JudgmentBatch, validate_answer

from .schema import Counts, Dataset, Group, Passage, Question, Record, Span

FINAL_K = 5
RANK_K = 10


@dataclass(frozen=True)
class Source:
    document: str
    document_id: UUID
    version_id: UUID
    unit_key: str
    text: str


def _prepare(dataset: Dataset, store, embedding, chat, profile, scope: Scope) -> dict[UUID, Source]:
    service = RAGService(store, embedding, chat, profile)
    sources = {}
    for document in dataset.documents:
        created = service.ingest(scope, document.parsed.filename, document.raw)
        for unit in document.parsed.units:
            locator = uuid5(created.version_id, f"{profile.parser_profile_id}:{unit.key}")
            sources[locator] = Source(document.entry.id, created.document_id, created.version_id, unit.key, unit.text)
    return sources


@contextmanager
def _timed(phases: dict[str, float], name: str):
    start = perf_counter()
    try:
        yield
    finally:
        phases[name] = (perf_counter() - start) * 1000


class EvaluationFailure(Exception):
    pass


@dataclass
class Work:
    phases: dict[str, float] = field(default_factory=dict)
    usage: dict[str, Counts] = field(default_factory=dict)
    search: SearchResult | None = None
    ranked: list[Passage] | None = None
    embedding_calls: int = 0

    def record(self, group: Group, question: Question, answer: Answer | None,
               error: str | None = None) -> Record:
        trace = self.search.trace if self.search else None
        if answer is not None:
            answer = answer.model_copy(update={"retrieval": trace})
        return Record(
            group=group, question=question, status=answer.answer_status if answer else "error", answer=answer,
            error_code=error, latency_ms=sum(self.phases.values()), phases_ms=self.phases.copy(),
            strategy=trace.strategy if trace else None, ranked=self.ranked,
            usage=self.usage.copy(), query_embedding_calls=self.embedding_calls,
            rerank_pairs=trace.rerank_pairs if trace else 0,
        )


def _passages(candidates: list[Candidate], sources: dict[UUID, Source]) -> list[Passage]:
    passages = []
    for item in candidates:
        locator = item.locator
        source = sources.get(locator.unit_id)
        if (source is None or source.document_id != item.source_id or source.version_id != locator.version_id
                or not 0 <= locator.start < locator.end <= len(source.text)
                or source.text[locator.start:locator.end] != item.content):
            raise EvaluationFailure("unresolved_candidate")
        passages.append(Passage(id=item.evidence_id, content=item.content,
                                span=Span(document=source.document, unit=source.unit_key,
                                          start=locator.start, end=locator.end)))
    return passages


def _error_code(error) -> str:
    if isinstance(error, psycopg.Error):
        return "database_unavailable"
    if isinstance(error, EvaluationFailure):
        return str(error)
    return error.code


EXPECTED_FAILURES = (EmbeddingError, GenerationError, psycopg.Error, EvaluationFailure)


def _evaluate(question: Question, scope: Scope, retriever: Retriever, chat,
              sources: dict[UUID, Source], *, hybrid: bool):
    group: Group = "B" if hybrid else "A"
    affected: tuple[Group, ...] = ("B", "C", "D") if hybrid else ("A",)
    work = Work()
    answer_id = uuid4()
    try:
        with _timed(work.phases, "retrieval"):
            work.embedding_calls = 1  # The run ingested a nonempty dedicated corpus before sampling.
            work.search = retriever.retrieve(scope, question.question, FINAL_K, review_k=RANK_K)
            reviewed = list(verified_candidates(work.search.review_candidates, scope.id).values())
            if len(reviewed) != len(work.search.review_candidates):
                raise EvaluationFailure("invalid_candidate_scope")
            candidates = list(verified_candidates(work.search.candidates, scope.id).values())
        work.ranked = _passages(reviewed, sources)
        if hybrid and work.search.trace.strategy != "hybrid_reranked":
            raise EvaluationFailure("rerank_degraded")
        if candidates:
            work.usage["generation"] = Counts()
            with _timed(work.phases, "generation"):
                result = chat.generate(question.question, candidates)
            work.usage["generation"] = Counts(input=result.input_tokens, output=result.output_tokens)
            draft = result.draft
        else:
            draft = Draft(claims=[])
        with _timed(work.phases, "binding"):
            base = (answer_from_draft(draft, candidates, scope.id, answer_id)
                    if candidates else insufficient(answer_id, "no_candidates"))
    except EXPECTED_FAILURES as exc:
        for item in affected:
            yield work.record(item, question, None, _error_code(exc))
        return

    yield work.record(group, question, base)
    if not hybrid:
        return
    if not draft.claims:
        yield work.record("C", question, base)
        yield work.record("D", question, base)
        return

    try:
        with _timed(work.phases, "review"):
            bound = answer_from_draft(draft, candidates, scope.id, answer_id, keep_unbound=True)
            if bound.citations:
                judgment = chat.review(question.question, bound, reviewed)
                for name, usage in (("semantic", judgment.semantic_usage), ("coverage", judgment.coverage_usage)):
                    if usage is not None:
                        work.usage[name] = Counts(input=usage.input, output=usage.output)
            else:
                judgment = JudgmentBatch(coverage="uncertain", claims=[])
    except EXPECTED_FAILURES as exc:
        for item in ("C", "D"):
            yield work.record(item, question, None, _error_code(exc))
        return

    for item, mode in (("C", "annotate"), ("D", "gate")):
        with _timed(work.phases, "policy"):
            verdict = validate_answer(bound, reviewed, judgment.claims, mode, chat.version,
                                      coverage=judgment.coverage)
        yield work.record(item, question, verdict)


def run_cases(dataset: Dataset, store, embedding, chat, profile, reranker,
              scope: Scope, *, questions: list[Question], seed: int):
    sources = _prepare(dataset, store, embedding, chat, profile, scope)
    dense = Retriever(store, embedding, profile)
    mixed = Retriever(store, embedding, profile, mode="hybrid", reranker=reranker)
    rng = Random(seed)
    shuffled = list(questions)
    rng.shuffle(shuffled)
    for question in shuffled:
        order = [(False, dense), (True, mixed)]
        rng.shuffle(order)
        for hybrid, retriever in order:
            yield from _evaluate(question, scope, retriever, chat, sources, hybrid=hybrid)
