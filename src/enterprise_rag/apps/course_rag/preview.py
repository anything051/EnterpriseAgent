"""Small server-rendered PDF previews with optional verified glyph highlighting."""

from io import BytesIO
from math import isfinite
from threading import Lock
from typing import Literal

from PIL import Image, ImageDraw
from pypdfium2 import PdfDocument, PdfiumError

from enterprise_rag.adapters.pdf_highlight import Highlight
from enterprise_rag.core.documents import DocumentInputError

_RENDER_LOCK = Lock()


def render_page(original: bytes, page_number: int, highlight: Highlight | None) -> tuple[bytes, Literal["highlight", "page_only"]]:
    try:
        # PDFium's process-global state is not safe across concurrent threads.
        with _RENDER_LOCK, PdfDocument(original) as document:
            page = document[page_number - 1]
            width, height = page.get_size()
            if not all(isfinite(value) and value > 0 for value in (width, height)):
                raise ValueError("Invalid page size")
            rendered = page.render(scale=min(1.6, 1400 / max(width, height)))
            try:
                image = rendered.to_pil().convert("RGBA")
            finally:
                rendered.close()
            page.close()
    except (PdfiumError, ValueError, IndexError, OverflowError) as error:
        raise DocumentInputError("preview_unavailable", "Unable to render this PDF page. Read the source text.") from error

    mode = "page_only"
    if (highlight is not None and highlight.status == "highlight"
            and highlight.page_width is not None and highlight.page_height is not None
            and abs(width - highlight.page_width) < 0.01 and abs(height - highlight.page_height) < 0.01):
        mode = "highlight"
        layer = Image.new("RGBA", image.size)
        overlay = ImageDraw.Draw(layer)
        sx, sy = image.width / width, image.height / height
        for box in highlight.boxes:
            overlay.rectangle((box.x0 * sx, box.top * sy, box.x1 * sx, box.bottom * sy), fill=(253, 195, 53, 105))
        image = Image.alpha_composite(image, layer)
    buffer = BytesIO()
    image.convert("RGB").save(buffer, "PNG")
    return buffer.getvalue(), mode
