"""The parts of one impact analysis: services, candidate search, judging one pair, and the summary.
The order they run in, retries, checkpoints and the review pause are in graph.py."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .diff import Change
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
    graph: Any = None  # the compiled run graph (graph.py), with its checkpointer
    exit_stack: Any = None  # contextlib.AsyncExitStack holding the checkpointer's connection

    async def close(self) -> None:
        for part in (self.embedder, self.reranker, self.judge, self.escalate, self.extractor):
            if part is not None and hasattr(part, "aclose"):
                await part.aclose()
        if self.exit_stack is not None:
            await self.exit_stack.aclose()
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


async def judge_pair(svc: Services, run_id: str, change: Change, change_id: int, row: ClauseRow, found: set[str],
                     head: dict, metas: dict, stage: str = "judge") -> dict:
    """One model's verdict on one (change, clause) pair, saved to the audit trail. A model failure becomes
    an "unsure" verdict that a person must check; the caller counts it from `model_error`."""
    model = svc.judge if stage == "judge" else svc.escalate
    pair = Pair(head["id"], head["title"], change, row, metas[row.policy_id]["title"], frozenset(found))
    prompt = render_pair(pair)
    prompt_hash = hashlib.sha256((JUDGE_SYSTEM + prompt).encode()).hexdigest()
    try:
        verdict = check_evidence(pair, await model.assess(pair))
    except Exception:
        log.exception("%s of %s against §%s failed", stage, row.key, change.number)
        return {**MODEL_ERROR, "model_error": True}
    await svc.store.save_judgement(run_id, change_id, row, sorted(found), stage, model.name, prompt_hash, verdict, prompt)
    return verdict


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
