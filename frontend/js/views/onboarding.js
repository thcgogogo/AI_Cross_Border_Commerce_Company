// ===========================================================================
// onboarding.js — 新手引导 wizard (5 steps, skippable & resumable). A direct
// port of the old app.js wizard into the new shell: same endpoints, same
// funnel events, light theme. The step list itself is reworked in Phase 8
// (history-import step + plan-gated finale).
//
// Phase 4: step 4's live feed runs through the shared customer filter
// (run_feed.js — no machinery lines), and 查看完整报告 closes the wizard and
// opens the branded report view at /reports/<run_id>. The inline preview
// survives only as the fallback for runs without a run id.
// ===========================================================================
import * as api from "../api.js";
import { state, loadAgents, loadIndustries } from "../state.js";
import { esc, renderMarkdown } from "../components.js";
import { feedHTML } from "../run_feed.js";
import { navigate } from "../main.js";
import { openLoginModal } from "./auth.js";

const $ = (id) => document.getElementById(id);

const OB_STEP_TITLES = { 1: "公司信息", 2: "上传数据", 3: "KPI 确认", 4: "生成首份报告", 5: "每周定时" };

let obOpen = false;
let obState = null;       // last /api/onboarding/* state payload
let obStep = 1;
let obBusy = false;
let obError = "";
let obData = null;        // step-2 result {file_name, headers, kpi_guesses}
let obJobId = null;       // step-4 run id (polls /api/status)
let obSnap = null;        // step-4 live snapshot
let obPollTimer = null;
let obScheduleDone = null;
let obShowReport = false; // Phase-2: inline report preview on step 4

export function init() {
  $("onboardRoot").innerHTML = `
    <div id="onboardView" class="onboard-view" aria-hidden="true">
      <div class="onboard-bar">
        <span class="onboard-brand">新手引导</span>
        <div class="ob-steps" id="obSteps"></div>
        <button id="onboardClose" class="onboard-close" title="稍后再说" aria-label="关闭">&times;</button>
      </div>
      <div id="onboardBody" class="onboard-body"></div>
    </div>`;
  $("onboardClose").addEventListener("click", () => {
    try { localStorage.setItem(obDismissKey(), "1"); } catch (e) { /* private mode */ }
    closeOnboarding();
  });
}

// A per-account, per-browser “don't auto-open again” mark (the × button). The
// wizard stays reachable from the workbench; skipping step 5 ends it for real.
function obDismissKey() {
  return "ob-dismissed-" + ((state.account && state.account.id) || "anon");
}

export async function maybeAutoOpenOnboarding() {
  if (!state.account || obOpen) return;
  let dismissed = false;
  try { dismissed = localStorage.getItem(obDismissKey()) === "1"; } catch (e) { /* fine */ }
  if (dismissed) return;
  try {
    const st = await api.onboardingState();
    obState = st;
    if (!st.done) openOnboarding();
  } catch (e) { /* the wizard stays reachable from the workbench */ }
}

export async function openOnboarding(opts) {
  if (!state.account) { openLoginModal(); return; }
  obError = ""; obBusy = false; obShowReport = false;
  // Industry packs feed step 1; agent names feed the live feed. Best-effort.
  loadIndustries();
  loadAgents();
  try {
    // /start returns the state AND records the funnel's “wizard opened” event.
    obState = await api.onboardingStart();
  } catch (e) { /* fall through with whatever state we have */ }
  if (!obState) obState = { steps: {}, next_step: 1, done: false };
  obStep = (opts && opts.demo) ? 2 : (obState.next_step || 1);
  obJobId = null; obSnap = null; obData = null; obScheduleDone = null;
  const v = $("onboardView");
  if (!v) return;
  obOpen = true;
  v.classList.add("open");
  v.setAttribute("aria-hidden", "false");
  renderOnboard();
  if (opts && opts.demo) obDemoData(); // the nudge-email deep link: load the demo now
}

export function closeOnboarding() {
  const v = $("onboardView");
  if (v) { v.classList.remove("open"); v.setAttribute("aria-hidden", "true"); }
  obOpen = false;
  if (obPollTimer) { clearInterval(obPollTimer); obPollTimer = null; }
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------
function obStepsBarHTML() {
  const st = (obState && obState.steps) || {};
  return [1, 2, 3, 4, 5].map((n) => {
    const done = st[String(n)] === "completed" || st[String(n)] === "skipped";
    const cls = n === obStep ? "active" : (done ? "done" : "");
    return `<span class="ob-step ${cls}">${n}. ${OB_STEP_TITLES[n]}${done ? " ✓" : ""}</span>`;
  }).join("");
}

function renderOnboard() {
  const bar = $("obSteps");
  if (bar) bar.innerHTML = obStepsBarHTML();
  const body = $("onboardBody");
  if (!body) return;
  const builders = { 1: obStep1HTML, 2: obStep2HTML, 3: obStep3HTML, 4: obStep4HTML, 5: obStep5HTML };
  body.innerHTML = (builders[obStep] || obStep1HTML)();
  wireOnboardBody(body);
  body.scrollTop = 0;
}

const obErrHTML = () => (obError ? `<p class="ob-err">${esc(obError)}</p>` : "");
const obSkipBtnHTML = (label) =>
  `<button type="button" class="ob-skip" id="obSkipBtn">${esc(label || "跳过这一步")}</button>`;

function obStep1HTML() {
  const name = (obState && obState.company_name) || "";
  const industry = (obState && obState.industry) || "";
  const opts = state.industries.map((p) =>
    `<option value="${esc(p.key)}" ${p.key === industry || p.display_name_cn === industry ? "selected" : ""}>${esc(p.display_name_cn)}</option>`).join("");
  return `
    <h2 class="ob-h">第 1 步 · 公司信息</h2>
    <p class="ob-sub">选好行业后，AI 团队会套用对应行业的报告模板包（必备指标、行业基准、报告结构）。</p>
    <div class="ob-card">
      <label class="ob-label" for="obCompany">公司名称</label>
      <input id="obCompany" class="ob-input" type="text" placeholder="例如：杭州示例科技有限公司" value="${esc(name)}">
      <label class="ob-label" for="obIndustry">所属行业</label>
      <select id="obIndustry" class="ob-select">${opts}</select>
      ${obErrHTML()}
      <div class="ob-actions">
        <button id="obNext1" class="run-btn" type="button" ${obBusy ? "disabled" : ""}>保存并继续 →</button>
        ${obSkipBtnHTML()}
      </div>
    </div>`;
}

function obStep2HTML() {
  const files = (obState && obState.data_files) || [];
  const existing = files.length
    ? `<p class="ob-file-note">已有数据：${files.map(esc).join("、")} — 可以直接进入下一步，或重新上传。</p>
       <div class="ob-actions"><button id="obNext2" class="run-btn" type="button">直接下一步 →</button></div>`
    : "";
  return `
    <h2 class="ob-h">第 2 步 · 上传一份近期数据文件</h2>
    <p class="ob-sub">Excel（.xlsx）或 CSV 都可以。没有现成数据？用内置示例数据，效果完全一样。</p>
    <div class="ob-choice">
      <div class="ob-card">
        <h3 style="margin:0 0 6px;font-size:16px;">上传我的数据</h3>
        <p class="ob-sub">单文件 ≤ 25MB</p>
        <input id="obFile" type="file" accept=".csv,.xlsx,.xls" class="ob-input">
        <button id="obUploadBtn" class="run-btn" type="button" ${obBusy ? "disabled" : ""}>上传并继续 →</button>
      </div>
      <div class="ob-card">
        <h3 style="margin:0 0 6px;font-size:16px;">用示例数据体验</h3>
        <p class="ob-sub">内置 12 周跨境电商销售数据（${esc((obState && obState.demo_file_name) || "示例销售数据.csv")}）。</p>
        <button id="obDemoBtn" class="run-btn" type="button" ${obBusy ? "disabled" : ""}>${obBusy ? "载入中…" : "用示例数据体验 →"}</button>
      </div>
    </div>
    ${existing}
    ${obErrHTML()}
    <div class="ob-actions">${obSkipBtnHTML()}</div>`;
}

function obStep3HTML() {
  const rows = (obData && obData.kpi_guesses) || (obState && obState.kpi_mappings) || [];
  const tr = (r, i) => `
    <tr>
      <td><input data-ob-kpi="name" data-i="${i}" value="${esc(r.kpi_name_cn || "")}"></td>
      <td><input data-ob-kpi="aliases" data-i="${i}" value="${esc(r.aliases || "")}"></td>
      <td><input data-ob-kpi="unit" data-i="${i}" value="${esc(r.unit || "")}" style="width:70px"></td>
    </tr>`;
  const table = rows.length
    ? `<table class="ob-table"><thead><tr><th>指标</th><th>对应的列名（别名，逗号分隔）</th><th>单位</th></tr></thead>
       <tbody>${rows.map(tr).join("")}</tbody></table>`
    : `<p class="ob-sub">（还没有读到表头 — 可先返回上一步上传数据，或直接跳过。）</p>`;
  const fileLine = obData && obData.file_name
    ? `<p class="ob-file-note">来自文件：${esc(obData.file_name)}</p>` : "";
  return `
    <h2 class="ob-h">第 3 步 · KPI 列名确认</h2>
    <p class="ob-sub">系统已按表头猜好每列对应的指标，确认（可修改）后，AI 团队每次分析都会先按这份映射对齐列名。</p>
    <div class="ob-card">
      ${fileLine}
      ${table}
      ${obErrHTML()}
      <div class="ob-actions">
        <button id="obKpiBtn" class="run-btn" type="button" ${obBusy || !rows.length ? "disabled" : ""}>确认并继续 →</button>
        ${obSkipBtnHTML()}
      </div>
    </div>`;
}

function obStep4HTML() {
  const status = obSnap ? obSnap.status : null;
  const acts = feedHTML((obSnap && obSnap.activity) || []);
  const feed = acts ? `<ul class="ob-feed" id="obFeed">${acts}</ul>` : "";
  let action;
  if (status === "done") {
    // With a run id the full branded report view exists — link there. The
    // inline preview only remains for runs the company brain couldn't record.
    const hasRunId = obSnap.run_id != null && obSnap.run_id !== "";
    const report = !hasRunId && obShowReport && obSnap.final_report
      ? `<div class="ob-report-preview report-body">${renderMarkdown(obSnap.final_report)}</div>` : "";
    action = `<p class="ob-ok">✅ 第一份报告已生成！</p>
      <div class="ob-actions">
        <button id="obViewReport" class="run-btn" type="button">${!hasRunId && obShowReport ? "收起报告" : "查看完整报告"}</button>
        <button id="obNext4" class="run-btn" type="button">下一步：设置每周定时 →</button>
      </div>
      ${report}`;
  } else if (status === "error") {
    action = `<p class="ob-err">生成出错：${esc((obSnap && obSnap.error) || "未知错误")}</p>
      <div class="ob-actions">
        <button id="obGenBtn" class="run-btn" type="button">重试</button>
        ${obSkipBtnHTML()}
      </div>`;
  } else if (obJobId) {
    action = `<p class="ob-sub">AI 团队正在工作… 可以实时看到每位专家的进展。</p>`;
  } else {
    action = `${obErrHTML()}
      <div class="ob-actions">
        <button id="obGenBtn" class="run-btn" type="button" ${obBusy ? "disabled" : ""}>${obBusy ? "启动中…" : "生成第一份报告"}</button>
        ${obSkipBtnHTML()}
      </div>
      <p class="ob-file-note">将使用你最近一次上传/示例的数据。免费体验包含 2 份完整报告。</p>`;
  }
  return `
    <h2 class="ob-h">第 4 步 · 自动生成第一份报告</h2>
    <p class="ob-sub">这是见证时刻：AI 团队规划任务、读取数据、撰写成稿 — 全程直播。</p>
    <div class="ob-card">
      ${action}
      ${feed}
    </div>`;
}

function obStep5HTML() {
  if (obScheduleDone) {
    const channelLabel = (c) => ({ email: "邮件", wechat: "企业微信", service_account: "服务号" }[c] || c);
    return `
      <h2 class="ob-h">完成 🎉</h2>
      <div class="ob-card ob-done">
        <h3>每周一早上 8 点，周报自动送达</h3>
        <p class="ob-sub">已创建定时任务「每周经营周报」（${(obScheduleDone.channels || []).map(channelLabel).map(esc).join(" + ") || "邮箱"}）。
        随时可在「定时与送达」里调整。</p>
        <div class="ob-actions" style="justify-content:center">
          <button id="obFinish" class="run-btn" type="button">开始使用</button>
          <a class="ob-skip" href="/help">看看帮助中心</a>
        </div>
      </div>`;
  }
  return `
    <h2 class="ob-h">第 5 步 · 设置每周定时 + 交付方式</h2>
    <p class="ob-sub">一键创建「每周一早上 8 点」自动生成任务，用最近上传的数据出报告并自动送达。</p>
    <div class="ob-card">
      <label class="ob-check"><input type="checkbox" id="obChEmail" checked> 邮箱（账号邮箱）</label>
      <label class="ob-check"><input type="checkbox" id="obChWechat"> 企业微信群机器人
        <span class="ob-file-note" style="margin:0">（需先配置 webhook）</span></label>
      ${obErrHTML()}
      <div class="ob-actions">
        <button id="obSchedBtn" class="run-btn" type="button" ${obBusy ? "disabled" : ""}>${obBusy ? "创建中…" : "一键开启每周定时 →"}</button>
        ${obSkipBtnHTML("跳过，结束引导")}
      </div>
    </div>`;
}

// ---------------------------------------------------------------------------
// Step actions
// ---------------------------------------------------------------------------
function wireOnboardBody(body) {
  const on = (id, fn) => { const el = body.querySelector("#" + id); if (el) el.addEventListener("click", fn); };
  on("obNext1", obSaveCompany);
  on("obUploadBtn", obUpload);
  on("obDemoBtn", obDemoData);
  on("obNext2", () => { obStep = 3; obError = ""; renderOnboard(); });
  on("obKpiBtn", obConfirmKpi);
  on("obGenBtn", obGenerate);
  on("obViewReport", () => {
    if (obSnap && obSnap.run_id != null && obSnap.run_id !== "") {
      closeOnboarding();
      navigate(`/reports/${obSnap.run_id}`);
      return;
    }
    obShowReport = !obShowReport; // fallback: inline preview (no run id)
    renderOnboard();
  });
  on("obNext4", () => { obStep = 5; obError = ""; renderOnboard(); });
  on("obSchedBtn", obSchedule);
  on("obFinish", closeOnboarding);
  const skip = body.querySelector("#obSkipBtn");
  if (skip) skip.addEventListener("click", obSkipStep);
}

async function obSkipStep() {
  const step = obStep;
  try {
    obState = await api.onboardingSkip(step);
  } catch (e) { /* even if the event write fails, let the user move on */ }
  obError = "";
  if (step >= 5) { closeOnboarding(); return; }
  obStep = step + 1;
  renderOnboard();
}

async function obSaveCompany() {
  const name = ($("obCompany") ? $("obCompany").value : "").trim();
  const industry = $("obIndustry") ? $("obIndustry").value : "";
  obError = ""; obBusy = true; renderOnboard();
  try {
    obState = await api.onboardingCompany({ company_name: name, industry });
    obStep = 2;
  } catch (e) {
    obError = e.message;
  }
  obBusy = false;
  renderOnboard();
}

async function obUpload() {
  const input = $("obFile");
  const f = input && input.files && input.files[0];
  if (!f) { obError = "请先选择一个文件。"; renderOnboard(); return; }
  obError = ""; obBusy = true; renderOnboard();
  try {
    const fd = new FormData();
    fd.append("file", f);
    const data = await api.onboardingUpload(fd);
    obData = data; obState = data;
    obStep = 3;
  } catch (e) {
    obError = e.message;
  }
  obBusy = false;
  renderOnboard();
}

async function obDemoData() {
  obError = ""; obBusy = true; renderOnboard();
  try {
    const data = await api.onboardingDemoData();
    obData = data; obState = data;
    obStep = 3;
  } catch (e) {
    obError = e.message;
  }
  obBusy = false;
  renderOnboard();
}

function obCollectKpiRows() {
  const rows = {};
  document.querySelectorAll("#onboardBody [data-ob-kpi]").forEach((el) => {
    const i = el.dataset.i;
    rows[i] = rows[i] || {};
    rows[i][el.dataset.obKpi] = el.value;
  });
  return Object.keys(rows).sort((a, b) => a - b).map((i) => ({
    kpi_name_cn: (rows[i].name || "").trim(),
    aliases: (rows[i].aliases || "").trim(),
    unit: (rows[i].unit || "").trim(),
  })).filter((r) => r.kpi_name_cn || r.aliases);
}

async function obConfirmKpi() {
  obError = ""; obBusy = true;
  const mappings = obCollectKpiRows();
  renderOnboard();
  try {
    obState = await api.onboardingKpiConfirm(mappings);
    obStep = 4;
  } catch (e) {
    obError = e.message;
  }
  obBusy = false;
  renderOnboard();
}

async function obGenerate() {
  obError = ""; obBusy = true; obSnap = null; obShowReport = false; renderOnboard();
  await loadAgents(); // Chinese role names for the live feed
  try {
    const data = await api.onboardingGenerate();
    obJobId = data.job_id;
    obBusy = false;
    renderOnboard();
    obPollTimer = setInterval(obPollJob, 900);
    obPollJob();
  } catch (e) {
    obError = e.message;
    obBusy = false;
    renderOnboard();
  }
}

async function obPollJob() {
  if (!obJobId) return;
  try {
    const snap = await api.getJobStatus(obJobId);
    const before = obSnap ? ((obSnap.activity || []).length + obSnap.status) : "";
    obSnap = snap;
    if (snap.status === "done" || snap.status === "error") {
      clearInterval(obPollTimer); obPollTimer = null;
      try { obState = await api.onboardingState(); } catch (e) { /* fine */ }
      renderOnboard();
      return;
    }
    const after = (snap.activity || []).length + snap.status;
    if (obOpen && obStep === 4 && before !== after) {
      renderOnboard();
      const feed = $("obFeed");
      if (feed) feed.scrollTop = feed.scrollHeight; // follow the live feed
    }
  } catch (e) {
    clearInterval(obPollTimer); obPollTimer = null;
    obError = e.message; obJobId = null;
    renderOnboard();
  }
}

async function obSchedule() {
  const channels = [];
  if (!$("obChEmail") || $("obChEmail").checked) channels.push("email");
  if ($("obChWechat") && $("obChWechat").checked) channels.push("wechat");
  obError = ""; obBusy = true; renderOnboard();
  try {
    const data = await api.onboardingSchedule(channels);
    obState = data;
    obScheduleDone = { job_id: data.job_id, channels: data.channels || channels };
  } catch (e) {
    obError = e.message;
  }
  obBusy = false;
  renderOnboard();
}
