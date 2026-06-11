"""
deliverables.py — Turn an agent's structured content into REAL files.

This is what lets the Creative & Media Producer (Leo) hand back a genuine
.pptx / .pdf / .docx / .svg the user can download — not just a description of one.

How it fits the framework:
  - Each builder below is exposed to Claude as a TOOL (see TOOL_SPECS). When an
    agent's config lists one of these tool names, the LLM wrapper lets that agent
    CALL the tool; Claude supplies the structured content (slides, sections, svg
    markup) and we write the actual file to the job's output folder.
  - Adding a new kind of deliverable = add one builder + one entry in _BUILDERS
    and TOOL_SPECS. Nothing else in the framework changes (config-driven).

Honesty note: a language model can author text and vector (SVG) markup directly,
so .pptx / .pdf / .docx / .svg are produced for real here. It cannot paint raster
photos or render video frames — those still come back as briefs/storyboards in the
agent's text. We never fake a binary we can't actually make.
"""

import os

from werkzeug.utils import secure_filename

from . import crypto


# ---------------------------------------------------------------------------
# TOOL SCHEMAS — the contract Claude sees. Keep these tight and unambiguous so
# the model fills them in reliably.
# ---------------------------------------------------------------------------
TOOL_SPECS = [
    {
        "name": "create_slide_deck",
        "description": (
            "Create a real PowerPoint (.pptx) slide deck and save it as a downloadable "
            "file for the user. Use this whenever the user wants slides, a deck, or a "
            "presentation. Provide a title slide and an ordered list of content slides."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string",
                             "description": "Base file name, no extension, e.g. 'q4_review'."},
                "title": {"type": "string", "description": "Deck title for the title slide."},
                "subtitle": {"type": "string", "description": "Optional subtitle/author/date line."},
                "slides": {
                    "type": "array",
                    "description": "Content slides, in presentation order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Slide headline."},
                            "bullets": {"type": "array", "items": {"type": "string"},
                                        "description": "Bullet points shown on the slide."},
                            "notes": {"type": "string", "description": "Optional speaker notes."},
                        },
                        "required": ["title"],
                    },
                },
            },
            "required": ["title", "slides"],
        },
    },
    {
        "name": "create_pdf_document",
        "description": (
            "Create a real PDF document and save it as a downloadable file for the user. "
            "Use this for reports, briefs, one-pagers, or any polished document the user "
            "wants as a PDF. Provide a title and ordered sections."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Base file name, no extension."},
                "title": {"type": "string", "description": "Document title."},
                "sections": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "heading": {"type": "string", "description": "Optional section heading."},
                            "body": {"type": "string",
                                     "description": "Section text. Separate paragraphs with a blank "
                                                    "line; start a line with '- ' for a bullet."},
                        },
                        "required": ["body"],
                    },
                },
            },
            "required": ["title", "sections"],
        },
    },
    {
        "name": "create_word_document",
        "description": (
            "Create a real Microsoft Word (.docx) document and save it as a downloadable "
            "file for the user. Use this when the user wants an editable Word document. "
            "Same shape as a PDF: a title and ordered sections."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Base file name, no extension."},
                "title": {"type": "string", "description": "Document title."},
                "sections": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "heading": {"type": "string", "description": "Optional section heading."},
                            "body": {"type": "string",
                                     "description": "Section text. Separate paragraphs with a blank "
                                                    "line; start a line with '- ' for a bullet."},
                        },
                        "required": ["body"],
                    },
                },
            },
            "required": ["title", "sections"],
        },
    },
    {
        "name": "create_svg_image",
        "description": (
            "Create a real vector image as an .svg file and save it as a downloadable file "
            "for the user. SVG is great for logos, diagrams, charts, icons, and infographics "
            "that you can describe precisely in markup. (It is NOT for photographic/raster "
            "images — for those, write an image-generation brief in your text instead.) "
            "Provide complete, valid SVG markup."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Base file name, no extension."},
                "title": {"type": "string", "description": "Short label / alt text for the image."},
                "svg": {"type": "string",
                        "description": "Complete, valid SVG markup beginning with <svg ...> and "
                                       "ending with </svg>, including a width/height or viewBox."},
            },
            "required": ["svg"],
        },
    },
]

# name -> (file extension, human kind, builder function)
# (builder functions are defined below; bound at the bottom of the module.)
TOOL_NAMES = {spec["name"] for spec in TOOL_SPECS}


def specs_for(names):
    """Return the TOOL_SPECS subset for `names`, preserving declaration order."""
    wanted = set(names or [])
    return [s for s in TOOL_SPECS if s["name"] in wanted]


# ---------------------------------------------------------------------------
# The executor the agent loop calls when Claude invokes a tool.
# ---------------------------------------------------------------------------
def run_tool(name, tool_input, out_dir):
    """Build the requested file in `out_dir`. Never raises — returns a result dict:
        {ok: bool, message: str, kind, ext, title, filename, path}  (file fields on ok)."""
    spec = _BUILDERS.get(name)
    if not spec:
        return {"ok": False, "message": f"Unknown tool: {name!r}"}
    ext, kind, builder = spec
    tool_input = tool_input or {}

    try:
        os.makedirs(out_dir, exist_ok=True)
        title = str(tool_input.get("title") or "").strip()
        base = tool_input.get("filename") or title or kind
        filename = _unique(out_dir, _safe_filename(base, ext))
        path = os.path.join(out_dir, filename)
        builder(tool_input, path)
        crypto.encrypt_file_in_place(path)  # at-rest; no-op when FERNET unset
    except Exception as e:  # surface the reason to the model so it can fix or move on
        return {"ok": False, "message": f"Could not create the {kind}: {type(e).__name__}: {e}"}

    return {"ok": True, "kind": kind, "ext": ext,
            "title": title or filename, "filename": filename, "path": path,
            "message": f"Created {kind} '{filename}'."}


# ---------------------------------------------------------------------------
# Builders — each takes (tool_input dict, output path) and writes a real file.
# ---------------------------------------------------------------------------
def _build_pptx(data, path):
    from pptx import Presentation

    prs = Presentation()
    # Title slide.
    title_slide = prs.slides.add_slide(prs.slide_layouts[0])
    title_slide.shapes.title.text = str(data.get("title") or "Untitled deck")
    if len(title_slide.placeholders) > 1:
        title_slide.placeholders[1].text = str(data.get("subtitle") or "")

    for slide in data.get("slides") or []:
        s = prs.slides.add_slide(prs.slide_layouts[1])  # "Title and Content"
        s.shapes.title.text = str(slide.get("title") or "")
        bullets = [str(b) for b in (slide.get("bullets") or []) if str(b).strip()]
        tf = s.placeholders[1].text_frame
        tf.clear()
        if bullets:
            tf.text = bullets[0]
            for b in bullets[1:]:
                tf.add_paragraph().text = b
        notes = slide.get("notes")
        if notes:
            s.notes_slide.notes_text_frame.text = str(notes)

    prs.save(path)


def _build_pdf(data, path):
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (ListFlowable, ListItem, Paragraph,
                                    SimpleDocTemplate, Spacer)

    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        path, pagesize=LETTER,
        topMargin=0.9 * inch, bottomMargin=0.9 * inch,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch,
        title=str(data.get("title") or "Document"),
    )
    flow = [Paragraph(_xml_escape(data.get("title") or "Document"), styles["Title"]),
            Spacer(1, 0.2 * inch)]
    for sec in data.get("sections") or []:
        heading = str(sec.get("heading") or "").strip()
        if heading:
            flow.append(Paragraph(_xml_escape(heading), styles["Heading2"]))
        flow.extend(_pdf_body(sec.get("body") or "", styles, ListFlowable, ListItem,
                              Paragraph, Spacer))
    doc.build(flow)


def _pdf_body(body, styles, ListFlowable, ListItem, Paragraph, Spacer):
    out, bullets = [], []

    def flush():
        if bullets:
            items = [ListItem(Paragraph(_xml_escape(b), styles["BodyText"])) for b in bullets]
            out.append(ListFlowable(items, bulletType="bullet", leftIndent=18))
            bullets.clear()

    for line in str(body).split("\n"):
        st = line.strip()
        if st[:2] in ("- ", "* "):
            bullets.append(st[2:].strip())
        elif st == "":
            flush()
            out.append(Spacer(1, 6))
        else:
            flush()
            out.append(Paragraph(_xml_escape(st), styles["BodyText"]))
    flush()
    return out


def _build_docx(data, path):
    import docx

    d = docx.Document()
    d.add_heading(str(data.get("title") or "Document"), level=0)
    for sec in data.get("sections") or []:
        heading = str(sec.get("heading") or "").strip()
        if heading:
            d.add_heading(heading, level=1)
        for line in str(sec.get("body") or "").split("\n"):
            st = line.strip()
            if st[:2] in ("- ", "* "):
                d.add_paragraph(st[2:].strip(), style="List Bullet")
            elif st:
                d.add_paragraph(st)
    d.save(path)


def _build_svg(data, path):
    svg = str(data.get("svg") or "").strip()
    if "<svg" not in svg.lower():
        raise ValueError("The 'svg' field must contain a complete <svg> element.")
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)


_BUILDERS = {
    "create_slide_deck": ("pptx", "slide deck", _build_pptx),
    "create_pdf_document": ("pdf", "PDF document", _build_pdf),
    "create_word_document": ("docx", "Word document", _build_docx),
    "create_svg_image": ("svg", "SVG image", _build_svg),
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _xml_escape(text):
    """Escape text for ReportLab's mini-markup so user content is shown literally."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _safe_filename(base, ext):
    """A safe, predictable file name with the correct extension."""
    name = secure_filename(str(base or "").strip())
    name = os.path.splitext(name)[0]  # drop any extension the model tacked on
    return f"{name or 'deliverable'}.{ext}"


def _unique(out_dir, filename):
    """Avoid clobbering an existing file by appending -2, -3, … if needed."""
    if not os.path.exists(os.path.join(out_dir, filename)):
        return filename
    stem, ext = os.path.splitext(filename)
    i = 2
    while os.path.exists(os.path.join(out_dir, f"{stem}-{i}{ext}")):
        i += 1
    return f"{stem}-{i}{ext}"
