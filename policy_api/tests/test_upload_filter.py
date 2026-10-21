"""The Open WebUI upload filter against the real API (demo mode), without Open WebUI."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

FILTER = Path(__file__).resolve().parents[2] / "openwebui" / "upload_filter.py"


@pytest.fixture
def filt(samples, tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("SAMPLES_DIR", str(samples))
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
    from app.main import app

    spec = importlib.util.spec_from_file_location("upload_filter", FILTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    f = mod.Filter()
    f.valves.UPLOAD_DIR = str(tmp_path)
    records = {}

    async def record(file_id):
        return records.get(file_id)

    f._record = record
    f.records = records
    with TestClient(app):
        f._client = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://policy-api",
                                              headers={"Authorization": "Bearer demo"}, timeout=60)
        yield f


USER = {"id": "u1", "role": "user"}


def _attach(filt, path: Path, name: str, owner="u1"):
    filt.records["f1"] = SimpleNamespace(id="f1", user_id=owner, filename=name, path=str(path))
    # The path and name the browser sends are ignored; only the id counts.
    return {"messages": [{"role": "user", "content": "Which Safety policies does this affect?"}],
            "files": [{"type": "file", "id": "f1", "file": {"id": "f1", "path": "/etc/passwd", "filename": "x"}}]}


def test_upload_note_names_the_policy(filt, samples, tmp_path):
    p = tmp_path / "x.md"
    p.write_bytes((samples / "changes" / "POL-007-v4.md").read_bytes())
    body = asyncio.run(filt.inlet(_attach(filt, p, "POL-007 v4 [draft](http://evil).md"), __user__=USER))
    msg = body["messages"][-1]["content"]
    assert msg.startswith("Which Safety policies")
    assert "upload id " in msg and "new version of POL-007" in msg
    assert "](http" not in msg  # file names lose their markup
    assert body["files"] == []
    assert filt.file_handler is True


def test_unreadable_file_is_explained_and_retried(filt, tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("just some words")
    body = asyncio.run(filt.inlet(_attach(filt, p, "note.txt"), __user__=USER))
    assert "could not be used: No numbered sections" in body["messages"][-1]["content"]
    assert filt._done == {}


def test_no_files_leaves_the_message_alone(filt):
    body = {"messages": [{"role": "user", "content": "hello"}]}
    assert asyncio.run(filt.inlet(dict(body)))["messages"][0]["content"] == "hello"


def test_other_peoples_and_outside_files_are_refused(filt, samples, tmp_path):
    p = tmp_path / "x.md"
    p.write_bytes((samples / "changes" / "POL-007-v4.md").read_bytes())
    body = asyncio.run(filt.inlet(_attach(filt, p, "x.md", owner="someone-else"), __user__=USER))
    assert "could not be found" in body["messages"][-1]["content"]
    outside = samples / "changes" / "POL-007-v4.md"  # a real file, but not in the upload folder
    body = asyncio.run(filt.inlet(_attach(filt, outside, "x.md"), __user__=USER))
    assert "could not be found" in body["messages"][-1]["content"]
    assert filt._done == {}
