#!/usr/bin/env python3
"""Build docs/pdf/handbook.pdf and one PDF per guide from docs/*.md.

Needs: Python with `markdown` (pip install markdown), Google Chrome or Chromium, and poppler's
`pdftotext` (for the page numbers in the contents). The first run downloads the fonts from Google
Fonts into source/.fonts; later runs work offline.

    python3 docs/pdf/source/build.py
"""
from __future__ import annotations

import html
import os
import posixpath
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

import markdown

from diagrams import figure

SRC = Path(__file__).resolve().parent
OUT = SRC.parent
DOCS = OUT.parent
FONTS = SRC / ".fonts"
BUILD = SRC / ".build"
REPO_URL = "https://github.com/shandraco/secretary-keeper-v2/blob/main/"
TITLE = "Policy Impact Assistant"
VERSION = "secretary-keeper version 0.1, October 2026. MIT License."

# Handbook order: what it is, using it, installing and running it, then assurance.
CHAPTERS = [
    ("architecture", "How it works and why it's built this way", "Everyone"),
    ("how-to", "Ask questions and read the results; add policies, groups and models", "Staff and administrators"),
    ("setup", "Install it on the GPU server", "Engineers"),
    ("operations", "Run it day to day, upgrade it, fix problems", "Administrators and engineers"),
    ("security", "Keep policy text contained and the system safe", "Everyone"),
    ("review-and-training", "Let people check answers, then teach the judge from them", "Administrators, reviewers and engineers"),
    ("evaluation", "Measure whether it finds the right policies", "Policy owners and engineers"),
    ("testing", "Run and understand the tests", "Engineers"),
]
SLUGS = {slug for slug, _, _ in CHAPTERS}

# Old-browser User-Agent: Google Fonts then serves static per-weight files, which Chrome embeds as
# TrueType. Variable fonts come out of Chrome's PDF printer as Type 3.
FONT_CSS = ("https://fonts.googleapis.com/css2?family=Barlow+Semi+Condensed:wght@500;600;700"
            "&family=Public+Sans:ital,wght@0,400;0,600;0,700;1,400&family=IBM+Plex+Mono:wght@400;500&display=swap")
# Public Sans has no arrows. IBM Plex Sans, cut down to just → and ←, fills them in as a fallback family.
ARROWS_CSS = "https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;600&text=%E2%86%92%E2%86%90"
OLD_UA = "Mozilla/5.0 (Windows NT 6.1) AppleWebKit/535.1 (KHTML, like Gecko) Chrome/12.0.742.112 Safari/535.1"
CHROME_PATHS = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium"]


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": OLD_UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def font_css() -> str:
    css_file = FONTS / "fonts.css"
    if not css_file.exists():
        FONTS.mkdir(exist_ok=True)
        css = fetch(FONT_CSS).decode()
        css += fetch(ARROWS_CSS).decode().replace("font-family: 'IBM Plex Sans'", "font-family: 'Arrows'")
        for i, url in enumerate(re.findall(r"url\((https://[^)]+)\)", css)):
            name = f"f{i}.woff"  # the old User-Agent always gets woff; text= URLs have no suffix
            (FONTS / name).write_bytes(fetch(url))
            css = css.replace(url, name)
        css_file.write_text(css)
    css = css_file.read_text()
    return re.sub(r"url\((f\d+\.\w+)\)", lambda m: f"url({(FONTS / m.group(1)).as_uri()})", css)


def chrome() -> str:
    for p in [os.environ.get("CHROME", "")] + CHROME_PATHS:
        if p and Path(p).exists():
            return p
    for name in ("google-chrome", "chromium", "chromium-browser"):
        if shutil.which(name):
            return shutil.which(name)
    sys.exit("Chrome or Chromium not found. Set CHROME=/path/to/chrome.")


def gfm_lists(text: str) -> str:
    """GitHub starts a list right after a line of text; python-markdown needs a blank line first."""
    out, fenced, prev = [], False, ""
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        is_item = re.match(r"\s*([-*]|\d+\.) ", line)
        prev_item = re.match(r"\s*([-*]|\d+\.) ", prev) or prev.startswith(("  ", "|", "#", ">"))
        if not fenced and is_item and prev.strip() and not prev_item:
            out.append("")
        out.append(line)
        prev = "" if line.startswith("```") else line
    return "\n".join(out) + "\n"


def place_figures(slug: str, body: str) -> str:
    def swap_pre(marker: str, key: str, text: str) -> str:
        for m in re.finditer(r"<pre>.*?</pre>", text, flags=re.S):
            if marker in html.unescape(m.group(0)):
                return text[:m.start()] + figure(key) + text[m.end():]
        raise SystemExit(f"{slug}: diagram marker {marker!r} not found")

    def before_h2(heading: str, key: str, text: str, after: bool = False) -> str:
        m = re.search(rf"<h2[^>]*>{re.escape(heading)}</h2>", text)
        if not m:
            raise SystemExit(f"{slug}: heading {heading!r} not found")
        at = m.end() if after else m.start()
        return text[:at] + figure(key) + text[at:]

    if slug == "architecture":
        body = swap_pre("1. Resolve", "pipeline", body)
        body = before_h2("Components", "system", body, after=True)
    elif slug == "security":
        body = swap_pre("frontend network", "containment", body)
    elif slug == "review-and-training":
        body = before_h2("For the administrator", "loop", body)
    return body


def fix_links(slug: str, body: str, combined: bool) -> str:
    def href(m: re.Match) -> str:
        h = html.unescape(m.group(1))
        if re.match(r"[a-z]+:", h):
            new = h
        elif h.startswith("#"):
            new = f"#{slug}--{h[1:]}"
        elif (md := re.fullmatch(r"([\w-]+)\.md(?:#(.*))?", h)) and md.group(1) in SLUGS:
            other, frag = md.groups()
            if combined or (other == slug and frag):
                new = f"#{other}" + (f"--{frag}" if frag else "")
            else:
                new = REPO_URL + "docs/" + h
        else:
            new = REPO_URL + posixpath.normpath("docs/" + h)
        return f'href="{html.escape(new)}"'

    body = re.sub(r'\bid="([^"]+)"', lambda m: f'id="{slug}--{m.group(1)}"', body)
    return re.sub(r'href="([^"]+)"', href, body)


def chapter_html(index: int, slug: str, what: str, who: str, combined: bool) -> tuple[str, str]:
    text = (DOCS / f"{slug}.md").read_text()
    title = text.splitlines()[0].lstrip("# ").strip()
    text = gfm_lists(text.split("\n", 1)[1])
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "toc", "sane_lists"],
                             extension_configs={"toc": {"permalink": False}})
    body = fix_links(slug, body, combined)
    body = place_figures(slug, body)  # after fix_links, which would rename the diagrams' marker ids
    # keep short code blocks on one page; let long ones break
    body = re.sub(r"<pre>(.*?)</pre>", lambda m: f'<pre class="short">{m.group(1)}</pre>' if m.group(1).count("\n") <= 12 else m.group(0),
                  body, flags=re.S)
    num = f'<p class="num">Chapter {index}</p>' if combined else f'<p class="num">{TITLE} handbook</p>'
    note = "" if combined else (f'<p class="standalone-note">One chapter of the handbook. The full handbook, with every chapter, '
                                f'is handbook.pdf in the same folder.</p>')
    opener = (f'<header class="opener">{num}<h1>{html.escape(title)}</h1><div class="bar"></div>'
              f'<p class="what">{html.escape(what)}.</p><p class="who">For: {html.escape(who)}</p></header>')
    return title, f'<section class="chapter" id="{slug}">{opener}{note}{body}</section>'


COVER = f"""<section class="cover">
<div class="tag"><h1>{TITLE}</h1><p class="for">Handbook for staff, administrators and engineers</p></div>
<div class="hole"></div>
<svg class="string" viewBox="0 0 816 1056" aria-hidden="true"><path d="M560,322 C650,322 690,430 752,418 S812,330 840,300"
 fill="none" stroke="#1E2B36" stroke-width="1.6"/></svg>
<p class="intro">Find every policy affected by a change to another, with the evidence for each, on your own
hardware. This handbook covers how it works, daily use, installation, operations, security, review and evaluation.</p>
<p class="meta">{VERSION}</p>
</section>"""


def document(parts: list[str], footer: str) -> str:
    page_css = f'@page {{ @bottom-left {{ content: "{footer}"; }} }} @page cover {{ @bottom-left {{ content: none; }} }}'
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(footer)}</title>'
            f'<style>{font_css()}</style><link rel="stylesheet" href="{(SRC / "style.css").as_uri()}">'
            f'<style>{page_css}</style></head><body>{"".join(parts)}</body></html>')


def print_pdf(doc: str, out: Path) -> None:
    BUILD.mkdir(exist_ok=True)
    page = BUILD / (out.stem + ".html")
    page.write_text(doc)
    subprocess.run([chrome(), "--headless", "--disable-gpu", "--no-pdf-header-footer", "--virtual-time-budget=10000",
                    f"--print-to-pdf={out}", page.as_uri()], check=True, capture_output=True)


def first_pages(pdf: Path, count: int) -> list[int]:
    """Page on which each chapter starts: the first page whose opening lines say "Chapter N"."""
    pages = int(re.search(r"Pages:\s+(\d+)", subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True).stdout).group(1))
    found, start = [], 1
    for t in (f"Chapter {n}" for n in range(1, count + 1)):
        for p in range(start, pages + 1):
            txt = subprocess.run(["pdftotext", "-f", str(p), "-l", str(p), str(pdf), "-"], capture_output=True, text=True).stdout
            head = [line.strip() for line in txt.splitlines() if line.strip()][:3]
            if t in head:
                found.append(p)
                start = p + 1
                break
        else:
            raise SystemExit(f"Could not find the page for chapter {t!r}")
    return found


def contents(chapters: list[tuple[str, str, str, str]], pages: list[int] | None) -> str:
    rows = []
    for i, (slug, title, what, who) in enumerate(chapters):
        p = str(pages[i]) if pages else "00"
        rows.append(f'<li><span class="n">{i + 1}</span><span><a href="#{slug}">{html.escape(title)}</a>'
                    f'<span class="what">{html.escape(what)}</span><span class="who">For: {html.escape(who)}</span></span>'
                    f'<span class="p">{p}</span></li>')
    return f'<section class="contents"><h1>Contents</h1><ol class="toc">{"".join(rows)}</ol></section>'


def main() -> None:
    built = [(slug, *chapter_html(i + 1, slug, what, who, True), what, who) for i, (slug, what, who) in enumerate(CHAPTERS)]
    toc_rows = [(slug, title, what, who) for slug, title, _, what, who in built]
    bodies = [b for _, _, b, _, _ in built]
    handbook = OUT / "handbook.pdf"
    footer = f"{TITLE} handbook"
    print_pdf(document([COVER, contents(toc_rows, None)] + bodies, footer), handbook)
    pages = first_pages(handbook, len(toc_rows))
    print_pdf(document([COVER, contents(toc_rows, pages)] + bodies, footer), handbook)
    if first_pages(handbook, len(toc_rows)) != pages:
        raise SystemExit("Page numbers moved between passes; run again.")
    print(f"{handbook.relative_to(DOCS.parent)}: chapters start on pages {pages}")

    for slug, what, who in CHAPTERS:
        title, body = chapter_html(0, slug, what, who, False)
        out = OUT / f"{slug}.pdf"
        print_pdf(document([body], f"{TITLE}: {title}"), out)
        print(out.relative_to(DOCS.parent))


if __name__ == "__main__":
    main()
