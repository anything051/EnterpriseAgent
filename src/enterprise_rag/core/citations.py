"""Bind model proposals to source evidence verified within the current request."""

from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from .documents import text_hash
from .generation import Draft


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


class Citation(BaseModel):
    citation_id: UUID
    answer_id: UUID
    claim_id: UUID
    evidence_id: str
    binding_status: Literal["bound"] = "bound"
    support_status: Literal["unchecked"] = "unchecked"


class Answer(BaseModel):
    answer_id: UUID
    answer_status: Literal["answered", "insufficient_evidence"]
    answer: str
    claims: list[Claim] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)


def insufficient(answer_id: UUID) -> Answer:
    return Answer(answer_id=answer_id, answer_status="insufficient_evidence",
                  answer="现有资料中没有足够的可引用证据回答这个问题。")


def answer_from_draft(draft: Draft, candidates: list[Candidate], scope_id: str, answer_id: UUID) -> Answer:
    allowed = {}
    for item in candidates:
        start, end = item.locator.start, item.locator.end
        if (item.scope_id == scope_id and 0 <= start < end <= len(item.source_text)
                and item.content.strip() and item.source_text[start:end] == item.content):
            allowed[item.evidence_id] = item

    if not draft.claims:
        return insufficient(answer_id)

    claims = []
    citations = []
    evidence = {}
    for proposal in draft.claims:
        cited = [allowed[key] for key in dict.fromkeys(proposal.evidence_ids) if key in allowed]
        if not cited:
            return insufficient(answer_id)
        claim = Claim(claim_id=uuid4(), text=proposal.text)
        claims.append(claim)
        for item in cited:
            citations.append(Citation(citation_id=uuid4(), answer_id=answer_id, claim_id=claim.claim_id,
                                      evidence_id=item.evidence_id))
            if item.evidence_id not in evidence:
                evidence[item.evidence_id] = Evidence(
                    evidence_id=item.evidence_id, scope_id=item.scope_id, source_id=item.source_id,
                    locator=item.locator, content=item.content, content_hash=text_hash(item.content),
                )

    return Answer(answer_id=answer_id, answer_status="answered", answer="\n".join(claim.text for claim in claims),
                  claims=claims, citations=citations, evidence=list(evidence.values()))
