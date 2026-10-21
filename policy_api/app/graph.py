"""One policy check as a LangGraph graph.

    load → diff → find_related → judge ─┬─────────────→ summarise ─┬────────→ END
                                         └→ escalate ─┘             └→ review ┘

Every node is ordinary code except `judge` and `escalate`, which ask a model about one pair at a time.
Routing is decided in code, never by a model. State is saved to the database after every node, so a
check cut off by a restart carries on from the last finished step instead of starting again. With
REVIEW_GATE=1, a check whose answer has "maybe" sections pauses at `review` until a person decides them
(POST /runs/{id}/review); sections left undecided stay "maybe" and the check then finishes.

The state holds plain JSON values only, so checkpoints never depend on our class layout.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import asdict
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .diff import Change, diff_clauses
from .documents import split_clauses
from .pipeline import RunError, Services, find_candidates, judge_pair, summarise
from .store import ClauseRow, now

log = logging.getLogger(__name__)
RECURSION_LIMIT = 25  # the graph has 8 steps; anything near this is a bug, not a big check


class Pair(TypedDict):
    change: int  # index into RunState.changes
    row: dict  # ClauseRow fields
    found: list[str]
    verdict: dict
    escalated: bool


class RunState(TypedDict, total=False):
    run_id: str
    head: dict
    labels: list[str]
    policy_ids: list[str]
    metas: dict
    changes: list[dict]  # Change fields plus "id", the saved change's row id
    pairs: list[Pair]
    errors: int
    result: dict
    reviewed: dict


def _change(c: dict) -> Change:
    return Change(**{k: v for k, v in c.items() if k != "id"})


def build_graph(svc: Services, checkpointer: Any):
    store = svc.store

    async def progress(run_id: str, text: str) -> None:
        await store.update_run(run_id, progress=text)

    async def load(state: RunState) -> RunState:
        run_id = state["run_id"]
        run = await store.get_run(run_id)
        head_id, subset = run["head_policy"], run["subset"]
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
            labels = [f"stored version {versions[0][0]}", f"uploaded file {upload['filename']}"]
        else:
            if len(versions) < 2:
                raise RunError(f"Only one version of {head_id} is stored. Upload the new version to compare it.")
            labels = [f"version {versions[1][0]}", f"version {versions[0][0]}"]
        policy_ids = [p for p in await store.subset_policy_ids(subset) if p != head_id]
        if not policy_ids:
            raise RunError(f"The group “{subset}” has no policies to check apart from {head_id}.")
        return {"head": {"id": head["id"], "title": head["title"]}, "labels": labels, "policy_ids": policy_ids,
                "metas": await store.get_policies(policy_ids)}

    async def diff(state: RunState) -> RunState:
        run_id = state["run_id"]
        run = await store.get_run(run_id)
        versions = await store.latest_versions(state["head"]["id"], 2)
        if run.get("upload_id"):
            old_body, new_body = versions[0][1], (await store.get_upload(run["upload_id"]))["body"]
        else:
            (_, new_body), (_, old_body) = versions
        await store.reset_run(run_id)  # this step may be running again after a crash
        # In a thread so a large upload can't stall other requests; diff_clauses caps its own time
        changes = await asyncio.to_thread(lambda: diff_clauses(split_clauses(old_body), split_clauses(new_body)))
        saved = [{**asdict(c), "id": await store.save_change(run_id, c)} for c in changes]
        await progress(run_id, f"Found {len(saved)} changed section(s). Looking for related sections…")
        return {"changes": saved}

    async def find_related(state: RunState) -> RunState:
        pairs: list[Pair] = []
        for i, c in enumerate(state["changes"]):
            cands = await find_candidates(svc, _change(c), state["head"]["id"], state["policy_ids"])
            pairs += [{"change": i, "row": asdict(row), "found": sorted(found), "verdict": {}, "escalated": False}
                      for row, found in cands.values()]
        await progress(state["run_id"], f"Checking {len(pairs)} section pairs…")
        return {"pairs": pairs}

    async def ask(state: RunState, todo: list[int], stage: str) -> tuple[list[Pair], int]:
        run_id, pairs = state["run_id"], [dict(p) for p in state["pairs"]]
        await store.delete_judgements(run_id, stage)  # this step may be running again after a crash
        sem = asyncio.Semaphore(svc.settings.judge_concurrency)
        done, errors = 0, 0

        async def one(i: int) -> None:
            nonlocal done, errors
            p = pairs[i]
            c = state["changes"][p["change"]]
            async with sem:
                v = await judge_pair(svc, run_id, _change(c), c["id"], ClauseRow(**p["row"]), set(p["found"]),
                                     state["head"], state["metas"], stage)
            if v.pop("model_error", False):
                errors += 1
                if stage == "escalate":
                    return  # keep the first opinion when the second model fails
            pairs[i] = {**p, "verdict": v, "escalated": p["escalated"] or stage == "escalate"}
            done += 1
            if done == len(todo) or done % 10 == 0:
                await progress(run_id, f"{'Checked' if stage == 'judge' else 'Second opinion on'} {done} of {len(todo)} section pairs…")

        await asyncio.gather(*(one(i) for i in todo))
        return pairs, errors

    async def judge(state: RunState) -> RunState:
        todo = list(range(len(state["pairs"])))
        pairs, errors = await ask(state, todo, "judge")
        if todo and errors == len(todo):
            raise RunError("The checking model is not responding. Please try again in a few minutes or contact the system owner.")
        return {"pairs": pairs, "errors": errors}

    def after_judge(state: RunState) -> str:
        unsure = any(p["verdict"].get("impacted") == "unsure" for p in state["pairs"])
        return "escalate" if unsure and svc.escalate is not None else "summarise"

    async def escalate(state: RunState) -> RunState:
        todo = [i for i, p in enumerate(state["pairs"]) if p["verdict"].get("impacted") == "unsure"]
        await progress(state["run_id"], f"Getting a second opinion on {len(todo)} unclear section pair(s)…")
        pairs, _ = await ask(state, todo, "escalate")
        return {"pairs": pairs}

    async def summarise_node(state: RunState) -> RunState:
        run = await store.get_run(state["run_id"])
        changes = [_change(c) for c in state["changes"]]
        verdicts = [(changes[p["change"]], ClauseRow(**p["row"]), set(p["found"]), p["verdict"], p["escalated"])
                    for p in state["pairs"]]
        result = summarise(run, state["head"], tuple(state["labels"]), changes, verdicts,
                           state["policy_ids"], state["metas"], svc, state.get("errors", 0))
        return {"result": result}

    def after_summarise(state: RunState) -> str:
        maybe = any(r["status"] == "Maybe" for r in state["result"]["rows"])
        return "review" if maybe and svc.settings.review_gate else END

    async def review(state: RunState) -> RunState:
        maybes = [{"policy_id": r["policy_id"], "clause": r["clause"], "why": r["why"]}
                  for r in state["result"]["rows"] if r["status"] == "Maybe"]
        answer = interrupt({"maybe": maybes})  # pauses here until POST /runs/{id}/review
        return {"result": apply_review(state["result"], answer), "reviewed": answer}

    g = StateGraph(RunState)
    g.add_node("load", load)
    g.add_node("diff", diff)
    g.add_node("find_related", find_related)
    g.add_node("judge", judge)
    g.add_node("escalate", escalate)
    g.add_node("summarise", summarise_node)
    g.add_node("review", review)
    g.add_edge(START, "load")
    g.add_edge("load", "diff")
    g.add_edge("diff", "find_related")
    g.add_edge("find_related", "judge")
    g.add_conditional_edges("judge", after_judge, ["escalate", "summarise"])
    g.add_edge("escalate", "summarise")
    g.add_conditional_edges("summarise", after_summarise, ["review", END])
    g.add_edge("review", END)
    return g.compile(checkpointer=checkpointer)


def apply_review(result: dict, answer: dict) -> dict:
    """Turn each "maybe" a person decided into "Yes" or drop it, and recount. Undecided ones stay "Maybe"."""
    decided = {(d["policy_id"], d["clause"]): bool(d["affected"]) for d in answer.get("decisions", [])}
    rows = []
    for r in result["rows"]:
        if r["status"] == "Maybe" and (r["policy_id"], r["clause"]) in decided:
            if not decided[(r["policy_id"], r["clause"])]:
                continue
            r = {**r, "status": "Yes", "why": f"{r['why']} Confirmed by {answer.get('by') or 'a reviewer'}."}
        rows.append(r)
    status: dict[str, str] = {}
    for r in rows:
        if status.get(r["policy_id"]) != "Yes":
            status[r["policy_id"]] = r["status"]
    gone = {r["policy_id"] for r in result["rows"]} - set(status)
    not_impacted = result["not_impacted"] + [
        {"policy_id": pid, "title": next(r["policy_title"] for r in result["rows"] if r["policy_id"] == pid),
         "owner": next(r["owner"] for r in result["rows"] if r["policy_id"] == pid),
         "reason": "A reviewer decided the flagged sections are not affected."}
        for pid in sorted(gone)
    ]
    counts = {**result["counts"], "impacted": sum(1 for s in status.values() if s == "Yes"),
              "maybe": sum(1 for s in status.values() if s == "Maybe"), "not_impacted": len(not_impacted)}
    return {**result, "rows": rows, "not_impacted": not_impacted, "counts": counts,
            "reviewed_by": answer.get("by") or "", "finished_at": now()}


@contextlib.asynccontextmanager
async def open_checkpointer(svc: Services):
    """Postgres in production (tables made on first start); memory in demo mode and tests."""
    if svc.settings.demo_mode or not svc.settings.database_url:
        from langgraph.checkpoint.memory import InMemorySaver

        yield InMemorySaver()
        return
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(svc.settings.database_url) as saver:
        await saver.setup()
        yield saver


async def attach_graph(svc: Services) -> Services:
    svc.exit_stack = contextlib.AsyncExitStack()
    saver = await svc.exit_stack.enter_async_context(open_checkpointer(svc))
    svc.graph = build_graph(svc, saver)
    return svc


def _config(run_id: str) -> dict:
    return {"configurable": {"thread_id": run_id}, "recursion_limit": RECURSION_LIMIT}


async def _record(svc: Services, run_id: str, out: dict) -> None:
    if out.get("__interrupt__"):
        maybe = out["__interrupt__"][0].value["maybe"]
        await svc.store.update_run(run_id, status="needs_review", result=out["result"],
                                   progress=f"Waiting for a person to decide {len(maybe)} section(s) marked “maybe”.")
    else:
        await svc.store.update_run(run_id, status="done", progress="Finished", result=out["result"], finished_at=now())


async def execute_run(svc: Services, run_id: str, final_attempt: bool = True, resume: dict | None = None) -> None:
    """Run (or carry on with) the graph for one check and record the outcome on the run row.

    A RunError is the user's problem (no such policy, nothing changed) and is recorded at once. Any other
    error is raised for the job queue to retry, except on the final attempt, where it is recorded. A retry
    starts from the last saved step. `resume` is a reviewer's answer for a check paused at `review`."""
    config = _config(run_id)
    try:
        if resume is not None:
            out = await svc.graph.ainvoke(Command(resume=resume), config, durability="sync")
        else:
            saved = await svc.graph.aget_state(config)
            start = None if saved.next else {"run_id": run_id}  # None = carry on from the checkpoint
            out = await svc.graph.ainvoke(start, config, durability="sync")
        await _record(svc, run_id, out)
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
