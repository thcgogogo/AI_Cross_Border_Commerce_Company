"""
helpdocs.py — the /help pages + CHANGELOG plumbing (Phase 10).

Three small jobs:

  render_markdown(md)       a tiny, dependency-free markdown→HTML renderer for
                            content WE ship (docs/help/*.md, CHANGELOG.md).
                            Escape-first: every line is HTML-escaped BEFORE any
                            markup is applied, so even a hostile doc can't
                            inject script. Supports the subset our docs use:
                            #/##/### headings, - bullets, 1. numbered lists,
                            **bold**, `code`, [text](http/https/mailto//-links),
                            > quotes, --- rules, paragraphs.
  list/load help docs       the registry of docs/help/<slug>.md pages (快速上手,
                            常见问题, 数据安全说明, 发票说明). Files are re-read
                            per request — content stays hot-editable like the
                            industry packs.
  changelog entries         CHANGELOG.md parsed into entries (one per '## '
                            heading). The newest entry's heading is the id the
                            新功能 badge compares against each account's
                            changelog_seen row.
"""

import os
import re
from html import escape as _h

from . import metrics_store

HELP_DIR = os.path.join(metrics_store.PROJECT_ROOT, "docs", "help")
CHANGELOG_PATH = os.path.join(metrics_store.PROJECT_ROOT, "CHANGELOG.md")

# slug -> 页面标题. The order here is the order on the /help index + nav.
HELP_DOCS = (
    ("quickstart", "快速上手"),
    ("faq", "常见问题"),
    ("data-security", "数据安全说明"),
    ("fapiao", "发票说明"),
)
_SLUGS = {slug for slug, _ in HELP_DOCS}


def list_help_docs():
    """[{slug, title}] for the /help index and the page nav."""
    return [{"slug": slug, "title": title} for slug, title in HELP_DOCS]


def help_doc_title(slug):
    for s, title in HELP_DOCS:
        if s == slug:
            return title
    return None


def load_help_doc(slug):
    """The markdown body of one registered help doc, or None for an unknown slug
    / missing file. Only registry slugs are ever read — the slug is never used
    as a raw path component beyond this allow-list."""
    if slug not in _SLUGS:
        return None
    path = os.path.join(HELP_DIR, "%s.md" % slug)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Markdown rendering (escape-first; the subset our own docs use)
# ---------------------------------------------------------------------------
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_CODE_RE = re.compile(r"`([^`]+)`")
_SAFE_HREF = re.compile(r"^(https?://|mailto:|/|#)", re.IGNORECASE)


def _inline(escaped):
    """Inline markup over an ALREADY-ESCAPED line: `code`, **bold**, [t](url)."""
    out = _CODE_RE.sub(r"<code>\1</code>", escaped)
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)

    def _link(m):
        label, href = m.group(1), m.group(2)
        if not _SAFE_HREF.match(href):
            return m.group(0)              # unknown scheme — leave it as text
        return '<a href="%s">%s</a>' % (href, label)

    return _LINK_RE.sub(_link, out)


def render_markdown(md):
    """Render trusted-ish markdown to HTML. Every line is escaped before any
    tag is added, so the output can be embedded directly in our pages."""
    html, para, list_tag = [], [], None

    def close_list():
        nonlocal list_tag
        if list_tag:
            html.append("</%s>" % list_tag)
            list_tag = None

    def flush_para():
        if para:
            html.append("<p>" + "<br>".join(para) + "</p>")
            para.clear()

    for raw in (md or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_para()
            close_list()
            continue
        esc = _inline(_h(stripped))
        m_head = re.match(r"^(#{1,3})\s+", stripped)
        m_num = re.match(r"^\d+\.\s+", stripped)
        if m_head:
            flush_para()
            close_list()
            level = len(m_head.group(1))
            html.append("<h%d>%s</h%d>"
                        % (level, _inline(_h(stripped[m_head.end():])), level))
        elif stripped in ("---", "***"):
            flush_para()
            close_list()
            html.append("<hr>")
        elif stripped.startswith(("- ", "* ")):
            flush_para()
            if list_tag != "ul":
                close_list()
                html.append("<ul>")
                list_tag = "ul"
            html.append("<li>%s</li>" % _inline(_h(stripped[2:])))
        elif m_num:
            flush_para()
            if list_tag != "ol":
                close_list()
                html.append("<ol>")
                list_tag = "ol"
            html.append("<li>%s</li>" % _inline(_h(stripped[m_num.end():])))
        elif stripped.startswith("> "):
            flush_para()
            close_list()
            html.append("<blockquote>%s</blockquote>" % _inline(_h(stripped[2:])))
        else:
            para.append(esc)
    flush_para()
    close_list()
    return "\n".join(html)


# ---------------------------------------------------------------------------
# Changelog
# ---------------------------------------------------------------------------
def load_changelog():
    """The raw CHANGELOG.md text ("" when the file is missing)."""
    try:
        with open(CHANGELOG_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def changelog_entries(md=None):
    """The changelog's entries, newest first: [{"id", "title"}] — one per '## '
    heading, in file order. `id` is the full heading text (unique per entry; the
    新功能 badge stores/compares it verbatim)."""
    md = load_changelog() if md is None else md
    out = []
    for line in md.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
            if heading:
                out.append({"id": heading[:64], "title": heading})
    return out


def latest_changelog_id(md=None):
    """The newest entry's id, or None when the changelog is empty."""
    entries = changelog_entries(md)
    return entries[0]["id"] if entries else None
