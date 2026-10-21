"""Documents and charts built in code from a finished run. Models never write document content directly."""
from __future__ import annotations

import re

ACCENT = "1F3A5F"
STATUS_FILL = {"Yes": "F8D7DA", "Maybe": "FFF3CD"}        # same fills as the Excel file
STATUS_STRONG = {"Yes": "B42318", "Maybe": "B7791F", "No": "8A94A0"}  # bars and text, readable on white
STATUS_WORD = {"Yes": "Needs updating", "Maybe": "Might be affected", "No": "Not affected"}
DELETE_RGB = "B42318"
INSERT_RGB = "1F3A5F"
CHANGE_WORD = {"modified": "Changed", "added": "Added", "removed": "Removed", "renumbered": "Renumbered",
               "same": "Same", "changed": "Changed"}
IMPACT_WORD = {
    "conflict": "Conflicts with the new wording",
    "outdated_reference": "Points to a moved or removed section",
    "changed_term": "Uses a term whose meaning changed",
    "needs_update": "Repeats old wording",
    "redundant": "Now repeats the head policy",
}
DISCLAIMER = "Prepared by the assistant at your request. Check it against the source policies before relying on it."

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean(value, limit: int | None = None) -> str:
    """Plain text safe for any file format: no control characters, optional length cap."""
    text = _CONTROL.sub("", "" if value is None else str(value))
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def headline(result: dict) -> str:
    c, head = result["counts"], result["head"]
    name = f"{head['id']} ({head['title']})"
    if not result["changes"]:
        return f"No changes found. The two versions of {name} have the same wording, so nothing in {result['subset']} is affected."
    n = c["subset_policies"]
    group = f"{n} {result['subset']} {plural(n, 'policy', 'policies')}"
    if c["impacted"] == 0 and c["maybe"] == 0:
        return f"None of the {group} are affected by the change to {name}."
    verb = plural(c["impacted"], "needs", "need")
    text = f"{c['impacted']} of the {group} {verb} updating because of the change to {name}."
    if c["maybe"]:
        text += f" {c['maybe']} more might be affected and need a person to check."
    return text


def impacted_policy_summary(result: dict) -> dict[str, dict]:
    """policy_id -> {title, yes: sections, maybe: sections, status}"""
    out: dict[str, dict] = {}
    for r in result["rows"]:
        p = out.setdefault(r["policy_id"], {"title": r["policy_title"], "Yes": 0, "Maybe": 0})
        if r["status"] in ("Yes", "Maybe"):
            p[r["status"]] += 1
    for p in out.values():
        p["status"] = "Yes" if p["Yes"] else "Maybe"
    return out
