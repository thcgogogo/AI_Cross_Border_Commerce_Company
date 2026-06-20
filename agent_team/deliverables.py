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

from . import crypto, workspace


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
    {
        "name": "create_excel_workbook",
        "description": (
            "Create a real Excel (.xlsx) workbook and save it as a downloadable file for "
            "the user. Use this whenever the user wants a spreadsheet, data table, data "
            "export, or any tabular results they can open in Excel / Google Sheets and keep "
            "working with. Provide one or more sheets, each with a header row and data rows. "
            "Put numeric cells in as real numbers (not strings) so totals and formulas work."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string",
                             "description": "Base file name, no extension, e.g. 'sales_q4'."},
                "title": {"type": "string",
                          "description": "Workbook title; also used for the file name when "
                                         "'filename' is omitted."},
                "sheets": {
                    "type": "array",
                    "description": "One or more worksheets, in tab order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Sheet / tab name."},
                            "headers": {"type": "array", "items": {"type": "string"},
                                        "description": "Column headers for the first row."},
                            "rows": {
                                "type": "array",
                                "description": "Data rows; each row is an array of cell values "
                                               "aligned to the headers. Use numbers (not strings) "
                                               "for numeric cells so Excel can sum them.",
                                "items": {
                                    "type": "array",
                                    "items": {"type": ["string", "number", "boolean", "null"]},
                                },
                            },
                        },
                        "required": ["name"],
                    },
                },
            },
            "required": ["title", "sheets"],
        },
    },
]

# name -> (file extension, human kind, builder function)
# (builder functions are defined below; bound at the bottom of the module.)
TOOL_NAMES = {spec["name"] for spec in TOOL_SPECS}


# ---------------------------------------------------------------------------
# LOCAL filesystem tools (Phase 11) — only for agents running on the desktop
# daemon. They read/list/write files on the USER'S OWN machine, and EVERY path
# passes the default-deny workspace chokepoint (workspace.assert_in_workspace)
# before any open. Unlike the builders above (which the model fills with content),
# these touch real local files the user has approved into the workspace.
# ---------------------------------------------------------------------------
LOCAL_TOOL_SPECS = [
    {
        "name": "read_file",
        "description": (
            "Read a local file the user has approved into the workspace and return its "
            "text. Handles xlsx/csv/docx/pdf/json/txt (the same parsing an uploaded "
            "document gets). Use this to inspect the user's own files before analyzing "
            "or transforming them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Path to a file inside the approved workspace."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_dir",
        "description": (
            "List the files in an approved local directory (one per line, with sizes). "
            "Use this to discover what files the user has in a folder before reading them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Path to a directory inside the approved workspace."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write a plain-text or Markdown file to an approved local path on the user's "
            "computer. Use this for notes, summaries, or text deliverables. For "
            "spreadsheets, slides, PDFs, Word docs or images, use the create_* tools "
            "instead — they produce real structured files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Destination path inside the approved workspace."},
                "content": {"type": "string",
                            "description": "The text/markdown content to write."},
            },
            "required": ["path", "content"],
        },
    },
    {
        # The IN-PLACE Excel editor — operates on an approved local path like the fs
        # tools (hence it lives here, routed through run_local_tool), distinct from
        # create_excel_workbook which emits a brand-new file into the job out_dir.
        "name": "drive_excel",
        "description": (
            "Edit an EXISTING Excel (.xlsx) workbook on the user's machine: set specific "
            "cell values or formulas, or append rows — without rebuilding the file. Use this "
            "when the user wants their OWN spreadsheet updated/annotated rather than a "
            "brand-new file. By DEFAULT the result is saved as a '<name>_edited.xlsx' copy "
            "next to the original (the original is left untouched); set overwrite=true only "
            "if the user explicitly asked to change the original in place. For a brand-new "
            "spreadsheet, use create_excel_workbook instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Absolute path of the existing .xlsx to edit."},
                "edits": {
                    "type": "array",
                    "description": "Targeted edits, applied in order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "sheet": {"type": "string",
                                      "description": "Sheet/tab name; defaults to the active sheet."},
                            "cell": {"type": "string",
                                     "description": "A1-style cell ref, e.g. 'B2' (with 'value' or 'formula')."},
                            "value": {"type": ["string", "number", "boolean", "null"],
                                      "description": "Value to set in 'cell' (numbers stay numeric)."},
                            "formula": {"type": "string",
                                        "description": "An Excel formula like '=SUM(B2:B10)' to set in 'cell'."},
                            "append_row": {"type": "array",
                                           "items": {"type": ["string", "number", "boolean", "null"]},
                                           "description": "Cell values appended as a new bottom row."},
                        },
                    },
                },
                "overwrite": {"type": "boolean",
                              "description": "If true, edit the original file in place instead of "
                                             "saving a *_edited.xlsx copy. Default false."},
            },
            "required": ["path", "edits"],
        },
    },
]
LOCAL_TOOL_NAMES = {spec["name"] for spec in LOCAL_TOOL_SPECS}


def local_specs():
    """The LOCAL filesystem tool specs (read_file/list_dir/write_file)."""
    return list(LOCAL_TOOL_SPECS)


def specs_for(names):
    """Return the tool-spec subset for `names`, from BOTH the deliverable builders
    and the LOCAL filesystem tools, preserving declaration order. (web_search is a
    capability flag handled by base_agent, not a spec here.)"""
    wanted = set(names or [])
    return [s for s in (TOOL_SPECS + LOCAL_TOOL_SPECS) if s["name"] in wanted]


# ---------------------------------------------------------------------------
# The executor the agent loop calls when Claude invokes a tool.
# ---------------------------------------------------------------------------
def run_tool(name, tool_input, out_dir, *, roots=None):
    """Build the requested file in `out_dir`. Never raises — returns a result dict:
        {ok: bool, message: str, kind, ext, title, filename, path}  (file fields on ok).

    Default-deny filesystem gate: roots=None → server mode, UNRESTRICTED (base_agent +
    existing tests). When a caller opts in with roots=[...], BOTH out_dir and the final
    path must resolve inside an approved root BEFORE any mkdir/open. A denial is a
    DISTINCT, security-flavored failure (denied=True) — not a soft, retry-able build
    error — so the runner can log/surface it differently."""
    spec = _BUILDERS.get(name)
    if not spec:
        return {"ok": False, "message": f"Unknown tool: {name!r}"}
    ext, kind, builder = spec
    tool_input = tool_input or {}

    if roots is not None and not workspace.is_in_workspace(out_dir, roots):
        return {"ok": False, "denied": True,
                "message": "Denied: the output directory is outside the approved workspace."}

    try:
        os.makedirs(out_dir, exist_ok=True)
        title = str(tool_input.get("title") or "").strip()
        base = tool_input.get("filename") or title or kind
        filename = _unique(out_dir, _safe_filename(base, ext))
        path = os.path.join(out_dir, filename)
        if roots is not None and not workspace.is_in_workspace(path, roots):
            return {"ok": False, "denied": True,
                    "message": f"Denied: '{filename}' is outside the approved workspace."}
        builder(tool_input, path)
        crypto.encrypt_file_in_place(path)  # at-rest; no-op when FERNET unset
    except Exception as e:  # surface the reason to the model so it can fix or move on
        return {"ok": False, "message": f"Could not create the {kind}: {type(e).__name__}: {e}"}

    return {"ok": True, "kind": kind, "ext": ext,
            "title": title or filename, "filename": filename, "path": path,
            "message": f"Created {kind} '{filename}'."}


def run_local_tool(name, tool_input, approved_roots):
    """Execute a LOCAL filesystem tool (read_file/list_dir/write_file) against the
    user's approved workspace. NEVER raises — returns the same result-dict contract
    as run_tool. EVERY path passes the default-deny workspace chokepoint
    (workspace.assert_in_workspace) BEFORE any open/read/write; a denied path comes
    back as a clear message (honesty rule: never a fake success)."""
    from . import workspace
    from .file_tools import parse_file

    tool_input = tool_input or {}
    path = str(tool_input.get("path") or "").strip()
    if not path:
        return {"ok": False, "message": "Missing required 'path'."}

    try:
        if name == "list_dir":
            real = workspace.assert_in_workspace(path, approved_roots, require_file=False)
            if not os.path.isdir(real):
                return {"ok": False, "message": f"Not a directory: {path}"}
            lines = []
            for entry in sorted(os.listdir(real)):
                full = os.path.join(real, entry)
                if os.path.isdir(full):
                    lines.append(f"{entry}/  (dir)")
                else:
                    lines.append(f"{entry}  ({os.path.getsize(full)} bytes)")
            body = "\n".join(lines) if lines else "(empty directory)"
            return {"ok": True, "kind": "listing",
                    "message": f"Files in {path}:\n{body}"}

        if name == "read_file":
            real = workspace.assert_in_workspace(path, approved_roots)  # require_file=True
            doc = parse_file(real)
            return {"ok": True, "kind": "file", "title": doc["name"],
                    "message": (f"Contents of {doc['name']} "
                                f"({doc['chars']:,} chars):\n\n{doc['text']}")}

        if name == "write_file":
            content = tool_input.get("content")
            if content is None:
                return {"ok": False, "message": "Missing required 'content'."}
            real = workspace.assert_in_workspace(
                path, approved_roots, require_file=False, require_exists=False)
            os.makedirs(os.path.dirname(real) or ".", exist_ok=True)
            with open(real, "w", encoding="utf-8") as f:
                f.write(str(content))
            crypto.encrypt_file_in_place(real)   # at-rest; no-op when FERNET unset
            fn = os.path.basename(real)
            return {"ok": True, "kind": "text file", "title": fn, "filename": fn,
                    "path": real, "ext": os.path.splitext(fn)[1].lstrip(".") or "txt",
                    "message": f"Wrote {fn} ({len(str(content)):,} chars)."}

        if name == "drive_excel":
            edits = tool_input.get("edits")
            if not isinstance(edits, list) or not edits:
                return {"ok": False, "message": "Missing 'edits' (a non-empty list)."}
            src = workspace.assert_in_workspace(path, approved_roots)   # must exist (require_file=True)
            overwrite = bool(tool_input.get("overwrite"))
            # Write-approval (locked 2026-06-15): default to a *_edited.xlsx COPY next to
            # the original, leaving the user's file untouched; overwrite is opt-in only.
            if overwrite:
                dest = src
            else:
                stem, ext = os.path.splitext(src)
                dest = workspace.assert_in_workspace(
                    f"{stem}_edited{ext or '.xlsx'}", approved_roots,
                    require_file=False, require_exists=False)
            _drive_xlsx(src, dest, edits)
            # NOTE: user-directory writes are NOT encrypted — the user opens them
            # directly in Excel. (The job/out_dir create_* family stays encrypted; do
            # NOT port the Fernet-at-rest invariant here or it corrupts their files.)
            fn = os.path.basename(dest)
            return {"ok": True, "kind": "Excel edit", "ext": "xlsx", "title": fn,
                    "filename": fn, "path": dest,
                    "message": (f"Edited workbook in place: {fn}." if overwrite
                                else f"Saved edited workbook as {fn} (original untouched).")}

        return {"ok": False, "message": f"Unknown local tool: {name!r}"}
    except workspace.WorkspaceError as e:
        # Default-deny: the path is outside the approved workspace (or unsafe).
        return {"ok": False,
                "message": f"Path is outside the approved workspace and was denied ({e})."}
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}


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


def _build_xlsx(data, path):
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    wb.remove(wb.active)  # drop the default empty sheet; we add our own below

    sheets = data.get("sheets") or []
    if not sheets:  # an empty workbook is invalid — always leave one usable sheet
        sheets = [{"name": data.get("title") or "Sheet1"}]

    used = set()
    for i, sheet in enumerate(sheets):
        ws = wb.create_sheet(title=_safe_sheet_title(sheet.get("name") or f"Sheet{i + 1}", used))
        headers = [str(h) for h in (sheet.get("headers") or [])]
        if headers:
            ws.append(headers)
            for cell in ws[1]:  # make the header row stand out
                cell.font = Font(bold=True)
        for row in (sheet.get("rows") or []):
            ws.append([_xlsx_cell(v) for v in (row or [])])

    wb.save(path)


def build_settlement_ledger(result, out_path):
    """Build the audit-ready 对账 ledger .xlsx from a settlement_engine result dict —
    DETERMINISTICALLY (the engine's numbers go straight in via _xlsx_cell, never
    re-typed by an LLM). Sheets: 对账汇总 / 差异归因 / 异常清单 / 数据来源与核验.
    Writes an UNENCRYPTED workbook (the caller serves it as an ephemeral download)."""
    result = result or {}
    summary = result.get("summary") or {}
    order = ["应收", "实收", "到账", "在途金额", "总手续费", "总佣金", "总退款",
             "未解释差异", "未解释笔数"]
    summary_rows = [[k, summary.get(k)] for k in order if k in summary]

    attr_rows = [[a.get("cause"), a.get("amount"), a.get("count")]
                 for a in (result.get("attributions") or [])]

    exc_rows = [[e.get("layer"), e.get("bucket"), e.get("cause"), e.get("amount"),
                 e.get("platform_ref"), e.get("bank_ref"), e.get("ledger_ref"),
                 e.get("status"), e.get("detail")]
                for e in (result.get("exceptions") or [])]

    src_rows = [[s.get("anchor"), s.get("claim_cn"), s.get("source_file"),
                 s.get("source_detail"), s.get("computed_by")]
                for s in (result.get("source_rows") or [])]

    data = {"sheets": [
        {"name": "对账汇总", "headers": ["项目", "金额"], "rows": summary_rows},
        {"name": "差异归因", "headers": ["差异类型", "金额", "笔数"], "rows": attr_rows},
        {"name": "异常清单",
         "headers": ["层级", "类型", "归因", "金额", "平台单号", "银行流水",
                     "账本单号", "状态", "说明"], "rows": exc_rows},
        {"name": "数据来源与核验",
         "headers": ["锚点", "数据 / 结论", "来源文件", "定位", "核验方式"],
         "rows": src_rows},
    ]}
    _build_xlsx(data, out_path)
    return out_path


def build_ar_aging_ledger(result, out_path):
    """Build the audit-ready 应收账款 ledger .xlsx from an ar_aging_engine result dict —
    DETERMINISTICALLY (engine numbers go straight in, never re-typed by an LLM). Sheets:
    账龄汇总 / 客户应收 / 逾期发票清单 / 回款核销 / 未核销回款 / 异常清单(确定·疑似) /
    数据来源与核验. Writes an UNENCRYPTED workbook (served as an ephemeral download)."""
    result = result or {}
    summary = result.get("summary") or {}
    order = ["应收账款总额", "逾期总额", "current", "0-30", "31-60", "61-90", "90+",
             "DSO", "未核销超额回款", "未结发票数", "逾期发票数", "疑似条目数"]
    summary_rows = [[k, summary.get(k)] for k in order if k in summary]

    cust_rows = [[c.get("customer_id"), c.get("customer_name"), c.get("outstanding"),
                  c.get("credit"), c.get("grade"),
                  "，".join(c.get("suspect_reasons") or [])]
                 for c in (result.get("customers") or [])]

    inv_rows = [[i.get("invoice_no"), i.get("customer_name"), i.get("due_date"),
                 i.get("dpd"), i.get("bucket"), i.get("open_balance"), i.get("grade"),
                 "，".join(i.get("suspect_reasons") or [])]
                for i in (result.get("invoices") or []) if i.get("overdue")]

    app_rows = [[a.get("payment_no"), a.get("invoice_no"), a.get("customer_id"),
                 a.get("amount"), a.get("applied_via"), a.get("pay_date")]
                for a in (result.get("applications") or [])]

    unapplied_rows = [[u.get("payment_no"), u.get("customer_id"), u.get("amount"),
                       u.get("kind"), u.get("invoice_ref")]
                      for u in (result.get("unapplied") or [])]

    flag_rows = [[e.get("layer"), e.get("bucket"), e.get("grade"), e.get("cause"),
                  e.get("amount"), e.get("customer_ref"), e.get("invoice_ref"),
                  e.get("status"), e.get("detail")]
                 for e in (result.get("exceptions") or [])]

    src_rows = [[s.get("anchor"), s.get("claim_cn"), s.get("source_file"),
                 s.get("source_detail"), s.get("computed_by")]
                for s in (result.get("source_rows") or [])]

    data = {"sheets": [
        {"name": "账龄汇总", "headers": ["项目", "金额 / 天数 / 数量"], "rows": summary_rows},
        {"name": "客户应收",
         "headers": ["客户编号", "客户名称", "未结金额", "账户余款", "分级", "疑似原因"],
         "rows": cust_rows},
        {"name": "逾期发票清单",
         "headers": ["发票号", "客户", "到期日", "逾期天数", "账龄段", "未结金额",
                     "分级", "疑似原因"], "rows": inv_rows},
        {"name": "回款核销",
         "headers": ["回款流水", "核销发票", "客户", "金额", "核销方式", "收款日期"],
         "rows": app_rows},
        {"name": "未核销回款",
         "headers": ["回款流水", "客户", "金额", "类型", "原对应发票"],
         "rows": unapplied_rows},
        {"name": "异常清单",
         "headers": ["层级", "账龄段", "分级", "归因", "金额", "客户", "发票",
                     "状态", "说明"], "rows": flag_rows},
        {"name": "数据来源与核验",
         "headers": ["锚点", "数据 / 结论", "来源文件", "定位", "核验方式"],
         "rows": src_rows},
    ]}
    _build_xlsx(data, out_path)
    return out_path


def _drive_xlsx(src_path, dest_path, edits):
    """IN-PLACE Excel editor: load an existing .xlsx, apply targeted edits, save to
    dest_path (a *_edited.xlsx copy by default; the same path when overwriting).
    Each edit names an optional sheet (default = active; created if missing) and ONE
    of: cell+value, cell+formula ('=…' stored as a real formula), or append_row.
    Reuses _xlsx_cell so numbers stay numeric. Raises on a missing/invalid workbook —
    run_local_tool's caller turns that into an {ok: False} result."""
    from openpyxl import load_workbook

    wb = load_workbook(src_path)
    for edit in edits or []:
        if not isinstance(edit, dict):
            continue
        sheet = edit.get("sheet")
        if sheet and sheet in wb.sheetnames:
            ws = wb[sheet]
        elif sheet:
            ws = wb.create_sheet(title=str(sheet))
        else:
            ws = wb.active
        if isinstance(edit.get("append_row"), list):
            ws.append([_xlsx_cell(v) for v in edit["append_row"]])
        elif edit.get("cell"):
            cell = str(edit["cell"])
            if edit.get("formula") is not None:
                formula = str(edit["formula"])
                ws[cell] = formula if formula.startswith("=") else "=" + formula
            else:
                ws[cell] = _xlsx_cell(edit.get("value"))
    wb.save(dest_path)


# Excel forbids these characters in a sheet/tab name, caps it at 31 chars, and
# requires names to be unique within a workbook — sanitize so a model-supplied
# name can never make openpyxl raise.
_XLSX_BAD_TITLE = {ord(c): "_" for c in "[]:*?/\\"}


def _safe_sheet_title(name, used):
    title = (str(name or "").translate(_XLSX_BAD_TITLE).strip() or "Sheet")[:31]
    base, n = title, 2
    while title.lower() in used:
        suffix = f"-{n}"
        title = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(title.lower())
    return title


def _xlsx_cell(value):
    """Keep numbers/bools numeric so Excel can sum them; stringify anything else
    (dicts/lists/etc.) so openpyxl never rejects a cell value."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


_BUILDERS = {
    "create_slide_deck": ("pptx", "slide deck", _build_pptx),
    "create_pdf_document": ("pdf", "PDF document", _build_pdf),
    "create_word_document": ("docx", "Word document", _build_docx),
    "create_svg_image": ("svg", "SVG image", _build_svg),
    "create_excel_workbook": ("xlsx", "Excel workbook", _build_xlsx),
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
