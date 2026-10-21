"""Charts as PNG bytes. matplotlib with the Agg backend; no display, no network."""
from __future__ import annotations

import io
import math

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import ListedColormap  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from . import ACCENT, STATUS_FILL, STATUS_STRONG, STATUS_WORD, clean, impacted_policy_summary  # noqa: E402

DPI = 160
INK, MUTED, GRID = "#1F2933", "#5B6670", "#D9DEE3"
PALETTE = ["#1F3A5F", "#C77D1A", "#2F7F6F", "#8A5A9B", "#B42318", "#5B6670", "#3B7DD8", "#8C6D1F", "#4F6D3B", "#A3476B"]
MAX_LABELS, MAX_SERIES = 50, 10
MAX_TITLE, MAX_LABEL, MAX_SERIES_NAME = 120, 60, 60
KINDS = ("bar", "stacked_bar", "line", "pie")


def _style() -> None:
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 10, "text.color": INK, "axes.labelcolor": MUTED,
        "xtick.color": MUTED, "ytick.color": MUTED, "axes.edgecolor": GRID, "axes.spines.top": False,
        "axes.spines.right": False, "axes.titleweight": "bold", "axes.titlesize": 12, "axes.titlelocation": "left",
        "figure.facecolor": "white", "axes.facecolor": "white",
        # Labels come from policy titles and the assistant: "$...$" must stay plain text, not mathtext
        # (which can be slow to lay out or fail to render).
        "text.parse_math": False, "text.usetex": False,
    })


def _png(fig) -> bytes:
    buf = io.BytesIO()
    try:
        fig.savefig(buf, format="png", dpi=DPI, bbox_inches="tight", facecolor="white")
    finally:
        plt.close(fig)
    return buf.getvalue()


def _wrap(text: str, width: int) -> str:
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    return "\n".join(lines + ([cur] if cur else []))


def _empty(message: str, title: str) -> bytes:
    _style()
    fig, ax = plt.subplots(figsize=(7, 2.2))
    ax.axis("off")
    ax.set_title(title)
    ax.text(0.0, 0.5, message, fontsize=11, color=MUTED, transform=ax.transAxes)
    return _png(fig)


def status_by_policy_png(result: dict) -> bytes:
    title = "Affected sections, by policy"
    pol = impacted_policy_summary(result)
    n_not = result["counts"]["not_impacted"]
    if not pol and not n_not:
        return _empty("No policies were checked.", title)
    _style()
    ids = sorted(pol, key=lambda k: (-pol[k]["Yes"], -pol[k]["Maybe"], k))
    labels = [f"{k}  {clean(pol[k]['title'], 28)}" for k in ids]
    yes = [pol[k]["Yes"] for k in ids]
    maybe = [pol[k]["Maybe"] for k in ids]
    if n_not:
        labels.append(f"{n_not} {'policy' if n_not == 1 else 'policies'} not affected")
        yes.append(0)
        maybe.append(0)
    n = len(labels)
    fig, ax = plt.subplots(figsize=(7.2, 0.5 * n + 1.3))
    ys = list(range(n))
    ax.barh(ys, yes, color="#"+STATUS_STRONG["Yes"], height=0.6, label="Needs updating")
    ax.barh(ys, maybe, left=yes, color="#"+STATUS_STRONG["Maybe"], height=0.6, label="Might be affected")
    for y, a, b in zip(ys, yes, maybe):
        if a + b:
            ax.text(a + b + 0.05, y, f"{a + b}", va="center", fontsize=9, color=INK)
        elif y == n - 1 and n_not:
            ax.text(0.05, y, "none of their sections are affected", va="center", fontsize=9, color=MUTED)
    ax.set_yticks(ys, labels)
    ax.invert_yaxis()
    top = max([a + b for a, b in zip(yes, maybe)] + [1])
    ax.set_xlim(0, top + 0.8)
    ax.xaxis.get_major_locator().set_params(integer=True)
    ax.set_xlabel("Number of sections")
    ax.set_title(title)
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.legend(loc="lower right", frameon=False, fontsize=9)
    return _png(fig)


def _matrix(result: dict):
    changes = result["changes"]
    labels = [c["label"] for c in changes]
    pols: dict[str, str] = {}
    for r in result["rows"]:
        pols.setdefault(r["policy_id"], r["policy_title"])
    cols = sorted(pols)
    rank = {"Maybe": 1, "Yes": 2}
    grid = [[0] * len(cols) for _ in labels]
    for r in result["rows"]:
        if r["head_change"] in labels and r["status"] in rank:
            i, j = labels.index(r["head_change"]), cols.index(r["policy_id"])
            grid[i][j] = max(grid[i][j], rank[r["status"]])
    return labels, cols, pols, grid


def change_matrix_png(result: dict) -> bytes:
    title = "Which change affects which policy"
    labels, cols, pols, grid = _matrix(result)
    if not labels or not cols:
        return _empty("No changes affected any policy.", title)
    _style()
    w = max(6.0, 1.3 * len(cols) + 3.2)
    h = max(2.2, 0.42 * len(labels) + 1.3)
    fig, ax = plt.subplots(figsize=(w, h))
    hexes = ["#FFFFFF", "#" + STATUS_FILL["Maybe"], "#" + STATUS_FILL["Yes"]]
    ax.imshow(grid, cmap=ListedColormap(hexes), vmin=0, vmax=2, aspect="auto")
    ax.set_xticks(range(len(cols)), cols, fontsize=9)
    ax.set_yticks(range(len(labels)), [_wrap(label, 26) for label in labels], fontsize=9)
    ax.xaxis.tick_top()
    ax.tick_params(length=0)
    ax.set_xticks([x - 0.5 for x in range(len(cols) + 1)], minor=True)
    ax.set_yticks([y - 0.5 for y in range(len(labels) + 1)], minor=True)
    ax.grid(which="minor", color=GRID, linewidth=1)
    ax.grid(which="major", visible=False)
    for s in ax.spines.values():
        s.set_visible(False)
    word = {1: ("Maybe", "#"+STATUS_STRONG["Maybe"]), 2: ("Yes", "#"+STATUS_STRONG["Yes"])}
    for i, row in enumerate(grid):
        for j, v in enumerate(row):
            if v:
                ax.text(j, i, word[v][0], ha="center", va="center", fontsize=9.5, fontweight="bold", color=word[v][1])
    ax.set_title(title, pad=34)
    ax.legend(handles=[Patch(fc=hexes[2], ec=GRID, label="Yes: needs updating"), Patch(fc=hexes[1], ec=GRID, label="Maybe: check by hand")],
              loc="upper center", bbox_to_anchor=(0.5, -0.04), ncol=2, frameon=False, fontsize=9)
    return _png(fig)


def impact_types_png(result: dict) -> bytes:
    from . import IMPACT_WORD
    title = "What kind of problem was found"
    counts: dict[str, int] = {}
    for r in result["rows"]:
        if r["status"] in ("Yes", "Maybe"):
            key = IMPACT_WORD.get(r["impact_type"], "Needs a person to decide")
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return _empty("No problems were found.", title)
    _style()
    items = sorted(counts.items(), key=lambda kv: -kv[1])
    fig, ax = plt.subplots(figsize=(7.2, 0.5 * len(items) + 1.2))
    ys = range(len(items))
    ax.barh(ys, [v for _, v in items], color="#" + ACCENT, height=0.6)
    for y, (_, v) in zip(ys, items):
        ax.text(v + 0.05, y, str(v), va="center", fontsize=9)
    ax.set_yticks(list(ys), [_wrap(k, 34) for k, _ in items])
    ax.invert_yaxis()
    ax.set_xlim(0, max(counts.values()) + 0.8)
    ax.xaxis.get_major_locator().set_params(integer=True)
    ax.set_xlabel("Number of sections")
    ax.set_title(title)
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    return _png(fig)


def _text(spec: dict, key: str, cap: int, required: bool = False) -> str:
    v = spec.get(key)
    if v is None or v == "":
        if required:
            raise ValueError(f"The chart needs a {key}.")
        return ""
    if not isinstance(v, str):
        raise ValueError(f"The chart’s {key} must be text.")
    if len(v) > cap:
        raise ValueError(f"The chart’s {key} is too long (the limit is {cap} characters).")
    return clean(v)


def validate_chart_spec(spec) -> dict:
    """Strict check. Returns a cleaned copy or raises ValueError with a plain-English message."""
    if not isinstance(spec, dict):
        raise ValueError("The chart description is not in the expected form.")
    kind = spec.get("type")
    if kind not in KINDS:
        raise ValueError("The chart type must be one of: bar, stacked_bar, line or pie.")
    labels = spec.get("labels")
    if not isinstance(labels, list) or not labels:
        raise ValueError("The chart needs a list of labels.")
    if len(labels) > MAX_LABELS:
        raise ValueError(f"The chart has too many labels (the limit is {MAX_LABELS}).")
    clean_labels = []
    for lab in labels:
        if not isinstance(lab, (str, int, float)) or isinstance(lab, bool):
            raise ValueError("Every chart label must be text or a number.")
        if len(str(lab)) > MAX_LABEL:
            raise ValueError(f"A chart label is too long (the limit is {MAX_LABEL} characters).")
        clean_labels.append(clean(lab))
    series = spec.get("series")
    if not isinstance(series, list) or not series:
        raise ValueError("The chart needs at least one series of numbers.")
    if len(series) > MAX_SERIES:
        raise ValueError(f"The chart has too many series (the limit is {MAX_SERIES}).")
    if kind == "pie" and len(series) != 1:
        raise ValueError("A pie chart can only show one series.")
    out_series = []
    for s in series:
        if not isinstance(s, dict):
            raise ValueError("Each series must have a name and a list of values.")
        name = s.get("name", "")
        if not isinstance(name, str) or len(name) > MAX_SERIES_NAME:
            raise ValueError(f"A series name must be text of at most {MAX_SERIES_NAME} characters.")
        vals = s.get("values")
        if not isinstance(vals, list) or len(vals) != len(labels):
            raise ValueError("Every series must have exactly one value for each label.")
        nums = []
        for v in vals:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError("Every chart value must be a finite number.")
            nums.append(float(v))
        if kind == "pie" and any(v < 0 for v in nums):
            raise ValueError("A pie chart cannot show negative numbers.")
        out_series.append({"name": clean(name), "values": nums})
    return {"type": kind, "title": _text(spec, "title", MAX_TITLE, required=True), "labels": clean_labels,
            "series": out_series, "x_label": _text(spec, "x_label", MAX_LABEL), "y_label": _text(spec, "y_label", MAX_LABEL)}


def custom_chart_png(spec: dict, footnote: str = "") -> bytes:
    s = validate_chart_spec(spec)
    _style()
    labels, series, kind = s["labels"], s["series"], s["type"]
    n = len(labels)
    wide = n > 8 or max(len(x) for x in labels) > 10
    fig, ax = plt.subplots(figsize=(max(6.4, min(12, 0.45 * n + 4)), 4.0))
    x = list(range(n))
    if kind == "pie":
        vals = series[0]["values"]
        if sum(vals) <= 0:
            plt.close(fig)
            raise ValueError("A pie chart needs at least one number above zero.")
        ax.pie(vals, labels=[_wrap(label, 18) for label in labels], colors=[PALETTE[i % 10] for i in range(n)],
               autopct=lambda p: f"{p:.0f}%" if p >= 4 else "", startangle=90, counterclock=False,
               wedgeprops={"edgecolor": "white", "linewidth": 1.5}, textprops={"fontsize": 9},
               pctdistance=0.75)
        for t in ax.texts:
            if t.get_text().endswith("%"):
                t.set_color("white")
                t.set_fontweight("bold")
        ax.set_title(s["title"])
    else:
        if kind == "bar":
            k = len(series)
            w = 0.8 / k
            for i, se in enumerate(series):
                ax.bar([v - 0.4 + w * (i + 0.5) for v in x], se["values"], width=w * 0.92, color=PALETTE[i], label=se["name"])
        elif kind == "stacked_bar":
            pos, neg = [0.0] * n, [0.0] * n
            for i, se in enumerate(series):
                bottoms = [pos[j] if v >= 0 else neg[j] for j, v in enumerate(se["values"])]
                ax.bar(x, se["values"], bottom=bottoms, width=0.6, color=PALETTE[i], label=se["name"])
                for j, v in enumerate(se["values"]):
                    if v >= 0:
                        pos[j] += v
                    else:
                        neg[j] += v
        else:
            for i, se in enumerate(series):
                ax.plot(x, se["values"], color=PALETTE[i], marker="o", linewidth=2, markersize=4, label=se["name"])
        ax.set_xticks(x, [_wrap(label, 14) if not wide else label for label in labels],
                      rotation=40 if wide else 0, ha="right" if wide else "center", fontsize=9)
        ax.axhline(0, color=GRID, linewidth=0.8)
        ax.grid(axis="y", color=GRID, linewidth=0.6)
        ax.set_axisbelow(True)
        if s["x_label"]:
            ax.set_xlabel(s["x_label"])
        if s["y_label"]:
            ax.set_ylabel(s["y_label"])
        ax.set_title(s["title"])
        if len(series) > 1 or series[0]["name"]:
            ax.legend(frameon=False, fontsize=9)
    if footnote:
        fig.text(0.0, -0.02, clean(footnote), fontsize=7.5, color=MUTED, va="top")
    return _png(fig)
