"""
file_tools.py — Turn uploaded files into text the agents can read.

Supports PDF, Word (.docx), Excel (.xlsx), CSV, and plain text/markdown/JSON.
Each parser is wrapped in its own try/except, so one unreadable file never
crashes the whole run — it just comes back with a clear note explaining why.

We never silently throw away content: if a file is enormous, we cut it at a
generous limit and add a VISIBLE "[... truncated ...]" notice so the team
(and you) know there was more.
"""

import json
import os

from . import workspace

# Cap any single document so one huge file can't blow up the context window.
# If we cut, we always say so out loud (see _truncate).
MAX_CHARS_PER_DOC = 60_000
MAX_PREVIEW_ROWS = 30


def parse_file(path, *, roots=None):
    """Parse one file into {name, ext, chars, text, meta}.

    Default-deny filesystem gate: roots=None → server mode, UNRESTRICTED (every existing
    caller — server uploads are already written into a server-controlled dir). When a
    caller opts in with roots=[...] (the local daemon's path-aware run), the path MUST
    resolve inside an approved root BEFORE we open it. The WorkspaceViolation is raised
    HERE, outside the reader try/except below, so it propagates to the caller and never
    degrades into a soft "[Could not read…]" string."""
    if roots is not None and not workspace.is_in_workspace(path, roots):
        raise workspace.WorkspaceViolation(f"Denied: '{path}' is outside the approved workspace.")
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    try:
        if ext == "pdf":
            text, meta = _read_pdf(path)
        elif ext == "docx":
            text, meta = _read_docx(path)
        elif ext in ("xlsx", "xls"):
            text, meta = _read_spreadsheet(path, kind="excel")
        elif ext == "csv":
            text, meta = _read_spreadsheet(path, kind="csv")
        elif ext == "json":
            text, meta = _read_json(path)
        elif ext in ("txt", "md", "markdown", "log", "text"):
            text, meta = _read_text(path)
        else:
            # Unknown extension — try to read it as plain text anyway.
            text, meta = _read_text(path)
            meta["note"] = f"Unrecognized .{ext} file; read as plain text."
    except Exception as e:  # never let one bad file kill the batch
        text = f"[Could not read this file: {type(e).__name__}: {e}]"
        meta = {"error": str(e)}

    text = _truncate(text)
    return {"name": name, "ext": ext, "chars": len(text), "text": text, "meta": meta}


# ---------------------------------------------------------------------------
# Per-format readers
# ---------------------------------------------------------------------------
def _read_pdf(path):
    from pypdf import PdfReader

    reader = PdfReader(path)
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        content = page.extract_text() or ""
        pages.append(f"--- Page {i} ---\n{content}")
    text = "\n\n".join(pages).strip() or "[This PDF has no extractable text — it may be a scan/image.]"
    return text, {"pages": len(reader.pages)}


def _read_docx(path):
    import docx

    doc = docx.Document(path)
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    # Also pull text out of any tables.
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                paragraphs.append(" | ".join(cells))
    text = "\n".join(paragraphs).strip() or "[This Word document appears to be empty.]"
    return text, {"paragraphs": len(paragraphs)}


def _read_spreadsheet(path, kind):
    import pandas as pd

    if kind == "excel":
        sheets = pd.read_excel(path, sheet_name=None)  # dict of {name: DataFrame}
    else:
        sheets = {"data": pd.read_csv(path)}

    blocks = []
    meta = {"sheets": {}}
    for sheet_name, df in sheets.items():
        rows, cols = df.shape
        meta["sheets"][sheet_name] = {"rows": int(rows), "cols": int(cols)}
        block = [f"### Sheet: {sheet_name}  ({rows} rows x {cols} columns)"]
        block.append("Columns: " + ", ".join(str(c) for c in df.columns))

        # Quick numeric summary — gives the Data Analyst real material.
        numeric = df.select_dtypes("number")
        if not numeric.empty:
            block.append("\nNumeric summary:")
            block.append(numeric.describe().round(3).to_string())

        # A sample of the actual rows.
        preview = df.head(MAX_PREVIEW_ROWS)
        block.append(f"\nFirst {len(preview)} rows:")
        block.append(preview.to_csv(index=False).strip())
        if rows > MAX_PREVIEW_ROWS:
            block.append(f"[... {rows - MAX_PREVIEW_ROWS} more rows not shown in preview ...]")

        blocks.append("\n".join(block))

    return "\n\n".join(blocks), meta


def _read_json(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        data = json.load(f)
    text = json.dumps(data, indent=2, ensure_ascii=False)
    return text, {"top_level_type": type(data).__name__}


def _read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    return text, {"lines": text.count("\n") + 1}


# ---------------------------------------------------------------------------
def _truncate(text):
    if len(text) <= MAX_CHARS_PER_DOC:
        return text
    kept = text[:MAX_CHARS_PER_DOC]
    dropped = len(text) - MAX_CHARS_PER_DOC
    return kept + f"\n\n[... truncated {dropped:,} of {len(text):,} characters to fit context ...]"
