// ===========================================================================
// import_flow.js — the shared 历史数据导入 flow (stage files → estimate →
// start → 900ms progress poll), extracted from the 企业大脑 import tab so the
// onboarding wizard's step ④ (Phase 8, plan §3.10 — the Moat-1 front-loading
// step) reuses the exact same machinery-free UI. Customers see 文件数 +
// 预计时间, never raw model ¥: the import endpoints strip estimated_cost/cost
// for non-founder sessions, and the ¥ line below renders ONLY when the API
// sent the field (founder).
//
// mountImportFlow(host, opts) clears `host`, paints into a root it owns, and
// returns stop(). The poll is a setTimeout chain guarded by stop() AND the
// root's connectedness (a re-rendered wizard body or a switched brain tab
// disconnects the root), but callers MUST still call stop() from their
// cleanup so no timer outlives the view:
//   brain.js — tab switch + mount()'s cleanup;
//   onboarding wizard — every renderOnboard repaint + closeOnboarding.
// opts:
//   lead         — the intro line above the drop zone
//   onboarding   — true marks the import as the wizard's step ④; the backend
//                  then records the step-4 funnel event when the job FINISHES
//   onFinished(job) — the job reached a terminal paint (done/stopped_budget):
//                  the brain refreshes its hero stats; the wizard reveals 下一步
// ===========================================================================
import * as api from "./api.js";
import { esc, fmtBytes } from "./components.js";

export function mountImportFlow(host, { lead, onboarding = false, onFinished = null } = {}) {
  let staged = [];        // File objects (sent to estimate AND start)
  let estimate = null;
  let timer = null;
  let stopped = false;

  const root = document.createElement("div");
  root.className = "import-flow";
  host.innerHTML = "";
  host.appendChild(root);

  function stop() {
    stopped = true;
    if (timer) { clearTimeout(timer); timer = null; }
  }
  const dead = () => stopped || !root.isConnected;

  function paint() {
    root.innerHTML = `
      <p class="muted" style="margin:4px 0 0;">${esc(lead || "")}</p>
      <div class="drop-zone" id="impDrop" tabindex="0" style="margin-top:12px;">
        <div class="dz-icon">📥</div>
        <div class="dz-title">拖拽历史数据到这里，或点击选择</div>
        <div class="dz-sub">Excel（.xlsx）或 CSV，可多选，单文件 ≤ 25MB</div>
        <input id="impFile" type="file" multiple accept=".csv,.xlsx,.xls" hidden>
      </div>
      <ul class="file-list" id="impStaged"></ul>
      <div id="impEstimate"></div>
      <p class="form-error" id="impErr" hidden></p>
      <div class="brain-add-row" id="impActions"></div>
      <div class="imp-progress" id="impProgress"></div>`;

    const drop = root.querySelector("#impDrop");
    const fileInput = root.querySelector("#impFile");
    drop.addEventListener("click", () => fileInput.click());
    drop.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
    });
    drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("drag"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
    drop.addEventListener("drop", (e) => {
      e.preventDefault();
      drop.classList.remove("drag");
      addFiles(e.dataTransfer.files);
    });
    fileInput.addEventListener("change", () => { addFiles(fileInput.files); fileInput.value = ""; });
    paintStaged();
  }

  function addFiles(list) {
    const incoming = Array.from(list || []).filter((f) => f && f.name);
    for (const f of incoming) {
      if (!staged.some((s) => s.name === f.name)) staged.push(f);
    }
    estimate = null;
    paintStaged();
  }

  function showErr(msg) {
    const p = root.querySelector("#impErr");
    if (!p) return;
    p.hidden = !msg;
    p.textContent = msg || "";
  }

  function paintStaged() {
    const ul = root.querySelector("#impStaged");
    ul.innerHTML = staged.map((f, i) => `
      <li class="file-item">
        <span class="file-name">${esc(f.name)}</span>
        <span class="file-size">${esc(fmtBytes(f.size || 0))}</span>
        <button type="button" class="file-rm" data-rm="${i}" title="移除" aria-label="移除 ${esc(f.name)}">&times;</button>
      </li>`).join("");
    ul.querySelectorAll("[data-rm]").forEach((b) => b.addEventListener("click", () => {
      staged.splice(Number(b.dataset.rm), 1);
      estimate = null;
      paintStaged();
    }));
    paintEstimate();
    paintActions();
  }

  function fmtDuration(seconds) {
    const mins = Math.max(1, Math.ceil((Number(seconds) || 0) / 60));
    return `约 ${mins} 分钟`;
  }

  function paintEstimate() {
    const box = root.querySelector("#impEstimate");
    if (!estimate) { box.innerHTML = ""; return; }
    // estimated_cost arrives ONLY for founder sessions — customers see
    // 文件数 + 预计时间, never raw model ¥.
    const cost = (estimate.estimated_cost != null)
      ? ` · 内部成本约 ¥${Number(estimate.estimated_cost).toFixed(2)}（仅你可见）` : "";
    box.innerHTML = `<p class="sch-ok" style="margin:10px 0 0;">
        将导入 <strong>${esc(estimate.file_count)}</strong> 个文件，预计 ${esc(fmtDuration(estimate.estimated_seconds))}${esc(cost)}。
        确认无误后点「开始导入」。</p>`;
  }

  function paintActions(running = false) {
    const box = root.querySelector("#impActions");
    if (running) { box.innerHTML = ""; return; }
    if (!staged.length) { box.innerHTML = ""; return; }
    box.innerHTML = estimate
      ? `<button type="button" class="run-btn" id="impStart" style="max-width:220px;">开始导入</button>
         <button type="button" class="ghost-btn" id="impReset">重新选择</button>`
      : `<button type="button" class="ghost-btn" id="impEstimateBtn">评估这 ${staged.length} 个文件</button>`;
    const est = box.querySelector("#impEstimateBtn");
    if (est) est.addEventListener("click", runEstimate);
    const start = box.querySelector("#impStart");
    if (start) start.addEventListener("click", runImport);
    const reset = box.querySelector("#impReset");
    if (reset) reset.addEventListener("click", () => { staged = []; estimate = null; paintStaged(); });
  }

  function formData() {
    const fd = new FormData();
    staged.forEach((f) => fd.append("files", f));
    return fd;
  }

  async function runEstimate(e) {
    showErr("");
    e.target.disabled = true;
    e.target.textContent = "评估中…";
    try {
      estimate = await api.estimateBrainImport(formData());
    } catch (err) {
      estimate = null;
      if (dead()) return;
      showErr(err.message);
    }
    if (dead()) return;
    paintEstimate();
    paintActions();
  }

  async function runImport() {
    showErr("");
    paintActions(true);
    const fd = formData();
    // The wizard's step ④ marks itself so ONLY wizard-context imports write
    // the step-4 funnel event (a later 企业大脑 import never creates funnel rows).
    if (onboarding) fd.append("onboarding", "1");
    let jobId = null;
    try {
      jobId = (await api.startBrainImport(fd)).job_id;
    } catch (err) {
      if (dead()) return;
      showErr(err.message);
      paintActions();
      return;
    }
    if (dead()) return;
    poll(jobId);
  }

  function paintProgress(job) {
    const box = root.querySelector("#impProgress");
    if (!box) return;
    const stateLabel = { pending: "等待中", working: "解析中…", done: "完成",
                         error: "出错", stopped_budget: "已中止" };
    const files = (job.files || []).map((f) => {
      const cls = f.status === "done" ? "done"
        : (f.status === "error" || f.status === "stopped_budget") ? "error" : "";
      const note = f.status === "done" ? `已记住 ${f.metrics} 个指标` : (stateLabel[f.status] || f.status);
      return `<div class="imp-file"><span class="imp-name">${esc(f.name)}</span>
          <span class="imp-state ${cls}">${esc(note)}</span></div>`;
    }).join("");
    const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
    let headline;
    if (job.status === "running") headline = `正在导入… ${job.done} / ${job.total} 个文件`;
    else if (job.status === "stopped_budget") headline = "导入提前结束（已达本次额度上限），已记住的部分仍然有效。";
    else headline = "✅ 导入完成 — 历史已记入企业大脑。";
    box.innerHTML = `
      <div class="quota" style="margin-top:6px;">
        <div class="quota-caption"><span>${esc(headline)}</span><strong>${pct}%</strong></div>
        <div class="meter"><div class="meter-fill ok" style="width:${pct}%"></div></div>
      </div>${files}`;
  }

  function poll(jobId) {
    if (timer) { clearTimeout(timer); timer = null; }
    const tick = async () => {
      let job = null;
      try {
        job = await api.getBrainImport(jobId);
      } catch (err) {
        if (dead()) return;
        // IMPORT_JOBS is in-memory: a restart turns the poll into a 404 —
        // stop gracefully instead of error-looping.
        showErr(err.status === 404
          ? "导入任务已结束或服务已重启 — 刷新页面查看最新统计。"
          : err.message);
        paintActions();
        return;
      }
      if (dead()) return;
      paintProgress(job);
      if (job.status === "running") {
        timer = setTimeout(tick, 900);
      } else {
        staged = [];
        estimate = null;
        paintStaged();      // clear the staged list; the progress box stays
        if (onFinished) onFinished(job);
      }
    };
    tick();
  }

  paint();
  return stop;
}
