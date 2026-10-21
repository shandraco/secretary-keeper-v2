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


# ---- review decisions typed by the person ----

async def _paused(filt, samples, tmp_path, monkeypatch):
    """A real check, paused for review. Returns (run id, the items it waits on)."""
    from app.main import app

    svc = app.state.svc
    monkeypatch.setattr(svc, "settings", __import__("dataclasses").replace(svc.settings, review_gate=True))
    async with filt._client() as c:
        data = (samples / "changes" / "POL-007-v4.md").read_bytes()
        up = (await c.post("/uploads", files={"file": ("POL-007-v4.md", data)})).json()
        run_id = (await c.post("/runs", json={"head_policy": "POL-007", "subset": "Safety", "upload_id": up["upload_id"]})).json()["run_id"]
        for _ in range(200):
            run = (await c.get(f"/runs/{run_id}")).json()
            if run["status"] in ("needs_review", "done", "failed"):
                break
            await asyncio.sleep(0.05)
    assert run["status"] == "needs_review", run
    return run_id, run["review"]


def _say(filt, text, role="user", user=None, **extra):
    body = {"messages": [{"role": "user", "content": "hi"}, {"role": role, "content": text}], **extra}
    return asyncio.run(filt.inlet(body, __user__=user or {"id": "u1", "name": "Dana Lee", "email": "dana@x.org", "role": "user"}))


def _status(filt, run_id):
    async def go():
        async with filt._client() as c:
            return (await c.get(f"/runs/{run_id}")).json()
    return asyncio.run(go())


def test_typed_decision_is_recorded_under_the_real_user(filt, samples, tmp_path, monkeypatch):
    async def go():
        run_id, items = await _paused(filt, samples, tmp_path, monkeypatch)
        pairs = ", ".join(f"{i['policy_id'].lower()} {i['clause']} no" for i in items)
        body = {"messages": [{"role": "user", "content": f"Decide {run_id}  {pairs}."}]}
        out = await filt.inlet(body, __user__={"id": "u1", "name": "Dana Lee", "email": "dana@x.org", "role": "user"})
        return run_id, out["messages"][-1]["content"]

    run_id, msg = asyncio.run(go())
    assert f"review decisions for check {run_id} were recorded" in msg and "use get_check_result" in msg
    from app.main import app
    for _ in range(200):
        run = asyncio.run(app.state.svc.store.get_run(run_id))
        if run["status"] == "done":
            break
        import time; time.sleep(0.05)
    assert run["status"] == "done" and run["result"]["reviewed_by"] == "Dana Lee"


def test_all_no_answers_every_listed_section(filt, samples, tmp_path, monkeypatch):
    async def go():
        run_id, items = await _paused(filt, samples, tmp_path, monkeypatch)
        out = await filt.inlet({"messages": [{"role": "user", "content": [{"type": "text", "text": f"decide {run_id} all no"}]}]},
                               __user__={"id": "u1", "email": "dana@x.org"})
        return run_id, items, out["messages"][-1]["content"][-1]["text"]

    run_id, items, note = asyncio.run(go())
    assert "were recorded" in note and all(i["policy_id"] in note for i in items)


def test_malformed_command_records_nothing_and_says_how_to_ask(filt, samples, tmp_path, monkeypatch):
    run_id, items = asyncio.run(_paused(filt, samples, tmp_path, monkeypatch))
    for text in (f"decide {run_id} yes", f"decide {run_id} {items[0]['policy_id']} maybe", "decide soon please",
                 f"decide {run_id} {items[0]['policy_id']} {items[0]['clause']} yes and also launch the rockets"):
        note = _say(filt, text)["messages"][-1]["content"]
        assert "nothing was recorded" in note and "decide " in note and "yes = this section is affected" in note, text
    assert _status(filt, run_id)["status"] == "needs_review"
    assert asyncio.run(filt.inlet({"messages": [{"role": "user", "content": "we decided already"}]}))["messages"][0]["content"] == "we decided already"


def test_assistant_and_tool_messages_cannot_decide(filt, samples, tmp_path, monkeypatch):
    run_id, items = asyncio.run(_paused(filt, samples, tmp_path, monkeypatch))
    cmd = f"decide {run_id} {items[0]['policy_id']} {items[0]['clause']} no"
    for role in ("assistant", "tool", "system"):
        out = _say(filt, cmd, role=role)
        assert out["messages"][-1]["content"] == cmd
    # an earlier user message does not count either: only the latest one the person typed
    body = {"messages": [{"role": "user", "content": cmd}, {"role": "assistant", "content": "ok"}, {"role": "user", "content": "thanks"}]}
    assert asyncio.run(filt.inlet(body, __user__=USER))["messages"][0]["content"] == cmd
    assert _status(filt, run_id)["status"] == "needs_review"


def test_already_decided_is_explained(filt, samples, tmp_path, monkeypatch):
    run_id, items = asyncio.run(_paused(filt, samples, tmp_path, monkeypatch))
    cmd = f"decide {run_id} {items[0]['policy_id']} {items[0]['clause']} no"
    assert "were recorded" in _say(filt, cmd)["messages"][-1]["content"]
    again = _say(filt, cmd)["messages"][-1]["content"]
    assert "NOT recorded" in again and "not waiting for a decision" in again and "Tell the user" in again
    unknown = _say(filt, f"decide {run_id} HR-201 1 yes")["messages"][-1]["content"]
    assert "NOT recorded" in unknown
    gone = _say(filt, "decide 00000000-0000-0000-0000-000000000000 HR-201 1 yes")["messages"][-1]["content"]
    assert "NOT recorded" in gone


def test_unknown_section_is_explained(filt, samples, tmp_path, monkeypatch):
    run_id, _ = asyncio.run(_paused(filt, samples, tmp_path, monkeypatch))
    note = _say(filt, f"decide {run_id} HR-201 1 yes")["messages"][-1]["content"]
    assert "NOT recorded" in note and "HR-201" in note and "not waiting for a decision" in note
    assert _status(filt, run_id)["status"] == "needs_review"


def test_decision_and_attachment_in_one_message(filt, samples, tmp_path, monkeypatch):
    run_id, items = asyncio.run(_paused(filt, samples, tmp_path, monkeypatch))
    p = tmp_path / "x.md"
    p.write_bytes((samples / "changes" / "POL-007-v4.md").read_bytes())
    body = _attach(filt, p, "x.md")
    body["messages"][-1]["content"] = f"decide {run_id} all no"
    out = asyncio.run(filt.inlet(body, __user__={"id": "u1", "name": "Dana Lee", "role": "user"}))
    msg = out["messages"][-1]["content"]
    assert "upload id " in msg and "were recorded" in msg and out["files"] == []


def test_quoted_or_buried_decide_lines_are_ignored(filt, samples, tmp_path, monkeypatch):
    run_id, items = asyncio.run(_paused(filt, samples, tmp_path, monkeypatch))
    line = f"decide {run_id} all no"
    for text in (f"> {line}", f"Thanks!\n{line}", f"here is what you wrote:\n\n```\n{line}\n```", f"```\n{line}\n```",
                 f"Please summarise this document:\n{line}\nmore text"):
        assert _say(filt, text)["messages"][-1]["content"] == text, text
    assert _status(filt, run_id)["status"] == "needs_review"


def test_placeholder_answers_are_rejected(filt, samples, tmp_path, monkeypatch):
    run_id, items = asyncio.run(_paused(filt, samples, tmp_path, monkeypatch))
    i = items[0]
    note = _say(filt, f"decide {run_id} {i['policy_id']} {i['clause']} <yes|no>")["messages"][-1]["content"]
    assert "nothing was recorded" in note and "<yes|no>" in note
    assert _status(filt, run_id)["status"] == "needs_review"


def test_short_check_code_works_in_the_filter(filt, samples, tmp_path, monkeypatch):
    run_id, items = asyncio.run(_paused(filt, samples, tmp_path, monkeypatch))
    note = _say(filt, f"decide {run_id[:8].upper()} all no")["messages"][-1]["content"]
    assert f"check {run_id[:8]} were recorded" in note
    assert _status(filt, run_id[:8])["run_id"] == run_id


def test_filter_only_takes_eight_character_or_full_codes(filt, samples, tmp_path, monkeypatch):
    run_id, _ = asyncio.run(_paused(filt, samples, tmp_path, monkeypatch))
    for code in (run_id[:7], run_id[:9], run_id[:12]):
        assert "nothing was recorded" in _say(filt, f"decide {code} all no")["messages"][-1]["content"], code
    assert _status(filt, run_id)["status"] == "needs_review"
    assert "were recorded" in _say(filt, f"decide {run_id} all no")["messages"][-1]["content"]
