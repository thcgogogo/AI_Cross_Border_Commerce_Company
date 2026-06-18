// ===========================================================================
// founder/analytics.js — 数据 / 经营分析 (first-party operating data, founder-only).
//
// The head-of-funnel data /admin can't show: web visits, traffic sources, the
// visit→signup→paid acquisition funnel, the anonymous Fusion front door, and top
// industry pack / specialist. Fed by GET /api/analytics (server-side _is_admin gate,
// 403 otherwise). No chart lib — inline .meter bars + small tables, dark scope.
// ===========================================================================
import * as api from "../api.js";
import { esc } from "../components.js";
import { founderFrame, requireAdmin } from "./common.js";

export async function mount(el) {
  if (!requireAdmin(el)) return;
  el.innerHTML = founderFrame(`<p class="muted">加载经营数据…</p>`);
  let d;
  try {
    d = await api.getAnalytics();
  } catch (e) {
    el.innerHTML = founderFrame(`<p class="form-error">无法加载经营数据：${esc(e.message)}</p>`);
    return;
  }
  el.innerHTML = founderFrame(bodyHTML(d));
  return () => {};
}

const pct = (n, max) => (max > 0 ? Math.round((n / max) * 100) : 0);

function meterRow(label, count, max, sub) {
  return `<div class="an-row">
    <div class="an-row-head"><span>${esc(label)}</span><span class="an-row-n">${esc(count)}${sub ? ` <span class="muted">${esc(sub)}</span>` : ""}</span></div>
    <div class="meter"><div class="meter-fill ok" style="width:${pct(count, max)}%"></div></div>
  </div>`;
}

function funnelHTML(funnel) {
  const stages = funnel.stages || [];
  const max = stages.length ? stages[0].count : 0;
  const rows = stages.map((s, i) => {
    const prev = i > 0 ? stages[i - 1].count : null;
    const conv = prev ? `转化 ${Math.round((s.count / prev) * 100)}%` : "";
    return meterRow(s.label, s.count, max, conv);
  }).join("");
  return `<div class="detail-section"><div class="block-title">获取漏斗（近30天 · 匿名前段→注册后段）</div>
    ${rows || `<p class="muted">暂无数据。</p>`}</div>`;
}

function visitsHTML(pv) {
  const days = pv || [];
  const totalV = days.reduce((a, d) => a + d.visits, 0);
  const maxV = days.reduce((a, d) => Math.max(a, d.visits), 0);
  const bars = days.map((d) =>
    `<div class="an-bar" title="${esc(d.day)}: ${d.visits} 次访问 · ${d.sessions} 独立"
       style="height:${pct(d.visits, maxV)}%"></div>`).join("");
  return `<div class="detail-section"><div class="block-title">访问趋势（近30天）</div>
    <div class="an-stat"><span class="an-big">${esc(totalV)}</span><span class="muted">次访问</span></div>
    <div class="an-bars">${bars}</div></div>`;
}

function tableHTML(title, items, keyCol, keyLabel) {
  if (!items || !items.length) return `<div class="detail-section"><div class="block-title">${esc(title)}</div><p class="muted">暂无数据。</p></div>`;
  const max = items[0].count || 0;
  const rows = items.map((it) => `<tr>
      <td>${esc(it[keyCol] || "—")}</td>
      <td class="an-td-bar"><div class="meter"><div class="meter-fill ok" style="width:${pct(it.count, max)}%"></div></div></td>
      <td class="an-td-n">${esc(it.count)}</td>
    </tr>`).join("");
  return `<div class="detail-section"><div class="block-title">${esc(title)}</div>
    <table class="an-table"><thead><tr><th>${esc(keyLabel)}</th><th></th><th>次数</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
}

function bodyHTML(d) {
  const fusion = d.fusion || {};
  const runs = d.runs_breakdown || {};
  const packsAgents = d.packs_agents || {};
  const runsTotal = runs.total || 0;
  return `
    <h2 class="detail-h">数据 / 经营分析</h2>
    <p class="detail-lead">第一方运营数据（自有、不出本机）：访问、来源、获取漏斗与 Fusion 前门使用情况 — /admin 看不到的「注册之前」那段。报告/付费等已有权威表的指标仍以原表为准，这里只补全网页与匿名漏斗。</p>
    ${funnelHTML(d.funnel || {})}
    ${visitsHTML(d.page_views)}
    ${tableHTML("流量来源", d.sources, "source", "来源")}
    <div class="detail-section"><div class="block-title">Fusion 前门（匿名）</div>
      <div class="an-stat"><span class="an-big">${esc(fusion.total || 0)}</span><span class="muted">次提问 · 近30天</span></div>
      ${(fusion.by_model && fusion.by_model.length)
        ? `<table class="an-table"><thead><tr><th>模型</th><th></th><th>被选次数</th></tr></thead><tbody>${
            fusion.by_model.map((m) => `<tr><td>${esc(m.model)}</td><td class="an-td-bar"><div class="meter"><div class="meter-fill ok" style="width:${pct(m.count, fusion.by_model[0].count)}%"></div></div></td><td class="an-td-n">${esc(m.count)}</td></tr>`).join("")
          }</tbody></table>`
        : `<p class="muted">暂无 Fusion 提问。</p>`}
    </div>
    ${tableHTML("热门行业包", packsAgents.packs, "pack", "行业包")}
    ${tableHTML("最常出动的专员", packsAgents.agents, "agent", "专员")}
    <div class="detail-section"><div class="block-title">定时 vs 手动（报告运行 · 近30天）</div>
      <div class="an-split">
        <div><span class="an-big">${esc(runs.scheduled || 0)}</span><span class="muted">定时</span></div>
        <div><span class="an-big">${esc(runs.manual || 0)}</span><span class="muted">手动</span></div>
        <div><span class="an-big">${esc(runsTotal)}</span><span class="muted">合计</span></div>
      </div>
    </div>`;
}
