"""HTTP API used by the Open WebUI tool. Every route except the signed Excel download needs the API key."""
from __future__ import annotations

import asyncio
import hmac
import logging
import re
import uuid
from contextlib import asynccontextmanager
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .answer import render_answer
from .config import load_settings
from .documents import split_clauses
from .safe_parse import ParseTimeout, parse_isolated
from .excel import build_workbook
from .reports.charts import change_matrix_png, impact_types_png, status_by_policy_png
from .reports.compare import build_comparison
from .reports.compose import build_custom
from .reports.docx_report import build_docx
from .reports.pdf_report import build_pdf
from .links import FILE_NAME, excel_url, file_url, verify
from .jobs import Worker
from .pipeline import Services
from .services import build_services

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
SETTINGS = load_settings()
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@asynccontextmanager
async def lifespan(app: FastAPI):
    svc = await build_services(load_settings())
    app.state.svc = svc
    Path(svc.settings.export_dir).mkdir(parents=True, exist_ok=True)
    # Checks run from the job queue in the database, so they survive restarts. Set WORKER_ENABLED=0
    # here when separate `worker` containers do the work.
    app.state.worker = Worker(svc) if svc.settings.worker_enabled else None
    worker_task = asyncio.create_task(app.state.worker.run_forever()) if app.state.worker else None
    yield
    if app.state.worker:
        await app.state.worker.stop()
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
    await svc.close()


app = FastAPI(title="Policy impact API", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
# Blocks DNS-rebinding: a request must name one of our own hostnames.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(SETTINGS.allowed_hosts))


def svc_of(request: Request) -> Services:
    return request.app.state.svc


def require_key(request: Request, authorization: str = Header(default="")) -> None:
    given = authorization.removeprefix("Bearer ").strip().encode()
    if not hmac.compare_digest(given, svc_of(request).settings.api_key.encode()):
        raise HTTPException(401, "Missing or wrong API key.")


AUTH = [Depends(require_key)]


def _score(query: str, *names: str) -> float:
    q = query.strip().lower()
    best = 0.0
    for name in names:
        n = (name or "").lower()
        if not n:
            continue
        if q == n:
            return 1.0
        if q in n or n in q:
            best = max(best, 0.9)
        best = max(best, SequenceMatcher(None, q, n).ratio() * 0.85)
    return round(best, 3)


def _run_id(run_id: str) -> str:
    try:
        return str(uuid.UUID(run_id))
    except ValueError:
        raise HTTPException(404, "No such run.") from None


_SHORT = re.compile(r"[0-9a-f]{8}")


async def _load_run(svc: Services, run_id: str, *, short_status: str | None = None, short_ok: bool = False) -> dict:
    """A run by its full id. With short_ok, also by its 8-character check number (exactly 8 hex characters, nothing
    else), looked up among runs in short_status when that is given. Only the chat-facing status and review
    routes allow it; every other route needs the full id."""
    wanted = run_id.strip().lower()
    try:
        full = str(uuid.UUID(wanted))
    except ValueError:
        if not (short_ok and _SHORT.fullmatch(wanted)):
            raise HTTPException(404, "No such run.") from None
        found = await svc.store.find_run_ids(wanted, short_status)
        if len(found) > 1:
            raise HTTPException(409, "More than one check starts with that number. Please give a longer check number.")
        if not found:
            raise HTTPException(404, "No such run.")
        full = found[0]
    run = await svc.store.get_run(full)
    if not run:
        raise HTTPException(404, "No such run.")
    return run


@app.get("/health")
async def health(request: Request) -> dict:
    return {"ok": True, "demo_mode": svc_of(request).settings.demo_mode}


@app.get("/queue", dependencies=AUTH)
async def queue(request: Request) -> dict:
    """How many checks are waiting, running, or gave up. `oldest_wait_s` growing means no worker is running."""
    return await svc_of(request).store.job_stats()


@app.get("/subsets", dependencies=AUTH)
async def subsets(request: Request, q: str = "") -> dict:
    items = await svc_of(request).store.list_subsets()
    if q:
        items = sorted(({**s, "score": _score(q, s["name"])} for s in items), key=lambda s: -s["score"])
        items = [s for s in items if s["score"] >= 0.4]
    return {"subsets": items}


@app.get("/policies", dependencies=AUTH)
async def policies(request: Request, q: str = Query(min_length=1, max_length=200), limit: int = Query(5, ge=1, le=20)) -> dict:
    items = [{**p, "score": _score(q, p["id"], p["title"])} for p in await svc_of(request).store.all_policies()]
    items = [p for p in sorted(items, key=lambda p: -p["score"]) if p["score"] >= 0.4][:limit]
    return {"policies": items}


SEARCH_TEXT_CHARS = 1500
READ_TEXT_CHARS = 20000


@app.get("/search", dependencies=AUTH)
async def search(request: Request, q: str = Query(min_length=2, max_length=500), group: str = Query("", max_length=200),
                 policy: str = Query("", max_length=100), limit: int = Query(8, ge=1, le=20)) -> dict:
    """Sections of stored policies that best answer a question: embeddings narrow, the reranker orders.
    The assistant answers only from what this returns, and cites policy and section."""
    svc = svc_of(request)
    if policy:
        if policy not in await svc.store.get_policies([policy]):
            raise HTTPException(404, f"There is no policy called {policy}.")
        ids = [policy]
    elif group:
        names = {s["name"].lower(): s["name"] for s in await svc.store.list_subsets()}
        if group.lower() not in names:
            raise HTTPException(404, f"There is no policy group called {group}.")
        ids = await svc.store.subset_policy_ids(names[group.lower()])
    else:
        ids = [p["id"] for p in await svc.store.all_policies()]
    [vector] = await svc.embedder.embed([q])
    rows = await svc.store.nearest(vector, ids, max(30, limit * 3))
    if not rows:
        return {"results": []}
    scores = await svc.reranker.rerank(q, [r.text for r in rows])
    metas = await svc.store.get_policies(list({r.policy_id for r in rows}))
    ranked = sorted(zip(scores, rows), key=lambda t: -t[0])[:limit]
    return {"results": [{
        "policy_id": r.policy_id, "policy_title": metas[r.policy_id]["title"], "clause": r.number,
        "heading": r.heading, "text": r.body[:SEARCH_TEXT_CHARS], "score": round(float(s), 3),
    } for s, r in ranked]}


@app.get("/policies/{policy_id}/text", dependencies=AUTH)
async def policy_text(request: Request, policy_id: str, version: int | None = Query(None, ge=1),
                      clause: str = Query("", max_length=50)) -> dict:
    """A stored policy's wording: the whole policy, or one section with its subsections."""
    svc = svc_of(request)
    pol = (await svc.store.get_policies([policy_id])).get(policy_id)
    if not pol:
        raise HTTPException(404, f"There is no policy called {policy_id}.")
    versions = dict(await svc.store.latest_versions(policy_id, 1000))
    v = version or max(versions)
    if v not in versions:
        raise HTTPException(404, f"{policy_id} has no version {v}. Stored versions: {', '.join(map(str, sorted(versions)))}.")
    body = versions[v]
    if clause:
        c = clause.strip().lstrip("§").strip()
        parts = [x for x in split_clauses(body) if x.number == c or x.number.startswith(c + ".")]
        if not parts:
            raise HTTPException(404, f"{policy_id} version {v} has no section {c}.")
        body = "\n\n".join(f"§{x.number} {x.text}" for x in parts)
    return {"policy_id": policy_id, "title": pol["title"], "version": v, "versions": sorted(versions),
            "clause": clause or None, "text": body[:READ_TEXT_CHARS], "truncated": len(body) > READ_TEXT_CHARS}


@app.post("/uploads", dependencies=AUTH)
async def upload(request: Request, file: UploadFile = File(...)) -> dict:
    svc = svc_of(request)
    limit = svc.settings.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"The file is larger than {svc.settings.max_upload_mb} MB.")
    filename = Path(file.filename or "upload.txt").name
    try:
        doc = await parse_isolated(filename, data, svc.settings.policy_id_pattern)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except ParseTimeout:
        raise HTTPException(400, "This file took too long to read. Try saving it as .docx or .txt.") from None
    if not doc.clauses or all(c.number == "0" for c in doc.clauses):
        raise HTTPException(400, "No numbered sections were found in this file, so it can't be compared section by section.")
    upload_id = await svc.store.save_upload(filename, doc.body)
    known = await svc.store.get_policies([doc.id]) if doc.id else {}
    if doc.id in known:
        matches = [{"id": doc.id, "title": known[doc.id]["title"], "score": 1.0}]
    else:
        matches = await svc.store.best_match(doc.body, 3)
    return {"upload_id": upload_id, "filename": filename, "sections": len(doc.clauses), "matches": matches}


class RunRequest(BaseModel):
    head_policy: str = Field(min_length=1, max_length=100)
    subset: str = Field(min_length=1, max_length=200)
    upload_id: str | None = None
    requested_by: str | None = Field(default=None, max_length=200)


@app.post("/runs", dependencies=AUTH, status_code=202)
async def start_run(request: Request, body: RunRequest) -> dict:
    svc = svc_of(request)
    if body.head_policy not in await svc.store.get_policies([body.head_policy]):
        raise HTTPException(404, f"There is no policy called {body.head_policy}.")
    names = {s["name"].lower(): s["name"] for s in await svc.store.list_subsets()}
    subset = names.get(body.subset.lower())
    if not subset:
        raise HTTPException(404, f"There is no policy group called {body.subset}.")
    if body.upload_id and not await svc.store.get_upload(_run_id(body.upload_id)):
        raise HTTPException(404, "The uploaded file could not be found. Please upload it again.")
    run_id = await svc.store.create_run(body.head_policy, subset, body.upload_id, body.requested_by,
                                        max_attempts=svc.settings.job_max_attempts)
    if request.app.state.worker:
        request.app.state.worker.poke()
    return {"run_id": run_id, "status": "queued"}


@app.get("/runs/{run_id}", dependencies=AUTH)
async def run_status(request: Request, run_id: str) -> dict:
    svc = svc_of(request)
    run = await _load_run(svc, run_id, short_ok=True)
    out = {"run_id": run["id"], "status": run["status"], "progress": run["progress"], "error": run["error"]}
    if run["status"] == "needs_review":
        out["review"] = [{"policy_id": r["policy_id"], "policy_title": r["policy_title"], "clause": r["clause"],
                          "clause_heading": r["clause_heading"], "why": r["why"]}
                         for r in run["result"]["rows"] if r["status"] == "Maybe"]
    if run["status"] == "done":
        s = svc.settings
        url = excel_url(s.public_base_url, s.link_signing_key, run["id"], s.link_ttl_hours)
        out.update(counts=run["result"]["counts"], excel_url=url, answer_markdown=render_answer(run["result"], url))
    return out


@app.get("/runs/{run_id}/results", dependencies=AUTH)
async def run_results(request: Request, run_id: str) -> dict:
    run = await _load_run(svc_of(request), run_id)
    if run["status"] != "done":
        raise HTTPException(409, "This run has not finished.")
    return run["result"]


class Decision(BaseModel):
    policy_id: str = Field(min_length=1, max_length=100)
    clause: str = Field(min_length=1, max_length=50)
    affected: bool


class ReviewRequest(BaseModel):
    decisions: list[Decision] = Field(min_length=1, max_length=500)
    reviewed_by: str | None = Field(default=None, max_length=200)


@app.post("/runs/{run_id}/review", dependencies=AUTH, status_code=202)
async def review_run(request: Request, run_id: str, body: ReviewRequest) -> dict:
    """A person's decisions on the "maybe" sections of a check paused for review (REVIEW_GATE=1)."""
    svc = svc_of(request)
    run = await _load_run(svc, run_id, short_ok=True, short_status="needs_review")
    if run["status"] != "needs_review":
        raise HTTPException(409, "This check is not waiting for a decision.")
    waiting = {(r["policy_id"], r["clause"]) for r in run["result"]["rows"] if r["status"] == "Maybe"}
    unknown = [f"{d.policy_id} §{d.clause}" for d in body.decisions if (d.policy_id, d.clause) not in waiting]
    if unknown:
        raise HTTPException(422, f"These sections are not waiting for a decision: {', '.join(unknown)}.")
    answer = {"decisions": [d.model_dump() for d in body.decisions], "by": body.reviewed_by or ""}
    if not await svc.store.claim_review(run["id"]):
        raise HTTPException(409, "This check is not waiting for a decision.")
    await svc.store.enqueue("review_run", {"run_id": run["id"], "answer": answer},
                            max_attempts=svc.settings.job_max_attempts)
    if request.app.state.worker:
        request.app.state.worker.poke()
    return {"run_id": run["id"], "status": "queued"}


MEDIA = {
    "xlsx": XLSX,
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "png": "image/png",
}
RUN_FILES = {  # what a finished run can be turned into: suffix -> (label, builder)
    ".xlsx": ("Excel workbook (all evidence, review column, charts)", lambda r: build_workbook(r)),
    ".pdf": ("PDF report", lambda r: build_pdf(r)),
    ".docx": ("Word report", lambda r: build_docx(r)),
    "-status.png": ("Chart: affected sections by policy", lambda r: status_by_policy_png(r)),
    "-matrix.png": ("Chart: which change affects which policy", lambda r: change_matrix_png(r)),
    "-types.png": ("Chart: kinds of impact", lambda r: impact_types_png(r)),
}
FORMAT_SUFFIXES = {"xlsx": [".xlsx"], "pdf": [".pdf"], "docx": [".docx"],
                   "charts": ["-status.png", "-matrix.png", "-types.png"]}


def _link(svc: Services, name: str, label: str) -> dict:
    s = svc.settings
    return {"label": label, "file": name, "url": file_url(s.public_base_url, s.link_signing_key, name, s.link_ttl_hours)}


async def _write(svc: Services, name: str, build) -> None:
    path = Path(svc.settings.export_dir) / name
    if not path.exists():
        data = await asyncio.to_thread(build)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)


class RunFilesRequest(BaseModel):
    formats: list[Literal["xlsx", "pdf", "docx", "charts"]] = Field(min_length=1, max_length=4)


@app.post("/runs/{run_id}/files", dependencies=AUTH)
async def run_files(request: Request, run_id: str, body: RunFilesRequest) -> dict:
    """Build the requested files for a finished run and return signed download links."""
    svc = svc_of(request)
    run = await _load_run(svc, run_id)
    if run["status"] != "done":
        raise HTTPException(409, "This check has not finished yet.")
    links = []
    for fmt in dict.fromkeys(body.formats):
        for suffix in FORMAT_SUFFIXES[fmt]:
            label, build = RUN_FILES[suffix]
            name = f"{run['id']}{suffix}"
            await _write(svc, name, lambda b=build: b(run["result"]))
            links.append(_link(svc, name, label))
    return {"files": links}


class PolicyRef(BaseModel):
    policy_id: str | None = Field(default=None, max_length=100)
    version: int | None = Field(default=None, ge=1)
    upload_id: str | None = None


async def _policy_text(svc: Services, ref: PolicyRef) -> tuple[str, str]:
    """(label, full text) for a stored policy version or an uploaded file."""
    if ref.upload_id:
        up = await svc.store.get_upload(_run_id(ref.upload_id))
        if not up:
            raise HTTPException(404, "The uploaded file could not be found. Please upload it again.")
        return f"uploaded file {up['filename']}", up["body"]
    if not ref.policy_id:
        raise HTTPException(422, "Name a policy or attach a file.")
    pol = (await svc.store.get_policies([ref.policy_id])).get(ref.policy_id)
    if not pol:
        raise HTTPException(404, f"There is no policy called {ref.policy_id}.")
    versions = dict(await svc.store.latest_versions(ref.policy_id, 1000))
    v = ref.version or max(versions)
    if v not in versions:
        have = ", ".join(str(n) for n in sorted(versions))
        raise HTTPException(404, f"{ref.policy_id} has no version {v}. Stored versions: {have}.")
    return f"{ref.policy_id} {pol['title']} (version {v})", versions[v]


class CompareRequest(BaseModel):
    left: PolicyRef
    right: PolicyRef
    format: Literal["pdf", "docx", "xlsx"] = "docx"


@app.post("/compare", dependencies=AUTH)
async def compare(request: Request, body: CompareRequest) -> dict:
    """Side-by-side, section-by-section comparison of two policies or two versions of one."""
    svc = svc_of(request)
    (l_label, l_text), (r_label, r_text) = await _policy_text(svc, body.left), await _policy_text(svc, body.right)
    name = f"{uuid.uuid4()}.{body.format}"
    await _write(svc, name, lambda: build_comparison(body.format, l_label, l_text, r_label, r_text))
    return {"files": [_link(svc, name, f"Comparison: {l_label} vs {r_label}")]}


class DocumentRequest(BaseModel):
    spec: dict
    format: Literal["pdf", "docx", "xlsx", "png"] = "docx"


@app.post("/documents", dependencies=AUTH)
async def documents(request: Request, body: DocumentRequest) -> dict:
    """A document, workbook or chart the assistant writes at a person's request."""
    svc = svc_of(request)
    name = f"{uuid.uuid4()}.{body.format}"
    try:
        await _write(svc, name, lambda: build_custom(body.spec, body.format))
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    title = str(body.spec.get("title") or "Document")[:120]
    return {"files": [_link(svc, name, title)]}


@app.get("/exports/{name}")
async def export(request: Request, name: str, exp: int, sig: str = Query(max_length=128)) -> FileResponse:
    svc = svc_of(request)
    m = FILE_NAME.match(name)
    if not m:
        raise HTTPException(404, "No such file.")
    if not verify(svc.settings.link_signing_key, name, exp, sig):
        raise HTTPException(403, "This download link has expired or is not valid. Ask the assistant for a new one.")
    ext = m.group(2)
    path = Path(svc.settings.export_dir) / name
    nice = name
    run = await svc.store.get_run(name[:36]) if _is_uuid(name[:36]) else None
    if run:
        if run["status"] != "done":
            raise HTTPException(409, "This run has not finished.")
        suffix = name[36:]
        await _write(svc, name, lambda: RUN_FILES[suffix][1](run["result"]))  # rebuilt if it was cleaned up
        nice = f"policy-impact-{run['result']['head']['id']}-{run['result']['finished_at'][:10]}{suffix}"
    elif not path.exists():
        raise HTTPException(404, "This file is no longer available. Ask the assistant to make it again.")
    inline = ext in ("png", "pdf")  # charts show in the chat; PDFs open in the browser
    return FileResponse(path, media_type=MEDIA[ext], filename=nice,
                        content_disposition_type="inline" if inline else "attachment")


def _is_uuid(text: str) -> bool:
    try:
        uuid.UUID(text)
        return True
    except ValueError:
        return False
