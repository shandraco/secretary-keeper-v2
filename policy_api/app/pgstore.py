"""Postgres + pgvector store. Schema: db/init.sql."""
from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .diff import Change
from .documents import PolicyDoc
from .migrate import migrate
from .store import ClauseRow, Ref

_RUN_FIELDS = {"status", "progress", "error", "result", "finished_at"}


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


def _row(r: dict) -> ClauseRow:
    return ClauseRow(r["policy_id"], r["number"], r["heading"], r["body"])


def _iso(value: Any) -> Any:
    return value.isoformat(timespec="seconds") if hasattr(value, "isoformat") else value


class PgStore:
    def __init__(self, url: str) -> None:
        self.pool = AsyncConnectionPool(url, min_size=1, max_size=10, kwargs={"row_factory": dict_row}, open=False)

    async def open(self) -> None:
        await self.pool.open(wait=True, timeout=30)
        await migrate(self.pool)

    async def close(self) -> None:
        await self.pool.close()

    async def _all(self, sql: str, *args: Any) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, args)
            return await cur.fetchall()

    async def _one(self, sql: str, *args: Any) -> dict | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, args)
            return await cur.fetchone()

    # --- ingest ---
    async def upsert_policy(self, doc: PolicyDoc, vectors: list[list[float]], refs: list[Ref]) -> int:
        assert doc.id
        async with self.pool.connection() as conn:  # one transaction, committed on exit
            await conn.execute(
                "INSERT INTO policies (id, title, owner, parent_id) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, owner = EXCLUDED.owner, parent_id = EXCLUDED.parent_id",
                (doc.id, doc.title, doc.owner, doc.parent),
            )
            version = doc.version
            if version is None:
                cur = await conn.execute(
                    "SELECT coalesce(max(version), 0) + 1 AS v FROM policy_versions WHERE policy_id = %s", (doc.id,)
                )
                version = (await cur.fetchone())["v"]
            await conn.execute(
                "INSERT INTO policy_versions (policy_id, version, body) VALUES (%s, %s, %s) "
                "ON CONFLICT (policy_id, version) DO UPDATE SET body = EXCLUDED.body, ingested_at = now()",
                (doc.id, version, doc.body),
            )
            await conn.execute("DELETE FROM clauses WHERE policy_id = %s AND version = %s", (doc.id, version))
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO clauses (policy_id, version, number, heading, body, embedding) "
                    "VALUES (%s, %s, %s, %s, %s, %s::vector)",
                    [(doc.id, version, c.number, c.heading, c.body, _vec(v)) for c, v in zip(doc.clauses, vectors)],
                )
            cur = await conn.execute("SELECT max(version) AS v FROM policy_versions WHERE policy_id = %s", (doc.id,))
            if (await cur.fetchone())["v"] == version:
                await conn.execute("DELETE FROM refs WHERE from_policy = %s", (doc.id,))
                async with conn.cursor() as cur:
                    await cur.executemany(
                        "INSERT INTO refs (from_policy, from_number, to_policy, to_number, source) VALUES (%s, %s, %s, %s, %s)",
                        [(doc.id, n, tp, tn, src) for n, tp, tn, src in refs],
                    )
            await conn.execute("DELETE FROM subsets WHERE policy_id = %s", (doc.id,))
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO subsets (name, policy_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    [(name, doc.id) for name in doc.subsets],
                )
        return version

    # --- lookups ---
    async def all_policies(self) -> list[dict]:
        return await self._all("SELECT id, title, owner, parent_id AS parent FROM policies")

    async def get_policies(self, ids: list[str]) -> dict[str, dict]:
        rows = await self._all("SELECT id, title, owner, parent_id AS parent FROM policies WHERE id = ANY(%s)", list(ids))
        return {r["id"]: r for r in rows}

    async def list_subsets(self) -> list[dict]:
        return await self._all("SELECT name, count(*)::int AS count FROM subsets GROUP BY name ORDER BY name")

    async def subset_policy_ids(self, name: str) -> list[str]:
        rows = await self._all("SELECT policy_id FROM subsets WHERE lower(name) = lower(%s) ORDER BY policy_id", name)
        return [r["policy_id"] for r in rows]

    async def latest_versions(self, policy_id: str, n: int = 2) -> list[tuple[int, str]]:
        rows = await self._all(
            "SELECT version, body FROM policy_versions WHERE policy_id = %s ORDER BY version DESC LIMIT %s", policy_id, n
        )
        return [(r["version"], r["body"]) for r in rows]

    async def refs_into(self, head_id: str, from_ids: list[str]) -> list[tuple[ClauseRow, str | None]]:
        rows = await self._all(
            "SELECT c.policy_id, c.number, c.heading, c.body, r.to_number FROM refs r "
            "JOIN current_clauses c ON c.policy_id = r.from_policy AND c.number = r.from_number "
            "WHERE r.to_policy = %s AND r.from_policy = ANY(%s)",
            head_id, list(from_ids),
        )
        return [(_row(r), r["to_number"]) for r in rows]

    async def clauses_using_term(self, term: str, policy_ids: list[str]) -> list[ClauseRow]:
        rows = await self._all(
            "SELECT policy_id, number, heading, body FROM current_clauses "
            "WHERE policy_id = ANY(%s) AND strpos(lower(heading || ' ' || body), lower(%s)) > 0",
            list(policy_ids), term,
        )
        return [_row(r) for r in rows]

    async def nearest(self, vector: list[float], policy_ids: list[str], k: int) -> list[ClauseRow]:
        rows = await self._all(
            "SELECT policy_id, number, heading, body FROM current_clauses "
            "WHERE policy_id = ANY(%s) AND embedding IS NOT NULL ORDER BY embedding <=> %s::vector LIMIT %s",
            list(policy_ids), _vec(vector), k,
        )
        return [_row(r) for r in rows]

    async def best_match(self, body: str, limit: int = 3) -> list[dict]:
        rows = await self._all(
            "SELECT p.id, p.title, round(similarity(left(v.body, 3000), left(%s, 3000))::numeric, 3)::float AS score "
            "FROM policies p JOIN LATERAL (SELECT body FROM policy_versions WHERE policy_id = p.id "
            "ORDER BY version DESC LIMIT 1) v ON true ORDER BY score DESC LIMIT %s",
            body, limit,
        )
        return [dict(r) for r in rows]

    # --- uploads and runs ---
    async def save_upload(self, filename: str, body: str) -> str:
        row = await self._one("INSERT INTO uploads (filename, body) VALUES (%s, %s) RETURNING id::text", filename, body)
        return row["id"]

    async def get_upload(self, upload_id: str) -> dict | None:
        return await self._one("SELECT id::text, filename, body FROM uploads WHERE id = %s::uuid", upload_id)

    async def create_run(self, head_policy: str, subset: str, upload_id: str | None, requested_by: str | None,
                         max_attempts: int = 3) -> str:
        # The run and its job are written in one transaction: either both exist or neither does.
        async with self.pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "INSERT INTO runs (head_policy, subset, upload_id, requested_by, status, progress) "
                    "VALUES (%s, %s, %s::uuid, %s, 'queued', 'Waiting to start…') RETURNING id::text",
                    (head_policy, subset, upload_id, requested_by),
                )
                run_id = (await cur.fetchone())["id"]
                await conn.execute(
                    "INSERT INTO jobs (kind, payload, max_attempts) VALUES ('impact_run', %s, %s)",
                    (Jsonb({"run_id": run_id}), max_attempts),
                )
        return run_id

    async def update_run(self, run_id: str, **fields: Any) -> None:
        unknown = set(fields) - _RUN_FIELDS
        if unknown:
            raise ValueError(f"Unknown run fields: {unknown}")
        cols = ", ".join(f"{k} = %s" for k in fields)  # keys come from _RUN_FIELDS only
        values = [Jsonb(v) if k == "result" and v is not None else v for k, v in fields.items()]
        async with self.pool.connection() as conn:
            await conn.execute(f"UPDATE runs SET {cols} WHERE id = %s::uuid", (*values, run_id))

    async def reset_run(self, run_id: str) -> None:
        """Forget a half-finished attempt so a retry starts clean (judgements go with their changes)."""
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM changes WHERE run_id = %s::uuid", (run_id,))

    # --- job queue ---
    async def enqueue(self, kind: str, payload: dict, max_attempts: int = 3, dedupe_key: str | None = None,
                      delay_s: float = 0) -> int | None:
        row = await self._one(
            "INSERT INTO jobs (kind, payload, max_attempts, dedupe_key, run_at) "
            "VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s)) "
            "ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL AND status IN ('pending', 'running') DO NOTHING "
            "RETURNING id",
            kind, Jsonb(payload), max_attempts, dedupe_key, delay_s,
        )
        return row["id"] if row else None

    async def claim_job(self, worker_id: str, lease_s: float) -> dict | None:
        return await self._one(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1, locked_by = %s, "
            "locked_until = now() + make_interval(secs => %s) "
            "WHERE id = (SELECT id FROM jobs WHERE run_at <= now() "
            "  AND (status = 'pending' OR (status = 'running' AND locked_until < now())) "
            "  ORDER BY run_at, id FOR UPDATE SKIP LOCKED LIMIT 1) "
            "RETURNING id, kind, payload, attempts, max_attempts",
            worker_id, lease_s,
        )

    async def extend_lease(self, job_id: int, worker_id: str, lease_s: float) -> bool:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE jobs SET locked_until = now() + make_interval(secs => %s) "
                "WHERE id = %s AND locked_by = %s AND status = 'running'",
                (lease_s, job_id, worker_id),
            )
            return cur.rowcount == 1

    async def finish_job(self, job_id: int, worker_id: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE jobs SET status = 'done', finished_at = now(), locked_until = NULL "
                "WHERE id = %s AND locked_by = %s AND status = 'running'",
                (job_id, worker_id),
            )

    async def fail_job(self, job_id: int, worker_id: str, error: str, retry_in_s: float | None) -> str:
        row = await self._one(
            "UPDATE jobs SET last_error = %s, locked_until = NULL, "
            "  status = CASE WHEN %s::float8 IS NULL OR attempts >= max_attempts THEN 'dead' ELSE 'pending' END, "
            "  finished_at = CASE WHEN %s::float8 IS NULL OR attempts >= max_attempts THEN now() END, "
            "  locked_by = CASE WHEN %s::float8 IS NULL OR attempts >= max_attempts THEN locked_by END, "
            "  run_at = now() + make_interval(secs => coalesce(%s::float8, 0)) "
            "WHERE id = %s AND locked_by = %s AND status = 'running' RETURNING status",
            error[:2000], retry_in_s, retry_in_s, retry_in_s, retry_in_s, job_id, worker_id,
        )
        return row["status"] if row else "lost"

    async def release_worker_jobs(self, worker_id: str) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE jobs SET locked_until = now() - interval '1 second' WHERE status = 'running' AND locked_by = %s",
                (worker_id,),
            )
            return cur.rowcount

    async def job_stats(self) -> dict:
        row = await self._one(
            "SELECT count(*) FILTER (WHERE status = 'pending' AND run_at <= now()) AS waiting, "
            "count(*) FILTER (WHERE status = 'running') AS running, "
            "count(*) FILTER (WHERE status = 'dead') AS dead, "
            "coalesce(extract(epoch FROM now() - min(run_at) FILTER (WHERE status = 'pending' AND run_at <= now())), 0)::int "
            "AS oldest_wait_s FROM jobs"
        )
        return dict(row)

    async def get_run(self, run_id: str) -> dict | None:
        row = await self._one(
            "SELECT id::text, head_policy, subset, upload_id::text, requested_by, status, progress, error, result, "
            "created_at, finished_at FROM runs WHERE id = %s::uuid",
            run_id,
        )
        return {k: _iso(v) for k, v in row.items()} if row else None

    async def save_change(self, run_id: str, change: Change) -> int:
        row = await self._one(
            "INSERT INTO changes (run_id, number, old_number, kind, heading, old_text, new_text, terms) "
            "VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            run_id, change.number, change.old_number, change.kind, change.heading,
            change.old_text, change.new_text, change.terms,
        )
        return row["id"]

    # --- review queue ---
    async def review_candidates(self, limit: int, sample_no: float) -> list[dict]:
        """Final verdicts from finished runs not yet sent for review: every yes and unsure, and a fixed
        sample of no (so reviewers also catch what the model missed)."""
        return await self._all(
            "SELECT * FROM ("
            "  SELECT DISTINCT ON (j.run_id, j.change_id, j.candidate_policy, j.candidate_number) "
            "    j.id, j.run_id::text AS run_id, j.result, j.prompt, j.candidate_policy, j.candidate_number, "
            "    j.review_pushed_at, r.head_policy, r.status AS run_status, c.number, c.old_number, c.kind, "
            "    c.heading, c.old_text, c.new_text, p.title AS candidate_title, "
            "    cc.heading AS clause_heading, cc.body AS clause_body "
            "  FROM judgements j JOIN changes c ON c.id = j.change_id JOIN runs r ON r.id = j.run_id "
            "  LEFT JOIN policies p ON p.id = j.candidate_policy "
            "  LEFT JOIN current_clauses cc ON cc.policy_id = j.candidate_policy AND cc.number = j.candidate_number "
            "  ORDER BY j.run_id, j.change_id, j.candidate_policy, j.candidate_number, j.id DESC"
            ") t WHERE review_pushed_at IS NULL AND run_status = 'done' "
            "  AND (result->>'impacted' IN ('yes', 'unsure') OR (id %% 100) < %s) "
            "ORDER BY id LIMIT %s",
            round(sample_no * 100), limit,
        )

    async def mark_review_pushed(self, ids: list[int]) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE judgements SET review_pushed_at = now() WHERE id = ANY(%s)", (ids,))

    async def last_review_at(self) -> str | None:
        row = await self._one("SELECT max(reviewed_at) AS t FROM judgements")
        return row["t"].isoformat() if row and row["t"] else None

    async def save_review_decisions(self, decisions: list) -> int:
        n = 0
        async with self.pool.connection() as conn:
            for d in decisions:
                cur = await conn.execute(
                    "UPDATE judgements SET reviewer_decision = %s, reviewer_impact_type = %s, reviewer_note = %s, "
                    "reviewed_by = %s, reviewed_at = %s WHERE id = %s",
                    (d.decision, d.impact_type or None, d.note or None, d.reviewer, d.reviewed_at, d.judgement_id),
                )
                n += cur.rowcount
        return n

    async def save_judgement(self, run_id: str, change_id: int, row: ClauseRow, found_by: list[str],
                             stage: str, model: str, prompt_hash: str, result: dict, prompt: str = "") -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO judgements (run_id, change_id, candidate_policy, candidate_number, found_by, stage, "
                "model, prompt_hash, result, prompt) VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (run_id, change_id, row.policy_id, row.number, found_by, stage, model, prompt_hash, Jsonb(result), prompt),
            )
