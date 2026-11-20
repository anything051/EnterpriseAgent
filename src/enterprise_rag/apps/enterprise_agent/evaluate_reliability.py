"""Pair E1 and E2, then ablate E2 retries on fixed inventory failures."""

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
from importlib.metadata import version
import json
from math import ceil
from pathlib import Path
import platform
from statistics import median
from time import perf_counter
from uuid import uuid4

from fastapi.testclient import TestClient

from enterprise_rag.config import Settings
from enterprise_rag.evaluation.artifacts import RUN_ID, write_manifest

from .agents.sessions import DATA_PROMPT, KNOWLEDGE_PROMPT, RETRY_DELAY, SHARED_PROMPT, TRANSIENT
from .agents.state_store import STATE_VERSION
from .agents.workflow import MAX_CALLS, MAX_MODEL_CALLS, SYSTEM, WALL_BUDGET_SECONDS
from .api import EnterpriseSettings, create_enterprise_app
from .evaluate import FILES, ROOT, _git, _hash, _provider_digest, _score, _write
from .tools import InventoryClient, ToolFailure

FAULTS = Path("data/enterprise_agent/evaluation/v0.6.0/faults.json")
GROUPS = ("E1", "E2", "E2-no-retry")


class FaultInventory:
    """Inject at the inventory adapter boundary. Successful reads use the real local HTTP fixture."""

    def __init__(self, delegate: InventoryClient):
        self.delegate = delegate
        self.reset(None)

    def reset(self, fault: str | None) -> None:
        self.fault, self.calls, self.injections = fault, 0, 0

    def lookup(self, sku: str):
        self.calls += 1
        if self.fault == "inventory_timeout_once" and self.calls == 1:
            self.injections += 1
            raise ToolFailure("inventory_timeout")
        if self.fault == "inventory_invalid_always":
            self.injections += 1
            raise ToolFailure("inventory_invalid")
        return self.delegate.lookup(sku)


def _source_proofs(client, auth: dict, result: dict | None) -> dict:
    checked = {}
    for item in result["evidence"] if result else []:
        if item["source_type"] != "document":
            continue
        doc = item["document"]
        loc = doc["locator"]
        route = f"/agent/sources/{doc['source_id']}/{loc['version_id']}/{loc['unit_id']}"
        source = client.get(route, headers=auth)
        if source.is_success:
            checked[item["evidence_id"]] = source.json()
    return checked


def _tally(rows: list[dict]) -> dict:
    return {"passed": sum(row["score"]["passed"] for row in rows), "samples": len(rows)}


def _permanent_safe(row: dict) -> bool:
    report = row["report"] or {}
    evidence = report.get("evidence", [])
    sections = report.get("sections", [])
    return (
        row["score"]["status"] == "partial" and row["score"]["supported_sources"] == ["document", "sql"]
        and sorted(item["source_type"] for item in evidence) == ["document", "sql"]
        and len(sections) == 2
        and {part["evidence_id"] for part in sections} == {item["evidence_id"] for item in evidence}
        and row["injections"] > 0 and report["missing_tools"] == ["inventory_status"]
    )


def summarize(directory: Path) -> dict:
    config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (directory / "samples.jsonl").read_text(encoding="utf-8").splitlines() if line]
    expected = {(group, identifier) for group, ids in config["case_ids"].items() for identifier in ids}
    got = {(row["group"], row["case"]["id"]) for row in rows}
    if expected != got or len(rows) != len(expected):
        raise ValueError("Incomplete or duplicated evaluation rows")
    limits = config["budgets"]
    for row in rows:
        row["score"] = _score(row)
        score = row["score"]
        report = row["report"]
        budget_ok = (report is not None and report["model_calls"] <= limits["model_calls"]
                     and len([entry for entry in report["audit"] if entry["tool"] != "authorize_task"]) <= limits["tool_calls"]
                     and report["elapsed_ms"] <= limits["wall_seconds"] * 1000)
        row["budget_ok"] = budget_ok
        score["passed"] = score["passed"] and budget_ok
    output = {}
    notes = ["# 未通过与故障样本", ""]
    lines = [
        "# EnterpriseAgent E1/E2 可靠性开发集", "",
        f"运行 `{config['run_id']}`，代码 `{config['git_sha']}`。", "",
        "| 组别 | 全部业务任务 | 常规任务 | 瞬时故障恢复 | 永久故障安全保留 | 权限阻断 | P50 / P95 (ms) | 输入／输出 Token |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for group in GROUPS:
        selected = [row for row in rows if row["group"] == group]
        positive = [row for row in selected if not row["case"].get("denied")]
        ordinary = [row for row in positive if not row["case"].get("fault")]
        transient = [row for row in positive if row["case"].get("fault") == "inventory_timeout_once"]
        injected = [row for row in transient if row["injections"] == 1]
        permanent = [row for row in positive if row["case"].get("fault") == "inventory_invalid_always"]
        latency = sorted(row["response_ms"] for row in positive)
        usage = {}
        for key in ("input_tokens", "output_tokens"):
            known = [row["score"][key] for row in positive if row["score"][key] is not None]
            usage[key] = {"total": sum(known) if known else None, "observed": len(known), "samples": len(positive)}
        item = {
            "task_success": _tally(positive), "ordinary": _tally(ordinary),
            "access_control": _tally([row for row in selected if row["case"].get("denied")]),
            "transient_recovery": {**_tally(injected), "assigned": len(transient)},
            "permanent_safe_partial": {"passed": sum(_permanent_safe(row) and row["budget_ok"] for row in permanent),
                                       "samples": len(permanent)},
            "budget_violations": sum(not row["budget_ok"] for row in selected),
            "p50_ms": round(median(latency), 2) if latency else None,
            "p95_ms": latency[ceil(0.95 * len(latency)) - 1] if latency else None,
            "tokens": usage, "inventory_attempts": sum(row["inventory_attempts"] for row in positive),
            "explicit_retries": sum((row["report"] or {}).get("retries", 0) for row in positive),
            "model_calls": sum((row["report"] or {}).get("model_calls", 0) for row in positive),
        }
        output[group] = item
        columns = [f"{item[key]['passed']}/{item[key]['samples']}" if item[key]["samples"] else "—"
                   for key in ("task_success", "ordinary", "transient_recovery", "permanent_safe_partial", "access_control")]
        lines.append("| " + " | ".join([
            group, *columns, f"{item['p50_ms']} / {item['p95_ms']}",
            f"{usage['input_tokens']['total']} / {usage['output_tokens']['total']}",
        ]) + " |")
        for row in selected:
            if not row["score"]["passed"] or row["case"].get("fault"):
                score = row["score"]
                notes.append(f"- {group} / {row['case']['id']}，{score['status']}，{score['stop_reason']}，"
                             f"库存尝试 {row['inventory_attempts']}，注入 {row['injections']}，"
                             f"缺少来源 {', '.join(score['missing_sources']) or '无'}。")
    lines.extend(["", "E1 和 E2 使用相同的模型、业务快照和任务。E2-no-retry 仅运行两条故障样本。"
                  "故障在库存适配器处注入。永久故障检查能否保留正确的销售与制度依据。", "",
                  "延迟为任务请求端到端用时，排除建线程和离线原文回查，包含故障与未完成样本。"
                  "Token 仅合计供应商有记录的请求。结果用于开发验收，样本量不足以推断总体改善。", ""])
    _write(directory / "metrics.json", output)
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")
    (directory / "failures.md").write_text("\n".join(notes) + "\n", encoding="utf-8")
    return output


def run(run_id: str, output: Path) -> Path:
    if RUN_ID.fullmatch(run_id) is None:
        raise ValueError("Invalid run ID")
    original = json.loads((ROOT / "cases.json").read_text(encoding="utf-8"))
    faults = json.loads(FAULTS.read_text(encoding="utf-8"))
    originals = {case["id"]: case for case in original["cases"]}
    cases = original["cases"] + [{**originals[fault["parent"]], **fault} for fault in faults["cases"]]

    environment = EnterpriseSettings()
    analyst, viewer = environment.analyst_token.get_secret_value(), environment.viewer_token.get_secret_value()
    db = {"database_url": environment.rag_database_url} if environment.rag_database_url else {}
    settings = Settings(app_id=f"reliability-eval-{uuid4().hex}", collection_tokens={"operator": analyst}, **db)
    auth = {"analyst": {"Authorization": f"Bearer {analyst}"}, "viewer": {"Authorization": f"Bearer {viewer}"}}
    destination = output / run_id
    destination.mkdir(parents=True, exist_ok=False)
    case_ids = {group: [case["id"] for case in cases if group != "E2-no-retry" or case.get("fault")] for group in GROUPS}
    _write(destination / "config.json", {
        "run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(), "git_sha": _git("rev-parse", "HEAD"),
        "dirty_tracked": bool(_git("status", "--porcelain", "-uno")), "case_ids": case_ids, "app_id": settings.app_id,
        "dataset": faults["version"], "snapshot": original["snapshot"], "timezone": original["timezone"],
        "files": {str(path): _hash(path.read_bytes()) for path in (*FILES, FAULTS)},
        "python": platform.python_version(), "platform": platform.platform(), "langgraph": version("langgraph"),
        "checkpointer": version("langgraph-checkpoint-postgres"), "state_version": STATE_VERSION,
        "chat": {"provider": settings.chat_provider, "model": settings.chat_model, "digest": _provider_digest(settings)},
        "embedding": {"model": settings.embedding_model, "revision": settings.embedding_revision},
        "retrieval": {"mode": settings.retrieval_mode, "rerank_configured": settings.reranker_dir is not None},
        "budgets": {"model_calls": MAX_MODEL_CALLS, "tool_calls": MAX_CALLS, "wall_seconds": WALL_BUDGET_SECONDS},
        "retry": {"codes": sorted(TRANSIENT), "delay_sec": RETRY_DELAY, "maximum": 1},
        "prompt_hashes": [_hash(prompt) for prompt in (SYSTEM, DATA_PROMPT, KNOWLEDGE_PROMPT, SHARED_PROMPT)],
        "execution_order": "Sequential; alternate group order by sample index. One thread per E2 case.",
    })

    with ExitStack() as stack:
        inventories = {group: FaultInventory(InventoryClient.from_url(environment.inventory_url)) for group in GROUPS}
        clients = {group: stack.enter_context(TestClient(create_enterprise_app(
            settings, analyst_token=analyst, viewer_token=viewer, enable_sessions=group != "E1",
            session_retry=group != "E2-no-retry", inventory_client=inventories[group],
        ))) for group in GROUPS}
        seed = clients["E1"].post("/agent/demo/policy", headers=auth["analyst"])
        seed.raise_for_status()
        stream = stack.enter_context((destination / "samples.jsonl").open("w", encoding="utf-8"))
        for index, case in enumerate(cases):
            groups = GROUPS if index % 2 == 0 else reversed(GROUPS)
            for group in groups:
                if case["id"] not in case_ids[group]:
                    continue
                client, headers = clients[group], auth[case["role"]]
                inventory = inventories[group]
                inventory.reset(case.get("fault"))
                route = "/agent/tasks"
                if group != "E1":
                    thread = client.post("/agent/threads", headers=headers)
                    thread.raise_for_status()
                    route = f"/agent/threads/{thread.json()['thread_id']}/runs/{uuid4()}"
                start = perf_counter()
                request = client.post if group == "E1" else client.put
                response = request(route, headers=headers, json=case["task"])
                elapsed = round((perf_counter() - start) * 1000, 2)
                result = response.json() if response.is_success else None
                if result is not None and group != "E1":
                    result = result["report"]
                record = {
                    "group": group, "case": case, "report": result, "http_status": response.status_code,
                    "inventory_attempts": inventory.calls, "injections": inventory.injections, "response_ms": elapsed,
                    "checked_sources": _source_proofs(client, headers, result),
                }
                stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                stream.flush()
                score = _score(record)
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
