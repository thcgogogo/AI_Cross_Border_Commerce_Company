"""
test_industry_packs.py — Phase 4 acceptance tests for the vertical template packs
(Moat 2): the fail-loud boot gate, the pack content reaching the agents' prompts,
the hot-edit-between-runs behavior, and the per-run override beating the profile.

Run from the project root:

    .venv/bin/python -m pytest tests/test_industry_packs.py -v

No test hits the network or spends real money — the LLM is always a local fake,
and the DB-touching test uses its OWN throwaway SQLite file via `fresh_db`.
"""

import os
import sys

import pytest
import yaml

# Make the project root importable no matter where pytest is launched from.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402 — module-level boot is one-time & idempotent
from agent_team import config, db, industry_packs, metrics_store  # noqa: E402
from agent_team.memory import SharedMemory  # noqa: E402
from agent_team.orchestrator import Orchestrator  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path):
    """Point the company brain at a throwaway SQLite file for this one test."""
    db.init_db(str(tmp_path / "brain.db"))
    yield


class _FakeLLM:
    """Network-free stand-in for ModelRouter — only Orchestrator construction is
    exercised here, so .complete is never reached."""

    def complete(self, **kwargs):
        return "ok"


def _write_pack(packs_dir, key, *, skeleton, kpis=None, benchmarks=None,
                analyst_extra="", writer_extra="", display="测试垂直"):
    """Write one valid pack.yaml under <packs_dir>/<key>/."""
    os.makedirs(os.path.join(packs_dir, key), exist_ok=True)
    data = {
        "display_name_cn": display,
        "required_kpis": kpis or [],
        "benchmarks": benchmarks or [],
        "report_skeleton_cn": skeleton,
        "analyst_extra_prompt": analyst_extra,
        "writer_extra_prompt": writer_extra,
    }
    with open(os.path.join(packs_dir, key, "pack.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True)


def _build_orchestrator(industry_key, tmp_path, company_id=None):
    mem = SharedMemory(f"pack-test-{industry_key or 'auto'}", store_dir=str(tmp_path / "mem"))
    return Orchestrator(mem, llm=_FakeLLM(), industry_key=industry_key, company_id=company_id)


# ---------------------------------------------------------------------------
# Test 1 — boot fails LOUDLY on a pack missing report_skeleton_cn, naming the
# offending file and the missing key.
# ---------------------------------------------------------------------------
def test_boot_rejects_pack_missing_required_key(tmp_path, monkeypatch):
    """A pack on disk missing report_skeleton_cn makes app start exit with a clear
    error that names BOTH the offending file and the missing key."""
    packs = tmp_path / "templates_industry"
    # A valid generic fallback (required to exist) ...
    _write_pack(str(packs), "generic", skeleton=["核心结论"])
    # ... and a malformed pack: every required key EXCEPT report_skeleton_cn.
    os.makedirs(packs / "bad")
    with open(packs / "bad" / "pack.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump({
            "display_name_cn": "坏包",
            "required_kpis": [], "benchmarks": [],
            "analyst_extra_prompt": "", "writer_extra_prompt": "",
        }, f, allow_unicode=True)

    monkeypatch.setattr(industry_packs, "PACKS_DIR", str(packs))

    # The underlying validator raises PackError naming the file + the missing key.
    with pytest.raises(industry_packs.PackError) as ei:
        industry_packs.validate_all_packs()
    msg = str(ei.value)
    assert "report_skeleton_cn" in msg
    assert os.path.join("bad", "pack.yaml") in msg

    # The app-start gate turns that into a clean SystemExit (no traceback noise),
    # carrying the same file + key — i.e. the app refuses to start.
    with pytest.raises(SystemExit) as se:
        app._validate_industry_packs_or_exit()
    smsg = str(se.value)
    assert "report_skeleton_cn" in smsg
    assert os.path.join("bad", "pack.yaml") in smsg


# ---------------------------------------------------------------------------
# Test 2 — a run with industry=cross_border_ecommerce tailors the agents: the
# Report Writer prompt carries "退款率" and the mandatory "下周建议行动" section.
# ---------------------------------------------------------------------------
def test_cross_border_pack_reaches_agent_prompts(tmp_path):
    """The shipped 跨境电商 pack injects its benchmarks + skeleton into the Report
    Writer, its required KPIs into the Data Analyst, and the verbatim benchmark
    label rule — without ever mutating the shared config."""
    orch = _build_orchestrator("cross_border_ecommerce", tmp_path)

    writer = orch.workers["report_writer"].cfg["system_prompt"]
    analyst = orch.workers["data_analyst"].cfg["system_prompt"]

    # Benchmark metric + the mandatory final section both reach the Report Writer.
    assert "退款率" in writer
    assert "下周建议行动" in writer
    # The benchmark instruction is injected VERBATIM (per the Phase 4 spec).
    assert industry_packs.BENCHMARK_INSTRUCTION in writer

    # The industry's required KPIs are merged into the Data Analyst's prompt.
    assert "总销售额" in analyst

    # Injection must never mutate the SHARED config — the base prompts stay clean.
    base = {w["id"]: w["system_prompt"] for w in config.get_workers()}
    assert "下周建议行动" not in base["report_writer"]
    assert "总销售额" not in base["data_analyst"]


# ---------------------------------------------------------------------------
# Test 3 — editing a pack's YAML between two runs changes the SECOND run's prompt
# with NO app restart (packs are re-read fresh on every run).
# ---------------------------------------------------------------------------
def test_pack_is_hot_editable_between_runs(tmp_path, monkeypatch):
    """Run 1 sees the original skeleton title; we edit the YAML on disk; run 2 (same
    process, no reload) sees the new title and not the old one."""
    packs = tmp_path / "templates_industry"
    _write_pack(str(packs), "generic", skeleton=["核心结论"])
    _write_pack(str(packs), "vert", skeleton=["原始章节标题ABC"])
    monkeypatch.setattr(industry_packs, "PACKS_DIR", str(packs))

    run1 = _build_orchestrator("vert", tmp_path).workers["report_writer"].cfg["system_prompt"]
    assert "原始章节标题ABC" in run1
    assert "新增章节标题XYZ" not in run1

    # Hot-edit the pack content on disk — no process restart, no module reload.
    _write_pack(str(packs), "vert", skeleton=["新增章节标题XYZ"])

    run2 = _build_orchestrator("vert", tmp_path).workers["report_writer"].cfg["system_prompt"]
    assert "新增章节标题XYZ" in run2
    assert "原始章节标题ABC" not in run2


# ---------------------------------------------------------------------------
# Test 4 — the per-run override beats the company profile, end to end.
# ---------------------------------------------------------------------------
def test_override_takes_precedence_over_profile(fresh_db, tmp_path):
    """With a profile of 贸易公司, an explicit cross_border_ecommerce override wins
    (resolve + the resulting Report Writer prompt), while "auto"/"" fall back to the
    profile's industry."""
    cid = metrics_store.get_or_create_company("ind@corp.com", company_name="IndCo")
    metrics_store.save_profile(cid, {"industry": "贸易公司"})

    # resolve precedence: a concrete override wins; auto/empty defer to the profile.
    assert app._resolve_industry("cross_border_ecommerce", cid) == "cross_border_ecommerce"
    assert app._resolve_industry("auto", cid) == "贸易公司"
    assert app._resolve_industry("", cid) == "贸易公司"

    # End to end: the override drives the pack actually injected into the run.
    override_key = app._resolve_industry("cross_border_ecommerce", cid)
    writer_override = _build_orchestrator(override_key, tmp_path, company_id=cid) \
        .workers["report_writer"].cfg["system_prompt"]
    assert "退款与售后" in writer_override        # a cross-border-only section
    assert "应收与账期" not in writer_override     # a trading-company-only section

    # With "auto", the profile (贸易公司) is used instead.
    auto_key = app._resolve_industry("auto", cid)
    writer_auto = _build_orchestrator(auto_key, tmp_path, company_id=cid) \
        .workers["report_writer"].cfg["system_prompt"]
    assert "应收与账期" in writer_auto
