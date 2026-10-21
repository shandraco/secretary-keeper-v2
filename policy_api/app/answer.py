"""The plain-language chat answer. Built in code so the numbers always match the Excel file."""
from __future__ import annotations

MAX_TABLE_ROWS = 10
MAX_CHANGES = 5


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


_MD_SPECIAL = set("\\`*_[]()<>!|~")  # our text never starts a line with document text, so #, - and + are safe


def _md(text) -> str:
    """Escape Markdown in document or model text, so it shows as written and can't add links,
    images or HTML (an image link could send data out of the browser)."""
    return "".join("\\" + ch if ch in _MD_SPECIAL else ch for ch in " ".join(str(text).split()))


def _short(text: str, limit: int = 140) -> str:
    text = " ".join(str(text).split())
    return _md(text if len(text) <= limit else text[: limit - 1].rstrip() + "…")


def _cell(text: str) -> str:
    return _short(text, 160)


def render_answer(result: dict, excel_url: str) -> str:
    c, head, subset = result["counts"], result["head"], _md(result["subset"])
    name = f"{_md(head['id'])} ({_md(head['title'])})"
    if not result["changes"]:
        return (f"**No changes found.** The two versions of {name} have the same wording, "
                f"so nothing in {subset} is affected.")

    n = c["subset_policies"]
    group = f"{n} {subset} {_plural(n, 'policy', 'policies')}"
    if c["impacted"] == 0 and c["maybe"] == 0:
        lines = [f"**None of the {group} are affected by the change to {name}.**"]
    else:
        verb = _plural(c["impacted"], "needs", "need")
        lines = [f"**{c['impacted']} of the {group} {verb} updating because of the change to {name}.**"]

    lines += ["", f"What changed ({_md(head['old_label'])} → {_md(head['new_label'])}):"]
    for ch in result["changes"][:MAX_CHANGES]:
        lines.append(f"- {_md(ch['label'])}: {_short(ch['new_text'] or ch['old_text'])}")
    if len(result["changes"]) > MAX_CHANGES:
        lines.append(f"- …and {len(result['changes']) - MAX_CHANGES} more (see the Excel file)")

    yes_rows = [r for r in result["rows"] if r["status"] == "Yes"]
    if yes_rows:
        lines += ["", "| Policy | Section | What's wrong |", "|---|---|---|"]
        for r in yes_rows[:MAX_TABLE_ROWS]:
            lines.append(f"| {_md(r['policy_id'])} {_cell(r['policy_title'])} | §{_md(r['clause'])} | {_cell(r['why'])} |")
        if len(yes_rows) > MAX_TABLE_ROWS:
            lines.append(f"\n…and {len(yes_rows) - MAX_TABLE_ROWS} more sections in the Excel file.")

    maybe_ids = sorted({r["policy_id"] for r in result["rows"]} - {r["policy_id"] for r in yes_rows})
    if maybe_ids:
        titles = {r["policy_id"]: r["policy_title"] for r in result["rows"]}
        listed = ", ".join(f"{_md(pid)} {_short(titles[pid])}" for pid in maybe_ids)
        lines += ["", f"**{len(maybe_ids)} might be affected**, and a person should check {_plural(len(maybe_ids), 'it', 'them')}: {listed}."]

    lines += ["", f"{c['not_impacted']} {_plural(c['not_impacted'], 'was', 'were')} checked and {_plural(c['not_impacted'], 'is', 'are')} not affected."]
    if c["errors"]:
        lines.append(f"⚠️ {c['errors']} section pairs could not be checked automatically and are marked “Maybe”.")
    lines += ["", f"📎 **[Download the full comparison (Excel)]({excel_url})**: old and new wording, and the evidence for every policy."]
    return "\n".join(lines)
