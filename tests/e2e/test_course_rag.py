from io import BytesIO
import os
from pathlib import Path
from shutil import copytree, ignore_patterns
from uuid import uuid4
from zipfile import ZipFile

from fastapi.testclient import TestClient
import httpx
from pypdf import PdfReader, PdfWriter
import pytest

from enterprise_rag.apps.course_rag.api import create_course_app
from enterprise_rag.apps.course_rag.catalog import CourseCatalog
from enterprise_rag.config import Settings
from enterprise_rag.evaluation.artifacts import write_manifest

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "local-course-regression-operator-v04"
HEADERS = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def course_app(settings, service):
    catalog = CourseCatalog(settings.database_url.get_secret_value(), settings.app_id)
    return create_course_app(settings, service, catalog=catalog, token=TOKEN,
                             evaluation_root=ROOT / "docs/evaluation_runs")


def create_course(client, title):
    response = client.post("/courses", headers=HEADERS, json={"title": title, "term": "实验学期"})
    assert response.status_code == 201, response.text
    return "/courses/" + response.json()["id"]


def create_material(client, course, format="md"):
    chapter = client.post(course + "/chapters", headers=HEADERS, json={"title": "第一章"})
    assert chapter.status_code == 201, chapter.text
    chapter_id = chapter.json()["id"]
    point = client.post(course + "/points", headers=HEADERS,
                        json={"title": "核心概念", "chapter_id": chapter_id, "notes": "用于定位原文"})
    assert point.status_code == 201, point.text
    material = client.post(course + "/materials", headers=HEADERS,
                           json={"title": "课程讲义", "chapter_id": chapter_id,
                                 "point_id": point.json()["id"], "format": format})
    assert material.status_code == 201, material.text
    return chapter_id, material.json()["id"]


def upload(client, course, material, name, body):
    return client.post(f"{course}/materials/{material}/revisions", headers=HEADERS, files={"file": (name, body)})


def ask(client, course, text):
    response = client.post(course + "/questions", headers=HEADERS, json={"question": text})
    assert response.status_code == 200, response.text
    return response.json()


def citation_path(course, evidence):
    return f"{course}/sources/{evidence['source_id']}/{evidence['locator']['version_id']}/{evidence['locator']['unit_id']}"


def test_course_catalog_revision_and_cross_course_isolation(course_app):
    with TestClient(course_app) as client:
        assert client.get("/courses").status_code == 401
        assert client.get("/documents").status_code == 404
        a = create_course(client, "操作系统")
        b = create_course(client, "计算机网络")
        chapter, material = create_material(client, a)
        rejected = client.post(b + "/points", headers=HEADERS, json={"title": "违规", "chapter_id": chapter})
        assert rejected.status_code == 404
        assert upload(client, b, material, "lesson.md", b"invalid").status_code == 404

        first = upload(client, a, material, "lesson.md", "页表记录旧版映射。".encode())
        assert first.status_code == 201, first.text
        duplicate = upload(client, a, material, "renamed.md", "页表记录旧版映射。".encode())
        assert duplicate.status_code == 200
        assert duplicate.json()["version_id"] == first.json()["version_id"]
        assert ask(client, b, "页表记录什么？")["answer_status"] == "insufficient_evidence"
        evidence = ask(client, a, "页表记录什么？")["evidence"][0]
        path = citation_path(a, evidence)
        assert client.get(path.replace(a, b, 1), headers=HEADERS).status_code == 404

        newer = upload(client, a, material, "lesson.md", "页表记录新版映射。".encode())
        assert newer.status_code == 201
        versions = client.get(f"{a}/materials/{material}/revisions", headers=HEADERS).json()
        assert [v["revision"] for v in versions] == [2, 1]
        catalog = client.get(a, headers=HEADERS).json()
        assert catalog["points"][0]["title"] == "核心概念"
        assert catalog["materials"][0]["active_revision"] == 2
        old = client.get(path, headers=HEADERS)
        assert old.status_code == 200
        assert "旧版" in old.json()["source_text"]
        assert "新版" in ask(client, a, "页表记录什么？")["answer"]


def test_pdf_preview_and_evaluation_export(course_app):
    with TestClient(course_app) as client:
        course = create_course(client, "PDF 证据")
        _, material = create_material(client, course, "pdf")
        data = (ROOT / "data/samples/course_memory.pdf").read_bytes()
        assert upload(client, course, material, "course_memory.pdf", data).status_code == 201
        evidence = ask(client, course, "页面置换")["evidence"][0]
        path = citation_path(course, evidence)
        locator = evidence["locator"]
        assert locator["page"] == 2
        params = {"start": locator["start"], "end": locator["end"]}
        assert client.get(path + "/highlight", headers=HEADERS, params=params).json()["status"] == "highlight"
        preview = client.get(path + "/preview", headers=HEADERS, params=params)
        assert preview.status_code == 200, preview.text
        assert preview.headers["X-Preview-Mode"] == "highlight"
        assert preview.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert client.get(path + "/preview", headers=HEADERS, params={"start": 0}).status_code == 422

        runs = client.get("/evaluations", headers=HEADERS).json()
        assert "course-dev-20261120" in runs
        archive = client.get("/evaluations/course-dev-20261120/export", headers=HEADERS)
        assert archive.status_code == 200
        with ZipFile(BytesIO(archive.content)) as bundle:
            assert {"config.json", "samples.jsonl", "report.md", "artifacts.json"} <= set(bundle.namelist())
        assert client.get("/evaluations/unknown/export", headers=HEADERS).status_code == 404


def test_rotated_pdf_has_page_only_preview(course_app):
    reader = PdfReader(ROOT / "data/samples/course_memory.pdf")
    writer = PdfWriter()
    writer.add_page(reader.pages[1].rotate(90))
    buffer = BytesIO()
    writer.write(buffer)

    with TestClient(course_app) as client:
        course = create_course(client, "旋转页面")
        _, material = create_material(client, course, "pdf")
        assert upload(client, course, material, "rotated.pdf", buffer.getvalue()).status_code == 201
        evidence = ask(client, course, "页面置换")["evidence"][0]
        path = citation_path(course, evidence)
        locator = evidence["locator"]
        preview = client.get(path + "/preview", headers=HEADERS,
                             params={"start": locator["start"], "end": locator["end"]})
        assert preview.status_code == 200, preview.text
        assert preview.headers["X-Preview-Mode"] == "page_only"
        assert preview.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert "LRU" in client.get(path, headers=HEADERS).json()["source_text"]


def test_prepared_run_can_export_and_changed_artifact_is_rejected(settings, service, tmp_path):
    run_id = "course-dev-20261120"
    directory = tmp_path / run_id
    copytree(ROOT / "docs/evaluation_runs" / run_id, directory, ignore=ignore_patterns("artifacts.json"))
    app = create_course_app(settings, service, token=TOKEN, evaluation_root=tmp_path)
    with TestClient(app) as client:
        assert client.get("/evaluations", headers=HEADERS).json() == []
        assert write_manifest(directory) == directory / "artifacts.json"
        assert client.get("/evaluations", headers=HEADERS).json() == [run_id]
        route = f"/evaluations/{run_id}/export"
        result = client.get(route, headers=HEADERS)
        assert result.status_code == 200
        with ZipFile(BytesIO(result.content)) as bundle:
            assert bundle.read("report.md") == (directory / "report.md").read_bytes()
        report = directory / "report.md"
        report.write_bytes(b"!" + report.read_bytes()[1:])
        assert client.get(route, headers=HEADERS).status_code == 404


def test_failed_upload_leaves_empty_material_without_leaking_content(course_app):
    with TestClient(course_app) as client:
        course = create_course(client, "导入失败回归")
        _, material = create_material(client, course)
        result = upload(client, course, material, "broken.md", b"\xff")
        assert result.status_code == 422
        assert client.get(course, headers=HEADERS).json()["materials"][0]["active_revision"] is None
        assert ask(client, course, "有什么资料？")["refusal_reason"] == "no_candidates"


def test_streamlit_switches_courses_and_opens_numbered_citation(course_app, monkeypatch):
    AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
    endpoint = "http://course-workbench.test"
    monkeypatch.setenv("COURSE_RAG_TOKEN", TOKEN)
    monkeypatch.setenv("COURSE_RAG_API_URL", endpoint)
    original = httpx.Client.request

    with TestClient(course_app) as client:
        course = create_course(client, "A 操作系统")
        _, material = create_material(client, course)
        assert upload(client, course, material, "notes.md", "页表记录课程映射。".encode()).status_code == 201

        def route_workbench_requests(self, method, url, **kwargs):
            if str(url).startswith(endpoint + "/"):
                return client.request(method, url.removeprefix(endpoint), **kwargs)
            return original(self, method, url, **kwargs)

        monkeypatch.setattr(httpx.Client, "request", route_workbench_requests)
        ui = AppTest.from_file(str(ROOT / "src/enterprise_rag/apps/course_rag/ui.py")).run()
        assert not ui.text_area and ui.info
        next(field for field in ui.text_input if field.label == "课程令牌").input(TOKEN).run()
        ui.text_area[0].input("页表记录什么？")
        next(button for button in ui.button if button.label == "查询课程").click().run()
        assert not ui.exception and not ui.error
        assert any(text.value.startswith("1. 页表") for text in ui.text)
        evidence_choice = next(box for box in ui.selectbox if box.label == "打开一条引用")
        assert evidence_choice.options[0].startswith("陈述 1 · 课程讲义")

        next(field for field in ui.text_input if field.label == "课程名称").input("B 空课程")
        next(field for field in ui.text_input if field.label == "学期").input("实验学期")
        next(button for button in ui.button if button.label == "创建课程").click().run()
        assert not ui.exception and not ui.error
        assert ui.title[0].value == "B 空课程"
        assert not ui.text
        ui.text_area[0].input("页表记录什么？")
        next(button for button in ui.button if button.label == "查询课程").click().run()
        assert any("证据" in message.value for message in ui.info)
        assert not ui.text

        next(box for box in ui.selectbox if box.label == "当前课程").select_index(0).run()
        assert any(text.value.startswith("1. 页表") for text in ui.text)
        assert not ui.exception and not ui.error


@pytest.mark.live
def test_real_course_rag_demo():
    dsn = os.environ.get("RAG_TEST_DATABASE_URL")
    if os.environ.get("RAG_RUN_LIVE") != "1" or not dsn:
        pytest.skip("Set RAG_RUN_LIVE=1 and RAG_TEST_DATABASE_URL for the model-backed CourseRAG demo")
    settings = Settings(database_url=dsn, app_id=f"test-course-{uuid4().hex}", collection_tokens={"operator": TOKEN},
                        retrieval_mode="hybrid", validation_mode="gate")
    if settings.reranker_dir is None:
        pytest.skip("Set RAG_RERANKER_DIR to the verified BGE artifacts")

    with TestClient(create_course_app(settings, token=TOKEN)) as client:
        course = create_course(client, "操作系统")
        _, pdf = create_material(client, course, "pdf")
        notes = ROOT / "data/samples/course_memory.pdf"
        assert upload(client, course, pdf, notes.name, notes.read_bytes()).status_code == 201
        network = create_course(client, "计算机网络")
        _, markdown = create_material(client, network)
        notes = ROOT / "data/course_rag/demo/network.md"
        assert upload(client, network, markdown, notes.name, notes.read_bytes()).status_code == 201

        answer = ask(client, course, "在 LRU 页面置换算法中，优先淘汰哪个页面？")
        assert answer["answer_status"] == "answered" and "最长时间" in answer["answer"]
        assert answer["retrieval"]["strategy"] == "hybrid_reranked"
        cite = next(item for item in answer["citations"] if item["support_status"] == "supported")
        evidence = next(item for item in answer["evidence"] if item["evidence_id"] == cite["evidence_id"])
        assert evidence["locator"]["page"] == 2
        path = citation_path(course, evidence)
        span = cite["quote_span"] or evidence["locator"]
        preview = client.get(path + "/preview", headers=HEADERS, params={"start": span["start"], "end": span["end"]})
        assert preview.status_code == 200 and preview.headers["X-Preview-Mode"] == "highlight"
        assert client.get(path.replace(course, network, 1), headers=HEADERS).status_code == 404

        empty = create_course(client, "空课程")
        assert ask(client, empty, "LRU 选择哪个页面？")["refusal_reason"] == "no_candidates"
        export = client.get("/evaluations/course-dev-20261120/export", headers=HEADERS)
        assert export.status_code == 200
        with ZipFile(BytesIO(export.content)) as bundle:
            assert bundle.testzip() is None
