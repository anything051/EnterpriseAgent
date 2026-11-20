from pathlib import Path
from uuid import UUID, uuid4

import pytest

from enterprise_rag.core.citations import Candidate, DocumentLocator, answer_from_draft
from enterprise_rag.core.documents import parse_document
from enterprise_rag.core.generation import Draft, DraftClaim
from enterprise_rag.core.ranking import bm25_rank, reciprocal_rank_fusion, tokenize
from enterprise_rag.core.validation import ClaimJudgment, EvidenceJudgment, validate_answer
from enterprise_rag.adapters.pdf_highlight import locate_span

SAMPLES = Path(__file__).resolve().parents[2] / "data" / "samples"


def ids(*values):
    return [UUID(int=value) for value in values]


def test_tokenizer_preserves_exact_codes_and_segments_chinese():
    assert tokenize("　ＡＢ-１７０ 页面置换，ＬＲＵ") == ["ab-170", "页面", "置换", "lru"]


def test_bm25_filters_zero_scores_and_orders_ties_by_chunk_id():
    first, second, absent = ids(1, 2, 3)
    ranked = bm25_rank("AB-170", [(second, "AB-170"), (absent, "AB-171"), (first, "AB-170")], 20)
    assert ranked == [first, second]
    assert bm25_rank("AB-999", [(first, "AB-170")], 20) == []


def test_rrf_counts_duplicate_chunks_once_per_branch_and_breaks_ties():
    first, second, third = ids(1, 2, 3)
    assert reciprocal_rank_fusion([second, second, first], [first, second, third]) == [first, second, third]
    assert reciprocal_rank_fusion([], []) == []


def candidate(key, text, base=5, scope="demo:course"):
    return Candidate(evidence_id=key, scope_id=scope, source_id=uuid4(), source_text=" " * base + text,
                     content=text, locator=DocumentLocator(version_id=uuid4(), unit_id=uuid4(), chunk_id=uuid4(),
                     parser_profile_id="fixture", paragraph_id="p1", start=base, end=base + len(text)))


def draft_answer(candidates, refs):
    return answer_from_draft(Draft(claims=[DraftClaim(text="青禾仓的登记期限是24小时。", evidence_ids=refs)]),
                             candidates, "demo:course", uuid4(), keep_unbound=True)


def test_combined_evidence_support_keeps_individual_limitations_and_exact_quote():
    a = candidate("A", "青禾仓遵循规则Q。")
    b = candidate("B", "规则Q要求在24小时内登记。")
    answer = draft_answer([a, b], ["A", "B"])
    judgement = ClaimJudgment(number=1, status="supported", evidence=[
        EvidenceJudgment(evidence_id="A", status="unsupported", quote="遵循规则Q"),
        EvidenceJudgment(evidence_id="B", status="unsupported", quote="24小时"),
    ], conflict_ids=[])
    checked = validate_answer(answer, [a, b], [judgement], "gate", "fixture:v1", coverage="complete")
    assert checked.answer_status == "answered"
    assert checked.verification.claims[0].status == "supported"
    assert all(c.support_status == "unsupported" for c in checked.citations)
    quote = checked.citations[1].quote_span
    assert b.source_text[quote.start:quote.end] == "24小时"


@pytest.mark.parametrize("mode", ["annotate", "gate"])
def test_contradiction_from_uncited_evidence_overrides_support(mode):
    a = candidate("A", "青禾仓规定24小时登记。")
    b = candidate("B", "青禾仓规定48小时登记。")
    answer = draft_answer([a, b], ["A"])
    judgement = ClaimJudgment(number=1, status="supported", evidence=[
        EvidenceJudgment(evidence_id="A", status="supported", quote="24小时"),
    ], conflict_ids=["B"])
    checked = validate_answer(answer, [a, b], [judgement], mode, "fixture:v1", coverage="complete")
    assert checked.verification.claims[0].status == "contradicted"
    assert checked.verification.claims[0].conflict_ids == ["B"]
    if mode == "gate":
        assert checked.answer_status == "insufficient_evidence"
        assert checked.refusal_reason == "contradictory_evidence"
        assert checked.claims == []
    else:
        assert checked.answer_status == "answered"
        assert checked.citations[0].support_status == "supported"


def test_rejects_invented_judgment_references_and_single_evidence_support_mismatch():
    item = candidate("A", "值班人员负责登记。")
    answer = draft_answer([item], ["A"])
    for evidence_id in ("A", "invented"):
        judgement = ClaimJudgment(number=1, status="supported", evidence=[
            EvidenceJudgment(evidence_id=evidence_id, status="unsupported", quote="不存在的引文"),
        ], conflict_ids=[])
        checked = validate_answer(answer, [item], [judgement], "gate", "fixture:v1", coverage="complete")
        assert checked.answer_status == "insufficient_evidence"
        assert checked.verification.claims[0].status != "supported"


@pytest.mark.parametrize("mode", ["annotate", "gate"])
def test_partial_missing_evidence_is_auditable(mode):
    item = candidate("A", "登记需要24小时。")
    answer = answer_from_draft(Draft(claims=[
        DraftClaim(text="登记需要24小时。", evidence_ids=["A"]),
        DraftClaim(text="下一班次处置。", evidence_ids=["invented"]),
    ]), [item], "demo:course", uuid4(), keep_unbound=True)
    judgment = ClaimJudgment(number=1, status="supported", evidence=[
        EvidenceJudgment(evidence_id="A", status="supported", quote="24小时"),
    ], conflict_ids=[])
    checked = validate_answer(answer, [item], [judgment], mode, "fixture:v1", coverage="complete")
    assert checked.answer_status == "insufficient_evidence"
    assert checked.refusal_reason == "missing_citations"
    assert checked.verification.claims[1].reason == "missing_citation"


def test_pdf_locator_resolves_pixel_independent_points_and_falls_back_on_mismatch():
    data = (SAMPLES / "course_memory.pdf").read_bytes()
    text = parse_document("course_memory.pdf", data).units[1].text
    start = text.index("最长时间")
    payload = locate_span(data, 2, text, start, start + 4)
    assert payload.status == "highlight"
    assert payload.excerpt == "最长时间"
    assert payload.page == 2
    assert payload.page_width > 0 and payload.page_height > 0
    assert payload.boxes
    assert all(0 <= box.x0 < box.x1 <= payload.page_width for box in payload.boxes)
    fallback = locate_span(data, 2, text.replace("最", "非", 1), start, start + 4)
    assert fallback.status == "page_only"
    assert fallback.boxes == []


def test_supported_but_incomplete_answer_is_not_presented_as_complete():
    item = candidate("A", "盘点异常须在24小时内登记。")
    answer = draft_answer([item], ["A"])
    judgment = ClaimJudgment(number=1, status="supported", evidence=[
        EvidenceJudgment(evidence_id="A", status="supported", quote="24小时"),
    ], conflict_ids=[])
    checked = validate_answer(answer, [item], [judgment], "gate", "fixture:v1", coverage="incomplete")
    assert checked.answer_status == "insufficient_evidence"
    assert checked.refusal_reason == "incomplete_answer"
    assert checked.verification.claims[0].status == "supported"
    assert checked.verification.coverage == "incomplete"


def test_highlight_falls_back_for_nonstandard_pdf_units():
    from io import BytesIO

    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import NameObject, NumberObject

    source = (SAMPLES / "course_memory.pdf").read_bytes()
    writer = PdfWriter()
    writer.add_page(PdfReader(BytesIO(source)).pages[1])
    writer.pages[0][NameObject("/UserUnit")] = NumberObject(2)
    result = BytesIO()
    writer.write(result)
    raw = result.getvalue()
    text = parse_document("scaled.pdf", raw).units[0].text
    span = locate_span(raw, 1, text, 0, 10)
    assert span.status == "page_only"
    assert span.reason == "unsupported_page"


def test_highlight_falls_back_for_malformed_optional_page_box():
    from io import BytesIO

    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, NameObject, NumberObject, TextStringObject

    writer = PdfWriter()
    source = (SAMPLES / "course_memory.pdf").read_bytes()
    writer.add_page(PdfReader(BytesIO(source)).pages[1])
    writer.pages[0][NameObject("/TrimBox")] = ArrayObject([
        TextStringObject("invalid"), NumberObject(0), NumberObject(20), NumberObject(30),
    ])
    result = BytesIO()
    writer.write(result)
    raw = result.getvalue()
    text = parse_document("bad-box.pdf", raw).units[0].text
    assert locate_span(raw, 1, text, 0, 10).status == "page_only"


def test_known_supplemental_conflict_does_not_create_an_extra_citation():
    a = candidate("A", "登记期限是24小时。")
    b = candidate("B", "登记期限是48小时。")
    answer = draft_answer([a, b], ["A"])
    judgment = ClaimJudgment(number=1, status="contradicted", evidence=[
        EvidenceJudgment(evidence_id="A", status="supported", quote="24小时"),
        EvidenceJudgment(evidence_id="B", status="contradicted", quote="48小时"),
    ], conflict_ids=[])
    checked = validate_answer(answer, [a, b], [judgment], "annotate", "fixture:v1", coverage="complete")
    assert checked.verification.claims[0].status == "contradicted"
    assert [citation.evidence_id for citation in checked.citations] == ["A"]
    assert [item.evidence_id for item in checked.verification.conflict_evidence] == ["B"]
