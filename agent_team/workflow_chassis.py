"""
workflow_chassis.py — the shared skeleton the LINEAR finished-report workflows reuse.

对账 (settlement_orchestrator), 应收账款 (ar_orchestrator), and the 跨境财务 evidence engines
(crossborder_orchestrator) are all linear drivers: a deterministic engine produces every
number, then the flow is (optional) writer → persist → FINISH (record the run + render a
branded, CJK-correct, encrypted PDF). They diverge in the engine, the writer (1-LLM vs
none), and the persistence table — but the FINISH tail and the ROUTE-level upload ingest
(screen + save + encrypt the uploaded role files) are IDENTICAL across all three. This
module owns those shared pieces so the drivers + routes don't each re-copy them; behavior
is byte-for-byte the inlined versions (proven by the unchanged 对账/AR/cross-border suites).

This is the extraction the §2b plan named CHASSIS-CANDIDATE — kept deliberately small: the
genuinely-shared leaves, NOT a premature WorkflowModule abstraction over the engines/writers.
"""
import os
from datetime import datetime

from . import crypto, metrics_store, security


# ---------------------------------------------------------------------------
# The FINISH tail — record the run + render the branded PDF.
# ---------------------------------------------------------------------------
def render_workflow_pdf(memory, company_id, run_id, *, report_title,
                        period_label=None, period_prefix="期间："):
    """Render `memory.final_report` into a branded, CJK-correct, encrypted PDF — the same
    path all three linear workflows use; only `report_title` + `period_prefix` differ.
    Best-effort: a PDF failure logs a skip and never aborts the run."""
    if not (company_id and run_id):
        return
    snap = memory.snapshot()
    final = (snap.get("final_report") or "").strip()
    if not final:
        return
    try:
        from . import pdf_report
        branding = metrics_store.get_branding(company_id)
        profile = metrics_store.get_profile(company_id)
        company_name = (profile.get("company_name") or "").strip() or "本公司"
        logo = branding.get("logo_path") or None
        if logo and not os.path.isabs(logo):
            logo = os.path.join(metrics_store.PROJECT_ROOT, logo)
        if logo and not os.path.isfile(logo):
            logo = None
        period = (period_prefix + period_label) if period_label else (
            "报告生成时间：" + datetime.now().strftime("%Y-%m-%d %H:%M"))
        out = metrics_store.report_pdf_path(company_id, run_id)
        pdf_report.build_report_pdf(
            out_path=out, markdown_text=final, company_name=company_name,
            report_title=report_title, period_label=period, logo_path=logo,
            brand_color=branding.get("brand_color") or pdf_report.DEFAULT_BRAND_COLOR,
            sources=snap.get("sources") or [], sources_status=snap.get("sources_status"),
            product_domain=(os.environ.get("PRODUCT_DOMAIN") or "").strip())
        crypto.encrypt_file_in_place(out)
        memory.log("ceo", f"已生成{report_title} PDF（{os.path.basename(out)}）。",
                   type="success")
    except Exception as e:  # noqa: BLE001 — a PDF hiccup must never lose the run
        memory.log("ceo", f"PDF 生成已跳过（{e}）。", type="info")


def finish_workflow_run(memory, company_id, run_id, status, *, cost_yuan=0.0,
                        report_title, period_label=None, period_prefix="期间："):
    """Record the run (status + ¥ cost) and render the branded PDF — the shared finish
    tail of every linear workflow. `cost_yuan` is 0 for the no-LLM cross-border engines."""
    if run_id:
        try:
            metrics_store.finish_run(run_id, status,
                                     total_cost_yuan=round(cost_yuan, 6),
                                     report_path=memory.job_id)
        except Exception:  # noqa: BLE001
            pass
    render_workflow_pdf(memory, company_id, run_id, report_title=report_title,
                        period_label=period_label, period_prefix=period_prefix)


# ---------------------------------------------------------------------------
# The ROUTE-level upload ingest — screen + save + encrypt the role files.
# ---------------------------------------------------------------------------
class UploadError(Exception):
    """A bad upload (executable-looking or oversize). Carries a customer-facing
    Chinese message + the HTTP status the route should return."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def screen_uploads(role_files, max_bytes):
    """Reject any executable-looking or oversize upload BEFORE persisting. `role_files`
    is a list of (role, cn_label, werkzeug FileStorage). Raises UploadError on a bad file
    (mirrors the inline guard the recon/AR/cross-border run routes all used)."""
    max_mb = max_bytes // (1024 * 1024)
    for _role, _cn, f in role_files:
        head = f.stream.read(16)
        f.stream.seek(0, os.SEEK_END)
        size = f.stream.tell()
        f.stream.seek(0)
        if security.looks_executable(head):
            raise UploadError(f"文件 “{f.filename}” 看起来是可执行程序，已拒绝上传。")
        if size > max_bytes:
            raise UploadError(f"文件 “{f.filename}” 超过单个文件 {max_mb} MB 的上限。")


def save_role_uploads(role_files, dest_dir, safe_name_fn, *, role_prefix=False,
                      memory=None, role_labels=None):
    """Persist screened role files (encrypted at rest) into `dest_dir`, returning
    files_by_role = {role: (path, original_filename)}. `safe_name_fn(filename)` sanitizes
    a name; `role_prefix=True` prepends the role so two same-named uploads can't collide
    (the cross-border + scheduling paths need this — several files per run). Optionally
    logs an 已接收 line per file to `memory`."""
    os.makedirs(dest_dir, exist_ok=True)
    files_by_role = {}
    for role, cn, f in role_files:
        base = safe_name_fn(f.filename)
        safe = f"{role}_{base}" if role_prefix else base
        path = os.path.join(dest_dir, safe)
        f.save(path)
        crypto.encrypt_file_in_place(path)
        files_by_role[role] = (path, f.filename)
        if memory is not None:
            label = (role_labels or {}).get(role, cn or role)
            memory.log("system", f"已接收{label}：{f.filename}", type="info")
    return files_by_role
