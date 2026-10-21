"""
title: Policy Upload Filter
author: secretary-keeper
version: 0.1.0
description: Sends documents attached in the chat to policy-api and tells the assistant their upload id. Turns off Open WebUI's own document search for them.
requirements: httpx
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

UPLOAD_MATCH = 0.6  # similarity needed to name the policy an upload is probably a new version of
_UNSAFE = re.compile(r"[\[\]\(\)<>`*_|\\\r\n]+")


def _plain(text) -> str:
    """File names come from users and documents: keep them on one line, without markup."""
    return _UNSAFE.sub(" ", " ".join(str(text).split()))[:120].strip() or "upload"


def _files(body: dict, files: list | None) -> list[dict]:
    """File ids mentioned in the request. Only the id is used: the browser sends this list, so its paths
    and names are never trusted (see Filter._lookup)."""
    seen, out = set(), []
    # Open WebUI's file shape has changed between releases; collect from every known place, once each.
    for f in [*(files or []), *(body.get("files") or []), *((body.get("metadata") or {}).get("files") or [])]:
        if not isinstance(f, dict):
            continue
        info = f.get("file") or {}
        file_id = info.get("id") or f.get("id")
        if file_id and file_id not in seen:
            seen.add(file_id)
            out.append({"id": str(file_id)})
    return out


async def _file_record(file_id: str):
    """Open WebUI's own record of an uploaded file: owner, name and where it is stored."""
    from open_webui.models.files import Files  # only importable inside Open WebUI

    rec = Files.get_file_by_id(file_id)
    return await rec if inspect.isawaitable(rec) else rec


def _append(message: dict, note: str) -> None:
    content = message.get("content")
    if isinstance(content, list):  # multimodal message: add a text part
        content.append({"type": "text", "text": note})
    else:
        message["content"] = f"{content or ''}\n\n{note}".strip()


class Filter:
    class Valves(BaseModel):
        API_URL: str = Field(default="http://policy-api:8000", description="policy-api address inside the Docker network.")
        API_KEY: str = Field(default="", description="The POLICY_API_KEY value from .env.")
        UPLOAD_DIR: str = Field(default="/app/backend/data/uploads", description="Open WebUI's upload folder. Files outside it are refused.")

    def __init__(self):
        self.valves = self.Valves()
        # We handle attached files ourselves, so Open WebUI does not cut them into its own search index.
        self.file_handler = True
        self._done: dict[tuple[str, str], str] = {}  # (user id, file id) -> note, so a resent message does not upload twice
        self._record = _file_record  # replaced in tests, which run without Open WebUI

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.valves.API_URL, timeout=120,
                                 headers={"Authorization": f"Bearer {self.valves.API_KEY}"})

    async def _lookup(self, file_id: str, user: dict) -> tuple[str, Path] | None:
        """(name, path) of a file this user uploaded and that sits in the upload folder, else None."""
        rec = await self._record(file_id)
        if rec is None or not getattr(rec, "path", None):
            return None
        if rec.user_id != user.get("id") and user.get("role") != "admin":
            return None
        path = Path(rec.path).resolve()
        if not path.is_relative_to(Path(self.valves.UPLOAD_DIR).resolve()) or not path.is_file():
            return None
        return rec.filename or path.name, path

    async def _note(self, c: httpx.AsyncClient, file_id: str, user: dict) -> str:
        found = await self._lookup(file_id, user)
        if not found:
            return "[An attached file could not be found. Ask the user to attach it again.]"
        filename, path = found
        name = _plain(filename)
        try:
            r = await c.post("/uploads", files={"file": (Path(filename).name, path.read_bytes())})
        except httpx.HTTPError as e:
            return f"[Attached file {name} could not be sent to the policy service ({type(e).__name__}). Tell the user to try again shortly.]"
        if r.status_code in (400, 413):
            return f"[Attached file {name} could not be used: {_plain(r.json().get('detail', ''))} Tell the user this in plain words.]"
        if r.status_code != 200:
            return f"[Attached file {name} could not be sent to the policy service (error {r.status_code}). Tell the user to try again shortly.]"
        up = r.json()
        best = up["matches"][0] if up.get("matches") else None
        guess = (f" It looks like a new version of {best['id']} {_plain(best['title'])}."
                 if best and best["score"] >= UPLOAD_MATCH else
                 " It does not clearly match a stored policy; ask the user which policy it is a new version of.")
        return (f"[Attached file {name}: upload id {up['upload_id']}, {up['sections']} numbered sections.{guess} "
                "Pass this upload id to the tools; do not quote the file from memory.]")

    async def inlet(self, body: dict, __user__: dict | None = None, __files__: list | None = None) -> dict:
        files = _files(body, __files__)
        if not files:
            return body
        user = __user__ or {}
        notes = []
        async with self._client() as c:
            for f in files:
                key = (str(user.get("id")), f["id"])
                note = self._done.get(key) or await self._note(c, f["id"], user)
                if ": upload id " in note:  # only successes are remembered; a failure is tried again next time
                    self._done[key] = note
                notes.append(note)
        users = [m for m in body.get("messages") or [] if m.get("role") == "user"]
        if users:
            _append(users[-1], "\n".join(notes))
        body["files"] = []
        return body
