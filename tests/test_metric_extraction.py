"""
test_metric_extraction.py — regression tests for the Phase-6 metric-extraction fix.

Run from the project root:

    .venv/bin/python -m pytest tests/test_metric_extraction.py -v

Background (runs f8036be5c3a6 et al., diagnosed 2026-06-11): every real run logged
"metric extraction produced no parseable JSON — skipping (0 metrics)" and the
metrics table stayed EMPTY across all four production runs — Moat 1 (与历史对比)
never fired. Root cause, reproduced against the real Haiku model: the reply is
(a) wrapped in a ```json fence and (b) cut at max_tokens=4000 mid-row, so the
fence never closes and the array never gets its ']' — both the closed-fence
regex and the find('[')..rfind(']') fallback returned None, silently.

The fix under test (metrics_store.py), mirroring the Phase-3 SOURCES_JSON
salvage in verification.py:
  - tolerant parse: closed fence, UNCLOSED fence, prose around the array;
  - truncation repair: close the array after the last COMPLETE row (a
    half-written row is dropped, never guessed at);
  - raised output ceiling (METRIC_EXTRACTION_MAX_TOKENS, was 4000);
  - one retry on total failure;
  - a LOUD warning line when extraction still yields 0 metrics — silence is
    what hid this for five phases.

No test hits the network or spends money — the LLM is a local fake and the DB
is a per-test tmp SQLite file.
"""

import json
import os
import sys

import pytest

# Make the project root importable no matter where pytest is launched from.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_team import db, metrics_store  # noqa: E402
from agent_team.models import Metric  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path):
    """Point the company brain at a throwaway SQLite file for this one test."""
    db.init_db(str(tmp_path / "brain.db"))
    yield


class _FakeLLM:
    """Network-free ModelRouter stand-in: returns canned responses in order and
    exposes last_stop_reason exactly like the real router."""

    def __init__(self, responses, stop_reasons=None):
        self.responses = list(responses)
        self.stop_reasons = list(stop_reasons or [])
        self.calls = []
        self.last_stop_reason = None

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        self.last_stop_reason = self.stop_reasons.pop(0) if self.stop_reasons else None
        return self.responses.pop(0)


_ROWS = [
    {"metric_name": "sales_revenue", "metric_name_cn": "销售额", "value": 164100,
     "unit": "USD", "period_label": "2026-W23", "dimension": None,
     "source_file": None, "source_detail": "两渠道求和"},
    {"metric_name": "order_count", "metric_name_cn": "订单数", "value": 764,
     "unit": "单", "period_label": "2026-W23", "dimension": None,
     "source_file": None, "source_detail": None},
    {"metric_name": "refund_rate", "metric_name_cn": "退款率", "value": 3.3,
     "unit": "%", "period_label": "2026-W23", "dimension": None,
     "source_file": None, "source_detail": None},
]

_ANALYSIS = "## 本周指标\n销售额 164,100 USD，订单 764 单，退款率 3.30%。"


def _company_and_run(email="extract@corp.com"):
    cid = metrics_store.get_or_create_company(email)
    rid = metrics_store.create_run(cid, "extraction test")
    return cid, rid


def _stored_names(cid):
    with db.session_scope() as s:
        rows = s.query(Metric).filter(Metric.company_id == cid).order_by(Metric.id).all()
        return [r.metric_name for r in rows]


def _truncated_fenced_reply():
    """The exact production shape: an OPEN ```json fence, then the array cut
    mid-row-3 by the output-token ceiling — no closing ']' and no closing fence."""
    pretty = json.dumps(_ROWS, ensure_ascii=False, indent=2)
    cut_at = pretty.rfind('"metric_name": "refund_rate"')
    assert cut_at != -1
    return "```json\n" + pretty[:cut_at + 20]


# ---------------------------------------------------------------------------
# _parse_metrics_json unit behavior
# ---------------------------------------------------------------------------
def test_parse_closed_fence():
    raw = "```json\n" + json.dumps(_ROWS, ensure_ascii=False) + "\n```"
    assert len(metrics_store._parse_metrics_json(raw)) == 3


def test_parse_prose_around_bare_array():
    raw = "好的，以下是提取结果：\n" + json.dumps(_ROWS, ensure_ascii=False) + "\n希望对你有帮助。"
    assert len(metrics_store._parse_metrics_json(raw)) == 3


def test_parse_unclosed_fence_truncated_mid_row_repairs():
    rows = metrics_store._parse_metrics_json(_truncated_fenced_reply())
    # Rows 1-2 are complete and survive verbatim; half-written row 3 is dropped.
    assert [r["metric_name"] for r in rows] == ["sales_revenue", "order_count"]
    assert rows[0]["value"] == 164100


def test_parse_pure_prose_and_junk_returns_none():
    assert metrics_store._parse_metrics_json("本周销售表现良好，无法提取。") is None
    assert metrics_store._parse_metrics_json("[销售额] 很高") is None
    assert metrics_store._parse_metrics_json("") is None
    assert metrics_store._parse_metrics_json(None) is None
    # An empty array parses to [] — falsy, so the caller still retries + warns.
    assert not metrics_store._parse_metrics_json("[]")


# ---------------------------------------------------------------------------
# extract_metrics_to_db end-to-end against the fake router
# ---------------------------------------------------------------------------
def test_fenced_reply_is_parsed_and_stored(fresh_db):
    cid, rid = _company_and_run()
    llm = _FakeLLM(["```json\n" + json.dumps(_ROWS, ensure_ascii=False) + "\n```"])

    n = metrics_store.extract_metrics_to_db(_ANALYSIS, cid, rid, llm=llm)

    assert n == 3
    assert _stored_names(cid) == ["sales_revenue", "order_count", "refund_rate"]
    # The raised ceiling actually reaches the call (4000 clipped real runs).
    assert llm.calls[0]["max_tokens"] == metrics_store.METRIC_EXTRACTION_MAX_TOKENS
    assert metrics_store.METRIC_EXTRACTION_MAX_TOKENS > 4000


def test_truncated_reply_is_repaired_and_logged(fresh_db):
    cid, rid = _company_and_run("truncated@corp.com")
    llm = _FakeLLM([_truncated_fenced_reply()], stop_reasons=["max_tokens"])
    lines = []

    n = metrics_store.extract_metrics_to_db(_ANALYSIS, cid, rid, llm=llm, log=lines.append)

    assert n == 2  # complete rows only — the half row is dropped, never guessed
    assert _stored_names(cid) == ["sales_revenue", "order_count"]
    assert len(llm.calls) == 1  # salvage succeeded; no retry burned
    assert any("token limit" in ln for ln in lines)  # the cut is loud


def test_prose_reply_yields_zero_with_loud_warning(fresh_db):
    cid, rid = _company_and_run("prose@corp.com")
    llm = _FakeLLM(["分析显示销售向好。", "（仍是散文，没有 JSON。）"])
    lines = []

    n = metrics_store.extract_metrics_to_db(_ANALYSIS, cid, rid, llm=llm, log=lines.append)

    assert n == 0
    assert _stored_names(cid) == []
    assert len(llm.calls) == 2  # exactly one retry on total failure
    # The 0-metric outcome is LOUD — the exact greppable warning line lands.
    assert metrics_store.METRIC_EXTRACTION_FAILURE_WARNING in lines
    assert any("⚠️" in ln for ln in lines)


def test_retry_recovers_after_first_bad_reply(fresh_db):
    cid, rid = _company_and_run("retry@corp.com")
    llm = _FakeLLM(["无法解析的散文。",
                    "```json\n" + json.dumps(_ROWS, ensure_ascii=False) + "\n```"])
    lines = []

    n = metrics_store.extract_metrics_to_db(_ANALYSIS, cid, rid, llm=llm, log=lines.append)

    assert n == 3
    assert len(llm.calls) == 2
    assert metrics_store.METRIC_EXTRACTION_FAILURE_WARNING not in lines
