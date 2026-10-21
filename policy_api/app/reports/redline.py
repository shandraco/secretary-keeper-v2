"""Word-level redline between two strings."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

_TOKEN = re.compile(r"\s+|\w+|[^\w\s]", re.UNICODE)


MAX_PRODUCT = 25_000_000


def redline(old: str, new: str) -> list[tuple[str, str]]:
    """Return [(op, text)] with op in equal/delete/insert, joined text reproduces old (equal+delete) and new (equal+insert)."""
    a, b = _TOKEN.findall(old or ""), _TOKEN.findall(new or "")
    out: list[tuple[str, str]] = []
    if len(a) * len(b) > MAX_PRODUCT:  # very long text: show it as one deletion and one insertion rather than stall
        return ([("delete", old)] if old else []) + ([("insert", new)] if new else [])

    def add(op: str, toks: list[str]) -> None:
        text = "".join(toks)
        if not text:
            return
        if out and out[-1][0] == op:
            out[-1] = (op, out[-1][1] + text)
        else:
            out.append((op, text))

    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            add("equal", a[i1:i2])
        else:
            if i2 > i1:
                add("delete", a[i1:i2])
            if j2 > j1:
                add("insert", b[j1:j2])
    return out
