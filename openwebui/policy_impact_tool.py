"""
title: Policy Impact Checker
author: secretary-keeper
version: 0.3.0
description: Finds which policies in a group are affected when a head policy changes; builds PDF, Word and Excel reports, comparisons and charts.
requirements: httpx
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

_MD = set("\\`*_[]()<>!|~")


def _md(text) -> str:
    """Show file names and titles literally: they can come from documents."""
    return "".join("\\" + ch if ch in _MD else ch for ch in " ".join(str(text).split()))


SHOW_FILES = ("Show the user these files exactly as written below: keep every link unchanged and "
              "show the chart images. Do not describe contents you have not been told.\n\n")

SURE = 0.85   # name-match score treated as certain
UPLOAD_MATCH = 0.6  # similarity needed to assume which policy an upload is a new version of


class Tools:
    class Valves(BaseModel):
        API_URL: str = Field(default="http://policy-api:8000", description="policy-api address inside the Docker network.")
        API_KEY: str = Field(default="", description="The POLICY_API_KEY value from .env.")
        POLL_SECONDS: float = Field(default=3.0, description="How often to check on a running analysis.")
        MAX_WAIT_MINUTES: int = Field(default=30, description="Give up waiting after this long.")
        UPLOAD_DIR: str = Field(default="/app/backend/data/uploads", description="Open WebUI's upload folder. Files outside it are refused.")

    def __init__(self):
        self.valves = self.Valves()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.valves.API_URL,
            headers={"Authorization": f"Bearer {self.valves.API_KEY}"},
            timeout=60,
        )

    @staticmethod
    async def _status(emit, text: str, done: bool = False) -> None:
        if emit:
            await emit({"type": "status", "data": {"description": text, "done": done}})

    def _attached_file(self, files: list | None) -> tuple[str, bytes] | None:
        # Open WebUI's __files__ shape has changed between releases; accept the known layouts. Only files
        # inside Open WebUI's upload folder are read, whatever path the request names.
        folder = Path(self.valves.UPLOAD_DIR).resolve()
        for f in reversed(files or []):
            info = f.get("file") or {}
            path = info.get("path") or f.get("path")
            name = f.get("name") or info.get("filename") or (info.get("meta") or {}).get("name") or "upload"
            if not path:
                continue
            real = Path(path).resolve()
            if real.is_relative_to(folder) and real.is_file():
                return Path(name).name, real.read_bytes()
        return None

    async def _wait_for_run(self, c: httpx.AsyncClient, run_id: str, emit) -> str:
        deadline = time.monotonic() + self.valves.MAX_WAIT_MINUTES * 60
        last = ""
        while time.monotonic() < deadline:
            r = await c.get(f"/runs/{run_id}")
            if r.status_code == 404:
                return "There is no check with that number. Tell the user, and offer to run the check again."
            r.raise_for_status()
            run = r.json()
            if run["status"] == "done":
                await self._status(emit, "Done", done=True)
                return ("Show the user this answer exactly as written. Do not change any numbers, "
                        "policy IDs or the link, and do not add policies that are not listed.\n\n"
                        + run["answer_markdown"]
                        + f"\n\n(For you only, do not show: check number {run_id}. If the user wants a PDF or Word "
                        "report or charts of this result, call make_check_files with this check number.)")
            if run["status"] == "failed":
                await self._status(emit, "Could not finish", done=True)
                return f"The check could not finish: {run['error']} Tell the user this in plain words."
            if run["progress"] and run["progress"] != last:
                last = run["progress"]
                await self._status(emit, last)
            await asyncio.sleep(self.valves.POLL_SECONDS)
        await self._status(emit, "Still running", done=True)
        return (f"The check is taking longer than expected and is still running (check number {run_id}). "
                "Tell the user it is still running and that they can ask you for the result in a few minutes; "
                "then call get_check_result with this number. Do not start a new check.")

    @staticmethod
    def _show_files(files: list[dict]) -> str:
        lines = []
        for f in files:
            if f["file"].endswith(".png"):
                lines.append(f"**{_md(f['label'])}**\n\n![{_md(f['label'])}]({f['url']})")
            else:
                lines.append(f"📎 [{_md(f['label'])}]({f['url']})")
        return SHOW_FILES + "\n\n".join(lines) + "\n\nLinks stop working after a few days; ask again for fresh ones."

    async def list_policy_groups(self, search: str = "") -> str:
        """
        List the groups of policies that can be checked, such as Safety or HR.
        :param search: Optional words to look for in group names. Leave empty to list every group.
        """
        try:
            async with self._client() as c:
                r = await c.get("/subsets", params={"q": search} if search else None)
                r.raise_for_status()
        except httpx.HTTPError as e:
            return f"The policy service could not be reached ({type(e).__name__}). Tell the user to try again shortly."
        groups = r.json()["subsets"]
        if not groups:
            return "No matching policy groups were found. Ask the user which group they mean."
        return "Policy groups:\n" + "\n".join(
            f"- {g['name']} ({g['count']} {'policy' if g['count'] == 1 else 'policies'})" for g in groups
        )

    async def find_policy(self, name_or_id: str) -> str:
        """
        Look up a policy by its ID (like POL-007) or by words from its title.
        :param name_or_id: The policy ID or title the user mentioned.
        """
        try:
            async with self._client() as c:
                r = await c.get("/policies", params={"q": name_or_id})
                r.raise_for_status()
        except httpx.HTTPError as e:
            return f"The policy service could not be reached ({type(e).__name__}). Tell the user to try again shortly."
        found = r.json()["policies"]
        if not found:
            return f"No policy matches “{name_or_id}”. Ask the user for the policy ID."
        return "Matching policies:\n" + "\n".join(f"- {p['id']}: {p['title']}" for p in found)

    async def check_policy_change(
        self,
        policy_group: str,
        head_policy: str = "",
        upload_id: str = "",
        __files__: list | None = None,
        __user__: dict | None = None,
        __event_emitter__=None,
    ) -> str:
        """
        Find which policies in a group are affected by a change to a head policy, and make an Excel comparison.
        If the user attached a document, it is used as the new version of the head policy.
        Without an attachment, the two most recent stored versions of the head policy are compared.
        :param policy_group: The group of policies to check, for example "Safety".
        :param head_policy: The ID or title of the policy that changed. Leave empty if the user attached the new version and did not name it.
        :param upload_id: The upload id from an "[Attached file ...]" note, when the user attached the new version. Leave empty otherwise.
        """
        emit = __event_emitter__
        try:
            async with self._client() as c:
                # 1. Which group?
                r = await c.get("/subsets", params={"q": policy_group})
                r.raise_for_status()
                groups = r.json()["subsets"]
                if not groups:
                    r = await c.get("/subsets")
                    r.raise_for_status()
                    options = ", ".join(g["name"] for g in r.json()["subsets"]) or "none set up yet"
                    return f"There is no policy group called “{policy_group}”. Ask the user which group they mean. Groups that exist: {options}."
                if groups[0]["score"] < SURE and len(groups) > 1:
                    options = ", ".join(g["name"] for g in groups[:5])
                    return f"Ask the user which policy group they mean. Closest groups: {options}."
                group = groups[0]["name"]

                # 2. Uploaded new version?
                upload_id, matches = upload_id.strip() or None, []
                attached = None if upload_id else self._attached_file(__files__)
                if attached:
                    await self._status(emit, f"Reading {attached[0]}…")
                    r = await c.post("/uploads", files={"file": attached})
                    if r.status_code == 400:
                        return f"The attached file could not be used: {r.json()['detail']} Tell the user this in plain words."
                    r.raise_for_status()
                    upload_id, matches = r.json()["upload_id"], r.json()["matches"]

                # 3. Which head policy?
                head = None
                if head_policy:
                    r = await c.get("/policies", params={"q": head_policy})
                    r.raise_for_status()
                    found = r.json()["policies"]
                    clear = found and found[0]["score"] >= SURE and (len(found) == 1 or found[1]["score"] < found[0]["score"] - 0.05)
                    if not clear:
                        options = ", ".join(f"{p['id']} {p['title']}" for p in found[:5]) or "none found"
                        return f"Ask the user which policy they mean. Closest matches: {options}."
                    head = found[0]["id"]
                elif matches and matches[0]["score"] >= UPLOAD_MATCH:
                    head = matches[0]["id"]
                else:
                    options = ", ".join(f"{m['id']} {m['title']}" for m in matches[:3]) or "none found"
                    return ("Ask the user which policy this document is a new version of "
                            f"(or which policy changed, if nothing is attached). Closest matches: {options}.")

                # 4. Queue the check and wait for it.
                await self._status(emit, f"Checking {group} policies against the change to {head}…")
                who = (__user__ or {}).get("email") or (__user__ or {}).get("name")
                r = await c.post("/runs", json={"head_policy": head, "subset": group, "upload_id": upload_id, "requested_by": who})
                if r.status_code == 404:
                    return f"{r.json()['detail']} Tell the user this in plain words."
                r.raise_for_status()
                return await self._wait_for_run(c, r.json()["run_id"], emit)
        except httpx.HTTPError as e:
            await self._status(emit, "Policy service unavailable", done=True)
            return (f"The policy service did not respond ({type(e).__name__}). Tell the user to try again in a few "
                    "minutes, and to contact the system owner if it keeps happening.")


    async def get_check_result(self, check_number: str, __event_emitter__=None) -> str:
        """
        Get the result of a policy check that was still running earlier. Use the check number you were given.
        :param check_number: The check number from the earlier message.
        """
        try:
            async with self._client() as c:
                return await self._wait_for_run(c, check_number.strip(), __event_emitter__)
        except httpx.HTTPError as e:
            return f"The policy service did not respond ({type(e).__name__}). Tell the user to try again shortly."

    async def make_check_files(self, check_number: str, formats: str = "pdf,docx,xlsx,charts", __event_emitter__=None) -> str:
        """
        Make files from a finished policy check: a PDF report, a Word report, the Excel workbook, and charts.
        :param check_number: The check number from the policy check.
        :param formats: Comma-separated, any of: pdf, docx, xlsx, charts. Use only what the user asked for; all four if they asked for "a report" without saying which.
        """
        wanted = [f.strip().lower() for f in formats.split(",") if f.strip()]
        wanted = [{"word": "docx", "excel": "xlsx", "graphs": "charts", "chart": "charts"}.get(f, f) for f in wanted]
        bad = [f for f in wanted if f not in ("pdf", "docx", "xlsx", "charts")]
        if bad or not wanted:
            return "Formats must be from: pdf, docx, xlsx, charts. Call again with those."
        await self._status(__event_emitter__, "Making the files…")
        try:
            async with self._client() as c:
                r = await c.post(f"/runs/{check_number.strip()}/files", json={"formats": wanted})
                if r.status_code in (404, 409):
                    return f"{r.json()['detail']} Tell the user this in plain words."
                r.raise_for_status()
        except httpx.HTTPError as e:
            return f"The policy service did not respond ({type(e).__name__}). Tell the user to try again shortly."
        await self._status(__event_emitter__, "Files ready", done=True)
        return self._show_files(r.json()["files"])

    async def compare_policies(
        self,
        first_policy: str,
        second_policy: str = "",
        first_version: int = 0,
        second_version: int = 0,
        file_format: str = "docx",
        upload_id: str = "",
        __files__: list | None = None,
        __event_emitter__=None,
    ) -> str:
        """
        Make a side-by-side, section-by-section comparison of two policies, two versions of one policy, or a
        policy and the attached document. Changes are marked: deleted words struck through, new words underlined.
        :param first_policy: ID or title of the first policy.
        :param second_policy: ID or title of the second policy. Leave empty to compare with the attached document, or to compare two versions of the first policy.
        :param first_version: Version number of the first policy. 0 means the latest.
        :param second_version: Version number of the second policy (or of the first policy, when comparing its versions). 0 means the latest.
        :param file_format: docx (Word, best for reading), pdf, or xlsx (one row per section).
        :param upload_id: The upload id from an "[Attached file ...]" note, to compare with that document. Leave empty otherwise.
        """
        fmt = {"word": "docx", "excel": "xlsx"}.get(file_format.strip().lower(), file_format.strip().lower())
        if fmt not in ("docx", "pdf", "xlsx"):
            return "file_format must be docx, pdf or xlsx."
        emit = __event_emitter__
        try:
            async with self._client() as c:
                async def resolve(name: str) -> str | None:
                    r = await c.get("/policies", params={"q": name})
                    r.raise_for_status()
                    found = r.json()["policies"]
                    if found and found[0]["score"] >= SURE and (len(found) == 1 or found[1]["score"] < found[0]["score"] - 0.05):
                        return found[0]["id"]
                    return None

                left_id = await resolve(first_policy)
                if not left_id:
                    return f"Ask the user which policy they mean by “{first_policy}”, or call find_policy to list matches."
                left = {"policy_id": left_id, "version": first_version or None}
                attached = self._attached_file(__files__)
                if second_policy:
                    right_id = await resolve(second_policy)
                    if not right_id:
                        return f"Ask the user which policy they mean by “{second_policy}”, or call find_policy to list matches."
                    right = {"policy_id": right_id, "version": second_version or None}
                elif upload_id.strip():
                    right = {"upload_id": upload_id.strip()}
                elif attached:
                    await self._status(emit, f"Reading {attached[0]}…")
                    r = await c.post("/uploads", files={"file": attached})
                    if r.status_code == 400:
                        return f"The attached file could not be used: {r.json()['detail']} Tell the user this in plain words."
                    r.raise_for_status()
                    right = {"upload_id": r.json()["upload_id"]}
                elif first_version or second_version:
                    right = {"policy_id": left_id, "version": second_version or None}
                else:
                    return ("Ask the user what to compare it with: another policy, an attached document, "
                            "or which two version numbers of this policy.")
                await self._status(emit, "Comparing section by section…")
                r = await c.post("/compare", json={"left": left, "right": right, "format": fmt})
                if r.status_code in (404, 422):
                    return f"{r.json()['detail']} Tell the user this in plain words."
                r.raise_for_status()
        except httpx.HTTPError as e:
            return f"The policy service did not respond ({type(e).__name__}). Tell the user to try again shortly."
        await self._status(emit, "Comparison ready", done=True)
        return self._show_files(r.json()["files"])

    async def make_document(self, content_json: str, file_format: str = "docx", __event_emitter__=None) -> str:
        """
        Make a Word, PDF or Excel file, or a chart image, from content YOU write when the user asks for a document,
        summary, table, chart or graph that is not a policy check or comparison. Only use facts from this conversation
        or from the other tools; never invent policy text. The file is marked as prepared by the assistant.
        :param content_json: JSON: {"title": str, "subtitle": str (optional), "sections": [{"heading": str, "paragraphs": [str], "bullets": [str], "table": {"columns": [str], "rows": [[str or number]]}, "chart": {"type": "bar"|"stacked_bar"|"line"|"pie", "title": str, "labels": [str], "series": [{"name": str, "values": [number]}], "x_label": str, "y_label": str}}]}. Every section key is optional.
        :param file_format: docx (Word), pdf, xlsx (Excel), or png (the first chart, as an image).
        """
        fmt = {"word": "docx", "excel": "xlsx", "image": "png", "chart": "png"}.get(file_format.strip().lower(), file_format.strip().lower())
        if fmt not in ("docx", "pdf", "xlsx", "png"):
            return "file_format must be docx, pdf, xlsx or png."
        try:
            spec = json.loads(content_json)
        except ValueError as e:
            return f"content_json is not valid JSON ({e}). Fix it and call again."
        if not isinstance(spec, dict):
            return "content_json must be a JSON object with a title and sections. Fix it and call again."
        await self._status(__event_emitter__, "Making the file…")
        try:
            async with self._client() as c:
                r = await c.post("/documents", json={"spec": spec, "format": fmt})
                if r.status_code == 422:
                    detail = r.json()["detail"]
                    return f"The content could not be used: {detail}. Fix the JSON and call again."
                r.raise_for_status()
        except httpx.HTTPError as e:
            return f"The policy service did not respond ({type(e).__name__}). Tell the user to try again shortly."
        await self._status(__event_emitter__, "File ready", done=True)
        return self._show_files(r.json()["files"])
