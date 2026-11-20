"""Deterministic CourseRAG metrics over saved answers and source coordinates."""

from collections import Counter
from hashlib import sha256
from math import floor
import re
from statistics import mean
from unicodedata import normalize

from .schema import Fact, Record, Span

GROUPS = ("A", "B", "C", "D")
KS = (5, 10)


def covers(target: Span, passages: list[Span]) -> bool:
    cursor = target.start
    matching = sorted((item.start, item.end) for item in passages
                      if (item.document, item.unit) == (target.document, target.unit))
    for start, end in matching:
        if start > cursor:
            break
        cursor = max(cursor, end)
        if cursor >= target.end:
            return True
    return False


def covers_fact(fact: Fact, spans: list[Span]) -> bool:
    return any(all(covers(target, spans) for target in bundle) for bundle in fact.bundles)


def _matches(fact: Fact, text: str) -> bool:
    return all(re.search(pattern, text) for pattern in fact.patterns) and not any(
        re.search(pattern, text) for pattern in fact.forbidden)


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", normalize("NFKC", text).casefold()).strip()


def _citations(record: Record) -> dict:
    counts: Counter = Counter()
    agreement: Counter = Counter()
    answer = record.answer
    if answer is None or record.status != "answered":
        return dict(counts=counts, agreement=agreement)
    ranked = {passage.id: passage for passage in record.ranked or []}
    evidence = {item.evidence_id: item for item in answer.evidence}
    for claim in answer.claims:
        counts["claims"] += 1
        matched_facts = [fact for fact in record.question.facts if _matches(fact, _normalized(claim.text))]
        cited = [item for item in answer.citations if item.claim_id == claim.claim_id]
        if not cited:
            continue
        counts["cited"] += 1
        if matched_facts:
            counts["assessed"] += 1
        spans = []
        for citation in cited:
            counts["citations"] += 1
            if matched_facts:
                counts["citation_assessed"] += 1
            passage = ranked.get(citation.evidence_id)
            bound = evidence.get(citation.evidence_id)
            if citation.quote_span is not None:
                counts["quotes"] += 1
            if (passage is None or bound is None or bound.content != passage.content
                    or not passage.matches_locator(bound)
                    or sha256(bound.content.encode()).hexdigest() != bound.content_hash
                    or (bound.locator.start, bound.locator.end) != (passage.span.start, passage.span.end)):
                continue
            resolved = passage.span
            if citation.quote_span is not None:
                quote = citation.quote_span
                start, end = quote.start - resolved.start, quote.end - resolved.start
                if 0 <= start < end <= len(passage.content) and passage.content[start:end] == quote.excerpt:
                    counts["quote_resolved"] += 1
            counts["resolved"] += 1
            spans.append(resolved)
            if matched_facts and any(covers_fact(fact, [resolved]) for fact in matched_facts):
                counts["citation_supported"] += 1
        supported = bool(matched_facts) and all(covers_fact(fact, spans) for fact in matched_facts)
        if supported:
            counts["supported"] += 1
        if matched_facts and answer.verification is not None:
            check = next((item for item in answer.verification.claims if item.claim_id == claim.claim_id), None)
            if check is not None:
                agreement[(("supported" if supported else "unsupported") + ":" + check.status)] += 1
    return dict(counts=counts, agreement=agreement)


def score(record: Record) -> dict:
    question = record.question
    recall, hit, all_evidence = {}, {}, {}
    accuracy = None
    if question.answerable:
        text = _normalized(record.answer.answer) if record.answer is not None else ""
        checks = {fact.id: _matches(fact, text) for fact in question.facts}
        extra_claims = (sum(not any(_matches(fact, _normalized(claim.text)) for fact in question.facts)
                            for claim in record.answer.claims) if record.answer else 0)
        accuracy = record.status == "answered" and all(checks.values()) and extra_claims == 0
        for k in KS:
            spans = [item.span for item in (record.ranked or [])[:k]]
            found = sum(covers_fact(fact, spans) for fact in question.facts)
            recall[str(k)] = found / len(question.facts)
            hit[str(k)] = found > 0
            all_evidence[str(k)] = found == len(question.facts)
    else:
        checks = None
        extra_claims = None
    refused = record.status == "insufficient_evidence"
    return {
        "recall": recall, "hit": hit, "all_evidence": all_evidence,
        "answer_correct": accuracy, "facts_correct": checks, "unmatched_claims": extra_claims,
        "correct_refusal": refused if not question.answerable else None,
        "false_refusal": refused if question.answerable else None,
        **_citations(record),
    }


def _ratio(n: int | float, d: int) -> dict:
    return {"numerator": n, "denominator": d, "value": n / d if d else None}


def _values(items) -> dict:
    values = [value for value in items if value is not None]
    return _ratio(sum(values), len(values))


def _latency(values: list[float]) -> dict:
    ordered = sorted(values)

    def quantile(p: float) -> float | None:
        if not ordered:
            return None
        position = p * (len(ordered) - 1)
        left = floor(position)
        return ordered[left] + (position - left) * (ordered[min(left + 1, len(ordered) - 1)] - ordered[left])

    return {"n": len(ordered), "p50": quantile(0.5), "p95": quantile(0.95)}


def _usage(rows: list[Record]) -> dict:
    usage = {}
    for phase in ("generation", "semantic", "coverage"):
        calls = [row.usage[phase] for row in rows if phase in row.usage]
        result = {}
        for kind in ("input", "output"):
            observed = [value for call in calls if (value := getattr(call, kind)) is not None]
            result[kind] = {"sum": sum(observed) if observed else None,
                            "observed": len(observed), "missing": len(calls) - len(observed)}
        usage[phase] = {"calls": len(calls), **result}
    usage["query_embedding_calls"] = sum(row.query_embedding_calls for row in rows)
    usage["query_embedding_tokens"] = None
    usage["rerank_pairs"] = sum(row.rerank_pairs for row in rows)
    usage["monetary_cost"] = None
    return usage


def aggregate(rows: list[Record]) -> dict:
    indexed = {}
    for row in rows:
        key = (row.group, row.question.id)
        if key in indexed:
            raise ValueError(f"duplicate sample result: {key}")
        indexed[key] = (row, score(row))

    groups = {}
    for group in GROUPS:
        pairs = [(row, metrics) for row, metrics in indexed.values() if row.group == group]
        records = [row for row, _ in pairs]
        scores = [metrics for _, metrics in pairs]
        counts: Counter = Counter()
        agreement: Counter = Counter()
        for metrics in scores:
            counts.update(metrics["counts"])
            agreement.update(metrics["agreement"])
        answered = sum(row.status == "answered" for row in records)
        errors = sum(row.status == "error" for row in records)
        groups[group] = {
            "samples": len(records), "errors": errors,
            "timeouts": sum((row.error_code or "").endswith("_timeout") for row in records),
            "strategies": dict(sorted(Counter(row.strategy for row in records if row.strategy).items())),
            "recall": {str(k): _values(item["recall"].get(str(k)) for item in scores) for k in KS},
            "hit": {str(k): _values(item["hit"].get(str(k)) for item in scores) for k in KS},
            "all_evidence": {str(k): _values(item["all_evidence"].get(str(k)) for item in scores) for k in KS},
            "answer_accuracy": _values(item["answer_correct"] for item in scores),
            "correct_refusal": _values(item["correct_refusal"] for item in scores),
            "false_refusal": _values(item["false_refusal"] for item in scores),
            "answer_coverage": _ratio(answered, len(records)),
            "answered_accuracy": _values(bool(metrics["answer_correct"]) for row, metrics in pairs
                                         if row.status == "answered"),
            "missing_rankings": sum(row.ranked is None for row in records),
            "citation_support": _ratio(counts["supported"], counts["assessed"]),
            "citation_coverage": _ratio(counts["supported"], counts["claims"]),
            "citation_precision": _ratio(counts["citation_supported"], counts["citation_assessed"]),
            "source_validity": _ratio(counts["resolved"], counts["citations"]),
            "quote_validity": _ratio(counts["quote_resolved"], counts["quotes"]),
            "unjudged_cited_claims": counts["cited"] - counts["assessed"],
            "validator_agreement": dict(sorted(agreement.items())),
            "latency_ms": {
                "all": _latency([row.latency_ms for row in records]),
                "success": _latency([row.latency_ms for row in records if row.status != "error"]),
            },
            "usage": _usage(records),
        }

    deltas = {}
    for later, earlier in (("B", "A"), ("C", "B"), ("D", "C")):
        keys = sorted({qid for group, qid in indexed if group == earlier}
                      & {qid for group, qid in indexed if group == later})
        values = [(indexed[later, key], indexed[earlier, key]) for key in keys]
        deltas[f"{later}-{earlier}"] = {
            "count": len(keys),
            "answer_accuracy": _values(int(a[1]["answer_correct"]) - int(b[1]["answer_correct"])
                                       for a, b in values
                                       if a[1]["answer_correct"] is not None and b[1]["answer_correct"] is not None),
            "correct_refusal": _values(int(a[1]["correct_refusal"]) - int(b[1]["correct_refusal"])
                                       for a, b in values
                                       if a[1]["correct_refusal"] is not None and b[1]["correct_refusal"] is not None),
            "latency_ms_mean": mean(a[0].latency_ms - b[0].latency_ms for a, b in values) if values else None,
        }
    return {"scoring_version": "course-v0.3.1", "groups": groups, "pairs": deltas}
