// ===========================================================================
// ar.js — 应收账款管理 + 催收 (AR aging + dunning).
//
// Two surfaces in one view (parallels recon.js; 对账 is untouched):
//   • /ar           the run form (pick format + upload 发票台账 / 回款记录 / 客户主数据)
//                   with a live progress feed, plus the 应收账款 history list.
//   • /ar/<runId>   the result: headline cards (应收账款总额 / 逾期总额 / DSO) + the
//                   aging buckets, the source-verified 账龄报告 with 【N】 anchors, a PDF +
//                   audit-ready Excel download, the 逾期/异常 worklist (确定/疑似) with
//                   one-tap classify, and the 催收 draft worklist (edit / approve /
//                   SEND=blocking / dismiss). Every number is the engine's; this view
//                   only displays them. Fed by the customer-safe /api/ar/* endpoints.
// ===========================================================================
import * as api from "../api.js";
import { cny, esc, renderMarkdown, skeleton, statusPillHTML } from "../components.js";
import { pollJobStatus } from "../poll.js";

const CLASSIFY_OPTS = [
  { label: "confirmed", text: "已确认" },
  { label: "paid", text: "已收款" },
  { label: "dispute", text: "争议" },
  { label: "pending", text: "挂账" },
];
const AGING_BUCKETS = ["current", "0-30", "31-60", "61-90", "90+"];
const BUCKET_CN = { current: "未到期", "0-30": "0-30 天", "31-60": "31-60 天",
  "61-90": "61-90 天", "90+": "90 天以上" };
const TIER_CN = { reminder: "提醒", urge: "催促", final_notice: "最终通知",
  handoff: "转人工/法务" };

// ---------------------------------------------------------------------------
export async function mount(el, ctx) {
  if (ctx.params && ctx.params.runId) return mountResult(el, ctx, ctx.params.runId);
  return mountForm(el, ctx);
}

// ---- the run form + history -----------------------------------------------
async function mountForm(el, ctx) {
  // base path for this view's own links — "/ar" standalone, or the 跨境财务
  // sub-tab path ("/cross-border/ar") when re-homed under the hub.
  const base = (ctx && ctx.base) || "/ar";
  el.innerHTML = `<div class="page page-narrow"><div class="card">${skeleton({ title: true, lines: 4 })}</div></div>`;

  let formats = [];
  try { formats = (await api.getArPacks()).formats || []; } catch (e) { /* leave empty */ }
  let history = [];
  try { history = (await api.getArHistory()).reports || []; } catch (e) { /* none yet */ }

  const opts = formats.map((p) =>
    `<option value="${esc(p.key)}">${esc(p.display_name_cn || p.key)}</option>`).join("");
  const histHTML = history.length ? history.map((r) => `
      <a class="recon-hist-row" href="${esc(base)}/${esc(r.run_id)}" data-nav>
        <span class="rh-period">${esc(r.period_label || ("应收 #" + r.run_id))}</span>
        <span class="rh-plat">逾期 ${esc(cny(r.overdue_total))}</span>
        <span class="rh-diff ${r.suspect_count ? "warn" : "ok"}">待核查 ${esc(r.suspect_count)} 项</span>
      </a>`).join("") : `<p class="muted">还没有应收核算记录。上传发票与回款，生成第一份账龄报告。</p>`;

  el.innerHTML = `<div class="page page-narrow">
    <header class="page-head">
      <h1>应收账款管理 + 催收</h1>
      <p class="muted">上传发票台账、回款记录与客户主数据，AI 团队会算清「谁欠多少、哪些逾期」，
        生成可溯源的账龄报告，并为逾期客户起草可发送的催收消息（需人工确认后才会发送）。</p>
    </header>

    <form id="arForm" class="card recon-form">
      <label class="field">
        <span class="field-label">应收格式</span>
        <select id="afFormat" class="field-input">${opts || `<option value="">通用应收台账</option>`}</select>
      </label>
      <label class="field">
        <span class="field-label">核算期间（可选）</span>
        <input id="afPeriod" class="field-input" type="text" placeholder="例如 2026-06">
      </label>
      <label class="field">
        <span class="field-label">应收发票台账 <em class="req">必填</em></span>
        <input id="afInvFile" class="field-input" type="file" accept=".xlsx,.xls,.csv" required>
      </label>
      <label class="field">
        <span class="field-label">回款记录 <em class="req">必填</em></span>
        <input id="afPayFile" class="field-input" type="file" accept=".xlsx,.xls,.csv" required>
      </label>
      <label class="field">
        <span class="field-label">客户主数据（账期 / 信用额度 / 联系方式，可选）</span>
        <input id="afCusFile" class="field-input" type="file" accept=".xlsx,.xls,.csv">
      </label>
      <button type="submit" id="afSubmit" class="run-btn">开始核算应收</button>
      <p class="form-error" id="afErr" hidden></p>
    </form>

    <div id="arProgress" class="card" hidden>
      <div class="recon-prog-title">应收核算进行中…</div>
      <div id="arFeed" class="recon-feed"></div>
    </div>

    <section class="recon-history">
      <h2>应收核算历史</h2>
      ${histHTML}
    </section>
  </div>`;

  const $ = (id) => el.querySelector("#" + id);
  let pollTimer = null;

  $("arForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $("afErr");
    err.hidden = true;
    const invFile = $("afInvFile").files[0];
    const payFile = $("afPayFile").files[0];
    const cusFile = $("afCusFile").files[0];
    if (!invFile || !payFile) {
      err.hidden = false; err.textContent = "请至少上传发票台账与回款记录。";
      return;
    }
    const fd = new FormData();
    fd.append("format_key", $("afFormat").value || "");
    fd.append("period", ($("afPeriod").value || "").trim());
    fd.append("invoices_file", invFile);
    fd.append("payments_file", payFile);
    if (cusFile) fd.append("customers_file", cusFile);

    const btn = $("afSubmit");
    btn.disabled = true; btn.textContent = "上传中…";
    let res;
    try {
      res = await api.startAr(fd);
    } catch (ex) {
      btn.disabled = false; btn.textContent = "开始核算应收";
      err.hidden = false; err.textContent = ex.message;
      return;
    }
    $("arProgress").hidden = false;
    pollTimer = pollJobStatus(res.job_id, $("arFeed"), (ok) => {
      if (ok && res.run_id) ctx.navigate(`${base}/${res.run_id}`);
      else { btn.disabled = false; btn.textContent = "重新核算"; }
    }, { errorText: "应收核算失败。" });
  });

  return () => { if (pollTimer) clearInterval(pollTimer); };
}

// ---- the result view ------------------------------------------------------
async function mountResult(el, ctx, runId) {
  const base = (ctx && ctx.base) || "/ar";
  el.innerHTML = `<div class="page page-narrow"><div class="card">${skeleton({ title: true, lines: 6 })}</div></div>`;

  let r;
  try { r = await api.getArResult(runId); } catch (e) {
    el.innerHTML = `<div class="page page-narrow"><div class="empty">
        <div class="empty-icon">📄</div>
        <div class="empty-title">应收 #${esc(runId)}</div>
        <p class="empty-lead">${esc((e.data && e.data.error) || e.message)}</p>
        <button type="button" class="ghost-btn" id="avBack">返回应收</button>
      </div></div>`;
    const b = el.querySelector("#avBack");
    if (b) b.addEventListener("click", () => ctx.navigate(base));
    return;
  }

  const s = r.summary || {};
  const dso = (s["DSO"] == null) ? "数据不足" : `${s["DSO"]} 天`;
  const cards = [
    ["应收账款总额", cny(s["应收账款总额"]), false],
    ["逾期总额", cny(s["逾期总额"]), Number(s["逾期总额"]) > 0],
    ["DSO", dso, false],
    ["待人工核查", `${esc(s["疑似条目数"] || 0)} 项`, Number(s["疑似条目数"]) > 0],
  ].map(([k, v, warn]) =>
    `<div class="recon-card ${warn ? "warn" : ""}">
       <div class="rc-k">${esc(k)}</div><div class="rc-v">${v}</div>
     </div>`).join("");

  const agingRow = AGING_BUCKETS.map((b) =>
    `<div class="ar-bucket"><div class="ab-k">${esc(BUCKET_CN[b])}</div>
       <div class="ab-v">${esc(cny(s[b]))}</div></div>`).join("");

  const bridgeBanner = (r.bridge_ok === false)
    ? `<div class="audit-banner"><div class="ab-title">⚠️ 应收核算未平</div>
         <p>应收账款总额与「已核销 + 未结」不一致，请人工复核数据源。</p></div>` : "";

  el.innerHTML = `<div class="page page-narrow recon-result">
    <header class="page-head">
      <a class="back-link" href="${esc(base)}" data-nav>← 应收账款</a>
      <h1>应收账款账龄报告</h1>
      <p class="muted">${r.period_label ? esc(r.period_label) + " · " : ""}${statusPillHTML(r.status)}</p>
    </header>

    <div class="recon-cards">${cards}</div>
    <div class="card ar-aging-row">${agingRow}</div>
    ${bridgeBanner}

    <div class="card recon-report-body">${r.final_report
      ? renderMarkdown(r.final_report, { anchors: true })
      : `<p class="muted">报告正文尚未生成。</p>`}
      ${sourcesHTML(r)}
    </div>

    <div class="report-actions">
      ${r.download_url ? `<a class="run-btn dl-btn" href="${esc(r.download_url)}">⬇ 下载 PDF</a>` : ""}
      ${r.ledger_url ? `<a class="ghost-btn" href="${esc(r.ledger_url)}">⬇ 导出应收明细 (Excel)</a>` : ""}
    </div>

    <section class="card recon-worklist">
      <h2>逾期 / 异常工作清单</h2>
      <p class="muted">确定 = 可直接催收；疑似 = 数据存疑，请先核实。一键分类后，下次核算会自动识别同类情况。</p>
      <div id="arFlagList">${skeleton({ lines: 3 })}</div>
    </section>

    <section class="card recon-worklist">
      <h2>催收草稿</h2>
      <p class="muted">系统已按账龄与金额自动判定催收级别，并注入真实金额/到期日。
        每封都需你审核、确认后才会发送 — 系统绝不自动发送。</p>
      <div id="arDunList">${skeleton({ lines: 3 })}</div>
    </section>
  </div>`;

  // Anchor chips scroll to the source row.
  el.addEventListener("click", (e) => {
    const chip = e.target.closest(".anchor-chip");
    if (chip) {
      const row = el.querySelector(`#asrc-${CSS.escape(String(chip.dataset.anchor))}`);
      const det = el.querySelector("#arSources");
      if (det) det.open = true;
      if (row) {
        row.scrollIntoView({ behavior: "smooth", block: "center" });
        row.classList.add("flash");
        setTimeout(() => row.classList.remove("flash"), 1600);
      }
    }
  });

  await renderFlags(el, runId);
  await renderDunning(el, runId);
}

function sourcesHTML(r) {
  const rows = r.sources || [];
  if (rows.length) {
    const trs = rows.map((sr) => `
      <tr id="asrc-${esc(sr.anchor)}">
        <td>${esc(sr.anchor != null ? `【${sr.anchor}】` : "—")}</td>
        <td>${esc(sr.claim_cn || "—")}</td>
        <td>${esc(sr.source_file || "—")}</td>
        <td>${esc(sr.source_detail || "—")}</td>
        <td>${sr.computed_by === "ar_aging_engine" ? '<span class="verify-badge">⚙ 应收引擎</span>' : esc(sr.computed_by || "—")}</td>
      </tr>`).join("");
    return `<details class="sources-block" id="arSources" open>
        <summary>数据来源与核验（${rows.length}）</summary>
        <div class="sources-table-wrap"><table class="sources-table">
          <thead><tr><th>锚点</th><th>数据 / 结论</th><th>来源文件</th><th>定位</th><th>核验方式</th></tr></thead>
          <tbody>${trs}</tbody>
        </table></div>
      </details>`;
  }
  return "";
}

// ---- 逾期 / 异常 worklist --------------------------------------------------
async function renderFlags(el, runId) {
  const host = el.querySelector("#arFlagList");
  let items = [];
  try { items = (await api.getArFlags({ run_id: runId })).flags || []; }
  catch (e) { host.innerHTML = `<p class="form-error">${esc(e.message)}</p>`; return; }

  if (!items.length) {
    host.innerHTML = `<p class="muted">没有逾期或需要处理的条目 ✓</p>`;
    return;
  }
  const suspect = items.filter((it) => it.grade === "疑似");
  const confirmed = items.filter((it) => it.grade !== "疑似");
  host.innerHTML =
    (suspect.length ? `<h3 class="ar-grp warn">疑似（请先核实）</h3>` + suspect.map(flagRowHTML).join("") : "") +
    (confirmed.length ? `<h3 class="ar-grp">确定逾期</h3>` + confirmed.map(flagRowHTML).join("") : "");

  host.addEventListener("click", async (e) => {
    const btn = e.target.closest(".exc-classify");
    if (!btn) return;
    const row = btn.closest(".exc-row");
    const flagId = row.dataset.id;
    row.querySelectorAll(".exc-classify").forEach((b) => { b.disabled = true; });
    btn.textContent = "处理中…";
    try {
      const res = await api.classifyArFlag(flagId, btn.dataset.label);
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
  const reasons = (it.suspect_reasons || []).join("、");
  return `<div class="exc-row ${done ? "done" : ""} ${it.grade === "疑似" ? "suspect" : ""}" data-id="${esc(it.id)}">
      <div class="exc-main">
        <span class="exc-amt">${esc(cny(it.amount))}</span>
        <span class="exc-cause">${esc(it.cause || "")}</span>
        <span class="exc-layer">${esc(it.bucket || "")}${reasons ? " · " + esc(reasons) : ""}</span>
      </div>
      <div class="exc-detail muted">${esc(it.detail || it.invoice_ref || it.customer_ref || "")}</div>
      <div class="exc-actions">${actions}</div>
      <p class="exc-msg form-error" hidden></p>
    </div>`;
}

// ---- 催收 draft worklist ---------------------------------------------------
async function renderDunning(el, runId) {
  const host = el.querySelector("#arDunList");
  let msgs = [];
  try { msgs = (await api.getArDunning({ run_id: runId })).messages || []; }
  catch (e) { host.innerHTML = `<p class="form-error">${esc(e.message)}</p>`; return; }

  if (!msgs.length) {
    host.innerHTML = `<p class="muted">本期暂无需催收的客户。</p>`;
    return;
  }
  host.innerHTML = msgs.map(dunCardHTML).join("");

  host.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const card = btn.closest(".dun-card");
    const id = card.dataset.id;
    const act = btn.dataset.act;
    const errEl = card.querySelector(".dun-err");
    errEl.hidden = true;
    try {
      if (act === "edit") {
        const ta = card.querySelector(".dun-body");
        ta.removeAttribute("readonly");
        ta.focus();
        btn.dataset.act = "save"; btn.textContent = "保存";
      } else if (act === "save") {
        const body = card.querySelector(".dun-body").value;
        await api.editArDunning(id, body);
        await reloadDunning(el, runId);
      } else if (act === "approve") {
        await api.approveArDunning(id);
        await reloadDunning(el, runId);
      } else if (act === "dismiss") {
        await api.dismissArDunning(id);
        await reloadDunning(el, runId);
      } else if (act === "send") {
        const channel = card.querySelector(".dun-channel").value;
        const to = (card.querySelector(".dun-to").value || "").trim();
        if (channel === "email" && !to) { throw new Error("请填写收件人邮箱。"); }
        await api.sendArDunning(id, { channel, to });
        await reloadDunning(el, runId);
      }
    } catch (ex) {
      errEl.hidden = false; errEl.textContent = ex.message;
    }
  });
}

async function reloadDunning(el, runId) {
  const host = el.querySelector("#arDunList");
  host.innerHTML = skeleton({ lines: 2 });
  // replace the listener-bearing node to avoid stacking listeners
  const fresh = host.cloneNode(false);
  host.replaceWith(fresh);
  fresh.id = "arDunList";
  await renderDunning(el, runId);
}

function dunCardHTML(m) {
  const tier = TIER_CN[m.tier] || m.tier;
  if (m.tier === "handoff") {
    return `<div class="dun-card handoff" data-id="${esc(m.id)}">
        <div class="dun-head"><span class="dun-tier legal">转人工 / 法务</span>
          <span class="dun-cust">${esc(m.customer_key)}</span>
          <span class="dun-amt">${esc(cny(m.amount))} · 逾期 ${esc(m.days_overdue)} 天</span></div>
        <p class="muted">该客户逾期严重或数据存疑，已转人工/法务处理，不自动起草催收消息。</p>
      </div>`;
  }
  const sent = m.state === "sent";
  const dismissed = m.state === "dismissed";
  const body = esc(m.body || "");
  const warn = m.audit_warning
    ? `<div class="audit-banner small"><div class="ab-title">⚠️ 自动核验提示（仅审核可见）</div>
         <pre class="dun-audit">${esc(m.audit_warning)}</pre></div>` : "";
  const stateBadge = sent ? `<span class="dun-state sent">已发送</span>`
    : dismissed ? `<span class="dun-state">已忽略</span>`
    : m.state === "approved" ? `<span class="dun-state ok">已批准</span>`
    : m.state === "edited" ? `<span class="dun-state">已编辑</span>` : "";
  const actions = (sent || dismissed) ? "" : `
      <div class="dun-actions">
        <button type="button" class="ghost-btn" data-act="edit">编辑</button>
        <button type="button" class="ghost-btn" data-act="approve">批准</button>
        <button type="button" class="ghost-btn" data-act="dismiss">忽略</button>
      </div>
      <div class="dun-send">
        <select class="dun-channel field-input">
          <option value="email">邮件发送给客户</option>
          <option value="wechat">企业微信（转内部群）</option>
        </select>
        <input class="dun-to field-input" type="email" placeholder="收件人邮箱">
        <button type="button" class="run-btn" data-act="send">确认发送</button>
      </div>`;
  return `<div class="dun-card ${m.grade === "疑似" ? "suspect" : ""}" data-id="${esc(m.id)}">
      <div class="dun-head">
        <span class="dun-tier ${esc(m.tier)}">${esc(tier)}</span>
        <span class="dun-cust">${esc(m.customer_key)}</span>
        <span class="dun-amt">${esc(cny(m.amount))} · 逾期 ${esc(m.days_overdue)} 天</span>
        ${m.grade === "疑似" ? '<span class="dun-state warn">疑似</span>' : ""}
        ${stateBadge}
      </div>
      ${warn}
      <textarea class="dun-body field-input" rows="5" readonly>${body}</textarea>
      ${actions}
      <p class="dun-err form-error" hidden></p>
    </div>`;
}
