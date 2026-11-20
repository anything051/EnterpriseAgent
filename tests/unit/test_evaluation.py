from uuid import uuid4

import pytest

from enterprise_rag.evaluation.schema import Question, Record, Span, load_dataset
from enterprise_rag.evaluation.scoring import aggregate, covers, score


def span(start, end, *, unit="p1"):
    return Span(document="os", unit=unit, start=start, end=end)


def record(group, question, status, **fields):
    return Record(group=group, question=question, status=status, latency_ms=1, **fields)


def test_union_covers_complete_span_but_never_joins_across_units_or_gaps():
    assert covers(span(0, 6), [span(0, 3), span(3, 6)])
    assert not covers(span(0, 6), [span(0, 3), span(4, 6)])
    assert not covers(span(0, 6), [span(0, 3), span(3, 6, unit="p2")])


def test_macro_recall_uses_per_fact_alternative_bundles():
    question = Question(id="one", question="两条事实？", gold_answer="甲，乙。", tags=["fixture"], answerable=True,
                        facts=[
                            {"id":"a","patterns":["甲"],"bundles":[[span(0,6)], [span(1,3,unit="p2")]]},
                            {"id":"b","patterns":["乙"],"bundles":[[span(7,10),span(0,4,unit="p3")]]},
                        ])
    ranked = [{"id":str(i), "span":value, "content":"test"} for i,value in enumerate([
        span(0,3),span(3,6),span(7,10),span(1,2,unit="p3"),span(3,4,unit="p3"),span(0,4,unit="p3"),
    ])]
    result = score(record("A", question, "error", ranked=ranked, error_code="model_error"))
    assert result["recall"] == {"5":0.5, "10":1.0}
    assert result["answer_correct"] is False


def test_scoring_distinguishes_error_refusal_and_zero_denominator():
    q = Question(id="no", question="未知兼容性？", gold_answer="", tags=["unknown"], answerable=False,
                 expected_refusal="no_answer")
    rows = [record("A", q, "error", error_code="chat_timeout"),
            record("B", q, "insufficient_evidence"),
            record("C", q, "insufficient_evidence"),
            record("D", q, "insufficient_evidence")]
    report = aggregate(rows)
    assert report["groups"]["A"]["correct_refusal"]["value"] == 0
    assert report["groups"]["A"]["answer_accuracy"]["value"] is None
    assert report["groups"]["B"]["correct_refusal"]["value"] == 1
    assert report["groups"]["A"]["latency_ms"]["success"]["p95"] is None
    assert report["groups"]["A"]["timeouts"] == 1
    assert report["pairs"]["D-C"]["count"] == 1
    with pytest.raises(ValueError, match="duplicate"):
        aggregate(rows + [rows[0]])


def test_gold_fixture_has_all_splits_and_span_offsets_are_verified():
    dataset = load_dataset("data/course_rag/evaluation/v0.3.0")
    assert len(dataset.questions) == 10
    assert {item.answerable for item in dataset.questions} == {True, False}
    assert len(dataset.documents) >= 4
