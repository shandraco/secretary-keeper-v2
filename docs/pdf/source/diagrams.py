"""The four handbook diagrams, drawn as inline SVG. They use the page's fonts and the classes in style.css."""
from __future__ import annotations

from html import escape

INK, STEEL, TAG, RED = "#1E2B36", "#5D6B78", "#F2B705", "#B42318"


def _arrowhead(mid: str, color: str = INK) -> str:
    return (f'<marker id="{mid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{color}"/></marker>')


def _svg(name: str, w: int, h: int, body: list[str], label: str) -> str:
    return (f'<svg class="diagram" viewBox="0 0 {w} {h}" role="img" aria-label="{escape(label)}" '
            f'xmlns="http://www.w3.org/2000/svg"><defs>{_arrowhead(name + "-ah")}</defs>{"".join(body)}</svg>')


def _box(x, y, w, h, title, sub="", sub2="", cls="box"):
    out = [f'<rect class="{cls}" x="{x}" y="{y}" width="{w}" height="{h}" rx="3"/>']
    lines = [s for s in (sub, sub2) if s]
    ty = y + h / 2 + 4 - 7 * len(lines)
    out.append(f'<text class="d-title" x="{x + w / 2}" y="{ty}" text-anchor="middle">{escape(title)}</text>')
    for i, s in enumerate(lines):
        out.append(f'<text class="d-sub" x="{x + w / 2}" y="{ty + 15 + 12 * i}" text-anchor="middle">{escape(s)}</text>')
    return "".join(out)


def _line(d, mid, cls="d-line", arrow=True):
    end = f' marker-end="url(#{mid}-ah)"' if arrow else ""
    return f'<path class="{cls}" d="{d}"{end}/>'


def _text(x, y, s, cls="d-note", anchor="start"):
    return f'<text class="{cls}" x="{x}" y="{y}" text-anchor="{anchor}">{escape(s)}</text>'


def system() -> str:
    n = "sys"
    b = [
        '<rect class="band-back" x="8" y="108" width="664" height="284" rx="4"/>',
        _text(22, 128, "backend network: sealed, no route out", "d-band"),
        _box(20, 24, 130, 48, "Staff", "in a web browser"),
        _box(220, 24, 130, 48, "proxy", "nginx, the only way in", cls="box door"),
        _line("M150,48 H218", n),
        _box(220, 150, 130, 50, "Open WebUI", "chat, uploads, the tool"),
        _box(430, 150, 150, 50, "policy-api", "the pipeline and its API"),
        _line("M285,72 V148", n),
        _line("M350,40 H505 V148", n), _text(362, 33, "Excel and report downloads only"),
        _line("M350,175 H428", n), _text(389, 168, "tool call", anchor="middle"),
        # the chat model answers in Open WebUI; the pipeline calls the models and the database
        _line("M240,200 V222 H60 V260", n), _text(150, 216, "chat answers", anchor="middle"),
        _line("M505,200 V238 H100", n, arrow=False), _text(514, 230, "pipeline calls"),
    ]
    for x in (100, 208, 336, 464, 592):
        b.append(_line(f"M{x},238 V260", n))
    b += [
        _box(20, 262, 120, 52, "judge", "gpt-oss-120b"),
        _box(148, 262, 120, 52, "escalate", "Inkling-Small", cls="box opt"),
        _box(276, 262, 120, 52, "extract", "gpt-oss-20b", cls="box opt"),
        _box(404, 262, 120, 52, "embed + rerank", "TEI"),
        _box(532, 262, 120, 52, "db", "Postgres + pgvector"),
        _box(532, 336, 120, 44, "Argilla", "review queue", cls="box opt"),
        _line("M580,175 H664 V358 H654", n), _text(598, 168, "review sync"),
        '<rect class="box door" x="22" y="344" width="16" height="12" rx="2"/>',
        _text(46, 354, "The only container people can reach"),
        '<rect class="box opt" x="22" y="364" width="16" height="12" rx="2"/>',
        _text(46, 374, "Optional, started with a Compose profile"),
    ]
    return _svg(n, 680, 400, b, "System architecture: staff reach only the proxy; all services sit on a sealed backend network")


PIPELINE = [
    ("Resolve", "Finds the policy group (“Safety”) and the head policy (POL-007 or an upload)", "policy-api + db"),
    ("Diff", "Compares old and new versions section by section, including defined terms", "policy-api"),
    ("Find related sections", "For each changed section, collects sections in the group from three sources", "embed + rerank"),
    ("Judge", "Reads each pair; answers yes, no or unsure, with a reason, quotes and an edit", "judge: gpt-oss-120b"),
    ("Escalate (optional)", "Sends “unsure” pairs to a larger model for a second opinion", "escalate: Inkling-Small"),
    ("Summarise", "Yes if any section says yes, Maybe if any is unsure, otherwise not affected", "policy-api"),
    ("Report", "Builds the chat answer and the Excel workbook in code, from the same result", "policy-api"),
]


def pipeline() -> str:
    n = "pipe"
    tops, y = [], 10
    for i in range(len(PIPELINE)):
        tops.append(y)
        y += 106 if i == 2 else 47
    b = [f'<path class="d-spine" d="M28,{tops[0] + 20} V{tops[-1] + 20}"/>']
    for i, ((name, desc, chip), top) in enumerate(zip(PIPELINE, tops)):
        optional = name.startswith("Escalate")
        b += [
            f'<circle class="{"step opt" if optional else "step"}" cx="28" cy="{top + 20}" r="14"/>',
            _text(28, top + 25, str(i + 1), "d-num", "middle"),
            _text(56, top + 19, name, "d-step"),
            _text(56, top + 37, desc, "d-desc"),
            f'<rect class="{"chip opt" if optional else "chip"}" x="510" y="{top + 7}" width="160" height="26" rx="13"/>',
            _text(590, top + 24, chip, "d-chip", "middle"),
        ]
        if i == 2:
            for j, (t, s) in enumerate((("a. Cites the head policy", "always kept"),
                                        ("b. Uses a changed term", "always kept"),
                                        ("c. Similar in meaning", "reranker keeps the top N"))):
                b.append(_box(56 + j * 148, top + 48, 140, 50, t, s, cls="box small"))
    return _svg(n, 680, tops[-1] + 46, b, "The seven steps that answer one question")


def containment() -> str:
    n = "net"
    b = [
        '<rect class="band-front" x="300" y="12" width="210" height="88" rx="4"/>',
        _text(312, 30, "frontend network", "d-band"),
        '<rect class="band-back" x="16" y="140" width="494" height="206" rx="4"/>',
        _text(28, 160, "backend network: internal: true, no gateway out", "d-band"),
        _box(16, 38, 100, 44, "Users", "web browsers"),
        _box(140, 38, 136, 44, "Your reverse proxy", "TLS and your hostname"),
        _box(372, 40, 120, 44, "proxy", "holds no data", cls="box door"),
        _line("M116,60 H138", n), _line("M276,62 H370", n),
        _line("M432,84 V174", n), _text(424, 118, "Open WebUI,", anchor="end"), _text(424, 131, "downloads, /health", anchor="end"),
    ]
    grid = [("judge", ""), ("embed", ""), ("rerank", ""), ("open-webui", ""),
            ("escalate", "opt"), ("extract", "opt"), ("db", ""), ("policy-api", ""),
            ("argilla", "opt"), ("lora-trainer", "opt"), ("judge-tuned", "opt")]
    for k, (name, opt) in enumerate(grid):
        x, y = 32 + (k % 4) * 116, 176 + (k // 4) * 56
        b.append(_box(x, y, 104, 38, name, cls="box opt" if opt else "box"))
    b += [
        '<rect class="wall" x="530" y="12" width="12" height="334"/>',
        f'<text class="d-band" transform="translate(524,110) rotate(-90)" text-anchor="middle">host firewall</text>',
        '<rect class="box cloud" x="574" y="196" width="96" height="56" rx="26"/>',
        _text(622, 229, "Internet", "d-title", "middle"),
        _line("M510,224 H572", n, cls="d-line blocked"),
        f'<path class="x" d="M528,216 L544,232 M544,216 L528,232"/>',
        _text(574, 278, "Blocked twice: the"), _text(574, 291, "network has no"),
        _text(574, 304, "gateway, and the host"), _text(574, 317, "firewall drops it"),
    ]
    return _svg(n, 680, 356, b, "Network containment: only the proxy is reachable, and nothing on the backend can reach the internet")


def loop() -> str:
    n = "loop"
    b = [
        _box(20, 20, 170, 60, "Policy checks", "the judge gives each pair", "a verdict"),
        _box(255, 20, 170, 60, "Review queue", "every yes and maybe, 1 in 10 no;", "people confirm or correct"),
        _box(490, 20, 170, 60, "Policy database", "answers copied back", "every 5 minutes"),
        _box(490, 160, 170, 60, "Export dataset", "needs 200 or more", "reviewed answers"),
        _box(490, 300, 170, 60, "Train and merge", "LoRA, on lora-trainer"),
        _box(255, 300, 170, 60, "Candidate judge", "served as judge-candidate"),
        _box(20, 300, 170, 60, "Compare", "the same test cases,", "old judge and new"),
        '<path class="box door" d="M105,148 L190,190 L105,232 L20,190 z"/>',
        _text(105, 187, "Recall not lower,", "d-sub-ink", "middle"), _text(105, 200, "precision higher?", "d-sub-ink", "middle"),
        _line("M190,50 H253", n), _line("M425,50 H488", n), _line("M575,80 V158", n), _line("M575,220 V298", n),
        _line("M490,330 H427", n), _line("M255,330 H192", n), _line("M105,300 V234", n),
        _line("M105,148 V82", n), _text(114, 112, "Yes: verdicts move to judge-tuned;"), _text(114, 125, "chat stays on the original judge"),
        _line("M190,190 H298", n, cls="d-line dash"), _text(244, 183, "No", anchor="middle"),
        _box(300, 168, 150, 44, "Keep the current judge", cls="box opt"),
    ]
    return _svg(n, 680, 380, b, "Review and training loop: a new judge replaces the old one only if it scores better")


FIGURES = {
    "system": (system, "What runs where. People reach only the proxy; every service that holds policy text is on a sealed network."),
    "pipeline": (pipeline, "How one question is answered. Code makes every decision; the judge rules on one pair at a time."),
    "containment": (containment, "Two barriers keep policy text in: a network with no gateway, and your host firewall."),
    "loop": (loop, "The review and training loop. A new judge replaces the old one only if it scores better."),
}


def figure(key: str) -> str:
    draw, caption = FIGURES[key]
    return f'<figure>{draw()}<figcaption>{escape(caption)}</figcaption></figure>'
