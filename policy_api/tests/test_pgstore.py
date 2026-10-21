"""Runs the sample flow against real Postgres + pgvector. Skipped unless TEST_DATABASE_URL is set.

    docker run -d --name sk-test-db -e POSTGRES_PASSWORD=test -p 55432:5432 \
      -v "$PWD/../db/init.sql:/docker-entrypoint-initdb.d/init.sql:ro" pgvector/pgvector:pg17
    TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres pytest tests/test_pgstore.py
"""
import asyncio
import os
from dataclasses import replace

import pytest

from app.config import load_settings
from app.documents import parse_policy
from app.fakes import FakeEmbedder, FakeJudge, FakeReranker
from app.ingest import ingest_dir
from app.graph import attach_graph, execute_run
from app.pipeline import Services

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


async def _flow(samples):
    from app.pgstore import PgStore

    store = PgStore(URL)
    await store.open()
    async with store.pool.connection() as conn:
        await conn.execute("TRUNCATE policies, uploads, runs, jobs CASCADE")
    embedder = FakeEmbedder()
    embedder.dim = 768  # match vector(768) in init.sql
    # Not demo mode, so the graph saves its checkpoints in Postgres too.
    settings = replace(load_settings(), demo_mode=False, database_url=URL, review_gate=True)
    svc = await attach_graph(Services(settings, store, embedder, FakeReranker(), FakeJudge()))
    try:
        await ingest_dir(svc, samples / "policies")
        # Store v4 as a second version too, to test the no-upload path.
        v4 = samples / "changes" / "POL-007-v4.md"
        doc = parse_policy(v4.name, v4.read_bytes(), svc.settings.policy_id_pattern)
        from app.ingest import ingest_doc

        await ingest_doc(svc, doc)
        assert [v for v, _ in await store.latest_versions("POL-007")] == [4, 3]
        assert {s["name"] for s in await store.list_subsets()} == {"Safety", "Head policies", "HR"}

        run_id = await store.create_run("POL-007", "Safety", None, "test")
        await execute_run(svc, run_id)
        assert (await store.get_run(run_id))["status"] == "needs_review"
        # A fresh process (new checkpointer connection) picks the paused check up from Postgres.
        later = await attach_graph(Services(settings, store, embedder, FakeReranker(), FakeJudge()))
        await execute_run(later, run_id, resume={"decisions": [], "by": "test"})
        later.exit_stack and await later.exit_stack.aclose()
        run = await store.get_run(run_id)
        assert run["status"] == "done", run["error"]
        async with store.pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) AS n, count(prompt) AS with_prompt FROM judgements "
                                     "WHERE run_id = %s::uuid", (run_id,))
            counts = await cur.fetchone()
            judged = counts["n"]
            assert counts["with_prompt"] == judged  # every verdict keeps the exact prompt, for review and training
        return run["result"], judged
    finally:
        await svc.exit_stack.aclose()
        await store.close()


def test_pgstore_end_to_end(samples):
    result, judged = asyncio.run(_flow(samples))
    statuses = {}
    for r in result["rows"]:
        if statuses.get(r["policy_id"]) != "Yes":
            statuses[r["policy_id"]] = r["status"]
    assert statuses == {"SAF-112": "Yes", "SAF-130": "Yes", "SAF-150": "Maybe"}
    assert result["head"]["old_label"] == "version 3"
    assert judged == result["counts"]["pairs_checked"]


def test_job_queue_on_postgres():
    """Outbox, SKIP LOCKED claims, leases, retry, dead-letter, restart release, dedupe, migrations rerun."""
    async def flow():
        from app.migrate import migrate
        from app.pgstore import PgStore

        store = PgStore(URL)
        await store.open()
        try:
            assert await migrate(store.pool) == []  # already applied by open(); rerunning is a no-op
            async with store.pool.connection() as conn:
                await conn.execute("TRUNCATE runs, jobs CASCADE")
                await conn.execute("INSERT INTO policies (id, title) VALUES ('X-1', 'x') ON CONFLICT DO NOTHING")
            run_a = await store.create_run("X-1", "Safety", None, "test", max_attempts=2)
            await store.create_run("X-1", "Safety", None, "test")
            assert (await store.get_run(run_a))["status"] == "queued"

            # Two workers claiming at once never get the same job.
            a, b = await asyncio.gather(store.claim_job("w1", 60), store.claim_job("w2", 60))
            assert a and b and a["id"] != b["id"]
            assert await store.claim_job("w3", 60) is None

            # Only the holder can renew or finish.
            assert await store.extend_lease(a["id"], "w1", 60)
            assert not await store.extend_lease(a["id"], "w2", 60)

            # Retry, then dead after max_attempts.
            job_a = a if a["payload"]["run_id"] == run_a else b
            holder = "w1" if job_a is a else "w2"
            assert await store.fail_job(job_a["id"], holder, "boom", 0) == "pending"
            again = await store.claim_job("w1", 60)
            assert again["id"] == job_a["id"] and again["attempts"] == 2
            assert await store.fail_job(again["id"], "w1", "boom", 0) == "dead"

            # A worker that restarts hands its jobs straight back.
            other = b if job_a is a else a
            other_holder = "w2" if job_a is a else "w1"
            assert await store.release_worker_jobs(other_holder) == 1
            taken = await store.claim_job("w9", 60)
            assert taken["id"] == other["id"] and taken["attempts"] == 2
            await store.finish_job(taken["id"], "w9")

            # Dedupe: one live periodic job at a time.
            first = await store.enqueue("review_sync", {}, dedupe_key="periodic:review_sync")
            assert first and await store.enqueue("review_sync", {}, dedupe_key="periodic:review_sync") is None
            stats = await store.job_stats()
            assert stats["dead"] == 1 and stats["waiting"] == 1
        finally:
            await store.close()

    asyncio.run(flow())


def test_review_queries_on_postgres(samples):
    """The review SQL picks final verdicts from finished runs, samples "no", and records answers."""
    asyncio.run(_flow(samples))  # fresh data: one finished run

    async def flow():
        from app.pgstore import PgStore
        from app.review import ReviewDecision

        store = PgStore(URL)
        await store.open()
        try:
            every = await store.review_candidates(1000, 1.0)
            only_flagged = await store.review_candidates(1000, 0.0)
            assert only_flagged and all(r["result"]["impacted"] in ("yes", "unsure") for r in only_flagged)
            assert len(every) > len(only_flagged)
            assert all(r["prompt"] and r["candidate_title"] for r in every)
            await store.mark_review_pushed([r["id"] for r in every])
            assert await store.review_candidates(1000, 1.0) == []
            target = only_flagged[0]["id"]
            saved = await store.save_review_decisions([ReviewDecision(target, "no", "", "already fixed", "sam",
                                                                       "2026-10-16T10:00:00+00:00")])
            assert saved == 1 and (await store.last_review_at()).startswith("2026-10-16T10:00:00")
        finally:
            await store.close()

    asyncio.run(flow())


def test_run_prefixes_on_postgres():
    async def flow():
        from app.pgstore import PgStore

        store = PgStore(URL)
        await store.open()
        try:
            async with store.pool.connection() as conn:
                await conn.execute("TRUNCATE runs, jobs CASCADE")
                await conn.execute("INSERT INTO policies (id, title) VALUES ('X-1', 'x') ON CONFLICT DO NOTHING")
                for i in ("aaaaaaaa-0000-4000-8000-000000000001", "aaaaaaaa-0000-4000-8000-000000000002",
                          "bbbbbbbb-0000-4000-8000-000000000003"):
                    await conn.execute("INSERT INTO runs (id, head_policy, subset, status) VALUES (%s::uuid, 'X-1', 'Safety', 'queued')", (i,))
            assert await store.find_run_ids("bbbbbbbb") == ["bbbbbbbb-0000-4000-8000-000000000003"]
            assert len(await store.find_run_ids("aaaaaaaa")) == 2
            assert await store.find_run_ids("cccccccc") == []
            assert await store.find_run_ids("a%") == []  # not a pattern
            assert await store.find_run_ids("aaaaaaaa", "needs_review") == []
            await store.update_run("aaaaaaaa-0000-4000-8000-000000000001", status="needs_review")
            assert await store.find_run_ids("aaaaaaaa", "needs_review") == ["aaaaaaaa-0000-4000-8000-000000000001"]
        finally:
            await store.close()

    asyncio.run(flow())
