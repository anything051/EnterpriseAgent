"""Structured output expected from a configured chat model."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from .citations import Candidate


class GenerationError(Exception):
    def __init__(self, message: str, *, code: str = "chat_unavailable"):
        super().__init__(message)
        self.code = code


class DraftClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=1200)
    evidence_ids: list[str] = Field(max_length=10)


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[DraftClaim] = Field(max_length=8)


@dataclass(frozen=True)
class GeneratedDraft:
    draft: Draft
    input_tokens: int | None = None
    output_tokens: int | None = None


class Chat(Protocol):
    def generate(self, question: str, candidates: list["Candidate"]) -> GeneratedDraft: ...
