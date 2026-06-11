// ===========================================================================
// founder/costs.js — 成本控制台 (the old Token Manager · CFO, ported as-is,
// dark scope). Internal COGS protection: overall ¥ budget, per-task budgets,
// spend counters. Customers never see this — their promise is the flat plan
// (“成本透明，绝不超支”).
// ===========================================================================
import * as api from "../api.js";
import { esc, cny } from "../components.js";
import { founderFrame, requireAdmin } from "./common.js";

let tokens = null;     // latest /api/tokens payload
let pollTimer = null;

export async function mount(el) {
  if (!requireAdmin(el)) return;
  el.innerHTML = founderFrame(`<p class="muted">加载预算数据…</p>`);
  await refresh(el);
  // Keep the meters live while the view is open (same cadence as the old drawer).
  pollTimer = setInterval(() => refresh(el, true), 3000);
  return () => { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } };
}

async function refresh(el, passive = false) {
  try {
    tokens = await api.getTokens();
  } catch (e) {
    if (!passive) el.innerHTML = founderFrame(`<p class="form-error">无法加载预算数据：${esc(e.message)}</p>`);
    return;
  }
  // Don't re-render out from under a field mid-type.
  const ae = document.activeElement;
  if (passive && ae && el.contains(ae) && /^(INPUT|SELECT|TEXTAREA)$/.test(ae.tagName)) return;
  render(el);
}

function render(el) {
  el.innerHTML = founderFrame(bodyHTML());
  const totalForm = el.querySelector("#dTotalForm");
  if (totalForm) totalForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const v = el.querySelector("#dTotalInput").value.trim();
    await post(el, () => api.setTokensTotal(v === "" ? 0 : Number(v)));
  });
  const budgetForm = el.querySelector("#dBudgetForm");
  if (budgetForm) budgetForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = el.querySelector("#dBudgetName").value.trim();
    const limit = el.querySelector("#dBudgetLimit").value.trim();
    if (!name) return;
    await post(el, () => api.setTokensBudget(name, limit === "" ? 0 : Number(limit)));
  });
  el.querySelectorAll(".budget-rm").forEach((btn) =>
    btn.addEventListener("click", () => {
      if (confirm(`删除「${btn.dataset.name}」预算？`)) {
        post(el, () => api.removeTokensBudget(btn.dataset.name));
      }
    })
  );
  const reset = el.querySelector("#dResetTokens");
  if (reset) reset.addEventListener("click", () => {
    if (confirm("将所有已花费计数清零？（预算上限保留）")) post(el, () => api.resetTokens());
  });
}

async function post(el, fn) {
  try {
    tokens = await fn();
    render(el);
  } catch (e) {
    alert(e.message || "操作失败。");
  }
}

function bodyHTML() {
  const s = tokens;
  if (!s) return `<h2 class="detail-h">成本控制台</h2><p class="muted">加载预算数据…</p>`;
  const t = s.total;
  const overall = (t.unlimited || t.limit == null)
    ? `<div class="tk-overall"><span class="tk-big">${cny(t.used)}</span><span class="tk-of">已花费 · 无上限</span></div>
       <div class="meter"><div class="meter-fill ok" style="width:0%"></div></div>`
    : `<div class="tk-overall"><span class="tk-big">${cny(t.used)}</span><span class="tk-of">/ ${cny(t.limit)}（${t.pct_used}%）</span></div>
       <div class="meter"><div class="meter-fill ${esc(t.state)}" style="width:${Math.min(t.pct_used, 100)}%"></div></div>`;
  const warns = (s.warnings && s.warnings.length)
    ? `<div class="token-warnings" style="margin-top:18px;">${s.warnings.map((w) => `<div>${esc(w)}</div>`).join("")}</div>`
    : "";
  const budgets = s.budgets.length
    ? `<div class="tk-grid">${s.budgets.map(budgetCardHTML).join("")}</div>`
    : `<p class="muted">还没有任务预算，在下方添加一个。</p>`;
  return `
    <h2 class="detail-h">成本控制台</h2>
    <p class="detail-lead">内部 COGS 看板：跟踪并限制整个团队的真实模型花费（¥）。对客户的承诺是固定套餐价 — 这里的数字永远不出现在客户界面。</p>
    <div class="detail-section">
      <div class="block-title">整体预算</div>
      ${overall}
      ${warns}
      <form id="dTotalForm" class="inline-form" style="margin-top:14px;max-width:430px;">
        <input type="number" id="dTotalInput" min="0" step="50" placeholder="设置总预算 ¥（0 = 不限）" />
        <button type="submit">设置</button>
      </form>
    </div>
    <div class="detail-section">
      <div class="block-title">任务预算</div>
      ${budgets}
      <form id="dBudgetForm" class="inline-form stacked" style="max-width:430px;margin-top:14px;">
        <input type="text" id="dBudgetName" placeholder="任务名（如 video_generation）" />
        <div class="inline-form">
          <input type="number" id="dBudgetLimit" min="0" step="50" placeholder="¥ 上限" />
          <button type="submit">添加 / 更新</button>
        </div>
      </form>
      <button id="dResetTokens" class="ghost-btn" style="max-width:430px;margin-top:12px;">清零花费计数</button>
    </div>`;
}

function budgetCardHTML(b) {
  const unlimited = b.unlimited || b.limit == null;
  const limitTxt = unlimited ? "无上限" : cny(b.limit);
  const pct = unlimited ? 0 : Math.min(b.pct_used, 100);
  const cls = unlimited ? "ok" : b.state;
  const badgeTxt = unlimited ? "tracking"
    : b.state === "ok" ? "ok" : b.state === "warning" ? "low" : "cut off";
  return `<div class="budget-item">
    <div class="budget-top">
      <span class="budget-name">${esc(b.name)}</span>
      <span style="display:flex;align-items:center;gap:8px;">
        <span class="budget-badge badge-${esc(cls)}">${badgeTxt}</span>
        <button class="budget-rm" title="删除" data-name="${esc(b.name)}">&times;</button>
      </span>
    </div>
    <div class="budget-vals">${cny(b.used)} / ${limitTxt}</div>
    <div class="meter"><div class="meter-fill ${esc(cls)}" style="width:${pct}%"></div></div>
  </div>`;
}
