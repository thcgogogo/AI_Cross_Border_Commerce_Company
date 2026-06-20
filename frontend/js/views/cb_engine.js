// ===========================================================================
// cb_engine.js — generic sub-view for the 跨境财务 evidence engines.
//
// ONE config-driven view behind the 三流合一 / 退税核算 / 发票审核 / 无票免税 sub-tabs.
// It fetches the kind's form/render config from /api/crossborder/<kind>/config, renders
// an upload form (one input per document role) + history, runs the engine, polls the
// live feed, and shows the result: summary cards + the deterministic report (with 【N】
// anchors + 数据来源与核验) + the 待人工核查 worklist. Every number is the engine's; the
// customer-safe /api/crossborder/* endpoints expose no models/tokens/¥.
// ===========================================================================
import * as api from "../api.js";
import { cny, esc, renderMarkdown, skeleton, statusPillHTML } from "../components.js";
import { pollJobStatus } from "../poll.js";
import { cronLabel } from "./schedule.js";

const CLASSIFY_OPTS = [
  { label: "已核实", text: "已核实" },
  { label: "无需处理", text: "无需处理" },
  { label: "待处理", text: "待处理" },
  { label: "转人工", text: "转人工核查" },
];

// money-ish summary keys render as ¥; everything else (counts) as a plain number.
const isMoneyKey = (k) => /额|税|所得/.test(k) && !/笔数|数$/.test(k);

// ---------------------------------------------------------------------------
export async function mount(el, ctx, kind) {
  if (ctx.params && ctx.params.runId) return mountResult(el, ctx, kind, ctx.params.runId);
  return mountForm(el, ctx, kind);
}

// ---- run form + history ----------------------------------------------------
async function mountForm(el, ctx, kind) {
  const base = (ctx && ctx.base) || `/cross-border/${kind}`;
  el.innerHTML = `<div class="page page-narrow"><div class="card">${skeleton({ title: true, lines: 4 })}</div></div>`;

  let cfg = null;
  try { cfg = await api.getCbConfig(kind); }
  catch (e) {
    el.innerHTML = `<div class="page page-narrow"><div class="empty">
        <div class="empty-icon">📄</div>
        <div class="empty-title">${esc(kind)}</div>
        <p class="empty-lead">${esc(e.message)}</p></div></div>`;
    return;
  }
  let history = [];
  try { history = (await api.getCbHistory(kind)).reports || []; } catch (e) { /* none */ }

  const inputs = cfg.roles.map((r) => `
      <label class="field">
        <span class="field-label">${esc(r.label)} ${r.required ? '<em class="req">必填</em>' : "<span class=\"muted\">（可选）</span>"}</span>
        <input class="field-input cbf-file" type="file" data-field="${esc(r.field)}"
               accept=".xlsx,.xls,.csv" ${r.required ? "required" : ""}>
      </label>`).join("");

  const histHTML = history.length ? history.map((r) => `
      <a class="recon-hist-row" href="${esc(base)}/${esc(r.run_id)}" data-nav>
        <span class="rh-period">${esc(r.period_label || (cfg.label + " #" + r.run_id))}</span>
        <span class="rh-diff ${r.exception_count ? "warn" : "ok"}">待核查 ${esc(r.exception_count)} 项</span>
        ${r.bridge_ok ? "" : `<span class="rh-bridge">自检未平</span>`}
      </a>`).join("") : `<p class="muted">还没有核验记录。上传单证，生成第一份报告。</p>`;

  el.innerHTML = `<div class="page page-narrow">
    <header class="page-head">
      <h1>${esc(cfg.label)}</h1>
      <p class="muted">上传相关单证，系统按确定性规则交叉核验并生成可溯源报告。
        本工具仅整理与提示合规证据，<strong>不替代申报/报税</strong>，税务口径以主管税务机关为准。</p>
    </header>

    <form id="cbForm" class="card recon-form">
      <label class="field">
        <span class="field-label">核验期间（可选）</span>
        <input id="cbPeriod" class="field-input" type="text" placeholder="例如 2026-06">
      </label>
      ${inputs}
      <button type="submit" id="cbSubmit" class="run-btn">开始核验</button>
      <p class="form-error" id="cbErr" hidden></p>
    </form>

    <div id="cbProgress" class="card" hidden>
      <div class="recon-prog-title">核验进行中…</div>
      <div id="cbFeed" class="recon-feed"></div>
    </div>

    <section class="recon-history">
      <h2>核验历史</h2>
      ${histHTML}
    </section>

    <section class="card cbf-sched">
      <h2>定时自动核验并发送</h2>
      <p class="muted">用上面选好的文件，按周期自动核验并把报告送达邮箱 / 企业微信 —
        企业版专属，催收类不在此列（绝不自动发送给客户）。</p>
      <div class="cbf-sched-grid">
        <label class="field"><span class="field-label">任务名称</span>
          <input id="cbSchName" class="field-input" type="text" value="${esc(cfg.label)}"></label>
        <label class="field"><span class="field-label">频率</span>
          <select id="cbSchFreq" class="field-input">
            <option value="weekly">每周一 08:00</option>
            <option value="monthly">每月 1 日 09:00</option>
            <option value="cron">自定义 (cron)</option>
          </select></label>
        <label class="field" id="cbSchCronWrap" hidden><span class="field-label">cron 表达式</span>
          <input id="cbSchCron" class="field-input" type="text" placeholder="0 8 * * mon"></label>
      </div>
      <div class="cbf-sched-chans">
        <label><input type="checkbox" class="cbSchChan" value="email" checked> 邮件</label>
        <label><input type="checkbox" class="cbSchChan" value="wechat"> 企业微信</label>
      </div>
      <button type="button" id="cbSchCreate" class="ghost-btn">创建定时任务</button>
      <p class="form-msg" id="cbSchMsg" hidden></p>
      <div id="cbSchList" class="cbf-sched-list">${skeleton({ lines: 1 })}</div>
    </section>
  </div>`;

  const $ = (id) => el.querySelector("#" + id);
  let pollTimer = null;

  $("cbForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $("cbErr");
    err.hidden = true;
    const fd = new FormData();
    fd.append("period", ($("cbPeriod").value || "").trim());
    let missing = null;
    for (const inp of el.querySelectorAll(".cbf-file")) {
      const f = inp.files[0];
      if (f) fd.append(inp.dataset.field, f);
      else if (inp.required) {
        const role = cfg.roles.find((r) => r.field === inp.dataset.field);
        missing = role ? role.label : "必填文件";
      }
    }
    if (missing) { err.hidden = false; err.textContent = `请上传${missing}文件。`; return; }

    const btn = $("cbSubmit");
    btn.disabled = true; btn.textContent = "上传中…";
    let res;
    try { res = await api.startCb(kind, fd); }
    catch (ex) {
      btn.disabled = false; btn.textContent = "开始核验";
      err.hidden = false; err.textContent = ex.message;
      return;
    }
    $("cbProgress").hidden = false;
    pollTimer = pollJobStatus(res.job_id, $("cbFeed"), (ok) => {
      if (ok && res.run_id) ctx.navigate(`${base}/${res.run_id}`);
      else { btn.disabled = false; btn.textContent = "重新核验"; }
    }, { errorText: "核验失败。" });
  });

  // ---- 定时任务 (auto-deliver this engine on cadence) ----
  $("cbSchFreq").addEventListener("change", () => {
    $("cbSchCronWrap").hidden = $("cbSchFreq").value !== "cron";
  });
  const composeCron = () => {
    const f = $("cbSchFreq").value;
    if (f === "weekly") return "0 8 * * mon";
    if (f === "monthly") return "0 9 1 * *";
    return ($("cbSchCron").value || "").trim();
  };
  const schedRowHTML = (j) => {
    const chans = (j.delivery_channels || []).join(" + ") || "未设置渠道";
    return `<div class="cbf-sched-job" data-id="${esc(j.id)}" data-active="${j.active}">
        <div class="cbf-sched-job-txt"><strong>${esc(j.name_cn)}</strong>
          <span class="muted">${esc(cronLabel(j.cron_expr))} · ${esc(chans)}${j.active ? "" : " · 已暂停"}</span></div>
        <div class="cbf-sched-acts">
          <button type="button" class="ghost-btn" data-act="toggle">${j.active ? "暂停" : "启用"}</button>
          <button type="button" class="ghost-btn" data-act="del">删除</button>
        </div>
      </div>`;
  };
  async function loadSchedules() {
    const host = $("cbSchList");
    let jobs = [];
    try { jobs = (await api.getScheduledJobs()).jobs || []; } catch (e) { host.innerHTML = ""; return; }
    const mine = jobs.filter((j) => j.engine_kind === kind);
    host.innerHTML = mine.length ? mine.map(schedRowHTML).join("")
      : `<p class="muted">还没有该核验的定时任务。</p>`;
  }
  $("cbSchList").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const row = btn.closest(".cbf-sched-job");
    const id = row.dataset.id;
    try {
      if (btn.dataset.act === "del") await api.deleteScheduledJob(id);
      else await api.updateScheduledJob({ id, active: row.dataset.active !== "true" });
    } catch (ex) { /* best-effort; reload reflects truth */ }
    loadSchedules();
  });
  $("cbSchCreate").addEventListener("click", async () => {
    const msg = $("cbSchMsg");
    msg.hidden = true; msg.classList.remove("ok");
    const cron = composeCron();
    if (!cron) { msg.hidden = false; msg.textContent = "请填写频率（cron）。"; return; }
    const chans = [...el.querySelectorAll(".cbSchChan:checked")].map((c) => c.value);
    if (!chans.length) { msg.hidden = false; msg.textContent = "请至少选择一个送达渠道。"; return; }
    const fd = new FormData();
    fd.append("name", ($("cbSchName").value || cfg.label).trim());
    fd.append("cron", cron);
    fd.append("channels", chans.join(","));
    let missing = null;
    for (const inp of el.querySelectorAll(".cbf-file")) {
      const f = inp.files[0];
      if (f) fd.append(inp.dataset.field, f);
      else if (inp.required) {
        const role = cfg.roles.find((r) => r.field === inp.dataset.field);
        missing = role ? role.label : "必填文件";
      }
    }
    if (missing) { msg.hidden = false; msg.textContent = `请先在上方选择${missing}文件。`; return; }
    const btn = $("cbSchCreate");
    btn.disabled = true; btn.textContent = "创建中…";
    try {
      await api.scheduleCb(kind, fd);
      await loadSchedules();
      msg.hidden = false; msg.classList.add("ok"); msg.textContent = "定时任务已创建 ✓";
    } catch (ex) {
      msg.hidden = false; msg.textContent = ex.message;
    }
    btn.disabled = false; btn.textContent = "创建定时任务";
  });
  loadSchedules();

  return () => { if (pollTimer) clearInterval(pollTimer); };
}

// ---- result view -----------------------------------------------------------
async function mountResult(el, ctx, kind, runId) {
  const base = (ctx && ctx.base) || `/cross-border/${kind}`;
  el.innerHTML = `<div class="page page-narrow"><div class="card">${skeleton({ title: true, lines: 6 })}</div></div>`;

  let r;
  try { r = await api.getCbResult(kind, runId); }
  catch (e) {
    el.innerHTML = `<div class="page page-narrow"><div class="empty">
        <div class="empty-icon">📄</div><div class="empty-title">核验 #${esc(runId)}</div>
        <p class="empty-lead">${esc((e.data && e.data.error) || e.message)}</p>
        <button type="button" class="ghost-btn" id="cbBack">返回</button>
      </div></div>`;
    const b = el.querySelector("#cbBack");
    if (b) b.addEventListener("click", () => ctx.navigate(base));
    return;
  }

  const s = r.summary || {};
  const cards = (r.summary_cards || []).map((c) => {
    const v = s[c.key];
    const shown = (v == null) ? "—" : (isMoneyKey(c.key) ? cny(v) : esc(v));
    return `<div class="recon-card ${c.warn && Number(v) > 0 ? "warn" : ""}">
       <div class="rc-k">${esc(c.label)}</div><div class="rc-v">${shown}</div>
     </div>`;
  }).join("");

  const upstream = (s["上游带入"] || "").trim();
  const upstreamHint = upstream
    ? `<p class="cbf-upstream muted">↪ 已自动带入上游数据：${esc(upstream)}（无需重复上传）</p>` : "";

  const bridgeBanner = (r.bridge_ok === false)
    ? `<div class="audit-banner"><div class="ab-title">⚠️ 自检未平</div>
         <p>系统自检发现汇总金额不一致，请人工复核数据源。</p></div>` : "";

  el.innerHTML = `<div class="page page-narrow recon-result">
    <header class="page-head">
      <a class="back-link" href="${esc(base)}" data-nav>← ${esc(r.label || "核验")}</a>
      <h1>${esc(r.label || "核验报告")}</h1>
      <p class="muted">${r.period_label ? esc(r.period_label) + " · " : ""}${statusPillHTML(r.status)}</p>
    </header>

    <div class="recon-cards">${cards}</div>
    ${upstreamHint}
    ${bridgeBanner}

    <div class="card recon-report-body">${r.final_report
      ? renderMarkdown(r.final_report, { anchors: true })
      : `<p class="muted">报告正文尚未生成。</p>`}
      ${sourcesHTML(r)}
    </div>

    <div class="report-actions">
      ${r.download_url ? `<a class="run-btn dl-btn" href="${esc(r.download_url)}">⬇ 下载 PDF</a>` : ""}
    </div>

    <section class="card recon-worklist">
      <h2>待人工核查</h2>
      <p class="muted">系统标记的不一致/异常条目。请逐条核实后分类。</p>
      <div id="cbFlagList">${skeleton({ lines: 3 })}</div>
    </section>
  </div>`;

  el.addEventListener("click", (e) => {
    const chip = e.target.closest(".anchor-chip");
    if (chip) {
      const row = el.querySelector(`#csrc-${CSS.escape(String(chip.dataset.anchor))}`);
      const det = el.querySelector("#cbSources");
      if (det) det.open = true;
      if (row) {
        row.scrollIntoView({ behavior: "smooth", block: "center" });
        row.classList.add("flash");
        setTimeout(() => row.classList.remove("flash"), 1600);
      }
    }
  });

  await renderFlags(el, kind, runId);
}

function sourcesHTML(r) {
  const rows = r.sources || [];
  if (!rows.length) return "";
  const trs = rows.map((sr) => `
      <tr id="csrc-${esc(sr.anchor)}">
        <td>${esc(sr.anchor != null ? `【${sr.anchor}】` : "—")}</td>
        <td>${esc(sr.claim_cn || "—")}</td>
        <td>${esc(sr.source_file || "—")}</td>
        <td>${esc(sr.source_detail || "—")}</td>
        <td>${/_engine$/.test(sr.computed_by || "") ? '<span class="verify-badge">⚙ 系统引擎</span>' : esc(sr.computed_by || "—")}</td>
      </tr>`).join("");
  return `<details class="sources-block" id="cbSources" open>
      <summary>数据来源与核验（${rows.length}）</summary>
      <div class="sources-table-wrap"><table class="sources-table">
        <thead><tr><th>锚点</th><th>数据 / 结论</th><th>来源文件</th><th>定位</th><th>核验方式</th></tr></thead>
        <tbody>${trs}</tbody>
      </table></div>
    </details>`;
}

async function renderFlags(el, kind, runId) {
  const host = el.querySelector("#cbFlagList");
  let items = [];
  try { items = (await api.getCbFlags(kind, { run_id: runId })).flags || []; }
  catch (e) { host.innerHTML = `<p class="form-error">${esc(e.message)}</p>`; return; }

  if (!items.length) {
    host.innerHTML = `<p class="muted">本期核验一致，无需人工处理 ✓</p>`;
    return;
  }
  host.innerHTML = items.map(flagRowHTML).join("");

  host.addEventListener("click", async (e) => {
    const btn = e.target.closest(".exc-classify");
    if (!btn) return;
    const row = btn.closest(".exc-row");
    const flagId = row.dataset.id;
    row.querySelectorAll(".exc-classify").forEach((b) => { b.disabled = true; });
    btn.textContent = "处理中…";
    try {
      const res = await api.classifyCbFlag(flagId, btn.dataset.label);
      row.outerHTML = flagRowHTML(res.flag, true);
    } catch (ex) {
      row.querySelectorAll(".exc-classify").forEach((b) => { b.disabled = false; });
      btn.textContent = (CLASSIFY_OPTS.find((o) => o.label === btn.dataset.label) || {}).text || "分类";
      const msg = row.querySelector(".exc-msg");
      if (msg) { msg.hidden = false; msg.textContent = ex.message; }
    }
  });
}

function flagRowHTML(it, justDone) {
  const done = justDone || (it.status && it.status !== "open");
  const actions = done
    ? `<span class="exc-done">已分类：${esc(it.user_label || it.cause || "—")} ✓</span>`
    : CLASSIFY_OPTS.map((o) =>
        `<button type="button" class="exc-classify ghost-btn" data-label="${esc(o.label)}">${esc(o.text)}</button>`).join("");
  const sev = it.severity ? `<span class="exc-sev sev-${esc(it.severity)}">${esc(it.severity)}</span>` : "";
  return `<div class="exc-row ${done ? "done" : ""} ${it.severity === "高" ? "suspect" : ""}" data-id="${esc(it.id)}">
      <div class="exc-main">
        ${it.amount ? `<span class="exc-amt">${esc(cny(it.amount))}</span>` : ""}
        <span class="exc-cause">${esc(it.cause || "")}</span>
        ${sev}
        ${it.ref ? `<span class="exc-layer">单据 ${esc(it.ref)}</span>` : ""}
      </div>
      <div class="exc-detail muted">${esc(it.detail || "")}</div>
      <div class="exc-actions">${actions}</div>
      <p class="exc-msg form-error" hidden></p>
    </div>`;
}
