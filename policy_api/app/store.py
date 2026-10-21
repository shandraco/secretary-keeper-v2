"""Storage interface, plus an in-memory store used by the demo and the tests.

The production store is PgStore in pgstore.py. Both implement the same methods.
"""
from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .diff import Change
    from .documents import PolicyDoc

# (from_number, to_policy, to_number or None, source)
Ref = tuple[str, str, "str | None", str]


@dataclass(frozen=True)
class ClauseRow:
    policy_id: str
    number: str
    heading: str
    body: str

    @property
    def key(self) -> str:
        return f"{self.policy_id}§{self.number}"

    @property
    def text(self) -> str:
        return f"{self.heading}\n{self.body}".strip() if self.heading else self.body


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class MemoryStore:
    def __init__(self) -> None:
        self.policies: dict[str, dict] = {}
        self.versions: dict[str, dict[int, str]] = {}
        self.current: dict[str, list[tuple[ClauseRow, list[float]]]] = {}
        self.refs: dict[str, list[tuple[str, str, str | None]]] = {}
        self.subsets: dict[str, set[str]] = {}
        self.uploads: dict[str, dict] = {}
        self.runs: dict[str, dict] = {}
        self.changes: list[dict] = []
        self.judgements: list[dict] = []
        self.jobs: list[dict] = []

    async def close(self) -> None:
        pass

    # --- ingest ---
    async def upsert_policy(self, doc: PolicyDoc, vectors: list[list[float]], refs: list[Ref]) -> int:
        assert doc.id
        versions = self.versions.setdefault(doc.id, {})
        version = doc.version or (max(versions, default=0) + 1)
        versions[version] = doc.body
        self.policies[doc.id] = {"id": doc.id, "title": doc.title, "owner": doc.owner, "parent": doc.parent}
        if version >= max(versions):
            self.current[doc.id] = [
                (ClauseRow(doc.id, c.number, c.heading, c.body), v) for c, v in zip(doc.clauses, vectors)
            ]
            self.refs[doc.id] = [(n, tp, tn) for n, tp, tn, _src in refs]
        for members in self.subsets.values():
            members.discard(doc.id)
        for name in doc.subsets:
            self.subsets.setdefault(name, set()).add(doc.id)
        return version

    # --- lookups ---
    async def all_policies(self) -> list[dict]:
        return list(self.policies.values())

    async def get_policies(self, ids: list[str]) -> dict[str, dict]:
        return {i: self.policies[i] for i in ids if i in self.policies}

    async def list_subsets(self) -> list[dict]:
        return [{"name": n, "count": len(m)} for n, m in sorted(self.subsets.items()) if m]

    async def subset_policy_ids(self, name: str) -> list[str]:
        for n, members in self.subsets.items():
            if n.lower() == name.lower():
                return sorted(members)
        return []

    async def latest_versions(self, policy_id: str, n: int = 2) -> list[tuple[int, str]]:
        versions = self.versions.get(policy_id, {})
        return [(v, versions[v]) for v in sorted(versions, reverse=True)[:n]]

    async def refs_into(self, head_id: str, from_ids: list[str]) -> list[tuple[ClauseRow, str | None]]:
        out = []
        for pid in from_ids:
            rows = {r.number: r for r, _ in self.current.get(pid, [])}
            for from_number, to_policy, to_number in self.refs.get(pid, []):
                if to_policy == head_id and from_number in rows:
                    out.append((rows[from_number], to_number))
        return out

    async def clauses_using_term(self, term: str, policy_ids: list[str]) -> list[ClauseRow]:
        t = term.lower()
        return [r for pid in policy_ids for r, _ in self.current.get(pid, []) if t in r.text.lower()]

    async def nearest(self, vector: list[float], policy_ids: list[str], k: int) -> list[ClauseRow]:
        scored = [(_cosine(vector, v), r) for pid in policy_ids for r, v in self.current.get(pid, [])]
        return [r for _, r in sorted(scored, key=lambda t: -t[0])[:k]]

    async def best_match(self, body: str, limit: int = 3) -> list[dict]:
        sample = body[:2000]
        scored = []
        for pid, versions in self.versions.items():
            latest = versions[max(versions)][:2000]
            score = SequenceMatcher(None, sample, latest).ratio()
            scored.append({"id": pid, "title": self.policies[pid]["title"], "score": round(score, 3)})
        return sorted(scored, key=lambda m: -m["score"])[:limit]

    # --- uploads and runs ---
    async def save_upload(self, filename: str, body: str) -> str:
        upload_id = str(uuid.uuid4())
        self.uploads[upload_id] = {"id": upload_id, "filename": filename, "body": body, "created_at": now()}
        return upload_id

    async def get_upload(self, upload_id: str) -> dict | None:
        return self.uploads.get(upload_id)

    async def create_run(self, head_policy: str, subset: str, upload_id: str | None, requested_by: str | None,
                         max_attempts: int = 3) -> str:
        run_id = str(uuid.uuid4())
        self.runs[run_id] = {
            "id": run_id, "head_policy": head_policy, "subset": subset, "upload_id": upload_id,
            "requested_by": requested_by, "status": "queued", "progress": "Waiting to start…", "error": None,
            "result": None, "created_at": now(), "finished_at": None,
        }
        await self.enqueue("impact_run", {"run_id": run_id}, max_attempts=max_attempts)
        return run_id

    async def update_run(self, run_id: str, **fields: Any) -> None:
        self.runs[run_id].update(fields)

    async def reset_run(self, run_id: str) -> None:
        """Forget a half-finished attempt so a retry starts clean."""
        self.judgements = [j for j in self.judgements if j["run_id"] != run_id]
        self.changes = [c for c in self.changes if c["run_id"] != run_id]

    # --- job queue (same behaviour as PgStore, for demo mode and tests) ---
    async def enqueue(self, kind: str, payload: dict, max_attempts: int = 3, dedupe_key: str | None = None,
                      delay_s: float = 0) -> int | None:
        if dedupe_key and any(j["dedupe_key"] == dedupe_key and j["status"] in ("pending", "running") for j in self.jobs):
            return None
        job = {"id": len(self.jobs) + 1, "kind": kind, "payload": payload, "status": "pending", "attempts": 0,
               "max_attempts": max_attempts, "run_at": time.time() + delay_s, "locked_by": None,
               "locked_until": 0.0, "last_error": None, "dedupe_key": dedupe_key}
        self.jobs.append(job)
        return job["id"]

    async def claim_job(self, worker_id: str, lease_s: float) -> dict | None:
        t = time.time()
        for j in self.jobs:
            due = j["run_at"] <= t and (j["status"] == "pending" or (j["status"] == "running" and j["locked_until"] < t))
            if due:
                j.update(status="running", attempts=j["attempts"] + 1, locked_by=worker_id, locked_until=t + lease_s)
                return {k: j[k] for k in ("id", "kind", "payload", "attempts", "max_attempts")}
        return None

    async def extend_lease(self, job_id: int, worker_id: str, lease_s: float) -> bool:
        j = self.jobs[job_id - 1]
        if j["status"] != "running" or j["locked_by"] != worker_id:
            return False
        j["locked_until"] = time.time() + lease_s
        return True

    async def finish_job(self, job_id: int, worker_id: str) -> None:
        j = self.jobs[job_id - 1]
        if j["locked_by"] == worker_id:
            j.update(status="done", locked_until=0.0)

    async def fail_job(self, job_id: int, worker_id: str, error: str, retry_in_s: float | None) -> str:
        j = self.jobs[job_id - 1]
        if j["locked_by"] != worker_id:
            return j["status"]
        if retry_in_s is None or j["attempts"] >= j["max_attempts"]:
            j.update(status="dead", last_error=error, locked_until=0.0)
        else:
            j.update(status="pending", last_error=error, locked_by=None, run_at=time.time() + retry_in_s)
        return j["status"]

    async def release_worker_jobs(self, worker_id: str) -> int:
        mine = [j for j in self.jobs if j["status"] == "running" and j["locked_by"] == worker_id]
        for j in mine:
            j["locked_until"] = 0.0
        return len(mine)

    async def job_stats(self) -> dict:
        t = time.time()
        due = [j for j in self.jobs if j["status"] == "pending" and j["run_at"] <= t]
        return {"waiting": len(due), "running": sum(j["status"] == "running" for j in self.jobs),
                "dead": sum(j["status"] == "dead" for j in self.jobs),
                "oldest_wait_s": round(t - min((j["run_at"] for j in due), default=t))}

    async def get_run(self, run_id: str) -> dict | None:
        return self.runs.get(run_id)

    async def find_run_ids(self, prefix: str, status: str | None = None) -> list[str]:
        """Ids of runs that start with this text; at most two, enough to tell unique from ambiguous."""
        return [i for i, r in self.runs.items() if i.startswith(prefix) and (status is None or r.get("status") == status)][:2]

    async def save_change(self, run_id: str, change: Change) -> int:
        change_id = max((c["id"] for c in self.changes), default=0) + 1
        self.changes.append({"id": change_id, "run_id": run_id, **change.__dict__})
        return change_id

    # --- review queue (same behaviour as PgStore) ---
    async def review_candidates(self, limit: int, sample_no: float) -> list[dict]:
        final: dict[tuple, dict] = {}
        for j in self.judgements:
            final[(j["run_id"], j["change_id"], j["candidate"])] = j  # later stage wins
        out = []
        for j in sorted(final.values(), key=lambda j: j["id"]):
            run = self.runs.get(j["run_id"])
            if j.get("review_pushed_at") or not run or run["status"] != "done":
                continue
            if j["result"]["impacted"] not in ("yes", "unsure") and j["id"] % 100 >= round(sample_no * 100):
                continue
            ch = next(c for c in self.changes if c["id"] == j["change_id"])
            pid, number = j["candidate"].split("§", 1)
            clause = next((r for r, _ in self.current.get(pid, []) if r.number == number), None)
            out.append({
                "id": j["id"], "run_id": j["run_id"], "result": j["result"], "prompt": j["prompt"],
                "candidate_policy": pid, "candidate_number": number, "head_policy": run["head_policy"],
                "number": ch["number"], "old_number": ch.get("old_number"), "kind": ch["kind"],
                "heading": ch.get("heading"), "old_text": ch.get("old_text"), "new_text": ch.get("new_text"),
                "candidate_title": self.policies.get(pid, {}).get("title", ""),
                "clause_heading": clause.heading if clause else "", "clause_body": clause.body if clause else "",
            })
            if len(out) >= limit:
                break
        return out

    async def mark_review_pushed(self, ids: list[int]) -> None:
        for j in self.judgements:
            if j["id"] in ids:
                j["review_pushed_at"] = now()

    async def last_review_at(self) -> str | None:
        return max((j["reviewed_at"] for j in self.judgements if j.get("reviewed_at")), default=None)

    async def save_review_decisions(self, decisions: list) -> int:
        by_id = {j["id"]: j for j in self.judgements}
        n = 0
        for d in decisions:
            if d.judgement_id in by_id:
                by_id[d.judgement_id].update(reviewer_decision=d.decision, reviewer_impact_type=d.impact_type or None,
                                             reviewer_note=d.note or None, reviewed_by=d.reviewer, reviewed_at=d.reviewed_at)
                n += 1
        return n

    async def claim_review(self, run_id: str) -> bool:
        """needs_review -> queued, once. False if someone else got there first."""
        run = self.runs.get(run_id)
        if not run or run["status"] != "needs_review":
            return False
        run.update(status="queued", progress="Waiting to apply the decisions…")
        return True

    async def delete_judgements(self, run_id: str, stage: str) -> None:
        self.judgements = [j for j in self.judgements if not (j["run_id"] == run_id and j["stage"] == stage)]

    async def save_judgement(self, run_id: str, change_id: int, row: ClauseRow, found_by: list[str],
                             stage: str, model: str, prompt_hash: str, result: dict, prompt: str = "") -> None:
        self.judgements.append({
            "id": max((j["id"] for j in self.judgements), default=0) + 1, "run_id": run_id, "change_id": change_id, "candidate": row.key,
            "found_by": found_by, "stage": stage, "model": model, "prompt_hash": prompt_hash, "prompt": prompt,
            "result": result, "created_at": now(),
        })
