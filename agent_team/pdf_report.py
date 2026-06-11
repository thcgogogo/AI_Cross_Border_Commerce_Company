"""
pdf_report.py — Render a finished weekly report (markdown) into a branded,
CJK-correct PDF.

Why this is a separate module from deliverables.py: that one writes English-only
deliverables with the default Helvetica font. A Chinese business report needs an
EMBEDDED CJK font or every character renders as a blank box in most viewers. We
bundle a TrueType build of Noto Sans SC at assets/fonts/NotoSansSC-Regular.ttf
because reportlab can subset + embed TrueType outlines but CANNOT embed
CFF/OpenType-PostScript outlines. The embedded subset is what makes the PDF
portable, and the acceptance test asserts it is present.

Public entry points:

    build_report_pdf(out_path=..., markdown_text=..., company_name=...,
                     report_title=..., period_label=..., logo_path=...,
                     brand_color="#1D9E75") -> out_path

    extract_section(markdown_text, *titles, max_lines=3) -> str
        a few lines under the first matching heading (for the email/WeChat blurb)
"""

from __future__ import annotations

import os
import re
from functools import partial

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    HRFlowable,
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from . import config

DEFAULT_BRAND_COLOR = "#1D9E75"
FONT_NAME = "NotoSansSC"

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
FONT_PATH = os.path.join(_PROJECT_ROOT, "assets", "fonts", "NotoSansSC-Regular.ttf")

_MARGIN = 1.8 * cm
_FONT_REGISTERED = False

# Inline source anchors (【1】). Lets us show the "未能生成数据溯源表" banner only
# for a real report that lost its SOURCES_JSON, not for a non-report deliverable.
_ANCHOR_RE = re.compile(r"【\s*\d+\s*】")

# How computed_by renders in the 核验方式 column of the 数据来源与核验 table.
_COMPUTED_BY_CN = {"trend_engine": "趋势引擎（精确计算）", "data_analyst": "数据分析"}


class PdfFontError(RuntimeError):
    """Raised when the bundled CJK font is missing — Chinese could not embed."""


# ---------------------------------------------------------------------------
# Font + small helpers
# ---------------------------------------------------------------------------
def _ensure_font():
    """Register the bundled TrueType CJK font exactly once. Loud if absent —
    the whole point of this module is correct Chinese rendering."""
    global _FONT_REGISTERED
    if _FONT_REGISTERED:
        return
    if not os.path.isfile(FONT_PATH):
        raise PdfFontError(
            "CJK font not found at %s — cannot render Chinese PDF. "
            "Expected bundled assets/fonts/NotoSansSC-Regular.ttf." % FONT_PATH
        )
    pdfmetrics.registerFont(TTFont(FONT_NAME, FONT_PATH))
    # Only a Regular weight is bundled; map every face to it so <b>/<i> markup
    # never errors (it simply renders at regular weight).
    pdfmetrics.registerFontFamily(
        FONT_NAME, normal=FONT_NAME, bold=FONT_NAME,
        italic=FONT_NAME, boldItalic=FONT_NAME,
    )
    _FONT_REGISTERED = True


def _brand(color_hex):
    try:
        return colors.HexColor(color_hex or DEFAULT_BRAND_COLOR)
    except Exception:
        return colors.HexColor(DEFAULT_BRAND_COLOR)


def _inline(text):
    """XML-escape, then re-enable simple **bold** as <b> for Paragraph markup
    and strip inline `code` backticks."""
    esc = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
    esc = re.sub(r"`(.+?)`", r"\1", esc)
    return esc


def _styles(brand):
    return {
        "cover_company": ParagraphStyle(
            "cover_company", fontName=FONT_NAME, fontSize=20, leading=26,
            textColor=brand, spaceAfter=2),
        "cover_title": ParagraphStyle(
            "cover_title", fontName=FONT_NAME, fontSize=15, leading=20,
            textColor=colors.HexColor("#222222")),
        "cover_period": ParagraphStyle(
            "cover_period", fontName=FONT_NAME, fontSize=10, leading=14,
            textColor=colors.HexColor("#666666")),
        "h1": ParagraphStyle(
            "h1", fontName=FONT_NAME, fontSize=15, leading=20, textColor=brand,
            spaceBefore=12, spaceAfter=6),
        "h2": ParagraphStyle(
            "h2", fontName=FONT_NAME, fontSize=13, leading=18,
            textColor=colors.HexColor("#1a1a1a"), spaceBefore=10, spaceAfter=5),
        "h3": ParagraphStyle(
            "h3", fontName=FONT_NAME, fontSize=11.5, leading=16,
            textColor=colors.HexColor("#333333"), spaceBefore=8, spaceAfter=4),
        "body": ParagraphStyle(
            "body", fontName=FONT_NAME, fontSize=10.5, leading=16,
            textColor=colors.HexColor("#222222"), spaceAfter=5),
        "bullet": ParagraphStyle(
            "bullet", fontName=FONT_NAME, fontSize=10.5, leading=16, leftIndent=14,
            textColor=colors.HexColor("#222222"), spaceAfter=3),
        "cell": ParagraphStyle(
            "cell", fontName=FONT_NAME, fontSize=9.5, leading=13,
            textColor=colors.HexColor("#222222")),
        "cell_head": ParagraphStyle(
            "cell_head", fontName=FONT_NAME, fontSize=9.5, leading=13,
            textColor=colors.white),
    }


# ---------------------------------------------------------------------------
# Markdown → flowables
# ---------------------------------------------------------------------------
def _split_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_table_sep(line):
    """A markdown table separator row, e.g. `| --- | :--: |`."""
    cells = [c.strip() for c in _split_row(line)]
    cells = [c for c in cells if c != ""]
    return bool(cells) and all(re.match(r"^:?-{1,}:?$", c) for c in cells)


def _make_table(rows, styles, brand):
    data = []
    for r_i, cells in enumerate(rows):
        style = styles["cell_head"] if r_i == 0 else styles["cell"]
        data.append([Paragraph(_inline(c), style) for c in cells])
    ncols = max(len(r) for r in data)
    for r in data:
        while len(r) < ncols:
            r.append(Paragraph("", styles["cell"]))
    avail = A4[0] - 2 * _MARGIN
    col_w = avail / ncols
    t = Table(data, colWidths=[col_w] * ncols, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), brand),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f4f8f6")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def _story_from_markdown(md, styles, brand):
    story = []
    lines = md.splitlines()
    i, n = 0, len(lines)
    in_fence = False
    while i < n:
        raw = lines[i]
        line = raw.rstrip()
        stripped = line.strip()

        # fenced code block — render the inner lines verbatim as body text
        if stripped.startswith("```"):
            in_fence = not in_fence
            i += 1
            continue
        if in_fence:
            if stripped:
                story.append(Paragraph(_inline(line), styles["body"]))
            i += 1
            continue

        if not stripped:
            i += 1
            continue

        # table: a row followed by a separator row
        if stripped.startswith("|") and i + 1 < n and _is_table_sep(lines[i + 1]):
            header = _split_row(line)
            i += 2  # skip header + separator
            body_rows = []
            while i < n and lines[i].strip().startswith("|"):
                body_rows.append(_split_row(lines[i]))
                i += 1
            story.append(_make_table([header] + body_rows, styles, brand))
            story.append(Spacer(1, 6))
            continue

        # heading
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            level = min(len(m.group(1)), 3)
            story.append(Paragraph(_inline(m.group(2)), styles["h%d" % level]))
            i += 1
            continue

        # horizontal rule
        if re.match(r"^([-*_])\1{2,}$", stripped):
            story.append(HRFlowable(width="100%", thickness=0.6,
                                    color=colors.HexColor("#dddddd"),
                                    spaceBefore=6, spaceAfter=6))
            i += 1
            continue

        # bullet
        m = re.match(r"^[-*]\s+(.*)$", stripped)
        if m:
            story.append(Paragraph("• " + _inline(m.group(1)), styles["bullet"]))
            i += 1
            continue

        # numbered list
        m = re.match(r"^(\d+)[.)]\s+(.*)$", stripped)
        if m:
            story.append(Paragraph("%s. %s" % (m.group(1), _inline(m.group(2))),
                                   styles["bullet"]))
            i += 1
            continue

        # ordinary paragraph (one source line == one paragraph)
        story.append(Paragraph(_inline(line), styles["body"]))
        i += 1
    return story


def _scaled_logo(path, max_w, max_h):
    ir = ImageReader(path)
    iw, ih = ir.getSize()
    if not iw or not ih:
        raise ValueError("logo has no dimensions")
    scale = min(max_w / iw, max_h / ih, 1.0)
    return Image(path, width=iw * scale, height=ih * scale, hAlign="LEFT")


def _cover(story, styles, brand, company_name, report_title, period_label, logo_path):
    if logo_path and os.path.isfile(logo_path):
        try:
            story.append(_scaled_logo(logo_path, max_w=5 * cm, max_h=1.8 * cm))
            story.append(Spacer(1, 8))
        except Exception:
            pass  # a bad logo must never block the report
    story.append(Paragraph(_inline(company_name or "—"), styles["cover_company"]))
    story.append(Paragraph(_inline(report_title or "经营周报"), styles["cover_title"]))
    if period_label:
        story.append(Paragraph(_inline(period_label), styles["cover_period"]))
    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", thickness=2, color=brand, spaceAfter=12))


def _append_sources_section(story, styles, brand, sources, status, markdown_text):
    """Append the 数据来源与核验 section: a table of source rows when parsing
    succeeded (status == "ok"), or a "本次未能生成数据溯源表" banner when it failed
    on a report that clearly had anchors. Adds nothing for a non-report output."""
    if status == "ok" and sources:
        story.append(Spacer(1, 14))
        story.append(HRFlowable(width="100%", thickness=0.8, color=brand,
                                spaceBefore=2, spaceAfter=8))
        story.append(Paragraph("数据来源与核验", styles["h2"]))
        header = ["锚点", "数据 / 结论", "来源文件", "定位", "核验方式"]
        rows = [header]
        for s in sources:
            anchor = s.get("anchor")
            anchor_txt = ("【%s】" % anchor) if anchor is not None else "—"
            rows.append([
                anchor_txt,
                s.get("claim_cn") or "—",
                s.get("source_file") or "—",
                s.get("source_detail") or "—",
                _COMPUTED_BY_CN.get(s.get("computed_by"), s.get("computed_by") or "—"),
            ])
        story.append(_make_table(rows, styles, brand))
        return

    # Parsing failed (or the writer dropped the block). Only warn when the body
    # actually carried 【N】 anchors — a poem or slide deck legitimately has none.
    if status == "malformed" or (status == "missing" and _ANCHOR_RE.search(markdown_text)):
        story.append(Spacer(1, 12))
        story.append(HRFlowable(width="100%", thickness=0.8, color=brand,
                                spaceBefore=2, spaceAfter=8))
        story.append(Paragraph("数据来源与核验", styles["h2"]))
        story.append(Paragraph(
            "⚠️ 本次未能生成数据溯源表，报告中的数字请以正文与原始数据为准。",
            styles["body"]))


# ---------------------------------------------------------------------------
# Per-page footer with a real "page X / Y" (two-pass canvas)
# ---------------------------------------------------------------------------
class _NumberedCanvas(canvas.Canvas):
    """Buffers each page so the footer can print the TOTAL page count, which is
    unknown until the whole document is laid out."""

    def __init__(self, *args, footer_text="", brand=colors.grey, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved = []
        self._footer_text = footer_text
        self._brand = brand

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self._draw_footer(total)
            super().showPage()
        super().save()

    def _draw_footer(self, total):
        w, _h = self._pagesize
        self.saveState()
        self.setStrokeColor(self._brand)
        self.setLineWidth(0.5)
        self.line(_MARGIN, 1.5 * cm, w - _MARGIN, 1.5 * cm)
        self.setFont(FONT_NAME, 8)
        self.setFillColor(colors.HexColor("#888888"))
        text = (self._footer_text
                .replace("{n}", str(self._pageNumber))
                .replace("{total}", str(total)))
        self.drawCentredString(w / 2.0, 1.0 * cm, text)
        self.restoreState()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def build_report_pdf(*, out_path, markdown_text, company_name,
                     report_title="经营周报", period_label="",
                     logo_path=None, brand_color=DEFAULT_BRAND_COLOR,
                     product_name=None, sources=None,
                     sources_status=None, product_domain=""):
    """Render `markdown_text` into a branded, CJK-correct PDF at `out_path`.
    Returns the path. Raises PdfFontError only if the bundled font is missing.

    `product_name` defaults to the config knob (redesign B1) — resolved at call
    time so a .env PRODUCT_NAME edit rebrands the next PDF without a restart.
    `sources` / `sources_status` (Phase 6) drive the 数据来源与核验 section:
    a table when status == "ok", a "本次未能生成数据溯源表" banner when parsing
    failed. `product_domain` (from .env, optional) appends to the marketing footer.
    """
    product_name = product_name or config.product_name()
    _ensure_font()
    brand = _brand(brand_color)
    styles = _styles(brand)

    out_dir = os.path.dirname(os.path.abspath(out_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    story = []
    _cover(story, styles, brand, company_name, report_title, period_label, logo_path)
    body = _story_from_markdown(markdown_text or "", styles, brand)
    if body:
        story.extend(body)
    else:
        story.append(Paragraph("（本次未生成正文内容）", styles["body"]))

    _append_sources_section(story, styles, brand, sources, sources_status,
                            markdown_text or "")

    # Marketing footer: the brief's byline (config.product_byline handles names
    # that already end in "AI", e.g. "Tangent AI") + the optional domain.
    if product_name == config.product_name():
        footer_bits = [config.product_byline()]
    else:  # explicit product_name override keeps the literal template
        footer_bits = ["由 %s AI 团队生成" % product_name, "每个数字可溯源"]
    if (product_domain or "").strip():
        footer_bits.append(product_domain.strip())
    footer_text = " · ".join(footer_bits) + " · 第 {n} / {total} 页"
    doc = SimpleDocTemplate(
        out_path, pagesize=A4,
        leftMargin=_MARGIN, rightMargin=_MARGIN,
        topMargin=1.6 * cm, bottomMargin=2.0 * cm,
        title=report_title, author=product_name,
    )
    doc.build(story, canvasmaker=partial(_NumberedCanvas,
                                         footer_text=footer_text, brand=brand))
    return out_path


def extract_section(markdown_text, *titles, max_lines=3):
    """Return up to `max_lines` content lines under the first heading whose text
    contains any of `titles` (default: "核心结论"). Used for the short
    email/WeChat blurb that accompanies the PDF."""
    if not markdown_text:
        return ""
    titles = titles or ("核心结论",)
    out, capturing = [], False
    for line in markdown_text.splitlines():
        h = re.match(r"^(#{1,6})\s+(.*)$", line.strip())
        if h:
            if capturing:
                break  # the next heading ends the section
            if any(t in h.group(2) for t in titles):
                capturing = True
            continue
        if capturing:
            s = line.strip().lstrip("-*").strip()
            s = re.sub(r"^\d+[.)]\s*", "", s)
            if s:
                out.append(s)
                if len(out) >= max_lines:
                    break
    return "\n".join(out)
