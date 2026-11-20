"""Run and recalculate the CourseRAG development experiment."""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import platform
import subprocess
from uuid import uuid4

import httpx
import psycopg

from enterprise_rag.adapters.models import COVERAGE_PROMPT, REVIEW_PROMPT, SYSTEM_PROMPT, create_models
from enterprise_rag.adapters.postgres import PostgresStore
from enterprise_rag.adapters.reranker import ARTIFACTS, OnnxReranker
from enterprise_rag.config import Settings
from enterprise_rag.core.documents import Scope
from enterprise_rag.core.retrieval import BRANCH_LIMIT, LEXICAL_CHUNK_LIMIT

from .artifacts import RUN_ID, write_manifest
from .runner import FINAL_K, RANK_K, run_cases
from .schema import Record, load_dataset
from .scoring import GROUPS, aggregate, score

DEFAULT_DATASET = Path("data/course_rag/evaluation/v0.3.0")


def _write_json(path: Path, content: dict) -> None:
    path.write_text(json.dumps(content, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def _git(*args: str) -> str | None:
    proc = subprocess.run(["git", *args], text=True, capture_output=True, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _chat_artifact(settings: Settings) -> str | None:
    if settings.chat_provider != "ollama":
        return None
    headers = {"Authorization": f"Bearer {settings.chat_api_key.get_secret_value()}"} if settings.chat_api_key else {}
    try:
        with httpx.Client(timeout=5, trust_env=False) as client:
            reply = client.get(f"{settings.model_url('chat')}/api/tags", headers=headers)
            reply.raise_for_status()
        tag = settings.chat_model
        if ":" not in tag.rsplit("/", 1)[-1]:
            tag += ":latest"
        return next((item.get("digest") for item in reply.json()["models"] if item.get("name") == tag), None)
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return None


def _config(settings, dataset, questions, scope, seed, split, reranker, run_id) -> dict:
    with psycopg.connect(settings.database_url.get_secret_value(), connect_timeout=5) as conn:
        database = conn.execute("SELECT current_setting('server_version'), extversion "
                                "FROM pg_extension WHERE extname='vector'").fetchone()
        if database is None:
            raise ValueError("pgvector extension is not installed")
    lock = Path("uv.lock")
    profile = asdict(settings.index_profile())
    profile.pop("embedding_endpoint")
    return {
        "run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(), "schema_version": 1,
        "dataset": dataset.manifest.model_dump(mode="json"), "dataset_sha256": dataset.fingerprint,
        "split": split, "question_ids": sorted(question.id for question in questions),
        "seed": seed, "scope": scope.id, "groups": list(GROUPS),
        "git_sha": _git("rev-parse", "HEAD"), "dirty_tracked": bool(_git("status", "--porcelain", "-uno")),
        "lock_sha256": sha256(lock.read_bytes()).hexdigest() if lock.is_file() else None,
        "platform": platform.platform(), "architecture": platform.machine(), "python": platform.python_version(),
        "runtime": {name: version(name) for name in ("bm25s", "jieba", "onnxruntime", "pypdf", "pgvector")},
        "database": {"postgres": database[0], "pgvector": database[1]},
        "chat": {"provider": settings.chat_provider, "model": settings.chat_model,
                 "artifact_digest": _chat_artifact(settings), "timeout_s": settings.chat_timeout,
                 "ollama_temperature": 0 if settings.chat_provider == "ollama" else None},
        "embedding": {**profile, "index_profile_id": settings.index_profile().id},
        "prompts_sha256": {label: sha256(prompt.encode()).hexdigest() for label, prompt in
                           (("generate", SYSTEM_PROMPT), ("semantic", REVIEW_PROMPT), ("coverage", COVERAGE_PROMPT))},
        "rerank": {"model": reranker.name, "sha256": ARTIFACTS, "timeout_s": settings.reranker_timeout},
        "retrieval": {"generation_k": FINAL_K, "ranking_k": RANK_K, "branch_k": BRANCH_LIMIT,
                      "bm25_chunk_limit": LEXICAL_CHUNK_LIMIT, "rrf_constant": 60},
        "timing": "Sequential seeded question/branch order. B/C/D reuse work. Group time sums included phases.",
        "cost": "Provider token counts where available. Monetary and GPU/electricity costs were not measured.",
    }


def _display(ratio: dict) -> str:
    return "NA" if ratio["value"] is None else f'{ratio["value"]:.1%}'


def summarize(directory: Path) -> dict:
    config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    with (directory / "samples.jsonl").open(encoding="utf-8") as stream:
        rows = [Record.model_validate_json(line) for line in stream if line.strip()]
    expected = {(g, qid) for g in config["groups"] for qid in config["question_ids"]}
    seen = {(row.group, row.question.id) for row in rows}
    if expected != seen:
        raise ValueError(f"Incomplete sample file: {len(expected - seen)} missing, {len(seen - expected)} unexpected")
    report = aggregate(rows)
    _write_json(directory / "metrics.json", report)

    header = [
        "# CourseRAG A–D 开发集运行", "",
        f"运行 {config['run_id']}，数据集 {config['dataset']['version']}。",
        f"代码提交 {config['git_sha'] or 'unavailable'}。每组 {len(config['question_ids'])} 题。",
        "", "| 组 | Recall@5 | Recall@10 | 正确率 | 正确拒答 | 误拒答 | 引用支持 | 错误 | P50 / P95 毫秒 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, value in report["groups"].items():
        ms = value["latency_ms"]["all"]
        timings = "NA" if ms["p50"] is None else f"{ms['p50']:.0f} / {ms['p95']:.0f}"
        cells = [name, _display(value["recall"]["5"]), _display(value["recall"]["10"]),
                 _display(value["answer_accuracy"]), _display(value["correct_refusal"]),
                 _display(value["false_refusal"]), _display(value["citation_support"]),
                 str(value["errors"]), timings]
        header.append("| " + " | ".join(cells) + " |")

    header.extend(["", "正确率分母为可回答题。拒答率分母按可回答性分开。引用支持率只纳入可由 gold 规则判定的陈述，"
                   "未判定计数见 metrics.json。B、C、D 的延迟按实际测量阶段组合。这个小型开发集用于功能验收。"])
    (directory / "report.md").write_text("\n".join(header) + "\n", encoding="utf-8")

    failures = ["# 失败与拒答检查", "", "| 题目 | 组 | 预期 | 实际 | 结果依据 |",
                "| --- | --- | --- | --- | --- |"]
    for row in sorted(rows, key=lambda item: (item.question.id, item.group)):
        metrics = score(row)
        if metrics["answer_correct"] is False or metrics["correct_refusal"] is False or row.error_code:
            reason = row.error_code or (row.answer.refusal_reason if row.answer else None)
            reason = reason or f"未通过事实规则或预期拒答，未匹配陈述 {metrics['unmatched_claims']}"
            failures.append(f"| {row.question.id} | {row.group} | {row.question.expected_refusal or 'answer'} "
                            f"| {row.status} | {reason} |")
    if len(failures) == 4:
        failures.extend(["", "本轮没有命中上述失败条件。"])
    (directory / "failures.md").write_text("\n".join(failures) + "\n", encoding="utf-8")
    return report


def run(args) -> int:
    dataset = load_dataset(args.dataset)
    questions = [item for item in dataset.questions if item.split == args.split]
    if not questions:
        raise ValueError(f"No questions in {args.split} split")
    run_id = args.run_id or f"course-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    if RUN_ID.fullmatch(run_id) is None:
        raise ValueError("Invalid run ID")
    scope = Scope(f"eval-{uuid4().hex}", "course-v03")
    settings = Settings(app_id=scope.app_id, collection_tokens={scope.collection_id: "offline-evaluation-fixture-token"})
    if settings.reranker_dir is None:
        raise ValueError("Set RAG_RERANKER_DIR for the strict A–D comparison")
    embedding, chat = create_models(settings)
    store = PostgresStore(settings.database_url.get_secret_value())
    destination = args.output / run_id
    reranker = OnnxReranker(settings.reranker_dir, settings.reranker_timeout)
    try:
        config = _config(settings, dataset, questions, scope, args.seed, args.split, reranker, run_id)
        destination.mkdir(parents=True, exist_ok=False)
        _write_json(destination / "config.json", config)
        with (destination / "samples.jsonl").open("w", encoding="utf-8") as stream:
            for row in run_cases(dataset, store, embedding, chat, settings.index_profile(), reranker, scope,
                                 questions=questions, seed=args.seed):
                stream.write(row.model_dump_json() + "\n")
                stream.flush()
                print(f"{row.question.id} {row.group} {row.status}", flush=True)
        report = summarize(destination)
    finally:
        reranker.close()
    print(destination)
    return int(any(group["errors"] for group in report["groups"].values()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    experiment = commands.add_parser("run")
    experiment.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    experiment.add_argument("--output", type=Path, default=Path("runs"))
    experiment.add_argument("--run-id")
    experiment.add_argument("--split", choices=("development", "holdout"), default="development")
    experiment.add_argument("--seed", type=int, default=42)
    replay = commands.add_parser("summarize")
    replay.add_argument("directory", type=Path)
    archive = commands.add_parser("archive", help="Create the manifest required for CourseRAG export")
    archive.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "run":
            return run(args)
        if args.command == "archive":
            print(write_manifest(args.directory))
            return 0
        summarize(args.directory)
        return 0
    except (ValueError, OSError) as error:
        parser.exit(1, f"Evaluation failed: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
