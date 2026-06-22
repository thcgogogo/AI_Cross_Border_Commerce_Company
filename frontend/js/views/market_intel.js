// ===========================================================================
// market_intel.js — 跨境市场情报日报 (daily cross-border market-intelligence report).
//
// Its OWN 工作流 section (a dedicated 跨境电商 product), mounted standalone by main.js.
// Two surfaces:
//   • /markets          a DASHBOARD (mirrors the 工作台 layout): a hero + cards
//                       (下次送达 / 今日日报 / 关注列表 / 订阅 / 情报范围) + an inline
//                       关注列表 editor. A 企业版 subscription feature — every morning
//                       08:00 the report auto-delivers; «立即生成» renders it on demand.
//   • /markets/<runId>  the report: the 8-section structure (抬头 / 今日要点 / 截止日期 /
//                       政策合规 / 成本利润 / 需求机会 / 竞争态势 / 数据来源) with a section
//                       jump-nav, 【N】 anchors, the two tables, and a branded-PDF download.
//
// Every item is the deterministic engine's, already recency-gated server-side. Customer-safe
// /api/market-intel/* endpoints only (no ¥/model/token).
// ===========================================================================
import * as api from "../api.js";
import { state } from "../state.js";
import { esc, pageHead, quotaMeter, renderMarkdown, skeleton, statusPillHTML } from "../components.js";
import { pollJobStatus } from "../poll.js";

const BASE = "/markets";
const LIST_FIELDS = [
  ["platforms", "销售平台", "例如 amazon, temu, tiktok"],
  ["markets", "目标市场 / 国家", "例如 US, DE, UK"],
  ["competitors", "竞品 / 品牌（可选）", "例如 Anker, 某品牌"],
  ["keywords", "关键词（可选）", "例如 air fryer, 智能家居"],
];
// The 8 report sections (picture-1 structure) — shown in 情报范围 + the result jump-nav.
const SECTIONS = ["抬头", "今日要点", "截止日期 / 时间敏感", "政策与合规",
  "成本与利润", "需求与机会", "竞争态势", "数据来源与核验"];

// Tinted line-icon chips (Claude Design); keyed by the emoji the call sites pass.
const CARD_ICONS = {
  "⏰": `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="12" cy="13" r="8"/><path d="M12 9v4l2.5 2"/><path d="M5 3 2 6M19 3l3 3"/></svg>`,
  "📄": `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h4"/></svg>`,
  "🎯": `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.3" fill="currentColor"/></svg>`,
  "📊": `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg>`,
  "🧭": `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="m15 9-2.5 5.5L7 17l2.5-5.5z"/></svg>`,
};
const cardTitle = (icon, label) =>
  `<h3 class="card-title"><span class="ct-ico" aria-hidden="true">${CARD_ICONS[icon] || icon}</span> ${label}</h3>`;

// ---------------------------------------------------------------------------
export async function mount(el, ctx) {
  if (ctx.params && ctx.params.settings) return mountSettings(el, ctx);
  if (ctx.params && ctx.params.runId) return mountResult(el, ctx, ctx.params.runId);
  return mountDashboard(el, ctx);
}

// ---- dashboard (picture-2 layout) -----------------------------------------
async function mountDashboard(el, ctx) {
  const name = (state.account && state.account.name) || "";
  el.innerHTML = `<div class="page">
    <div class="wb-hero">
      ${pageHead(`你好，${esc(name)}`, "你的跨境市场情报雷达 · 每天早上 8 点自动送达。")}
      <button type="button" class="run-btn" id="miGen">立即生成今日日报</button>
    </div>
    <div class="card-grid">
      <div class="card" id="miNext">${cardTitle("⏰", "下次送达")}${skeleton({ lines: 2 })}</div>
      <div class="card" id="miLatest">${cardTitle("📄", "今日日报")}${skeleton({ lines: 2 })}</div>
      <div class="card" id="miWatch">${cardTitle("🎯", "关注列表")}${skeleton({ lines: 2 })}</div>
      <div class="card" id="miPlan">${cardTitle("📊", "订阅")}${skeleton({ lines: 2 })}</div>
      <div class="card" id="miScope">
        ${cardTitle("🧭", "情报范围")}
        <p class="muted">每份日报覆盖 8 个板块，每条情报都可溯源（数据来源与核验）：</p>
        <ul class="wb-files">${SECTIONS.map((s) => `<li>· ${esc(s)}</li>`).join("")}</ul>
      </div>
    </div>
    <div class="card" id="miEditor" hidden></div>
    <div class="card" id="miProgress" hidden>
      <div class="recon-prog-title">跨境市场情报日报生成中…</div>
      <div id="miFeed" class="recon-feed"></div>
    </div>
  </div>`;

  const $ = (id) => el.querySelector("#" + id);
  let pollTimer = null;
  let watchlist = null;
  let isPro = false;

  // Fetch dashboard data (watchlist, latest report, plan) in parallel.
  const [wlRes, histRes, plansRes] = await Promise.allSettled([
    api.getMarketIntelWatchlist(), api.getMarketIntelHistory(), api.getPlans(),
  ]);
  watchlist = (wlRes.value && wlRes.value.watchlist) || {};
  const history = (histRes.value && histRes.value.reports) || [];
  const plans = (plansRes.value && plansRes.value) || {};
  const curCode = (plans.current && plans.current.plan_code) || "free";
  const curRow = (plans.plans || []).find((p) => p.code === curCode) || {};
  isPro = ((curRow.features) || []).includes("market_intel");
  const hasWatch = !!(watchlist && (watchlist.platforms || []).length);

  // ---- ⏰ 下次送达 ----------------------------------------------------------
  $("miNext").innerHTML = isPro
    ? `${cardTitle("⏰", "下次送达")}
       <div class="wb-count-sub">每天早上 <strong>08:00</strong> 自动送到企业微信 / 邮箱。</div>
       <div class="wb-links"><a href="/markets/settings" data-nav>管理送达渠道 →</a></div>`
    : `${cardTitle("⏰", "下次送达")}
       <p class="muted">升级企业版后，每天早 8 点自动把当日市场情报送到企业微信和邮箱 — 你什么都不用做。</p>
       <div class="wb-card-cta"><button type="button" class="ghost-btn" data-go="/account">查看套餐与升级</button></div>`;

  // ---- 📄 今日日报 ----------------------------------------------------------
  const latest = history[0];
  if (!latest) {
    $("miLatest").innerHTML = `${cardTitle("📄", "今日日报")}
      <p class="muted">${hasWatch ? "今天还没有日报。点「立即生成今日日报」马上看一份。" : "先设置关注列表，之后每天自动生成。"}</p>`;
  } else {
    const noNews = (latest.highlight_count || 0) === 0 && (latest.kept_count || 0) === 0;
    $("miLatest").innerHTML = `${cardTitle("📄", "今日日报")}
      <div class="wb-report-title">${esc(latest.period_label || ("日报 #" + latest.run_id))}</div>
      <div class="wb-report-meta">
        <span>${noNews ? "今日无重大情报" : `要点 ${esc(latest.highlight_count || 0)} · 截止 ${esc(latest.deadline_count || 0)}`}</span>
      </div>
      <div class="wb-links"><a href="${BASE}/${esc(latest.run_id)}" data-nav>查看 →</a>
        <a href="${BASE}" data-nav>全部历史 →</a></div>`;
  }

  // ---- 🎯 关注列表 ----------------------------------------------------------
  function watchSummary() {
    const chip = (label, vals) => (vals && vals.length)
      ? `<span class="chip">${esc(label)}：${esc(vals.join("、"))}</span>` : "";
    const chips = [chip("平台", watchlist.platforms), chip("市场", watchlist.markets),
      (watchlist.category ? `<span class="chip">类目：${esc(watchlist.category)}</span>` : ""),
      chip("竞品", watchlist.competitors)].filter(Boolean).join(" ");
    $("miWatch").innerHTML = `${cardTitle("🎯", "关注列表")}
      ${hasWatch ? `<div class="chip-row">${chips}</div>` : `<p class="muted">还没有设置关注的平台、市场与类目。</p>`}
      <div class="wb-card-cta"><button type="button" class="ghost-btn" id="miEditBtn">${hasWatch ? "编辑关注列表" : "设置关注列表"}</button></div>`;
    const b = $("miEditBtn");
    if (b) b.addEventListener("click", () => toggleEditor(true));
  }
  watchSummary();

  // ---- 📊 订阅 --------------------------------------------------------------
  $("miPlan").innerHTML = isPro
    ? `${cardTitle("📊", "订阅")}
       <p class="muted">企业版 · 跨境市场情报日报已开通 ✓ — 每日自动送达，不计入报告份额。</p>
       <div class="wb-links"><a href="/account" data-nav>账户与套餐 →</a></div>`
    : `${cardTitle("📊", "订阅")}
       <p class="muted">跨境市场情报日报是企业版功能。升级后每天自动送达，且不占用报告份额。</p>
       <div class="wb-card-cta"><button type="button" class="ghost-btn" data-go="/account">升级到企业版</button></div>`;

  // ---- inline 关注列表 editor ----------------------------------------------
  function renderEditor() {
    const val = (k) => esc((Array.isArray(watchlist[k]) ? watchlist[k].join(", ") : (watchlist[k] || "")));
    const fields = LIST_FIELDS.map(([k, label, ph]) => `
      <label class="field">
        <span class="field-label">${esc(label)}</span>
        <input id="mi_${k}" class="field-input" type="text" value="${val(k)}" placeholder="${esc(ph)}">
      </label>`).join("");
    $("miEditor").innerHTML = `${cardTitle("🎯", "编辑关注列表")}
      <p class="muted">日报的每个板块都会按这里的设置过滤。</p>
      ${fields}
      <label class="field">
        <span class="field-label">主营类目</span>
        <input id="mi_category" class="field-input" type="text" value="${esc(watchlist.category || "")}" placeholder="例如 家居、3C、服饰">
      </label>
      <div class="report-actions">
        <button type="button" id="miSave" class="run-btn">保存</button>
        <button type="button" id="miCancel" class="ghost-btn">收起</button>
      </div>
      <p class="form-error" id="miErr" hidden></p>
      <p class="muted" id="miMsg" hidden></p>`;
    $("miSave").addEventListener("click", saveWatchlist);
    $("miCancel").addEventListener("click", () => toggleEditor(false));
  }
  function toggleEditor(open) {
    $("miEditor").hidden = !open;
    if (open) { renderEditor(); $("miEditor").scrollIntoView({ behavior: "smooth", block: "center" }); }
  }
  const collect = () => {
    const body = { category: ($("mi_category").value || "").trim() };
    for (const [k] of LIST_FIELDS) {
      body[k] = ($("mi_" + k).value || "").split(",").map((s) => s.trim()).filter(Boolean);
    }
    return body;
  };
  async function saveWatchlist() {
    $("miErr").hidden = true; $("miMsg").hidden = true;
    const btn = $("miSave"); btn.disabled = true; btn.textContent = "保存中…";
    try {
      const res = await api.saveMarketIntelWatchlist(collect());
      watchlist = res.watchlist || collect();
      $("miMsg").hidden = false; $("miMsg").textContent = "关注列表已保存 ✓";
      watchSummary();
    } catch (ex) { $("miErr").hidden = false; $("miErr").textContent = ex.message; }
    btn.disabled = false; btn.textContent = "保存";
  }
  if (!hasWatch) toggleEditor(true);   // first-time setup: open the editor right away

  // ---- 立即生成 -------------------------------------------------------------
  $("miGen").addEventListener("click", async () => {
    if (!isPro) { ctx.navigate("/account"); return; }
    if (!hasWatch) { toggleEditor(true); return; }
    const btn = $("miGen"); btn.disabled = true; btn.textContent = "生成中…";
    try {
      const res = await api.startMarketIntel();
      $("miProgress").hidden = false;
      pollTimer = pollJobStatus(res.job_id, $("miFeed"), async (ok) => {
        if (!ok) { btn.disabled = false; btn.textContent = "重新生成"; return; }
        let runId = res.run_id;
        if (!runId) {
          // Run succeeded but the run-row id wasn't returned (e.g. a transient
          // company-brain hiccup). The report still exists — recover its id from history.
          try {
            const h = await api.getMarketIntelHistory();
            runId = ((h.reports || [])[0] || {}).run_id || null;
          } catch (_) { /* handled just below */ }
        }
        if (runId) { ctx.navigate(`${BASE}/${runId}`); return; }
        // Couldn't recover an id, but the run DID succeed — surface that instead of a
        // misleading "重新生成" retry button on a report that actually exists.
        $("miProgress").insertAdjacentHTML("beforeend",
          `<p class="muted">✓ 今日日报已生成，请在「全部历史」中查看。</p>`);
        btn.disabled = false; btn.textContent = "立即生成今日日报";
      }, { errorText: "生成失败。" });
    } catch (ex) {
      btn.disabled = false; btn.textContent = "立即生成今日日报";
      const code = ex.status;
      const msg = (ex.data && ex.data.error) || ex.message;
      if (code === 400) { toggleEditor(true); }      // no watchlist
      else { alert(msg); }
    }
  });

  // delegated [data-go] navigation (mirrors workbench). el is the PERSISTENT #view node
  // (main.js only swaps its innerHTML, never the node), so this listener MUST be removed
  // on unmount — otherwise it accumulates one handler per visit to /markets.
  const onGo = (e) => {
    const go = e.target.closest("[data-go]");
    if (go) ctx.navigate(go.dataset.go);
  };
  el.addEventListener("click", onGo);

  return () => {
    if (pollTimer) clearInterval(pollTimer);
    el.removeEventListener("click", onGo);
  };
}

// ---- 送达设置 (market-intel's OWN delivery subpage; cadence is fixed daily 08:00) --------
async function mountSettings(el, ctx) {
  el.innerHTML = `<div class="page page-narrow"><div class="card">${skeleton({ title: true, lines: 5 })}</div></div>`;

  const [dRes, plansRes] = await Promise.allSettled([api.getMarketIntelDelivery(), api.getPlans()]);
  const d = (dRes.value && dRes.value.delivery) || {};
  const plans = plansRes.value || {};
  const curCode = (plans.current && plans.current.plan_code) || "free";
  const curRow = (plans.plans || []).find((p) => p.code === curCode) || {};
  const isPro = ((curRow.features) || []).includes("market_intel");

  const channels = d.channels || ["email"];

  el.innerHTML = `<div class="page page-narrow">
    <header class="page-head">
      <a class="back-link" href="${BASE}" data-nav>← 跨境市场情报日报</a>
      <h1>送达设置</h1>
      <p class="muted">日报每天早上 08:00 自动生成 — 这里只需设置「送到哪里」。</p>
    </header>
    ${isPro ? "" : `<div class="audit-banner"><div class="ab-title">企业版功能</div>
       <p>升级到企业版后即可开启每日自动送达与多渠道推送。<a href="/account" data-nav>查看套餐 →</a></p></div>`}

    <div class="card">
      <h3 class="card-title">送达渠道</h3>
      <label class="mi-check-row"><input type="checkbox" id="dEmail" ${channels.includes("email") ? "checked" : ""}>
        邮箱 <span class="muted">${esc(d.email || "")}</span></label>
      <label class="mi-check-row"><input type="checkbox" id="dWechat" ${channels.includes("wechat") ? "checked" : ""}>
        企业微信群机器人</label>
      <label class="field">
        <span class="field-label">企业微信 Webhook（群机器人地址）</span>
        <input id="dHook" class="field-input" type="text" value="${esc(d.wechat_webhook || "")}"
               placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…">
      </label>
      <div class="report-actions">
        <button type="button" id="dTestEmail" class="ghost-btn">测试邮箱</button>
        <button type="button" id="dTestWechat" class="ghost-btn">测试企业微信</button>
      </div>
    </div>

    <div class="card">
      <h3 class="card-title">每日推送</h3>
      <label class="mi-check-row"><input type="checkbox" id="dPaused" ${d.paused ? "checked" : ""}>
        暂停每日推送 <span class="muted">勾选后不再自动生成与送达，随时可恢复。</span></label>
    </div>

    <div class="report-actions">
      <button type="button" id="dSave" class="run-btn" ${isPro ? "" : "disabled"}>保存</button>
      <p class="form-error" id="dErr" hidden></p>
      <p class="muted" id="dMsg" hidden></p>
    </div>
  </div>`;

  const $ = (id) => el.querySelector("#" + id);
  const collect = () => {
    const chans = [];
    if ($("dEmail").checked) chans.push("email");
    if ($("dWechat").checked) chans.push("wechat");
    return { channels: chans, wechat_webhook: ($("dHook").value || "").trim(), paused: $("dPaused").checked };
  };
  $("dSave").addEventListener("click", async () => {
    $("dErr").hidden = true; $("dMsg").hidden = true;
    const btn = $("dSave"); btn.disabled = true; btn.textContent = "保存中…";
    try {
      await api.saveMarketIntelDelivery(collect());
      $("dMsg").hidden = false; $("dMsg").textContent = "送达设置已保存 ✓";
    } catch (ex) {
      $("dErr").hidden = false; $("dErr").textContent = (ex.data && ex.data.error) || ex.message;
    }
    btn.disabled = false; btn.textContent = "保存";
  });
  const test = async (channel, btnId) => {
    const b = $(btnId); const label = b.textContent; b.disabled = true; b.textContent = "发送中…";
    try { const r = await api.deliveryTest(channel); b.textContent = r.ok ? "已发送 ✓" : "失败，请先保存"; }
    catch (ex) { b.textContent = "失败"; }
    setTimeout(() => { b.disabled = false; b.textContent = label; }, 2500);
  };
  $("dTestEmail").addEventListener("click", () => test("email", "dTestEmail"));
  $("dTestWechat").addEventListener("click", () => test("wechat", "dTestWechat"));
}

// ---- result view (the 8-section report) -----------------------------------
async function mountResult(el, ctx, runId) {
  el.innerHTML = `<div class="page page-narrow"><div class="card">${skeleton({ title: true, lines: 6 })}</div></div>`;

  let r;
  try { r = await api.getMarketIntelResult(runId); } catch (e) {
    el.innerHTML = `<div class="page page-narrow"><div class="empty">
        <div class="empty-icon">📄</div>
        <div class="empty-title">跨境市场情报日报 #${esc(runId)}</div>
        <p class="empty-lead">${esc((e.data && e.data.error) || e.message)}</p>
        <button type="button" class="ghost-btn" id="miBack">返回</button>
      </div></div>`;
    const b = el.querySelector("#miBack");
    if (b) b.addEventListener("click", () => ctx.navigate(BASE));
    return;
  }

  const noNews = (r.highlight_count || 0) === 0 && (r.kept_count || 0) === 0;
  const cards = [
    ["覆盖", r.period_label || "—"],
    ["今日要点", noNews ? "0" : String(r.highlight_count ?? "—")],
    ["截止事项", String(r.deadline_count ?? "—")],
  ].map(([k, v]) =>
    `<div class="recon-card"><div class="rc-k">${esc(k)}</div><div class="rc-v">${esc(v)}</div></div>`).join("");

  // A jump-nav over the 8 sections (easy navigation for a long report) — reuses the
  // existing in-page tab-strip style (.wf-tabs/.wf-tab), no new CSS.
  const nav = SECTIONS.map((s) =>
    `<button type="button" class="wf-tab" data-sec="${esc(s)}">${esc(s)}</button>`).join("");

  el.innerHTML = `<div class="page page-narrow recon-result">
    <header class="page-head">
      <a class="back-link" href="${BASE}" data-nav>← 跨境市场情报日报</a>
      <h1>跨境市场情报日报</h1>
      <p class="muted">${esc(r.period_label || "")} · ${statusPillHTML(r.status)}</p>
    </header>

    <div class="recon-cards">${cards}</div>
    ${noNews ? `<div class="audit-banner"><div class="ab-title">今日无重大情报</div>
       <p>系统已按发布日期核验，今日你的关注范围内没有新的市场情报。明早 8 点会继续为你盯。</p></div>` : ""}

    <div class="wf-tabs" id="miNav">${nav}</div>

    <div class="card recon-report-body" id="miBody">${r.final_report
      ? renderMarkdown(r.final_report, { anchors: true })
      : `<p class="muted">报告正文尚未生成。</p>`}
    </div>

    <div class="report-actions">
      ${r.download_url ? `<a class="run-btn dl-btn" href="${esc(r.download_url)}">⬇ 下载 PDF</a>` : ""}
    </div>
  </div>`;

  // Jump-nav: scroll to the section whose <h1/h2> text matches.
  el.querySelector("#miNav").addEventListener("click", (e) => {
    const chip = e.target.closest(".wf-tab");
    if (!chip) return;
    const want = chip.dataset.sec;
    const head = [...el.querySelectorAll("#miBody h1, #miBody h2")]
      .find((h) => h.textContent.trim() === want);
    if (head) head.scrollIntoView({ behavior: "smooth", block: "start" });
  });
}
