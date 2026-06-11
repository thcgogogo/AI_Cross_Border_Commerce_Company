"""
verification.py — The "we don't hallucinate numbers" layer (Phase 6, Moat 3).

Two best-effort, never-fatal steps that run AFTER the CEO assembles the final
report:

  1. split_report_and_sources(text)
        The Report Writer is contracted (see config.py) to put an inline 【N】
        anchor after every number and a machine block at the very end:

            SOURCES_JSON: [{"anchor": 1, "claim_cn": "...", "source_file": "...",
                            "source_detail": "...", "computed_by": "data_analyst"}]

        This pulls that block off the end, returns the clean report body (the
        body the user sees — WITHOUT the machine line), the parsed source rows,
        and a status: "ok" | "malformed" | "missing". A parse failure NEVER
        raises — the caller still ships the report and the UI shows a small
        "本次未能生成数据溯源表" banner.

        Because the block is contracted to be the LAST thing in the output, an
        output-token cut destroys it first (and silently). recover_sources() is
        the salvage path the orchestrator tries when the strict parse fails: it
        keeps the complete rows the writer actually wrote and drops the
        half-written one — rows are recovered verbatim, never reconstructed.

  2. audit_report(report, known_numbers, llm, ...)
        One cheap-model (Haiku) pass: given the report body and the set of
        numbers the system actually KNOWS (every metric in the company brain
        plus the pre-computed trend/anomaly figures), it lists any number in the
        report that is NOT backed by that set. build_audit_warning() turns a
        non-empty list into a "请人工确认" section the orchestrator PREPENDS to
        the report — we flag suspicious numbers, we never silently delete them.

Everything here is best-effort: any exception degrades to "no sources" / "no
warning" so a verification hiccup can never fail or block a run.
"""

import json
import re

from . import config
from .token_manager import BudgetExceededError

# The exact prefix the Report Writer must emit before its machine-readable JSON.
SOURCES_MARKER = "SOURCES_JSON:"

# Inline source anchors look like 【1】 (fullwidth brackets + integer).
_ANCHOR_RE = re.compile(r"【\s*\d+\s*】")

# The cheap auditor's job, stated tightly so a small model stays on-task and
# returns ONLY a JSON array (we parse it back out and ignore any prose).
_AUDITOR_SYSTEM = (
    "你是一个严格的数字核验助手。你会收到一份中文经营报告，以及一个『系统已核验数字』"
    "清单（来自公司指标库与确定性趋势引擎，这些数字是可信的）。你的唯一任务：找出报告"
    "正文中出现、但无法在已核验清单中找到对应来源的数字（金额、百分比、计数、环比/同比等）。"
    "忽略页码、序号、日期、章节编号这类非业务数字。"
    "只输出一个 JSON 数组，元素是这些『未核验数字』的字符串原文，例如 "
    '["1,234,567", "3.2%"]。如果每个数字都能在清单中找到，就输出空数组 []。'
    "不要输出任何解释、不要使用代码块，只输出 JSON 数组本身。"
)

# Headers for the prepended warning section (the verbatim phrase the spec/tests
# look for is "请人工确认").
_AUDIT_WARNING_TITLE = "## ⚠️ 数据核验提示"
_AUDIT_WARNING_LEAD = "以下数字未能自动核验，请人工确认："


# ---------------------------------------------------------------------------
# 1. Split the machine SOURCES_JSON block off the end of the report
# ---------------------------------------------------------------------------
def split_report_and_sources(text):
    """Return (body, sources, status).

    - body:    the report WITHOUT the trailing SOURCES_JSON line (always a str).
    - sources: a list of normalized source dicts, or None when none were parsed.
    - status:  "ok"        SOURCES_JSON present and parsed into >=1 row,
               "malformed" SOURCES_JSON present but unparseable / empty,
               "missing"   no SOURCES_JSON block at all.

    Never raises — a malformed block degrades to ("<body>", None, "malformed").
    """
    if not text or not str(text).strip():
        return "", None, "missing"
    text = str(text)

    # The machine block is the LAST thing in the output; rfind tolerates a
    # number like 【1】 appearing earlier or the literal string in prose.
    idx = text.rfind(SOURCES_MARKER)
    if idx == -1:
        return text.strip(), None, "missing"

    body = text[:idx].rstrip()
    tail = text[idx + len(SOURCES_MARKER):]
    parsed = _extract_json_array(tail)
    if not isinstance(parsed, list):
        return body, None, "malformed"

    sources = _normalize_sources(parsed)
    if not sources:
        return body, None, "malformed"
    return body, sources, "ok"


def _normalize_sources(rows):
    """Coerce the raw JSON rows into tidy {anchor, claim_cn, source_file,
    source_detail, computed_by} dicts, dropping anything that isn't an object."""
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        anchor = r.get("anchor")
        try:
            anchor = int(anchor)
        except (TypeError, ValueError):
            anchor = None
        computed_by = (str(r.get("computed_by") or "").strip().lower()
                       or "data_analyst")
        if computed_by not in ("data_analyst", "trend_engine"):
            computed_by = "data_analyst"
        out.append({
            "anchor": anchor,
            "claim_cn": str(r.get("claim_cn") or "").strip(),
            "source_file": str(r.get("source_file") or "").strip(),
            "source_detail": str(r.get("source_detail") or "").strip(),
            "computed_by": computed_by,
        })
    # Stable order by anchor when present, else preserve input order.
    out.sort(key=lambda s: (s["anchor"] is None, s["anchor"] if s["anchor"] is not None else 0))
    return out


def report_has_anchors(text):
    """True if the report body still carries inline 【N】 source anchors. Lets the
    UI/PDF tell a real (but unparseable) report from a non-report deliverable: we
    only show the "本次未能生成数据溯源表" banner when anchors are present."""
    return bool(text) and bool(_ANCHOR_RE.search(str(text)))


def count_anchors(text):
    """Number of DISTINCT inline 【N】 anchors in the text, so a salvage log can
    say how many anchors the body carries vs. how many source rows survived."""
    if not text:
        return 0
    return len({int(re.sub(r"\D", "", m)) for m in _ANCHOR_RE.findall(str(text))})


def recover_sources(text):
    """Salvage whatever COMPLETE source rows follow the (last) SOURCES_JSON
    marker, tolerating an array whose tail was cut off mid-row by an
    output-token limit. A half-written row is dropped, never guessed at, so
    every surviving row is one the writer actually authored (the trail is a
    trust feature). Returns normalized rows (>=1) or None. Never raises."""
    if not text:
        return None
    s = str(text)
    idx = s.rfind(SOURCES_MARKER)
    if idx == -1:
        return None
    tail = s[idx + len(SOURCES_MARKER):]
    parsed = _extract_json_array(tail)
    if not isinstance(parsed, list):
        parsed = _repair_truncated_array(tail)
    if not isinstance(parsed, list):
        return None
    return _normalize_sources(parsed) or None


# ---------------------------------------------------------------------------
# 2. Cheap-model auditor — flag numbers the system can't back up
# ---------------------------------------------------------------------------
def audit_report(report_text, known_numbers, llm, model=None, budget=None):
    """Ask a cheap model which numbers in `report_text` are NOT in the verified
    `known_numbers` set. Returns a list of unverified number strings (possibly
    empty). Best-effort: ANY failure (including a budget cut-off) returns [] so
    the auditor can never fail, block, or truncate a run."""
    if not (report_text and str(report_text).strip() and llm):
        return []
    model = model or config.HAIKU
    budget = budget or config.AGENT_TEAM_BUDGET

    known_lines = "\n".join(f"- {k}" for k in _known_list(known_numbers)) or "（无）"
    user = (
        "系统已核验数字清单：\n" + known_lines +
        "\n\n报告正文：\n" + str(report_text)
    )
    try:
        raw = llm.complete(
            system_prompt=_AUDITOR_SYSTEM,
            user_content=user,
            model=model,
            effort="low",
            max_tokens=600,
            budget=budget,
        )
    except BudgetExceededError:
        # The report is already assembled & saved; skip the optional audit
        # rather than letting a budget error unwind the whole run.
        return []
    except Exception:
        return []

    arr = _extract_json_array(raw)
    if not isinstance(arr, list):
        return []
    seen, out = set(), []
    for x in arr:
        s = str(x).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def build_audit_warning(unverified):
    """Render the prepended warning section for a non-empty `unverified` list, or
    "" when everything checked out. The caller PREPENDS this to the report body —
    we flag, never delete."""
    nums = [str(x).strip() for x in (unverified or []) if str(x).strip()]
    if not nums:
        return ""
    bullets = "\n".join(f"- {n}" for n in nums)
    return f"{_AUDIT_WARNING_TITLE}\n\n{_AUDIT_WARNING_LEAD}\n{bullets}\n\n"


def _known_list(known_numbers):
    """Normalize the known-numbers input (set/list of numbers or strings) into a
    de-duplicated list of clean strings for the prompt."""
    out, seen = [], set()
    for k in (known_numbers or []):
        s = str(k).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Tolerant JSON-array extraction (shares the spirit of orchestrator._extract_json)
# ---------------------------------------------------------------------------
def _extract_json_array(text):
    """Pull the first JSON array out of a model response, tolerating code fences
    and surrounding prose. Returns a list, or None if nothing parses."""
    if not text:
        return None
    s = str(text)
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", s, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start, end = s.find("["), s.rfind("]")
        if start != -1 and end != -1 and end > start:
            candidate = s[start:end + 1]
    if not candidate:
        return None
    try:
        val = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    return val if isinstance(val, list) else None


def _repair_truncated_array(text):
    """Parse a JSON array whose closing bracket (and likely its final element)
    was cut off mid-stream: close the array right after a complete object,
    trying from the LAST '}' backward, and return the first variant that
    parses. Returns a list, or None when no complete element survives."""
    if not text:
        return None
    s = str(text)
    start = s.find("[")
    if start == -1:
        return None
    frag = s[start:]
    for end in reversed([m.end() for m in re.finditer(r"\}", frag)]):
        try:
            val = json.loads(frag[:end] + "]")
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(val, list):
            return val
    return None
