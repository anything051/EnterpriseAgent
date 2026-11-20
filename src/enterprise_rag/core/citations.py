"""Bind model proposals to source evidence verified within the current request."""

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from .documents import text_hash
from .generation import Draft
from .ranking import RetrievalTrace

Support = Literal["supported", "unsupported", "contradicted", "uncertain"]
ValidationMode = Literal["off", "annotate", "gate"]
Coverage = Literal["complete", "incomplete", "uncertain"]
RefusalReason = Literal[
    "no_candidates", "no_draft", "missing_citations", "unsupported_claims", "contradictory_evidence",
    "incomplete_answer",
]


class DocumentLocator(BaseModel):
    version_id: UUID
    parser_profile_id: str
    unit_id: UUID
    page: int | None = None
    page_label: str | None = None
    heading_path: list[str] = Field(default_factory=list)
    start_line: int | None = None
    end_line: int | None = None
    paragraph_id: str
    start: int
    end: int
    chunk_id: UUID


class Candidate(BaseModel):
    evidence_id: str
    scope_id: str
    source_id: UUID
    locator: DocumentLocator
    content: str
    source_text: str


class Evidence(BaseModel):
    evidence_id: str
    scope_id: str
    source_type: Literal["document"] = "document"
    source_id: UUID
    locator: DocumentLocator
    content: str
    content_hash: str


class Claim(BaseModel):
    claim_id: UUID
    text: str


class QuoteSpan(BaseModel):
    start: int
    end: int
    excerpt: str


class Citation(BaseModel):
    citation_id: UUID
    answer_id: UUID
    claim_id: UUID
    evidence_id: str
    binding_status: Literal["bound"] = "bound"
    support_status: Support | Literal["unchecked"] = "unchecked"
    validator_version: str | None = None
    checked_at: datetime | None = None
    quote_span: QuoteSpan | None = None


class ClaimCheck(BaseModel):
    claim_id: UUID
    number: int
    status: Support
    reason: Literal["reviewed", "missing_citation", "invalid_judgment"]
    conflict_ids: list[str] = Field(default_factory=list)


class VerificationTrace(BaseModel):
    mode: Literal["annotate", "gate"]
    validator_version: str
    checked_at: datetime
    claims: list[ClaimCheck]
    coverage: Coverage
    conflict_evidence: list[Evidence] = Field(default_factory=list)


class Answer(BaseModel):
    answer_id: UUID
    answer_status: Literal["answered", "insufficient_evidence"]
    answer: str
    claims: list[Claim] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    refusal_reason: RefusalReason | None = None
    retrieval: RetrievalTrace | None = None
    verification: VerificationTrace | None = None


def insufficient(answer_id: UUID, reason: RefusalReason = "missing_citations") -> Answer:
    messages = {
        "contradictory_evidence": "检索到的资料对关键事实存在冲突，无法给出可靠答案。",
        "incomplete_answer": "现有资料不足以完整回答这个问题。",
    }
    message = messages.get(reason, "现有资料中没有足够的可引用证据回答这个问题。")
    return Answer(answer_id=answer_id, answer_status="insufficient_evidence", answer=message, refusal_reason=reason)


def verified_candidates(candidates: list[Candidate], scope_id: str) -> dict[str, Candidate]:
    allowed = {}
    for item in candidates:
        start, end = item.locator.start, item.locator.end
        if (item.scope_id == scope_id and 0 <= start < end <= len(item.source_text)
                and item.content.strip() and item.source_text[start:end] == item.content):
            allowed[item.evidence_id] = item
    return allowed


def evidence_from_candidate(item: Candidate) -> Evidence:
    return Evidence(evidence_id=item.evidence_id, scope_id=item.scope_id, source_id=item.source_id,
                    locator=item.locator, content=item.content, content_hash=text_hash(item.content))


def answer_from_draft(draft: Draft, candidates: list[Candidate], scope_id: str, answer_id: UUID, *,
                      keep_unbound: bool = False) -> Answer:
    if not draft.claims:
        return insufficient(answer_id, "no_draft")
    allowed = verified_candidates(candidates, scope_id)

    claims = []
    citations = []
    evidence = {}
    for proposal in draft.claims:
        cited = [allowed[key] for key in dict.fromkeys(proposal.evidence_ids) if key in allowed]
        if not cited and not keep_unbound:
            return insufficient(answer_id)
        claim = Claim(claim_id=uuid4(), text=proposal.text)
        claims.append(claim)
        for item in cited:
            citations.append(Citation(citation_id=uuid4(), answer_id=answer_id, claim_id=claim.claim_id,
                                      evidence_id=item.evidence_id))
            if item.evidence_id not in evidence:
                evidence[item.evidence_id] = evidence_from_candidate(item)

    return Answer(answer_id=answer_id, answer_status="answered", answer="\n".join(claim.text for claim in claims),
                  claims=claims, citations=citations, evidence=list(evidence.values()))
