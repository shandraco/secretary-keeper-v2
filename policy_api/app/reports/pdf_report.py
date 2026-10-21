"""PDF report (reportlab). Every value from a document or model is escaped: Paragraph reads mini-HTML."""
from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

import matplotlib
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import registerFontFamily
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (Image, KeepTogether, ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from . import (ACCENT, CHANGE_WORD, DELETE_RGB, IMPACT_WORD, INSERT_RGB, STATUS_FILL, STATUS_STRONG, STATUS_WORD, clean,
               headline, impacted_policy_summary)
from .charts import change_matrix_png, impact_types_png, status_by_policy_png
from .redline import redline

FONT, BOLD, ITALIC = "DejaVuSans", "DejaVuSans-Bold", "DejaVuSans-Oblique"
PAGE_W, PAGE_H = LETTER
MARGIN = 0.8 * inch
CONTENT_W = PAGE_W - 2 * MARGIN
_registered = False


def register_fonts() -> None:
    """DejaVu ships inside matplotlib, so no font is ever downloaded."""
    global _registered
    if _registered:
        return
    ttf = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
    for name, file in ((FONT, "DejaVuSans.ttf"), (BOLD, "DejaVuSans-Bold.ttf"), (ITALIC, "DejaVuSans-Oblique.ttf"),
                       ("DejaVuSans-BoldOblique", "DejaVuSans-BoldOblique.ttf")):
        pdfmetrics.registerFont(TTFont(name, str(ttf / file)))
    registerFontFamily(FONT, normal=FONT, bold=BOLD, italic=ITALIC, boldItalic="DejaVuSans-BoldOblique")
    _registered = True


def styles() -> dict[str, ParagraphStyle]:
    register_fonts()
    ink = colors.HexColor("#1F2933")
    base = ParagraphStyle("base", fontName=FONT, fontSize=9.5, leading=13.5, textColor=ink, alignment=TA_LEFT)
    return {
        "body": base,
        "small": ParagraphStyle("small", parent=base, fontSize=8.5, leading=11.5),
        "muted": ParagraphStyle("muted", parent=base, fontSize=8.5, leading=11.5, textColor=colors.HexColor("#5B6670")),
        "title": ParagraphStyle("title", parent=base, fontName=BOLD, fontSize=21, leading=26, textColor=colors.HexColor("#" + ACCENT), spaceAfter=4),
        "lead": ParagraphStyle("lead", parent=base, fontSize=12.5, leading=18, spaceAfter=6),
        "h1": ParagraphStyle("h1", parent=base, fontName=BOLD, fontSize=14, leading=18, textColor=colors.HexColor("#" + ACCENT), spaceBefore=16, spaceAfter=6, keepWithNext=1),
        "h2": ParagraphStyle("h2", parent=base, fontName=BOLD, fontSize=10.5, leading=14, spaceBefore=8, spaceAfter=3, keepWithNext=1),
        "big": ParagraphStyle("big", parent=base, fontName=BOLD, fontSize=22, leading=28),
        "head": ParagraphStyle("head", parent=base, fontName=BOLD, fontSize=8.5, leading=11, textColor=colors.white),
        "cell": ParagraphStyle("cell", parent=base, fontSize=8.5, leading=11.5),
    }


def esc(value, limit: int | None = None) -> str:
    return escape(clean(value, limit)).replace("\n", "<br/>")


def redline_markup(old: str, new: str) -> str:
    out = []
    prev = ""
    for op, text in redline(clean(old), clean(new)):
        if op == "insert" and prev == "delete":
            out.append(" ")  # a thin gap so the struck and the new word do not run together
        t = esc(text)
        if op == "delete":
            out.append(f'<font color="#{DELETE_RGB}"><strike>{t}</strike></font>')
        elif op == "insert":
            out.append(f'<font color="#{INSERT_RGB}"><u>{t}</u></font>')
        else:
            out.append(t)
        prev = op
    return "".join(out)


def legend_para(st) -> Paragraph:
    return Paragraph(f'How to read the wording: <font color="#{DELETE_RGB}"><strike>red crossed-out text</strike></font> was removed; '
                     f'<font color="#{INSERT_RGB}"><u>blue underlined text</u></font> is new.', st["muted"])


def image(png: bytes, width: float = CONTENT_W) -> Image:
    from reportlab.lib.utils import ImageReader
    iw, ih = ImageReader(io.BytesIO(png)).getSize()
    w = min(width, iw * 72 / 160)
    return Image(io.BytesIO(png), width=w, height=w * ih / iw, hAlign="LEFT")


def table(st, header: list[str], rows: list[list], widths: list[float], status_col: int | None = None,
          raw_cols: tuple[int, ...] = ()) -> Table:
    """rows hold plain text (escaped here) except columns in raw_cols, which hold ready-made markup."""
    data = [[Paragraph(esc(h), st["head"]) for h in header]]
    for r in rows:
        data.append([Paragraph(str(v) if i in raw_cols else esc(v), st["cell"]) for i, v in enumerate(r)])
    total = sum(widths)
    t = Table(data, colWidths=[w / total * CONTENT_W for w in widths], repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#" + ACCENT)),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, colors.HexColor("#D9DEE3")),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]
    if status_col is not None:
        for i, r in enumerate(rows, 1):
            key = {STATUS_WORD["Yes"]: "Yes", STATUS_WORD["Maybe"]: "Maybe"}.get(str(r[status_col]))
            if key:
                style.append(("BACKGROUND", (status_col, i), (status_col, i), colors.HexColor("#" + STATUS_FILL[key])))
    t.setStyle(TableStyle(style))
    return t


def bullets(st, items: list[str], raw: bool = False) -> ListFlowable:
    return ListFlowable([ListItem(Paragraph(i if raw else esc(i), st["body"]), leftIndent=14) for i in items],
                        bulletType="bullet", bulletFontName=FONT, bulletFontSize=8, leftIndent=14, start="•")


class _NumberedCanvas(canvas.Canvas):
    footer_text = ""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._pages: list[dict] = []

    def showPage(self):
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self.setFont(FONT, 7.5)
            self.setFillColor(colors.HexColor("#5B6670"))
            self.setStrokeColor(colors.HexColor("#D9DEE3"))
            self.line(MARGIN, 0.62 * inch, PAGE_W - MARGIN, 0.62 * inch)
            self.drawString(MARGIN, 0.45 * inch, self.footer_text)
            self.drawRightString(PAGE_W - MARGIN, 0.45 * inch, f"Page {self._pageNumber} of {total}")
            super().showPage()
        super().save()


def render(story: list, title: str, footer: str) -> bytes:
    register_fonts()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=LETTER, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=0.75 * inch,
                            bottomMargin=0.9 * inch, title=clean(title, 200), author="Policy impact checker")
    footer = clean(footer, 160)
    canvas_cls = type("Canvas", (_NumberedCanvas,), {"footer_text": footer})
    doc.build(story, canvasmaker=canvas_cls)
    return buf.getvalue()


def _date(result: dict) -> str:
    raw = str(result.get("finished_at") or result.get("created_at") or "")
    try:
        return datetime.fromisoformat(raw).strftime("%d %b %Y")
    except ValueError:
        return clean(raw[:10])


def build_pdf(result: dict) -> bytes:
    st = styles()
    c, head = result["counts"], result["head"]
    story: list = [
        Paragraph("Policy impact check", st["title"]),
        Paragraph(esc(f"{head['id']} {head['title']}: {head['old_label']} → {head['new_label']}"), st["muted"]),
        Spacer(1, 10),
        Paragraph(esc(headline(result)), st["lead"]),
    ]
    if result["method"].get("demo_mode"):
        story.append(Paragraph('<font color="#B42318"><b>DEMO DATA:</b> produced by stand-in models, not real ones. Never use for real decisions.</font>', st["small"]))
    # counts
    def tile(n, label, key):
        return [Paragraph(f'<font color="#{STATUS_STRONG.get(key, ACCENT)}">{n}</font>', st["big"]),
                Paragraph(esc(label), st["small"])]
    tiles = Table([[tile(c["impacted"], "need updating", "Yes"), tile(c["maybe"], "might be affected", "Maybe"),
                    tile(c["not_impacted"], "not affected", "No"), tile(c["changes"], "changed sections in the head policy", None)]],
                  colWidths=[CONTENT_W / 4] * 4)
    tiles.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D9DEE3")),
                               ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D9DEE3")),
                               ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#" + STATUS_FILL["Yes"])),
                               ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#" + STATUS_FILL["Maybe"])),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story += [Spacer(1, 8), tiles]
    if c["errors"]:
        story.append(Spacer(1, 4))
        story.append(Paragraph(esc(f"{c['errors']} section pairs could not be checked automatically and are marked “Might be affected”."), st["small"]))

    # charts
    story.append(Paragraph("At a glance", st["h1"]))
    story.append(image(status_by_policy_png(result), CONTENT_W * 0.8))
    story.append(Spacer(1, 8))
    story.append(KeepTogether([image(change_matrix_png(result), CONTENT_W * 0.72)]))
    if any(r["status"] in ("Yes", "Maybe") for r in result["rows"]):
        story.append(Spacer(1, 8))
        story.append(KeepTogether([image(impact_types_png(result), CONTENT_W * 0.8)]))

    # impacted table
    yes = [r for r in result["rows"] if r["status"] == "Yes"]
    story.append(Paragraph("Needs updating", st["h1"]))
    if yes:
        story.append(table(st, ["Policy", "Section", "Status", "Why"],
                           [[f"{r['policy_id']}\n{r['policy_title']}", f"§{r['clause']}", STATUS_WORD["Yes"], r["why"]] for r in yes],
                           [23, 11, 14, 52], status_col=2))
    else:
        story.append(Paragraph("No section needs updating.", st["body"]))

    # what changed
    intro = [Paragraph("What changed in the head policy", st["h1"]), legend_para(st), Spacer(1, 4)]
    if result["changes"]:
        for i, ch in enumerate(result["changes"]):
            block = [Paragraph(esc(f"{ch['label']}"), st["h2"]), Paragraph(redline_markup(ch["old_text"], ch["new_text"]), st["body"])]
            story.append(KeepTogether((intro if i == 0 else []) + block))
    else:
        story += intro + [Paragraph("No wording changed.", st["body"])]

    # maybe
    maybe = [r for r in result["rows"] if r["status"] == "Maybe"]
    story.append(Paragraph("Might be affected", st["h1"]))
    if maybe:
        story.append(Paragraph("A person should read these and decide.", st["muted"]))
        story.append(Spacer(1, 4))
        story.append(table(st, ["Policy", "Section", "Status", "Why"],
                           [[f"{r['policy_id']}\n{r['policy_title']}", f"§{r['clause']}", STATUS_WORD["Maybe"], r["why"]] for r in maybe],
                           [23, 11, 14, 52], status_col=2))
    else:
        story.append(Paragraph("Nothing needs a second look.", st["body"]))

    story.append(Paragraph("Checked, not affected", st["h1"]))
    if result["not_impacted"]:
        story.append(table(st, ["Policy", "Owner", "Reason"],
                           [[f"{p['policy_id']}\n{p['title']}", p.get("owner") or "", p["reason"]] for p in result["not_impacted"]], [28, 20, 52]))
    else:
        story.append(Paragraph("Every policy in the group had at least one section to look at.", st["body"]))

    m = result["method"]
    story.append(Paragraph("How this was checked", st["h1"]))
    story.append(bullets(st, [
        f"Each changed section of {head['id']} was compared with sections in the other {result['subset']} policies. {c['pairs_checked']} section pairs were looked at.",
        f"Sections were found by what they cite and what they mean (matching: {m.get('embedder')}, {m.get('reranker')}).",
        f"A checking model ({m.get('judge_model')}) judged each pair; {c['escalated']} unclear pairs were passed to a second model ({m.get('escalate_model')}).",
        "Documents, charts and tables are built by code from these results. The model does not write them.",
        "A person should still read every “Needs updating” and “Might be affected” item before changing a policy.",
    ]))
    return render(story, f"Policy impact check {head['id']}", f"Run {result['run_id']}  ·  {_date(result)}")
