"""Validate document support at citation and claim level and apply refusal policy."""

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .citations import (
    Answer, Candidate, Citation, ClaimCheck, Coverage, QuoteSpan, Support, VerificationTrace,
    evidence_from_candidate, insufficient,
)

ReviewMode = Literal["annotate", "gate"]


class EvidenceJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    status: Support
    quote: str = Field(max_length=900)


class ClaimJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int
    status: Support
    evidence: list[EvidenceJudgment] = Field(max_length=10)
    conflict_ids: list[str] = Field(max_length=10)


class SemanticReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[ClaimJudgment] = Field(max_length=8)


class CoveragePart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirement: str = Field(min_length=1, max_length=300)
    claim_numbers: list[int] = Field(max_length=8)


class CoverageReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirements: list[CoveragePart] = Field(max_length=8)

    def status(self, claim_count: int) -> Coverage:
        if not self.requirements or any(number < 1 or number > claim_count
                                        for part in self.requirements for number in part.claim_numbers):
            return "uncertain"
        return "complete" if all(part.claim_numbers for part in self.requirements) else "incomplete"


class CallUsage(BaseModel):
    input: int | None = None
    output: int | None = None


class JudgmentBatch(SemanticReview):
    coverage: Coverage
    semantic_usage: CallUsage | None = None
    coverage_usage: CallUsage | None = None


class EvidenceJudge(Protocol):
    version: str

    def review(self, question: str, answer: Answer, candidates: list[Candidate]) -> JudgmentBatch: ...


def _exact_quote(item: Candidate, quote: str) -> QuoteSpan | None:
    quote = quote.strip()
    if not quote:
        return None
    offset = item.content.find(quote)
    if offset < 0 or item.content.find(quote, offset + 1) >= 0:
        return None
    start = item.locator.start + offset
    return QuoteSpan(start=start, end=start + len(quote), excerpt=quote)


def validate_answer(answer: Answer, candidates: list[Candidate], judgments: list[ClaimJudgment],
                    mode: ReviewMode, version: str, *, coverage: Coverage) -> Answer:
    now = datetime.now(timezone.utc)
    allowed = {candidate.evidence_id: candidate for candidate in candidates}
    citations_by_claim = defaultdict(list)
    for citation in answer.citations:
        citations_by_claim[citation.claim_id].append(citation)

    counts = Counter(item.number for item in judgments)
    reviews = {item.number: item for item in judgments if counts[item.number] == 1}
    checked = []
    checked_citations: list[Citation] = []
    conflicts_seen = {}

    for number, claim in enumerate(answer.claims, 1):
        citations = citations_by_claim[claim.claim_id]
        cited_ids = {item.evidence_id for item in citations}
        judgment = reviews.get(number)
        evidence = {item.evidence_id: item for item in judgment.evidence} if judgment else {}
        check = ClaimCheck(claim_id=claim.claim_id, number=number, status="uncertain", reason="invalid_judgment")

        if not citations:
            check.status = "unsupported"
            check.reason = "missing_citation"
        elif (judgment is not None and cited_ids <= evidence.keys() <= allowed.keys()
              and len(evidence) == len(judgment.evidence) and set(judgment.conflict_ids) <= allowed.keys()):
            check.reason = "reviewed"
            contradictions = [item.evidence_id for item in evidence.values() if item.status == "contradicted"]
            check.conflict_ids = list(dict.fromkeys([*contradictions, *judgment.conflict_ids]))
            if check.conflict_ids:
                check.status = "contradicted"
            elif judgment.status == "contradicted":
                check.status = "uncertain"
            elif judgment.status == "supported" and len(cited_ids) == 1:
                check.status = evidence[citations[0].evidence_id].status
            else:
                check.status = judgment.status

        for citation in citations:
            assessment = evidence.get(citation.evidence_id) if check.reason == "reviewed" else None
            quote = _exact_quote(allowed[citation.evidence_id], assessment.quote) if assessment else None
            checked_citations.append(citation.model_copy(update={
                "support_status": assessment.status if assessment else "uncertain",
                "validator_version": version,
                "checked_at": now,
                "quote_span": quote,
            }))
        for evidence_id in check.conflict_ids:
            conflicts_seen[evidence_id] = evidence_from_candidate(allowed[evidence_id])
        checked.append(check)

    trace = VerificationTrace(mode=mode, validator_version=version, checked_at=now, claims=checked,
                              coverage=coverage, conflict_evidence=list(conflicts_seen.values()))
    if not answer.citations or any(item.reason == "missing_citation" for item in checked):
        result = insufficient(answer.answer_id, "missing_citations")
    elif mode == "gate" and any(item.status == "contradicted" for item in checked):
        result = insufficient(answer.answer_id, "contradictory_evidence")
    elif mode == "gate" and any(item.status != "supported" for item in checked):
        result = insufficient(answer.answer_id, "unsupported_claims")
    elif mode == "gate" and coverage != "complete":
        result = insufficient(answer.answer_id, "incomplete_answer")
    else:
        result = answer.model_copy(update={"citations": checked_citations})
    result.verification = trace
    return result
