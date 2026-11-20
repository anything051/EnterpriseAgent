from enterprise_rag.core.documents import Scope
from enterprise_rag.core.generation import Draft, DraftClaim, GeneratedDraft
from enterprise_rag.core.validation import ClaimJudgment, EvidenceJudgment, JudgmentBatch
from enterprise_rag.evaluation.runner import run_cases
from enterprise_rag.evaluation.schema import load_dataset
from enterprise_rag.evaluation.scoring import aggregate


class CountingChat:
    version = "eval-fixture"

    def __init__(self):
        self.generation_calls = 0
        self.review_calls = 0

    def generate(self, question, candidates):
        self.generation_calls += 1
        first = candidates[0]
        return GeneratedDraft(Draft(claims=[DraftClaim(text=first.content, evidence_ids=[first.evidence_id])]), 12, 4)

    def review(self, question, answer, candidates):
        self.review_calls += 1
        evidence = [EvidenceJudgment(evidence_id=c.evidence_id, status="unsupported", quote="") for c in answer.citations]
        return JudgmentBatch(coverage="complete", claims=[
            ClaimJudgment(number=1, status="unsupported", evidence=evidence, conflict_ids=[]),
        ])


class RankingFixture:
    name = "constant-score"

    def score(self, question, passages):
        return [0.0] * len(passages)


def test_paired_pipeline_reuses_one_mixed_draft_and_review(service, settings):
    dataset = load_dataset("data/course_rag/evaluation/v0.3.0")
    chat = CountingChat()
    outputs = list(run_cases(dataset, service.store, service.embedding, chat, service.profile, RankingFixture(),
                             Scope(settings.app_id, "course"), questions=dataset.questions[:1], seed=13))
    assert chat.generation_calls == 2
    assert chat.review_calls == 1
    report = aggregate(outputs)
    by_group = {r.group:r for r in outputs}
    assert set(by_group) == {"A", "B", "C", "D"}
    assert by_group["A"].strategy == "vector"
    assert by_group["B"].ranked == by_group["C"].ranked == by_group["D"].ranked
    assert by_group["B"].answer.answer == by_group["C"].answer.answer
    assert by_group["D"].status == "insufficient_evidence"
    assert report["groups"]["B"]["usage"]["generation"]["input"]["sum"] == 12
