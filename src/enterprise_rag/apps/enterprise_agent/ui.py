"""Streamlit entry point for the local enterprise demo."""

import os
from uuid import uuid4

from dotenv import load_dotenv
import httpx
import streamlit as st

load_dotenv()
API = os.environ.get("ENTERPRISE_AGENT_API_URL", "http://127.0.0.1:8002").rstrip("/")
MODES = {"overview": "业务概览", "sales": "销售汇总", "inventory": "库存快照", "policy": "仓库制度"}
SOURCE_NAME = {"sql": "PostgreSQL 销售汇总", "api": "库存 HTTP API", "document": "仓库制度原文"}
STATUSES = {"completed": "已完成", "partial": "已取得部分结果", "incomplete": "未取得有效结果", "denied": "当前角色无权执行"}
STOP_MESSAGES = {
    "capability_missing": "缺少任务所需的工具。",
    "no_tool_calls": "模型未提交进一步的工具调用。",
    "call_budget": "已达到工具调用预算。",
    "model_budget": "已达到模型调用预算。",
    "time_budget": "已达到任务等待预算。",
    "model_timeout": "模型响应超时。",
    "model_unavailable": "模型服务暂不可用。",
    "model_invalid": "模型返回了无法解析的工具调用。",
    "workers_busy": "执行线程正在处理其他任务。",
    "permission_denied": "该任务超出当前角色的权限。",
    "missing_evidence": "部分工具未能提供可核查的证据。",
}
TOOL_NAMES = {
    "sales_summary": "销售汇总", "inventory_status": "库存快照", "warehouse_policy": "仓库制度",
    "authorize_task": "权限校验", "unknown": "未知工具",
}
DEFAULT_QUESTIONS = {
    "overview": "汇总指定日期和区域的销售，查询指定商品库存，并引用盘点差异登记时限。",
    "sales": "当天该地区有多少订单、销售件数和销售额？",
    "inventory": "这个商品当前有多少在库、预留和可用数量？",
    "policy": "发现账面库存与实物不符时，应在多久内登记差异？",
}


class UIError(Exception):
    pass


def request(token: str, method: str, path: str, **kwargs) -> dict:
    try:
        with httpx.Client(timeout=165, trust_env=False, follow_redirects=False) as client:
            reply = client.request(method, API + path, headers={"Authorization": f"Bearer {token}"}, **kwargs)
    except httpx.HTTPError as error:
        raise UIError("暂时无法连接 EnterpriseAgent API。") from error
    if not reply.is_success:
        if reply.status_code == 401:
            raise UIError("访问令牌无效。")
        if reply.status_code == 403:
            raise UIError("当前角色无权执行此操作。")
        if reply.status_code == 409:
            raise UIError("会话中已有执行中的任务。刷新页面后可查看或继续。")
        identifier = reply.headers.get("X-Request-ID")
        raise UIError(f"请求失败（HTTP {reply.status_code}）。" + (f"请求 ID {identifier}" if identifier else ""))
    return reply.json()


def show_report(token: str, result: dict) -> None:
    st.subheader("业务报告")
    status = result["status"]
    if status == "completed":
        st.success(STATUSES[status])
    else:
        st.warning(f"{STATUSES[status]}。{STOP_MESSAGES.get(result['stop_reason'], '')}")
    if missing := result["missing_tools"]:
        st.caption("缺少依据：" + "、".join(TOOL_NAMES[tool] for tool in missing))

    evidence = {item["evidence_id"]: item for item in result["evidence"]}
    for section in result["sections"]:
        item = evidence[section["evidence_id"]]
        kind = item["source_type"]
        with st.container(border=True):
            st.subheader(section["title"])
            st.text(section["text"])
            with st.expander(SOURCE_NAME[kind]):
                if kind == "document":
                    document = item["document"]
                    loc = document["locator"]
                    st.caption(f"原文第 {loc['start_line']}–{loc['end_line']} 行 · 文档版本 {loc['version_id']}")
                    if st.button("读取完整原文单元", key=f"source-{result['task_id']}"):
                        path = f"/agent/sources/{document['source_id']}/{loc['version_id']}/{loc['unit_id']}"
                        st.text(request(token, "GET", path)["source_text"])
                else:
                    st.caption(f"数据快照 {item['snapshot']}")
                    st.json({key: value for key, value in item.items() if key != "source_type"}, expanded=False)

    with st.expander("工具调用记录"):
        st.caption(f"任务 ID {result['task_id']} · {result['elapsed_ms'] / 1000:.2f} 秒")
        input_tokens, output_tokens = result["input_tokens"], result["output_tokens"]
        if input_tokens is not None and output_tokens is not None:
            st.caption(f"模型调用 {result['model_calls']} 次 · 输入 {input_tokens} Token · 输出 {output_tokens} Token")
        if result["dropped_calls"]:
            st.caption(f"超出预算的提议 {result['dropped_calls']} 条")
        if result.get("agent_steps"):
            names = {"supervisor": "主管", "data_agent": "数据", "knowledge_agent": "知识", "report_agent": "报告"}
            st.caption(" → ".join(names[step["name"]] for step in result["agent_steps"]))
            st.caption(f"重试 {result['retries']} 次 · 恢复时复用结果 {result['reused_results']} 条")
        for step in result["audit"]:
            name = TOOL_NAMES[step["tool"]]
            st.text(f"{name} · {step['status']} / {step['code']} · {step['elapsed_ms']:.2f} ms")
            marker = " · 已复用" if step.get("reused") else ""
            st.caption(f"第 {step.get('attempt', 1)} 次 · {step['call_id']}{marker}")


def select_thread(token: str) -> str | None:
    threads = {row["thread_id"]: row for row in request(token, "GET", "/agent/threads")}
    if st.button("新建会话"):
        created = request(token, "POST", "/agent/threads")
        st.session_state["pending_thread"] = created["thread_id"]
        st.rerun()
    key = "enterprise_thread"
    if new := st.session_state.pop("pending_thread", None):
        st.session_state[key] = new
    return st.selectbox(
        "当前会话", list(threads), key=key,
        format_func=lambda key: f"{threads[key]['created_at'][:10]} · {key[:8]}",
    ) if threads else None


def show_thread_runs(token: str, thread_id: str, runs: list[dict]) -> None:
    if not runs:
        st.info("提交任务后查看报告、引用与工具调用记录。")
        return
    by_id = {run["run_id"]: run for run in runs}
    key = f"enterprise_run-{thread_id}"
    if new := st.session_state.pop("pending_run", None):
        if new in by_id:
            st.session_state[key] = new
    chosen = st.selectbox(
        "会话记录", list(by_id), key=key,
        format_func=lambda identifier: f"{MODES[by_id[identifier]['task']['kind']]} · {identifier[:8]}",
    )
    if report := by_id[chosen]["report"]:
        show_report(token, report)
    else:
        st.info("这次任务尚未生成报告。继续时会复用已保存的工具结果。")
        if st.button("继续任务", key=f"resume-{chosen}"):
            with st.spinner("正在恢复任务…"):
                request(token, "POST", f"/agent/threads/{thread_id}/runs/{chosen}/resume")
            st.rerun()


def main() -> None:
    st.set_page_config(page_title="EnterpriseAgent · 业务工作台", layout="wide", page_icon="📊")
    with st.sidebar:
        st.title("EnterpriseAgent")
        token = st.text_input("访问令牌", type="password")
        if not token:
            st.info("输入分析员或制度查阅令牌。")
            return
        try:
            profile = request(token, "GET", "/agent/session")
            role = profile["role"]
            if st.session_state.get("report_token") != token:
                for key in ("enterprise_report", "enterprise_thread", "pending_thread", "pending_run"):
                    st.session_state.pop(key, None)
                st.session_state["report_token"] = token
            threaded = profile.get("threads_enabled", False)
            thread = select_thread(token) if threaded else None
            st.caption("分析员" if role == "analyst" else "制度查阅")
            if role == "analyst" and st.button("载入演示制度"):
                request(token, "POST", "/agent/demo/policy")
                st.success("演示制度已可检索。")
        except UIError as error:
            st.error(str(error))
            return

    st.title("业务工作台")
    st.caption("青禾公司 · 合成业务快照")
    try:
        runs = request(token, "GET", f"/agent/threads/{thread}/runs") if thread else []
    except UIError as error:
        st.error(str(error))
        return
    busy = any(run["report"] is None for run in runs)
    left, right = st.columns([1, 1.3], gap="large")
    with left:
        st.subheader("分析任务")
        available = list(MODES) if role == "analyst" else ["policy"]
        kind = st.selectbox("任务类型", available, format_func=MODES.__getitem__)
        with st.form(f"task-{kind}"):
            question = st.text_area("问题", DEFAULT_QUESTIONS[kind], max_chars=1400)
            payload = {"kind": kind, "question": question}
            if kind in {"overview", "sales"}:
                date_col, region_col = st.columns(2)
                payload["day"] = date_col.selectbox("销售日期", ["2026-06-01", "2026-06-02"])
                payload["region"] = region_col.selectbox("区域", ["华东", "华南"])
            if kind in {"overview", "inventory"}:
                payload["sku"] = st.selectbox("商品", ["A100", "B200", "C300"])
            if st.form_submit_button("生成报告", type="primary", disabled=busy):
                st.session_state.pop("enterprise_report", None)
                if not question.strip():
                    st.warning("请输入问题。")
                else:
                    try:
                        with st.spinner("正在调用授权工具并核对来源…"):
                            if threaded:
                                if thread is None:
                                    thread = request(token, "POST", "/agent/threads")["thread_id"]
                                    st.session_state["pending_thread"] = thread
                                run = str(uuid4())
                                st.session_state["pending_run"] = run
                                request(token, "PUT", f"/agent/threads/{thread}/runs/{run}", json=payload)
                                st.rerun()
                            else:
                                st.session_state["enterprise_report"] = request(token, "POST", "/agent/tasks", json=payload)
                    except UIError as error:
                        st.error(str(error))
    with right:
        try:
            if threaded and thread:
                show_thread_runs(token, thread, runs)
            elif result := st.session_state.get("enterprise_report"):
                show_report(token, result)
            else:
                st.info("提交任务后查看报告、引用与工具调用记录。")
        except UIError as error:
            st.error(str(error))


if __name__ == "__main__":
    main()
