"""The check as a LangGraph graph: routing, carrying on after a crash, and the review pause."""
import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.documents import parse_policy
from app.fakes import FakeEmbedder, FakeJudge, FakeReranker
from app.graph import _config, attach_graph, execute_run
from app.ingest import ingest_dir
from app.pipeline import Services
from app.store import MemoryStore


async def _svc(samples, escalate=None, **settings):
    s = replace(load_settings(), demo_mode=True, **settings)
    svc = await attach_graph(Services(s, MemoryStore(), FakeEmbedder(), FakeReranker(), FakeJudge(), escalate))
    await ingest_dir(svc, samples / "policies")
    v4 = samples / "changes" / "POL-007-v4.md"
    upload_id = await svc.store.save_upload(v4.name, parse_policy(v4.name, v4.read_bytes(), s.policy_id_pattern).body)
    run_id = await svc.store.create_run("POL-007", "Safety", upload_id, "t")
    return svc, run_id


def _status(result):
    out = {}
    for r in result["rows"]:
        if out.get(r["policy_id"]) != "Yes":
            out[r["policy_id"]] = r["status"]
    return out


def test_unsure_pairs_go_to_the_second_model_only_when_it_is_on(samples):
    async def flow(escalate):
        svc, run_id = await _svc(samples, escalate)
        await execute_run(svc, run_id)
        return svc, await svc.store.get_run(run_id)

    svc, run = asyncio.run(flow(None))
    assert run["result"]["counts"]["escalated"] == 0
    assert {j["stage"] for j in svc.store.judgements} == {"judge"}

    svc, run = asyncio.run(flow(FakeJudge()))
    unsure = sum(1 for j in svc.store.judgements if j["stage"] == "judge" and j["result"]["impacted"] == "unsure")
    assert unsure and run["result"]["counts"]["escalated"] == unsure
    assert sum(1 for j in svc.store.judgements if j["stage"] == "escalate") == unsure


def test_a_crash_carries_on_from_the_last_finished_step(samples):
    async def flow():
        svc, run_id = await _svc(samples)
        real, calls = svc.reranker.rerank, {"diff": 0, "rerank": 0}
        save_change = svc.store.save_change

        async def counted_save(*a):
            calls["diff"] += 1
            return await save_change(*a)

        async def flaky(*a):
            calls["rerank"] += 1
            if calls["rerank"] == 1:
                raise ConnectionError("reranker restarting")
            return await real(*a)

        svc.store.save_change, svc.reranker.rerank = counted_save, flaky
        with pytest.raises(ConnectionError):
            await execute_run(svc, run_id, final_attempt=False)
        saved = await svc.graph.aget_state(_config(run_id))
        assert saved.next == ("find_related",)
        changes_after_first = calls["diff"]
        await execute_run(svc, run_id)
        assert calls["diff"] == changes_after_first  # the diff was not redone
        return await svc.store.get_run(run_id)

    run = asyncio.run(flow())
    assert run["status"] == "done"
    assert _status(run["result"]) == {"SAF-112": "Yes", "SAF-130": "Yes", "SAF-150": "Maybe"}


def test_review_gate_pauses_until_a_person_decides(samples):
    async def flow(affected):
        svc, run_id = await _svc(samples, review_gate=True)
        await execute_run(svc, run_id)
        paused = dict(await svc.store.get_run(run_id))  # the memory store hands back its live record
        maybes = [r for r in paused["result"]["rows"] if r["status"] == "Maybe"]
        decisions = [{"policy_id": r["policy_id"], "clause": r["clause"], "affected": affected} for r in maybes]
        await execute_run(svc, run_id, resume={"decisions": decisions, "by": "Dana"})
        return paused, await svc.store.get_run(run_id)

    paused, run = asyncio.run(flow(False))
    assert paused["status"] == "needs_review" and "decide" in paused["progress"]
    assert run["status"] == "done" and _status(run["result"]) == {"SAF-112": "Yes", "SAF-130": "Yes"}
    assert "SAF-150" in {p["policy_id"] for p in run["result"]["not_impacted"]}
    assert run["result"]["counts"]["maybe"] == 0 and run["result"]["reviewed_by"] == "Dana"

    _, run = asyncio.run(flow(True))
    assert _status(run["result"])["SAF-150"] == "Yes" and run["result"]["counts"]["impacted"] == 3


def test_review_over_the_api(samples, tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("REVIEW_GATE", "1")
    monkeypatch.setenv("SAMPLES_DIR", str(samples))
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
    from app.main import app

    key = {"Authorization": "Bearer demo"}
    with TestClient(app) as client:
        with open(samples / "changes" / "POL-007-v4.md", "rb") as f:
            up = client.post("/uploads", files={"file": ("POL-007-v4.md", f, "text/markdown")}, headers=key).json()
        run_id = client.post("/runs", json={"head_policy": "POL-007", "subset": "Safety", "upload_id": up["upload_id"]},
                             headers=key).json()["run_id"]
        for _ in range(200):
            run = client.get(f"/runs/{run_id}", headers=key).json()
            if run["status"] in ("needs_review", "done", "failed"):
                break
            asyncio.run(asyncio.sleep(0.02))
        assert run["status"] == "needs_review", run
        item = run["review"][0]
        short = run_id[:8]
        assert client.get(f"/runs/{short}", headers=key).json()["run_id"] == run_id
        assert client.get(f"/runs/{short.upper()}", headers=key).status_code == 200
        for bad_code in (run_id[:7], run_id[:9], run_id[:12], run_id[:36 - 1], run_id[:8] + "-", run_id[:4] + "-" + run_id[5:8] + "0"):
            assert client.get(f"/runs/{bad_code}", headers=key).status_code == 404, bad_code  # exactly 8 hex or the full id
        assert client.post(f"/runs/{run_id[:9]}/review", headers=key, json={"decisions": [
            {"policy_id": item["policy_id"], "clause": item["clause"], "affected": False}]}).status_code == 404
        # other routes need the full id
        assert client.get(f"/runs/{short}/results", headers=key).status_code == 404
        assert client.post(f"/runs/{short}/files", headers=key, json={"formats": ["pdf"]}).status_code == 404
        assert client.get("/runs/ffffffff", headers=key).status_code == 404
        bad = client.post(f"/runs/{run_id}/review", json={"decisions": [{"policy_id": "HR-201", "clause": "1", "affected": True}]}, headers=key)
        assert bad.status_code == 422
        ok = client.post(f"/runs/{short}/review", headers=key, json={
            "decisions": [{"policy_id": i["policy_id"], "clause": i["clause"], "affected": False} for i in run["review"]],
            "reviewed_by": "Dana"})
        assert ok.status_code == 202
        for _ in range(200):
            run = client.get(f"/runs/{run_id}", headers=key).json()
            if run["status"] == "done":
                break
            asyncio.run(asyncio.sleep(0.02))
        assert run["status"] == "done" and run["counts"]["maybe"] == 0
        result = client.get(f"/runs/{run_id}/results", headers=key).json()
        assert (item["policy_id"], item["clause"]) not in {(r["policy_id"], r["clause"]) for r in result["rows"]}
        assert client.post(f"/runs/{run_id}/review", json={"decisions": [{"policy_id": "x", "clause": "1", "affected": True}]},
                           headers=key).status_code == 409


def test_ambiguous_check_code_and_status_rules(samples, tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("SAMPLES_DIR", str(samples))
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
    from app.main import app

    key = {"Authorization": "Bearer demo"}
    body = {"decisions": [{"policy_id": "X-1", "clause": "1", "affected": True}]}
    with TestClient(app) as client:
        runs = app.state.svc.store.runs
        base = {"progress": "", "error": None, "result": {"rows": []}}
        for n, status in ((1, "needs_review"), (2, "needs_review"), (3, "done"), (4, "queued")):
            i = f"abcdef1{n}-0000-4000-8000-00000000000{n}"
            runs[i] = {"id": i, "status": status, **base}
        runs["abcdef12-0000-4000-8000-000000000002"]["status"] = "done"   # abcdef11 is now the only paused one
        runs["abcdef12-0000-4000-8000-000000000002"]["id"] = "abcdef12-0000-4000-8000-000000000002"
        # a short code reads any status, but review resolves it only among paused checks
        assert client.get("/runs/abcdef14", headers=key).status_code == 200
        assert client.post("/runs/abcdef13/review", headers=key, json=body).status_code == 404  # done
        assert client.post("/runs/abcdef14/review", headers=key, json=body).status_code == 404  # queued
        assert client.post("/runs/abcdef11/review", headers=key, json=body).status_code == 422  # found: X-1 not in a result
        # two paused checks with the same code: conflict, for both routes
        runs["abcdef11-1111-4000-8000-000000000009"] = {"id": "abcdef11-1111-4000-8000-000000000009", "status": "needs_review", **base}
        r = client.get("/runs/abcdef11", headers=key)
        assert r.status_code == 409 and "longer check number" in r.json()["detail"]
        assert client.post("/runs/abcdef11/review", headers=key, json=body).status_code == 409
        assert client.get("/runs/abcdef11-0000-4000-8000-000000000001", headers=key).status_code == 200
