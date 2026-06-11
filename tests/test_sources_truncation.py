"""
test_sources_truncation.py — regression tests for the truncated-SOURCES_JSON fix.

Run from the project root:

    .venv/bin/python -m pytest tests/test_sources_truncation.py -v

Background (job onboard-26c4746a00, 2026-06-11): the Report Writer is contracted
to put the machine SOURCES_JSON block at the very END of its output, so when a
long report hits the output-token ceiling the block is the first thing cut — the
array stopped mid-row 47 of 75, the strict parser reported "malformed", and the
PDF showed the 「本次未能生成数据溯源表」 fallback banner instead of the trust table.

The mitigation under test, in three layers:
  - CURE      verification.recover_sources() salvages the COMPLETE rows the
              writer actually wrote (a half-written row is dropped, never
              invented), and the orchestrator falls back to the Report Writer's
              own draft artifact when the CEO's synthesis dropped/re-clipped the
              block. Salvaged runs are marked sources_recovered=True so the
              contract-miss rate stays measurable.
  - PREVENT   the Report Writer and the CEO's synthesis call carry a raised
              output ceiling (config-driven max_tokens) instead of the 8000
              default that clipped real runs.
  - MEASURE   a call that stops at the ceiling (stop_reason == "max_tokens") is
              logged as a warning in the live feed instead of passing silently.

No test hits the network or spends real money — the LLM is a local fake, and
each SharedMemory writes to its own tmp_path store.
"""

import json
import os
import sys

# Make the project root importable no matter where pytest is launched from.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_team import config, verification  # noqa: E402
from agent_team.memory import SharedMemory  # noqa: E402
from agent_team.orchestrator import Orchestrator  # noqa: E402


class _FakeLLM:
    """Network-free ModelRouter stand-in. Records every complete() call's kwargs
    (so tests can assert max_tokens plumbing) and returns a canned response —
    the default "[]" keeps the cheap number-auditor silent."""

    last_stop_reason = None

    def __init__(self, response="[]"):
        self.response = response
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


# A report body carrying five inline anchors, like a real writer draft.
_BODY = (
    "# 经营周报\n\n"
    "## 核心结论\n"
    "- 本周销售额 164,100 元【1】。\n"
    "- 环比 −2.84%【2】。\n"
    "- 线上渠道 131,500 元【3】。\n"
    "- 线下渠道 37,400 元【4】。\n"
    "- 连续 11 周正增长后首次回落【5】。\n"
)


def _row(n):
    return {"anchor": n, "claim_cn": f"指标 {n} 的数值与口径说明",
            "source_file": "示例销售数据.csv",
            "source_detail": f"Sheet 'data' 第 {n} 行", "computed_by": "data_analyst"}


_ROWS = [_row(n) for n in range(1, 6)]


def _writer_output(body=_BODY, rows=_ROWS):
    """A contract-complete Report Writer output: body + the machine line."""
    return body + "\n\n" + verification.SOURCES_MARKER + " " + json.dumps(rows, ensure_ascii=False)


def _truncated():
    """The same output cut mid-row-5, exactly like an output-token stop."""
    full = _writer_output()
    cut_at = full.rfind('"anchor": 5')
    assert cut_at != -1
    return full[:cut_at + 12]  # ends inside row 5's JSON object


def _orch(tmp_path, llm=None):
    mem = SharedMemory("trunc-test", store_dir=str(tmp_path / "mem"))
    return Orchestrator(mem, llm=llm or _FakeLLM()), mem


# ---------------------------------------------------------------------------
# CURE — verification.recover_sources() unit behavior
# ---------------------------------------------------------------------------
def test_recover_sources_keeps_complete_rows_drops_half_row():
    cut = _truncated()

    # The strict contract parse still reports the truth: the block is malformed.
    body, sources, status = verification.split_report_and_sources(cut)
    assert status == "malformed" and sources is None
    assert "【1】" in body and verification.SOURCES_MARKER not in body

    # The salvage path recovers rows 1-4 verbatim and drops half-written row 5.
    rows = verification.recover_sources(cut)
    assert [r["anchor"] for r in rows] == [1, 2, 3, 4]
    assert rows[0]["claim_cn"] == _ROWS[0]["claim_cn"]
    assert rows[3]["source_detail"] == _ROWS[3]["source_detail"]


def test_recover_sources_nothing_to_salvage():
    # No marker at all.
    assert verification.recover_sources("正文【1】，没有机器块。") is None
    # Marker present but not even one complete row survived the cut.
    assert verification.recover_sources(
        _BODY + "\n\n" + verification.SOURCES_MARKER + ' [{"anchor": 1, "cla') is None
    # Marker present but the tail is garbage, not a truncated array.
    assert verification.recover_sources(
        _BODY + "\n\n" + verification.SOURCES_MARKER + " [ {oops not json") is None
    assert verification.recover_sources("") is None
    assert verification.recover_sources(None) is None


def test_count_anchors_counts_distinct_numbers():
    assert verification.count_anchors(_BODY) == 5
    assert verification.count_anchors("重复引用【1】两次【1】，再加【 2 】") == 2
    assert verification.count_anchors("没有锚点") == 0
    assert verification.count_anchors(None) == 0


# ---------------------------------------------------------------------------
# CURE — the orchestrator's salvage in _apply_verification_trail
# ---------------------------------------------------------------------------
def test_truncated_final_salvages_sources_instead_of_banner(tmp_path):
    """The exact onboard-26c4746a00 shape: writer hit the ceiling mid-block and
    the CEO copied the clipped line verbatim. The run must ship a trust table
    with every complete row instead of the fallback banner."""
    orch, mem = _orch(tmp_path)
    cut = _truncated()
    mem.add_artifact("report_writer", cut)

    out = orch._apply_verification_trail(cut)

    snap = mem.snapshot()
    assert snap["sources_status"] == "ok"
    assert snap["sources_recovered"] is True
    assert [r["anchor"] for r in snap["sources"]] == [1, 2, 3, 4]
    # The user-facing body keeps its anchors and never shows the machine line.
    assert "【1】" in out and "【5】" in out
    assert verification.SOURCES_MARKER not in out
    # The salvage is loud, not silent — a warning line lands in the live feed.
    assert any(a["type"] == "warning" and "恢复" in a["message"]
               for a in snap["activity"])


def test_synthesis_dropped_block_recovered_from_writer_draft(tmp_path):
    """If the CEO forgets to copy the machine line entirely (status "missing"),
    the writer's own draft artifact still has it — all rows come back."""
    orch, mem = _orch(tmp_path)
    mem.add_artifact("report_writer", _writer_output())

    out = orch._apply_verification_trail(_BODY)  # final lost the block

    snap = mem.snapshot()
    assert snap["sources_status"] == "ok"
    assert snap["sources_recovered"] is True
    assert [r["anchor"] for r in snap["sources"]] == [1, 2, 3, 4, 5]
    assert verification.SOURCES_MARKER not in out


def test_unrecoverable_block_still_degrades_to_malformed(tmp_path):
    """When not even one complete row exists anywhere, the old graceful path is
    unchanged: status "malformed", no rows, report still ships."""
    orch, mem = _orch(tmp_path)
    garbage = _BODY + "\n\n" + verification.SOURCES_MARKER + " [ {oops not json"
    mem.add_artifact("report_writer", garbage)

    out = orch._apply_verification_trail(garbage)

    snap = mem.snapshot()
    assert snap["sources_status"] == "malformed"
    assert snap["sources"] == []
    assert snap["sources_recovered"] is False
    assert "【1】" in out and verification.SOURCES_MARKER not in out


def test_non_report_without_anchors_skips_salvage(tmp_path):
    """A deliverable with no anchors (a poem, a slide outline) must not trigger
    the salvage machinery or its warning log."""
    orch, mem = _orch(tmp_path)

    orch._apply_verification_trail("一首小诗，没有任何数字。")

    snap = mem.snapshot()
    assert snap["sources_status"] == "missing"
    assert snap["sources_recovered"] is False
    assert not any("恢复" in a["message"] for a in snap["activity"])


# ---------------------------------------------------------------------------
# PREVENT — the raised output ceilings actually reach the LLM call
# ---------------------------------------------------------------------------
def test_report_writer_and_synthesis_get_raised_output_ceiling(tmp_path):
    llm = _FakeLLM()
    orch, mem = _orch(tmp_path, llm=llm)

    cfg = config.get_agent("report_writer")
    assert cfg.get("max_tokens", 0) > 8000  # the old default that clipped real runs

    orch.workers["report_writer"].run("写一份经营周报")
    assert llm.calls[-1]["max_tokens"] == cfg["max_tokens"]

    orch._synthesize()
    assert llm.calls[-1]["max_tokens"] > 8000


# ---------------------------------------------------------------------------
# MEASURE — a call that stops at the ceiling is logged, not silent
# ---------------------------------------------------------------------------
def test_max_tokens_stop_reason_logged_as_warning(tmp_path):
    llm = _FakeLLM()
    llm.last_stop_reason = "max_tokens"
    orch, mem = _orch(tmp_path, llm=llm)

    orch.workers["report_writer"].run("写一份经营周报")

    snap = mem.snapshot()
    assert any(a["agent"] == "report_writer" and a["type"] == "warning"
               and "length ceiling" in a["message"] for a in snap["activity"])


def test_clean_stop_logs_no_truncation_warning(tmp_path):
    orch, mem = _orch(tmp_path)  # _FakeLLM.last_stop_reason is None

    orch.workers["report_writer"].run("写一份经营周报")

    snap = mem.snapshot()
    assert not any("length ceiling" in a["message"] for a in snap["activity"])
