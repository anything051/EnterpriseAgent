"""Map a verified source excerpt to PDF glyphs when the two parsers agree."""

from io import BytesIO
from math import isfinite
from typing import Literal

import pdfplumber
from pdfplumber.utils.exceptions import MalformedPDFException, PdfminerException
from pydantic import BaseModel, Field

from enterprise_rag.core.documents import DocumentInputError

MAX_HIGHLIGHT_CHARS = 900


class Box(BaseModel):
    x0: float
    top: float
    x1: float
    bottom: float


class Highlight(BaseModel):
    status: Literal["highlight", "page_only", "text_only"]
    reason: Literal["not_pdf", "unsupported_page", "text_mismatch", "invalid_geometry"] | None = None
    page: int | None = None
    excerpt: str
    start: int
    end: int
    page_width: float | None = None
    page_height: float | None = None
    coordinate_system: Literal["top_left_points"] | None = None
    page_box: Literal["media_box"] | None = None
    rotation: int | None = None
    extractor: str | None = None
    boxes: list[Box] = Field(default_factory=list)


def locate_span(original: bytes | None, page_number: int | None, source_text: str, start: int, end: int) -> Highlight:
    if not 0 <= start < end <= len(source_text) or end - start > MAX_HIGHLIGHT_CHARS:
        raise DocumentInputError("invalid_span", "The highlight span must be within the source and at most 900 characters.")

    fields = dict(page=page_number, excerpt=source_text[start:end], start=start, end=end)
    if original is None or page_number is None:
        return Highlight(status="text_only", reason="not_pdf", **fields)

    try:
        with pdfplumber.open(BytesIO(original), pages=[page_number]) as pdf:
            if not pdf.pages:
                return Highlight(status="page_only", reason="unsupported_page", **fields)
            page = pdf.pages[0]
            if (page.rotation or page.page_obj.attrs.get("UserUnit", 1) != 1
                    or tuple(page.bbox) != (0, 0, page.width, page.height) or page.cropbox != page.mediabox):
                return Highlight(status="page_only", reason="unsupported_page", **fields)
            glyphs = [(char, glyph) for glyph in page.chars for char in glyph["text"] if not char.isspace()]
            expected = [(index, char) for index, char in enumerate(source_text) if not char.isspace()]
            if [char for _, char in expected] != [char for char, _ in glyphs]:
                return Highlight(status="page_only", reason="text_mismatch", **fields)

            selected = []
            for (index, _), (_, glyph) in zip(expected, glyphs, strict=True):
                if start <= index < end:
                    x0, top, x1, bottom = (float(glyph[key]) for key in ("x0", "top", "x1", "bottom"))
                    if (not all(isfinite(value) for value in (x0, top, x1, bottom))
                            or not 0 <= x0 < x1 <= page.width or not 0 <= top < bottom <= page.height):
                        return Highlight(status="page_only", reason="invalid_geometry", **fields)
                    selected.append((x0, top, x1, bottom))
            if not selected:
                return Highlight(status="page_only", reason="invalid_geometry", **fields)

            return Highlight(status="highlight", coordinate_system="top_left_points", page_box="media_box", rotation=0,
                             extractor=f"pdfplumber-{pdfplumber.__version__}", page_width=page.width,
                             page_height=page.height, **fields,
                             boxes=[Box(x0=x0, top=top, x1=x1, bottom=bottom)
                                    for x0, top, x1, bottom in dict.fromkeys(selected)])
    except (MalformedPDFException, PdfminerException, KeyError, ValueError, TypeError, IndexError, RecursionError):
        return Highlight(status="page_only", reason="unsupported_page", **fields)
