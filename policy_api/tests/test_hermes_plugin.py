"""The Hermes plugin's tools against the real API (demo mode), without Hermes."""
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PLUGIN = Path(__file__).resolve().parents[2] / "hermes" / "plugins" / "secretary" / "__init__.py"


@pytest.fixture
def plugin(samples, tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("SAMPLES_DIR", str(samples))
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://dl.test")
    from app.main import app

    spec = importlib.util.spec_from_file_location("secretary_plugin", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.POLL_SECONDS = 0.05
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer demo"
        mod._client = lambda: _Borrowed(client)
        mod.client = client
        yield mod


class _Borrowed:
    """The test client, used in `with` blocks without being closed."""
    def __init__(self, c):
        self.c = c

    def __enter__(self):
        return self.c

    def __exit__(self, *exc):
        return False


def test_registers_every_tool_with_a_valid_schema(plugin):
    seen = []

    class Ctx:
        def register_tool(self, **kw):
            seen.append(kw)

    plugin.register(Ctx())
    names = {"search_policies", "read_policy", "list_policy_groups", "find_policy", "check_policy_change",
             "get_check_result", "make_check_files", "compare_policies", "make_document"}
    assert {k["name"] for k in seen} == names
    for k in seen:
        assert k["toolset"] == "secretary" and k["schema"]["name"] == k["name"]
        assert k["schema"]["parameters"]["type"] == "object"
        assert set(k["schema"]["parameters"]["required"]) <= set(k["schema"]["parameters"]["properties"])
        json.dumps(k["schema"])


def test_search_and_read_answer_from_the_database(plugin):
    out = plugin.search_policies({"question": "contractor permit to work"})
    assert "[SAF-" in out and "<<<" in out and "cite each fact" in out
    assert "Pass the user's question" in plugin.search_policies({})
    assert "no policy group" in plugin.search_policies({"question": "x y", "policy_group": "Nope"}).lower()
    text = plugin.read_policy({"policy_id": "POL-007", "section": "4"})
    assert "version" in text and "§4" in text
    assert "Ask the user which policy" in plugin.read_policy({"policy_id": "zzz"})


def test_check_with_an_upload_then_files(plugin, samples):
    with open(samples / "changes" / "POL-007-v4.md", "rb") as f:
        up = plugin.client.post("/uploads", files={"file": ("POL-007-v4.md", f, "text/markdown")}).json()
    assert "Ask the user which policy changed" in plugin.check_policy_change({"policy_group": "safety"})
    out = plugin.check_policy_change({"policy_group": "safety", "head_policy": "POL-007", "upload_id": up["upload_id"]})
    assert out.startswith("Show the user this answer") and "2 of the 5 Safety policies need updating" in out
    run_id = out.split("check number ")[1].split(".")[0]
    files = plugin.make_check_files({"check_number": run_id, "formats": ["pdf", "word"]})
    assert files.count("📎") == 2 and "http://dl.test/exports/" in files
    assert "must be from" in plugin.make_check_files({"check_number": run_id, "formats": ["zip"]})
    assert "2 of the 5" in plugin.get_check_result({"check_number": run_id})
    cmp = plugin.compare_policies({"first_policy": "POL-007", "upload_id": up["upload_id"], "file_format": "pdf"})
    assert ".pdf?" in cmp


def test_make_document_and_lookups(plugin):
    out = plugin.make_document({"content": {"title": "[x](http://evil)", "sections": [{"paragraphs": ["Hi."]}]}, "file_format": "word"})
    assert "\\[x\\]\\(http://evil\\)" in out and ".docx?" in out
    assert "not valid JSON" in plugin.make_document({"content": "{nope"})
    assert "Safety" in plugin.list_policy_groups({})
    assert "POL-007" in plugin.find_policy({"name_or_id": "POL-007"})


def test_service_down_is_explained(plugin, monkeypatch):
    import httpx

    def broken():
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(plugin, "_client", broken)
    assert plugin.search_policies({"question": "anything"}).startswith("The policy service did not respond")


def test_review_pause_through_hermes_tools(plugin, samples, monkeypatch):
    import dataclasses

    from app.main import app

    svc = app.state.svc
    monkeypatch.setattr(svc, "settings", dataclasses.replace(svc.settings, review_gate=True))
    with open(samples / "changes" / "POL-007-v4.md", "rb") as f:
        up = plugin.client.post("/uploads", files={"file": ("POL-007-v4.md", f, "text/markdown")}).json()
    out = plugin.check_policy_change({"policy_group": "Safety", "head_policy": "POL-007", "upload_id": up["upload_id"]})
    assert out.startswith("The check is almost done") and "SAF-150" in out
    assert "no tool for it" in out and "decide " in out and "<yes|no>" in out
    import re
    assert re.search(r"decide [0-9a-f]{8} ", out) and not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}", out)
    assert not hasattr(plugin, "decide_sections") and not hasattr(plugin, "REVIEW_TOOLS")
