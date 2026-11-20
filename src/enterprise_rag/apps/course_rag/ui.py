"""Streamlit workbench for a local CourseRAG operator."""

from hashlib import sha256
import os

import httpx
import streamlit as st
from dotenv import load_dotenv

load_dotenv()
API_URL = os.environ.get("COURSE_RAG_API_URL", "http://127.0.0.1:8001").rstrip("/")
STYLE = """
<style>
:root { --ink:#17263b; --muted:#53647a; --line:#e2e8ee; }
h1, h2, h3 { color:var(--ink) !important; letter-spacing:-0.035em; }
[data-testid="stSidebar"] { background:#eaf0f3; }
[data-testid="stSidebar"] h1 { font-size:1.75rem; color:#113d43 !important; }
[data-testid="stMainBlockContainer"] { padding-top:2.5rem; max-width:78rem; }
[data-testid="stVerticalBlockBorderWrapper"] { border-color:var(--line); }
[data-testid="stCaptionContainer"] { color:var(--muted); }
</style>
"""
GROUP_NAMES = {"A": "向量基线", "B": "混合检索", "C": "证据标注", "D": "严格拒答"}
SUPPORT_NAMES = {
    "supported": "支持", "unsupported": "不支持", "contradicted": "矛盾",
    "uncertain": "待确认", "unchecked": "未审阅",
}


class CourseError(Exception):
    pass


class CourseClient:
    def __init__(self, token: str):
        self.headers = {"Authorization": f"Bearer {token}"}

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            with httpx.Client(timeout=180, trust_env=False) as client:
                response = client.request(method, API_URL + path, headers=self.headers, **kwargs)
        except httpx.HTTPError as error:
            raise CourseError("无法连接课程服务。请检查 CourseRAG API 是否已启动。") from error
        if response.is_error:
            messages = {
                401: "课程令牌无效。请检查本地配置。",
                404: "未找到这门课程的资料或评测归档。",
                409: "模型或索引配置与已有资料不一致。",
                413: "上传文件超过允许大小。",
                502: "模型未能生成可处理的结果，请稍后重试。",
                503: "数据库暂不可用。",
                504: "模型响应超时，请稍后重试。",
            }
            message = messages.get(response.status_code, "请求未通过。请核对输入类型与内容。")
            if response.is_server_error and (request_id := response.headers.get("X-Request-ID")):
                message = f"{message} 请求 ID {request_id}"
            raise CourseError(message)
        return response

    def get(self, path: str):
        return self.request("GET", path).json()

    def create(self, path: str, data: dict, message: str):
        result = self.request("POST", path, json=data).json()
        st.session_state["course_notice"] = message
        return result


def _explain_source(client: CourseClient, prefix: str, evidence: dict, span: dict) -> None:
    loc = evidence["locator"]
    path = f"{prefix}/sources/{evidence['source_id']}/{loc['version_id']}/{loc['unit_id']}"
    source = client.get(path)
    params = {"start": span["start"], "end": span["end"]}
    text = source["source_text"]
    if source["page"] is not None:
        st.caption(f"PDF 第 {source['page']} 页 · 段落 {loc['paragraph_id']}")
        try:
            image = client.request("GET", path + "/preview", params=params)
            st.image(image.content, width="stretch")
            if image.headers.get("X-Preview-Mode") != "highlight":
                st.caption("当前显示整页，引用片段见下方。")
        except CourseError:
            st.caption("该页无法生成预览，仍可核对引用片段与原文。")
    else:
        heading = " / ".join(source["heading_path"]) or "课程材料"
        st.caption(f"{heading} · 原文第 {source['start_line']}–{source['end_line']} 行")
    st.text(text[span["start"]:span["end"]])
    with st.expander("查看完整原文"):
        st.text(text)
        st.caption(f"文档版本 {loc['version_id']}")


def _questions(client: CourseClient, prefix: str, catalog: dict) -> None:
    course_id = catalog["course"]["id"]
    results = st.session_state.setdefault("course_answers", {})
    left, right = st.columns([1.07, 1], gap="large")
    with left:
        with st.container(border=True):
            st.subheader("课程问答")
            with st.form(f"ask-{course_id}"):
                question = st.text_area("问题", placeholder="在 LRU 页面置换算法中，优先淘汰哪个页面？", max_chars=2000)
                if st.form_submit_button("查询课程", type="primary"):
                    if not question.strip():
                        st.warning("请输入问题。")
                    else:
                        with st.spinner("正在检索并审阅课程证据…"):
                            results[course_id] = client.request(
                                "POST", prefix + "/questions", json={"question": question},
                            ).json()
            answer = results.get(course_id)
            if answer is not None:
                if answer["answer_status"] == "answered":
                    for number, claim in enumerate(answer["claims"], 1):
                        st.text(f"{number}. {claim['text']}")
                else:
                    st.info(answer["answer"])
                strategy = {
                    "none": "无候选", "vector": "向量检索", "hybrid_rrf": "混合检索",
                    "hybrid_reranked": "混合检索与重排",
                }.get((answer.get("retrieval") or {}).get("strategy"))
                if strategy:
                    st.caption(strategy)
    with right:
        with st.container(border=True):
            st.subheader("来源与证据")
            if not answer:
                st.caption("提交问题后，可在这里打开引用。")
                return
            materials = {item["document_id"]: item["title"] for item in catalog["materials"]}
            evidence = {item["evidence_id"]: item for item in answer["evidence"]}
            claims = {item["claim_id"]: number for number, item in enumerate(answer["claims"], 1)}
            options = []
            for cite in answer["citations"]:
                item = evidence[cite["evidence_id"]]
                options.append((item, cite.get("quote_span") or item["locator"], cite["support_status"],
                                claims.get(cite["claim_id"])))
            for item in (answer.get("verification") or {}).get("conflict_evidence", []):
                options.append((item, item["locator"], "contradicted", None))
            if not options:
                st.caption("本次问答没有可打开的证据。")
                return

            def label(index):
                item, _, status, claim_number = options[index]
                page = item["locator"]["page"]
                location = f"第 {page} 页" if page else f"第 {item['locator']['start_line']} 行"
                title = materials.get(item["source_id"], "课程资料")
                claim_label = f"陈述 {claim_number}" if claim_number is not None else "补充证据"
                return f"{claim_label} · {title} · {location} · {SUPPORT_NAMES[status]}"

            chosen = st.selectbox("打开一条引用", range(len(options)), format_func=label,
                                  key=f"evidence-{answer['answer_id']}")
            item, span, _, _ = options[chosen]
            _explain_source(client, prefix, item, span)


def _materials(client: CourseClient, prefix: str, data: dict) -> None:
    st.subheader("章节与资料")
    chapters = {item["id"]: item for item in data["chapters"]}
    with st.expander("添加章节", expanded=not chapters):
        with st.form(f"chapter-{prefix}"):
            title = st.text_input("章节名称", max_chars=120)
            position = st.number_input("章节顺序", min_value=1, max_value=10000, value=min(len(chapters) + 1, 10000))
            parent = st.selectbox("上级章节", [None, *chapters],
                                  format_func=lambda item: chapters[item]["title"] if item else "顶级章节")
            if st.form_submit_button("添加章节"):
                client.create(prefix + "/chapters", {"title": title, "position": position, "parent_id": parent},
                              "章节已添加。")
                st.rerun()
    if not chapters:
        return

    def chapter_label(key):
        item = chapters[key]
        parent = chapters.get(item["parent_id"])
        return f"{item['position']} · " + (f"{parent['title']} / " if parent else "") + item["title"]

    chapter_id = st.selectbox("查看章节", list(chapters), format_func=chapter_label, key=f"section-{prefix}")
    points = {item["id"]: item for item in data["points"] if item["chapter_id"] == chapter_id}
    materials = {item["id"]: item for item in data["materials"] if item["chapter_id"] == chapter_id}
    left, right = st.columns(2, gap="large")
    with left:
        with st.container(border=True):
            st.subheader("知识点")
            for point in points.values():
                st.write(point["title"])
                if point["notes"]:
                    st.caption(point["notes"])
            if not points:
                st.caption("本章还没有知识点。")
            with st.expander("添加知识点"):
                with st.form(f"point-{chapter_id}"):
                    title = st.text_input("知识点名称", max_chars=120)
                    notes = st.text_area("说明", max_chars=1600)
                    if st.form_submit_button("添加知识点"):
                        client.create(prefix + "/points", {"title": title, "notes": notes, "chapter_id": chapter_id},
                                      "知识点已添加。")
                        st.rerun()
    with right:
        with st.container(border=True):
            st.subheader("材料")
            with st.expander("登记材料", expanded=not materials):
                with st.form(f"material-{chapter_id}"):
                    title = st.text_input("材料名称", max_chars=120)
                    material_format = st.selectbox("材料类型", ["pdf", "md"], format_func=str.upper)
                    point_id = st.selectbox("关联知识点", [None, *points],
                                            format_func=lambda item: points[item]["title"] if item else "不关联")
                    if st.form_submit_button("登记材料"):
                        client.create(
                            prefix + "/materials",
                            {"title": title, "format": material_format, "chapter_id": chapter_id, "point_id": point_id},
                            "材料已登记，可以上传版本。",
                        )
                        st.rerun()

            if not materials:
                return
            active = st.selectbox("查看材料", list(materials), format_func=lambda item: materials[item]["title"],
                                  key=f"material-choice-{chapter_id}")
            item = materials[active]
            route = f"{prefix}/materials/{active}/revisions"
            revisions = client.get(route)
            if not revisions:
                st.caption("还没有发布版本。")
            else:
                rows = [
                    {"版本": entry["revision"], "状态": "当前" if entry["active"] else "历史",
                     "SHA-256": entry["file_sha256"][:12]}
                    for entry in revisions
                ]
                st.dataframe(rows, hide_index=True)
            with st.form(f"upload-{active}", clear_on_submit=True):
                uploaded = st.file_uploader("上传材料版本", type=["pdf"] if item["format"] == "pdf" else ["md", "markdown"])
                if st.form_submit_button("发布版本", type="primary"):
                    if uploaded is None:
                        st.warning("请先选择文件。")
                    else:
                        with st.spinner("正在处理材料并建立索引…"):
                            result = client.request("POST", route, files={"file": (uploaded.name, uploaded.getvalue())})
                        st.session_state["course_notice"] = "版本已发布。" if result.status_code == 201 else "已存在相同版本。"
                        st.rerun()


def _evaluations(client: CourseClient) -> None:
    st.subheader("评测归档")
    runs = client.get("/evaluations")
    if not runs:
        st.info("当前没有可导出的课程评测。")
        return
    run = st.selectbox("运行记录", runs)
    metrics = client.get(f"/evaluations/{run}")
    rows = []
    for group, item in metrics["groups"].items():
        accuracy = item["answer_accuracy"]
        rows.append({"组别": group, "策略": GROUP_NAMES[group], "题数": item["samples"],
                     "正确数": accuracy["numerator"], "可回答题": accuracy["denominator"], "错误": item["errors"]})
    st.dataframe(rows, hide_index=True)
    st.caption("该归档记录小型开发集的实际结果。正式效果比较需要独立测试集。")
    archive = client.request("GET", f"/evaluations/{run}/export")
    st.download_button("下载逐例记录与报告", archive.content, file_name=f"{run}.zip", mime="application/zip")


def main() -> None:
    st.set_page_config(page_title="CourseRAG · 课程工作台", layout="wide", page_icon="📚")
    st.markdown(STYLE, unsafe_allow_html=True)
    with st.sidebar:
        st.title("CourseRAG")
        token = st.text_input("课程令牌", type="password")
        identity = sha256(token.encode()).hexdigest()
        if st.session_state.get("course_operator") != identity:
            for key in ("course_answers", "active_course", "created_course", "course_notice"):
                st.session_state.pop(key, None)
            st.session_state["course_operator"] = identity
        if not token:
            st.info("输入本地课程服务的访问令牌。")
            return
        client = CourseClient(token)
        try:
            courses = {item["id"]: item for item in client.get("/courses")}
        except CourseError as error:
            st.error(str(error))
            return

        if created := st.session_state.pop("created_course", None):
            st.session_state["active_course"] = created
        active = None
        if courses:
            active = st.selectbox(
                "当前课程", list(courses), format_func=lambda key: f"{courses[key]['title']} · {courses[key]['term']}",
                key="active_course",
            )
        with st.expander("新建课程", expanded=not courses):
            with st.form("course-create"):
                title = st.text_input("课程名称", max_chars=120)
                term = st.text_input("学期", max_chars=80)
                if st.form_submit_button("创建课程"):
                    try:
                        created = client.create("/courses", {"title": title, "term": term}, "课程已创建。")
                        st.session_state["created_course"] = created["id"]
                        st.rerun()
                    except CourseError as error:
                        st.error(str(error))

    if notice := st.session_state.pop("course_notice", None):
        st.toast(notice)
    if active:
        st.title(courses[active]["title"])
        st.caption(courses[active]["term"])
    else:
        st.title("课程工作台")
    mode = st.segmented_control("工作区", ["问答", "课程资料", "评测"], default="问答", label_visibility="collapsed")
    try:
        if mode == "评测":
            _evaluations(client)
        elif not active:
            st.info("在左侧创建课程，或打开评测查看开发集结果。")
        else:
            prefix = f"/courses/{active}"
            data = client.get(prefix)
            if mode == "课程资料":
                _materials(client, prefix, data)
            else:
                _questions(client, prefix, data)
    except CourseError as error:
        st.error(str(error))


if __name__ == "__main__":
    main()
