from pathlib import Path
from uuid import uuid4

import pytest
from pypdf import PdfWriter

from enterprise_rag.core.citations import Candidate, DocumentLocator, answer_from_draft
from enterprise_rag.core.documents import DocumentInputError, chunk_units, parse_document
from enterprise_rag.core.embeddings import EmbeddingError, validate_vectors
from enterprise_rag.core.generation import Draft, DraftClaim

SAMPLES = Path(__file__).resolve().parents[2] / "data" / "samples"


def test_markdown_preserves_original_spans_headings_and_lines():
    body = "# 内存\r\n\r\n## 页表\r\n\r\n汉字🙂第一句。\r\n第二行。\r\n\r\n另一个段落。\r\n"
    parsed = parse_document("os.md", body.encode())
    unit = next(u for u in parsed.units if "🙂" in u.text)
    assert unit.heading_path == ("内存", "页表")
    assert (unit.start_line, unit.end_line) == (5, 6)
    assert unit.text == "汉字🙂第一句。\r\n第二行。"
    chunks = [c for c in chunk_units(parsed.units, 8) if c.unit_key == unit.key]
    assert len(chunks) > 1
    assert all(unit.text[c.start:c.end] == c.text for c in chunks)
    assert "".join(c.text for c in chunks) == unit.text


def test_markdown_fenced_code_does_not_change_heading_path():
    body = b"# Root\n\n````markdown\n# Not a heading\n\n```\n````\n\nAfter"
    parsed = parse_document("safe.md", body)
    assert all(u.heading_path == ("Root",) for u in parsed.units)
    assert any("# Not a heading" in u.text for u in parsed.units)


def test_pdf_preserves_chinese_text_and_physical_page():
    parsed = parse_document("course_memory.pdf", (SAMPLES / "course_memory.pdf").read_bytes())
    assert [u.page for u in parsed.units] == [1, 2]
    unit = parsed.units[1]
    assert "页面置换" in unit.text
    chunks = [c for c in chunk_units(parsed.units, 100) if c.unit_key == unit.key]
    assert all(c.text == unit.text[c.start:c.end] for c in chunks)


@pytest.mark.parametrize(
    ("name", "payload", "code"),
    [("wrong.exe", b"abc", "unsupported_document"), ("bad.md", b"\xff", "invalid_document"),
     ("bad.pdf", b"%PDF-not-a-document", "invalid_document"),
     ("nul.md", b"a\x00b", "invalid_document"), ("empty.md", b"  \n", "no_extractable_text")],
)
def test_rejects_unsupported_or_invalid_document(name, payload, code):
    with pytest.raises(DocumentInputError) as caught:
        parse_document(name, payload)
    assert caught.value.code == code


def test_blank_pdf_has_no_extractable_text(tmp_path):
    path = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer.write(path)
    with pytest.raises(DocumentInputError, match="extractable") as caught:
        parse_document("blank.pdf", path.read_bytes())
    assert caught.value.code == "no_extractable_text"


@pytest.mark.parametrize("vectors", [[[1.0]], [[float("nan"), 0]], [[0, 0]], [],
                                     [[1e100, 1.0]], [[1e-99, 0.0]], [[True, 0]]])
def test_rejects_invalid_embeddings(vectors):
    with pytest.raises(EmbeddingError):
        validate_vectors(vectors, count=1, dimension=2)


def candidate(scope="demo:course"):
    return Candidate(
        evidence_id=str(uuid4()), scope_id=scope, source_id=uuid4(),
        locator=DocumentLocator(version_id=uuid4(), parser_profile_id="pypdf:test",
                                unit_id=uuid4(), page=2, paragraph_id="p1",
                                start=0, end=7, chunk_id=uuid4()),
        content="页表保存映射。", source_text="页表保存映射。其余内容",
    )


def test_binds_only_authorized_real_spans():
    allowed = candidate()
    unauthorized = candidate(scope="demo:other")
    draft = Draft(claims=[DraftClaim(text="页表保存映射。", evidence_ids=[
        allowed.evidence_id, "invented", unauthorized.evidence_id, allowed.evidence_id
    ])])
    answer = answer_from_draft(draft, [allowed, unauthorized], "demo:course", uuid4())
    assert answer.answer_status == "answered"
    assert [item.evidence_id for item in answer.evidence] == [allowed.evidence_id]
    assert len(answer.citations) == 1
    assert answer.citations[0].support_status == "unchecked"
    assert answer.citations[0].claim_id == answer.claims[0].claim_id


@pytest.mark.parametrize("invalid_kind", ["fabricated", "out_of_scope", "span", "unsupported_claim"])
def test_refuses_when_any_claim_lacks_a_resolvable_citation(invalid_kind):
    ev = candidate(scope="demo:other" if invalid_kind == "out_of_scope" else "demo:course")
    if invalid_kind == "span":
        ev = ev.model_copy(update={"source_text": "错误原文"})
    ids = ["fabricated"] if invalid_kind == "fabricated" else [ev.evidence_id]
    claims = [DraftClaim(text="Claim", evidence_ids=ids)]
    if invalid_kind == "unsupported_claim":
        claims.append(DraftClaim(text="Another claim", evidence_ids=[]))
    answer = answer_from_draft(Draft(claims=claims), [ev], "demo:course", uuid4())
    assert answer.answer_status == "insufficient_evidence"
    assert not answer.citations
    assert not answer.claims
    assert not answer.evidence


def test_unicode_line_separator_is_not_a_physical_markdown_newline():
    body = "第一行\u2028仍是同一行\n\n第二段".encode()
    parsed = parse_document("physical.md", body)
    assert [(unit.start_line, unit.end_line) for unit in parsed.units] == [(1, 1), (3, 3)]
    assert "\u2028" in parsed.units[0].text
