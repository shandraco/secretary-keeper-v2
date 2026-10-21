"""Clients for the locally hosted models: vLLM (OpenAI-compatible) and Text Embeddings Inference."""
from __future__ import annotations

import json
import logging
import re

import httpx
from openai import AsyncOpenAI

from .prompts import (
    EXTRACT_SCHEMA, EXTRACT_SYSTEM, IMPACT_SCHEMA, IMPACT_TYPES, JUDGE_SYSTEM, Pair, render_extract, render_pair,
)

log = logging.getLogger(__name__)
TEI_BATCH = 32  # TEI's default --max-client-batch-size

UNREADABLE = {
    "impacted": "unsure",
    "impact_type": "none",
    "why": "The model's answer could not be read, so a person should check this clause.",
    "evidence_head": "",
    "evidence_candidate": "",
    "suggested_edit": "",
}


def clean_verdict(raw: dict) -> dict:
    """Check a verdict's shape; raise ValueError if it can't be trusted."""
    if raw.get("impacted") not in ("yes", "no", "unsure"):
        raise ValueError(f"bad 'impacted': {raw.get('impacted')!r}")
    impact_type = raw.get("impact_type") if raw.get("impact_type") in IMPACT_TYPES else "none"
    out = {"impacted": raw["impacted"], "impact_type": impact_type}
    for key in ("why", "evidence_head", "evidence_candidate", "suggested_edit"):
        out[key] = str(raw.get(key) or "").strip()
    return out


def _norm(text: str) -> str:
    return " ".join(text.lower().split()).strip(" \"'“”‘’.,;:")


def check_evidence(pair, verdict: dict) -> dict:
    """A "yes" must quote both texts. If either quote isn't really there, the verdict can't be trusted
    (the model may have been steered by text inside a document), so a person checks it instead."""
    if verdict["impacted"] != "yes":
        return verdict
    head = _norm(f"{pair.change.old_text}\n{pair.change.new_text}")
    cand = _norm(pair.clause.text)
    eh, ec = _norm(verdict["evidence_head"]), _norm(verdict["evidence_candidate"])
    if eh and ec and eh in head and ec in cand:
        return verdict
    return {**verdict, "impacted": "unsure",
            "why": (verdict["why"] + " (Downgraded to Maybe: the quoted evidence was not found in the policy text.)").strip()}


class TEIEmbedder:
    def __init__(self, base_url: str) -> None:
        self.name = base_url
        self.http = httpx.AsyncClient(base_url=base_url, timeout=120)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), TEI_BATCH):
            r = await self.http.post("/embed", json={"inputs": texts[i : i + TEI_BATCH], "truncate": True})
            r.raise_for_status()
            out.extend(r.json())
        return out

    async def aclose(self) -> None:
        await self.http.aclose()


class TEIReranker:
    def __init__(self, base_url: str) -> None:
        self.name = base_url
        self.http = httpx.AsyncClient(base_url=base_url, timeout=120)

    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        scores = [0.0] * len(texts)
        for i in range(0, len(texts), TEI_BATCH):
            r = await self.http.post("/rerank", json={"query": query, "texts": texts[i : i + TEI_BATCH], "truncate": True})
            r.raise_for_status()
            for item in r.json():
                scores[i + item["index"]] = float(item["score"])
        return scores

    async def aclose(self) -> None:
        await self.http.aclose()


class LLMJudge:
    def __init__(self, base_url: str, api_key: str, model: str, template_kwargs: dict | None = None) -> None:
        self.name = model
        # Model-specific chat template switches, e.g. {"enable_thinking": False} for Granite 4.2.
        self.extra = {"chat_template_kwargs": template_kwargs} if template_kwargs else None
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key or "unused", timeout=180, max_retries=2)

    async def assess(self, pair: Pair) -> dict:
        messages = [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": render_pair(pair)}]
        for attempt in (1, 2):
            r = await self.client.chat.completions.create(
                model=self.name,
                messages=messages,
                temperature=0,
                response_format={"type": "json_schema", "json_schema": {"name": "impact", "schema": IMPACT_SCHEMA, "strict": True}},
                extra_body=self.extra,
            )
            try:
                return clean_verdict(json.loads(r.choices[0].message.content or ""))
            except (ValueError, TypeError) as e:
                log.warning("unreadable verdict from %s (attempt %d): %s", self.name, attempt, e)
        return dict(UNREADABLE)

    async def aclose(self) -> None:
        await self.client.close()


class LLMExtractor:
    """Finds references that the regex misses ("the lockout policy", "as set out in the EHS manual")."""

    def __init__(self, base_url: str, api_key: str, model: str, id_pattern: str) -> None:
        self.name = model
        self.id_re = re.compile(id_pattern)
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key or "unused", timeout=120, max_retries=2)

    async def refs(self, text: str) -> list[tuple[str, str | None]]:
        r = await self.client.chat.completions.create(
            model=self.name,
            messages=[{"role": "system", "content": EXTRACT_SYSTEM}, {"role": "user", "content": render_extract(text)}],
            temperature=0,
            response_format={"type": "json_schema", "json_schema": {"name": "refs", "schema": EXTRACT_SCHEMA, "strict": True}},
        )
        try:
            items = json.loads(r.choices[0].message.content or "")["references"]
        except (ValueError, KeyError, TypeError):
            return []
        out = []
        for item in items:
            pid, section = str(item.get("policy_id", "")).strip(), str(item.get("section", "")).strip()
            if self.id_re.fullmatch(pid):
                out.append((pid, section if re.fullmatch(r"\d+(?:\.\d+)*", section) else None))
        return out

    async def aclose(self) -> None:
        await self.client.close()
