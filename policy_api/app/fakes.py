"""Stand-in models for the demo and the tests. They need no GPU and are deterministic.

They are deliberately simple and are NOT a substitute for the real models: never use them for real decisions.
"""
from __future__ import annotations

import math
import re
import zlib

from .prompts import Pair

_WORD = re.compile(r"[a-z0-9][a-z0-9-]*")
_STOP = {
    "the", "and", "for", "are", "an", "of", "to", "in", "on", "or", "by", "is", "be", "this", "that", "with",
    "from", "at", "as", "it", "its", "all", "any", "each", "every", "must", "before", "after", "under", "per",
    "a", "if", "only", "their", "them", "using", "used",
}


def words(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if len(w) >= 2 and w not in _STOP]


class FakeEmbedder:
    name = "fake-embedder"
    dim = 256

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            v = [0.0] * self.dim
            for w in words(text):
                v[zlib.crc32(w.encode()) % self.dim] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out


class FakeReranker:
    name = "fake-reranker"

    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        q = set(words(query))
        return [len(q & set(words(t))) / (len(q | set(words(t))) or 1) for t in texts]


class FakeJudge:
    """'yes' when a cited or term-linked clause still contains wording the head policy removed."""

    name = "fake-judge"

    async def assess(self, pair: Pair) -> dict:
        ch, text = pair.change, pair.clause.text
        removed = sorted(set(words(ch.old_text)) - set(words(ch.new_text)))
        linked = bool(pair.found_by & {"cites", "term"})
        hits = [w for w in removed if re.search(rf"\b{re.escape(w)}\b", text, re.I)]
        if linked and hits:
            sentence = next((s.strip() for s in re.split(r"(?<=[.;])\s+", text) if hits[0] in s.lower()), text)
            return {
                "impacted": "yes",
                "impact_type": "conflict" if "cites" in pair.found_by else "needs_update",
                "why": f"This section still says “{hits[0]}”, which the head policy no longer says.",
                "evidence_head": hits[0],
                "evidence_candidate": sentence,
                "suggested_edit": "",
            }
        if linked:
            return {
                "impacted": "unsure",
                "impact_type": "none",
                "why": "This section depends on the changed part of the head policy; a person should confirm it still fits.",
                "evidence_head": "",
                "evidence_candidate": "",
                "suggested_edit": "",
            }
        return {"impacted": "no", "impact_type": "none", "why": "", "evidence_head": "",
                "evidence_candidate": "", "suggested_edit": ""}
