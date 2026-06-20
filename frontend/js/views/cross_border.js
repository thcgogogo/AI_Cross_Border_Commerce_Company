// ===========================================================================
// cross_border.js — 跨境财务 (cross-border finance hub).
//
// ONE section that consolidates the formerly-separate 对账 (回款核对+收汇结汇) and
// 应收账款+催收 surfaces behind a 贸易方式 (trade-mode) selector and mode-gated
// sub-tabs. The seller picks their 贸易方式 once (persisted to the company brain);
// that gates which sub-tabs render. The 回款核对 and 账龄+催收 sub-tabs REUSE the
// existing recon.js / ar.js views verbatim (re-homed via their `base` path) — the
// engines and their endpoints are untouched. The remaining facets (三流合一, 发票
// 审核, 退税, 无票免税) land in later phases and show a phase-gated preview for now.
//
// Customer-safe throughout: this view only calls /api/cross-border/settings plus
// the customer-safe /api/recon|ar/* endpoints — no models, tokens, or ¥ COGS.
// ===========================================================================
import * as api from "../api.js";
import { esc, tabsHTML } from "../components.js";
import * as recon from "./recon.js";
import * as ar from "./ar.js";
import * as cbEngine from "./cb_engine.js";

// 贸易方式 options (value mirrors metrics_store.CROSS_BORDER_MODES).
const MODES = [
  { key: "9810", label: "9810 海外仓" },
  { key: "general", label: "一般贸易" },
  { key: "9710", label: "9710 B2B" },
  { key: "9610", label: "9610 综试区零售" },
  { key: "1210", label: "1210 保税" },
];

// Every facet of the hub. `recon` and `ar` are live (reuse the existing views);
// the rest are phase-gated previews until their engines ship.
const SUBTABS = {
  recon:     { label: "回款核对 + 收汇结汇", live: true },
  threeflow: { label: "三流合一核验", live: false },
  rebate:    { label: "退税核算", live: false },
  vatexempt: { label: "无票免税 + 核定征收", live: false },
  invoice:   { label: "发票审核", live: false },
  ar:        { label: "账龄 + 催收", live: true },
};

// 贸易方式 → the ordered sub-tabs that render for that mode (plan §3 gating).
// 回款核对 + 三流合一 are universal; the tax/AR facet is mode-specific.
const GATING = {
  "9810":    ["recon", "threeflow", "rebate", "invoice"],
  "general": ["recon", "threeflow", "rebate", "invoice"],
  "9710":    ["recon", "threeflow", "ar", "invoice"],
  "9610":    ["recon", "threeflow", "vatexempt", "invoice"],
  "1210":    ["recon", "threeflow", "invoice"],
};
const DEFAULT_MODE = "9810";   // 海外仓 / Amazon FBA — the first customer
const DEFAULT_TAB = "recon";

// ---------------------------------------------------------------------------
export async function mount(el, ctx) {
  const params = ctx.params || {};
  const activeTab = SUBTABS[params.tab] ? params.tab : DEFAULT_TAB;
  const runId = params.runId || null;

  // The seller's 贸易方式 lives in the company brain; default to 9810 海外仓.
  let mode = DEFAULT_MODE;
  try {
    const s = (await api.getCrossBorderSettings()).settings || {};
    if (s.mode) mode = s.mode;
  } catch (e) { /* unavailable — fall back to the default mode */ }

  el.innerHTML = `<div class="cbf-topbar page-narrow">
    <div class="cbf-head">
      <div class="cbf-head-text">
        <h1 class="cbf-title">跨境财务</h1>
        <p class="cbf-lead">出口跨境电商的钱、票、税一站核对：回款是否到账结汇、单证是否合规、税务怎么处理 — 每个数字可溯源。</p>
      </div>
      <label class="cbf-mode">
        <span class="cbf-mode-label">贸易方式</span>
        <select id="cbfMode" class="field-input">
          ${MODES.map((m) =>
            `<option value="${esc(m.key)}" ${m.key === mode ? "selected" : ""}>${esc(m.label)}</option>`).join("")}
        </select>
      </label>
    </div>
    <div id="cbfTabs" class="cbf-tabs"></div>
  </div>
  <div id="cbfBody"></div>`;

  const tabsHost = el.querySelector("#cbfTabs");
  const bodyHost = el.querySelector("#cbfBody");

  const renderTabs = (m) => {
    const keys = tabKeysFor(m, activeTab);
    tabsHost.innerHTML = tabsHTML(keys.map((k) => ({ key: k, label: SUBTABS[k].label })), activeTab);
  };
  renderTabs(mode);

  // Tab click → navigate to that sub-tab (the router re-mounts this view).
  tabsHost.addEventListener("click", (e) => {
    const btn = e.target.closest(".tab");
    if (!btn) return;
    const key = btn.dataset.tab;
    if (key && key !== activeTab) ctx.navigate(`/cross-border/${key}`);
  });

  // 贸易方式 change → persist to the brain, re-gate the tab bar; if the active tab
  // is no longer in the new mode's set, fall back to 回款核对.
  el.querySelector("#cbfMode").addEventListener("change", async (e) => {
    const newMode = e.target.value;
    try { await api.saveCrossBorderSettings({ mode: newMode }); } catch (ex) { /* best-effort */ }
    renderTabs(newMode);
    const allowed = GATING[newMode] || GATING[DEFAULT_MODE];
    if (!allowed.includes(activeTab) && activeTab !== DEFAULT_TAB) {
      ctx.navigate(`/cross-border/${DEFAULT_TAB}`);
    }
  });

  // Mount the active sub-tab's body. recon/ar reuse their existing views verbatim,
  // re-homed via `base`; the new evidence engines (threeflow/rebate/invoice/vatexempt)
  // all go through the one config-driven cb_engine view.
  const subCtx = { params: { runId }, navigate: ctx.navigate, base: `/cross-border/${activeTab}` };
  let subCleanup = null;
  if (activeTab === "recon") subCleanup = await recon.mount(bodyHost, subCtx);
  else if (activeTab === "ar") subCleanup = await ar.mount(bodyHost, subCtx);
  else subCleanup = await cbEngine.mount(bodyHost, subCtx, activeTab);

  return () => { if (typeof subCleanup === "function") subCleanup(); };
}

// The mode's gated tab set, with the currently-active tab force-included (so a deep
// link to a tab the mode wouldn't otherwise show — e.g. /cross-border/ar while in
// 9810 — still renders and is selectable), de-duplicated, order preserved.
function tabKeysFor(mode, activeTab) {
  const base = GATING[mode] || GATING[DEFAULT_MODE];
  const keys = base.includes(activeTab) ? base.slice() : [...base, activeTab];
  return keys.filter((k, i) => SUBTABS[k] && keys.indexOf(k) === i);
}
