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


def test_retrieval_failure_counts_as_a_miss_for_gold_labeled_questions():
    q = Question(id="yes", question="哪条事实？", gold_answer="甲。", tags=["fixture"], answerable=True,
                 facts=[{"id":"a","patterns":["甲"],"bundles":[[span(0,2)]]}])
    failed = record("A", q, "error", error_code="embedding_timeout")
    report = aggregate([failed])
    assert report["groups"]["A"]["recall"]["5"] == {"numerator":0.0,"denominator":1,"value":0.0}


def test_conditional_accuracy_counts_answering_negative_questions_as_wrong():
    positive = Question(id="yes", question="哪条事实？", gold_answer="甲。", tags=["fixture"], answerable=True,
                        facts=[{"id":"a","patterns":["甲"],"bundles":[[span(0,2)]]}])
    negative = Question(id="no", question="未知事实？", gold_answer="", tags=["fixture"], answerable=False,
                        expected_refusal="no_answer")
    answer = {"answer_id":str(uuid4()), "answer_status":"answered","answer":"甲。"}
    rows = [record("A", positive, "answered", answer=answer), record("A", negative, "answered", answer=answer)]
    assert aggregate(rows)["groups"]["A"]["answered_accuracy"]["value"] == 0.5


def test_narrow_quote_does_not_change_bound_citation_support():
    from hashlib import sha256

    content = "NW-170-X 对应助教演示的镜像端口"
    q = Question(id="yes",question="NW-170-X 用途？",gold_answer="镜像端口。",tags=["fixture"],answerable=True,
                 facts=[{"id":"a","patterns":["镜像端口"],"bundles":[[span(0,len(content))]]}])
    answer_id, claim_id, chunk_id = [str(uuid4()) for _ in range(3)]
    answer = {"answer_id":answer_id, "answer_status":"answered", "answer":"镜像端口。",
              "claims":[{"claim_id":claim_id,"text":"镜像端口。"}],
              "citations":[{"citation_id":str(uuid4()),"answer_id":answer_id,"claim_id":claim_id,"evidence_id":chunk_id,
                            "quote_span":{"start":len(content)-4,"end":len(content),"excerpt":"镜像端口"}}],
              "evidence":[{"evidence_id":chunk_id,"scope_id":"a:b","source_id":str(uuid4()),"content":content,
                  "content_hash":sha256(content.encode()).hexdigest(),"locator":{
                      "version_id":str(uuid4()),"unit_id":str(uuid4()),"parser_profile_id":"p1","paragraph_id":"p1",
                      "start":0,"end":len(content),"chunk_id":chunk_id}}]}
    row = record("C", q, "answered", answer=answer, ranked=[{"id":chunk_id,"span":span(0,len(content)),
                                                          "content":content}])
    result = score(row)["counts"]
    assert result["supported"] == 1
    assert result["quote_resolved"] == 1


def test_offline_summarize_rebuilds_identical_files(tmp_path):
    import json

    from enterprise_rag.evaluation.__main__ import summarize

    question = Question(id="no", question="未知兼容性？", gold_answer="", tags=["unknown"], answerable=False,
                        expected_refusal="no_answer")
    config = {"groups":["A","B","C","D"],"question_ids":["no"],"run_id":"unit-fixture",
              "dataset":{"version":"unit"},"git_sha":"fixture"}
    (tmp_path/"config.json").write_text(json.dumps(config))
    (tmp_path/"samples.jsonl").write_text("\n".join(
        record(group, question, "insufficient_evidence").model_dump_json() for group in config["groups"]
    ))
    summarize(tmp_path)
    files = [tmp_path/name for name in ("metrics.json","report.md","failures.md")]
    first = [path.read_bytes() for path in files]
    summarize(tmp_path)
    assert [path.read_bytes() for path in files] == first
