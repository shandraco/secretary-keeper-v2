"""One impact analysis: diff the head policy, find candidate clauses, judge each pair, summarise."""
from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .diff import Change, diff_clauses
from .documents import split_clauses
from .llm import check_evidence
from .prompts import JUDGE_SYSTEM, Pair, render_pair
from .store import ClauseRow, now

log = logging.getLogger(__name__)
RANK = {"no": 0, "unsure": 1, "yes": 2}
STATUS = {2: "Yes", 1: "Maybe"}
FOUND_BY_LABEL = {"cites": "cites the head policy", "term": "uses a changed term", "similar": "similar wording"}
MODEL_ERROR = {
    "impacted": "unsure", "impact_type": "none",
    "why": "This section could not be checked automatically (model error); a person should check it.",
    "evidence_head": "", "evidence_candidate": "", "suggested_edit": "",
}


class RunError(Exception):
    """A problem the person asking can fix. The message is shown to them as written."""


@dataclass
class Services:
    settings: Settings
    store: Any
    embedder: Any
    reranker: Any
    judge: Any
    escalate: Any = None
    extractor: Any = None
    review: Any = None  # app.review.ArgillaReview when the review queue is on

    async def close(self) -> None:
        for part in (self.embedder, self.reranker, self.judge, self.escalate, self.extractor):
            if part is not None and hasattr(part, "aclose"):
                await part.aclose()
        await self.store.close()


def covers(cited: str, changed: str) -> bool:
    """A citation of §4 covers a change to §4.2, and a citation of §4.2.1 is hit by a change to §4.2."""
    return cited == changed or changed.startswith(cited + ".") or cited.startswith(changed + ".")


async def find_candidates(svc: Services, change: Change, head_id: str, policy_ids: list[str]) -> dict[str, tuple[ClauseRow, set[str]]]:
    """Clauses that might be affected. Citations and term matches are always kept; similar wording is reranked and cut."""
    cands: dict[str, tuple[ClauseRow, set[str]]] = {}

    def add(row: ClauseRow, why: str) -> None:
        cands.setdefault(row.key, (row, set()))[1].add(why)

    numbers = [n for n in (change.number, change.old_number) if n]
    for row, cited in await svc.store.refs_into(head_id, policy_ids):
        if cited is None or any(covers(cited, n) for n in numbers):
            add(row, "cites")
    for term in change.terms:
        for row in await svc.store.clauses_using_term(term, policy_ids):
            add(row, "term")

    query = change.new_text or change.old_text
    [vector] = await svc.embedder.embed([query])
    similar = [r for r in await svc.store.nearest(vector, policy_ids, svc.settings.semantic_k) if r.key not in cands]
    if similar:
        scores = await svc.reranker.rerank(query, [r.text for r in similar])
        ranked = sorted(zip(scores, similar), key=lambda t: -t[0])[: svc.settings.rerank_keep]
        for _, row in ranked:
            add(row, "similar")
    return cands


async def _judge_all(svc: Services, run_id: str, head: dict, metas: dict, work: list, progress) -> tuple[list, int]:
    sem = asyncio.Semaphore(svc.settings.judge_concurrency)
    done, errors = 0, 0

    async def one(change: Change, change_id: int, row: ClauseRow, found: set[str]):
        nonlocal done, errors
        pair = Pair(head["id"], head["title"], change, row, metas[row.policy_id]["title"], frozenset(found))
        prompt = render_pair(pair)
        prompt_hash = hashlib.sha256((JUDGE_SYSTEM + prompt).encode()).hexdigest()
        escalated = False
        async with sem:
            try:
                verdict = check_evidence(pair, await svc.judge.assess(pair))
                await svc.store.save_judgement(run_id, change_id, row, sorted(found), "judge", svc.judge.name, prompt_hash, verdict, prompt)
                if verdict["impacted"] == "unsure" and svc.escalate is not None:
                    verdict = check_evidence(pair, await svc.escalate.assess(pair))
                    escalated = True
                    await svc.store.save_judgement(run_id, change_id, row, sorted(found), "escalate", svc.escalate.name, prompt_hash, verdict, prompt)
            except Exception:
                log.exception("judging %s against §%s failed", row.key, change.number)
                errors += 1
                verdict = dict(MODEL_ERROR)
        done += 1
        if done == len(work) or done % 10 == 0:
            await progress(f"Checked {done} of {len(work)} section pairs…")
        return change, row, found, verdict, escalated

    results = await asyncio.gather(*(one(*w) for w in work))
    return results, errors


def summarise(run: dict, head: dict, labels: tuple[str, str], changes: list[Change], verdicts: list,
              policy_ids: list[str], metas: dict, svc: Services, errors: int) -> dict:
    best: dict[str, dict] = {}
    heads: dict[str, set[str]] = {}
    checked: dict[str, set[str]] = {}
    for change, row, found, v, escalated in verdicts:
        rank = RANK[v["impacted"]]
        checked.setdefault(row.policy_id, set()).add(row.number)
        if rank:
            heads.setdefault(row.key, set()).add(change.number)
        if row.key not in best or rank > best[row.key]["rank"]:
            best[row.key] = {"rank": rank, "change": change, "row": row, "found": found, "v": v, "escalated": escalated}

    rows, policy_rank = [], {}
    for key, b in best.items():
        if not b["rank"]:
            continue
        row, ch, v, meta = b["row"], b["change"], b["v"], metas[b["row"].policy_id]
        policy_rank[row.policy_id] = max(policy_rank.get(row.policy_id, 0), b["rank"])
        rows.append({
            "status": STATUS[b["rank"]], "policy_id": row.policy_id, "policy_title": meta["title"],
            "owner": meta.get("owner") or "", "clause": row.number, "clause_heading": row.heading,
            "clause_text": row.text, "impact_type": v["impact_type"],
            "head_clauses": ", ".join(f"§{n}" for n in sorted(heads.get(key, {ch.number}))),
            "head_change": ch.label, "head_old_text": ch.old_text, "head_new_text": ch.new_text,
            "why": v["why"], "evidence_head": v["evidence_head"], "evidence_candidate": v["evidence_candidate"],
            "suggested_edit": v["suggested_edit"],
            "found_by": ", ".join(FOUND_BY_LABEL[f] for f in sorted(b["found"])), "escalated": b["escalated"],
        })
    rows.sort(key=lambda r: (r["status"] != "Yes", r["policy_id"], r["clause"]))

    not_impacted = []
    for pid in policy_ids:
        if pid in policy_rank:
            continue
        n = len(checked.get(pid, ()))
        reason = (f"Checked {n} related section{'s' if n != 1 else ''}; none are affected." if n
                  else "Nothing in this policy relates to the changed sections.")
        not_impacted.append({"policy_id": pid, "title": metas[pid]["title"], "owner": metas[pid].get("owner") or "", "reason": reason})

    s = svc.settings
    return {
        "run_id": run["id"], "subset": run["subset"], "created_at": run["created_at"], "finished_at": now(),
        "head": {"id": head["id"], "title": head["title"], "old_label": labels[0], "new_label": labels[1]},
        "counts": {
            "subset_policies": len(policy_ids),
            "impacted": sum(1 for r in policy_rank.values() if r == 2),
            "maybe": sum(1 for r in policy_rank.values() if r == 1),
            "not_impacted": len(not_impacted), "changes": len(changes), "pairs_checked": len(verdicts),
            "escalated": sum(1 for *_, e in verdicts if e), "errors": errors,
        },
        "changes": [{**c.__dict__, "label": c.label} for c in changes],
        "rows": rows,
        "not_impacted": not_impacted,
        "method": {
            "judge_model": svc.judge.name, "escalate_model": svc.escalate.name if svc.escalate else "off",
            "embedder": svc.embedder.name, "reranker": svc.reranker.name,
            "semantic_k": s.semantic_k, "rerank_keep": s.rerank_keep, "demo_mode": s.demo_mode,
        },
    }


async def run_impact(svc: Services, run_id: str) -> dict:
    store = svc.store
    run = await store.get_run(run_id)
    head_id, subset = run["head_policy"], run["subset"]

    async def progress(text: str) -> None:
        await store.update_run(run_id, progress=text)

    head = (await store.get_policies([head_id])).get(head_id)
    if not head:
        raise RunError(f"There is no policy called {head_id}.")
    versions = await store.latest_versions(head_id, 2)
    if run.get("upload_id"):
        upload = await store.get_upload(run["upload_id"])
        if not upload:
            raise RunError("The uploaded file could not be found. Please upload it again.")
        if not versions:
            raise RunError(f"No version of {head_id} is stored yet, so there is nothing to compare the upload with.")
        old_body, new_body = versions[0][1], upload["body"]
        labels = (f"stored version {versions[0][0]}", f"uploaded file {upload['filename']}")
    else:
        if len(versions) < 2:
            raise RunError(f"Only one version of {head_id} is stored. Upload the new version to compare it.")
        (new_v, new_body), (old_v, old_body) = versions
        labels = (f"version {old_v}", f"version {new_v}")

    policy_ids = [p for p in await store.subset_policy_ids(subset) if p != head_id]
    if not policy_ids:
        raise RunError(f"The group “{subset}” has no policies to check apart from {head_id}.")
    metas = await store.get_policies(policy_ids)

    # In a thread so a large upload can't stall other requests; diff_clauses caps its own time
    changes = await asyncio.to_thread(lambda: diff_clauses(split_clauses(old_body), split_clauses(new_body)))
    await progress(f"Found {len(changes)} changed section(s). Looking for related sections…")
    work = []
    for change in changes:
        change_id = await store.save_change(run_id, change)
        cands = await find_candidates(svc, change, head_id, policy_ids)
        work += [(change, change_id, row, found) for row, found in cands.values()]

    await progress(f"Checking {len(work)} section pairs…")
    verdicts, errors = await _judge_all(svc, run_id, head, metas, work, progress) if work else ([], 0)
    if work and errors == len(work):
        raise RunError("The checking model is not responding. Please try again in a few minutes or contact the system owner.")
    return summarise(run, head, labels, changes, verdicts, policy_ids, metas, svc, errors)


async def execute_run(svc: Services, run_id: str, final_attempt: bool = True) -> None:
    """Do the run and record the outcome on the run row.

    A RunError is the user's problem (no such policy, nothing changed) and is recorded at once. Any other
    error is raised for the job queue to retry, except on the final attempt, where it is recorded."""
    try:
        result = await run_impact(svc, run_id)
        await svc.store.update_run(run_id, status="done", progress="Finished", result=result, finished_at=now())
    except RunError as e:
        await svc.store.update_run(run_id, status="failed", error=str(e), finished_at=now())
    except Exception:
        if not final_attempt:
            await svc.store.update_run(run_id, progress="Hit a problem; trying again shortly…")
            raise
        log.exception("run %s failed", run_id)
        await svc.store.update_run(
            run_id, status="failed", finished_at=now(),
            error="Something went wrong on our side. The error has been logged; please contact the system owner.",
        )
