"""
title: Policy Upload Filter
author: secretary-keeper
version: 0.2.0
description: Sends documents attached in the chat to policy-api and tells the assistant their upload id. Records a person's own typed review decisions ("decide <check number> POL-007 3.1 yes ..."). Turns off Open WebUI's own document search for them.
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


def _plain(text, limit: int = 120) -> str:
    """File names come from users and documents: keep them on one line, without markup."""
    return _UNSAFE.sub(" ", " ".join(str(text).split()))[:limit].strip() or "upload"


# A review decision typed by the person: "decide <check number> <POLICY-ID> <section> yes|no ...".
# Only the human's own message is read, here, in trusted code; the model never submits a decision.
# The whole message must start with the word "decide": a quoted ("> decide"), fenced or pasted line never counts.
_DECIDE = re.compile(r"\Adecide\b[ \t:,]*(.*)\Z", re.IGNORECASE | re.DOTALL)
_CHECK_NO = re.compile(r"\s*(?:check\s*)?(?:number\s*|no\.?\s*|#\s*)?([0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?)(?![\w-])", re.IGNORECASE)
_PAIR = re.compile(r"([A-Za-z][A-Za-z0-9]*-\d+)[\s:,]*(?:§|section|clause)?\s*(\d+(?:\.\w+)*)\b[\s:=,.-]*(yes|no|y|n)\b", re.IGNORECASE)
_ALL = re.compile(r"^\W*(?:all\W+(yes|no)|(yes|no)\W+to\W+all|(yes|no)\W+for\W+all)\W*$", re.IGNORECASE)
_LEFTOVER = re.compile(r"\b(?:and|then|also|the|a)\b|[\W_]+", re.IGNORECASE)

HOW_TO_DECIDE = ("Tell the user to type, as a message that starts with the word decide, like: decide {check} POL-007 3.1 <yes|no>, POL-009 2.4 <yes|no> "
                 "(replacing each <yes|no> with their own answer) "
                 "(yes = this section is affected, no = it is not; one POLICY-ID, section number and yes or no for each section on the list). "
                 "Say you cannot decide for them.")


def _text_of(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text")
    return content if isinstance(content, str) else ""


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

    async def _decide(self, c: httpx.AsyncClient, text: str, user: dict) -> str | None:
        """If the person's own message is a decision command, record it and return a note for the assistant."""
        m = _DECIDE.match(text.strip())
        if not m:
            return None
        rest = m.group(1)
        c_no = _CHECK_NO.match(rest)
        if not c_no:
            return ("[The user started a review decision but did not give a valid check number, so nothing was recorded. "
                    + HOW_TO_DECIDE.format(check="<check number>") + "]")
        check, rest = c_no.group(1).lower(), rest[c_no.end():]
        pairs = [(p.group(1).upper(), p.group(2), p.group(3).lower() in ("yes", "y")) for p in _PAIR.finditer(rest)]
        leftover = _LEFTOVER.sub("", _PAIR.sub("", rest))
        who = user.get("name") or user.get("email") or user.get("id")
        if not who:
            return "[The user's review decision was not recorded because Open WebUI did not say who they are. Tell the user to sign in again.]"
        try:
            if not pairs and (a := _ALL.match(rest)):  # "decide <check> all no": every section the check is waiting on
                r = await c.get(f"/runs/{check}")
                if r.status_code == 200 and r.json().get("review"):
                    affected = (a.group(1) or a.group(2) or a.group(3)).lower() == "yes"
                    pairs = [(i["policy_id"], i["clause"], affected) for i in r.json()["review"]]
                    leftover = ""
            if not pairs or leftover:
                return (f"[The user's review decision for check {check} could not be read, so nothing was recorded. "
                        + HOW_TO_DECIDE.format(check=check) + "]")
            r = await c.post(f"/runs/{check}/review", json={
                "decisions": [{"policy_id": p, "clause": cl, "affected": a} for p, cl, a in pairs],
                "reviewed_by": _plain(who, 200)})
        except httpx.HTTPError as e:
            return (f"[The user's review decision for check {check} was NOT recorded: the policy service did not respond "
                    f"({type(e).__name__}). Tell the user to send the same decision again shortly.]")
        if r.status_code == 202:
            done = ", ".join(f"{p} section {cl} {'yes' if a else 'no'}" for p, cl, a in pairs)
            return (f"[The user's review decisions for check {check} were recorded: {_plain(done, 600)}. "
                    "The check is continuing; use get_check_result to follow it.]")
        try:
            detail = _plain(r.json().get("detail", ""), 300)
        except ValueError:
            detail = f"error {r.status_code}"
        if r.status_code == 409:
            return (f"[The user's review decision for check {check} was NOT recorded: {detail} It may already have been decided. "
                    "Tell the user in plain words; use get_check_result to see where it stands.]")
        if r.status_code == 404:
            return (f"[The user's review decision was NOT recorded: there is no check with that number ({check}). "
                    "Ask the user to copy the check number from your earlier message.]")
        if r.status_code == 422:
            return (f"[The user's review decision for check {check} was NOT recorded: {detail} "
                    "Tell the user in plain words, and ask them to type it again using only the sections on the list.]")
        return (f"[The user's review decision for check {check} was NOT recorded (error {r.status_code}). "
                "Tell the user to try again shortly.]")

    async def inlet(self, body: dict, __user__: dict | None = None, __files__: list | None = None) -> dict:
        files = _files(body, __files__)
        user = __user__ or {}
        users = [m for m in body.get("messages") or [] if m.get("role") == "user"]
        # Decisions come only from what the person typed in their latest message, never from the assistant or tools.
        typed = _text_of(users[-1]) if users else ""
        command = bool(_DECIDE.match(typed.strip()))
        if not files and not command:
            return body
        notes = []
        async with self._client() as c:
            for f in files:
                key = (str(user.get("id")), f["id"])
                note = self._done.get(key) or await self._note(c, f["id"], user)
                if ": upload id " in note:  # only successes are remembered; a failure is tried again next time
                    self._done[key] = note
                notes.append(note)
            if command:  # never cached: each message is its own decision
                notes.append(await self._decide(c, typed, user))
        notes = [n for n in notes if n]
        if users and notes:
            _append(users[-1], "\n".join(notes))
        if files:
            body["files"] = []
        return body
