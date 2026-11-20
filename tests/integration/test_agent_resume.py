"""PostgreSQL-backed restart, isolation and retry regressions."""

import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
from time import sleep
from uuid import UUID, uuid4

from langchain_core.messages import AIMessage
from fastapi.testclient import TestClient
import pytest

pytest.importorskip("langgraph.checkpoint.postgres")

from enterprise_rag.apps.enterprise_agent.agents.sessions import SessionService
from enterprise_rag.apps.enterprise_agent.agents.state_store import SessionConflict, SessionStore
from enterprise_rag.apps.enterprise_agent.api import create_enterprise_app
from enterprise_rag.apps.enterprise_agent.contracts import APIEvidence, InventoryData, SalesData, SQLEvidence, TaskInput, sales_summary
from enterprise_rag.apps.enterprise_agent.tools import ToolFailure, content_digest
from enterprise_rag.core.documents import DocumentNotFound

OWNER = "fixture-owner"
TASK = TaskInput(kind="overview", question="汇总销售与库存。")


def planner(_messages, tools):
    args = {
        "sales_summary": {"day": TASK.day, "region": TASK.region},
        "inventory_status": {"sku": TASK.sku},
        "warehouse_policy": {"topic": "discrepancy"},
    }
    return AIMessage(content="", tool_calls=[
        {"id": "test-" + tool.__name__, "name": tool.__name__, "args": args[tool.__name__]}
        for tool in tools
    ])


class ProbeTools:
    def __init__(self, fault=None):
        self.calls = []
        self.fault = fault

    def execute(self, params):
        name = type(params).__name__
        self.calls.append(name)
        if isinstance(params, sales_summary):
            data = SalesData(order_count=2, units=12, revenue_cents=145000)
            return SQLEvidence(evidence_id="sale-fixed-proof", parameters=params,
                               data=data, content_hash=content_digest(data))
        if name == "inventory_status":
            if self.fault and (self.fault != "transient" or self.calls.count(name) == 1):
                raise ToolFailure("inventory_invalid" if self.fault == "permanent" else "inventory_timeout")
            data = InventoryData(sku=params.sku, snapshot="qh-2026-06-v1", on_hand=18, reserved=5, safety_stock=10)
            return APIEvidence(evidence_id="inventory-fixed-proof", endpoint="/inventory/A100",
                               data=data, content_hash=content_digest(data))
        raise ToolFailure("policy_no_evidence")


@pytest.fixture
def store(settings):
    return SessionStore(settings.database_url.get_secret_value(), settings.app_id)


def test_recovers_committed_tool_after_process_exit(store):
    thread = store.create_thread(OWNER, "analyst")
    identifier = uuid4()
    child = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), store.app_id, str(thread.thread_id), str(identifier)],
        capture_output=True, timeout=25,
    )
    assert child.returncode == 81
    assert store.history(OWNER, thread.thread_id)[0].report is None

    tools = ProbeTools()
    service = SessionService(store, planner, tools)
    with pytest.raises(SessionConflict):
        service.start(OWNER, "analyst", thread.thread_id, uuid4(), TASK)
    result = service.resume(OWNER, "analyst", thread.thread_id, identifier)
    assert result.report.status == "partial"
    assert result.report.reused_results == 1
    assert tools.calls == ["inventory_status", "warehouse_policy"]
    assert result.report.sections[0].evidence_id == "sale-fixed-proof"
    assert result.report.audit[0].execution_key
    assert service.start(OWNER, "analyst", thread.thread_id, identifier, TASK) == result

    with pytest.raises(SessionConflict):
        service.start(OWNER, "analyst", thread.thread_id, identifier, TaskInput(kind="sales", question="different"))
    with pytest.raises(DocumentNotFound):
        service.resume("other-user", "analyst", thread.thread_id, identifier)
    assert not store.list_threads("other-user")


@pytest.mark.parametrize("fault, calls, expected", [
    ("transient", 2, "completed"),
    ("permanent", 1, "incomplete"),
])
def test_retry_restricted_to_transient_errors(store, fault, calls, expected):
    thread = store.create_thread(OWNER, "analyst")
    tools = ProbeTools(fault)
    task = TaskInput(kind="inventory", question="库存有多少？")
    result = SessionService(store, planner, tools).start(OWNER, "analyst", thread.thread_id, uuid4(), task).report
    assert tools.calls == ["inventory_status"] * calls
    assert result.status == expected
    assert result.retries == calls - 1
    assert [step.attempt for step in result.audit] == list(range(1, calls + 1))
    assert [step.tool for step in result.audit] == tools.calls


def test_busy_thread_and_memory_isolation(store):
    first = store.create_thread(OWNER, "analyst")
    second = store.create_thread(OWNER, "analyst")
    entered, proceed = Event(), Event()
    observed, results = [], []

    def gated(messages, tools):
        observed.append(messages[-1].content)
        if len(observed) == 1:
            entered.set()
            assert proceed.wait(10)
        return planner(messages, tools)

    service = SessionService(store, gated, ProbeTools())
    previous = TaskInput(kind="sales", question="历史销售任务标记")
    current = TaskInput(kind="sales", question="当前销售任务")
    worker = Thread(target=lambda: results.append(
        service.start(OWNER, "analyst", first.thread_id, uuid4(), previous)
    ))
    worker.start()
    try:
        assert entered.wait(10)
        with pytest.raises(SessionConflict):
            service.start(OWNER, "analyst", first.thread_id, uuid4(), current)
    finally:
        proceed.set()
        worker.join(10)
    assert len(results) == 1
    assert service.start(OWNER, "analyst", first.thread_id, uuid4(), current).report.status == "completed"
    assert service.start(OWNER, "analyst", second.thread_id, uuid4(), current).report.status == "completed"
    assert "历史销售任务标记" in observed[1] and "历史销售任务标记" not in observed[2]


@pytest.mark.parametrize("budget", ["time_budget", "model_budget"])
def test_budget_survives_restart(store, monkeypatch, budget):
    thread = store.create_thread(OWNER, "analyst").thread_id
    run = uuid4()
    with store.lock_run(OWNER, "analyst", thread) as conn:
        store.prepare(conn, thread, run, TASK)
        if budget == "model_budget":
            for _ in range(3):
                assert store.reserve_model(conn, thread, run, 3)
    if budget == "time_budget":
        monkeypatch.setattr("enterprise_rag.apps.enterprise_agent.agents.workflow.WALL_BUDGET_SECONDS", 0.02)
        sleep(0.03)

    def unexpected(*_):
        pytest.fail("A depleted budget must prevent another model call")

    report = SessionService(store, unexpected, ProbeTools()).resume(OWNER, "analyst", thread, run).report
    assert report.status == "incomplete" and report.stop_reason == budget
    assert not report.audit
    assert report.model_calls == (3 if budget == "model_budget" else 0)


def test_thread_routes_require_original_credential(settings, service, store):
    analyst = "session-operator-test-fixture-token"
    viewer = "session-policy-test-fixture-token"
    auth = {"Authorization": f"Bearer {analyst}"}
    other = {"Authorization": f"Bearer {viewer}"}
    app = create_enterprise_app(settings, service, tools=ProbeTools(), planner=planner, analyst_token=analyst,
                                viewer_token=viewer, session_store=store)
    with TestClient(app) as client:
        assert client.get("/agent/threads").status_code == 401
        thread = client.post("/agent/threads", headers=auth).json()["thread_id"]
        run = f"/agent/threads/{thread}/runs/{uuid4()}"
        task = {"kind": "sales", "question": "查询销售"}
        assert client.put(run, headers=other, json=task).status_code == 404
        assert client.get(f"/agent/threads/{thread}/runs", headers=other).status_code == 404
        assert client.get("/agent/threads", headers=other).json() == []
        result = client.put(run, headers=auth, json=task).json()
        assert result["report"]["status"] == "completed"
        assert client.post(run + "/resume", headers=auth).json() == result
        assert client.put(run, headers=auth, json={**task, "question": "另一问题"}).status_code == 409
    downgraded = create_enterprise_app(settings, service, tools=ProbeTools(), planner=planner, analyst_token=viewer,
                                       viewer_token=analyst, session_store=store)
    with TestClient(downgraded) as client:
        assert client.get(f"/agent/threads/{thread}/runs", headers=auth).status_code == 404
        assert client.get("/agent/threads", headers=auth).json() == []


if __name__ == "__main__":
    app_id, thread_id, run_id = sys.argv[1:]
    store = SessionStore(os.environ["RAG_TEST_DATABASE_URL"], app_id)
    finish = store.finish_attempt

    def stop_after_success(*args, **kwargs):
        finish(*args, **kwargs)
        os._exit(81)

    store.finish_attempt = stop_after_success
    SessionService(store, planner, ProbeTools()).start(OWNER, "analyst", UUID(thread_id), UUID(run_id), TASK)
