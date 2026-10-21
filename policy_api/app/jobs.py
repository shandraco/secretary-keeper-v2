"""Background worker: takes jobs from the queue in the database and runs them.

A job is claimed with a lease. While it runs, the worker renews the lease; if the process dies, the
lease runs out and any worker (including this one after a restart) picks the job up again. So a
policy check that was cut off by a restart or crash is retried rather than lost.

Handlers must be safe to run twice: delivery is at-least-once.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import socket
import time
from pathlib import Path
from typing import Awaitable, Callable

from .diff import Change
from .graph import execute_run
from .pipeline import RunError, Services
from .review import ReviewItem
from .store import now

log = logging.getLogger(__name__)
IDLE_POLL_S = 1.0
RETRY_BASE_S = 10.0
RETRY_CAP_S = 300.0

Handler = Callable[[Services, dict, bool], Awaitable[None]]


class Retry(Exception):
    """Raise from a handler for a temporary problem worth trying again."""


async def handle_impact_run(svc: Services, job: dict, final_attempt: bool) -> None:
    run_id = job["payload"]["run_id"]
    run = await svc.store.get_run(run_id)
    if not run or run["status"] in ("done", "failed", "needs_review"):
        return  # already finished (or paused for a person) by an earlier attempt that couldn't record it
    # No reset on a retry: the graph carries on from its last saved step.
    await svc.store.update_run(run_id, status="running", progress=(
        "Starting…" if job["attempts"] == 1 else "Starting again after an interruption…"))
    await execute_run(svc, run_id, final_attempt=final_attempt)


async def handle_review_run(svc: Services, job: dict, final_attempt: bool) -> None:
    """Carry on with a check that paused for a person to decide its "maybe" sections."""
    run_id = job["payload"]["run_id"]
    run = await svc.store.get_run(run_id)
    if not run or run["status"] not in ("queued", "running"):
        return  # only a run this answer claimed (POST /runs/{id}/review set it to queued)
    await svc.store.update_run(run_id, status="running", progress="Applying the decisions…")
    await execute_run(svc, run_id, final_attempt=final_attempt, resume=job["payload"]["answer"])


async def on_impact_run_dead(svc: Services, job: dict, error: str) -> None:
    run = await svc.store.get_run(job["payload"]["run_id"])
    if run and run["status"] not in ("done", "failed"):
        await svc.store.update_run(
            run["id"], status="failed", finished_at=now(),
            error="This check failed several times in a row. The problem has been logged; please contact the system owner.")


async def handle_cleanup_exports(svc: Services, job: dict, final_attempt: bool) -> None:
    """Delete generated files older than EXPORT_RETENTION_DAYS. A run's own files are rebuilt on the
    next download; one-off documents and comparisons are gone and must be asked for again."""
    cutoff = time.time() - svc.settings.export_retention_days * 86400
    removed = 0
    folder = Path(svc.settings.export_dir)
    if not folder.is_dir():
        return
    for path in folder.iterdir():
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    if removed:
        log.info("removed %d generated file(s) older than %d days", removed, svc.settings.export_retention_days)


REVIEW_BATCH = 200
_CLAUSE_IN_PROMPT = re.compile(r"\nCLAUSE §[^\n]*:\n<<<\n(.*?)\n>>>", re.S)


def review_item(row: dict) -> ReviewItem:
    """Build a review card from a stored verdict. The clause text is taken from the exact prompt the
    model saw (the policy may have changed since); the current clause is the fallback."""
    m = _CLAUSE_IN_PROMPT.search(row.get("prompt") or "")
    clause = m.group(1) if m else "\n".join(x for x in (row.get("clause_heading"), row.get("clause_body")) if x)
    change = Change(number=row["number"], kind=row["kind"], heading=row.get("heading") or "",
                    old_text=row.get("old_text") or "", new_text=row.get("new_text") or "", old_number=row.get("old_number"))
    v = row["result"]
    return ReviewItem(
        judgement_id=row["id"], run_id=row["run_id"], head_policy=row["head_policy"], head_change_label=change.label,
        old_text=change.old_text, new_text=change.new_text, candidate_policy=row["candidate_policy"],
        candidate_title=row.get("candidate_title") or "", candidate_number=row["candidate_number"],
        candidate_text=clause, model_verdict=v.get("impacted", "unsure"), impact_type=v.get("impact_type", "none"),
        why=v.get("why", ""), evidence_head=v.get("evidence_head", ""), evidence_candidate=v.get("evidence_candidate", ""),
    )


async def handle_review_sync(svc: Services, job: dict, final_attempt: bool) -> None:
    """Send new verdicts to the review queue and bring reviewers' answers back into `judgements`."""
    if svc.review is None:
        return
    store = svc.store
    await asyncio.to_thread(svc.review.ensure_dataset)
    while True:
        rows = await store.review_candidates(REVIEW_BATCH, svc.settings.review_sample_no)
        if not rows:
            break
        await asyncio.to_thread(svc.review.push, [review_item(r) for r in rows])
        await store.mark_review_pushed([r["id"] for r in rows])
        if len(rows) < REVIEW_BATCH:
            break
    since = await store.last_review_at()
    decisions = await asyncio.to_thread(svc.review.pull_submitted, since)
    saved = await store.save_review_decisions(decisions)
    if saved:
        log.info("saved %d reviewer decision(s)", saved)


HANDLERS: dict[str, Handler] = {"impact_run": handle_impact_run, "review_run": handle_review_run,
                                "cleanup_exports": handle_cleanup_exports, "review_sync": handle_review_sync}
ON_DEAD: dict[str, Callable[[Services, dict, str], Awaitable[None]]] = {"impact_run": on_impact_run_dead,
                                                                        "review_run": on_impact_run_dead}
# kind -> seconds between runs. Added to by optional features (e.g. the review sync).
PERIODIC: dict[str, float] = {"cleanup_exports": 6 * 3600}


def backoff(attempt: int) -> float:
    """Capped exponential backoff with full jitter."""
    return random.uniform(0, min(RETRY_CAP_S, RETRY_BASE_S * 2 ** (attempt - 1)))


def default_worker_id() -> str:
    # The container hostname survives `docker restart`, so a restarted worker can release its own
    # jobs at once instead of waiting for the lease to expire.
    return os.environ.get("WORKER_ID") or socket.gethostname()


class Worker:
    def __init__(self, svc: Services, worker_id: str | None = None) -> None:
        self.svc = svc
        self.id = worker_id or default_worker_id()
        self.lease = float(svc.settings.job_lease_seconds)
        self.slots = asyncio.Semaphore(svc.settings.worker_concurrency)
        self.wake = asyncio.Event()
        self.tasks: set[asyncio.Task] = set()
        self._stop = asyncio.Event()
        self.periodic = dict(PERIODIC)
        if svc.review is not None:
            self.periodic["review_sync"] = float(svc.settings.review_sync_seconds)

    def poke(self) -> None:
        """Start looking for work now instead of at the next poll."""
        self.wake.set()

    async def run_forever(self) -> None:
        released = await self.svc.store.release_worker_jobs(self.id)
        if released:
            log.warning("picking up %d job(s) this worker was running before it restarted", released)
        last_periodic: dict[str, float] = {}
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            for kind, every in self.periodic.items():
                if loop.time() - last_periodic.get(kind, -every) >= every:
                    last_periodic[kind] = loop.time()
                    await self.svc.store.enqueue(kind, {}, dedupe_key=f"periodic:{kind}")
            await self.slots.acquire()
            try:
                job = await self.svc.store.claim_job(self.id, self.lease)
            except Exception:
                log.exception("could not claim a job")
                job = None
            if job is None:
                self.slots.release()
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), IDLE_POLL_S)
                except TimeoutError:
                    pass
                continue
            task = asyncio.create_task(self._run(job))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def stop(self) -> None:
        self._stop.set()
        self.wake.set()
        for t in list(self.tasks):
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        # Hand our jobs straight back so another worker (or this one after restart) can take them.
        await self.svc.store.release_worker_jobs(self.id)

    async def _heartbeat(self, job: dict, work: asyncio.Task) -> None:
        while True:
            await asyncio.sleep(self.lease / 3)
            if not await self.svc.store.extend_lease(job["id"], self.id, self.lease):
                log.warning("lost the lease on job %s; stopping it", job["id"])
                work.cancel()
                return

    async def _run(self, job: dict) -> None:
        store, kind = self.svc.store, job["kind"]
        final = job["attempts"] >= job["max_attempts"]
        try:
            handler = HANDLERS.get(kind)
            if handler is None:
                await store.fail_job(job["id"], self.id, f"no handler for job kind {kind!r}", None)
                return
            work = asyncio.create_task(handler(self.svc, job, final))
            beat = asyncio.create_task(self._heartbeat(job, work))
            try:
                await work
            finally:
                beat.cancel()
            await store.finish_job(job["id"], self.id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            permanent = isinstance(e, RunError)
            if not permanent:
                log.exception("job %s (%s) attempt %s failed", job["id"], kind, job["attempts"])
            status = await store.fail_job(job["id"], self.id, f"{type(e).__name__}: {e}",
                                          None if permanent else backoff(job["attempts"]))
            if status == "dead" and kind in ON_DEAD:
                await ON_DEAD[kind](self.svc, job, str(e))
        finally:
            self.slots.release()
