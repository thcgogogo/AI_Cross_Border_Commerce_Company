// ===========================================================================
// recon.js — 对账 / 平台结算核对 (settlement reconciliation).
//
// Two surfaces in one view:
//   • /recon            the run form (pick platform + upload 平台结算单 / 银行流水 /
//                       账本) with a live progress feed, plus the 对账 history list.
//   • /recon/<runId>    the result: headline cards (应收 / 实收 / 到账 / 在途 /
//                       未解释差异), the source-verified 对账报告 with 【N】 anchors,
//                       a PDF + audit-ready Excel download, and the NON-BLOCKING
//                       exception worklist with one-tap 这是退款 / 已核对 / 挂账.
//
// Every number is the deterministic engine's; this view only displays them. Fed by
// the customer-safe /api/recon/* endpoints (no ¥ COGS, model names, or machinery).
// ===========================================================================
import * as api from "../api.js";
import { state } from "../state.js";
import { cny, esc, renderMarkdown, skeleton, statusPillHTML } from "../components.js";
import { pollJobStatus } from "../poll.js";

const CLASSIFY_OPTS = [
  { label: "refund", text: "这是退款" },
  { label: "checked", text: "已核对" },
  { label: "pending", text: "挂账" },
];

// ---------------------------------------------------------------------------
export async function mount(el, ctx) {
  if (ctx.params && ctx.params.runId) return mountResult(el, ctx, ctx.params.runId);
  return mountForm(el, ctx);
}

// ---- the run form + history -----------------------------------------------
async function mountForm(el, ctx) {
  // base path for this view's own links — "/recon" standalone, or the 跨境财务
  // sub-tab path ("/cross-border/recon") when re-homed under the hub.
  const base = (ctx && ctx.base) || "/recon";
  el.innerHTML = `<div class="page page-narrow"><div class="card">${skeleton({ title: true, lines: 4 })}</div></div>`;

  let platforms = [];
  try { platforms = (await api.getReconPlatforms()).platforms || []; } catch (e) { /* leave empty */ }
  let history = [];
  try { history = (await api.getReconHistory()).reports || []; } catch (e) { /* none yet */ }

  const opts = platforms.map((p) =>
    `<option value="${esc(p.key)}">${esc(p.display_name_cn || p.key)}</option>`).join("");
  const histHTML = history.length ? history.map((r) => `
      <a class="recon-hist-row" href="${esc(base)}/${esc(r.run_id)}" data-nav>
        <span class="rh-period">${esc(r.period_label || ("对账 #" + r.run_id))}</span>
        <span class="rh-plat">${esc(r.platform || "")}</span>
        <span class="rh-diff ${r.unexplained_count ? "warn" : "ok"}">未解释 ${esc(r.unexplained_count)} 笔</span>
        ${r.bridge_ok ? "" : `<span class="rh-bridge">桥式未平</span>`}
      </a>`).join("") : `<p class="muted">还没有对账记录。上传文件，生成第一份对账报告。</p>`;

  el.innerHTML = `<div class="page page-narrow">
    <header class="page-head">
      <h1>对账 / 平台结算核对</h1>
      <p class="muted">上传平台结算单、银行流水与账本，AI 团队会核对「钱是否都到账了，差在哪里」，并生成可溯源的对账报告。</p>
    </header>

    <form id="reconForm" class="card recon-form">
      <label class="field">
        <span class="field-label">结算平台</span>
        <select id="rfPlatform" class="field-input">${opts || `<option value="">通用 / 其他</option>`}</select>
      </label>
      <label class="field">
        <span class="field-label">对账期间（可选）</span>
        <input id="rfPeriod" class="field-input" type="text" placeholder="例如 2026-06">
      </label>
      <label class="field">
        <span class="field-label">平台结算单 <em class="req">必填</em></span>
        <input id="rfPlatformFile" class="field-input" type="file" accept=".xlsx,.xls,.csv" required>
      </label>
      <label class="field">
        <span class="field-label">银行流水 <em class="req">必填</em></span>
        <input id="rfBankFile" class="field-input" type="file" accept=".xlsx,.xls,.csv" required>
      </label>
      <label class="field">
        <span class="field-label">账本 / ERP 导出（可选）</span>
        <input id="rfLedgerFile" class="field-input" type="file" accept=".xlsx,.xls,.csv">
      </label>
      <button type="submit" id="rfSubmit" class="run-btn">开始对账</button>
      <p class="form-error" id="rfErr" hidden></p>
    </form>

    <div id="reconProgress" class="card" hidden>
      <div class="recon-prog-title">对账进行中…</div>
      <div id="reconFeed" class="recon-feed"></div>
    </div>

    <section class="recon-history">
      <h2>对账历史</h2>
      ${histHTML}
    </section>
  </div>`;

  const $ = (id) => el.querySelector("#" + id);
  let pollTimer = null;

  $("reconForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $("rfErr");
    err.hidden = true;
    const platformFile = $("rfPlatformFile").files[0];
    const bankFile = $("rfBankFile").files[0];
    const ledgerFile = $("rfLedgerFile").files[0];
    if (!platformFile || !bankFile) {
      err.hidden = false; err.textContent = "请至少上传平台结算单与银行流水。";
      return;
    }
    const fd = new FormData();
    fd.append("platform_key", $("rfPlatform").value || "");
    fd.append("period", ($("rfPeriod").value || "").trim());
    fd.append("platform_file", platformFile);
    fd.append("bank_file", bankFile);
    if (ledgerFile) fd.append("ledger_file", ledgerFile);

    const btn = $("rfSubmit");
    btn.disabled = true; btn.textContent = "上传中…";
    let res;
    try {
      res = await api.startRecon(fd);
    } catch (ex) {
      btn.disabled = false; btn.textContent = "开始对账";
      err.hidden = false; err.textContent = ex.message;
      return;
    }
    $("reconProgress").hidden = false;
    pollTimer = pollJobStatus(res.job_id, $("reconFeed"), (ok) => {
      if (ok && res.run_id) ctx.navigate(`${base}/${res.run_id}`);
      else { btn.disabled = false; btn.textContent = "重新对账"; }
    }, { errorText: "对账失败。" });
  });

  return () => { if (pollTimer) clearInterval(pollTimer); };
}

// ---- the result view ------------------------------------------------------
async function mountResult(el, ctx, runId) {
  const base = (ctx && ctx.base) || "/recon";
  el.innerHTML = `<div class="page page-narrow"><div class="card">${skeleton({ title: true, lines: 6 })}</div></div>`;

  let r;
  try { r = await api.getReconResult(runId); } catch (e) {
    el.innerHTML = `<div class="page page-narrow"><div class="empty">
        <div class="empty-icon">📄</div>
        <div class="empty-title">对账 #${esc(runId)}</div>
        <p class="empty-lead">${esc((e.data && e.data.error) || e.message)}</p>
        <button type="button" class="ghost-btn" id="rvBack">返回对账</button>
      </div></div>`;
    const b = el.querySelector("#rvBack");
    if (b) b.addEventListener("click", () => ctx.navigate(base));
    return;
  }

  const s = r.summary || {};
  const cards = [
    ["应收", s["应收"]], ["实收", s["实收"]], ["到账", s["到账"]],
    ["在途", s["在途金额"]], ["总手续费", s["总手续费"]], ["未解释差异", s["未解释差异"]],
  ].map(([k, v]) =>
    `<div class="recon-card ${k === "未解释差异" && Number(v) ? "warn" : ""}">
       <div class="rc-k">${esc(k)}</div><div class="rc-v">${esc(cny(v))}</div>
     </div>`).join("");

  const bridgeBanner = (r.bridge_ok === false)
    ? `<div class="audit-banner"><div class="ab-title">⚠️ 桥式核对未平</div>
         <p>平台结算单内部金额不一致（应收 − 优惠 ≠ 平台应结），请人工复核。</p></div>` : "";

  const attrHTML = (r.attributions || []).filter((a) => Number(a.amount)).map((a) =>
    `<li><span>${esc(a.cause)}</span><span>${esc(cny(a.amount))}（${esc(a.count)} 笔）</span></li>`).join("");

  el.innerHTML = `<div class="page page-narrow recon-result">
    <header class="page-head">
      <a class="back-link" href="${esc(base)}" data-nav>← 对账</a>
      <h1>对账报告</h1>
      <p class="muted">${esc(r.platform || "")}${r.period_label ? " · " + esc(r.period_label) : ""}
        · ${statusPillHTML(r.status)}</p>
    </header>

    <div class="recon-cards">${cards}</div>
    ${bridgeBanner}

    ${attrHTML ? `<div class="card"><h2>差异归因</h2><ul class="recon-attr">${attrHTML}</ul></div>` : ""}

    <div class="card recon-report-body">${r.final_report
      ? renderMarkdown(r.final_report, { anchors: true })
      : `<p class="muted">报告正文尚未生成。</p>`}
      ${sourcesHTML(r)}
    </div>

    <div class="report-actions">
      ${r.download_url ? `<a class="run-btn dl-btn" href="${esc(r.download_url)}">⬇ 下载 PDF</a>` : ""}
      ${r.ledger_url ? `<a class="ghost-btn" href="${esc(r.ledger_url)}">⬇ 导出对账明细 (Excel)</a>` : ""}
    </div>

    <section class="card recon-worklist" id="reconWorklist">
      <h2>异常工作清单</h2>
      <p class="muted">需要人工核查的条目。一键分类后，下次对账会自动识别同类情况。</p>
      <div id="reconExcList">${skeleton({ lines: 3 })}</div>
    </section>
  </div>`;

  // Anchor chips scroll to the source row.
  el.addEventListener("click", (e) => {
    const chip = e.target.closest(".anchor-chip");
    if (chip) {
      const row = el.querySelector(`#rsrc-${CSS.escape(String(chip.dataset.anchor))}`);
      const det = el.querySelector("#reconSources");
      if (det) det.open = true;
      if (row) {
        row.scrollIntoView({ behavior: "smooth", block: "center" });
        row.classList.add("flash");
        setTimeout(() => row.classList.remove("flash"), 1600);
      }
    }
  });

  await renderWorklist(el, runId);
}

function sourcesHTML(r) {
  const rows = r.sources || [];
  if ((r.sources_status === "ok" || rows.length) && rows.length) {
    const trs = rows.map((sr) => `
      <tr id="rsrc-${esc(sr.anchor)}">
        <td>${esc(sr.anchor != null ? `【${sr.anchor}】` : "—")}</td>
        <td>${esc(sr.claim_cn || "—")}</td>
        <td>${esc(sr.source_file || "—")}</td>
        <td>${esc(sr.source_detail || "—")}</td>
        <td>${sr.computed_by === "recon_engine" ? '<span class="verify-badge">⚙ 对账引擎</span>' : esc(sr.computed_by || "—")}</td>
      </tr>`).join("");
    return `<details class="sources-block" id="reconSources" open>
        <summary>数据来源与核验（${rows.length}）</summary>
        <div class="sources-table-wrap"><table class="sources-table">
          <thead><tr><th>锚点</th><th>数据 / 结论</th><th>来源文件</th><th>定位</th><th>核验方式</th></tr></thead>
          <tbody>${trs}</tbody>
        </table></div>
      </details>`;
  }
  return "";
}

async function renderWorklist(el, runId) {
  const host = el.querySelector("#reconExcList");
  let items = [];
  try { items = (await api.getReconExceptions({ run_id: runId })).exceptions || []; }
  catch (e) { host.innerHTML = `<p class="form-error">${esc(e.message)}</p>`; return; }

  if (!items.length) {
    host.innerHTML = `<p class="muted">没有需要处理的异常 — 本期账目核对一致 ✓</p>`;
    return;
  }
  host.innerHTML = items.map((it) => excRowHTML(it)).join("");

  host.addEventListener("click", async (e) => {
    const btn = e.target.closest(".exc-classify");
    if (!btn) return;
    const row = btn.closest(".exc-row");
    const excId = row.dataset.id;
    row.querySelectorAll(".exc-classify").forEach((b) => { b.disabled = true; });
    btn.textContent = "处理中…";
    try {
      const res = await api.classifyReconException(excId, btn.dataset.label);
      row.outerHTML = excRowHTML(res.exception, true);
    } catch (ex) {
      row.querySelectorAll(".exc-classify").forEach((b) => { b.disabled = false; });
      btn.textContent = CLASSIFY_OPTS.find((o) => o.label === btn.dataset.label).text;
      const msg = row.querySelector(".exc-msg");
      if (msg) { msg.hidden = false; msg.textContent = ex.message; }
    }
  });
}

function excRowHTML(it, justDone) {
  const done = justDone || (it.status && it.status !== "open");
  const actions = done
    ? `<span class="exc-done">已分类：${esc(it.user_label || it.cause || "—")} ✓</span>`
    : CLASSIFY_OPTS.map((o) =>
        `<button type="button" class="exc-classify ghost-btn" data-label="${esc(o.label)}">${esc(o.text)}</button>`).join("");
  return `<div class="exc-row ${done ? "done" : ""}" data-id="${esc(it.id)}">
      <div class="exc-main">
        <span class="exc-amt">${esc(cny(it.amount))}</span>
        <span class="exc-cause">${esc(it.cause || "")}</span>
        <span class="exc-layer">${esc(it.layer || "")} · ${esc(it.bucket || "")}</span>
      </div>
      <div class="exc-detail muted">${esc(it.detail || it.platform_ref || it.ledger_ref || it.bank_ref || "")}</div>
      <div class="exc-actions">${actions}</div>
      <p class="exc-msg form-error" hidden></p>
    </div>`;
}
