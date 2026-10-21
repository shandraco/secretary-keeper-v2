"""The job queue keeps checks alive across failures and restarts (in-memory store; Postgres in test_pgstore)."""
import asyncio
from dataclasses import replace

import pytest

from app import jobs
from app.config import load_settings
from app.fakes import FakeEmbedder, FakeJudge, FakeReranker
from app.ingest import ingest_dir
from app.pipeline import Services
from app.store import MemoryStore


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(jobs, "RETRY_BASE_S", 0.0)
    monkeypatch.setattr(jobs, "IDLE_POLL_S", 0.01)


async def _svc(samples, **settings):
    s = replace(load_settings(), demo_mode=True, job_lease_seconds=1, **settings)
    svc = Services(s, MemoryStore(), FakeEmbedder(), FakeReranker(), FakeJudge())
    await ingest_dir(svc, samples / "policies")
    from app.documents import parse_policy
    from app.ingest import ingest_doc

    v4 = samples / "changes" / "POL-007-v4.md"
    await ingest_doc(svc, parse_policy(v4.name, v4.read_bytes(), s.policy_id_pattern))
    return svc


async def _until(cond, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not await cond():
        if loop.time() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


async def _run_with_worker(svc, run_id, worker_id="w1"):
    worker = jobs.Worker(svc, worker_id)
    task = asyncio.create_task(worker.run_forever())
    try:
        await _until(lambda: _finished(svc, run_id))
    finally:
        await worker.stop()
        task.cancel()
    return await svc.store.get_run(run_id)


async def _finished(svc, run_id):
    return (await svc.store.get_run(run_id))["status"] in ("done", "failed")


def test_queued_run_completes(samples):
    async def flow():
        svc = await _svc(samples)
        run_id = await svc.store.create_run("POL-007", "Safety", None, "t")
        assert (await svc.store.get_run(run_id))["status"] == "queued"
        return await _run_with_worker(svc, run_id)

    run = asyncio.run(flow())
    assert run["status"] == "done" and run["result"]["counts"]["impacted"] == 2


def test_temporary_failure_is_retried_from_a_clean_start(samples, monkeypatch):
    calls = {"n": 0}
    real = jobs.execute_run

    async def flaky(svc, run_id, final_attempt=True):
        calls["n"] += 1
        if calls["n"] == 1:
            await svc.store.save_change(run_id, type("C", (), {"__dict__": {"number": "x"}})())  # partial work
            await svc.store.update_run(run_id, progress="Hit a problem; trying again shortly…")
            raise ConnectionError("embedding server restarting")
        return await real(svc, run_id, final_attempt)

    monkeypatch.setattr(jobs, "execute_run", flaky)

    async def flow():
        svc = await _svc(samples)
        run_id = await svc.store.create_run("POL-007", "Safety", None, "t")
        run = await _run_with_worker(svc, run_id)
        return svc, run

    svc, run = asyncio.run(flow())
    assert run["status"] == "done" and calls["n"] == 2
    assert all(c["number"] != "x" for c in svc.store.changes)  # the half-finished attempt was cleared
    assert svc.store.jobs[0]["attempts"] == 2 and svc.store.jobs[0]["status"] == "done"


def test_repeated_failure_marks_the_run_failed_in_plain_words(samples, monkeypatch):
    async def broken(svc, run_id, final_attempt=True):
        raise ConnectionError("judge unreachable")

    monkeypatch.setattr(jobs, "execute_run", broken)

    async def flow():
        svc = await _svc(samples, job_max_attempts=2)
        run_id = await svc.store.create_run("POL-007", "Safety", None, "t", max_attempts=2)
        return svc, await _run_with_worker(svc, run_id)

    svc, run = asyncio.run(flow())
    assert run["status"] == "failed" and "several times" in run["error"]
    assert svc.store.jobs[0]["status"] == "dead" and "judge unreachable" in svc.store.jobs[0]["last_error"]


def test_a_crashed_workers_run_is_picked_up_by_another(samples):
    async def flow():
        svc = await _svc(samples)
        run_id = await svc.store.create_run("POL-007", "Safety", None, "t")
        # Worker "dead" claims the job and then vanishes without finishing or renewing.
        assert await svc.store.claim_job("dead", 0.2)
        run = await _run_with_worker(svc, run_id, "w2")
        return svc, run

    svc, run = asyncio.run(flow())
    assert run["status"] == "done"
    assert svc.store.jobs[0]["locked_by"] == "w2" and svc.store.jobs[0]["attempts"] == 2


def test_restarted_worker_takes_its_own_job_back_at_once(samples):
    async def flow():
        svc = await _svc(samples)
        run_id = await svc.store.create_run("POL-007", "Safety", None, "t")
        assert await svc.store.claim_job("same-host", 3600)  # long lease: only release can free it quickly
        return await _run_with_worker(svc, run_id, "same-host")

    assert asyncio.run(flow())["status"] == "done"


def test_user_errors_are_not_retried(samples):
    async def flow():
        svc = await _svc(samples)
        run_id = await svc.store.create_run("HR-201", "Safety", None, "t")  # one stored version, no upload
        return svc, await _run_with_worker(svc, run_id)

    svc, run = asyncio.run(flow())
    assert run["status"] == "failed" and svc.store.jobs[0]["attempts"] == 1


def test_periodic_jobs_are_not_stacked(samples, monkeypatch):
    seen = []

    async def tick(svc, job, final):
        seen.append(job["id"])

    monkeypatch.setitem(jobs.HANDLERS, "tick", tick)
    monkeypatch.setitem(jobs.PERIODIC, "tick", 3600)

    async def flow():
        svc = await _svc(samples)
        assert await svc.store.enqueue("tick", {}, dedupe_key="periodic:tick")
        assert await svc.store.enqueue("tick", {}, dedupe_key="periodic:tick") is None
        worker = jobs.Worker(svc, "w")
        task = asyncio.create_task(worker.run_forever())
        await _until(lambda: _done(seen))
        await worker.stop()
        task.cancel()

    async def _done(s):
        return len(s) >= 1

    asyncio.run(flow())
    assert len(seen) == 1


def test_review_sync_round_trip(samples):
    """Verdicts go to reviewers (all yes/unsure plus a sample of no); answers come back to judgements."""
    from app.review import FakeReview

    async def flow():
        svc = await _svc(samples, review_sample_no=1.0)  # sample every "no" so the count is exact
        svc.review = FakeReview()
        run_id = await svc.store.create_run("POL-007", "Safety", None, "t")
        await _run_with_worker(svc, run_id)
        await jobs.handle_review_sync(svc, {"id": 0}, True)
        pushed = list(svc.review.items.values())
        first = await svc.store.review_candidates(1000, 1.0)
        assert first == []  # everything was pushed and marked
        j = next(j for j in svc.store.judgements if j["result"]["impacted"] == "yes")
        svc.review.submit(j["id"], "no", note="This clause was already updated last month.", reviewer="sam")
        await jobs.handle_review_sync(svc, {"id": 0}, True)
        await jobs.handle_review_sync(svc, {"id": 0}, True)  # running twice changes nothing
        return svc, j, pushed

    svc, j, pushed = asyncio.run(flow())
    finals = {(x["run_id"], x["change_id"], x["candidate"]) for x in svc.store.judgements}
    assert len(pushed) == len(finals) > 0          # one card per pair, final stage only
    assert {p.model_verdict for p in pushed} >= {"yes", "no"}
    assert j["reviewer_decision"] == "no" and j["reviewed_by"] == "sam" and "last month" in j["reviewer_note"]
    assert all(x.get("review_pushed_at") for x in svc.store.judgements)


def test_review_card_uses_the_exact_prompt_text():
    row = {"id": 7, "run_id": "r", "head_policy": "POL-007", "number": "4.2", "kind": "modified", "heading": "",
           "old_text": "weekly", "new_text": "before every shift", "old_number": None, "candidate_policy": "SAF-112",
           "candidate_title": "Maintenance", "candidate_number": "2.2", "clause_heading": "", "clause_body": "CURRENT TEXT",
           "prompt": "HEAD POLICY: x\nCLAUSE §2.2:\n<<<\nTEXT THE MODEL SAW\n>>>\nTHIS CLAUSE WAS SELECTED BECAUSE: x",
           "result": {"impacted": "yes", "impact_type": "conflict", "why": "w", "evidence_head": "a", "evidence_candidate": "b"}}
    item = jobs.review_item(row)
    assert item.candidate_text == "TEXT THE MODEL SAW" and item.head_change_label.startswith("§4.2")
    assert jobs.review_item({**row, "prompt": ""}).candidate_text == "CURRENT TEXT"
