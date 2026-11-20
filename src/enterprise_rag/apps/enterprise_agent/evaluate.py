"""Run and re-score a paired EnterpriseAgent E0/E1 development set."""

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from importlib.metadata import version
import json
from math import ceil
from pathlib import Path
import platform
from statistics import median
import subprocess
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import psycopg

from enterprise_rag.config import Settings
from enterprise_rag.evaluation.artifacts import RUN_ID, write_manifest

from .agents.planner import MAX_OUTPUT_TOKENS
from .agents.workflow import MAX_CALLS, MAX_MODEL_CALLS, SYSTEM, WALL_BUDGET_SECONDS
from .api import EnterpriseSettings, create_enterprise_app
from .contracts import APIEvidence, AgentReport, SQLEvidence, TaskInput
from .tools import POLICY_CANDIDATES, POLICY_QUERY, content_digest
from .tools.inventory import DEFAULT_FIXTURE

ROOT = Path("data/enterprise_agent/evaluation/v0.5.0")
FILES = (Path("uv.lock"), Path("migrations/enterprise_agent/005_business_snapshot.sql"),
         DEFAULT_FIXTURE, Path("data/samples/enterprise_inventory.md"), ROOT / "cases.json")
GROUPS = ("E0", "E1")


def _hash(data: str | bytes) -> str:
    return sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def _write(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def _git(*args: str) -> str | None:
    result = subprocess.run(["git", *args], text=True, capture_output=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def _provider_digest(settings: Settings) -> str | None:
    if settings.chat_provider != "ollama":
        return None
    headers = {"Authorization": f"Bearer {settings.chat_api_key.get_secret_value()}"} if settings.chat_api_key else {}
    try:
        with httpx.Client(timeout=3, trust_env=False) as client:
            reply = client.get(settings.model_url("chat") + "/api/tags", headers=headers)
            reply.raise_for_status()
        name = settings.chat_model
        tag = name if ":" in name.rsplit("/", 1)[-1] else f"{name}:latest"
        return next((row["digest"] for row in reply.json()["models"] if row["name"] == tag), None)
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return None


def _score(row: dict) -> dict:
    case = row["case"]
    report = AgentReport.model_validate(row["report"]) if row["report"] is not None else None
    required = set(case["expected"])
    supported = set()
    if report is not None:
        for item in report.evidence:
            expected = case["expected"].get(item.source_type)
            text = next((section.text for section in report.sections if section.evidence_id == item.evidence_id), "")
            if isinstance(item, SQLEvidence):
                params = item.parameters.model_dump()
                target = TaskInput.model_validate(case["task"]).model_dump()
                valid = (params == {key: target[key] for key in params}
                         and item.data.model_dump() == expected and content_digest(item.data) == item.content_hash)
                if valid:
                    amount = Decimal(expected["revenue_cents"]) / 100
                    valid = all(fragment in text for fragment in (
                        f"{target['day']} {target['region']}", f"{expected['order_count']} 笔订单",
                        f"{expected['units']} 件", f"¥{amount:,.2f}",
                    ))
            elif isinstance(item, APIEvidence):
                valid = (item.endpoint == f"/inventory/{item.data.sku}"
                         and item.data.model_dump() == expected and content_digest(item.data) == item.content_hash)
                if valid:
                    available = max(expected["on_hand"] - expected["reserved"], 0)
                    valid = all(fragment in text for fragment in (
                        expected["sku"], f"在库 {expected['on_hand']} 件", f"预留 {expected['reserved']} 件",
                        f"可用 {available} 件", f"安全库存 {expected['safety_stock']} 件",
                    ))
            else:
                doc = item.document
                loc = doc.locator
                source = row["checked_sources"].get(item.evidence_id)
                valid = (source is not None and isinstance(expected, str) and expected in doc.content
                         and source["source_text"][loc.start:loc.end] == doc.content
                         and source["version_id"] == str(loc.version_id) and source["unit_id"] == str(loc.unit_id)
                         and source["document_id"] == str(doc.source_id) and _hash(doc.content) == doc.content_hash
                         and text == doc.content)
            if valid and any(step.status == "ok" and step.evidence_id == item.evidence_id for step in report.audit):
                supported.add(item.source_type)
    permission = bool(report and report.status == "denied" and report.model_calls == 0
                      and not report.evidence and not report.sections and not report.dropped_calls
                      and [step.code for step in report.audit] == ["permission_denied"])
    complete = bool(report and report.status == "completed" and not report.missing_tools
                    and not report.dropped_calls and len(report.sections) == len(required)
                    and len(report.evidence) == len(required) and supported == required)
    scored = {
        "passed": permission if case.get("denied") else complete,
        "supported_sources": sorted(supported),
        "missing_sources": sorted(required - supported),
    }
    if report is not None:
        scored.update(status=report.status, stop_reason=report.stop_reason, elapsed_ms=report.elapsed_ms,
                      input_tokens=report.input_tokens, output_tokens=report.output_tokens, model_calls=report.model_calls,
                      tool_proposals=len([step for step in report.audit if step.tool != "authorize_task"]) + report.dropped_calls,
                      rejected_proposals=len([step for step in report.audit
                                              if step.status == "rejected" and step.tool != "authorize_task"]))
    else:
        scored.update(status="http_error", stop_reason=f"http_{row['http_status']}", elapsed_ms=None,
                      input_tokens=None, output_tokens=None, model_calls=None, tool_proposals=0, rejected_proposals=0)
    return scored


def summarize(directory: Path) -> dict:
    config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (directory / "samples.jsonl").read_text(encoding="utf-8").splitlines() if line]
    expected = {(group, name) for group in GROUPS for name in config["case_ids"]}
    seen = {(row["group"], row["case"]["id"]) for row in rows}
    if seen != expected or len(rows) != len(expected):
        raise ValueError("Incomplete or duplicated paired evaluation records")
    metrics = {}
    failures = ["# 未通过样本", ""]
    for group in GROUPS:
        scores = [(row["case"], _score(row)) for row in rows if row["group"] == group]
        tasks = [score for case, score in scores if not case.get("denied")]
        access = [score for case, score in scores if case.get("denied")]
        latencies = sorted(score["elapsed_ms"] for score in tasks if score["elapsed_ms"] is not None)
        usage = {}
        for field in ("input_tokens", "output_tokens"):
            observed = [score[field] for score in tasks if score[field] is not None]
            usage[field] = {"total": sum(observed) if observed else None, "observed": len(observed), "samples": len(tasks)}
        metrics[group] = {
            "task_success": {"passed": sum(score["passed"] for score in tasks), "samples": len(tasks)},
            "access_control": {"passed": sum(score["passed"] for score in access), "samples": len(access)},
            "source_coverage": {kind: {
                "supported": sum(kind in score["supported_sources"] for case, score in scores if kind in case["expected"]),
                "required": sum(kind in case["expected"] for case, _ in scores),
            } for kind in ("sql", "api", "document")},
            "tool_proposals": sum(score["tool_proposals"] for score in tasks),
            "rejected_proposals": sum(score["rejected_proposals"] for score in tasks),
            "p50_ms": round(median(latencies), 2) if latencies else None,
            "p95_ms": latencies[ceil(len(latencies) * 0.95) - 1] if latencies else None,
            "tokens": usage,
        }
        for case, score in scores:
            if not score["passed"]:
                missing = ", ".join(score["missing_sources"]) or "无"
                failures.append(f"- {group} / {case['id']}，状态 {score['status']}，停止原因 {score['stop_reason']}，缺少依据 {missing}。")
    if len(failures) == 2:
        failures.append("所有样本符合其预设验收条件。")

    report = [
        "# EnterpriseAgent E0/E1 开发集", "",
        f"运行 `{config['run_id']}`，数据集 `{config['dataset_version']}`，代码 `{config['git_sha']}`。", "",
        "| 组别 | 任务成功 | 权限阻断 | SQL 支持 | API 支持 | 制度支持 | 延迟 P50 / P95 (ms) | 有记录的输入／输出 Token |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for group, item in metrics.items():
        task, access = item["task_success"], item["access_control"]
        coverage = [f"{entry['supported']}/{entry['required']}" for entry in item["source_coverage"].values()]
        cells = [group, f"{task['passed']}/{task['samples']}", f"{access['passed']}/{access['samples']}", *coverage,
                 f"{item['p50_ms']} / {item['p95_ms']}",
                 f"{item['tokens']['input_tokens']['total']} / {item['tokens']['output_tokens']['total']}"]
        report.append("| " + " | ".join(cells) + " |")
    report.extend(["", "E0 只开放 SQL/API 工具，E1 同时开放授权制度检索。分母按预登记任务固定。权限任务单列，"
                   "拒答不产生模型用量。Token 只合计供应商有记录的样本，覆盖数见 metrics.json。"
                   "延迟包含模型与工具，P95 采用最近秩。此为小型功能开发集。", ""])
    _write(directory / "metrics.json", metrics)
    (directory / "report.md").write_text("\n".join(report), encoding="utf-8")
    (directory / "failures.md").write_text("\n".join(failures) + "\n", encoding="utf-8")
    return metrics


def run(run_id: str, root: Path) -> Path:
    if RUN_ID.fullmatch(run_id) is None:
        raise ValueError("Invalid run ID")
    dataset = json.loads((ROOT / "cases.json").read_text(encoding="utf-8"))
    app = EnterpriseSettings()
    analyst = app.analyst_token.get_secret_value()
    viewer = app.viewer_token.get_secret_value()
    database = {"database_url": app.rag_database_url} if app.rag_database_url else {}
    settings = Settings(app_id=f"enterprise-eval-{uuid4().hex}", collection_tokens={"operator": analyst}, **database)
    roles = {"analyst": {"Authorization": f"Bearer {analyst}"}, "viewer": {"Authorization": f"Bearer {viewer}"}}
    with psycopg.connect(app.reader_url.get_secret_value(), connect_timeout=3) as conn:
        postgres = conn.execute("SHOW server_version").fetchone()[0]
    config = {
        "run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(), "schema_version": 1,
        "dataset_version": dataset["version"], "split": dataset["split"], "case_ids": [case["id"] for case in dataset["cases"]],
        "snapshot": dataset["snapshot"], "timezone": dataset["timezone"], "git_sha": _git("rev-parse", "HEAD"),
        "dirty_tracked": bool(_git("status", "--porcelain", "-uno")), "app_id": settings.app_id,
        "files": {str(path): _hash(path.read_bytes()) for path in FILES},
        "python": platform.python_version(), "platform": platform.platform(), "postgres": postgres,
        "langgraph": version("langgraph"), "chat": {"provider": settings.chat_provider, "model": settings.chat_model,
                                                   "digest": _provider_digest(settings), "temperature": 0},
        "embedding": {"model": settings.embedding_model, "revision": settings.embedding_revision,
                      "profile": settings.index_profile().id},
        "retrieval": {"mode": settings.retrieval_mode, "rerank_configured": settings.reranker_dir is not None,
                      "policy_query_sha256": _hash(POLICY_QUERY), "policy_candidates": POLICY_CANDIDATES},
        "prompt_sha256": _hash(SYSTEM), "max_tool_calls": MAX_CALLS, "max_model_calls": MAX_MODEL_CALLS,
        "wall_budget_s": WALL_BUDGET_SECONDS, "max_output_tokens_per_call": MAX_OUTPUT_TOKENS,
        "order": "Alternate E0/E1 order per case in dataset order. Single sequential worker.",
    }

    destination = root / run_id
    destination.mkdir(parents=True, exist_ok=False)
    _write(destination / "config.json", config)
    with ExitStack() as stack:
        clients = {group: stack.enter_context(TestClient(create_enterprise_app(
            settings, analyst_token=analyst, viewer_token=viewer, include_policy=group == "E1",
        ))) for group in GROUPS}
        seed = clients["E1"].post("/agent/demo/policy", headers=roles["analyst"])
        seed.raise_for_status()
        stream = stack.enter_context((destination / "samples.jsonl").open("w", encoding="utf-8"))
        for index, case in enumerate(dataset["cases"]):
            for group in (GROUPS if index % 2 == 0 else reversed(GROUPS)):
                client, auth = clients[group], roles[case["role"]]
                response = client.post("/agent/tasks", json=case["task"], headers=auth)
                result = response.json() if response.is_success else None
                checked = {}
                if result:
                    for item in result["evidence"]:
                        if item["source_type"] == "document":
                            doc = item["document"]
                            loc = doc["locator"]
                            route = f"/agent/sources/{doc['source_id']}/{loc['version_id']}/{loc['unit_id']}"
                            source = client.get(route, headers=auth)
                            if source.is_success:
                                checked[item["evidence_id"]] = source.json()
                row = {"group": group, "case": case, "http_status": response.status_code,
                       "report": result, "checked_sources": checked}
                stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                stream.flush()
                score = _score(row)
                print(group, case["id"], score["status"], score["passed"], flush=True)
    summarize(destination)
    return write_manifest(destination)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    live = commands.add_parser("run")
    live.add_argument("--run-id", required=True)
    live.add_argument("--output", type=Path, default=Path("runs/enterprise"))
    offline = commands.add_parser("summarize")
    offline.add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "run":
        print(run(args.run_id, args.output))
    else:
        summarize(args.directory)


if __name__ == "__main__":
    main()
