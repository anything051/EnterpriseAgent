import os
from pathlib import Path
from time import sleep

from fastapi.testclient import TestClient
import httpx
from langchain_core.messages import AIMessage, ToolMessage
import psycopg
from pydantic import SecretStr
import pytest

pytest.importorskip("langgraph.graph")

from enterprise_rag.apps.enterprise_agent.api import create_enterprise_app, policy_scope
from enterprise_rag.apps.enterprise_agent.agents.workflow import AgentWorkflow, MAX_CALLS
from enterprise_rag.apps.enterprise_agent.contracts import TaskInput, sales_summary
from enterprise_rag.apps.enterprise_agent.tools import BusinessTools, InventoryClient, SalesRepository
from enterprise_rag.apps.enterprise_agent.tools.inventory import create_inventory_app

ROOT = Path(__file__).resolve().parents[2]
ANALYST = "local-enterprise-analyst-fixture-token"
VIEWER = "local-enterprise-viewer-fixture-token"
TARGET = {"day": "2026-06-01", "region": "华东", "sku": "A100"}
QUESTION = "汇总 2026-06-01 华东销售，查询 A100 库存，并引用盘点差异登记时限。"
CALLS = {
    "sales_summary": {"day": TARGET["day"], "region": TARGET["region"]},
    "inventory_status": {"sku": TARGET["sku"]},
    "warehouse_policy": {"topic": "discrepancy"},
}


def ai(*names):
    return AIMessage(content="", tool_calls=[
        {"id": f"call-{number}-{name}", "name": name, "args": CALLS.get(name, {})}
        for number, name in enumerate(names)
    ])


@pytest.fixture
def inventory():
    with TestClient(create_inventory_app()) as client:
        def get(path):
            reply = client.get(path)
            reply.raise_for_status()
            return reply.json()
        yield InventoryClient(get)


@pytest.fixture
def business_db():
    dsn = os.environ.get("ENTERPRISE_TEST_READER_URL")
    if not dsn:
        pytest.skip("Set ENTERPRISE_TEST_READER_URL to a migrated test business database")
    return SecretStr(dsn)


@pytest.fixture
def business_tools(settings, service, inventory, business_db):
    scope = policy_scope(settings.app_id)
    body = (ROOT / "data/samples/enterprise_inventory.md").read_bytes()
    service.ingest(scope, "warehouse-policy.md", body)
    return BusinessTools(SalesRepository(business_db.get_secret_value()), inventory, service, scope)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_report_and_authorization(settings, service, business_tools):
    exchanges = []

    def planner(messages, tools):
        names = [tool.__name__ for tool in tools]
        exchanges.append((messages, names))
        return ai("sales_summary") if len(exchanges) == 1 else ai("inventory_status", "warehouse_policy")

    app = create_enterprise_app(settings, service, tools=business_tools, planner=planner,
                                analyst_token=ANALYST, viewer_token=VIEWER)
    with TestClient(app) as client:
        route = "/agent/tasks"
        payload = {"kind": "overview", "question": QUESTION, **TARGET}
        assert client.post(route, json=payload).status_code == 401
        denied = client.post(route, json=payload, headers=auth(VIEWER))
        assert denied.status_code == 200
        assert denied.json()["status"] == "denied"
        assert denied.json()["audit"][0]["code"] == "permission_denied"
        assert not exchanges
        reply = client.post(route, json=payload, headers=auth(ANALYST))
        assert reply.status_code == 200, reply.text

        task = reply.json()
        assert task["status"] == "completed"
        assert [item["source_type"] for item in task["evidence"]] == ["sql", "api", "document"]
        evidence = {item["source_type"]: item for item in task["evidence"]}
        assert evidence["sql"]["data"] == {"order_count": 2, "units": 12, "revenue_cents": 145000}
        assert evidence["api"]["data"]["on_hand"] == 18
        assert "24 小时" in evidence["document"]["document"]["content"]
        assert {part["evidence_id"] for part in task["sections"]} == {item["evidence_id"] for item in evidence.values()}
        assert [step["status"] for step in task["audit"]] == ["ok", "ok", "ok"]
        tool_reply = exchanges[1][0][-1]
        assert isinstance(tool_reply, ToolMessage)
        assert tool_reply.tool_call_id == exchanges[1][0][-2].tool_calls[0]["id"] == "call-0-sales_summary"
        assert set(exchanges[0][1]) == set(CALLS)

        loc = evidence["document"]["document"]["locator"]
        source_route = f"/agent/sources/{evidence['document']['document']['source_id']}/{loc['version_id']}/{loc['unit_id']}"
        assert client.get(source_route).status_code == 401
        source = client.get(source_route, headers=auth(VIEWER))
        assert source.status_code == 200
        assert source.json()["source_text"][loc["start"]:loc["end"]] == evidence["document"]["document"]["content"]
        foreign_scope = policy_scope("foreign-" + settings.app_id)
        service.ingest(foreign_scope, "private.md", "其他范围".encode())
        candidate = service.retriever.search(foreign_scope, "其他范围", 1)[0]
        loc = candidate.locator
        foreign_route = f"/agent/sources/{candidate.source_id}/{loc.version_id}/{loc.unit_id}"
        assert service.store.source(foreign_scope, candidate.source_id, loc.version_id, loc.unit_id).source_text
        assert client.get(foreign_route, headers=auth(VIEWER)).status_code == 404


def test_proposals_are_validated_and_bounded(business_tools):
    generated = [
        {"name": "sales_summary", "args": {"sql": "SELECT 1"}, "id": "wrong-schema"},
        {"name": "sales_summary", "args": {"day": TARGET["day"], "region": "华南"}, "id": "wrong-scope"},
        {"name": "other", "args": {}, "id": "unknown"},
    ] + [{"name": "sales_summary", "args": CALLS["sales_summary"], "id": f"legit-{i}"}
         for i in range(MAX_CALLS + 2)]

    def planner(_messages, _tools):
        return AIMessage(content="", tool_calls=generated)

    report = AgentWorkflow(planner, business_tools).run(TaskInput(kind="overview", question=QUESTION, **TARGET), "analyst")
    assert report.status == "partial"
    assert report.stop_reason == "call_budget"
    assert [step.code for step in report.audit][:3] == ["invalid_arguments", "outside_task", "tool_unavailable"]
    assert len(report.audit) == MAX_CALLS and report.dropped_calls == len(generated) - MAX_CALLS
    assert len(report.evidence) == 1
    assert report.audit[-1].code == "already_obtained"


def test_role_rechecks_model_proposals(business_tools):
    def planner(_messages, tools):
        assert [tool.__name__ for tool in tools] == ["warehouse_policy"]
        return ai("sales_summary", "warehouse_policy")

    report = AgentWorkflow(planner, business_tools).run(TaskInput(kind="policy", question="盘点差异怎么登记？"), "viewer")
    assert report.status == "completed"
    assert [entry.code for entry in report.audit] == ["permission_denied", "ok"]
    assert [item.source_type for item in report.evidence] == ["document"]


def test_reader_permissions_and_recomputable_values(business_db):
    with psycopg.connect(business_db.get_secret_value()) as conn:
        role, readonly, can_write, can_read_rag = conn.execute("""
            SELECT current_user, current_setting('transaction_read_only'),
                   has_table_privilege(current_user, 'enterprise_demo.sales', 'INSERT'),
                   has_schema_privilege(current_user, 'rag', 'USAGE')
        """).fetchone()
        assert (role, readonly, can_write, can_read_rag) == ("enterprise_reader", "on", False, False)
        units, cents = conn.execute("""
            SELECT sum(units), sum(units * unit_price_cents)
            FROM enterprise_demo.sales WHERE sales_day=%s AND region=%s
        """, (TARGET["day"], TARGET["region"])).fetchone()
    assert (units, cents) == (12, 145000)


@pytest.mark.parametrize("problem, expected", [("timeout", "inventory_timeout"), ("wrong_sku", "inventory_invalid")])
def test_inventory_failure_keeps_verified_sales(business_tools, problem, expected):
    def get(_path):
        if problem == "timeout":
            raise httpx.ReadTimeout("fixture")
        return {"sku": "C300", "snapshot": "qh-2026-06-v1", "on_hand": 18, "reserved": 5, "safety_stock": 10}

    business_tools.inventory = InventoryClient(get)

    def planner(messages, _tools):
        return ai("sales_summary", "inventory_status") if len(messages) == 2 else AIMessage(content="")

    report = AgentWorkflow(planner, business_tools, include_policy=False).run(
        TaskInput(kind="overview", question=QUESTION, **TARGET), "analyst",
    )
    assert report.status == "partial"
    assert report.audit[-1].code == expected
    assert [item.source_type for item in report.evidence] == ["sql"]


def test_task_deadline_keeps_verified_sales(business_tools, monkeypatch):
    sales = business_tools.execute(sales_summary(**CALLS["sales_summary"]))

    def slow_inventory(args):
        if isinstance(args, sales_summary):
            return sales
        sleep(0.55)
        return None

    monkeypatch.setattr(business_tools, "execute", slow_inventory)
    monkeypatch.setattr("enterprise_rag.apps.enterprise_agent.agents.workflow.WALL_BUDGET_SECONDS", 0.2)
    report = AgentWorkflow(lambda *_: ai("sales_summary", "inventory_status"), business_tools).run(
        TaskInput(kind="overview", question=QUESTION, **TARGET), "analyst",
    )
    assert report.status == "partial" and report.stop_reason == "time_budget"
    assert report.elapsed_ms < 450
    assert report.audit[0].evidence_id == sales.evidence_id
    assert report.audit[-1].code == "time_budget"


def test_duplicate_provider_ids_do_not_execute_tools(business_tools):
    def duplicate(*_):
        return AIMessage(content="", tool_calls=[
            {"name": name, "args": CALLS[name], "id": "reused-id"}
            for name in ("sales_summary", "inventory_status")
        ])

    report = AgentWorkflow(duplicate, business_tools).run(TaskInput(kind="overview", question=QUESTION), "analyst")
    assert not report.evidence
    assert {step.code for step in report.audit} == {"invalid_call_id"}


def test_streamlit_report_respects_role_change(settings, service, business_tools, monkeypatch):
    AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
    endpoint = "http://enterprise-ui.test"
    monkeypatch.setenv("ENTERPRISE_AGENT_API_URL", endpoint)
    original = httpx.Client.request

    def planner(_messages, tools):
        return ai(*(tool.__name__ for tool in tools))

    app = create_enterprise_app(settings, service, tools=business_tools, planner=planner,
                                analyst_token=ANALYST, viewer_token=VIEWER)
    with TestClient(app) as client:
        def redirect(self, method, url, **kwargs):
            if str(url).startswith(endpoint + "/"):
                return client.request(method, url.removeprefix(endpoint), **kwargs)
            return original(self, method, url, **kwargs)

        monkeypatch.setattr(httpx.Client, "request", redirect)
        ui = AppTest.from_file(str(ROOT / "src/enterprise_rag/apps/enterprise_agent/ui.py")).run()
        ui.text_input[0].input(ANALYST).run()
        next(btn for btn in ui.button if btn.label == "生成报告").click().run()
        assert not ui.exception and not ui.error
        assert any("¥1,450.00" in item.value for item in ui.text)
        assert any("24 小时" in item.value for item in ui.text)
        ui.text_input[0].input(VIEWER).run()
        assert ui.selectbox[0].options == ["仓库制度"]
        assert all("¥1,450.00" not in item.value for item in ui.text)
        assert not ui.exception and not ui.error
