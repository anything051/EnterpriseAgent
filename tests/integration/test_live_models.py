import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from enterprise_rag.api.main import create_app
from enterprise_rag.config import Settings

SAMPLES = Path(__file__).resolve().parents[2] / "data" / "samples"
TOKEN = "local-real-model-smoke-test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.mark.live
def test_configured_models_answer_with_both_source_types():
    if os.environ.get("RAG_RUN_LIVE") != "1":
        pytest.skip("Set RAG_RUN_LIVE=1 to exercise the configured models")
    settings = Settings(app_id=f"live-{uuid4().hex}", collection_tokens={"demo": TOKEN})
    with TestClient(create_app(settings)) as client:
        cases = [
            ("course_memory.pdf", "根据课程笔记，LRU 选择哪个页面？", 2, "最长时间"),
            ("enterprise_inventory.md", "青禾公司发现盘点差异后，值班人员多久内登记？",
             None, "24"),
        ]
        for filename, question, page, expected in cases:
            body = (SAMPLES / filename).read_bytes()
            first = client.post("/documents", headers=HEADERS,
                                data={"collection_id": "demo"}, files={"file": (filename, body)})
            assert first.status_code == 201, first.text
            repeated = client.post("/documents", headers=HEADERS,
                                   data={"collection_id": "demo"}, files={"file": (filename, body)})
            assert repeated.status_code == 200
            assert repeated.json() == first.json()

            response = client.post("/questions", headers=HEADERS,
                                   json={"collection_id": "demo", "question": question, "k": 10})
            assert response.status_code == 200, response.text
            answer = response.json()
            assert answer["answer_status"] == "answered", answer
            assert expected in answer["answer"], answer
            evidence = next(item for item in answer["evidence"] if item["source_id"] == first.json()["document_id"])
            locator = evidence["locator"]
            assert locator["page"] == page
            original = client.get(
                f"/documents/{evidence['source_id']}/versions/{locator['version_id']}/units/{locator['unit_id']}",
                headers=HEADERS,
            )
            assert original.status_code == 200
            assert original.json()["source_text"][locator["start"]:locator["end"]] == evidence["content"]
        refused = client.post("/questions", headers=HEADERS,
                              json={"collection_id": "demo", "question": "青禾公司的总经理是谁？"})
        assert refused.status_code == 200
        assert refused.json()["answer_status"] == "insufficient_evidence", refused.text
