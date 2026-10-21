"""The handbook diagrams, drawn as inline SVG. They use the page's fonts and the classes in style.css."""
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
        _text(22, 128, "backend network: sealed, one way out", "d-band"),
        _box(20, 24, 130, 48, "Staff", "in a web browser"),
        _box(220, 24, 130, 48, "proxy", "nginx, the only way in", cls="box door"),
        _line("M150,48 H218", n),
        _box(220, 150, 130, 50, "Open WebUI", "chat, uploads, the tool"),
        _box(430, 150, 150, 50, "policy-api", "the pipeline and its API"),
        _line("M285,72 V148", n),
        _line("M350,40 H505 V148", n), _text(362, 33, "Excel and report downloads only"),
        _line("M350,175 H428", n), _text(389, 168, "tool call", anchor="middle"),
        # the chat model answers in Open WebUI; the pipeline calls the models and the database
        _line("M240,200 V222 H80 V260", n), _text(150, 216, "chat answers", anchor="middle"),
        _line("M505,200 V238 H100", n, arrow=False), _text(514, 230, "pipeline calls"),
    ]
    for x in (100, 208, 336, 464, 592):
        b.append(_line(f"M{x},238 V260", n))
    b += [
        _box(20, 262, 120, 52, "granite-gateway", "nginx, the one way out"),
        _box(148, 262, 120, 52, "escalate", "Inkling-Small", cls="box opt"),
        _box(276, 262, 120, 52, "extract", "gpt-oss-20b", cls="box opt"),
        _box(404, 262, 120, 52, "embed + rerank", "TEI"),
        _box(532, 262, 120, 52, "db", "Postgres + pgvector"),
        _box(532, 336, 120, 44, "Argilla", "review queue", cls="box opt"),
        _line("M580,175 H664 V358 H654", n), _text(598, 168, "review sync"),
        _line("M80,314 V410", n, cls="d-line dash"), _text(88, 362, "GRANITE_URL only"),
        _box(20, 412, 120, 40, "Granite 4.2 30B", "your own vLLM server", cls="box cloud"),
        _text(150, 436, "Runs outside this stack; reached only through the gateway"),
        '<rect class="box door" x="180" y="344" width="16" height="12" rx="2"/>',
        _text(204, 354, "The only container people can reach"),
        '<rect class="box opt" x="180" y="364" width="16" height="12" rx="2"/>',
        _text(204, 374, "Optional, started with a Compose profile"),
    ]
    return _svg(n, 680, 460, b, "System architecture: staff reach only the proxy; all services sit on a sealed backend network whose one way out is granite-gateway")


PIPELINE = [
    ("Resolve", "Finds the policy group (“Safety”) and the head policy (POL-007 or an upload)", "policy-api + db"),
    ("Diff", "Compares old and new versions section by section, including defined terms", "policy-api"),
    ("Find related sections", "For each changed section, collects sections in the group from three sources", "embed + rerank"),
    ("Judge", "Reads each pair; answers yes, no or unsure, with a reason, quotes and an edit", "Granite 4.2 30B"),
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
        _text(28, 160, "backend network: internal: true, one way out", "d-band"),
        _box(16, 38, 100, 44, "Users", "web browsers"),
        _box(140, 38, 136, 44, "Your reverse proxy", "TLS and your hostname"),
        _box(372, 40, 120, 44, "proxy", "holds no data", cls="box door"),
        _line("M116,60 H138", n), _line("M276,62 H370", n),
        _line("M432,84 V174", n), _text(424, 118, "Open WebUI,", anchor="end"), _text(424, 131, "downloads, /health", anchor="end"),
    ]
    grid = [("embed", ""), ("rerank", ""), ("open-webui", ""), ("hermes", ""),
            ("escalate", "opt"), ("extract", "opt"), ("db", ""), ("policy-api", ""),
            ("argilla", "opt"), ("lora-trainer", "opt"), ("judge-tuned", "opt")]
    for k, (name, opt) in enumerate(grid):
        x, y = 32 + (k % 4) * 116, 176 + (k // 4) * 56
        b.append(_box(x, y, 104, 38, name, cls="box opt" if opt else "box"))
    b += [
        _box(380, 288, 104, 38, "granite-gateway", cls="box door"),
        _line("M484,307 H572", n),
        _box(574, 285, 96, 44, "Granite server", "your vLLM", cls="box cloud"),
        _text(622, 343, "chat + models only", anchor="middle"),
    ]
    b += [
        '<rect class="wall" x="530" y="12" width="12" height="334"/>',
        f'<text class="d-band" transform="translate(524,110) rotate(-90)" text-anchor="middle">host firewall</text>',
        '<rect class="box cloud" x="574" y="150" width="96" height="56" rx="26"/>',
        _text(622, 183, "Internet", "d-title", "middle"),
        _line("M510,178 H572", n, cls="d-line blocked"),
        f'<path class="x" d="M528,170 L544,186 M544,170 L528,186"/>',
        _text(574, 222, "Blocked twice: the"), _text(574, 235, "gateway only reaches"),
        _text(574, 248, "Granite, and the host"), _text(574, 261, "firewall drops the rest"),
    ]
    return _svg(n, 680, 356, b, "Network containment: only the proxy is reachable, and the backend reaches only your Granite server, through granite-gateway")


def loop() -> str:
    n = "loop"
    b = [
        _box(20, 20, 170, 60, "Policy checks", "Granite gives each pair", "a verdict"),
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


def _badge(x, y, n, mid_cls="step"):
    return f'<circle class="{mid_cls}" cx="{x}" cy="{y}" r="10"/>' + _text(x, y + 4.5, str(n), "d-num", "middle")


def hl_flow() -> str:
    n = "hlflow"
    b = [
        '<rect class="band-front" x="8" y="8" width="664" height="64" rx="4"/>',
        _text(664, 24, "frontend network", "d-band", "end"),
        '<rect class="band-back" x="8" y="88" width="664" height="290" rx="4"/>',
        _text(22, 370, "backend network: sealed, one way out", "d-band"),
        '<rect class="band-front" x="300" y="346" width="210" height="96" rx="4"/>',
        _text(308, 436, "llm-egress network", "d-band"),
        _box(20, 26, 100, 40, "Staff", "web browser"),
        _box(150, 26, 100, 40, "proxy", "nginx", cls="box door"),
        _line("M120,46 H148", n),
        _box(150, 138, 100, 54, "Open WebUI", "chat screen,", "upload filter"),
        _line("M200,66 V136", n),
        # Hermes, with the plugin inside it
        '<rect class="box" x="310" y="134" width="170" height="140" rx="3"/>',
        _text(395, 154, "Hermes Agent", "d-title", "middle"),
        _text(395, 169, "API server on port 8642", "d-sub", "middle"),
        _text(395, 182, "decides which tool to call", "d-sub", "middle"),
        _box(322, 196, 146, 50, "secretary plugin", "9 policy tools,", "no web, files or terminal", cls="box small"),
        # policy-api, with the worker and the check graph inside it
        '<rect class="box" x="500" y="124" width="170" height="140" rx="3"/>',
        _text(585, 144, "policy-api", "d-title", "middle"),
        _text(585, 159, "FastAPI and job queue", "d-sub", "middle"),
        _box(510, 172, 150, 82, "worker + check graph", "LangGraph, one run per check", "saved after every step", cls="box small"),
        _box(500, 296, 76, 46, "db", "Postgres", "+ checkpoints"),
        _box(594, 296, 76, 46, "embed", "+ rerank", "(TEI)"),
        _line("M538,264 V294", n), _line("M632,264 V294", n),
        _line("M250,165 H308", n),
        _line("M468,205 H498", n),
        _line("M380,274 V350", n),
        _line("M500,250 H490 V372 H482", n),
        _line("M230,138 V118 H600 V122", n, cls="d-line dash"),
        _box(330, 352, 150, 40, "granite-gateway", "backend + llm-egress", cls="box door"),
        _line("M450,392 V458", n, cls="d-line dash"), _text(458, 428, "chat + model list only"),
        _box(330, 460, 150, 40, "Granite 4.2 30B", "your own vLLM server", cls="box cloud"),
        _badge(135, 29, 1), _badge(216, 102, 2), _badge(279, 150, 3), _badge(394, 300, 4),
        _badge(487, 188, 5), _badge(490, 300, 6), _badge(420, 118, 7),
    ]
    legend = ["Staff reach only the proxy", "The proxy passes the chat to Open WebUI",
              "Open WebUI sends the chat to Hermes", "Hermes asks Granite what to do next",
              "The plugin calls policy-api", "The graph asks Granite for verdicts",
              "Files and decisions go to policy-api"]
    for i, t in enumerate(legend):
        y = 228 + i * 15
        b += [f'<circle class="step" cx="30" cy="{y}" r="6.5"/>', _text(30, y + 3, str(i + 1), "d-sub-ink", "middle"), _text(44, y + 3.5, t, "d-sub")]
    return _svg(n, 680, 508, b, "Request path: Open WebUI to Hermes to the plugin to policy-api, where a worker runs each check as a graph; Hermes and the graph reach Granite only through granite-gateway")


def langgraph_graph() -> str:
    n = "lgraph"
    b = [
        '<rect class="band-back" x="8" y="104" width="664" height="24" rx="4"/>',
        _text(18, 120, "Saved to Postgres after every step (checkpoint). A crash resumes from the last saved step.", "d-band"),
        _text(20, 40, "start", "d-sub"),
        _box(20, 50, 110, 44, "load", "policies and versions"),
        _box(150, 50, 110, 44, "diff", "what changed"),
        _box(280, 50, 110, 44, "find_related", "candidate sections"),
        _box(410, 50, 110, 44, "judge", "Granite, per pair"),
        _box(552, 50, 112, 44, "escalate", "Inkling-Small", cls="box opt"),
        _line("M130,72 H148", n), _line("M260,72 H278", n), _line("M390,72 H408", n),
        _line("M520,72 H550", n), _text(536, 44, "some unsure", "d-sub-ink", "middle"),
    ]
    for x in (75, 205, 335, 440, 608):
        b.append(_line(f"M{x},94 V104", n, cls="d-line dash", arrow=False))
    b += [
        _box(410, 150, 110, 44, "summarise", "Yes, Maybe, no"),
        _line("M465,94 V148", n), _text(473, 142, "none unsure", "d-sub-ink"),
        _line("M608,94 V172 H522", n),
        '<circle class="step" cx="300" cy="172" r="17"/>', _text(300, 175, "END", "d-sub-ink", "middle"),
        _line("M408,172 H319", n), _text(364, 165, "no Maybe", "d-sub-ink", "middle"),
        _box(410, 250, 110, 50, "review", "interrupt(): waits here", cls="box door"),
        _line("M465,194 V248", n), _text(473, 222, "Maybe, and REVIEW_GATE on", "d-sub-ink"),
        _line("M410,275 H300 V191", n), _text(356, 268, "answer applied", "d-sub-ink", "middle"),
        # how the answer gets in
        _box(20, 330, 130, 50, "Person types", "decide 3f9a1c2e SAF-150", "3.1 yes, ..."),
        _box(180, 330, 130, 50, "Upload filter", "reads their own message", "in Open WebUI"),
        _box(340, 330, 150, 50, "POST /runs/id/review", "policy-api claims the check", "and queues a job"),
        _line("M150,355 H178", n), _line("M310,355 H338", n),
        _line("M440,330 V302", n), _text(450, 322, "worker resumes the graph", "d-sub-ink"),
        _text(20, 160, "Routes are chosen by code, never by a model.", "d-sub"),
        _text(20, 175, "Only judge and escalate ask a model,", "d-sub"),
        _text(20, 190, "and only about one pair of sections.", "d-sub"),
        _text(20, 205, "Recursion limit 25; a check takes 8 steps.", "d-sub"),
        _text(20, 220, "REVIEW_GATE off: the check ends at END.", "d-sub"),
        _text(20, 300, "Decisions come from the person, not the model.", "d-band"),
    ]
    return _svg(n, 680, 400, b, "The check graph: load, diff, find related, judge, optional escalate, summarise, then an optional pause for a person's decision, with a checkpoint saved after each step")


def hermes_loop() -> str:
    n = "hloop"
    cols = {"ow": 70, "he": 208, "gr": 346, "pl": 484, "pa": 614}
    heads = [("ow", "Open WebUI", "chat screen"), ("he", "Hermes", "the agent"), ("gr", "Granite 4.2 30B", "via granite-gateway"),
             ("pl", "secretary plugin", "inside Hermes"), ("pa", "policy-api", "job queue and graph")]
    msgs = [("ow", "he", "question + upload id", False), ("he", "gr", "history + tool list", False),
            ("gr", "he", "calls check_policy_change", True), ("he", "pl", "run the tool with these arguments", False),
            ("pl", "pa", "POST /runs", False), ("pa", "pl", "202: run id, job queued", True),
            ("pl", "pa", "GET /runs/id, every 3 s", False), ("pa", "pl", "done: answer + Excel link", True),
            ("pl", "he", "tool result, as text", True), ("he", "gr", "result added to the chat", False),
            ("gr", "he", "writes the final answer", True), ("he", "ow", "answer sent back to the chat", True)]
    ys = [84] + [130 + 30 * i for i in range(10)] + [452]
    b = [f'<rect class="band-back" x="150" y="{ys[1] - 36}" width="516" height="{ys[10] - ys[1] + 48}" rx="4"/>']
    for k, x in cols.items():
        b.append(f'<path class="d-spine" d="M{x},50 V470"/>')
    b.append(_text(158, ys[1] - 22, "Repeats until Granite answers in words, at most 30 rounds (max_turns)", "d-band"))
    for k, name, sub in heads:
        b.append(_box(cols[k] - 58, 12, 116, 38, name, sub))
    for (a, c, label, ret), y in zip(msgs, ys):
        x1, x2 = cols[a], cols[c]
        b.append(_line(f"M{x1},{y} H{x2 + (-3 if x2 < x1 else 3)}", n, cls="d-line dash" if ret else "d-line"))
        b.append(_text((x1 + x2) / 2, y - 5, label, "d-sub-ink", "middle"))
    return _svg(n, 680, 480, b, "The agent loop: Hermes asks Granite, Granite proposes a tool call, the plugin runs it against policy-api, and the result goes back to Granite until it writes the answer")


FIGURES = {
    "system": (system, "What runs where. People reach only the proxy; every service that holds policy text is on a sealed network."),
    "pipeline": (pipeline, "How one question is answered. Code makes every decision; the judge rules on one pair at a time."),
    "containment": (containment, "Two barriers keep policy text in: a network with one narrow way out, and your host firewall."),
    "hl-flow": (hl_flow, "The request path. People reach only the proxy; Hermes and the check graph reach Granite only through the gateway."),
    "langgraph-graph": (langgraph_graph, "One check as a graph. Code picks every route, a checkpoint is saved after each step, and a person's typed decision resumes a paused check."),
    "hermes-loop": (hermes_loop, "One question, step by step. Granite proposes tool calls; the plugin runs them against policy-api; code makes every decision."),
    "loop": (loop, "The review and training loop. A new judge replaces the old one only if it scores better."),
}


def figure(key: str) -> str:
    draw, caption = FIGURES[key]
    return f'<figure>{draw()}<figcaption>{escape(caption)}</figcaption></figure>'
