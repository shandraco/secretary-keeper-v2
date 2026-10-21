"""The Open WebUI tool's logic against the real API (demo mode), without Open WebUI."""
import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

TOOL = Path(__file__).resolve().parents[2] / "openwebui" / "policy_impact_tool.py"


@pytest.fixture
def tool(samples, tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("SAMPLES_DIR", str(samples))
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://dl.test")
    from app.main import app

    spec = importlib.util.spec_from_file_location("policy_tool", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    t = mod.Tools()
    t.valves.API_KEY = "demo"
    t.valves.POLL_SECONDS = 0.05
    t.valves.UPLOAD_DIR = str(tmp_path)
    with TestClient(app):  # runs startup: services and the queue worker
        t._client = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://policy-api",
                                              headers={"Authorization": "Bearer demo"}, timeout=60)
        yield t


def _upload(samples, tmp_path):
    p = tmp_path / "POL-007-v4.md"
    p.write_bytes((samples / "changes" / "POL-007-v4.md").read_bytes())
    return [{"name": p.name, "file": {"path": str(p)}}]


def test_check_then_reports(tool, samples, tmp_path):
    out = asyncio.run(tool.check_policy_change("safety", "", __files__=_upload(samples, tmp_path)))
    assert "2 of the 5 Safety policies need updating" in out
    run_id = out.split("check number ")[1].split(".")[0]
    files = asyncio.run(tool.make_check_files(run_id, "pdf, word, charts"))
    assert files.startswith("Show the user these files")
    assert files.count("📎") == 2 and files.count("![") == 3 and "http://dl.test/exports/" in files
    assert "must be from" in asyncio.run(tool.make_check_files(run_id, "zip"))
    again = asyncio.run(tool.get_check_result(run_id))
    assert "2 of the 5 Safety policies" in again


def test_compare_versions_and_with_attachment(tool, samples, tmp_path):
    out = asyncio.run(tool.compare_policies("POL-007", __files__=_upload(samples, tmp_path), file_format="pdf"))
    assert "Comparison" in out and ".pdf?" in out
    asked = asyncio.run(tool.compare_policies("POL-007"))
    assert asked.startswith("Ask the user what to compare it with")
    two = asyncio.run(tool.compare_policies("Maintenance Access", "Contractor Safety", file_format="excel"))
    assert ".xlsx?" in two


def test_make_document_and_label_escaping(tool):
    spec = {"title": "[x](http://evil) summary", "sections": [{"paragraphs": ["Two policies need updating."]}]}
    out = asyncio.run(tool.make_document(json.dumps(spec), "word"))
    assert "\\[x\\]\\(http://evil\\)" in out and ".docx?" in out
    assert "not valid JSON" in asyncio.run(tool.make_document("{nope", "pdf"))
    bad = asyncio.run(tool.make_document(json.dumps({"title": "t", "sections": [{"chart": {"type": "radar"}}]}), "png"))
    assert bad.startswith("The content could not be used")


def test_check_with_upload_id_from_the_filter(tool, samples):
    async def upload():
        async with tool._client() as c:
            data = (samples / "changes" / "POL-007-v4.md").read_bytes()
            return (await c.post("/uploads", files={"file": ("POL-007-v4.md", data)})).json()["upload_id"]

    upload_id = asyncio.run(upload())
    asked = asyncio.run(tool.check_policy_change("safety", upload_id=upload_id))
    assert asked.startswith("Ask the user which policy")
    out = asyncio.run(tool.check_policy_change("safety", "POL-007", upload_id=upload_id))
    assert "2 of the 5 Safety policies need updating" in out


def test_attachment_outside_the_upload_folder_is_ignored(tool, samples):
    outside = [{"name": "x.md", "file": {"path": str(samples / "changes" / "POL-007-v4.md")}}]
    assert tool._attached_file(outside) is None


def test_review_pause_in_the_chat(tool, samples, tmp_path, monkeypatch):
    from app.main import app

    svc = app.state.svc
    monkeypatch.setattr(svc, "settings", __import__("dataclasses").replace(svc.settings, review_gate=True))
    out = asyncio.run(tool.check_policy_change("safety", "", __files__=_upload(samples, tmp_path)))
    assert out.startswith("The check is almost done") and "SAF-150" in out
    import re
    assert re.search(r"decide [0-9a-f]{8} ", out) and not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}", out)
    assert "no tool for it" in out and "decide " in out and "<yes|no>" in out
    assert not hasattr(tool, "decide_sections")
