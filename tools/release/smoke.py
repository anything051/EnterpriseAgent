"""Exercise the two release APIs and verify restored evidence using the same IDs."""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
from time import perf_counter
from uuid import uuid4
from zipfile import ZipFile

import httpx

ROOT = Path(__file__).resolve().parents[2]
RELEASE_TASK = {
    "kind": "overview", "question": "汇总指定日期和区域的销售，查询指定商品库存，并引用盘点差异登记时限。",
    "day": "2026-06-01", "region": "华东", "sku": "A100",
}


def canonical_digest(data) -> str:
    return sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def local_url(name: str, default: int) -> str:
    return f"http://127.0.0.1:{int(os.environ.get(name, default))}"


def authorized(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def checked(client, method: str, path: str, headers=None, status: int = 200, **kwargs):
    result = client.request(method, path, headers=headers, **kwargs)
    error = None
    if result.headers.get("content-type", "").startswith("application/json") and result.is_error:
        error = result.json().get("error", {}).get("code")
    expected = status if isinstance(status, tuple) else (status,)
    if result.status_code not in expected:
        raise ValueError(f"Expected HTTP {status}, got {result.status_code} ({error})")
    if not (result.headers.get("cache-control") == "private, no-store"
            and result.headers.get("x-content-type-options") == "nosniff"
            and result.headers.get("x-request-id")):
        raise ValueError("API cache, content type or request-ID response header missing")
    return result


def check_value(condition, message: str) -> None:
    if not condition:
        raise ValueError(message)


def publish(client, auth, title: str, asset: Path | None) -> str:
    course = checked(client, "POST", "/courses", auth, 201, json={"title": title, "term": "V1.0 验收"}).json()
    prefix = f"/courses/{course['id']}"
    if asset is None:
        return prefix
    chapter = checked(client, "POST", prefix + "/chapters", auth, 201, json={"title": "验收材料"}).json()
    material = checked(client, "POST", prefix + "/materials", auth, 201,
                       json={"title": asset.stem, "chapter_id": chapter["id"], "format": asset.suffix[1:]}).json()
    checked(client, "POST", f"{prefix}/materials/{material['id']}/revisions", auth, 201,
            files={"file": (asset.name, asset.read_bytes())})
    return prefix


def course_evidence(client, auth, course, question, expected, page):
    answer = checked(client, "POST", course + "/questions", auth, json={"question": question}).json()
    check_value(answer["answer_status"] == "answered" and expected in answer["answer"], "Expected grounded course answer")
    candidates = {item["evidence_id"]: item for item in answer["evidence"]}
    for cite in answer["citations"]:
        item = candidates[cite["evidence_id"]]
        if cite["support_status"] == "supported" and expected in item["content"]:
            break
    else:
        raise ValueError("Expected evidence in a supported citation")
    loc = item["locator"]
    check_value(loc["page"] == page, "Incorrect physical page for cited evidence")
    span = cite["quote_span"] or loc
    suffix = f"/sources/{item['source_id']}/{loc['version_id']}/{loc['unit_id']}"
    path = course + suffix
    source = checked(client, "GET", path, auth).json()
    text = source["source_text"]
    check_value(expected in text[span["start"]:span["end"]], "Original source span differs from the citation")
    geometry = None
    if page:
        preview = checked(client, "GET", path + "/preview", auth, params={"start": span["start"], "end": span["end"]})
        check_value(preview.content.startswith(b"\x89PNG\r\n\x1a\n") and preview.headers.get("x-preview-mode") == "highlight",
                    "Physical PDF highlight did not render")
        geometry = {"start": span["start"], "end": span["end"], "sha256": sha256(preview.content).hexdigest()}
    else:
        check_value(isinstance(source["start_line"], int) and source["start_line"] >= 1, "Markdown line location missing")
    return {"path": path, "source_suffix": suffix, "source_sha256": sha256(text.encode()).hexdigest(),
            "page": page, "start_line": source["start_line"], "preview": geometry,
            "strategy": answer["retrieval"]["strategy"], "request_id": answer["request_id"]}


def enterprise_report(client, auth, viewer):
    checked(client, "POST", "/agent/demo/policy", auth, (200, 201))
    thread = checked(client, "POST", "/agent/threads", auth, 201).json()["thread_id"]
    identifier = str(uuid4())
    path = f"/agent/threads/{thread}/runs/{identifier}"
    result = checked(client, "PUT", path, auth, json=RELEASE_TASK).json()
    report = result["report"]
    check_value(report is not None and report["status"] == "completed", "Enterprise workflow is incomplete")
    check_value([step["name"] for step in report["agent_steps"]]
                == ["supervisor", "data_agent", "knowledge_agent", "report_agent"], "Incorrect role sequence")
    evidence = {item["source_type"]: item for item in report["evidence"]}
    check_value(set(evidence) == {"sql", "api", "document"}, "Missing enterprise evidence types")
    check_value(evidence["sql"]["data"] == {"order_count": 2, "units": 12, "revenue_cents": 145000},
                "SQL reference result differs")
    inventory = evidence["api"]["data"]
    check_value(inventory["on_hand"] == 18 and inventory["reserved"] == 5 and inventory["sku"] == "A100",
                "HTTP inventory reference result differs")
    ids = {item["evidence_id"] for item in evidence.values()}
    check_value(ids == {step["evidence_id"] for step in report["audit"] if step["status"] == "ok"}
                == {section["evidence_id"] for section in report["sections"]}, "Incomplete evidence links")
    doc = evidence["document"]["document"]
    loc = doc["locator"]
    suffix = f"/sources/{doc['source_id']}/{loc['version_id']}/{loc['unit_id']}"
    source = checked(client, "GET", "/agent" + suffix, viewer).json()["source_text"]
    check_value("24 小时" in doc["content"] and source[loc["start"]:loc["end"]] == doc["content"],
                "Policy source could not be recomputed")
    replay = checked(client, "POST", path + "/resume", auth).json()
    check_value(replay == result, "Terminal replay changed the enterprise report")
    checked(client, "GET", f"/agent/threads/{thread}/runs", viewer, 404)
    return {"path": path, "source_suffix": suffix, "source_sha256": sha256(source.encode()).hexdigest(),
            "report_sha256": canonical_digest(result), "model_calls": report["model_calls"],
            "input_tokens": report["input_tokens"], "output_tokens": report["output_tokens"]}


def run(restore_from: Path | None) -> dict:
    output = {"created_at": datetime.now(timezone.utc).isoformat(), "cases": [], "proof": {},
              "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
              "dirty_tracked": bool(subprocess.check_output(["git", "status", "--porcelain", "-uno"], text=True).strip()),
              "image_id": os.environ.get("RELEASE_IMAGE_ID"),
              "mode": "restore" if restore_from else "live"}

    def case(name, check):
        started = perf_counter()
        try:
            proof = check()
        except (ValueError, KeyError, httpx.HTTPError) as error:
            # HTTP exceptions can include URLs. Record only the transport exception type.
            detail = str(error) if isinstance(error, ValueError) else type(error).__name__
            output["cases"].append({"case": name, "status": "failed", "detail": detail})
        else:
            output["cases"].append({"case": name, "status": "passed", "elapsed_ms": round((perf_counter() - started) * 1000, 2)})
            if proof is not None:
                output["proof"][name] = proof
            return proof

    course_auth = authorized(os.environ["COURSE_RAG_TOKEN"])
    analyst = authorized(os.environ["ENTERPRISE_ANALYST_TOKEN"])
    viewer = authorized(os.environ["ENTERPRISE_VIEWER_TOKEN"])
    with (
        httpx.Client(base_url=local_url("COURSE_RELEASE_API_PORT", 8001), timeout=165, trust_env=False) as course,
        httpx.Client(base_url=local_url("ENTERPRISE_RELEASE_API_PORT", 8002), timeout=165, trust_env=False) as agent,
    ):
        def health():
            for client in (course, agent):
                checked(client, "GET", "/readyz")
            for env, port in (("COURSE_RELEASE_UI_PORT", 8501), ("ENTERPRISE_RELEASE_UI_PORT", 8502)):
                check_value(httpx.get(local_url(env, port) + "/_stcore/health", timeout=5, trust_env=False).status_code == 200,
                            "A Streamlit UI was not healthy")
        case("two_api_and_ui_healthchecks", health)

        def auth_boundaries():
            checked(course, "GET", "/courses", analyst, 401)
            checked(agent, "GET", "/agent/session", course_auth, 401)
            checked(agent, "GET", "/agent/threads", None, 401)
            checked(course, "GET", "/healthz", {"Host": "untrusted.invalid"}, 400)
        case("cross_app_credentials_and_host_denied", auth_boundaries)

        if restore_from:
            original = json.loads(restore_from.read_text(encoding="utf-8"))["proof"]
            for name in ("course_pdf", "course_markdown"):
                def restored_course(name=name):
                    ref = original[name]
                    check_value(ref["path"].startswith("/courses/"), "Unexpected stored course reference")
                    text = checked(course, "GET", ref["path"], course_auth).json()["source_text"]
                    check_value(sha256(text.encode()).hexdigest() == ref["source_sha256"], "Restored original source differs")
                    if ref["preview"]:
                        params = {key: ref["preview"][key] for key in ("start", "end")}
                        png = checked(course, "GET", ref["path"] + "/preview", course_auth, params=params)
                        check_value(sha256(png.content).hexdigest() == ref["preview"]["sha256"], "Restored PDF preview differs")
                case("restored_" + name, restored_course)

            def restored_index():
                ref = original["course_pdf"]
                prefix = ref["path"].split("/sources/", 1)[0]
                check_value(prefix.startswith("/courses/"), "Unexpected stored course reference")
                proof = course_evidence(course, course_auth, prefix,
                                        "在 LRU 页面置换算法中，优先淘汰哪个页面？", "最长时间", 2)
                check_value(proof["source_sha256"] == ref["source_sha256"], "Restored index cited a different source")
                return proof
            case("restored_index_answers_existing_course", restored_index)

            def restored_enterprise():
                ref = original["enterprise_report"]
                check_value(ref["path"].startswith("/agent/threads/") and ref["source_suffix"].startswith("/sources/"),
                            "Unexpected stored enterprise reference")
                stored = checked(agent, "POST", ref["path"] + "/resume", analyst).json()
                check_value(canonical_digest(stored) == ref["report_sha256"], "Restored report differs")
                checked(agent, "POST", ref["path"] + "/resume", viewer, 404)
                text = checked(agent, "GET", "/agent" + ref["source_suffix"], viewer).json()["source_text"]
                check_value(sha256(text.encode()).hexdigest() == ref["source_sha256"], "Restored policy source differs")
            case("restored_enterprise_report_and_role", restored_enterprise)
            return output

        pdf_course = case("import_pdf", lambda: publish(course, course_auth, "操作系统 " + uuid4().hex[:8],
                                                         ROOT / "data/samples/course_memory.pdf"))
        md_course = case("import_markdown", lambda: publish(course, course_auth, "网络 " + uuid4().hex[:8],
                                                             ROOT / "data/course_rag/demo/network.md"))
        if pdf_course:
            case("course_pdf", lambda: course_evidence(course, course_auth, pdf_course,
                 "在 LRU 页面置换算法中，优先淘汰哪个页面？", "最长时间", 2))
        if md_course:
            case("course_markdown", lambda: course_evidence(course, course_auth, md_course,
                 "练习链路的 IP MTU 是多少字节？", "1500", None))

        def refusal():
            empty = publish(course, course_auth, "空课 " + uuid4().hex[:8], None)
            response = checked(course, "POST", empty + "/questions", course_auth, json={"question": "LRU 选择哪个页面？"})
            check_value(response.json()["refusal_reason"] == "no_candidates", "Empty course did not refuse")
        case("course_insufficient_evidence", refusal)

        def export():
            response = checked(course, "GET", "/evaluations/course-dev-20261120/export", course_auth)
            with ZipFile(BytesIO(response.content)) as bundle:
                check_value(bundle.testzip() is None and "samples.jsonl" in bundle.namelist(), "Invalid evaluation ZIP")
        case("course_evaluation_export", export)

        business_proof = case("enterprise_report", lambda: enterprise_report(agent, analyst, viewer))

        def no_business_as_viewer():
            report = checked(agent, "POST", "/agent/tasks", viewer, json=RELEASE_TASK).json()
            check_value(report["status"] == "denied" and report["model_calls"] == 0 and not report["evidence"],
                        "Viewer received business data or invoked a model")
        case("enterprise_viewer_denied", no_business_as_viewer)
        pdf = output["proof"].get("course_pdf")
        if pdf and md_course and business_proof:
            def source_boundaries():
                checked(course, "GET", md_course + pdf["source_suffix"], course_auth, 404)
                checked(course, "GET", pdf_course + business_proof["source_suffix"], course_auth, 404)
                checked(agent, "GET", "/agent" + pdf["source_suffix"], viewer, 404)
            case("cross_course_and_cross_application_sources_denied", source_boundaries)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--restore-from", type=Path)
    args = parser.parse_args()
    record = run(args.restore_from)
    args.output.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for item in record["cases"]:
        print(item["case"], item["status"], item.get("detail", ""))
    raise SystemExit(int(any(case["status"] != "passed" for case in record["cases"])))
