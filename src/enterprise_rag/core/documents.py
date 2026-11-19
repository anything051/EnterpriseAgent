"""Parse immutable source text and derive chunks with reversible text offsets."""

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import PurePosixPath
import re
from unicodedata import normalize
from uuid import UUID

from pydantic import BaseModel
from pypdf import PdfReader, __version__ as pypdf_version
from pypdf.errors import PyPdfError

PARSER_PROFILE_ID = f"pdf-pypdf-{pypdf_version}__markdown-blocks-v1"
MAX_PDF_PAGES = 100
MAX_SOURCE_CHARS = 300_000
MAX_CHUNKS = 1200
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)\s*$")
FENCE = re.compile(r"^ {0,3}(\x60{3,}|~{3,})(.*)$")
PDF_PARAGRAPH_BREAK = re.compile(r"(?:\r?\n[ \t]*){2,}")


class DocumentInputError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class DocumentNotFound(Exception):
    pass


class SourceView(BaseModel):
    document_id: UUID
    version_id: UUID
    unit_id: UUID
    parser_profile_id: str
    source_text: str
    text_sha256: str
    page: int | None
    page_label: str | None
    heading_path: list[str]
    start_line: int | None
    end_line: int | None
    paragraphs: list[dict[str, int | str]]


@dataclass(frozen=True)
class Scope:
    app_id: str
    collection_id: str

    @property
    def id(self) -> str:
        return f"{self.app_id}:{self.collection_id}"


@dataclass(frozen=True)
class Paragraph:
    id: str
    start: int
    end: int


@dataclass(frozen=True)
class SourceUnit:
    key: str
    text: str
    paragraphs: tuple[Paragraph, ...]
    page: int | None = None
    page_label: str | None = None
    heading_path: tuple[str, ...] = ()
    start_line: int | None = None
    end_line: int | None = None


@dataclass(frozen=True)
class ParsedDocument:
    filename: str
    media_type: str
    sha256: str
    units: tuple[SourceUnit, ...]
    skipped_pages: tuple[int, ...] = ()


@dataclass(frozen=True)
class Chunk:
    unit_key: str
    paragraph_id: str
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class Build:
    document_id: UUID
    version_id: UUID
    index_id: UUID
    revision: int
    chunk_count: int | None


@dataclass(frozen=True)
class IngestResult:
    document_id: UUID
    version_id: UUID
    index_id: UUID
    chunk_count: int
    created: bool
    skipped_pages: tuple[int, ...]


def text_hash(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def parse_document(filename: str, data: bytes) -> ParsedDocument:
    filename = normalize("NFC", PurePosixPath(filename.replace("\\", "/")).name)
    if not filename or len(filename) > 255 or any(ord(char) < 32 or ord(char) == 127 for char in filename):
        raise DocumentInputError("invalid_document", "Invalid document filename.")

    extension = PurePosixPath(filename).suffix.lower()
    if extension not in {".md", ".markdown", ".pdf"}:
        raise DocumentInputError("unsupported_document", "Upload a PDF or UTF-8 Markdown file.")
    if not data:
        raise DocumentInputError("no_extractable_text", "The document contains no extractable text.")

    if extension == ".pdf":
        units, skipped = _pdf_units(data)
        media_type = "application/pdf"
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise DocumentInputError("invalid_document", "The Markdown file must contain UTF-8 text.") from error
        if "\x00" in text:
            raise DocumentInputError("invalid_document", "The Markdown file contains NUL characters.")
        _check_text_budget(len(text))
        units = tuple(_markdown_units(text))
        skipped = ()
        media_type = "text/markdown"

    if not units:
        raise DocumentInputError("no_extractable_text", "The document contains no extractable text.")
    return ParsedDocument(filename, media_type, sha256(data).hexdigest(), units, skipped)


def _check_text_budget(length: int) -> None:
    if length > MAX_SOURCE_CHARS:
        raise DocumentInputError("document_too_large", "The document exceeds the extracted text limit.")


def _pdf_units(data: bytes) -> tuple[tuple[SourceUnit, ...], tuple[int, ...]]:
    if not data.startswith(b"%PDF-"):
        raise DocumentInputError("invalid_document", "The file does not have a PDF header.")
    units = []
    skipped = []
    total_chars = 0
    try:
        reader = PdfReader(BytesIO(data), strict=False, xref_stream_maximum_entries=100_000)
        if reader.is_encrypted:
            raise DocumentInputError("invalid_document", "Encrypted PDFs are unsupported.")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise DocumentInputError("document_too_large", "The PDF exceeds the page limit.")
        labels = reader.page_labels if "/PageLabels" in reader.root_object else []
        for page_number, page in enumerate(reader.pages, 1):
            text = page.extract_text()
            if "\x00" in text:
                raise DocumentInputError("invalid_document", "The PDF text contains NUL characters.")
            total_chars += len(text)
            _check_text_budget(total_chars)
            paragraphs = _pdf_paragraphs(text)
            if not paragraphs:
                skipped.append(page_number)
                continue
            label = labels[page_number - 1] if page_number <= len(labels) else None
            units.append(SourceUnit(f"page-{page_number}", text, paragraphs, page_number, label))
    except DocumentInputError:
        raise
    except (PyPdfError, ValueError, TypeError, KeyError, IndexError, RecursionError) as error:
        raise DocumentInputError("invalid_document", "The PDF could not be parsed.") from error
    return tuple(units), tuple(skipped)


def _pdf_paragraphs(text: str) -> tuple[Paragraph, ...]:
    spans = []
    start = 0
    for separator in PDF_PARAGRAPH_BREAK.finditer(text):
        spans.append((start, separator.start()))
        start = separator.end()
    spans.append((start, len(text)))
    return tuple(Paragraph(f"p{number}", start, end)
                 for number, (start, end) in enumerate(spans, 1) if text[start:end].strip())


def _markdown_units(text: str):
    lines = re.findall(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+$", text)
    headings: list[tuple[int, str]] = []
    block_start = None
    fence = None
    paragraph_number = 0

    def make_unit(start: int, end: int, number: int) -> SourceUnit:
        content = "".join(lines[start:end]).rstrip("\r\n")
        key = f"p{number}"
        return SourceUnit(key, content, (Paragraph(key, 0, len(content)),),
                          heading_path=tuple(title for _, title in headings),
                          start_line=start + 1, end_line=end)

    for index, raw in enumerate(lines):
        line = raw.rstrip("\r\n")
        fence_match = FENCE.match(line)
        if fence:
            if fence_match:
                marker, tail = fence_match.groups()
                if marker[0] == fence[0] and len(marker) >= len(fence) and not tail.strip():
                    fence = None
            continue

        heading = HEADING.match(line)
        if (heading or not line.strip()) and block_start is not None:
            paragraph_number += 1
            yield make_unit(block_start, index, paragraph_number)
            block_start = None

        if heading:
            hashes, title = heading.groups()
            level = len(hashes)
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, re.sub(r"[ \t]+#+[ \t]*$", "", title).strip()))
            paragraph_number += 1
            yield make_unit(index, index + 1, paragraph_number)
        elif line.strip() and block_start is None:
            block_start = index

        if fence_match:
            fence = fence_match.group(1)

    if block_start is not None:
        yield make_unit(block_start, len(lines), paragraph_number + 1)


def chunk_units(units: tuple[SourceUnit, ...], max_chars: int) -> list[Chunk]:
    chunks = []
    for unit in units:
        for paragraph in unit.paragraphs:
            for start in range(paragraph.start, paragraph.end, max_chars):
                end = min(start + max_chars, paragraph.end)
                text = unit.text[start:end]
                if text.strip():
                    chunks.append(Chunk(unit.key, paragraph.id, start, end, text))
                    if len(chunks) > MAX_CHUNKS:
                        raise DocumentInputError("document_too_large", "The document exceeds the chunk limit.")
    return chunks
