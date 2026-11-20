"""Versioned CourseRAG labels and portable source coordinates."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from enterprise_rag.core.citations import Answer
from enterprise_rag.core.documents import PARSER_PROFILE_ID, ParsedDocument, parse_document

Group = Literal["A", "B", "C", "D"]
Refusal = Literal["no_answer", "conflict", "expired", "partial"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Span(StrictModel):
    document: str
    unit: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str | None = None

    @model_validator(mode="after")
    def ordered(self):
        if self.start >= self.end:
            raise ValueError("Invalid half-open span")
        return self


class Fact(StrictModel):
    id: str
    patterns: list[str] = Field(min_length=1)
    forbidden: list[str] = Field(default_factory=list)
    bundles: list[list[Span]] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_rules(self):
        if any(not bundle for bundle in self.bundles):
            raise ValueError("Gold evidence bundles cannot be empty")
        for pattern in self.patterns + self.forbidden:
            re.compile(pattern)
        return self


class Question(StrictModel):
    id: str
    split: Literal["development", "holdout"] = "development"
    question: str = Field(min_length=1, max_length=2000)
    gold_answer: str
    tags: list[str] = Field(min_length=1)
    answerable: bool
    expected_refusal: Refusal | None = None
    facts: list[Fact] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_labels(self):
        if self.answerable and (not self.facts or not self.gold_answer or self.expected_refusal):
            raise ValueError(f"{self.id}: answerable question requires facts and gold answer")
        if not self.answerable and (not self.expected_refusal or self.facts):
            raise ValueError(f"{self.id}: unanswerable question requires a refusal reason")
        if len({fact.id for fact in self.facts}) != len(self.facts):
            raise ValueError(f"{self.id}: duplicate fact ID")
        return self


class ManifestEntry(StrictModel):
    id: str
    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class Manifest(StrictModel):
    version: str
    parser_profile: str
    label_provenance: str
    documents: list[ManifestEntry] = Field(min_length=1)


@dataclass(frozen=True)
class Document:
    entry: ManifestEntry
    raw: bytes
    parsed: ParsedDocument


@dataclass(frozen=True)
class Dataset:
    manifest: Manifest
    fingerprint: str
    documents: list[Document]
    questions: list[Question]


class Passage(StrictModel):
    id: str
    span: Span
    content: str


class Counts(StrictModel):
    input: int | None = Field(default=None, ge=0)
    output: int | None = Field(default=None, ge=0)


class Record(StrictModel):
    group: Group
    question: Question
    status: Literal["answered", "insufficient_evidence", "error"]
    latency_ms: float = Field(ge=0)
    phases_ms: dict[str, float] = Field(default_factory=dict)
    strategy: str | None = None
    ranked: list[Passage] | None = None
    answer: Answer | None = None
    usage: dict[str, Counts] = Field(default_factory=dict)
    query_embedding_calls: int = Field(default=0, ge=0)
    rerank_pairs: int = Field(default=0, ge=0)
    error_code: str | None = None


def load_dataset(root: str | Path) -> Dataset:
    root = Path(root).resolve()
    manifest_raw = (root / "manifest.json").read_bytes()
    questions_raw = (root / "questions.jsonl").read_bytes()
    manifest = Manifest.model_validate_json(manifest_raw)
    if manifest.parser_profile != PARSER_PROFILE_ID:
        raise ValueError(f"Dataset parser profile requires {manifest.parser_profile}")

    documents = []
    unit_text = {}
    names = set()
    for entry in manifest.documents:
        path = (root / entry.path).resolve()
        if not path.is_relative_to(root) or path.name in names or entry.id in unit_text:
            raise ValueError(f"Invalid or duplicate dataset document: {entry.id}")
        names.add(path.name)
        raw = path.read_bytes()
        if sha256(raw).hexdigest() != entry.sha256:
            raise ValueError(f"Source hash mismatch: {entry.id}")
        parsed = parse_document(path.name, raw)
        documents.append(Document(entry, raw, parsed))
        unit_text[entry.id] = {unit.key: unit.text for unit in parsed.units}

    questions = [Question.model_validate_json(line) for line in questions_raw.splitlines() if line.strip()]
    if not questions or len({item.id for item in questions}) != len(questions):
        raise ValueError("Dataset requires unique, nonempty question IDs")
    for question in questions:
        for fact in question.facts:
            for bundle in fact.bundles:
                for span in bundle:
                    text = unit_text.get(span.document, {}).get(span.unit, "")
                    if span.end > len(text) or not span.quote or text[span.start:span.end] != span.quote:
                        raise ValueError(f"{question.id}: unresolved gold span {span.document}/{span.unit}")
    fingerprint = sha256(manifest_raw + b"\0" + questions_raw).hexdigest()
    return Dataset(manifest, fingerprint, documents, questions)
